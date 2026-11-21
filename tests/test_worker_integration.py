from devflow_ai.services import AdmissionRecord
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
from tempfile import TemporaryFile
from time import monotonic
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

import devflow_ai.coordinator as coordinator_module
import devflow_ai.harness as harness_module
import devflow_ai.worker as worker_module
from devflow_ai.artifacts import ToolArtifactResult, write_artifact_file
from devflow_ai.admission_dispatch import dispatch_one_admission
from devflow_ai.benchmark_task import create_benchmark_task
from devflow_ai.config import HeartbeatConfig, LeaseConfig
from devflow_ai.context import AgentReadEvidenceError, ContextBuilder, ReadEvidenceRef, VerifierPatchEvidenceError
from devflow_ai.coordinator import (
    AgentExecutionStateConflictError, CoderArtifactError, FailureDecision, HandoffAlreadyStartedError, HandoffApprovalError, HandoffContext, HandoffDecision, ModelTokenUsage,
    ModelUsageConflictError, RecoveryDecisionUnavailableError, RunCoordinator, RunExecutionDeniedError, RunLeaseNotExpiredError,
)
from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter
from devflow_ai.enums import AgentExecutionStatus, AgentRole, FailureCategory, RunStatus, TaskStatus, ToolCallStatus, VerificationResult
from devflow_ai.harness import (
    GitCommandFailed, GitCommandResult, GitCommandTimedOut, GitExecutionCancelledError, GitOutputPreview,
    GitHeadMismatchError, GitHeadOutputError,
)
from devflow_ai.models import AgentExecution, Artifact, FailureRecord, Handoff, Run, Task, ToolCall, Verification
from devflow_ai.orchestration import HandoffRoutingError, run_handoff_cycle
from devflow_ai.model_gateway import AdmissionModelGateway, ModelGateway, ModelReply, PlannerModelGateway
from devflow_ai.repositories import ToolCallRepository
from devflow_ai.policy import build_default_policy
from devflow_ai.runtime_environment import observe_current_python_environment
from devflow_ai.skills import REGISTERED_CHECK, SkillNotApplicable, check_diff_scope, run_registered_check, supports_diff_scope
from devflow_ai.task_pack import ScopeSpec, load_manifest, scope_evidence_sha256
from devflow_ai.services import TaskService, TaskStateConflictError
from devflow_ai.worker import (
    ExplorerResult, PlannerContextUnavailableError, PlannerModelBudgetUnavailableError,
    CoderContextUnavailableError, ExplorerSupervisorContextError, PlannerSupervisorContextError, RunClaim,
    RunDirectories, VerifierEvidenceSelection, Worker,
)
from devflow_ai.workspace_lock import (
    WorkspaceLockReceipt, WorkspaceLockUnavailableError, WorkspaceQuarantinedError, workspace_operation_lock,
)
from devflow_ai.verifier import (
    SemanticCitation, SemanticReviewDraft, SemanticReviewDraftRecord, VerificationEvidenceError, compare_recorded_pytest_reports, load_pytest_report_artifact,
    load_python_environment_observation, load_semantic_review_draft, load_verifier_command_evidence,
    require_test_comparison_sequence, validate_semantic_review_citations,
)


@pytest.fixture
def worker_audit_session_factory(run_session_factory):
    engine = run_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            for revision_id in (
                "c7e1a29b4d63", "d4f63c192ba8", "f83ab2d16c49",
                "ab6e1c30f42d", "e8c27b14a693", "4b79c6e21a50", "62eae84cebd1",
                "97f3a25cb190",
            ):
                scripts.get_revision(revision_id).module.upgrade()
    return run_session_factory


def create_eligible_task(session_factory, *, repo="example-repo", base_commit="a" * 40):
    with session_factory() as session:
        task, _ = TaskService(session).create_task(
            title="Worker entry test", description="Verify one claim from a separate entry point",
            repo=repo, idempotency_key=uuid4(),
        )
        task_id = task.id
    with session_factory() as session:
        # Simulated approved decision; no real Supervisor is invoked.
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit=base_commit, admission_record=AdmissionRecord(reason="test decision", evidence=["test fixture"]))
    return task_id


def create_test_baseline(factory, worker, run_id, planner_id, workspace, artifacts):
    """Create a real baseline report for Coder integration tests."""
    test_file = artifacts.parent / "test_baseline_fixture.py"
    test_file.write_text("def test_fixture(): assert True\n", encoding="utf-8")
    with factory() as session:
        coordinator = RunCoordinator(session)
        supervisor = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        handoff = coordinator.finish_agent_execution(run_id, worker.worker_id, agent_execution_id=supervisor,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision(from_agent_execution_id=planner_id, to_agent_role=AgentRole.VERIFIER,
                context=HandoffContext(completed_work="Plan prepared", blocker=None, evidence_refs=(),
                    decision_required="Run baseline", scope_impact="No modification")))
    result = worker.execute_baseline_verifier(run_id, handoff_id=handoff,
        directories=RunDirectories(workspace, artifacts), command_name="pytest",
        registry={"pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file))},
        stop_event=Event())
    assert result["valid"]
    return result["execution_id"]


def tracking_session_factory(run_session_factory):
    closed_sessions = []

    class TrackingSession(Session):
        def close(self):
            super().close()
            closed_sessions.append(self)

    factory = sessionmaker(bind=run_session_factory.kw["bind"], class_=TrackingSession)
    return factory, closed_sessions


@pytest.mark.parametrize("has_work", [False, True])
def test_worker_returns_detached_receipt_and_closes_session(run_session_factory, has_work):
    task_id = create_eligible_task(run_session_factory) if has_work else None
    factory, closed_sessions = tracking_session_factory(run_session_factory)
    claim = Worker(factory, "worker-entry-test").claim_once()
    assert len(closed_sessions) == 1
    assert not closed_sessions[0].in_transaction()
    if not has_work:
        assert claim is None
        return
    assert isinstance(claim, RunClaim)
    assert claim.task_id == task_id and claim.status == RunStatus.RUNNING
    # The caller can serialize the receipt after Session has already closed.
    assert json.loads(claim.model_dump_json())["run_id"] == str(claim.run_id)
    with run_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.RUNNING
        run = session.get(Run, claim.run_id)
        assert run.worker_id == claim.worker_id
        assert run.creation_idempotency_key == claim.creation_idempotency_key


def test_worker_closes_session_after_claim_failure(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        session.get(Task, task_id).policy_snapshot = {"invalid": True}
        session.commit()
    factory, closed_sessions = tracking_session_factory(run_session_factory)
    with pytest.raises(ValueError):
        Worker(factory, "worker-entry-test").claim_once()
    assert len(closed_sessions) == 1
    assert not closed_sessions[0].in_transaction()
    with run_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE
        assert not session.scalars(select(Run)).all()


@pytest.mark.parametrize("blocker", [None, "another-worker", "existing-artifact", "existing-workspace"])
def test_worker_prepares_run_directories_without_reusing_existing_paths(
    run_session_factory, tmp_path, blocker,
):
    create_eligible_task(run_session_factory)
    owner = Worker(run_session_factory, "directory-owner")
    claim = owner.claim_once()
    workspaces_root = tmp_path / "workspaces"
    artifacts_root = tmp_path / "artifacts"
    workspaces_root.mkdir()
    artifacts_root.mkdir()
    workspace = workspaces_root / str(claim.run_id)
    artifact = artifacts_root / str(claim.run_id)
    worker = owner
    if blocker == "another-worker":
        worker = Worker(run_session_factory, "different-worker")
        expected_error = RunExecutionDeniedError
    elif blocker in ("existing-artifact", "existing-workspace"):
        existing_path = artifact if blocker == "existing-artifact" else workspace
        existing_path.mkdir()
        (existing_path / "keep.txt").write_bytes(b"preserved")
        expected_error = FileExistsError

    if blocker is None:
        directories = worker.prepare_run_directories(
            claim.run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
        )
        assert directories.workspace_root == workspace.resolve()
        assert directories.artifact_root == artifact.resolve()
        assert workspace.is_dir() and artifact.is_dir()
        return

    with pytest.raises(expected_error):
        worker.prepare_run_directories(
            claim.run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
        )
    assert workspace.exists() == (blocker == "existing-workspace")
    assert artifact.exists() == (blocker == "existing-artifact")
    if blocker in ("existing-artifact", "existing-workspace"):
        assert (existing_path / "keep.txt").read_bytes() == b"preserved"


def create_local_repo(repo):
    repo.mkdir()
    (repo / "README.md").write_bytes(b"pinned content")
    for args in (
        ["init", "--quiet"],
        ["add", "README.md"],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=15)
    base_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=15)
    return base_commit.decode("ascii").strip()


@pytest.mark.parametrize("outcome", ["success", "lease-lost", "mismatch", "invalid-head", "truncated-head"])
def test_worker_initializes_and_audits_pinned_checkout(
    worker_audit_session_factory, tmp_path, monkeypatch, outcome,
):
    run_session_factory = worker_audit_session_factory
    repo = tmp_path / "repo"
    base_commit = create_local_repo(repo)
    task_id = create_eligible_task(run_session_factory, repo=str(repo), base_commit=base_commit)
    with run_session_factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 17},
        }
    factory, closed_sessions = tracking_session_factory(run_session_factory)
    worker = Worker(factory, "workspace-owner")
    claim = worker.claim_once()
    closed_sessions.clear()
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    original_checkout = coordinator_module.checkout_base_commit
    head_outputs = {
        "invalid-head": (base_commit + "\n\x00", False, "HEAD_INVALID_OUTPUT"),
        "truncated-head": (base_commit + "\r\n", True, "HEAD_OUTPUT_TRUNCATED"),
    }
    if outcome in head_outputs:
        original_git = harness_module._run_git
        head_output, truncated, _ = head_outputs[outcome]

        def run_git(args, **kwargs):
            if args[-2:] == ["rev-parse", "HEAD"]:
                preview = GitOutputPreview(head_output.encode("ascii"), b"diagnostic", truncated, False)
                return GitCommandResult(["git", *args], 0, preview)
            return original_git(args, **kwargs)

        monkeypatch.setattr(harness_module, "_run_git", run_git)

    def checkout_then_maybe_expire(workspace_root, pinned_commit, *, timeout_seconds, stop_event):
        assert len(closed_sessions) == 2
        assert all(not session.in_transaction() for session in closed_sessions)
        assert pinned_commit == base_commit and timeout_seconds == 17
        with run_session_factory() as observer:
            requested = observer.scalars(select(ToolCall).where(
                ToolCall.run_id == claim.run_id, ToolCall.tool_name == "git_checkout",
            )).one()
            assert requested.status == ToolCallStatus.REQUESTED
        actual_commit = original_checkout(
            workspace_root, pinned_commit, timeout_seconds=timeout_seconds, stop_event=stop_event,
        )
        if outcome == "lease-lost":
            with run_session_factory.begin() as session:
                session.get(Run, claim.run_id).lease_expires_at = func.clock_timestamp()
        if outcome == "mismatch":
            raise GitHeadMismatchError(base_commit, "f" * len(base_commit), 0)
        return actual_commit

    monkeypatch.setattr(coordinator_module, "checkout_base_commit", checkout_then_maybe_expire)
    if outcome == "lease-lost":
        with pytest.raises(RunExecutionDeniedError, match="expired"):
            worker.initialize_run_workspace(claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)
    elif outcome == "mismatch":
        with pytest.raises(GitHeadMismatchError):
            worker.initialize_run_workspace(claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)
    elif outcome in head_outputs:
        with pytest.raises(GitHeadOutputError):
            worker.initialize_run_workspace(claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)
    else:
        directories, verified_commit = worker.initialize_run_workspace(
            claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts,
        )
        assert directories.workspace_root == workspaces.resolve() / str(claim.run_id)
        assert directories.artifact_root == artifacts.resolve() / str(claim.run_id)
        assert verified_commit == base_commit
    assert len(closed_sessions) == 3
    assert (workspaces / str(claim.run_id) / "README.md").read_bytes() == b"pinned content"
    with run_session_factory() as session:
        calls = {
            call.tool_name: call
            for call in session.scalars(select(ToolCall).where(ToolCall.run_id == claim.run_id))
        }
        call = calls["git_clone"]
        assert (call.tool_name, call.agent_execution_id, call.status, call.exit_code) == (
            "git_clone", None, ToolCallStatus.SUCCEEDED, 0,
        )
        checkout = calls["git_checkout"]
        assert checkout.agent_execution_id is None and checkout.args == {"base_commit": base_commit}
        expected = {
            "success": (ToolCallStatus.SUCCEEDED, 0, None, base_commit),
            "lease-lost": (ToolCallStatus.REQUESTED, None, None, None),
            "mismatch": (ToolCallStatus.FAILED, 0, "HEAD_MISMATCH", "f" * len(base_commit)),
        }
        for name, (output, _, reason) in head_outputs.items():
            expected[name] = (ToolCallStatus.FAILED, 0, reason, output.replace("\x00", "\ufffd"))
        assert (checkout.status, checkout.exit_code, checkout.reason, checkout.stdout_excerpt) == expected[outcome]
        if outcome in head_outputs:
            assert checkout.stderr_excerpt == "diagnostic"


def test_worker_refuses_legacy_run_without_pinned_commit(run_session_factory, tmp_path):
    task_id = create_eligible_task(run_session_factory)
    worker = Worker(run_session_factory, "workspace-owner")
    claim = worker.claim_once()
    with run_session_factory.begin() as session:
        session.get(Task, task_id).base_commit = None
    with pytest.raises(TaskStateConflictError, match="no pinned Task base commit"):
        worker.initialize_run_workspace(
            claim.run_id, workspaces_root=tmp_path / "workspaces", artifacts_root=tmp_path / "artifacts",
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("outcome", "error_type", "status", "exit_code", "reason"), [
    ("failed", GitCommandFailed, ToolCallStatus.FAILED, 128, "GIT_CLONE_FAILED"),
    ("timeout", GitCommandTimedOut, ToolCallStatus.TIMED_OUT, None, "GIT_CLONE_TIMEOUT"),
    ("lease-lost", RunExecutionDeniedError, ToolCallStatus.REQUESTED, None, None),
])
def test_worker_clone_audit_commits_only_while_owned(
    worker_audit_session_factory, tmp_path, monkeypatch, outcome, error_type, status, exit_code, reason,
):
    factory = worker_audit_session_factory
    create_eligible_task(factory)
    worker = Worker(factory, "clone-owner")
    claim = worker.claim_once()
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    artifact_id = uuid4()

    def clone(repo, workspace_root, *, timeout_seconds, stop_event, artifact_root):
        with factory.begin() as observer:
            requested = observer.scalars(select(ToolCall).where(ToolCall.run_id == claim.run_id)).one()
            assert requested.status == ToolCallStatus.REQUESTED and requested.agent_execution_id is None
            assert requested.args == {"checkout": False}
            if outcome == "lease-lost":
                observer.get(Run, claim.run_id).lease_expires_at = func.clock_timestamp()
        content = b"complete diagnostic"
        path, digest = write_artifact_file(artifact_root, artifact_id, BytesIO(content), chunk_size=4)
        artifact = ToolArtifactResult(
            id=artifact_id, kind="tool_stderr", content_ref=str(path), content_hash=digest,
        )
        preview = GitOutputPreview(b"", b"complete", False, True, (artifact,))
        if outcome == "failed":
            raise GitCommandFailed(GitCommandResult(["git", "clone"], 128, preview))
        if outcome == "timeout":
            raise GitCommandTimedOut(subprocess.TimeoutExpired(["git", "clone"], timeout_seconds), preview)
        return GitCommandResult(["git", "clone"], 0, preview)

    monkeypatch.setattr(coordinator_module, "clone_repository", clone)
    monkeypatch.setattr(
        coordinator_module, "checkout_base_commit", lambda *args, **kwargs: pytest.fail("unexpected checkout"),
    )
    with pytest.raises(error_type):
        worker.initialize_run_workspace(claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)

    with factory() as observer:
        call = observer.scalars(select(ToolCall).where(ToolCall.run_id == claim.run_id)).one()
        assert (call.status, call.exit_code, call.reason) == (status, exit_code, reason)
        artifact = observer.get(Artifact, artifact_id)
        if outcome == "lease-lost":
            assert artifact is None and call.stderr_excerpt is None
        else:
            assert call.stderr_excerpt == "complete"
            assert (artifact.tool_call_id, artifact.agent_execution_id, artifact.kind) == (
                call.id, None, "tool_stderr",
            )
            assert Path(artifact.content_ref).read_bytes() == b"complete diagnostic"


@pytest.mark.parametrize("outcome", ["success", "processor-error", "lease-lost", "git-cancelled"])
def test_worker_heartbeat_covers_processing_and_stops_on_exit(
    run_session_factory, tmp_path, monkeypatch, outcome,
):
    create_eligible_task(run_session_factory)
    worker = Worker(
        run_session_factory, "heartbeat-owner",
        LeaseConfig(heartbeat_interval_seconds=1, lease_duration_seconds=10),
    )
    claim = worker.claim_once()
    directories = RunDirectories(tmp_path / "workspace", tmp_path / "artifact")
    renewed = Event()
    heartbeats = []
    lost_lease = outcome in ("lease-lost", "git-cancelled")

    class ObservedHeartbeat(worker_module.RunHeartbeat):
        def start(self):
            heartbeats.append(self)
            return super().start()

        def _renew_once(self):
            receipt = super()._renew_once()
            renewed.set()
            return receipt

    def initialize_run_workspace(*args, stop_event, **kwargs):
        heartbeat = heartbeats[0]
        assert heartbeat.is_alive()
        assert stop_event is heartbeat.execution_stop_event
        if lost_lease:
            with run_session_factory.begin() as session:
                session.get(Run, claim.run_id).lease_expires_at = func.clock_timestamp()
            assert stop_event.wait(6)
            if outcome == "git-cancelled":
                raise GitExecutionCancelledError("Git execution cancelled before process start")
        return directories, "a" * 40

    def process_run(prepared, actual_commit, stop_event):
        assert not lost_lease
        assert prepared is directories and actual_commit == "a" * 40
        assert stop_event is heartbeats[0].execution_stop_event
        assert renewed.wait(6)
        assert not stop_event.is_set()
        if outcome == "processor-error":
            raise RuntimeError("processor failed")
        return "processed"

    monkeypatch.setattr(worker_module, "RunHeartbeat", ObservedHeartbeat)
    monkeypatch.setattr(worker, "initialize_run_workspace", initialize_run_workspace)

    def execute():
        return worker.execute_run(
            claim.run_id, workspaces_root=directories.workspace_root, artifacts_root=directories.artifact_root,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=1, shutdown_timeout_seconds=3),
            process_run=process_run,
        )

    if lost_lease:
        with pytest.raises(RunExecutionDeniedError, match="expired"):
            execute()
    elif outcome == "processor-error":
        with pytest.raises(RuntimeError, match="processor failed"):
            execute()
    else:
        assert execute() == "processed"
    assert len(heartbeats) == 1
    heartbeat = heartbeats[0]
    assert not heartbeat.is_alive()
    assert heartbeat.heartbeat_stop_event.is_set()
    if not lost_lease:
        with run_session_factory() as session:
            assert session.get(Run, claim.run_id).last_heartbeat_at == heartbeat.last_renewal.last_heartbeat_at


def test_bound_agent_read_file_uses_fresh_sessions_and_stops(worker_audit_session_factory, tmp_path):
    factory = worker_audit_session_factory
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }

    tracked_factory, closed_sessions = tracking_session_factory(factory)
    worker = Worker(tracked_factory, "reader")
    claim = worker.claim_once()
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    directories = worker.prepare_run_directories(
        claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts,
    )
    (directories.workspace_root / "auth.py").write_bytes(b"auth data")
    (directories.workspace_root / "notes.txt").write_bytes(b"ok")
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            claim.run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={},
        )
    closed_sessions.clear()

    stop_event = Event()
    read_file = worker.build_agent_read_file(
        claim.run_id, agent_execution_id=execution_id, directories=directories,
        max_bytes=4, stop_event=stop_event,
    )
    first = read_file("auth.py")
    assert len(closed_sessions) == 1 and not closed_sessions[0].in_transaction()
    assert (first.preview, first.truncated) == ("auth", True)
    assert first.ends_with_newline is None
    second = read_file("notes.txt")
    assert len(closed_sessions) == 2 and closed_sessions[0] is not closed_sessions[1]
    assert not closed_sessions[1].in_transaction()
    assert (second.preview, second.truncated) == ("ok", False)
    assert second.ends_with_newline is False

    stop_event.set()
    with pytest.raises(RunExecutionDeniedError, match="asked to stop"):
        read_file("auth.py")
    assert len(closed_sessions) == 2
    with factory() as observer:
        calls = observer.scalars(select(ToolCall).where(ToolCall.run_id == claim.run_id)).all()
        assert {call.id for call in calls} == {first.tool_call_id, second.tool_call_id}
        assert {call.agent_execution_id for call in calls} == {execution_id}
        artifact = observer.get(Artifact, first.artifact_ids[0])
        assert Path(artifact.content_ref).read_bytes() == b"auth data"


@pytest.mark.parametrize("failure", ["lock_lost", "stopped"])
def test_read_result_is_not_committed_after_lock_loss_or_stop(worker_audit_session_factory, tmp_path, monkeypatch, failure):
    factory = worker_audit_session_factory
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "VERIFIER": ["read_file"],
            }},
        }
    worker = Worker(factory, "read-guard-worker")
    run_id = worker.claim_once().run_id
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "sample.py").write_text("example\n", encoding="utf-8")
    stop_event = Event()
    if failure == "lock_lost":
        def lost_lock(_receipt, _session):
            raise WorkspaceLockUnavailableError("simulated lost lock")
        monkeypatch.setattr(WorkspaceLockReceipt, "assert_held", lost_lock)
        expected = WorkspaceLockUnavailableError
    else:
        original = coordinator_module.read_text_file_with_artifact
        def stop_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            stop_event.set()
            return result
        monkeypatch.setattr(coordinator_module, "read_text_file_with_artifact", stop_after_read)
        expected = GitExecutionCancelledError
    read = worker.build_agent_read_file(
        run_id, agent_execution_id=execution_id, directories=RunDirectories(workspace, artifacts),
        max_bytes=4096, stop_event=stop_event,
    )
    with pytest.raises(expected):
        read("sample.py")
    with factory() as observer:
        call = observer.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution_id)).one()
        assert call.status == ToolCallStatus.REQUESTED
        assert call.stdout_excerpt is None
        assert observer.scalars(select(Artifact).where(Artifact.tool_call_id == call.id)).all() == []


@pytest.mark.parametrize("outcome", ["success", "failed", "stopped"])
def test_worker_runs_explorer_from_checkout_to_committed_result(worker_audit_session_factory, tmp_path, outcome):
    factory = worker_audit_session_factory
    repo = tmp_path / "repo"
    base_commit = create_local_repo(repo)
    task_id = create_eligible_task(factory, repo=str(repo), base_commit=base_commit)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }
    worker = Worker(factory, "explorer-worker")
    claim = worker.claim_once()
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    agent_input = {"issue": "inspect the README", "base_commit": base_commit}

    def process(directories, actual_commit, stop_event):
        assert actual_commit == base_commit

        def explore(explorer_input, read_file):
            assert explorer_input == agent_input
            with factory() as observer:
                execution = observer.scalars(select(AgentExecution).where(
                    AgentExecution.run_id == claim.run_id,
                )).one()
                assert execution.status == AgentExecutionStatus.RUNNING
                assert execution.input == agent_input

            if outcome == "failed":
                receipt = read_file("absent.txt")
                assert receipt.status == ToolCallStatus.FAILED
                return ExplorerResult(
                    status=AgentExecutionStatus.FAILED,
                    output={"tool_call_id": str(receipt.tool_call_id), "reason": receipt.reason},
                )

            receipt = read_file("README.md")
            assert receipt.preview == "pinned" and receipt.truncated
            if outcome == "stopped":
                stop_event.set()
            return ExplorerResult(
                status=AgentExecutionStatus.SUCCEEDED,
                output={"preview": receipt.preview, "summary": "Inspected the complete file"},
                read_evidence=(ReadEvidenceRef(path="README.md", tool_call_id=receipt.tool_call_id),),
            )

        return worker.execute_explorer(
            claim.run_id, directories=directories, agent_input=agent_input,
            max_bytes=6, stop_event=stop_event, explore=explore,
        )

    def execute():
        return worker.execute_run(
            claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=1, shutdown_timeout_seconds=3),
            process_run=process,
        )

    if outcome == "stopped":
        with pytest.raises(RunExecutionDeniedError, match="asked to stop"):
            execute()
    else:
        execution_id = execute()

    with factory() as observer:
        execution = observer.scalars(select(AgentExecution).where(AgentExecution.run_id == claim.run_id)).one()
        call = observer.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution.id)).one()
        assert call.run_id == claim.run_id and call.tool_name == "read_file"
        assert execution.agent_role == "explorer" and execution.llm_usage is None
        planner_exploration = ContextBuilder(observer).build_planner_exploration(
            claim.run_id, explorer_execution_id=execution.id,
        )
        if outcome == "stopped":
            assert execution.status == AgentExecutionStatus.RUNNING and execution.output is None
        else:
            assert execution.id == execution_id
            assert execution.status == {
                "success": AgentExecutionStatus.SUCCEEDED, "failed": AgentExecutionStatus.FAILED,
            }[outcome]
            if outcome == "success":
                observed_read = {
                    "path": "README.md", "tool_call_id": str(call.id), "truncated": True,
                    "start_line": 1, "end_line": 1, "total_lines": None, "has_more": True,
                    "byte_truncated": True, "partial_line": True,
                }
                assert execution.output["read_evidence"] == [observed_read]
                assert planner_exploration == {
                    "explorer_execution_id": str(execution.id),
                    "explorer_output": {"preview": "pinned", "summary": "Inspected the complete file"},
                    "read_evidence": [observed_read],
                }
            else:
                assert execution.output["tool_call_id"] == str(call.id)
                assert "read_evidence" not in execution.output
        if outcome != "success":
            assert planner_exploration is None


@pytest.mark.parametrize("role", [AgentRole.EXPLORER, AgentRole.CODER, AgentRole.VERIFIER])
def test_a_ranged_read_records_the_window_it_actually_covered(
    worker_audit_session_factory, tmp_path, role,
):
    """A line range is how a large file's later part is reached without resending its head."""
    factory = worker_audit_session_factory
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], role.value: ["read_file"],
            }},
        }
    worker = Worker(factory, "range-reader")
    claim = worker.claim_once()
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    directories = worker.prepare_run_directories(
        claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)
    body = "".join(f"line {number}\n" for number in range(1, 41))
    (directories.workspace_root / "core.py").write_bytes(body.encode("utf-8"))
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            claim.run_id, worker.worker_id, role=role, agent_input={})
    read_file = worker.build_agent_read_file(
        claim.run_id, agent_execution_id=execution_id, directories=directories,
        max_bytes=4096, stop_event=Event())

    middle = read_file("core.py", 20, 5)
    assert middle.preview == "line 20\nline 21\nline 22\nline 23\nline 24\n"
    # It is not the whole file, so the read is marked truncated whichever way it stops.
    assert middle.truncated is True
    # The receipt separates what was asked for from what was actually read.
    assert (middle.requested_offset, middle.requested_limit) == (20, 5)
    assert (middle.start_line, middle.end_line, middle.total_lines) == (20, 24, 40)
    assert (middle.has_more, middle.byte_truncated, middle.partial_line) == (True, False, False)

    everything = read_file("core.py", 1, 40)
    assert everything.truncated is False
    assert everything.preview == body
    assert (everything.start_line, everything.end_line, everything.total_lines) == (1, 40, 40)
    assert (everything.has_more, everything.byte_truncated) == (False, False)

    past_the_end = read_file("core.py", 100, 5)
    assert (past_the_end.preview, past_the_end.truncated) == ("", True)

    with factory() as observer:
        calls = observer.scalars(select(ToolCall).where(
            ToolCall.run_id == claim.run_id).order_by(ToolCall.created_at)).all()
        assert [(call.args.get("offset"), call.args.get("limit")) for call in calls] == [
            (20, 5), (1, 40), (100, 5)]
        assert calls[0].args["observed"] == {
            "start_line": 20, "end_line": 24, "total_lines": 40,
            "has_more": True, "byte_truncated": False, "partial_line": False,
        }
        # The path boundary is unchanged by the range: outside the workspace is still denied.
        escaped = read_file("../outside.py", 1, 5)
        assert escaped.status == ToolCallStatus.DENIED
        assert escaped.reason == "WORKSPACE_PATH_DENIED"


@pytest.mark.parametrize("scenario", ["success", "forged_reference", "tool_budget", "model_timeout"])
def test_worker_executes_one_model_directed_explorer_read(worker_audit_session_factory, tmp_path, scenario):
    factory = worker_audit_session_factory
    repo = tmp_path / "repo"
    base_commit = create_local_repo(repo)
    task_id = create_eligible_task(factory, repo=str(repo), base_commit=base_commit)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "budget": {
                **snapshot["budget"], "max_tool_calls_per_run": 2 if scenario == "tool_budget" else 3,
            },
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }
    worker = Worker(factory, "explorer-model-worker")
    run_id = worker.claim_once().run_id
    workspaces = tmp_path / "workspaces"
    artifacts = tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    turns = []

    def generate(prompt, output_limit):
        assert output_limit == 200
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        turns.append(data)
        if scenario == "model_timeout":
            raise TimeoutError("simulated Explorer model timeout")
        if len(turns) == 1:
            output = {"path": "README.md"}
        else:
            receipt = data["read"]
            assert receipt["preview"] == "pinned" and receipt["truncated"] is True
            output = {
                "summary": "The README preview begins with pinned.",
                "read_evidence": {
                    "path": receipt["path"],
                    "tool_call_id": str(uuid4()) if scenario == "forged_reference" else receipt["tool_call_id"],
                },
            }
        return ModelReply(json.dumps(output), ModelTokenUsage(input_tokens=100, output_tokens=40))

    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 300,
        generate=generate, max_output_tokens=200,
    )

    def process(directories, actual_commit, stop_event):
        return worker.execute_explorer(
            run_id, directories=directories, agent_input={"issue": "Inspect the README", "base_commit": actual_commit},
            max_bytes=6, stop_event=stop_event, model_gateway=gateway,
        )

    def execute():
        return worker.execute_run(
            run_id, workspaces_root=workspaces, artifacts_root=artifacts,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=1, shutdown_timeout_seconds=3),
            process_run=process,
        )

    if scenario == "model_timeout":
        with pytest.raises(TimeoutError, match="simulated Explorer model timeout"):
            execute()
    else:
        execution_id = execute()
    with factory() as observer:
        execution = observer.scalars(select(AgentExecution).where(AgentExecution.run_id == run_id)).one()
        calls = observer.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution.id)).all()
        assert len(turns) == (1 if scenario in ("tool_budget", "model_timeout") else 2)
        assert len(execution.llm_usage["model_requests"]) == len(turns)
        if scenario == "model_timeout":
            reservation, = execution.llm_usage["model_requests"].values()
            assert reservation["actual_usage"] is None
        else:
            assert execution.id == execution_id
        if scenario == "success":
            call, = calls
            assert execution.status == AgentExecutionStatus.SUCCEEDED
            planner_input = ContextBuilder(observer).build_planner_input(run_id, explorer_execution_id=execution_id)
            assert planner_input["read_evidence"] == [
                {"path": "README.md", "tool_call_id": str(call.id), "truncated": True, "preview": "pinned",
                 "ends_with_newline": None, "preview_chars": 6, "preview_clipped": False,
                 "start_line": 1, "end_line": 1, "total_lines": None, "has_more": True,
                 "byte_truncated": True, "partial_line": True},
            ]
        else:
            assert len(calls) == (1 if scenario == "forged_reference" else 0)
            assert execution.status == AgentExecutionStatus.FAILED
            assert execution.output == {"reason": {
                "tool_budget": "EXPLORER_TOOL_BUDGET_UNAVAILABLE",
                "forged_reference": "EXPLORER_READ_REFERENCE_INVALID",
                "model_timeout": "EXPLORER_EXECUTION_ERROR",
            }[scenario]}


