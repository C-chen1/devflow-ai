from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.repositories import TaskRepository


BASE_COMMIT = "a" * 40


def create_task_record(session_factory, status=TaskStatus.PENDING):
    with session_factory() as session:
        # Database fixture only; not a production Policy or Supervisor decision.
        task = Task(
            idempotency_key=uuid4(),
            title="Admission repository test",
            description="Verify conditional status updates",
            repo="example-repo",
            status=status,
            base_commit=BASE_COMMIT if status == TaskStatus.ELIGIBLE else None,
            policy_version="db_test_only",
            policy_snapshot={"test_only": True},
        )
        session.add(task)
        session.flush()
        task_id = task.id
        session.commit()
        return task_id


@pytest.mark.parametrize("target_status", [TaskStatus.ELIGIBLE, TaskStatus.DECLINED])
def test_admission_updates_only_the_requested_pending_task(test_session_factory, target_status):
    task_id = create_task_record(test_session_factory)
    other_task_id = create_task_record(test_session_factory)
    with test_session_factory() as session:
        affected_rows = TaskRepository(session).update_admission_status(
            task_id, target_status,
            base_commit=BASE_COMMIT if target_status == TaskStatus.ELIGIBLE else None,
        )
        assert affected_rows == 1
        session.commit()
    with test_session_factory() as session:
        assert session.get(Task, task_id).status == target_status
        assert session.get(Task, other_task_id).status == TaskStatus.PENDING


@pytest.mark.parametrize("current_status", [status for status in TaskStatus if status != TaskStatus.PENDING])
def test_admission_does_not_overwrite_non_pending_status(test_session_factory, current_status):
    task_id = create_task_record(test_session_factory, current_status)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        original_updated_at = task.updated_at
        assert TaskRepository(session).update_admission_status(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT) == 0
        session.commit()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == current_status
        assert task.updated_at == original_updated_at


def test_admission_missing_task_returns_zero_without_affecting_existing_task(test_session_factory):
    existing_id = create_task_record(test_session_factory)
    with test_session_factory() as session:
        repository = TaskRepository(session)
        missing_id = uuid4()
        assert repository.update_admission_status(missing_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT) == 0
        assert repository.get_by_id(missing_id) is None
        session.commit()
    with test_session_factory() as session:
        assert session.get(Task, existing_id).status == TaskStatus.PENDING


def test_admission_update_allows_caller_rollback(test_session_factory):
    task_id = create_task_record(test_session_factory)
    with test_session_factory() as session:
        assert TaskRepository(session).update_admission_status(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT) == 1
        assert session.in_transaction()
        session.rollback()
    with test_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.PENDING


@pytest.mark.parametrize("target_status", [TaskStatus.PENDING, TaskStatus.RUNNING, None])
def test_admission_rejects_invalid_target_without_starting_transaction(test_session_factory, target_status):
    task_id = create_task_record(test_session_factory)
    with test_session_factory() as session:
        with pytest.raises(ValueError, match="ELIGIBLE or DECLINED"):
            TaskRepository(session).update_admission_status(task_id, target_status)
        assert not session.in_transaction()
    with test_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.PENDING


def test_admission_stale_python_state_cannot_overwrite_committed_decision(test_session_factory):
    task_id = create_task_record(test_session_factory)
    with test_session_factory() as stale_session:
        stale_task = stale_session.get(Task, task_id)
        assert stale_task.status == TaskStatus.PENDING
        with test_session_factory() as other_session:
            assert TaskRepository(other_session).update_admission_status(task_id, TaskStatus.DECLINED) == 1
            other_session.commit()
        # The SQL predicate protects the row even though this Python object is stale.
        assert stale_task.status == TaskStatus.PENDING
        assert TaskRepository(stale_session).update_admission_status(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT) == 0
        stale_session.rollback()
    with test_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.DECLINED


def test_concurrent_admission_updates_only_one_wins(test_session_factory):
    task_id = create_task_record(test_session_factory)
    barrier = Barrier(2, timeout=10)

    def apply_decision(target_status):
        with test_session_factory() as session:
            assert session.get(Task, task_id).status == TaskStatus.PENDING
            barrier.wait()
            affected_rows = TaskRepository(session).update_admission_status(
                task_id, target_status,
                base_commit=BASE_COMMIT if target_status == TaskStatus.ELIGIBLE else None,
            )
            session.commit()
            return affected_rows, target_status

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply_decision, status) for status in (TaskStatus.ELIGIBLE, TaskStatus.DECLINED)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(count for count, _ in results) == [0, 1]
    winning_status = next(status for count, status in results if count == 1)
    with test_session_factory() as session:
        assert session.get(Task, task_id).status == winning_status


