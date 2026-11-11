from devflow_ai.services import AdmissionRecord
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, text, update
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.services import TaskNotFoundError, TaskService, TaskStateConflictError


BASE_COMMIT = "a" * 40
ISSUE = {"title": "Admission service test", "description": "Verify admission persistence", "repo": "example-repo"}


def create_pending_task(session_factory):
    with session_factory() as session:
        task, created = TaskService(session).create_task(**ISSUE, idempotency_key=uuid4())
        assert created is True
        return task.id


def test_admission_column_migration_preserves_legacy_task(test_session_factory):
    task_id = create_pending_task(test_session_factory)
    engine = test_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    path = Path(__file__).resolve().parents[1] / "migrations/versions/d98f41a76e20_add_task_admission_record.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
            revision.upgrade()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING
        assert task.admission_record is None


@pytest.mark.parametrize("target_status", [TaskStatus.ELIGIBLE, TaskStatus.DECLINED])
def test_admission_service_commits_and_preserves_task_content(test_session_factory, target_status):
    task_id = create_pending_task(test_session_factory)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        original = {field: getattr(task, field) for field in (
            "title", "description", "repo", "idempotency_key", "policy_version", "policy_snapshot", "created_at",
        )}
    with test_session_factory() as session:
        expected_commit = BASE_COMMIT if target_status == TaskStatus.ELIGIBLE else None
        task = TaskService(session).apply_admission_decision(task_id, target_status, base_commit=expected_commit, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert task.id == task_id
        assert task.status == target_status
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == target_status
        assert task.base_commit == expected_commit
        assert task.admission_record == {"reason": "test decision", "evidence": ["test fixture"]}
        assert {field: getattr(task, field) for field in original} == original


@pytest.mark.parametrize("invalid_commit", [None, "", "main", "a" * 39])
def test_admission_requires_full_base_commit_in_service_and_database(test_session_factory, invalid_commit):
    task_id = create_pending_task(test_session_factory)
    with test_session_factory() as session:
        with pytest.raises(ValueError, match="base_commit"):
            TaskService(session).apply_admission_decision(
                task_id, TaskStatus.ELIGIBLE, base_commit=invalid_commit,
             admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        with pytest.raises(IntegrityError) as exc_info:
            with session.begin():
                session.execute(update(Task).where(Task.id == task_id).values(
                    status=TaskStatus.ELIGIBLE, base_commit=invalid_commit,
                ))
        assert exc_info.value.orig.diag.constraint_name == "ck_tasks_eligible_base_commit"
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert (task.status, task.base_commit) == (TaskStatus.PENDING, None)


def test_admission_service_missing_task_rolls_back_and_session_remains_usable(test_session_factory):
    missing_id = uuid4()
    with test_session_factory() as session:
        service = TaskService(session)
        with pytest.raises(TaskNotFoundError, match=str(missing_id)):
            service.apply_admission_decision(missing_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert not session.in_transaction()
        task, created = service.create_task(**ISSUE, idempotency_key=uuid4())
        assert created is True
        assert task.status == TaskStatus.PENDING


@pytest.mark.parametrize("current_status", [status for status in TaskStatus if status != TaskStatus.PENDING])
def test_admission_service_rejects_non_pending_without_overwriting(test_session_factory, current_status):
    task_id = create_pending_task(test_session_factory)
    # Fixture simulation only; this does not implement the remaining lifecycle or Run.
    with test_session_factory() as session:
        session.execute(update(Task).where(Task.id == task_id).values(
            status=current_status, base_commit=BASE_COMMIT if current_status == TaskStatus.ELIGIBLE else None,
        ))
        session.commit()
        original_updated_at = session.get(Task, task_id).updated_at
    with test_session_factory() as session:
        with pytest.raises(TaskStateConflictError, match=f"is {current_status.value};"):
            TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert not session.in_transaction()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == current_status
        assert task.updated_at == original_updated_at


@pytest.mark.parametrize("target_status", [TaskStatus.PENDING, TaskStatus.RUNNING, None])
def test_admission_service_rejects_invalid_target_and_leaves_task_unchanged(test_session_factory, target_status):
    task_id = create_pending_task(test_session_factory)
    with test_session_factory() as session:
        with pytest.raises(ValueError, match="ELIGIBLE or DECLINED"):
            TaskService(session).apply_admission_decision(task_id, target_status, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert not session.in_transaction()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING
        assert task.admission_record is None


def test_admission_service_failure_after_update_rolls_back(test_session_factory, monkeypatch):
    task_id = create_pending_task(test_session_factory)
    with test_session_factory() as session:
        service = TaskService(session)
        update_status = service.task_repository.update_admission_status
        updates = []

        def counted_update(task_id, target_status, *, base_commit, admission_record):
            affected_rows = update_status(task_id, target_status, base_commit=base_commit, admission_record=admission_record)
            updates.append(affected_rows)
            return affected_rows

        def failed_lookup(*args, **kwargs):
            raise RuntimeError("simulate failure after status update")

        monkeypatch.setattr(service.task_repository, "update_admission_status", counted_update)
        monkeypatch.setattr(service.task_repository, "get_by_id", failed_lookup)
        with pytest.raises(RuntimeError, match="failure after status update"):
            service.apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert updates == [1]
        assert not session.in_transaction()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING
        assert task.admission_record is None


def test_admission_service_commit_failure_is_not_reported_as_success(test_session_factory):
    task_id = create_pending_task(test_session_factory)
    with test_session_factory() as session:
        def fail_commit(session):
            raise RuntimeError("simulate commit failure")

        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="simulate commit failure"):
            TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert not session.in_transaction()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING
        assert task.admission_record is None


@pytest.mark.parametrize("deleted", [False, True])
def test_admission_service_refreshes_stale_identity_map(test_session_factory, deleted):
    task_id = create_pending_task(test_session_factory)
    with test_session_factory(expire_on_commit=False) as stale_session:
        stale_task = stale_session.get(Task, task_id)
        stale_session.commit()
        with test_session_factory() as other_session:
            if deleted:
                other_session.execute(delete(Task).where(Task.id == task_id))
                other_session.commit()
            else:
                TaskService(other_session).apply_admission_decision(task_id, TaskStatus.DECLINED, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert stale_task.status == TaskStatus.PENDING
        if deleted:
            expected_error, expected_message = TaskNotFoundError, "not found"
        else:
            expected_error, expected_message = TaskStateConflictError, "is DECLINED;"
        with pytest.raises(expected_error, match=expected_message):
            TaskService(stale_session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit=BASE_COMMIT, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
        assert not stale_session.in_transaction()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        if deleted:
            assert task is None
        else:
            assert task.status == TaskStatus.DECLINED


@pytest.mark.parametrize("same_decision", [True, False])
def test_concurrent_admission_service_calls_have_one_success(test_session_factory, same_decision):
    task_id = create_pending_task(test_session_factory)
    barrier = Barrier(2, timeout=10)

    def apply_decision(target_status):
        with test_session_factory() as session:
            service = TaskService(session)
            original_update = service.task_repository.update_admission_status

            def synchronized_update(task_id, status, *, base_commit, admission_record):
                barrier.wait()
                return original_update(task_id, status, base_commit=base_commit, admission_record=admission_record)

            service.task_repository.update_admission_status = synchronized_update
            try:
                task = service.apply_admission_decision(
                    task_id, target_status,
                    base_commit=BASE_COMMIT if target_status == TaskStatus.ELIGIBLE else None,
                 admission_record=AdmissionRecord(reason=target_status.value, evidence=["test fixture"]))
            except TaskStateConflictError as error:
                assert not session.in_transaction()
                return {"success": False, "error": str(error)}
            return {"success": True, "status": task.status}

    second_status = TaskStatus.ELIGIBLE if same_decision else TaskStatus.DECLINED
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply_decision, status) for status in (TaskStatus.ELIGIBLE, second_status)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result["success"] for result in results) == 1
    winner = next(result for result in results if result["success"])
    loser = next(result for result in results if not result["success"])
    assert f"is {winner['status'].value};" in loser["error"]
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == winner["status"]
        assert task.admission_record["reason"] == winner["status"].value
