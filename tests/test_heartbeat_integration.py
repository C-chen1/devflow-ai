from devflow_ai.services import AdmissionRecord
from queue import Queue
from threading import Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from devflow_ai.config import HeartbeatConfig, LeaseConfig
from devflow_ai.coordinator import RunExecutionDeniedError
from devflow_ai.database import get_database_time
from devflow_ai.enums import RunStatus, TaskStatus
from devflow_ai.heartbeat import HeartbeatShutdownError, RunHeartbeat
from devflow_ai.models import Run, Task
from devflow_ai.repositories import RunRepository
from devflow_ai.services import TaskService
from devflow_ai.worker import Worker


def create_claim(session_factory):
    with session_factory() as session:
        task, _ = TaskService(session).create_task(
            title="Heartbeat integration", description="Verify a single Run heartbeat",
            repo="example-repo", idempotency_key=uuid4(),
        )
        task_id = task.id
    with session_factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
    return Worker(session_factory, "heartbeat-worker").claim_once()


def tracking_factory(session_factory):
    sessions = []
    closed = []
    twice_closed = Event()

    class TrackingSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append((self, get_ident()))

        def close(self):
            super().close()
            closed.append((self, get_ident()))
            if len(closed) >= 2:
                twice_closed.set()

    factory = sessionmaker(bind=session_factory.kw["bind"], class_=TrackingSession)
    return factory, sessions, closed, twice_closed


def make_heartbeat(factory, claim, *, statement_timeout=1, shutdown_timeout=3, worker_id=None):
    # All short timings here are test inputs, not new production defaults.
    return RunHeartbeat(
        factory, claim.run_id, worker_id if worker_id is not None else claim.worker_id,
        heartbeat_config=HeartbeatConfig(
            statement_timeout_seconds=statement_timeout, shutdown_timeout_seconds=shutdown_timeout,
        ),
        lease_config=LeaseConfig(heartbeat_interval_seconds=1, lease_duration_seconds=30),
    )


def test_real_heartbeats_use_separate_sessions_while_main_flow_waits(run_session_factory, monkeypatch):
    claim = create_claim(run_session_factory)
    factory, sessions, closed, twice_closed = tracking_factory(run_session_factory)
    with run_session_factory() as session:
        original_task = session.get(Task, claim.task_id)
        policy_version = original_task.policy_version
        policy_snapshot = original_task.policy_snapshot
        original_heartbeat = session.get(Run, claim.run_id).last_heartbeat_at
        original_timeout = session.scalar(text("SHOW statement_timeout"))
    statement_timeouts = []
    original_lock = RunRepository.get_by_id_for_update

    def inspect_timeout(repository, run_id):
        statement_timeouts.append(repository.session.scalar(text("SHOW statement_timeout")))
        return original_lock(repository, run_id)

    monkeypatch.setattr(RunRepository, "get_by_id_for_update", inspect_timeout)
    heartbeat = make_heartbeat(factory, claim)
    with run_session_factory() as main_session:
        # Pending main-flow work must not be committed or rolled back by heartbeats.
        pending_task = Task(
            title="Pending main-flow write", description="Transaction isolation probe", repo="example-repo",
            idempotency_key=uuid4(), policy_version=policy_version, policy_snapshot=policy_snapshot,
        )
        main_session.add(pending_task)
        main_session.flush()
        pending_id = pending_task.id
        with heartbeat:
            # Simulates the main flow waiting on a long tool/model operation.
            assert twice_closed.wait(6)
        assert heartbeat.failure is None and not heartbeat.execution_stop_event.is_set()
        assert not heartbeat.is_alive()
        assert len(sessions) >= 2 and len(closed) == len(sessions)
        assert len({id(session) for session, _ in sessions}) == len(sessions)
        assert {thread_id for _, thread_id in sessions} == {thread_id for _, thread_id in closed}
        assert get_ident() not in {thread_id for _, thread_id in sessions}
        assert all(not session.in_transaction() for session, _ in closed)
        assert all(value in ("1s", "1000ms") for value in statement_timeouts)
        with run_session_factory() as observer:
            assert observer.get(Task, pending_id) is None
            assert observer.scalar(text("SHOW statement_timeout")) == original_timeout
            run = observer.get(Run, claim.run_id)
            assert run.last_heartbeat_at == heartbeat.last_renewal.last_heartbeat_at > original_heartbeat
            assert run.lease_expires_at == heartbeat.last_renewal.lease_expires_at
            assert run.status == RunStatus.RUNNING and run.finished_at is None
        assert main_session.in_transaction()
        main_session.rollback()
    with run_session_factory() as session:
        assert session.get(Task, pending_id) is None
        assert session.scalar(select(func.count()).select_from(Run)) == 1
        assert session.get(Task, claim.task_id).status == TaskStatus.RUNNING


@pytest.mark.parametrize("reason", ["owner", "expired", "terminal"])
def test_real_heartbeat_denial_notifies_owner_and_closes_session(run_session_factory, reason):
    claim = create_claim(run_session_factory)
    with run_session_factory() as session:
        run = session.get(Run, claim.run_id)
        if reason == "expired":
            run.lease_expires_at = get_database_time(session)
        elif reason == "terminal":
            run.status = RunStatus.FAILED
            run.finished_at = get_database_time(session)
        session.commit()
    with run_session_factory() as session:
        run = session.get(Run, claim.run_id)
        original = {column.name: getattr(run, column.name) for column in Run.__table__.columns}
    factory, sessions, closed, _ = tracking_factory(run_session_factory)
    heartbeat = make_heartbeat(factory, claim, worker_id="other-worker" if reason == "owner" else None)
    with pytest.raises(RunExecutionDeniedError) as exc_info:
        with heartbeat:
            assert heartbeat.execution_stop_event.wait(5)
    assert heartbeat.failure is exc_info.value
    assert not heartbeat.is_alive()
    # Every session opened here was closed cleanly. A denial also costs one read to learn
    # whether the Run had already passed, so the count is not assumed to be exactly one.
    assert len(sessions) == len(closed)
    assert all(not session.in_transaction() for session, _ in closed)
    assert not closed[0][0].in_transaction()
    with run_session_factory() as session:
        run = session.get(Run, claim.run_id)
        assert {name: getattr(run, name) for name in original} == original