def test_explorer_rejects_unverified_read_evidence(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "explorer-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    create_eligible_task(factory)
    other_run_id = worker.claim_once().run_id
    path = "README.md"

    def start(run_id):
        with factory() as session:
            return RunCoordinator(session).start_agent_execution(
                run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={},
            )

    execution_id = start(run_id)
    other_execution_id = start(run_id)
    foreign_execution_id = start(other_run_id)

    with factory.begin() as session:
        calls = ToolCallRepository(session)

        def save_call(run_id, execution_id, tool_name="read_file", status=ToolCallStatus.SUCCEEDED):
            call_id = calls.add_requested(execution_id, tool_name, {"path": path}, run_id=run_id)
            calls.save_result(call_id, status=status)
            return call_id

        matching_id = save_call(run_id, execution_id)
        save_call(run_id, execution_id)  # A successful read that the Explorer did not cite.
        invalid_refs = [
            ReadEvidenceRef(path="wrong.md", tool_call_id=matching_id),
            ReadEvidenceRef(path=path, tool_call_id=uuid4()),
            ReadEvidenceRef(path=path, tool_call_id=save_call(run_id, other_execution_id)),
            ReadEvidenceRef(path=path, tool_call_id=save_call(other_run_id, foreign_execution_id)),
            ReadEvidenceRef(path=path, tool_call_id=save_call(run_id, execution_id, tool_name="search")),
            ReadEvidenceRef(path=path, tool_call_id=save_call(run_id, execution_id, status=ToolCallStatus.FAILED)),
            ReadEvidenceRef(path=path, tool_call_id=save_call(run_id, execution_id, status=ToolCallStatus.DENIED)),
        ]

    matching_ref = ReadEvidenceRef(path=path, tool_call_id=matching_id)

    def finish(output, references):
        with factory() as session:
            RunCoordinator(session).finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=execution_id,
                status=AgentExecutionStatus.SUCCEEDED, output=output, llm_usage=None,
                read_evidence=references,
            )

    for reference in invalid_refs:
        with pytest.raises(AgentReadEvidenceError, match=str(reference.tool_call_id)):
            finish({}, (matching_ref, reference))
    with pytest.raises(AgentReadEvidenceError, match="separately"):
        finish({"read_evidence": [matching_ref.model_dump(mode="json")]}, ())

    with factory() as observer:
        execution = observer.get(AgentExecution, execution_id)
        assert execution.status == AgentExecutionStatus.RUNNING
        assert execution.output is None
    finish({}, (matching_ref,))
    with factory() as observer:
        execution = observer.get(AgentExecution, execution_id)
        assert execution.status == AgentExecutionStatus.SUCCEEDED
        assert execution.output == {
            "read_evidence": [{"path": path, "tool_call_id": str(matching_id), "truncated": False}],
        }
    with factory.begin() as session:
        execution = session.get(AgentExecution, execution_id)
        execution.output = {"read_evidence": [{"path": path, "tool_call_id": str(matching_id)}]}
    with factory() as observer:
        builder = ContextBuilder(observer)
        assert builder.build_planner_exploration(other_run_id, explorer_execution_id=execution_id) is None
        assert builder.build_planner_exploration(run_id, explorer_execution_id=execution_id) == {
            "explorer_execution_id": str(execution_id),
            "explorer_output": {},
            "read_evidence": [{"path": path, "tool_call_id": str(matching_id), "truncated": False}],
        }


@pytest.mark.parametrize("outcome", ["missing", "unfinished", "plan", "gap", "exception", "invalid", "stopped"])
def test_worker_validates_context_before_running_planner(worker_audit_session_factory, outcome):
    factory = worker_audit_session_factory
    worker = Worker(factory, "planner-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    explorer_execution_id = uuid4()
    report = {"summary": "The service has multiple entry points"}
    stop_event = Event()

    if outcome != "missing":
        with factory() as session:
            coordinator = RunCoordinator(session)
            explorer_execution_id = coordinator.start_agent_execution(
                run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={},
            )
            if outcome != "unfinished":
                coordinator.finish_agent_execution(
                    run_id, worker.worker_id, agent_execution_id=explorer_execution_id,
                    status=AgentExecutionStatus.SUCCEEDED, output=report, llm_usage=None,
                )

    expected_input = {
        "issue": {
            "title": "Worker entry test",
            "description": "Verify one claim from a separate entry point",
        },
        "base_commit": "a" * 40,
        "explorer_execution_id": str(explorer_execution_id),
        "explorer_output": report,
        "read_evidence": [],
    }
    plan_ready = {
        "outcome": "PLAN_READY",
        "plan": {
            "approach": "Check permissions in the service",
            "target_paths": ["auth.py"],
            "steps": ["Guard task updates"],
            "scope_limits": "Permission checks for task updates only",
            "verification": ["Exercise both known entry points"],
        },
    }
    needs_evidence = {
        "outcome": "NEEDS_EVIDENCE",
        "evidence_gap": "Locate the callers that bypass the request handler",
        "affected_decision": "Choose where to enforce permissions",
    }
    executions = select(AgentExecution).where(
        AgentExecution.run_id == run_id, AgentExecution.agent_role == "planner",
    )

    def plan(agent_input):
        assert outcome not in ("missing", "unfinished")
        assert agent_input == expected_input
        with factory() as observer:
            execution = observer.scalars(executions).one()
            assert execution.status == AgentExecutionStatus.RUNNING
            assert execution.input == expected_input
        if outcome == "exception":
            raise RuntimeError("simulated Planner failure")
        if outcome == "invalid":
            return {"outcome": "NEEDS_EVIDENCE"}
        if outcome == "stopped":
            stop_event.set()
        return needs_evidence if outcome == "gap" else plan_ready

    def execute():
        return worker.execute_planner(
            run_id, explorer_execution_id=explorer_execution_id, stop_event=stop_event, plan=plan,
        )

    if outcome in ("missing", "unfinished"):
        with pytest.raises(PlannerContextUnavailableError):
            execute()
        with factory() as observer:
            assert observer.scalars(executions).all() == []
        return
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="simulated Planner failure"):
            execute()
    elif outcome == "invalid":
        with pytest.raises(ValidationError):
            execute()
    elif outcome == "stopped":
        with pytest.raises(RunExecutionDeniedError, match="asked to stop"):
            execute()
    else:
        execution_id = execute()

    with factory() as observer:
        execution = observer.scalars(executions).one()
        if outcome in ("exception", "invalid"):
            assert execution.status == AgentExecutionStatus.FAILED
            assert execution.output == {"reason": {
                "exception": "PLANNER_EXECUTION_ERROR",
                "invalid": "PLANNER_OUTPUT_INVALID",
            }[outcome]}
        elif outcome == "stopped":
            assert execution.status == AgentExecutionStatus.RUNNING
            assert execution.output is None
        else:
            assert execution.id == execution_id
            assert execution.status == AgentExecutionStatus.SUCCEEDED
            assert execution.output == (needs_evidence if outcome == "gap" else plan_ready)


@pytest.mark.parametrize(("scenario", "gateway_calls"), [
    ("repaired", 1),
    ("still_invalid", 1),
    ("legacy", 0),
    ("zero_tokens", 0),
    ("gateway_denies", 1),
    ("unknown_balance", 1),
    ("timeout", 1),
    ("late_reply", 1),
    ("stopped", 0),
])
def test_worker_limits_planner_output_repairs(worker_audit_session_factory, scenario, gateway_calls):
    factory = worker_audit_session_factory
    worker = Worker(factory, "planner-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        budget = dict(task.policy_snapshot["budget"])
        budget["max_llm_tokens_per_run"] = 320 if scenario == "gateway_denies" else 500
        if scenario == "legacy":
            del budget["max_output_repairs_per_agent_execution"]
        if scenario == "zero_tokens":
            budget["max_llm_tokens_per_run"] = 0
        task.policy_snapshot = {**task.policy_snapshot, "budget": budget}
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        explorer_execution_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={},
        )
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=explorer_execution_id,
            status=AgentExecutionStatus.SUCCEEDED, output={},
            llm_usage={"source": "unknown"} if scenario == "unknown_balance" else None,
        )

    stop_event = Event()
    invalid = {"outcome": "NEEDS_EVIDENCE", "evidence_gap": "Locate the service callers"}
    valid = {**invalid, "affected_decision": "Choose where to enforce permissions"}
    requests = []
    generated = []
    prompt = "Repair the Planner output using the input, invalid output, and validation errors."

    def plan(_):
        if scenario == "stopped":
            stop_event.set()
        return invalid

    def build_prompt(request):
        requests.append(request)
        return prompt

    def estimate_input_tokens(actual_prompt):
        assert actual_prompt == prompt
        return 320

    def generate(actual_prompt, max_output_tokens):
        generated.append((actual_prompt, max_output_tokens))
        with factory.begin() as session:
            execution = session.get(AgentExecution, requests[-1].agent_execution_id)
            entry, = execution.llm_usage["model_requests"].values()
            assert entry["reserved_tokens"] == 500 and entry["actual_usage"] is None
            if scenario == "late_reply":
                session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
        if scenario == "timeout":
            raise TimeoutError("simulated provider timeout")
        return ModelReply(
            output=invalid if scenario == "still_invalid" else valid,
            usage=ModelTokenUsage(input_tokens=300, output_tokens=100),
        )

    gateway = PlannerModelGateway(
        session_factory=factory, worker_id=worker.worker_id,
        build_initial_prompt=lambda _: "unused", build_repair_prompt=build_prompt,
        estimate_input_tokens=estimate_input_tokens,
        generate=generate, max_output_tokens=200,
    )

    def execute():
        return worker.execute_planner(
            run_id, explorer_execution_id=explorer_execution_id, stop_event=stop_event,
            plan=plan, repair_under_budget=gateway.repair,
        )

    if scenario == "repaired":
        execution_id = execute()
    elif scenario == "timeout":
        with pytest.raises(TimeoutError, match="simulated provider timeout"):
            execute()
    elif scenario == "late_reply":
        with pytest.raises(RunExecutionDeniedError, match="lease or duration budget"):
            execute()
    elif scenario == "stopped":
        with pytest.raises(RunExecutionDeniedError, match="asked to stop"):
            execute()
    else:
        with pytest.raises(ValidationError):
            execute()

    assert len(requests) == gateway_calls
    sent = scenario in ("repaired", "still_invalid", "timeout", "late_reply")
    assert generated == ([(prompt, 180)] if sent else [])
    with factory() as observer:
        execution = observer.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "planner",
        )).one()
        for request in requests:
            assert request.run_id == run_id and request.agent_execution_id == execution.id
            assert request.agent_input == execution.input
            assert request.invalid_output == invalid
            assert request.validation_errors[0]["loc"] == ("NEEDS_EVIDENCE", "affected_decision")
            assert "input" not in request.validation_errors[0]
        if scenario == "repaired":
            assert execution.id == execution_id
            assert execution.status == AgentExecutionStatus.SUCCEEDED and execution.output == valid
        elif scenario in ("stopped", "late_reply"):
            assert execution.status == AgentExecutionStatus.RUNNING and execution.output is None
        else:
            assert execution.status == AgentExecutionStatus.FAILED
            reason = "PLANNER_EXECUTION_ERROR" if scenario == "timeout" else "PLANNER_OUTPUT_INVALID"
            assert execution.output == {"reason": reason}
        if scenario in ("repaired", "still_invalid"):
            assert execution.llm_usage["output_repair_attempts"] == 1
        if sent:
            request_id, = execution.llm_usage["model_requests"]
            assert execution.llm_usage["model_requests"][request_id] == {
                "worker_id": worker.worker_id,
                "reserved_tokens": 500,
                "actual_usage": None if scenario == "timeout" else {"input_tokens": 300, "output_tokens": 100},
            }
            settled_execution_id = execution.id
        else:
            assert execution.llm_usage is None

    if scenario == "late_reply":
        for actual_output_tokens in (100, 101):
            with factory() as session:
                def settle():
                    RunCoordinator(session).settle_model_request(
                        run_id, worker.worker_id, agent_execution_id=settled_execution_id,
                        request_id=UUID(request_id),
                        actual_usage=ModelTokenUsage(input_tokens=300, output_tokens=actual_output_tokens),
                    )

                if actual_output_tokens == 100:
                    settle()
                else:
                    with pytest.raises(ModelUsageConflictError, match="different usage"):
                        settle()

    if scenario in ("repaired", "timeout"):
        with factory() as session:
            coordinator = RunCoordinator(session)
            next_execution_id = coordinator.start_agent_execution(
                run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={},
            )
            next_reservation = coordinator.reserve_model_request(
                run_id, worker.worker_id, agent_execution_id=next_execution_id,
                estimated_input_tokens=80, max_output_tokens=100,
            )
        if scenario == "repaired":
            assert next_reservation.max_output_tokens == 20
        else:
            assert next_reservation is None


@pytest.mark.parametrize("scenario", ["repaired", "repair_denied", "initial_denied", "already_valid"])
def test_planner_model_gateway_budgets_initial_and_repair_together(worker_audit_session_factory, scenario):
    factory = worker_audit_session_factory
    worker = Worker(factory, "planner-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        task.policy_snapshot = {
            **task.policy_snapshot,
            "budget": {
                **task.policy_snapshot["budget"],
                "max_llm_tokens_per_run": 320 if scenario == "initial_denied" else 500,
            },
        }
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        explorer_execution_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={},
        )
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=explorer_execution_id,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
        )

    invalid = {"outcome": "NEEDS_EVIDENCE", "evidence_gap": "Locate callers"}
    valid = {**invalid, "affected_decision": "Choose where to enforce permissions"}
    sent = []
    input_contexts = []

    def estimate_input_tokens(prompt):
        if "\nRepair data JSON:\n" in prompt:
            return 80 if scenario == "repair_denied" else 50
        return 320

    def respond(request):
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        initial, repair_section, repair_data = prompt.partition("\nRepair data JSON:\n")
        phase = "repair" if repair_section else "initial"
        sent.append((phase, body["max_tokens"]))
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert body["model"] == "configured-model"
        assert body["response_format"] == {"type": "json_object"}
        assert request.extensions["timeout"]["read"] == 5
        instructions_and_schema, _, input_json = initial.partition("\nInput data JSON:\n")
        if repair_section:
            input_json = input_json.split("\nRegenerate one JSON object", 1)[0]
            repair_feedback = json.loads(repair_data)
            assert repair_feedback["invalid_output"] == json.dumps(invalid)[:-1]
            assert repair_feedback["validation_errors"][0]["type"] == "json_invalid"
        schema = json.loads(instructions_and_schema.split("\nOutput schema JSON:\n", 1)[1])
        assert schema["discriminator"]["propertyName"] == "outcome"
        input_contexts.append(json.loads(input_json))
        if phase == "initial":
            content = json.dumps(valid) if scenario == "already_valid" else json.dumps(invalid)[:-1]
            usage = {"prompt_tokens": 320, "completion_tokens": 100}
        else:
            content = json.dumps(valid)
            usage = {"prompt_tokens": 50, "completion_tokens": 20}
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": usage})

    def execute():
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            gateway = PlannerModelGateway(
                session_factory=factory, worker_id=worker.worker_id,
                estimate_input_tokens=estimate_input_tokens, max_output_tokens=200,
                generate=DeepSeekPlannerAdapter(client, api_key="test-key", model="configured-model", timeout_seconds=5),
            )
            return worker.execute_planner(
                run_id, explorer_execution_id=explorer_execution_id, stop_event=Event(), model_gateway=gateway,
            )

    if scenario == "initial_denied":
        with pytest.raises(PlannerModelBudgetUnavailableError):
            execute()
    elif scenario == "repair_denied":
        with pytest.raises(ValidationError):
            execute()
    else:
        execution_id = execute()

    expected_calls = {
        "repaired": [("initial", 180), ("repair", 30)],
        "repair_denied": [("initial", 180)],
        "initial_denied": [],
        "already_valid": [("initial", 180)],
    }[scenario]
    assert sent == expected_calls
    with factory() as session:
        execution = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "planner",
        )).one()
        assert input_contexts == [execution.input] * len(expected_calls)
        assert execution.input["issue"] == {
            "title": "Worker entry test",
            "description": "Verify one claim from a separate entry point",
        }
        assert execution.input["base_commit"] == "a" * 40
        entries = (execution.llm_usage or {}).get("model_requests", {})
        assert len(entries) == len(expected_calls)
        if entries:
            assert {"input_tokens": 320, "output_tokens": 100} in [
                entry["actual_usage"] for entry in entries.values()
            ]
        if scenario == "initial_denied":
            assert execution.status == AgentExecutionStatus.FAILED
            assert execution.output == {"reason": "PLANNER_MODEL_BUDGET_UNAVAILABLE"}
        elif scenario == "repair_denied":
            assert execution.status == AgentExecutionStatus.FAILED
            assert execution.output == {"reason": "PLANNER_OUTPUT_INVALID"}
        else:
            assert execution.id == execution_id
            assert execution.status == AgentExecutionStatus.SUCCEEDED and execution.output == valid


def test_model_reservations_share_run_budget(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "planner-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        task.policy_snapshot = {
            **task.policy_snapshot,
            "budget": {**task.policy_snapshot["budget"], "max_llm_tokens_per_run": 500},
        }
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        executions = [
            coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={})
            for _ in range(2)
        ]

    barrier = Barrier(2, timeout=5)

    def reserve(execution_id):
        with factory() as session:
            barrier.wait()
            return RunCoordinator(session).reserve_model_request(
                run_id, worker.worker_id, agent_execution_id=execution_id,
                estimated_input_tokens=320, max_output_tokens=200,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = [reservation for reservation in pool.map(reserve, executions) if reservation is not None]

    assert len(admitted) == 1
    assert admitted[0].max_output_tokens == 180


def initial_handoff(supervisor_id, target=AgentRole.EXPLORER):
    return HandoffDecision.model_validate({
        "from_agent_execution_id": supervisor_id, "to_agent_role": target.value,
        "context": {
            "completed_work": "Reviewed the admitted issue", "blocker": None,
            "evidence_refs": [], "decision_required": "Explore the repository",
            "scope_impact": "Read the authorized workspace",
        },
    })


def test_initial_supervisor_handoff_dispatches_explorer(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "initial-handoff-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        first = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        second = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        with pytest.raises(HandoffApprovalError):
            coordinator.finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=first, status=AgentExecutionStatus.SUCCEEDED,
                output={}, llm_usage=None, approved_handoff=initial_handoff(first, AgentRole.PLANNER),
            )
        handoff_id = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=first, status=AgentExecutionStatus.SUCCEEDED,
            output={"decision": "explore"}, llm_usage=None, approved_handoff=initial_handoff(first),
        )
        with pytest.raises(HandoffApprovalError):
            coordinator.finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=second, status=AgentExecutionStatus.SUCCEEDED,
                output={}, llm_usage=None, approved_handoff=initial_handoff(second),
            )
    with factory() as session:
        handoff = session.get(Handoff, handoff_id)
        assert handoff.from_agent_execution_id == first and handoff.to_agent_role == "explorer"
        assert session.get(AgentExecution, first).output["approved_handoff_id"] == str(handoff_id)
        assert session.get(AgentExecution, second).status == AgentExecutionStatus.RUNNING
    received = []
    def receive(handoff):
        with factory() as session:
            received.append(RunCoordinator(session).start_agent_execution(
                run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={}, handoff_id=handoff.id,
            ))
    assert run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=lambda _: first, receivers={AgentRole.EXPLORER: receive},
    ) == handoff_id
    with factory() as session:
        assert session.get(AgentExecution, received[0]).handoff_id == handoff_id


def test_initial_supervisor_handoff_rolls_back_with_commit_failure(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "initial-rollback-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        supervisor = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
    with factory() as session:
        def fail_commit(_session):
            raise RuntimeError("simulated commit failure")
        event.listen(session, "before_commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failure"):
            RunCoordinator(session).finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=supervisor, status=AgentExecutionStatus.SUCCEEDED,
                output={}, llm_usage=None, approved_handoff=initial_handoff(supervisor),
            )
    with factory() as session:
        assert session.get(AgentExecution, supervisor).status == AgentExecutionStatus.RUNNING
        assert session.scalars(select(Handoff)).all() == []


def test_concurrent_initial_supervisors_create_only_one_handoff(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "initial-race-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        supervisors = [coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        ) for _ in range(2)]
    barrier = Barrier(2)
    def decide(supervisor):
        with factory() as session:
            barrier.wait(timeout=10)
            try:
                return RunCoordinator(session).finish_agent_execution(
                    run_id, worker.worker_id, agent_execution_id=supervisor, status=AgentExecutionStatus.SUCCEEDED,
                    output={}, llm_usage=None, approved_handoff=initial_handoff(supervisor),
                )
            except HandoffApprovalError:
                return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(decide, supervisors))
    assert sum(outcome is not None for outcome in outcomes) == 1
    with factory() as session:
        assert len(session.scalars(select(Handoff)).all()) == 1


def test_initial_supervisor_model_decides_and_dispatches_explorer(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "initial-model-supervisor")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }
    run_id = worker.claim_once().run_id
    prompts = []
    def generate(prompt, _limit):
        prompts.append(prompt)
        output = ({"outcome": "EXPLORE", "reason": "Ready"} if len(prompts) == 1 else {
            "outcome": "EXPLORE", "reason": "Issue needs code exploration", "exploration_focus": "Locate affected code",
        })
        return ModelReply(output, ModelTokenUsage(input_tokens=30, output_tokens=10))
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        generate=generate, max_output_tokens=100,
    )
    supervise = worker.build_initial_supervise(run_id, model_gateway=gateway, stop_event=Event())
    with pytest.raises(ValueError, match="another Run"):
        supervise(uuid4())
    received = []
    handoff_id = run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=supervise, receivers={AgentRole.EXPLORER: received.append},
    )
    assert handoff_id is not None
    assert len(prompts) == 2 and "previous output did not match" in prompts[1]
    with factory() as session:
        execution = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "supervisor",
        )).one()
        handoff = session.get(Handoff, handoff_id)
        assert execution.status == AgentExecutionStatus.SUCCEEDED
        assert execution.output["outcome"] == "EXPLORE"
        assert execution.llm_usage["output_repair_attempts"] == 1
        assert len(execution.llm_usage["model_requests"]) == 2
        assert handoff.from_agent_execution_id == execution.id
        assert handoff.context["decision_required"] == "Locate affected code"
    assert len(received) == 1 and received[0].id == handoff_id


