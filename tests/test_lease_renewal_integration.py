from devflow_ai.services import AdmissionRecord
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

import devflow_ai.coordinator as coordinator_module
import devflow_ai.services as services_module
from devflow_ai.config import LeaseConfig
from devflow_ai.coordinator import LeaseRenewal, RunCoordinator, RunExecutionDeniedError
from devflow_ai.database import get_database_time
from devflow_ai.enums import RunStatus, TaskStatus
from devflow_ai.models import Run, Task
from devflow_ai.repositories import RunRepository
from devflow_ai.services import TaskService


def create_running_run(session_factory, *, max_duration=1800):
    with session_factory() as session:
        task, _ = TaskService(session).create_task(
            title="Lease renewal test", description="Check execution rights and renewal",
            repo="example-repo", idempotency_key=uuid4(),
        )
        task_id = task.id
    with session_factory() as session:
        # Test fixture: choose the saved Policy before admission and Run creation.
        task = session.get(Task, task_id)
        snapshot = dict(task.policy_snapshot)
        snapshot["budget"] = dict(snapshot["budget"], max_run_duration_seconds=max_duration)
        task.policy_snapshot = snapshot
        session.commit()
    with session_factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
    with session_factory() as session:
        run, created = RunCoordinator(session).start_initial_run(task_id, "worker-A", "initial-run")
        assert created
        return run.id


def read_state(session_factory, run_id):
    with session_factory() as session:
        run = session.get(Run, run_id)
        task = session.get(Task, run.task_id)
        return (
            {column.name: getattr(run, column.name) for column in Run.__table__.columns},
            {column.name: getattr(task, column.name) for column in Task.__table__.columns},
        )


@pytest.mark.parametrize("lose_authority", [False, True])
def test_tool_call_releases_lock_during_execution_and_rechecks_before_save(
    run_session_factory, lose_authority,
):
    run_id = create_running_run(run_session_factory)
    task_id = read_state(run_session_factory, run_id)[0]["task_id"]
    call_id = uuid4()
    saved = []
    with run_session_factory() as session:
        def record_start(transaction_session):
            # Use a fixture-only marker in lieu of the future ToolCall table.
            transaction_session.get(Task, task_id).title = "initial tool record"
            return call_id

        def execute(received_id):
            assert received_id == call_id
            assert not session.in_transaction()
            with run_session_factory.begin() as other:
                assert other.get(Task, task_id).title == "initial tool record"
                run = other.scalar(select(Run).where(Run.id == run_id).with_for_update(nowait=True))
                if lose_authority:
                    run.lease_expires_at = get_database_time(other)
            return "tool output"

        def record_result(transaction_session, received_id, output):
            assert transaction_session.in_transaction()
            saved.append((received_id, output))
            transaction_session.get(Task, task_id).title = output

        coordinator = RunCoordinator(session)
        callbacks = dict(record_start=record_start, execute=execute, record_result=record_result)
        if lose_authority:
            with pytest.raises(RunExecutionDeniedError):
                coordinator.execute_tool_call(run_id, "worker-A", **callbacks)
        else:
            assert coordinator.execute_tool_call(run_id, "worker-A", **callbacks) == call_id
        assert not session.in_transaction()
    assert saved == ([] if lose_authority else [(call_id, "tool output")])
    assert read_state(run_session_factory, run_id)[1]["title"] == (
        "initial tool record" if lose_authority else "tool output"
    )


def test_execution_right_requires_caller_transaction():
    with Session() as session:
        with pytest.raises(RuntimeError, match="caller-owned transaction"):
            RunCoordinator(session).assert_execution_right(uuid4(), "worker-A")
        assert not session.in_transaction()


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_execution_right_keeps_run_locked_until_caller_finishes(run_session_factory, finish):
    run_id = create_running_run(run_session_factory)
    original = read_state(run_session_factory, run_id)
    lock_run = select(Run.id).where(Run.id == run_id).with_for_update(nowait=True)
    with run_session_factory() as session:
        transaction = session.begin()
        RunCoordinator(session).assert_execution_right(run_id, "worker-A")
        assert session.get_transaction() is transaction
        with run_session_factory() as contender:
            with pytest.raises(DBAPIError) as exc_info:
                contender.scalar(lock_run)
            assert exc_info.value.orig.sqlstate == "55P03"
        getattr(transaction, finish)()
        with run_session_factory() as contender:
            assert contender.scalar(lock_run) == run_id
    assert read_state(run_session_factory, run_id) == original


