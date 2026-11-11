from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from devflow_ai.admission_claim import AdmissionClaimDeniedError, AdmissionClaimService
from devflow_ai.admission_budget import AdmissionBudgetService
from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService


def create_task(factory):
    def configured_policy():
        policy = build_default_policy()
        policy.budget.max_admission_llm_tokens_per_task = 1000
        return policy
    with factory() as session:
        task, _ = TaskService(session, policy_factory=configured_policy).create_task("title", "description", "repo", uuid4())
        return task.id


def test_concurrent_claims_only_one_wins(test_session_factory):
    task_id = create_task(test_session_factory)
    barrier = Barrier(2, timeout=10)
    def claim(owner):
        with test_session_factory() as session:
            barrier.wait()
            return AdmissionClaimService(session).claim(task_id, owner=owner, lease_seconds=30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    assert sum(item is not None for item in claims) == 1


def test_expired_claim_replaced_and_old_owner_cannot_release(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory() as session:
        first = AdmissionClaimService(session).claim(task_id, owner="worker-a", lease_seconds=30)
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with test_session_factory() as session:
        second = AdmissionClaimService(session).claim(task_id, owner="worker-b", lease_seconds=30)
        assert second.token != first.token
        with pytest.raises(AdmissionClaimDeniedError):
            AdmissionClaimService(session).release(first)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.admission_claim_token == second.token
        assert task.admission_attempt_count == 2
        assert task.admission_blocked is None
    with test_session_factory() as session:
        AdmissionClaimService(session).release(second)
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_claim_token is None


def test_active_claim_blocks_unclaimed_admission_writes(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory() as session:
        AdmissionClaimService(session).claim(task_id, owner="worker-a", lease_seconds=30)
    with test_session_factory() as session:
        with pytest.raises(AdmissionClaimDeniedError):
            TaskService(session).apply_admission_decision(task_id, TaskStatus.DECLINED,
                admission_record=AdmissionRecord(reason="decline", evidence=[]))
        with pytest.raises(AdmissionClaimDeniedError):
            AdmissionBudgetService(session).reserve(task_id, estimated_input_tokens=1, max_output_tokens=1)
    with test_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.PENDING


def test_automatic_retry_claims_only_ready_timeout_and_has_one_winner(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": 1}
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    barrier = Barrier(2, timeout=10)
    def claim(owner):
        with test_session_factory() as session:
            barrier.wait()
            return AdmissionClaimService(session).claim(task_id, owner=owner, lease_seconds=30, automatic_retry=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    assert sum(item is not None for item in claims) == 1


def test_legacy_unknown_attempt_count_stays_unknown(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory.begin() as session:
        session.get(Task, task_id).admission_attempt_count = None
    with test_session_factory() as session:
        assert AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=30) is not None
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_attempt_count is None


def test_failed_claim_commit_does_not_count_as_started(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory() as session:
        def fail_commit(_session):
            raise RuntimeError("commit failed")
        event.listen(session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="commit failed"):
            AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=30)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.admission_attempt_count == 0
        assert task.admission_claim_token is None


def test_crashes_cannot_trigger_unlimited_automatic_claims(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory() as session:
        assert AdmissionClaimService(session).claim(task_id, owner="initial", lease_seconds=30) is not None
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": 1}
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    for expected_count in range(2, 6):
        with test_session_factory.begin() as session:
            task = session.get(Task, task_id)
            task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        with test_session_factory() as session:
            assert AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=30,
                                                        automatic_retry=True) is not None
        with test_session_factory() as session:
            task = session.get(Task, task_id)
            assert task.admission_attempt_count == expected_count
            assert task.admission_blocked["consecutive_timeouts"] == 1
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with test_session_factory() as session:
        assert AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=30,
                                                    automatic_retry=True) is None
        assert AdmissionClaimService(session).claim(task_id, owner="human", lease_seconds=30) is not None


def test_automatic_claim_requires_known_attempt_count_and_limit(test_session_factory):
    task_id = create_task(test_session_factory)
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_attempt_count = None
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": 1}
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    with test_session_factory() as session:
        assert AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=30,
                                                    automatic_retry=True) is None
