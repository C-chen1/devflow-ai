from devflow_ai.services import AdmissionRecord
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

import devflow_ai.coordinator as coordinator_module
import devflow_ai.services as services_module
from devflow_ai.config import LeaseConfig
from devflow_ai.coordinator import RunCoordinator, RunCreationConflictError, TaskClaimBusyError
from devflow_ai.database import get_database_time
from devflow_ai.enums import RunStatus, TaskStatus
from devflow_ai.models import Run, Task
from devflow_ai.policy import build_default_policy
from devflow_ai.repositories import TaskRepository
from devflow_ai.services import TaskNotFoundError, TaskService, TaskStateConflictError


def create_eligible_task(session_factory):
    with session_factory() as session:
        task, _ = TaskService(session).create_task(
            title="Coordinator test", description="Verify atomic initial Run start",
            repo="example-repo", idempotency_key=uuid4(),
        )
        task_id = task.id
    with session_factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
    return task_id


def save_run_state(session_factory, run_id, status):
    # Simulated later lifecycle only; no Supervisor/Verifier/recovery is implemented here.
    with session_factory() as session:
        run = session.get(Run, run_id)
        run.status = status
        run.finished_at = func.now()
        task = session.get(Task, run.task_id)
        task.status = TaskStatus.RETRYING if status == RunStatus.FAILED else TaskStatus.AWAITING_REVIEW
        session.commit()


def start_run(session_factory, task_id, key="creation-key", worker_id="worker-A"):
    with session_factory() as session:
        run, created = RunCoordinator(session).start_initial_run(task_id, worker_id, key)
        assert created is True
        return run.id