@pytest.mark.parametrize("condition", ["wrong-owner", "terminal", "expired", "duration"])
def test_execution_right_rejects_ineligible_run_in_caller_transaction(run_session_factory, condition):
    run_id = create_running_run(run_session_factory, max_duration=60)
    with run_session_factory.begin() as session:
        run = session.get(Run, run_id)
        now = get_database_time(session)
        if condition == "terminal":
            run.status = RunStatus.FAILED
            run.finished_at = now
        elif condition == "expired":
            run.lease_expires_at = now
        elif condition == "duration":
            run.started_at = now - timedelta(seconds=60)
    worker_id = "worker-B" if condition == "wrong-owner" else "worker-A"
    with run_session_factory() as session:
        with pytest.raises(RunExecutionDeniedError), session.begin():
            RunCoordinator(session).assert_execution_right(run_id, worker_id)
        assert not session.in_transaction()


def test_renewal_commits_times_and_returns_receipt_usable_after_session_close(run_session_factory):
    run_id = create_running_run(run_session_factory)
    with run_session_factory() as session:
        now = get_database_time(session)
        session.get(Run, run_id).lease_expires_at = now + timedelta(seconds=5)
        session.commit()
    original_run, original_task = read_state(run_session_factory, run_id)
    with run_session_factory() as session:
        before = get_database_time(session)
    with run_session_factory() as session:
        config = LeaseConfig(heartbeat_interval_seconds=2, lease_duration_seconds=20)
        receipt = RunCoordinator(session, config).renew_lease(run_id, "worker-A")
        assert not session.in_transaction()
    assert isinstance(receipt, LeaseRenewal)
    assert receipt.run_id == run_id and receipt.worker_id == "worker-A"
    assert receipt.model_dump(mode="json")["lease_expires_at"].endswith("Z")
    assert str(run_id) in receipt.model_dump_json()
    stored_run, stored_task = read_state(run_session_factory, run_id)
    with run_session_factory() as session:
        after = get_database_time(session)
        assert session.scalars(select(Run.id)).all() == [run_id]
    assert stored_task == original_task
    assert before <= receipt.last_heartbeat_at <= after
    assert before + timedelta(seconds=20) <= receipt.lease_expires_at <= after + timedelta(seconds=20)
    assert stored_run["last_heartbeat_at"] == receipt.last_heartbeat_at
    assert stored_run["lease_expires_at"] == receipt.lease_expires_at > original_run["lease_expires_at"]
    for field in original_run.keys() - {"last_heartbeat_at", "lease_expires_at", "updated_at"}:
        assert stored_run[field] == original_run[field]


@pytest.mark.parametrize("condition", ["missing", "owner", "FAILED", "PASSED", "expired", "duration"])
def test_ineligible_renewal_is_denied_without_changing_records(run_session_factory, condition):
    run_id = create_running_run(run_session_factory, max_duration=60)
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        now = get_database_time(session)
        if condition in ("FAILED", "PASSED"):
            run.status = RunStatus(condition)
            run.finished_at = now
        elif condition == "expired":
            run.lease_expires_at = now - timedelta(seconds=1)
        elif condition == "duration":
            # Future lease alone must not bypass the fixed Run duration budget.
            run.started_at = now - timedelta(seconds=61)
            run.lease_expires_at = now + timedelta(seconds=30)
        session.commit()
    original = read_state(run_session_factory, run_id)
    requested_run = uuid4() if condition == "missing" else run_id
    worker_id = "worker-B" if condition == "owner" else "worker-A"
    with run_session_factory() as session:
        with pytest.raises(RunExecutionDeniedError):
            RunCoordinator(session).renew_lease(requested_run, worker_id)
        assert not session.in_transaction()
    assert read_state(run_session_factory, run_id) == original


@pytest.mark.parametrize("boundary", ["lease", "duration"])
def test_renewal_rejects_exact_expiration_boundary(run_session_factory, monkeypatch, boundary):
    run_id = create_running_run(run_session_factory, max_duration=60)
    run, _ = read_state(run_session_factory, run_id)
    boundary_time = run["lease_expires_at"] if boundary == "lease" else run["started_at"] + timedelta(seconds=60)
    if boundary == "duration":
        with run_session_factory() as session:
            session.get(Run, run_id).lease_expires_at = boundary_time + timedelta(seconds=30)
            session.commit()
    original = read_state(run_session_factory, run_id)
    monkeypatch.setattr(coordinator_module, "get_database_time", lambda session: boundary_time)
    with run_session_factory() as session:
        with pytest.raises(RunExecutionDeniedError):
            RunCoordinator(session).renew_lease(run_id, "worker-A")
    assert read_state(run_session_factory, run_id) == original


