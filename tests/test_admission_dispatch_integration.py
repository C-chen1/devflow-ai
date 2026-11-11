from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4
import json
import os
import subprocess
import sys
import pytest

from sqlalchemy import func, select

from devflow_ai.admission_claim import AdmissionClaimService
from devflow_ai.admission_dispatch import dispatch_one_admission
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import TaskStatus
from devflow_ai.harness import RepositoryHeadObservation
from devflow_ai.model_gateway import AdmissionModelGateway, ModelReply
from devflow_ai.models import Task
from devflow_ai.policy import build_default_policy
from devflow_ai.services import TaskService


def make_task(factory, repo="approved", *, enable_budget=True, token_budget=1000):
    def policy_factory():
        policy = build_default_policy()
        policy.budget.max_admission_llm_tokens_per_task = token_budget if enable_budget else None
        return policy
    with factory() as session:
        task, _ = TaskService(session, policy_factory=policy_factory).create_task("title", "description", repo, uuid4())
        return task.id


def gateway(factory):
    return AdmissionModelGateway(session_factory=factory, estimate_input_tokens=lambda _: 100,
        max_output_tokens=100, generate=lambda *args: ModelReply(
            {"outcome": "ELIGIBLE", "reason": "ready", "evidence_refs": ["repository_head"]},
            ModelTokenUsage(input_tokens=20, output_tokens=10)))


def test_dispatches_only_approved_configured_repository(test_session_factory):
    untrusted = make_task(test_session_factory, "untrusted")
    unconfigured = make_task(test_session_factory, enable_budget=False)
    target = make_task(test_session_factory)
    calls = []
    def observe(repo, **kwargs):
        calls.append(repo)
        return RepositoryHeadObservation(status="observed", base_commit="a" * 40)
    result = dispatch_one_admission(session_factory=test_session_factory, gateway=gateway(test_session_factory),
        trusted_repositories=frozenset({"approved"}), timeout_seconds=5, observe=observe)
    assert result.task_id == target and result.attempt.outcome == "ELIGIBLE" and calls == ["approved"]
    with test_session_factory() as session:
        assert session.get(Task, untrusted).status == TaskStatus.PENDING
        assert session.get(Task, unconfigured).admission_attempt_count == 0
        assert session.get(Task, target).status == TaskStatus.ELIGIBLE


def test_cursor_passes_blocked_task_and_does_not_skip_later_work(test_session_factory):
    first = make_task(test_session_factory)
    second = make_task(test_session_factory)
    with test_session_factory.begin() as session:
        task = session.get(Task, first)
        task.admission_blocked = {"retry_hint": "needs_review", "consecutive_timeouts": 0}
    options = dict(session_factory=test_session_factory, gateway=gateway(test_session_factory),
                   trusted_repositories=frozenset({"approved"}), timeout_seconds=5, page_size=1,
                   observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    first_page = dispatch_one_admission(**options)
    assert first_page.task_id is None and first_page.next_cursor is not None
    next_page = dispatch_one_admission(**options, after=first_page.next_cursor)
    assert next_page.task_id == second and next_page.attempt.outcome == "ELIGIBLE"


def test_scanner_skips_a_locked_task_and_processes_the_next(test_session_factory):
    first = make_task(test_session_factory)
    second = make_task(test_session_factory)
    with test_session_factory() as locker, locker.begin():
        locker.scalar(select(Task).where(Task.id == first).with_for_update())
        result = dispatch_one_admission(session_factory=test_session_factory, gateway=gateway(test_session_factory),
            trusted_repositories=frozenset({"approved"}), timeout_seconds=5,
            observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
        assert result.task_id == second


def test_recover_first_attempt_only_after_expired_claim_and_delay(test_session_factory):
    task_id = make_task(test_session_factory)
    with test_session_factory() as session:
        claim = AdmissionClaimService(session).claim(task_id, owner="crashed", lease_seconds=30, automatic_initial=True)
        assert claim is not None
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=10)
    options = dict(session_factory=test_session_factory, gateway=gateway(test_session_factory),
        trusted_repositories=frozenset({"approved"}), timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    assert dispatch_one_admission(**options).task_id is None
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    result = dispatch_one_admission(**options)
    assert result.task_id == task_id and result.attempt.outcome == "ELIGIBLE"
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_attempt_count == 2


def test_two_dispatchers_cannot_execute_the_same_task(test_session_factory):
    task_id = make_task(test_session_factory)
    entered, resume = Event(), Event()
    def observe(*args, **kwargs):
        entered.set()
        assert resume.wait(10)
        return RepositoryHeadObservation(status="observed", base_commit="a" * 40)
    options = dict(session_factory=test_session_factory, gateway=gateway(test_session_factory),
                   trusted_repositories=frozenset({"approved"}), timeout_seconds=5, observe=observe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(dispatch_one_admission, **options)
        assert entered.wait(10)
        try:
            second = dispatch_one_admission(**options)
            assert second.task_id is None
        finally:
            resume.set()
        assert first.result(timeout=10).task_id == task_id


def test_model_failure_is_recorded_without_automatic_repeat(test_session_factory):
    task_id = make_task(test_session_factory)
    calls = []
    def fail(*args):
        calls.append("model")
        raise TimeoutError("private transport details")
    model = AdmissionModelGateway(session_factory=test_session_factory, estimate_input_tokens=lambda _: 100,
                                  max_output_tokens=100, generate=fail)
    options = dict(session_factory=test_session_factory, gateway=model, trusted_repositories=frozenset({"approved"}),
                   timeout_seconds=5,
                   observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    with pytest.raises(TimeoutError):
        dispatch_one_admission(**options)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING
        assert task.admission_blocked["kind"] == "execution_error"
        assert task.admission_blocked["retry_hint"] == "needs_review"
        assert "private transport details" not in task.admission_blocked["reason"]
    assert dispatch_one_admission(**options).task_id is None
    assert calls == ["model"]


def test_scan_surfaces_reached_limit_and_processes_next_task(test_session_factory):
    exhausted = make_task(test_session_factory)
    ready = make_task(test_session_factory)
    with test_session_factory.begin() as session:
        task = session.get(Task, exhausted)
        task.admission_attempt_count = 5
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": 1}
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    result = dispatch_one_admission(session_factory=test_session_factory, gateway=gateway(test_session_factory),
        trusted_repositories=frozenset({"approved"}), timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    assert result.task_id == ready
    with test_session_factory() as session:
        task = session.get(Task, exhausted)
        assert task.status == TaskStatus.PENDING
        assert task.admission_intervention["reason_codes"] == ["ADMISSION_ATTEMPT_LIMIT"]


@pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE_ADMISSION_TEST") != "1", reason="Explicit live model test only")
def test_dispatch_cli_real_model_and_isolated_database(test_session_factory, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    task_id = make_task(test_session_factory, str(tmp_path), token_budget=10000)
    schema = test_session_factory.kw["bind"].get_execution_options()["schema_translate_map"][None]
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema},public"}
    result = subprocess.run([sys.executable, "-m", "devflow_ai.admission_dispatch",
                             "--trusted-repo", str(tmp_path)], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["task_id"] == str(task_id)
    assert output["outcome"] in ("BLOCKED", "DECLINED", "ELIGIBLE")
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.admission_attempt_count == 1 and len(task.admission_usage) == 1
        assert task.status == (TaskStatus.PENDING if output["outcome"] == "BLOCKED" else TaskStatus(output["outcome"]))
    print(f"live dispatch outcome={output['outcome']}; scanned={output['scanned_count']}")
