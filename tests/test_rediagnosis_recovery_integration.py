from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, BrokenBarrierError, Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import event, func, inspect, select, text

import devflow_ai.control as control_module
import devflow_ai.coordinator as coordinator_module
from devflow_ai.config import HeartbeatConfig
from devflow_ai.control import main as control_main
from devflow_ai.control import reclaim_expired_runs as control_reclaim_expired_runs
import devflow_ai.worker as worker_module
from devflow_ai.coordinator import (
    LEASE_EXPIRED_SIGNATURE, FailureDecision, RecoveryDecisionUnavailableError, RunCoordinator,
    RunCreationConflictError, RunExecutionDeniedError, RunLeaseNotExpiredError, TaskClaimBusyError,
)
from devflow_ai.enums import (
    AgentExecutionStatus, AgentRole, FailureCategory, RunStatus, TaskStatus, ToolCallStatus, VerificationResult,
)
from devflow_ai.harness import run_registered_command
from devflow_ai.independent_evaluation import IndependentEvaluationMaterial, PrivateCase
from devflow_ai.context import ContextBuilder
from devflow_ai.models import AgentExecution, Artifact, FailureRecord, Handoff, Run, Task, ToolCall, Verification
from devflow_ai.model_gateway import ModelGateway, ModelReply, ModelTokenUsage, PlannerModelGateway
from devflow_ai.orchestration import CommittedHandoff
from devflow_ai.policy import build_default_policy
from devflow_ai.repositories import TaskRepository
from devflow_ai.services import AdmissionRecord, TaskService, TaskStateConflictError
from devflow_ai.supervisor import VerificationFailureProposal
from devflow_ai.verification_config import VerificationConfig
from devflow_ai.verifier import load_pytest_report_artifact
from devflow_ai.worker import PlannerSupervisorContextError, RunDirectories, Worker
from devflow_ai.workspace_lock import (
    WorkspaceLockUnavailableError, WorkspaceQuarantinedError, workspace_operation_lock,
)


@pytest.fixture
def recovery_session_factory(run_session_factory):
    """Add the failure/verification tables inside this test's isolated schema only."""
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision_id in ("c7e1a29b4d63", "d4f63c192ba8", "e8c27b14a693",
                                "62eae84cebd1", "97f3a25cb190",
                                "a43cb50f67e9", "f154b6e82639",
                                "f83ab2d16c49", "ab6e1c30f42d", "4b79c6e21a50"):
                scripts.get_revision(revision_id).module.upgrade()
    return run_session_factory


def rediagnosis_policy():
    policy = build_default_policy()
    policy.policy_version = "rediagnosis-test-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = ["apply_patch"]
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = ["run_command", "git_diff"]
    return policy


def create_running_run(
    factory, *, worker_id="rediagnosis-worker", repo="example-repo", base_commit="a" * 40,
    policy_factory=rediagnosis_policy,
):
    with factory() as session:
        task, _ = TaskService(session, policy_factory=policy_factory).create_task(
            title="Rediagnosis test", description="Start exactly the recorded rediagnosis",
            repo=repo, idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="test decision", evidence=["fixture"]))
    worker = Worker(factory, worker_id)
    run_id = worker.claim_once().run_id
    return worker, task_id, run_id


def record_unknown_failure(
    factory, task_id, run_id, *, signature="unknown-v1:test", action="REDIAGNOSE",
    task_status=TaskStatus.RETRYING,
):
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(timezone.utc)
        session.get(Task, task_id).status = task_status
        session.add(Verification(run_id=run_id, result=VerificationResult.FAILED,
                                 evidence={"policy_reasons": []}))
        session.add(FailureRecord(run_id=run_id, category=FailureCategory.UNKNOWN_FAILURE,
                                  signature=signature, recovery_action=action))


def count_rediagnosis_runs(factory, task_id):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id, Run.recovery_action == "REDIAGNOSE"))


def count_rediagnosis_starts(factory, task_id):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id, Run.recovery_action == "REDIAGNOSE",
            Run.recovery_started_at.is_not(None)))


def _exhausted_rediagnosis_policy():
    policy = rediagnosis_policy()
    return policy.model_copy(update={"budget": policy.budget.model_copy(
        update={"max_rediagnose_attempts": 0})})


def _expire_run_with_one_pending_call(factory, run_id):
    """Expire one Run and leave a single in-flight ToolCall it cannot settle."""
    with factory.begin() as session:
        call = ToolCall(run_id=run_id, agent_execution_id=None, tool_name="git_clone",
                        args={}, status=ToolCallStatus.RUNNING)
        session.add(call)
        session.flush()
        call_id = call.id
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    return call_id


def _expired_run_decision(recovery_action="REDIAGNOSE", signature="lease-expired-v1"):
    return FailureDecision(category=FailureCategory.UNKNOWN_FAILURE,
                           signature=signature, recovery_action=recovery_action)


def test_expired_run_closes_with_a_verified_unknown_failure_decision(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-close-worker")
    call_id = _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        failure_id = RunCoordinator(session).close_expired_run(
            run_id, decision=_expired_run_decision())

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED and run.finished_at is not None
        failure = session.get(FailureRecord, failure_id)
        assert failure.category == FailureCategory.UNKNOWN_FAILURE
        assert failure.signature == "lease-expired-v1"
        assert failure.recovery_action == "REDIAGNOSE"
        # A further attempt leaves the Task recoverable, as every other failure
        # commit does.
        assert session.get(Task, task_id).status == TaskStatus.RETRYING
        # The in-flight call cannot be attributed, so it is recorded as unknown.
        call = session.get(ToolCall, call_id)
        assert call.status == ToolCallStatus.UNKNOWN_RESULT
        assert call.reason == "WORKER_LEASE_EXPIRED" and call.exit_code is None


def test_expired_run_close_rejects_actions_it_cannot_support(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-close-reject")
    _expire_run_with_one_pending_call(factory, run_id)

    for decision in (
        FailureDecision(category=FailureCategory.IMPLEMENTATION_FAILURE,
                        signature="implementation-v1", recovery_action="REVISE_IMPLEMENTATION"),
        _expired_run_decision(recovery_action="RETRY_SAME_STEP"),
    ):
        with factory() as session:
            with pytest.raises(RecoveryDecisionUnavailableError):
                RunCoordinator(session).close_expired_run(run_id, decision=decision)

    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING
        assert session.get(Run, run_id).finished_at is None
        assert not session.scalars(select(FailureRecord)).all()


def test_expired_run_close_rechecks_the_rediagnosis_quota(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(
        factory, worker_id="expired-close-quota", policy_factory=_exhausted_rediagnosis_policy)
    _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        with pytest.raises(RecoveryDecisionUnavailableError):
            RunCoordinator(session).close_expired_run(run_id, decision=_expired_run_decision())
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING

    # Escalation stays available when the quota is exhausted.
    with factory() as session:
        failure_id = RunCoordinator(session).close_expired_run(
            run_id, decision=_expired_run_decision(recovery_action="ESCALATE_TO_HUMAN"))
    with factory() as session:
        assert session.get(FailureRecord, failure_id).recovery_action == "ESCALATE_TO_HUMAN"
        assert session.get(Run, run_id).status == RunStatus.FAILED
        assert session.get(Task, task_id).status == TaskStatus.ESCALATED


def test_expired_run_close_needs_an_expired_run_and_does_not_repeat(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-close-once")

    with factory() as session:
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session).close_expired_run(run_id, decision=_expired_run_decision())

    _expire_run_with_one_pending_call(factory, run_id)
    with factory() as session:
        first = RunCoordinator(session).close_expired_run(run_id, decision=_expired_run_decision())
    with factory() as session:
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session).close_expired_run(run_id, decision=_expired_run_decision())
    with factory() as session:
        records = session.scalars(select(FailureRecord).where(FailureRecord.run_id == run_id)).all()
        assert [record.id for record in records] == [first]



@pytest.mark.parametrize("run_status", [RunStatus.RUNNING, RunStatus.FAILED])
def test_expired_close_replay_rejects_unrelated_failure(recovery_session_factory, run_status):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    with factory.begin() as session:
        session.get(Run, run_id).status = run_status
        session.add(FailureRecord(run_id=run_id, category=FailureCategory.IMPLEMENTATION_FAILURE,
                                  signature="unrelated", recovery_action="REVISE_IMPLEMENTATION"))
        session.add(Verification(run_id=run_id, result=VerificationResult.FAILED,
                                 evidence={"scope": "ordinary_test_failure"}))
    with pytest.raises(RunLeaseNotExpiredError):
        worker._close_expired_run_once(run_id)
    with factory() as session:
        assert session.get(Run, run_id).status == run_status
        assert session.scalar(select(func.count()).select_from(FailureRecord).where(
            FailureRecord.run_id == run_id)) == 1


def test_expired_close_replay_requires_lease_expiry_verification(recovery_session_factory):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.FAILED
        session.add(FailureRecord(run_id=run_id, category=FailureCategory.UNKNOWN_FAILURE,
                                  signature=LEASE_EXPIRED_SIGNATURE, recovery_action="REDIAGNOSE"))
        session.add(Verification(run_id=run_id, result=VerificationResult.FAILED,
                                 evidence={"scope": "no_verification", "reason": "other",
                                           "run_id": str(run_id)}))
    with pytest.raises(RunLeaseNotExpiredError):
        worker._close_expired_run_once(run_id)


def test_expired_run_close_is_idempotent_under_concurrent_workers(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-close-race")
    call_id = _expire_run_with_one_pending_call(factory, run_id)

    barrier = Barrier(2)

    def _attempt(worker_id):
        w = Worker(factory, worker_id)
        barrier.wait(timeout=10)
        return w._close_expired_run_once(run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_attempt, ["race-worker-a", "race-worker-b"]))

    assert len(results) == 2
    assert all(r is not None for r in results)
    assert results[0]["failure_id"] == results[1]["failure_id"]
    failure_id = results[0]["failure_id"]

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED and run.finished_at is not None

        records = session.scalars(
            select(FailureRecord).where(FailureRecord.run_id == run_id)).all()
        assert len(records) == 1 and records[0].id == failure_id

        verifications = session.scalars(
            select(Verification).where(Verification.run_id == run_id)).all()
        failed = [v for v in verifications if v.result == VerificationResult.FAILED]
        assert len(failed) == 1

        assert session.get(Task, task_id).status in (
            TaskStatus.RETRYING, TaskStatus.ESCALATED)

        call = session.get(ToolCall, call_id)
        assert call.status == ToolCallStatus.UNKNOWN_RESULT
        assert call.reason == "WORKER_LEASE_EXPIRED" and call.exit_code is None


def _seed_attempt_run(factory, task_id, *, status, with_record=False):
    """Add one auxiliary Run so history and signature limits can be seeded."""
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        attempt = (session.scalar(select(func.max(Run.attempt_number)).where(
            Run.task_id == task_id)) or 0) + 1
        run = Run(task_id=task_id, attempt_number=attempt, status=status,
                  creation_idempotency_key=f"lease-seed-{uuid4()}", worker_id="seed-worker",
                  recovery_action="REVISE_IMPLEMENTATION", started_at=now,
                  last_heartbeat_at=now, lease_expires_at=now,
                  finished_at=now if status == RunStatus.FAILED else None)
        session.add(run)
        session.flush()
        if status == RunStatus.RUNNING:
            # A RUNNING Run implies a RUNNING Task, as every real start path does.
            session.get(Task, task_id).status = TaskStatus.RUNNING
        run_id = run.id
        if with_record:
            session.add(FailureRecord(run_id=run_id, category=FailureCategory.UNKNOWN_FAILURE,
                                      signature=LEASE_EXPIRED_SIGNATURE,
                                      recovery_action="REDIAGNOSE"))
    return run_id


def test_expired_run_derives_rediagnose_with_the_lease_expired_signature(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-derived")
    call_id = _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        closed = RunCoordinator(session).close_expired_run_with_derived_decision(run_id)

    assert closed["outcome"] == "closed" and closed["recovery_action"] == "REDIAGNOSE"
    assert closed["category"] == FailureCategory.UNKNOWN_FAILURE.value
    assert closed["signature"] == LEASE_EXPIRED_SIGNATURE
    assert closed["policy_reasons"] == []
    with factory() as session:
        failure = session.get(FailureRecord, closed["failure_id"])
        assert failure.category == FailureCategory.UNKNOWN_FAILURE
        assert failure.signature == LEASE_EXPIRED_SIGNATURE
        assert failure.recovery_action == "REDIAGNOSE"
        assert session.get(Run, run_id).status == RunStatus.FAILED
        assert session.get(Task, task_id).status == TaskStatus.RETRYING
        assert session.get(ToolCall, call_id).status == ToolCallStatus.UNKNOWN_RESULT
    with factory() as session:
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session).close_expired_run_with_derived_decision(run_id)


def test_expired_run_derives_escalation_when_the_rediagnosis_quota_is_exhausted(
    recovery_session_factory,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(
        factory, worker_id="expired-derived-quota", policy_factory=_exhausted_rediagnosis_policy)
    _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        closed = RunCoordinator(session).close_expired_run_with_derived_decision(run_id)

    assert closed["recovery_action"] == "ESCALATE_TO_HUMAN"
    assert "rediagnose_limit" in closed["policy_reasons"]
    with factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.ESCALATED


def test_expired_run_derives_escalation_after_repeated_lease_expiries(recovery_session_factory):
    """The shared signature bounds repeated expiries instead of granting unlimited quota."""
    factory = recovery_session_factory

    def bounded_policy():
        policy = rediagnosis_policy()
        return policy.model_copy(update={"budget": policy.budget.model_copy(
            update={"max_same_signature_occurrences": 2})})

    worker, task_id, run_id = create_running_run(
        factory, worker_id="expired-derived-repeat", policy_factory=bounded_policy)
    outcomes = []
    target = run_id
    for index in range(3):
        if index:
            # One RUNNING Run per Task, so each expiry is seeded after the last close.
            target = _seed_attempt_run(factory, task_id, status=RunStatus.RUNNING)
        _expire_run_with_one_pending_call(factory, target)
        with factory() as session:
            outcomes.append(RunCoordinator(session).close_expired_run_with_derived_decision(target))

    assert [item["recovery_action"] for item in outcomes] == [
        "REDIAGNOSE", "REDIAGNOSE", "ESCALATE_TO_HUMAN"]
    assert "signature_limit" in outcomes[-1]["policy_reasons"]
    assert all(item["signature"] == LEASE_EXPIRED_SIGNATURE for item in outcomes)


def test_expired_run_derives_escalation_when_history_is_incomplete(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-derived-history")
    # A failed Run without its FailureRecord makes the history untrustworthy.
    _seed_attempt_run(factory, task_id, status=RunStatus.FAILED)
    _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        closed = RunCoordinator(session).close_expired_run_with_derived_decision(run_id)

    assert closed["recovery_action"] == "ESCALATE_TO_HUMAN"
    assert "unrecorded_failures" in closed["policy_reasons"]


def test_expired_run_close_unlocks_a_rediagnosis_run(recovery_session_factory):
    """The close records missing evidence, which is what the recovery entry accepts."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="expired-unlock")
    _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        closed = RunCoordinator(session).close_expired_run_with_derived_decision(run_id)
    assert closed["recovery_action"] == "REDIAGNOSE"

    with factory() as session:
        verification = session.scalar(select(Verification).where(Verification.run_id == run_id))
        assert verification.result == VerificationResult.FAILED
        # The row records that no check ran, not that a check failed.
        assert verification.evidence["scope"] == "no_verification"
        assert verification.evidence["reason"] == "worker_lease_expired"

    with factory() as session:
        run, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "expired-close-recovery")
        attempt_number, recovery_action = run.attempt_number, run.recovery_action
    assert created is True
    assert attempt_number == 2 and recovery_action == "REDIAGNOSE"
    with factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.RUNNING


def test_expired_run_close_escalation_blocks_recovery(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(
        factory, worker_id="expired-escalated", policy_factory=_exhausted_rediagnosis_policy)
    _expire_run_with_one_pending_call(factory, run_id)

    with factory() as session:
        closed = RunCoordinator(session).close_expired_run_with_derived_decision(run_id)
    assert closed["recovery_action"] == "ESCALATE_TO_HUMAN"

    with factory() as session:
        with pytest.raises(TaskStateConflictError):
            RunCoordinator(session).start_rediagnosis_run(
                task_id, worker.worker_id, "expired-escalated-recovery")


def test_expired_close_and_recover_escalation_starts_nothing(recovery_session_factory, tmp_path):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(
        factory, worker_id="expired-recover-escalate",
        policy_factory=_exhausted_rediagnosis_policy)
    _expire_run_with_one_pending_call(factory, run_id)
    workspaces, artifacts = tmp_path / "w", tmp_path / "a"
    workspaces.mkdir()
    artifacts.mkdir()

    result = worker.close_expired_run_and_recover(
        run_id, creation_idempotency_key="expired-escalate-key",
        workspaces_root=workspaces, artifacts_root=artifacts,
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        recovery_max_read_bytes=1024, stop_event=Event())

    assert result["outcome"] == "escalated" and result["rediagnosis"] is None
    assert result["recovery_action"] == "ESCALATE_TO_HUMAN"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 1
        assert session.get(Task, task_id).status == TaskStatus.ESCALATED


def test_expired_close_and_recover_starts_the_rediagnosis_run(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="expired-recover", repo=str(repo), base_commit=base_commit)
    _expire_run_with_one_pending_call(factory, run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "Review the expired attempt",
             "decision_required": "Decide how to continue"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)),
    )
    request = dict(
        creation_idempotency_key="expired-recover-key", workspaces_root=workspaces,
        artifacts_root=artifacts, supervisor_gateway=supervisor_gateway,
        explorer_gateway=None, planner_gateway=None, recovery_max_read_bytes=1024,
        stop_event=Event())

    result = worker.close_expired_run_and_recover(run_id, **request)

    assert result["recovery_action"] == "REDIAGNOSE"
    assert result["rediagnosis"]["outcome"] == "blocked"
    with factory() as session:
        runs = session.scalars(select(Run).where(
            Run.task_id == task_id).order_by(Run.attempt_number)).all()
        assert [item.attempt_number for item in runs] == [1, 2]
        assert runs[1].recovery_action == "REDIAGNOSE"

    # Repeating the same request reads the committed record: the expired Run is not
    # closed twice and no third Run appears.
    worker.close_expired_run_and_recover(run_id, **request)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 2
        assert session.scalar(select(func.count()).select_from(FailureRecord).where(
            FailureRecord.run_id == run_id)) == 1
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == run_id)) == 1


def test_expired_run_can_be_taken_over_by_another_worker(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="lost-worker")
    with factory() as session:
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session).take_over_expired_run(run_id, "recovery-worker-2")
    with factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output=None,
        )
        session.add(execution)
        session.flush()
        session.add(ToolCall(run_id=run_id, agent_execution_id=execution.id,
                             tool_name="read_file", args={"path": "README.md"},
                             status=ToolCallStatus.RUNNING))
        execution_id = execution.id
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    with factory() as session:
        taken = RunCoordinator(session).take_over_expired_run(run_id, "recovery-worker-2")

    assert taken["outcome"] == "taken_over"
    assert taken["previous_worker_id"] == "lost-worker"
    assert taken["worker_id"] == "recovery-worker-2"
    # The in-flight call cannot be attributed, so it is recorded, not replayed.
    assert taken["unknown_tool_calls"] == 1
    assert taken["orphaned_executions"] == [
        {"agent_execution_id": str(execution_id), "agent_role": AgentRole.EXPLORER.value.lower()}]
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.worker_id == "recovery-worker-2" and run.status == RunStatus.RUNNING
        assert run.lease_expires_at > run.started_at
        call = session.scalar(select(ToolCall).where(ToolCall.run_id == run_id))
        assert call.status == ToolCallStatus.UNKNOWN_RESULT
        assert call.reason == "WORKER_LEASE_EXPIRED"

    # The new owner can now act; the lost worker and a third party cannot.
    with factory() as session, session.begin():
        RunCoordinator(session).assert_execution_right(run_id, "recovery-worker-2")
    for other in ("lost-worker", "recovery-worker-3"):
        with factory() as session:
            with pytest.raises((RunExecutionDeniedError, RunLeaseNotExpiredError)):
                RunCoordinator(session).take_over_expired_run(run_id, other)


def test_takeover_is_refused_past_the_fixed_run_deadline(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="lost-worker")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        # A Run may not be revived past started_at + max_run_duration_seconds.
        run.started_at = datetime.now(timezone.utc) - timedelta(days=30)
        run.lease_expires_at = func.clock_timestamp()

    with factory() as session:
        with pytest.raises(RunExecutionDeniedError, match="duration deadline"):
            RunCoordinator(session).take_over_expired_run(run_id, "recovery-worker-2")

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.worker_id == "lost-worker" and run.status == RunStatus.RUNNING


def test_pending_expired_recovery_discovery_tracks_closed_unstarted_run(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="pending-discovery")
    with factory() as session:
        assert RunCoordinator(session).find_pending_expired_recovery_id() is None
    _expire_run_with_one_pending_call(factory, run_id)
    with factory() as session:
        RunCoordinator(session).close_expired_run_with_derived_decision(run_id)
    with factory() as session:
        assert RunCoordinator(session).find_pending_expired_recovery_id() == run_id
    with factory.begin() as session:
        verification = session.scalar(select(Verification).where(Verification.run_id == run_id))
        original = dict(verification.evidence)
        verification.evidence = {**original, "reason": "other"}
    with factory() as session:
        assert RunCoordinator(session).find_pending_expired_recovery_id() is None
    with factory.begin() as session:
        session.scalar(select(Verification).where(Verification.run_id == run_id)).evidence = original
    with factory() as session:
        _, created = RunCoordinator(session).start_rediagnosis_run(task_id, worker.worker_id, "pending-start")
        assert created
    with factory() as session:
        assert RunCoordinator(session).find_pending_expired_recovery_id() is None


def test_expired_run_discovery_ignores_live_runs(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="discovery-worker")
    with factory() as session:
        assert RunCoordinator(session).find_next_expired_run_id() is None
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    with factory() as session:
        assert RunCoordinator(session).find_next_expired_run_id() == run_id
    with factory() as session:
        RunCoordinator(session).close_expired_run_with_derived_decision(run_id)
    with factory() as session:
        assert RunCoordinator(session).find_next_expired_run_id() is None


def test_reclaim_next_expired_run_closes_and_starts_recovery(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="lost-worker", repo=str(repo), base_commit=base_commit)
    _expire_run_with_one_pending_call(factory, run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "Review the reclaimed attempt",
             "decision_required": "Decide how to continue"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)),
    )
    request = dict(
        workspaces_root=workspaces, artifacts_root=artifacts,
        supervisor_gateway=supervisor_gateway, explorer_gateway=None,
        planner_gateway=None, recovery_max_read_bytes=1024, stop_event=Event())

    result = worker.reclaim_next_expired_run(**request)

    assert result["outcome"] == "blocked"
    assert result["run_id"] == run_id
    assert result["recovery_action"] == "REDIAGNOSE"
    with factory() as session:
        runs = session.scalars(select(Run).where(
            Run.task_id == task_id).order_by(Run.attempt_number)).all()
        assert [item.attempt_number for item in runs] == [1, 2]
        assert runs[1].recovery_action == "REDIAGNOSE"

    # The expired Run is gone from discovery and the recovery Run holds a live
    # lease, so a second sweep is idle instead of closing anything twice.
    again = worker.reclaim_next_expired_run(**request)
    assert again["outcome"] == "idle" and again["run_id"] is None
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 2


def test_two_workers_reclaim_same_expired_run_without_duplicate_recovery(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    _, task_id, run_id = create_running_run(
        factory, worker_id="lost-worker", repo=str(repo), base_commit=base_commit)
    _expire_run_with_one_pending_call(factory, run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    barrier = Barrier(2)

    def same_discovery(_coordinator):
        barrier.wait(timeout=10)
        return run_id

    monkeypatch.setattr(RunCoordinator, "find_next_expired_run_id", same_discovery)

    def reclaim(worker_id):
        worker = Worker(factory, worker_id)
        gateway = ModelGateway(
            session_factory=factory, worker_id=worker_id,
            estimate_input_tokens=lambda _: 100, max_output_tokens=100,
            generate=lambda *_: ModelReply(
                {"outcome": "BLOCKED", "reason": "Review the reclaimed attempt",
                 "decision_required": "Decide how to continue"},
                ModelTokenUsage(input_tokens=40, output_tokens=20)))
        try:
            return worker.reclaim_next_expired_run(
                workspaces_root=workspaces, artifacts_root=artifacts,
                supervisor_gateway=gateway, explorer_gateway=None, planner_gateway=None,
                recovery_max_read_bytes=1024, stop_event=Event())
        except (RunExecutionDeniedError, TaskClaimBusyError) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in (
            pool.submit(reclaim, "reclaimer-a"), pool.submit(reclaim, "reclaimer-b"))]
    assert any(isinstance(result, dict) and result["outcome"] == "blocked" for result in results)
    with factory() as session:
        runs = session.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).all()
        assert [run.attempt_number for run in runs] == [1, 2]
        assert len(session.scalars(select(FailureRecord).where(FailureRecord.run_id == run_id)).all()) == 1
        assert len(session.scalars(select(Verification).where(Verification.run_id == run_id)).all()) == 1


def test_reclaim_resumes_after_close_before_recovery_creation(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="reclaim-worker", repo=str(repo), base_commit=base_commit)
    _expire_run_with_one_pending_call(factory, run_id)
    # Simulate a process stopping after the close transaction commits.
    worker._close_expired_run_once(run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "Review the reclaimed attempt",
             "decision_required": "Decide how to continue"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))
    request = dict(workspaces_root=workspaces, artifacts_root=artifacts,
                   supervisor_gateway=gateway, explorer_gateway=None, planner_gateway=None,
                   recovery_max_read_bytes=1024, stop_event=Event())
    results = []

    def receive(result):
        results.append(result)
        request["stop_event"].set()

    worker.run_recovery_loop(**request, poll_interval_seconds=1, on_result=receive)
    assert len(results) == 1
    result = results[0]
    assert result["run_id"] == run_id and result["outcome"] == "blocked"
    request["stop_event"].clear()
    assert worker.reclaim_next_expired_run(**request)["outcome"] == "idle"
    with factory() as session:
        runs = session.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).all()
        assert [run.attempt_number for run in runs] == [1, 2]
        assert len(session.scalars(select(FailureRecord).where(FailureRecord.run_id == run_id)).all()) == 1


def test_recovery_loop_runs_one_real_idle_scan(recovery_session_factory, tmp_path):
    factory = recovery_session_factory
    worker = Worker(factory, "loop-worker")
    create_running_run(factory, worker_id="live-worker")
    stop = Event()
    results = []

    def receive(result):
        results.append(result)
        stop.set()

    worker.run_recovery_loop(
        workspaces_root=tmp_path, artifacts_root=tmp_path,
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        recovery_max_read_bytes=1024, stop_event=stop, poll_interval_seconds=1,
        on_result=receive)
    assert results == [{"outcome": "idle", "run_id": None}]


def test_reclaim_is_idle_when_nothing_has_expired(recovery_session_factory, tmp_path):
    factory = recovery_session_factory
    worker = Worker(factory, "idle-reclaimer")
    create_running_run(factory, worker_id="live-worker")
    workspaces, artifacts = tmp_path / "w", tmp_path / "a"
    workspaces.mkdir()
    artifacts.mkdir()

    result = worker.reclaim_next_expired_run(
        workspaces_root=workspaces, artifacts_root=artifacts, supervisor_gateway=None,
        explorer_gateway=None, planner_gateway=None, recovery_max_read_bytes=1024,
        stop_event=Event())

    assert result == {"outcome": "idle", "run_id": None}


def test_continue_taken_over_run_waits_for_recovery_dispatch_lock(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    engine = factory.kw["bind"]
    real_lock = workspace_operation_lock

    def short_lock(*args, **kwargs):
        kwargs["timeout_seconds"] = 0.2
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(worker_module, "workspace_operation_lock", short_lock)
    monkeypatch.setattr(Worker, "_ensure_recovery_workspace",
                        lambda *a, **k: pytest.fail("workspace preparation must wait"))
    with real_lock(engine, run_id, namespace=worker_module._RECOVERY_DISPATCH_LOCK_NAMESPACE):
        with pytest.raises(WorkspaceLockUnavailableError):
            worker.continue_taken_over_run(
                run_id, workspaces_root=tmp_path, artifacts_root=tmp_path,
                supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
                max_read_bytes=1024, stop_event=Event())


def test_continue_taken_over_run_checks_owner_before_workspace_preparation(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    _, _, run_id = create_running_run(factory)
    worker = Worker(factory, "another-worker")
    monkeypatch.setattr(Worker, "_ensure_recovery_workspace",
                        lambda *a, **k: pytest.fail("unauthorized workspace preparation"))
    with pytest.raises(RunExecutionDeniedError):
        worker.continue_taken_over_run(
            run_id, workspaces_root=tmp_path, artifacts_root=tmp_path,
            supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
            max_read_bytes=1024, stop_event=Event())


def test_taken_over_run_with_a_running_node_is_not_continued(
    recovery_session_factory, tmp_path,
):
    """One Handoff allows one execution, so a half-run node blocks a takeover."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="lost-worker")
    with factory.begin() as session:
        orphan = AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output=None)
        session.add(orphan)
        session.flush()
        orphan_id = orphan.id
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    workspaces, artifacts = tmp_path / "w", tmp_path / "a"
    workspaces.mkdir()
    artifacts.mkdir()

    result = worker.take_over_expired_run_and_resume(
        run_id, workspaces_root=workspaces, artifacts_root=artifacts,
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        max_read_bytes=1024, stop_event=Event())

    assert result["outcome"] == "blocked_by_orphaned_nodes"
    assert result["continued"] is None
    assert result["orphaned_executions"] == [
        {"agent_execution_id": str(orphan_id),
         "agent_role": AgentRole.EXPLORER.value.lower()}]
    with factory() as session:
        # The half-run node is never rewritten: the schema keeps one execution per
        # Handoff, and a second one would make replays ambiguous.
        assert session.get(AgentExecution, orphan_id).status == AgentExecutionStatus.RUNNING
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == run_id)) == 1
        assert session.get(Run, run_id).worker_id == worker.worker_id


def test_taken_over_run_without_a_running_node_continues_in_place(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="lost-worker", repo=str(repo), base_commit=base_commit)
    # The previous worker committed its first Supervisor route and died mid-read.
    with factory.begin() as session:
        supervisor = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={},
            output={"outcome": "EXPLORE", "reason": "Repository facts are missing"})
        session.add(supervisor)
        session.flush()
        handoff = Handoff(
            from_agent_execution_id=supervisor.id,
            to_agent_role=AgentRole.EXPLORER.value.lower(),
            context={"completed_work": "The issue needs evidence", "blocker": None,
                     "evidence_refs": [], "decision_required": "Read the README",
                     "scope_impact": "Read only"})
        session.add(handoff)
        session.flush()
        supervisor.output = {"outcome": "EXPLORE", "approved_handoff_id": str(handoff.id)}
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    explorer_prompts = []

    def explore(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        if not explorer_prompts:
            response = {"path": "README.md"}
        else:
            response = {"summary": "Read the repository facts", "read_evidence": {
                "path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}}
        explorer_prompts.append(data)
        return ModelReply(response, ModelTokenUsage(input_tokens=40, output_tokens=20))

    explorer_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=explore)
    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "The recovered evidence needs review",
             "decision_required": "Review the recovered evidence"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))

    result = worker.take_over_expired_run_and_resume(
        run_id, workspaces_root=workspaces, artifacts_root=artifacts,
        supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
        planner_gateway=None, max_read_bytes=1024, stop_event=Event())

    assert result["previous_worker_id"] == "lost-worker"
    assert result["orphaned_executions"] == [] and result["continued"] is not None
    assert explorer_prompts, "the undispatched read must run under the new owner"
    with factory() as session:
        explorers = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.EXPLORER.value.lower()).order_by(
                AgentExecution.created_at, AgentExecution.id)).all()
        assert len(explorers) == 1
        assert explorers[0].status == AgentExecutionStatus.SUCCEEDED
        assert session.get(Run, run_id).worker_id == worker.worker_id