def test_renewal_is_capped_by_saved_policy_without_resetting_start(run_session_factory, monkeypatch):
    run_id = create_running_run(run_session_factory, max_duration=60)
    with run_session_factory() as session:
        now = get_database_time(session)
        run = session.get(Run, run_id)
        run.started_at = now - timedelta(seconds=40)
        run.lease_expires_at = now + timedelta(seconds=10)
        session.commit()
    original_run, original_task = read_state(run_session_factory, run_id)

    def unexpected_default_policy():
        pytest.fail("Renewal must use the Task's saved Policy")

    monkeypatch.setattr(services_module, "build_default_policy", unexpected_default_policy)
    config = LeaseConfig(heartbeat_interval_seconds=2, lease_duration_seconds=120)
    with run_session_factory() as session:
        receipt = RunCoordinator(session, config).renew_lease(run_id, "worker-A")
    stored_run, stored_task = read_state(run_session_factory, run_id)
    assert receipt.lease_expires_at == original_run["started_at"] + timedelta(seconds=60)
    assert receipt.last_heartbeat_at < receipt.lease_expires_at
    assert stored_run["started_at"] == original_run["started_at"]
    assert stored_task == original_task


@pytest.mark.parametrize("corruption", ["snapshot", "version"])
def test_invalid_saved_policy_fails_without_renewing(run_session_factory, corruption):
    run_id = create_running_run(run_session_factory)
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        task = session.get(Task, run.task_id)
        if corruption == "snapshot":
            task.policy_snapshot = {"invalid": True}
        else:
            task.policy_version = "mismatched-version"
        session.commit()
    original = read_state(run_session_factory, run_id)
    with run_session_factory() as session:
        with pytest.raises(ValueError):
            RunCoordinator(session).renew_lease(run_id, "worker-A")
        assert not session.in_transaction()
    assert read_state(run_session_factory, run_id) == original


@pytest.mark.parametrize("worker_id", ["", "   ", None])
def test_renewal_rejects_invalid_worker_before_transaction(run_session_factory, worker_id):
    with run_session_factory() as session:
        with pytest.raises(ValueError, match="worker_id"):
            RunCoordinator(session).renew_lease(uuid4(), worker_id)
        assert not session.in_transaction()


@pytest.mark.parametrize("stage", ["lock", "clock", "update"])
def test_database_errors_propagate_without_becoming_execution_denial(run_session_factory, monkeypatch, stage):
    run_id = create_running_run(run_session_factory)
    original = read_state(run_session_factory, run_id)
    failure = SQLAlchemyError("simulated database operation failure")

    def fail(*args, **kwargs):
        raise failure

    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        if stage == "clock":
            monkeypatch.setattr(coordinator_module, "get_database_time", fail)
        else:
            method = "get_by_id_for_update" if stage == "lock" else "renew_lease_if_owned"
            monkeypatch.setattr(coordinator.run_repository, method, fail)
        with pytest.raises(SQLAlchemyError) as exc_info:
            coordinator.renew_lease(run_id, "worker-A")
        assert exc_info.value is failure
        assert not session.in_transaction()
    assert read_state(run_session_factory, run_id) == original


def test_actual_database_statement_error_is_propagated_and_transaction_rolled_back(run_session_factory, monkeypatch):
    run_id = create_running_run(run_session_factory)
    original = read_state(run_session_factory, run_id)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)

        def invalid_statement(*args, **kwargs):
            session.execute(text("SELECT devflow_missing_test_column"))

        monkeypatch.setattr(coordinator.run_repository, "renew_lease_if_owned", invalid_statement)
        with pytest.raises(ProgrammingError) as exc_info:
            coordinator.renew_lease(run_id, "worker-A")
        assert exc_info.value.orig.sqlstate == "42703"
        assert not session.in_transaction()
    assert read_state(run_session_factory, run_id) == original


@pytest.mark.parametrize("failure_point", ["after_update", "before_commit"])
def test_failure_after_update_rolls_back_renewal_and_returns_no_receipt(run_session_factory, monkeypatch, failure_point):
    run_id = create_running_run(run_session_factory)
    original = read_state(run_session_factory, run_id)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        if failure_point == "after_update":
            original_update = coordinator.run_repository.renew_lease_if_owned

            def update_then_fail(*args, **kwargs):
                assert original_update(*args, **kwargs) is not None
                raise RuntimeError("simulated failure after lease update")

            monkeypatch.setattr(coordinator.run_repository, "renew_lease_if_owned", update_then_fail)
        else:
            def fail_commit(session):
                refreshed_run = session.get(Run, run_id, populate_existing=True)
                assert refreshed_run.last_heartbeat_at > original[0]["last_heartbeat_at"]
                raise RuntimeError("simulated failure before lease commit")

            event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="simulated failure"):
            coordinator.renew_lease(run_id, "worker-A")
        assert not session.in_transaction()
    assert read_state(run_session_factory, run_id) == original


