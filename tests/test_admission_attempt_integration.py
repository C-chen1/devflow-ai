import os
import json
import subprocess
import sys
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from devflow_ai.admission import (assess_recorded_timeout_limit, attempt_admission,
                                  inspect_timeout_retry, retry_timed_out_admission)
from devflow_ai.admission_claim import AdmissionClaimDeniedError, AdmissionClaimService
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.enums import TaskStatus
from devflow_ai.harness import RepositoryHeadObservation
from devflow_ai.model_gateway import AdmissionModelGateway, ModelReply
from devflow_ai.models import Run, Task
from devflow_ai.policy import PolicyConfig, build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker import Worker


def create_task(factory, repo, *, token_budget=2000, title="Fix issue", description="Expected behavior is different"):
    def policy_factory():
        policy = build_default_policy()
        policy.budget.max_admission_llm_tokens_per_task = token_budget
        return policy
    with factory() as session:
        task, _ = TaskService(session, policy_factory=policy_factory).create_task(
            title, description, str(repo), uuid4())
        return task.id


def gateway(factory, decision):
    return AdmissionModelGateway(session_factory=factory, estimate_input_tokens=lambda _: 100,
        max_output_tokens=100, generate=lambda prompt, maximum: ModelReply(
            output=decision, usage=ModelTokenUsage(input_tokens=90, output_tokens=50)))


def test_admission_pins_observed_real_repository_head(test_session_factory, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-q", "--allow-empty", "-m", "test"], check=True)
    expected = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    task_id = create_task(test_session_factory, tmp_path)
    decision = {"outcome": "ELIGIBLE", "reason": "Issue is actionable", "evidence_refs": ["issue", "repository_head"]}
    result = attempt_admission(task_id, session_factory=test_session_factory,
        gateway=gateway(test_session_factory, decision), trusted_repository=lambda repo: repo == str(tmp_path),
        timeout_seconds=10)
    assert result.outcome == "ELIGIBLE"
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.ELIGIBLE and task.base_commit == expected
        assert expected in task.admission_record["evidence"][1]
        assert len(task.admission_usage) == 1


@pytest.mark.parametrize("status", ["timeout", "unavailable", "invalid_output"])
def test_repository_unknown_leaves_pending_without_model(test_session_factory, status):
    task_id = create_task(test_session_factory, "trusted")
    def no_model(*args):
        raise AssertionError("model should not be called")
    model = AdmissionModelGateway(session_factory=test_session_factory,
        estimate_input_tokens=lambda _: 10, max_output_tokens=10, generate=no_model)
    result = attempt_admission(task_id, session_factory=test_session_factory, gateway=model,
        trusted_repository=lambda _: True, timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status=status))
    assert result.outcome == "BLOCKED"
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING and task.admission_record is None and task.admission_usage == {}
        assert task.admission_blocked["kind"] == "repository_check"
        assert task.admission_blocked["retry_hint"] == ("transient" if status == "timeout" else "needs_review")
        assert task.admission_blocked["consecutive_timeouts"] == (1 if status == "timeout" else 0)
        assert task.admission_blocked_at is not None and task.admission_blocked_at.tzinfo is not None