@pytest.mark.skipif(os.name != "nt", reason="Trusted baseline needs the Windows Job pytest report")
def test_initial_supervisor_graph_runs_real_explorer_with_model_stubs(worker_audit_session_factory, tmp_path, monkeypatch):
    factory = worker_audit_session_factory
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    worker = Worker(factory, "initial-explorer-graph")
    repo = tmp_path / "source-repo"
    repo.mkdir()
    (repo / "README.md").write_bytes(b"Repository facts\n")
    for args in (["init", "--quiet"], ["add", "README.md"],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                  "commit", "--quiet", "-m", "initial"]):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=15)
    policy = build_default_policy()
    policy.policy_version = "initial-explorer-test-v1"
    policy.budget.max_admission_llm_tokens_per_task = 1000
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = ["read_file", "apply_patch"]
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = ["run_command", "git_diff"]
    with factory() as session:
        task, created = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Inspect the README", description="Find repository facts", repo=str(repo), idempotency_key=uuid4(),
        )
        assert created
        task_id = task.id
    admission_gateway = AdmissionModelGateway(session_factory=factory, estimate_input_tokens=lambda _: 100,
        max_output_tokens=100, generate=lambda *_: ModelReply(
            {"outcome": "ELIGIBLE", "reason": "ready", "evidence_refs": ["repository_head"]},
            ModelTokenUsage(input_tokens=40, output_tokens=20)))
    admission = dispatch_one_admission(session_factory=factory, gateway=admission_gateway,
        trusted_repositories=frozenset({str(repo)}), timeout_seconds=10)
    assert admission.task_id == task_id and admission.attempt.outcome == "ELIGIBLE"
    run_id = worker.claim_once().run_id
    workspace_root, artifact_root = tmp_path / "workspaces", tmp_path / "artifacts"
    workspace_root.mkdir()
    artifact_root.mkdir()
    directories, _ = worker.initialize_run_workspace(run_id, workspaces_root=workspace_root,
                                                      artifacts_root=artifact_root, stop_event=Event())
    workspace, artifacts = directories.workspace_root, directories.artifact_root
    def gateway(generate):
        return ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                            estimate_input_tokens=lambda _: 100, generate=generate, max_output_tokens=100)
    supervisor_gateway = gateway(lambda _prompt, _limit: ModelReply({
        "outcome": "EXPLORE", "reason": "The Issue needs repository evidence", "exploration_focus": "Locate relevant facts",
    }, ModelTokenUsage(input_tokens=40, output_tokens=20)))
    explorer_prompts = []
    def explore(prompt, _limit):
        explorer_prompts.append(prompt)
        if len(explorer_prompts) == 1:
            output = {"path": "README.md"}
        else:
            data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            output = {"summary": "Read repository facts", "read_evidence": {
                "path": "README.md", "tool_call_id": data["read"]["tool_call_id"],
            }}
        return ModelReply(output, ModelTokenUsage(input_tokens=40, output_tokens=20))
    explorer_gateway = gateway(explore)
    planner_gateway = PlannerModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        generate=lambda _prompt, _limit: pytest.fail("Planner is not part of the first handoff"), max_output_tokens=100,
    )
    stop = Event()
    handoff_id = run_handoff_cycle(
        factory, run_id, worker.worker_id,
        supervise=worker.build_initial_supervise(run_id, model_gateway=supervisor_gateway, stop_event=stop),
        receivers=worker.build_handoff_receivers(
            run_id, directories=RunDirectories(workspace, artifacts), max_bytes=4096, stop_event=stop,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
        ),
    )
    with factory() as session:
        handoff = session.get(Handoff, handoff_id)
        supervisor = session.get(AgentExecution, handoff.from_agent_execution_id)
        explorer = session.scalars(select(AgentExecution).where(AgentExecution.handoff_id == handoff_id)).one()
        assert supervisor.agent_role == "supervisor" and supervisor.status == AgentExecutionStatus.SUCCEEDED
        assert explorer.agent_role == "explorer" and explorer.status == AgentExecutionStatus.SUCCEEDED
        assert explorer.output["read_evidence"][0]["path"] == "README.md"
        read_call = session.get(ToolCall, UUID(explorer.output["read_evidence"][0]["tool_call_id"]))
        assert read_call.status == ToolCallStatus.SUCCEEDED and read_call.stdout_excerpt == "Repository facts\n"
        supervisor_context = ContextBuilder(session).build_supervisor_exploration(
            run_id, explorer_execution_id=explorer.id,
        )
        assert "preview" not in supervisor_context["read_evidence"][0]
        assert supervisor_context["read_evidence"][0]["preview_chars"] == len("Repository facts\n")
        assert supervisor_context["read_evidence"][0]["truncated"] is False
        explorer_id = explorer.id
        assert session.get(Run, run_id).status == RunStatus.RUNNING
        assert session.get(Task, task_id).policy_version == "initial-explorer-test-v1"

    def route_to_planner(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        assert "preview" not in data["read_evidence"][0]
        assert data["read_evidence"][0]["preview_chars"] == len("Repository facts\n")
        return ModelReply({"outcome": "PLAN", "reason": "Relevant evidence is available",
                           "next_goal": "Plan a bounded change"}, ModelTokenUsage(input_tokens=40, output_tokens=20))
    planner_gateway = PlannerModelGateway(session_factory=factory, worker_id=worker.worker_id,
        estimate_input_tokens=lambda _: 100, max_output_tokens=100,
        generate=lambda *_: ModelReply({"outcome": "PLAN_READY", "plan": {
            "approach": "Update the relevant documentation", "target_paths": ["README.md"],
            "steps": ["Edit the relevant section"], "scope_limits": "Only README.md", "verification": ["Review diff"],
        }}, ModelTokenUsage(input_tokens=40, output_tokens=20)))
    planner_handoff_id = run_handoff_cycle(factory, run_id, worker.worker_id,
        supervise=worker.build_post_explorer_supervise(run_id, explorer_execution_id=explorer_id,
                                                       model_gateway=gateway(route_to_planner), stop_event=stop),
        receivers=worker.build_handoff_receivers(run_id, directories=RunDirectories(workspace, artifacts),
            max_bytes=4096, stop_event=stop, explorer_gateway=explorer_gateway, planner_gateway=planner_gateway))
    with factory() as session:
        handoff = session.get(Handoff, planner_handoff_id)
        planner = session.scalars(select(AgentExecution).where(AgentExecution.handoff_id == planner_handoff_id)).one()
        assert handoff.from_agent_execution_id == explorer_id
        assert planner.agent_role == "planner" and planner.status == AgentExecutionStatus.SUCCEEDED
        assert planner.output["outcome"] == "PLAN_READY"
        planner_id = planner.id

    model = gateway(lambda *_: ModelReply({"outcome": "CODE", "reason": "Plan is within scope",
                                           "next_goal": "Implement approved README change"},
                                          ModelTokenUsage(input_tokens=40, output_tokens=20)))
    baseline_test = tmp_path / "test_expected.py"
    baseline_test.write_text('from pathlib import Path\ndef test_result():\n    assert Path("README.md").read_text() == "Repository facts updated\\n"\n', encoding="utf-8")
    from devflow_ai.verification_config import VerificationConfig
    with factory() as session:
        task_base = session.get(Task, task_id).base_commit
    verification_config = VerificationConfig(task_id=task_id, base_commit=task_base,
        command_name="pytest", command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                        "--rootdir", str(tmp_path), str(baseline_test)),
        target_tests=("test_expected::test_result",), external_connections="none")
    baseline_handoff = run_handoff_cycle(factory, run_id, worker.worker_id,
        supervise=worker.build_post_planner_supervise(run_id, planner_execution_id=planner_id,
                                                       model_gateway=model, stop_event=stop),
        receivers=worker.build_handoff_receivers(run_id, directories=directories, max_bytes=4096,
            stop_event=stop, explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            verification_config=verification_config))
    with factory() as session:
        verifier = session.scalars(select(AgentExecution).where(AgentExecution.handoff_id == baseline_handoff)).one()
        assert verifier.output["outcome"] == "BASELINE_RECORDED"
        prior_supervisor = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == "supervisor",
            AgentExecution.output["approved_handoff_id"].as_string() == str(baseline_handoff),
        )).one()
        assert prior_supervisor.input["baseline_available"] is False
        assert prior_supervisor.input["observed_baseline_verifier_id"] is None
        baseline_receipt = load_verifier_command_evidence(
            session, run_id, verifier.id, (UUID(verifier.output["tool_call_id"]),))[0]
        baseline = {"execution_id": verifier.id, "receipt": baseline_receipt}
    assert baseline["receipt"]["exit_code"] == 1
    result = worker.execute_supervisor_after_planner(run_id, planner_execution_id=planner_id,
                                                     model_gateway=model, stop_event=stop)
    assert result.outcome == "CODE" and result.handoff_id is not None
    with factory() as session:
        routed_supervisor = session.get(AgentExecution, result.execution_id)
        assert routed_supervisor.input["baseline_available"] is True
        assert routed_supervisor.input["observed_baseline_verifier_id"] == str(baseline["execution_id"])
        handoff = session.get(Handoff, result.handoff_id)
        coder_input = ContextBuilder(session).build_coder_input(run_id, handoff_id=result.handoff_id)
        assert handoff.from_agent_execution_id == planner_id
        assert coder_input["plan"]["target_paths"] == ["README.md"]
        assert coder_input["explorer_evidence"][0]["ends_with_newline"] is True
        assert {"baseline_verifier_id": str(baseline["execution_id"])} in handoff.context["evidence_refs"]
    patch = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
             "@@ -1 +1 @@\n-Repository facts\n+Repository facts updated\n")
    coder_model = gateway(lambda *_: ModelReply({"outcome": "PATCH_DRAFT", "patch": patch, "summary": "Update README"},
                                                ModelTokenUsage(input_tokens=40, output_tokens=20)))
    with factory() as session:
        original_context = session.get(Handoff, result.handoff_id).context
    for invalid_refs in ([], [{"baseline_verifier_id": str(uuid4())}]):
        with factory.begin() as session:
            session.get(Handoff, result.handoff_id).context = {**original_context, "evidence_refs": invalid_refs}
        try:
            with pytest.raises(CoderContextUnavailableError, match="baseline"):
                worker.execute_coder(run_id, handoff_id=result.handoff_id, directories=directories,
                    model_gateway=coder_model, max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop)
        finally:
            with factory.begin() as session:
                session.get(Handoff, result.handoff_id).context = original_context
    with factory.begin() as session:
        calls = ToolCallRepository(session)
        intervening_id = calls.add_requested(explorer_id, "apply_patch", {}, run_id=run_id)
        calls.save_result(intervening_id, status=ToolCallStatus.SUCCEEDED)
    try:
        with pytest.raises(CoderContextUnavailableError, match="baseline"):
            worker.execute_coder(run_id, handoff_id=result.handoff_id, directories=directories,
                model_gateway=coder_model, max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop)
    finally:
        with factory.begin() as session:
            session.execute(delete(ToolCall).where(ToolCall.id == intervening_id))
    report_id = UUID(next(item["id"] for item in baseline["receipt"]["artifacts"] if item["kind"] == "pytest_junit"))
    with factory() as session:
        report_path = Path(session.get(Artifact, report_id).content_ref)
    original_report = report_path.read_bytes()
    report_path.write_bytes(b"tampered")
    try:
        with pytest.raises(CoderContextUnavailableError, match="baseline"):
            worker.execute_coder(run_id, handoff_id=result.handoff_id, directories=directories,
                model_gateway=coder_model, max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop)
        with factory() as session:
            assert not session.scalars(select(AgentExecution.id).where(
                AgentExecution.run_id == run_id, AgentExecution.agent_role == "coder")).all()
    finally:
        report_path.write_bytes(original_report)
    coder_id = worker.execute_coder(run_id, handoff_id=result.handoff_id, directories=directories,
        model_gateway=coder_model, max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop)
    with factory() as session:
        coder = session.get(AgentExecution, coder_id)
        assert coder.status == AgentExecutionStatus.SUCCEEDED and coder.output["outcome"] == "PATCH_READY"
        assert (workspace / "README.md").read_bytes() == b"Repository facts updated\n"
    received = []
    verify_gateway = gateway(lambda *_: ModelReply(
        {"outcome": "VERIFY", "reason": "Applied patch needs independent checks", "next_goal": "Verify the patch"},
        ModelTokenUsage(input_tokens=40, output_tokens=20)))
    with factory() as session:
        patch_path = Path(session.get(Artifact, UUID(session.get(AgentExecution, coder_id).output["patch_artifact_id"])).content_ref)
    patch_bytes = patch_path.read_bytes()
    patch_path.write_bytes(b"tampered")
    try:
        with pytest.raises(ValueError, match="patch evidence"):
            worker.execute_supervisor_after_coder(run_id, coder_execution_id=coder_id, directories=directories,
                model_gateway=verify_gateway, max_patch_bytes=4096, stop_event=stop)
    finally:
        patch_path.write_bytes(patch_bytes)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        original_policy = task.policy_snapshot
        roles = dict(original_policy["permissions"]["allowed_tools_by_role"])
        roles["VERIFIER"] = []
        task.policy_snapshot = {**original_policy, "permissions": {"allowed_tools_by_role": roles}}
    try:
        blocked = worker.execute_supervisor_after_coder(run_id, coder_execution_id=coder_id, directories=directories,
            model_gateway=verify_gateway, max_patch_bytes=4096, stop_event=stop)
        assert blocked.outcome == "BLOCKED" and blocked.handoff_id is None
    finally:
        with factory.begin() as session:
            session.get(Task, task_id).policy_snapshot = original_policy
    baseline_receiver = worker.build_handoff_receivers(run_id, directories=directories, max_bytes=4096,
        stop_event=stop, explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
        baseline_registry={"pytest": (sys.executable, "-m", "pytest")}, baseline_command_name="pytest")
    from devflow_ai.verifier import parse_pytest_junit_report
    target_name = parse_pytest_junit_report(original_report,
        tool_call_id=UUID(baseline["receipt"]["tool_call_id"]), command_name="pytest").tests[0].name
    def semantic_review(prompt, _limit):
        data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        return ModelReply({
        "outcome": "REVIEW", "result": "passed", "reason": "The requested README text is present",
        "citations": [{"source": "workspace_diff", "path": "README.md", "snippet": "Repository facts updated",
                       "tool_call_id": data["context"]["workspace_diff"]["tool_call_id"],
                       "diff_side": "added", "reason": "The requested text is added"}],
        }, ModelTokenUsage(input_tokens=40, output_tokens=20))
    semantic_gateway = gateway(semantic_review)
    assert verification_config.target_tests == (target_name,)
    with pytest.raises(ValueError, match="does not match"):
        worker.build_handoff_receivers(run_id, directories=directories, max_bytes=4096, stop_event=stop,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            verification_config=verification_config.model_copy(update={"task_id": uuid4()}))
    receiver = worker.build_handoff_receivers(run_id, directories=directories, max_bytes=4096,
        stop_event=stop, explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
        verification_config=verification_config, verifier_gateway=semantic_gateway,
        verifier_max_diff_bytes=65536, max_patch_bytes=4096)
    def dispatch_verifier(handoff):
        received.append(handoff)
        assert handoff.from_agent_execution_id == coder_id
        assert {"baseline_verifier_id": str(baseline["execution_id"])} in handoff.context["evidence_refs"]
        with pytest.raises(ValueError, match="Planner handoff"):
            baseline_receiver[AgentRole.VERIFIER](handoff)
        with factory.begin() as session:
            verifier_handoff = session.get(Handoff, handoff.id)
            original_verifier_context = verifier_handoff.context
            verifier_handoff.context = {**original_verifier_context, "evidence_refs": []}
        try:
            with pytest.raises(ValueError, match="baseline_verifier_id"):
                receiver[AgentRole.VERIFIER](handoff)
        finally:
            with factory.begin() as session:
                session.get(Handoff, handoff.id).context = original_verifier_context
        receiver[AgentRole.VERIFIER](handoff)
    verify_handoff = run_handoff_cycle(factory, run_id, worker.worker_id,
        supervise=worker.build_post_coder_supervise(run_id, coder_execution_id=coder_id, directories=directories,
            model_gateway=verify_gateway, max_patch_bytes=4096, stop_event=stop),
        receivers={AgentRole.VERIFIER: dispatch_verifier})
    assert len(received) == 1 and received[0].id == verify_handoff
    with factory() as session:
        acceptance = session.scalars(select(AgentExecution).where(AgentExecution.handoff_id == verify_handoff)).one()
        assert acceptance.status == AgentExecutionStatus.RUNNING
        assert acceptance.output["outcome"] == "VERIFICATION_BLOCKED"
        assert acceptance.output["preliminary_public_outcome"] == "blocked"
        assert "test_integrity" in acceptance.output["missing_evidence"]
        assert "external_connection_provenance" not in acceptance.output["missing_evidence"]
        assert "baseline_workspace_attestation" not in acceptance.output["missing_evidence"]
        assert acceptance.output["external_connections_declaration"] == "none"
        assert session.get(Artifact, UUID(acceptance.output["draft_artifact_id"])).kind == "semantic_review_draft"
        acceptance_id = acceptance.id
        draft_id = UUID(acceptance.output["draft_artifact_id"])
    with factory() as session:
        resumed = RunCoordinator(session).assess_verification_readiness(
            run_id, worker.worker_id, agent_execution_id=acceptance_id, draft_artifact_id=draft_id,
            artifact_root=artifacts, max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
        assert resumed.status == "blocked" and "test_integrity" in resumed.missing_evidence
        assert "baseline_workspace_attestation" not in resumed.missing_evidence
        assert "external_connection_provenance" in resumed.missing_evidence
    with factory() as session:
        resumed_with_config = RunCoordinator(session).assess_verification_readiness(
            run_id, worker.worker_id, agent_execution_id=acceptance_id, draft_artifact_id=draft_id,
            artifact_root=artifacts, max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
            verification_config=verification_config)
        assert "external_connection_provenance" not in resumed_with_config.missing_evidence
    with factory() as session, pytest.raises(ValueError, match="does not match"):
        RunCoordinator(session).assess_verification_readiness(
            run_id, worker.worker_id, agent_execution_id=acceptance_id, draft_artifact_id=draft_id,
            artifact_root=artifacts, max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
            verification_config=verification_config.model_copy(update={"target_tests": ("other::test",)}))
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING
    with factory() as session:
        coordinator = RunCoordinator(session)
        supervisor_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        blocked_handoff = coordinator.finish_agent_execution(run_id, worker.worker_id,
            agent_execution_id=supervisor_id, status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision(from_agent_execution_id=planner_id, to_agent_role=AgentRole.CODER,
                context=HandoffContext(completed_work="Plan prepared", blocker=None,
                    evidence_refs=({"explorer_execution_id": str(explorer_id)},),
                    decision_required="Implement plan", scope_impact="README only")))
        blocked_coder = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER,
            agent_input={}, handoff_id=blocked_handoff)
        coordinator.finish_agent_execution(run_id, worker.worker_id, agent_execution_id=blocked_coder,
            status=AgentExecutionStatus.SUCCEEDED, llm_usage=None, output={"outcome": "BLOCKED",
                "completed_work": "Reviewed the plan", "blocker": "Plan needs clarification",
                "evidence_refs": [{"explorer_execution_id": str(explorer_id)}],
                "decision_required": "Revise plan", "scope_impact": "README only", "recommendation": None})
    blocked_route = worker.execute_supervisor_after_coder(run_id, coder_execution_id=blocked_coder,
        directories=directories, model_gateway=verify_gateway, max_patch_bytes=4096, stop_event=stop)
    assert blocked_route.outcome == "BLOCKED" and blocked_route.handoff_id is None
    replan_gateway = gateway(lambda *_: ModelReply(
        {"outcome": "REPLAN", "reason": "Plan needs clarification", "next_goal": "Review the plan"},
        ModelTokenUsage(input_tokens=40, output_tokens=20)))
    replanned = worker.execute_supervisor_after_coder(run_id, coder_execution_id=blocked_coder,
        directories=directories, model_gateway=replan_gateway, max_patch_bytes=4096, stop_event=stop)
    assert replanned.outcome == "REPLAN"
    with factory() as session:
        replan_handoff = session.get(Handoff, replanned.handoff_id)
        assert replan_handoff.to_agent_role == "planner" and replan_handoff.from_agent_execution_id == blocked_coder
        assert replan_handoff.context["blocker"] == "Plan needs clarification"


def test_planner_context_keeps_bounded_prior_exploration(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "exploration-history")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    explorers = []
    for path, preview in (("first.py", "a" * 7000), ("second.py", "new evidence")):
        with factory() as session:
            explorer_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
                role=AgentRole.EXPLORER, agent_input={})
        with factory.begin() as session:
            repo = ToolCallRepository(session)
            call_id = repo.add_requested(explorer_id, "read_file", {"path": path}, run_id=run_id)
            repo.save_result(call_id, status=ToolCallStatus.SUCCEEDED, stdout_excerpt=preview)
        with factory() as session:
            RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id, agent_execution_id=explorer_id,
                status=AgentExecutionStatus.SUCCEEDED, output={"summary": "Read file"}, llm_usage=None,
                read_evidence=(ReadEvidenceRef(path=path, tool_call_id=call_id),))
        explorers.append(explorer_id)
    with factory() as session:
        context = ContextBuilder(session).build_planner_input(run_id, explorer_execution_id=explorers[1])
        prior = context["prior_explorations"][0]
        assert prior["explorer_execution_id"] == str(explorers[0])
        # The read itself was complete, so it is not reported as truncated; only the
        # builder's own budget clip would say otherwise, in its own field.
        assert prior["read_evidence"][0]["truncated"] is False
        assert prior["read_evidence"][0]["preview"] == "a" * 7000
        assert prior["read_evidence"][0]["preview_chars"] == 7000
        assert prior["read_evidence"][0]["preview_clipped"] is False
        assert context["read_evidence"][0]["preview"] == "new evidence"


@pytest.mark.parametrize("mode", ["reexplore", "denied", "blocked", "no_preview", "invalid", "missing", "foreign"])
def test_supervisor_after_explorer_respects_evidence_and_policy(worker_audit_session_factory, mode):
    factory = worker_audit_session_factory
    worker = Worker(factory, "post-explorer")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        roles = dict(snapshot["permissions"]["allowed_tools_by_role"])
        roles["EXPLORER"] = [] if mode == "denied" else ["read_file"]
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": roles}}
    run_id = worker.claim_once().run_id
    foreign_id = None
    if mode == "foreign":
        create_eligible_task(factory)
        foreign_run = worker.claim_once().run_id
        with factory() as session:
            foreign_id = RunCoordinator(session).start_agent_execution(
                foreign_run, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
        with factory() as session:
            RunCoordinator(session).finish_agent_execution(foreign_run, worker.worker_id,
                agent_execution_id=foreign_id, status=AgentExecutionStatus.SUCCEEDED,
                output={"summary": "other run"}, llm_usage=None)
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
    with factory.begin() as session:
        repository = ToolCallRepository(session)
        call_id = repository.add_requested(explorer_id, "read_file", {"path": "src.py"}, run_id=run_id)
        repository.save_result(call_id, status=ToolCallStatus.SUCCEEDED,
            stdout_excerpt=None if mode == "no_preview" else "partial code",
            reason="OUTPUT_TRUNCATED")
    with factory() as session:
        RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id,
            agent_execution_id=explorer_id, status=AgentExecutionStatus.SUCCEEDED,
            output={"summary": "A partial read"}, llm_usage=None,
            read_evidence=(ReadEvidenceRef(path="src.py", tool_call_id=call_id),))
    prompts, captured = [], []
    def generate(prompt, maximum):
        prompts.append(prompt)
        parsed = json.loads(prompt.split("\nInput data JSON:\n", 1)[1].split("\nThe previous", 1)[0])
        captured.append(parsed)
        assert parsed["read_evidence"][0]["truncated"] is True
        if mode == "blocked":
            output = {"outcome": "BLOCKED", "reason": "conflicting facts", "decision_required": "clarify"}
        elif mode == "invalid":
            output = {"outcome": "PLAN"}
        else:
            output = {"outcome": "RE_EXPLORE", "reason": "preview truncated", "next_goal": "inspect remaining code"}
        return ModelReply(output, ModelTokenUsage(input_tokens=5, output_tokens=5))
    model = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                         estimate_input_tokens=lambda _: 10, generate=generate, max_output_tokens=50)
    if mode in ("invalid", "missing", "foreign"):
        expected = ValidationError if mode == "invalid" else ExplorerSupervisorContextError
        with pytest.raises(expected):
            worker.execute_supervisor_after_explorer(run_id,
                explorer_execution_id=(uuid4() if mode == "missing" else foreign_id if mode == "foreign" else explorer_id),
                model_gateway=model, stop_event=Event())
    else:
        result = worker.execute_supervisor_after_explorer(run_id,
            explorer_execution_id=explorer_id, model_gateway=model, stop_event=Event())
        if mode == "reexplore":
            assert result.outcome == "RE_EXPLORE" and result.handoff_id is not None
            # Routing can see how much model budget is left before asking for another round.
            assert isinstance(captured[0]["remaining_model_tokens"], int)
            with factory() as session:
                handoff = session.get(Handoff, result.handoff_id)
                assert handoff.from_agent_execution_id == explorer_id and handoff.to_agent_role == "explorer"
        else:
            assert result.outcome == "BLOCKED" and result.handoff_id is None
    if mode in ("no_preview", "missing", "foreign"):
        assert prompts == []
    if mode == "invalid":
        assert len(prompts) == 2
        with factory() as session:
            supervisor = session.scalars(select(AgentExecution).where(
                AgentExecution.run_id == run_id, AgentExecution.agent_role == "supervisor")).one()
            assert supervisor.status == AgentExecutionStatus.FAILED
            assert supervisor.output["reason"] == "SUPERVISOR_OUTPUT_INVALID"


@pytest.mark.parametrize("mode", ["gap_code", "gap_explore", "denied", "no_preview", "blocked", "invalid", "missing"])
def test_supervisor_after_planner_obeys_plan_and_permissions(worker_audit_session_factory, mode):
    factory = worker_audit_session_factory
    worker = Worker(factory, "post-planner")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        roles = dict(snapshot["permissions"]["allowed_tools_by_role"])
        roles["EXPLORER"] = ["read_file"]
        roles["CODER"] = [] if mode == "denied" else ["apply_patch"]
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": roles}}
    run_id = worker.claim_once().run_id
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
            role=AgentRole.EXPLORER, agent_input={})
    with factory.begin() as session:
        calls = ToolCallRepository(session)
        call_id = calls.add_requested(explorer_id, "read_file", {"path": "src.py"}, run_id=run_id)
        calls.save_result(call_id, status=ToolCallStatus.SUCCEEDED,
                          stdout_excerpt=None if mode == "no_preview" else "code excerpt")
    with factory() as session:
        RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id, agent_execution_id=explorer_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"summary": "code found"}, llm_usage=None,
            read_evidence=(ReadEvidenceRef(path="src.py", tool_call_id=call_id),))
    with factory() as session:
        planner_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
            role=AgentRole.PLANNER, agent_input={"explorer_execution_id": str(explorer_id)})
    planner_output = ({"outcome": "NEEDS_EVIDENCE", "evidence_gap": "find caller", "affected_decision": "where to edit"}
        if mode.startswith("gap") else {"outcome": "PLAN_READY", "plan": {
            "approach": "modify caller", "target_paths": ["src.py"], "steps": ["edit function"],
            "scope_limits": "src.py only", "verification": ["run test"],
        }})
    with factory() as session:
        RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id, agent_execution_id=planner_id,
            status=AgentExecutionStatus.SUCCEEDED, output=planner_output, llm_usage=None)
    prompts = []
    def generate(prompt, maximum):
        prompts.append(prompt)
        if mode == "blocked":
            output = {"outcome": "BLOCKED", "reason": "unclear", "decision_required": "review"}
        elif mode == "invalid":
            output = {"outcome": "CODE"}
        elif mode == "gap_explore":
            output = {"outcome": "RE_EXPLORE", "reason": "missing caller", "next_goal": "locate caller"}
        else:
            output = {"outcome": "CODE", "reason": "ready", "next_goal": "implement plan"}
        return ModelReply(output, ModelTokenUsage(input_tokens=5, output_tokens=5))
    model = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                         estimate_input_tokens=lambda _: 10, generate=generate, max_output_tokens=50)
    if mode in ("invalid", "missing"):
        with pytest.raises(ValidationError if mode == "invalid" else PlannerSupervisorContextError):
            worker.execute_supervisor_after_planner(run_id, planner_execution_id=uuid4() if mode == "missing" else planner_id,
                                                    model_gateway=model, stop_event=Event())
    else:
        result = worker.execute_supervisor_after_planner(run_id, planner_execution_id=planner_id,
                                                        model_gateway=model, stop_event=Event())
        if mode == "gap_explore":
            assert result.outcome == "RE_EXPLORE" and result.handoff_id is not None
            with factory() as session:
                handoff = session.get(Handoff, result.handoff_id)
                assert handoff.from_agent_execution_id == planner_id and handoff.to_agent_role == "explorer"
        else:
            assert result.outcome == "BLOCKED" and result.handoff_id is None
    if mode == "missing":
        assert prompts == []
    if mode == "invalid":
        assert len(prompts) == 2


@pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE_SUPERVISOR_TEST") != "1", reason="Explicit live model test only")
def test_post_explorer_supervisor_real_model_on_fixture(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "post-explorer-live")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        roles = dict(snapshot["permissions"]["allowed_tools_by_role"])
        roles["EXPLORER"] = ["read_file"]
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": roles}}
    run_id = worker.claim_once().run_id
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
    with factory.begin() as session:
        repository = ToolCallRepository(session)
        call_id = repository.add_requested(explorer_id, "read_file", {"path": "README.md"}, run_id=run_id)
        repository.save_result(call_id, status=ToolCallStatus.SUCCEEDED,
            stdout_excerpt="Worker currently only claims tasks; execution is not started.")
    with factory() as session:
        RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id, agent_execution_id=explorer_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"summary": "Worker only claims tasks"}, llm_usage=None,
            read_evidence=(ReadEvidenceRef(path="README.md", tool_call_id=call_id),))
    with httpx.Client() as client:
        model = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
            estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), max_output_tokens=300,
            generate=DeepSeekPlannerAdapter.from_env(client, timeout_seconds=30))
        result = worker.execute_supervisor_after_explorer(run_id, explorer_execution_id=explorer_id,
                                                          model_gateway=model, stop_event=Event())
    with factory() as session:
        execution = session.get(AgentExecution, result.execution_id)
        assert execution.status == AgentExecutionStatus.SUCCEEDED
        request, = execution.llm_usage["model_requests"].values()
        assert request["actual_usage"] is not None
        if result.handoff_id is not None:
            assert session.get(Handoff, result.handoff_id).from_agent_execution_id == explorer_id
        print(f"post-explorer supervisor outcome={result.outcome}; usage={request['actual_usage']}")


@pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE_END_TO_END") != "1", reason="Explicit live model test only")
def test_real_models_attempt_synthetic_issue_through_coder(worker_audit_session_factory, tmp_path, monkeypatch):
    """Observe live behavior; an explicit blocked or additional-exploration route is not success."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    factory = worker_audit_session_factory
    repo = tmp_path / "source"
    create_local_repo(repo)
    policy = build_default_policy()
    policy.policy_version = "synthetic-live-v1"
    policy.budget.max_admission_llm_tokens_per_task = 10000
    policy.budget.max_llm_tokens_per_run = 30000
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = ["read_file", "apply_patch"]
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = ["run_command"]
    baseline_test = tmp_path / "test_synthetic_expectation.py"
    baseline_test.write_text(
        "from pathlib import Path\n\n"
        "def test_readme_greeting():\n"
        "    assert Path('README.md').read_bytes() == b'pinned content updated'\n",
        encoding="utf-8",
    )
    with factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Update README greeting", description='README.md currently says "pinned content"; '
            'change it to "pinned content updated" without modifying other files.', repo=str(repo), idempotency_key=uuid4())
        task_id = task.id
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    with httpx.Client() as client:
        adapter = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=30)
        admission_gateway = AdmissionModelGateway(session_factory=factory,
            estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), generate=adapter, max_output_tokens=512)
        admitted = dispatch_one_admission(session_factory=factory, gateway=admission_gateway,
                                          trusted_repositories=frozenset({str(repo)}), timeout_seconds=10)
        assert admitted.task_id == task_id
        if admitted.attempt.outcome != "ELIGIBLE":
            print(f"synthetic live stopped at admission: {admitted.attempt.outcome}")
            return
        worker = Worker(factory, "synthetic-live-worker")
        run_id = worker.claim_once().run_id
        def model():
            return ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), generate=adapter, max_output_tokens=512)
        planner_gateway = PlannerModelGateway(session_factory=factory, worker_id=worker.worker_id,
            estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), generate=adapter, max_output_tokens=512)
        def process(directories, actual_commit, stop_event):
            receivers = worker.build_handoff_receivers(run_id, directories=directories, max_bytes=4096,
                stop_event=stop_event, explorer_gateway=model(), planner_gateway=planner_gateway,
                coder_gateway=model(), max_patch_bytes=4096,
                baseline_registry={"pytest": (sys.executable, "-B", "-m", "pytest", "-q",
                                              "-p", "no:cacheprovider", str(baseline_test))},
                baseline_command_name="pytest")
            def receiver_for(handoff_id):
                with factory() as session:
                    execution = session.scalars(select(AgentExecution).where(AgentExecution.handoff_id == handoff_id)).one()
                    return execution.id, execution.agent_role, execution.status
            first = run_handoff_cycle(factory, run_id, worker.worker_id,
                supervise=worker.build_initial_supervise(run_id, model_gateway=model(), stop_event=stop_event),
                receivers=receivers)
            if first is None:
                return "initial_supervisor_blocked"
            explorer_id, role, status = receiver_for(first)
            if role != "explorer" or status != AgentExecutionStatus.SUCCEEDED:
                return "explorer_did_not_succeed"
            second = run_handoff_cycle(factory, run_id, worker.worker_id,
                supervise=worker.build_post_explorer_supervise(run_id, explorer_execution_id=explorer_id,
                                                               model_gateway=model(), stop_event=stop_event),
                receivers=receivers)
            if second is None:
                return "post_explorer_blocked"
            planner_id, role, status = receiver_for(second)
            if role != "planner" or status != AgentExecutionStatus.SUCCEEDED:
                return f"post_explorer_routed_{role}"
            third = run_handoff_cycle(factory, run_id, worker.worker_id,
                supervise=worker.build_post_planner_supervise(run_id, planner_execution_id=planner_id,
                                                              model_gateway=model(), stop_event=stop_event),
                receivers=receivers)
            if third is None:
                return "post_planner_blocked"
            baseline_id, role, status = receiver_for(third)
            if role != "verifier" or status != AgentExecutionStatus.SUCCEEDED:
                return f"post_planner_routed_{role}"
            with factory() as session:
                baseline = session.get(AgentExecution, baseline_id)
                if baseline.output.get("outcome") != "BASELINE_RECORDED":
                    return "baseline_not_recorded"
            fourth = run_handoff_cycle(factory, run_id, worker.worker_id,
                supervise=worker.build_post_planner_supervise(run_id, planner_execution_id=planner_id,
                                                              model_gateway=model(), stop_event=stop_event),
                receivers=receivers)
            if fourth is None:
                return "post_baseline_blocked"
            coder_id, role, status = receiver_for(fourth)
            if role != "coder" or status != AgentExecutionStatus.SUCCEEDED:
                return f"post_planner_routed_{role}"
            with factory() as session:
                coder = session.get(AgentExecution, coder_id)
                if coder.output.get("outcome") != "PATCH_READY":
                    return f"coder_blocked: {coder.output.get('blocker', 'no blocker recorded')}"
                assert session.get(Artifact, UUID(coder.output["patch_artifact_id"])) is not None
            content_matches = (directories.workspace_root / "README.md").read_bytes() == b"pinned content updated"
            return f"patch_ready_unverified; exact_fixture_content_matches={content_matches}"
        result = worker.execute_run(run_id, workspaces_root=workspaces, artifacts_root=artifacts,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=2, shutdown_timeout_seconds=5), process_run=process)
        print(f"synthetic live result={result}")
        with factory() as session:
            assert session.get(Run, run_id).status == RunStatus.RUNNING


@pytest.mark.skipif(os.environ.get("DEVFLOW_LIVE_CLICK_3019") != "1", reason="Explicit live Click evaluation only")
def test_real_click_3019_through_formal_verification(worker_audit_session_factory, tmp_path, monkeypatch):
    """One actual model attempt; private cases are read only by the trusted controller."""
    import shutil
    from sqlalchemy.inspection import inspect as inspect_model
    from devflow_ai.independent_evaluation import PrivateCase
    from devflow_ai.verification_config import VerificationConfig

    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    factory = worker_audit_session_factory
    base_repo = Path(os.environ["DEVFLOW_CLICK_BASE"])
    reference_repo = Path(os.environ["DEVFLOW_CLICK_REFERENCE"])
    private_suite = Path(os.environ["DEVFLOW_CLICK_PRIVATE_SUITE"])
    result_root = Path(os.environ["DEVFLOW_CLICK_RESULTS"])
    result_root.mkdir(parents=True, exist_ok=False)
    reference_commit = subprocess.run(["git", "-C", str(reference_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    private_items = json.loads(private_suite.read_text(encoding="utf-8"))
    cases = tuple(PrivateCase(json.dumps(item["request"], ensure_ascii=False).encode(),
        (json.dumps(item["expected"], ensure_ascii=False) + "\n").encode()) for item in private_items)
    adapter_text = Path("benchmarks/click_3019_public/stdio_adapter.py").read_text(encoding="utf-8")
    public_test = Path("benchmarks/click_3019_public/scenarios.py").resolve()
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            scripts.get_revision("a43cb50f67e9").module.upgrade()
            scripts.get_revision("f154b6e82639").module.upgrade()
    policy = build_default_policy()
    policy.policy_version = "click-3019-live-v1"
    policy.budget.max_llm_tokens_per_run = 250000
    policy.budget.max_run_duration_seconds = 1800
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    policy.permissions.allowed_tools_by_role[AgentRole.CODER] = ["read_file", "apply_patch"]
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = ["run_command", "git_diff", "read_file"]
    base_commit = "00fadb8904387158ce6e9aa1573be770446895c1"
    with factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Respect an empty prompt suffix",
            description="In click.prompt and click.confirm, an empty prompt_suffix currently inserts a space "
                        "before user input. Honor the supplied suffix exactly while preserving normal prompt and "
                        "confirmation behavior, including explicit space suffixes.",
            repo=str(base_repo), idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="Controller verified the local candidate checkout", evidence=["local_git_head"]))
    worker = Worker(factory, "click-3019-live-worker")
    run_id = worker.claim_once().run_id
    config = VerificationConfig(task_id=task_id, base_commit=base_commit, command_name="pytest",
        command=(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "--rootdir", str(public_test.parent), str(public_test)),
        target_tests=("scenarios::test_prompt_suffix[empty-suffix]", "scenarios::test_confirm_suffix[empty-suffix]"),
        pythonpath_relative="src", external_connections="none", independent_suite_version="click3019-private-v1")
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    outcome = {"stage": "initializing", "outcome": "incomplete"}
    try:
        with httpx.Client() as client:
            adapter = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=60)
            def model():
                return ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), generate=adapter, max_output_tokens=1800)
            planner_gateway = PlannerModelGateway(session_factory=factory, worker_id=worker.worker_id,
                estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), generate=adapter, max_output_tokens=1800)
            def process(directories, actual_commit, stop):
                outcome["stage"] = "initial_supervisor"
                assert actual_commit == base_commit
                receivers = worker.build_handoff_receivers(run_id, directories=directories, max_bytes=40000,
                    stop_event=stop, explorer_gateway=model(), planner_gateway=planner_gateway,
                    coder_gateway=model(), verifier_gateway=model(), verification_config=config,
                    max_patch_bytes=16000, verifier_max_diff_bytes=200000)
                def receiver(handoff_id):
                    with factory() as session:
                        entry = session.scalars(select(AgentExecution).where(AgentExecution.handoff_id == handoff_id)).one()
                        return entry.id, entry.agent_role, entry.status, entry.output
                def cycle(stage, supervise):
                    outcome["stage"] = stage
                    handoff = run_handoff_cycle(factory, run_id, worker.worker_id, supervise=supervise, receivers=receivers)
                    return receiver(handoff) if handoff else None
                item = cycle("explorer", worker.build_initial_supervise(run_id, model_gateway=model(), stop_event=stop))
                if item is None or item[1] != "explorer" or item[2] != AgentExecutionStatus.SUCCEEDED:
                    return {"stage": "explorer", "outcome": "blocked_or_failed"}
                seen_reads = set()
                def plan_from_explorer(explorer_id):
                    for _ in range(4):
                        with factory() as session:
                            prior = ContextBuilder(session).build_planner_input(run_id, explorer_execution_id=explorer_id)
                            fresh = {(entry["path"], entry["preview"], entry["truncated"]) for entry in prior["read_evidence"]}
                        if not fresh - seen_reads:
                            return None
                        seen_reads.update(fresh)
                        next_item = cycle("planner", worker.build_post_explorer_supervise(run_id, explorer_execution_id=explorer_id,
                            model_gateway=model(), stop_event=stop))
                        if next_item is None:
                            return None
                        if next_item[1] == "planner" and next_item[2] == AgentExecutionStatus.SUCCEEDED:
                            return next_item
                        if next_item[1] != "explorer" or next_item[2] != AgentExecutionStatus.SUCCEEDED:
                            return None
                        explorer_id = next_item[0]
                    return None
                item = plan_from_explorer(item[0])
                if item is None:
                    return {"stage": "planner", "outcome": "blocked_or_no_new_evidence"}
                planner_id = item[0]
                for _ in range(3):
                    item = cycle("baseline", worker.build_post_planner_supervise(run_id, planner_execution_id=planner_id,
                        model_gateway=model(), stop_event=stop))
                    if item is not None and item[1] == "explorer" and item[2] == AgentExecutionStatus.SUCCEEDED:
                        item = plan_from_explorer(item[0])
                        if item is None:
                            return {"stage": "reexplore", "outcome": "blocked_or_no_new_evidence"}
                        planner_id = item[0]
                        continue
                    if item is not None and item[1] == "verifier" and (item[3] or {}).get("outcome") == "BASELINE_RECORDED":
                        item = cycle("coder", worker.build_post_planner_supervise(run_id, planner_execution_id=planner_id,
                            model_gateway=model(), stop_event=stop))
                    if item is None or item[1] != "coder" or item[2] != AgentExecutionStatus.SUCCEEDED:
                        return {"stage": "coder", "outcome": "blocked_or_routed_elsewhere"}
                    if (item[3] or {}).get("outcome") == "PATCH_READY":
                        break
                    if (item[3] or {}).get("outcome") != "BLOCKED":
                        return {"stage": "coder", "outcome": "invalid_result"}
                    item = cycle("replan", worker.build_post_coder_supervise(run_id, coder_execution_id=item[0],
                        directories=directories, model_gateway=model(), max_patch_bytes=16000, stop_event=stop))
                    if item is None or item[1] != "planner" or item[2] != AgentExecutionStatus.SUCCEEDED:
                        return {"stage": "replan", "outcome": "blocked_or_routed_elsewhere"}
                    planner_id = item[0]
                else:
                    return {"stage": "coder", "outcome": "replan_limit"}
                coder_id = item[0]
                item = cycle("public_verifier", worker.build_post_coder_supervise(run_id, coder_execution_id=coder_id,
                    directories=directories, model_gateway=model(), max_patch_bytes=16000, stop_event=stop))
                if item is None or item[1] != "verifier" or (item[3] or {}).get("outcome") != "VERIFICATION_BLOCKED":
                    return {"stage": "public_verifier", "outcome": "blocked_or_routed_elsewhere"}
                if item[3].get("preliminary_public_outcome") != "passed":
                    return {"stage": "public_verifier", "outcome": item[3].get("preliminary_public_outcome", "unavailable")}
                outcome["stage"] = "independent_verifier"
                finalized = worker.finalize_with_independent_evaluation(run_id, agent_execution_id=item[0],
                    draft_artifact_id=UUID(item[3]["draft_artifact_id"]), directories=directories,
                    verification_config=config, image="python:3.11-slim", command=("python", "-B", "-c", adapter_text),
                    cases=cases, calibration_workspaces=(base_repo, reference_repo, reference_commit),
                    max_diff_bytes=200000, max_patch_bytes=16000, timeout_seconds=20, stop_event=stop)
                return {"stage": "independent_verifier", "outcome": finalized["outcome"]}
            outcome.update(worker.execute_run(run_id, workspaces_root=workspaces, artifacts_root=artifacts,
                heartbeat_config=HeartbeatConfig(statement_timeout_seconds=2, shutdown_timeout_seconds=5), process_run=process))
    except Exception as error:
        outcome.update({"outcome": "error", "error_type": type(error).__name__})
    finally:
        with factory() as session:
            def rows(model, query):
                return [{column.key: getattr(row, column.key) for column in inspect_model(model).columns}
                        for row in session.scalars(query).all()]
            trace = {"outcome": outcome, "task": rows(Task, select(Task).where(Task.id == task_id)),
                "runs": rows(Run, select(Run).where(Run.task_id == task_id)),
                "agents": rows(AgentExecution, select(AgentExecution).where(AgentExecution.run_id == run_id)),
                "tools": rows(ToolCall, select(ToolCall).where(ToolCall.run_id == run_id)),
                "artifacts": rows(Artifact, select(Artifact).join(AgentExecution,
                    Artifact.agent_execution_id == AgentExecution.id).where(AgentExecution.run_id == run_id)),
                "handoffs": rows(Handoff, select(Handoff).join(AgentExecution,
                    Handoff.from_agent_execution_id == AgentExecution.id).where(AgentExecution.run_id == run_id)),
                "failures": rows(FailureRecord, select(FailureRecord).where(FailureRecord.run_id == run_id)),
                "verifications": rows(Verification, select(Verification).where(Verification.run_id == run_id))}
            (result_root / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        if (artifacts / str(run_id)).is_dir():
            shutil.copytree(artifacts / str(run_id), result_root / "artifacts")
    print("live Click result:", outcome["stage"], outcome["outcome"])


@pytest.mark.parametrize("mode", ["model_blocked", "invalid_output", "insufficient_tokens"])
def test_initial_supervisor_does_not_invent_a_handoff_when_model_cannot_route(worker_audit_session_factory, mode):
    factory = worker_audit_session_factory
    worker = Worker(factory, "initial-model-blocked")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "budget": {**snapshot["budget"], "max_llm_tokens_per_run": 1 if mode == "insufficient_tokens" else 1000},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }
    run_id = worker.claim_once().run_id
    prompts = []
    def generate(prompt, _limit):
        prompts.append(prompt)
        output = ({"outcome": "BLOCKED", "reason": "Issue is ambiguous", "decision_required": "Clarify expected behavior"}
                  if mode == "model_blocked" else {"outcome": "EXPLORE", "reason": "Missing focus"})
        return ModelReply(output, ModelTokenUsage(input_tokens=10, output_tokens=10))
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        generate=generate, max_output_tokens=100,
    )
    if mode == "invalid_output":
        with pytest.raises(ValidationError):
            worker.execute_initial_supervisor(run_id, model_gateway=gateway, stop_event=Event())
        assert len(prompts) == 2
    else:
        result = worker.execute_initial_supervisor(run_id, model_gateway=gateway, stop_event=Event())
        assert result.outcome == "BLOCKED" and result.handoff_id is None
        assert len(prompts) == (1 if mode == "model_blocked" else 0)
    with factory() as session:
        execution = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == "supervisor",
        )).one()
        if mode == "invalid_output":
            assert execution.status == AgentExecutionStatus.FAILED
            assert execution.output["reason"] == "SUPERVISOR_OUTPUT_INVALID"
        else:
            assert execution.status == AgentExecutionStatus.SUCCEEDED and execution.output["outcome"] == "BLOCKED"
        assert session.scalars(select(Handoff)).all() == []


@pytest.mark.parametrize("reason", ["no_permission", "no_tools", "no_steps", "no_tokens"])
def test_initial_supervisor_blocks_without_starting_model_when_policy_cannot_support_exploration(
    worker_audit_session_factory, reason,
):
    factory = worker_audit_session_factory
    worker = Worker(factory, "initial-blocked-supervisor")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        budget = dict(snapshot["budget"])
        if reason == "no_tools":
            budget["max_tool_calls_per_run"] = 0
        elif reason == "no_steps":
            budget["max_graph_steps_per_run"] = 1
        elif reason == "no_tokens":
            budget["max_llm_tokens_per_run"] = 0
        task.policy_snapshot = {
            **snapshot, "budget": budget, "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "EXPLORER": [] if reason == "no_permission" else ["read_file"],
            }},
        }
    run_id = worker.claim_once().run_id
    def should_not_call(_prompt, _limit):
        pytest.fail("The model must not be called when the first route is unavailable")
    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 1,
        generate=should_not_call, max_output_tokens=10,
    )
    result = worker.execute_initial_supervisor(run_id, model_gateway=gateway, stop_event=Event())
    assert result.outcome == "BLOCKED" and result.handoff_id is None
    with factory() as session:
        execution = session.get(AgentExecution, result.execution_id)
        assert execution.output["outcome"] == "BLOCKED" and execution.llm_usage is None
        assert session.scalars(select(Handoff)).all() == []
        assert session.get(Run, run_id).status == RunStatus.RUNNING


def test_supervisor_commits_approved_handoff_with_its_result(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "handoff-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    create_eligible_task(factory)
    other_run_id = worker.claim_once().run_id

    with factory() as session:
        coordinator = RunCoordinator(session)
        planner_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={},
        )
        supervisor_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
        foreign_id = coordinator.start_agent_execution(
            other_run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={},
        )
        coordinator.finish_agent_execution(
            other_run_id, worker.worker_id, agent_execution_id=foreign_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"outcome": "NEEDS_EVIDENCE"}, llm_usage=None,
        )

    def approve(approver_id, source_id):
        decision = HandoffDecision.model_validate({
            "from_agent_execution_id": source_id,
            "to_agent_role": "EXPLORER",
            "context": {
                "completed_work": "Inspected the existing service evidence",
                "blocker": "The caller list is incomplete",
                "evidence_refs": [{"path": "auth.py"}],
                "decision_required": "Determine which entry points invoke the service",
                "scope_impact": "Permission placement is unresolved",
            },
        })
        with factory() as session:
            return RunCoordinator(session).finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=approver_id,
                status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None, approved_handoff=decision,
            )

    with pytest.raises(HandoffApprovalError, match="successful Supervisor"):
        approve(planner_id, planner_id)
    for invalid_source in (planner_id, foreign_id, supervisor_id):
        with pytest.raises(HandoffApprovalError, match="completed execution in the same Run"):
            approve(supervisor_id, invalid_source)
    with factory() as observer:
        assert observer.scalars(select(Handoff)).all() == []
        assert observer.get(AgentExecution, supervisor_id).status == AgentExecutionStatus.RUNNING
        assert observer.get(AgentExecution, planner_id).status == AgentExecutionStatus.RUNNING
    explorer_calls = []
    planner_calls = []
    receivers = {AgentRole.EXPLORER: explorer_calls.append, AgentRole.PLANNER: planner_calls.append}
    assert run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=lambda _: None, receivers=receivers,
    ) is None
    with pytest.raises(HandoffRoutingError, match="no committed Handoff approval"):
        run_handoff_cycle(
            factory, run_id, worker.worker_id, supervise=lambda _: supervisor_id, receivers=receivers,
        )
    assert explorer_calls == planner_calls == []

    with factory() as session:
        RunCoordinator(session).finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=planner_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"outcome": "NEEDS_EVIDENCE"}, llm_usage=None,
        )
    def supervise(selected_run_id):
        assert selected_run_id == run_id
        assert approve(supervisor_id, planner_id) is not None
        return supervisor_id

    handoff_id = run_handoff_cycle(factory, run_id, worker.worker_id, supervise=supervise, receivers=receivers)
    receipt, = explorer_calls
    assert receipt.id == handoff_id and receipt.from_agent_execution_id == planner_id
    assert receipt.to_agent_role == AgentRole.EXPLORER and planner_calls == []
    with pytest.raises(HandoffRoutingError, match="no committed Handoff approval"):
        run_handoff_cycle(
            factory, other_run_id, worker.worker_id, supervise=lambda _: supervisor_id, receivers=receivers,
        )
    assert explorer_calls == [receipt] and planner_calls == []
    with pytest.raises(AgentExecutionStateConflictError):
        approve(supervisor_id, planner_id)
    with factory() as observer:
        supervisor = observer.get(AgentExecution, supervisor_id)
        handoff = observer.scalars(select(Handoff)).one()
        assert supervisor.status == AgentExecutionStatus.SUCCEEDED
        assert supervisor.output == {"approved_handoff_id": str(handoff_id)}
        assert handoff.id == handoff_id and handoff.from_agent_execution_id == planner_id
        assert handoff.to_agent_role == "explorer"
        assert handoff.context["decision_required"] == "Determine which entry points invoke the service"

    with factory() as session:
        coordinator = RunCoordinator(session)
        for invalid_run, invalid_role in ((other_run_id, AgentRole.EXPLORER), (run_id, AgentRole.PLANNER)):
            with pytest.raises(HandoffApprovalError, match="receiving Run and Agent role"):
                coordinator.start_agent_execution(
                    invalid_run, worker.worker_id, role=invalid_role, agent_input={}, handoff_id=handoff_id,
                )
    barrier = Barrier(2, timeout=5)

    def start_receiving_execution():
        with factory() as session:
            barrier.wait()
            try:
                return RunCoordinator(session).start_agent_execution(
                    run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={}, handoff_id=handoff_id,
                )
            except HandoffAlreadyStartedError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [pool.submit(start_receiving_execution) for _ in range(2)]
        results = (first.result(), second.result())
    started, = [result for result in results if isinstance(result, UUID)]
    rejected, = [result for result in results if isinstance(result, HandoffAlreadyStartedError)]
    assert rejected.execution_id == started
    with factory() as observer:
        execution = observer.scalars(select(AgentExecution).where(AgentExecution.handoff_id == handoff_id)).one()
        assert execution.id == started and execution.run_id == run_id
        assert execution.status == AgentExecutionStatus.RUNNING
        assert observer.get(AgentExecution, planner_id).handoff_id is None


def test_graph_dispatches_committed_handoff_to_planner(worker_audit_session_factory, tmp_path):
    factory = worker_audit_session_factory
    worker = Worker(factory, "graph-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        explorer_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={},
        )
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=explorer_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"summary": "Inspected"}, llm_usage=None,
        )
        supervisor_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
        handoff_id = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor_id,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": explorer_id, "to_agent_role": "PLANNER",
                "context": {
                    "completed_work": "Read the repository", "blocker": None, "evidence_refs": [],
                    "decision_required": "Assess the fix", "scope_impact": "Within the issue",
                },
            }),
        )
    prompts = []
    gateway = PlannerModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 200,
        max_output_tokens=100,
        generate=lambda prompt, cap: (
            prompts.append(prompt) or ModelReply(
                {"outcome": "NEEDS_EVIDENCE", "evidence_gap": "Find callers", "affected_decision": "Choose scope"},
                ModelTokenUsage(input_tokens=100, output_tokens=20),
            )
        ),
    )
    receivers = worker.build_handoff_receivers(
        run_id, directories=RunDirectories(tmp_path / "workspace", tmp_path / "artifacts"),
        max_bytes=100, stop_event=Event(), explorer_gateway=gateway, planner_gateway=gateway,
    )
    assert run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=lambda _: supervisor_id, receivers=receivers,
    ) == handoff_id
    with pytest.raises(HandoffAlreadyStartedError):
        run_handoff_cycle(
            factory, run_id, worker.worker_id, supervise=lambda _: supervisor_id, receivers=receivers,
        )
    assert len(prompts) == 1 and "Assess the fix" in prompts[0]
    with factory() as observer:
        execution = observer.scalars(select(AgentExecution).where(AgentExecution.handoff_id == handoff_id)).one()
        assert execution.agent_role == "planner" and execution.status == AgentExecutionStatus.SUCCEEDED
        assert execution.input["handoff"]["decision_required"] == "Assess the fix"
        planner_id = execution.id

    with factory() as session:
        coordinator = RunCoordinator(session)
        second_supervisor_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
        explorer_handoff_id = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=second_supervisor_id,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": planner_id, "to_agent_role": "EXPLORER",
                "context": {
                    "completed_work": "Reviewed the evidence", "blocker": "Callers are unknown",
                    "evidence_refs": [], "decision_required": "Find callers", "scope_impact": "Unknown",
                },
            }),
        )
    explorer_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 200,
        max_output_tokens=100,
        generate=lambda prompt, cap: ModelReply(
            {"path": "auth.py"}, ModelTokenUsage(input_tokens=100, output_tokens=20),
        ),
    )
    receivers = worker.build_handoff_receivers(
        run_id, directories=RunDirectories(tmp_path / "workspace", tmp_path / "artifacts"),
        max_bytes=100, stop_event=Event(), explorer_gateway=explorer_gateway, planner_gateway=gateway,
    )
    assert run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=lambda _: second_supervisor_id, receivers=receivers,
    ) == explorer_handoff_id
    with factory() as observer:
        execution = observer.scalars(
            select(AgentExecution).where(AgentExecution.handoff_id == explorer_handoff_id),
        ).one()
        assert execution.input["issue"] == {
            "title": "Worker entry test",
            "description": "Verify one claim from a separate entry point",
        }
        assert execution.input["base_commit"] == "a" * 40
        assert execution.input["handoff"]["decision_required"] == "Find callers"
        assert execution.output["reason"] == "EXPLORER_READ_UNAVAILABLE"

    with factory() as session:
        coordinator = RunCoordinator(session)
        coder_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=coder_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"blocker": "Plan cannot be applied"}, llm_usage=None,
        )
        supervisor_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
        replan_id = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor_id,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": coder_id, "to_agent_role": "PLANNER",
                "context": {
                    "completed_work": "Checked the plan", "blocker": "Plan cannot be applied",
                    "evidence_refs": [{"explorer_execution_id": str(explorer_id)}],
                    "decision_required": "Revise the plan", "scope_impact": "Unknown",
                },
            }),
        )
    assert run_handoff_cycle(
        factory, run_id, worker.worker_id, supervise=lambda _: supervisor_id, receivers=receivers,
    ) == replan_id
    with factory() as observer:
        replan = observer.scalars(select(AgentExecution).where(AgentExecution.handoff_id == replan_id)).one()
        assert replan.input["explorer_execution_id"] == str(explorer_id)
        assert replan.input["handoff"]["blocker"] == "Plan cannot be applied"


@pytest.mark.parametrize("interruption_stage", ["before_write", "partial_write", "after_write"])
def test_coder_patch_check_uses_supervisor_approved_plan(
    worker_audit_session_factory, tmp_path, monkeypatch, interruption_stage,
):
    factory = worker_audit_session_factory
    worker = Worker(factory, "patch-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "CODER": ["patch_path_check", "apply_patch"],
            }},
        }
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        planner_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={})
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=planner_id,
            status=AgentExecutionStatus.SUCCEEDED, llm_usage=None,
            output={"outcome": "PLAN_READY", "plan": {
                "approach": "Fix authorization", "target_paths": ["auth.py"],
                "steps": ["Update auth"], "scope_limits": "Only auth.py", "verification": ["Test auth"],
            }},
        )
        supervisor_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
        handoff_id = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor_id,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": planner_id, "to_agent_role": "CODER",
                "context": {
                    "completed_work": "Plan prepared", "blocker": None, "evidence_refs": [],
                    "decision_required": "Implement", "scope_impact": "Only auth.py",
                },
            }),
        )
        coder_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.CODER, agent_input={}, handoff_id=handoff_id,
        )
        allowed = b"--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-old\n+new\n"
        denied = allowed + b"--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+new\n"
        allowed_id = coordinator.execute_patch_path_check(
            run_id, worker.worker_id, agent_execution_id=coder_id, workspace_root=tmp_path, patch=allowed,
        )
        denied_id = coordinator.execute_patch_path_check(
            run_id, worker.worker_id, agent_execution_id=coder_id, workspace_root=tmp_path, patch=denied,
        )
        (tmp_path / "auth.py").write_text("old\n")
        (tmp_path / "config.py").write_text("old\n")
        denied_apply_id = coordinator.execute_apply_patch(
            run_id, worker.worker_id, agent_execution_id=coder_id,
            workspace_root=tmp_path, patch=denied, stop_event=Event(),
        )
        assert (tmp_path / "auth.py").read_text() == "old\n"
        assert (tmp_path / "config.py").read_text() == "old\n"
        artifact_root = tmp_path.parent / f"{tmp_path.name}-artifacts"
        artifact_root.mkdir()
        artifact_id = uuid4()
        artifact_path, digest = write_artifact_file(artifact_root, artifact_id, BytesIO(allowed), chunk_size=16)
        with factory.begin() as writer:
            writer.add(Artifact(
                id=artifact_id, agent_execution_id=coder_id, kind="patch",
                content_ref=str(artifact_path), content_hash=digest,
            ))
        artifact_path.write_bytes(b"tampered")
        tampered_id = coordinator.execute_apply_patch_artifact(
            run_id, worker.worker_id, agent_execution_id=coder_id, workspace_root=tmp_path,
            artifact_root=artifact_root, artifact_id=artifact_id, max_bytes=4096, stop_event=Event(),
        )
        assert (tmp_path / "auth.py").read_text() == "old\n"
        artifact_path.write_bytes(allowed)
        applied_id = coordinator.execute_apply_patch_artifact(
            run_id, worker.worker_id, agent_execution_id=coder_id,
            workspace_root=tmp_path, artifact_root=artifact_root, artifact_id=artifact_id,
            max_bytes=4096, stop_event=Event(),
        )
    with factory() as observer:
        assert observer.get(ToolCall, allowed_id).status == ToolCallStatus.SUCCEEDED
        assert observer.get(ToolCall, allowed_id).tool_name == "patch_path_check"
        rejected = observer.get(ToolCall, denied_id)
        assert rejected.status == ToolCallStatus.DENIED and rejected.reason == "PATCH_PATH_DENIED"
        assert "patch_sha256" in rejected.args
        assert observer.get(ToolCall, denied_apply_id).status == ToolCallStatus.DENIED
        assert observer.get(ToolCall, tampered_id).reason == "PATCH_ARTIFACT_INVALID"
        applied = observer.get(ToolCall, applied_id)
        assert applied.status == ToolCallStatus.SUCCEEDED and applied.tool_name == "apply_patch"
        assert applied.args == {"artifact_id": str(artifact_id), "recount": True}
    assert (tmp_path / "auth.py").read_text() == "new\n"
    assert (tmp_path / "config.py").read_text() == "old\n"

    if os.name == "nt":
        from contextlib import contextmanager

        original_start = harness_module._start_git_process
        started = Event()
        stop_event = Event()
        key = int.from_bytes(run_id.bytes[:8], byteorder="big")

        @contextmanager
        def slow_apply(command, *, cwd, stdout, stderr):
            if command[1:4] != ["apply", "--recount", "--"]:
                with original_start(command, cwd=cwd, stdout=stdout, stderr=stderr) as process:
                    yield process
                return
            if interruption_stage == "partial_write":
                script = (
                    "from pathlib import Path; import time; "
                    "Path('auth.py').write_text('partial\\n'); "
                    "Path('partial-ready').write_text('ready'); time.sleep(60)"
                )
            else:
                script = "import subprocess,sys,time; time.sleep(60); subprocess.run(sys.argv[1:],check=True)"
            delayed = [sys.executable, "-c", script, *command]
            with original_start(delayed, cwd=cwd, stdout=stdout, stderr=stderr) as process:
                started.set()
                yield process

        if interruption_stage != "after_write":
            monkeypatch.setattr(harness_module, "_start_git_process", slow_apply)
        patch = b"--- a/auth.py\n+++ b/auth.py\n@@ -1 +1 @@\n-new\n+changed\n"

        def terminate_connection():
            with factory.kw["bind"].begin() as observer:
                pid = observer.scalar(text(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' "
                    "AND classid=:high AND objid=:low AND granted"
                ), {"high": key >> 32, "low": key & 0xffffffff})
                assert pid is not None
                assert observer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

        def apply_after_write(*args, **kwargs):
            original_apply(*args, **kwargs)
            terminate_connection()

        if interruption_stage == "after_write":
            original_apply = coordinator_module.apply_validated_patch
            monkeypatch.setattr(coordinator_module, "apply_validated_patch", apply_after_write)
            with factory() as session, pytest.raises((GitExecutionCancelledError, WorkspaceLockUnavailableError)):
                RunCoordinator(session).execute_apply_patch(
                    run_id, worker.worker_id, agent_execution_id=coder_id,
                    workspace_root=tmp_path, patch=patch, stop_event=stop_event,
                )
        else:
            def interrupt_lock():
                assert started.wait(timeout=10)
                if interruption_stage == "partial_write":
                    deadline = monotonic() + 10
                    while not (tmp_path / "partial-ready").exists():
                        if monotonic() >= deadline:
                            raise TimeoutError("The simulated partial write did not start")
                        Event().wait(0.02)
                terminate_connection()

            with ThreadPoolExecutor(max_workers=1) as pool:
                interruption = pool.submit(interrupt_lock)
                with factory() as session, pytest.raises(GitExecutionCancelledError):
                    RunCoordinator(session).execute_apply_patch(
                        run_id, worker.worker_id, agent_execution_id=coder_id,
                        workspace_root=tmp_path, patch=patch, stop_event=stop_event,
                    )
                interruption.result(timeout=10)
        if interruption_stage != "after_write":
            assert stop_event.is_set()
        with factory() as observer:
            pending = observer.scalars(select(ToolCall).where(
                ToolCall.run_id == run_id, ToolCall.tool_name == "apply_patch",
                ToolCall.status == ToolCallStatus.REQUESTED,
            )).one()
            assert pending.exit_code is None
        with pytest.raises(WorkspaceQuarantinedError):
            with workspace_operation_lock(factory.kw["bind"], run_id, check_unsettled_mutations=True):
                pytest.fail("Workspace with an unresolved patch must stay isolated")
        expected = {"partial_write": "partial\n", "before_write": "new\n", "after_write": "changed\n"}[
            interruption_stage
        ]
        assert (tmp_path / "auth.py").read_text() == expected


def test_workspace_lock_blocks_other_operations_and_releases_after_exception(run_session_factory):
    engine = run_session_factory.kw["bind"]
    run_id = uuid4()
    other_run_id = uuid4()
    acquired = Event()
    release = Event()

    def holder():
        with pytest.raises(RuntimeError, match="simulated failure"):
            with workspace_operation_lock(engine, run_id):
                acquired.set()
                assert release.wait(timeout=5)
                raise RuntimeError("simulated failure")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(holder)
        assert acquired.wait(timeout=5)
        with workspace_operation_lock(engine, other_run_id, timeout_seconds=1):
            pass
        with pytest.raises(WorkspaceLockUnavailableError, match="timed out"):
            with workspace_operation_lock(engine, run_id, timeout_seconds=0.2):
                pass
        release.set()
        future.result(timeout=5)
    with workspace_operation_lock(engine, run_id, timeout_seconds=1):
        pass


def test_workspace_lock_wait_can_be_stopped(run_session_factory):
    engine = run_session_factory.kw["bind"]
    run_id = uuid4()
    waiting = Event()
    entered = Event()

    class ObservedStopEvent(Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    stop_event = ObservedStopEvent()

    def waiting_operation():
        with pytest.raises(WorkspaceLockUnavailableError, match="stopped"):
            with workspace_operation_lock(engine, run_id, stop_event=stop_event, timeout_seconds=5):
                entered.set()

    with workspace_operation_lock(engine, run_id):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(waiting_operation)
            assert waiting.wait(timeout=5)
            stop_event.set()
            future.result(timeout=5)
        assert not entered.is_set()
    with workspace_operation_lock(engine, run_id, timeout_seconds=1):
        pass


@pytest.mark.parametrize("status", [
    ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING, ToolCallStatus.UNKNOWN_RESULT,
])
def test_workspace_quarantines_unsettled_mutation(worker_audit_session_factory, status):
    factory = worker_audit_session_factory
    engine = factory.kw["bind"]
    worker = Worker(factory, "quarantine-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory.begin() as session:
        call_id = ToolCallRepository(session).add_requested(None, "apply_patch", {}, run_id=run_id)
        session.get(ToolCall, call_id).status = status

    entered = False
    with pytest.raises(WorkspaceQuarantinedError):
        with workspace_operation_lock(engine, run_id, check_unsettled_mutations=True):
            entered = True
    assert not entered
    with factory.begin() as session:
        session.get(ToolCall, call_id).status = ToolCallStatus.SUCCEEDED
    with workspace_operation_lock(engine, run_id, check_unsettled_mutations=True):
        entered = True
    assert entered


def test_workspace_quarantine_survives_lock_connection_termination(worker_audit_session_factory):
    factory = worker_audit_session_factory
    engine = factory.kw["bind"]
    worker = Worker(factory, "disconnect-test-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    lock_key = int.from_bytes(run_id.bytes[:8], byteorder="big", signed=True)

    with engine.connect() as old_connection:
        pid = old_connection.scalar(text("SELECT pg_backend_pid()"))
        old_connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        with factory.begin() as session:
            call_id = ToolCallRepository(session).add_requested(None, "apply_patch", {}, run_id=run_id)
        with engine.begin() as terminator:
            assert terminator.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        old_connection.invalidate()

    with pytest.raises(WorkspaceQuarantinedError):
        with workspace_operation_lock(engine, run_id, check_unsettled_mutations=True, timeout_seconds=2):
            pytest.fail("Unsettled mutation must prevent workspace entry")
    with factory() as observer:
        assert observer.get(ToolCall, call_id).status == ToolCallStatus.REQUESTED


def test_old_lock_receipt_rejects_new_lock_owner(run_session_factory):
    factory = run_session_factory
    engine = factory.kw["bind"]
    run_id = uuid4()
    key = int.from_bytes(run_id.bytes[:8], byteorder="big", signed=True)
    with engine.connect() as old_connection:
        old_pid = old_connection.scalar(text("SELECT pg_backend_pid()"))
        old_connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        receipt = WorkspaceLockReceipt(old_pid, run_id)
        with factory() as observer:
            receipt.assert_held(observer)
        with engine.begin() as terminator:
            assert terminator.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": old_pid})
        old_connection.invalidate()

    with engine.connect() as new_connection:
        new_pid = new_connection.scalar(text("SELECT pg_backend_pid()"))
        assert new_pid != old_pid
        new_connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        try:
            with factory() as observer, pytest.raises(WorkspaceLockUnavailableError, match="no longer held"):
                receipt.assert_held(observer)
        finally:
            assert new_connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def test_workspace_lock_connection_loss_sets_stop_signal(run_session_factory):
    engine = run_session_factory.kw["bind"]
    run_id = uuid4()
    key = int.from_bytes(run_id.bytes[:8], byteorder="big")
    stop_event = Event()
    with pytest.raises((WorkspaceLockUnavailableError, DBAPIError)):
        with workspace_operation_lock(engine, run_id, stop_event=stop_event):
            with engine.begin() as observer:
                pid = observer.scalar(text(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' "
                    "AND classid=:high AND objid=:low AND granted"
                ), {"high": key >> 32, "low": key & 0xffffffff})
                assert pid is not None
                assert observer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            assert stop_event.wait(timeout=5)
    assert stop_event.is_set()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows Job Object process cleanup")
def test_lock_disconnect_stops_real_managed_process(run_session_factory):
    engine = run_session_factory.kw["bind"]
    run_id = uuid4()
    key = int.from_bytes(run_id.bytes[:8], byteorder="big")
    stop_event = Event()
    cleaned = False

    with pytest.raises((WorkspaceLockUnavailableError, DBAPIError)):
        with workspace_operation_lock(engine, run_id, stop_event=stop_event):
            with TemporaryFile() as stdout, TemporaryFile() as stderr:
                with harness_module._start_git_process(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    cwd=None, stdout=stdout, stderr=stderr,
                ) as process:
                    assert process.poll() is None
                    try:
                        with engine.begin() as observer:
                            pid = observer.scalar(text(
                                "SELECT pid FROM pg_locks WHERE locktype='advisory' "
                                "AND classid=:high AND objid=:low AND granted"
                            ), {"high": key >> 32, "low": key & 0xffffffff})
                            assert pid is not None
                            assert observer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                        with pytest.raises(GitExecutionCancelledError):
                            harness_module._wait_for_git(
                                process, deadline=monotonic() + 10, timeout_seconds=10, stop_event=stop_event,
                            )
                    finally:
                        harness_module._stop_git_process(process)
                    assert process.poll() is not None
                    assert process.active_process_count() == 0
                    cleaned = True
    assert stop_event.is_set() and cleaned


def test_workspace_lock_cleanup_failure_preserves_operation_error():
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def scalar(self, statement, parameters=None):
            if "pg_try_advisory_lock" in str(statement):
                return True
            if "pg_backend_pid" in str(statement):
                return 123
            raise OSError("simulated disconnected lock session")

        def invalidate(self):
            pass

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    with pytest.raises(ValueError, match="operation failed") as exc_info:
        with workspace_operation_lock(FakeEngine(), uuid4()):
            raise ValueError("operation failed")
    assert any("Workspace lock cleanup could not be confirmed" in note for note in exc_info.value.__notes__)


def test_expired_run_marks_only_unsettled_tool_calls_unknown(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "expired-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    create_eligible_task(factory)
    other_run_id = worker.claim_once().run_id
    with factory.begin() as session:
        calls = ToolCallRepository(session)
        requested_id = calls.add_requested(None, "git_clone", {}, run_id=run_id)
        running_id = calls.add_requested(None, "git_checkout", {}, run_id=run_id)
        finished_id = calls.add_requested(None, "git_clone", {}, run_id=run_id)
        other_id = calls.add_requested(None, "git_clone", {}, run_id=other_run_id)
        session.get(ToolCall, running_id).status = ToolCallStatus.RUNNING
        calls.save_result(finished_id, status=ToolCallStatus.SUCCEEDED, exit_code=0)
    with factory() as session:
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session).mark_expired_run_tool_calls_unknown(run_id)
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session).get_expired_run_evidence(run_id)
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    with factory() as session:
        coordinator = RunCoordinator(session)
        before = coordinator.get_expired_run_evidence(run_id)
        by_id = {call["tool_call_id"]: call for call in before["tool_calls"]}
        assert before["run_id"] == str(run_id)
        assert by_id[str(requested_id)]["status"] == "requested"
        assert by_id[str(running_id)]["status"] == "running"
        assert str(other_id) not in by_id
        assert coordinator.mark_expired_run_tool_calls_unknown(run_id) == 2
        assert coordinator.mark_expired_run_tool_calls_unknown(run_id) == 0
        after = coordinator.get_expired_run_evidence(run_id)
        assert next(call for call in after["tool_calls"] if call["tool_call_id"] == str(running_id))["status"] == "unknown_result"
    with factory() as observer:
        for call_id in (requested_id, running_id):
            call = observer.get(ToolCall, call_id)
            assert call.status == ToolCallStatus.UNKNOWN_RESULT
            assert call.reason == "WORKER_LEASE_EXPIRED" and call.exit_code is None
        assert observer.get(ToolCall, finished_id).status == ToolCallStatus.SUCCEEDED
        assert observer.get(ToolCall, other_id).status == ToolCallStatus.REQUESTED


def test_discovery_marks_expired_run_once_without_touching_live_run(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "recovery-worker")
    create_eligible_task(factory)
    expired_run_id = worker.claim_once().run_id
    create_eligible_task(factory)
    live_run_id = worker.claim_once().run_id
    with factory.begin() as session:
        calls = ToolCallRepository(session)
        expired_call_id = calls.add_requested(None, "git_clone", {}, run_id=expired_run_id)
        live_call_id = calls.add_requested(None, "git_clone", {}, run_id=live_run_id)
        session.get(Run, expired_run_id).lease_expires_at = func.clock_timestamp()
    barrier = Barrier(2, timeout=5)

    def discover():
        with factory() as session:
            barrier.wait()
            return RunCoordinator(session).mark_next_expired_run_tool_calls_unknown()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [pool.submit(discover) for _ in range(2)]
        results = [first.result(), second.result()]
    assert results.count(expired_run_id) == 1 and results.count(None) == 1
    with factory() as observer:
        assert observer.get(ToolCall, expired_call_id).status == ToolCallStatus.UNKNOWN_RESULT
        assert observer.get(ToolCall, live_call_id).status == ToolCallStatus.REQUESTED
        assert observer.get(Run, expired_run_id).status == RunStatus.RUNNING


def test_failure_record_migration_roundtrip_and_persistence(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "failure-record-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    revision = scripts.get_revision("a43cb50f67e9").module
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.downgrade()
            revision.upgrade()
    with factory.begin() as session:
        # Test the table only. No recovery decision is made in this fixture.
        record = FailureRecord(
            run_id=run_id, category=FailureCategory.UNKNOWN_FAILURE,
            signature="test-fingerprint", recovery_action="REDIAGNOSE",
        )
        session.add(record)
        session.flush()
        record_id = record.id
    with factory() as observer:
        saved = observer.get(FailureRecord, record_id)
        assert saved.run_id == run_id and saved.category == FailureCategory.UNKNOWN_FAILURE
        assert saved.signature == "test-fingerprint" and saved.recovery_action == "REDIAGNOSE"
    create_eligible_task(factory)
    other_run_id = worker.claim_once().run_id
    with factory.begin() as session:
        task_id = session.get(Run, run_id).task_id
        now = datetime.now(timezone.utc)
        old = Run(task_id=task_id, attempt_number=2, status=RunStatus.FAILED, worker_id=worker.worker_id,
                  creation_idempotency_key="historic-2", lease_expires_at=now, finished_at=now)
        missing = Run(task_id=task_id, attempt_number=3, status=RunStatus.FAILED, worker_id=worker.worker_id,
                      creation_idempotency_key="historic-3", lease_expires_at=now, finished_at=now)
        session.add_all([old, missing])
        session.flush()
        session.add_all([
            FailureRecord(run_id=old.id, category=FailureCategory.UNKNOWN_FAILURE,
                          signature="test-fingerprint", recovery_action="REDIAGNOSE"),
            FailureRecord(run_id=old.id, category=FailureCategory.UNKNOWN_FAILURE,
                          signature="test-fingerprint", recovery_action="REDIAGNOSE"),
            FailureRecord(run_id=other_run_id, category=FailureCategory.UNKNOWN_FAILURE,
                          signature="test-fingerprint", recovery_action="REDIAGNOSE"),
        ])
    with factory() as session:
        history = RunCoordinator(session).observe_recovery_history(run_id, worker.worker_id, signature="test-fingerprint")
        assert (history.retries_started, history.previous_signature_occurrences, history.failed_runs_without_record) == (2, 1, 1)
        assert history.rediagnoses_started is None
    with factory() as session, pytest.raises(RunExecutionDeniedError):
        RunCoordinator(session).observe_recovery_history(run_id, "other-worker", signature="test-fingerprint")


def test_close_expired_run_commits_failure_and_unknown_tools_together(worker_audit_session_factory, monkeypatch):
    factory = worker_audit_session_factory
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            scripts.get_revision("a43cb50f67e9").module.upgrade()
            # Closing an expired Run also records the missing acceptance evidence.
            scripts.get_revision("f154b6e82639").module.upgrade()
    worker = Worker(factory, "recovery-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory.begin() as session:
        call_id = ToolCallRepository(session).add_requested(None, "git_clone", {}, run_id=run_id)
    decision = FailureDecision(
        category=FailureCategory.UNKNOWN_FAILURE, signature="lease-expired:test", recovery_action="REDIAGNOSE",
    )
    with factory() as session:
        with pytest.raises(RunLeaseNotExpiredError):
            RunCoordinator(session)._commit_expired_run_decision(run_id, decision=decision)
    with factory() as observer:
        assert observer.get(Run, run_id).status == RunStatus.RUNNING
        assert observer.get(ToolCall, call_id).status == ToolCallStatus.REQUESTED
        assert observer.scalars(select(FailureRecord)).all() == []
    with factory.begin() as session:
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    with factory() as session:
        # An expired Run admits only UNKNOWN_FAILURE with rediagnosis or escalation;
        # a concrete action is rejected before anything is written.
        with pytest.raises(RecoveryDecisionUnavailableError, match="rediagnosis or escalation"):
            RunCoordinator(session).close_expired_run(run_id, decision=FailureDecision(
                category=FailureCategory.UNKNOWN_FAILURE, signature="lease-expired:test",
                recovery_action="RETRY_SAME_STEP"))
    with factory() as observer:
        assert observer.get(Run, run_id).status == RunStatus.RUNNING
        assert observer.get(ToolCall, call_id).status == ToolCallStatus.REQUESTED
        assert observer.scalars(select(FailureRecord)).all() == []
    with factory() as session:
        def fail_flush():
            raise RuntimeError("simulated failure record insert failure")

        monkeypatch.setattr(session, "flush", fail_flush)
        with pytest.raises(RuntimeError, match="simulated failure"):
            RunCoordinator(session)._commit_expired_run_decision(run_id, decision=decision)
    with factory() as observer:
        assert observer.get(Run, run_id).status == RunStatus.RUNNING
        assert observer.get(ToolCall, call_id).status == ToolCallStatus.REQUESTED
        assert observer.scalars(select(FailureRecord)).all() == []
    barrier = Barrier(2, timeout=5)

    def close_run():
        with factory() as session:
            barrier.wait()
            try:
                return RunCoordinator(session)._commit_expired_run_decision(run_id, decision=decision)
            except RunLeaseNotExpiredError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [pool.submit(close_run) for _ in range(2)]
        results = [first.result(timeout=10), second.result(timeout=10)]
    failure_id, = [result for result in results if result is not None]
    assert results.count(None) == 1
    with factory() as observer:
        run = observer.get(Run, run_id)
        failure, = observer.scalars(select(FailureRecord)).all()
        assert run.status == RunStatus.FAILED and run.finished_at is not None
        assert failure.id == failure_id and failure.run_id == run_id
        assert failure.category == FailureCategory.UNKNOWN_FAILURE
        assert observer.get(ToolCall, call_id).status == ToolCallStatus.UNKNOWN_RESULT


def test_coder_patch_reference_requires_owned_patch_artifact(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "coder-artifact-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        coder_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
        other_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
    with factory.begin() as session:
        own_patch = Artifact(agent_execution_id=coder_id, kind="patch", content_ref="patch.bin", content_hash="a" * 64)
        wrong_kind = Artifact(agent_execution_id=coder_id, kind="analysis", content_ref="analysis.bin", content_hash="b" * 64)
        foreign_patch = Artifact(agent_execution_id=other_id, kind="patch", content_ref="foreign.bin", content_hash="c" * 64)
        session.add_all([own_patch, wrong_kind, foreign_patch])
        session.flush()
        own_id, wrong_id, foreign_id = own_patch.id, wrong_kind.id, foreign_patch.id

    def finish(artifact_id):
        with factory() as session:
            RunCoordinator(session).finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=coder_id, status=AgentExecutionStatus.SUCCEEDED,
                output={"outcome": "PATCH_READY", "patch_artifact_id": str(artifact_id), "summary": "Patch ready"},
                llm_usage=None,
            )

    for invalid_id in (uuid4(), wrong_id, foreign_id):
        with pytest.raises(CoderArtifactError):
            finish(invalid_id)
    with factory() as observer:
        assert observer.get(AgentExecution, coder_id).status == AgentExecutionStatus.RUNNING
    finish(own_id)
    with factory() as observer:
        assert observer.get(AgentExecution, coder_id).output["patch_artifact_id"] == str(own_id)


def test_coder_patch_artifact_and_result_commit_atomically(worker_audit_session_factory, tmp_path, monkeypatch):
    factory = worker_audit_session_factory
    worker = Worker(factory, "coder-commit-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        coder_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.CODER, agent_input={},
        )
    artifact_id = uuid4()
    path, digest = write_artifact_file(tmp_path, artifact_id, BytesIO(b"patch bytes"), chunk_size=4)
    artifact = ToolArtifactResult(id=artifact_id, kind="patch", content_ref=str(path), content_hash=digest)
    output = {"outcome": "PATCH_READY", "patch_artifact_id": str(artifact_id), "summary": "Patch ready"}

    with factory() as session:
        original_flush = session.flush

        def fail_after_flush(*args, **kwargs):
            original_flush(*args, **kwargs)
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(session, "flush", fail_after_flush)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            RunCoordinator(session).finish_agent_execution(
                run_id, worker.worker_id, agent_execution_id=coder_id, status=AgentExecutionStatus.SUCCEEDED,
                output=output, llm_usage=None, patch_artifact=artifact,
            )
    with factory() as observer:
        assert observer.get(Artifact, artifact_id) is None
        assert observer.get(AgentExecution, coder_id).status == AgentExecutionStatus.RUNNING
        assert path.read_bytes() == b"patch bytes"

    with factory() as session:
        RunCoordinator(session).finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=coder_id, status=AgentExecutionStatus.SUCCEEDED,
            output=output, llm_usage=None, patch_artifact=artifact,
        )
    with factory() as observer:
        assert observer.get(Artifact, artifact_id).agent_execution_id == coder_id
        assert observer.get(AgentExecution, coder_id).output == output
        assert observer.get(AgentExecution, coder_id).status == AgentExecutionStatus.SUCCEEDED


@pytest.mark.skipif(os.name != "nt", reason="Trusted baseline needs the Windows Job pytest report")
@pytest.mark.parametrize("outcome", ["patch", "blocked", "out_of_scope", "apply_fails", "read_then_patch", "read_denied", "baseline_tampered_after_start", "format_then_patch", "format_exhausted", "format_no_budget", "recount_patch", "json_then_patch", "json_exhausted", "json_no_budget"])
def test_coder_executes_approved_plan_with_guarded_patch(worker_audit_session_factory, tmp_path, outcome):
    factory = worker_audit_session_factory
    worker = Worker(factory, "coder-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "max_output_repairs_per_agent_execution":
                                   0 if outcome in ("format_no_budget", "json_no_budget") else snapshot["budget"]["max_output_repairs_per_agent_execution"]},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "CODER": ["apply_patch", "read_file"] if outcome == "read_then_patch" else ["apply_patch"],
                "VERIFIER": ["run_command"],
            }},
        }
    run_id = worker.claim_once().run_id
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "auth.py").write_text("old\n")
    (workspace / "config.py").write_text("old\n")
    with factory() as session:
        coordinator = RunCoordinator(session)
        planner_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={})
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=planner_id, status=AgentExecutionStatus.SUCCEEDED,
            output={"outcome": "PLAN_READY", "plan": {
                "approach": "Fix authorization", "target_paths": ["auth.py"], "steps": ["Update auth"],
                "scope_limits": "Only auth.py", "verification": ["Test auth"],
            }}, llm_usage=None,
        )
    baseline_id = create_test_baseline(factory, worker, run_id, planner_id, workspace, artifacts)
    with factory() as session:
        baseline_execution = session.get(AgentExecution, baseline_id)
        baseline_artifact = session.get(Artifact, UUID(baseline_execution.output["report_artifact_id"]))
        baseline_path = Path(baseline_artifact.content_ref)
    with factory() as session:
        coordinator = RunCoordinator(session)
        supervisor_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={},
        )
        handoff_id = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor_id,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": planner_id, "to_agent_role": "CODER",
                "context": {
                    "completed_work": "Plan prepared", "blocker": None,
                    "evidence_refs": [{"baseline_verifier_id": str(baseline_id)}],
                    "decision_required": "Implement", "scope_impact": "Only auth.py",
                },
            }),
        )
    path = "config.py" if outcome == "out_of_scope" else "auth.py"
    patch = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
    if outcome == "recount_patch":
        patch = patch.replace("@@ -1 +1 @@", "@@ -1,2 +1 @@")
    if outcome == "apply_fails":
        patch = patch.replace("-old\n", "-different\n")
    if outcome == "blocked":
        response = {
            "outcome": "BLOCKED", "completed_work": "Read the plan", "blocker": "Code evidence is missing",
            "evidence_refs": [{"path": "auth.py"}], "decision_required": "Inspect the file",
            "scope_impact": "Unknown",
        }
    else:
        response = {"outcome": "PATCH_DRAFT", "patch": patch, "summary": "Change authorization"}

    model_requests = []

    def generate(prompt, _):
        assert "Fix authorization" in prompt and "auth.py" in prompt
        model_requests.append(prompt)
        if outcome == "baseline_tampered_after_start":
            baseline_path.write_bytes(b"tampered after coder start")
        if outcome in ("read_then_patch", "read_denied") and len(model_requests) == 1:
            return ModelReply({"outcome": "READ_FILE", "path": "auth.py"}, ModelTokenUsage(input_tokens=100, output_tokens=20))
        if outcome == "read_then_patch":
            prompt_data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            assert prompt_data["reads"][0]["preview"].replace("\r\n", "\n") == "old\n"
            assert prompt_data["reads"][0]["truncated"] is False
        if outcome in ("format_then_patch", "format_exhausted", "format_no_budget"):
            if len(model_requests) == 1 or outcome in ("format_exhausted", "format_no_budget"):
                bad_patch = patch.replace("@@ -1 +1 @@", "@@ -x +1 @@")
                return ModelReply({"outcome": "PATCH_DRAFT", "patch": bad_patch, "summary": "Bad format"},
                                  ModelTokenUsage(input_tokens=100, output_tokens=50))
            prompt_data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            assert prompt_data["patch_feedback"][0]["reason"] == "PATCH_FORMAT_INVALID"
            assert prompt_data["patch_feedback"][0]["stderr"]
        if outcome in ("json_then_patch", "json_exhausted", "json_no_budget"):
            if len(model_requests) == 1 or outcome in ("json_exhausted", "json_no_budget"):
                return ModelReply({"outcome": "PATCH_DRAFT"}, ModelTokenUsage(input_tokens=100, output_tokens=20))
            prompt_data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            assert prompt_data["patch_feedback"][0]["reason"] == "CODER_OUTPUT_INVALID"
        return ModelReply(response, ModelTokenUsage(input_tokens=100, output_tokens=50))

    gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 300,
        generate=generate, max_output_tokens=200,
    )
    receivers = worker.build_handoff_receivers(
        run_id, directories=RunDirectories(workspace, artifacts), max_bytes=4096, stop_event=Event(),
        explorer_gateway=gateway, planner_gateway=gateway, coder_gateway=gateway, max_patch_bytes=4096,
    )

    def execute():
        return run_handoff_cycle(
            factory, run_id, worker.worker_id, supervise=lambda _: supervisor_id, receivers=receivers,
        )

    if outcome in ("json_exhausted", "json_no_budget"):
        with pytest.raises(ValidationError):
            execute()
    elif outcome in ("out_of_scope", "apply_fails", "baseline_tampered_after_start", "format_exhausted", "format_no_budget"):
        with pytest.raises(ValueError, match="not successful"):
            execute()
    else:
        assert execute() == handoff_id

    with factory() as observer:
        execution = observer.scalars(select(AgentExecution).where(AgentExecution.handoff_id == handoff_id)).one()
        if outcome in ("out_of_scope", "apply_fails", "baseline_tampered_after_start", "format_exhausted", "format_no_budget", "json_exhausted", "json_no_budget"):
            assert execution.status == AgentExecutionStatus.FAILED
            calls = observer.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution.id)).all()
            if outcome in ("json_exhausted", "json_no_budget"):
                assert calls == [] and len(model_requests) == (2 if outcome == "json_exhausted" else 1)
            elif outcome == "format_exhausted":
                assert len(calls) == 2 and all(call.reason == "PATCH_FORMAT_INVALID" for call in calls)
            elif outcome == "format_no_budget":
                assert len(calls) == 1 and calls[0].reason == "PATCH_FORMAT_INVALID" and len(model_requests) == 1
            elif outcome == "out_of_scope":
                call, = calls
                assert call.status == ToolCallStatus.DENIED and call.reason == "PATCH_PATH_DENIED"
            elif outcome == "apply_fails":
                call, = calls
                assert call.status == ToolCallStatus.FAILED and call.reason == "PATCH_APPLY_FAILED"
                assert call.exit_code != 0 and "patch" in call.stderr_excerpt.lower()
            else:
                call, = calls
                assert call.status == ToolCallStatus.DENIED and call.reason == "BASELINE_UNAVAILABLE"
        else:
            assert execution.status == AgentExecutionStatus.SUCCEEDED
            if outcome in ("patch", "read_then_patch", "format_then_patch", "recount_patch", "json_then_patch"):
                artifact = observer.get(Artifact, UUID(execution.output["patch_artifact_id"]))
                assert Path(artifact.content_ref).read_bytes() == patch.encode()
                assert artifact.agent_execution_id == execution.id
                patch_call = observer.scalars(select(ToolCall).where(
                    ToolCall.agent_execution_id == execution.id, ToolCall.tool_name == "apply_patch",
                    ToolCall.status == ToolCallStatus.SUCCEEDED,
                )).one()
                assert patch_call.args["recount"] is True
                context = ContextBuilder(observer).build_verifier_patch_input(
                    run_id, coder_execution_id=execution.id, patch_call_id=patch_call.id,
                    artifact_root=artifacts, max_patch_bytes=4096,
                )
                assert context["patch"] == patch and context["patch_sha256"] == artifact.content_hash
                with pytest.raises(VerifierPatchEvidenceError):
                    ContextBuilder(observer).build_verifier_patch_input(
                        run_id, coder_execution_id=execution.id, patch_call_id=uuid4(),
                        artifact_root=artifacts, max_patch_bytes=4096,
                    )
                original_args = patch_call.args
                patch_call.args = {"patch_sha256": "0" * 64}
                with pytest.raises(VerifierPatchEvidenceError, match="does not match"):
                    ContextBuilder(observer).build_verifier_patch_input(
                        run_id, coder_execution_id=execution.id, patch_call_id=patch_call.id,
                        artifact_root=artifacts, max_patch_bytes=4096,
                    )
                patch_call.args = original_args
                artifact_path = Path(artifact.content_ref)
                artifact_path.write_bytes(b"tampered")
                with pytest.raises(VerifierPatchEvidenceError):
                    ContextBuilder(observer).build_verifier_patch_input(
                        run_id, coder_execution_id=execution.id, patch_call_id=patch_call.id,
                        artifact_root=artifacts, max_patch_bytes=4096,
                    )
                artifact_path.write_bytes(patch.encode())
            elif outcome == "read_denied":
                assert execution.output["outcome"] == "BLOCKED"
                call = observer.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution.id)).one()
                assert call.status == ToolCallStatus.DENIED
                assert execution.output["evidence_refs"][0]["tool_call_id"] == str(call.id)
                assert len(model_requests) == 1
            else:
                assert execution.output["outcome"] == "BLOCKED"
                assert observer.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution.id)).all() == []
    assert (workspace / "auth.py").read_text() == (
        "new\n" if outcome in ("patch", "read_then_patch", "format_then_patch", "recount_patch", "json_then_patch") else "old\n"
    )
    assert (workspace / "config.py").read_text() == "old\n"


@pytest.mark.skipif(os.name != "nt", reason="Trusted baseline needs the Windows Job pytest report")
@pytest.mark.parametrize("tamper_report", [False, True])
def test_baseline_verifier_records_failing_test_before_patch(worker_audit_session_factory, tmp_path, monkeypatch, tamper_report):
    factory = worker_audit_session_factory
    worker = Worker(factory, "baseline-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        roles = dict(snapshot["permissions"]["allowed_tools_by_role"])
        roles["VERIFIER"] = ["run_command"]
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": roles}}
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        explorer = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
        coordinator.finish_agent_execution(run_id, worker.worker_id, agent_execution_id=explorer,
            status=AgentExecutionStatus.SUCCEEDED, output={"summary": "fixture"}, llm_usage=None)
        supervisor = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        handoff = coordinator.finish_agent_execution(run_id, worker.worker_id, agent_execution_id=supervisor,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision(from_agent_execution_id=explorer, to_agent_role=AgentRole.VERIFIER,
                context=HandoffContext(completed_work="Prepare baseline", blocker=None, evidence_refs=(),
                    decision_required="Run registered test", scope_impact="No source modification")))
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "test_baseline.py").write_text("def test_bug(): assert False\n", encoding="utf-8")
    if tamper_report:
        original_builder = worker.build_verifier_command
        def build_tampering_command(*args, **kwargs):
            command = original_builder(*args, **kwargs)
            def run_and_tamper(name):
                receipt = command(name)
                report_id = UUID(next(item["id"] for item in receipt["artifacts"] if item["kind"] == "pytest_junit"))
                with factory() as session:
                    report_path = Path(session.get(Artifact, report_id).content_ref)
                report_path.write_bytes(b"tampered report")
                return receipt
            return run_and_tamper
        monkeypatch.setattr(worker, "build_verifier_command", build_tampering_command)
    result = worker.execute_baseline_verifier(run_id, handoff_id=handoff,
        directories=RunDirectories(workspace, artifacts), command_name="pytest",
        registry={"pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_baseline.py")},
        stop_event=Event())
    with factory() as session:
        call = session.get(ToolCall, UUID(result["receipt"]["tool_call_id"]))
        assert call.args["skill"] == {"name": "run_registered_check", "version": "2", "pytest_capture": True}
    if tamper_report:
        assert result["valid"] is False
        with factory() as session:
            execution = session.get(AgentExecution, result["execution_id"])
            assert execution.status == AgentExecutionStatus.FAILED
            assert execution.output["outcome"] == "BASELINE_UNAVAILABLE"
        return
    assert result["valid"] is True
    assert (result["receipt"]["status"], result["receipt"]["exit_code"]) == ("failed", 1)
    with factory() as session:
        execution = session.get(AgentExecution, result["execution_id"])
        assert execution.status == AgentExecutionStatus.SUCCEEDED
        assert execution.output["outcome"] == "BASELINE_RECORDED"
    # Simulate an earlier committed patch call; a later baseline must be denied.
    with factory() as session:
        coordinator = RunCoordinator(session)
        coder = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
    with factory.begin() as session:
        calls = ToolCallRepository(session)
        patch_call = calls.add_requested(coder, "apply_patch", {}, run_id=run_id)
        calls.save_result(patch_call, status=ToolCallStatus.SUCCEEDED)
    with factory() as session:
        coordinator = RunCoordinator(session)
        supervisor = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        late_handoff = coordinator.finish_agent_execution(run_id, worker.worker_id, agent_execution_id=supervisor,
            status=AgentExecutionStatus.SUCCEEDED, output={}, llm_usage=None,
            approved_handoff=HandoffDecision(from_agent_execution_id=explorer, to_agent_role=AgentRole.VERIFIER,
                context=HandoffContext(completed_work="Prepare baseline", blocker=None, evidence_refs=(),
                    decision_required="Run registered test", scope_impact="No source modification")))
    late = worker.execute_baseline_verifier(run_id, handoff_id=late_handoff,
        directories=RunDirectories(workspace, artifacts), command_name="pytest",
        registry={"pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_baseline.py")},
        stop_event=Event())
    assert late["valid"] is False and late["receipt"]["reason"] == "BASELINE_AFTER_PATCH"


def test_verification_migration_roundtrip_and_persistence(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "verification-test-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    revision = scripts.get_revision("f154b6e82639").module
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.downgrade()
            revision.upgrade()
    with factory.begin() as session:
        verification = Verification(
            run_id=run_id, result=VerificationResult.FAILED,
            evidence={"test": {"tool_call_id": str(uuid4()), "exit_code": 1}},
        )
        session.add(verification)
        session.flush()
        verification_id = verification.id
    with factory() as observer:
        saved = observer.get(Verification, verification_id)
        assert saved.run_id == run_id and saved.result == VerificationResult.FAILED
        assert saved.evidence["test"]["exit_code"] == 1


@pytest.mark.parametrize("allow,role,exit_value,expected", [
    (True, AgentRole.VERIFIER, 0, "succeeded"),
    (True, AgentRole.VERIFIER, 7, "failed"),
    (False, AgentRole.VERIFIER, 0, "denied"),
    (True, AgentRole.CODER, 0, "denied"),
])
def test_registered_check_skill_respects_existing_tool_boundary(
    worker_audit_session_factory, tmp_path, allow, role, exit_value, expected,
):
    factory = worker_audit_session_factory
    worker = Worker(factory, f"skill-{uuid4().hex[:8]}")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": {
            **snapshot["permissions"]["allowed_tools_by_role"],
            "VERIFIER": ["run_command"] if allow else [], "CODER": ["run_command"]}}}
    run_id = worker.claim_once().run_id
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=role, agent_input={})
    registry = {"check": (sys.executable, "-c", f"import sys; sys.exit({exit_value})")}
    if role is AgentRole.CODER:
        with pytest.raises(SkillNotApplicable, match="not a Verifier"):
            run_registered_check(version="1", worker=worker, run_id=run_id,
                verifier_execution_id=execution_id, directories=RunDirectories(tmp_path, artifact_root),
                registry=registry, command_name="check", stop_event=Event())
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ToolCall).where(
                ToolCall.agent_execution_id == execution_id)) == 0
        return
    result = run_registered_check(version="1", worker=worker, run_id=run_id,
        verifier_execution_id=execution_id, directories=RunDirectories(tmp_path, artifact_root),
        registry=registry, command_name="check", stop_event=Event())
    assert result["skill"] == REGISTERED_CHECK.name and result["version"] == "1"
    assert result["tool_status"] == expected
    with factory() as session:
        call = session.get(ToolCall, UUID(result["tool_call_id"]))
        assert call.run_id == run_id and call.agent_execution_id == execution_id
        assert call.tool_name == "run_command" and call.status.value == expected
        assert call.args["command_name"] == "check"
        assert call.args["skill"] == {"name": "run_registered_check", "version": "1", "pytest_capture": False}
    with pytest.raises(SkillNotApplicable):
        run_registered_check(version="1", worker=worker, run_id=run_id, verifier_execution_id=execution_id,
            directories=RunDirectories(tmp_path, artifact_root), registry=registry,
            command_name="unregistered", stop_event=Event())
    with pytest.raises(SkillNotApplicable):
        run_registered_check(version="3", worker=worker, run_id=run_id, verifier_execution_id=execution_id,
            directories=RunDirectories(tmp_path, artifact_root), registry=registry,
            command_name="check", stop_event=Event())
    with pytest.raises(SkillNotApplicable):
        run_registered_check(version="1", worker=worker, run_id=run_id, verifier_execution_id=execution_id,
            directories=RunDirectories(tmp_path, artifact_root), registry=registry,
            command_name="check", stop_event=Event(), trusted_pytest_capture=True)


def test_scope_skill_supports_literal_rules_but_not_wildcards():
    assert supports_diff_scope(ScopeSpec(allowed_paths=("src/", "README.md")), 4096)
    assert not supports_diff_scope(ScopeSpec(allowed_paths=("*.py",)), 4096)
    assert not supports_diff_scope(ScopeSpec(allowed_paths=("src/",)), 6 * 1024 * 1024)


def test_registered_skill_reuses_same_version_across_isolated_tasks(worker_audit_session_factory, tmp_path):
    factory = worker_audit_session_factory
    attempts = []
    for name, allowed in (("first", True), ("second", True), ("denied", False)):
        worker = Worker(factory, f"skill-{name}-{uuid4().hex[:8]}")
        task_id = create_eligible_task(factory, repo=f"fixture-{name}")
        with factory.begin() as session:
            task = session.get(Task, task_id)
            snapshot = task.policy_snapshot
            task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "VERIFIER": ["run_command"] if allowed else []}}}
        run_id = worker.claim_once().run_id
        workspace = tmp_path / name
        workspace.mkdir()
        artifacts = tmp_path / f"{name}-artifacts"
        artifacts.mkdir()
        with factory() as session:
            execution_id = RunCoordinator(session).start_agent_execution(
                run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={})
        registry = {"check": (sys.executable, "-c", f"print('{name}')")}
        result = run_registered_check(version="2", worker=worker, run_id=run_id,
            verifier_execution_id=execution_id, directories=RunDirectories(workspace, artifacts),
            registry=registry, command_name="check", stop_event=Event())
        assert result["tool_status"] == ("succeeded" if allowed else "denied")
        with factory() as session:
            call = session.get(ToolCall, UUID(result["tool_call_id"]))
            assert call.run_id == run_id and call.agent_execution_id == execution_id
            assert call.args["skill"]["name"] == "run_registered_check"
            assert call.args["skill"]["version"] == "2"
            assert session.get(Run, run_id).task_id == task_id
        attempts.append((worker, run_id, execution_id, workspace, artifacts, registry, result))
    assert len({entry[1] for entry in attempts}) == 3
    assert attempts[0][-1]["command_sha256"] != attempts[1][-1]["command_sha256"]
    first, second = attempts[:2]
    with pytest.raises(SkillNotApplicable, match="not a Verifier"):
        run_registered_check(version="2", worker=first[0], run_id=first[1],
            verifier_execution_id=second[2], directories=RunDirectories(first[3], first[4]),
            registry=first[5], command_name="check", stop_event=Event())


@pytest.mark.skipif(os.name != "nt", reason="Report capture requires a managed Windows Job")
@pytest.mark.parametrize("assertion,expected_status,expected_case", [
    ("True", "succeeded", "passed"), ("False", "failed", "failed"),
])
def test_registered_check_v2_captures_bound_pytest_report(
    worker_audit_session_factory, tmp_path, monkeypatch, assertion, expected_status, expected_case,
):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    factory = worker_audit_session_factory
    worker = Worker(factory, f"skill-report-{uuid4().hex[:8]}")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 20},
            "permissions": {"allowed_tools_by_role": {**snapshot["permissions"]["allowed_tools_by_role"],
                                                        "VERIFIER": ["run_command"]}}}
    run_id = worker.claim_once().run_id
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "test_example.py").write_text(f"def test_example(): assert {assertion}\n", encoding="utf-8")
    with factory() as session:
        verifier_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={})
    registry = {"pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_example.py")}
    result = run_registered_check(version="2", worker=worker, run_id=run_id,
        verifier_execution_id=verifier_id, directories=RunDirectories(workspace, artifacts), registry=registry,
        command_name="pytest", stop_event=Event(), trusted_pytest_capture=True)
    assert result["tool_status"] == expected_status and result["version"] == "2"
    assert len(result["command_sha256"]) == 64
    junit = next(artifact for artifact in result["artifacts"] if artifact["kind"] == "pytest_junit")
    with factory() as session:
        report = load_pytest_report_artifact(session, run_id, verifier_id,
            tool_call_id=UUID(result["tool_call_id"]), artifact_id=UUID(junit["id"]),
            artifact_root=artifacts, expected_command_name="pytest")
        assert report.tests[0].status == expected_case
        assert session.get(Run, run_id).status is RunStatus.RUNNING
        call = session.get(ToolCall, UUID(result["tool_call_id"]))
        assert call.args["skill"] == {"name": "run_registered_check", "version": "2", "pytest_capture": True}
        assert call.args["command_sha256"] == result["command_sha256"]
    missing = run_registered_check(version="2", worker=worker, run_id=run_id,
        verifier_execution_id=verifier_id, directories=RunDirectories(workspace, artifacts),
        registry={"missing": (sys.executable, "-c", "print('no report')")},
        command_name="missing", stop_event=Event(), trusted_pytest_capture=True)
    assert missing["reason"] == "PYTEST_REPORT_MISSING_OR_INVALID"
    assert not any(artifact["kind"] == "pytest_junit" for artifact in missing["artifacts"])


def test_skill_identity_is_committed_before_command_failure(worker_audit_session_factory, tmp_path, monkeypatch):
    factory = worker_audit_session_factory
    worker = Worker(factory, f"skill-audit-{uuid4().hex[:8]}")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": {
            **snapshot["permissions"]["allowed_tools_by_role"], "VERIFIER": ["run_command"]}}}
    run_id = worker.claim_once().run_id
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={})
    with factory() as session:
        with pytest.raises(ValueError, match="Unsupported registered-check"):
            RunCoordinator(session).execute_verifier_command(run_id, worker.worker_id,
                agent_execution_id=execution_id, workspace_root=tmp_path, artifact_root=artifacts,
                command_name="check", registry={"check": (sys.executable, "-c", "pass")},
                stop_event=Event(), registered_check_skill_version="999")
        assert session.scalar(select(func.count()).select_from(ToolCall).where(
            ToolCall.agent_execution_id == execution_id)) == 0
    observed = []
    def fail_after_observing_request(*args, **kwargs):
        with factory() as observer:
            call = observer.scalar(select(ToolCall).where(ToolCall.agent_execution_id == execution_id))
            observed.append((call.id, call.args["skill"]))
        raise RuntimeError("simulated uncertain result")
    monkeypatch.setattr(coordinator_module, "run_registered_command", fail_after_observing_request)
    with pytest.raises(RuntimeError, match="simulated uncertain result"):
        run_registered_check(version="2", worker=worker, run_id=run_id,
            verifier_execution_id=execution_id, directories=RunDirectories(tmp_path, artifacts),
            registry={"check": (sys.executable, "-c", "pass")}, command_name="check", stop_event=Event())
    assert observed[0][1] == {"name": "run_registered_check", "version": "2", "pytest_capture": False}
    with factory() as session:
        call = session.get(ToolCall, observed[0][0])
        assert call.status is ToolCallStatus.UNKNOWN_RESULT


def test_verifier_registered_commands_are_audited(worker_audit_session_factory, tmp_path):
    factory = worker_audit_session_factory
    worker = Worker(factory, "verifier-command-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 1},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "VERIFIER": ["run_command"], "CODER": ["run_command"],
            }},
        }
    run_id = worker.claim_once().run_id
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    registry = {
        "pass": (sys.executable, "-c", "print('passed')"),
        "fail": (sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"),
        "timeout": (sys.executable, "-c", "import time; time.sleep(30)"),
        "large": (sys.executable, "-c", "print('x' * 20000)"),
    }
    with factory() as session:
        coordinator = RunCoordinator(session)
        verifier_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
        coder_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
        denied_id = coordinator.execute_verifier_command(
            run_id, worker.worker_id, agent_execution_id=coder_id, workspace_root=tmp_path,
            artifact_root=artifact_root, command_name="pass", registry=registry, stop_event=Event(),
        )
    run_command = worker.build_verifier_command(
        run_id, agent_execution_id=verifier_id, directories=RunDirectories(tmp_path, artifact_root),
        registry=registry, stop_event=Event(),
    )
    registry["injected_later"] = registry["pass"]
    receipts = [run_command(name) for name in ("pass", "fail", "missing", "timeout", "large", "injected_later")]
    ids = [UUID(receipt["tool_call_id"]) for receipt in receipts]
    with factory() as observer:
        calls = [observer.get(ToolCall, call_id) for call_id in ids]
        assert [(call.status, call.exit_code) for call in calls] == [
            (ToolCallStatus.SUCCEEDED, 0), (ToolCallStatus.FAILED, 7),
            (ToolCallStatus.DENIED, None), (ToolCallStatus.TIMED_OUT, None),
            (ToolCallStatus.SUCCEEDED, 0),
            (ToolCallStatus.DENIED, None),
        ]
        assert calls[2].reason == "COMMAND_NOT_REGISTERED"
        assert all(call.args.keys() == {"command_name", "command_sha256"} for call in calls)
        evidence = load_verifier_command_evidence(observer, run_id, verifier_id, tuple(ids))
        assert calls[3].reason == "COMMAND_TIMED_OUT"
        assert observer.get(ToolCall, denied_id).reason == "VERIFIER_ROLE_REQUIRED"
        assert [item["status"] for item in evidence] == ["succeeded", "failed", "denied", "timed_out", "succeeded", "denied"]
        assert evidence == receipts
        assert evidence[5]["reason"] == "COMMAND_NOT_REGISTERED"
        assert evidence[1]["exit_code"] == 7
        assert evidence[0]["artifacts"] == []
        assert evidence[4]["reason"] == "OUTPUT_TRUNCATED"
        output_artifact = evidence[4]["artifacts"][0]
        artifact = observer.get(Artifact, UUID(output_artifact["id"]))
        assert output_artifact["kind"] == "tool_stdout"
        assert output_artifact["content_hash"] == artifact.content_hash
        assert artifact.tool_call_id == ids[4]


@pytest.mark.skipif(os.name != "nt", reason="Report capture requires a managed Windows Job")
def test_real_pytest_reports_are_fresh_and_bound_to_each_call(worker_audit_session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    factory = worker_audit_session_factory
    worker = Worker(factory, "real-pytest-report-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 20},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "VERIFIER": ["run_command"],
            }},
        }
    run_id = worker.claim_once().run_id
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    (workspace / "report.xml").write_text("old report", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "example_module.py").write_text("value = 1\n", encoding="utf-8")
    test_file = workspace / "test_demo.py"
    test_file.write_text("from example_module import value\ndef test_example(): assert value == 0\n", encoding="utf-8")
    with factory() as session:
        verifier_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
    run_command = worker.build_verifier_command(
        run_id, agent_execution_id=verifier_id, directories=RunDirectories(workspace, artifacts),
        registry={
            "pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_demo.py"),
            "no_report": (sys.executable, "-c", "print('no report')"),
        }, pytest_command_names=frozenset({"pytest", "no_report"}), stop_event=Event(), pythonpath_relative="src",
    )
    first = run_command("pytest")
    assert (first["status"], first["exit_code"]) == ("failed", 1)
    test_file.write_text("from example_module import value\ndef test_example(): assert value == 1\n", encoding="utf-8")
    second = run_command("pytest")
    assert (second["status"], second["exit_code"]) == ("succeeded", 0)
    assert first["args"]["command_sha256"] == second["args"]["command_sha256"]
    assert first["args"]["pythonpath"] == second["args"]["pythonpath"] == "src"
    missing = run_command("no_report")
    assert missing["reason"] == "PYTEST_REPORT_MISSING_OR_INVALID"
    assert not any(item["kind"] == "pytest_junit" for item in missing["artifacts"])
    assert (workspace / "report.xml").read_text(encoding="utf-8") == "old report"
    with factory() as session:
        observations = []
        for receipt, expected in ((first, "failed"), (second, "passed")):
            junit = next(item for item in receipt["artifacts"] if item["kind"] == "pytest_junit")
            parsed = load_pytest_report_artifact(
                session, run_id, verifier_id, tool_call_id=UUID(receipt["tool_call_id"]),
                artifact_id=UUID(junit["id"]), artifact_root=artifacts, expected_command_name="pytest",
            )
            assert parsed.tests[0].status == expected
            observations.append(load_python_environment_observation(
                session, run_id, verifier_id, tool_call_id=UUID(receipt["tool_call_id"]), artifact_root=artifacts,
            ))
        assert observations[0] == observations[1]
        assert observations[0].dont_write_bytecode is True
    assert first["tool_call_id"] != second["tool_call_id"]
    assert not list(artifacts.glob("pytest-*"))


@pytest.mark.skipif(os.name != "nt", reason="Report capture requires a managed Windows Job")
@pytest.mark.skipif(not os.environ.get("DEVFLOW_CLICK_3019_CHECKOUT"), reason="Explicit Click baseline checkout not provided")
def test_click_public_baseline_through_verifier_command(worker_audit_session_factory, tmp_path, monkeypatch):
    checkout = Path(os.environ["DEVFLOW_CLICK_3019_CHECKOUT"]).resolve(strict=True)
    expected_commit = "00fadb8904387158ce6e9aa1573be770446895c1"
    def git(*args):
        return subprocess.run(["git", "-C", str(checkout), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert git("rev-parse", "HEAD") == expected_commit
    assert not git("status", "--porcelain=v1", "--untracked-files=all")
    assert not git("ls-files", "--others")
    monkeypatch.delenv("DEVFLOW_CLICK_SOURCE", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    factory = worker_audit_session_factory
    worker = Worker(factory, "click-baseline-worker")
    task_id = create_eligible_task(factory, repo=str(checkout), base_commit=expected_commit)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 20},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "VERIFIER": ["run_command"],
            }},
        }
    run_id = worker.claim_once().run_id
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    with factory() as session:
        verifier_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
    scenario = Path(__file__).resolve().parents[1] / "benchmarks" / "click_3019_public" / "scenarios.py"
    run_command = worker.build_verifier_command(
        run_id, agent_execution_id=verifier_id, directories=RunDirectories(checkout, artifact_root),
        registry={"pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             f"--rootdir={scenario.parent}", str(scenario))},
        pytest_command_names=frozenset({"pytest"}), pythonpath_relative="src", stop_event=Event(),
    )
    receipt = run_command("pytest")
    assert (receipt["status"], receipt["exit_code"]) == ("failed", 1)
    assert receipt["args"]["pythonpath"] == "src"
    junit = next(item for item in receipt["artifacts"] if item["kind"] == "pytest_junit")
    with factory() as session:
        report = load_pytest_report_artifact(
            session, run_id, verifier_id, tool_call_id=UUID(receipt["tool_call_id"]),
            artifact_id=UUID(junit["id"]), artifact_root=artifact_root, expected_command_name="pytest",
        )
        assert sorted(test.status.value for test in report.tests) == ["failed", "failed", "passed", "passed"]
    assert not git("status", "--porcelain=v1", "--untracked-files=all")
    assert not git("ls-files", "--others")


@pytest.mark.skipif(os.name != "nt", reason="Report capture requires a managed Windows Job")
def test_environment_probe_and_pytest_share_one_timeout(worker_audit_session_factory, tmp_path, monkeypatch):
    factory = worker_audit_session_factory
    worker = Worker(factory, "environment-budget-worker")
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 1},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "VERIFIER": ["run_command"],
            }},
        }
    run_id = worker.claim_once().run_id
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    with factory() as session:
        verifier_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
    calls = []
    def fake_command(_workspace, name, **_kwargs):
        calls.append(name)
        assert name == "python_environment"
        data = observe_current_python_environment().model_dump_json().encode("utf-8")
        return GitCommandResult(["probe"], 0, GitOutputPreview(data, b"", False, False))
    times = iter((100.0, 102.0))
    monkeypatch.setattr(coordinator_module, "run_registered_command", fake_command)
    monkeypatch.setattr(coordinator_module, "monotonic", lambda: next(times))
    run_command = worker.build_verifier_command(
        run_id, agent_execution_id=verifier_id, directories=RunDirectories(tmp_path, artifact_root),
        registry={"pytest": (sys.executable, "-m", "pytest")},
        pytest_command_names=frozenset({"pytest"}), stop_event=Event(),
    )
    result = run_command("pytest")
    assert calls == ["python_environment"]
    assert result["status"] == "timed_out" and result["reason"] == "COMMAND_BUDGET_EXHAUSTED"
    assert [item["kind"] for item in result["artifacts"]] == ["python_environment"]


def test_comparison_requires_ordered_calls_without_intervening_workspace_operations(worker_audit_session_factory, tmp_path):
    factory = worker_audit_session_factory
    worker = Worker(factory, "comparison-order-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        verifier_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={})
        coder_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
    baseline_id, patch_id, current_id, extra_id = (uuid4() for _ in range(4))
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    baseline_xml = (b'<testsuite tests="2" failures="1" errors="0" skipped="0">'
                    b'<testcase classname="demo" name="target"><failure/></testcase>'
                    b'<testcase classname="demo" name="other"/></testsuite>')
    current_xml = (b'<testsuite tests="2" failures="0" errors="0" skipped="0">'
                   b'<testcase classname="demo" name="target"/>'
                   b'<testcase classname="demo" name="other"/></testsuite>')
    baseline_artifact, current_artifact = uuid4(), uuid4()
    baseline_path, baseline_hash = write_artifact_file(tmp_path, baseline_artifact, BytesIO(baseline_xml), chunk_size=128)
    current_path, current_hash = write_artifact_file(tmp_path, current_artifact, BytesIO(current_xml), chunk_size=128)
    environment_data = observe_current_python_environment().model_dump_json().encode("utf-8")
    baseline_env_id, current_env_id = uuid4(), uuid4()
    baseline_env_path, baseline_env_hash = write_artifact_file(tmp_path, baseline_env_id, BytesIO(environment_data), chunk_size=128)
    current_env_path, current_env_hash = write_artifact_file(tmp_path, current_env_id, BytesIO(environment_data), chunk_size=128)
    with factory.begin() as session:
        session.add_all([
            ToolCall(id=baseline_id, run_id=run_id, agent_execution_id=verifier_id, tool_name="run_command",
                     args={"command_name": "pytest", "command_sha256": "a" * 64}, status=ToolCallStatus.FAILED, exit_code=1,
                     created_at=start, updated_at=start + timedelta(seconds=1)),
            ToolCall(id=patch_id, run_id=run_id, agent_execution_id=coder_id, tool_name="apply_patch",
                     args={}, status=ToolCallStatus.SUCCEEDED, exit_code=0,
                     created_at=start + timedelta(seconds=2), updated_at=start + timedelta(seconds=3)),
            ToolCall(id=current_id, run_id=run_id, agent_execution_id=verifier_id, tool_name="run_command",
                     args={"command_name": "pytest", "command_sha256": "a" * 64}, status=ToolCallStatus.SUCCEEDED, exit_code=0,
                     created_at=start + timedelta(seconds=4), updated_at=start + timedelta(seconds=5)),
        ])
        session.flush()
        session.add_all([
            Artifact(id=baseline_artifact, agent_execution_id=verifier_id, tool_call_id=baseline_id,
                     kind="pytest_junit", content_ref=str(baseline_path.absolute()), content_hash=baseline_hash),
            Artifact(id=current_artifact, agent_execution_id=verifier_id, tool_call_id=current_id,
                     kind="pytest_junit", content_ref=str(current_path.absolute()), content_hash=current_hash),
            Artifact(id=baseline_env_id, agent_execution_id=verifier_id, tool_call_id=baseline_id,
                     kind="python_environment", content_ref=str(baseline_env_path.absolute()), content_hash=baseline_env_hash),
            Artifact(id=current_env_id, agent_execution_id=verifier_id, tool_call_id=current_id,
                     kind="python_environment", content_ref=str(current_env_path.absolute()), content_hash=current_env_hash),
        ])
    def check(session, baseline=baseline_id, patch=patch_id, current=current_id):
        require_test_comparison_sequence(
            session, run_id, baseline_call_id=baseline, patch_call_id=patch, current_call_id=current,
        )
    with factory() as session:
        check(session)
        comparison = compare_recorded_pytest_reports(
            session, run_id, baseline_verifier_id=verifier_id, baseline_call_id=baseline_id,
            baseline_artifact_id=baseline_artifact, patch_call_id=patch_id, current_verifier_id=verifier_id,
            current_call_id=current_id, current_artifact_id=current_artifact, artifact_root=tmp_path,
            expected_command_name="pytest", target_tests=("demo::target",),
        )
        assert comparison.target_result == comparison.regression_result == "passed"
        with pytest.raises(VerificationEvidenceError):
            compare_recorded_pytest_reports(
                session, run_id, baseline_verifier_id=verifier_id, baseline_call_id=baseline_id,
                baseline_artifact_id=current_artifact, patch_call_id=patch_id, current_verifier_id=verifier_id,
                current_call_id=current_id, current_artifact_id=current_artifact, artifact_root=tmp_path,
                expected_command_name="pytest", target_tests=("demo::target",),
            )
        with pytest.raises(VerificationEvidenceError):
            check(session, baseline=current_id, current=baseline_id)
        with pytest.raises(VerificationEvidenceError):
            check(session, patch=uuid4())
    with factory.begin() as session:
        session.get(Artifact, current_env_id).kind = "other"
    with factory() as session, pytest.raises(VerificationEvidenceError, match="missing or ambiguous"):
        compare_recorded_pytest_reports(
            session, run_id, baseline_verifier_id=verifier_id, baseline_call_id=baseline_id,
            baseline_artifact_id=baseline_artifact, patch_call_id=patch_id, current_verifier_id=verifier_id,
            current_call_id=current_id, current_artifact_id=current_artifact, artifact_root=tmp_path,
            expected_command_name="pytest", target_tests=("demo::target",),
        )
    with factory.begin() as session:
        session.get(Artifact, current_env_id).kind = "python_environment"
    different_environment = observe_current_python_environment().model_copy(update={"python_version": "different"})
    different_data = different_environment.model_dump_json().encode("utf-8")
    current_env_path.write_bytes(different_data)
    with factory.begin() as session:
        session.get(Artifact, current_env_id).content_hash = sha256(different_data).hexdigest()
    with factory() as session, pytest.raises(VerificationEvidenceError, match="environment fields differ"):
        compare_recorded_pytest_reports(
            session, run_id, baseline_verifier_id=verifier_id, baseline_call_id=baseline_id,
            baseline_artifact_id=baseline_artifact, patch_call_id=patch_id, current_verifier_id=verifier_id,
            current_call_id=current_id, current_artifact_id=current_artifact, artifact_root=tmp_path,
            expected_command_name="pytest", target_tests=("demo::target",),
        )
    current_env_path.write_bytes(environment_data)
    with factory.begin() as session:
        session.get(Artifact, current_env_id).content_hash = current_env_hash
    with factory.begin() as session:
        session.get(ToolCall, current_id).args = {"command_name": "pytest", "command_sha256": "b" * 64}
    with factory() as session, pytest.raises(VerificationEvidenceError, match="fingerprints"):
        check(session)
    with factory.begin() as session:
        session.get(ToolCall, current_id).args = {"command_name": "pytest", "command_sha256": "a" * 64}
    with factory.begin() as session:
        session.add(ToolCall(
            id=extra_id, run_id=run_id, agent_execution_id=coder_id, tool_name="apply_patch",
            args={}, status=ToolCallStatus.FAILED, exit_code=1,
            created_at=start + timedelta(seconds=3, milliseconds=500),
            updated_at=start + timedelta(seconds=3, milliseconds=600),
        ))
    with factory() as session, pytest.raises(VerificationEvidenceError, match="intervened"):
        check(session)
    with factory.begin() as session:
        session.get(ToolCall, extra_id).status = ToolCallStatus.DENIED
    with factory() as session:
        check(session)
    with factory.begin() as session:
        session.get(ToolCall, patch_id).created_at = start + timedelta(seconds=1)
    with factory() as session, pytest.raises(VerificationEvidenceError, match="order"):
        check(session)


def test_pytest_report_reference_must_match_committed_call_and_hash(worker_audit_session_factory, tmp_path):
    factory = worker_audit_session_factory
    worker = Worker(factory, "report-evidence-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        verifier_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
    report = (b'<testsuite tests="1" failures="1" errors="0" skipped="0">'
              b'<testcase classname="sample" name="test_target"><failure/></testcase></testsuite>')
    artifact_id = uuid4()
    path, digest = write_artifact_file(tmp_path, artifact_id, BytesIO(report), chunk_size=128)
    mismatched_id = uuid4()
    mismatched_path, mismatched_digest = write_artifact_file(tmp_path, mismatched_id, BytesIO(report), chunk_size=128)
    with factory.begin() as session:
        repo = ToolCallRepository(session)
        failed_call = repo.add_requested(verifier_id, "run_command", {"command_name": "pytest"}, run_id=run_id)
        repo.save_result(failed_call, status=ToolCallStatus.FAILED, exit_code=1)
        other_call = repo.add_requested(verifier_id, "run_command", {"command_name": "pytest"}, run_id=run_id)
        repo.save_result(other_call, status=ToolCallStatus.SUCCEEDED, exit_code=0)
        invalid_exit_call = repo.add_requested(verifier_id, "run_command", {"command_name": "pytest"}, run_id=run_id)
        repo.save_result(invalid_exit_call, status=ToolCallStatus.FAILED, exit_code=3)
        session.add(Artifact(
            id=artifact_id, agent_execution_id=verifier_id, tool_call_id=failed_call,
            kind="pytest_junit", content_ref=str(path.absolute()), content_hash=digest,
        ))
        session.add(Artifact(
            id=mismatched_id, agent_execution_id=verifier_id, tool_call_id=other_call,
            kind="pytest_junit", content_ref=str(mismatched_path.absolute()), content_hash=mismatched_digest,
        ))
    with factory() as session:
        def load(call_id, name="pytest"):
            return load_pytest_report_artifact(
                session, run_id, verifier_id, tool_call_id=call_id, artifact_id=artifact_id,
                artifact_root=tmp_path, expected_command_name=name,
            )
        parsed = load(failed_call)
        assert parsed.tool_call_id == failed_call and parsed.tests[0].status == "failed"
        for call_id, name in ((other_call, "pytest"), (invalid_exit_call, "pytest"), (failed_call, "other")):
            with pytest.raises(VerificationEvidenceError):
                load(call_id, name)
        with pytest.raises(VerificationEvidenceError, match="exit code"):
            load_pytest_report_artifact(
                session, run_id, verifier_id, tool_call_id=other_call, artifact_id=mismatched_id,
                artifact_root=tmp_path, expected_command_name="pytest",
            )
        path.write_bytes(report + b" ")
        with pytest.raises(VerificationEvidenceError, match="verified"):
            load(failed_call)


@pytest.mark.parametrize("allow_tool", [True, False])
def test_diff_scope_skill_uses_guarded_diff_and_literal_rules(worker_audit_session_factory, tmp_path, allow_tool):
    workspace, artifact_root = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifact_root.mkdir()
    (workspace / "src" / "private").mkdir(parents=True)
    paths = ("src/public.py", "src/private/secret.py", "outside.py", "src2.py")
    for path in paths:
        (workspace / path).write_text("value = 1\n", encoding="utf-8")
    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    git("init", "-q")
    git("add", *paths)
    git("-c", "user.name=DevFlow Test", "-c", "user.email=devflow@example.invalid", "commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD").strip()
    for path in paths:
        (workspace / path).write_text("value = 2\n", encoding="utf-8")
    factory = worker_audit_session_factory
    worker = Worker(factory, f"scope-skill-{uuid4().hex[:8]}")
    task_id = create_eligible_task(factory, base_commit=base)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {**snapshot, "permissions": {"allowed_tools_by_role": {
            **snapshot["permissions"]["allowed_tools_by_role"], "VERIFIER": ["git_diff"] if allow_tool else []}}}
    run_id = worker.claim_once().run_id
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
            role=AgentRole.VERIFIER, agent_input={})
    with factory() as session:
        coder_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
            role=AgentRole.CODER, agent_input={})
    kwargs = dict(version="1", worker=worker, run_id=run_id, verifier_execution_id=execution_id,
                  directories=RunDirectories(workspace, artifact_root), max_diff_bytes=65536, stop_event=Event())
    for rule in ("../", "/root", "src/*", "src/../private", "src\\private"):
        with pytest.raises(SkillNotApplicable):
            check_diff_scope(**kwargs, trusted_scope=ScopeSpec(allowed_paths=(rule,)))
    scope = ScopeSpec(allowed_paths=("src/",), forbidden_paths=("src/private/",))
    with pytest.raises(SkillNotApplicable, match="does not pin"):
        check_diff_scope(**{**kwargs, "version": "2"}, trusted_scope=scope)
    with pytest.raises(SkillNotApplicable, match="not a Verifier"):
        check_diff_scope(**{**kwargs, "verifier_execution_id": coder_id}, trusted_scope=scope)
    if not allow_tool:
        with pytest.raises(VerificationEvidenceError):
            check_diff_scope(**kwargs, trusted_scope=scope)
        with factory() as session:
            call = session.scalar(select(ToolCall).where(ToolCall.agent_execution_id == execution_id))
            assert call.status is ToolCallStatus.DENIED
        return
    result = check_diff_scope(**kwargs, trusted_scope=scope)
    assert result["result"] == "outside_declared_scope"
    assert {v["path"]: v["reason"] for v in result["violations"]} == {
        "src/private/secret.py": "forbidden", "outside.py": "not_allowed", "src2.py": "not_allowed"}
    with factory() as session:
        call = session.get(ToolCall, UUID(result["tool_call_id"]))
        assert call.tool_name == "git_diff" and call.status is ToolCallStatus.SUCCEEDED
        assert session.get(Artifact, UUID(result["numstat_artifact_id"])).tool_call_id == call.id
        assert call.args["skill"] == {"name": "check_diff_scope", "version": "1",
                                      "scope_sha256": result["scope_sha256"]}
    clean_scope = ScopeSpec(allowed_paths=("src/", "outside.py", "src2.py"))
    assert check_diff_scope(**kwargs, trusted_scope=clean_scope)["result"] == "within_declared_scope"
    with factory.begin() as session:
        call = session.get(ToolCall, UUID(result["tool_call_id"]))
        call.args = {**call.args, "skill": {**call.args["skill"], "version": "unknown"}}
    from devflow_ai.verifier import load_verifier_workspace_diff
    with factory() as session:
        with pytest.raises(VerificationEvidenceError):
            load_verifier_workspace_diff(session, run_id, execution_id, tool_call_id=UUID(result["tool_call_id"]),
                                         artifact_root=artifact_root, max_bytes=65536)
    (workspace / "untracked.txt").write_text("not covered", encoding="utf-8")
    with pytest.raises(VerificationEvidenceError):
        check_diff_scope(**kwargs, trusted_scope=scope)


def test_diff_scope_v2_requires_admission_binding_and_rechecks_before_tool(
    worker_audit_session_factory, tmp_path, monkeypatch,
):
    workspace, artifact_root = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifact_root.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "file.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "src" / "[file].py").write_text("x = 1\n", encoding="utf-8")
    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    git("init", "-q")
    git("add", "src")
    git("-c", "user.name=DevFlow Test", "-c", "user.email=devflow@example.invalid", "commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD").strip()
    (workspace / "src" / "file.py").write_text("x = 2\n", encoding="utf-8")
    scope = ScopeSpec(allowed_paths=("src/",))
    manifest = load_manifest(Path(__file__).resolve().parents[1] / "benchmarks" / "click_3019_public")
    manifest = manifest.model_copy(update={"base_commit": base, "scope": scope})
    policy = build_default_policy()
    policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] = ["git_diff"]
    factory = worker_audit_session_factory
    task_id = create_benchmark_task(factory, manifest, repo="example-repo", policy=policy, operator="test")
    worker = Worker(factory, f"scope-bound-{uuid4().hex[:8]}")
    run_id = worker.claim_once().run_id
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
            role=AgentRole.VERIFIER, agent_input={})
    kwargs = dict(version="2", worker=worker, run_id=run_id, verifier_execution_id=execution_id,
                  directories=RunDirectories(workspace, artifact_root), max_diff_bytes=65536, stop_event=Event())
    with pytest.raises(SkillNotApplicable, match="does not pin"):
        check_diff_scope(**kwargs, trusted_scope=ScopeSpec(allowed_paths=("elsewhere/",)))
    result = check_diff_scope(**kwargs, trusted_scope=scope)
    assert result["result"] == "within_declared_scope"
    assert result["scope_sha256"] == scope_evidence_sha256(scope)
    (workspace / "src" / "[file].py").write_text("x = 2\n", encoding="utf-8")
    unusual = check_diff_scope(**kwargs, trusted_scope=scope)
    assert unusual["result"] == "outside_declared_scope"
    assert {v["path"]: v["reason"] for v in unusual["violations"]} == {
        "src/[file].py": "unsupported_path"}
    (workspace / "src" / "[file].py").write_text("x = 1\n", encoding="utf-8")
    with factory() as session:
        call = session.get(ToolCall, UUID(result["tool_call_id"]))
        assert call.args["skill"]["version"] == "2"
    original_builder = worker.build_verifier_diff
    def changed_binding(*args, **kw):
        callback = original_builder(*args, **kw)
        def invoke():
            with factory.begin() as session:
                task = session.get(Task, task_id)
                task.admission_record = {"reason": "changed", "evidence": ["pack:changed"]}
            return callback()
        return invoke
    monkeypatch.setattr(worker, "build_verifier_diff", changed_binding)
    with pytest.raises(VerificationEvidenceError):
        check_diff_scope(**kwargs, trusted_scope=scope)
    with factory() as session:
        calls = session.scalars(select(ToolCall).where(ToolCall.agent_execution_id == execution_id)
                                .order_by(ToolCall.created_at)).all()
        assert calls[-1].status is ToolCallStatus.DENIED
        assert calls[-1].reason == "TASK_SCOPE_BINDING_MISSING_OR_CHANGED"
    with factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_record = {"reason": "fixture restore", "evidence": [f"pack:{manifest.pack_id}",
            f"scope-sha256-v1:{scope_evidence_sha256(scope)}"]}
    def changed_after_tool(*args, **kw):
        callback = original_builder(*args, **kw)
        def invoke():
            evidence = callback()
            with factory.begin() as session:
                session.get(Task, task_id).admission_record = {"reason": "changed", "evidence": ["pack:changed"]}
            return evidence
        return invoke
    monkeypatch.setattr(worker, "build_verifier_diff", changed_after_tool)
    with pytest.raises(SkillNotApplicable, match="changed during inspection"):
        check_diff_scope(**kwargs, trusted_scope=scope)


def test_verifier_reads_actual_git_diff_and_rejects_untracked_files(worker_audit_session_factory, tmp_path):
    workspace, artifact_root = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifact_root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout

    git("init", "-q")
    (workspace / "demo.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (workspace / "image.bin").write_bytes(b"\x00before")
    git("add", "demo.py", ".gitignore", "image.bin")
    git("-c", "user.name=DevFlow Test", "-c", "user.email=devflow@example.invalid", "commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD").strip()
    factory = worker_audit_session_factory
    worker = Worker(factory, "workspace-diff-worker")
    task_id = create_eligible_task(factory, base_commit=base)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "VERIFIER": ["git_diff", "read_file"], "CODER": ["git_diff"],
            }},
        }
    run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        verifier_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={})
        coder_id = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.CODER, agent_input={})
        denied_id = coordinator.execute_verifier_git_diff(
            run_id, worker.worker_id, agent_execution_id=coder_id, workspace_root=workspace,
            artifact_root=artifact_root, stop_event=Event(),
        )
    read_diff = worker.build_verifier_diff(
        run_id, agent_execution_id=verifier_id, directories=RunDirectories(workspace, artifact_root),
        max_bytes=65536, stop_event=Event(),
    )
    with pytest.raises(VerificationEvidenceError):
        read_diff()
    (workspace / "demo.py").write_text("value = 2\n", encoding="utf-8")
    evidence = read_diff()
    assert evidence["base_commit"] == base
    assert "-value = 1" in evidence["diff"] and "+value = 2" in evidence["diff"]
    assert evidence["statistics"]["file_count"] == 1
    assert evidence["statistics"]["added_lines"] == evidence["statistics"]["deleted_lines"] == 1
    assert evidence["statistics"]["files"][0]["path"] == "demo.py"
    read_file = worker.build_agent_read_file(
        run_id, agent_execution_id=verifier_id, directories=RunDirectories(workspace, artifact_root),
        max_bytes=4096, stop_event=Event(),
    )
    read = read_file("demo.py")
    review = SemanticReviewDraft(outcome="REVIEW", result="passed", reason="Reviewed actual code", citations=(
        SemanticCitation(source="read_file", tool_call_id=read.tool_call_id, path="demo.py",
                         snippet="value = 2", reason="Updated value"),
        SemanticCitation(source="workspace_diff", tool_call_id=UUID(evidence["tool_call_id"]), path="demo.py",
                         snippet="value = 2", reason="Actual change", diff_side="added"),
    ))
    with factory() as session:
        assert session.get(ToolCall, denied_id).reason == "VERIFIER_ROLE_REQUIRED"
        def validate(candidate):
            return validate_semantic_review_citations(
                session, run_id, verifier_id, diff_call_id=UUID(evidence["tool_call_id"]),
                artifact_root=artifact_root, max_diff_bytes=65536, review=candidate,
            )
        assert validate(review) == review
        for invalid in (
            review.citations[0].model_copy(update={"snippet": "invented code"}),
            review.citations[0].model_copy(update={"path": "other.py"}),
            review.citations[1].model_copy(update={"snippet": "invented code"}),
            review.citations[1].model_copy(update={"path": "other.py"}),
            review.citations[1].model_copy(update={"diff_side": "removed"}),
            review.citations[1].model_copy(update={"diff_side": None}),
            review.citations[0].model_copy(update={"diff_side": "added"}),
            review.citations[0].model_copy(update={"tool_call_id": denied_id}),
        ):
            with pytest.raises(VerificationEvidenceError):
                validate(review.model_copy(update={"citations": (invalid,)}))
        artifact = session.get(Artifact, UUID(evidence["artifact_id"]))
        artifact_path = Path(artifact.content_ref)
        original = artifact_path.read_bytes()
        artifact_path.write_bytes(b"tampered")
        from devflow_ai.verifier import load_verifier_workspace_diff
        with pytest.raises(VerificationEvidenceError, match="verified"):
            load_verifier_workspace_diff(
                session, run_id, verifier_id, tool_call_id=UUID(evidence["tool_call_id"]),
                artifact_root=artifact_root, max_bytes=65536,
            )
        artifact_path.write_bytes(original)
        numstat_artifact = session.get(Artifact, UUID(evidence["numstat_artifact_id"]))
        numstat_path = Path(numstat_artifact.content_ref)
        original_numstat = numstat_path.read_bytes()
        numstat_path.write_bytes(b"0\t0\tdemo.py\0")
        with pytest.raises(VerificationEvidenceError, match="verified"):
            load_verifier_workspace_diff(
                session, run_id, verifier_id, tool_call_id=UUID(evidence["tool_call_id"]),
                artifact_root=artifact_root, max_bytes=65536,
            )
        numstat_path.write_bytes(original_numstat)
    with factory.begin() as session:
        repo = ToolCallRepository(session)
        intervening = repo.add_requested(verifier_id, "run_command", {}, run_id=run_id)
        repo.save_result(intervening, status=ToolCallStatus.FAILED, exit_code=1)
    with factory() as session, pytest.raises(VerificationEvidenceError, match="may have changed"):
        validate_semantic_review_citations(
            session, run_id, verifier_id, diff_call_id=UUID(evidence["tool_call_id"]),
            artifact_root=artifact_root, max_diff_bytes=65536, review=review,
        )
    (workspace / "untracked.py").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(VerificationEvidenceError):
        read_diff()
    with factory() as session:
        failed = session.scalars(select(ToolCall).where(
            ToolCall.agent_execution_id == verifier_id, ToolCall.tool_name == "git_diff",
            ToolCall.status == ToolCallStatus.FAILED, ToolCall.reason == "WORKSPACE_UNTRACKED_FILES",
        )).one()
        assert failed.reason == "WORKSPACE_UNTRACKED_FILES"
    (workspace / "untracked.py").unlink()
    (workspace / "ignored.py").write_text("ignored\n", encoding="utf-8")
    with pytest.raises(VerificationEvidenceError):
        read_diff()
    with factory() as session:
        failures = session.scalars(select(ToolCall).where(
            ToolCall.agent_execution_id == verifier_id, ToolCall.tool_name == "git_diff",
            ToolCall.status == ToolCallStatus.FAILED, ToolCall.reason == "WORKSPACE_UNTRACKED_FILES",
        )).all()
        assert len(failures) == 2 and all(call.reason == "WORKSPACE_UNTRACKED_FILES" for call in failures)
    (workspace / "ignored.py").unlink()
    (workspace / "image.bin").write_bytes(b"\x00after")
    with pytest.raises(VerificationEvidenceError):
        read_diff()
    with factory() as session:
        reasons = set(session.scalars(select(ToolCall.reason).where(
            ToolCall.agent_execution_id == verifier_id, ToolCall.tool_name == "git_diff",
            ToolCall.status == ToolCallStatus.FAILED,
        )))
        assert {"WORKSPACE_DIFF_EMPTY", "GIT_BINARY_DIFF_UNSUPPORTED"} <= reasons


@pytest.mark.skipif(os.name != "nt", reason="Report capture requires a managed Windows Job")
def test_semantic_review_uses_committed_test_patch_and_diff_evidence(worker_audit_session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    workspace, artifacts = tmp_path / "workspace", tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    git("init", "-q")
    (workspace / "example_app.py").write_bytes(b"def value():\n    return 0\n")
    (workspace / "test_code.py").write_bytes(
        b"from example_app import value\n\ndef test_target():\n    assert value() == 1\n"
        b"\ndef test_existing():\n    assert True\n"
    )
    git("add", "example_app.py", "test_code.py")
    git("-c", "user.name=DevFlow Test", "-c", "user.email=devflow@example.invalid", "commit", "-qm", "baseline")
    base_commit = git("rev-parse", "HEAD").strip()
    factory = worker_audit_session_factory
    engine = factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    migration = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")).get_revision("f154b6e82639").module
    with engine.begin() as connection:
        connection.execution_options(schema_translate_map=None)
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations")).get_revision("a43cb50f67e9").module.upgrade()
            migration.upgrade()
    worker = Worker(factory, "semantic-worker")
    task_id = create_eligible_task(factory, repo=str(workspace), base_commit=base_commit)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot, "budget": {**snapshot["budget"], "tool_call_timeout_seconds": 20},
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"],
                "VERIFIER": ["run_command", "git_diff", "read_file"], "CODER": ["apply_patch"],
                "EXPLORER": ["read_file"],
            }},
        }
    run_id = worker.claim_once().run_id
    directories = RunDirectories(workspace, artifacts)
    stop = Event()
    registry = {"pytest": (sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_code.py")}
    with factory() as session:
        coordinator = RunCoordinator(session)
        planner = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.PLANNER, agent_input={})
        coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=planner, status=AgentExecutionStatus.SUCCEEDED,
            output={"outcome": "PLAN_READY", "plan": {
                "approach": "Fix value", "target_paths": ["example_app.py"], "steps": ["Update return value"],
                "scope_limits": "Only example_app.py", "verification": ["Run target test"],
            }}, llm_usage=None,
        )
        supervisor = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        baseline_handoff = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor, status=AgentExecutionStatus.SUCCEEDED,
            output={}, llm_usage=None, approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": planner, "to_agent_role": "VERIFIER", "context": {
                    "completed_work": "Plan prepared", "blocker": None, "evidence_refs": [],
                    "decision_required": "Run baseline", "scope_impact": "No modifications",
                },
            }),
        )
    baseline_result = worker.execute_baseline_verifier(run_id, handoff_id=baseline_handoff,
        directories=directories, command_name="pytest", registry=registry, stop_event=stop,
        verify_baseline=os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") == "1")
    assert baseline_result["valid"]
    baseline_verifier = baseline_result["execution_id"]
    baseline = baseline_result["receipt"]
    assert (baseline["status"], baseline["exit_code"]) == ("failed", 1)
    with factory() as session:
        coordinator = RunCoordinator(session)
        supervisor = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        coder_handoff = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor, status=AgentExecutionStatus.SUCCEEDED,
            output={}, llm_usage=None, approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": planner, "to_agent_role": "CODER", "context": {
                    "completed_work": "Plan prepared", "blocker": None,
                    "evidence_refs": [{"baseline_verifier_id": str(baseline_verifier)}],
                    "decision_required": "Implement", "scope_impact": "Only example_app.py",
                },
            }),
        )
    patch = "--- a/example_app.py\n+++ b/example_app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 1\n"
    coder_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        generate=lambda _prompt, _limit: ModelReply(
            {"outcome": "PATCH_DRAFT", "patch": patch, "summary": "Fix value"},
            ModelTokenUsage(input_tokens=100, output_tokens=30),
        ), max_output_tokens=200,
    )
    coder = worker.execute_coder(
        run_id, handoff_id=coder_handoff, directories=directories, model_gateway=coder_gateway,
        max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop,
    )
    with factory() as session:
        coordinator = RunCoordinator(session)
        patch_call = session.scalars(select(ToolCall.id).where(
            ToolCall.agent_execution_id == coder, ToolCall.tool_name == "apply_patch",
        )).one()
        session.rollback()
        supervisor = coordinator.start_agent_execution(run_id, worker.worker_id, role=AgentRole.SUPERVISOR, agent_input={})
        verifier_handoff = coordinator.finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=supervisor, status=AgentExecutionStatus.SUCCEEDED,
            output={}, llm_usage=None, approved_handoff=HandoffDecision.model_validate({
                "from_agent_execution_id": coder, "to_agent_role": "VERIFIER", "context": {
                    "completed_work": "Patch prepared", "blocker": None, "evidence_refs": [],
                    "decision_required": "Verify", "scope_impact": "Only example_app.py",
                },
            }),
        )
        verifier = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={}, handoff_id=verifier_handoff,
        )
    current = worker.build_verifier_command(
        run_id, agent_execution_id=verifier, directories=directories, registry=registry,
        pytest_command_names=frozenset({"pytest"}), stop_event=stop,
    )("pytest")
    assert (current["status"], current["exit_code"]) == ("succeeded", 0)
    assert not (workspace / ".pytest_cache").exists()
    assert not list(workspace.rglob("__pycache__"))
    diff = worker.build_verifier_diff(
        run_id, agent_execution_id=verifier, directories=directories, max_bytes=65536, stop_event=stop,
    )()
    prompts = []
    def generate(prompt, _limit):
        prompts.append(prompt)
        if len(prompts) == 1:
            return ModelReply({"outcome": "READ_FILE", "path": "example_app.py"}, ModelTokenUsage(input_tokens=100, output_tokens=20))
        context = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
        assert context["context"]["test_evidence"]["baseline"]["status"] == "failed"
        assert context["context"]["test_evidence"]["current"]["status"] == "succeeded"
        assert context["context"]["evidence_coverage"]["external_connections"] == "unverified"
        read_id = context["reads"][0]["tool_call_id"]
        return ModelReply({"outcome": "REVIEW", "result": "passed", "reason": "Expected value implemented",
                           "citations": [{"source": "read_file", "tool_call_id": read_id, "path": "example_app.py",
                                          "snippet": "return 1", "reason": "The value is corrected"}]},
                          ModelTokenUsage(input_tokens=100, output_tokens=30))
    semantic_gateway = ModelGateway(
        session_factory=factory, worker_id=worker.worker_id, estimate_input_tokens=lambda _: 100,
        generate=generate, max_output_tokens=200,
    )
    def report_id(receipt):
        return UUID(next(item["id"] for item in receipt["artifacts"] if item["kind"] == "pytest_junit"))
    selection = VerifierEvidenceSelection(
        baseline_verifier_id=baseline_verifier, baseline_call_id=UUID(baseline["tool_call_id"]),
        baseline_artifact_id=report_id(baseline), patch_call_id=patch_call,
        current_call_id=UUID(current["tool_call_id"]), current_artifact_id=report_id(current),
        diff_call_id=UUID(diff["tool_call_id"]), command_name="pytest", target_tests=("test_code::test_target",),
    )
    result = worker.review_verifier_semantics(
        run_id, agent_execution_id=verifier, directories=directories, selection=selection,
        model_gateway=semantic_gateway, max_read_bytes=4096, max_diff_bytes=65536,
        max_patch_bytes=4096, stop_event=stop,
    )
    assert result.comparison.target_result == result.comparison.regression_result == "passed"
    assert result.review.result == "passed" and len(prompts) == 2
    assert result.evidence_coverage.external_connections == "unverified"
    assert result.evidence_coverage.finalization_allowed is False
    if os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") == "1":
        from devflow_ai.independent_evaluation import PrivateCase
        from devflow_ai.verifier import load_independent_evaluation
        baseline_copy = tmp_path / "calibration-baseline"
        corrected_copy = tmp_path / "calibration-corrected"
        for target in (baseline_copy, corrected_copy):
            subprocess.run(["git", "clone", "-q", "--no-local", "--", str(workspace), str(target)],
                           check=True, capture_output=True, timeout=15)
        (corrected_copy / "example_app.py").write_bytes(b"def value():\n    return 1\n")
        subprocess.run(["git", "-C", str(corrected_copy), "add", "example_app.py"],
                       check=True, capture_output=True, timeout=15)
        subprocess.run(["git", "-C", str(corrected_copy), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "known"],
                       check=True, capture_output=True, timeout=15)
        corrected_commit = subprocess.run(["git", "-C", str(corrected_copy), "rev-parse", "HEAD"],
            check=True, capture_output=True, timeout=15).stdout.decode("ascii").strip()
        adapter = ("perl", "-e", 'open my $f, "<", "example_app.py" or die; local $/; '
                   'my $s=<$f>; print $s =~ /return 1/ ? "ok\\n" : "bad\\n";')
        with factory() as session, pytest.raises(VerificationEvidenceError, match="calibration"):
            RunCoordinator(session).run_independent_evaluation(
                run_id, worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
                workspace_root=workspace, artifact_root=artifacts, image="postgres:17.11", command=adapter,
                cases=(PrivateCase(b"", b"bad\n"),), suite_version="synthetic-invalid",
                max_diff_bytes=65536, max_patch_bytes=4096, timeout_seconds=15, stop_event=stop,
                calibration_workspaces=(baseline_copy, corrected_copy, corrected_commit))
        with factory() as session:
            assert not session.scalars(select(Artifact).where(Artifact.agent_execution_id == verifier,
                Artifact.kind == "independent_evaluation")).all()
        with factory() as session:
            independent_id = RunCoordinator(session).run_independent_evaluation(
                run_id, worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
                workspace_root=workspace, artifact_root=artifacts, image="postgres:17.11",
                command=adapter,
                cases=(PrivateCase(b"", b"ok\n"),), suite_version="synthetic-v1",
                max_diff_bytes=65536, max_patch_bytes=4096, timeout_seconds=15, stop_event=stop,
                calibration_workspaces=(baseline_copy, corrected_copy, corrected_commit))
        with factory() as session:
            receipt = session.get(Artifact, independent_id)
            assert receipt.kind == "independent_evaluation" and receipt.agent_execution_id == verifier
            receipt_data = json.loads(Path(receipt.content_ref).read_bytes())
            assert receipt_data["outcome"] == "passed" and receipt_data["run_id"] == str(run_id)
            assert receipt_data["calibrated"] is True and corrected_commit not in json.dumps(receipt_data)
            assert receipt_data["patch_artifact_id"] == session.get(AgentExecution, coder).output["patch_artifact_id"]
            assert receipt_data["draft_artifact_id"] == str(result.draft_artifact_id)
            assert load_independent_evaluation(session, run_id, verifier, artifact_id=independent_id,
                                               artifact_root=artifacts).outcome == "passed"
            with pytest.raises(VerificationEvidenceError, match="does not belong"):
                load_independent_evaluation(session, uuid4(), verifier, artifact_id=independent_id,
                                            artifact_root=artifacts)
            receipt_path = Path(receipt.content_ref)
            receipt_original = receipt_path.read_bytes()
            receipt_path.write_bytes(b"tampered")
            try:
                with pytest.raises(VerificationEvidenceError, match="verified"):
                    load_independent_evaluation(session, run_id, verifier, artifact_id=independent_id,
                                                artifact_root=artifacts)
            finally:
                receipt_path.write_bytes(receipt_original)
        with factory() as session:
            failed_id = RunCoordinator(session).run_independent_evaluation(
                run_id, worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
                workspace_root=workspace, artifact_root=artifacts, image="postgres:17.11",
                command=("perl", "-e", "print 1"), cases=(PrivateCase(b"", b"different"),),
                suite_version="synthetic-v1", max_diff_bytes=65536, max_patch_bytes=4096,
                timeout_seconds=15, stop_event=stop)
        with factory() as session:
            assert json.loads(Path(session.get(Artifact, failed_id).content_ref).read_bytes())["outcome"] == "failed"
        with factory() as session, pytest.raises(RunExecutionDeniedError):
            RunCoordinator(session).run_independent_evaluation(
                uuid4(), worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
                workspace_root=workspace, artifact_root=artifacts, image="postgres:17.11",
                command=("perl", "-e", "print 1"), cases=(PrivateCase(b"", b"1"),),
                suite_version="synthetic-v1", max_diff_bytes=65536, max_patch_bytes=4096,
                timeout_seconds=15, stop_event=stop)
        changed = workspace / "example_app.py"
        original = changed.read_bytes()
        changed.write_bytes(b"def value():\n    return 2\n")
        try:
            with factory() as session, pytest.raises(VerificationEvidenceError, match="differs"):
                RunCoordinator(session).run_independent_evaluation(
                    run_id, worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
                    workspace_root=workspace, artifact_root=artifacts, image="postgres:17.11",
                    command=("perl", "-e", "print 1"), cases=(PrivateCase(b"", b"1"),),
                    suite_version="synthetic-v1", max_diff_bytes=65536, max_patch_bytes=4096,
                    timeout_seconds=15, stop_event=stop)
        finally:
            changed.write_bytes(original)
        supervisor_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
            estimate_input_tokens=lambda _: 100, max_output_tokens=100,
            generate=lambda *_: ModelReply({"category": "IMPLEMENTATION_FAILURE", "reason": "Review identified a defect",
                "evidence_refs": ["semantic_review"]}, ModelTokenUsage(input_tokens=40, output_tokens=20)))
        with pytest.raises(VerificationEvidenceError, match="No recorded failure"):
            worker.classify_verification_failure(run_id, verifier_execution_id=verifier,
                draft_artifact_id=result.draft_artifact_id, directories=directories, model_gateway=supervisor_gateway,
                max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
        failed_review = SemanticReviewDraft(outcome="REVIEW", result="failed", reason="Synthetic failure for classification",
                                            citations=result.review.citations)
        with factory() as session:
            failed_draft = RunCoordinator(session).record_verifier_semantic_draft(
                run_id, worker.worker_id, agent_execution_id=verifier, selection=selection,
                review=failed_review, read_call_ids=result.read_call_ids, artifact_root=artifacts,
                max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
        classification_id = worker.classify_verification_failure(run_id, verifier_execution_id=verifier,
            draft_artifact_id=failed_draft, directories=directories, model_gateway=supervisor_gateway,
            max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
        with factory() as session:
            classification = session.get(AgentExecution, classification_id)
            assert classification.output["category"] == "IMPLEMENTATION_FAILURE"
            assert classification.output["evidence_refs"] == ["semantic_review"]
            assert session.get(Run, run_id).status == RunStatus.RUNNING
            failed_draft_path = Path(session.get(Artifact, failed_draft).content_ref)
        with factory() as session:
            observed = RunCoordinator(session).prepare_recovery_evidence(run_id, worker.worker_id,
                supervisor_execution_id=classification_id, artifact_root=artifacts,
                max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
            assert observed["category"] == "IMPLEMENTATION_FAILURE"
            assert observed["signature"].startswith("semantic-v1:")
            assert observed["history"]["previous_signature_occurrences"] == 0
            assert observed["history"]["rediagnoses_started"] == 0
        failed_draft_original = failed_draft_path.read_bytes()
        def tamper_during_model(*_):
            failed_draft_path.write_bytes(b"tampered")
            return ModelReply({"category": "IMPLEMENTATION_FAILURE", "reason": "Review identified a defect",
                "evidence_refs": ["semantic_review"]}, ModelTokenUsage(input_tokens=40, output_tokens=20))
        tampering_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
            estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=tamper_during_model)
        try:
            with pytest.raises(VerificationEvidenceError, match="verified"):
                worker.classify_verification_failure(run_id, verifier_execution_id=verifier,
                    draft_artifact_id=failed_draft, directories=directories, model_gateway=tampering_gateway,
                    max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
        finally:
            failed_draft_path.write_bytes(failed_draft_original)
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING
        assert session.get(AgentExecution, verifier).status == AgentExecutionStatus.RUNNING
        assert session.scalars(select(Verification).where(Verification.run_id == run_id)).all() == []
        saved = session.get(Artifact, result.draft_artifact_id)
        assert saved.kind == "semantic_review_draft" and saved.agent_execution_id == verifier
        content = Path(saved.content_ref).read_bytes()
        assert sha256(content).hexdigest() == saved.content_hash
        draft = json.loads(content)
        assert draft["record_type"] == "semantic_review_draft"
        assert draft["selection"]["current_call_id"] == current["tool_call_id"]
        assert draft["read_call_ids"] == [str(call_id) for call_id in result.read_call_ids]
        assert draft["review"]["result"] == "passed"
        assert draft["evidence_coverage"]["finalization_allowed"] is False
        assert draft["diff_numstat_artifact_id"] == diff["numstat_artifact_id"]
        loaded = load_semantic_review_draft(
            session, run_id, verifier, artifact_id=result.draft_artifact_id, artifact_root=artifacts,
        )
        assert loaded.selection == selection and loaded.review == result.review
        legacy = SemanticReviewDraftRecord.model_validate(loaded.model_dump(
            exclude={"evidence_coverage", "diff_numstat_artifact_id"},
        ))
        assert legacy.diff_numstat_artifact_id is None
        assert legacy.evidence_coverage.finalization_allowed is False
        with pytest.raises(VerificationEvidenceError, match="does not belong"):
            load_semantic_review_draft(
                session, uuid4(), verifier, artifact_id=result.draft_artifact_id, artifact_root=artifacts,
            )
        saved_path = Path(saved.content_ref)
        saved_path.write_bytes(b"tampered")
        with pytest.raises(VerificationEvidenceError, match="verified"):
            load_semantic_review_draft(
                session, run_id, verifier, artifact_id=result.draft_artifact_id, artifact_root=artifacts,
            )
        saved_path.write_bytes(content)
    with factory() as session:
        readiness = RunCoordinator(session).assess_verification_readiness(
            run_id, worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
            artifact_root=artifacts, max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
        )
    assert readiness.status == "blocked"
    expected_missing = {"external_connection_provenance", "test_integrity"}
    if os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1":
        expected_missing.add("baseline_workspace_attestation")
    assert set(readiness.missing_evidence) == expected_missing
    with factory() as session:
        assert session.get(Run, run_id).status == RunStatus.RUNNING
        assert session.get(AgentExecution, verifier).status == AgentExecutionStatus.RUNNING
        assert session.scalars(select(Verification).where(Verification.run_id == run_id)).all() == []
    with factory() as session, pytest.raises(VerificationEvidenceError, match="read history"):
        RunCoordinator(session).record_verifier_semantic_draft(
            run_id, worker.worker_id, agent_execution_id=verifier, selection=selection,
            review=result.review, read_call_ids=(), artifact_root=artifacts,
            max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
        )
    with monkeypatch.context() as scoped:
        def lost_lock(_receipt, _session):
            raise WorkspaceLockUnavailableError("simulated lost lock")
        scoped.setattr(WorkspaceLockReceipt, "assert_held", lost_lock)
        with factory() as session, pytest.raises(WorkspaceLockUnavailableError):
            RunCoordinator(session).record_verifier_semantic_draft(
                run_id, worker.worker_id, agent_execution_id=verifier, selection=selection,
                review=result.review, read_call_ids=result.read_call_ids, artifact_root=artifacts,
                max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
            )
    with factory() as session:
        assert len(session.scalars(select(Artifact).where(
            Artifact.agent_execution_id == verifier, Artifact.kind == "semantic_review_draft",
        )).all()) == (2 if os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") == "1" else 1)
    with factory.begin() as session:
        session.get(AgentExecution, verifier).handoff_id = None
    with pytest.raises(VerificationEvidenceError, match="Supervisor-approved"):
        worker.review_verifier_semantics(
            run_id, agent_execution_id=verifier, directories=directories, selection=selection,
            model_gateway=semantic_gateway, max_read_bytes=4096, max_diff_bytes=65536,
            max_patch_bytes=4096, stop_event=stop,
        )
    with factory.begin() as session:
        session.get(AgentExecution, verifier).handoff_id = verifier_handoff
        session.get(Run, run_id).lease_expires_at = func.clock_timestamp()
    with factory() as session, pytest.raises(RunExecutionDeniedError):
        RunCoordinator(session).record_verifier_semantic_draft(
            run_id, worker.worker_id, agent_execution_id=verifier, selection=selection,
            review=result.review, read_call_ids=result.read_call_ids, artifact_root=artifacts,
            max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
        )
    with factory() as session, pytest.raises(RunExecutionDeniedError):
        RunCoordinator(session).assess_verification_readiness(
            run_id, worker.worker_id, agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
            artifact_root=artifacts, max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop,
        )
    with factory() as session:
        assert len(session.scalars(select(Artifact).where(
            Artifact.agent_execution_id == verifier, Artifact.kind == "semantic_review_draft",
        )).all()) == (2 if os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") == "1" else 1)

    if os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") == "1":
        from devflow_ai.verification_config import VerificationConfig
        config = VerificationConfig(task_id=task_id, base_commit=base_commit, command_name="pytest",
            command=registry["pytest"], target_tests=("test_code::test_target",),
            external_connections="none", independent_suite_version="synthetic-v1")
        with factory.begin() as session:
            session.get(Run, run_id).lease_expires_at = func.clock_timestamp() + text("interval '5 minutes'")
        if os.environ.get("DEVFLOW_TEST_FAILURE_FINAL") == "1":
            limit_reached = os.environ.get("DEVFLOW_TEST_FAILURE_LIMIT") == "1"
            if limit_reached:
                with factory.begin() as session:
                    task = session.get(Task, task_id)
                    snapshot = task.policy_snapshot
                    task.policy_snapshot = {**snapshot, "budget": {**snapshot["budget"], "max_retries_per_task": 0}}
            failure_args = dict(supervisor_execution_id=classification_id, verification_config=config,
                workspace_root=workspace, artifact_root=artifacts, max_diff_bytes=65536,
                max_patch_bytes=4096, stop_event=stop)
            with factory() as session, monkeypatch.context() as scoped:
                scoped.setattr(session, "flush", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failure commit probe")))
                with pytest.raises(RuntimeError, match="failure commit probe"):
                    RunCoordinator(session).finalize_implementation_failure(run_id, worker.worker_id, **failure_args)
            with factory() as session:
                assert session.get(Run, run_id).status == RunStatus.RUNNING
                assert session.get(Task, task_id).status == TaskStatus.RUNNING
                assert not session.scalars(select(Verification).where(Verification.run_id == run_id)).all()
                assert not session.scalars(select(FailureRecord).where(FailureRecord.run_id == run_id)).all()
            finalized_failure = worker.classify_and_finalize_implementation_failure(run_id,
                verifier_execution_id=verifier, draft_artifact_id=failed_draft, directories=directories,
                verification_config=config, model_gateway=supervisor_gateway,
                max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
            verification_id = finalized_failure["verification_id"]
            with factory() as session:
                assert session.get(Verification, verification_id).result == VerificationResult.FAILED
                assert session.get(Run, run_id).status == RunStatus.FAILED
                assert session.get(Task, task_id).status == (TaskStatus.ESCALATED if limit_reached else TaskStatus.RETRYING)
                failure = session.scalars(select(FailureRecord).where(FailureRecord.run_id == run_id)).one()
                assert failure.category == FailureCategory.IMPLEMENTATION_FAILURE
                assert failure.signature.startswith("semantic-v1:")
                assert failure.recovery_action == ("ESCALATE_TO_HUMAN" if limit_reached else "REVISE_IMPLEMENTATION")
            if limit_reached:
                with factory() as session, pytest.raises(TaskStateConflictError):
                    RunCoordinator(session).start_retry_run(task_id, worker.worker_id, "retry-1")
            else:
                from devflow_ai.coordinator import RunCreationConflictError
                with factory() as session, monkeypatch.context() as scoped:
                    scoped.setattr(session, "flush", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("retry commit probe")))
                    with pytest.raises(RuntimeError, match="retry commit probe"):
                        RunCoordinator(session).start_retry_run(task_id, worker.worker_id, "retry-1")
                with factory() as session:
                    assert session.get(Task, task_id).status == TaskStatus.RETRYING
                    assert len(session.scalars(select(Run).where(Run.task_id == task_id)).all()) == 1
                def route_retry(prompt, _limit):
                    supplied = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
                    assert supplied["retry_context"]["previous_run_id"] == str(run_id)
                    return ModelReply({"outcome": "EXPLORE", "reason": "Investigate prior failure",
                        "exploration_focus": "Review the cited implementation"},
                        ModelTokenUsage(input_tokens=40, output_tokens=20))
                retry_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=route_retry)
                explorer_prompts = []
                def explore_retry(prompt, _limit):
                    data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
                    explorer_prompts.append(data)
                    if len(explorer_prompts) == 1:
                        assert data["retry_context"]["previous_run_id"] == str(run_id)
                        response = {"path": "example_app.py"}
                    else:
                        response = {"summary": "Investigated the previous failure", "read_evidence": {
                            "path": "example_app.py", "tool_call_id": data["read"]["tool_call_id"]}}
                    return ModelReply(response, ModelTokenUsage(input_tokens=40, output_tokens=20))
                explorer_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=explore_retry)
                planner_gateway = PlannerModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100,
                    generate=lambda *_: pytest.fail("Planner is not part of this handoff"))
                retry_workspaces, retry_artifacts = tmp_path / "retry-workspaces", tmp_path / "retry-artifacts"
                retry_workspaces.mkdir()
                retry_artifacts.mkdir()
                resume_args = dict(creation_idempotency_key="retry-1", workspaces_root=retry_workspaces,
                    artifacts_root=retry_artifacts, supervisor_gateway=retry_gateway, explorer_gateway=explorer_gateway,
                    planner_gateway=planner_gateway, max_read_bytes=4096, stop_event=stop)
                resumed = worker.resume_implementation_recovery(task_id, **resume_args)
                retry_id = resumed["run_id"]
                assert resumed["outcome"] == "dispatched" and len(explorer_prompts) == 2
                assert worker.resume_implementation_recovery(task_id, **resume_args)["outcome"] == "existing_retry"
                with factory() as session, pytest.raises(RunCreationConflictError):
                    RunCoordinator(session).start_retry_run(task_id, "another-worker", "retry-1")
                with factory() as session, pytest.raises(TaskStateConflictError):
                    RunCoordinator(session).start_retry_run(task_id, worker.worker_id, "another-retry")
                with factory() as session:
                    assert session.get(Run, run_id).status == RunStatus.FAILED
                    assert session.get(Run, retry_id).status == RunStatus.RUNNING
                    assert session.get(Task, task_id).status == TaskStatus.RUNNING
                    retry_context = ContextBuilder(session).build_retry_context(retry_id)
                    assert retry_context["previous_run_id"] == str(run_id)
                    assert retry_context["recovery_action"] == "REVISE_IMPLEMENTATION"
                    assert retry_context["semantic_review"]["result"] == "failed"
                    retry_explorer = session.scalars(select(AgentExecution).where(
                        AgentExecution.handoff_id == resumed["handoff_id"])).one()
                    retry_explorer_id = retry_explorer.id
                    assert ContextBuilder(session).build_planner_input(retry_id,
                        explorer_execution_id=retry_explorer_id)["retry_context"]["previous_run_id"] == str(run_id)
                plan_supervisor = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100,
                    generate=lambda *_: ModelReply({"outcome": "PLAN", "reason": "Evidence available",
                        "next_goal": "Plan around the previous failure"}, ModelTokenUsage(input_tokens=40, output_tokens=20)))
                def plan_retry(prompt, _limit):
                    supplied = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
                    assert supplied["retry_context"]["previous_run_id"] == str(run_id)
                    return ModelReply({"outcome": "PLAN_READY", "plan": {"approach": "Revise the implementation",
                        "target_paths": ["example_app.py"], "steps": ["Address prior review"],
                        "scope_limits": "Only example_app.py", "verification": ["Run tests"]}},
                        ModelTokenUsage(input_tokens=40, output_tokens=20))
                retry_planner_gateway = PlannerModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100, generate=plan_retry)
                planner_handoff = run_handoff_cycle(factory, retry_id, worker.worker_id,
                    supervise=worker.build_post_explorer_supervise(retry_id, explorer_execution_id=retry_explorer_id,
                        model_gateway=plan_supervisor, stop_event=stop),
                    receivers=worker.build_handoff_receivers(retry_id,
                        directories=RunDirectories(retry_workspaces / str(retry_id), retry_artifacts / str(retry_id)),
                        max_bytes=4096, stop_event=stop, explorer_gateway=explorer_gateway,
                        planner_gateway=retry_planner_gateway))
                with factory() as session:
                    planner_execution = session.scalars(select(AgentExecution).where(
                        AgentExecution.handoff_id == planner_handoff)).one()
                    assert planner_execution.output["outcome"] == "PLAN_READY"
                    assert planner_execution.input["retry_context"]["previous_run_id"] == str(run_id)
                    retry_planner_id = planner_execution.id
                retry_directories = RunDirectories(retry_workspaces / str(retry_id), retry_artifacts / str(retry_id))
                code_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100,
                    generate=lambda *_: ModelReply({"outcome": "CODE", "reason": "Ready to revise",
                        "next_goal": "Implement revised plan"}, ModelTokenUsage(input_tokens=40, output_tokens=20)))
                retry_receivers = worker.build_handoff_receivers(retry_id, directories=retry_directories,
                    max_bytes=4096, stop_event=stop, explorer_gateway=explorer_gateway,
                    planner_gateway=retry_planner_gateway, verification_config=config)
                baseline_handoff_retry = run_handoff_cycle(factory, retry_id, worker.worker_id,
                    supervise=worker.build_post_planner_supervise(retry_id, planner_execution_id=retry_planner_id,
                        model_gateway=code_gateway, stop_event=stop), receivers=retry_receivers)
                with factory() as session:
                    retry_baseline = session.scalars(select(AgentExecution).where(
                        AgentExecution.handoff_id == baseline_handoff_retry)).one()
                    assert retry_baseline.output["outcome"] == "BASELINE_RECORDED"
                retry_route = worker.execute_supervisor_after_planner(retry_id,
                    planner_execution_id=retry_planner_id, model_gateway=code_gateway, stop_event=stop)
                same_patch_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=200,
                    generate=lambda *_: ModelReply({"outcome": "PATCH_DRAFT", "patch": patch,
                        "summary": "Repeat old patch"}, ModelTokenUsage(input_tokens=40, output_tokens=20)))
                with pytest.raises(ValueError, match="patch application"):
                    worker.execute_coder(retry_id, handoff_id=retry_route.handoff_id, directories=retry_directories,
                        model_gateway=same_patch_gateway, max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop)
                with factory() as session:
                    assert session.scalar(select(ToolCall.id).where(ToolCall.run_id == retry_id,
                        ToolCall.reason == "UNCHANGED_FAILED_PATCH")) is not None
                    assert (retry_directories.workspace_root / "example_app.py").read_bytes() == b"def value():\n    return 0\n"
                retry_route = worker.execute_supervisor_after_planner(retry_id,
                    planner_execution_id=retry_planner_id, model_gateway=code_gateway, stop_event=stop)
                revised_patch = patch.replace("+    return 1\n", "+    return 1 + 0\n")
                def code_retry(prompt, _limit):
                    supplied = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
                    assert supplied["context"]["retry_context"]["previous_run_id"] == str(run_id)
                    return ModelReply({"outcome": "PATCH_DRAFT", "patch": revised_patch,
                        "summary": "Revise previous patch"}, ModelTokenUsage(input_tokens=40, output_tokens=20))
                retry_coder_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=200, generate=code_retry)
                retry_coder_id = worker.execute_coder(retry_id, handoff_id=retry_route.handoff_id,
                    directories=retry_directories, model_gateway=retry_coder_gateway,
                    max_patch_bytes=4096, max_read_bytes=4096, stop_event=stop)
                with factory() as session:
                    assert session.get(AgentExecution, retry_coder_id).output["outcome"] == "PATCH_READY"
                verify_retry_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=100,
                    generate=lambda *_: ModelReply({"outcome": "VERIFY", "reason": "Check revised patch",
                        "next_goal": "Run the verification"}, ModelTokenUsage(input_tokens=40, output_tokens=20)))
                def semantic_retry(prompt, _limit):
                    supplied = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
                    return ModelReply({"outcome": "REVIEW", "result": "passed", "reason": "Revised implementation",
                        "citations": [{"source": "workspace_diff", "path": "example_app.py",
                            "tool_call_id": supplied["context"]["workspace_diff"]["tool_call_id"],
                            "snippet": "return 1 + 0", "diff_side": "added", "reason": "Value is computed"}]},
                        ModelTokenUsage(input_tokens=40, output_tokens=20))
                semantic_retry_gateway = ModelGateway(session_factory=factory, worker_id=worker.worker_id,
                    estimate_input_tokens=lambda _: 100, max_output_tokens=200, generate=semantic_retry)
                verifier_receivers = worker.build_handoff_receivers(retry_id, directories=retry_directories,
                    max_bytes=4096, stop_event=stop, explorer_gateway=explorer_gateway,
                    planner_gateway=retry_planner_gateway, verification_config=config,
                    verifier_gateway=semantic_retry_gateway, verifier_max_diff_bytes=65536, max_patch_bytes=4096)
                verifier_handoff_retry = run_handoff_cycle(factory, retry_id, worker.worker_id,
                    supervise=worker.build_post_coder_supervise(retry_id, coder_execution_id=retry_coder_id,
                        directories=retry_directories, model_gateway=verify_retry_gateway,
                        max_patch_bytes=4096, stop_event=stop), receivers=verifier_receivers)
                with factory() as session:
                    retry_verifier = session.scalars(select(AgentExecution).where(
                        AgentExecution.handoff_id == verifier_handoff_retry)).one()
                    assert retry_verifier.output["preliminary_public_outcome"] == "passed"
                    retry_verifier_id = retry_verifier.id
                    retry_draft_id = UUID(retry_verifier.output["draft_artifact_id"])
                retry_final = worker.finalize_with_independent_evaluation(retry_id,
                    agent_execution_id=retry_verifier_id, draft_artifact_id=retry_draft_id,
                    directories=retry_directories, verification_config=config, image="postgres:17.11",
                    command=adapter, cases=(PrivateCase(b"", b"ok\n"),),
                    calibration_workspaces=(baseline_copy, corrected_copy, corrected_commit),
                    max_diff_bytes=65536, max_patch_bytes=4096, timeout_seconds=15, stop_event=stop)
                assert retry_final["outcome"] == "passed"
                with factory() as session:
                    assert session.get(Run, run_id).status == RunStatus.FAILED
                    assert session.get(Run, retry_id).status == RunStatus.PASSED
                    assert session.get(Task, task_id).status == TaskStatus.AWAITING_REVIEW
            return
        final_args = dict(agent_execution_id=verifier, draft_artifact_id=result.draft_artifact_id,
            independent_artifact_id=independent_id, verification_config=config, workspace_root=workspace,
            artifact_root=artifacts, max_diff_bytes=65536, max_patch_bytes=4096, stop_event=stop)
        with factory() as session, pytest.raises(VerificationEvidenceError, match="calibrated"):
            RunCoordinator(session).finalize_passing_verification(run_id, worker.worker_id,
                **{**final_args, "independent_artifact_id": failed_id})
        with factory() as session:
            assert session.get(Run, run_id).status == RunStatus.RUNNING
            assert not session.scalars(select(Verification).where(Verification.run_id == run_id)).all()
        with factory() as session, monkeypatch.context() as scoped:
            scoped.setattr(session, "flush", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("commit probe")))
            with pytest.raises(RuntimeError, match="commit probe"):
                RunCoordinator(session).finalize_passing_verification(run_id, worker.worker_id, **final_args)
        with factory() as session:
            assert session.get(Run, run_id).status == RunStatus.RUNNING
            assert session.get(Task, task_id).status == TaskStatus.RUNNING
            assert session.get(AgentExecution, verifier).status == AgentExecutionStatus.RUNNING
            assert not session.scalars(select(Verification).where(Verification.run_id == run_id)).all()
        finalized = worker.finalize_with_independent_evaluation(run_id, agent_execution_id=verifier,
            draft_artifact_id=result.draft_artifact_id, directories=directories, verification_config=config,
            image="postgres:17.11", command=adapter, cases=(PrivateCase(b"", b"ok\n"),),
            calibration_workspaces=(baseline_copy, corrected_copy, corrected_commit),
            max_diff_bytes=65536, max_patch_bytes=4096, timeout_seconds=15, stop_event=stop)
        assert finalized["outcome"] == "passed"
        verification_id = finalized["verification_id"]
        with factory() as session:
            assert session.get(Verification, verification_id).result == VerificationResult.PASSED
            assert session.get(Run, run_id).status == RunStatus.PASSED
            assert session.get(Task, task_id).status == TaskStatus.AWAITING_REVIEW
            assert session.get(AgentExecution, verifier).status == AgentExecutionStatus.SUCCEEDED


def test_verifier_loads_only_its_committed_command_evidence(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "verifier-worker")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    create_eligible_task(factory)
    other_run_id = worker.claim_once().run_id
    with factory() as session:
        coordinator = RunCoordinator(session)
        verifier_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
        other_id = coordinator.start_agent_execution(
            other_run_id, worker.worker_id, role=AgentRole.VERIFIER, agent_input={},
        )
        coder_id = coordinator.start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.CODER, agent_input={},
        )
    with factory.begin() as session:
        repository = ToolCallRepository(session)
        valid_id = repository.add_requested(verifier_id, "run_command", {"command": "test"}, run_id=run_id)
        repository.save_result(valid_id, status=ToolCallStatus.FAILED, exit_code=1, stderr_excerpt="failure")
        foreign_id = repository.add_requested(other_id, "run_command", {}, run_id=other_run_id)
        coder_call_id = repository.add_requested(coder_id, "run_command", {}, run_id=run_id)
        wrong_tool_id = repository.add_requested(verifier_id, "read_file", {}, run_id=run_id)
        pending_id = repository.add_requested(verifier_id, "run_command", {}, run_id=run_id)
        for call_id in (foreign_id, coder_call_id, wrong_tool_id):
            repository.save_result(call_id, status=ToolCallStatus.SUCCEEDED, exit_code=0)

    with factory() as session:
        evidence = load_verifier_command_evidence(session, run_id, verifier_id, (valid_id,))
        assert evidence[0]["status"] == "failed" and evidence[0]["exit_code"] == 1
        assert evidence[0]["stderr_excerpt"] == "failure"
        for invalid_id in (uuid4(), foreign_id, coder_call_id, wrong_tool_id, pending_id):
            with pytest.raises(VerificationEvidenceError):
                load_verifier_command_evidence(session, run_id, verifier_id, (invalid_id,))
        with pytest.raises(VerificationEvidenceError, match="Duplicate"):
            load_verifier_command_evidence(session, run_id, verifier_id, (valid_id, valid_id))
        with pytest.raises(VerificationEvidenceError, match="Verifier execution"):
            load_verifier_command_evidence(session, run_id, coder_id, ())


def isolated_child_environment(run_session_factory):
    engine = run_session_factory.kw["bind"]
    assert engine.url.database == "devflow_test"
    schema = engine.get_execution_options()["schema_translate_map"][None]
    assert schema.startswith("service_test_") and schema != "public"
    environment = os.environ.copy()
    environment["PGOPTIONS"] = f"-c search_path={schema},public"
    environment["DEVFLOW_TEST_SCHEMA"] = schema
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Verify libpq's child-only search path before invoking any mutating CLI command.
    preflight = '''
import os
from sqlalchemy import text
from devflow_ai.database import create_db_engine
engine = create_db_engine()
try:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "devflow_test"
        assert connection.scalar(text("SELECT current_schema()")) == os.environ["DEVFLOW_TEST_SCHEMA"]
finally:
    engine.dispose()
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", preflight], env=environment,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding="utf-8", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, "Child database/schema isolation preflight failed"
    return environment


def run_worker_process(environment):
    return subprocess.run(
        [sys.executable, "-B", "-m", "devflow_ai.worker", "--once", "--worker-id", "subprocess-worker"],
        env=environment, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_real_worker_process_claims_and_then_reports_idle(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    environment = isolated_child_environment(run_session_factory)
    result = run_worker_process(environment)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["event"] == "worker_run_claimed"
    assert receipt["task_id"] == str(task_id)
    assert receipt["status"] == "RUNNING"
    assert receipt["worker_id"] == "subprocess-worker"
    with run_session_factory() as session:
        run = session.scalar(select(Run).where(Run.task_id == task_id))
        assert str(run.id) == receipt["run_id"]
        assert run.creation_idempotency_key == receipt["creation_idempotency_key"]
        assert session.get(Task, task_id).status == TaskStatus.RUNNING
    idle = run_worker_process(environment)
    assert idle.returncode == 0, idle.stderr
    assert json.loads(idle.stdout)["event"] == "worker_idle"
    with run_session_factory() as session:
        assert len(session.scalars(select(Run)).all()) == 1


def test_real_worker_process_reports_failure_without_sensitive_policy_input(run_session_factory):
    task_id = create_eligible_task(run_session_factory)
    with run_session_factory() as session:
        session.get(Task, task_id).policy_snapshot = {"invalid": "test-value-must-not-leak"}
        session.commit()
    environment = isolated_child_environment(run_session_factory)
    result = run_worker_process(environment)
    assert result.returncode == 1
    assert not result.stdout
    assert json.loads(result.stderr)["event"] == "worker_claim_failed"
    assert "test-value-must-not-leak" not in result.stderr
    with run_session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.ELIGIBLE
        assert not session.scalars(select(Run)).all()


def test_prior_exploration_previews_share_one_budget_and_say_what_was_cut(
    worker_audit_session_factory,
):
    """A budgeted clip is reported as a clip, never as "the tool truncated the read"."""
    from devflow_ai.context import PRIOR_PREVIEW_TOTAL_CHARS

    factory = worker_audit_session_factory
    worker = Worker(factory, "exploration-budget")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    explorers = []
    first_preview = "b" * (PRIOR_PREVIEW_TOTAL_CHARS + 5000)
    for path, preview in (("huge.py", first_preview), ("small.py", "small evidence")):
        with factory() as session:
            explorer_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
                role=AgentRole.EXPLORER, agent_input={})
        with factory.begin() as session:
            repository = ToolCallRepository(session)
            call_id = repository.add_requested(explorer_id, "read_file", {"path": path}, run_id=run_id)
            repository.save_result(call_id, status=ToolCallStatus.SUCCEEDED, stdout_excerpt=preview)
        with factory() as session:
            RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id, agent_execution_id=explorer_id,
                status=AgentExecutionStatus.SUCCEEDED, output={"summary": "Read file"}, llm_usage=None,
                read_evidence=(ReadEvidenceRef(path=path, tool_call_id=call_id),))
        explorers.append(explorer_id)

    with factory() as session:
        context = ContextBuilder(session).build_planner_input(run_id, explorer_execution_id=explorers[1])
    prior = context["prior_explorations"][0]["read_evidence"][0]
    assert prior["preview_chars"] == len(first_preview)
    assert len(prior["preview"]) == PRIOR_PREVIEW_TOTAL_CHARS
    assert prior["preview_clipped"] is True
    # The tool read the whole file; only this prompt was clipped, and it says so.
    assert prior["truncated"] is False


def test_supervisor_exploration_carries_references_without_preview_bodies(
    worker_audit_session_factory,
):
    """Routing needs the finding and the references, not a second copy of the bytes."""
    factory = worker_audit_session_factory
    worker = Worker(factory, "supervisor-view")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(run_id, worker.worker_id,
            role=AgentRole.EXPLORER, agent_input={})
    with factory.begin() as session:
        repository = ToolCallRepository(session)
        call_id = repository.add_requested(explorer_id, "read_file", {"path": "src.py"}, run_id=run_id)
        repository.save_result(call_id, status=ToolCallStatus.SUCCEEDED, stdout_excerpt="def f():\n    return 1\n")
    with factory() as session:
        RunCoordinator(session).finish_agent_execution(run_id, worker.worker_id, agent_execution_id=explorer_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"summary": "Read src.py"}, llm_usage=None,
            read_evidence=(ReadEvidenceRef(path="src.py", tool_call_id=call_id),))

    with factory() as session:
        builder = ContextBuilder(session)
        routing = builder.build_supervisor_exploration(run_id, explorer_execution_id=explorer_id)
        planner = builder.build_planner_input(run_id, explorer_execution_id=explorer_id)

    entry = routing["read_evidence"][0]
    assert "preview" not in entry
    assert entry["preview_omitted"] is True
    assert entry["preview_chars"] == len("def f():\n    return 1\n")
    assert entry["truncated"] is False
    assert routing["preview_bodies"] == "omitted; the Planner receives them"
    # Routing can see what was already read, so it need not spend a call re-reading it.
    already = routing["already_read"]
    assert len(already) == 1 and already[0]["path"] == "src.py"
    assert already[0]["tool_call_ids"] == [str(call_id)]
    assert already[0]["truncated"] is False and already[0]["reads"] == 1
    # This row was seeded without an observation, so no coverage is claimed for it.
    assert already[0]["covered_ranges"] == []
    # The Planner still gets the body: the split is explicit, not a loss of evidence.
    assert planner["read_evidence"][0]["preview"] == "def f():\n    return 1\n"

    # Prompt and payload agree: the Supervisor is told it has no bodies, and it has none.
    from devflow_ai.supervisor import build_post_explorer_supervisor_prompt

    assert "not file contents" in build_post_explorer_supervisor_prompt(routing)
    assert '"preview":' not in json.dumps(routing)


def test_a_ranged_read_is_visible_to_the_planner_and_to_routing(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "range-evidence")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
    with factory.begin() as session:
        repository = ToolCallRepository(session)
        call_id = repository.add_requested(
            explorer_id, "read_file", {"path": "core.py", "offset": 20, "limit": 5}, run_id=run_id)
        repository.save_result(call_id, status=ToolCallStatus.SUCCEEDED,
                               stdout_excerpt="line 20\nline 21\n", reason="OUTPUT_TRUNCATED")
    with factory() as session:
        RunCoordinator(session).finish_agent_execution(
            run_id, worker.worker_id, agent_execution_id=explorer_id,
            status=AgentExecutionStatus.SUCCEEDED, output={"summary": "Option is at line 20"},
            llm_usage=None, read_evidence=(ReadEvidenceRef(path="core.py", tool_call_id=call_id),))

    with factory() as session:
        builder = ContextBuilder(session)
        planner = builder.build_planner_input(run_id, explorer_execution_id=explorer_id)
        routing = builder.build_supervisor_exploration(run_id, explorer_execution_id=explorer_id)

    entry = planner["read_evidence"][0]
    assert (entry["offset"], entry["limit"]) == (20, 5)
    # Ranged and partial: the Planner can see it has lines 20-21, not the whole file.
    assert entry["truncated"] is True
    # A row with no recorded observation says nothing about the total instead of inventing one.
    assert "total_lines" not in entry
    # Routing sees the same read; with no recorded observation it cannot claim coverage, and
    # it does not substitute the requested window for the actual one.
    already_read = routing["already_read"][0]
    assert already_read["covered_ranges"] == []
    assert already_read["reads"] == 1 and already_read["truncated"] is True


def test_a_range_cut_by_the_byte_limit_still_saves_the_whole_file(worker_audit_session_factory, tmp_path):
    """A cut preview keeps the same guarantee as a head read: the bytes are preserved."""
    factory = worker_audit_session_factory
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }
    worker = Worker(factory, "range-cap")
    claim = worker.claim_once()
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    directories = worker.prepare_run_directories(
        claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)
    body = "".join(f"line {number}\n" for number in range(1, 21))
    (directories.workspace_root / "core.py").write_bytes(body.encode("utf-8"))
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            claim.run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
    read_file = worker.build_agent_read_file(
        claim.run_id, agent_execution_id=execution_id, directories=directories,
        max_bytes=12, stop_event=Event())

    receipt = read_file("core.py", 1, 20)

    # The preview keeps the whole first line and a bounded prefix of the second; the point is
    # that it is not empty, and that the cut is stated rather than silently dropped.
    assert receipt.truncated is True and receipt.preview == "line 1\nline "
    assert (receipt.end_line, receipt.byte_truncated, receipt.partial_line) == (2, True, True)
    assert len(receipt.artifact_ids) == 1
    with factory() as observer:
        artifact = observer.get(Artifact, receipt.artifact_ids[0])
        assert Path(artifact.content_ref).read_bytes() == body.encode("utf-8")


def test_already_read_merges_overlapping_windows_into_coverage(worker_audit_session_factory):
    """The #3084 shape: three overlapping reads of one file must read as one coverage claim."""
    factory = worker_audit_session_factory
    worker = Worker(factory, "already-read")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})

    def record(offset, limit, start_line, end_line, *, total=3415):
        with factory.begin() as session:
            repository = ToolCallRepository(session)
            call = repository.add_requested(
                explorer_id, "read_file",
                {"path": "core.py", "offset": offset, "limit": limit,
                 "observed": {"start_line": start_line, "end_line": end_line,
                              "total_lines": total, "has_more": end_line < total,
                              "byte_truncated": False, "partial_line": False}},
                run_id=run_id)
            repository.save_result(call, status=ToolCallStatus.SUCCEEDED,
                                   stdout_excerpt="line\n", reason="OUTPUT_TRUNCATED")
            return call

    record(1656, 900, 1656, 2555)
    record(2555, 860, 2555, 3415)
    # A later, narrower re-read of a range that is already covered.
    record(2555, 450, 2555, 3004)
    with factory.begin() as session:
        repository = ToolCallRepository(session)
        failed = repository.add_requested(
            explorer_id, "read_file", {"path": "other.py", "offset": 1, "limit": 5}, run_id=run_id)
        repository.save_result(failed, status=ToolCallStatus.FAILED, reason="FILE_NOT_FOUND")

    with factory() as session:
        coverage = ContextBuilder(session).build_already_read(run_id)

    assert [entry["path"] for entry in coverage] == ["core.py"]
    # 1656-2555 and 2555-3415 are adjacent, and 2555-3004 adds nothing new: one range.
    assert coverage[0]["covered_ranges"] == [[1656, 3415]]
    assert coverage[0]["reads"] == 3 and coverage[0]["total_lines"] == 3415
    assert coverage[0]["truncated"] is True
    assert len(coverage[0]["tool_call_ids"]) == 3