@pytest.mark.parametrize("expired_bound", ["lease", "duration"])
def test_update_time_guard_rejects_even_if_earlier_time_sample_appeared_valid(run_session_factory, monkeypatch, expired_bound):
    run_id = create_running_run(run_session_factory, max_duration=60)
    with run_session_factory() as session:
        now = get_database_time(session)
        run = session.get(Run, run_id)
        if expired_bound == "lease":
            run.started_at = now - timedelta(seconds=10)
            run.lease_expires_at = now - timedelta(seconds=1)
        else:
            run.started_at = now - timedelta(seconds=61)
            run.lease_expires_at = now + timedelta(seconds=20)
        session.commit()
    original = read_state(run_session_factory, run_id)
    # Model a pause between obtaining a formerly valid time and executing UPDATE.
    old_time = now - timedelta(seconds=2)
    monkeypatch.setattr(coordinator_module, "get_database_time", lambda session: old_time)
    with run_session_factory() as session:
        with pytest.raises(RunExecutionDeniedError, match="no longer"):
            RunCoordinator(session).renew_lease(run_id, "worker-A")
        assert not session.in_transaction()
    assert read_state(run_session_factory, run_id) == original


@pytest.mark.parametrize("change", ["expiry", "terminal"])
def test_renewal_waits_for_lock_and_checks_time_and_state_after_wait(run_session_factory, monkeypatch, change):
    run_id = create_running_run(run_session_factory)
    waiting_pids = Queue()
    clock_reads = []
    original_clock = coordinator_module.get_database_time

    def observe_clock(session):
        value = original_clock(session)
        clock_reads.append(value)
        return value

    monkeypatch.setattr(coordinator_module, "get_database_time", observe_clock)

    def renew_in_waiting_session():
        with run_session_factory() as session:
            def record_backend(session, transaction, connection):
                connection.execute(text("SET LOCAL statement_timeout = '6s'"))
                waiting_pids.put(connection.scalar(text("SELECT pg_backend_pid()")))

            event.listen(session, "after_begin", record_backend, once=True)
            with pytest.raises(RunExecutionDeniedError):
                RunCoordinator(session).renew_lease(run_id, "worker-A")
            assert not session.in_transaction()

    engine = run_session_factory.kw["bind"]
    with run_session_factory() as holder:
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
        run = RunRepository(holder).get_by_id_for_update(run_id)
        original_heartbeat = run.last_heartbeat_at
        if change == "expiry":
            run.lease_expires_at = get_database_time(holder) + timedelta(milliseconds=300)
        else:
            run.status = RunStatus.FAILED
            run.finished_at = func.now()
        holder.flush()
        saved_expiry = run.lease_expires_at
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(renew_in_waiting_session)
            try:
                waiting_pid = waiting_pids.get(timeout=5)
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                    deadline = monotonic() + 4
                    while holder_pid not in observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid},
                    ):
                        if future.done():
                            future.result()
                            pytest.fail("Renewal did not wait for the held Run lock")
                        assert monotonic() < deadline, "Expected lock wait was not observed"
                        sleep(0.01)
                    assert not clock_reads, "Time must not be sampled before the row lock is acquired"
                    if change == "expiry":
                        while observer.scalar(select(func.clock_timestamp())) < saved_expiry:
                            assert monotonic() < deadline
                            sleep(0.01)
                holder.commit()
                future.result(timeout=7)
            finally:
                holder.rollback()
    stored_run, _ = read_state(run_session_factory, run_id)
    assert stored_run["last_heartbeat_at"] == original_heartbeat
    assert stored_run["lease_expires_at"] == saved_expiry
    if change == "expiry":
        assert clock_reads and clock_reads[0] >= saved_expiry
        assert stored_run["status"] == RunStatus.RUNNING
    else:
        assert stored_run["status"] == RunStatus.FAILED


def test_two_concurrent_renewals_preserve_one_owned_run_and_duration_cap(run_session_factory):
    run_id = create_running_run(run_session_factory)
    original_run, original_task = read_state(run_session_factory, run_id)
    barrier = Barrier(2, timeout=5)

    def renew():
        with run_session_factory() as session:
            barrier.wait()
            return RunCoordinator(session).renew_lease(run_id, "worker-A")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(renew) for _ in range(2)]
        receipts = [future.result(timeout=10) for future in futures]
    stored_run, stored_task = read_state(run_session_factory, run_id)
    latest = max(receipts, key=lambda receipt: receipt.last_heartbeat_at)
    assert stored_run["last_heartbeat_at"] == latest.last_heartbeat_at
    assert stored_run["lease_expires_at"] == latest.lease_expires_at
    assert stored_run["started_at"] == original_run["started_at"]
    assert latest.lease_expires_at <= original_run["started_at"] + timedelta(seconds=1800)
    assert stored_task == original_task
    with run_session_factory() as session:
        assert session.scalars(select(Run.id)).all() == [run_id]
