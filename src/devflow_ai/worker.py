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
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.artifacts import ToolArtifactResult, write_artifact_file
from devflow_ai.baseline import BaselineUnavailableError, validate_coder_baseline
from devflow_ai.coder import CoderBlocked, CoderDraft, CoderReadRequest, CoderOutput, PatchReady, build_coder_prompt
from devflow_ai.config import HeartbeatConfig, LeaseConfig
from devflow_ai.context import ContextBuilder
from devflow_ai.coordinator import HandoffContext, HandoffDecision, ReadFileReceipt, RunCoordinator, RunExecutionDeniedError
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, ToolCallStatus
from devflow_ai.explorer import ExplorerResult, explore_one_file
from devflow_ai.harness import GitExecutionCancelledError
from devflow_ai.heartbeat import RunHeartbeat
from devflow_ai.model_gateway import ModelGateway, PlannerModelGateway
from devflow_ai.orchestration import CommittedHandoff, run_handoff_cycle
from devflow_ai.models import AgentExecution, Handoff, Run, Task, ToolCall
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
from devflow_ai.verification_config import VerificationConfig
from devflow_ai.independent_evaluation import PrivateCase
from devflow_ai.verifier import load_independent_evaluation
import json
import sys
import argparse


RunResult = TypeVar("RunResult")


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
            agent_input = {**agent_input, "remaining_graph_steps": remaining_steps,
                           "remaining_tool_calls": remaining_tools, "explorer_read_allowed": explorer_allowed}
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
            elif not any(item.get("preview") is not None for item in evidence):
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
            baseline_id = session.scalar(select(AgentExecution.id).join(Handoff, AgentExecution.handoff_id == Handoff.id).where(
                AgentExecution.run_id == run_id, AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                AgentExecution.output["outcome"].as_string() == "BASELINE_RECORDED",
                Handoff.from_agent_execution_id == planner_execution_id,
            ).order_by(AgentExecution.created_at.desc()).limit(1))
            baseline_available = baseline_id is not None
            repair_limit = policy.budget.max_output_repairs_per_agent_execution
            agent_input = {**exploration, "planner_execution_id": str(planner_execution_id),
                "planner_output": planner_output.model_dump(mode="json"), "remaining_graph_steps": remaining_steps,
                "remaining_tool_calls": remaining_tools, "coder_patch_allowed": coder_allowed,
                "explorer_read_allowed": explorer_allowed, "verifier_command_allowed": verifier_allowed,
                "baseline_available": baseline_available}
        _require_run_not_stopped(run_id, stop_event)
        with self.session_factory() as session:
            execution_id = RunCoordinator(session, self.lease_config).start_agent_execution(
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
                elif decision.outcome == "CODE" and not any(
                    item.get("preview") is not None for item in agent_input["read_evidence"]
                ):
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

    def build_handoff_receivers(
        self, run_id: UUID, *, directories: RunDirectories, max_bytes: int, stop_event: Event,
        explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        coder_gateway: ModelGateway | None = None, max_patch_bytes: int | None = None,
        baseline_registry: Mapping[str, tuple[str, ...]] | None = None,
        baseline_command_name: str | None = None, baseline_pythonpath_relative: str | None = None,
        verifier_gateway: ModelGateway | None = None, verifier_target_tests: tuple[str, ...] = (),
        verifier_max_diff_bytes: int | None = None,
        verification_config: VerificationConfig | None = None,
    ) -> dict[AgentRole, Callable[[CommittedHandoff], None]]:
        """Bind committed handoffs to the existing Explorer and Planner entry points."""
        if verification_config is not None:
            if baseline_registry is not None or baseline_command_name is not None or baseline_pythonpath_relative is not None or verifier_target_tests:
                raise ValueError("Verification config cannot be combined with individual test settings")
            config = VerificationConfig.model_validate(verification_config.model_dump())
            with self.session_factory() as session, session.begin():
                RunCoordinator(session, self.lease_config).assert_execution_right(run_id, self.worker_id)
                task = session.scalar(select(Task).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
                if task is None or task.id != config.task_id or task.base_commit != config.base_commit:
                    raise ValueError("Verification config does not match the Run Task and base commit")
            baseline_registry = {config.command_name: config.command}
            baseline_command_name = config.command_name
            baseline_pythonpath_relative = config.pythonpath_relative
            verifier_target_tests = config.target_tests
        def receive_explorer(handoff: CommittedHandoff) -> None:
            with self.session_factory() as session:
                agent_input = ContextBuilder(session).build_issue_context(run_id)
                retry_context = ContextBuilder(session).build_retry_context(run_id)
                if retry_context is not None:
                    agent_input["retry_context"] = retry_context
            agent_input["handoff"] = handoff.context
            self.execute_explorer(
                run_id, directories=directories, max_bytes=max_bytes, stop_event=stop_event,
                agent_input=agent_input, model_gateway=explorer_gateway, handoff_id=handoff.id,
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
                        pythonpath_relative=baseline_pythonpath_relative, verification_config=verification_config)
                    return
                if source_role != AgentRole.PLANNER.value.lower():
                    raise ValueError("Baseline receiver requires a Planner handoff")
                self.execute_baseline_verifier(run_id, handoff_id=handoff.id, directories=directories,
                    command_name=baseline_command_name, registry=baseline_registry, stop_event=stop_event,
                    pythonpath_relative=baseline_pythonpath_relative, verify_baseline=verification_config is not None)
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
            receipt = self.build_verifier_command(run_id, agent_execution_id=execution_id, directories=directories,
                registry=registry, stop_event=stop_event, pytest_command_names=frozenset({command_name}),
                pythonpath_relative=pythonpath_relative)(command_name)
            reports = [item for item in receipt["artifacts"] if item["kind"] == "pytest_junit"]
            if len(reports) != 1:
                raise VerificationEvidenceError("Current pytest report is unavailable")
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
                    external_connections_declaration=(verification_config.external_connections
                                                      if verification_config is not None else "unknown"))
            return {"execution_id": execution_id, "readiness": readiness, "review": review}
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

    def resume_implementation_recovery(
        self, task_id: UUID, *, creation_idempotency_key: str, workspaces_root: Path, artifacts_root: Path,
        supervisor_gateway: ModelGateway, explorer_gateway: ModelGateway, planner_gateway: PlannerModelGateway,
        max_read_bytes: int, stop_event: Event,
    ) -> dict:
        """Start one approved retry and dispatch its first Supervisor-to-Explorer handoff."""
        if max_read_bytes <= 0 or stop_event is None:
            raise ValueError("A positive read limit and stop event are required")
        if not workspaces_root.resolve(strict=True).is_dir() or not artifacts_root.resolve(strict=True).is_dir():
            raise ValueError("Workspace and artifact roots must be existing directories")
        if stop_event.is_set():
            raise GitExecutionCancelledError("Recovery stopped before retry creation")
        with self.session_factory() as session:
            run, created = RunCoordinator(session, self.lease_config).start_retry_run(
                task_id, self.worker_id, creation_idempotency_key)
            retry_id = run.id
        if not created:
            return {"outcome": "existing_retry", "run_id": retry_id, "handoff_id": None}
        directories, _ = self.initialize_run_workspace(retry_id, workspaces_root=workspaces_root,
                                                       artifacts_root=artifacts_root, stop_event=stop_event)
        handoff_id = run_handoff_cycle(self.session_factory, retry_id, self.worker_id,
            supervise=self.build_initial_supervise(retry_id, model_gateway=supervisor_gateway, stop_event=stop_event),
            receivers=self.build_handoff_receivers(retry_id, directories=directories, max_bytes=max_read_bytes,
                stop_event=stop_event, explorer_gateway=explorer_gateway, planner_gateway=planner_gateway))
        return {"outcome": "dispatched" if handoff_id else "blocked", "run_id": retry_id,
                "handoff_id": handoff_id}

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

        def read_file(relative_path: str) -> ReadFileReceipt:
            _require_run_not_stopped(run_id, stop_event)
            with self.session_factory() as session:
                return RunCoordinator(session, self.lease_config).execute_read_file_with_receipt(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    workspace_root=directories.workspace_root, relative_path=relative_path,
                    max_bytes=max_bytes, artifact_root=directories.artifact_root, stop_event=stop_event,
                )

        return read_file

    def build_verifier_command(
        self, run_id: UUID, *, agent_execution_id: UUID, directories: RunDirectories,
        registry: Mapping[str, tuple[str, ...]], stop_event: Event,
        pytest_command_names: frozenset[str] = frozenset(),
        pythonpath_relative: str | None = None,
        require_unpatched: bool = False,
        verify_baseline: bool = False,
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
                )
            with self.session_factory() as session:
                return load_verifier_command_evidence(session, run_id, agent_execution_id, (call_id,))[0]

        return run_command

    def execute_baseline_verifier(
        self, run_id: UUID, *, handoff_id: UUID, directories: RunDirectories,
        command_name: str, registry: Mapping[str, tuple[str, ...]], stop_event: Event,
        pythonpath_relative: str | None = None,
        verify_baseline: bool = False,
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
            receipt = self.build_verifier_command(run_id, agent_execution_id=execution_id, directories=directories,
                registry=registry, stop_event=stop_event, pytest_command_names=frozenset({command_name}),
                pythonpath_relative=pythonpath_relative, require_unpatched=True, verify_baseline=verify_baseline)(command_name)
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

        def read_file(path: str) -> dict:
            _require_run_not_stopped(run_id, stop_event)
            return reader(path).model_dump(mode="json")

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
    ) -> UUID:
        """Register Explorer, run it with a bound reader, and commit its outcome."""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if (explore is None) == (model_gateway is None):
            raise ValueError("Explorer requires either a model gateway or a local callback")
        _require_run_not_stopped(run_id, stop_event)
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
                    receipt = read_file(draft.path)
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
    ) -> RunResult:
        """Run initialization and processing under one heartbeat for a committed claim."""
        with RunHeartbeat(
            self.session_factory, run_id, self.worker_id,
            heartbeat_config=heartbeat_config, lease_config=self.lease_config,
        ) as heartbeat:
            stop_event = heartbeat.execution_stop_event
            try:
                directories, actual_commit = self.initialize_run_workspace(
                    run_id, workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                    stop_event=stop_event,
                )
                heartbeat.raise_if_failed()
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
