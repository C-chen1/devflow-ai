"""Worker execution helpers and the single-claim CLI."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Literal, TypeVar
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.artifacts import ToolArtifactResult, write_artifact_file
from devflow_ai.baseline import BaselineUnavailableError, validate_coder_baseline, validate_recorded_baseline
from devflow_ai.coder import CoderBlocked, CoderDraft, CoderReadRequest, CoderOutput, PatchReady, build_coder_prompt
from devflow_ai.config import HeartbeatConfig, LeaseConfig
from devflow_ai.context import ContextBuilder, MEMORY_RECALL_CONTEXT_KEY
from devflow_ai.coordinator import (
    HandoffContext, HandoffDecision, ReadFileReceipt, RunCoordinator, RunExecutionDeniedError,
    LEASE_EXPIRED_SIGNATURE, RecoveryDecisionUnavailableError, RunLeaseNotExpiredError,
    RunVersionConflictError,
)
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.execution_binding import verify_optional_binding
from devflow_ai.enums import AgentExecutionStatus, AgentRole, FailureCategory, RunStatus, ToolCallStatus, VerificationResult
from devflow_ai.explorer import ExplorerResult, explore_one_file
from devflow_ai.harness import GitExecutionCancelledError
from devflow_ai.heartbeat import RunHeartbeat
from devflow_ai.model_gateway import ModelGateway, PlannerModelGateway
from devflow_ai.memory import MemoryRecallRequest, MemoryService, RepositoryScope
from devflow_ai.orchestration import CommittedHandoff, HandoffRoutingError, run_handoff_cycle
from devflow_ai.models import AgentExecution, Artifact, FailureRecord, Handoff, Run, Task, ToolCall, Verification
from devflow_ai.planner import NeedsEvidence, PlannerOutput, PlannerRawOutput, PlannerRepairRequest
from devflow_ai.policy import PolicyConfig
from devflow_ai.services import TaskStateConflictError
from devflow_ai.supervisor import (
    InitialBlockedDecision, InitialExploreDecision, InitialSupervisorDecision, build_initial_supervisor_prompt,
    ExplorerBlockedDecision, ExplorerRouteDecision, PostExplorerDecision, build_post_explorer_supervisor_prompt,
    PlannerBlockedDecision, PlannerRouteDecision, PostPlannerDecision, build_post_planner_supervisor_prompt,
    CoderBlockedDecision, CoderRouteDecision, PostCoderDecision, build_post_coder_supervisor_prompt,
    VerificationFailureProposal, resolve_verification_failure_evidence, build_verification_failure_prompt,
)
from devflow_ai.verifier import (
    SemanticReviewDraft, TestComparison, VerificationEvidenceCoverage, VerifierEvidenceSelection, build_verifier_semantic_context,
    load_verifier_command_evidence, load_verifier_workspace_diff, load_pytest_report_artifact,
    VerificationEvidenceError, run_semantic_review, validate_semantic_review_citations,
    preliminary_public_outcome, build_failure_observations,
)
from devflow_ai.workspace_lock import workspace_operation_lock
from devflow_ai.verification_config import (
    VerificationConfig, copied_public_tests_sha256, public_check_command, public_test_modules,
    public_tests_copy_root, public_tests_sha256,
)
from devflow_ai.independent_evaluation import IndependentEvaluationMaterial, PrivateCase
from devflow_ai.verifier import load_independent_evaluation
import json
import sys
import argparse


RunResult = TypeVar("RunResult")
# Keep this distinct from the default workspace lock namespace: rediagnosis
# dispatch holds it while the V1 evaluation/finalize paths take that same Run's
# default-namespace workspace lock.
_RECOVERY_DISPATCH_LOCK_NAMESPACE = 0x5EED5EED5EED5EED
_REDIAGNOSIS_MAX_SUPERVISOR_STEPS = 3


class RunClaim(BaseModel):
    """A detached receipt of a committed claim, not a live execution-right check."""

    model_config = ConfigDict(frozen=True)

    task_id: UUID
    run_id: UUID
    attempt_number: int
    status: RunStatus
    worker_id: str
    creation_idempotency_key: str
    lease_expires_at: datetime


class PlannerContextUnavailableError(LookupError):
    """The selected Explorer execution cannot supply Planner context for this Run."""


class PlannerModelBudgetUnavailableError(RuntimeError):
    """Run budget could not admit the initial Planner model request."""


class CoderContextUnavailableError(LookupError):
    """The Coder cannot obtain an approved plan for this Run."""


class InitialSupervisorContextError(ValueError):
    """The Run no longer permits an initial Supervisor decision."""


class ExplorerSupervisorContextError(ValueError):
    """A committed Explorer result is unavailable for this Supervisor decision."""


class PlannerSupervisorContextError(ValueError):
    """A committed Planner result or its source evidence is unavailable."""


class CoderSupervisorContextError(ValueError):
    """A committed Coder result or its evidence is unavailable."""


class LinkedStopEvent:
    """Report a stop when either of two events is set.

    Used so a caller's deadline reaches the workspace initialization, which only takes
    one stop event. Waiting polls both because the standard library cannot wait on two
    events at once; the poll interval matches the lock helpers' own granularity.
    """

    def __init__(self, primary: Event, secondary: Event | None = None):
        self._primary = primary
        self._secondary = secondary

    def is_set(self) -> bool:
        return self._primary.is_set() or (
            self._secondary is not None and self._secondary.is_set())

    def set(self) -> None:
        self._primary.set()

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is None:
            while not self.is_set():
                self._primary.wait(0.1)
            return True
        remaining = timeout
        while remaining > 0:
            if self.is_set():
                return True
            step = 0.1 if remaining > 0.1 else remaining
            self._primary.wait(step)
            remaining -= step
        return self.is_set()


@dataclass(frozen=True)
class RunDirectories:
    workspace_root: Path
    artifact_root: Path


class VerifierSemanticResult(BaseModel):
    """Persisted intermediate review, not a Verification or Run verdict."""

    model_config = ConfigDict(frozen=True)

    comparison: TestComparison
    review: SemanticReviewDraft
    diff_call_id: UUID
    read_call_ids: tuple[UUID, ...]
    draft_artifact_id: UUID
    evidence_coverage: VerificationEvidenceCoverage


class InitialSupervisorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: UUID
    outcome: Literal["EXPLORE", "BLOCKED"]
    handoff_id: UUID | None


class ExplorerSupervisorResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    execution_id: UUID
    outcome: Literal["PLAN", "RE_EXPLORE", "BLOCKED"]
    handoff_id: UUID | None


class PlannerSupervisorResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    execution_id: UUID
    outcome: Literal["CODE", "RE_EXPLORE", "BASELINE", "BLOCKED"]
    handoff_id: UUID | None


class CoderSupervisorResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    execution_id: UUID
    outcome: Literal["VERIFY", "REPLAN", "BLOCKED"]
    handoff_id: UUID | None


def resolve_run_directories(
    run_id: UUID, *, workspaces_root: Path, artifacts_root: Path,
) -> RunDirectories:
    """Resolve the Run's directory layout without creating either directory."""
    workspaces = workspaces_root.resolve()
    artifacts = artifacts_root.resolve()
    if workspaces.is_relative_to(artifacts) or artifacts.is_relative_to(workspaces):
        raise ValueError("Workspace and Artifact roots must be separate")

    workspace_root = workspaces / str(run_id)
    artifact_root = artifacts / str(run_id)
    for path in (workspace_root, artifact_root):
        # Check resolved spelling too, including case-sensitive Windows directories.
        if path.resolve().parts != path.parts:
            raise ValueError("Run directories must not redirect to another path")

    return RunDirectories(workspace_root=workspace_root, artifact_root=artifact_root)


def _create_run_directories(directories: RunDirectories) -> None:
    directories.workspace_root.mkdir(exist_ok=False)
    try:
        directories.artifact_root.mkdir(exist_ok=False)
    except BaseException as error:
        try:
            directories.workspace_root.rmdir()
        except OSError as cleanup_error:
            error.add_note(f"Empty workspace cleanup failed: {cleanup_error}")
        raise


def _validate_worker_id(worker_id: str) -> str:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-blank string")
    return worker_id


def _has_reviewable_read(entries) -> bool:
    """True when a committed read actually produced content.

    The Supervisor receives references and char counts, not the bodies, so this asks
    "did a read produce anything" rather than "is a body in this prompt".
    """
    return any((entry.get("preview_chars") or 0) > 0 for entry in entries)


def _require_run_not_stopped(run_id: UUID, stop_event: Event) -> None:
    if stop_event.is_set():
        raise RunExecutionDeniedError(f"Run {run_id} has been asked to stop")


