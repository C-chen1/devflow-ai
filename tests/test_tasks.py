from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

import devflow_ai.api as api_module
from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.services import IdempotencyConflictError


@pytest.fixture
def api_client_without_lifespan():
    client = TestClient(api_module.app)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("created,status,expected_code", [
    (True, TaskStatus.PENDING, 201),
    (False, TaskStatus.RUNNING, 200),
])
def test_create_task_passes_request_and_session_to_service(
    monkeypatch, api_client_without_lifespan, created, status, expected_code,
):
    session = object()
    task_id = uuid4()
    idempotency_key = uuid4()
    calls = []

    class FakeTaskService:
        def __init__(self, received_session):
            assert received_session is session

        def create_task(self, **fields):
            calls.append(fields)
            return SimpleNamespace(id=task_id, status=status), created

    monkeypatch.setattr(api_module, "TaskService", FakeTaskService)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: session)
    payload = {"title": "Example issue", "description": "Example description", "repo": "example-repo"}
    response = api_client_without_lifespan.post(
        "/tasks", json=payload, headers={"Idempotency-Key": str(idempotency_key)},
    )
    assert response.status_code == expected_code
    assert response.json() == {"id": str(task_id), "status": status.value}
    assert calls == [{**payload, "idempotency_key": idempotency_key}]


