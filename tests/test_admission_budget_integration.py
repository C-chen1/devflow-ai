from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event

from devflow_ai.admission_budget import AdmissionBudgetService, AdmissionUsageConflictError
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.model_gateway import AdmissionModelGateway, ModelReply
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService, TaskStateConflictError


def make_task(factory, limit):
    def policy_factory():
        policy = build_default_policy()
        policy.budget.max_admission_llm_tokens_per_task = limit
        return policy
    with factory() as session:
        task, _ = TaskService(session, policy_factory=policy_factory).create_task(
            "title", "description", "repo", uuid4())
        return task.id


def test_admission_reservation_settlement_and_task_isolation(test_session_factory):
    first = make_task(test_session_factory, 100)
    second = make_task(test_session_factory, 100)
    with test_session_factory() as session:
        budget = AdmissionBudgetService(session)
        reservation = budget.reserve(first, estimated_input_tokens=30, max_output_tokens=90)
        assert reservation.max_output_tokens == 70
        assert budget.reserve(first, estimated_input_tokens=1, max_output_tokens=1) is None
        assert budget.reserve(second, estimated_input_tokens=10, max_output_tokens=20) is not None
        usage = ModelTokenUsage(input_tokens=20, output_tokens=10)
        budget.settle(first, request_id=reservation.request_id, actual_usage=usage)
        budget.settle(first, request_id=reservation.request_id, actual_usage=usage)
        with pytest.raises(AdmissionUsageConflictError):
            budget.settle(first, request_id=reservation.request_id,
                          actual_usage=ModelTokenUsage(input_tokens=21, output_tokens=10))
        with pytest.raises(AdmissionUsageConflictError):
            budget.settle(second, request_id=reservation.request_id, actual_usage=usage)
        assert budget.reserve(first, estimated_input_tokens=20, max_output_tokens=100).max_output_tokens == 50


@pytest.mark.parametrize("limit", [None, 0])
def test_no_admission_budget_does_not_reserve(test_session_factory, limit):
    task_id = make_task(test_session_factory, limit)
    with test_session_factory() as session:
        assert AdmissionBudgetService(session).reserve(task_id, estimated_input_tokens=0, max_output_tokens=1) is None


def test_late_usage_settles_after_admission_without_authorizing_new_requests(test_session_factory):
    task_id = make_task(test_session_factory, 100)
    with test_session_factory() as session:
        budget = AdmissionBudgetService(session)
        reservation = budget.reserve(task_id, estimated_input_tokens=10, max_output_tokens=40)
        TaskService(session).apply_admission_decision(task_id, TaskStatus.DECLINED,
            admission_record=AdmissionRecord(reason="not eligible", evidence=[]))
        budget.settle(task_id, request_id=reservation.request_id,
                      actual_usage=ModelTokenUsage(input_tokens=9, output_tokens=20))
        with pytest.raises(TaskStateConflictError):
            budget.reserve(task_id, estimated_input_tokens=1, max_output_tokens=1)
    with test_session_factory() as session:
        entry = session.get(Task, task_id).admission_usage[str(reservation.request_id)]
        assert entry["actual_usage"] == {"input_tokens": 9, "output_tokens": 20}


def test_concurrent_reservations_cannot_overbook(test_session_factory):
    task_id = make_task(test_session_factory, 100)
    barrier = Barrier(2, timeout=10)
    def reserve():
        with test_session_factory() as session:
            barrier.wait()
            return AdmissionBudgetService(session).reserve(task_id, estimated_input_tokens=50, max_output_tokens=50)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(result is not None for result in results) == 1
    with test_session_factory() as session:
        assert len(session.get(Task, task_id).admission_usage) == 1


def test_gateway_settles_actual_usage_and_reserves_before_call(test_session_factory):
    task_id = make_task(test_session_factory, 50)
    def generate(prompt, maximum):
        with test_session_factory() as session:
            assert len(session.get(Task, task_id).admission_usage) == 1
        assert maximum == 40
        return ModelReply(output="result", usage=ModelTokenUsage(input_tokens=8, output_tokens=12))
    gateway = AdmissionModelGateway(session_factory=test_session_factory,
        estimate_input_tokens=lambda prompt: 10, generate=generate, max_output_tokens=100)
    assert gateway.request(task_id, prompt="example") == "result"
    with test_session_factory() as session:
        entry = next(iter(session.get(Task, task_id).admission_usage.values()))
        assert entry["actual_usage"] == {"input_tokens": 8, "output_tokens": 12}


def test_gateway_timeout_keeps_reservation_and_prevents_overbooking(test_session_factory):
    task_id = make_task(test_session_factory, 50)
    calls = []
    def timeout(prompt, maximum):
        calls.append(prompt)
        raise TimeoutError("model timeout")
    gateway = AdmissionModelGateway(session_factory=test_session_factory,
        estimate_input_tokens=lambda prompt: 10, generate=timeout, max_output_tokens=40)
    with pytest.raises(TimeoutError):
        gateway.request(task_id, prompt="example")
    assert gateway.request(task_id, prompt="again") is None
    assert calls == ["example"]
    with test_session_factory() as session:
        entry = next(iter(session.get(Task, task_id).admission_usage.values()))
        assert entry == {"reserved_tokens": 50, "actual_usage": None}


def test_failed_reservation_commit_leaves_no_usage(test_session_factory):
    task_id = make_task(test_session_factory, 50)
    with test_session_factory() as session:
        def fail_commit(_session):
            raise RuntimeError("reservation commit failed")
        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="reservation commit failed"):
            AdmissionBudgetService(session).reserve(task_id, estimated_input_tokens=10, max_output_tokens=40)
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_usage == {}