def test_takeover_is_refused_while_a_mutation_is_unsettled(recovery_session_factory):
    """An expired lease does not prove the old worker stopped modifying the tree."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="lost-worker")
    with factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.CODER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output=None)
        session.add(execution)
        session.flush()
        session.add(ToolCall(run_id=run_id, agent_execution_id=execution.id,
                             tool_name="apply_patch", args={"artifact_id": str(uuid4())},
                             status=ToolCallStatus.RUNNING))
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    with factory() as session:
        with pytest.raises(WorkspaceQuarantinedError):
            RunCoordinator(session).take_over_expired_run(run_id, "recovery-worker-2")

    with factory() as session:
        run = session.get(Run, run_id)
        assert run.worker_id == "lost-worker" and run.status == RunStatus.RUNNING
        assert session.scalar(select(ToolCall.status).where(
            ToolCall.run_id == run_id)) == ToolCallStatus.RUNNING


def test_takeover_is_refused_while_the_workspace_lock_is_held(recovery_session_factory):
    """A live operation on the workspace blocks ownership transfer."""
    factory = recovery_session_factory
    engine = factory.kw["bind"]
    worker, task_id, run_id = create_running_run(factory, worker_id="lost-worker")
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    with workspace_operation_lock(engine, run_id):
        with factory() as session:
            with pytest.raises(WorkspaceLockUnavailableError):
                RunCoordinator(session).take_over_expired_run(
                    run_id, "recovery-worker-2", lock_timeout_seconds=0.2)

    with factory() as session:
        assert session.get(Run, run_id).worker_id == "lost-worker"


def test_replan_supervisor_phase_rejects_two_routes_at_the_same_time(
    recovery_session_factory,
):
    """Two legacy routes created at one instant cannot be told apart."""
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    seeded = _seed_committed_coder_route(
        factory, run_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    stamp = datetime.now(timezone.utc)
    with factory.begin() as session:
        handoff = Handoff(from_agent_execution_id=seeded.replan_planner_id,
                          to_agent_role=AgentRole.VERIFIER.value.lower(), context={})
        session.add(handoff)
        session.flush()
        session.add_all([
            AgentExecution(run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
                           status=AgentExecutionStatus.SUCCEEDED, handoff_id=handoff.id,
                           created_at=stamp, input={}, output={"outcome": "BASELINE_RECORDED"}),
            # Legacy rows without an observation: only creation order could separate them.
            AgentExecution(run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                           status=AgentExecutionStatus.SUCCEEDED,
                           created_at=stamp - timedelta(seconds=5),
                           input={"planner_execution_id": str(seeded.replan_planner_id)},
                           output={"outcome": "BASELINE"}),
            AgentExecution(run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                           status=AgentExecutionStatus.SUCCEEDED,
                           created_at=stamp - timedelta(seconds=5),
                           input={"planner_execution_id": str(seeded.replan_planner_id)},
                           output={"outcome": "CODE"}),
        ])

    with pytest.raises(PlannerSupervisorContextError, match="share a creation time"):
        worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase="pre_baseline")


def test_inspect_expired_run_reports_takeover_blockers(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="inspect-worker")

    with factory() as session:
        live = RunCoordinator(session).inspect_expired_run(run_id)
    assert live["lease_expired"] is False and live["duration_exhausted"] is False
    assert live["takeover_blockers"] == ["lease_not_expired"]

    with factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output=None)
        session.add(execution)
        session.flush()
        session.add(ToolCall(run_id=run_id, agent_execution_id=execution.id,
                             tool_name="apply_patch", args={}, status=ToolCallStatus.RUNNING))
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    with factory() as session:
        blocked = RunCoordinator(session).inspect_expired_run(run_id)
    assert blocked["lease_expired"] is True and blocked["duration_exhausted"] is False
    assert blocked["takeover_blockers"] == ["unsettled_mutation", "orphaned_executions"]
    assert blocked["unsettled_mutation_tool_calls"][0]["tool_name"] == "apply_patch"
    assert blocked["orphaned_executions"][0]["agent_role"] == AgentRole.EXPLORER.value.lower()


def test_inspect_expired_run_marks_a_run_past_its_deadline(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="inspect-deadline")
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.started_at = datetime.now(timezone.utc) - timedelta(days=30)
        run.lease_expires_at = func.clock_timestamp()

    with factory() as session:
        report = RunCoordinator(session).inspect_expired_run(run_id)

    assert report["lease_expired"] is True and report["duration_exhausted"] is True
    assert report["takeover_blockers"] == ["duration_exhausted"]


def test_control_cli_reports_a_run_and_rejects_a_bad_id(recovery_session_factory, capsys):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="inspect-cli")
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    exit_code = control_main(["--inspect-expired-run", str(run_id)], session_factory=factory)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "control_run_inspected"
    assert payload["run_id"] == str(run_id)
    assert payload["takeover_blockers"] == []
    assert control_main(["--inspect-expired-run", "not-a-uuid"], session_factory=factory) == 2
    assert "control_configuration_error" in capsys.readouterr().err


def test_control_cli_closes_expired_runs_without_starting_recovery(
    recovery_session_factory, capsys,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="sweep-closed")
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    exit_code = control_main(
        ["--close-expired-runs", "--max-runs", "2", "--worker-id", "control-sweeper"],
        session_factory=factory)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "control_expired_runs_closed"
    assert payload["skipped"] == []
    assert [item["run_id"] for item in payload["closed"]] == [str(run_id)]
    assert payload["closed"][0]["recovery_action"] == "REDIAGNOSE"
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.FAILED
        assert session.get(Task, task_id).status == TaskStatus.RETRYING
        # The control plane closes only; the recovery Run needs a model gateway.
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 1

    # Nothing left to close, and a non-positive limit is a configuration error.
    assert control_main(["--close-expired-runs"], session_factory=factory) == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["closed"] == [] and idle["skipped"] == []
    assert control_main(
        ["--close-expired-runs", "--max-runs", "0"], session_factory=factory) == 2


def test_control_reclaims_expired_runs_with_gateways(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="lost-worker", repo=str(repo), base_commit=base_commit)
    _expire_run_with_one_pending_call(factory, run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id="control-sweeper",
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "Review the reclaimed attempt",
             "decision_required": "Decide how to continue"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))
    gateways = SimpleNamespace(supervisor=supervisor_gateway, explorer=None, planner=None)

    report = control_reclaim_expired_runs(
        factory, gateways, worker_id="control-sweeper", workspaces_root=workspaces,
        artifacts_root=artifacts, max_scans=2)

    assert report["scans"] == 2
    assert report["results"][0]["run_id"] == str(run_id)
    assert report["results"][0]["outcome"] == "blocked"
    assert report["results"][0]["recovery_action"] == "REDIAGNOSE"
    assert report["results"][1]["outcome"] == "idle"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 2


def test_control_cli_needs_gateway_configuration_and_roots(
    recovery_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = recovery_session_factory
    workspaces, artifacts = tmp_path / "w", tmp_path / "a"
    workspaces.mkdir()
    artifacts.mkdir()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEVFLOW_DEEPSEEK_MODEL", raising=False)

    assert control_main(["--reclaim-expired-runs"], session_factory=factory) == 2
    assert "control_configuration_error" in capsys.readouterr().err

    missing_credentials = control_main(
        ["--reclaim-expired-runs", "--workspaces-root", str(workspaces),
         "--artifacts-root", str(artifacts)], session_factory=factory)
    assert missing_credentials == 2
    assert "Model gateways are not configured" in capsys.readouterr().err


@contextmanager
def _stub_gateways(gateways):
    """Stand in for the environment-built gateways inside control.main."""
    yield gateways


def test_control_cli_refuses_a_takeover_for_a_live_run(
    recovery_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = recovery_session_factory
    workspaces, artifacts = tmp_path / "w", tmp_path / "a"
    workspaces.mkdir()
    artifacts.mkdir()
    worker, task_id, run_id = create_running_run(factory, worker_id="live-worker")
    stub = SimpleNamespace(supervisor=None, explorer=None, planner=None)
    monkeypatch.setattr(control_module, "model_gateways_from_env",
                        lambda *args, **kwargs: _stub_gateways(stub))

    exit_code = control_main(
        ["--take-over-expired-run", str(run_id), "--workspaces-root", str(workspaces),
         "--artifacts-root", str(artifacts), "--worker-id", "control-actor"],
        session_factory=factory)

    assert exit_code == 1
    assert "control_run_not_expired" in capsys.readouterr().err
    with factory() as session:
        assert session.get(Run, run_id).worker_id == "live-worker"


def test_control_cli_takes_over_and_continues_an_expired_run(
    recovery_session_factory, tmp_path, monkeypatch, capsys,
):
    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="lost-worker", repo=str(repo), base_commit=base_commit)
    with factory.begin() as session:
        supervisor = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={},
            output={"outcome": "EXPLORE", "reason": "Repository facts are missing"})
        session.add(supervisor)
        session.flush()
        handoff = Handoff(
            from_agent_execution_id=supervisor.id,
            to_agent_role=AgentRole.EXPLORER.value.lower(),
            context={"completed_work": "The issue needs evidence", "blocker": None,
                     "evidence_refs": [], "decision_required": "Read the README",
                     "scope_impact": "Read only"})
        session.add(handoff)
        session.flush()
        supervisor.output = {"outcome": "EXPLORE", "approved_handoff_id": str(handoff.id)}
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    explorer_prompts = []

    def explore(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        if not explorer_prompts:
            response = {"path": "README.md"}
        else:
            response = {"summary": "Read the repository facts", "read_evidence": {
                "path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}}
        explorer_prompts.append(data)
        return ModelReply(response, ModelTokenUsage(input_tokens=40, output_tokens=20))

    explorer_gateway = ModelGateway(
        session_factory=factory, worker_id="control-actor",
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=explore)
    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id="control-actor",
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "The recovered evidence needs review",
             "decision_required": "Review the recovered evidence"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))
    stub = SimpleNamespace(supervisor=supervisor_gateway, explorer=explorer_gateway,
                           planner=None)
    monkeypatch.setattr(control_module, "model_gateways_from_env",
                        lambda *args, **kwargs: _stub_gateways(stub))

    exit_code = control_main(
        ["--take-over-expired-run", str(run_id), "--workspaces-root", str(workspaces),
         "--artifacts-root", str(artifacts), "--worker-id", "control-actor"],
        session_factory=factory)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "control_expired_run_taken_over"
    assert payload["run_id"] == str(run_id)
    assert payload["previous_worker_id"] == "lost-worker"
    assert payload["worker_id"] == "control-actor"
    assert payload["orphaned_executions"] == []
    assert payload["continued_outcome"] is not None
    assert explorer_prompts, "the undispatched read must run under the new owner"
    with factory() as session:
        explorers = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.EXPLORER.value.lower())).all()
        assert len(explorers) == 1
        assert explorers[0].status == AgentExecutionStatus.SUCCEEDED
        assert session.get(Run, run_id).worker_id == "control-actor"


def test_expired_run_close_refuses_a_stale_run_whose_task_moved_on(
    recovery_session_factory,
):
    """A stale Run may not overwrite state a newer transition already wrote."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stale-close")
    with factory.begin() as session:
        session.get(Task, task_id).status = TaskStatus.AWAITING_REVIEW
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()

    with factory() as session:
        report = RunCoordinator(session).inspect_expired_run(run_id)
    assert report["takeover_blockers"] == ["task_not_running"]

    with factory() as session:
        with pytest.raises(TaskStateConflictError):
            RunCoordinator(session).close_expired_run_with_derived_decision(run_id)
    with factory() as session:
        with pytest.raises(TaskStateConflictError):
            RunCoordinator(session).take_over_expired_run(run_id, "recovery-worker-2")

    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING
        assert session.get(Task, task_id).status == TaskStatus.AWAITING_REVIEW
        assert not session.scalars(select(FailureRecord)).all()


def _benchmark_pack(tmp_path, *, base_commit):
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    manifest = {
        "pack_id": "demo-pack", "schema_version": 1, "title": "Demo pack",
        "source": {"issue_url": "https://example.invalid/issues/1",
                   "repository_url": "https://example.invalid/repo",
                   "license": "MIT", "issue_reference": "demo#1"},
        "base_commit": base_commit,
        "environment": {"python_requires": ">=3.11", "dependencies": ["pytest==9.1.1"],
                        "platform_notes": "Linux"},
        "issue": {"sanitized_problem": "A sanitized problem statement.",
                  "observable_error": "An observable wrong output.",
                  "expected_behavior": "The expected behaviour."},
        "scope": {"allowed_paths": ["src/"]},
        "public_checks": {"command": ["python", "-m", "pytest"], "command_name": "pytest",
                          "target_tests": ["tests/test_demo.py::test_target"]},
        "baseline": {"status": "verified_here"},
        "external_connections": "none",
        "policy": {"policy_version": "demo-v1", "model_id": "demo-model"},
        "independent_suite": {"suite_version": "suite-1", "materials_sha256": "b" * 64,
                              "digest_scope": "bytes of the suite archive as delivered",
                              "digest_method": "sha256 over those bytes",
                              "provided_by": "trusted_control_plane",
                              "declaration": "Provided by the trusted control plane."},
    }
    (pack_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


def _eligible_benchmark_task(factory, *, repo="example-repo", base_commit="a" * 40, policy_factory=None):
    """Create an ELIGIBLE Task with no Run, so the runner itself claims one."""
    with factory() as session:
        task, _ = TaskService(session, policy_factory=policy_factory).create_task(
            title="Benchmark task", description="Run one benchmark attempt",
            repo=repo, idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="benchmark fixture", evidence=["fixture"]))
    return task_id


def _benchmark_request(tmp_path, task_id, *, base_commit="a" * 40, limits=None):
    from devflow_ai.benchmark_run import BenchmarkRunRequest

    return BenchmarkRunRequest(
        pack_dir=_benchmark_pack(tmp_path, base_commit=base_commit), task_id=task_id,
        operator="operator-a", python_executable=sys.executable,
        workspaces_root=tmp_path / "w", artifacts_root=tmp_path / "a",
        record_path=tmp_path / "record.json", **({} if limits is None else {"limits": limits}))


def test_benchmark_run_refuses_a_policy_above_its_token_limit(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.benchmark_run import BenchmarkLimits, BenchmarkRunError, run_one_task

    factory = recovery_session_factory
    task_id = _eligible_benchmark_task(factory)

    with pytest.raises(BenchmarkRunError, match="more tokens"):
        run_one_task(
            _benchmark_request(tmp_path, task_id, limits=BenchmarkLimits(max_model_tokens=1)),
            session_factory=factory, worker=Worker(factory, "bench-worker"),
            gateways=None, heartbeat_config=None)
    assert not (tmp_path / "record.json").exists()


def test_benchmark_run_refuses_a_mismatched_base_commit(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_run import BenchmarkRunError, run_one_task

    factory = recovery_session_factory
    task_id = _eligible_benchmark_task(factory, base_commit="a" * 40)

    with pytest.raises(BenchmarkRunError, match="base commit"):
        run_one_task(
            _benchmark_request(tmp_path, task_id, base_commit="b" * 40),
            session_factory=factory, worker=Worker(factory, "bench-worker"),
            gateways=None, heartbeat_config=None)


def test_benchmark_run_records_one_attempt(recovery_session_factory, tmp_path, monkeypatch):
    from devflow_ai.benchmark_run import run_one_task
    from devflow_ai.benchmark_record import load_run_record

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-chat")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()

    result = run_one_task(
        _benchmark_request(tmp_path, task_id, base_commit=base_commit),
        session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5),
        drive=lambda directories, stop: None)

    assert result["outcome"] == "incomplete" and result["deadline_fired"] is False
    assert result["reason"] == "run_still_running"
    record = load_run_record(tmp_path / "record.json")
    assert record.pack_id == "demo-pack" and record.attempt_index == 1
    assert record.model_id == "deepseek-chat"
    assert "attempts=1" in record.notes and "auto_retries=0" in record.notes
    assert record.counts_as_solved is False
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 1


def test_benchmark_run_claims_only_the_requested_task(
    recovery_session_factory, tmp_path, monkeypatch,
):
    from devflow_ai.benchmark_run import run_one_task

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    other_repo = tmp_path / "other"
    other_commit = _create_git_fixture(other_repo)
    _eligible_benchmark_task(factory, repo=str(other_repo), base_commit=other_commit)
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()

    run_one_task(
        _benchmark_request(tmp_path, task_id, base_commit=base_commit),
        session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5),
        drive=lambda directories, stop: None)

    with factory() as session:
        runs = session.scalars(select(Run)).all()
        assert [run.task_id for run in runs] == [task_id]


def test_benchmark_run_deadline_stops_the_execution(
    recovery_session_factory, tmp_path, monkeypatch,
):
    from devflow_ai.benchmark_run import BenchmarkLimits, run_one_task
    from devflow_ai.benchmark_record import load_run_record

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()

    def slow_drive(directories, stop):
        # Stands in for a long external call: it must end because the deadline set
        # the execution's own stop event, not a private one.
        started = monotonic()
        while not stop.is_set() and monotonic() - started < 30:
            sleep(0.05)

    result = run_one_task(
        _benchmark_request(tmp_path, task_id, base_commit=base_commit,
                           limits=BenchmarkLimits(max_duration_seconds=1)),
        session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5),
        drive=slow_drive)

    assert result["deadline_fired"] is True
    assert result["outcome"] == "timeout"
    assert load_run_record(tmp_path / "record.json").outcome.value == "timeout"


def test_benchmark_run_times_out_during_workspace_initialization(
    recovery_session_factory, tmp_path, monkeypatch,
):
    from devflow_ai.benchmark_run import BenchmarkLimits, run_one_task

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()
    observed = {"stopped_in_init": False}

    def slow_initialize(self, run_id, *, workspaces_root, artifacts_root, stop_event):
        # The prepare step must see the caller's deadline through the stop event it is
        # given, not only after it finishes.
        observed["stopped_in_init"] = stop_event.wait(5)
        if not observed["stopped_in_init"]:
            pytest.fail("workspace initialization never observed the deadline")
        return (RunDirectories(workspaces_root / str(run_id), artifacts_root / str(run_id)),
                "a" * 40)

    monkeypatch.setattr(Worker, "initialize_run_workspace", slow_initialize)

    result = run_one_task(
        _benchmark_request(tmp_path, task_id, base_commit=base_commit,
                           limits=BenchmarkLimits(max_duration_seconds=1)),
        session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5),
        drive=lambda directories, stop: None)

    assert observed["stopped_in_init"] is True
    assert result["outcome"] == "timeout"
    assert result["reason"] == "deadline_during_initialization"
    assert result["deadline_fired"] is True


def test_benchmark_run_does_not_repeat_an_attempt(
    recovery_session_factory, tmp_path, monkeypatch,
):
    from devflow_ai.benchmark_run import run_one_task

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()
    request = _benchmark_request(tmp_path, task_id, base_commit=base_commit)
    worker = Worker(factory, "bench-worker")
    heartbeat = HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5)

    first = run_one_task(request, session_factory=factory, worker=worker, gateways=None,
                         heartbeat_config=heartbeat, drive=lambda directories, stop: None)
    second = run_one_task(request, session_factory=factory, worker=worker, gateways=None,
                          heartbeat_config=heartbeat, drive=lambda directories, stop: None)

    assert first["outcome"] == "incomplete" and second["outcome"] == "existing_run"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == task_id)) == 1


def test_a_passed_row_alone_is_not_a_formal_pass(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="evidence-chain")
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.PASSED
        verification = Verification(
            run_id=run_id, result=VerificationResult.PASSED,
            evidence={"scope": "trusted_v1", "draft_artifact_id": str(uuid4()),
                      "independent_artifact_id": str(uuid4()),
                      "suite_version": "suite-1"})
        session.add(verification)
        session.flush()
        session.add(AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={},
            output={"outcome": "VERIFICATION_PASSED",
                    "verification_id": str(verification.id)}))

    # Without the artifact root the references cannot be checked at all.
    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1")
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason
    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "artifacts_unverifiable"

    # Pointing at an artifact root does not help when the artifacts are not there.
    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=tmp_path)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason
    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "artifacts_unverifiable"

    # A suite that does not match the pack is refused before any artifact is read.
    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-2",
            artifact_root=tmp_path)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason
    assert outcome is RunOutcome.INCOMPLETE and reason == "suite_version_mismatch"


def test_a_passed_row_without_trusted_scope_is_not_a_formal_pass(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="evidence-scope")
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.PASSED
        session.add(Verification(run_id=run_id, result=VerificationResult.PASSED,
                                 evidence={"scope": "no_verification"}))

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version=None,
            artifact_root=tmp_path)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason

    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "evidence_scope_untrusted"


def test_an_interrupted_attempt_is_recorded_then_reraised(
    recovery_session_factory, tmp_path, monkeypatch,
):
    from devflow_ai.benchmark_record import RunOutcome, load_run_record
    from devflow_ai.benchmark_run import run_one_task

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()

    def explode(directories, stop):
        raise RuntimeError("gateway exploded")

    with pytest.raises(RuntimeError, match="gateway exploded"):
        run_one_task(
            _benchmark_request(tmp_path, task_id, base_commit=base_commit),
            session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5,
                                             shutdown_timeout_seconds=5),
            drive=explode)

    record = load_run_record(tmp_path / "record.json")
    assert record.outcome is RunOutcome.INCOMPLETE
    assert "execution_error:RuntimeError" in record.notes


def _stage_driver_args(worker, run_id, *, supervisor_gateway=None, max_stages=8):
    from devflow_ai.orchestration import run_task_stages

    directories = RunDirectories(Path("/tmp/none"), Path("/tmp/none"))
    stop = Event()
    gateways = SimpleNamespace(supervisor=supervisor_gateway, explorer=None, planner=None)
    builders = {}
    if supervisor_gateway is not None:
        builders = {
            "post_explorer_supervise": lambda execution_id: worker.build_post_explorer_supervise(
                run_id, explorer_execution_id=execution_id,
                model_gateway=supervisor_gateway, stop_event=stop),
            "post_planner_supervise": lambda execution_id: worker.build_post_planner_supervise(
                run_id, planner_execution_id=execution_id,
                model_gateway=supervisor_gateway, stop_event=stop),
            "post_coder_supervise": lambda execution_id: worker.build_post_coder_supervise(
                run_id, coder_execution_id=execution_id, directories=directories,
                model_gateway=supervisor_gateway, max_patch_bytes=4096, stop_event=stop),
        }
    return dict(
        session_factory=worker.session_factory, run_id=run_id, worker_id=worker.worker_id,
        directories=directories, gateways=gateways,
        receivers_factory=lambda: worker.build_handoff_receivers(
            run_id, directories=directories, max_bytes=1024, stop_event=stop,
            explorer_gateway=None, planner_gateway=None),
        initial_supervise=worker.build_initial_supervise(
            run_id, model_gateway=supervisor_gateway, stop_event=stop),
        stop_event=stop, max_stages=max_stages, **builders,
    ), run_task_stages


def test_stage_driver_stops_when_the_supervisor_does_not_route(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-blocked")
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "not enough evidence",
             "decision_required": "Collect evidence"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=gateway)

    result = driver(**args)

    assert result == {"outcome": "stopped", "reason": "supervisor_did_not_route", "stages": 1}


def test_stage_driver_stops_when_the_run_is_not_running(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-finished")
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.FAILED
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)

    result = driver(**args)

    assert result["outcome"] == "stopped" and result["reason"] == "run_not_running"
    assert result["stages"] == 0


def _seed_stage_execution(factory, run_id, role, *, status=None, output=None, source=None):
    """Seed one execution, optionally behind a Handoff sourced from another execution."""
    from devflow_ai.enums import AgentExecutionStatus

    status = status or AgentExecutionStatus.SUCCEEDED
    with factory.begin() as session:
        handoff_id = None
        if source is not None:
            handoff = Handoff(from_agent_execution_id=source,
                              to_agent_role=role.value.lower(), context={})
            session.add(handoff)
            session.flush()
            handoff_id = handoff.id
        execution = AgentExecution(
            run_id=run_id, agent_role=role.value.lower(), status=status,
            handoff_id=handoff_id, input={}, output=output or {})
        session.add(execution)
        session.flush()
        execution_id = execution.id
    return execution_id


def test_stage_driver_reads_the_latest_stage_not_an_older_success(recovery_session_factory):
    """A successful Explorer must not hide a later failed Planner."""
    from devflow_ai.enums import AgentExecutionStatus

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-stale")
    _seed_stage_execution(factory, run_id, AgentRole.EXPLORER, output={"outcome": "EXPLORED"})
    _seed_stage_execution(factory, run_id, AgentRole.PLANNER,
                          status=AgentExecutionStatus.FAILED, output={"outcome": "ERROR"})
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)

    result = driver(**args)

    assert result == {"outcome": "stopped", "reason": "last_stage_not_succeeded", "stages": 0}


def test_stage_driver_continues_after_a_recorded_baseline(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-baseline")
    planner_id = _seed_stage_execution(factory, run_id, AgentRole.PLANNER,
                                       output={"outcome": "PLAN_READY"})
    _seed_stage_execution(factory, run_id, AgentRole.VERIFIER,
                          output={"outcome": "BASELINE_RECORDED"}, source=planner_id)
    routed = []
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)
    args["post_planner_supervise"] = lambda execution_id: (
        routed.append(execution_id) or (lambda run_id: None))

    result = driver(**args)

    # The baseline authorises one further route for the Planner that asked for it.
    assert routed == [planner_id]
    assert result["reason"] == "supervisor_did_not_route" and result["stages"] == 1


def test_stage_driver_continues_after_a_replan_baseline(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-replan-baseline")
    coder_id = _seed_stage_execution(factory, run_id, AgentRole.CODER,
                                     output={"outcome": "BLOCKED"})
    replan_planner_id = _seed_stage_execution(factory, run_id, AgentRole.PLANNER,
                                              output={"outcome": "PLAN_READY"},
                                              source=coder_id)
    _seed_stage_execution(factory, run_id, AgentRole.VERIFIER,
                          output={"outcome": "BASELINE_RECORDED"}, source=replan_planner_id)
    routed = []
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)
    args["post_planner_supervise"] = lambda execution_id: (
        routed.append(execution_id) or (lambda run_id: None))

    result = driver(**args)

    assert routed == [replan_planner_id]
    assert result["reason"] == "supervisor_did_not_route" and result["stages"] == 1


def test_stage_driver_sends_a_blocked_coder_to_its_supervisor(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-coder-blocked")
    coder_id = _seed_stage_execution(factory, run_id, AgentRole.CODER,
                                     output={"outcome": "BLOCKED"})
    routed = []
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)
    args["post_coder_supervise"] = lambda execution_id: (
        routed.append(execution_id) or (lambda run_id: None))

    result = driver(**args)

    # BLOCKED is not a verdict this driver may read off a role name; the Supervisor decides.
    assert routed == [coder_id]
    assert result["reason"] == "supervisor_did_not_route" and result["stages"] == 1


def test_stage_driver_keeps_a_blocked_verifier_reason(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-verifier-blocked")
    _seed_stage_execution(
        factory, run_id, AgentRole.VERIFIER,
        output={"outcome": "VERIFICATION_BLOCKED", "missing_evidence": ["public_tests"]})
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)

    result = driver(**args)

    assert result["reason"] == "verification_blocked" and result["stages"] == 0
    assert result["missing_evidence"] == ["public_tests"]