def test_invented_reference_does_not_update_task(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    decision = {"outcome": "ELIGIBLE", "reason": "ready", "evidence_refs": ["invented"]}
    with pytest.raises(ValueError, match="supplied observations"):
        attempt_admission(task_id, session_factory=test_session_factory,
            gateway=gateway(test_session_factory, decision), trusted_repository=lambda _: True, timeout_seconds=5,
            observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING and task.admission_record is None
        assert len(task.admission_usage) == 1


@pytest.mark.parametrize("outcome", ["DECLINED", "BLOCKED"])
def test_decline_and_blocked_leave_no_base_commit(test_session_factory, outcome):
    task_id = create_task(test_session_factory, "trusted")
    decision = {"outcome": outcome, "reason": "Issue details are insufficient", "evidence_refs": ["issue"]}
    if outcome == "BLOCKED":
        decision["missing_information"] = "Expected behavior"
    result = attempt_admission(task_id, session_factory=test_session_factory,
        gateway=gateway(test_session_factory, decision), trusted_repository=lambda _: True, timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    assert result.outcome == outcome
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == (TaskStatus.DECLINED if outcome == "DECLINED" else TaskStatus.PENDING)
        assert task.base_commit is None
        assert (task.admission_record is not None) == (outcome == "DECLINED")
        if outcome == "BLOCKED":
            assert task.admission_blocked["missing_information"] == "Expected behavior"
            assert task.admission_blocked["retry_hint"] == "needs_review"
            assert task.admission_blocked_at is not None


def test_untrusted_repository_does_not_run_a_probe(test_session_factory):
    task_id = create_task(test_session_factory, "untrusted")
    def unexpected_probe(*args, **kwargs):
        raise AssertionError("untrusted repository was probed")
    result = attempt_admission(task_id, session_factory=test_session_factory,
        gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: False,
        timeout_seconds=5, observe=unexpected_probe)
    assert result.outcome == "BLOCKED"


def test_late_blocked_observation_cannot_overwrite_completed_admission(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    def late_timeout(*args, **kwargs):
        with test_session_factory.begin() as session:
            task = session.get(Task, task_id)
            task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        with test_session_factory() as session:
            new_claim = AdmissionClaimService(session).claim(task_id, owner="new-worker", lease_seconds=60)
        with test_session_factory() as session:
            TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE,
                admission_record=AdmissionRecord(reason="completed", evidence=["verified"]), base_commit="a" * 40,
                claim=new_claim)
        return RepositoryHeadObservation(status="timeout")
    with pytest.raises(AdmissionClaimDeniedError):
        attempt_admission(task_id, session_factory=test_session_factory,
            gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: True,
            timeout_seconds=5, observe=late_timeout)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.ELIGIBLE
        assert task.admission_blocked is None and task.admission_blocked_at is None


def test_late_model_result_is_not_committed_but_usage_is_settled(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    new_claims = []
    def generate(prompt, maximum):
        with test_session_factory.begin() as session:
            task = session.get(Task, task_id)
            task.admission_claim_expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
        with test_session_factory() as session:
            new_claims.append(AdmissionClaimService(session).claim(task_id, owner="new-worker", lease_seconds=60))
        return ModelReply(output={"outcome": "ELIGIBLE", "reason": "ready", "evidence_refs": ["repository_head"]},
                          usage=ModelTokenUsage(input_tokens=40, output_tokens=10))
    model = AdmissionModelGateway(session_factory=test_session_factory, estimate_input_tokens=lambda _: 100,
                                  max_output_tokens=100, generate=generate)
    with pytest.raises(AdmissionClaimDeniedError):
        attempt_admission(task_id, session_factory=test_session_factory, gateway=model,
            trusted_repository=lambda _: True, timeout_seconds=5,
            observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING and task.admission_record is None
        assert task.admission_claim_token == new_claims[0].token
        usage = next(iter(task.admission_usage.values()))
        assert usage["actual_usage"] == {"input_tokens": 40, "output_tokens": 10}


def test_concurrent_attempts_only_one_probes_repository(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    from threading import Event
    entered = Event()
    resume = Event()
    def observe(*args, **kwargs):
        entered.set()
        assert resume.wait(10)
        return RepositoryHeadObservation(status="timeout")
    def attempt():
        return attempt_admission(task_id, session_factory=test_session_factory,
            gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: True,
            timeout_seconds=5, observe=observe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(attempt)
        assert entered.wait(10)
        try:
            with pytest.raises(AdmissionClaimDeniedError):
                attempt()
        finally:
            resume.set()
        assert first.result(timeout=10).outcome == "BLOCKED"
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_blocked["consecutive_timeouts"] == 1


def test_prior_timeout_record_without_count_stays_unknown(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_blocked = {"retry_hint": "transient", "reason": "old timeout"}
    attempt_admission(task_id, session_factory=test_session_factory,
        gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: True, timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status="timeout"))
    with test_session_factory() as session:
        assert session.get(Task, task_id).admission_blocked["consecutive_timeouts"] is None


def test_recorded_timeouts_reach_snapshotted_limit(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    for expected in ("BELOW_LIMIT", "BELOW_LIMIT", "LIMIT_REACHED"):
        attempt_admission(task_id, session_factory=test_session_factory,
            gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: True, timeout_seconds=5,
            observe=lambda *a, **k: RepositoryHeadObservation(status="timeout"))
        with test_session_factory() as session:
            task = session.get(Task, task_id)
            budget = PolicyConfig.model_validate(task.policy_snapshot).budget
            assert assess_recorded_timeout_limit(task.admission_blocked, budget) == expected


def test_retry_readiness_uses_persisted_time_and_stops_at_limit(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    def timed_out():
        attempt_admission(task_id, session_factory=test_session_factory,
            gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: True, timeout_seconds=5,
            observe=lambda *a, **k: RepositoryHeadObservation(status="timeout"))
    timed_out()
    waiting = inspect_timeout_retry(task_id, session_factory=test_session_factory)
    assert waiting.status == "WAIT" and waiting.retry_at.tzinfo is not None
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    assert inspect_timeout_retry(task_id, session_factory=test_session_factory).status == "READY"
    timed_out()
    timed_out()
    assert inspect_timeout_retry(task_id, session_factory=test_session_factory).status == "LIMIT_REACHED"


def test_retry_readiness_requires_configured_delay(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    attempt_admission(task_id, session_factory=test_session_factory,
        gateway=gateway(test_session_factory, {}), trusted_repository=lambda _: True, timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status="timeout"))
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        budget = dict(snapshot["budget"])
        budget.pop("admission_timeout_retry_delay_seconds")
        task.policy_snapshot = {**snapshot, "budget": budget}
    assert inspect_timeout_retry(task_id, session_factory=test_session_factory).status == "UNKNOWN"


def test_retry_readiness_respects_attempt_limit(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_attempt_count = 5
        task.admission_blocked = {"retry_hint": "transient", "consecutive_timeouts": 1}
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    assert inspect_timeout_retry(task_id, session_factory=test_session_factory).status == "ATTEMPT_LIMIT_REACHED"


def test_retry_readiness_does_not_report_ready_during_active_claim(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    with test_session_factory() as session:
        claim = AdmissionClaimService(session).claim(task_id, owner="worker", lease_seconds=60)
        assert claim is not None
    assert inspect_timeout_retry(task_id, session_factory=test_session_factory).status == "IN_PROGRESS"


def test_automatic_timeout_retry_waits_and_stops_after_three(test_session_factory):
    task_id = create_task(test_session_factory, "trusted")
    calls = []
    def timeout(*args, **kwargs):
        calls.append("probe")
        return RepositoryHeadObservation(status="timeout")
    kwargs = dict(session_factory=test_session_factory, gateway=gateway(test_session_factory, {}),
                  trusted_repository=lambda _: True, timeout_seconds=5, observe=timeout)
    assert retry_timed_out_admission(task_id, **kwargs) is None
    assert attempt_admission(task_id, **kwargs).outcome == "BLOCKED"
    assert retry_timed_out_admission(task_id, **kwargs) is None
    for _ in range(2):
        with test_session_factory.begin() as session:
            task = session.get(Task, task_id)
            task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
        assert retry_timed_out_admission(task_id, **kwargs).outcome == "BLOCKED"
    with test_session_factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_blocked_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=61)
    assert retry_timed_out_admission(task_id, **kwargs) is None
    assert calls == ["probe", "probe", "probe"]


def test_invalid_model_output_does_not_update_task(test_session_factory):
    from pydantic import ValidationError
    task_id = create_task(test_session_factory, "trusted")
    with pytest.raises(ValidationError):
        attempt_admission(task_id, session_factory=test_session_factory,
            gateway=gateway(test_session_factory, {"outcome": "ELIGIBLE"}),
            trusted_repository=lambda _: True, timeout_seconds=5,
            observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.PENDING and task.admission_record is None
        assert len(task.admission_usage) == 1


def test_approved_admission_can_be_claimed_as_first_run(run_session_factory):
    factory = run_session_factory
    task_id = create_task(factory, "trusted")
    decision = {"outcome": "ELIGIBLE", "reason": "ready", "evidence_refs": ["issue", "repository_head"]}
    result = attempt_admission(task_id, session_factory=factory, gateway=gateway(factory, decision),
        trusted_repository=lambda _: True, timeout_seconds=5,
        observe=lambda *a, **k: RepositoryHeadObservation(status="observed", base_commit="a" * 40))
    assert result.outcome == "ELIGIBLE"
    receipt = Worker(factory, "admission-worker").claim_once()
    with factory() as session:
        assert session.get(Run, receipt.run_id).task_id == task_id
        assert session.get(Task, task_id).status == TaskStatus.RUNNING


@pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE_ADMISSION_TEST") != "1", reason="Explicit live model test only")
def test_real_model_admission_on_local_repository(test_session_factory, tmp_path):
    import httpx
    from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "greeting.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "greeting.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-q", "-m", "fixture"], check=True)
    task_id = create_task(test_session_factory, tmp_path, token_budget=10000,
        title="Correct greeting output",
        description='greeting.py defines greet(). It currently returns "hello"; '
                    'change it to return "hello devflow". Reproduce by calling greet().')
    with httpx.Client() as client:
        model = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=30)
        model_gateway = AdmissionModelGateway(session_factory=test_session_factory,
            estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")),
            generate=model, max_output_tokens=300)
        result = attempt_admission(task_id, session_factory=test_session_factory, gateway=model_gateway,
            trusted_repository=lambda repo: repo == str(tmp_path), timeout_seconds=10)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        entries = list(task.admission_usage.values())
        assert len(entries) == 1 and entries[0]["actual_usage"] is not None
        assert result.outcome in ("ELIGIBLE", "DECLINED", "BLOCKED")
        expected = TaskStatus.PENDING if result.outcome == "BLOCKED" else TaskStatus(result.outcome)
        assert task.status == expected
        print(f"live admission outcome={result.outcome}; reason={result.reason}; usage={entries[0]['actual_usage']}")


@pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE_ADMISSION_TEST") != "1", reason="Explicit live model test only")
def test_real_admission_cli_isolated_process(test_session_factory, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    task_id = create_task(test_session_factory, tmp_path, token_budget=10000,
        title="Correct greeting output",
        description='Change greet() to return "hello devflow" instead of "hello".')
    schema = test_session_factory.kw["bind"].get_execution_options()["schema_translate_map"][None]
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema},public"}
    result = subprocess.run([sys.executable, "-m", "devflow_ai.admission", "--task-id", str(task_id),
                             "--trusted-repo", str(tmp_path)], env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["task_id"] == str(task_id)
    with test_session_factory() as session:
        task = session.get(Task, task_id)
        assert len(task.admission_usage) == 1
        assert task.status == (TaskStatus.PENDING if output["outcome"] == "BLOCKED" else TaskStatus(output["outcome"]))
    print(f"live CLI outcome={output['outcome']}; reason={output['reason']}")