class Worker:
    def __init__(
        self, session_factory: sessionmaker[Session], worker_id: str,
        lease_config: LeaseConfig | None = None,
    ):
        self.session_factory = session_factory
        self.worker_id = _validate_worker_id(worker_id)
        self.lease_config = lease_config if lease_config is not None else LeaseConfig()

    def claim_once(self) -> RunClaim | None:
        with self.session_factory() as session:
            run = RunCoordinator(session, self.lease_config).claim_next_initial_run(self.worker_id)
            if run is None:
                return None
            # Commit already succeeded. Copy values while ORM refreshes can still use Session.
            return RunClaim(
                task_id=run.task_id,
                run_id=run.id,
                attempt_number=run.attempt_number,
                status=run.status,
                worker_id=run.worker_id,
                creation_idempotency_key=run.creation_idempotency_key,
                lease_expires_at=run.lease_expires_at.astimezone(timezone.utc),
            )

    def _require_run_execution_version(self, run_id: UUID, verified: str | None) -> None:
        """Refuse to execute a Run under a version other than the one it recorded.

        Read-only and before any workspace or Agent work. A Run claimed without a pinned
        manifest keeps the V1 behaviour; a Run claimed with one is executable only by a caller
        that just verified the same bytes.
        """
        with self.session_factory() as session:
            pinned = session.scalar(select(Run.execution_sha256).where(Run.id == run_id))
        if pinned is None:
            return
        if verified != pinned:
            raise RunVersionConflictError(
                "This Run was started under a different execution version; refusing to execute it")

    def execute_initial_supervisor(
        self, run_id: UUID, *, model_gateway: ModelGateway, stop_event: Event,
    ) -> InitialSupervisorResult:
        """Decide the first Explorer route for an already admitted Task."""
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            has_expert = session.scalar(select(exists().where(
                AgentExecution.run_id == run_id, AgentExecution.agent_role != AgentRole.SUPERVISOR.value.lower(),
            )))
            has_handoff = session.scalar(select(exists().where(
                Handoff.from_agent_execution_id == AgentExecution.id, AgentExecution.run_id == run_id,
            )))
            if has_expert or has_handoff:
                raise InitialSupervisorContextError("Run already has an expert execution or handoff")
            task_id, repo = session.execute(
                select(Task.id, Task.repo).join(Run, Run.task_id == Task.id).where(Run.id == run_id),
            ).one()
            graph_steps = session.scalar(select(func.count()).select_from(AgentExecution).where(AgentExecution.run_id == run_id))
            tool_calls = session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id))
            remaining_steps = max(0, policy.budget.max_graph_steps_per_run - graph_steps)
            remaining_tools = max(0, policy.budget.max_tool_calls_per_run - tool_calls)
            explorer_tools = policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER]
            agent_input = {
                **ContextBuilder(session).build_issue_context(run_id), "repo": repo,
                "remaining_graph_steps": remaining_steps, "remaining_tool_calls": remaining_tools,
                "explorer_allowed_tools": list(explorer_tools),
            }
            retry_context = ContextBuilder(session).build_retry_context(run_id)
            if retry_context is not None:
                agent_input["retry_context"] = retry_context
            max_repairs = policy.budget.max_output_repairs_per_agent_execution
            token_budget = policy.budget.max_llm_tokens_per_run

        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.SUPERVISOR, agent_input=agent_input,
            )

        repairs = 0
        failure_reason = "SUPERVISOR_EXECUTION_ERROR"
        try:
            if remaining_steps < 2:
                decision = InitialBlockedDecision(
                    outcome="BLOCKED", reason="Not enough graph steps remain for Explorer",
                    decision_required="Review the Run budget",
                )
            elif remaining_tools == 0 or "read_file" not in explorer_tools:
                decision = InitialBlockedDecision(
                    outcome="BLOCKED", reason="Explorer cannot read the repository under the current policy",
                    decision_required="Review the Task policy and available budget",
                )
            elif token_budget == 0:
                decision = InitialBlockedDecision(
                    outcome="BLOCKED", reason="No model token budget is available",
                    decision_required="Review the Run budget",
                )
            else:
                repair_note = ""
                while True:
                    _require_run_not_stopped(run_id, stop_event)
                    raw = model_gateway.request(
                        run_id, agent_execution_id=execution_id,
                        prompt=build_initial_supervisor_prompt(agent_input) + repair_note,
                    )
                    if raw is None:
                        decision = InitialBlockedDecision(
                            outcome="BLOCKED", reason="The model request could not be admitted within budget",
                            decision_required="Review remaining model budget",
                        )
                        break
                    try:
                        decision = (
                            InitialSupervisorDecision.model_validate_json(raw).root
                            if isinstance(raw, str) else InitialSupervisorDecision.model_validate(raw).root
                        )
                        break
                    except ValidationError:
                        failure_reason = "SUPERVISOR_OUTPUT_INVALID"
                        if repairs >= max_repairs:
                            raise
                        repairs += 1
                        repair_note = "\nThe previous output did not match the schema. Return one corrected JSON object."
            failure_reason = "SUPERVISOR_EXECUTION_ERROR"
            handoff = None
            if isinstance(decision, InitialExploreDecision):
                handoff = HandoffDecision(
                    from_agent_execution_id=execution_id, to_agent_role=AgentRole.EXPLORER,
                    context=HandoffContext(
                        completed_work="Reviewed the admitted Issue and Run context.", blocker=None,
                        evidence_refs=({"task_id": str(task_id)},), decision_required=decision.exploration_focus,
                        scope_impact="Exploration remains within the authorized workspace and policy.",
                    ),
                )
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                handoff_id = RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id,
                    status=AgentExecutionStatus.SUCCEEDED, output=decision.model_dump(mode="json"),
                    llm_usage={"output_repair_attempts": repairs} if repairs else None, approved_handoff=handoff,
                )
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(
                        run_id, self.worker_id, agent_execution_id=execution_id,
                        status=AgentExecutionStatus.FAILED, output={"reason": failure_reason},
                        llm_usage={"output_repair_attempts": repairs} if repairs else None,
                    )
            except Exception as record_error:
                error.add_note(f"Supervisor failure could not be committed: {type(record_error).__name__}")
            raise
        return InitialSupervisorResult(execution_id=execution_id, outcome=decision.outcome, handoff_id=handoff_id)

    def execute_supervisor_after_explorer(
        self, run_id: UUID, *, explorer_execution_id: UUID, model_gateway: ModelGateway, stop_event: Event,
    ) -> ExplorerSupervisorResult:
        """Route one committed Explorer result without inventing professional findings."""
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            agent_input = ContextBuilder(session).build_supervisor_exploration(
                run_id, explorer_execution_id=explorer_execution_id,
            )
            if agent_input is None:
                raise ExplorerSupervisorContextError("Committed Explorer result is unavailable")
            steps = session.scalar(select(func.count()).select_from(AgentExecution).where(AgentExecution.run_id == run_id))
            remaining_steps = max(0, policy.budget.max_graph_steps_per_run - steps)
            tool_calls = session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id))
            remaining_tools = max(0, policy.budget.max_tool_calls_per_run - tool_calls)
            explorer_allowed = "read_file" in policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER]
            repair_limit = policy.budget.max_output_repairs_per_agent_execution
            budget = RunCoordinator(session, self.lease_config).model_token_budget(run_id, policy)
            agent_input = {**agent_input, "remaining_graph_steps": remaining_steps,
                           "remaining_tool_calls": remaining_tools, "explorer_read_allowed": explorer_allowed,
                           # Routing on "is there budget for another exploration" needs the number.
                           "remaining_model_tokens": budget["remaining"]}
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.SUPERVISOR, agent_input=agent_input,
            )
        repairs = 0
        failure_reason = "SUPERVISOR_EXECUTION_ERROR"
        try:
            evidence = agent_input["read_evidence"]
            if remaining_steps < 2:
                decision = ExplorerBlockedDecision(outcome="BLOCKED", reason="Not enough graph steps remain",
                                                   decision_required="Review the Run budget")
            elif not _has_reviewable_read(evidence):
                decision = ExplorerBlockedDecision(outcome="BLOCKED", reason="No committed read preview is available",
                                                   decision_required="Obtain reviewable exploration evidence")
            else:
                repair_note = ""
                while True:
                    _require_run_not_stopped(run_id, stop_event)
                    raw = model_gateway.request(run_id, agent_execution_id=execution_id,
                        prompt=build_post_explorer_supervisor_prompt(agent_input) + repair_note)
                    if raw is None:
                        decision = ExplorerBlockedDecision(outcome="BLOCKED", reason="Model budget unavailable",
                                                           decision_required="Review remaining model budget")
                        break
                    try:
                        decision = (PostExplorerDecision.model_validate_json(raw).root if isinstance(raw, str)
                                    else PostExplorerDecision.model_validate(raw).root)
                        break
                    except ValidationError:
                        failure_reason = "SUPERVISOR_OUTPUT_INVALID"
                        if repairs >= repair_limit:
                            raise
                        repairs += 1
                        repair_note = "\nThe previous output did not match the schema. Return one corrected JSON object."
            failure_reason = "SUPERVISOR_EXECUTION_ERROR"
            if isinstance(decision, ExplorerRouteDecision) and decision.outcome == "RE_EXPLORE" and (
                not explorer_allowed or remaining_tools == 0
            ):
                decision = ExplorerBlockedDecision(outcome="BLOCKED", reason="Explorer cannot read under current policy",
                                                   decision_required="Review permissions and tool budget")
            handoff = None
            if isinstance(decision, ExplorerRouteDecision):
                role = AgentRole.PLANNER if decision.outcome == "PLAN" else AgentRole.EXPLORER
                handoff = HandoffDecision(from_agent_execution_id=explorer_execution_id, to_agent_role=role,
                    context=HandoffContext(completed_work="Explorer submitted a committed investigation result.",
                        blocker=None if decision.outcome == "PLAN" else decision.reason,
                        evidence_refs=({"explorer_execution_id": str(explorer_execution_id)},),
                        decision_required=decision.next_goal,
                        scope_impact="Continue within the Issue and Task policy."))
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                handoff_id = RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id, status=AgentExecutionStatus.SUCCEEDED,
                    output=decision.model_dump(mode="json"),
                    llm_usage={"output_repair_attempts": repairs} if repairs else None, approved_handoff=handoff,
                )
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(
                        run_id, self.worker_id, agent_execution_id=execution_id, status=AgentExecutionStatus.FAILED,
                        output={"reason": failure_reason}, llm_usage={"output_repair_attempts": repairs} if repairs else None,
                    )
            except Exception as record_error:
                error.add_note(f"Supervisor failure could not be committed: {type(record_error).__name__}")
            raise
        return ExplorerSupervisorResult(execution_id=execution_id, outcome=decision.outcome, handoff_id=handoff_id)

    def execute_supervisor_after_planner(
        self, run_id: UUID, *, planner_execution_id: UUID, model_gateway: ModelGateway, stop_event: Event,
    ) -> PlannerSupervisorResult:
        """Route a committed Planner result using its original Explorer evidence."""
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            planner = session.scalar(select(AgentExecution).where(
                AgentExecution.id == planner_execution_id, AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            ))
            if planner is None:
                raise PlannerSupervisorContextError("Committed Planner result is unavailable")
            try:
                planner_output = PlannerOutput.model_validate(planner.output).root
                explorer_id = UUID(planner.input["explorer_execution_id"])
            except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise PlannerSupervisorContextError("Planner result or Explorer reference is invalid") from error
            exploration = ContextBuilder(session).build_supervisor_exploration(run_id, explorer_execution_id=explorer_id)
            if exploration is None:
                raise PlannerSupervisorContextError("Planner's Explorer evidence is unavailable")
            steps = session.scalar(select(func.count()).select_from(AgentExecution).where(AgentExecution.run_id == run_id))
            tool_calls = session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id))
            remaining_steps = max(0, policy.budget.max_graph_steps_per_run - steps)
            remaining_tools = max(0, policy.budget.max_tool_calls_per_run - tool_calls)
            coder_allowed = "apply_patch" in policy.permissions.allowed_tools_by_role[AgentRole.CODER]
            explorer_allowed = "read_file" in policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER]
            verifier_allowed = "run_command" in policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER]
            baseline_ids = session.scalars(select(AgentExecution.id).join(Handoff, AgentExecution.handoff_id == Handoff.id).where(
                AgentExecution.run_id == run_id, AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                AgentExecution.output["outcome"].as_string() == "BASELINE_RECORDED",
                Handoff.from_agent_execution_id == planner_execution_id,
                Handoff.to_agent_role == AgentRole.VERIFIER.value.lower(),
            ).order_by(AgentExecution.created_at.desc()).limit(2)).all()
            if len(baseline_ids) > 1:
                raise PlannerSupervisorContextError("Multiple recorded baselines make the phase ambiguous")
            baseline_id = baseline_ids[0] if baseline_ids else None
            baseline_available = baseline_id is not None
            repair_limit = policy.budget.max_output_repairs_per_agent_execution
            budget = RunCoordinator(session, self.lease_config).model_token_budget(run_id, policy)
            agent_input = {**exploration, "planner_execution_id": str(planner_execution_id),
                "planner_output": planner_output.model_dump(mode="json"), "remaining_graph_steps": remaining_steps,
                "remaining_tool_calls": remaining_tools, "coder_patch_allowed": coder_allowed,
                "explorer_read_allowed": explorer_allowed, "verifier_command_allowed": verifier_allowed,
                "baseline_available": baseline_available,
                "observed_baseline_verifier_id": str(baseline_id) if baseline_id is not None else None,
                "remaining_model_tokens": budget["remaining"]}
            _require_run_not_stopped(run_id, stop_event)
            execution_id = RunCoordinator(session, self.lease_config)._start_agent_execution_in_transaction(
                run_id, self.worker_id, role=AgentRole.SUPERVISOR, agent_input=agent_input)
        repairs = 0
        failure_reason = "SUPERVISOR_EXECUTION_ERROR"
        try:
            if remaining_steps < 2:
                decision = PlannerBlockedDecision(outcome="BLOCKED", reason="Not enough graph steps remain",
                                                   decision_required="Review the Run budget")
            else:
                repair_note = ""
                while True:
                    _require_run_not_stopped(run_id, stop_event)
                    raw = model_gateway.request(run_id, agent_execution_id=execution_id,
                        prompt=build_post_planner_supervisor_prompt(agent_input) + repair_note)
                    if raw is None:
                        decision = PlannerBlockedDecision(outcome="BLOCKED", reason="Model budget unavailable",
                                                           decision_required="Review remaining model budget")
                        break
                    try:
                        decision = (PostPlannerDecision.model_validate_json(raw).root if isinstance(raw, str)
                                    else PostPlannerDecision.model_validate(raw).root)
                        break
                    except ValidationError:
                        failure_reason = "SUPERVISOR_OUTPUT_INVALID"
                        if repairs >= repair_limit:
                            raise
                        repairs += 1
                        repair_note = "\nThe previous output did not match the schema. Return one corrected JSON object."
            failure_reason = "SUPERVISOR_EXECUTION_ERROR"
            if isinstance(decision, PlannerRouteDecision):
                if decision.outcome in ("CODE", "BASELINE") and isinstance(planner_output, NeedsEvidence):
                    decision = PlannerBlockedDecision(outcome="BLOCKED", reason="Planner requires additional evidence",
                                                       decision_required="Resolve the Planner evidence gap")
                elif decision.outcome == "CODE" and not _has_reviewable_read(
                        agent_input["read_evidence"]):
                    decision = PlannerBlockedDecision(outcome="BLOCKED", reason="No committed read preview is available",
                                                       decision_required="Obtain reviewable exploration evidence")
                elif decision.outcome == "CODE" and (not coder_allowed or remaining_tools == 0):
                    decision = PlannerBlockedDecision(outcome="BLOCKED", reason="Coder cannot apply a patch under current policy",
                                                       decision_required="Review permissions and tool budget")
                elif decision.outcome == "CODE" and not baseline_available:
                    decision = PlannerRouteDecision(outcome="BASELINE", reason="A baseline is required before modification",
                                                    next_goal="Run the trusted registered baseline test")
                elif decision.outcome == "RE_EXPLORE" and (not explorer_allowed or remaining_tools == 0):
                    decision = PlannerBlockedDecision(outcome="BLOCKED", reason="Explorer cannot read under current policy",
                                                       decision_required="Review permissions and tool budget")
                if isinstance(decision, PlannerRouteDecision) and decision.outcome == "BASELINE" and (
                    not verifier_allowed or remaining_tools == 0
                ):
                    decision = PlannerBlockedDecision(outcome="BLOCKED", reason="Verifier cannot run the baseline under current policy",
                                                       decision_required="Review permissions and tool budget")
            handoff = None
            if isinstance(decision, PlannerRouteDecision):
                role = (AgentRole.CODER if decision.outcome == "CODE" else AgentRole.VERIFIER
                        if decision.outcome == "BASELINE" else AgentRole.EXPLORER)
                references = [{"explorer_execution_id": str(explorer_id)}]
                if decision.outcome == "CODE" and baseline_id is not None:
                    references.append({"baseline_verifier_id": str(baseline_id)})
                handoff = HandoffDecision(from_agent_execution_id=planner_execution_id, to_agent_role=role,
                    context=HandoffContext(completed_work="Planner submitted a committed result.",
                        blocker=decision.reason if decision.outcome == "RE_EXPLORE" else None,
                        evidence_refs=tuple(references),
                        decision_required=decision.next_goal, scope_impact="Continue within the Issue and Task policy."))
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                handoff_id = RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id, status=AgentExecutionStatus.SUCCEEDED,
                    output=decision.model_dump(mode="json"),
                    llm_usage={"output_repair_attempts": repairs} if repairs else None, approved_handoff=handoff)
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(
                        run_id, self.worker_id, agent_execution_id=execution_id, status=AgentExecutionStatus.FAILED,
                        output={"reason": failure_reason}, llm_usage={"output_repair_attempts": repairs} if repairs else None)
            except Exception as record_error:
                error.add_note(f"Supervisor failure could not be committed: {type(record_error).__name__}")
            raise
        return PlannerSupervisorResult(execution_id=execution_id, outcome=decision.outcome, handoff_id=handoff_id)

    def execute_supervisor_after_coder(
        self, run_id: UUID, *, coder_execution_id: UUID, directories: RunDirectories,
        model_gateway: ModelGateway, max_patch_bytes: int, stop_event: Event,
    ) -> CoderSupervisorResult:
        """Propose one committed route from a Coder result; never issue an acceptance verdict."""
        if max_patch_bytes <= 0:
            raise ValueError("max_patch_bytes must be positive")
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            coder = session.scalar(select(AgentExecution).where(
                AgentExecution.id == coder_execution_id, AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.CODER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            ))
            if coder is None or coder.handoff_id is None:
                raise CoderSupervisorContextError("Committed Coder result is unavailable")
            try:
                output = CoderOutput.model_validate(coder.output).root
                coder_input = ContextBuilder(session).build_coder_input(run_id, handoff_id=coder.handoff_id)
                if coder_input is None:
                    raise ValueError("Missing approved plan")
                planner_id = UUID(coder_input["planner_execution_id"])
                planner = session.get(AgentExecution, planner_id)
                explorer_id = UUID(planner.input["explorer_execution_id"])
            except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise CoderSupervisorContextError("Coder result or approved plan is invalid") from error
            refs = [{"explorer_execution_id": str(explorer_id)}]
            patch_evidence = None
            if isinstance(output, CoderBlocked):
                refs.extend(output.evidence_refs)
            if isinstance(output, PatchReady):
                calls = session.scalars(select(ToolCall.id).where(
                    ToolCall.run_id == run_id, ToolCall.agent_execution_id == coder_execution_id,
                    ToolCall.tool_name == "apply_patch", ToolCall.status == ToolCallStatus.SUCCEEDED,
                )).all()
                if len(calls) != 1:
                    raise CoderSupervisorContextError("A unique applied patch is required")
                try:
                    patch_evidence = ContextBuilder(session).build_verifier_patch_input(
                        run_id, coder_execution_id=coder_execution_id, patch_call_id=calls[0],
                        artifact_root=directories.artifact_root, max_patch_bytes=max_patch_bytes,
                    )
                except ValueError as error:
                    raise CoderSupervisorContextError("Applied patch evidence is invalid") from error
                baseline_refs = [ref for ref in coder_input["handoff"].get("evidence_refs", [])
                                 if isinstance(ref, dict) and "baseline_verifier_id" in ref]
                if len(baseline_refs) != 1:
                    raise CoderSupervisorContextError("Baseline reference is unavailable")
                refs.extend([baseline_refs[0], {"patch_tool_call_id": str(calls[0])},
                             {"patch_artifact_id": str(output.patch_artifact_id)}])
            steps = session.scalar(select(func.count()).select_from(AgentExecution).where(AgentExecution.run_id == run_id))
            tools_used = session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id))
            remaining_steps = max(0, policy.budget.max_graph_steps_per_run - steps)
            remaining_tools = max(0, policy.budget.max_tool_calls_per_run - tools_used)
            can_verify = "run_command" in policy.permissions.allowed_tools_by_role[AgentRole.VERIFIER] and remaining_tools > 0
            repair_limit = policy.budget.max_output_repairs_per_agent_execution
            agent_input = {"coder_execution_id": str(coder_execution_id), "coder_output": output.model_dump(mode="json"),
                           "patch_evidence": patch_evidence, "remaining_graph_steps": remaining_steps,
                           "remaining_tool_calls": remaining_tools, "verifier_command_allowed": can_verify}
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.SUPERVISOR, agent_input=agent_input)
        repairs = 0
        try:
            if remaining_steps < 2:
                decision = CoderBlockedDecision(outcome="BLOCKED", reason="Not enough graph steps remain",
                                                decision_required="Review the Run budget")
            else:
                repair_note = ""
                while True:
                    _require_run_not_stopped(run_id, stop_event)
                    raw = model_gateway.request(run_id, agent_execution_id=execution_id,
                        prompt=build_post_coder_supervisor_prompt(agent_input) + repair_note)
                    if raw is None:
                        decision = CoderBlockedDecision(outcome="BLOCKED", reason="Model budget unavailable",
                                                        decision_required="Review remaining model budget")
                        break
                    try:
                        decision = (PostCoderDecision.model_validate_json(raw).root if isinstance(raw, str)
                                    else PostCoderDecision.model_validate(raw).root)
                        break
                    except ValidationError:
                        if repairs >= repair_limit:
                            raise
                        repairs += 1
                        repair_note = "\nThe previous output did not match the schema. Return one corrected JSON object."
            if isinstance(decision, CoderRouteDecision) and decision.outcome == "VERIFY" and (
                not isinstance(output, PatchReady) or not can_verify
            ):
                decision = CoderBlockedDecision(outcome="BLOCKED", reason="A verifiable patch or permission is unavailable",
                                                decision_required="Review the Coder result and policy")
            handoff = None
            if isinstance(decision, CoderRouteDecision):
                handoff = HandoffDecision(from_agent_execution_id=coder_execution_id,
                    to_agent_role=AgentRole.VERIFIER if decision.outcome == "VERIFY" else AgentRole.PLANNER,
                    context=HandoffContext(completed_work="Coder submitted a committed result.",
                        blocker=output.blocker if isinstance(output, CoderBlocked) else None,
                        evidence_refs=tuple(refs), decision_required=decision.next_goal,
                        scope_impact="Continue within the Issue and Task policy."))
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                handoff_id = RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id, status=AgentExecutionStatus.SUCCEEDED,
                    output=decision.model_dump(mode="json"),
                    llm_usage={"output_repair_attempts": repairs} if repairs else None, approved_handoff=handoff)
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(
                        run_id, self.worker_id, agent_execution_id=execution_id, status=AgentExecutionStatus.FAILED,
                        output={"reason": "SUPERVISOR_EXECUTION_ERROR"}, llm_usage=None)
            except Exception as record_error:
                error.add_note(f"Supervisor failure could not be committed: {type(record_error).__name__}")
            raise
        return CoderSupervisorResult(execution_id=execution_id, outcome=decision.outcome, handoff_id=handoff_id)

    def build_post_coder_supervise(
        self, run_id: UUID, *, coder_execution_id: UUID, directories: RunDirectories,
        model_gateway: ModelGateway, max_patch_bytes: int, stop_event: Event,
    ) -> Callable[[UUID], UUID | None]:
        def supervise(selected_run_id: UUID) -> UUID | None:
            if selected_run_id != run_id:
                raise CoderSupervisorContextError("Supervisor callback is bound to another Run")
            result = self.execute_supervisor_after_coder(run_id, coder_execution_id=coder_execution_id,
                directories=directories, model_gateway=model_gateway, max_patch_bytes=max_patch_bytes, stop_event=stop_event)
            return result.execution_id if result.handoff_id is not None else None
        return supervise

    def build_post_planner_supervise(
        self, run_id: UUID, *, planner_execution_id: UUID, model_gateway: ModelGateway, stop_event: Event,
    ) -> Callable[[UUID], UUID | None]:
        def supervise(selected_run_id: UUID) -> UUID | None:
            if selected_run_id != run_id:
                raise PlannerSupervisorContextError("Supervisor callback is bound to another Run")
            result = self.execute_supervisor_after_planner(run_id, planner_execution_id=planner_execution_id,
                                                           model_gateway=model_gateway, stop_event=stop_event)
            return result.execution_id if result.handoff_id is not None else None
        return supervise

    def build_post_explorer_supervise(
        self, run_id: UUID, *, explorer_execution_id: UUID, model_gateway: ModelGateway, stop_event: Event,
    ) -> Callable[[UUID], UUID | None]:
        def supervise(selected_run_id: UUID) -> UUID | None:
            if selected_run_id != run_id:
                raise ExplorerSupervisorContextError("Supervisor callback is bound to another Run")
            result = self.execute_supervisor_after_explorer(run_id, explorer_execution_id=explorer_execution_id,
                                                            model_gateway=model_gateway, stop_event=stop_event)
            return result.execution_id if result.handoff_id is not None else None
        return supervise

    def build_initial_supervise(
        self, run_id: UUID, *, model_gateway: ModelGateway, stop_event: Event,
    ) -> Callable[[UUID], UUID | None]:
        """Bind the graph's initial Supervisor callback to one Run."""
        def supervise(selected_run_id: UUID) -> UUID | None:
            if selected_run_id != run_id:
                raise InitialSupervisorContextError("Supervisor callback is bound to another Run")
            result = self.execute_initial_supervisor(run_id, model_gateway=model_gateway, stop_event=stop_event)
            return result.execution_id if result.handoff_id is not None else None
        return supervise

    def _public_check_command(
        self, config: VerificationConfig, directories: RunDirectories,
    ) -> tuple[str, ...]:
        """Return the registered public-check command, materialising outside test files.

        Public tests may live in a task pack rather than inside the checkout (the Click
        scenarios are one example). Copying them into the checkout would break the
        baseline's workspace integrity check, so they are copied into the run's artifact
        directory instead and the command is pointed at that copy with an explicit
        rootdir. Pytest reports node names relative to that rootdir, so identifiers stay
        stable, and no configuration from above the copy is loaded.

        The command itself is derived by the shared helper that formal acceptance also
        uses, so what runs and what is checked cannot drift apart.
        """
        modules = public_test_modules(config)
        if not modules:
            return config.command
        target = public_tests_copy_root(directories.artifact_root)
        target.mkdir(parents=True, exist_ok=True)
        for name, data in modules.items():
            (target / name).write_bytes(data)
        return public_check_command(config, artifact_root=directories.artifact_root, modules=modules)

    def build_handoff_receivers(
        self, run_id: UUID, *, directories: RunDirectories, max_bytes: int, stop_event: Event,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        baseline_registry: Mapping[str, tuple[str, ...]] | None = None,
        baseline_command_name: str | None = None, baseline_pythonpath_relative: str | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_target_tests: tuple[str, ...] = (),
        verifier_max_diff_bytes: int | None = None,
        verification_config: VerificationConfig | None = None,
        memory_recall: MemoryRecallRequest | None = None,
    ) -> dict[AgentRole, Callable[[CommittedHandoff], None]]:
        """Bind committed handoffs to the existing Explorer and Planner entry points."""
        public_tests_digest: dict[str, str] | None = None
        if verification_config is not None:
            if baseline_registry is not None or baseline_command_name is not None or baseline_pythonpath_relative is not None or verifier_target_tests:
                raise ValueError("Verification config cannot be combined with individual test settings")
            config = VerificationConfig.model_validate(verification_config.model_dump())
            with self.session_factory() as session, session.begin():
                RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
                task = session.scalar(select(Task).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
                if task is None or task.id != config.task_id or task.base_commit != config.base_commit:
                    raise ValueError("Verification config does not match the Run Task and base commit")
            baseline_registry = {
                config.command_name: self._public_check_command(config, directories)}
            baseline_command_name = config.command_name
            baseline_pythonpath_relative = config.pythonpath_relative
            verifier_target_tests = config.target_tests
            # Digest the copy that will actually run, so acceptance can compare it with
            # the trusted pack instead of trusting a path.
            public_tests_digest = copied_public_tests_sha256(directories.artifact_root)
        pending_recall = memory_recall
        def receive_explorer(handoff: CommittedHandoff) -> None:
            nonlocal pending_recall
            with self.session_factory() as session:
                builder = ContextBuilder(session)
                agent_input = builder.build_issue_context(run_id)
                retry_context = builder.build_retry_context(run_id)
                if retry_context is not None:
                    agent_input["retry_context"] = retry_context
                # Which windows are already in hand, so a follow-up exploration asks for a
                # different range instead of re-reading the same head.
                already_read = builder.build_already_read(run_id)
                if already_read:
                    agent_input["already_read"] = already_read
            agent_input["handoff"] = handoff.context
            selected_recall, pending_recall = pending_recall, None
            self.execute_explorer(
                run_id, directories=directories, max_bytes=max_bytes, stop_event=stop_event,
                agent_input=agent_input, model_gateway=explorer_gateway, handoff_id=handoff.id,
                **({"memory_recall": selected_recall} if selected_recall is not None else {}),
            )

        def receive_planner(handoff: CommittedHandoff) -> None:
            explorer_id = handoff.from_agent_execution_id
            with self.session_factory() as session:
                source_role = session.scalar(select(AgentExecution.agent_role).where(
                    AgentExecution.id == handoff.from_agent_execution_id,
                    AgentExecution.run_id == run_id,
                ))
            if source_role == AgentRole.CODER.value.lower():
                references = [
                    ref["explorer_execution_id"]
                    for ref in handoff.context.get("evidence_refs", [])
                    if isinstance(ref, dict) and "explorer_execution_id" in ref
                ]
                if len(references) != 1:
                    raise PlannerContextUnavailableError("Replanning requires one Explorer execution reference")
                try:
                    explorer_id = UUID(references[0])
                except (ValueError, TypeError, AttributeError):
                    raise PlannerContextUnavailableError("Invalid Explorer execution reference") from None
            self.execute_planner(
                run_id, explorer_execution_id=explorer_id, stop_event=stop_event,
                model_gateway=planner_gateway, handoff_id=handoff.id,
            )

        receivers = {AgentRole.EXPLORER: receive_explorer, AgentRole.PLANNER: receive_planner}
        if baseline_registry is not None:
            if baseline_command_name is None or baseline_command_name not in baseline_registry:
                raise ValueError("A registered baseline command name is required")
            def receive_baseline(handoff: CommittedHandoff) -> None:
                with self.session_factory() as session:
                    source_role = session.scalar(select(AgentExecution.agent_role).where(
                        AgentExecution.id == handoff.from_agent_execution_id, AgentExecution.run_id == run_id,
                    ))
                if source_role == AgentRole.CODER.value.lower() and verifier_gateway is not None:
                    self.execute_post_patch_verifier(run_id, handoff_id=handoff.id, directories=directories,
                        command_name=baseline_command_name, registry=baseline_registry,
                        target_tests=verifier_target_tests, model_gateway=verifier_gateway,
                        max_read_bytes=max_bytes, max_diff_bytes=verifier_max_diff_bytes,
                        max_patch_bytes=max_patch_bytes, stop_event=stop_event,
                        pythonpath_relative=baseline_pythonpath_relative, verification_config=verification_config,
                        public_tests_digest=public_tests_digest)
                    return
                if source_role != AgentRole.PLANNER.value.lower():
                    raise ValueError("Baseline receiver requires a Planner handoff")
                self.execute_baseline_verifier(run_id, handoff_id=handoff.id, directories=directories,
                    command_name=baseline_command_name, registry=baseline_registry, stop_event=stop_event,
                    pythonpath_relative=baseline_pythonpath_relative, verify_baseline=verification_config is not None,
                    public_tests_digest=public_tests_digest)
            receivers[AgentRole.VERIFIER] = receive_baseline
        if coder_gateway is not None:
            if max_patch_bytes is None or max_patch_bytes <= 0:
                raise ValueError("Coder requires a positive max_patch_bytes")

            def receive_coder(handoff: CommittedHandoff) -> None:
                self.execute_coder(
                    run_id, handoff_id=handoff.id, directories=directories, model_gateway=coder_gateway,
                    max_patch_bytes=max_patch_bytes, max_read_bytes=max_bytes, stop_event=stop_event,
                )

            receivers[AgentRole.CODER] = receive_coder
        return receivers

    def execute_post_patch_verifier(
        self, run_id: UUID, *, handoff_id: UUID, directories: RunDirectories,
        command_name: str, registry: Mapping[str, tuple[str, ...]], target_tests: tuple[str, ...],
        model_gateway: ModelGateway, max_read_bytes: int, max_diff_bytes: int | None,
        max_patch_bytes: int | None, stop_event: Event, pythonpath_relative: str | None = None,
        verification_config: VerificationConfig | None = None,
        public_tests_digest: Mapping[str, str] | None = None,
    ) -> dict:
        """Collect post-patch evidence and report readiness without claiming final acceptance."""
        if (command_name not in registry or not target_tests or any(not name.strip() for name in target_tests)
                or max_diff_bytes is None or max_patch_bytes is None
                or min(max_read_bytes, max_diff_bytes, max_patch_bytes) <= 0):
            raise ValueError("Registered command, target tests and positive evidence limits are required")
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            handoff = session.get(Handoff, handoff_id)
            approval = session.scalar(select(AgentExecution.id).where(
                AgentExecution.run_id == run_id, AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                AgentExecution.output["approved_handoff_id"].as_string() == str(handoff_id)))
            coder = session.get(AgentExecution, handoff.from_agent_execution_id) if handoff else None
            if (approval is None or handoff is None or handoff.to_agent_role != AgentRole.VERIFIER.value.lower()
                    or coder is None or coder.run_id != run_id or coder.agent_role != AgentRole.CODER.value.lower()
                    or coder.status != AgentExecutionStatus.SUCCEEDED or coder.handoff_id is None):
                raise VerificationEvidenceError("Approved Coder handoff is required")
            refs = (handoff.context or {}).get("evidence_refs", [])
            def reference(key):
                values = [ref[key] for ref in refs if isinstance(ref, dict) and key in ref]
                if len(values) != 1:
                    raise VerificationEvidenceError(f"Exactly one {key} reference is required")
                try:
                    return UUID(str(values[0]))
                except (ValueError, TypeError, AttributeError):
                    raise VerificationEvidenceError(f"Invalid {key} reference") from None
            baseline_id, patch_id = reference("baseline_verifier_id"), reference("patch_tool_call_id")
            patch_artifact_id = reference("patch_artifact_id")
            coder_input = validate_coder_baseline(session, run_id, handoff_id=coder.handoff_id,
                artifact_root=directories.artifact_root, exclude_call_id=patch_id)
            baseline_refs = [ref.get("baseline_verifier_id") for ref in coder_input["handoff"].get("evidence_refs", [])
                             if isinstance(ref, dict) and "baseline_verifier_id" in ref]
            baseline = session.get(AgentExecution, baseline_id)
            patch = ContextBuilder(session).build_verifier_patch_input(run_id, coder_execution_id=coder.id,
                patch_call_id=patch_id, artifact_root=directories.artifact_root, max_patch_bytes=max_patch_bytes)
            if (baseline_refs != [str(baseline_id)] or baseline.input.get("command_name") != command_name
                    or str(patch["patch_artifact_id"]) != str(patch_artifact_id)):
                raise VerificationEvidenceError("Handoff evidence does not match the approved baseline and patch")
            baseline_call_id = UUID(baseline.output["tool_call_id"])
            baseline_artifact_id = UUID(baseline.output["report_artifact_id"])
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.VERIFIER,
                agent_input={"purpose": "post_patch_verification", "command_name": command_name}, handoff_id=handoff_id)
        try:
            from devflow_ai.skills import run_registered_check
            receipt = run_registered_check(version="2", worker=self, run_id=run_id,
                verifier_execution_id=execution_id, directories=directories, registry=registry,
                command_name=command_name, stop_event=stop_event, trusted_pytest_capture=True,
                trusted_pythonpath_relative=pythonpath_relative,
                trusted_public_tests_digest=public_tests_digest)["receipt"]
            reports = [item for item in receipt["artifacts"] if item["kind"] == "pytest_junit"]
            if len(reports) != 1:
                raise VerificationEvidenceError("Current pytest report is unavailable")
            from devflow_ai.skills import check_diff_scope, supports_diff_scope
            from devflow_ai.task_pack import admission_has_scope_digest, scope_evidence_sha256
            scope = verification_config.scope if verification_config is not None else None
            with self.session_factory() as session:
                admission = session.scalar(select(Task.admission_record).join(Run, Run.task_id == Task.id)
                                           .where(Run.id == run_id))
            scope_check = None
            if (scope is not None and supports_diff_scope(scope, max_diff_bytes)
                    and admission_has_scope_digest(admission, scope_evidence_sha256(scope))):
                scope_check = check_diff_scope(version="2", worker=self, run_id=run_id,
                    verifier_execution_id=execution_id, directories=directories,
                    trusted_scope=scope, max_diff_bytes=max_diff_bytes, stop_event=stop_event)
                diff = scope_check
            else:
                diff = self.build_verifier_diff(run_id, agent_execution_id=execution_id, directories=directories,
                    max_bytes=max_diff_bytes, stop_event=stop_event)()
            selection = VerifierEvidenceSelection(baseline_verifier_id=baseline_id, baseline_call_id=baseline_call_id,
                baseline_artifact_id=baseline_artifact_id, patch_call_id=patch_id,
                current_call_id=UUID(receipt["tool_call_id"]), current_artifact_id=UUID(reports[0]["id"]),
                diff_call_id=UUID(diff["tool_call_id"]), command_name=command_name, target_tests=target_tests)
            review = self.review_verifier_semantics(run_id, agent_execution_id=execution_id, directories=directories,
                selection=selection, model_gateway=model_gateway, max_read_bytes=max_read_bytes,
                max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes, stop_event=stop_event)
            with self.session_factory() as session:
                readiness = RunCoordinator(session, self.lease_config).assess_verification_readiness(
                    run_id, self.worker_id, agent_execution_id=execution_id, draft_artifact_id=review.draft_artifact_id,
                    artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes,
                    max_patch_bytes=max_patch_bytes, stop_event=stop_event, verification_config=verification_config)
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).checkpoint_blocked_verifier(run_id, self.worker_id,
                    agent_execution_id=execution_id, draft_artifact_id=review.draft_artifact_id,
                    missing_evidence=readiness.missing_evidence,
                    preliminary_public_outcome=preliminary_public_outcome(review.comparison, review.review).value,
                    scope_check=scope_check,
                    external_connections_declaration=(verification_config.external_connections
                                                      if verification_config is not None else "unknown"))
            return {"execution_id": execution_id, "readiness": readiness, "review": review,
                    "scope_check": scope_check}
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                failure_output = {"outcome": "VERIFICATION_ERROR", "error_type": type(error).__name__}
                if isinstance(error, ValidationError):
                    failure_output["validation_errors"] = [
                        {"loc": list(item["loc"]), "type": item["type"]}
                        for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]
                    ]
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(run_id, self.worker_id,
                        agent_execution_id=execution_id, status=AgentExecutionStatus.FAILED,
                        output=failure_output, llm_usage=None)
            except Exception as record_error:
                error.add_note(f"Verifier failure could not be committed: {type(record_error).__name__}")
            raise

    def classify_verification_failure(
        self, run_id: UUID, *, verifier_execution_id: UUID, draft_artifact_id: UUID,
        directories: RunDirectories, model_gateway: ModelGateway, max_diff_bytes: int,
        max_patch_bytes: int, stop_event: Event, independent_artifact_id: UUID | None = None,
    ) -> UUID:
        """Have Supervisor classify recorded failed evidence; this does not authorize recovery."""
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            observations = build_failure_observations(session, run_id, verifier_execution_id,
                draft_artifact_id=draft_artifact_id, independent_artifact_id=independent_artifact_id,
                artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
            max_repairs = policy.budget.max_output_repairs_per_agent_execution
            agent_input = {"verifier_execution_id": str(verifier_execution_id),
                "draft_artifact_id": str(draft_artifact_id),
                "independent_artifact_id": str(independent_artifact_id) if independent_artifact_id else None,
                "observations": observations}
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.SUPERVISOR, agent_input=agent_input)
        repairs = 0
        try:
            repair_note = ""
            while True:
                _require_run_not_stopped(run_id, stop_event)
                raw = model_gateway.request(run_id, agent_execution_id=execution_id,
                    prompt=build_verification_failure_prompt(agent_input) + repair_note)
                if raw is None:
                    raise RuntimeError("Supervisor model budget unavailable")
                try:
                    proposal = (VerificationFailureProposal.model_validate_json(raw) if isinstance(raw, str)
                                else VerificationFailureProposal.model_validate(raw))
                    resolve_verification_failure_evidence(proposal, observations)
                    break
                except (ValidationError, ValueError):
                    if repairs >= max_repairs:
                        raise
                    repairs += 1
                    repair_note = "\nReturn a corrected object citing only supplied observation identifiers."
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).commit_failure_classification(run_id, self.worker_id,
                    supervisor_execution_id=execution_id, proposal=proposal, repairs=repairs,
                    artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes,
                    max_patch_bytes=max_patch_bytes, stop_event=stop_event)
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(run_id, self.worker_id,
                        agent_execution_id=execution_id, status=AgentExecutionStatus.FAILED,
                        output={"outcome": "FAILURE_CLASSIFICATION_ERROR"}, llm_usage=None)
            except Exception as record_error:
                error.add_note(f"Supervisor failure could not be committed: {type(record_error).__name__}")
            raise
        return execution_id

    def _resume_recovery_run(
        self, run_id: UUID, created: bool, *, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
    ) -> dict:
        if not created:
            return {"outcome": "existing_recovery", "run_id": run_id, "handoff_id": None}
        directories, _ = self.initialize_run_workspace(run_id, workspaces_root=workspaces_root,
                                                       artifacts_root=artifacts_root, stop_event=stop_event)
        handoff_id = run_handoff_cycle(self.session_factory, run_id, self.worker_id,
            supervise=self.build_initial_supervise(run_id, model_gateway=supervisor_gateway, stop_event=stop_event),
            receivers=self.build_handoff_receivers(run_id, directories=directories, max_bytes=max_read_bytes,
                stop_event=stop_event, explorer_gateway=explorer_gateway, planner_gateway=planner_gateway))
        return {"outcome": "dispatched" if handoff_id else "blocked", "run_id": run_id,
                "handoff_id": handoff_id}

    def _ensure_recovery_workspace(
        self, run_id: UUID, *, workspaces_root: Path, artifacts_root: Path, stop_event: Event,
    ) -> RunDirectories:
        """Make an interrupted rediagnosis workspace usable without deleting existing data."""
        directories = resolve_run_directories(
            run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
        )
        directories.workspace_root.mkdir(exist_ok=True)
        directories.artifact_root.mkdir(exist_ok=True)
        if not directories.workspace_root.is_dir() or not directories.artifact_root.is_dir():
            raise ValueError("Recovery workspace and artifact roots must be directories")
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            repo, base_commit, policy_snapshot = session.execute(
                select(Task.repo, Task.base_commit, Task.policy_snapshot)
                .join(Run, Run.task_id == Task.id).where(Run.id == run_id),
            ).one()
            if base_commit is None:
                raise TaskStateConflictError(f"Run {run_id} has no pinned Task base commit")
            timeout_seconds = PolicyConfig.model_validate(policy_snapshot).budget.tool_call_timeout_seconds

        with workspace_operation_lock(
            self.session_factory.kw["bind"], run_id, stop_event=stop_event,
            check_unsettled_mutations=False,
        ):
            if not (directories.workspace_root / ".git").is_dir():
                if any(directories.workspace_root.iterdir()):
                    raise ValueError("Recovery workspace is not empty and has no Git metadata")
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).execute_worker_git_clone(
                        run_id, self.worker_id, repo=repo, workspace_root=directories.workspace_root,
                        artifact_root=directories.artifact_root, timeout_seconds=timeout_seconds,
                        stop_event=stop_event,
                    )
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).execute_worker_git_checkout(
                    run_id, self.worker_id, workspace_root=directories.workspace_root,
                    base_commit=base_commit, timeout_seconds=timeout_seconds, stop_event=stop_event,
                )
        return directories

    def _load_committed_handoff(self, run_id: UUID, handoff_id: UUID) -> CommittedHandoff | None:
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            row = session.execute(
                select(Handoff, AgentExecution)
                .join(AgentExecution, Handoff.from_agent_execution_id == AgentExecution.id)
                .where(
                    Handoff.id == handoff_id, AgentExecution.run_id == run_id,
                    AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                ),
            ).one_or_none()
            if row is None:
                return None
            handoff, _source = row
            approval = session.scalar(select(AgentExecution.id).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                AgentExecution.output["approved_handoff_id"].as_string() == str(handoff_id),
            ))
            if approval is None:
                return None
            return CommittedHandoff(
                id=handoff.id, from_agent_execution_id=handoff.from_agent_execution_id,
                to_agent_role=AgentRole(handoff.to_agent_role.upper()), context=dict(handoff.context),
            )

    def _handoff_receiver_exists(self, run_id: UUID, handoff_id: UUID) -> bool:
        with self.session_factory() as session:
            return session.scalar(select(exists().where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
            )))

    def _dispatch_committed_handoff(
        self, run_id: UUID, handoff_id: UUID, *, directories: RunDirectories,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
    ) -> bool:
        if self._handoff_receiver_exists(run_id, handoff_id):
            return False
        handoff = self._load_committed_handoff(run_id, handoff_id)
        if handoff is None:
            raise HandoffRoutingError("Committed recovery Handoff is unavailable")
        receivers = self.build_handoff_receivers(
            run_id, directories=directories, max_bytes=max_read_bytes, stop_event=stop_event,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            verification_config=verification_config,
            coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
            verifier_gateway=verifier_gateway, verifier_max_diff_bytes=verifier_max_diff_bytes,
        )
        receiver = receivers.get(handoff.to_agent_role)
        if receiver is None:
            raise HandoffRoutingError(
                f"No recovery receiver is registered for {handoff.to_agent_role.value}")
        receiver(handoff)
        return True

    def _dispatch_initial_rediagnosis(
        self, run_id: UUID, *, directories: RunDirectories, supervisor_gateway: ModelGateway,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
    ) -> dict:
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            executions = session.execute(select(
                AgentExecution.id, AgentExecution.agent_role, AgentExecution.status, AgentExecution.output,
            ).where(AgentExecution.run_id == run_id).order_by(
                AgentExecution.created_at, AgentExecution.id)).all()
            handoffs = session.execute(
                select(Handoff.id, Handoff.to_agent_role)
                .join(AgentExecution, Handoff.from_agent_execution_id == AgentExecution.id)
                .where(AgentExecution.run_id == run_id).order_by(Handoff.created_at, Handoff.id),
            ).all()
        if any(row.agent_role != AgentRole.SUPERVISOR.value.lower() for row in executions):
            return {"outcome": "existing", "reason": "exploration_already_started"}
        if handoffs:
            for handoff_id, to_agent_role in handoffs:
                if self._handoff_receiver_exists(run_id, handoff_id):
                    return {"outcome": "existing", "handoff_id": handoff_id}
                if to_agent_role != AgentRole.EXPLORER.value.lower():
                    return {"outcome": "unresumable", "handoff_id": handoff_id}
                dispatched = self._dispatch_committed_handoff(
                    run_id, handoff_id, directories=directories, explorer_gateway=explorer_gateway,
                    planner_gateway=planner_gateway, max_read_bytes=max_read_bytes, stop_event=stop_event,
                )
                return ({"outcome": "dispatched", "handoff_id": handoff_id, "resumed": True}
                        if dispatched else {"outcome": "existing", "handoff_id": handoff_id})
        if executions:
            if all(row.status == AgentExecutionStatus.SUCCEEDED
                   and (row.output or {}).get("outcome") == "BLOCKED" for row in executions):
                return {"outcome": "blocked", "reason": "initial_supervisor_blocked"}
            return {"outcome": "unresumable", "reason": "initial_supervisor_state_unknown"}
        handoff_id = run_handoff_cycle(
            self.session_factory, run_id, self.worker_id,
            supervise=self.build_initial_supervise(run_id, model_gateway=supervisor_gateway, stop_event=stop_event),
            receivers=self.build_handoff_receivers(
                run_id, directories=directories, max_bytes=max_read_bytes, stop_event=stop_event,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway),
        )
        if handoff_id is None:
            return {"outcome": "blocked", "reason": "initial_supervisor_blocked"}
        return {"outcome": "dispatched", "handoff_id": handoff_id, "resumed": False}

    def _drive_rediagnosis_after_exploration(
        self, run_id: UUID, *, directories: RunDirectories, supervisor_gateway: ModelGateway,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        # Once the Coder has a committed execution, resume its post-Coder
        # Supervisor and Verifier branch directly. Earlier Supervisor rows are
        # no longer the latest meaningful route, and replaying the Explorer /
        # Planner loop against them would misread a Coder-sourced handoff.
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            coder_handoff_id = session.scalar(select(AgentExecution.handoff_id).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.CODER.value.lower(),
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))
        if coder_handoff_id is not None:
            return self._resume_rediagnosis_after_committed_coder(
                run_id, code_handoff_id=coder_handoff_id, directories=directories,
                supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, verification_config=verification_config,
                max_read_bytes=max_read_bytes, stop_event=stop_event,
                coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )
        for _ in range(_REDIAGNOSIS_MAX_SUPERVISOR_STEPS):
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session, session.begin():
                RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
                explorer = session.scalar(select(AgentExecution).where(
                    AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.EXPLORER.value.lower(),
                    AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))
                any_explorer = session.scalar(select(exists().where(
                    AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.EXPLORER.value.lower(),
                )))
                supervisor = session.scalar(select(AgentExecution).where(
                    AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))
                explorer_id = explorer.id if explorer is not None else None
                explorer_created = explorer.created_at if explorer is not None else None
                supervisor_id = supervisor.id if supervisor is not None else None
                supervisor_created = supervisor.created_at if supervisor is not None else None
                supervisor_status = supervisor.status if supervisor is not None else None
                supervisor_output = dict(supervisor.output or {}) if supervisor is not None else {}
            if explorer_id is None:
                if any_explorer:
                    return {"outcome": "unresumable", "reason": "exploration_not_completed"}
                return {"outcome": "blocked", "reason": "no_committed_explorer"}
            if supervisor_id is not None and supervisor_created > explorer_created:
                if supervisor_status != AgentExecutionStatus.SUCCEEDED:
                    return {"outcome": "unresumable", "supervisor_execution_id": supervisor_id,
                            "reason": "supervisor_not_succeeded"}
                raw_handoff = supervisor_output.get("approved_handoff_id")
                if raw_handoff is None:
                    if supervisor_output.get("outcome") == "BLOCKED":
                        return {"outcome": "blocked", "supervisor_execution_id": supervisor_id}
                    return {"outcome": "unresumable", "supervisor_execution_id": supervisor_id,
                            "reason": "supervisor_has_no_handoff"}
                try:
                    handoff_id = UUID(str(raw_handoff))
                except (ValueError, TypeError):
                    return {"outcome": "unresumable", "supervisor_execution_id": supervisor_id,
                            "reason": "supervisor_handoff_invalid"}
            else:
                result = self.execute_supervisor_after_explorer(
                    run_id, explorer_execution_id=explorer_id, model_gateway=supervisor_gateway,
                    stop_event=stop_event,
                )
                if result.handoff_id is None:
                    return {"outcome": "blocked", "supervisor_execution_id": result.execution_id}
                handoff_id = result.handoff_id
            handoff = self._load_committed_handoff(run_id, handoff_id)
            if handoff is None:
                return {"outcome": "unresumable", "handoff_id": handoff_id,
                        "reason": "handoff_unavailable"}
            # A Supervisor route whose Handoff comes from Planner is a post-Planner
            # decision bound to the committed Planner result, not to the Explorer
            # evidence that the rest of this loop revisits.
            if self._committed_handoff_source_role(run_id, handoff) == AgentRole.PLANNER.value.lower():
                return self._continue_rediagnosis_after_planner(
                    run_id, planner_execution_id=handoff.from_agent_execution_id,
                    directories=directories, supervisor_gateway=supervisor_gateway,
                    explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                    verification_config=verification_config, max_read_bytes=max_read_bytes,
                    stop_event=stop_event, coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                    verifier_gateway=verifier_gateway, verifier_max_diff_bytes=verifier_max_diff_bytes,
                )
            if handoff.to_agent_role not in (AgentRole.EXPLORER, AgentRole.PLANNER):
                return {"outcome": "unresumable", "handoff_id": handoff_id,
                        "reason": "handoff_target_unsupported"}
            if self._handoff_receiver_exists(run_id, handoff_id):
                if handoff.to_agent_role == AgentRole.EXPLORER:
                    with self.session_factory() as session:
                        newer = session.scalar(select(AgentExecution).where(
                            AgentExecution.run_id == run_id,
                            AgentExecution.agent_role == AgentRole.EXPLORER.value.lower(),
                            AgentExecution.created_at > explorer_created,
                        ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))
                        newer_status = newer.status if newer is not None else None
                    if newer_status == AgentExecutionStatus.SUCCEEDED:
                        continue
                    return {"outcome": "existing", "reason": "reexploration_not_completed"}
                planner = self._load_committed_planner(run_id, handoff_id)
                if planner is None:
                    return self._planner_not_completed(run_id, handoff_id)
                return self._continue_rediagnosis_after_planner(
                    run_id, planner_execution_id=planner.id, directories=directories,
                    supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                    planner_gateway=planner_gateway, verification_config=verification_config,
                    max_read_bytes=max_read_bytes, stop_event=stop_event,
                    coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                    verifier_gateway=verifier_gateway, verifier_max_diff_bytes=verifier_max_diff_bytes,
                )
            dispatched = self._dispatch_committed_handoff(
                run_id, handoff_id, directories=directories, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes, stop_event=stop_event,
            )
            if not dispatched:
                if handoff.to_agent_role == AgentRole.PLANNER:
                    planner = self._load_committed_planner(run_id, handoff_id)
                    if planner is not None:
                        return self._continue_rediagnosis_after_planner(
                            run_id, planner_execution_id=planner.id, directories=directories,
                            supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                            planner_gateway=planner_gateway,
                            verification_config=verification_config,
                            max_read_bytes=max_read_bytes, stop_event=stop_event,
                            coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                            verifier_gateway=verifier_gateway,
                            verifier_max_diff_bytes=verifier_max_diff_bytes,
                        )
                return {"outcome": "existing", "handoff_id": handoff_id}
            if handoff.to_agent_role == AgentRole.PLANNER:
                planner = self._load_committed_planner(run_id, handoff_id)
                if planner is None:
                    return {"outcome": "dispatched", "handoff_id": handoff_id, "resumed": True}
                return self._continue_rediagnosis_after_planner(
                    run_id, planner_execution_id=planner.id, directories=directories,
                    supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                    planner_gateway=planner_gateway, verification_config=verification_config,
                    max_read_bytes=max_read_bytes, stop_event=stop_event,
                    coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                    verifier_gateway=verifier_gateway, verifier_max_diff_bytes=verifier_max_diff_bytes,
                )
        return {"outcome": "bounded", "reason": "rediagnosis_supervisor_step_limit"}

    def _committed_handoff_source_role(self, run_id: UUID, handoff: CommittedHandoff) -> str | None:
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution.agent_role).where(
                AgentExecution.id == handoff.from_agent_execution_id,
                AgentExecution.run_id == run_id,
            ))

    def _load_committed_planner(self, run_id: UUID, handoff_id: UUID) -> AgentExecution | None:
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            ))

    def _planner_not_completed(self, run_id: UUID, handoff_id: UUID) -> dict:
        with self.session_factory() as session:
            status = session.scalar(select(AgentExecution.status).where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
            ))
        if status == AgentExecutionStatus.RUNNING:
            return {"outcome": "existing", "handoff_id": handoff_id,
                    "reason": "planner_not_completed"}
        return {"outcome": "unresumable", "handoff_id": handoff_id,
                "reason": "planner_not_completed"}

    def _load_replan_planner(self, run_id: UUID, handoff_id: UUID) -> AgentExecution | None:
        """Load the Planner execution bound to a specific replan Handoff, any status."""
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))

    def _load_rediagnosis_baseline(self, run_id: UUID, handoff_id: UUID) -> AgentExecution | None:
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
            ).order_by(AgentExecution.created_at.desc()).limit(1))

    def _load_rediagnosis_coder(self, run_id: UUID, handoff_id: UUID) -> AgentExecution | None:
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
                AgentExecution.agent_role == AgentRole.CODER.value.lower(),
            ).order_by(AgentExecution.created_at.desc()).limit(1))

    def _recovery_verification_config_matches(
        self, run_id: UUID, verification_config: VerificationConfig,
    ) -> bool:
        config = VerificationConfig.model_validate(verification_config.model_dump())
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            task = session.scalar(select(Task).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
            return task is not None and task.id == config.task_id and task.base_commit == config.base_commit

    def _rediagnosis_baseline_is_valid(
        self, run_id: UUID, *, planner_execution_id: UUID, baseline_id: UUID, artifact_root: Path,
    ) -> bool:
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            try:
                validate_recorded_baseline(
                    session, run_id, planner_execution_id=planner_execution_id,
                    baseline_id=baseline_id, artifact_root=artifact_root,
                )
            except BaselineUnavailableError:
                return False
        return True

    def _load_rediagnosis_verifier(self, run_id: UUID, handoff_id: UUID) -> AgentExecution | None:
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id, AgentExecution.handoff_id == handoff_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))

    def _load_post_coder_supervisor(
        self, run_id: UUID, coder_execution_id: UUID,
    ) -> AgentExecution | None:
        with self.session_factory() as session:
            return session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.input["coder_execution_id"].as_string() == str(coder_execution_id),
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))

    def _resume_rediagnosis_after_committed_coder(
        self, run_id: UUID, *, code_handoff_id: UUID, directories: RunDirectories,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, verification_config: VerificationConfig | None,
        max_read_bytes: int, stop_event: Event, coder_gateway: ModelGateway | None,
        max_patch_bytes: int | None, verifier_gateway: ModelGateway | None,
        verifier_max_diff_bytes: int | None,
    ) -> dict:
        """Rebuild the committed CODE route context and resume from its Coder.

        A Coder whose approved Handoff came from a re-plan Planner is terminal:
        its committed result is reported without starting its Supervisor.
        """
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            handoff = session.get(Handoff, code_handoff_id)
            if handoff is None or handoff.to_agent_role != AgentRole.CODER.value.lower():
                return {"outcome": "unresumable", "reason": "coder_handoff_unavailable",
                        "handoff_id": code_handoff_id, "resumed": True}
            planner_execution_id = handoff.from_agent_execution_id
            approval = session.scalar(select(AgentExecution.id).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                AgentExecution.output["approved_handoff_id"].as_string() == str(code_handoff_id),
            ))
            baseline_values = [
                ref.get("baseline_verifier_id")
                for ref in (handoff.context or {}).get("evidence_refs", [])
                if isinstance(ref, dict) and "baseline_verifier_id" in ref
            ]
        if approval is None:
            return {"outcome": "unresumable", "reason": "coder_supervisor_unavailable",
                    "handoff_id": code_handoff_id, "resumed": True}
        baseline_verifier_id = None
        if len(baseline_values) == 1:
            try:
                baseline_verifier_id = UUID(str(baseline_values[0]))
            except (ValueError, TypeError, AttributeError):
                baseline_verifier_id = None
        # A Coder whose approved Handoff came from a re-plan Planner is terminal
        # for this recovery step: its committed result is saved, but its own
        # Supervisor/Verifier branch must not start from here.
        stop_after_coder = False
        was_replanned = self._planner_was_replanned(run_id, planner_execution_id)
        pre_baseline = None
        if was_replanned:
            pre_baseline = self._load_replan_supervisor(
                run_id, planner_execution_id, phase="pre_baseline")
            # Only the earlier, pre-baseline CODE path ends at the Coder. A
            # post-baseline CODE must resume its own Supervisor on replay.
            stop_after_coder = pre_baseline is None or pre_baseline.id == approval
        result = self._run_rediagnosis_coder(
            run_id, handoff_id=code_handoff_id, planner_execution_id=planner_execution_id,
            supervisor_execution_id=approval, baseline_verifier_id=baseline_verifier_id,
            directories=directories, coder_gateway=coder_gateway,
            max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
            stop_event=stop_event, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, supervisor_gateway=supervisor_gateway,
            verification_config=verification_config, verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
            stop_after_coder=stop_after_coder,
        )
        if was_replanned and not stop_after_coder:
            post_baseline = self._load_replan_supervisor(
                run_id, planner_execution_id, phase="post_baseline")
            if post_baseline is not None and post_baseline.id == approval and pre_baseline is not None:
                pre_output = pre_baseline.output or {}
                result.update({
                    "replan_supervisor_execution_id": pre_baseline.id,
                    "replan_supervisor_outcome": pre_output.get("outcome"),
                    "post_baseline_supervisor_execution_id": approval,
                    "post_baseline_outcome": (post_baseline.output or {}).get("outcome"),
                    "post_baseline_handoff_id": code_handoff_id,
                })
        return result

    def _planner_was_replanned(self, run_id: UUID, planner_execution_id: UUID) -> bool:
        """Return whether the Planner itself was produced by a Coder REPLAN route."""
        with self.session_factory() as session:
            planner_handoff_id = session.scalar(select(AgentExecution.handoff_id).where(
                AgentExecution.id == planner_execution_id,
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
            ))
            if planner_handoff_id is None:
                return False
            source_id = session.scalar(select(Handoff.from_agent_execution_id).where(
                Handoff.id == planner_handoff_id))
            if source_id is None:
                return False
            source_role = session.scalar(select(AgentExecution.agent_role).where(
                AgentExecution.id == source_id, AgentExecution.run_id == run_id))
        return source_role == AgentRole.CODER.value.lower()

    def _run_rediagnosis_coder(
        self, run_id: UUID, *, handoff_id: UUID, planner_execution_id: UUID, supervisor_execution_id: UUID,
        baseline_verifier_id: UUID | None, directories: RunDirectories, coder_gateway: ModelGateway | None,
        max_patch_bytes: int | None, max_read_bytes: int, stop_event: Event,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        supervisor_gateway: ModelGateway | None = None,
        verification_config: VerificationConfig | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
        stop_after_coder: bool = False,
        dispatch_replan: bool = True,
    ) -> dict:
        """Dispatch the Coder once, then either stop or continue its Supervisor route.

        ``stop_after_coder=True`` is the pre-baseline re-plan CODE route: the Coder result is
        committed and reported and the post-Coder Supervisor, Verifier and any automatic loop
        are deliberately not started. The post-baseline route passes ``False``: there the
        committed Coder result continues into that Coder's own Supervisor route, which needs
        its own gateway and otherwise stops at ``code_pending``/``supervisor_config_missing``.
        """
        common = {"handoff_id": handoff_id, "planner_execution_id": planner_execution_id,
                  "supervisor_execution_id": supervisor_execution_id,
                  "baseline_verifier_id": baseline_verifier_id}
        if coder_gateway is None or max_patch_bytes is None or max_patch_bytes <= 0:
            # Preserve the old safe code_pending result when the caller did not
            # supply the trusted Coder configuration.
            return {"outcome": "code_pending", "reason": "coder_config_missing",
                    **common, "resumed": False}
        if baseline_verifier_id is None:
            return {"outcome": "baseline_unavailable", "reason": "coder_baseline_unavailable",
                    **common, "resumed": False}
        existing = self._load_rediagnosis_coder(run_id, handoff_id)
        if existing is not None:
            if stop_after_coder:
                return self._rediagnosis_coder_result(existing, common=common, resumed=True)
            return self._continue_rediagnosis_after_coder(
                run_id, coder=existing, common=common, directories=directories,
                supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, verification_config=verification_config,
                verifier_gateway=verifier_gateway, verifier_max_diff_bytes=verifier_max_diff_bytes,
                max_read_bytes=max_read_bytes, max_patch_bytes=max_patch_bytes,
                stop_event=stop_event, resumed=True, coder_gateway=coder_gateway,
                dispatch_replan=dispatch_replan,
            )
        # Reject invalid baseline evidence before creating or starting Coder. A
        # committed Coder is replayed because its own successful apply_patch is
        # necessarily a workspace operation after the baseline.
        if not self._rediagnosis_baseline_is_valid(
            run_id, planner_execution_id=planner_execution_id, baseline_id=baseline_verifier_id,
            artifact_root=directories.artifact_root,
        ):
            return {"outcome": "baseline_unavailable", "reason": "baseline_evidence_invalid",
                    "baseline_verifier_id": baseline_verifier_id, **common, "resumed": False}
        _require_run_not_stopped(run_id, stop_event)
        try:
            self._dispatch_committed_handoff(
                run_id, handoff_id, directories=directories, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes, stop_event=stop_event,
                coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
            )
        except (CoderContextUnavailableError, BaselineUnavailableError):
            # The existing Coder baseline check rejected the evidence before any
            # Coder execution was created. Report a safe stop, never a patch.
            return {"outcome": "baseline_unavailable", "reason": "coder_baseline_unavailable",
                    **common, "resumed": False}
        coder = self._load_rediagnosis_coder(run_id, handoff_id)
        if stop_after_coder:
            return self._rediagnosis_coder_result(coder, common=common, resumed=False)
        return self._continue_rediagnosis_after_coder(
            run_id, coder=coder, common=common, directories=directories,
            supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, verification_config=verification_config,
            verifier_gateway=verifier_gateway, verifier_max_diff_bytes=verifier_max_diff_bytes,
            max_read_bytes=max_read_bytes, max_patch_bytes=max_patch_bytes,
            stop_event=stop_event, resumed=False, coder_gateway=coder_gateway,
            dispatch_replan=dispatch_replan,
        )

    def _rediagnosis_coder_result(
        self, coder: AgentExecution | None, *, common: dict, resumed: bool,
    ) -> dict:
        """Report the committed Coder result without starting its Supervisor."""
        if coder is None:
            return {"outcome": "unresumable", "reason": "coder_not_completed",
                    **common, "resumed": resumed}
        base = {"coder_execution_id": coder.id,
                "coder_outcome": (coder.output or {}).get("outcome"), **common}
        if coder.status == AgentExecutionStatus.RUNNING:
            return {"outcome": "existing", "reason": "coder_not_completed",
                    **base, "resumed": True}
        if coder.status != AgentExecutionStatus.SUCCEEDED:
            return {"outcome": "unresumable", "reason": "coder_not_completed",
                    **base, "resumed": resumed}
        if base["coder_outcome"] not in ("PATCH_READY", "BLOCKED"):
            return {"outcome": "unresumable", "reason": "coder_output_unexpected",
                    **base, "resumed": resumed}
        return {"outcome": "coder_result_saved", "reason": "coder_result_committed",
                **base, "resumed": resumed}

    def _rediagnosis_planner_result(
        self, planner: AgentExecution | None, *, handoff_id: UUID, common: dict,
        resumed: bool, failure_reason: str = "planner_not_completed",
    ) -> dict:
        """Report a missing or failed re-plan result without continuing the graph."""
        base = {"post_coder_handoff_id": handoff_id, **common, "resumed": resumed}
        if planner is not None:
            base["planner_execution_id"] = planner.id
            output = dict(planner.output or {})
            base["planner_outcome"] = output.get("outcome")
            if planner.status == AgentExecutionStatus.RUNNING:
                return {"outcome": "existing", "reason": "planner_not_completed", **base}
            if planner.status != AgentExecutionStatus.SUCCEEDED:
                return {"outcome": "unresumable", "reason": "planner_execution_failed", **base}
            return {"outcome": "replan_unavailable", "reason": "planner_output_unexpected", **base}
        return {"outcome": "replan_unavailable", "reason": failure_reason, **base}

    def _load_replan_supervisor(
        self, run_id: UUID, planner_execution_id: UUID, *, phase: str | None = None,
    ) -> AgentExecution | None:
        """Load the post-Planner Supervisor bound to this exact re-plan execution.

        The binding is the Planner identity in the Supervisor input or, for older
        rows, the Supervisor-approved Handoff sourced from that Planner. The
        initial plan's Supervisor is sourced from a different Planner, so it is
        never reused for the re-plan.

        New rows record the exact baseline observed by the Supervisor. Older
        rows lack this marker and use creation time as a compatibility heuristic;
        equal timestamps cannot establish their order and are rejected.
        """
        conditions = [
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
        ]
        with self.session_factory() as session:
            baselines = []
            if phase is not None:
                baselines = session.execute(select(AgentExecution.id, AgentExecution.created_at).join(
                    Handoff, AgentExecution.handoff_id == Handoff.id).where(
                        AgentExecution.run_id == run_id,
                        AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
                        AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                        AgentExecution.output["outcome"].as_string() == "BASELINE_RECORDED",
                        Handoff.from_agent_execution_id == planner_execution_id,
                        Handoff.to_agent_role == AgentRole.VERIFIER.value.lower(),
                    )).all()
                if len(baselines) > 1:
                    raise PlannerSupervisorContextError("Multiple recorded baselines make the phase ambiguous")
                if phase == "post_baseline" and not baselines:
                    return None
            handoff_ids = [str(handoff_id) for handoff_id in session.scalars(select(Handoff.id).where(
                Handoff.from_agent_execution_id == planner_execution_id))]
            candidates = session.scalars(select(AgentExecution).where(
                *conditions, or_(
                    AgentExecution.input["planner_execution_id"].as_string() == str(planner_execution_id),
                    (AgentExecution.status == AgentExecutionStatus.SUCCEEDED) &
                    AgentExecution.output["approved_handoff_id"].as_string().in_(handoff_ids),
                )).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc())).all()
            matching: AgentExecution | None = None
            for supervisor in candidates:
                if phase is None:
                    return supervisor
                data = supervisor.input or {}
                if "observed_baseline_verifier_id" in data:
                    observed = data["observed_baseline_verifier_id"]
                    available = data.get("baseline_available")
                    if observed is None and available is False:
                        candidate_phase = "pre_baseline"
                    elif (baselines and observed == str(baselines[0].id) and available is True):
                        candidate_phase = "post_baseline"
                    else:
                        raise PlannerSupervisorContextError("Supervisor baseline observation is invalid")
                elif not baselines:
                    candidate_phase = "pre_baseline"
                else:
                    baseline_created = baselines[0].created_at
                    if supervisor.created_at == baseline_created:
                        raise PlannerSupervisorContextError("Supervisor and baseline ordering is ambiguous")
                    candidate_phase = ("pre_baseline" if supervisor.created_at < baseline_created
                                       else "post_baseline")
                if candidate_phase == phase:
                    if matching is not None and matching.created_at == supervisor.created_at:
                        # Creation order cannot separate these two routes, so which one
                        # decided this phase is not knowable; refuse instead of guessing.
                        raise PlannerSupervisorContextError(
                            "Two routes for this Planner share a creation time")
                    matching = supervisor
            return matching

    def _replanned_supervisor_result(
        self, run_id: UUID, *, supervisor: AgentExecution, planner_execution_id: UUID,
        planner_outcome: str | None, post_coder_handoff_id: UUID, common: dict,
        resumed: bool,
    ) -> dict:
        """Report one committed re-plan route; a CODE or BASELINE Handoff may be dispatched."""
        base = {"post_coder_handoff_id": post_coder_handoff_id, **common,
                "planner_execution_id": planner_execution_id,
                "planner_outcome": planner_outcome,
                "replan_supervisor_execution_id": supervisor.id,
                "post_planner_supervisor_execution_id": supervisor.id}
        if supervisor.status == AgentExecutionStatus.RUNNING:
            return {"outcome": "existing", "reason": "replan_supervisor_not_completed",
                    **base, "resumed": True}
        if supervisor.status != AgentExecutionStatus.SUCCEEDED:
            return {"outcome": "unresumable", "reason": "replan_supervisor_not_succeeded",
                    **base, "resumed": resumed}
        output = dict(supervisor.output or {})
        outcome = output.get("outcome")
        base["replan_supervisor_outcome"] = outcome
        raw_handoff = output.get("approved_handoff_id")
        if raw_handoff is None:
            if outcome == "BLOCKED":
                return {"outcome": "blocked", "reason": "replan_supervisor_blocked",
                        **base, "resumed": resumed}
            return {"outcome": "unresumable", "reason": "replan_supervisor_has_no_handoff",
                    **base, "resumed": resumed}
        try:
            handoff_id = UUID(str(raw_handoff))
        except (ValueError, TypeError, AttributeError):
            return {"outcome": "unresumable", "reason": "replan_supervisor_handoff_invalid",
                    **base, "resumed": resumed}
        handoff = self._load_committed_handoff(run_id, handoff_id)
        if handoff is None or handoff.from_agent_execution_id != planner_execution_id:
            return {"outcome": "unresumable", "reason": "replan_supervisor_handoff_unavailable",
                    "replan_handoff_id": handoff_id, **base, "resumed": resumed}
        return {"outcome": "replanned", "reason": "coder_replan",
                "replan_handoff_id": handoff.id,
                "post_planner_handoff_id": handoff.id,
                "replan_handoff_target": handoff.to_agent_role.value.lower(),
                **base, "resumed": resumed}

    def _route_rediagnosis_after_replan_planner(
        self, run_id: UUID, *, planner: AgentExecution, post_coder_handoff_id: UUID,
        common: dict, supervisor_gateway: ModelGateway | None,
        stop_event: Event, resumed: bool, directories: RunDirectories,
        coder_gateway: ModelGateway | None, max_patch_bytes: int | None,
        max_read_bytes: int, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway | None,
        verification_config: VerificationConfig | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Route a committed re-plan through the existing post-Planner Supervisor once.

        A missing gateway, invalid Planner evidence, an unexpected Planner output,
        or a running/failed Supervisor stops safely. Replay is bound by the Planner
        execution identity, so the initial plan's Supervisor is never reused. The
        committed route is saved; a CODE Handoff target is handed to the existing
        Coder receiver once and a BASELINE target runs the trusted re-plan baseline
        once. Both stop there: the post-baseline Supervisor, the Coder's own
        Supervisor, the Verifier and any automatic loop are not started.
        """
        output = dict(planner.output or {})
        planner_outcome = output.get("outcome")
        base = {"post_coder_handoff_id": post_coder_handoff_id, **common,
                "planner_execution_id": planner.id, "planner_outcome": planner_outcome}
        if planner.status == AgentExecutionStatus.RUNNING:
            return {"outcome": "existing", "reason": "planner_not_completed",
                    **base, "resumed": True}
        if planner.status != AgentExecutionStatus.SUCCEEDED:
            return {"outcome": "unresumable", "reason": "planner_execution_failed",
                    **base, "resumed": resumed}
        if planner_outcome not in ("PLAN_READY", "NEEDS_EVIDENCE"):
            return {"outcome": "replan_unavailable", "reason": "planner_output_unexpected",
                    **base, "resumed": resumed}
        existing = self._load_replan_supervisor(run_id, planner.id, phase="pre_baseline")
        if existing is not None:
            return self._complete_replanned_route(
                run_id, supervisor=existing, planner_execution_id=planner.id,
                planner_outcome=planner_outcome,
                post_coder_handoff_id=post_coder_handoff_id, common=common, resumed=True,
                directories=directories, coder_gateway=coder_gateway,
                max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
                stop_event=stop_event, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway,
                verification_config=verification_config,
                supervisor_gateway=supervisor_gateway,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes)
        if supervisor_gateway is None:
            # Preserve the pre-route safe stop for callers without the trusted
            # Supervisor model configuration.
            if planner_outcome == "NEEDS_EVIDENCE":
                return {"outcome": "planner_needs_evidence",
                        "reason": "supervisor_config_missing", **base, "resumed": resumed}
            return {"outcome": "replanned", "reason": "supervisor_config_missing",
                    **base, "resumed": resumed}
        _require_run_not_stopped(run_id, stop_event)
        try:
            self.execute_supervisor_after_planner(
                run_id, planner_execution_id=planner.id,
                model_gateway=supervisor_gateway, stop_event=stop_event,
            )
        except PlannerSupervisorContextError:
            return {"outcome": "replan_unavailable", "reason": "supervisor_evidence_invalid",
                    **base, "resumed": resumed}
        except (RunExecutionDeniedError, GitExecutionCancelledError):
            # Lease, budget control and cancellation keep their existing
            # propagation rather than being reported as a Planner route.
            raise
        except Exception:
            # The Supervisor commit path records its own FAILED execution before
            # raising. Reload by Planner identity so a replay never starts it again.
            supervisor = self._load_replan_supervisor(run_id, planner.id, phase="pre_baseline")
            if supervisor is None:
                return {"outcome": "replan_unavailable", "reason": "supervisor_not_completed",
                        **base, "resumed": resumed}
            return self._complete_replanned_route(
                run_id, supervisor=supervisor, planner_execution_id=planner.id,
                planner_outcome=planner_outcome,
                post_coder_handoff_id=post_coder_handoff_id, common=common, resumed=True,
                directories=directories, coder_gateway=coder_gateway,
                max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
                stop_event=stop_event, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway,
                verification_config=verification_config,
                supervisor_gateway=supervisor_gateway,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes)
        supervisor = self._load_replan_supervisor(run_id, planner.id, phase="pre_baseline")
        if supervisor is None:
            return {"outcome": "replan_unavailable", "reason": "supervisor_not_completed",
                    **base, "resumed": resumed}
        return self._complete_replanned_route(
            run_id, supervisor=supervisor, planner_execution_id=planner.id,
            planner_outcome=planner_outcome,
            post_coder_handoff_id=post_coder_handoff_id, common=common, resumed=resumed,
            directories=directories, coder_gateway=coder_gateway,
            max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
            stop_event=stop_event, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway,
            verification_config=verification_config,
            supervisor_gateway=supervisor_gateway,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes)

    def _complete_replanned_route(
        self, run_id: UUID, *, supervisor: AgentExecution, planner_execution_id: UUID,
        planner_outcome: str | None, post_coder_handoff_id: UUID, common: dict,
        resumed: bool, directories: RunDirectories, coder_gateway: ModelGateway | None,
        max_patch_bytes: int | None, max_read_bytes: int, stop_event: Event,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway | None,
        verification_config: VerificationConfig | None = None,
        supervisor_gateway: ModelGateway | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Report the committed route, dispatching one approved CODE or BASELINE Handoff."""
        result = self._replanned_supervisor_result(
            run_id, supervisor=supervisor, planner_execution_id=planner_execution_id,
            planner_outcome=planner_outcome,
            post_coder_handoff_id=post_coder_handoff_id, common=common, resumed=resumed)
        if result.get("outcome") != "replanned":
            return result
        if result.get("replan_supervisor_outcome") == "BASELINE":
            if result.get("replan_handoff_target") != AgentRole.VERIFIER.value.lower():
                return {**result, "outcome": "unresumable", "reason": "handoff_outcome_mismatch"}
            baseline_result = self._dispatch_replanned_baseline(
                run_id, route=result, planner_execution_id=planner_execution_id,
                directories=directories, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes,
                stop_event=stop_event, verification_config=verification_config)
            if baseline_result.get("outcome") != "baseline_recorded":
                return baseline_result
            # The recorded baseline unlocks exactly one further route for the same
            # re-plan Planner; the Coder it may approve still stops after its own
            # committed result.
            return self._reroute_replanned_after_baseline(
                run_id, route=baseline_result, planner_execution_id=planner_execution_id,
                planner_outcome=planner_outcome, post_coder_handoff_id=post_coder_handoff_id,
                common=common, directories=directories,
                supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, coder_gateway=coder_gateway,
                max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
                stop_event=stop_event, verification_config=verification_config,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes)
        if result.get("replan_supervisor_outcome") != "CODE":
            return result
        if result.get("replan_handoff_target") != AgentRole.CODER.value.lower():
            return {**result, "outcome": "unresumable", "reason": "handoff_outcome_mismatch"}
        return self._dispatch_replanned_coder(
            run_id, route=result, planner_execution_id=planner_execution_id,
            directories=directories, coder_gateway=coder_gateway,
            max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
            stop_event=stop_event, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway)

    def _dispatch_replanned_coder(
        self, run_id: UUID, *, route: dict, planner_execution_id: UUID,
        directories: RunDirectories, coder_gateway: ModelGateway | None,
        max_patch_bytes: int | None, max_read_bytes: int, stop_event: Event,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway | None,
        stop_after_coder: bool = True,
        dispatch_replan: bool = True,
        supervisor_gateway: ModelGateway | None = None,
        verification_config: VerificationConfig | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Hand a committed re-plan CODE route to the existing Coder receiver once."""
        if coder_gateway is None or max_patch_bytes is None or max_patch_bytes <= 0:
            return {**route, "outcome": "code_pending", "reason": "coder_config_missing",
                    "resumed": False}
        handoff_id = route.get("replan_handoff_id")
        supervisor_id = route.get("replan_supervisor_execution_id")
        if handoff_id is None or supervisor_id is None:
            return {**route, "outcome": "unresumable",
                    "reason": "replan_coder_handoff_unavailable"}
        handoff = self._load_committed_handoff(run_id, handoff_id)
        if handoff is None or handoff.from_agent_execution_id != planner_execution_id:
            return {**route, "outcome": "unresumable",
                    "reason": "replan_coder_handoff_unavailable"}
        baseline_values = [
            ref.get("baseline_verifier_id")
            for ref in (handoff.context or {}).get("evidence_refs", [])
            if isinstance(ref, dict) and "baseline_verifier_id" in ref
        ]
        baseline_verifier_id = None
        if len(baseline_values) == 1:
            try:
                baseline_verifier_id = UUID(str(baseline_values[0]))
            except (ValueError, TypeError, AttributeError):
                baseline_verifier_id = None
        if baseline_verifier_id is None:
            return {**route, "outcome": "baseline_unavailable",
                    "reason": "coder_baseline_unavailable", "resumed": False}
        coder_result = self._run_rediagnosis_coder(
            run_id, handoff_id=handoff_id, planner_execution_id=planner_execution_id,
            supervisor_execution_id=supervisor_id,
            baseline_verifier_id=baseline_verifier_id, directories=directories,
            coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
            max_read_bytes=max_read_bytes, stop_event=stop_event,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            stop_after_coder=stop_after_coder, supervisor_gateway=supervisor_gateway,
            dispatch_replan=dispatch_replan,
            verification_config=verification_config, verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
        )
        return {**route, **coder_result}

    def _dispatch_replanned_baseline(
        self, run_id: UUID, *, route: dict, planner_execution_id: UUID,
        directories: RunDirectories, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway | None, max_read_bytes: int,
        stop_event: Event, verification_config: VerificationConfig | None = None,
    ) -> dict:
        """Run the trusted baseline for an approved re-plan route once, then stop.

        Only the existing trusted Verifier receiver may record the baseline. A
        missing or mismatched trusted configuration, an unavailable or foreign
        Handoff, a running or failed Verifier and a workspace that already carries
        an applied patch all stop without creating another Verifier, without
        cleaning the workspace and without treating the run as verified. Replay
        reuses the committed baseline instead of executing it again. The
        post-baseline Supervisor route, the Coder and any automatic loop are not
        started; that boundary belongs to a later step.
        """
        handoff_id = route.get("replan_handoff_id")
        if handoff_id is None:
            return {**route, "outcome": "unresumable",
                    "reason": "replan_baseline_handoff_unavailable"}
        handoff = self._load_committed_handoff(run_id, handoff_id)
        if (handoff is None or handoff.from_agent_execution_id != planner_execution_id
                or handoff.to_agent_role != AgentRole.VERIFIER):
            return {**route, "outcome": "unresumable",
                    "reason": "replan_baseline_handoff_unavailable"}
        bound = {**route, "baseline_handoff_id": handoff_id}
        existing = self._load_rediagnosis_baseline(run_id, handoff_id)
        if existing is not None:
            # A committed baseline needs no trusted configuration to be reported,
            # because nothing is executed again.
            return self._replanned_baseline_result(
                run_id, existing, common=bound, resumed=True,
                directories=directories, planner_execution_id=planner_execution_id)
        if verification_config is None:
            return {**bound, "outcome": "baseline_config_missing", "resumed": False}
        if not self._recovery_verification_config_matches(run_id, verification_config):
            return {**bound, "outcome": "baseline_config_invalid", "resumed": False}
        _require_run_not_stopped(run_id, stop_event)
        self._dispatch_committed_handoff(
            run_id, handoff_id, directories=directories, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, max_read_bytes=max_read_bytes,
            stop_event=stop_event, verification_config=verification_config,
        )
        verifier = self._load_rediagnosis_baseline(run_id, handoff_id)
        if verifier is None:
            return {**bound, "outcome": "baseline_unavailable",
                    "reason": "baseline_not_completed", "resumed": False}
        return self._replanned_baseline_result(
            run_id, verifier, common=bound, resumed=False,
            directories=directories, planner_execution_id=planner_execution_id)

    def _replanned_baseline_result(
        self, run_id: UUID, verifier: AgentExecution, *, common: dict, resumed: bool,
        directories: RunDirectories, planner_execution_id: UUID,
    ) -> dict:
        """Report the re-plan baseline without widening it into the next route."""
        base = {**common, "baseline_verifier_id": verifier.id, "resumed": resumed}
        if verifier.status == AgentExecutionStatus.RUNNING:
            return {**base, "outcome": "existing", "reason": "baseline_not_completed"}
        if verifier.status != AgentExecutionStatus.SUCCEEDED:
            return {**base, "outcome": "baseline_unavailable",
                    "reason": "baseline_execution_failed"}
        if (verifier.output or {}).get("outcome") != "BASELINE_RECORDED":
            # The dispatch happened but recorded no baseline (for example the report could not
            # be captured). That is "nothing was recorded", which is a different answer from
            # "a recorded baseline no longer verifies" below; a Supervisor cannot cite such an
            # execution as its observed baseline, so this state only reaches here from the
            # dispatch that just ran.
            return {**base, "outcome": "baseline_unavailable",
                    "reason": "baseline_evidence_unavailable"}
        if not self._rediagnosis_baseline_is_valid(
            run_id, planner_execution_id=planner_execution_id, baseline_id=verifier.id,
            artifact_root=directories.artifact_root,
        ):
            return {**base, "outcome": "baseline_unavailable",
                    "reason": "baseline_evidence_invalid"}
        return {**base, "outcome": "baseline_recorded",
                "reason": "replan_baseline_recorded"}

    def _reroute_replanned_after_baseline(
        self, run_id: UUID, *, route: dict, planner_execution_id: UUID,
        planner_outcome: str | None, post_coder_handoff_id: UUID, common: dict,
        directories: RunDirectories, supervisor_gateway: ModelGateway | None,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway | None,
        coder_gateway: ModelGateway | None, max_patch_bytes: int | None,
        max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Run exactly one post-baseline route for the re-plan Planner, then follow it.

        Only routes created at or after the committed re-plan baseline are read, so
        the pre-baseline decision is never mistaken for this one. A missing gateway,
        invalid Planner evidence, a running or failed Supervisor, a second BASELINE
        request and missing Coder configuration all stop without starting anything
        new. A CODE route dispatches the existing Coder receiver once and follows
        its committed result into the Coder's own Supervisor; an approved VERIFY
        Handoff records the post-patch Verifier draft only. A REPLAN request from
        that Coder may start one more Planner cycle, so the failure loop is bounded
        by the existing graph-step and tool budgets rather than by a new switch; a
        workspace that already carries an applied patch keeps the next trusted
        baseline blocked, which ends the loop instead of resetting the workspace.
        """
        supervisor = self._load_replan_supervisor(
            run_id, planner_execution_id, phase="post_baseline")
        resumed = supervisor is not None
        if supervisor is None:
            if supervisor_gateway is None:
                return {**route, "outcome": "post_baseline_route_pending",
                        "reason": "supervisor_config_missing", "resumed": False}
            _require_run_not_stopped(run_id, stop_event)
            try:
                self.execute_supervisor_after_planner(
                    run_id, planner_execution_id=planner_execution_id,
                    model_gateway=supervisor_gateway, stop_event=stop_event,
                )
            except PlannerSupervisorContextError:
                return {**route, "outcome": "post_baseline_route_unavailable",
                        "reason": "supervisor_evidence_invalid", "resumed": False}
            except (RunExecutionDeniedError, GitExecutionCancelledError):
                raise
            except Exception:
                # The Supervisor commit path already stored a FAILED execution;
                # report it below instead of starting it a second time.
                resumed = True
            supervisor = self._load_replan_supervisor(
                run_id, planner_execution_id, phase="post_baseline")
            if supervisor is None:
                return {**route, "outcome": "post_baseline_route_unavailable",
                        "reason": "supervisor_not_completed", "resumed": resumed}
        committed = self._replanned_supervisor_result(
            run_id, supervisor=supervisor, planner_execution_id=planner_execution_id,
            planner_outcome=planner_outcome,
            post_coder_handoff_id=post_coder_handoff_id, common=common, resumed=resumed)
        base = {**route,
                "post_baseline_supervisor_execution_id": committed.get("replan_supervisor_execution_id"),
                "post_baseline_outcome": committed.get("replan_supervisor_outcome"),
                "post_baseline_handoff_id": committed.get("replan_handoff_id"),
                "post_baseline_handoff_target": committed.get("replan_handoff_target"),
                "resumed": committed.get("resumed")}
        if committed.get("outcome") != "replanned":
            return {**base, "outcome": committed.get("outcome"),
                    "reason": committed.get("reason")}
        if committed.get("replan_supervisor_outcome") == "BASELINE":
            # A second BASELINE request after the trusted baseline exists must not
            # run the baseline again or start the Coder.
            return {**base, "outcome": "baseline_recorded",
                    "reason": "baseline_already_recorded"}
        if committed.get("replan_supervisor_outcome") != "CODE":
            return {**base, "outcome": "replanned", "reason": "post_baseline_route_saved"}
        if committed.get("replan_handoff_target") != AgentRole.CODER.value.lower():
            return {**base, "outcome": "unresumable", "reason": "handoff_outcome_mismatch"}
        coder_result = self._dispatch_replanned_coder(
            run_id, route=committed, planner_execution_id=planner_execution_id,
            directories=directories, coder_gateway=coder_gateway,
            max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
            stop_event=stop_event, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, stop_after_coder=False,
            supervisor_gateway=supervisor_gateway, verification_config=verification_config,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes)
        # The dispatch route describes the post-baseline decision. Preserve the
        # pre-baseline replan identity separately when combining both results.
        merged = {**base, **coder_result}
        for key in ("replan_supervisor_execution_id", "replan_supervisor_outcome",
                    "replan_handoff_id", "replan_handoff_target"):
            if key in base:
                merged[key] = base[key]
        return merged

    def _rediagnosis_verifier_result(
        self, verifier: AgentExecution | None, *, common: dict, verifier_handoff_id: UUID,
        resumed: bool,
    ) -> dict:
        """Report the intermediate Verifier draft or blocking evidence, never acceptance."""
        base = {"verifier_handoff_id": verifier_handoff_id, **common, "resumed": resumed}
        if verifier is None:
            return {"outcome": "unresumable", "reason": "verifier_not_completed", **base}
        base["verifier_execution_id"] = verifier.id
        output = dict(verifier.output or {})
        if output.get("outcome") == "VERIFICATION_BLOCKED":
            return {"outcome": "verifier_blocked", "draft_artifact_id": output.get("draft_artifact_id"),
                    "missing_evidence": output.get("missing_evidence"),
                    "preliminary_public_outcome": output.get("preliminary_public_outcome"), **base}
        if output.get("outcome") == "VERIFICATION_PASSED":
            return {"outcome": "passed", "verification_id": output.get("verification_id"), **base}
        if verifier.status == AgentExecutionStatus.RUNNING:
            return {"outcome": "existing", "reason": "verifier_not_completed", **base}
        if verifier.status != AgentExecutionStatus.SUCCEEDED:
            return {"outcome": "verification_unavailable", "reason": "verifier_not_completed", **base}
        return {"outcome": "verifier_evidence_saved", **base}

    def _load_rediagnosis_independent_receipt(
        self, run_id: UUID, *, verifier_execution_id: UUID, draft_artifact_id: UUID,
        artifact_root: Path,
    ) -> tuple[UUID, str] | None:
        """Find an already committed receipt for this Verifier and draft.

        A receipt that cannot be revalidated is reported as unavailable instead of
        being ignored, so a tampered record cannot trigger a fresh evaluation.
        """
        with self.session_factory() as session:
            artifact_ids = list(session.scalars(select(Artifact.id).where(
                Artifact.agent_execution_id == verifier_execution_id,
                Artifact.kind == "independent_evaluation",
            ).order_by(Artifact.created_at.desc(), Artifact.id.desc())))
        for artifact_id in artifact_ids:
            try:
                with self.session_factory() as session:
                    receipt = load_independent_evaluation(
                        session, run_id, verifier_execution_id,
                        artifact_id=artifact_id, artifact_root=artifact_root)
            except VerificationEvidenceError:
                return artifact_id, "unavailable"
            if receipt.draft_artifact_id == draft_artifact_id:
                return artifact_id, receipt.outcome
        return None

    @staticmethod
    def _independent_evaluation_material_is_valid(
        material: IndependentEvaluationMaterial | None, directories: RunDirectories,
    ) -> bool:
        if not isinstance(material, IndependentEvaluationMaterial):
            return False
        if (not isinstance(material.image, str) or not material.image.strip()
                or not isinstance(material.command, tuple) or not material.command
                or not isinstance(material.cases, tuple) or not material.cases
                or any(not isinstance(part, str) or not part for part in material.command)
                or any(not isinstance(case, PrivateCase) for case in material.cases)
                or not isinstance(material.timeout_seconds, (int, float))
                or material.timeout_seconds <= 0
                or not isinstance(material.calibration_workspaces, tuple)
                or len(material.calibration_workspaces) != 3):
            return False
        baseline, corrected, corrected_commit = material.calibration_workspaces
        if not isinstance(corrected_commit, str) or not corrected_commit:
            return False
        try:
            candidate_root = directories.workspace_root.resolve(strict=True)
            baseline_root = baseline.resolve(strict=True)
            corrected_root = corrected.resolve(strict=True)
        except OSError:
            return False
        return (baseline_root != corrected_root and baseline_root != candidate_root
                and corrected_root != candidate_root)

    def _commit_rediagnosis_independent_evaluation(
        self, run_id: UUID, *, result: dict, directories: RunDirectories,
        verification_config: VerificationConfig | None,
        material: IndependentEvaluationMaterial | None,
        max_diff_bytes: int | None, max_patch_bytes: int | None, stop_event: Event,
    ) -> dict | None:
        """Finish a public-passing Verifier draft with the existing trusted V1 path.

        Missing material or configuration, public failure, calibration failure and
        an unavailable or failed evaluation all return a non-passing result. An
        already committed receipt is reused instead of evaluating again. Callers
        must hold the Run's recovery dispatch lock across lookup, evaluation, and
        commit; the nested V1 workspace lock uses a separate namespace.
        """
        if result.get("outcome") != "verifier_blocked":
            return None
        if result.get("preliminary_public_outcome") != "passed":
            return None
        if material is None:
            # Preserve the exact old intermediate result for callers that did not
            # opt into independent evaluation.
            return None
        if verification_config is None or verification_config.independent_suite_version is None:
            return {"outcome": "verifier_blocked", "reason": "independent_evaluation_config_missing"}
        if (max_diff_bytes is None or max_diff_bytes <= 0
                or max_patch_bytes is None or max_patch_bytes <= 0):
            return {"outcome": "verifier_blocked", "reason": "independent_evaluation_limits_missing"}
        if not self._independent_evaluation_material_is_valid(material, directories):
            return {"outcome": "verifier_blocked", "reason": "independent_evaluation_material_invalid"}
        try:
            with self.session_factory() as session, session.begin():
                coordinator = RunCoordinator(session, self.lease_config)
                coordinator.assert_execution_right(run_id, self.worker_id)
                coordinator.assert_independent_memory_isolation(run_id)
        except VerificationEvidenceError:
            return {"outcome": "verifier_blocked", "reason": "memory_context_precludes_independent_evaluation"}
        try:
            verifier_id = UUID(str(result["verifier_execution_id"]))
            draft_id = UUID(str(result["draft_artifact_id"]))
        except (KeyError, TypeError, ValueError):
            return {"outcome": "verification_unavailable",
                    "reason": "independent_evaluation_evidence_invalid"}
        existing = self._load_rediagnosis_independent_receipt(
            run_id, verifier_execution_id=verifier_id, draft_artifact_id=draft_id,
            artifact_root=directories.artifact_root)
        if existing is not None:
            receipt_id, receipt_outcome = existing
            if receipt_outcome == "passed":
                try:
                    with self.session_factory() as session:
                        verification_id = RunCoordinator(
                            session, self.lease_config).finalize_passing_verification(
                                run_id, self.worker_id, agent_execution_id=verifier_id,
                                draft_artifact_id=draft_id, independent_artifact_id=receipt_id,
                                verification_config=verification_config,
                                workspace_root=directories.workspace_root,
                                artifact_root=directories.artifact_root,
                                max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
                                stop_event=stop_event)
                except (OSError, ValueError):
                    return {"outcome": "independent_evaluation_unavailable",
                            "reason": "independent_evaluation_evidence_invalid"}
                if not verification_id:
                    return {"outcome": "independent_evaluation_unavailable",
                            "reason": "independent_evaluation_evidence_invalid"}
                return {"outcome": "passed", "independent_artifact_id": receipt_id,
                        "verification_id": verification_id}
            return {"outcome": ("independent_evaluation_failed" if receipt_outcome == "failed"
                                else "independent_evaluation_unavailable"),
                    "independent_artifact_id": receipt_id, "verification_id": None}
        try:
            finalized = self.finalize_with_independent_evaluation(
                run_id, agent_execution_id=verifier_id, draft_artifact_id=draft_id,
                directories=directories, verification_config=verification_config,
                image=material.image, command=material.command, cases=material.cases,
                calibration_workspaces=material.calibration_workspaces,
                max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
                timeout_seconds=material.timeout_seconds, stop_event=stop_event)
        except (OSError, ValueError):
            return {"outcome": "independent_evaluation_unavailable",
                    "reason": "independent_evaluation_evidence_invalid"}
        if finalized.get("outcome") == "passed" and not finalized.get("verification_id"):
            return {"outcome": "independent_evaluation_unavailable",
                    "independent_artifact_id": finalized.get("independent_artifact_id"),
                    "verification_id": None}
        return {"outcome": ("passed" if finalized.get("outcome") == "passed" else
                            "independent_evaluation_failed" if finalized.get("outcome") == "failed"
                            else "independent_evaluation_unavailable"),
                "independent_artifact_id": finalized.get("independent_artifact_id"),
                "verification_id": finalized.get("verification_id")}

    def _continue_rediagnosis_after_coder(
        self, run_id: UUID, *, coder: AgentExecution | None, common: dict,
        directories: RunDirectories, supervisor_gateway: ModelGateway | None,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway | None,
        verification_config: VerificationConfig | None, verifier_gateway: ModelGateway | None,
        verifier_max_diff_bytes: int | None, max_read_bytes: int, max_patch_bytes: int | None,
        stop_event: Event, resumed: bool, coder_gateway: ModelGateway | None = None,
        dispatch_replan: bool = True,
    ) -> dict:
        """Run the existing post-Coder Supervisor and follow its approved Handoff once.

        A REPLAN Handoff is dispatched to the existing Planner receiver; its
        committed result then gets one existing post-Planner Supervisor route. A
        BASELINE route records the trusted re-plan baseline once and stops before
        the post-baseline route. Other routes are saved but not dispatched. A
        VERIFY Handoff records only the post-patch public tests and semantic-review
        draft. No formal Verification, Run verdict, hidden test or automatic loop
        runs here. (This method is the *caller* of the Coder's Supervisor: whether a
        committed Coder result reaches it is decided by ``stop_after_coder`` in
        ``_run_rediagnosis_coder``.)
        """
        if coder is None:
            return {"outcome": "unresumable", "reason": "coder_not_completed",
                    **common, "resumed": resumed}
        if coder.status == AgentExecutionStatus.RUNNING:
            return {"outcome": "existing", "reason": "coder_not_completed",
                    "coder_execution_id": coder.id, **common, "resumed": True}
        if coder.status != AgentExecutionStatus.SUCCEEDED:
            return {"outcome": "unresumable", "reason": "coder_not_completed",
                    "coder_execution_id": coder.id, **common, "resumed": resumed}
        coder_output = dict(coder.output or {})
        base = {"coder_execution_id": coder.id, "coder_outcome": coder_output.get("outcome"),
                **common}
        if max_patch_bytes is None or max_patch_bytes <= 0:
            return {"outcome": "code_pending", "reason": "coder_config_missing",
                    **base, "resumed": resumed}
        post = self._load_post_coder_supervisor(run_id, coder.id)
        if post is None:
            if supervisor_gateway is None:
                return {"outcome": "code_pending", "reason": "supervisor_config_missing",
                        **base, "resumed": resumed}
            _require_run_not_stopped(run_id, stop_event)
            try:
                supervised = self.execute_supervisor_after_coder(
                    run_id, coder_execution_id=coder.id, directories=directories,
                    model_gateway=supervisor_gateway, max_patch_bytes=max_patch_bytes,
                    stop_event=stop_event,
                )
            except CoderSupervisorContextError:
                return {"outcome": "patch_unavailable", "reason": "coder_evidence_invalid",
                        **base, "resumed": resumed}
            supervisor_id = supervised.execution_id
            supervisor_outcome = supervised.outcome
            handoff_id = supervised.handoff_id
        else:
            if post.status == AgentExecutionStatus.RUNNING:
                return {"outcome": "existing", "reason": "post_coder_supervisor_not_completed",
                        "post_coder_supervisor_execution_id": post.id, **base, "resumed": True}
            if post.status != AgentExecutionStatus.SUCCEEDED:
                return {"outcome": "unresumable", "reason": "supervisor_not_succeeded",
                        "post_coder_supervisor_execution_id": post.id, **base, "resumed": resumed}
            post_output = dict(post.output or {})
            supervisor_id = post.id
            supervisor_outcome = post_output.get("outcome")
            raw_handoff = post_output.get("approved_handoff_id")
            if raw_handoff is None:
                if supervisor_outcome == "BLOCKED":
                    return {"outcome": "blocked", "reason": "coder_supervisor_blocked",
                            "post_coder_supervisor_execution_id": supervisor_id, **base,
                            "resumed": True}
                return {"outcome": "unresumable", "reason": "supervisor_has_no_handoff",
                        "post_coder_supervisor_execution_id": supervisor_id, **base,
                        "resumed": resumed}
            try:
                handoff_id = UUID(str(raw_handoff))
            except (ValueError, TypeError):
                return {"outcome": "unresumable", "reason": "supervisor_handoff_invalid",
                        "post_coder_supervisor_execution_id": supervisor_id, **base,
                        "resumed": resumed}
        base["post_coder_supervisor_execution_id"] = supervisor_id
        if handoff_id is None:
            return {"outcome": "blocked", "reason": "coder_supervisor_blocked", **base,
                    "resumed": resumed}
        handoff = self._load_committed_handoff(run_id, handoff_id)
        if handoff is None or handoff.from_agent_execution_id != coder.id:
            return {"outcome": "unresumable", "reason": "coder_supervisor_handoff_unavailable",
                    "post_coder_handoff_id": handoff_id, **base, "resumed": resumed}
        if supervisor_outcome == "REPLAN":
            if handoff.to_agent_role != AgentRole.PLANNER:
                return {"outcome": "unresumable", "reason": "handoff_outcome_mismatch",
                        "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
            if not dispatch_replan:
                # A bounded recovery saves this request and stops instead of
                # starting another Planner cycle from a later Coder.
                return {"outcome": "replan", "reason": "replan_deferred",
                        "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
            # Reuse a previously dispatched Planner result first, so replaying the
            # recovery never runs the Planner model twice or needs its gateway.
            existing_planner = self._load_replan_planner(run_id, handoff.id)
            if existing_planner is not None:
                return self._route_rediagnosis_after_replan_planner(
                    run_id, planner=existing_planner, post_coder_handoff_id=handoff.id,
                    common=base, supervisor_gateway=supervisor_gateway,
                    stop_event=stop_event, resumed=True,
                    directories=directories, coder_gateway=coder_gateway,
                    max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
                    explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                    verification_config=verification_config,
                    verifier_gateway=verifier_gateway,
                    verifier_max_diff_bytes=verifier_max_diff_bytes)
            if planner_gateway is None:
                # Preserve the pre-dispatch safe stop for callers without the
                # trusted Planner model configuration.
                return {"outcome": "replan", "reason": "planner_config_missing",
                        "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
            _require_run_not_stopped(run_id, stop_event)
            try:
                # The existing receiver reads the committed Handoff's Coder blocker
                # and Explorer/Coder evidence refs; this path adds no material.
                self._dispatch_committed_handoff(
                    run_id, handoff.id, directories=directories,
                    explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                    max_read_bytes=max_read_bytes, stop_event=stop_event,
                )
            except PlannerContextUnavailableError:
                return {"outcome": "replan_unavailable", "reason": "planner_evidence_invalid",
                        "post_coder_handoff_id": handoff.id, **base, "resumed": False}
            except HandoffRoutingError:
                return {"outcome": "replan_unavailable", "reason": "planner_handoff_unavailable",
                        "post_coder_handoff_id": handoff.id, **base, "resumed": False}
            except (RunExecutionDeniedError, GitExecutionCancelledError):
                # Lease/budget denial and cancellation are control decisions, not
                # Planner results, and must keep their existing propagation.
                raise
            except Exception:
                # The Planner execution stores its own FAILED reason before raising.
                # Report the safe stop here instead of continuing the graph.
                planner = self._load_replan_planner(run_id, handoff.id)
                return self._rediagnosis_planner_result(
                    planner, handoff_id=handoff.id, common=base,
                    resumed=planner is not None,
                    failure_reason="planner_execution_failed")
            planner = self._load_replan_planner(run_id, handoff.id)
            if planner is None:
                return self._rediagnosis_planner_result(
                    None, handoff_id=handoff.id, common=base, resumed=False)
            # Save the real post-Planner route and Handoff. Only a CODE route is
            # handed to the Coder once; the Coder's own Supervisor, the Verifier,
            # Explorer and any automatic loop are deliberately not started.
            return self._route_rediagnosis_after_replan_planner(
                run_id, planner=planner, post_coder_handoff_id=handoff.id,
                common=base, supervisor_gateway=supervisor_gateway,
                stop_event=stop_event, resumed=False,
                directories=directories, coder_gateway=coder_gateway,
                max_patch_bytes=max_patch_bytes, max_read_bytes=max_read_bytes,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                verification_config=verification_config,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes)
        if supervisor_outcome != "VERIFY" or handoff.to_agent_role != AgentRole.VERIFIER:
            return {"outcome": "unresumable", "reason": "handoff_outcome_mismatch",
                    "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
        if verification_config is None:
            return {"outcome": "verification_config_missing",
                    "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
        if not self._recovery_verification_config_matches(run_id, verification_config):
            return {"outcome": "verification_config_invalid",
                    "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
        if verifier_gateway is None or verifier_max_diff_bytes is None or verifier_max_diff_bytes <= 0:
            return {"outcome": "verifier_config_missing",
                    "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
        existing = self._load_rediagnosis_verifier(run_id, handoff.id)
        if existing is not None:
            return self._rediagnosis_verifier_result(
                existing, common=base, verifier_handoff_id=handoff.id, resumed=True)
        _require_run_not_stopped(run_id, stop_event)
        try:
            self._dispatch_committed_handoff(
                run_id, handoff.id, directories=directories, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes,
                stop_event=stop_event, verification_config=verification_config,
                max_patch_bytes=max_patch_bytes, verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )
        except VerificationEvidenceError:
            verifier = self._load_rediagnosis_verifier(run_id, handoff.id)
            return {"outcome": "verification_unavailable", "reason": "verification_evidence_invalid",
                    "verifier_execution_id": verifier.id if verifier is not None else None,
                    "post_coder_handoff_id": handoff.id, **base, "resumed": resumed}
        verifier = self._load_rediagnosis_verifier(run_id, handoff.id)
        return self._rediagnosis_verifier_result(
            verifier, common=base, verifier_handoff_id=handoff.id, resumed=resumed)

    def _reroute_rediagnosis_after_baseline(
        self, run_id: UUID, *, planner_execution_id: UUID, baseline_verifier_id: UUID,
        directories: RunDirectories, supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, coder_gateway: ModelGateway | None,
        max_patch_bytes: int | None, max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Run exactly one post-baseline Planner Supervisor route."""
        _require_run_not_stopped(run_id, stop_event)
        result = self.execute_supervisor_after_planner(
            run_id, planner_execution_id=planner_execution_id,
            model_gateway=supervisor_gateway, stop_event=stop_event,
        )
        if result.handoff_id is None:
            return {"outcome": "blocked", "supervisor_execution_id": result.execution_id,
                    "planner_execution_id": planner_execution_id}
        handoff = self._load_committed_handoff(run_id, result.handoff_id)
        if handoff is None or handoff.from_agent_execution_id != planner_execution_id:
            return {"outcome": "unresumable", "handoff_id": result.handoff_id,
                    "reason": "planner_handoff_unavailable"}
        if handoff.to_agent_role == AgentRole.CODER:
            return self._run_rediagnosis_coder(
                run_id, handoff_id=handoff.id, planner_execution_id=planner_execution_id,
                supervisor_execution_id=result.execution_id, baseline_verifier_id=baseline_verifier_id,
                directories=directories, coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                max_read_bytes=max_read_bytes, stop_event=stop_event,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                supervisor_gateway=supervisor_gateway, verification_config=verification_config,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )
        common = {"handoff_id": handoff.id, "planner_execution_id": planner_execution_id,
                  "supervisor_execution_id": result.execution_id,
                  "baseline_verifier_id": baseline_verifier_id}
        if handoff.to_agent_role == AgentRole.EXPLORER:
            return {"outcome": "replan", **common, "resumed": False}
        if handoff.to_agent_role == AgentRole.VERIFIER:
            # A second BASELINE request after the trusted baseline exists must not
            # re-run the baseline or start Coder.
            return {"outcome": "baseline_recorded", "reason": "baseline_already_recorded",
                    **common, "resumed": True}
        return {"outcome": "unresumable", "reason": "handoff_target_unsupported", **common}

    def _continue_rediagnosis_after_planner(
        self, run_id: UUID, *, planner_execution_id: UUID, directories: RunDirectories,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, verification_config: VerificationConfig | None,
        max_read_bytes: int, stop_event: Event,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Route a committed Planner result, then one post-baseline route and Coder.

        A baseline is executed only through the existing trusted Verifier receiver.
        After it is recorded, exactly one further Planner Supervisor route may run;
        CODE dispatches the existing Coder receiver once, then the committed Coder
        continues into its own Supervisor route and, only for an approved VERIFY
        Handoff, the existing post-patch Verifier. It still stops before formal
        verification, hidden tests or any automatic loop.
        """
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            planner = session.scalar(select(AgentExecution).where(
                AgentExecution.id == planner_execution_id,
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            ))
            if planner is None:
                return {"outcome": "unresumable", "reason": "planner_not_committed"}
            baseline = session.scalar(select(AgentExecution).join(
                Handoff, AgentExecution.handoff_id == Handoff.id).where(
                    AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
                    AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                    AgentExecution.output["outcome"].as_string() == "BASELINE_RECORDED",
                    Handoff.from_agent_execution_id == planner_execution_id,
                    Handoff.to_agent_role == AgentRole.VERIFIER.value.lower(),
                ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))
            baseline_id = baseline.id if baseline is not None else None
            baseline_created = baseline.created_at if baseline is not None else None
            consumer = session.scalar(select(AgentExecution).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.created_at > planner.created_at,
                AgentExecution.input["coder_execution_id"].as_string().is_(None),
            ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1))
            consumer_id = consumer.id if consumer is not None else None
            consumer_status = consumer.status if consumer is not None else None
            consumer_output = dict(consumer.output or {}) if consumer is not None else {}
            consumer_created = consumer.created_at if consumer is not None else None
            post_baseline_route = (baseline_created is not None and consumer_created is not None
                                   and consumer_created > baseline_created)
            if baseline_id is not None and not post_baseline_route:
                # The latest committed route authorized the baseline (or predates
                # it). Force exactly one fresh post-baseline route below.
                consumer_id = None
        if consumer_id is None:
            result = self.execute_supervisor_after_planner(
                run_id, planner_execution_id=planner_execution_id,
                model_gateway=supervisor_gateway, stop_event=stop_event,
            )
            supervisor_id, outcome, handoff_id = result.execution_id, result.outcome, result.handoff_id
            post_baseline_route = baseline_id is not None
        else:
            if consumer_status != AgentExecutionStatus.SUCCEEDED:
                return {"outcome": "unresumable", "supervisor_execution_id": consumer_id,
                        "reason": "supervisor_not_succeeded"}
            supervisor_id = consumer_id
            outcome = consumer_output.get("outcome")
            raw_handoff = consumer_output.get("approved_handoff_id")
            if raw_handoff is None:
                if outcome == "BLOCKED":
                    return {"outcome": "blocked", "supervisor_execution_id": supervisor_id,
                            "planner_execution_id": planner_execution_id}
                return {"outcome": "unresumable", "supervisor_execution_id": supervisor_id,
                        "reason": "supervisor_has_no_handoff"}
            try:
                handoff_id = UUID(str(raw_handoff))
            except (ValueError, TypeError):
                return {"outcome": "unresumable", "supervisor_execution_id": supervisor_id,
                        "reason": "supervisor_handoff_invalid"}
        if handoff_id is None:
            return {"outcome": "blocked", "supervisor_execution_id": supervisor_id,
                    "planner_execution_id": planner_execution_id}
        handoff = self._load_committed_handoff(run_id, handoff_id)
        if handoff is None or handoff.from_agent_execution_id != planner_execution_id:
            return {"outcome": "unresumable", "handoff_id": handoff_id,
                    "reason": "planner_handoff_unavailable"}
        common = {"handoff_id": handoff_id, "planner_execution_id": planner_execution_id,
                  "supervisor_execution_id": supervisor_id}
        if handoff.to_agent_role == AgentRole.CODER:
            return self._run_rediagnosis_coder(
                run_id, handoff_id=handoff_id, planner_execution_id=planner_execution_id,
                supervisor_execution_id=supervisor_id, baseline_verifier_id=baseline_id,
                directories=directories, coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                max_read_bytes=max_read_bytes, stop_event=stop_event,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                supervisor_gateway=supervisor_gateway, verification_config=verification_config,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )
        if handoff.to_agent_role == AgentRole.EXPLORER:
            return {"outcome": "replan", **common, "resumed": False}
        if handoff.to_agent_role != AgentRole.VERIFIER:
            return {"outcome": "unresumable", "handoff_id": handoff_id,
                    "reason": "handoff_target_unsupported"}
        # Only a declared BASELINE decision may run the trusted baseline.
        if outcome != "BASELINE":
            return {"outcome": "unresumable", "handoff_id": handoff_id,
                    "reason": "handoff_outcome_mismatch"}
        if baseline_id is not None:
            if (verification_config is not None
                    and not self._recovery_verification_config_matches(run_id, verification_config)):
                return {"outcome": "baseline_config_invalid", **common, "resumed": False}
            if not self._rediagnosis_baseline_is_valid(
                run_id, planner_execution_id=planner_execution_id, baseline_id=baseline_id,
                artifact_root=directories.artifact_root,
            ):
                return {"outcome": "baseline_unavailable", "reason": "baseline_evidence_invalid",
                        "baseline_verifier_id": baseline_id, **common, "resumed": False}
            if post_baseline_route:
                return {"outcome": "baseline_recorded", "reason": "baseline_already_recorded",
                        "baseline_verifier_id": baseline_id, **common, "resumed": True}
            return self._reroute_rediagnosis_after_baseline(
                run_id, planner_execution_id=planner_execution_id, baseline_verifier_id=baseline_id,
                directories=directories, supervisor_gateway=supervisor_gateway,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                max_read_bytes=max_read_bytes, stop_event=stop_event,
                verification_config=verification_config,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )
        if verification_config is None:
            return {"outcome": "baseline_config_missing", **common, "resumed": False}
        if not self._recovery_verification_config_matches(run_id, verification_config):
            return {"outcome": "baseline_config_invalid", **common, "resumed": False}
        verifier = self._load_rediagnosis_baseline(run_id, handoff_id)
        resumed = verifier is not None
        if verifier is None:
            _require_run_not_stopped(run_id, stop_event)
            self._dispatch_committed_handoff(
                run_id, handoff_id, directories=directories, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes,
                stop_event=stop_event, verification_config=verification_config,
            )
            verifier = self._load_rediagnosis_baseline(run_id, handoff_id)
        if verifier is None:
            return {"outcome": "baseline_unavailable", **common, "resumed": resumed}
        output = dict(verifier.output or {})
        recorded = (verifier.status == AgentExecutionStatus.SUCCEEDED
                    and output.get("outcome") == "BASELINE_RECORDED")
        if not recorded:
            return {"outcome": "baseline_unavailable", "baseline_verifier_id": verifier.id,
                    **common, "resumed": resumed}
        return self._reroute_rediagnosis_after_baseline(
            run_id, planner_execution_id=planner_execution_id, baseline_verifier_id=verifier.id,
            directories=directories, supervisor_gateway=supervisor_gateway,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
            max_read_bytes=max_read_bytes, stop_event=stop_event,
            verification_config=verification_config,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
        )

    def _resume_rediagnosis_locked(
        self, run_id: UUID, *, directories: RunDirectories, supervisor_gateway: ModelGateway,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        initial = self._dispatch_initial_rediagnosis(
            run_id, directories=directories, supervisor_gateway=supervisor_gateway,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            max_read_bytes=max_read_bytes, stop_event=stop_event,
        )
        if initial["outcome"] not in ("dispatched", "existing"):
            return initial
        return self._drive_rediagnosis_after_exploration(
            run_id, directories=directories, supervisor_gateway=supervisor_gateway,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            max_read_bytes=max_read_bytes, stop_event=stop_event,
            verification_config=verification_config,
            coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
        )

    def resume_implementation_recovery(
        self, task_id: UUID, *, creation_idempotency_key: str, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
        execution_manifest: Path | None = None, execution_sha256: str | None = None,
    ) -> dict:
        """Start one approved retry and dispatch its first Supervisor-to-Explorer handoff.

        ``execution_manifest``/``execution_sha256`` mirror the one-shot entry: verified before
        the retry Run is created, and the verified digest must equal what this Task's earlier
        Runs recorded. A Task whose history is already pinned cannot be recovered without the
        same manifest — that refusal is deliberate, not an oversight.
        """
        if max_read_bytes <= 0 or stop_event is None:
            raise ValueError("A positive read limit and stop event are required")
        execution_sha256 = verify_optional_binding(
            manifest_path=execution_manifest, expected_sha256=execution_sha256,
            require_complete=True)
        if not workspaces_root.resolve(strict=True).is_dir() or not artifacts_root.resolve(strict=True).is_dir():
            raise ValueError("Workspace and artifact roots must be existing directories")
        if stop_event.is_set():
            raise GitExecutionCancelledError("Recovery stopped before retry creation")
        with self.session_factory() as session:
            run, created = RunCoordinator(session, self.lease_config).start_retry_run(
                task_id, self.worker_id, creation_idempotency_key,
                execution_sha256=execution_sha256)
            retry_id = run.id
        if not created:
            return {"outcome": "existing_retry", "run_id": retry_id, "handoff_id": None}
        return self._resume_recovery_run(retry_id, created, workspaces_root=workspaces_root,
            artifacts_root=artifacts_root, supervisor_gateway=supervisor_gateway,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            max_read_bytes=max_read_bytes, stop_event=stop_event)

    def resume_rediagnosis_recovery(
        self, task_id: UUID, *, creation_idempotency_key: str, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
        independent_evaluation: IndependentEvaluationMaterial | None = None,
        execution_manifest: Path | None = None, execution_sha256: str | None = None,
    ) -> dict:
        """Start or resume one rediagnosis Run through Coder and its Supervisor route.

        An approved VERIFY Handoff reaches the existing post-patch Verifier, which
        records only public-test and semantic-review draft/blocking evidence here.
        A post-Coder REPLAN Handoff reaches the existing Planner receiver once; the
        committed result gets one existing post-Planner Supervisor route. A CODE
        route is handed to the existing Coder receiver once and its result is saved
        before stopping; other routes and Handoffs are saved but not dispatched.
        The Coder's own Supervisor, the Verifier, a loop or any downstream dispatch
        do not follow the re-plan route. When the Verifier draft's public checks
        pass and the trusted caller supplies calibrated independent-evaluation
        material, the existing V1 pass transaction may finish the Run. No automatic
        loop runs here.
        The recovery dispatch lock serializes dispatch, receipt lookup,
        evaluation, and commit; a terminal Run observed after waiting is replayed
        without redispatch or reevaluation.
        """
        if max_read_bytes <= 0 or stop_event is None:
            raise ValueError("A positive read limit and stop event are required")
        # Same rule as every other creation path: verify the pinned manifest before the Run
        # exists, and refuse to continue a history that is pinned to another version.
        execution_sha256 = verify_optional_binding(
            manifest_path=execution_manifest, expected_sha256=execution_sha256,
            require_complete=True)
        if not workspaces_root.resolve(strict=True).is_dir() or not artifacts_root.resolve(strict=True).is_dir():
            raise ValueError("Workspace and artifact roots must be existing directories")
        if stop_event.is_set():
            raise GitExecutionCancelledError("Recovery stopped before rediagnosis creation")
        with self.session_factory() as session:
            run, created = RunCoordinator(session, self.lease_config).start_rediagnosis_run(
                task_id, self.worker_id, creation_idempotency_key,
                execution_sha256=execution_sha256)
            retry_id = run.id
            run_status = run.status
        if run_status != RunStatus.RUNNING:
            return {"outcome": "existing_rediagnosis", "run_id": retry_id, "handoff_id": None,
                    "resumed": not created, "continued": False}
        with workspace_operation_lock(
            self.session_factory.kw["bind"], retry_id, stop_event=stop_event,
            namespace=_RECOVERY_DISPATCH_LOCK_NAMESPACE,
        ):
            # A concurrent resume may have finished this Run while this caller
            # waited for the dispatch lock. Re-read the terminal state before
            # any start, dispatch, or evaluation work; a terminal Run must not
            # be re-dispatched or re-evaluated.
            with self.session_factory() as session, session.begin():
                run_status = session.scalar(select(Run.status).where(Run.id == retry_id))
                if run_status != RunStatus.RUNNING:
                    return {"outcome": "existing_rediagnosis", "run_id": retry_id,
                            "handoff_id": None, "resumed": not created, "continued": False}
                _require_run_not_stopped(retry_id, stop_event)
                RunCoordinator(session, self.lease_config).assert_execution_right(
                    retry_id, self.worker_id)
                has_activity = session.scalar(select(exists().where(
                    AgentExecution.run_id == retry_id))) or session.scalar(
                    select(exists().where(Handoff.from_agent_execution_id.in_(
                        select(AgentExecution.id).where(AgentExecution.run_id == retry_id)))))
            if has_activity:
                directories = resolve_run_directories(
                    retry_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root)
                if not directories.workspace_root.is_dir() or not directories.artifact_root.is_dir():
                    raise ValueError("Interrupted recovery workspace is unavailable")
            else:
                directories = self._ensure_recovery_workspace(
                    retry_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                    stop_event=stop_event)
            # Commit the actual-start event before the first node can run. A crash
            # after this commit still counts; repeated resumes of this Run do not.
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).begin_rediagnosis_start(
                    retry_id, self.worker_id)
            result = self._resume_rediagnosis_locked(
                retry_id, directories=directories, supervisor_gateway=supervisor_gateway,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                max_read_bytes=max_read_bytes, stop_event=stop_event,
                verification_config=verification_config,
                coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )
            # Receipt lookup, evaluation, and formal commit stay under the same
            # recovery dispatch lock. The nested V1 workspace lock uses the
            # default namespace, so it cannot deadlock on this key.
            finalized = self._commit_rediagnosis_independent_evaluation(
                retry_id, result=result, directories=directories,
                verification_config=verification_config, material=independent_evaluation,
                max_diff_bytes=verifier_max_diff_bytes, max_patch_bytes=max_patch_bytes,
                stop_event=stop_event,
            )
            if finalized is not None:
                result = {**result, **finalized}
        outcome = result.get("outcome")
        if not created and outcome == "existing":
            outcome = "existing_rediagnosis"
        return {**result, "outcome": outcome, "run_id": retry_id, "resumed": not created,
                "continued": bool(result.get("resumed"))}

    def resume_recovery(
        self, task_id: UUID, *, creation_idempotency_key: str, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
        independent_evaluation: IndependentEvaluationMaterial | None = None,
    ) -> dict:
        """Dispatch the recovery type recorded by the Task's latest formal failure."""
        with self.session_factory() as session:
            action = session.scalar(select(FailureRecord.recovery_action).join(
                Run, FailureRecord.run_id == Run.id).where(
                    Run.task_id == task_id, Run.status == RunStatus.FAILED,
                ).order_by(Run.attempt_number.desc()).limit(1))
        if action == "REDIAGNOSE":
            return self.resume_rediagnosis_recovery(
                task_id, creation_idempotency_key=creation_idempotency_key,
                workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes, stop_event=stop_event,
                verification_config=verification_config,
                coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
                independent_evaluation=independent_evaluation)
        if action == "REVISE_IMPLEMENTATION":
            return self.resume_implementation_recovery(
                task_id, creation_idempotency_key=creation_idempotency_key,
                workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway, max_read_bytes=max_read_bytes, stop_event=stop_event)
        raise RecoveryDecisionUnavailableError("No supported recovery action is available")

    def classify_and_finalize_implementation_failure(
        self, run_id: UUID, *, verifier_execution_id: UUID, draft_artifact_id: UUID,
        directories: RunDirectories, verification_config: VerificationConfig, model_gateway: ModelGateway,
        max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
        independent_artifact_id: UUID | None = None,
    ) -> dict:
        """Classify and apply the currently supported implementation-failure recovery path."""
        supervisor_id = self.classify_verification_failure(run_id, verifier_execution_id=verifier_execution_id,
            draft_artifact_id=draft_artifact_id, directories=directories, model_gateway=model_gateway,
            max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes, stop_event=stop_event,
            independent_artifact_id=independent_artifact_id)
        with self.session_factory() as session:
            verification_id = RunCoordinator(session, self.lease_config).finalize_implementation_failure(
                run_id, self.worker_id, supervisor_execution_id=supervisor_id, verification_config=verification_config,
                workspace_root=directories.workspace_root, artifact_root=directories.artifact_root,
                max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes, stop_event=stop_event)
        return {"supervisor_execution_id": supervisor_id, "verification_id": verification_id}

    def classify_and_finalize_unknown_failure(
        self, run_id: UUID, *, verifier_execution_id: UUID, draft_artifact_id: UUID,
        directories: RunDirectories, verification_config: VerificationConfig, model_gateway: ModelGateway,
        max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
        independent_artifact_id: UUID | None = None,
    ) -> dict:
        """Classify and commit the UNKNOWN_FAILURE decision without starting a new Run."""
        supervisor_id = self.classify_verification_failure(run_id, verifier_execution_id=verifier_execution_id,
            draft_artifact_id=draft_artifact_id, directories=directories, model_gateway=model_gateway,
            max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes, stop_event=stop_event,
            independent_artifact_id=independent_artifact_id)
        with self.session_factory() as session:
            result = RunCoordinator(session, self.lease_config).finalize_unknown_failure(
                run_id, self.worker_id, supervisor_execution_id=supervisor_id,
                verification_config=verification_config, workspace_root=directories.workspace_root,
                artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes,
                max_patch_bytes=max_patch_bytes, stop_event=stop_event)
        return {"supervisor_execution_id": supervisor_id, **result}

    def continue_taken_over_run(
        self, run_id: UUID, *, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Continue an owned RUNNING Run from its committed state, creating nothing.

        This is the in-place counterpart to the recovery entries: it never creates a
        Run and never needs a creation key, because the Run under work already exists
        and its ownership was settled by a takeover. A rediagnosis Run still records
        its durable start marker first, so a taken-over Run cannot spend budget
        without the same accounting every other path uses.
        """
        with workspace_operation_lock(
            self.session_factory.kw["bind"], run_id, stop_event=stop_event,
            namespace=_RECOVERY_DISPATCH_LOCK_NAMESPACE,
        ):
            _require_run_not_stopped(run_id, stop_event=stop_event)
            with self.session_factory() as session, session.begin():
                RunCoordinator(session, self.lease_config).assert_execution_right(
                    run_id, self.worker_id)
            directories = resolve_run_directories(
                run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root)
            if not directories.workspace_root.is_dir() or not directories.artifact_root.is_dir():
                directories = self._ensure_recovery_workspace(
                    run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                    stop_event=stop_event)
            with self.session_factory() as session:
                recovery_action = session.scalar(select(Run.recovery_action).where(Run.id == run_id))
            if recovery_action == "REDIAGNOSE":
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).begin_rediagnosis_start(
                        run_id, self.worker_id)
            return self._resume_rediagnosis_locked(
                run_id, directories=directories, supervisor_gateway=supervisor_gateway,
                explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
                max_read_bytes=max_read_bytes, stop_event=stop_event,
                verification_config=verification_config, coder_gateway=coder_gateway,
                max_patch_bytes=max_patch_bytes, verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
            )

    def take_over_expired_run_and_resume(
        self, run_id: UUID, *, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
    ) -> dict:
        """Take over an expired Run and continue it only if no node was left running.

        A node the lost worker left RUNNING cannot be dispatched again: the schema
        allows one AgentExecution per Handoff, and every replay lookup resolves a
        committed step through that pair, so a second execution would make "which
        execution produced this evidence" ambiguous. Such a Run is reported as blocked
        and left to the closing path instead of being rewritten. A crash before
        dispatch leaves no execution behind, and that is the case this entry continues
        in place — never on the automatic path, which always closes.
        """
        with self.session_factory() as session:
            taken = RunCoordinator(session, self.lease_config).take_over_expired_run(
                run_id, self.worker_id, stop_event=stop_event)
        if taken["orphaned_executions"]:
            return {**taken, "outcome": "blocked_by_orphaned_nodes", "continued": None}
        continued = self.continue_taken_over_run(
            run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
            supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, max_read_bytes=max_read_bytes,
            stop_event=stop_event, verification_config=verification_config,
            coder_gateway=coder_gateway, max_patch_bytes=max_patch_bytes,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
        )
        return {**taken, "outcome": continued["outcome"], "continued": continued}

    def run_recovery_loop(
        self, *, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, recovery_max_read_bytes: int, stop_event: Event,
        poll_interval_seconds: float,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None,
        coder_max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
        independent_evaluation: IndependentEvaluationMaterial | None = None,
        on_result: Callable[[dict], None] | None = None,
    ) -> None:
        """Periodically reclaim one expired Run until stop_event is set.

        The caller explicitly runs this loop; errors propagate to the caller.
        Cancellation interrupts the wait, but not necessarily a running scan.
        """
        import math

        if stop_event is None:
            raise ValueError("stop_event is required")
        if on_result is not None and not callable(on_result):
            raise TypeError("on_result must be callable")

        if isinstance(poll_interval_seconds, bool) or not isinstance(
            poll_interval_seconds, (int, float)
        ):
            raise TypeError("poll_interval_seconds must be a finite positive number")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be a finite positive number")
        while not stop_event.is_set():
            result = self.reclaim_next_expired_run(
                workspaces_root=workspaces_root,
                artifacts_root=artifacts_root,
                supervisor_gateway=supervisor_gateway,
                explorer_gateway=explorer_gateway,
                planner_gateway=planner_gateway,
                recovery_max_read_bytes=recovery_max_read_bytes,
                stop_event=stop_event,
                verification_config=verification_config,
                coder_gateway=coder_gateway,
                coder_max_patch_bytes=coder_max_patch_bytes,
                verifier_gateway=verifier_gateway,
                verifier_max_diff_bytes=verifier_max_diff_bytes,
                independent_evaluation=independent_evaluation,
            )
            if on_result is not None:
                on_result(result)
            stop_event.wait(poll_interval_seconds)

    def reclaim_next_expired_run(
        self, *, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, recovery_max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None,
        coder_max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
        independent_evaluation: IndependentEvaluationMaterial | None = None,
    ) -> dict:
        """Find one expired or pending-expired Run, close it and, when permitted, start its recovery.

        The automatic path closes rather than takes over: a node left RUNNING by a
        lost worker cannot be trusted to be replayable, so continuing the same Run
        needs a caller that knows better. Both expired and pending-expired Runs are
        handled through the same close_expired_run_and_recover path using the
        expired-close:<run_id> creation key, which revalidates the Run on close and
        ensures reclaiming the same Run twice cannot start two recovery Runs.

        Expired Runs are looked up first on the current session. If none is found,
        a fresh session is used to look up a pending-expired recovery id. The
        outcome is idle only when both lookups return None.
        """
        with self.session_factory() as session:
            run_id = RunCoordinator(
                session, self.lease_config).find_next_expired_run_id()
        if run_id is None:
            with self.session_factory() as session:
                run_id = RunCoordinator(
                    session, self.lease_config).find_pending_expired_recovery_id()
        if run_id is None:
            return {"outcome": "idle", "run_id": None}
        reclaimed = self.close_expired_run_and_recover(
            run_id, creation_idempotency_key=f"expired-close:{run_id}",
            workspaces_root=workspaces_root, artifacts_root=artifacts_root,
            supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, recovery_max_read_bytes=recovery_max_read_bytes,
            stop_event=stop_event, verification_config=verification_config,
            coder_gateway=coder_gateway, coder_max_patch_bytes=coder_max_patch_bytes,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
            independent_evaluation=independent_evaluation,
        )
        return {**reclaimed, "run_id": run_id}

    def close_expired_run_and_recover(
        self, run_id: UUID, *, creation_idempotency_key: str, workspaces_root: Path,
        artifacts_root: Path, supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway,
        planner_gateway: PlannerModelGateway, recovery_max_read_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
        coder_gateway: ModelGateway | None = None,
        coder_max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None,
        verifier_max_diff_bytes: int | None = None,
        independent_evaluation: IndependentEvaluationMaterial | None = None,
    ) -> dict:
        """Close an expired Run and, when permitted, start its rediagnosis Run.

        The decision is derived from durable history inside the coordinator, never
        from this caller. Escalation returns without starting anything. A repeated
        call after the Run is already closed re-reads the committed FailureRecord
        instead of asking for a second decision, so repeating the same request
        cannot close one Run twice, and the recovery Run's creation key keeps the
        started attempt single.
        """
        closed = self._close_expired_run_once(run_id)
        if closed["recovery_action"] != "REDIAGNOSE":
            return {**closed, "outcome": "escalated", "rediagnosis": None}
        resumed = self.resume_rediagnosis_recovery(
            closed["task_id"], creation_idempotency_key=creation_idempotency_key,
            workspaces_root=workspaces_root, artifacts_root=artifacts_root,
            supervisor_gateway=supervisor_gateway, explorer_gateway=explorer_gateway,
            planner_gateway=planner_gateway, max_read_bytes=recovery_max_read_bytes,
            stop_event=stop_event, verification_config=verification_config,
            coder_gateway=coder_gateway, max_patch_bytes=coder_max_patch_bytes,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
            independent_evaluation=independent_evaluation,
        )
        return {**closed, "outcome": resumed["outcome"], "rediagnosis": resumed}

    def _close_expired_run_once(self, run_id: UUID) -> dict:
        """Derive the decision once; replay the committed record on a closed Run."""
        try:
            with self.session_factory() as session:
                return RunCoordinator(
                    session, self.lease_config).close_expired_run_with_derived_decision(run_id)
        except RunLeaseNotExpiredError:
            with self.session_factory() as session:
                run = session.get(Run, run_id)
                failures = session.scalars(select(FailureRecord).where(
                    FailureRecord.run_id == run_id)).all()
                verifications = session.scalars(select(Verification).where(
                    Verification.run_id == run_id)).all()
                if run is None or run.status != RunStatus.FAILED:
                    raise
                if len(failures) != 1 or len(verifications) != 1:
                    raise
                failure = failures[0]
                verification = verifications[0]
                if failure.category != FailureCategory.UNKNOWN_FAILURE:
                    raise
                if failure.signature != LEASE_EXPIRED_SIGNATURE:
                    raise
                if failure.recovery_action not in (
                        "REDIAGNOSE",
                        "ESCALATE_TO_HUMAN"):
                    raise
                if verification.result != VerificationResult.FAILED:
                    raise
                evidence = verification.evidence
                if not isinstance(evidence, dict):
                    raise
                if evidence.get("scope") != "no_verification":
                    raise
                if evidence.get("reason") != "worker_lease_expired":
                    raise
                if evidence.get("run_id") != str(run_id):
                    raise
                task_id = run.task_id
                return {"outcome": "closed", "failure_id": failure.id,
                        "recovery_action": failure.recovery_action,
                        "category": failure.category.value, "signature": failure.signature,
                        "task_id": task_id, "policy_reasons": [], "resumed": True}

    def classify_and_recover_unknown_failure(
        self, run_id: UUID, *, verifier_execution_id: UUID, draft_artifact_id: UUID,
        directories: RunDirectories, verification_config: VerificationConfig, model_gateway: ModelGateway,
        max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
        independent_artifact_id: UUID | None = None, creation_idempotency_key: str,
        workspaces_root: Path, artifacts_root: Path, supervisor_gateway: ModelGateway,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        recovery_max_read_bytes: int, coder_gateway: ModelGateway | None = None,
        coder_max_patch_bytes: int | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_max_diff_bytes: int | None = None,
        independent_evaluation: IndependentEvaluationMaterial | None = None,
    ) -> dict:
        """Commit an UNKNOWN failure and, when permitted, actually start its rediagnosis Run."""
        finalized = self.classify_and_finalize_unknown_failure(run_id,
            verifier_execution_id=verifier_execution_id, draft_artifact_id=draft_artifact_id,
            directories=directories, verification_config=verification_config, model_gateway=model_gateway,
            max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes, stop_event=stop_event,
            independent_artifact_id=independent_artifact_id)
        if finalized["recovery_action"] != "REDIAGNOSE":
            return {**finalized, "outcome": "escalated", "rediagnosis": None}
        resumed = self.resume_rediagnosis_recovery(finalized["task_id"],
            creation_idempotency_key=creation_idempotency_key, workspaces_root=workspaces_root,
            artifacts_root=artifacts_root, supervisor_gateway=supervisor_gateway,
            explorer_gateway=explorer_gateway, planner_gateway=planner_gateway,
            max_read_bytes=recovery_max_read_bytes, stop_event=stop_event,
            verification_config=verification_config,
            coder_gateway=coder_gateway, max_patch_bytes=coder_max_patch_bytes,
            verifier_gateway=verifier_gateway,
            verifier_max_diff_bytes=verifier_max_diff_bytes,
            independent_evaluation=independent_evaluation)
        return {**finalized, "outcome": resumed["outcome"], "rediagnosis": resumed}

    def finalize_with_independent_evaluation(
        self, run_id: UUID, *, agent_execution_id: UUID, draft_artifact_id: UUID,
        directories: RunDirectories, verification_config: VerificationConfig, image: str,
        command: tuple[str, ...], cases: tuple[PrivateCase, ...],
        calibration_workspaces: tuple[Path, Path, str], max_diff_bytes: int,
        max_patch_bytes: int, timeout_seconds: float, stop_event: Event,
    ) -> dict:
        """Run a trusted calibrated suite and commit a passing result if all gates pass."""
        config = VerificationConfig.model_validate(verification_config.model_dump())
        if config.independent_suite_version is None:
            raise ValueError("An independent suite version is required")
        with self.session_factory() as session:
            receipt_id = RunCoordinator(session, self.lease_config).run_independent_evaluation(
                run_id, self.worker_id, agent_execution_id=agent_execution_id, draft_artifact_id=draft_artifact_id,
                workspace_root=directories.workspace_root, artifact_root=directories.artifact_root,
                image=image, command=command, cases=cases, suite_version=config.independent_suite_version,
                max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
                timeout_seconds=timeout_seconds, stop_event=stop_event, calibration_workspaces=calibration_workspaces)
        with self.session_factory() as session:
            receipt = load_independent_evaluation(session, run_id, agent_execution_id,
                                                  artifact_id=receipt_id, artifact_root=directories.artifact_root)
        if receipt.outcome != "passed":
            return {"outcome": receipt.outcome, "independent_artifact_id": receipt_id, "verification_id": None}
        with self.session_factory() as session:
            verification_id = RunCoordinator(session, self.lease_config).finalize_passing_verification(
                run_id, self.worker_id, agent_execution_id=agent_execution_id, draft_artifact_id=draft_artifact_id,
                independent_artifact_id=receipt_id, verification_config=config, workspace_root=directories.workspace_root,
                artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes,
                max_patch_bytes=max_patch_bytes, stop_event=stop_event)
        return {"outcome": "passed", "independent_artifact_id": receipt_id, "verification_id": verification_id}

    def prepare_run_directories(
        self, run_id: UUID, *, workspaces_root: Path, artifacts_root: Path,
    ) -> RunDirectories:
        """Create exclusive Run subdirectories under pre-existing configured roots."""
        directories = resolve_run_directories(
            run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
        )
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)

        _create_run_directories(directories)
        return directories

    def initialize_run_workspace(
        self, run_id: UUID, *, workspaces_root: Path, artifacts_root: Path,
        stop_event: Event | None = None,
    ) -> tuple[RunDirectories, str]:
        """Prepare the pinned checkout and recheck ownership before returning it."""
        directories = resolve_run_directories(
            run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
        )
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            repo, base_commit, policy_snapshot = session.execute(
                select(Task.repo, Task.base_commit, Task.policy_snapshot)
                .join(Run, Run.task_id == Task.id)
                .where(Run.id == run_id)
            ).one()
            if base_commit is None:
                raise TaskStateConflictError(f"Run {run_id} has no pinned Task base commit")
            timeout_seconds = PolicyConfig.model_validate(policy_snapshot).budget.tool_call_timeout_seconds

        _create_run_directories(directories)
        with workspace_operation_lock(
            self.session_factory.kw["bind"], run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ):
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).execute_worker_git_clone(
                    run_id, self.worker_id, repo=repo, workspace_root=directories.workspace_root,
                    artifact_root=directories.artifact_root, timeout_seconds=timeout_seconds, stop_event=stop_event,
                )
            with self.session_factory() as session:
                actual_commit = RunCoordinator(session, self.lease_config).execute_worker_git_checkout(
                    run_id, self.worker_id, workspace_root=directories.workspace_root, base_commit=base_commit,
                    timeout_seconds=timeout_seconds, stop_event=stop_event,
                )
        return directories, actual_commit

    def build_agent_read_file(
        self, run_id: UUID, *, agent_execution_id: UUID, directories: RunDirectories,
        max_bytes: int, stop_event: Event,
    ) -> Callable[[str], ReadFileReceipt]:
        """Bind a narrow read callback to one Agent execution and its Run directories."""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        def read_file(relative_path: str, offset: int | None = None,
                      limit: int | None = None) -> ReadFileReceipt:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                return RunCoordinator(session, self.lease_config).execute_read_file_with_receipt(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    workspace_root=directories.workspace_root, relative_path=relative_path,
                    max_bytes=max_bytes, artifact_root=directories.artifact_root, stop_event=stop_event,
                    start_line=offset, line_limit=limit,
                )

        return read_file

    def build_verifier_command(
        self, run_id: UUID, *, agent_execution_id: UUID, directories: RunDirectories,
        registry: Mapping[str, tuple[str, ...]], stop_event: Event,
        pytest_command_names: frozenset[str] = frozenset(),
        pythonpath_relative: str | None = None,
        require_unpatched: bool = False,
        verify_baseline: bool = False,
        public_tests_digest: Mapping[str, str] | None = None,
        registered_check_skill_version: str | None = None,
    ) -> Callable[[str], dict]:
        """Bind server-approved command names to one Verifier execution."""
        registered_commands = {name: tuple(command) for name, command in registry.items()}
        pytest_command_names = frozenset(pytest_command_names)
        if not pytest_command_names.issubset(registered_commands):
            raise ValueError("Pytest commands must be server-registered")

        def run_command(command_name: str) -> dict:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                call_id = RunCoordinator(session, self.lease_config).execute_verifier_command(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    workspace_root=directories.workspace_root, artifact_root=directories.artifact_root,
                    command_name=command_name, registry=registered_commands, stop_event=stop_event,
                    pytest_command_names=pytest_command_names,
                    pythonpath_relative=pythonpath_relative,
                    require_unpatched=require_unpatched,
                    verify_baseline=verify_baseline,
                    public_tests_sha256=public_tests_digest,
                    registered_check_skill_version=registered_check_skill_version,
                )
            with self.session_factory() as session:
                return load_verifier_command_evidence(session, run_id, agent_execution_id, (call_id,))[0]

        return run_command

    def execute_baseline_verifier(
        self, run_id: UUID, *, handoff_id: UUID, directories: RunDirectories,
        command_name: str, registry: Mapping[str, tuple[str, ...]], stop_event: Event,
        pythonpath_relative: str | None = None,
        verify_baseline: bool = False,
        public_tests_digest: Mapping[str, str] | None = None,
    ) -> dict:
        """Run one registered pytest baseline through an approved Verifier handoff."""
        if command_name not in registry:
            raise ValueError("Baseline command must be registered")
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            approved = session.scalar(select(AgentExecution.id).where(
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                AgentExecution.output["approved_handoff_id"].as_string() == str(handoff_id),
            ))
            if approved is None:
                raise ValueError("Baseline requires a committed Supervisor approval")
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.VERIFIER,
                agent_input={"purpose": "baseline", "command_name": command_name}, handoff_id=handoff_id,
            )
        try:
            from devflow_ai.skills import run_registered_check
            receipt = run_registered_check(version="2", worker=self, run_id=run_id,
                verifier_execution_id=execution_id, directories=directories, registry=registry,
                command_name=command_name, stop_event=stop_event, trusted_pytest_capture=True,
                trusted_pythonpath_relative=pythonpath_relative, trusted_require_unpatched=True,
                trusted_verify_baseline=verify_baseline, trusted_public_tests_digest=public_tests_digest)["receipt"]
            reports = [item for item in receipt["artifacts"] if item["kind"] == "pytest_junit"]
            valid = (receipt["status"], receipt["exit_code"]) in (("succeeded", 0), ("failed", 1)) and len(reports) == 1
            if valid:
                try:
                    with self.session_factory() as session:
                        load_pytest_report_artifact(
                            session, run_id, execution_id, tool_call_id=UUID(receipt["tool_call_id"]),
                            artifact_id=UUID(reports[0]["id"]), artifact_root=directories.artifact_root,
                            expected_command_name=command_name,
                        )
                except (VerificationEvidenceError, ValueError, KeyError, TypeError):
                    valid = False
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id,
                    status=AgentExecutionStatus.SUCCEEDED if valid else AgentExecutionStatus.FAILED,
                    output={"outcome": "BASELINE_RECORDED" if valid else "BASELINE_UNAVAILABLE",
                            "tool_call_id": receipt["tool_call_id"],
                            "report_artifact_id": reports[0]["id"] if valid else None}, llm_usage=None,
                )
            return {"execution_id": execution_id, "valid": valid, "receipt": receipt}
        except RunExecutionDeniedError:
            raise
        except Exception:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id,
                    status=AgentExecutionStatus.FAILED, output={"outcome": "BASELINE_ERROR"}, llm_usage=None,
                )
            raise

    def build_verifier_diff(
        self, run_id: UUID, *, agent_execution_id: UUID, directories: RunDirectories,
        max_bytes: int, stop_event: Event,
        diff_scope_skill_sha256: str | None = None,
        diff_scope_skill_version: str | None = None,
    ) -> Callable[[], dict]:
        """Bind a tracked-workspace diff reader to one Verifier execution."""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        def read_diff() -> dict:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                call_id = RunCoordinator(session, self.lease_config).execute_verifier_git_diff(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    workspace_root=directories.workspace_root, artifact_root=directories.artifact_root,
                    stop_event=stop_event,
                    diff_scope_skill_sha256=diff_scope_skill_sha256,
                    diff_scope_skill_version=diff_scope_skill_version,
                )
            with self.session_factory() as session:
                return load_verifier_workspace_diff(
                    session, run_id, agent_execution_id, tool_call_id=call_id,
                    artifact_root=directories.artifact_root, max_bytes=max_bytes,
                )

        return read_diff

    def review_verifier_semantics(
        self, run_id: UUID, *, agent_execution_id: UUID, directories: RunDirectories,
        selection: VerifierEvidenceSelection, model_gateway: ModelGateway,
        max_read_bytes: int, max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
    ) -> VerifierSemanticResult:
        """Review already-committed evidence without finishing the Agent or changing Run state."""
        if min(max_read_bytes, max_diff_bytes, max_patch_bytes) <= 0:
            raise ValueError("Verifier evidence limits must be positive")
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session, session.begin():
            policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
            agent_input, comparison = build_verifier_semantic_context(
                session, run_id, agent_execution_id, selection=selection,
                artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
            )
            max_reads = policy.budget.max_tool_calls_per_run
            max_repairs = policy.budget.max_output_repairs_per_agent_execution

        reader = self.build_agent_read_file(
            run_id, agent_execution_id=agent_execution_id, directories=directories,
            max_bytes=max_read_bytes, stop_event=stop_event,
        )

        def request_model(prompt: str):
            _require_run_not_stopped(run_id, stop_event)
            return model_gateway.request(run_id, agent_execution_id=agent_execution_id, prompt=prompt)

        def read_file(path: str, offset: int | None = None, limit: int | None = None) -> dict:
            _require_run_not_stopped(run_id, stop_event)
            return reader(path, offset, limit).model_dump(mode="json")

        def validate(review: SemanticReviewDraft) -> SemanticReviewDraft:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session, session.begin():
                RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
                return validate_semantic_review_citations(
                    session, run_id, agent_execution_id, diff_call_id=selection.diff_call_id,
                    artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes, review=review,
                )

        semantic_run = run_semantic_review(
            agent_input, request_model=request_model, read_file=read_file, validate_review=validate,
            max_reads=max_reads, max_repairs=max_repairs,
        )
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            draft_artifact_id = RunCoordinator(session, self.lease_config).record_verifier_semantic_draft(
                run_id, self.worker_id, agent_execution_id=agent_execution_id, selection=selection,
                review=semantic_run.review, read_call_ids=semantic_run.read_call_ids,
                artifact_root=directories.artifact_root, max_diff_bytes=max_diff_bytes,
                max_patch_bytes=max_patch_bytes, stop_event=stop_event,
            )
        return VerifierSemanticResult(
            comparison=comparison, review=semantic_run.review, diff_call_id=selection.diff_call_id,
            read_call_ids=semantic_run.read_call_ids, draft_artifact_id=draft_artifact_id,
            evidence_coverage=VerificationEvidenceCoverage.model_validate(agent_input["evidence_coverage"]),
        )

    def execute_explorer(
        self, run_id: UUID, *, directories: RunDirectories, agent_input: dict,
        max_bytes: int, stop_event: Event,
        explore: Callable[[dict, Callable[[str], ReadFileReceipt]], ExplorerResult] | None = None,
        model_gateway: ModelGateway | None = None,
        handoff_id: UUID | None = None,
        memory_recall: MemoryRecallRequest | None = None,
    ) -> UUID:
        """Register Explorer, run it with a bound reader, and commit its outcome."""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if (explore is None) == (model_gateway is None):
            raise ValueError("Explorer requires either a model gateway or a local callback")
        _require_run_not_stopped(run_id, stop_event)
        if memory_recall is not None:
            if not isinstance(memory_recall, MemoryRecallRequest) or MEMORY_RECALL_CONTEXT_KEY in agent_input:
                raise ValueError("Memory recall requires one explicit trusted selection")
            with self.session_factory() as session, session.begin():
                RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
                task_id, repository = session.execute(select(Run.task_id, Task.repo).join(
                    Task, Task.id == Run.task_id).where(Run.id == run_id)).one()
            with self.session_factory() as session:
                prepared = MemoryService(session).prepare_recall(
                    idempotency_key=memory_recall.idempotency_key, scope=RepositoryScope(repository),
                    task_id=task_id, run_id=run_id, memory_id=memory_recall.memory_id,
                    candidate_sha256=memory_recall.candidate_sha256, operator=memory_recall.operator,
                    purpose=memory_recall.purpose, target_environment=memory_recall.target_environment)
            candidate, use = prepared["candidate"], prepared["use"]
            agent_input = {**agent_input, MEMORY_RECALL_CONTEXT_KEY: {
                "use_id": use["id"], "candidate_id": candidate["id"],
                "candidate_sha256": candidate["candidate_sha256"],
                "content": candidate["content"], "content_status": candidate["content_status"],
                "source_task_id": candidate["task_id"], "source_run_id": candidate["run_id"],
                "source_verification_id": candidate["verification_id"],
                "source_task_review_id": candidate["task_review_id"],
                "source_artifact_id": candidate["artifact_id"],
                "source_artifact_sha256": candidate["artifact_sha256"],
                "source_state": candidate["source_state"], "review_id": use["review_id"],
                "applicable_base_commit": candidate["applicable_base_commit"],
                "applicable_environment": candidate["applicable_environment"],
                "valid_until": candidate["valid_until"],
            }}
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.EXPLORER, agent_input=agent_input, handoff_id=handoff_id,
            )
        read_file = self.build_agent_read_file(
            run_id, agent_execution_id=execution_id, directories=directories,
            max_bytes=max_bytes, stop_event=stop_event,
        )
        _require_run_not_stopped(run_id, stop_event)
        if model_gateway is None:
            result = explore(agent_input, read_file)
        else:
            def request_model(prompt):
                _require_run_not_stopped(run_id, stop_event)
                return model_gateway.request(run_id, agent_execution_id=execution_id, prompt=prompt)

            try:
                result = explore_one_file(agent_input, read_file, request_model)
            except RunExecutionDeniedError:
                raise
            except Exception:
                _require_run_not_stopped(run_id, stop_event)
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(
                        run_id, self.worker_id, agent_execution_id=execution_id,
                        status=AgentExecutionStatus.FAILED, output={"reason": "EXPLORER_EXECUTION_ERROR"},
                        llm_usage=None,
                    )
                raise
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            RunCoordinator(session, self.lease_config).finish_agent_execution(
                run_id, self.worker_id, agent_execution_id=execution_id,
                status=result.status, output=result.output, llm_usage=result.llm_usage,
                read_evidence=result.read_evidence,
            )
        return execution_id

    def execute_coder(
        self, run_id: UUID, *, handoff_id: UUID, directories: RunDirectories,
        model_gateway: ModelGateway, max_patch_bytes: int, max_read_bytes: int, stop_event: Event,
    ) -> UUID:
        """Run Coder once against a committed, approved plan."""
        if max_patch_bytes <= 0 or max_read_bytes <= 0:
            raise ValueError("Coder patch and read limits must be positive")
        _require_run_not_stopped(run_id, stop_event)
        try:
            with self.session_factory() as session, session.begin():
                policy = RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
                agent_input = validate_coder_baseline(session, run_id, handoff_id=handoff_id,
                                                      artifact_root=directories.artifact_root)
                repair_limit = policy.budget.max_output_repairs_per_agent_execution
        except BaselineUnavailableError as error:
            raise CoderContextUnavailableError("Verified baseline for the approved plan is unavailable") from error
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.CODER, agent_input=agent_input, handoff_id=handoff_id,
            )
        repairs = 0
        try:
            read_file = self.build_agent_read_file(
                run_id, agent_execution_id=execution_id, directories=directories,
                max_bytes=max_read_bytes, stop_event=stop_event,
            )
            reads = []
            patch_feedback = []
            patch_artifact = None
            while True:
                _require_run_not_stopped(run_id, stop_event)
                raw = model_gateway.request(
                    run_id, agent_execution_id=execution_id,
                    prompt=build_coder_prompt(agent_input, reads=reads, patch_feedback=patch_feedback),
                )
                if raw is None:
                    raise ValueError("Coder model budget unavailable")
                try:
                    draft = CoderDraft.model_validate_json(raw).root if isinstance(raw, str) else CoderDraft.model_validate(raw).root
                except ValidationError as error:
                    if repairs >= repair_limit:
                        raise
                    previous = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                    patch_feedback.append({"reason": "CODER_OUTPUT_INVALID",
                        "validation_errors": error.errors(include_input=False, include_context=False, include_url=False),
                        "previous_response": previous[:16000], "response_truncated": len(previous) > 16000})
                    repairs += 1
                    continue
                _require_run_not_stopped(run_id, stop_event)
                if isinstance(draft, CoderReadRequest):
                    receipt = read_file(draft.path, draft.offset, draft.limit)
                    if receipt.status != ToolCallStatus.SUCCEEDED:
                        draft = CoderBlocked(
                            outcome="BLOCKED", completed_work="Requested code evidence",
                            blocker=f"Could not read {draft.path}: {receipt.reason or receipt.status.value}",
                            evidence_refs=({"tool_call_id": str(receipt.tool_call_id), "path": draft.path},),
                            decision_required="Resolve the missing code evidence",
                            scope_impact="The requested implementation cannot be assessed",
                        )
                        output = draft.model_dump(mode="json")
                        break
                    reads.append({"path": draft.path, **receipt.model_dump(mode="json")})
                    continue
                if isinstance(draft, CoderBlocked):
                    output = draft.model_dump(mode="json")
                    break
                patch = draft.patch.encode("utf-8")
                if len(patch) > max_patch_bytes:
                    raise ValueError("Coder patch exceeds its size limit")
                artifact_id = uuid4()
                path, digest = write_artifact_file(
                    directories.artifact_root, artifact_id, BytesIO(patch), chunk_size=64 * 1024,
                )
                _require_run_not_stopped(run_id, stop_event)
                with self.session_factory() as session:
                    call_id = RunCoordinator(session, self.lease_config).execute_apply_patch(
                        run_id, self.worker_id, agent_execution_id=execution_id,
                        workspace_root=directories.workspace_root, patch=patch, stop_event=stop_event,
                        baseline_artifact_root=directories.artifact_root,
                    )
                with self.session_factory() as session:
                    call = session.get(ToolCall, call_id)
                    if call.status != ToolCallStatus.SUCCEEDED:
                        if call.status == ToolCallStatus.DENIED and call.reason == "PATCH_FORMAT_INVALID" and repairs < repair_limit:
                            patch_feedback.append({"previous_patch": draft.patch, "reason": call.reason,
                                                   "stderr": call.stderr_excerpt})
                            repairs += 1
                            continue
                        raise ValueError(f"Coder patch application was not successful: {call.status.value}")
                patch_artifact = ToolArtifactResult(
                    id=artifact_id, kind="patch", content_ref=str(path.absolute()), content_hash=digest,
                )
                output = {
                    "outcome": "PATCH_READY", "patch_artifact_id": str(artifact_id), "summary": draft.summary,
                }
                break
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id,
                    status=AgentExecutionStatus.SUCCEEDED, output=output,
                    llm_usage={"output_repair_attempts": repairs} if repairs else None,
                    patch_artifact=patch_artifact,
                )
        except RunExecutionDeniedError:
            raise
        except Exception as error:
            _require_run_not_stopped(run_id, stop_event)
            try:
                with self.session_factory() as session:
                    RunCoordinator(session, self.lease_config).finish_agent_execution(
                        run_id, self.worker_id, agent_execution_id=execution_id,
                        status=AgentExecutionStatus.FAILED, output={"reason": "CODER_EXECUTION_ERROR"},
                        llm_usage={"output_repair_attempts": repairs} if repairs else None,
                    )
            except Exception as record_error:
                error.add_note(f"Coder failure could not be committed: {type(record_error).__name__}")
            raise
        return execution_id

    def execute_planner(
        self, run_id: UUID, *, explorer_execution_id: UUID, stop_event: Event,
        model_gateway: PlannerModelGateway | None = None,
        plan: Callable[[dict[str, object]], PlannerRawOutput] | None = None,
        repair_under_budget: Callable[[PlannerRepairRequest], PlannerRawOutput | None] | None = None,
        handoff_id: UUID | None = None,
    ) -> UUID:
        """Execute Planner through its model gateway or a supplied local callback."""
        if plan is None and model_gateway is None:
            raise ValueError("Planner requires a model gateway or a local callback")
        if model_gateway is not None:
            if plan is not None or repair_under_budget is not None:
                raise ValueError("Planner model gateway cannot be combined with local callbacks")
            repair_under_budget = model_gateway.repair

        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            agent_input = ContextBuilder(session).build_planner_input(
                run_id, explorer_execution_id=explorer_execution_id,
            )
            if agent_input is not None and handoff_id is not None:
                handoff = session.scalar(
                    select(Handoff).join(AgentExecution, Handoff.from_agent_execution_id == AgentExecution.id)
                    .where(
                        Handoff.id == handoff_id,
                        Handoff.to_agent_role == AgentRole.PLANNER.value.lower(),
                        AgentExecution.run_id == run_id,
                    )
                )
                if handoff is None:
                    raise PlannerContextUnavailableError("Handoff does not match the selected Explorer execution")
                if handoff.from_agent_execution_id != explorer_execution_id:
                    source = session.get(AgentExecution, handoff.from_agent_execution_id)
                    references = handoff.context.get("evidence_refs", [])
                    if source.agent_role != AgentRole.CODER.value.lower() or not any(
                        isinstance(ref, dict) and ref.get("explorer_execution_id") == str(explorer_execution_id)
                        for ref in references
                    ):
                        raise PlannerContextUnavailableError("Handoff lacks the selected Explorer reference")
                agent_input["handoff"] = handoff.context
        if agent_input is None:
            raise PlannerContextUnavailableError(
                f"Explorer execution {explorer_execution_id} cannot supply Planner context for Run {run_id}"
            )

        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
                run_id, self.worker_id, role=AgentRole.PLANNER, agent_input=agent_input, handoff_id=handoff_id,
            )

        _require_run_not_stopped(run_id, stop_event)
        failure_reason = "PLANNER_EXECUTION_ERROR"
        llm_usage = None
        repair_attempts = 0
        try:
            if model_gateway is not None:
                raw_output = model_gateway.initial(
                    run_id, agent_execution_id=execution_id, agent_input=agent_input,
                )
                if raw_output is None:
                    failure_reason = "PLANNER_MODEL_BUDGET_UNAVAILABLE"
                    raise PlannerModelBudgetUnavailableError(
                        f"Run {run_id} cannot admit the initial Planner model request"
                    )
            else:
                raw_output = plan(agent_input)
            while True:
                failure_reason = "PLANNER_OUTPUT_INVALID"
                try:
                    validated = (
                        PlannerOutput.model_validate_json(raw_output)
                        if isinstance(raw_output, str) else PlannerOutput.model_validate(raw_output)
                    )
                    output = validated.model_dump(mode="json")
                    break
                except ValidationError as error:
                    if repair_under_budget is None:
                        raise
                    _require_run_not_stopped(run_id, stop_event)
                    with self.session_factory() as session, session.begin():
                        policy = RunCoordinator(session, self.lease_config).assert_execution_right(
                            run_id, self.worker_id,
                        )
                    if (repair_attempts >= policy.budget.max_output_repairs_per_agent_execution
                            or policy.budget.max_llm_tokens_per_run == 0):
                        raise
                    request = PlannerRepairRequest(
                        run_id=run_id, agent_execution_id=execution_id, agent_input=agent_input,
                        invalid_output=raw_output, validation_errors=tuple(
                            error.errors(include_input=False, include_context=False, include_url=False),
                        ),
                    )
                    _require_run_not_stopped(run_id, stop_event)
                    failure_reason = "PLANNER_EXECUTION_ERROR"
                    repaired_output = repair_under_budget(request)
                    if repaired_output is None:
                        failure_reason = "PLANNER_OUTPUT_INVALID"
                        raise
                    repair_attempts += 1
                    llm_usage = {"output_repair_attempts": repair_attempts}
                    raw_output = repaired_output
        except RunExecutionDeniedError:
            raise
        except Exception:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                RunCoordinator(session, self.lease_config).finish_agent_execution(
                    run_id, self.worker_id, agent_execution_id=execution_id,
                    status=AgentExecutionStatus.FAILED, output={"reason": failure_reason}, llm_usage=llm_usage,
                )
            raise

        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            RunCoordinator(session, self.lease_config).finish_agent_execution(
                run_id, self.worker_id, agent_execution_id=execution_id,
                status=AgentExecutionStatus.SUCCEEDED, output=output, llm_usage=llm_usage,
            )
        return execution_id

    def execute_run(
        self, run_id: UUID, *, workspaces_root: Path, artifacts_root: Path,
        heartbeat_config: HeartbeatConfig,
        process_run: Callable[[RunDirectories, str, Event], RunResult],
        external_stop_event: Event | None = None,
        execution_manifest: Path | None = None, execution_sha256: str | None = None,
    ) -> RunResult:
        """Run initialization and processing under one heartbeat for a committed claim.

        ``external_stop_event`` lets a caller bound the whole attempt, including the
        workspace initialization that happens before any node can observe a stop. The
        initialization receives a stop event linked to it, so the deadline is visible
        during the prepare step, and it is checked again afterwards. Outside calls that
        started before the deadline are not interrupted: a request to stop is not proof
        that execution has exited.

        **The execution boundary verifies the version itself.** ``execution_manifest`` and
        ``execution_sha256`` are verified here, before any workspace work, and the result must
        equal the digest the Run recorded when it was claimed. A caller cannot pass a bare
        digest and have it trusted; a Run started under a pinned version whose bytes are not
        re-verified is refused. Runs created without a pin (the V1 path) stay executable, and
        executing one under a verified manifest does not retroactively bind it.
        """
        verified = verify_optional_binding(
            manifest_path=execution_manifest, expected_sha256=execution_sha256,
            require_complete=True)
        self._require_run_execution_version(run_id, verified)
        if external_stop_event is not None and external_stop_event.is_set():
            raise GitExecutionCancelledError("Run deadline reached before initialization")
        with RunHeartbeat(
            self.session_factory, run_id, self.worker_id,
            heartbeat_config=heartbeat_config, lease_config=self.lease_config,
        ) as heartbeat:
            stop_event = heartbeat.execution_stop_event
            try:
                directories, actual_commit = self.initialize_run_workspace(
                    run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                    stop_event=(stop_event if external_stop_event is None
                                else LinkedStopEvent(stop_event, external_stop_event)),
                )
                heartbeat.raise_if_failed()
                if external_stop_event is not None and external_stop_event.is_set():
                    # Initialization outlived the attempt's budget. Stop here rather than
                    # start a stage whose result could never be timely.
                    stop_event.set()
                    raise GitExecutionCancelledError(
                        "Run deadline reached during initialization")
                return process_run(directories, actual_commit, stop_event)
            except GitExecutionCancelledError:
                heartbeat.raise_if_failed()
                raise


def main(argv: list[str] | None = None) -> int:
    defaults = LeaseConfig()
    parser = argparse.ArgumentParser(description="Claim one initial Run for inspection; no Agent execution.")
    parser.add_argument("--once", action="store_true", required=True, help="Claim at most one Run, then exit.")
    parser.add_argument("--worker-id", help="Worker identifier; defaults to a fresh UUID-based identifier.")
    parser.add_argument("--heartbeat-interval-seconds", type=int, default=defaults.heartbeat_interval_seconds)
    parser.add_argument("--lease-duration-seconds", type=int, default=defaults.lease_duration_seconds)
    args = parser.parse_args(argv)
    try:
        worker_id = _validate_worker_id(args.worker_id if args.worker_id is not None else f"worker-{uuid4()}")
        lease_config = LeaseConfig(
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            lease_duration_seconds=args.lease_duration_seconds,
        )
    except ValueError as error:
        failure = {
            "event": "worker_configuration_error", "error_type": type(error).__name__,
            "detail": "Invalid worker ID or lease configuration",
        }
        if isinstance(error, ValidationError):
            failure["errors"] = error.errors(include_input=False, include_context=False, include_url=False)
        else:
            failure["detail"] = str(error)
        print(json.dumps(failure), file=sys.stderr)
        return 2

    engine = None
    try:
        engine = create_db_engine()
        worker = Worker(create_session_factory(engine), worker_id, lease_config)
        claim = worker.claim_once()
    except Exception as error:
        # CLI boundary: report a nonzero failure without exposing DSNs, SQL parameters or secrets.
        print(json.dumps({
            "event": "worker_claim_failed", "worker_id": worker_id,
            "error_type": type(error).__name__, "detail": "Task claim failed",
        }), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    if claim is None:
        result = {"event": "worker_idle", "worker_id": worker_id}
    else:
        result = {"event": "worker_run_claimed", **claim.model_dump(mode="json")}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