def test_stage_driver_stops_on_an_unreadable_verifier_outcome(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-verifier-odd")
    _seed_stage_execution(factory, run_id, AgentRole.VERIFIER, output={})
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)

    result = driver(**args)

    assert result["reason"] == "verifier_outcome_unsupported" and result["stages"] == 0


def test_stage_driver_requires_execution_right(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-lease")
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)

    with pytest.raises(RunExecutionDeniedError):
        driver(**args)


def test_stage_driver_is_bounded_by_its_stage_limit(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-limit")
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "BLOCKED", "reason": "bounded", "decision_required": "review"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=gateway, max_stages=1)

    result = driver(**args)

    # The graph-step budget or the single stage limit stops it; either way it ends.
    assert result["outcome"] == "stopped" and result["stages"] <= 1


def test_stage_driver_stops_when_the_last_stage_did_not_succeed(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="stage-failed-role")
    with factory.begin() as session:
        session.add(AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.FAILED, input={}, output={"outcome": "ERROR"}))
    args, driver = _stage_driver_args(worker, run_id, supervisor_gateway=None)

    result = driver(**args)

    # Restarting the initial route here would be an unstated retry.
    assert result == {"outcome": "stopped", "reason": "last_stage_not_succeeded", "stages": 0}


def _write_benchmark_evidence(
    factory, artifact_root, *, run_id, verifier_execution_id, suite_version,
    receipt_suite_version=None, receipt_outcome="passed", owner_execution_id=None,
    receipt_calibrated=True,
):
    """Write a real-format draft and independent receipt artifact for one Verifier.

    These artifacts are constructed fixtures, not evidence from a real run: they exist to
    exercise the reader's checks (ownership, hash, cross-references), not to claim a task
    passed.
    """
    import io

    from devflow_ai.artifacts import write_artifact_file
    from devflow_ai.verifier import (
        IndependentEvaluationRecord, SemanticReviewDraftRecord,
    )

    owner = owner_execution_id or verifier_execution_id
    artifact_root = Path(artifact_root).resolve()
    draft_id, patch_id, diff_id = uuid4(), uuid4(), uuid4()
    diff_path, diff_hash = write_artifact_file(
        artifact_root, diff_id, io.BytesIO(b"diff --git a/x b/x\n"), chunk_size=65536)
    draft = SemanticReviewDraftRecord.model_validate({
        "record_type": "semantic_review_draft", "schema_version": 1,
        "run_id": str(run_id), "agent_execution_id": str(verifier_execution_id),
        "selection": {
            "baseline_verifier_id": str(uuid4()), "baseline_call_id": str(uuid4()),
            "baseline_artifact_id": str(uuid4()), "patch_call_id": str(uuid4()),
            "current_call_id": str(uuid4()), "current_artifact_id": str(uuid4()),
            "diff_call_id": str(uuid4()), "command_name": "pytest",
            "target_tests": ["test_baseline.py::test_baseline"]},
        # A passed review with a passed comparison is what the public outcome is recomputed
        # from; the checkpoint's own claim is only checked against this.
        "review": {"outcome": "REVIEW", "result": "passed",
                   "reason": "Constructed fixture for a reader test.",
                   "citations": [{"source": "workspace_diff", "tool_call_id": str(uuid4()),
                                  "path": "x.py", "snippet": "return 1",
                                  "reason": "Constructed fixture citation.",
                                  "diff_side": "added"}]},
        "test_comparison": {"target_result": "passed", "regression_result": "passed"},
        "read_call_ids": [], "patch_artifact_id": str(patch_id),
        "diff_artifact_id": str(diff_id),
    })
    draft_bytes = draft.model_dump_json().encode("utf-8")
    draft_path, draft_hash = write_artifact_file(
        artifact_root, draft_id, io.BytesIO(draft_bytes), chunk_size=65536)
    receipt = IndependentEvaluationRecord.model_validate({
        "record_type": "independent_evaluation", "schema_version": 1,
        "run_id": str(run_id), "agent_execution_id": str(verifier_execution_id),
        "draft_artifact_id": str(draft_id), "patch_artifact_id": str(patch_id),
        "diff_artifact_id": str(diff_id), "diff_sha256": diff_hash,
        "suite_version": receipt_suite_version or suite_version, "outcome": receipt_outcome,
        "planned_case_count": 1, "image_id": None, "calibrated": receipt_calibrated,
        "adapter_sha256": "d" * 64,
    })
    receipt_id = uuid4()
    receipt_bytes = receipt.model_dump_json().encode("utf-8")
    receipt_path, receipt_hash = write_artifact_file(
        artifact_root, receipt_id, io.BytesIO(receipt_bytes), chunk_size=65536)
    with factory.begin() as session:
        session.add(Artifact(
            id=draft_id, agent_execution_id=owner,
            kind="semantic_review_draft", content_ref=str(draft_path),
            content_hash=draft_hash))
        session.add(Artifact(
            id=receipt_id, agent_execution_id=owner,
            kind="independent_evaluation", content_ref=str(receipt_path),
            content_hash=receipt_hash))
        session.add(Artifact(
            id=diff_id, agent_execution_id=owner, kind="workspace_diff",
            content_ref=str(diff_path), content_hash=diff_hash))
    return SimpleNamespace(draft_id=draft_id, receipt_id=receipt_id,
                           receipt_path=receipt_path, receipt_hash=receipt_hash)


def _seed_formal_candidate(factory, *, suite_version="suite-1", worker_id="formal-candidate"):
    worker, task_id, run_id = create_running_run(factory, worker_id=worker_id)
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.PASSED
        verifier = AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={})
        session.add(verifier)
        session.flush()
        verifier_id = verifier.id
    return worker, run_id, verifier_id


def _attach_formal_verification(factory, run_id, verifier_id, *, draft_id, receipt_id, suite_version):
    with factory.begin() as session:
        verification = Verification(
            run_id=run_id, result=VerificationResult.PASSED,
            evidence={"scope": "trusted_v1", "draft_artifact_id": str(draft_id),
                      "independent_artifact_id": str(receipt_id),
                      "suite_version": suite_version})
        session.add(verification)
        session.flush()
        session.get(AgentExecution, verifier_id).output = {
            "outcome": "VERIFICATION_PASSED", "verification_id": str(verification.id)}
        return verification.id


def test_a_receipt_with_its_draft_is_a_formal_pass(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1")
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve())
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason

    assert reason == ""
    assert outcome is RunOutcome.FORMAL_PASS
    assert formal.independent_suite_version == "suite-1"
    assert formal.independent_receipt_sha256 == written.receipt_hash


def test_a_formal_pass_rebuilds_its_summary_from_the_verification_it_cites(
    recovery_session_factory, tmp_path,
):
    """After the formal transaction the Verifier output is a verdict, not a checkpoint.

    The summary therefore has to come from the Verification's own draft and receipt; a
    formal_pass label alone is never used to fill the fields.
    """
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory, worker_id="formal-summary")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1")
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")
    with factory.begin() as session:
        # Exactly what the formal transaction leaves behind: the checkpoint is gone.
        session.get(AgentExecution, verifier_id).output = {
            "outcome": "VERIFICATION_PASSED",
            "verification_id": str(session.scalar(select(Verification.id)))}

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve())

    assert decision.outcome is RunOutcome.FORMAL_PASS
    assert decision.public_checks.verified_outcome == "passed"
    assert decision.public_checks.draft_artifact_id == str(written.draft_id)
    assert decision.public_checks.verifier_execution_id == str(verifier_id)
    assert decision.independent.outcome == "passed"
    assert decision.independent.verified is True and decision.independent.calibrated is True
    assert decision.independent.receipt_artifact_id == str(written.receipt_id)
    assert decision.independent.suite_version == "suite-1"


def test_a_formal_pass_whose_draft_no_longer_agrees_does_not_claim_a_public_result(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome
    from devflow_ai.verifier import SemanticReviewDraftRecord

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory, worker_id="formal-draft-drift")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1")
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")
    with factory.begin() as session:
        artifact = session.get(Artifact, written.draft_id)
        original = json.loads(Path(artifact.content_ref).read_text(encoding="utf-8"))
        drifted = SemanticReviewDraftRecord.model_validate(
            {**original, "review": {**original["review"], "result": "failed"}})
        path, digest = _rewrite_artifact(
            artifacts.resolve(), written.draft_id, drifted.model_dump_json().encode("utf-8"))
        artifact.content_ref, artifact.content_hash = str(path), digest

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve())

    # The formal verdict stands (it was committed from the then-verified bytes), but the
    # record does not pretend the public half still recomputes to passed.
    assert decision.outcome is RunOutcome.FORMAL_PASS
    assert decision.public_checks.verified_outcome is None
    assert "no longer recomputes" in decision.public_checks.notes


def test_an_unpinned_suite_version_blocks_a_formal_pass(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory, worker_id="formal-unpinned")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1")
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version=None,
            artifact_root=artifacts)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason

    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "suite_version_unpinned"


def test_a_receipt_for_another_suite_is_refused(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory, worker_id="formal-suite")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1", receipt_suite_version="suite-9")
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason

    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "receipt_mismatch"


def test_a_tampered_receipt_is_refused(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory, worker_id="formal-tamper")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1")
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")
    written.receipt_path.write_bytes(b"{}")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason

    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "artifacts_unverifiable"


def test_artifacts_owned_by_another_execution_are_refused(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, run_id, verifier_id = _seed_formal_candidate(factory, worker_id="formal-owner")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    other_verifier_id = _seed_stage_execution(
        factory, run_id, AgentRole.VERIFIER, output={"outcome": "VERIFICATION_BLOCKED"})
    written = _write_benchmark_evidence(
        factory, artifacts, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1", owner_execution_id=other_verifier_id)
    _attach_formal_verification(factory, run_id, verifier_id,
                                draft_id=written.draft_id, receipt_id=written.receipt_id,
                                suite_version="suite-1")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts)
        outcome, formal, reason = decision.outcome, decision.formal, decision.reason

    assert outcome is RunOutcome.INCOMPLETE and formal is None
    assert reason == "artifacts_unverifiable"


def test_finalize_entry_reports_why_it_cannot_proceed(recovery_session_factory, tmp_path):
    from devflow_ai.orchestration import finalize_benchmark_run

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="finalize-gates")
    fake = SimpleNamespace(
        session_factory=factory,
        finalize_with_independent_evaluation=lambda *a, **k: pytest.fail(
            "Finalize must not run without every gate"))
    directories = RunDirectories(tmp_path / "w", tmp_path / "a")
    material = SimpleNamespace(image="img", command=("python",), cases=(),
                               calibration_workspaces=(), timeout_seconds=1.0)
    config = SimpleNamespace(independent_suite_version="suite-1")

    def call(**overrides):
        arguments = dict(directories=directories, verification_config=config,
                         material=material, max_diff_bytes=1024, max_patch_bytes=1024)
        arguments.update(overrides)
        return finalize_benchmark_run(fake, run_id, **arguments)

    assert call(material=None)["reason"] == "independent_evaluation_material_missing"
    assert call(verification_config=None)["reason"] == "independent_suite_version_missing"
    assert call(max_diff_bytes=0)["reason"] == "evidence_limits_missing"
    assert call()["reason"] == "no_verifier_execution"

    _seed_stage_execution(factory, run_id, AgentRole.VERIFIER,
                          output={"outcome": "VERIFICATION_BLOCKED",
                                  "preliminary_public_outcome": "failed"})
    assert call()["reason"] == "public_checks_not_passed"


def test_finalize_entry_hands_a_passing_draft_to_the_existing_path(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.orchestration import finalize_benchmark_run

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="finalize-handoff")
    draft_id = uuid4()
    verifier_id = _seed_stage_execution(
        factory, run_id, AgentRole.VERIFIER,
        output={"outcome": "VERIFICATION_BLOCKED", "preliminary_public_outcome": "passed",
                "draft_artifact_id": str(draft_id)})
    recorded = {}

    def fake_finalize(run_id_argument, **kwargs):
        recorded.update(kwargs, run_id=run_id_argument)
        return {"outcome": "passed", "independent_artifact_id": uuid4(),
                "verification_id": uuid4()}

    fake = SimpleNamespace(session_factory=factory,
                           finalize_with_independent_evaluation=fake_finalize)
    material = SimpleNamespace(image="image-1", command=("python", "-V"), cases=("case",),
                               calibration_workspaces=(tmp_path, tmp_path, "abc"),
                               timeout_seconds=5.0)

    result = finalize_benchmark_run(
        fake, run_id, directories=RunDirectories(tmp_path / "w", tmp_path / "a"),
        verification_config=SimpleNamespace(independent_suite_version="suite-1"),
        material=material, max_diff_bytes=2048, max_patch_bytes=4096, stop_event=None)

    assert result["outcome"] == "finalized" and result["independent_outcome"] == "passed"
    assert recorded["run_id"] == run_id
    assert recorded["agent_execution_id"] == verifier_id
    assert recorded["draft_artifact_id"] == draft_id
    assert recorded["image"] == "image-1" and recorded["command"] == ("python", "-V")
    assert recorded["max_diff_bytes"] == 2048 and recorded["max_patch_bytes"] == 4096


def test_a_benchmark_run_hands_its_draft_to_the_finalize_entry(
    recovery_session_factory, tmp_path, monkeypatch,
):
    import devflow_ai.benchmark_run as benchmark_run_module
    from devflow_ai.benchmark_record import load_run_record

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()
    calls = []

    def stub_finalize(worker_argument, run_id_argument, **kwargs):
        # The formal path must run while the worker still holds the Run: same heartbeat,
        # same lease. If this raises, the wiring is wrong.
        with factory() as session, session.begin():
            RunCoordinator(session).assert_execution_right(
                run_id_argument, worker_argument.worker_id)
            run_status = session.get(Run, run_id_argument).status
        calls.append({"run_id": run_id_argument, "run_status": run_status, **kwargs})
        return {"outcome": "not_finalized", "reason": "no_verifier_execution"}

    monkeypatch.setattr(benchmark_run_module, "finalize_benchmark_run", stub_finalize)

    from devflow_ai.benchmark_run import run_one_task

    result = run_one_task(
        _benchmark_request(tmp_path, task_id, base_commit=base_commit),
        session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
        heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5, shutdown_timeout_seconds=5),
        drive=lambda directories, stop: None, independent_evaluation=None)

    assert len(calls) == 1
    # The formal path needs a live stop event; it must not be handed None or run outside
    # the heartbeat that owns the lease and the deadline.
    assert calls[0]["stop_event"] is not None
    assert calls[0]["run_status"] == RunStatus.RUNNING
    assert result["finalization"]["reason"] == "no_verifier_execution"
    assert "finalization=not_finalized" in load_run_record(tmp_path / "record.json").notes


def test_a_failing_finalize_still_writes_a_record(
    recovery_session_factory, tmp_path, monkeypatch,
):
    import devflow_ai.benchmark_run as benchmark_run_module
    from devflow_ai.benchmark_record import RunOutcome, load_run_record
    from devflow_ai.benchmark_run import run_one_task

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    task_id = _eligible_benchmark_task(factory, repo=str(repo), base_commit=base_commit)
    (tmp_path / "w").mkdir()
    (tmp_path / "a").mkdir()

    def failing_finalize(worker_argument, run_id_argument, **kwargs):
        raise RuntimeError("independent evaluation is unavailable")

    monkeypatch.setattr(benchmark_run_module, "finalize_benchmark_run", failing_finalize)

    with pytest.raises(RuntimeError, match="independent evaluation is unavailable"):
        run_one_task(
            _benchmark_request(tmp_path, task_id, base_commit=base_commit),
            session_factory=factory, worker=Worker(factory, "bench-worker"), gateways=None,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=5,
                                             shutdown_timeout_seconds=5),
            drive=lambda directories, stop: None)

    # The attempt leaves evidence instead of vanishing mid-failure, and it is not filed
    # as either a pass or a failure of the task.
    record = load_run_record(tmp_path / "record.json")
    assert record.outcome is RunOutcome.INCOMPLETE
    assert "finalization=finalization_error" in record.notes
    assert "execution_error:RuntimeError" in record.notes


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Job")
def test_a_pack_public_test_runs_as_a_baseline_without_touching_the_checkout(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """Derive the config from a manifest and run it down the real baseline path.

    The public test lives in a task pack, not in the checkout. The assembly must make the
    controlled command find it while the checkout stays byte-identical to its commit, or
    the baseline integrity check would be meaningless.
    """
    from devflow_ai.task_pack import load_manifest, to_verification_config

    factory = recovery_session_factory
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, worker_id="pack-baseline", repo=str(repo), base_commit=base_commit)
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "pack-baseline")
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    stop = Event()
    directories = worker._ensure_recovery_workspace(
        rediagnosis_id, workspaces_root=workspaces, artifacts_root=artifacts, stop_event=stop)
    pack_dir = _benchmark_pack(tmp_path, base_commit=base_commit)
    (pack_dir / "scenarios.py").write_text(
        "from pathlib import Path\n\n"
        "def test_target():\n"
        "    assert Path(\"README.md\").read_bytes() == b\"Repository facts updated\\n\"\n\n"
        "def test_control():\n"
        "    assert True\n", encoding="utf-8")
    manifest = load_manifest(pack_dir)
    config = to_verification_config(
        manifest, task_id=task_id, public_tests_root=pack_dir,
        python_executable=sys.executable)
    assert config.command[0] == sys.executable
    with factory.begin() as session:
        supervisor = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={})
        session.add(supervisor)
        session.flush()
        handoff = Handoff(
            from_agent_execution_id=supervisor.id,
            to_agent_role=AgentRole.VERIFIER.value.lower(), context={})
        session.add(handoff)
        session.flush()
        supervisor.output = {"outcome": "BASELINE", "approved_handoff_id": str(handoff.id)}
        handoff_id = handoff.id
    command = worker._public_check_command(config, directories)
    assert str(directories.workspace_root) not in " ".join(command)

    receipt = worker.execute_baseline_verifier(
        rediagnosis_id, handoff_id=handoff_id, directories=directories,
        command_name=config.command_name, registry={config.command_name: command},
        stop_event=stop, pythonpath_relative=config.pythonpath_relative, verify_baseline=True)

    # The target test fails at the pinned commit, so a recorded baseline is valid.
    assert receipt["valid"] is True
    # The checkout itself was never modified to make this work.
    assert not (directories.workspace_root / "scenarios.py").exists()
    assert (directories.artifact_root / "public_tests" / "scenarios.py").is_file()
    with factory() as session:
        baseline = session.scalar(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.handoff_id == handoff_id))
        assert baseline.status == AgentExecutionStatus.SUCCEEDED
        assert baseline.output["outcome"] == "BASELINE_RECORDED"


def test_the_recorded_command_evidence_is_the_evidence_acceptance_recomputes(
    recovery_session_factory, tmp_path,
):
    """Execution and acceptance derive one command and one content digest from the config.

    This is the gap that made the assembly unverifiable: the run recorded the assembled
    command while acceptance hashed the untouched config command, so the two could never
    agree once the public tests lived outside the checkout.
    """
    from devflow_ai.verification_config import copied_public_tests_sha256, expected_command_evidence

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="command-evidence")
    directories = RunDirectories(tmp_path / "ws", tmp_path / "artifacts")
    directories.workspace_root.mkdir()
    directories.artifact_root.mkdir()
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "scenarios.py").write_text("def test_target():\n    assert False\n", encoding="utf-8")
    config = VerificationConfig(
        task_id=task_id, base_commit="a" * 40, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "scenarios.py"),
        target_tests=("scenarios::test_target",), public_tests_root=str(pack_dir))

    assembled = worker._public_check_command(config, directories)
    assert assembled != config.command
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER,
            agent_input={"purpose": "post_patch_verification"})
    receipt = worker.build_verifier_command(
        run_id, agent_execution_id=execution_id, directories=directories,
        registry={"pytest": assembled}, stop_event=Event(), pytest_command_names=frozenset(),
        public_tests_digest=copied_public_tests_sha256(directories.artifact_root))("pytest")
    assert (receipt["status"], receipt["exit_code"]) == ("failed", 1)

    expected = expected_command_evidence(config, directories.artifact_root)
    assert expected.command == assembled
    with factory() as session:
        call = session.get(ToolCall, UUID(receipt["tool_call_id"]))
        assert call.args["command_sha256"] == expected.command_sha256
        assert call.args["public_tests_sha256"] == expected.public_tests_sha256

    # The content, not just the path, is pinned: a rewritten copy stops matching.
    (directories.artifact_root / "public_tests" / "scenarios.py").write_text(
        "def test_target():\n    assert True\n", encoding="utf-8")
    assert copied_public_tests_sha256(directories.artifact_root) != expected.public_tests_sha256


def test_a_tool_the_boundary_proved_unstarted_is_recorded_as_failed(recovery_session_factory, tmp_path):
    """A start the boundary rejected must not leave a REQUESTED row masking the error."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="tool-start-failure")
    execution_id = _seed_stage_execution(
        factory, run_id, AgentRole.VERIFIER, output={},
        status=AgentExecutionStatus.RUNNING)

    def reject_before_start(call_id):
        # The real boundary rejects the relative interpreter before any process exists,
        # so it is the boundary — not the test — that proves non-execution.
        return run_registered_command(
            tmp_path, "pytest", registry={"pytest": ("python", "-m", "pytest")},
            timeout_seconds=5, stop_event=Event())

    with factory() as session:
        with pytest.raises(ValueError, match="absolute executable"):
            RunCoordinator(session).execute_agent_tool_call(
                run_id, worker.worker_id, agent_execution_id=execution_id,
                tool_name="run_command", args={"command": "pytest"},
                registered_tools={"run_command"}, execute=reject_before_start)

    with factory() as session:
        calls = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id)).all()
        assert len(calls) == 1
        assert calls[0].status == ToolCallStatus.FAILED
        assert calls[0].reason == "COMMAND_START_FAILED:ValueError"


def test_the_same_error_after_a_real_start_is_recorded_as_unknown(
    recovery_session_factory, tmp_path,
):
    """The same ValueError proves nothing once a child process has already run."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="tool-after-start")
    execution_id = _seed_stage_execution(
        factory, run_id, AgentRole.VERIFIER, output={},
        status=AgentExecutionStatus.RUNNING)

    def fail_after_start(call_id):
        run_registered_command(
            tmp_path, "probe", registry={"probe": (sys.executable, "-B", "-c", "print('started')")},
            timeout_seconds=15, stop_event=Event())
        raise ValueError("Registered command must use an absolute executable path")

    with factory() as session:
        with pytest.raises(ValueError, match="absolute executable"):
            RunCoordinator(session).execute_agent_tool_call(
                run_id, worker.worker_id, agent_execution_id=execution_id,
                tool_name="run_command", args={"command": "pytest"},
                registered_tools={"run_command"}, execute=fail_after_start)

    with factory() as session:
        calls = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id)).all()
        assert len(calls) == 1
        assert calls[0].status == ToolCallStatus.UNKNOWN_RESULT
        assert calls[0].reason == "COMMAND_RESULT_UNKNOWN:ValueError"


def test_rediagnosis_start_persists_count_and_replays_without_double_counting(
    recovery_session_factory,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    with factory() as session:
        history = RunCoordinator(session).observe_recovery_history(
            run_id, worker.worker_id, signature="unknown-v1:test")
    assert history.rediagnoses_started == 0 and history.retries_started == 0

    record_unknown_failure(factory, task_id, run_id)
    # A FailureRecord/REDIAGNOSE decision is not an actual node start.
    assert count_rediagnosis_runs(factory, task_id) == 0
    with factory() as session:
        run, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "rediagnosis-1")
        rediagnosis_id, attempt_number, action = run.id, run.attempt_number, run.recovery_action
    assert created and attempt_number == 2 and action == "REDIAGNOSE"
    # Creating a REDIAGNOSE Run is only a reservation; it is not an actual start.
    assert count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 0
    with factory() as session:
        history = RunCoordinator(session).observe_recovery_history(
            rediagnosis_id, worker.worker_id, signature="unknown-v1:test")
    assert history.rediagnoses_started == 0 and history.retries_started == 1

    with factory() as session:
        recorded = RunCoordinator(session).begin_rediagnosis_start(
            rediagnosis_id, worker.worker_id)
    assert recorded is True
    assert count_rediagnosis_starts(factory, task_id) == 1

    # A separate session represents a crash after the start transaction committed.
    with factory() as session:
        started_run = session.get(Run, rediagnosis_id)
        assert started_run.status == RunStatus.RUNNING
        assert started_run.recovery_started_at is not None
        assert session.get(Task, task_id).status == TaskStatus.RUNNING
    with factory() as session:
        same, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "rediagnosis-1")
        same_id = same.id
    assert not created and same_id == rediagnosis_id
    with factory() as session:
        assert RunCoordinator(session).begin_rediagnosis_start(
            rediagnosis_id, worker.worker_id) is False
    assert count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 1

    with factory() as session, pytest.raises(RunCreationConflictError):
        RunCoordinator(session).start_rediagnosis_run(task_id, "another-worker", "rediagnosis-1")
    with factory() as session, pytest.raises(TaskStateConflictError):
        RunCoordinator(session).start_rediagnosis_run(task_id, worker.worker_id, "rediagnosis-2")
    with factory() as session:
        history = RunCoordinator(session).observe_recovery_history(
            rediagnosis_id, worker.worker_id, signature="unknown-v1:test")
    assert history.rediagnoses_started == 1 and history.retries_started == 1


def test_existing_rediagnosis_can_resume_without_creating_or_counting_another_run(
    recovery_session_factory, monkeypatch, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    record_unknown_failure(factory, task_id, run_id)
    with factory() as session:
        run, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "resume-key")
        rediagnosis_id = run.id
    assert created and count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 0
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    resumed_runs = []

    def fake_ensure(self, run_id, *, workspaces_root, artifacts_root, stop_event):
        return RunDirectories(workspaces_root / str(run_id), artifacts_root / str(run_id))

    def fake_resume(self, run_id, **kwargs):
        resumed_runs.append(run_id)
        return {"outcome": "dispatched", "handoff_id": None}

    monkeypatch.setattr(Worker, "_ensure_recovery_workspace", fake_ensure)
    monkeypatch.setattr(Worker, "_resume_rediagnosis_locked", fake_resume)
    args = dict(creation_idempotency_key="resume-key", workspaces_root=workspaces,
                artifacts_root=artifacts, supervisor_gateway=None, explorer_gateway=None,
                planner_gateway=None, max_read_bytes=1024, stop_event=Event())
    first = worker.resume_rediagnosis_recovery(task_id, **args)
    second = worker.resume_rediagnosis_recovery(task_id, **args)
    assert first["run_id"] == rediagnosis_id and first["resumed"] is True
    assert second["run_id"] == rediagnosis_id and second["resumed"] is True
    assert resumed_runs == [rediagnosis_id, rediagnosis_id]
    assert count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 1


def test_rediagnosis_lock_busy_reports_busy_then_same_key_starts_once(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    record_unknown_failure(factory, task_id, run_id)
    with factory() as holder:
        with holder.begin():
            assert TaskRepository(holder).get_by_id_for_update(task_id) is not None
            with factory() as session:
                def set_timeout(session, transaction, connection):
                    connection.execute(text("SET LOCAL statement_timeout = '2s'"))

                event.listen(session, "after_begin", set_timeout)
                with pytest.raises(TaskClaimBusyError):
                    RunCoordinator(session).start_rediagnosis_run(
                        task_id, worker.worker_id, "busy-key")
                assert not session.in_transaction()
    with factory() as session:
        run, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "busy-key")
        first_id = run.id
    assert created and count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 0
    with factory() as session:
        assert RunCoordinator(session).begin_rediagnosis_start(first_id, worker.worker_id) is True
    assert count_rediagnosis_starts(factory, task_id) == 1
    with factory() as session:
        replay, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "busy-key")
        replay_id = replay.id
    assert not created and replay_id == first_id
    assert count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 1


