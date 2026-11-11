from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import devflow_ai.services as services_module
from devflow_ai.enums import AgentRole, TaskStatus
from devflow_ai.models import Task
from devflow_ai.policy import build_default_policy
from devflow_ai.services import IdempotencyConflictError, TaskService


def test_task_service_create_commits_policy_snapshot(test_session_factory):
    task_id = None
    try:
        with test_session_factory() as write_session:
            task, created = TaskService(write_session).create_task(
                title="service persistence test",
                description="verify service commits a complete Policy snapshot",
                repo="example-repo",
                idempotency_key=uuid4(),
            )
            assert created is True
            task_id = task.id
            assert task_id is not None

        with test_session_factory() as read_session:
            loaded_task = TaskService(read_session).get_task(task_id)
            assert loaded_task is not None
            assert loaded_task.title == "service persistence test"
            assert loaded_task.description == "verify service commits a complete Policy snapshot"
            assert loaded_task.repo == "example-repo"
            assert loaded_task.status == TaskStatus.PENDING
            expected_policy = build_default_policy()
            assert loaded_task.policy_version == expected_policy.policy_version
            assert loaded_task.policy_snapshot == expected_policy.model_dump(mode="json")
    finally:
        if task_id is not None:
            with test_session_factory() as cleanup_session:
                cleanup_session.execute(delete(Task).where(Task.id == task_id))
                cleanup_session.commit()


ISSUE = {"title": "Example issue", "description": "Example description", "repo": "example-repo"}


def test_trusted_policy_factory_applies_only_to_new_task_and_replay_keeps_snapshot(test_session_factory):
    policy = build_default_policy()
    policy.policy_version = "trusted-test-policy-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    key = uuid4()
    with test_session_factory() as session:
        task, created = TaskService(session, policy_factory=lambda: policy).create_task(**ISSUE, idempotency_key=key)
        assert created
        task_id = task.id
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER].append("later-change")
    def must_not_rebuild():
        pytest.fail("Replay must not request a new policy")
    with test_session_factory() as session:
        replay, created = TaskService(session, policy_factory=must_not_rebuild).create_task(**ISSUE, idempotency_key=key)
        assert not created and replay.id == task_id
        assert replay.policy_version == "trusted-test-policy-v1"
        assert replay.policy_snapshot["permissions"]["allowed_tools_by_role"]["EXPLORER"] == ["read_file"]
    with test_session_factory() as session:
        ordinary, created = TaskService(session).create_task(**ISSUE, idempotency_key=uuid4())
        assert created and ordinary.policy_version == "bootstrap-no-tools-v6"
        assert ordinary.policy_snapshot["permissions"]["allowed_tools_by_role"]["EXPLORER"] == []


def test_invalid_trusted_policy_rolls_back_task_creation(test_session_factory):
    invalid = build_default_policy()
    invalid.policy_version = " "
    with test_session_factory() as session:
        with pytest.raises(ValidationError, match="policy_version"):
            TaskService(session, policy_factory=lambda: invalid).create_task(**ISSUE, idempotency_key=uuid4())
    with test_session_factory() as session:
        assert session.scalars(select(Task)).all() == []


def create_committed_task(session_factory, key):
    with session_factory() as session:
        task, created = TaskService(session).create_task(**ISSUE, idempotency_key=key)
        assert created is True
        return task.id


def test_task_service_replay_does_not_replace_policy_or_write_task(test_session_factory, monkeypatch):
    key = uuid4()
    task_id = create_committed_task(test_session_factory, key)
    with test_session_factory() as session:
        original = session.get(Task, task_id)
        original_updated_at = original.updated_at
        original_policy = original.policy_snapshot

    def unexpected_default_policy():
        pytest.fail("Replaying an existing Task must not rebuild its Policy")

    monkeypatch.setattr(services_module, "build_default_policy", unexpected_default_policy)
    with test_session_factory() as session:
        task, created = TaskService(session).create_task(**ISSUE, idempotency_key=key)
        assert created is False
        assert task.id == task_id
        assert task.updated_at == original_updated_at
        assert task.policy_snapshot == original_policy
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_task_service_same_content_with_new_key_creates_new_task(test_session_factory):
    first_id = create_committed_task(test_session_factory, uuid4())
    second_id = create_committed_task(test_session_factory, uuid4())
    assert first_id != second_id
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 2


@pytest.mark.parametrize("field,value", [
    ("title", "Different title"),
    ("description", "Different description"),
    ("repo", "different-repo"),
    ("description", "Example description "),
])
def test_task_service_conflicting_content_preserves_existing_task(test_session_factory, field, value):
    key = uuid4()
    task_id = create_committed_task(test_session_factory, key)
    changed_issue = {**ISSUE, field: value}
    with test_session_factory() as session:
        with pytest.raises(IdempotencyConflictError):
            TaskService(session).create_task(**changed_issue, idempotency_key=key)
        assert not session.in_transaction()
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert {name: getattr(task, name) for name in ISSUE} == ISSUE
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_task_service_replay_returns_current_status(test_session_factory):
    key = uuid4()
    task_id = create_committed_task(test_session_factory, key)
    # Simulate a lifecycle update; this does not implement Supervisor admission.
    with test_session_factory() as session:
        session.execute(update(Task).where(Task.id == task_id).values(status=TaskStatus.ELIGIBLE, base_commit="a" * 40))
        session.commit()
    with test_session_factory() as session:
        task, created = TaskService(session).create_task(**ISSUE, idempotency_key=key)
        assert created is False
        assert task.id == task_id
        assert task.status == TaskStatus.ELIGIBLE