def test_coverage_keeps_separate_files_apart(worker_audit_session_factory):
    factory = worker_audit_session_factory
    worker = Worker(factory, "coverage-files")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory() as session:
        explorer_id = RunCoordinator(session).start_agent_execution(
            run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
    for path, start_line, end_line in (("a.py", 1, 10), ("b.py", 5, 9)):
        with factory.begin() as session:
            repository = ToolCallRepository(session)
            call = repository.add_requested(
                explorer_id, "read_file",
                {"path": path, "offset": start_line, "limit": end_line - start_line + 1,
                 "observed": {"start_line": start_line, "end_line": end_line,
                              "total_lines": None, "has_more": True,
                              "byte_truncated": False, "partial_line": False}},
                run_id=run_id)
            repository.save_result(call, status=ToolCallStatus.SUCCEEDED,
                                   stdout_excerpt="x\n", reason="OUTPUT_TRUNCATED")

    with factory() as session:
        coverage = ContextBuilder(session).build_already_read(run_id)

    assert [(entry["path"], entry["covered_ranges"]) for entry in coverage] == [
        ("a.py", [[1, 10]]), ("b.py", [[5, 9]])]
    # An unknown total stays unknown instead of being inferred from the window.
    assert all(entry["total_lines"] is None for entry in coverage)


def test_coverage_does_not_mark_a_partially_read_line_complete():
    from devflow_ai.context import summarize_read_coverage

    entries = [
        {"path": "x.py", "start_line": 1, "end_line": 3, "partial_line": True},
        {"path": "y.py", "start_line": 4, "end_line": 4, "partial_line": True},
    ]
    coverage = summarize_read_coverage(entries)
    assert coverage[0]["covered_ranges"] == [[1, 2]]
    assert coverage[1]["covered_ranges"] == []


def test_a_head_read_reports_the_total_only_when_it_actually_reached_the_end(
    worker_audit_session_factory, tmp_path,
):
    """A head read does not scan the file, so an unknown total must stay unknown."""
    factory = worker_audit_session_factory
    task_id = create_eligible_task(factory)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        snapshot = task.policy_snapshot
        task.policy_snapshot = {
            **snapshot,
            "permissions": {"allowed_tools_by_role": {
                **snapshot["permissions"]["allowed_tools_by_role"], "EXPLORER": ["read_file"],
            }},
        }
    worker = Worker(factory, "head-window")
    claim = worker.claim_once()
    workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    directories = worker.prepare_run_directories(
        claim.run_id, workspaces_root=workspaces, artifacts_root=artifacts)
    (directories.workspace_root / "small.py").write_bytes(b"one\ntwo\n")
    big = b"".join(b"line\n" for _ in range(1000))
    (directories.workspace_root / "big.py").write_bytes(big)
    with factory() as session:
        execution_id = RunCoordinator(session).start_agent_execution(
            claim.run_id, worker.worker_id, role=AgentRole.EXPLORER, agent_input={})
    read_file = worker.build_agent_read_file(
        claim.run_id, agent_execution_id=execution_id, directories=directories,
        max_bytes=64, stop_event=Event())

    whole = read_file("small.py")
    assert (whole.start_line, whole.end_line, whole.total_lines) == (1, 2, 2)
    assert (whole.has_more, whole.byte_truncated) == (False, False)

    cut = read_file("big.py")
    assert cut.truncated is True
    assert cut.total_lines is None and cut.has_more is True and cut.byte_truncated is True
    # What the preview did contain is still reported exactly: 12 whole lines plus a partial
    # thirteenth, so the last line it touched is 13.
    assert cut.start_line == 1
    assert cut.end_line == cut.preview.count("\n") + 1


def test_the_model_budget_splits_settled_and_reserved_requests(worker_audit_session_factory):
    """A route needs to know what is left; an unusable legacy usage stays unknown."""
    factory = worker_audit_session_factory
    worker = Worker(factory, "budget-report")
    create_eligible_task(factory)
    run_id = worker.claim_once().run_id
    with factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.EXPLORER.value.lower(),
            status=AgentExecutionStatus.SUCCEEDED, input={}, output={},
            llm_usage={"model_requests": {
                "settled": {"worker_id": "w", "reserved_tokens": 4096,
                            "actual_usage": {"input_tokens": 100, "output_tokens": 20}},
                "pending": {"worker_id": "w", "reserved_tokens": 500, "actual_usage": None},
            }})
        session.add(execution)

    with factory() as session, session.begin():
        coordinator = RunCoordinator(session)
        policy = coordinator.assert_execution_right(run_id, worker.worker_id)
        budget = coordinator.model_token_budget(run_id, policy)

    assert budget["legacy_usage"] is False and budget["unsettled"] == 1
    assert budget["used"] == 120 + 500
    assert budget["remaining"] == policy.budget.max_llm_tokens_per_run - 620

    with factory.begin() as session:
        session.add(AgentExecution(
            run_id=run_id, agent_role=AgentRole.PLANNER.value.lower(),
            status=AgentExecutionStatus.FAILED, input={}, output={},
            llm_usage={"prompt": "legacy usage without per-request records"}))
    with factory() as session, session.begin():
        coordinator = RunCoordinator(session)
        policy = coordinator.assert_execution_right(run_id, worker.worker_id)
        legacy = coordinator.model_token_budget(run_id, policy)

    # Legacy usage cannot be charged reliably, so the remaining amount is unknown, not zero.
    assert legacy["legacy_usage"] is True and legacy["remaining"] is None