def test_explorer_completion_immediately_reaches_a_supervisor_route(
    recovery_session_factory, monkeypatch, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    record_unknown_failure(factory, task_id, run_id)
    with factory() as session:
        run, _ = RunCoordinator(session).start_rediagnosis_run(task_id, worker.worker_id, "drive-key")
        rediagnosis_id = run.id
    with factory() as session:
        RunCoordinator(session).begin_rediagnosis_start(rediagnosis_id, worker.worker_id)
    with factory.begin() as session:
        explorer = AgentExecution(run_id=rediagnosis_id,
                                  agent_role=AgentRole.EXPLORER.value.lower(),
                                  status=AgentExecutionStatus.SUCCEEDED, input={}, output={})
        session.add(explorer)
        session.flush()
        explorer_id = explorer.id
    route_handoff_id = uuid4()
    supervised = []
    dispatched = []

    def fake_supervise(self, run_id, *, explorer_execution_id, model_gateway, stop_event):
        supervised.append(explorer_execution_id)
        return SimpleNamespace(execution_id=uuid4(), handoff_id=route_handoff_id)

    def fake_load(self, run_id, handoff_id):
        return CommittedHandoff(id=handoff_id, from_agent_execution_id=explorer_id,
                                to_agent_role=AgentRole.PLANNER, context={})

    def fake_exists(self, run_id, handoff_id):
        return False

    def fake_dispatch(self, run_id, handoff_id, **kwargs):
        dispatched.append(handoff_id)
        return True

    monkeypatch.setattr(Worker, "execute_supervisor_after_explorer", fake_supervise)
    monkeypatch.setattr(Worker, "_load_committed_handoff", fake_load)
    monkeypatch.setattr(Worker, "_handoff_receiver_exists", fake_exists)
    monkeypatch.setattr(Worker, "_dispatch_committed_handoff", fake_dispatch)
    result = worker._drive_rediagnosis_after_exploration(
        rediagnosis_id, directories=RunDirectories(tmp_path / "w", tmp_path / "a"),
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        max_read_bytes=1024, stop_event=Event())
    assert result["outcome"] == "dispatched"
    assert supervised == [explorer_id]
    assert dispatched == [route_handoff_id]


def test_committed_initial_handoff_is_dispatched_after_interruption(
    recovery_session_factory, monkeypatch, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    record_unknown_failure(factory, task_id, run_id)
    with factory() as session:
        run, _ = RunCoordinator(session).start_rediagnosis_run(task_id, worker.worker_id, "handoff-key")
        rediagnosis_id = run.id
    with factory() as session:
        RunCoordinator(session).begin_rediagnosis_start(rediagnosis_id, worker.worker_id)
    with factory.begin() as session:
        supervisor = AgentExecution(run_id=rediagnosis_id,
                                    agent_role=AgentRole.SUPERVISOR.value.lower(),
                                    status=AgentExecutionStatus.SUCCEEDED, input={},
                                    output={"outcome": "EXPLORE"})
        session.add(supervisor)
        session.flush()
        handoff = Handoff(from_agent_execution_id=supervisor.id,
                          to_agent_role=AgentRole.EXPLORER.value.lower(), context={})
        session.add(handoff)
        session.flush()
        supervisor.output = {"outcome": "EXPLORE", "approved_handoff_id": str(handoff.id)}
        handoff_id = handoff.id
    dispatched = []

    def fake_dispatch(self, run_id, handoff_id, **kwargs):
        dispatched.append(handoff_id)
        return True

    monkeypatch.setattr(Worker, "_dispatch_committed_handoff", fake_dispatch)
    result = worker._dispatch_initial_rediagnosis(
        rediagnosis_id, directories=RunDirectories(tmp_path / "w", tmp_path / "a"),
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        max_read_bytes=1024, stop_event=Event())
    assert result["outcome"] == "dispatched" and result["resumed"] is True
    assert dispatched == [handoff_id]


def test_rediagnosis_budget_and_legacy_history_are_conservative(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "budget": {**snapshot["budget"], "max_rediagnose_attempts": 1}}
    record_unknown_failure(factory, task_id, run_id)
    with factory() as session:
        first, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "rediagnosis-budget-1")
        first_id = first.id
    assert created
    with factory() as session:
        assert RunCoordinator(session).begin_rediagnosis_start(first_id, worker.worker_id) is True
    record_unknown_failure(factory, task_id, first_id)
    with factory() as session, pytest.raises(RecoveryDecisionUnavailableError, match="no longer permits"):
        RunCoordinator(session).start_rediagnosis_run(task_id, worker.worker_id, "rediagnosis-budget-2")
    assert count_rediagnosis_runs(factory, task_id) == 1
    assert count_rediagnosis_starts(factory, task_id) == 1

    # A REDIAGNOSE marker created before actual-start tracking is unknown too.
    now = datetime.now(timezone.utc)
    orphan_worker, orphan_task, orphan_run = create_running_run(factory, worker_id="orphan-worker")
    record_unknown_failure(factory, orphan_task, orphan_run)
    with factory.begin() as session:
        orphan_extra = Run(task_id=orphan_task, attempt_number=2, status=RunStatus.FAILED,
                           creation_idempotency_key="untracked-rediagnosis",
                           worker_id=orphan_worker.worker_id,
                           recovery_action="REDIAGNOSE",
                           lease_expires_at=now, finished_at=now)
        session.add(orphan_extra)
        session.flush()
        session.add(Verification(run_id=orphan_extra.id, result=VerificationResult.FAILED, evidence={}))
        session.add(FailureRecord(run_id=orphan_extra.id, category=FailureCategory.UNKNOWN_FAILURE,
                                  signature="unknown-v1:test", recovery_action="REDIAGNOSE"))
    with factory() as session, pytest.raises(RecoveryDecisionUnavailableError, match="history is unknown"):
        RunCoordinator(session).start_rediagnosis_run(
            orphan_task, orphan_worker.worker_id, "untracked-rediagnosis-1")

    legacy_worker, legacy_task, legacy_run = create_running_run(factory, worker_id="legacy-worker")
    record_unknown_failure(factory, legacy_task, legacy_run)
    with factory.begin() as session:
        # A pre-marker recovery Run: its action is unknown, so rediagnosis counts stay unknown.
        legacy_extra = Run(task_id=legacy_task, attempt_number=2, status=RunStatus.FAILED,
                           creation_idempotency_key="legacy-recovery", worker_id=legacy_worker.worker_id,
                           lease_expires_at=now, finished_at=now)
        session.add(legacy_extra)
        session.flush()
        session.add(Verification(run_id=legacy_extra.id, result=VerificationResult.FAILED, evidence={}))
        session.add(FailureRecord(run_id=legacy_extra.id, category=FailureCategory.UNKNOWN_FAILURE,
                                  signature="unknown-v1:test", recovery_action="REDIAGNOSE"))
        session.get(Task, legacy_task).status = TaskStatus.RETRYING
    with factory() as session, pytest.raises(RecoveryDecisionUnavailableError, match="history is unknown"):
        RunCoordinator(session).start_rediagnosis_run(
            legacy_task, legacy_worker.worker_id, "legacy-rediagnosis-1")


def test_pending_rediagnosis_reserves_budget_before_actual_start(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    record_unknown_failure(factory, task_id, run_id)
    with factory() as session:
        run, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, "pending-reservation")
        pending_id = run.id
    assert created and count_rediagnosis_starts(factory, task_id) == 0
    # Tighten the persisted quota after creation: the pending Run itself must
    # reserve the only slot, so this start cannot slip past the budget.
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "budget": {
            **snapshot["budget"], "max_rediagnose_attempts": 0}}
    with factory() as session, pytest.raises(RecoveryDecisionUnavailableError, match="no longer permits"):
        RunCoordinator(session).begin_rediagnosis_start(pending_id, worker.worker_id)
    assert count_rediagnosis_starts(factory, task_id) == 0
    with factory() as session:
        assert session.get(Run, pending_id).recovery_started_at is None


def test_rediagnosis_starts_are_task_isolated_and_concurrent(recovery_session_factory):
    factory = recovery_session_factory
    first_worker, first_task, first_run = create_running_run(factory, worker_id="first-worker")
    second_worker, second_task, second_run = create_running_run(factory, worker_id="second-worker")
    record_unknown_failure(factory, first_task, first_run)
    record_unknown_failure(factory, second_task, second_run)
    with factory() as session:
        RunCoordinator(session).start_rediagnosis_run(first_task, first_worker.worker_id, "first-rediagnosis")
    assert count_rediagnosis_runs(factory, first_task) == 1
    assert count_rediagnosis_runs(factory, second_task) == 0
    with factory() as session:
        RunCoordinator(session).start_rediagnosis_run(second_task, second_worker.worker_id, "second-rediagnosis")
    assert count_rediagnosis_runs(factory, first_task) == 1
    assert count_rediagnosis_runs(factory, second_task) == 1

    race_worker, race_task, race_run = create_running_run(factory, worker_id="race-worker")
    record_unknown_failure(factory, race_task, race_run, signature="unknown-v1:race")
    barrier = Barrier(2)

    def start_same_request(key):
        # A locked Task is reported as TaskClaimBusyError; the caller retries the
        # same creation key, which must replay the committed Run without counting again.
        barrier.wait()
        deadline = monotonic() + 10
        while True:
            try:
                with factory() as session:
                    run, created = RunCoordinator(session).start_rediagnosis_run(
                        race_task, race_worker.worker_id, key)
                    return run.id, created
            except TaskClaimBusyError:
                if monotonic() >= deadline:
                    raise
                sleep(0.02)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(start_same_request, "race-key"), pool.submit(start_same_request, "race-key")]
        results = [item.result() for item in results]
    assert sum(1 for _, created in results if created) == 1
    assert len({run_id for run_id, _ in results}) == 1
    race_run_id = results[0][0]
    assert count_rediagnosis_runs(factory, race_task) == 1
    assert count_rediagnosis_starts(factory, race_task) == 0
    with factory() as session:
        assert len(session.scalars(select(Run).where(Run.task_id == race_task)).all()) == 2

    start_barrier = Barrier(2)

    def start_same_run():
        start_barrier.wait()
        with factory() as session:
            return RunCoordinator(session).begin_rediagnosis_start(
                race_run_id, race_worker.worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        recorded = [pool.submit(start_same_run), pool.submit(start_same_run)]
        recorded = [item.result() for item in recorded]
    assert sorted(recorded) == [False, True]
    assert count_rediagnosis_starts(factory, race_task) == 1


def test_unknown_finalization_uses_persisted_budget_and_escalates(
    recovery_session_factory, monkeypatch, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "budget": {**snapshot["budget"], "max_rediagnose_attempts": 0}}
        verifier = AgentExecution(run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
                                  status=AgentExecutionStatus.RUNNING, input={})
        session.add(verifier)
        session.flush()
        verifier_id = verifier.id

    class Payload:
        def __init__(self, data):
            self.data = data

        def model_dump(self, mode="json"):
            return self.data

    proposal = VerificationFailureProposal(category=FailureCategory.UNKNOWN_FAILURE,
        reason="Evidence does not identify a known failure category", evidence_refs=("semantic_review",))
    draft = SimpleNamespace(selection=Payload({"command_name": "pytest"}),
                            review=Payload({"result": "failed"}))
    context = {"workspace_diff": {"content_hash": sha256(b"diff").hexdigest(), "statistics": {}}}
    comparison = Payload({"target_result": "failed"})
    data = {"verifier_execution_id": str(verifier_id), "draft_artifact_id": str(uuid4()),
            "independent_artifact_id": None}

    def fake_finalizable(self, run_id, *, task, supervisor_execution_id, config, artifact_root,
                         max_diff_bytes, max_patch_bytes):
        return proposal, draft, context, comparison, "semantic-v1:test", data

    monkeypatch.setattr(RunCoordinator, "_validate_finalizable_failure", fake_finalizable)
    monkeypatch.setattr(coordinator_module, "inspect_workspace_diff", lambda *args, **kwargs: SimpleNamespace(
        diff=SimpleNamespace(preview=SimpleNamespace(stdout_truncated=False), stdout=b"diff")))
    config = VerificationConfig(task_id=task_id, base_commit="a" * 40, command_name="pytest",
        command=("pytest",), target_tests=("tests::test_value",), external_connections="none")
    with factory() as session:
        result = RunCoordinator(session).finalize_unknown_failure(
            run_id, worker.worker_id, supervisor_execution_id=uuid4(), verification_config=config,
            workspace_root=tmp_path, artifact_root=tmp_path, max_diff_bytes=1024,
            max_patch_bytes=1024, stop_event=Event())
    assert result["recovery_action"] == "ESCALATE_TO_HUMAN"
    assert result["rediagnoses_started"] == 0 and result["rediagnoses_pending"] == 0
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.FAILED
        assert session.get(Task, task_id).status == TaskStatus.ESCALATED
        verification = session.scalars(select(Verification).where(Verification.run_id == run_id)).one()
        assert verification.result == VerificationResult.FAILED
        failure = session.scalars(select(FailureRecord).where(FailureRecord.run_id == run_id)).one()
        assert failure.category == FailureCategory.UNKNOWN_FAILURE
        assert failure.recovery_action == "ESCALATE_TO_HUMAN"
        assert "rediagnose_limit" in verification.evidence["policy_reasons"]


def test_recovery_action_migration_roundtrip_keeps_legacy_runs_unknown(recovery_session_factory):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="migration-worker")
    with factory.begin() as session:
        session.get(Run, run_id).status = RunStatus.FAILED
        legacy = Run(task_id=task_id, attempt_number=2, status=RunStatus.RUNNING,
                     creation_idempotency_key="migration-recovery", worker_id=worker.worker_id,
                     lease_expires_at=datetime.now(timezone.utc))
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    migration = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")).get_revision(
        "e7a1c4d9b2f0").module
    start_migration = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")).get_revision(
        "f3b8d2a6c1e4").module
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert "recovery_action" not in {
                column["name"] for column in inspect(connection).get_columns("runs", schema=schema)}
            migration.upgrade()
            assert "recovery_action" in {
                column["name"] for column in inspect(connection).get_columns("runs", schema=schema)}
            start_migration.downgrade()
            start_columns = {column["name"] for column in inspect(connection).get_columns("runs", schema=schema)}
            assert "recovery_started_at" not in start_columns
            assert "recovery_start_tracking" not in start_columns
            start_migration.upgrade()
            start_columns = {column["name"] for column in inspect(connection).get_columns("runs", schema=schema)}
            assert {"recovery_started_at", "recovery_start_tracking"} <= start_columns
    with factory() as session:
        legacy = session.get(Run, legacy_id)
        assert legacy.recovery_action is None
        assert legacy.recovery_started_at is None and legacy.recovery_start_tracking is None


def _start_rediagnosis(factory, worker, task_id, run_id, key):
    record_unknown_failure(factory, task_id, run_id)
    with factory() as session:
        run, created = RunCoordinator(session).start_rediagnosis_run(
            task_id, worker.worker_id, key)
        rediagnosis_id = run.id
    assert created
    with factory() as session:
        assert RunCoordinator(session).begin_rediagnosis_start(
            rediagnosis_id, worker.worker_id) is True
    return rediagnosis_id


def _seed_committed_planner_route(factory, run_id, *, target=None, outcome):
    """Seed committed Planner and post-Planner Supervisor rows without a model call."""
    with factory.begin() as session:
        planner = AgentExecution(
            run_id=run_id, agent_role=AgentRole.PLANNER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=1), input={},
            output={"outcome": "PLAN_READY", "plan": {
                "approach": "Update the documentation", "target_paths": ["README.md"],
                "steps": ["Edit the relevant section"], "scope_limits": "Only README.md",
                "verification": ["Review the diff"],
            }},
        )
        session.add(planner)
        session.flush()
        planner_id = planner.id
    # A distinct transaction gives the Supervisor a strictly later created_at.
    with factory.begin() as session:
        supervisor = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={"outcome": outcome},
        )
        session.add(supervisor)
        session.flush()
        if target is not None:
            handoff = Handoff(
                from_agent_execution_id=planner_id, to_agent_role=target.value.lower(),
                context={"completed_work": "Plan prepared", "blocker": None, "evidence_refs": [],
                         "decision_required": "Continue", "scope_impact": "Continue within policy"},
            )
            session.add(handoff)
            session.flush()
            supervisor.output = {"outcome": outcome, "approved_handoff_id": str(handoff.id)}
    return planner_id


def _call_post_planner(worker, run_id, planner_id, tmp_path, *, verification_config=None,
                       coder_gateway=None, max_patch_bytes=None):
    return worker._continue_rediagnosis_after_planner(
        run_id, planner_execution_id=planner_id,
        directories=RunDirectories(tmp_path / "workspace", tmp_path / "artifacts"),
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        verification_config=verification_config, max_read_bytes=1024, stop_event=Event(),
        coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
    )


@pytest.mark.parametrize(("outcome", "target", "expected"), [
    ("BLOCKED", None, "blocked"),
    ("RE_EXPLORE", AgentRole.EXPLORER, "replan"),
    ("CODE", AgentRole.CODER, "code_pending"),
])
def test_post_planner_non_baseline_routes_return_safely(
    recovery_session_factory, tmp_path, outcome, target, expected,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, f"route-{expected}")
    planner_id = _seed_committed_planner_route(
        factory, rediagnosis_id, target=target, outcome=outcome)

    result = _call_post_planner(worker, rediagnosis_id, planner_id, tmp_path)

    assert result["outcome"] == expected
    if target is None:
        assert "handoff_id" not in result
    if expected == "code_pending":
        assert result["reason"] == "coder_config_missing"
    with factory() as session:
        roles = set(session.scalars(select(AgentExecution.agent_role).where(
            AgentExecution.run_id == rediagnosis_id)))
        assert AgentRole.CODER.value.lower() not in roles
        assert AgentRole.VERIFIER.value.lower() not in roles


def test_post_planner_baseline_needs_a_matching_trusted_config(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "baseline-config")
    planner_id = _seed_committed_planner_route(
        factory, rediagnosis_id, target=AgentRole.VERIFIER, outcome="BASELINE")

    missing = _call_post_planner(worker, rediagnosis_id, planner_id, tmp_path)
    assert missing["outcome"] == "baseline_config_missing"

    wrong_task = VerificationConfig(
        task_id=uuid4(), base_commit="b" * 40, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "test_baseline.py"),
        target_tests=("test_baseline.py::test_baseline",), external_connections="none",
    )
    mismatched = _call_post_planner(
        worker, rediagnosis_id, planner_id, tmp_path, verification_config=wrong_task)
    assert mismatched["outcome"] == "baseline_config_invalid"
    with factory() as session:
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
        )).all()


def test_post_baseline_code_rejects_invalid_baseline_without_starting_coder(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "invalid-baseline")
    now = datetime.now(timezone.utc)
    plan = {"outcome": "PLAN_READY", "plan": {
        "approach": "Update the documentation", "target_paths": ["README.md"],
        "steps": ["Edit the relevant section"], "scope_limits": "Only README.md",
        "verification": ["Review the diff"],
    }}
    with factory.begin() as session:
        planner = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.PLANNER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=4),
            input={}, output=plan,
        )
        session.add(planner)
        session.flush()
        planner_id = planner.id
    with factory.begin() as session:
        baseline_handoff = Handoff(
            from_agent_execution_id=planner_id, to_agent_role=AgentRole.VERIFIER.value.lower(),
            context={"completed_work": "Baseline requested", "blocker": None,
                     "evidence_refs": [], "decision_required": "Run baseline",
                     "scope_impact": "Only README.md"},
        )
        session.add(baseline_handoff)
        session.flush()
        session.add(AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=3),
            input={}, output={"outcome": "BASELINE", "approved_handoff_id": str(baseline_handoff.id)},
        ))
        baseline = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, handoff_id=baseline_handoff.id,
            created_at=now - timedelta(seconds=2), input={"command_name": "pytest"},
            # The claimed report artifact does not exist, so the existing Coder
            # baseline evidence check must reject it.
            output={"outcome": "BASELINE_RECORDED", "tool_call_id": str(uuid4()),
                    "report_artifact_id": str(uuid4())},
        )
        session.add(baseline)
        session.flush()
        baseline_id = baseline.id
    with factory.begin() as session:
        code_handoff = Handoff(
            from_agent_execution_id=planner_id, to_agent_role=AgentRole.CODER.value.lower(),
            context={"completed_work": "Plan prepared", "blocker": None,
                     "evidence_refs": [{"baseline_verifier_id": str(baseline_id)}],
                     "decision_required": "Implement", "scope_impact": "Only README.md"},
        )
        session.add(code_handoff)
        session.flush()
        session.add(AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now,
            input={}, output={"outcome": "CODE", "approved_handoff_id": str(code_handoff.id)},
        ))
    coder_gateway = SimpleNamespace(
        request=lambda *args, **kwargs: pytest.fail("Coder model must not run with an invalid baseline"))

    result = _call_post_planner(
        worker, rediagnosis_id, planner_id, tmp_path,
        coder_gateway=coder_gateway, max_patch_bytes=4096,
    )

    assert result["outcome"] == "baseline_unavailable"
    assert result["reason"] == "baseline_evidence_invalid"
    with factory() as session:
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower(),
        )).all()


def test_post_planner_coder_route_requires_the_current_lease(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "lease-coder")
    planner_id = _seed_committed_planner_route(
        factory, rediagnosis_id, target=AgentRole.CODER, outcome="CODE")
    with factory.begin() as session:
        session.get(Run, rediagnosis_id).lease_expires_at = func.clock_timestamp()

    with pytest.raises(RunExecutionDeniedError):
        _call_post_planner(
            worker, rediagnosis_id, planner_id, tmp_path,
            coder_gateway=SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("Coder must not run")),
            max_patch_bytes=4096,
        )

    with factory() as session:
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower(),
        )).all()


