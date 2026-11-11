from datetime import timedelta
import importlib.util
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, func, select
from sqlalchemy import text

import devflow_ai.api as api_module
from devflow_ai.admission import attempt_admission
from devflow_ai.admission_claim import AdmissionClaimService
from devflow_ai.admission_intervention import get_admission_status
from devflow_ai.enums import TaskStatus
from devflow_ai.harness import RepositoryHeadObservation
from devflow_ai.model_gateway import AdmissionModelGateway
from devflow_ai.models import Task
from devflow_ai.services import AdmissionRecord, TaskService


def make_task(factory):
    with factory() as session:
        task, _ = TaskService(session).create_task("title", "description", "trusted", uuid4())
        return task.id


def test_three_timeouts_persist_notice_and_query_api(test_session_factory, monkeypatch):
    task_id = make_task(test_session_factory)
    gateway = AdmissionModelGateway(session_factory=test_session_factory, estimate_input_tokens=lambda _: 1,
                                    max_output_tokens=1, generate=lambda *args: None)
    for _ in range(3):
        attempt_admission(task_id, session_factory=test_session_factory, gateway=gateway,
            trusted_repository=lambda _: True, timeout_seconds=5,
            observe=lambda *args, **kwargs: RepositoryHeadObservation(status="timeout"))
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING
        assert task.admission_intervention["reason_codes"] == ["ADMISSION_TIMEOUT_LIMIT"]
    def db_session():
        with test_session_factory() as session:
            yield session
    monkeypatch.setitem(api_module.app.dependency_overrides, api_module.get_db_session, db_session)
    client = TestClient(api_module.app)
    try:
        response = client.get(f"/tasks/{task_id}/admission")
        missing = client.get(f"/tasks/{uuid4()}/admission")
    finally:
        client.close()
    assert response.status_code == 200 and missing.status_code == 404
    result = response.json()
    assert result["status"] == "PENDING"
    assert result["intervention"]["reason_codes"] == ["ADMISSION_TIMEOUT_LIMIT"]
    assert result["intervention"]["consecutive_timeouts"] == 3
    assert result["last_blocked"]["kind"] == "repository_check"
    assert result["last_blocked_at"] is not None


def test_expired_fifth_claim_surfaces_notice_but_active_attempt_does_not(test_session_factory):
    task_id = make_task(test_session_factory)
    with test_session_factory.begin() as session:
        session.get(Task, task_id).admission_attempt_count = 4
    with test_session_factory() as session:
        claim = AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=30)
    with test_session_factory() as session:
        assert get_admission_status(session, task_id)["intervention"] is None
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with test_session_factory() as session:
        result = get_admission_status(session, task_id)
        assert result["intervention"]["reason_codes"] == ["ADMISSION_ATTEMPT_LIMIT"]
        assert result["intervention"]["attempt_count"] == 5
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_intervention == result["intervention"]
    with test_session_factory() as session:
        new_claim = AdmissionClaimService(session).claim(task_id, owner="operator", lease_seconds=30)
        assert new_claim.token != claim.token
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_intervention is None
    with test_session_factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.DECLINED,
            admission_record=AdmissionRecord(reason="not appropriate", evidence=["review"]), claim=new_claim)
    with test_session_factory() as session:
        assert get_admission_status(session, task_id)["intervention"] is None


def test_unknown_legacy_counts_are_not_fabricated_as_limits(test_session_factory):
    task_id = make_task(test_session_factory)
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_attempt_count = None
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": None}
    with test_session_factory() as session:
        assert get_admission_status(session, task_id)["intervention"] is None


def test_both_limits_are_recorded_and_failed_commit_rolls_back(test_session_factory):
    task_id = make_task(test_session_factory)
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_attempt_count = 5
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": 3}
    with test_session_factory() as session:
        def fail_commit(_session):
            raise RuntimeError("commit failed")
        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="commit failed"):
            get_admission_status(session, task_id)
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_intervention is None
    with test_session_factory() as session:
        result = get_admission_status(session, task_id)
        assert result["intervention"]["reason_codes"] == ["ADMISSION_ATTEMPT_LIMIT", "ADMISSION_TIMEOUT_LIMIT"]


def test_intervention_migration_round_trip_preserves_existing_task(test_session_factory):
    task_id = make_task(test_session_factory)
    engine = test_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    path = Path(__file__).resolve().parents[1] / "migrations/versions/c61af287b039_add_admission_intervention.py"
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
        assert task.status == TaskStatus.PENDING and task.admission_intervention is None