def test_create_task_missing_repo_does_not_call_service(monkeypatch, api_client_without_lifespan):
    def unexpected_service_call(session):
        pytest.fail("Invalid request must not call TaskService")

    monkeypatch.setattr(api_module, "TaskService", unexpected_service_call)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: object())
    response = api_client_without_lifespan.post(
        "/tasks", json={"title": "Example issue", "description": "Example description"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(error["loc"] == ["body", "repo"] and error["type"] == "missing" for error in errors)


@pytest.mark.parametrize("headers,error_type", [
    ({}, "missing"),
    ({"Idempotency-Key": "not-a-uuid"}, "uuid_parsing"),
    ({"Idempotency-Key": ""}, "uuid_parsing"),
])
def test_create_task_invalid_key_does_not_call_service(
    monkeypatch, api_client_without_lifespan, headers, error_type,
):
    def unexpected_service_call(session):
        pytest.fail("Invalid submission key must not call TaskService")

    monkeypatch.setattr(api_module, "TaskService", unexpected_service_call)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: object())
    response = api_client_without_lifespan.post(
        "/tasks", headers=headers,
        json={"title": "Example issue", "description": "Example description", "repo": "example-repo"},
    )
    assert response.status_code == 422
    assert any(
        error["loc"] == ["header", "Idempotency-Key"] and error["type"] == error_type
        for error in response.json()["detail"]
    )


def test_create_task_content_conflict_returns_409(monkeypatch, api_client_without_lifespan):
    class FakeTaskService:
        def __init__(self, session):
            pass

        def create_task(self, **fields):
            raise IdempotencyConflictError("Idempotency key is already used for different task content")

    monkeypatch.setattr(api_module, "TaskService", FakeTaskService)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: object())
    response = api_client_without_lifespan.post(
        "/tasks", headers={"Idempotency-Key": str(uuid4())},
        json={"title": "Example issue", "description": "Example description", "repo": "example-repo"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Idempotency key is already used for different task content"}


def test_create_task_endpoint_persists_task_and_can_be_queried(monkeypatch, test_session_factory):
    # Real migrations and transactions in an isolated schema; public data is untouched.
    monkeypatch.setattr(api_module, "create_db_engine", lambda: test_session_factory.kw["bind"])
    idempotency_key = uuid4()
    headers = {"Idempotency-Key": str(idempotency_key)}
    payload = {"title": "HTTP persistence test", "description": "Verify HTTP Task persistence", "repo": "example-repo"}
    with TestClient(api_module.app) as client:
        response = client.post("/tasks", json=payload, headers=headers)
    assert response.status_code == 201
    result = response.json()
    task_id = UUID(result["id"])
    assert result == {"id": str(task_id), "status": "PENDING"}

    with test_session_factory() as read_session:
        task = read_session.get(Task, task_id)
        assert task is not None
        assert task.idempotency_key == idempotency_key
        assert {field: getattr(task, field) for field in payload} == payload
        assert task.policy_version == "bootstrap-no-tools-v6"
        assert task.policy_snapshot["policy_version"] == task.policy_version
        assert task.policy_snapshot["budget"]["max_output_repairs_per_agent_execution"] == 1
        original_policy = task.policy_snapshot

    # Simulate later lifecycle progress; Supervisor admission is not implemented here.
    with test_session_factory() as update_session:
        update_session.execute(update(Task).where(Task.id == task_id).values(status=TaskStatus.ELIGIBLE, base_commit="a" * 40))
        update_session.commit()

    # A new lifespan and new request Sessions must still recognize the persisted key.
    with TestClient(api_module.app) as query_client:
        replay = query_client.post("/tasks", json=dict(reversed(list(payload.items()))), headers=headers)
        assert replay.status_code == 200
        assert replay.json() == {"id": str(task_id), "status": "ELIGIBLE"}
        for field in payload:
            conflict = query_client.post("/tasks", json={**payload, field: payload[field] + " "}, headers=headers)
            assert conflict.status_code == 409
            assert conflict.json() == {"detail": "Idempotency key is already used for different task content"}
        query_response = query_client.get(f"/tasks/{task_id}")
        assert query_response.status_code == 200
        assert query_response.json() == {"id": str(task_id), "status": "ELIGIBLE", **payload}
        new_task = query_client.post("/tasks", json=payload, headers={"Idempotency-Key": str(uuid4())})
        assert new_task.status_code == 201
        assert new_task.json()["id"] != str(task_id)

    with test_session_factory() as read_session:
        task = read_session.get(Task, task_id)
        assert task.policy_snapshot == original_policy
        assert {field: getattr(task, field) for field in payload} == payload
        assert read_session.scalar(select(func.count()).select_from(Task)) == 2


def test_get_task_passes_uuid_and_session_to_service(monkeypatch, api_client_without_lifespan):
    session = object()
    task_id = uuid4()
    calls = []

    class FakeTaskService:
        def __init__(self, received_session):
            assert received_session is session

        def get_task(self, received_task_id):
            assert isinstance(received_task_id, UUID)
            calls.append(received_task_id)
            return SimpleNamespace(
                id=task_id, status=TaskStatus.PENDING,
                title="Example issue", description="Example description", repo="example-repo",
            )

    monkeypatch.setattr(api_module, "TaskService", FakeTaskService)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: session)
    response = api_client_without_lifespan.get(f"/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json() == {
        "id": str(task_id), "status": "PENDING", "title": "Example issue",
        "description": "Example description", "repo": "example-repo",
    }
    assert calls == [task_id]


def test_get_task_not_found_returns_404(monkeypatch, api_client_without_lifespan):
    task_id = uuid4()
    calls = []

    class FakeTaskService:
        def __init__(self, session):
            pass

        def get_task(self, received_task_id):
            calls.append(received_task_id)
            return None

    monkeypatch.setattr(api_module, "TaskService", FakeTaskService)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: object())
    response = api_client_without_lifespan.get(f"/tasks/{task_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}
    assert calls == [task_id]


def test_get_task_invalid_uuid_does_not_call_service(monkeypatch, api_client_without_lifespan):
    def unexpected_service_call(session):
        pytest.fail("Invalid UUID must not call TaskService")

    monkeypatch.setattr(api_module, "TaskService", unexpected_service_call)
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, lambda: object())
    response = api_client_without_lifespan.get("/tasks/not-a-uuid")
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(error["loc"] == ["path", "task_id"] and error["type"] == "uuid_parsing" for error in errors)