def _create_git_fixture(path):
    path.mkdir()
    (path / "README.md").write_bytes(b"Repository facts\n")
    # The target must fail at the trusted baseline and pass after the patch;
    # the control test keeps regression comparison available.
    (path / "test_baseline.py").write_bytes(
        b"from pathlib import Path\n\n"
        b"def test_baseline():\n"
        b"    assert Path(\"README.md\").read_bytes() == b\"Repository facts updated\\n\"\n\n"
        b"def test_control():\n"
        b"    assert True\n")

    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, timeout=15)

    git("init", "-q")
    git("add", "README.md", "test_baseline.py")
    git("-c", "user.name=DevFlow Test", "-c", "user.email=devflow@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    return git("rev-parse", "HEAD").stdout.decode("ascii").strip()


def _run_rediagnosis_to_verifier(
    factory, tmp_path, monkeypatch, *, key, material=None, worker_id="baseline-worker",
    config_factory=None,
):
    """Drive one real Git/pytest chain to the intermediate post-patch Verifier."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, repo=str(repo), base_commit=base_commit, worker_id=worker_id)
    # A rediagnosis Run may only start from the committed UNKNOWN_FAILURE/REDIAGNOSE
    # decision, not from the still-RUNNING initial Run.
    record_unknown_failure(factory, task_id, run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    stop = Event()

    supervisor_prompts = []
    def supervise(prompt, _limit):
        supervisor_prompts.append(prompt)
        if len(supervisor_prompts) == 1:
            decision = {"outcome": "EXPLORE", "reason": "Repository facts are missing",
                        "exploration_focus": "Read the README"}
        elif len(supervisor_prompts) == 2:
            decision = {"outcome": "PLAN", "reason": "The evidence is sufficient",
                        "next_goal": "Plan the documentation change"}
        elif len(supervisor_prompts) == 3:
            decision = {"outcome": "BASELINE", "reason": "Establish the registered baseline",
                        "next_goal": "Run the trusted registered baseline test"}
        elif len(supervisor_prompts) == 4:
            decision = {"outcome": "CODE", "reason": "The trusted baseline is recorded",
                        "next_goal": "Apply the documentation change"}
        else:
            assert len(supervisor_prompts) == 5
            decision = {"outcome": "VERIFY", "reason": "The patch is ready for public checks",
                        "next_goal": "Run the post-patch verification"}
        return ModelReply(decision, ModelTokenUsage(input_tokens=40, output_tokens=20))

    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=supervise,
    )
    explorer_prompts = []
    def explore(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        explorer_prompts.append(data)
        if len(explorer_prompts) == 1:
            response = {"path": "README.md"}
        else:
            response = {"summary": "Read the repository facts", "read_evidence": {
                "path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}}
        return ModelReply(response, ModelTokenUsage(input_tokens=40, output_tokens=20))

    explorer_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=explore,
    )
    planner_gateway = PlannerModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        max_output_tokens=100,
        generate=lambda *_: ModelReply({"outcome": "PLAN_READY", "plan": {
            "approach": "Update the documentation", "target_paths": ["README.md"],
            "steps": ["Edit the relevant section"], "scope_limits": "Only README.md",
            "verification": ["Review the diff"],
        }}, ModelTokenUsage(input_tokens=40, output_tokens=20)),
    )
    config = VerificationConfig(
        task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "test_baseline.py"),
        target_tests=("test_baseline::test_baseline",), external_connections="none",
    )
    if config_factory is not None:
        config = config_factory(task_id, base_commit)
    patch = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
             "@@ -1 +1 @@\n-Repository facts\n+Repository facts updated\n")
    coder_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply(
            {"outcome": "PATCH_DRAFT", "patch": patch, "summary": "Update README"},
            ModelTokenUsage(input_tokens=40, output_tokens=20)),
    )
    semantic_prompts = []
    def semantic_review(prompt, _limit):
        supplied = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        semantic_prompts.append(supplied)
        return ModelReply({"outcome": "REVIEW", "result": "passed",
            "reason": "The documentation change matches the committed patch",
            "citations": [{"source": "workspace_diff", "path": "README.md",
                "tool_call_id": supplied["context"]["workspace_diff"]["tool_call_id"],
                "snippet": "Repository facts updated", "diff_side": "added",
                "reason": "The changed line is the documented update"}]},
            ModelTokenUsage(input_tokens=40, output_tokens=20))
    verifier_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=200, generate=semantic_review)
    args = dict(
        creation_idempotency_key=key, workspaces_root=workspaces,
        artifacts_root=artifacts, supervisor_gateway=supervisor_gateway,
        explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
        max_read_bytes=4096, stop_event=stop, verification_config=config,
        coder_gateway=coder_gateway, max_patch_bytes=4096,
        verifier_gateway=verifier_gateway, verifier_max_diff_bytes=65536,
    )
    if material is not None:
        args["independent_evaluation"] = material
    result = worker.resume_rediagnosis_recovery(task_id, **args)
    return SimpleNamespace(
        worker=worker, task_id=task_id, factory=factory, artifacts=artifacts,
        workspaces=workspaces, repo=repo, base_commit=base_commit, config=config,
        stop=stop, args=args, result=result, rediagnosis_id=result["run_id"],
        supervisor_prompts=supervisor_prompts, semantic_prompts=semantic_prompts,
    )


def _run_rediagnosis_to_replan(
    factory, tmp_path, monkeypatch, *, key,
    replan_planner_output=None, replan_route=None,
):
    """Drive one real Git/pytest chain to a Coder BLOCKED -> Supervisor REPLAN."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, repo=str(repo), base_commit=base_commit, worker_id="replan-worker")
    record_unknown_failure(factory, task_id, run_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    stop = Event()

    supervisor_prompts = []
    def supervise(prompt, _limit):
        supervisor_prompts.append(prompt)
        if len(supervisor_prompts) == 1:
            decision = {"outcome": "EXPLORE", "reason": "Repository facts are missing",
                        "exploration_focus": "Read the README"}
        elif len(supervisor_prompts) == 2:
            decision = {"outcome": "PLAN", "reason": "The evidence is sufficient",
                        "next_goal": "Plan the documentation change"}
        elif len(supervisor_prompts) == 3:
            decision = {"outcome": "BASELINE", "reason": "Establish the registered baseline",
                        "next_goal": "Run the trusted registered baseline test"}
        elif len(supervisor_prompts) == 4:
            decision = {"outcome": "CODE", "reason": "The trusted baseline is recorded",
                        "next_goal": "Apply the documentation change"}
        elif len(supervisor_prompts) == 5:
            decision = {"outcome": "REPLAN", "reason": "The Coder found the plan too broad",
                        "next_goal": "Plan a narrower documentation change"}
        elif len(supervisor_prompts) == 6:
            decision = replan_route or {
                "outcome": "RE_EXPLORE", "reason": "The narrower plan needs refreshed evidence",
                "next_goal": "Re-read the repository facts for the narrower plan"}
        else:
            pytest.fail("The chain must stop after the committed post-Planner route")
        return ModelReply(decision, ModelTokenUsage(input_tokens=40, output_tokens=20))

    supervisor_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=supervise,
    )
    explorer_prompts = []
    def explore(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        if len(explorer_prompts) == 0:
            response = {"path": "README.md"}
        else:
            response = {"summary": "Read the repository facts", "read_evidence": {
                "path": "README.md", "tool_call_id": data["read"]["tool_call_id"]}}
        explorer_prompts.append(data)
        return ModelReply(response, ModelTokenUsage(input_tokens=40, output_tokens=20))

    explorer_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=explore,
    )
    blocker = "The approved plan requires a broader change than the Issue permits"
    planner_prompts = []
    def plan(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        planner_prompts.append(data)
        if len(planner_prompts) == 1:
            response = {"outcome": "PLAN_READY", "plan": {
                "approach": "Update the documentation", "target_paths": ["README.md"],
                "steps": ["Edit the relevant section"], "scope_limits": "Only README.md",
                "verification": ["Review the diff"],
            }}
        elif replan_planner_output is not None:
            response = replan_planner_output
        else:
            response = {"outcome": "PLAN_READY", "plan": {
                "approach": "Take the narrower documentation update",
                "target_paths": ["README.md"], "steps": ["Edit the relevant section"],
                "scope_limits": "Only README.md", "verification": ["Review the diff"],
            }}
        return ModelReply(response, ModelTokenUsage(input_tokens=40, output_tokens=20))

    planner_gateway = PlannerModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=plan,
    )
    config = VerificationConfig(
        task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "test_baseline.py"),
        target_tests=("test_baseline::test_baseline",), external_connections="none",
    )
    coder_prompts = []
    def code(prompt, _limit):
        coder_prompts.append(prompt)
        return ModelReply(
            {"outcome": "BLOCKED", "completed_work": "Reviewed the approved plan",
             "blocker": blocker, "evidence_refs": [{"path": "README.md"}],
             "decision_required": "Replan within the documented scope",
             "scope_impact": "Only README.md"},
            ModelTokenUsage(input_tokens=40, output_tokens=20))

    coder_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=code,
    )
    args = dict(
        creation_idempotency_key=key, workspaces_root=workspaces,
        artifacts_root=artifacts, supervisor_gateway=supervisor_gateway,
        explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
        max_read_bytes=4096, stop_event=stop, verification_config=config,
        coder_gateway=coder_gateway, max_patch_bytes=4096,
    )
    result = worker.resume_rediagnosis_recovery(task_id, **args)
    return SimpleNamespace(
        worker=worker, task_id=task_id, factory=factory, artifacts=artifacts,
        workspaces=workspaces, repo=repo, base_commit=base_commit, config=config,
        stop=stop, args=args, result=result, rediagnosis_id=result["run_id"],
        supervisor_prompts=supervisor_prompts, explorer_prompts=explorer_prompts,
        planner_prompts=planner_prompts, coder_prompts=coder_prompts, blocker=blocker,
    )


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_post_coder_replan_dispatches_existing_planner_once_and_stops(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """Stub models plus real Git/pytest exercise the post-Coder REPLAN receiver."""
    setup = _run_rediagnosis_to_replan(
        recovery_session_factory, tmp_path, monkeypatch, key="rediagnosis-replan")
    factory, result = setup.factory, setup.result

    assert result["outcome"] == "replanned"
    assert result["reason"] == "coder_replan"
    assert result["planner_outcome"] == "PLAN_READY"
    assert result["resumed"] is False
    assert len(setup.supervisor_prompts) == 6
    assert len(setup.coder_prompts) == 1
    assert len(setup.planner_prompts) == 2
    handoff_id = result["post_coder_handoff_id"]
    replan_supervisor_id = result["replan_supervisor_execution_id"]
    replan_handoff_id = result["replan_handoff_id"]

    with factory() as session:
        planners = list(session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
        ).order_by(AgentExecution.created_at, AgentExecution.id)))
        assert len(planners) == 2
        original, replanned = planners
        assert replanned.id == result["planner_execution_id"]
        assert replanned.handoff_id == handoff_id
        assert replanned.status == AgentExecutionStatus.SUCCEEDED
        assert replanned.output["plan"]["approach"] == "Take the narrower documentation update"
        explorer_id = original.input["explorer_execution_id"]
        assert replanned.input["explorer_execution_id"] == explorer_id
        handoff = session.get(Handoff, handoff_id)
        assert handoff.context["blocker"] == setup.blocker
        assert {"explorer_execution_id": explorer_id} in handoff.context["evidence_refs"]
        assert {"path": "README.md"} in handoff.context["evidence_refs"]
        assert replanned.input["handoff"]["blocker"] == setup.blocker
        assert {"explorer_execution_id": explorer_id} in replanned.input["handoff"]["evidence_refs"]
        assert {"path": "README.md"} in replanned.input["handoff"]["evidence_refs"]
        # One real post-replan Supervisor route is committed under the re-plan's
        # identity; its Handoff is saved but never dispatched to Explorer/Coder.
        replan_supervisor = session.get(AgentExecution, replan_supervisor_id)
        assert replan_supervisor.input["planner_execution_id"] == str(replanned.id)
        assert replan_supervisor.status == AgentExecutionStatus.SUCCEEDED
        assert replan_supervisor.output["outcome"] == "RE_EXPLORE"
        assert replan_supervisor.output["approved_handoff_id"] == str(replan_handoff_id)
        route_handoff = session.get(Handoff, replan_handoff_id)
        assert route_handoff.from_agent_execution_id == replanned.id
        assert route_handoff.to_agent_role == AgentRole.EXPLORER.value.lower()
        assert result["replan_handoff_target"] == AgentRole.EXPLORER.value.lower()
        # The initial plan's Supervisors are not reused for this route.
        other_supervisors = list(session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
            AgentExecution.id != replan_supervisor_id)))
        assert len(other_supervisors) == 5
        assert all(
            supervisor.input.get("planner_execution_id") != str(replanned.id)
            for supervisor in other_supervisors)
        # The committed route stops the graph; no later Coder, Verifier, formal
        # Verification or terminal Run change is created.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 6
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING

    replay = setup.worker.resume_rediagnosis_recovery(setup.task_id, **setup.args)
    assert replay["outcome"] == "replanned" and replay["resumed"] is True
    assert replay["planner_execution_id"] == result["planner_execution_id"]
    assert replay["replan_supervisor_execution_id"] == replan_supervisor_id
    assert replay["replan_handoff_id"] == replan_handoff_id
    assert len(setup.supervisor_prompts) == 6
    assert len(setup.coder_prompts) == 1
    assert len(setup.planner_prompts) == 2
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.PLANNER.value.lower())) == 2
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 6


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_rediagnosis_planner_route_records_a_real_trusted_baseline_once(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """Stub models, real Git/pytest and the isolated schema exercise Planner to Coder."""
    setup = _run_rediagnosis_to_verifier(
        recovery_session_factory, tmp_path, monkeypatch, key="rediagnosis-baseline")
    worker, task_id, factory, artifacts = setup.worker, setup.task_id, setup.factory, setup.artifacts
    workspaces, args, result = setup.workspaces, setup.args, setup.result
    supervisor_prompts, semantic_prompts = setup.supervisor_prompts, setup.semantic_prompts
    rediagnosis_id = setup.rediagnosis_id

    assert result["outcome"] == "verifier_blocked"
    assert result["preliminary_public_outcome"] == "passed"
    assert len(supervisor_prompts) == 5 and len(semantic_prompts) == 1
    rediagnosis_id = result["run_id"]
    with factory() as session:
        executions = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id)).all()
        roles = [execution.agent_role for execution in executions]
        assert (roles.count("planner") == 1 and roles.count("verifier") == 2
                and roles.count("coder") == 1 and roles.count("supervisor") == 5)
        baseline = next(execution for execution in executions
                        if execution.agent_role == "verifier"
                        and execution.output["outcome"] == "BASELINE_RECORDED")
        report_id = UUID(baseline.output["report_artifact_id"])
        assert session.get(Artifact, report_id).kind == "pytest_junit"
        report = load_pytest_report_artifact(
            session, rediagnosis_id, baseline.id,
            tool_call_id=UUID(baseline.output["tool_call_id"]), artifact_id=report_id,
            artifact_root=artifacts / str(rediagnosis_id), expected_command_name="pytest",
        )
        assert len(report.tests) == 2
        baseline_tests = {test.name: test.status.value for test in report.tests}
        assert baseline_tests["test_baseline::test_baseline"] == "failed"
        assert baseline_tests["test_baseline::test_control"] == "passed"
        coder = next(execution for execution in executions if execution.agent_role == "coder")
        assert coder.output["outcome"] == "PATCH_READY"
        assert {"baseline_verifier_id": str(baseline.id)} in coder.input["handoff"]["evidence_refs"]
        assert session.get(Artifact, UUID(coder.output["patch_artifact_id"])).kind == "patch"
        assert len(session.scalars(select(ToolCall).where(
            ToolCall.run_id == rediagnosis_id, ToolCall.agent_execution_id == coder.id,
            ToolCall.tool_name == "apply_patch", ToolCall.status == ToolCallStatus.SUCCEEDED)).all()) == 1
        post_verifier = next(execution for execution in executions
                             if execution.agent_role == "verifier"
                             and execution.output["outcome"] == "VERIFICATION_BLOCKED")
        draft_id = UUID(post_verifier.output["draft_artifact_id"])
        assert post_verifier.id == result["verifier_execution_id"]
        assert draft_id == UUID(result["draft_artifact_id"])
        assert post_verifier.status == AgentExecutionStatus.RUNNING
        assert session.get(Artifact, draft_id).kind == "semantic_review_draft"
        assert "test_integrity" in post_verifier.output["missing_evidence"]
        assert post_verifier.output["external_connections_declaration"] == "none"
        # This step stops at intermediate evidence; no formal Verification or verdict.
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == rediagnosis_id)) == 0
        assert session.get(Run, rediagnosis_id).status == RunStatus.RUNNING
    assert (workspaces / str(rediagnosis_id) / "README.md").read_bytes() == b"Repository facts updated\n"

    replay = worker.resume_rediagnosis_recovery(task_id, **args)
    assert replay["outcome"] == "verifier_blocked" and replay["resumed"] is True
    assert len(supervisor_prompts) == 5 and len(semantic_prompts) == 1
    with factory() as session:
        assert len(session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
        )).all()) == 2
        assert len(session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower(),
        )).all()) == 1
        assert len(session.scalars(select(ToolCall).where(
            ToolCall.run_id == rediagnosis_id, ToolCall.tool_name == "apply_patch")).all()) == 1
        assert len(session.scalars(select(ToolCall).where(
            ToolCall.run_id == rediagnosis_id, ToolCall.tool_name == "run_command")).all()) == 2
        assert len(session.scalars(select(Artifact).where(
            Artifact.agent_execution_id.in_(select(AgentExecution.id).where(
                AgentExecution.run_id == rediagnosis_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())),
            Artifact.kind == "semantic_review_draft")).all()) == 1
    assert count_rediagnosis_starts(factory, task_id) == 1


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_a_pack_located_public_test_keeps_one_command_from_baseline_to_acceptance(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """Baseline, post-patch check and acceptance must agree on one command and one copy.

    The public test lives in a task pack, so the assembled command differs from the
    configured one. Execution records the assembled digest and the acceptance check
    recomputes it from the trusted config: if those two ever diverge, the post-patch
    readiness step fails and the whole chain stops here.
    """
    from devflow_ai.verification_config import copied_public_tests_sha256, expected_command_evidence

    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "scenarios.py").write_text(
        "from pathlib import Path\n\n"
        "def test_target():\n"
        "    assert Path(\"README.md\").read_bytes() == b\"Repository facts updated\\n\"\n\n"
        "def test_control():\n"
        "    assert True\n", encoding="utf-8")

    def config_factory(task_id, base_commit):
        return VerificationConfig(
            task_id=task_id, base_commit=base_commit, command_name="pytest",
            command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     "scenarios.py"),
            # The parser records `classname::name`, where classname is the module stem.
            target_tests=("scenarios::test_target",), external_connections="none",
            public_tests_root=str(pack_dir))

    setup = _run_rediagnosis_to_verifier(
        recovery_session_factory, tmp_path, monkeypatch, key="pack-chain",
        config_factory=config_factory)
    assert setup.result["outcome"] == "verifier_blocked"
    assert setup.result["preliminary_public_outcome"] == "passed"
    rediagnosis_id = setup.result["run_id"]
    directories = worker_module.resolve_run_directories(
        rediagnosis_id, workspaces_root=setup.workspaces, artifacts_root=setup.artifacts)

    expected = expected_command_evidence(setup.config, directories.artifact_root)
    assert expected.command != setup.config.command
    assert (directories.artifact_root / "public_tests" / "scenarios.py").is_file()
    # The checkout still matches its commit; the tests ran from the run-scoped copy.
    assert not (directories.workspace_root / "scenarios.py").exists()
    assert copied_public_tests_sha256(directories.artifact_root) == expected.public_tests_sha256
    with setup.factory() as session:
        calls = session.scalars(select(ToolCall).where(
            ToolCall.run_id == rediagnosis_id, ToolCall.tool_name == "run_command",
        ).order_by(ToolCall.created_at)).all()
        # One baseline and one post-patch run, recording the same evidence.
        assert len(calls) == 2
        for call in calls:
            assert call.args["command_sha256"] == expected.command_sha256
            assert call.args["public_tests_sha256"] == expected.public_tests_sha256


def _create_calibration_repo(path, content):
    path.mkdir()
    (path / "README.md").write_bytes(content)

    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, timeout=15)

    git("init", "-q")
    git("add", "README.md")
    git("-c", "user.name=DevFlow Test", "-c", "user.email=devflow@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "known correction")
    return git("rev-parse", "HEAD").stdout.decode("ascii").strip()


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="Requires an explicitly enabled Docker probe and postgres:17.11 image")
@pytest.mark.parametrize(("marker", "expected"), [
    ("Repository facts updated", "passed"),
    ("known correction", "independent_evaluation_failed"),
])
def test_rediagnosis_independent_evaluation_commits_or_blocks_once(
    recovery_session_factory, tmp_path, monkeypatch, marker, expected,
):
    """Real Git/pytest plus controller-held Docker cases finish or block the Run."""
    setup = _run_rediagnosis_to_verifier(
        recovery_session_factory, tmp_path, monkeypatch,
        key=f"rediagnosis-independent-{expected}", worker_id="independent-worker")
    assert setup.result["outcome"] == "verifier_blocked"
    assert setup.result["preliminary_public_outcome"] == "passed"
    verifier_id = UUID(str(setup.result["verifier_execution_id"]))
    corrected = tmp_path / "corrected-calibration"
    corrected_commit = _create_calibration_repo(corrected, (marker + "\n").encode())
    adapter = ("perl", "-e", 'open my $f, "<", "README.md" or die; local $/; '
               f'my $s=<$f>; print $s =~ /{marker}/ ? "ok\\n" : "wrong\\n";')
    material = IndependentEvaluationMaterial(
        image="postgres:17.11", command=adapter,
        cases=(PrivateCase(b"", b"ok\n"),),
        calibration_workspaces=(setup.repo, corrected.resolve(), corrected_commit),
        timeout_seconds=20,
    )
    config = setup.config.model_copy(update={"independent_suite_version": "rediagnosis-synthetic-v1"})
    args = {**setup.args, "verification_config": config, "independent_evaluation": material}

    missing = setup.worker.resume_rediagnosis_recovery(
        setup.task_id, **{**args, "verification_config": config.model_copy(
            update={"independent_suite_version": None})})
    assert missing["outcome"] == "verifier_blocked"
    assert missing["reason"] == "independent_evaluation_config_missing"
    with setup.factory() as session:
        assert not session.scalars(select(Artifact).where(
            Artifact.agent_execution_id == verifier_id,
            Artifact.kind == "independent_evaluation")).all()

    result = setup.worker.resume_rediagnosis_recovery(setup.task_id, **args)
    with setup.factory() as session:
        receipts = session.scalars(select(Artifact).where(
            Artifact.agent_execution_id == verifier_id,
            Artifact.kind == "independent_evaluation")).all()
        assert len(receipts) == 1
        receipt_bytes = Path(receipts[0].content_ref).read_bytes()
        receipt = json.loads(receipt_bytes)
        assert receipt["calibrated"] is True
        assert "expected_stdout" not in receipt and "stdin" not in receipt
        assert marker.encode() not in receipt_bytes
        verifications = session.scalars(select(Verification).where(
            Verification.run_id == setup.rediagnosis_id)).all()
        run = session.get(Run, setup.rediagnosis_id)
        verifier = session.get(AgentExecution, verifier_id)
        if expected == "passed":
            assert result["outcome"] == "passed" and result["verification_id"]
            assert receipt["outcome"] == "passed"
            assert len(verifications) == 1
            assert verifications[0].result == VerificationResult.PASSED
            assert run.status == RunStatus.PASSED
            assert session.get(Task, setup.task_id).status == TaskStatus.AWAITING_REVIEW
            assert verifier.status == AgentExecutionStatus.SUCCEEDED
            assert verifier.output["outcome"] == "VERIFICATION_PASSED"
        else:
            assert result["outcome"] == "independent_evaluation_failed"
            assert result["verification_id"] is None
            assert receipt["outcome"] == "failed"
            assert verifications == []
            assert run.status == RunStatus.RUNNING
            assert verifier.status == AgentExecutionStatus.RUNNING
            assert verifier.output["outcome"] == "VERIFICATION_BLOCKED"

    replay = setup.worker.resume_rediagnosis_recovery(setup.task_id, **args)
    if expected == "passed":
        assert replay["outcome"] == "existing_rediagnosis"
    else:
        assert replay["outcome"] == "independent_evaluation_failed"
    with setup.factory() as session:
        assert len(session.scalars(select(Artifact).where(
            Artifact.agent_execution_id == verifier_id,
            Artifact.kind == "independent_evaluation")).all()) == 1
        assert len(session.scalars(select(Verification).where(
            Verification.run_id == setup.rediagnosis_id)).all()) == (
            1 if expected == "passed" else 0)


def _seed_independent_evaluation_concurrency(factory, tmp_path, *, key):
    """Seed a committed Coder->Verifier draft without real models or Docker."""
    worker, task_id, run_id = create_running_run(
        factory, worker_id=f"independent-concurrency-{key}")
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, key)
    draft_id = uuid4()
    seeded = _seed_committed_coder_route(
        factory, rediagnosis_id, post_status=AgentExecutionStatus.SUCCEEDED,
        post_outcome="VERIFY", post_target=AgentRole.VERIFIER,
        verifier_status=AgentExecutionStatus.RUNNING,
        verifier_output={"outcome": "VERIFICATION_BLOCKED",
                         "draft_artifact_id": str(draft_id),
                         "missing_evidence": ["test_integrity"],
                         "preliminary_public_outcome": "passed"})
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    (workspaces / str(rediagnosis_id)).mkdir(parents=True)
    (artifacts / str(rediagnosis_id)).mkdir(parents=True)
    baseline = tmp_path / "baseline-calibration"
    corrected = tmp_path / "corrected-calibration"
    baseline.mkdir()
    corrected.mkdir()
    material = IndependentEvaluationMaterial(
        image="postgres:17.11", command=("perl", "-e", "print 1"),
        cases=(PrivateCase(b"", b"1"),),
        calibration_workspaces=(baseline, corrected, "a" * 40),
        timeout_seconds=20)
    config = _matching_verification_config(task_id).model_copy(
        update={"independent_suite_version": "rediagnosis-concurrency-v1"})
    args = dict(
        creation_idempotency_key=key, workspaces_root=workspaces,
        artifacts_root=artifacts, supervisor_gateway=None, explorer_gateway=None,
        planner_gateway=None, max_read_bytes=4096, stop_event=Event(),
        verification_config=config,
        coder_gateway=SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Coder must not rerun")),
        max_patch_bytes=4096,
        verifier_gateway=SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Model must not run")),
        verifier_max_diff_bytes=65536, independent_evaluation=material)
    return SimpleNamespace(
        worker=worker, task_id=task_id, factory=factory,
        rediagnosis_id=rediagnosis_id, draft_id=draft_id,
        verifier_id=seeded.verifier_id, args=args)


def test_concurrent_independent_evaluation_failure_is_committed_once(
    recovery_session_factory, tmp_path, monkeypatch,
):
    setup = _seed_independent_evaluation_concurrency(
        recovery_session_factory, tmp_path, key="independent-concurrent-failure")
    receipts = {}
    evaluations = []
    barrier = Barrier(2, timeout=2)

    def fake_load(self, run_id, *, verifier_execution_id, draft_artifact_id, artifact_root):
        return receipts.get((run_id, verifier_execution_id, draft_artifact_id))

    def fake_finalize(self, run_id, *, agent_execution_id, draft_artifact_id, **kwargs):
        evaluations.append((agent_execution_id, draft_artifact_id))
        # The second caller can only join this barrier if the recovery dispatch
        # lock was released before evaluation, which is the gap under test.
        try:
            barrier.wait()
        except BrokenBarrierError:
            pass
        artifact_id = uuid4()
        receipts[(run_id, agent_execution_id, draft_artifact_id)] = (artifact_id, "failed")
        return {"outcome": "failed", "independent_artifact_id": artifact_id,
                "verification_id": None}

    monkeypatch.setattr(Worker, "_load_rediagnosis_independent_receipt", fake_load)
    monkeypatch.setattr(Worker, "finalize_with_independent_evaluation", fake_finalize)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(
            setup.worker.resume_rediagnosis_recovery, setup.task_id, **setup.args) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]

    assert len(evaluations) == 1
    assert {result["outcome"] for result in results} == {"independent_evaluation_failed"}
    assert len(receipts) == 1
    with setup.factory() as session:
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING
        assert session.get(AgentExecution, setup.verifier_id).status == AgentExecutionStatus.RUNNING


def test_concurrent_independent_evaluation_pass_leaves_one_committed_result(
    recovery_session_factory, tmp_path, monkeypatch,
):
    setup = _seed_independent_evaluation_concurrency(
        recovery_session_factory, tmp_path, key="independent-concurrent-pass")
    receipts = {}
    evaluations = []
    barrier = Barrier(2, timeout=2)

    def fake_load(self, run_id, *, verifier_execution_id, draft_artifact_id, artifact_root):
        return receipts.get((run_id, verifier_execution_id, draft_artifact_id))

    def fake_finalize(self, run_id, *, agent_execution_id, draft_artifact_id, **kwargs):
        evaluations.append((agent_execution_id, draft_artifact_id))
        try:
            barrier.wait()
        except BrokenBarrierError:
            pass
        artifact_id, verification_id = uuid4(), uuid4()
        receipts[(run_id, agent_execution_id, draft_artifact_id)] = (artifact_id, "passed")
        with self.session_factory() as session, session.begin():
            run = session.get(Run, run_id)
            run.status = RunStatus.PASSED
            run.finished_at = datetime.now(timezone.utc)
            session.get(Task, run.task_id).status = TaskStatus.AWAITING_REVIEW
        return {"outcome": "passed", "independent_artifact_id": artifact_id,
                "verification_id": verification_id}

    monkeypatch.setattr(Worker, "_load_rediagnosis_independent_receipt", fake_load)
    monkeypatch.setattr(Worker, "finalize_with_independent_evaluation", fake_finalize)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(
            setup.worker.resume_rediagnosis_recovery, setup.task_id, **setup.args) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]

    assert len(evaluations) == 1
    assert {result["outcome"] for result in results} == {"passed", "existing_rediagnosis"}
    assert len(receipts) == 1
    with setup.factory() as session:
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.PASSED
        assert session.get(Task, setup.task_id).status == TaskStatus.AWAITING_REVIEW


@pytest.mark.parametrize(("public_outcome", "suite_version", "reason"), [
    ("failed", "rediagnosis-synthetic-v1", None),
    ("passed", None, "independent_evaluation_config_missing"),
])
def test_independent_evaluation_is_blocked_before_execution(
    recovery_session_factory, tmp_path, monkeypatch, public_outcome, suite_version, reason,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "blocked-evaluation")
    _seed_committed_coder_route(
        factory, rediagnosis_id, post_status=AgentExecutionStatus.SUCCEEDED,
        post_outcome="VERIFY", post_target=AgentRole.VERIFIER,
        verifier_status=AgentExecutionStatus.RUNNING,
        verifier_output={"outcome": "VERIFICATION_BLOCKED", "draft_artifact_id": str(uuid4()),
                         "missing_evidence": ["test_integrity"],
                         "preliminary_public_outcome": public_outcome})
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    (workspaces / str(rediagnosis_id)).mkdir(parents=True)
    (artifacts / str(rediagnosis_id)).mkdir(parents=True)
    material = IndependentEvaluationMaterial(
        image="postgres:17.11", command=("perl", "-e", "print 1"),
        cases=(PrivateCase(b"", b"1"),),
        calibration_workspaces=(tmp_path / "baseline", tmp_path / "corrected", "a" * 40))
    config = _matching_verification_config(task_id)
    if suite_version is not None:
        config = config.model_copy(update={"independent_suite_version": suite_version})
    monkeypatch.setattr(worker, "finalize_with_independent_evaluation",
                        lambda *args, **kwargs: pytest.fail("Independent evaluation must not run"))

    result = worker.resume_rediagnosis_recovery(
        task_id, creation_idempotency_key="blocked-evaluation", workspaces_root=workspaces,
        artifacts_root=artifacts, supervisor_gateway=None, explorer_gateway=None,
        planner_gateway=None, max_read_bytes=4096, stop_event=Event(),
        verification_config=config,
        coder_gateway=SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("Coder must not run")),
        max_patch_bytes=4096,
        verifier_gateway=SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("Model must not run")),
        verifier_max_diff_bytes=65536, independent_evaluation=material)

    assert result["outcome"] == "verifier_blocked"
    if reason is None:
        assert result["preliminary_public_outcome"] == "failed"
    else:
        assert result["reason"] == reason
    with factory() as session:
        assert not session.scalars(select(Artifact).where(
            Artifact.agent_execution_id.in_(select(AgentExecution.id).where(
                AgentExecution.run_id == rediagnosis_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())),
            Artifact.kind == "independent_evaluation")).all()


def _matching_verification_config(task_id, *, base_commit="a" * 40):
    return VerificationConfig(
        task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "test_baseline.py"),
        target_tests=("test_baseline::test_baseline",), external_connections="none",
    )


def _seed_committed_coder_route(
    factory, run_id, *, coder_status=AgentExecutionStatus.SUCCEEDED,
    coder_outcome="PATCH_READY", post_status=None, post_outcome=None, post_target=None,
    verifier_status=None, verifier_output=None, post_explorer_reference=True,
    replan_planner_status=None, replan_planner_output=None,
    replan_explorer_reference=True, replan_supervisor_status=None,
    replan_supervisor_outcome=None, replan_supervisor_target=None,
    replan_supervisor_evidence_refs=None,
):
    """Seed committed rows for the post-Coder branch without a real Coder model call."""
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        explorer = AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=7),
            input={}, output={"read_evidence": []},
        )
        session.add(explorer)
        session.flush()
        explorer_id = explorer.id
        planner = AgentExecution(
            run_id=run_id, agent_role=AgentRole.PLANNER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=6),
            input={"explorer_execution_id": str(explorer.id)},
            output={"outcome": "PLAN_READY", "plan": {
                "approach": "Update the documentation", "target_paths": ["README.md"],
                "steps": ["Edit the relevant section"], "scope_limits": "Only README.md",
                "verification": ["Review the diff"],
            }},
        )
        session.add(planner)
        session.flush()
        planner_id = planner.id
        baseline_id = uuid4()
        code_handoff = Handoff(
            from_agent_execution_id=planner.id, to_agent_role=AgentRole.CODER.value.lower(),
            context={"completed_work": "Plan prepared", "blocker": None,
                     "evidence_refs": [{"baseline_verifier_id": str(baseline_id)}],
                     "decision_required": "Implement", "scope_impact": "Only README.md"},
        )
        session.add(code_handoff)
        session.flush()
        code_handoff_id = code_handoff.id
        code_supervisor = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=5),
            input={}, output={"outcome": "CODE", "approved_handoff_id": str(code_handoff.id)},
        )
        session.add(code_supervisor)
        session.flush()
        supervisor_id = code_supervisor.id
        coder_output = ({"outcome": "PATCH_READY", "patch_artifact_id": str(uuid4()),
                         "summary": "Update README"}
                        if coder_outcome == "PATCH_READY" else
                        {"outcome": "BLOCKED", "completed_work": "Inspected the plan",
                         "blocker": "No safe change", "evidence_refs": [{"planner_execution_id": str(planner_id)}],
                         "decision_required": "Replan", "scope_impact": "Only README.md"})
        coder = AgentExecution(
            run_id=run_id, agent_role=AgentRole.CODER.value.lower(), status=coder_status,
            handoff_id=code_handoff.id, created_at=now - timedelta(seconds=4),
            input={}, output=coder_output,
        )
        session.add(coder)
        session.flush()
        coder_id = coder.id
        post_handoff_id = None
        if post_status is not None:
            post = AgentExecution(
                run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                status=post_status, created_at=now - timedelta(seconds=3),
                input={"coder_execution_id": str(coder.id)}, output={"outcome": post_outcome},
            )
            session.add(post)
            session.flush()
            if post_target is not None:
                evidence_refs = [{"baseline_verifier_id": str(baseline_id)}]
                if post_explorer_reference:
                    evidence_refs.insert(0, {"explorer_execution_id": str(explorer.id)})
                post_handoff = Handoff(
                    from_agent_execution_id=coder.id,
                    to_agent_role=post_target.value.lower(),
                    context={"completed_work": "Coder submitted a committed result.",
                             "blocker": ("No safe change" if post_outcome == "REPLAN"
                                         else None),
                             "evidence_refs": evidence_refs,
                             "decision_required": "Continue",
                             "scope_impact": "Continue within the Issue and Task policy."},
                )
                session.add(post_handoff)
                session.flush()
                post.output = {"outcome": post_outcome,
                               "approved_handoff_id": str(post_handoff.id)}
                post_handoff_id = post_handoff.id
        verifier_id = None
        if verifier_status is not None and post_handoff_id is not None:
            verifier = AgentExecution(
                run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
                status=verifier_status, handoff_id=post_handoff_id,
                created_at=now - timedelta(seconds=2), input={}, output=verifier_output,
            )
            session.add(verifier)
            session.flush()
            verifier_id = verifier.id
        replan_planner_id = None
        replan_supervisor_id = None
        replan_handoff_id = None
        if replan_planner_status is not None and post_handoff_id is not None:
            replanner = AgentExecution(
                run_id=run_id, agent_role=AgentRole.PLANNER.value.lower(),
                status=replan_planner_status, handoff_id=post_handoff_id,
                created_at=now - timedelta(seconds=1),
                input=({"explorer_execution_id": str(explorer.id)}
                       if replan_explorer_reference else {}),
                output=replan_planner_output or {
                    "outcome": "PLAN_READY", "plan": {
                        "approach": "Take the narrower documented change",
                        "target_paths": ["README.md"],
                        "steps": ["Edit the documented line"],
                        "scope_limits": "Only README.md",
                        "verification": ["Review the diff"],
                    }},
            )
            session.add(replanner)
            session.flush()
            replan_planner_id = replanner.id
            if replan_supervisor_status is not None:
                supervisor = AgentExecution(
                    run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                    status=replan_supervisor_status, created_at=now,
                    input={"planner_execution_id": str(replanner.id)},
                    output=({"outcome": replan_supervisor_outcome}
                            if replan_supervisor_outcome is not None else None),
                )
                session.add(supervisor)
                session.flush()
                if replan_supervisor_target is not None:
                    route_handoff = Handoff(
                        from_agent_execution_id=replanner.id,
                        to_agent_role=replan_supervisor_target.value.lower(),
                        context={"completed_work": "The re-plan was routed",
                                 "blocker": None,
                                 "evidence_refs": list(replan_supervisor_evidence_refs or ()),
                                 "decision_required": "Continue",
                                 "scope_impact": "Continue within policy"},
                    )
                    session.add(route_handoff)
                    session.flush()
                    replan_handoff_id = route_handoff.id
                    supervisor.output = {"outcome": replan_supervisor_outcome,
                                         "approved_handoff_id": str(route_handoff.id)}
                replan_supervisor_id = supervisor.id
    return SimpleNamespace(coder_id=coder_id, planner_id=planner_id,
                           explorer_id=explorer_id, supervisor_id=supervisor_id,
                           code_handoff_id=code_handoff_id,
                           baseline_id=baseline_id, post_handoff_id=post_handoff_id,
                           verifier_id=verifier_id, replan_planner_id=replan_planner_id,
                           replan_supervisor_id=replan_supervisor_id,
                           replan_handoff_id=replan_handoff_id)


def _drive_after_committed_coder(
    worker, run_id, tmp_path, *, verification_config=None, verifier_gateway=None,
    verifier_max_diff_bytes=65536, supervisor_gateway=None, planner_gateway=None,
    coder_gateway=None, directories=None, max_patch_bytes=4096,
):
    return worker._drive_rediagnosis_after_exploration(
        run_id, directories=directories or RunDirectories(tmp_path / "w", tmp_path / "a"),
        supervisor_gateway=supervisor_gateway, explorer_gateway=None,
        planner_gateway=planner_gateway,
        max_read_bytes=1024, stop_event=Event(), verification_config=verification_config,
        coder_gateway=(coder_gateway or SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Coder must not rerun"))),
        max_patch_bytes=max_patch_bytes, verifier_gateway=verifier_gateway,
        verifier_max_diff_bytes=verifier_max_diff_bytes,
    )



def test_run_row_lock_blocks_concurrent_execution_right(recovery_session_factory):
    import sqlalchemy.exc
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    session1 = factory()
    session2 = factory()
    try:
        session1.begin()
        coordinator1 = RunCoordinator(session1)
        coordinator1.assert_execution_right(run_id, worker.worker_id)
        coordinator1._start_agent_execution_in_transaction(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR,
            agent_input={"observed_baseline_verifier_id": None})

        session2.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(sqlalchemy.exc.DBAPIError) as excinfo:
            RunCoordinator(session2).assert_execution_right(run_id, worker.worker_id)
        assert excinfo.value.orig.sqlstate == "55P03"
        session2.rollback()

        session1.commit()
    finally:
        session1.close()
        session2.close()

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == run_id)) == 1
        RunCoordinator(session).assert_execution_right(run_id, worker.worker_id)


def test_transaction_owned_agent_start_rolls_back_with_caller(recovery_session_factory):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    with factory() as session:
        coordinator = RunCoordinator(session)
        with pytest.raises(RuntimeError, match="caller-owned transaction"):
            coordinator._start_agent_execution_in_transaction(
                run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        with pytest.raises(RuntimeError, match="rollback test"):
            with session.begin():
                coordinator.assert_execution_right(run_id, worker.worker_id)
                coordinator._start_agent_execution_in_transaction(
                    run_id, worker.worker_id, role=AgentRole.SUPERVISOR,
                    agent_input={"observed_baseline_verifier_id": None})
                raise RuntimeError("rollback test")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == run_id)) == 0


def test_replan_supervisor_phase_rejects_equal_baseline_timestamp(recovery_session_factory):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    seeded = _seed_committed_coder_route(
        factory, run_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    stamp = datetime.now(timezone.utc)
    with factory.begin() as session:
        handoff = Handoff(from_agent_execution_id=seeded.replan_planner_id,
                          to_agent_role=AgentRole.VERIFIER.value.lower(), context={})
        session.add(handoff)
        session.flush()
        baseline_handoff_id = handoff.id
        session.add_all([
            AgentExecution(run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
                           status=AgentExecutionStatus.SUCCEEDED, handoff_id=handoff.id,
                           created_at=stamp, input={}, output={"outcome": "BASELINE_RECORDED"}),
            AgentExecution(run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                           status=AgentExecutionStatus.SUCCEEDED, created_at=stamp - timedelta(seconds=1),
                           input={"planner_execution_id": str(seeded.replan_planner_id)}, output={"outcome": "BASELINE"}),
            AgentExecution(run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                           status=AgentExecutionStatus.SUCCEEDED, created_at=stamp + timedelta(seconds=1),
                           input={"planner_execution_id": str(seeded.replan_planner_id)}, output={"outcome": "CODE"}),
        ])
    pre = worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase="pre_baseline")
    post = worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase="post_baseline")
    assert pre.output["outcome"] == "BASELINE" and post.output["outcome"] == "CODE"
    with factory.begin() as session:
        session.get(AgentExecution, post.id).created_at = stamp
    for phase in ("pre_baseline", "post_baseline"):
        with pytest.raises(PlannerSupervisorContextError, match="ordering is ambiguous"):
            worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase=phase)
    with factory.begin() as session:
        baseline = session.scalar(select(AgentExecution).where(AgentExecution.handoff_id == baseline_handoff_id))
        session.get(AgentExecution, pre.id).input = {
            "planner_execution_id": str(seeded.replan_planner_id),
            "baseline_available": False, "observed_baseline_verifier_id": None}
        session.get(AgentExecution, post.id).input = {
            "planner_execution_id": str(seeded.replan_planner_id),
            "baseline_available": True, "observed_baseline_verifier_id": str(baseline.id)}
    assert worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase="pre_baseline").id == pre.id
    assert worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase="post_baseline").id == post.id
    with factory.begin() as session:
        session.get(AgentExecution, post.id).input = {
            "planner_execution_id": str(seeded.replan_planner_id),
            "baseline_available": True, "observed_baseline_verifier_id": str(uuid4())}
    with pytest.raises(PlannerSupervisorContextError, match="observation is invalid"):
        worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase="post_baseline")



def test_post_planner_supervisor_rejects_multiple_baselines_before_start(
    recovery_session_factory, monkeypatch,
):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    seeded = _seed_committed_coder_route(
        factory, run_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    with factory.begin() as session:
        for _ in range(2):
            handoff = Handoff(from_agent_execution_id=seeded.replan_planner_id,
                              to_agent_role=AgentRole.VERIFIER.value.lower(), context={})
            session.add(handoff)
            session.flush()
            session.add(AgentExecution(run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
                                       status=AgentExecutionStatus.SUCCEEDED, handoff_id=handoff.id,
                                       input={}, output={"outcome": "BASELINE_RECORDED"}))
        before = session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "supervisor"))
    monkeypatch.setattr(ContextBuilder, "build_supervisor_exploration", lambda *args, **kwargs: {})
    with pytest.raises(PlannerSupervisorContextError, match="Multiple recorded baselines"):
        worker.execute_supervisor_after_planner(
            run_id, planner_execution_id=seeded.replan_planner_id,
            model_gateway=SimpleNamespace(request=lambda *a, **k: pytest.fail("model must not run")),
            stop_event=Event())
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "supervisor")) == before


def test_replan_supervisor_rejects_multiple_recorded_baselines(recovery_session_factory):
    factory = recovery_session_factory
    worker, _, run_id = create_running_run(factory)
    seeded = _seed_committed_coder_route(
        factory, run_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    stamp = datetime.now(timezone.utc)
    with factory.begin() as session:
        handoffs = []
        for _ in range(2):
            handoff = Handoff(from_agent_execution_id=seeded.replan_planner_id,
                              to_agent_role=AgentRole.VERIFIER.value.lower(), context={})
            session.add(handoff)
            session.flush()
            handoffs.append(handoff.id)
        for idx, handoff_id in enumerate(handoffs):
            session.add(AgentExecution(
                run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
                status=AgentExecutionStatus.SUCCEEDED, handoff_id=handoff_id,
                created_at=stamp + timedelta(seconds=idx), input={},
                output={"outcome": "BASELINE_RECORDED"}))
        for offset in (-2, 2):
            session.add(AgentExecution(
                run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                status=AgentExecutionStatus.SUCCEEDED, created_at=stamp + timedelta(seconds=offset),
                input={"planner_execution_id": str(seeded.replan_planner_id)},
                output={"outcome": "BASELINE" if offset < 0 else "CODE"}))
    for phase in ("pre_baseline", "post_baseline"):
        with pytest.raises(PlannerSupervisorContextError, match="Multiple recorded baselines"):
            worker._load_replan_supervisor(run_id, seeded.replan_planner_id, phase=phase)


def _seed_replan_code_with_trusted_baseline(
    factory, tmp_path, monkeypatch, *, key, worker_id="replan-code-worker",
):
    """Seed the post-Coder REPLAN route and record a real re-plan baseline."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, repo=str(repo), base_commit=base_commit, worker_id=worker_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    stop = Event()
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, key)
    directories = worker._ensure_recovery_workspace(
        rediagnosis_id, workspaces_root=workspaces, artifacts_root=artifacts, stop_event=stop)
    seeded = _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    now = datetime.now(timezone.utc)
    config = VerificationConfig(
        task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "test_baseline.py"),
        target_tests=("test_baseline::test_baseline",), external_connections="none")
    with factory.begin() as session:
        baseline_handoff = Handoff(
            from_agent_execution_id=seeded.replan_planner_id,
            to_agent_role=AgentRole.VERIFIER.value.lower(),
            context={"completed_work": "A baseline was requested for the re-plan",
                     "blocker": None, "evidence_refs": [],
                     "decision_required": "Run the trusted registered baseline",
                     "scope_impact": "Only README.md"},
        )
        session.add(baseline_handoff)
        session.flush()
        session.add(AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=2),
            # Exactly what a real pre-baseline Supervisor records: no baseline observed.
            input={"planner_execution_id": str(seeded.replan_planner_id),
                   "baseline_available": False, "observed_baseline_verifier_id": None},
            output={"outcome": "BASELINE", "approved_handoff_id": str(baseline_handoff.id)},
        ))
        baseline_handoff_id = baseline_handoff.id
    receipt = worker.execute_baseline_verifier(
        rediagnosis_id, handoff_id=baseline_handoff_id, directories=directories,
        command_name="pytest", registry={config.command_name: config.command},
        stop_event=stop, pythonpath_relative=config.pythonpath_relative,
        verify_baseline=True,
    )
    assert receipt["valid"] is True
    with factory() as session:
        baseline = session.scalar(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.handoff_id == baseline_handoff_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
        ))
        assert baseline is not None
        assert baseline.output["outcome"] == "BASELINE_RECORDED"
        baseline_id = baseline.id
    with factory.begin() as session:
        code_handoff = Handoff(
            from_agent_execution_id=seeded.replan_planner_id,
            to_agent_role=AgentRole.CODER.value.lower(),
            context={"completed_work": "The re-plan is ready for implementation",
                     "blocker": None,
                     "evidence_refs": [{"baseline_verifier_id": str(baseline_id)}],
                     "decision_required": "Implement the narrower plan",
                     "scope_impact": "Only README.md"},
        )
        session.add(code_handoff)
        session.flush()
        code_supervisor = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now,
            # A real post-baseline Supervisor records the baseline it observed. Without this
            # marker the row is a legacy shape whose phase is inferred from creation time —
            # and here that timestamp precedes the baseline it followed.
            input={"planner_execution_id": str(seeded.replan_planner_id),
                   "baseline_available": True,
                   "observed_baseline_verifier_id": str(baseline_id)},
            output={"outcome": "CODE", "approved_handoff_id": str(code_handoff.id)},
        )
        session.add(code_supervisor)
        session.flush()
        code_handoff_id, code_supervisor_id = code_handoff.id, code_supervisor.id
    return SimpleNamespace(
        worker=worker, task_id=task_id, rediagnosis_id=rediagnosis_id,
        directories=directories, seeded=seeded, config=config,
        baseline_id=baseline_id, baseline_handoff_id=baseline_handoff_id,
        code_handoff_id=code_handoff_id, code_supervisor_id=code_supervisor_id,
        stop=stop, repo=repo, base_commit=base_commit,
        workspaces=workspaces, artifacts=artifacts,
    )


def _seed_replan_baseline_route(
    factory, tmp_path, monkeypatch, *, key, worker_id="replan-baseline-worker",
):
    """Seed the post-Coder REPLAN route whose committed decision is BASELINE.

    Unlike the CODE fixture, no baseline is recorded: the re-plan baseline is what
    the recovery path under test has to establish on a clean workspace itself.
    """
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    worker, task_id, run_id = create_running_run(
        factory, repo=str(repo), base_commit=base_commit, worker_id=worker_id)
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    stop = Event()
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, key)
    directories = worker._ensure_recovery_workspace(
        rediagnosis_id, workspaces_root=workspaces, artifacts_root=artifacts, stop_event=stop)
    seeded = _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_outcome="BASELINE",
        replan_supervisor_target=AgentRole.VERIFIER)
    config = VerificationConfig(
        task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "test_baseline.py"),
        target_tests=("test_baseline::test_baseline",), external_connections="none")
    return SimpleNamespace(
        worker=worker, task_id=task_id, rediagnosis_id=rediagnosis_id,
        directories=directories, seeded=seeded, config=config, stop=stop, repo=repo,
        base_commit=base_commit, workspaces=workspaces, artifacts=artifacts,
    )


def _seed_post_baseline_supervisor(
    factory, setup, *, outcome, target, status=AgentExecutionStatus.SUCCEEDED,
):
    """Seed a committed re-plan baseline and one route created after it."""
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        session.add(AgentExecution(
            run_id=setup.rediagnosis_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, handoff_id=setup.seeded.replan_handoff_id,
            created_at=now, input={"command_name": "pytest"},
            output={"outcome": "BASELINE_RECORDED"},
        ))
        supervisor = AgentExecution(
            run_id=setup.rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=status, created_at=now + timedelta(hours=1),
            input={"planner_execution_id": str(setup.seeded.replan_planner_id)},
            output={"outcome": outcome} if outcome is not None else {},
        )
        session.add(supervisor)
        session.flush()
        handoff_id = None
        if target is not None:
            handoff = Handoff(
                from_agent_execution_id=setup.seeded.replan_planner_id,
                to_agent_role=target.value.lower(),
                context={"completed_work": "The re-plan baseline is recorded",
                         "blocker": None,
                         "evidence_refs": [],
                         "decision_required": "Continue the re-plan",
                         "scope_impact": "Only README.md"},
            )
            session.add(handoff)
            session.flush()
            handoff_id = handoff.id
            supervisor.output = {"outcome": outcome, "approved_handoff_id": str(handoff.id)}
        supervisor_id = supervisor.id
    return SimpleNamespace(supervisor_id=supervisor_id, handoff_id=handoff_id)


def _call_replanned_post_baseline_route(
    setup, *, supervisor_gateway=None, coder_gateway=None, max_patch_bytes=4096,
):
    """Call the post-baseline route directly, without a real baseline execution."""
    return setup.worker._reroute_replanned_after_baseline(
        setup.rediagnosis_id,
        route={"baseline_verifier_id": None,
               "baseline_handoff_id": setup.seeded.replan_handoff_id,
               "replan_supervisor_execution_id": setup.seeded.replan_supervisor_id,
               "replan_handoff_id": setup.seeded.replan_handoff_id},
        planner_execution_id=setup.seeded.replan_planner_id,
        planner_outcome="PLAN_READY",
        post_coder_handoff_id=setup.seeded.post_handoff_id,
        common={}, directories=setup.directories,
        supervisor_gateway=supervisor_gateway, explorer_gateway=None,
        planner_gateway=None, coder_gateway=coder_gateway,
        max_patch_bytes=max_patch_bytes, max_read_bytes=1024,
        stop_event=setup.stop,
    )


def _seed_post_coder_supervisor(factory, run_id, coder_id, *, outcome, target, evidence_refs=()):
    """Seed one committed post-Coder Supervisor route for an existing Coder."""
    with factory.begin() as session:
        handoff = Handoff(
            from_agent_execution_id=coder_id, to_agent_role=target.value.lower(),
            context={"completed_work": "Coder submitted a committed result.",
                     "blocker": None, "evidence_refs": list(evidence_refs),
                     "decision_required": "Continue",
                     "scope_impact": "Continue within the Issue and Task policy."},
        )
        session.add(handoff)
        session.flush()
        supervisor = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED,
            input={"coder_execution_id": str(coder_id)},
            output={"outcome": outcome, "approved_handoff_id": str(handoff.id)},
        )
        session.add(supervisor)
        session.flush()
        supervisor_id, handoff_id = supervisor.id, handoff.id
    return SimpleNamespace(supervisor_id=supervisor_id, handoff_id=handoff_id)


def _call_after_replanned_coder(
    setup, *, supervisor_gateway=None, verification_config=None, verifier_gateway=None,
    dispatch_replan=True, planner_gateway=None,
):
    """Call the post-Coder continuation for the re-plan Coder directly."""
    with setup.worker.session_factory() as session:
        coder = session.get(AgentExecution, setup.seeded.coder_id)
    return setup.worker._continue_rediagnosis_after_coder(
        setup.rediagnosis_id, coder=coder, common={}, directories=setup.directories,
        supervisor_gateway=supervisor_gateway, explorer_gateway=None,
        planner_gateway=planner_gateway,
        verification_config=verification_config, verifier_gateway=verifier_gateway,
        verifier_max_diff_bytes=65536, max_read_bytes=1024, max_patch_bytes=4096,
        stop_event=setup.stop, resumed=False, dispatch_replan=dispatch_replan,
    )


def _coder_gateway(factory, worker_id, outcome, *, prompts=None, patch=None):
    def generate(prompt, _limit):
        if prompts is not None:
            prompts.append(prompt)
        if outcome == "PATCH_READY":
            reply = {"outcome": "PATCH_DRAFT", "patch": patch, "summary": "Update README"}
        else:
            reply = {"outcome": "BLOCKED", "completed_work": "Reviewed the re-plan",
                     "blocker": "The narrower plan still needs a decision",
                     "evidence_refs": [{"path": "README.md"}],
                     "decision_required": "Replan within the documented scope",
                     "scope_impact": "Only README.md"}
        return ModelReply(reply, ModelTokenUsage(input_tokens=40, output_tokens=20))
    return ModelGateway(
        session_factory=factory, worker_id=worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=generate,
    )


def test_post_coder_verify_requires_matching_trusted_verifier_inputs(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "coder-config")
    _seed_committed_coder_route(
        factory, rediagnosis_id, post_status=AgentExecutionStatus.SUCCEEDED,
        post_outcome="VERIFY", post_target=AgentRole.VERIFIER)

    missing = _drive_after_committed_coder(worker, rediagnosis_id, tmp_path)
    assert missing["outcome"] == "verification_config_missing"

    config = _matching_verification_config(task_id)
    no_gateway = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path, verification_config=config)
    assert no_gateway["outcome"] == "verifier_config_missing"

    invalid = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path,
        verification_config=_matching_verification_config(uuid4()),
        verifier_gateway=SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("Model must not run")),
    )
    assert invalid["outcome"] == "verification_config_invalid"

    with factory() as session:
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()


def test_post_coder_blocked_stops_before_verification(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "coder-blocked")
    _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="BLOCKED",
        post_target=None)

    result = _drive_after_committed_coder(worker, rediagnosis_id, tmp_path)

    assert result["outcome"] == "blocked"
    with factory() as session:
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()


def test_post_coder_replan_requires_configuration_and_valid_evidence(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    missing_config_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-config")
    seeded = _seed_committed_coder_route(
        factory, missing_config_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER)

    missing = _drive_after_committed_coder(worker, missing_config_run, tmp_path)
    assert missing["outcome"] == "replan"
    assert missing["reason"] == "planner_config_missing"
    assert missing["post_coder_handoff_id"] == seeded.post_handoff_id

    worker, task_id, run_id = create_running_run(factory, worker_id="invalid-evidence-worker")
    invalid_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-evidence")
    invalid = _seed_committed_coder_route(
        factory, invalid_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER, post_explorer_reference=False)
    planner_gateway = SimpleNamespace(
        initial=lambda *args, **kwargs: pytest.fail("Planner model must not run"),
        repair=None)

    rejected = _drive_after_committed_coder(
        worker, invalid_run, tmp_path, planner_gateway=planner_gateway)

    assert rejected["outcome"] == "replan_unavailable"
    assert rejected["reason"] == "planner_evidence_invalid"
    assert rejected["post_coder_handoff_id"] == invalid.post_handoff_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id.in_((missing_config_run, invalid_run)),
            AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
            AgentExecution.handoff_id.in_((seeded.post_handoff_id, invalid.post_handoff_id)),
        )) == 0


def test_post_coder_replan_reuses_committed_running_and_failed_planner(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    committed_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-committed")
    committed = _seed_committed_coder_route(
        factory, committed_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    failing_gateway = SimpleNamespace(
        initial=lambda *args, **kwargs: pytest.fail("Committed Planner must not rerun"),
        repair=None)

    first = _drive_after_committed_coder(
        worker, committed_run, tmp_path, planner_gateway=failing_gateway)
    replay = _drive_after_committed_coder(
        worker, committed_run, tmp_path, planner_gateway=failing_gateway)

    assert first["outcome"] == "replanned" and first["resumed"] is True
    assert replay["outcome"] == "replanned" and replay["resumed"] is True
    assert first["planner_execution_id"] == committed.replan_planner_id
    assert replay["planner_execution_id"] == committed.replan_planner_id
    assert first["planner_outcome"] == "PLAN_READY"

    worker, task_id, run_id = create_running_run(factory, worker_id="planner-status-worker")
    running_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-running")
    running = _seed_committed_coder_route(
        factory, running_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.RUNNING)
    waiting = _drive_after_committed_coder(
        worker, running_run, tmp_path, planner_gateway=failing_gateway)
    assert waiting["outcome"] == "existing"
    assert waiting["reason"] == "planner_not_completed"
    assert waiting["planner_execution_id"] == running.replan_planner_id

    worker, task_id, run_id = create_running_run(factory, worker_id="planner-failed-worker")
    failed_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-failed")
    failed = _seed_committed_coder_route(
        factory, failed_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.FAILED)
    unavailable = _drive_after_committed_coder(
        worker, failed_run, tmp_path, planner_gateway=failing_gateway)
    assert unavailable["outcome"] == "unresumable"
    assert unavailable["reason"] == "planner_execution_failed"
    assert unavailable["planner_execution_id"] == failed.replan_planner_id
    with factory() as session:
        for run_id in (committed_run, running_run, failed_run):
            assert len(session.scalars(select(AgentExecution).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
                AgentExecution.handoff_id.is_not(None),
            )).all()) == 1
            assert not session.scalars(select(AgentExecution).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()


def test_post_coder_replan_planner_output_failure_stops_safely(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-output-failure")
    _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER)
    planner_gateway = SimpleNamespace(
        initial=lambda *args, **kwargs: {"outcome": "NOT_A_PLAN"}, repair=None)

    result = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path, planner_gateway=planner_gateway)

    assert result["outcome"] == "unresumable"
    assert result["reason"] == "planner_execution_failed"
    assert result["resumed"] is True
    with factory() as session:
        planner = session.get(AgentExecution, result["planner_execution_id"])
        assert planner.status == AgentExecutionStatus.FAILED
        assert planner.output == {"reason": "PLANNER_OUTPUT_INVALID"}
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()


def test_post_coder_replan_routes_committed_planner_through_one_supervisor(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-route")
    seeded = _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    prompts = []
    def route(prompt, _limit):
        prompts.append(prompt)
        return ModelReply({"outcome": "RE_EXPLORE", "reason": "Refresh the evidence",
                           "next_goal": "Re-read the narrower plan's evidence"},
                          ModelTokenUsage(input_tokens=40, output_tokens=20))
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=route)

    result = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path, supervisor_gateway=gateway)

    assert result["outcome"] == "replanned" and result["reason"] == "coder_replan"
    assert result["planner_execution_id"] == seeded.replan_planner_id
    assert result["replan_handoff_target"] == AgentRole.EXPLORER.value.lower()
    assert len(prompts) == 1
    with factory() as session:
        supervisor = session.get(AgentExecution, result["replan_supervisor_execution_id"])
        assert supervisor.input["planner_execution_id"] == str(seeded.replan_planner_id)
        assert supervisor.status == AgentExecutionStatus.SUCCEEDED
        assert supervisor.output["outcome"] == "RE_EXPLORE"
        route_handoff = session.get(Handoff, result["replan_handoff_id"])
        assert route_handoff.from_agent_execution_id == seeded.replan_planner_id
        assert route_handoff.to_agent_role == AgentRole.EXPLORER.value.lower()
        # The route is saved, not dispatched: no second Explorer, Coder or Verifier.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 3
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.EXPLORER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()

    replay = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path,
        supervisor_gateway=SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Committed Supervisor must not rerun")))

    assert replay["outcome"] == "replanned" and replay["resumed"] is True
    assert replay["replan_supervisor_execution_id"] == result["replan_supervisor_execution_id"]
    assert replay["replan_handoff_id"] == result["replan_handoff_id"]
    assert len(prompts) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 3


def test_post_coder_replan_needs_evidence_is_routed_once_and_stops(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-needs-evidence")
    seeded = _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_planner_output={
            "outcome": "NEEDS_EVIDENCE",
            "evidence_gap": "The narrower plan needs a current read preview",
            "affected_decision": "Whether the README change stays in scope",
        })
    prompts = []
    def route(prompt, _limit):
        prompts.append(prompt)
        return ModelReply({"outcome": "RE_EXPLORE", "reason": "The plan needs current evidence",
                           "next_goal": "Re-read the README before planning again"},
                          ModelTokenUsage(input_tokens=40, output_tokens=20))
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=route)

    result = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path, supervisor_gateway=gateway)

    assert result["outcome"] == "replanned"
    assert result["planner_outcome"] == "NEEDS_EVIDENCE"
    assert result["replan_supervisor_outcome"] == "RE_EXPLORE"
    assert result["replan_handoff_target"] == AgentRole.EXPLORER.value.lower()
    assert len(prompts) == 1
    with factory() as session:
        assert session.get(AgentExecution, seeded.replan_planner_id).output["outcome"] == "NEEDS_EVIDENCE"
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.EXPLORER.value.lower())) == 1
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()


def test_post_coder_replan_route_does_not_reuse_initial_plan_supervisor(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-identity")
    seeded = _seed_committed_coder_route(
        factory, rediagnosis_id, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED)
    with factory.begin() as session:
        stale = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED,
            created_at=datetime.now(timezone.utc),
            input={"planner_execution_id": str(seeded.planner_id)},
            output={"outcome": "BLOCKED"},
        )
        session.add(stale)
        session.flush()
        stale_id = stale.id
    prompts = []
    def route(prompt, _limit):
        prompts.append(prompt)
        return ModelReply({"outcome": "RE_EXPLORE", "reason": "Refresh the evidence",
                           "next_goal": "Re-read the narrower plan's evidence"},
                          ModelTokenUsage(input_tokens=40, output_tokens=20))
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=route)

    result = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path, supervisor_gateway=gateway)

    assert result["outcome"] == "replanned"
    assert result["replan_supervisor_execution_id"] != stale_id
    assert len(prompts) == 1
    with factory() as session:
        assert session.get(AgentExecution, stale_id).output == {"outcome": "BLOCKED"}
        supervisor = session.get(AgentExecution, result["replan_supervisor_execution_id"])
        assert supervisor.input["planner_execution_id"] == str(seeded.replan_planner_id)


def test_post_coder_replan_blocked_and_incomplete_supervisors_do_not_rerun(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    blocked_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-blocked")
    blocked_seed = _seed_committed_coder_route(
        factory, blocked_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_outcome="BLOCKED")
    failing_gateway = SimpleNamespace(
        request=lambda *args, **kwargs: pytest.fail("Committed Supervisor must not rerun"))

    blocked = _drive_after_committed_coder(
        worker, blocked_run, tmp_path, supervisor_gateway=failing_gateway)
    replay = _drive_after_committed_coder(
        worker, blocked_run, tmp_path, supervisor_gateway=failing_gateway)

    assert blocked["outcome"] == "blocked"
    assert blocked["reason"] == "replan_supervisor_blocked"
    assert replay["outcome"] == "blocked"
    assert blocked["replan_supervisor_execution_id"] == blocked_seed.replan_supervisor_id
    assert replay["replan_supervisor_execution_id"] == blocked_seed.replan_supervisor_id

    worker, task_id, run_id = create_running_run(factory, worker_id="running-route-worker")
    running_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-supervisor-running")
    running_seed = _seed_committed_coder_route(
        factory, running_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_status=AgentExecutionStatus.RUNNING)
    waiting = _drive_after_committed_coder(
        worker, running_run, tmp_path, supervisor_gateway=failing_gateway)
    assert waiting["outcome"] == "existing"
    assert waiting["reason"] == "replan_supervisor_not_completed"
    assert waiting["replan_supervisor_execution_id"] == running_seed.replan_supervisor_id

    worker, task_id, run_id = create_running_run(factory, worker_id="failed-route-worker")
    failed_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-supervisor-failed")
    failed_seed = _seed_committed_coder_route(
        factory, failed_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_status=AgentExecutionStatus.FAILED)
    unavailable = _drive_after_committed_coder(
        worker, failed_run, tmp_path, supervisor_gateway=failing_gateway)
    assert unavailable["outcome"] == "unresumable"
    assert unavailable["reason"] == "replan_supervisor_not_succeeded"
    assert unavailable["replan_supervisor_execution_id"] == failed_seed.replan_supervisor_id


def test_post_coder_replan_missing_supervisor_and_invalid_evidence_stop_safely(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    missing_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-no-supervisor")
    missing_seed = _seed_committed_coder_route(
        factory, missing_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED)

    missing = _drive_after_committed_coder(worker, missing_run, tmp_path)
    assert missing["outcome"] == "replanned"
    assert missing["reason"] == "supervisor_config_missing"
    assert missing["planner_execution_id"] == missing_seed.replan_planner_id

    worker, task_id, run_id = create_running_run(factory, worker_id="invalid-route-worker")
    invalid_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-replan-invalid-evidence")
    _seed_committed_coder_route(
        factory, invalid_run, coder_outcome="BLOCKED",
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_explorer_reference=False)
    failing_gateway = SimpleNamespace(
        request=lambda *args, **kwargs: pytest.fail("Invalid evidence must not start a model"))

    invalid = _drive_after_committed_coder(
        worker, invalid_run, tmp_path, supervisor_gateway=failing_gateway)
    assert invalid["outcome"] == "replan_unavailable"
    assert invalid["reason"] == "supervisor_evidence_invalid"
    with factory() as session:
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id.in_((missing_run, invalid_run)),
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
            AgentExecution.input["planner_execution_id"].as_string().is_not(None),
        )).all()


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_post_coder_replan_code_dispatches_trusted_coder_once_and_stops(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """A committed re-plan CODE route runs the existing Coder once, then stops."""
    factory = recovery_session_factory
    setup = _seed_replan_code_with_trusted_baseline(
        factory, tmp_path, monkeypatch, key="replan-code-dispatch")
    patch = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
             "@@ -1 +1 @@\n-Repository facts\n+Repository facts updated\n")
    prompts = []
    result = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        coder_gateway=_coder_gateway(
            factory, setup.worker.worker_id, "PATCH_READY", prompts=prompts, patch=patch),
    )

    # The post-baseline Coder's result continues into its own Supervisor route; without that
    # gateway the step stops after the committed Coder result (same contract as
    # test_post_coder_replan_baseline_then_routes_code_and_dispatches_coder_once).
    assert result["outcome"] == "code_pending"
    assert result["reason"] == "supervisor_config_missing"
    assert result["coder_outcome"] == "PATCH_READY"
    assert result["resumed"] is False
    assert result["planner_execution_id"] == setup.seeded.replan_planner_id
    assert result["replan_supervisor_execution_id"] != setup.code_supervisor_id
    assert result["replan_handoff_id"] == setup.baseline_handoff_id
    assert result["replan_handoff_target"] == AgentRole.VERIFIER.value.lower()
    assert result["replan_supervisor_outcome"] == "BASELINE"
    assert result["post_baseline_supervisor_execution_id"] == setup.code_supervisor_id
    assert result["post_baseline_handoff_id"] == setup.code_handoff_id
    assert result["post_baseline_handoff_target"] == AgentRole.CODER.value.lower()
    assert result["post_baseline_outcome"] == "CODE"
    assert result["post_coder_handoff_id"] == setup.seeded.post_handoff_id
    assert result["baseline_verifier_id"] == setup.baseline_id
    assert len(prompts) == 1
    coder_id = result["coder_execution_id"]
    with factory() as session:
        coder = session.get(AgentExecution, coder_id)
        assert coder.handoff_id == setup.code_handoff_id
        assert coder.status == AgentExecutionStatus.SUCCEEDED
        assert coder.output["outcome"] == "PATCH_READY"
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 2
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
        # The re-plan Coder's own Supervisor and the Verifier must not start.
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
            AgentExecution.input["coder_execution_id"].as_string() == str(coder_id))).all()
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING
    assert (setup.directories.workspace_root / "README.md").read_bytes() == (
        b"Repository facts updated\n")

    replay = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        coder_gateway=SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Committed Coder must not rerun")))
    assert replay["outcome"] == "code_pending" and replay["resumed"] is True
    assert replay["reason"] == "supervisor_config_missing"
    assert replay["coder_execution_id"] == coder_id
    assert replay["coder_outcome"] == "PATCH_READY"
    assert len(prompts) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 2


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_post_coder_replan_code_rejects_invalid_baseline_without_starting_coder(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    setup = _seed_replan_code_with_trusted_baseline(
        factory, tmp_path, monkeypatch, key="replan-code-invalid-baseline")
    # Rewriting the baseline execution's output would also falsify the Supervisor's recorded
    # observation of it, which is a contradictory history rather than an invalid baseline.
    # Keep the observation true and make the evidence unverifiable instead.
    with factory() as session:
        baseline = session.get(AgentExecution, setup.baseline_id)
        report_id = UUID(baseline.output["report_artifact_id"])
        report_path = Path(session.get(Artifact, report_id).content_ref)
    report_path.write_bytes(b"tampered baseline report")
    before = (setup.directories.workspace_root / "README.md").read_bytes()

    result = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories)

    assert result["outcome"] == "baseline_unavailable"
    assert result["reason"] == "baseline_evidence_invalid"
    assert result["baseline_verifier_id"] == setup.baseline_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
    assert (setup.directories.workspace_root / "README.md").read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_post_coder_replan_code_preserves_existing_changes_and_stops(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    setup = _seed_replan_code_with_trusted_baseline(
        factory, tmp_path, monkeypatch, key="replan-code-existing-change")
    sentinel = setup.directories.workspace_root / "previous-coder-change.txt"
    sentinel.write_text("previous change\n", encoding="utf-8")
    with factory.begin() as session:
        session.add(ToolCall(
            run_id=setup.rediagnosis_id, agent_execution_id=setup.seeded.coder_id,
            tool_name="apply_patch", args={"artifact_id": str(uuid4())},
            status=ToolCallStatus.SUCCEEDED, exit_code=0,
            created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        ))
    before = (setup.directories.workspace_root / "README.md").read_bytes()

    result = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories)

    assert result["outcome"] == "baseline_unavailable"
    assert result["reason"] == "baseline_evidence_invalid"
    # Existing workspace content is reported, never silently reset or cleaned.
    assert sentinel.read_text(encoding="utf-8") == "previous change\n"
    assert (setup.directories.workspace_root / "README.md").read_bytes() == before
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_post_coder_replan_code_saves_blocked_result_and_stops(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    setup = _seed_replan_code_with_trusted_baseline(
        factory, tmp_path, monkeypatch, key="replan-code-blocked")
    prompts = []
    result = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        coder_gateway=_coder_gateway(
            factory, setup.worker.worker_id, "BLOCKED", prompts=prompts))

    # Same contract as the PATCH_READY case: the committed Coder result is saved, and the
    # step stops where the Coder's own Supervisor route would need a gateway.
    assert result["outcome"] == "code_pending"
    assert result["reason"] == "supervisor_config_missing"
    assert result["coder_outcome"] == "BLOCKED"
    assert result["resumed"] is False
    assert len(prompts) == 1
    coder_id = result["coder_execution_id"]
    with factory() as session:
        coder = session.get(AgentExecution, coder_id)
        assert coder.handoff_id == setup.code_handoff_id
        assert coder.status == AgentExecutionStatus.SUCCEEDED
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 2
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
            AgentExecution.input["coder_execution_id"].as_string() == str(coder_id))).all()
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING

    replay = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        coder_gateway=SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Committed Coder must not rerun")))
    assert replay["outcome"] == "code_pending" and replay["resumed"] is True
    assert replay["reason"] == "supervisor_config_missing"
    assert replay["coder_execution_id"] == coder_id
    assert replay["coder_outcome"] == "BLOCKED"
    assert len(prompts) == 1


def test_post_coder_replan_code_requires_trusted_inputs_and_handles_incomplete_coder(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    route = dict(
        post_status=AgentExecutionStatus.SUCCEEDED, post_outcome="REPLAN",
        post_target=AgentRole.PLANNER,
        replan_planner_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_status=AgentExecutionStatus.SUCCEEDED,
        replan_supervisor_outcome="CODE",
        replan_supervisor_target=AgentRole.CODER,
    )

    worker, task_id, run_id = create_running_run(factory, worker_id="replan-code-missing")
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "replan-code-missing-baseline")
    _seed_committed_coder_route(factory, rediagnosis_id, coder_outcome="BLOCKED", **route)
    missing = _drive_after_committed_coder(worker, rediagnosis_id, tmp_path)
    assert missing["outcome"] == "baseline_unavailable"
    assert missing["reason"] == "coder_baseline_unavailable"
    no_config = worker._drive_rediagnosis_after_exploration(
        rediagnosis_id, directories=RunDirectories(tmp_path / "w2", tmp_path / "a2"),
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        max_read_bytes=1024, stop_event=Event(), coder_gateway=None, max_patch_bytes=4096)
    assert no_config["outcome"] == "code_pending"
    assert no_config["reason"] == "coder_config_missing"

    worker, task_id, run_id = create_running_run(factory, worker_id="replan-code-invalid-ref")
    invalid_run = _start_rediagnosis(
        factory, worker, task_id, run_id, "replan-code-invalid-ref")
    _seed_committed_coder_route(
        factory, invalid_run, coder_outcome="BLOCKED",
        replan_supervisor_evidence_refs=[{"baseline_verifier_id": str(uuid4())}], **route)
    invalid = _drive_after_committed_coder(worker, invalid_run, tmp_path)
    assert invalid["outcome"] == "baseline_unavailable"
    assert invalid["reason"] == "baseline_evidence_invalid"

    for status, expected in (
        (AgentExecutionStatus.RUNNING, "existing"),
        (AgentExecutionStatus.FAILED, "unresumable"),
    ):
        worker, task_id, run_id = create_running_run(
            factory, worker_id=f"replan-code-{status.value.lower()}")
        rediagnosis_id = _start_rediagnosis(
            factory, worker, task_id, run_id, f"replan-code-{status.value.lower()}")
        seeded = _seed_committed_coder_route(
            factory, rediagnosis_id, coder_outcome="BLOCKED",
            replan_supervisor_evidence_refs=[{"baseline_verifier_id": str(uuid4())}], **route)
        with factory.begin() as session:
            session.add(AgentExecution(
                run_id=rediagnosis_id, agent_role=AgentRole.CODER.value.lower(),
                status=status, handoff_id=seeded.replan_handoff_id, input={}, output=None,
            ))
        result = _drive_after_committed_coder(worker, rediagnosis_id, tmp_path)
        assert result["outcome"] == expected
        assert result["reason"] == "coder_not_completed"
        # The Coder execution was already present, so this call reused it.
        assert result["resumed"] is True
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(AgentExecution).where(
                AgentExecution.run_id == rediagnosis_id,
                AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 2


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_post_coder_replan_baseline_dispatches_trusted_baseline_once_and_stops(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """A committed re-plan BASELINE route records the trusted baseline, then stops."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(factory, tmp_path, monkeypatch, key="replan-baseline")
    before = (setup.directories.workspace_root / "README.md").read_bytes()

    result = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        verification_config=setup.config)

    assert result["outcome"] == "post_baseline_route_pending"
    assert result["reason"] == "supervisor_config_missing"
    assert result["resumed"] is False
    assert result["planner_execution_id"] == setup.seeded.replan_planner_id
    assert result["replan_supervisor_execution_id"] == setup.seeded.replan_supervisor_id
    assert result["replan_supervisor_outcome"] == "BASELINE"
    assert result["replan_handoff_target"] == AgentRole.VERIFIER.value.lower()
    assert result["baseline_handoff_id"] == setup.seeded.replan_handoff_id
    baseline_id = result["baseline_verifier_id"]
    with factory() as session:
        baseline = session.get(AgentExecution, baseline_id)
        assert baseline.status == AgentExecutionStatus.SUCCEEDED
        assert baseline.output["outcome"] == "BASELINE_RECORDED"
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
        # A recorded baseline is not an acceptance, and the later route must not
        # start: still no Verification, no second Coder, no post-baseline Supervisor.
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 3
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING
    # The baseline runs against the pinned commit and leaves the workspace alone.
    assert (setup.directories.workspace_root / "README.md").read_bytes() == before

    replay = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories)
    assert replay["outcome"] == "post_baseline_route_pending"
    assert replay["baseline_verifier_id"] == baseline_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Job")
def test_post_coder_replan_baseline_then_routes_code_and_dispatches_coder_once(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """The pre-baseline decision stays separate from the post-baseline CODE route."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-baseline-phase")
    first = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        verification_config=setup.config)
    # The recorded baseline unlocks the post-baseline route, which needs its own
    # Supervisor configuration; without it the step stops at the pending boundary.
    assert first["outcome"] == "post_baseline_route_pending"
    assert first["reason"] == "supervisor_config_missing"
    assert first["baseline_verifier_id"] is not None
    baseline_id = first["baseline_verifier_id"]
    # A later route for the same re-plan Planner drives the Coder, but the reported
    # re-plan decision must still be the pre-baseline Supervisor.
    with factory.begin() as session:
        later_handoff = Handoff(
            from_agent_execution_id=setup.seeded.replan_planner_id,
            to_agent_role=AgentRole.CODER.value.lower(),
            context={"completed_work": "The re-plan baseline is recorded",
                     "blocker": None,
                     "evidence_refs": [{"baseline_verifier_id": str(baseline_id)}],
                     "decision_required": "Implement the narrower plan",
                     "scope_impact": "Only README.md"},
        )
        session.add(later_handoff)
        session.flush()
        later_supervisor = AgentExecution(
            run_id=setup.rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED,
            created_at=datetime.now(timezone.utc) + timedelta(hours=1),
            input={"planner_execution_id": str(setup.seeded.replan_planner_id)},
            output={"outcome": "CODE", "approved_handoff_id": str(later_handoff.id)},
        )
        session.add(later_supervisor)
        session.flush()
        later_handoff_id, later_supervisor_id = later_handoff.id, later_supervisor.id
    prompts = []

    replay = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        coder_gateway=_coder_gateway(
            factory, setup.worker.worker_id, "BLOCKED", prompts=prompts))

    # The post-baseline Coder result continues into the Coder's own Supervisor
    # route, which needs its own gateway; without one the step stops after the
    # committed Coder result instead of pretending that route completed.
    assert replay["outcome"] == "code_pending"
    assert replay["reason"] == "supervisor_config_missing"
    assert replay["coder_outcome"] == "BLOCKED"
    assert replay["baseline_verifier_id"] == baseline_id
    assert replay["replan_supervisor_execution_id"] == setup.seeded.replan_supervisor_id
    assert replay["replan_supervisor_outcome"] == "BASELINE"
    assert replay["post_baseline_supervisor_execution_id"] == later_supervisor_id
    assert replay["post_baseline_outcome"] == "CODE"
    assert replay["post_baseline_handoff_id"] == later_handoff_id
    assert len(prompts) == 1
    with factory() as session:
        coders = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())).all()
        assert len(coders) == 2
        assert [coder.handoff_id for coder in coders if coder.handoff_id == later_handoff_id] == [
            later_handoff_id]
        # Without a Supervisor gateway the post-baseline Coder's own Supervisor,
        # the Verifier and any formal acceptance must not start; the earlier
        # Verifier remains the baseline.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING

    replay = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        coder_gateway=_coder_gateway(
            factory, setup.worker.worker_id, "BLOCKED", prompts=prompts))
    assert replay["outcome"] == "code_pending"
    assert replay["post_baseline_supervisor_execution_id"] == later_supervisor_id
    assert len(prompts) == 1


def test_replanned_post_baseline_route_requires_its_own_supervisor_config(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """Without a Supervisor gateway the post-baseline route stops before starting."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-post-baseline-missing")

    result = _call_replanned_post_baseline_route(setup)

    assert result["outcome"] == "post_baseline_route_pending"
    assert result["reason"] == "supervisor_config_missing"
    assert result["resumed"] is False
    with factory() as session:
        # No Supervisor and no Coder were started for the post-baseline route.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 3
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1


def test_replanned_post_baseline_second_baseline_request_does_not_rerun(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """A second BASELINE decision after the trusted baseline neither reruns nor patches."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-post-baseline-again")
    seeded = _seed_post_baseline_supervisor(
        factory, setup, outcome="BASELINE", target=AgentRole.VERIFIER)

    result = _call_replanned_post_baseline_route(setup)

    assert result["outcome"] == "baseline_recorded"
    assert result["reason"] == "baseline_already_recorded"
    assert result["post_baseline_supervisor_execution_id"] == seeded.supervisor_id
    assert result["post_baseline_outcome"] == "BASELINE"
    with factory() as session:
        # The trusted baseline is not run a second time and no Coder is started.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1


def test_replanned_post_baseline_code_needs_the_trusted_coder_config(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-post-baseline-code")
    seeded = _seed_post_baseline_supervisor(
        factory, setup, outcome="CODE", target=AgentRole.CODER)

    result = _call_replanned_post_baseline_route(setup)

    assert result["outcome"] == "code_pending"
    assert result["reason"] == "coder_config_missing"
    assert result["post_baseline_supervisor_execution_id"] == seeded.supervisor_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1


def test_replanned_post_baseline_running_and_failed_supervisors_stop_safely(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    for status, outcome, reason in (
        (AgentExecutionStatus.RUNNING, "existing", "replan_supervisor_not_completed"),
        (AgentExecutionStatus.FAILED, "unresumable", "replan_supervisor_not_succeeded"),
        (AgentExecutionStatus.SUCCEEDED, "blocked", "replan_supervisor_blocked"),
    ):
        base = tmp_path / status.value.lower()
        base.mkdir()
        setup = _seed_replan_baseline_route(
            factory, base, monkeypatch, key=f"replan-post-baseline-{status.value.lower()}")
        _seed_post_baseline_supervisor(
            factory, setup, outcome=None if status != AgentExecutionStatus.SUCCEEDED else "BLOCKED",
            target=None, status=status)

        result = _call_replanned_post_baseline_route(setup)

        assert result["outcome"] == outcome
        assert result["reason"] == reason
        with factory() as session:
            # The committed Supervisor is reused instead of being started again.
            assert session.scalar(select(func.count()).select_from(AgentExecution).where(
                AgentExecution.run_id == setup.rediagnosis_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower())) == 4
            assert session.scalar(select(func.count()).select_from(AgentExecution).where(
                AgentExecution.run_id == setup.rediagnosis_id,
                AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1


def test_replanned_coder_result_without_its_supervisor_gateway_stops(
    recovery_session_factory, tmp_path,
):
    """A committed Coder result is not a completed step without its Supervisor route."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="coder-supervisor-missing")
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "coder-supervisor-missing")
    seeded = _seed_committed_coder_route(factory, rediagnosis_id, coder_outcome="BLOCKED")
    with factory() as session:
        coder = session.get(AgentExecution, seeded.coder_id)

    result = worker._continue_rediagnosis_after_coder(
        rediagnosis_id, coder=coder, common={},
        directories=RunDirectories(tmp_path / "w", tmp_path / "a"),
        supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
        verification_config=None, verifier_gateway=None, verifier_max_diff_bytes=None,
        max_read_bytes=1024, max_patch_bytes=4096, stop_event=Event(), resumed=False)

    assert result["outcome"] == "code_pending"
    assert result["reason"] == "supervisor_config_missing"
    assert result["coder_outcome"] == "BLOCKED"


def test_replanned_coder_replan_request_is_deferred_not_dispatched(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """A later Coder's REPLAN is saved and stops instead of starting a new Planner."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-coder-deferred")
    _seed_post_coder_supervisor(
        factory, setup.rediagnosis_id, setup.seeded.coder_id,
        outcome="REPLAN", target=AgentRole.PLANNER)

    result = _call_after_replanned_coder(setup, dispatch_replan=False)

    assert result["outcome"] == "replan"
    assert result["reason"] == "replan_deferred"
    assert result["post_coder_supervisor_execution_id"] is not None
    with factory() as session:
        # No second Planner cycle and no extra Supervisor were started.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.PLANNER.value.lower())) == 2
        assert session.scalar(select(func.count()).select_from(Handoff).where(
            Handoff.from_agent_execution_id == setup.seeded.coder_id,
            Handoff.to_agent_role == AgentRole.PLANNER.value.lower())) == 2


def test_replanned_coder_replan_request_can_start_one_more_planner_cycle(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """A REPLAN from the later Coder continues into another Planner within budget."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-coder-loop")
    _seed_post_coder_supervisor(
        factory, setup.rediagnosis_id, setup.seeded.coder_id,
        outcome="REPLAN", target=AgentRole.PLANNER,
        evidence_refs=[{"explorer_execution_id": str(setup.seeded.explorer_id)}])
    planner_gateway = PlannerModelGateway(
        session_factory=factory, worker_id=setup.worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply({"outcome": "PLAN_READY", "plan": {
            "approach": "Take the narrower documented change",
            "target_paths": ["README.md"], "steps": ["Edit the documented line"],
            "scope_limits": "Only README.md", "verification": ["Review the diff"],
        }}, ModelTokenUsage(input_tokens=40, output_tokens=20)),
    )

    result = _call_after_replanned_coder(setup, planner_gateway=planner_gateway)

    # The new Planner is committed; the next Supervisor route needs its own
    # configuration, so the cycle stops there instead of guessing.
    assert result["outcome"] == "replanned"
    assert result["reason"] == "supervisor_config_missing"
    with factory() as session:
        planners = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.PLANNER.value.lower()).order_by(
                AgentExecution.created_at, AgentExecution.id)).all()
        assert len(planners) == 3
        assert planners[-1].status == AgentExecutionStatus.SUCCEEDED
        assert planners[-1].output["outcome"] == "PLAN_READY"
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING


def test_replanned_coder_verify_route_needs_trusted_verifier_inputs(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """A VERIFY route passes the trusted configuration down to the Verifier gates."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-coder-verify")
    _seed_post_coder_supervisor(
        factory, setup.rediagnosis_id, setup.seeded.coder_id,
        outcome="VERIFY", target=AgentRole.VERIFIER)

    no_config = _call_after_replanned_coder(setup)
    assert no_config["outcome"] == "verification_config_missing"

    no_gateway = _call_after_replanned_coder(setup, verification_config=setup.config)
    assert no_gateway["outcome"] == "verifier_config_missing"

    # Both inputs reach the existing post-patch Verifier, which rejects the
    # handoff for missing applied-patch evidence and records no formal result.
    blocked = _call_after_replanned_coder(
        setup, verification_config=setup.config,
        verifier_gateway=SimpleNamespace(
            request=lambda *args, **kwargs: pytest.fail("Verifier model must not run")))
    assert blocked["outcome"] == "verification_unavailable"
    assert blocked["reason"] == "verification_evidence_invalid"
    with factory() as session:
        # No Verifier result, no formal Verification and no Run verdict are written.
        assert not [verifier for verifier in session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()
            if verifier.status == AgentExecutionStatus.SUCCEEDED]
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.get(Run, setup.rediagnosis_id).status == RunStatus.RUNNING


def test_post_coder_replan_baseline_stops_on_an_applied_patch_without_cleaning(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """An unverified applied patch keeps the re-plan baseline blocked and untouched."""
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-baseline-patched")
    sentinel = setup.directories.workspace_root / "previous-coder-change.txt"
    sentinel.write_text("previous change\n", encoding="utf-8")
    with factory.begin() as session:
        session.add(ToolCall(
            run_id=setup.rediagnosis_id, agent_execution_id=setup.seeded.coder_id,
            tool_name="apply_patch", args={"artifact_id": str(uuid4())},
            status=ToolCallStatus.SUCCEEDED, exit_code=0,
            created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        ))
    before = (setup.directories.workspace_root / "README.md").read_bytes()

    result = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        verification_config=setup.config)

    assert result["outcome"] == "baseline_unavailable"
    assert result["reason"] == "baseline_execution_failed"
    assert result["baseline_verifier_id"] is not None
    # Existing workspace content is reported, never silently reset or cleaned.
    assert sentinel.read_text(encoding="utf-8") == "previous change\n"
    assert (setup.directories.workspace_root / "README.md").read_bytes() == before
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower())) == 1


def test_post_coder_replan_baseline_requires_the_matching_trusted_config(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-baseline-config")
    before = (setup.directories.workspace_root / "README.md").read_bytes()

    missing = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories)
    assert missing["outcome"] == "baseline_config_missing"
    assert missing["resumed"] is False

    other_task = VerificationConfig(
        task_id=uuid4(), base_commit=setup.base_commit, command_name="pytest",
        command=setup.config.command, target_tests=setup.config.target_tests,
        external_connections="none")
    invalid = _drive_after_committed_coder(
        setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
        verification_config=other_task)
    assert invalid["outcome"] == "baseline_config_invalid"

    with factory() as session:
        # No Verifier is created and the workspace is never touched.
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 0
        assert session.scalar(select(func.count()).select_from(Verification).where(
            Verification.run_id == setup.rediagnosis_id)) == 0
    assert (setup.directories.workspace_root / "README.md").read_bytes() == before


def test_post_coder_replan_baseline_preserves_running_and_failed_verifiers(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    for status, outcome, reason in (
        (AgentExecutionStatus.RUNNING, "existing", "baseline_not_completed"),
        (AgentExecutionStatus.FAILED, "baseline_unavailable", "baseline_execution_failed"),
    ):
        base = tmp_path / status.value.lower()
        base.mkdir()
        setup = _seed_replan_baseline_route(
            factory, base, monkeypatch,
            key=f"replan-baseline-{status.value.lower()}")
        with factory.begin() as session:
            session.add(AgentExecution(
                run_id=setup.rediagnosis_id, agent_role=AgentRole.VERIFIER.value.lower(),
                status=status, handoff_id=setup.seeded.replan_handoff_id, input={}, output=None,
            ))

        result = _drive_after_committed_coder(
            setup.worker, setup.rediagnosis_id, tmp_path, directories=setup.directories,
            verification_config=setup.config)

        assert result["outcome"] == outcome
        assert result["reason"] == reason
        assert result["resumed"] is True
        with factory() as session:
            # The existing Verifier is reused instead of being started again.
            assert session.scalar(select(func.count()).select_from(AgentExecution).where(
                AgentExecution.run_id == setup.rediagnosis_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 1
            assert session.scalar(select(func.count()).select_from(Verification).where(
                Verification.run_id == setup.rediagnosis_id)) == 0


def test_post_coder_replan_baseline_requires_the_current_lease(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    setup = _seed_replan_baseline_route(
        factory, tmp_path, monkeypatch, key="replan-baseline-lease")
    with factory() as session:
        planner = session.get(AgentExecution, setup.seeded.replan_planner_id)
    with factory.begin() as session:
        session.get(Run, setup.rediagnosis_id).lease_expires_at = func.clock_timestamp()

    with pytest.raises(RunExecutionDeniedError):
        setup.worker._route_rediagnosis_after_replan_planner(
            setup.rediagnosis_id, planner=planner,
            post_coder_handoff_id=setup.seeded.post_handoff_id, common={},
            supervisor_gateway=None, stop_event=setup.stop, resumed=False,
            directories=setup.directories, coder_gateway=None, max_patch_bytes=None,
            max_read_bytes=1024, explorer_gateway=None, planner_gateway=None,
            verification_config=setup.config)

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == setup.rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())) == 0


def test_post_coder_patch_without_an_applied_patch_is_not_sent_to_verifier(
    recovery_session_factory, tmp_path,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "coder-no-apply")
    _seed_committed_coder_route(factory, rediagnosis_id, coder_outcome="PATCH_READY")

    result = _drive_after_committed_coder(
        worker, rediagnosis_id, tmp_path,
        supervisor_gateway=SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("Model must not run")),
    )

    assert result["outcome"] == "patch_unavailable"
    assert result["reason"] == "coder_evidence_invalid"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
            AgentExecution.input["coder_execution_id"].as_string().is_not(None))) == 0
        assert not session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == rediagnosis_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()


def test_running_post_coder_nodes_are_handled_conservatively(
    recovery_session_factory, tmp_path, monkeypatch,
):
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory)
    waiting_run = _start_rediagnosis(factory, worker, task_id, run_id, "coder-running-supervisor")
    _seed_committed_coder_route(factory, waiting_run, post_status=AgentExecutionStatus.RUNNING)

    waiting = _drive_after_committed_coder(worker, waiting_run, tmp_path)
    assert waiting["outcome"] == "existing"
    assert waiting["reason"] == "post_coder_supervisor_not_completed"

    worker, task_id, run_id = create_running_run(factory, worker_id="second-worker")
    verifier_run = _start_rediagnosis(factory, worker, task_id, run_id, "coder-running-verifier")
    draft_id = uuid4()
    _seed_committed_coder_route(
        factory, verifier_run, post_status=AgentExecutionStatus.SUCCEEDED,
        post_outcome="VERIFY", post_target=AgentRole.VERIFIER,
        verifier_status=AgentExecutionStatus.RUNNING,
        verifier_output={"outcome": "VERIFICATION_BLOCKED", "draft_artifact_id": str(draft_id),
                         "missing_evidence": ["test_integrity"],
                         "preliminary_public_outcome": "passed"})
    monkeypatch.setattr(
        worker, "_dispatch_committed_handoff",
        lambda *args, **kwargs: pytest.fail("A running Verifier must not be dispatched again"))

    replayed = _drive_after_committed_coder(
        worker, verifier_run, tmp_path,
        verification_config=_matching_verification_config(task_id),
        verifier_gateway=SimpleNamespace(request=lambda *args, **kwargs: pytest.fail("Model must not run")),
    )
    assert replayed["outcome"] == "verifier_blocked" and replayed["resumed"] is True
    assert replayed["draft_artifact_id"] == str(draft_id)
    with factory() as session:
        assert len(session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == verifier_run,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower())).all()) == 1



def test_a_rehearsal_runs_through_the_cli_and_stays_out_of_the_sample(
    recovery_session_factory, tmp_path, monkeypatch, capsys,
):
    """The CLI drives the real single-task entry; a rehearsal is recorded but not counted.

    Stub models make the attempt fail early, which is enough: the point is that the CLI,
    the real claim, the workspace preparation, the record write and the rehearsal marker
    all work together — not that a model produced a patch.
    """
    from devflow_ai.benchmark_run import main as benchmark_run_main
    from devflow_ai.benchmark_record import main as record_main
    from devflow_ai.model_gateways import ModelGateways

    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    factory = recovery_session_factory
    repo = tmp_path / "repo"
    base_commit = _create_git_fixture(repo)
    policy = rediagnosis_policy()
    policy.budget.max_retries_per_task = 0
    policy.budget.max_rediagnose_attempts = 0
    policy.budget.max_llm_tokens_per_run = 80_000
    with factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Rehearsal task", description="Rehearse the CLI entry",
            repo=str(repo), idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="rehearsal", evidence=["fixture"]))
    pack_dir = _benchmark_pack(tmp_path, base_commit=base_commit)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy.model_dump(mode="json")), encoding="utf-8")
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    record_path = tmp_path / "record.json"

    def refuse(*_args, **_kwargs):
        raise RuntimeError("stub model refuses during the rehearsal")

    @contextmanager
    def gateways(session_factory, worker_id):
        def gateway():
            return ModelGateway(session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=lambda _: 10, max_output_tokens=10, generate=refuse)
        planner = PlannerModelGateway(session_factory=session_factory, worker_id=worker_id,
            estimate_input_tokens=lambda _: 10, max_output_tokens=10, generate=refuse)
        yield ModelGateways(supervisor=gateway(), explorer=gateway(), planner=planner,
                            coder=gateway(), verifier=gateway())

    exit_code = benchmark_run_main([
        "--pack", str(pack_dir), "--task-id", str(task_id), "--operator", "operator-a",
        "--python-executable", sys.executable,
        "--workspaces-root", str(workspaces), "--artifacts-root", str(artifacts),
        "--record", str(record_path), "--policy", str(policy_path),
        "--attempt-kind", "rehearsal", "--confirm-run", "--max-patch-bytes", "4096",
        "--max-diff-bytes", "65536"],                       # fmt: skip
        session_factory=factory, gateways_factory=gateways)

    captured = capsys.readouterr()
    assert exit_code == 1, captured
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["attempt_kind"] == "rehearsal"
    assert record["outcome"] == "incomplete"
    assert "execution_error:" in record["notes"]
    assert "attempt_kind=" not in record["notes"]
    with factory() as session:
        run_id = session.scalar(select(Run.id).where(Run.task_id == task_id))
    assert (artifacts / str(run_id)).is_dir()
    # The rehearsal stays evidence, and the summary keeps it out of the formal sample.
    assert record_main(["summarize", str(record_path)]) == 0
    summary = json.loads(capsys.readouterr().out.strip())
    assert summary["sample_size"] == 0 and summary["rehearsals_excluded"] == 1


def test_run_usage_is_summed_per_role_from_committed_rows(recovery_session_factory):
    """The accounting reads committed usage rows only, and keeps unsettled requests apart."""
    from devflow_ai.usage_report import load_run_usage

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="usage-report")
    with factory.begin() as session:
        for role, usage in (
            (AgentRole.SUPERVISOR, {"model_requests": {"a": {
                "worker_id": "w", "reserved_tokens": 4096,
                "actual_usage": {"input_tokens": 300, "output_tokens": 20}}}}),
            (AgentRole.SUPERVISOR, {"model_requests": {"b": {
                "worker_id": "w", "reserved_tokens": 4096,
                "actual_usage": {"input_tokens": 100, "output_tokens": 10}}}}),
            (AgentRole.EXPLORER, {"model_requests": {"c": {
                "worker_id": "w", "reserved_tokens": 4096, "actual_usage": None}}}),
        ):
            session.add(AgentExecution(
                run_id=run_id, agent_role=role.value.lower(), status=AgentExecutionStatus.SUCCEEDED,
                input={}, output={}, llm_usage=usage))

    with factory() as session:
        report = load_run_usage(session, run_id)

    rows = {row["role"]: row for row in report["roles"]}
    assert rows["supervisor"]["requests"] == 2 and rows["supervisor"]["total_tokens"] == 430
    # A request that never reported usage is not counted as free: it is counted apart.
    assert rows["explorer"]["unsettled_requests"] == 1 and rows["explorer"]["total_tokens"] == 0
    assert report["total_tokens"] == 430
    assert report["status"] == "RUNNING" and report["attempt_number"] == 1


def _seed_blocked_verifier(factory, run_id, *, output=None):
    """One Verifier execution in the state the post-patch checkpoint leaves behind."""
    with factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={},
            output=output if output is not None else {
                "outcome": "VERIFICATION_BLOCKED", "draft_artifact_id": str(uuid4()),
                "missing_evidence": ["test_integrity"],
                "preliminary_public_outcome": "passed",
                "external_connections_declaration": "none"})
        session.add(execution)
        session.flush()
        return execution.id


def test_the_stage_driver_treats_a_checkpointed_draft_as_blocked_not_incomplete(
    recovery_session_factory,
):
    """A blocked Verifier stays RUNNING by design; that is not an unfinished stage."""
    from devflow_ai.orchestration import run_task_stages

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="checkpointed-draft")
    verifier_id = _seed_blocked_verifier(factory, run_id)

    result = run_task_stages(
        factory, run_id, worker.worker_id, directories=None, gateways=None,
        receivers_factory=lambda: {}, initial_supervise=lambda: None, stop_event=Event())

    assert result["outcome"] == "stopped" and result["stages"] == 0
    assert result["reason"] == "verification_blocked"
    assert result["draft_pending_finalization"] is True
    assert result["missing_evidence"] == ["test_integrity"]
    with factory() as session:
        assert session.get(AgentExecution, verifier_id).status == AgentExecutionStatus.RUNNING


def test_a_running_verifier_without_a_draft_is_still_reported_as_incomplete(
    recovery_session_factory,
):
    from devflow_ai.orchestration import run_task_stages

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="verifier-running")
    _seed_blocked_verifier(factory, run_id, output={})

    result = run_task_stages(
        factory, run_id, worker.worker_id, directories=None, gateways=None,
        receivers_factory=lambda: {}, initial_supervise=lambda: None, stop_event=Event())

    assert result["reason"] == "last_stage_incomplete"


def test_the_finalize_entry_finds_a_checkpointed_draft_on_a_running_verifier(
    recovery_session_factory, tmp_path,
):
    """The formal entry must reach the draft the checkpoint committed, or it can never run."""
    from devflow_ai.orchestration import finalize_benchmark_run

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="finalize-entry")
    verifier_id = _seed_blocked_verifier(factory, run_id)
    with factory() as session:
        draft_id = UUID(session.get(AgentExecution, verifier_id).output["draft_artifact_id"])
    calls = []

    def finalize(run_id_argument, **kwargs):
        calls.append({"run_id": run_id_argument, **kwargs})
        return {"outcome": "passed", "verification_id": str(uuid4())}

    stub = SimpleNamespace(session_factory=factory, finalize_with_independent_evaluation=finalize)
    config = VerificationConfig(
        task_id=task_id, base_commit="a" * 40, command_name="pytest",
        command=("python", "-m", "pytest"), target_tests=("x::y",),
        external_connections="none", independent_suite_version="suite-1")
    material = SimpleNamespace(
        image="python:3.11-slim", command=("python", "-B", "adapter.py"), cases=(),
        calibration_workspaces=(tmp_path / "baseline", tmp_path / "corrected", "d" * 40),
        timeout_seconds=15)

    result = finalize_benchmark_run(
        stub, run_id, directories=None, verification_config=config, material=material,
        max_diff_bytes=65536, max_patch_bytes=4096, stop_event=Event())

    assert result["outcome"] == "finalized"
    assert result["independent_outcome"] == "passed"
    assert calls[0]["agent_execution_id"] == verifier_id
    assert calls[0]["draft_artifact_id"] == draft_id


def test_the_finalize_entry_names_a_verifier_that_has_not_produced_a_draft(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.orchestration import finalize_benchmark_run

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="finalize-running")
    verifier_id = _seed_blocked_verifier(factory, run_id, output={})
    stub = SimpleNamespace(session_factory=factory, finalize_with_independent_evaluation=None)
    config = VerificationConfig(
        task_id=task_id, base_commit="a" * 40, command_name="pytest",
        command=("python", "-m", "pytest"), target_tests=("x::y",),
        external_connections="none", independent_suite_version="suite-1")
    material = SimpleNamespace(
        image="python:3.11-slim", command=("python",), cases=(),
        calibration_workspaces=(tmp_path / "b", tmp_path / "c", "d" * 40), timeout_seconds=15)

    result = finalize_benchmark_run(
        stub, run_id, directories=None, verification_config=config, material=material,
        max_diff_bytes=65536, max_patch_bytes=4096, stop_event=Event())

    assert result["outcome"] == "not_finalized"
    assert result["reason"] == "verifier_still_running"
    assert result["verifier_execution_id"] == str(verifier_id)
    assert result["verifier_status"] == "running"


@pytest.mark.skipif(os.name != "nt", reason="pytest report capture requires a managed Windows Job")
def test_a_real_checkpointed_draft_reaches_the_finalize_entry(
    recovery_session_factory, tmp_path, monkeypatch,
):
    """End to end over a real chain: checkpoint → stage driver → finalize entry.

    The models are stubs and the finalize call is replaced, so this proves the wiring the
    second real attempt depended on: a draft committed with the Verifier still RUNNING is
    reported as blocked (not incomplete) and is located by the formal entry.
    """
    from devflow_ai.orchestration import finalize_benchmark_run, run_task_stages

    setup = _run_rediagnosis_to_verifier(
        recovery_session_factory, tmp_path, monkeypatch, key="checkpoint-wiring")
    assert setup.result["outcome"] == "verifier_blocked"
    run_id = UUID(str(setup.result["run_id"]))
    verifier_id = UUID(str(setup.result["verifier_execution_id"]))
    draft_id = UUID(str(setup.result["draft_artifact_id"]))

    stages = run_task_stages(
        setup.factory, run_id, setup.worker.worker_id, directories=None, gateways=None,
        receivers_factory=lambda: {}, initial_supervise=lambda: None, stop_event=setup.stop)

    assert stages["reason"] == "verification_blocked"
    assert stages["draft_pending_finalization"] is True
    assert stages["missing_evidence"]

    calls = []

    def finalize(run_id_argument, **kwargs):
        calls.append({"run_id": run_id_argument, **kwargs})
        return {"outcome": "passed", "verification_id": str(uuid4())}

    stub = SimpleNamespace(session_factory=setup.factory, finalize_with_independent_evaluation=finalize)
    result = finalize_benchmark_run(
        stub, run_id, directories=None,
        verification_config=setup.config.model_copy(update={"independent_suite_version": "test-suite-v1"}),
        material=SimpleNamespace(
            image="python:3.11-slim", command=("python", "-B", "adapter.py"), cases=(),
            calibration_workspaces=(tmp_path / "baseline", tmp_path / "corrected", "d" * 40),
            timeout_seconds=15),
        max_diff_bytes=65536, max_patch_bytes=4096, stop_event=setup.stop)

    assert result["outcome"] == "finalized"
    assert calls[0]["agent_execution_id"] == verifier_id
    assert calls[0]["draft_artifact_id"] == draft_id


def _rewrite_artifact(artifact_root, artifact_id, payload):
    """Replace one artifact's bytes in place, the way a tamper test needs to."""
    import io

    from devflow_ai.artifacts import write_artifact_file

    for existing in Path(artifact_root).glob(f"{artifact_id}.*"):
        existing.unlink()
    return write_artifact_file(
        artifact_root, artifact_id, io.BytesIO(payload), chunk_size=65536)


def _seed_checkpointed_verifier(factory, run_id, *, artifact_root, receipt_outcome,
                                receipt_calibrated=True, preliminary="passed"):
    """A Verifier checkpoint whose public result and evaluation receipt are readable fixtures."""
    with factory.begin() as session:
        verifier = AgentExecution(
            run_id=run_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output={})
        session.add(verifier)
        session.flush()
        verifier_id = verifier.id
    written = _write_benchmark_evidence(
        factory, artifact_root, run_id=run_id, verifier_execution_id=verifier_id,
        suite_version="suite-1", receipt_outcome=receipt_outcome,
        receipt_calibrated=receipt_calibrated)
    with factory.begin() as session:
        session.get(AgentExecution, verifier_id).output = {
            "outcome": "VERIFICATION_BLOCKED", "draft_artifact_id": str(written.draft_id),
            "missing_evidence": ["test_integrity"], "preliminary_public_outcome": preliminary,
            "external_connections_declaration": "none"}
    return verifier_id, written


def test_a_passed_checkpoint_with_an_unavailable_evaluation_is_a_public_only_pass(
    recovery_session_factory, tmp_path,
):
    """The attempt ended, the public result is verified, and the evaluation says why not."""
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="public-only")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    verifier_id, written = _seed_checkpointed_verifier(
        factory, run_id, artifact_root=artifacts, receipt_outcome="unavailable")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve(),
            finalization={"outcome": "finalized", "independent_outcome": "unavailable",
                          "independent_artifact_id": str(written.receipt_id)})

    assert decision.outcome is RunOutcome.PUBLIC_ONLY_PASS
    assert decision.public_checks.verified_outcome == "passed"
    assert decision.public_checks.verifier_execution_id == str(verifier_id)
    assert decision.independent.outcome == "unavailable" and decision.independent.reason
    assert decision.reason.startswith("public_passed_independent_unavailable:")


def test_a_calibrated_independent_failure_is_a_failure_with_both_halves_kept(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="independent-failed")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    verifier_id, written = _seed_checkpointed_verifier(
        factory, run_id, artifact_root=artifacts, receipt_outcome="failed")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve(),
            finalization={"outcome": "finalized", "independent_outcome": "failed",
                          "independent_artifact_id": str(written.receipt_id)})

    assert decision.outcome is RunOutcome.FAILED
    assert decision.reason == "independent_evaluation_failed"
    assert decision.independent.outcome == "failed" and decision.independent.calibrated is True
    # The public pass is kept beside the failure instead of being replaced by a label.
    assert decision.public_checks.verified_outcome == "passed"


def test_an_uncalibrated_independent_failure_is_not_reported_as_a_failure(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="uncalibrated")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    verifier_id, written = _seed_checkpointed_verifier(
        factory, run_id, artifact_root=artifacts, receipt_outcome="failed",
        receipt_calibrated=False)

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve(),
            finalization={"outcome": "finalized", "independent_outcome": "failed",
                          "independent_artifact_id": str(written.receipt_id)})

    # The evaluation ran but its result cannot be trusted, so neither failure nor public
    # only applies; the specific reason is kept instead.
    assert decision.outcome is RunOutcome.INCOMPLETE
    assert decision.reason == "public_passed_but_independent_failure_uncalibrated"
    assert decision.independent.outcome == "unavailable"
    assert decision.independent.verified is True


def test_a_checkpoint_whose_draft_cannot_be_read_is_not_a_public_pass(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="unverifiable-public")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _seed_checkpointed_verifier(factory, run_id, artifact_root=artifacts,
                                receipt_outcome="unavailable")

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=None,
            finalization={"outcome": "not_finalized",
                          "reason": "independent_evaluation_material_missing"})

    # Without a readable draft the public half is unverifiable, so no pass label applies.
    assert decision.outcome is RunOutcome.INCOMPLETE
    assert decision.public_checks.verified_outcome is None
    assert decision.independent.reason == "independent_evaluation_material_missing"


def test_a_checkpoint_that_disagrees_with_its_draft_is_not_a_verified_public_result(
    recovery_session_factory, tmp_path,
):
    """The recorded outcome is only accepted when the draft itself recomputes to it."""
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="checkpoint-disagrees")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    # The draft's review is failed, but the checkpoint claims the public checks passed.
    verifier_id, written = _seed_checkpointed_verifier(
        factory, run_id, artifact_root=artifacts, receipt_outcome="unavailable")
    import io

    from devflow_ai.artifacts import write_artifact_file
    from devflow_ai.verifier import SemanticReviewDraftRecord

    draft = SemanticReviewDraftRecord.model_validate({
        "record_type": "semantic_review_draft", "schema_version": 1,
        "run_id": str(run_id), "agent_execution_id": str(verifier_id),
        "selection": {
            "baseline_verifier_id": str(uuid4()), "baseline_call_id": str(uuid4()),
            "baseline_artifact_id": str(uuid4()), "patch_call_id": str(uuid4()),
            "current_call_id": str(uuid4()), "current_artifact_id": str(uuid4()),
            "diff_call_id": str(uuid4()), "command_name": "pytest",
            "target_tests": ["test_baseline.py::test_baseline"]},
        "review": {"outcome": "REVIEW", "result": "failed",
                   "reason": "Constructed fixture: the review says the patch is wrong.",
                   "citations": [{"source": "workspace_diff", "tool_call_id": str(uuid4()),
                                  "path": "x.py", "snippet": "return 1",
                                  "reason": "Constructed fixture citation.",
                                  "diff_side": "added"}]},
        "test_comparison": {"target_result": "passed", "regression_result": "passed"},
        "read_call_ids": [], "patch_artifact_id": str(uuid4()),
        "diff_artifact_id": str(uuid4()),
    })
    path, digest = _rewrite_artifact(
        artifacts.resolve(), written.draft_id, draft.model_dump_json().encode("utf-8"))
    with factory.begin() as session:
        artifact = session.get(Artifact, written.draft_id)
        artifact.content_ref, artifact.content_hash = str(path), digest

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve(),
            finalization={"outcome": "finalized", "independent_outcome": "unavailable",
                          "independent_artifact_id": str(written.receipt_id)})

    assert decision.public_checks.verified_outcome is None
    assert "does not match its own draft" in decision.public_checks.notes
    assert decision.outcome is RunOutcome.INCOMPLETE


def test_a_receipt_from_another_suite_version_is_not_a_verdict(
    recovery_session_factory, tmp_path,
):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="receipt-suite")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _verifier_id, written = _seed_checkpointed_verifier(
        factory, run_id, artifact_root=artifacts, receipt_outcome="failed")
    with factory.begin() as session:
        # The receipt names a different suite than the pack pins.
        artifact = session.get(Artifact, written.receipt_id)
        import io

        from devflow_ai.artifacts import write_artifact_file
        from devflow_ai.verifier import IndependentEvaluationRecord

        original = Path(artifact.content_ref).read_text(encoding="utf-8")
        tampered = IndependentEvaluationRecord.model_validate(
            {**json.loads(original), "suite_version": "some-other-suite"})
        path, digest = _rewrite_artifact(
            artifacts.resolve(), written.receipt_id,
            tampered.model_dump_json().encode("utf-8"))
        artifact.content_ref, artifact.content_hash = str(path), digest

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve(),
            finalization={"outcome": "finalized", "independent_outcome": "failed",
                          "independent_artifact_id": str(written.receipt_id)})

    assert decision.outcome is RunOutcome.INCOMPLETE
    assert decision.reason == "public_passed_but_suite_version_mismatch"
    assert decision.independent.verified is False


def test_a_receipt_for_another_draft_is_not_a_verdict(recovery_session_factory, tmp_path):
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="receipt-draft")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _verifier_id, written = _seed_checkpointed_verifier(
        factory, run_id, artifact_root=artifacts, receipt_outcome="failed")
    with factory.begin() as session:
        import io

        from devflow_ai.artifacts import write_artifact_file
        from devflow_ai.verifier import IndependentEvaluationRecord

        artifact = session.get(Artifact, written.receipt_id)
        original = Path(artifact.content_ref).read_text(encoding="utf-8")
        other = IndependentEvaluationRecord.model_validate(
            {**json.loads(original), "draft_artifact_id": str(uuid4())})
        path, digest = _rewrite_artifact(
            artifacts.resolve(), written.receipt_id, other.model_dump_json().encode("utf-8"))
        artifact.content_ref, artifact.content_hash = str(path), digest

    with factory() as session:
        decision = classify_outcome(
            session, run_id, deadline_fired=False, independent_suite_version="suite-1",
            artifact_root=artifacts.resolve(),
            finalization={"outcome": "finalized", "independent_outcome": "failed",
                          "independent_artifact_id": str(written.receipt_id)})

    assert decision.outcome is RunOutcome.INCOMPLETE
    # The existing loader rejects a receipt whose draft is not this Verifier's before the
    # entry can compare ids, so the stated reason is "unverifiable" — still not a verdict.
    assert decision.reason == "public_passed_but_receipt_unverifiable"
    assert decision.independent.verified is False


def test_an_evaluation_that_was_never_attempted_is_not_a_public_only_pass(
    recovery_session_factory, tmp_path,
):
    """Skipped or stopped-before-evaluation attempts keep `incomplete`, with the reason."""
    from devflow_ai.benchmark_record import RunOutcome
    from devflow_ai.benchmark_run import classify_outcome

    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="not-attempted")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _seed_checkpointed_verifier(factory, run_id, artifact_root=artifacts,
                                receipt_outcome="unavailable")

    for finalization, expected in (
        ({"outcome": "skipped", "reason": "not_attempted"},
         "public_passed_but_evaluation_not_attempted"),
        ({"outcome": "not_finalized", "reason": "independent_evaluation_material_missing"},
         "public_passed_but_independent_evaluation_material_missing"),
        (None, "public_passed_but_finalization_not_recorded"),
    ):
        with factory() as session:
            decision = classify_outcome(
                session, run_id, deadline_fired=False, independent_suite_version="suite-1",
                artifact_root=artifacts.resolve(), finalization=finalization)
        assert decision.outcome is RunOutcome.INCOMPLETE, expected
        assert decision.reason == expected
        assert decision.public_checks.verified_outcome == "passed"


def _seed_recorded_baseline_with_supervisor(
    factory, rediagnosis_id, *, marker=True, supervisor_after_baseline=True,
    baseline_outcome="BASELINE_RECORDED",
):
    """Seed one recorded re-plan baseline and one post-Planner Supervisor decision.

    These are row-level fixtures: no baseline is executed and no model is called, so this
    runs anywhere. ``marker`` selects the shape a real Supervisor records (it names the
    baseline it observed) versus the legacy shape whose phase has to be inferred from time.
    """
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        planner = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.PLANNER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=now - timedelta(seconds=6),
            input={}, output={"outcome": "PLAN_READY", "plan": {
                "approach": "Update the documentation", "target_paths": ["README.md"],
                "steps": ["Edit the section"], "scope_limits": "Only README.md",
                "verification": ["Review the diff"]}})
        session.add(planner)
        session.flush()
        baseline_handoff = Handoff(
            from_agent_execution_id=planner.id, to_agent_role=AgentRole.VERIFIER.value.lower(),
            context={"completed_work": "A baseline was requested", "blocker": None,
                     "evidence_refs": [], "decision_required": "Run the baseline",
                     "scope_impact": "Only README.md"})
        session.add(baseline_handoff)
        session.flush()
        baseline = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.VERIFIER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, handoff_id=baseline_handoff.id,
            created_at=now, input={"command_name": "pytest"},
            output={"outcome": baseline_outcome, "tool_call_id": str(uuid4()),
                    "report_artifact_id": str(uuid4())})
        session.add(baseline)
        session.flush()
        code_handoff = Handoff(
            from_agent_execution_id=planner.id, to_agent_role=AgentRole.CODER.value.lower(),
            context={"completed_work": "Plan prepared", "blocker": None,
                     "evidence_refs": [{"baseline_verifier_id": str(baseline.id)}],
                     "decision_required": "Implement", "scope_impact": "Only README.md"})
        session.add(code_handoff)
        session.flush()
        supervisor_input = {"planner_execution_id": str(planner.id)}
        if marker:
            supervisor_input["baseline_available"] = True
            supervisor_input["observed_baseline_verifier_id"] = str(baseline.id)
        created = (baseline.created_at + timedelta(seconds=2) if supervisor_after_baseline
                   else baseline.created_at - timedelta(seconds=2))
        supervisor = AgentExecution(
            run_id=rediagnosis_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, created_at=created,
            input=supervisor_input,
            output={"outcome": "CODE", "approved_handoff_id": str(code_handoff.id)})
        session.add(supervisor)
        session.flush()
        return SimpleNamespace(planner_id=planner.id, baseline_id=baseline.id,
                               supervisor_id=supervisor.id,
                               code_handoff_id=code_handoff.id)


def test_a_supervisor_that_recorded_its_baseline_is_post_baseline(
    recovery_session_factory,
):
    """The recorded observation decides the phase, not the row's timestamp."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="phase-marker")
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "phase-marker-key")
    seeded = _seed_recorded_baseline_with_supervisor(
        factory, rediagnosis_id, marker=True, supervisor_after_baseline=False)

    post = worker._load_replan_supervisor(
        rediagnosis_id, seeded.planner_id, phase="post_baseline")

    assert post is not None and post.id == seeded.supervisor_id
    # The same row is not the pre-baseline decision: its marker says it saw the baseline.
    assert worker._load_replan_supervisor(
        rediagnosis_id, seeded.planner_id, phase="pre_baseline") is None


def test_a_legacy_supervisor_is_classified_by_creation_time(recovery_session_factory):
    """Rows without the marker keep the documented time-order compatibility rule."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="phase-legacy")
    rediagnosis_id = _start_rediagnosis(factory, worker, task_id, run_id, "phase-legacy-key")
    after = _seed_recorded_baseline_with_supervisor(
        factory, rediagnosis_id, marker=False, supervisor_after_baseline=True)

    post = worker._load_replan_supervisor(
        rediagnosis_id, after.planner_id, phase="post_baseline")

    assert post is not None and post.id == after.supervisor_id
    assert worker._load_replan_supervisor(
        rediagnosis_id, after.planner_id, phase="pre_baseline") is None

    worker, task_id, run_id = create_running_run(factory, worker_id="phase-legacy-before")
    before_id = _start_rediagnosis(factory, worker, task_id, run_id, "phase-legacy-before-key")
    before = _seed_recorded_baseline_with_supervisor(
        factory, before_id, marker=False, supervisor_after_baseline=False)

    assert worker._load_replan_supervisor(
        before_id, before.planner_id, phase="post_baseline") is None
    pre = worker._load_replan_supervisor(
        before_id, before.planner_id, phase="pre_baseline")
    assert pre is not None and pre.id == before.supervisor_id


def test_a_baseline_execution_without_a_recorded_report_is_unavailable(
    recovery_session_factory, tmp_path,
):
    """Nothing recorded is a different answer from a recorded baseline that no longer verifies."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="baseline-no-report")
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "baseline-no-report-key")
    seeded = _seed_recorded_baseline_with_supervisor(
        factory, rediagnosis_id, marker=True,
        baseline_outcome="BASELINE_UNAVAILABLE")
    with factory() as session:
        verifier = session.get(AgentExecution, seeded.baseline_id)

    result = worker._replanned_baseline_result(
        rediagnosis_id, verifier, common={}, resumed=True,
        directories=RunDirectories(tmp_path / "workspace", tmp_path / "artifacts"),
        planner_execution_id=seeded.planner_id)

    assert result["outcome"] == "baseline_unavailable"
    assert result["reason"] == "baseline_evidence_unavailable"
    assert result["baseline_verifier_id"] == seeded.baseline_id


def test_a_recorded_baseline_whose_report_no_longer_verifies_is_invalid(
    recovery_session_factory, tmp_path,
):
    """A baseline that says it recorded something, but whose evidence is gone, is invalid."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="baseline-bad-report")
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "baseline-bad-report-key")
    seeded = _seed_recorded_baseline_with_supervisor(
        factory, rediagnosis_id, marker=True)
    with factory() as session:
        verifier = session.get(AgentExecution, seeded.baseline_id)

    result = worker._replanned_baseline_result(
        rediagnosis_id, verifier, common={}, resumed=True,
        directories=RunDirectories(tmp_path / "workspace", tmp_path / "artifacts"),
        planner_execution_id=seeded.planner_id)

    assert result["outcome"] == "baseline_unavailable"
    assert result["reason"] == "baseline_evidence_invalid"


def test_a_supervisor_citing_a_baseline_without_its_report_is_contradictory(
    recovery_session_factory,
):
    """Changing the cited baseline's outcome makes the Supervisor's observation false."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="baseline-contradiction")
    rediagnosis_id = _start_rediagnosis(
        factory, worker, task_id, run_id, "baseline-contradiction-key")
    seeded = _seed_recorded_baseline_with_supervisor(
        factory, rediagnosis_id, marker=True)
    with factory.begin() as session:
        session.get(AgentExecution, seeded.baseline_id).output = {
            "outcome": "BASELINE_UNAVAILABLE", "tool_call_id": None,
            "report_artifact_id": None}

    with pytest.raises(PlannerSupervisorContextError, match="observation is invalid"):
        worker._load_replan_supervisor(
            rediagnosis_id, seeded.planner_id, phase="pre_baseline")


def test_a_committed_coder_result_is_saved_without_its_supervisor(
    recovery_session_factory,
):
    """The stop-after-Coder report never starts or implies the Coder's Supervisor."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="coder-result-saved")
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        coder = AgentExecution(
            run_id=run_id, agent_role=AgentRole.CODER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={},
            output={"outcome": "PATCH_READY", "patch_artifact_id": str(uuid4()),
                    "summary": "applied"}, created_at=now)
        session.add(coder)
        session.flush()
        coder_id = coder.id

    with factory() as session:
        coder_row = session.get(AgentExecution, coder_id)
        result = worker._rediagnosis_coder_result(coder_row, common={}, resumed=False)
        assert result["outcome"] == "coder_result_saved"
        assert result["reason"] == "coder_result_committed"
        assert result["coder_outcome"] == "PATCH_READY"
        coder_row.output = {"outcome": "BLOCKED"}
        assert worker._rediagnosis_coder_result(
            coder_row, common={}, resumed=True)["outcome"] == "coder_result_saved"


def test_a_post_baseline_coder_result_stops_at_the_missing_supervisor_gateway(
    recovery_session_factory, tmp_path,
):
    """Without the Coder's Supervisor gateway the step saves the Coder result and stops."""
    factory = recovery_session_factory
    worker, task_id, run_id = create_running_run(factory, worker_id="coder-post-supervisor")
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        coder = AgentExecution(
            run_id=run_id, agent_role=AgentRole.CODER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={},
            output={"outcome": "BLOCKED", "completed_work": "Inspected the plan",
                    "blocker": "No safe change", "decision_required": "Replan",
                    "scope_impact": "Only README.md"}, created_at=now)
        session.add(coder)
        session.flush()
        coder_id = coder.id

    with factory() as session:
        result = worker._continue_rediagnosis_after_coder(
            run_id, coder=session.get(AgentExecution, coder_id), common={},
            directories=RunDirectories(tmp_path / "workspace", tmp_path / "artifacts"),
            supervisor_gateway=None, explorer_gateway=None, planner_gateway=None,
            verification_config=None, verifier_gateway=None, verifier_max_diff_bytes=None,
            max_read_bytes=1024, max_patch_bytes=4096, stop_event=Event(), resumed=False,
            coder_gateway=None)

    assert result["outcome"] == "code_pending"
    assert result["reason"] == "supervisor_config_missing"
    assert result["coder_execution_id"] == coder_id
    assert result["coder_outcome"] == "BLOCKED"