def test_real_heartbeat_lock_wait_is_bounded_and_database_error_is_preserved(run_session_factory):
    claim = create_claim(run_session_factory)
    factory, sessions, closed, _ = tracking_factory(run_session_factory)
    heartbeat = make_heartbeat(factory, claim)
    with run_session_factory() as holder:
        RunRepository(holder).get_by_id_for_update(claim.run_id)
        with pytest.raises(DBAPIError) as exc_info:
            with heartbeat:
                assert heartbeat.execution_stop_event.wait(6)
        assert exc_info.value.orig.sqlstate == "57014"
        assert heartbeat.failure is exc_info.value
        assert not heartbeat.is_alive()
        assert len(sessions) == len(closed) == 1
        assert not closed[0][0].in_transaction()
        holder.rollback()
    with run_session_factory() as session:
        assert session.get(Run, claim.run_id).status == RunStatus.RUNNING


def test_real_heartbeat_shutdown_timeout_reports_alive_thread_until_lock_released(run_session_factory, monkeypatch):
    claim = create_claim(run_session_factory)
    factory, sessions, closed, _ = tracking_factory(run_session_factory)
    heartbeat = make_heartbeat(factory, claim, statement_timeout=5, shutdown_timeout=1)
    waiting_pids = Queue()
    original_lock = RunRepository.get_by_id_for_update

    def observe_wait(repository, run_id):
        waiting_pids.put(repository.session.scalar(text("SELECT pg_backend_pid()")))
        return original_lock(repository, run_id)

    engine = run_session_factory.kw["bind"]
    with run_session_factory() as holder:
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        original_lock(RunRepository(holder), claim.run_id)
        monkeypatch.setattr(RunRepository, "get_by_id_for_update", observe_wait)
        heartbeat.start()
        try:
            waiting_pid = waiting_pids.get(timeout=5)
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 3
                while holder_pid not in observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid},
                ):
                    assert heartbeat.is_alive()
                    assert monotonic() < deadline, "Heartbeat did not wait for the held row lock"
                    sleep(0.01)
            with pytest.raises(HeartbeatShutdownError):
                heartbeat.stop()
            assert heartbeat.is_alive()
            assert heartbeat.execution_stop_event.is_set()
        finally:
            holder.rollback()
            heartbeat.stop()
    assert not heartbeat.is_alive()
    assert len(sessions) == len(closed) == 1
    with pytest.raises(HeartbeatShutdownError):
        heartbeat.raise_if_failed()


def test_a_heartbeat_stops_quietly_once_its_own_execution_has_passed(run_session_factory):
    """This worker's own commit ends the loop: renewing is meaningless, and it is not an error."""
    claim = create_claim(run_session_factory)
    with run_session_factory() as session:
        run = session.get(Run, claim.run_id)
        # The formal transaction does not change the owner; this worker still holds the Run.
        assert run.worker_id == claim.worker_id
        run.status = RunStatus.PASSED
        run.finished_at = get_database_time(session)
        session.commit()
    factory, sessions, closed, _ = tracking_factory(run_session_factory)
    heartbeat = make_heartbeat(factory, claim)

    with heartbeat:
        deadline = monotonic() + 5
        while heartbeat.is_alive() and monotonic() < deadline:
            sleep(0.05)
        assert not heartbeat.is_alive(), "the loop should end once the Run has passed"

    assert heartbeat.failure is None
    assert heartbeat.last_renewal is None
    # A finished Run does not ask the main flow to stop: its own work is what finished it.
    assert not heartbeat.execution_stop_event.is_set()
    # One session for the renewal attempt and one for reading the Run's status.
    assert len(sessions) == len(closed) == 2


def test_a_pass_by_another_owner_does_not_hide_lost_execution_right(run_session_factory):
    """A replacement owner's pass is not this execution's result: the old owner must stop."""
    claim = create_claim(run_session_factory)
    with run_session_factory() as session:
        run = session.get(Run, claim.run_id)
        run.worker_id = "replacement-worker"
        run.status = RunStatus.PASSED
        run.finished_at = get_database_time(session)
        session.commit()
    heartbeat = make_heartbeat(run_session_factory, claim)
    heartbeat.start()
    try:
        assert heartbeat.execution_stop_event.wait(5), "a previous owner must be stopped"
    finally:
        heartbeat.stop()
    assert isinstance(heartbeat.failure, RunExecutionDeniedError)


def test_a_status_read_that_fails_is_not_taken_as_success(run_session_factory):
    """Only a readable PASSED owned by this worker counts; an unreadable status does not."""
    claim = create_claim(run_session_factory)
    heartbeat = make_heartbeat(run_session_factory, claim)

    def unavailable():
        raise RuntimeError("database unavailable")

    heartbeat.session_factory = unavailable

    assert heartbeat._run_passed_for_this_worker() is False