def test_task_service_non_null_error_propagates_and_session_can_be_reused(test_session_factory):
    key = uuid4()
    with test_session_factory() as session:
        service = TaskService(session)
        with pytest.raises(IntegrityError) as exc_info:
            service.create_task(title=None, description=ISSUE["description"], repo=ISSUE["repo"], idempotency_key=key)
        assert exc_info.value.orig.sqlstate == "23502"
        assert exc_info.value.orig.diag.column_name == "title"
        assert not session.in_transaction()
        task, created = service.create_task(**ISSUE, idempotency_key=key)
        assert created is True
        assert task.id is not None
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_task_service_other_unique_constraint_error_is_not_replayed(test_session_factory, monkeypatch):
    existing_id = create_committed_task(test_session_factory, uuid4())
    key = uuid4()
    with test_session_factory() as session:
        service = TaskService(session)
        original_add = service.task_repository.add
        original_lookup = service.task_repository.get_by_idempotency_key
        lookups = []

        def duplicate_primary_key(task):
            task.id = existing_id
            return original_add(task)

        def counted_lookup(submission_key):
            lookups.append(submission_key)
            return original_lookup(submission_key)

        monkeypatch.setattr(service.task_repository, "add", duplicate_primary_key)
        monkeypatch.setattr(service.task_repository, "get_by_idempotency_key", counted_lookup)
        with pytest.raises(IntegrityError) as exc_info:
            service.create_task(**ISSUE, idempotency_key=key)
        assert exc_info.value.orig.sqlstate == "23505"
        assert exc_info.value.orig.diag.constraint_name != "uq_tasks_idempotency_key"
        assert lookups == [key]
        assert not session.in_transaction()


@pytest.mark.parametrize("same_content", [True, False])
def test_task_service_concurrent_same_key_requests(test_session_factory, same_content):
    key = uuid4()
    barrier = Barrier(2, timeout=10)

    def worker(description):
        with test_session_factory() as session:
            service = TaskService(session)
            original_lookup = service.task_repository.get_by_idempotency_key
            lookup_count = 0

            def synchronized_lookup(submission_key):
                nonlocal lookup_count
                lookup_count += 1
                task = original_lookup(submission_key)
                if lookup_count == 1:
                    assert task is None
                    barrier.wait()
                return task

            # Each worker owns its Session and repository instance.
            service.task_repository.get_by_idempotency_key = synchronized_lookup
            try:
                task, created = service.create_task(
                    title=ISSUE["title"], description=description, repo=ISSUE["repo"], idempotency_key=key,
                )
            except IdempotencyConflictError:
                return {"conflict": True, "lookups": lookup_count}
            return {
                "conflict": False, "created": created, "id": task.id,
                "description": task.description, "lookups": lookup_count,
            }

    second_description = ISSUE["description"] if same_content else "A different request"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, description) for description in (ISSUE["description"], second_description)]
        results = [future.result(timeout=20) for future in futures]

    # Both initial reads saw no Task; the loser must take the post-rollback lookup path.
    assert sorted(result["lookups"] for result in results) == [1, 2]
    if same_content:
        assert not any(result["conflict"] for result in results)
        assert sorted(result["created"] for result in results) == [False, True]
        assert results[0]["id"] == results[1]["id"]
    else:
        assert sum(result["conflict"] for result in results) == 1
        winner = next(result for result in results if not result["conflict"])
        assert winner["created"] is True
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_task_service_failure_rolls_back_flushed_task(test_session_factory, monkeypatch):
    task_id = None
    try:
        with test_session_factory() as write_session:
            service = TaskService(write_session)
            repository_add = service.task_repository.add

            def add_then_fail(task):
                nonlocal task_id
                repository_add(task)
                task_id = task.id
                raise RuntimeError("simulate failure after flush")

            monkeypatch.setattr(service.task_repository, "add", add_then_fail)
            with pytest.raises(RuntimeError, match="simulate failure after flush"):
                service.create_task(
                    title="service rollback test",
                    description="verify a flushed Task is rolled back on failure",
                    repo="example-repo",
                    idempotency_key=uuid4(),
                )
            assert task_id is not None
            assert not write_session.in_transaction()

        with test_session_factory() as read_session:
            assert TaskService(read_session).get_task(task_id) is None
    finally:
        if task_id is not None:
            with test_session_factory() as cleanup_session:
                cleanup_session.execute(delete(Task).where(Task.id == task_id))
                cleanup_session.commit()