def test_next_eligible_returns_none_when_no_tasks_exist(test_session_factory):
    with test_session_factory() as session:
        with session.begin():
            assert TaskRepository(session).get_next_eligible_for_update() is None


def test_next_eligible_filters_states_and_does_not_update_task(test_session_factory):
    ids_by_status = {status: create_task_record(test_session_factory, status) for status in TaskStatus}
    with test_session_factory() as session:
        with session.begin():
            task = TaskRepository(session).get_next_eligible_for_update()
            assert task.id == ids_by_status[TaskStatus.ELIGIBLE]
            assert task.status == TaskStatus.ELIGIBLE
            assert not session.dirty
    with test_session_factory() as session:
        for status, task_id in ids_by_status.items():
            assert session.get(Task, task_id).status == status


def test_next_eligible_orders_by_creation_time_then_id(test_session_factory):
    task_ids = sorted(
        (create_task_record(test_session_factory, TaskStatus.ELIGIBLE) for _ in range(3)),
        key=lambda task_id: task_id.int,
    )
    # The largest ID is oldest, so sorting only by ID would give the wrong Task.
    older_id = task_ids[-1]
    same_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with test_session_factory() as session:
        session.execute(update(Task).where(Task.id.in_(task_ids)).values(created_at=same_time))
        session.execute(update(Task).where(Task.id == older_id).values(created_at=same_time - timedelta(days=1)))
        session.commit()
    with test_session_factory() as session:
        with session.begin():
            assert TaskRepository(session).get_next_eligible_for_update().id == older_id
    with test_session_factory() as session:
        # Remove the oldest fixture from the candidate set to test the ID tie-break.
        session.execute(update(Task).where(Task.id == older_id).values(status=TaskStatus.DECLINED))
        session.commit()
    with test_session_factory() as session:
        with session.begin():
            assert TaskRepository(session).get_next_eligible_for_update().id == task_ids[0]


@pytest.mark.parametrize("transaction_end", ["commit", "rollback"])
def test_next_eligible_skips_locks_and_caller_releases_them(test_session_factory, transaction_end):
    task_ids = {create_task_record(test_session_factory, TaskStatus.ELIGIBLE) for _ in range(2)}
    with test_session_factory() as first_session, test_session_factory() as second_session:
        first_transaction = first_session.begin()
        second_transaction = second_session.begin()
        # Fail quickly if a regression waits on a lock instead of skipping it.
        second_session.execute(text("SET LOCAL statement_timeout = '2s'"))
        first_task = TaskRepository(first_session).get_next_eligible_for_update()
        second_task = TaskRepository(second_session).get_next_eligible_for_update()
        first_task_id, second_task_id = first_task.id, second_task.id
        assert {first_task.id, second_task.id} == task_ids
        assert first_task.id != second_task.id
        assert first_task.status == second_task.status == TaskStatus.ELIGIBLE
        assert first_session.in_transaction() and second_session.in_transaction()

        with test_session_factory() as blocked_session:
            with blocked_session.begin():
                blocked_session.execute(text("SET LOCAL statement_timeout = '2s'"))
                assert TaskRepository(blocked_session).get_next_eligible_for_update() is None

        second_transaction.rollback()
        with test_session_factory() as retry_session:
            with retry_session.begin():
                retry_session.execute(text("SET LOCAL statement_timeout = '2s'"))
                assert TaskRepository(retry_session).get_next_eligible_for_update().id == second_task_id

        if transaction_end == "commit":
            first_transaction.commit()
        else:
            first_transaction.rollback()

    # This method did not complete a business claim or change the Task's status.
    with test_session_factory() as session:
        with session.begin():
            task = TaskRepository(session).get_next_eligible_for_update()
            assert task.id == first_task_id
            assert task.status == TaskStatus.ELIGIBLE


def test_next_eligible_refreshes_an_existing_cached_task(test_session_factory):
    task_id = create_task_record(test_session_factory)
    with test_session_factory(expire_on_commit=False) as stale_session:
        stale_task = stale_session.get(Task, task_id)
        stale_session.commit()
        with test_session_factory() as other_session:
            assert TaskRepository(other_session).update_admission_status(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT) == 1
            other_session.commit()
        assert stale_task.status == TaskStatus.PENDING
        with stale_session.begin():
            task = TaskRepository(stale_session).get_next_eligible_for_update()
            assert task is stale_task
            assert task.status == TaskStatus.ELIGIBLE