def test_initial_start_commits_task_run_and_database_timed_lease(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        task = session.get(Task, task_id)
        original = {name: getattr(task, name) for name in (
            "title", "description", "repo", "policy_version", "policy_snapshot", "created_at",
        )}
        before = get_database_time(session)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        assert coordinator.task_repository.session is coordinator.run_repository.session is session
        run, created = coordinator.start_initial_run(task_id, "worker-A", "creation-key")
        assert created is True
        run_id = run.id
    with run_session_factory() as session:
        after = get_database_time(session)
        task = session.get(Task, task_id)
        run = session.get(Run, run_id)
        assert task.status == TaskStatus.RUNNING
        assert run.status == RunStatus.RUNNING and run.attempt_number == 1
        assert run.worker_id == "worker-A" and run.creation_idempotency_key == "creation-key"
        assert before <= run.started_at <= after
        assert run.started_at.utcoffset() == timedelta(0)
        assert run.last_heartbeat_at == run.started_at
        assert run.lease_expires_at == run.started_at + timedelta(seconds=30)
        assert run.finished_at is None
        assert {name: getattr(task, name) for name in original} == original


@pytest.mark.parametrize("status", list(RunStatus))
def test_replay_preserves_original_record_even_after_completion(run_session_factory, monkeypatch, status):
    task_id = create_eligible_task(run_session_factory)
    run_id = start_run(run_session_factory, task_id)
    if status != RunStatus.RUNNING:
        save_run_state(run_session_factory, run_id, status)
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        original = {column.name: getattr(run, column.name) for column in Run.__table__.columns}
        task_status = session.get(Task, task_id).status

    def unexpected_clock_read(session):
        pytest.fail("A replay must not recalculate or extend the original lease")

    monkeypatch.setattr(coordinator_module, "get_database_time", unexpected_clock_read)
    with run_session_factory() as session:
        run, created = RunCoordinator(session, LeaseConfig(lease_duration_seconds=60)).start_initial_run(
            task_id, "worker-A", "creation-key",
        )
        assert created is False
        assert run.id == run_id and run.status == status
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        assert {column.name: getattr(run, column.name) for column in Run.__table__.columns} == original
        assert session.get(Task, task_id).status == task_status
        assert len(session.scalars(select(Run)).all()) == 1


def test_creation_key_with_different_worker_is_conflict(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    run_id = start_run(run_session_factory, task_id)
    with run_session_factory() as session:
        with pytest.raises(RunCreationConflictError):
            RunCoordinator(session).start_initial_run(task_id, "worker-B", "creation-key")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert session.get(Run, run_id).worker_id == "worker-A"
        assert len(session.scalars(select(Run)).all()) == 1


def test_creation_key_can_be_used_for_different_tasks(run_session_factory):
    first_task = create_eligible_task(run_session_factory)
    second_task = create_eligible_task(run_session_factory)
    first_id = start_run(run_session_factory, first_task, worker_id="worker-A")
    second_id = start_run(run_session_factory, second_task, worker_id="worker-B")
    assert first_id != second_id


def test_new_key_cannot_restart_running_task(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    start_run(run_session_factory, task_id)
    with run_session_factory() as session:
        with pytest.raises(TaskStateConflictError, match="RUNNING"):
            RunCoordinator(session).start_initial_run(task_id, "worker-A", "another-key")
    with run_session_factory() as session:
        assert len(session.scalars(select(Run)).all()) == 1


@pytest.mark.parametrize("max_duration", [5, 1800])
def test_initial_start_uses_saved_policy_and_allows_zero_extra_retries(
    run_session_factory, monkeypatch, max_duration,
):
    policy = build_default_policy()
    policy.policy_version = f"test-duration-{max_duration}"
    policy.budget.max_run_duration_seconds = max_duration
    policy.budget.max_retries_per_task = 0
    with monkeypatch.context() as context:
        context.setattr(services_module, "build_default_policy", lambda: policy)
        task_id = create_eligible_task(run_session_factory)
    run_id = start_run(run_session_factory, task_id)
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        assert run.attempt_number == 1
        assert run.lease_expires_at - run.started_at == timedelta(seconds=min(30, max_duration))
        assert session.get(Task, task_id).policy_version == policy.policy_version


def test_missing_task_is_not_reported_as_busy(run_session_factory):
    with run_session_factory() as session:
        with pytest.raises(TaskNotFoundError):
            RunCoordinator(session).start_initial_run(uuid4(), "worker-A", "creation-key")
        assert not session.in_transaction()


@pytest.mark.parametrize("status", [status for status in TaskStatus if status != TaskStatus.ELIGIBLE])
def test_new_initial_start_rejects_other_task_states(run_session_factory, status):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        # Fixture state only; these transitions are not application lifecycle operations.
        session.execute(update(Task).where(Task.id == task_id).values(status=status))
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(TaskStateConflictError, match=status.value):
            RunCoordinator(session).start_initial_run(task_id, "worker-A", "creation-key")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == status


def test_eligible_task_with_existing_history_is_not_started_as_run1(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    run_id = start_run(run_session_factory, task_id)
    save_run_state(run_session_factory, run_id, RunStatus.FAILED)
    with run_session_factory() as session:
        # Deliberately inconsistent fixture data must not be silently repaired.
        session.execute(update(Task).where(Task.id == task_id).values(status=TaskStatus.ELIGIBLE, base_commit="a" * 40))
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(TaskStateConflictError, match="history"):
            RunCoordinator(session).start_initial_run(task_id, "worker-A", "new-key")


@pytest.mark.parametrize("field,value", [
    ("worker_id", ""), ("worker_id", "   "), ("worker_id", None),
    ("creation_idempotency_key", ""), ("creation_idempotency_key", "   "),
])
def test_blank_creation_parameters_are_rejected_before_transaction(run_session_factory, field, value):
    values = {"task_id": uuid4(), "worker_id": "worker-A", "creation_idempotency_key": "creation-key"}
    values[field] = value
    with run_session_factory() as session:
        with pytest.raises(ValueError, match=field):
            RunCoordinator(session).start_initial_run(**values)
        assert not session.in_transaction()


@pytest.mark.parametrize("corruption", ["snapshot", "version"])
def test_invalid_policy_fails_without_creating_run(run_session_factory, corruption):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        task = session.get(Task, task_id)
        if corruption == "snapshot":
            task.policy_snapshot = {"invalid": True}
        else:
            task.policy_version = "mismatched-version"
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(ValueError):
            RunCoordinator(session).start_initial_run(task_id, "worker-A", "creation-key")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE


def test_locked_task_reports_busy_then_same_request_can_start(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as holder:
        with holder.begin():
            assert TaskRepository(holder).get_by_id_for_update(task_id) is not None
            with run_session_factory() as session:
                def set_timeout(session, transaction, connection):
                    connection.execute(text("SET LOCAL statement_timeout = '2s'"))

                event.listen(session, "after_begin", set_timeout)
                with pytest.raises(TaskClaimBusyError):
                    RunCoordinator(session).start_initial_run(task_id, "worker-A", "creation-key")
                assert not session.in_transaction()
    start_run(run_session_factory, task_id)


def test_run_committed_after_first_lookup_is_replayed_after_lock(run_session_factory, monkeypatch):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        original_lookup = coordinator.run_repository.get_by_creation_idempotency_key
        lookup_count = 0
        first_run_id = None

        def interleaved_lookup(task_id, key):
            nonlocal lookup_count, first_run_id
            lookup_count += 1
            run = original_lookup(task_id, key)
            if lookup_count == 1:
                assert run is None
                first_run_id = start_run(run_session_factory, task_id, key)
            return run

        monkeypatch.setattr(coordinator.run_repository, "get_by_creation_idempotency_key", interleaved_lookup)
        run, created = coordinator.start_initial_run(task_id, "worker-A", "creation-key")
        assert created is False and run.id == first_run_id
        assert lookup_count == 2


def test_failure_after_run_flush_rolls_back_both_records(run_session_factory, monkeypatch):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        original_add = coordinator.run_repository.add

        def add_then_fail(run):
            original_add(run)
            assert run.id is not None
            raise RuntimeError("simulate failure after Run flush")

        monkeypatch.setattr(coordinator.run_repository, "add", add_then_fail)
        with pytest.raises(RuntimeError, match="after Run flush"):
            coordinator.start_initial_run(task_id, "worker-A", "creation-key")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE


def test_commit_failure_rolls_back_run_and_task_status(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        def fail_commit(session):
            assert session.get(Task, task_id).status == TaskStatus.RUNNING
            assert len(session.scalars(select(Run)).all()) == 1
            raise RuntimeError("simulate commit failure")

        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="simulate commit failure"):
            RunCoordinator(session).start_initial_run(task_id, "worker-A", "creation-key")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE


def test_unrelated_integrity_error_is_not_creation_conflict(run_session_factory, monkeypatch):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        original_add = coordinator.run_repository.add

        def invalid_owner(run):
            run.worker_id = None
            return original_add(run)

        monkeypatch.setattr(coordinator.run_repository, "add", invalid_owner)
        with pytest.raises(IntegrityError) as exc_info:
            coordinator.start_initial_run(task_id, "worker-A", "creation-key")
        assert exc_info.value.orig.diag.column_name == "worker_id"
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE


@pytest.mark.parametrize("same_key,same_owner", [(True, True), (True, False), (False, False)])
def test_concurrent_initial_starts_create_one_run(run_session_factory, same_key, same_owner):
    task_id = create_eligible_task(run_session_factory)
    barrier = Barrier(2, timeout=10)

    def worker(owner, key):
        with run_session_factory() as session:
            coordinator = RunCoordinator(session)
            original_lock = coordinator.task_repository.get_by_id_for_update

            def synchronized_lock(task_id):
                barrier.wait()
                return original_lock(task_id)

            coordinator.task_repository.get_by_id_for_update = synchronized_lock
            try:
                run, created = coordinator.start_initial_run(task_id, owner, key)
                return {"owner": owner, "key": key, "run_id": run.id, "created": created}
            except (TaskClaimBusyError, TaskStateConflictError, RunCreationConflictError) as error:
                assert not session.in_transaction()
                return {"owner": owner, "key": key, "error": type(error)}

    second_owner = "worker-A" if same_owner else "worker-B"
    second_key = "creation-key" if same_key else "other-key"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, owner, key) for owner, key in (
            ("worker-A", "creation-key"), (second_owner, second_key),
        )]
        results = [future.result(timeout=20) for future in futures]
    winner = next(result for result in results if result.get("created"))
    assert sum(result.get("created", False) for result in results) == 1
    if same_key and same_owner:
        with run_session_factory() as session:
            run, created = RunCoordinator(session).start_initial_run(task_id, "worker-A", "creation-key")
            assert created is False and run.id == winner["run_id"]
    else:
        loser = next(result for result in results if not result.get("created"))
        expected_error = RunCreationConflictError if same_key else TaskStateConflictError
        with run_session_factory() as session:
            with pytest.raises(expected_error):
                RunCoordinator(session).start_initial_run(task_id, loser["owner"], loser["key"])
    with run_session_factory() as session:
        assert len(session.scalars(select(Run)).all()) == 1
        assert session.get(Task, task_id).status == TaskStatus.RUNNING


def test_expired_lease_replay_does_not_renew_or_reopen_run(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    run_id = start_run(run_session_factory, task_id)
    with run_session_factory() as session:
        run = session.get(Run, run_id)
        expired_at = run.started_at - timedelta(seconds=1)
        run.lease_expires_at = expired_at
        session.commit()
    with run_session_factory() as session:
        run, created = RunCoordinator(session).start_initial_run(task_id, "worker-A", "creation-key")
        assert created is False and run.id == run_id
        assert run.lease_expires_at == expired_at


def test_database_clock_advances_inside_same_transaction(run_session_factory):
    with run_session_factory() as session:
        with session.begin():
            transaction_time = session.scalar(select(func.now()))
            first = get_database_time(session)
            session.execute(select(func.pg_sleep(0.01)))
            second = get_database_time(session)
            assert second > first >= transaction_time
            assert session.scalar(select(func.now())) == transaction_time


def test_automatic_claim_returns_none_when_no_eligible_task_exists(run_session_factory):
    with run_session_factory() as session:
        assert RunCoordinator(session).claim_next_initial_run("worker-A") is None
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()


def test_automatic_claims_are_new_work_and_can_be_replayed_by_task_and_key(run_session_factory):
    task_ids = {create_eligible_task(run_session_factory) for _ in range(2)}
    with run_session_factory() as session:
        expected_order = session.scalars(select(Task.id).order_by(Task.created_at, Task.id)).all()
    claimed = []
    for expected_task_id in expected_order:
        with run_session_factory() as session:
            run = RunCoordinator(session).claim_next_initial_run("worker-A")
            assert run.task_id == expected_task_id
            assert run.attempt_number == 1 and run.status == RunStatus.RUNNING
            assert run.worker_id == "worker-A" and run.creation_idempotency_key
            claimed.append((run.id, run.task_id, run.creation_idempotency_key, run.lease_expires_at))
    assert {task_id for _, task_id, _, _ in claimed} == task_ids
    with run_session_factory() as session:
        assert RunCoordinator(session).claim_next_initial_run("worker-A") is None
    for run_id, task_id, key, original_expiry in claimed:
        with run_session_factory() as session:
            run, created = RunCoordinator(session).start_initial_run(task_id, "worker-A", key)
            assert created is False and run.id == run_id
            assert run.lease_expires_at == original_expiry
    with run_session_factory() as session:
        assert len(session.scalars(select(Run)).all()) == 2
        assert all(session.get(Task, task_id).status == TaskStatus.RUNNING for task_id in task_ids)


def test_automatic_claim_skips_locked_candidate_and_keeps_it_unchanged(run_session_factory):
    first_id = create_eligible_task(run_session_factory)
    second_id = create_eligible_task(run_session_factory)
    with run_session_factory() as holder:
        with holder.begin():
            assert TaskRepository(holder).get_by_id_for_update(first_id) is not None
            with run_session_factory() as session:
                def set_timeout(session, transaction, connection):
                    connection.execute(text("SET LOCAL statement_timeout = '2s'"))

                event.listen(session, "after_begin", set_timeout)
                run = RunCoordinator(session).claim_next_initial_run("worker-B")
                assert run.task_id == second_id
    with run_session_factory() as session:
        assert session.get(Task, first_id).status == TaskStatus.ELIGIBLE
        assert session.get(Task, second_id).status == TaskStatus.RUNNING
        assert len(session.scalars(select(Run)).all()) == 1


def test_automatic_claim_returns_none_when_all_candidates_are_locked(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as holder:
        with holder.begin():
            TaskRepository(holder).get_by_id_for_update(task_id)
            with run_session_factory() as session:
                assert RunCoordinator(session).claim_next_initial_run("worker-B") is None
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE


def test_automatic_claim_does_not_admit_pending_or_start_recovery(run_session_factory):
    task_ids = [create_eligible_task(run_session_factory) for _ in range(2)]
    states = [TaskStatus.PENDING, TaskStatus.RETRYING]
    with run_session_factory() as session:
        for task_id, status in zip(task_ids, states):
            session.execute(update(Task).where(Task.id == task_id).values(status=status))
        session.commit()
    with run_session_factory() as session:
        assert RunCoordinator(session).claim_next_initial_run("worker-A") is None
    with run_session_factory() as session:
        assert [session.get(Task, task_id).status for task_id in task_ids] == states
        assert not session.scalars(select(Run)).all()


@pytest.mark.parametrize("worker_id", ["", "   ", None])
def test_automatic_claim_rejects_blank_owner_before_transaction(run_session_factory, worker_id):
    with run_session_factory() as session:
        with pytest.raises(ValueError, match="worker_id"):
            RunCoordinator(session).claim_next_initial_run(worker_id)
        assert not session.in_transaction()


def test_automatic_claim_holds_same_transaction_and_lock_during_creation(run_session_factory, monkeypatch):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        original_select = coordinator.task_repository.get_next_eligible_for_update
        original_create = coordinator._create_initial_run
        selected_transaction = None

        def tracked_select():
            nonlocal selected_transaction
            task = original_select()
            selected_transaction = session.get_transaction()
            return task

        def checked_create(task, worker_id, key):
            assert session.get_transaction() is selected_transaction
            with run_session_factory() as probe:
                with probe.begin():
                    probe.execute(text("SET LOCAL statement_timeout = '2s'"))
                    assert TaskRepository(probe).get_by_id_for_update(task_id) is None
                    assert probe.get(Task, task_id).status == TaskStatus.ELIGIBLE
            return original_create(task, worker_id, key)

        monkeypatch.setattr(coordinator.task_repository, "get_next_eligible_for_update", tracked_select)
        monkeypatch.setattr(coordinator, "_create_initial_run", checked_create)
        run = coordinator.claim_next_initial_run("worker-A")
        assert run.task_id == task_id


def test_automatic_claim_flush_failure_rolls_back_and_can_be_retried(run_session_factory, monkeypatch):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        coordinator = RunCoordinator(session)
        original_add = coordinator.run_repository.add

        def add_then_fail(run):
            original_add(run)
            raise RuntimeError("simulate automatic claim failure")

        with monkeypatch.context() as context:
            context.setattr(coordinator.run_repository, "add", add_then_fail)
            with pytest.raises(RuntimeError, match="automatic claim failure"):
                coordinator.claim_next_initial_run("worker-A")
        assert not session.in_transaction()
        with run_session_factory() as probe:
            assert not probe.scalars(select(Run)).all()
            assert probe.get(Task, task_id).status == TaskStatus.ELIGIBLE
        run = coordinator.claim_next_initial_run("worker-A")
        assert run.task_id == task_id and run.attempt_number == 1


def test_automatic_claim_commit_failure_rolls_back_both_records(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        def fail_commit(session):
            assert session.get(Task, task_id).status == TaskStatus.RUNNING
            assert len(session.scalars(select(Run)).all()) == 1
            raise RuntimeError("simulate automatic claim commit failure")

        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="automatic claim commit failure"):
            RunCoordinator(session).claim_next_initial_run("worker-A")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE


@pytest.mark.parametrize("task_count", [1, 2])
def test_concurrent_automatic_claims_get_different_available_tasks(run_session_factory, task_count):
    task_ids = {create_eligible_task(run_session_factory) for _ in range(task_count)}
    barrier = Barrier(2, timeout=10)

    def worker(worker_id):
        with run_session_factory() as session:
            coordinator = RunCoordinator(session)
            original_select = coordinator.task_repository.get_next_eligible_for_update

            def synchronized_select():
                task = original_select()
                barrier.wait()
                return task

            coordinator.task_repository.get_next_eligible_for_update = synchronized_select
            run = coordinator.claim_next_initial_run(worker_id)
            if run is None:
                return None
            return run.id, run.task_id, run.worker_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, owner) for owner in ("worker-A", "worker-B")]
        results = [future.result(timeout=20) for future in futures]
    claimed = [result for result in results if result is not None]
    assert len(claimed) == task_count
    assert len({run_id for run_id, _, _ in claimed}) == task_count
    assert {task_id for _, task_id, _ in claimed} == task_ids
    with run_session_factory() as session:
        assert len(session.scalars(select(Run)).all()) == task_count
        assert all(session.get(Task, task_id).status == TaskStatus.RUNNING for task_id in task_ids)


def test_automatic_claim_uses_injected_lease_config(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        config = LeaseConfig(heartbeat_interval_seconds=2, lease_duration_seconds=6)
        run = RunCoordinator(session, config).claim_next_initial_run("worker-A")
        assert run.task_id == task_id
        assert run.lease_expires_at - run.started_at == timedelta(seconds=6)


def test_automatic_claim_does_not_hide_invalid_candidate_policy(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        session.get(Task, task_id).policy_snapshot = {"invalid": True}
        session.commit()
    with run_session_factory() as session:
        with pytest.raises(ValueError):
            RunCoordinator(session).claim_next_initial_run("worker-A")
        assert not session.in_transaction()
    with run_session_factory() as session:
        assert not session.scalars(select(Run)).all()
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE
