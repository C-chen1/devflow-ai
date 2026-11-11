from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import os
import json
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal, TypeVar
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session, load_only
from devflow_ai.artifacts import ArtifactVerificationError, ToolArtifactResult, read_verified_artifact, write_artifact_file
from devflow_ai.baseline import BaselineUnavailableError, validate_coder_baseline
from devflow_ai.config import LeaseConfig
from devflow_ai.coder import CoderOutput, PatchReady
from devflow_ai.context import AgentReadEvidenceError, ReadEvidenceRef, resolve_read_evidence
from devflow_ai.database import get_database_time
from devflow_ai.enums import AgentExecutionStatus, AgentRole, FailureCategory, RunStatus, TaskStatus, ToolCallStatus, VerificationResult
from devflow_ai.harness import (
    GitCommandFailed,
    GitCommandTimedOut,
    GitExecutionCancelledError,
    GitHeadMismatchError,
    GitHeadOutputError,
    GitOutputPreview,
    WorkspaceFileNotFoundError,
    WorkspacePathError,
    PatchFormatError,
    WorkspaceUntrackedFilesError,
    check_tool_permission,
    checkout_base_commit,
    clone_repository,
    read_text_file,
    read_text_file_with_artifact,
    run_registered_command,
    inspect_workspace_diff,
    verify_baseline_workspace,
    validate_patch_paths,
    apply_validated_patch,
)
from devflow_ai.models import AgentExecution, Artifact, FailureRecord, Handoff, Run, Task, ToolCall, Verification
from devflow_ai.policy import PolicyConfig
from devflow_ai.planner import PlanReady, PlannerOutput
from devflow_ai.repositories import RunRepository, TaskRepository, ToolCallRepository
from devflow_ai.runtime_environment import PythonEnvironmentObservation
from devflow_ai.services import TaskNotFoundError, TaskStateConflictError
from devflow_ai.verifier import (
    SemanticReviewDraft, VerifierEvidenceSelection, VerificationEvidenceError, VerificationReadiness,
    build_verifier_semantic_context, load_semantic_review_draft, parse_git_numstat,
    validate_semantic_read_history, validate_semantic_review_citations, load_independent_evaluation,
    preliminary_public_outcome, build_failure_observations, load_pytest_report_artifact,
)
from devflow_ai.workspace_lock import WorkspaceLockReceipt, workspace_operation_lock
from devflow_ai.verification_config import VerificationConfig
from devflow_ai.independent_evaluation import PrivateCase, calibrate_stdio_suite, evaluate_stdio_candidate
from devflow_ai.supervisor import VerificationFailureProposal, resolve_verification_failure_evidence
from devflow_ai.recovery_policy import (RecoveryFacts, RecoveryHistory, assess_recovery_policy,
                                        public_failure_signature, semantic_failure_signature)

ToolOutput = TypeVar("ToolOutput")


class RunCreationConflictError(ValueError):
    """A Run creation key was reused with different creation parameters."""


class TaskClaimBusyError(RuntimeError):
    """A Task exists but another transaction currently holds its row lock."""


class RunExecutionDeniedError(RuntimeError):
    """The requested Run does not meet the execution-right conditions."""


class GraphStepBudgetExceededError(RuntimeError):
    """The Run has no remaining AgentExecution slots."""


class ToolCallBudgetExceededError(RuntimeError):
    """The Run has no remaining tool request slots."""


class RunLeaseNotExpiredError(RuntimeError):
    """Pending ToolCalls cannot be classified while the Run lease is valid."""


class RecoveryDecisionUnavailableError(RuntimeError):
    """Recovery cannot commit until Supervisor and Policy verification are available."""


class AgentExecutionStateConflictError(RuntimeError):
    """The AgentExecution is missing, belongs to another Run, or is already terminal."""


class AgentExecutionPendingToolsError(AgentExecutionStateConflictError):
    """Completion can be retried after the execution's tools have settled."""


class HandoffAlreadyStartedError(AgentExecutionStateConflictError):
    """The Handoff is already linked to a receiving execution."""

    def __init__(self, handoff_id: UUID, execution_id: UUID):
        super().__init__(f"Handoff {handoff_id} already started AgentExecution {execution_id}")
        self.execution_id = execution_id


class HandoffApprovalError(ValueError):
    """The proposed Handoff has no eligible approving execution or source."""


class CoderArtifactError(ValueError):
    """The Coder's patch reference is not owned by this execution."""


class HandoffContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    completed_work: str
    blocker: str | None
    evidence_refs: tuple[str | dict[str, JsonValue], ...]
    decision_required: str
    scope_impact: str
    recommendation: str | None = None


class HandoffDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_agent_execution_id: UUID
    to_agent_role: AgentRole
    context: HandoffContext


class FailureDecision(BaseModel):
    """A failure decision supplied by the controlling application."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: FailureCategory
    signature: str = Field(min_length=1, pattern=r"\S")
    recovery_action: Literal[
        "RETRY_SAME_STEP", "FIX_TOOL_INVOCATION", "PREPARE_ENVIRONMENT", "RE_EXPLORE",
        "REPLAN", "REVISE_IMPLEMENTATION", "RETRY_AGENT_OUTPUT", "FIND_COMPLIANT_ALTERNATIVE",
        "REDIAGNOSE", "ESCALATE_TO_HUMAN",
    ]


class ToolCallStateConflictError(RuntimeError):
    """The ToolCall is missing or no longer accepts a result."""


class ModelUsageConflictError(RuntimeError):
    """A model usage update does not match the committed reservation."""


class ModelTokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


class ModelRequestReservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: UUID
    max_output_tokens: int


class ToolCallResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ToolCallStatus
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    reason: str | None = None
    artifacts: tuple[ToolArtifactResult, ...] = ()


class ReadFileReceipt(BaseModel):
    """Committed read result that can be handed to an Agent."""

    model_config = ConfigDict(frozen=True)

    tool_call_id: UUID
    status: ToolCallStatus
    preview: str | None
    truncated: bool
    artifact_ids: tuple[UUID, ...]
    reason: str | None
    ends_with_newline: bool | None = None


class LeaseRenewal(BaseModel):
    """A detached receipt of a committed renewal, not permanent execution rights."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    worker_id: str
    last_heartbeat_at: datetime
    lease_expires_at: datetime


class RunCoordinator:
    def __init__(self, session: Session, lease_config: LeaseConfig | None = None):
        self.session = session
        self.lease_config = lease_config if lease_config is not None else LeaseConfig()
        self.task_repository = TaskRepository(session)
        self.run_repository = RunRepository(session)

    def start_initial_run(
        self, task_id: UUID, worker_id: str, creation_idempotency_key: str,
    ) -> tuple[Run, bool]:
        """Commit an initial Run or replay its result; replay grants no new lease."""
        for name, value in (
            ("worker_id", worker_id),
            ("creation_idempotency_key", creation_idempotency_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")

        with self.session.begin():
            run = self._get_replay(task_id, worker_id, creation_idempotency_key)
            created = False
            if run is None:
                task = self.task_repository.get_by_id_for_update(task_id)
                # Another creator may have committed between our first read and lock attempt.
                run = self._get_replay(task_id, worker_id, creation_idempotency_key)
                if run is None:
                    if task is None:
                        if self.task_repository.get_by_id(task_id, populate_existing=True) is None:
                            raise TaskNotFoundError(f"Task {task_id} not found")
                        raise TaskClaimBusyError(f"Task {task_id} is locked; retry the same creation request")
                    run = self._create_initial_run(task, worker_id, creation_idempotency_key)
                    created = True
        return run, created

    def claim_next_initial_run(self, worker_id: str) -> Run | None:
        """Claim new work; this is not a replay of an earlier claim-next call."""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-blank string")
        with self.session.begin():
            task = self.task_repository.get_next_eligible_for_update()
            run = None
            if task is not None:
                # This call produces a new creation intent for the selected Task.
                run = self._create_initial_run(task, worker_id, str(uuid4()))
        return run

    def start_retry_run(self, task_id: UUID, worker_id: str, creation_idempotency_key: str) -> tuple[Run, bool]:
        """Create one policy-authorized implementation retry or replay its existing receipt."""
        for value in (worker_id, creation_idempotency_key):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("worker_id and creation_idempotency_key must be nonblank")
        with self.session.begin():
            run = self._get_replay(task_id, worker_id, creation_idempotency_key)
            created = False
            if run is None:
                task = self.task_repository.get_by_id_for_update(task_id)
                run = self._get_replay(task_id, worker_id, creation_idempotency_key)
                if run is None:
                    if task is None:
                        if self.task_repository.get_by_id(task_id, populate_existing=True) is None:
                            raise TaskNotFoundError(f"Task {task_id} not found")
                        raise TaskClaimBusyError(f"Task {task_id} is locked; retry the same creation request")
                    if task.status != TaskStatus.RETRYING:
                        raise TaskStateConflictError("Retry Run requires a RETRYING Task")
                    policy = PolicyConfig.model_validate(task.policy_snapshot)
                    if policy.policy_version != task.policy_version:
                        raise ValueError("Task policy_version does not match its policy_snapshot")
                    runs = self.session.scalars(select(Run).where(Run.task_id == task_id)
                                                .order_by(Run.attempt_number)).all()
                    if (not runs or [item.attempt_number for item in runs] != list(range(1, len(runs) + 1))
                            or any(item.status != RunStatus.FAILED for item in runs)):
                        raise RecoveryDecisionUnavailableError("Retry history is incomplete or inconsistent")
                    last = runs[-1]
                    failures = self.session.scalars(select(FailureRecord).where(FailureRecord.run_id == last.id)).all()
                    verification = self.session.scalars(select(Verification).where(Verification.run_id == last.id,
                        Verification.result == VerificationResult.FAILED)).all()
                    if (len(failures) != 1 or len(verification) != 1
                            or failures[0].category != FailureCategory.IMPLEMENTATION_FAILURE
                            or failures[0].recovery_action != "REVISE_IMPLEMENTATION"):
                        raise RecoveryDecisionUnavailableError("A verified implementation recovery decision is required")
                    history = self._read_recovery_history(last, failures[0].signature)
                    prior_count = self.session.scalar(select(func.count(func.distinct(Run.id))).select_from(FailureRecord).join(
                        Run, FailureRecord.run_id == Run.id).where(Run.task_id == task_id, Run.status == RunStatus.FAILED,
                                                                   FailureRecord.signature == failures[0].signature))
                    if (history.failed_runs_without_record or len(runs) - 1 >= policy.budget.max_retries_per_task
                            or prior_count >= policy.budget.max_same_signature_occurrences
                            or "apply_patch" not in policy.permissions.allowed_tools_by_role[AgentRole.CODER]):
                        raise RecoveryDecisionUnavailableError("Retry policy or history no longer permits recovery")
                    database_time = get_database_time(self.session)
                    lease_seconds = min(self.lease_config.lease_duration_seconds, policy.budget.max_run_duration_seconds)
                    run = Run(task_id=task_id, attempt_number=len(runs) + 1, status=RunStatus.RUNNING,
                        creation_idempotency_key=creation_idempotency_key, worker_id=worker_id,
                        started_at=database_time, last_heartbeat_at=database_time,
                        lease_expires_at=database_time + timedelta(seconds=lease_seconds))
                    self.run_repository.add(run)
                    task.status = TaskStatus.RUNNING
                    created = True
            if run.attempt_number == 1:
                raise RunCreationConflictError("An initial Run creation key cannot identify a retry")
        return run, created

    def renew_lease(self, run_id: UUID, worker_id: str) -> LeaseRenewal:
        """Commit an owned, live lease renewal; database failures propagate unchanged."""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-blank string")

        with self.session.begin():
            hard_deadline, _ = self._lock_and_check_execution_right(run_id, worker_id)

            # Recheck time in the UPDATE too, in case execution paused after the first check.
            times = self.run_repository.renew_lease_if_owned(
                run_id, worker_id, hard_deadline=hard_deadline,
                lease_duration_seconds=self.lease_config.lease_duration_seconds,
            )
            if times is None:
                raise RunExecutionDeniedError(f"Run {run_id} no longer meets the renewal conditions")
            receipt = LeaseRenewal(
                run_id=run_id, worker_id=worker_id,
                last_heartbeat_at=times[0].astimezone(timezone.utc),
                lease_expires_at=times[1].astimezone(timezone.utc),
            )
        # A flush or commit failure must escape instead of returning a success receipt.
        return receipt

    def start_agent_execution(
        self, run_id: UUID, worker_id: str, *, role: AgentRole, agent_input: dict,
        handoff_id: UUID | None = None,
    ) -> UUID:
        """Commit one RUNNING AgentExecution and return its ID."""
        role = AgentRole(role)
        if not isinstance(agent_input, dict):
            raise ValueError("agent_input must be an object")
        with self.session.begin():
            _, policy = self._lock_and_check_execution_right(run_id, worker_id)
            if handoff_id is not None:
                matching_handoff = self.session.scalar(
                    select(Handoff.id)
                    .join(AgentExecution, Handoff.from_agent_execution_id == AgentExecution.id)
                    .where(
                        Handoff.id == handoff_id,
                        Handoff.to_agent_role == role.value.lower(),
                        AgentExecution.run_id == run_id,
                    ),
                )
                if matching_handoff is None:
                    raise HandoffApprovalError("Handoff must match the receiving Run and Agent role")
                existing_execution_id = self.session.scalar(
                    select(AgentExecution.id).where(AgentExecution.handoff_id == handoff_id),
                )
                if existing_execution_id is not None:
                    raise HandoffAlreadyStartedError(handoff_id, existing_execution_id)
            used_steps = self.session.scalar(
                select(func.count()).select_from(AgentExecution).where(AgentExecution.run_id == run_id),
            )
            if used_steps >= policy.budget.max_graph_steps_per_run:
                raise GraphStepBudgetExceededError(f"Run {run_id} has exhausted its graph step budget")
            execution = AgentExecution(
                run_id=run_id, agent_role=role.value.lower(),
                status=AgentExecutionStatus.RUNNING, input=agent_input, handoff_id=handoff_id,
            )
            self.session.add(execution)
            self.session.flush()
            execution_id = execution.id
        return execution_id

    def finish_agent_execution(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        status: AgentExecutionStatus, output: dict | None, llm_usage: dict | None,
        read_evidence: tuple[ReadEvidenceRef, ...] = (),
        approved_handoff: HandoffDecision | None = None,
        patch_artifact: ToolArtifactResult | None = None,
    ) -> UUID | None:
        """Commit one terminal outcome while the execution is still RUNNING."""
        status = AgentExecutionStatus(status)
        if status not in (AgentExecutionStatus.SUCCEEDED, AgentExecutionStatus.FAILED):
            raise ValueError("AgentExecution completion requires SUCCEEDED or FAILED")
        if output is not None and not isinstance(output, dict):
            raise ValueError("output must be an object or None")
        if output is not None and "read_evidence" in output:
            raise AgentReadEvidenceError("Submit read_evidence separately from output")
        if output is not None and "approved_handoff_id" in output:
            raise HandoffApprovalError("The approved Handoff ID is assigned by the coordinator")
        if llm_usage is not None and (not isinstance(llm_usage, dict) or "model_requests" in llm_usage):
            raise ValueError("llm_usage must be an object without the managed model_requests field or None")
        with self.session.begin():
            self.assert_execution_right(run_id, worker_id)
            execution = self.session.scalar(
                select(AgentExecution).where(
                    AgentExecution.id == agent_execution_id,
                    AgentExecution.run_id == run_id,
                    AgentExecution.status == AgentExecutionStatus.RUNNING,
                ).execution_options(populate_existing=True)
            )
            if execution is None:
                raise AgentExecutionStateConflictError(
                    f"AgentExecution {agent_execution_id} cannot be completed for Run {run_id}"
                )
            has_pending_tools = self.session.scalar(
                select(exists().where(
                    ToolCall.run_id == run_id,
                    ToolCall.agent_execution_id == agent_execution_id,
                    ToolCall.status.in_((ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING)),
                ))
            )
            if has_pending_tools:
                raise AgentExecutionPendingToolsError(
                    f"AgentExecution {agent_execution_id} still has pending tool calls"
                )
            if patch_artifact is not None:
                if (execution.agent_role != AgentRole.CODER.value.lower()
                        or status != AgentExecutionStatus.SUCCEEDED
                        or patch_artifact.kind != "patch"):
                    raise CoderArtifactError("A patch Artifact requires a successful Coder result")
                coder_result = CoderOutput.model_validate(output).root
                if not isinstance(coder_result, PatchReady) or coder_result.patch_artifact_id != patch_artifact.id:
                    raise CoderArtifactError("The patch Artifact must match the Coder result")
                self.session.add(Artifact(
                    id=patch_artifact.id, agent_execution_id=agent_execution_id, kind=patch_artifact.kind,
                    content_ref=patch_artifact.content_ref, content_hash=patch_artifact.content_hash,
                ))
            if (
                execution.agent_role == AgentRole.CODER.value.lower()
                and status == AgentExecutionStatus.SUCCEEDED
                and output is not None
                and output.get("outcome") in ("PATCH_READY", "BLOCKED")
            ):
                coder_output = CoderOutput.model_validate(output).root
                if isinstance(coder_output, PatchReady):
                    artifact = self.session.scalar(select(Artifact.id).where(
                        Artifact.id == coder_output.patch_artifact_id,
                        Artifact.agent_execution_id == agent_execution_id,
                        Artifact.kind == "patch",
                    ))
                    if artifact is None:
                        raise CoderArtifactError("Patch Artifact must belong to this Coder execution")
            if read_evidence:
                output = {
                    **(output or {}),
                    "read_evidence": resolve_read_evidence(self.session, run_id, agent_execution_id, read_evidence),
                }
            if execution.llm_usage and "model_requests" in execution.llm_usage:
                llm_usage = {**(llm_usage or {}), "model_requests": execution.llm_usage["model_requests"]}
            execution.status = status
            execution.output = output
            execution.llm_usage = llm_usage
            handoff_id = None
            if approved_handoff is not None:
                if execution.agent_role != AgentRole.SUPERVISOR.value.lower() or status != AgentExecutionStatus.SUCCEEDED:
                    raise HandoffApprovalError("Only a successful Supervisor execution can approve a Handoff")
                if approved_handoff.from_agent_execution_id == execution.id:
                    has_expert_execution = self.session.scalar(select(exists().where(
                        AgentExecution.run_id == run_id,
                        AgentExecution.agent_role != AgentRole.SUPERVISOR.value.lower(),
                    )))
                    has_handoff = self.session.scalar(select(exists().where(
                        Handoff.from_agent_execution_id == AgentExecution.id,
                        AgentExecution.run_id == run_id,
                    )))
                    source = execution.id if (
                        approved_handoff.to_agent_role == AgentRole.EXPLORER
                        and not has_expert_execution and not has_handoff
                    ) else None
                else:
                    source = self.session.scalar(select(AgentExecution.id).where(
                        AgentExecution.id == approved_handoff.from_agent_execution_id,
                        AgentExecution.run_id == run_id,
                        AgentExecution.status.in_((AgentExecutionStatus.SUCCEEDED, AgentExecutionStatus.FAILED)),
                    ))
                if source is None:
                    raise HandoffApprovalError("Handoff source must be a completed execution in the same Run")
                handoff = Handoff(
                    from_agent_execution_id=source,
                    to_agent_role=approved_handoff.to_agent_role.value.lower(),
                    context=approved_handoff.context.model_dump(mode="json"),
                )
                self.session.add(handoff)
                self.session.flush()
                handoff_id = handoff.id
                execution.output = {**(output or {}), "approved_handoff_id": str(handoff_id)}
        return handoff_id

    def reserve_model_request(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        estimated_input_tokens: int, max_output_tokens: int,
    ) -> ModelRequestReservation | None:
        """Commit a budget reservation before a model request can be sent."""
        if type(estimated_input_tokens) is not int or estimated_input_tokens < 0:
            raise ValueError("estimated input tokens must be a non-negative integer")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        with self.session.begin():
            policy = self.assert_execution_right(run_id, worker_id)
            executions = self.session.scalars(
                select(AgentExecution).where(AgentExecution.run_id == run_id)
                .options(load_only(AgentExecution.id, AgentExecution.status, AgentExecution.llm_usage))
                .execution_options(populate_existing=True),
            ).all()
            execution = next((item for item in executions if item.id == agent_execution_id), None)
            if execution is None or execution.status != AgentExecutionStatus.RUNNING:
                raise AgentExecutionStateConflictError("Model reservation requires a running AgentExecution")

            remaining = policy.budget.max_llm_tokens_per_run
            for item in executions:
                if item.llm_usage and "model_requests" not in item.llm_usage:
                    return None  # Legacy usage has no reliable Run-wide charge.
                for entry in (item.llm_usage or {}).get("model_requests", {}).values():
                    actual = entry["actual_usage"]
                    remaining -= entry["reserved_tokens"] if actual is None else (
                        actual["input_tokens"] + actual["output_tokens"]
                    )

            output_limit = min(max_output_tokens, remaining - estimated_input_tokens)
            if output_limit <= 0:
                return None
            reservation = ModelRequestReservation(request_id=uuid4(), max_output_tokens=output_limit)
            usage = execution.llm_usage or {}
            execution.llm_usage = {
                **usage, "model_requests": {
                    **usage.get("model_requests", {}),
                    str(reservation.request_id): {
                        "worker_id": worker_id,
                        "reserved_tokens": estimated_input_tokens + output_limit,
                        "actual_usage": None,
                    },
                },
            }
        return reservation

    def settle_model_request(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        request_id: UUID, actual_usage: ModelTokenUsage,
    ) -> None:
        """Reconcile an existing request without giving its worker execution rights."""
        reported = actual_usage.model_dump()
        with self.session.begin():
            if self.run_repository.get_by_id_for_update(run_id) is None:
                raise ModelUsageConflictError("Model request Run does not exist")
            execution = self.session.scalar(
                select(AgentExecution).where(
                    AgentExecution.run_id == run_id, AgentExecution.id == agent_execution_id,
                ).execution_options(populate_existing=True)
            )
            usage = execution.llm_usage if execution is not None else None
            requests = (usage or {}).get("model_requests", {})
            entry = requests.get(str(request_id))
            if entry is None or entry["worker_id"] != worker_id:
                raise ModelUsageConflictError("Model request does not match a reservation for this worker")
            if entry["actual_usage"] == reported:
                return
            if entry["actual_usage"] is not None:
                raise ModelUsageConflictError("Model request was already settled with different usage")
            execution.llm_usage = {
                **usage, "model_requests": {
                    **requests, str(request_id): {**entry, "actual_usage": reported},
                },
            }

    def execute_read_file(
        self, run_id: UUID, worker_id: str, *,
        agent_execution_id: UUID, workspace_root: Path, relative_path: str, max_bytes: int,
        artifact_root: Path | None = None, artifact_chunk_size: int = 64 * 1024,
        stop_event: Event | None = None,
    ) -> UUID:
        """Execute an audited read with the worker's trusted workspace and byte limit."""
        def execute(_: UUID) -> ToolCallResult:
            artifacts: tuple[ToolArtifactResult, ...] = ()
            try:
                if artifact_root is None:
                    text, truncated = read_text_file(workspace_root, relative_path, max_bytes=max_bytes)
                else:
                    artifact_id = uuid4()
                    text, artifact = read_text_file_with_artifact(
                        workspace_root, relative_path, max_bytes=max_bytes,
                        artifact_root=artifact_root, artifact_id=artifact_id,
                        chunk_size=artifact_chunk_size,
                    )
                    truncated = artifact is not None
                    if artifact is not None:
                        path, digest = artifact
                        artifacts = (ToolArtifactResult(
                            id=artifact_id, kind="tool_output",
                            content_ref=str(path.absolute()), content_hash=digest,
                        ),)
            except WorkspacePathError:
                return ToolCallResult(status=ToolCallStatus.DENIED, reason="WORKSPACE_PATH_DENIED")
            except WorkspaceFileNotFoundError:
                return ToolCallResult(status=ToolCallStatus.FAILED, reason="FILE_NOT_FOUND")
            except UnicodeDecodeError:
                return ToolCallResult(status=ToolCallStatus.FAILED, reason="INVALID_UTF8")
            if "\x00" in text:
                # PostgreSQL TEXT cannot store NUL even when the bytes are valid UTF-8.
                return ToolCallResult(
                    status=ToolCallStatus.FAILED, reason="UNSUPPORTED_TEXT_CONTENT", artifacts=artifacts,
                )
            return ToolCallResult(
                status=ToolCallStatus.SUCCEEDED,
                stdout_excerpt=text,
                reason="OUTPUT_TRUNCATED" if truncated else None,
                artifacts=artifacts,
            )

        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            return self.execute_agent_tool_call(
                run_id, worker_id, agent_execution_id=agent_execution_id,
                tool_name="read_file", args={"path": relative_path},
                registered_tools={"read_file"}, execute=execute,
                stop_event=stop_event, workspace_receipt=workspace_receipt,
            )

    def execute_read_file_with_receipt(
        self, run_id: UUID, worker_id: str, *,
        agent_execution_id: UUID, workspace_root: Path, relative_path: str, max_bytes: int,
        artifact_root: Path, artifact_chunk_size: int = 64 * 1024, stop_event: Event | None = None,
    ) -> ReadFileReceipt:
        """Read and audit the file, then build a receipt from committed records."""
        call_id = self.execute_read_file(
            run_id, worker_id, agent_execution_id=agent_execution_id,
            workspace_root=workspace_root, relative_path=relative_path, max_bytes=max_bytes,
            artifact_root=artifact_root, artifact_chunk_size=artifact_chunk_size, stop_event=stop_event,
        )
        with self.session.begin():
            call = self.session.execute(
                select(ToolCall.status, ToolCall.stdout_excerpt, ToolCall.reason).where(
                    ToolCall.id == call_id,
                    ToolCall.run_id == run_id,
                    ToolCall.agent_execution_id == agent_execution_id,
                )
            ).one()
            artifact_ids = tuple(self.session.scalars(
                select(Artifact.id).where(Artifact.tool_call_id == call_id).order_by(Artifact.id),
            ))
            receipt = ReadFileReceipt(
                tool_call_id=call_id, status=call.status, preview=call.stdout_excerpt,
                truncated=call.reason == "OUTPUT_TRUNCATED",
                artifact_ids=artifact_ids, reason=call.reason,
                ends_with_newline=(call.stdout_excerpt.endswith("\n") if call.status == ToolCallStatus.SUCCEEDED
                    and call.stdout_excerpt is not None and call.reason != "OUTPUT_TRUNCATED" else None),
            )
        return receipt

    def execute_worker_git_clone(
        self, run_id: UUID, worker_id: str, *, repo: str, workspace_root: Path,
        artifact_root: Path, timeout_seconds: int, stop_event: Event | None = None,
    ) -> UUID:
        """Audit a Worker-owned clone and propagate any recorded Git failure."""
        def clone() -> ToolCallResult:
            result = clone_repository(
                repo, workspace_root, timeout_seconds=timeout_seconds,
                stop_event=stop_event, artifact_root=artifact_root,
            )
            preview = result.preview
            reason = "OUTPUT_TRUNCATED" if preview.stdout_truncated or preview.stderr_truncated else None
            return self._git_preview_result(preview, ToolCallStatus.SUCCEEDED, result.returncode, reason)

        return self._execute_worker_git_operation(
            run_id, worker_id, tool_name="git_clone", args={"checkout": False}, operation=clone,
        )

    def execute_worker_git_checkout(
        self, run_id: UUID, worker_id: str, *, workspace_root: Path,
        base_commit: str, timeout_seconds: int, stop_event: Event | None = None,
    ) -> str:
        """Audit checkout and HEAD verification, then return the verified commit."""
        actual_commit: str

        def checkout() -> ToolCallResult:
            nonlocal actual_commit
            actual_commit = checkout_base_commit(
                workspace_root, base_commit, timeout_seconds=timeout_seconds, stop_event=stop_event,
            )
            return ToolCallResult(status=ToolCallStatus.SUCCEEDED, stdout_excerpt=actual_commit, exit_code=0)

        self._execute_worker_git_operation(
            run_id, worker_id, tool_name="git_checkout", args={"base_commit": base_commit},
            operation=checkout,
        )
        return actual_commit

    @staticmethod
    def _git_preview_result(
        preview: GitOutputPreview, status: ToolCallStatus, exit_code: int | None, reason: str | None,
    ) -> ToolCallResult:
        return ToolCallResult(
            status=status, exit_code=exit_code, reason=reason,
            stdout_excerpt=preview.stdout.decode("utf-8", errors="replace").replace("\x00", "\ufffd"),
            stderr_excerpt=preview.stderr.decode("utf-8", errors="replace").replace("\x00", "\ufffd"),
            artifacts=preview.artifacts,
        )

    def _execute_worker_git_operation(
        self, run_id: UUID, worker_id: str, *, tool_name: str, args: dict,
        operation: Callable[[], ToolCallResult],
    ) -> UUID:
        failure: GitCommandFailed | GitCommandTimedOut | GitHeadMismatchError | GitHeadOutputError | None = None

        def record_start(session: Session) -> UUID:
            return ToolCallRepository(session).add_requested(None, tool_name, args, run_id=run_id)

        def execute(_: UUID) -> ToolCallResult:
            nonlocal failure
            try:
                return operation()
            except GitCommandFailed as error:
                failure = error
                return self._git_preview_result(
                    error.preview, ToolCallStatus.FAILED, error.returncode, f"{tool_name.upper()}_FAILED",
                )
            except GitCommandTimedOut as error:
                failure = error
                return self._git_preview_result(
                    error.preview, ToolCallStatus.TIMED_OUT, None, f"{tool_name.upper()}_TIMEOUT",
                )
            except GitHeadMismatchError as error:
                failure = error
                return ToolCallResult(
                    status=ToolCallStatus.FAILED, exit_code=error.exit_code,
                    stdout_excerpt=error.actual_commit, reason="HEAD_MISMATCH",
                )
            except GitHeadOutputError as error:
                failure = error
                return self._git_preview_result(
                    error.preview, ToolCallStatus.FAILED, error.exit_code, error.reason,
                )

        call_id = self.execute_tool_call(
            run_id, worker_id, record_start=record_start, execute=execute,
            record_result=self._record_tool_result,
        )
        if failure is not None:
            raise failure
        return call_id

    def execute_verifier_command(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        workspace_root: Path, artifact_root: Path, command_name: str,
        registry: Mapping[str, tuple[str, ...]], stop_event: Event,
        pytest_command_names: frozenset[str] = frozenset(),
        pythonpath_relative: str | None = None,
        require_unpatched: bool = False,
        verify_baseline: bool = False,
    ) -> UUID:
        """Execute one server-registered command for a Verifier with an audited result."""
        if stop_event is None:
            raise ValueError("A stop event is required")
        registry = dict(registry)
        pytest_command_names = frozenset(pytest_command_names)

        def execute(call_id: UUID) -> ToolCallResult:
            with self.session.begin():
                policy = self.assert_execution_right(run_id, worker_id)
                execution = self.session.get(AgentExecution, agent_execution_id)
                if execution.agent_role != AgentRole.VERIFIER.value.lower():
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="VERIFIER_ROLE_REQUIRED")
                if require_unpatched and self.session.scalar(select(exists().where(
                    ToolCall.run_id == run_id, ToolCall.tool_name == "apply_patch",
                    ToolCall.status != ToolCallStatus.DENIED,
                ))):
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="BASELINE_AFTER_PATCH")
                timeout = policy.budget.tool_call_timeout_seconds
                base_commit = self.session.scalar(select(Task.base_commit).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
            if verify_baseline:
                if not require_unpatched or not base_commit:
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="BASELINE_COMMIT_UNAVAILABLE")
                try:
                    verify_baseline_workspace(workspace_root, base_commit, timeout_seconds=timeout,
                                              stop_event=stop_event, artifact_root=artifact_root)
                except (ValueError, OSError, GitCommandFailed, GitCommandTimedOut, WorkspaceUntrackedFilesError):
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="BASELINE_WORKSPACE_MISMATCH")
            if command_name not in registry:
                return ToolCallResult(status=ToolCallStatus.DENIED, reason="COMMAND_NOT_REGISTERED")
            capture_report = command_name in pytest_command_names
            report_dir = report_path = None
            active_registry = registry
            if capture_report:
                # On Windows, the managed Job waits for the entire process tree to finish.
                if os.name != "nt":
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="PYTEST_REPORT_CAPTURE_UNSUPPORTED")
                command = registry[command_name]
                if any(part.startswith(("--junitxml", "--junit-xml")) for part in command):
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="PYTEST_REPORT_PATH_CONFLICT")
                root = artifact_root.resolve(strict=True)
                report_dir = root / f"pytest-{call_id}"
                report_dir.mkdir(exist_ok=False)
                report_path = report_dir / "report.xml"
                active_registry = {command_name: (*command, f"--junitxml={report_path}")}
            try:
                environment_artifact = None
                deadline = monotonic() + timeout
                python_flags = ()
                if capture_report and command[1:4] == ("-B", "-m", "pytest"):
                    python_flags = ("-B",)
                python_pytest = capture_report and (
                    command[1:3] == ("-m", "pytest") or command[1:4] == ("-B", "-m", "pytest")
                )
                if python_pytest:
                    probe_source = Path(__file__).with_name("python_environment_probe.py").read_text(encoding="utf-8")
                    try:
                        probe = run_registered_command(
                            workspace_root, "python_environment", registry={
                                "python_environment": (command[0], *python_flags, "-c", probe_source),
                            }, timeout_seconds=timeout, stop_event=stop_event, max_stdout_bytes=2 * 1024 * 1024,
                            pythonpath_relative=pythonpath_relative,
                        )
                    except GitCommandTimedOut:
                        return ToolCallResult(status=ToolCallStatus.TIMED_OUT, reason="ENVIRONMENT_PROBE_TIMEOUT")
                    except GitCommandFailed:
                        return ToolCallResult(status=ToolCallStatus.FAILED, reason="ENVIRONMENT_PROBE_FAILED")
                    except WorkspacePathError:
                        return ToolCallResult(status=ToolCallStatus.DENIED, reason="PYTHON_SOURCE_PATH_INVALID")
                    if probe.preview.stdout_truncated:
                        return ToolCallResult(status=ToolCallStatus.FAILED, reason="ENVIRONMENT_PROBE_TOO_LARGE")
                    try:
                        environment = PythonEnvironmentObservation.model_validate_json(probe.stdout)
                    except ValidationError:
                        return ToolCallResult(status=ToolCallStatus.FAILED, reason="ENVIRONMENT_PROBE_INVALID")
                    environment_id = uuid4()
                    saved_path, digest = write_artifact_file(
                        root, environment_id, BytesIO(environment.model_dump_json().encode("utf-8")),
                        chunk_size=64 * 1024,
                    )
                    environment_artifact = ToolArtifactResult(
                        id=environment_id, kind="python_environment", content_ref=str(saved_path.absolute()),
                        content_hash=digest,
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return ToolCallResult(
                        status=ToolCallStatus.TIMED_OUT, reason="COMMAND_BUDGET_EXHAUSTED",
                        artifacts=(environment_artifact,) if environment_artifact else (),
                    )
                try:
                    result = run_registered_command(
                        workspace_root, command_name, registry=active_registry, timeout_seconds=remaining,
                        stop_event=stop_event, artifact_root=artifact_root, pythonpath_relative=pythonpath_relative,
                    )
                    reason = "OUTPUT_TRUNCATED" if result.preview.stdout_truncated or result.preview.stderr_truncated else None
                    output = self._git_preview_result(result.preview, ToolCallStatus.SUCCEEDED, result.returncode, reason)
                except GitCommandFailed as error:
                    output = self._git_preview_result(error.preview, ToolCallStatus.FAILED, error.returncode, "COMMAND_FAILED")
                except GitCommandTimedOut as error:
                    output = self._git_preview_result(error.preview, ToolCallStatus.TIMED_OUT, None, "COMMAND_TIMED_OUT")
                except WorkspacePathError:
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="PYTHON_SOURCE_PATH_INVALID")
                if environment_artifact:
                    output = output.model_copy(update={"artifacts": (*output.artifacts, environment_artifact)})
                if not capture_report or output.exit_code not in (0, 1):
                    return output
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Report rejected after stop signal")
                if report_path.is_symlink() or not report_path.is_file() or report_path.resolve().parent != report_dir:
                    return output.model_copy(update={"reason": "PYTEST_REPORT_MISSING_OR_INVALID"})
                with report_path.open("rb") as report_file:
                    report = report_file.read(5 * 1024 * 1024 + 1)
                if len(report) > 5 * 1024 * 1024:
                    return output.model_copy(update={"reason": "PYTEST_REPORT_TOO_LARGE"})
                report_id = uuid4()
                saved_path, digest = write_artifact_file(root, report_id, BytesIO(report), chunk_size=64 * 1024)
                artifact = ToolArtifactResult(
                    id=report_id, kind="pytest_junit", content_ref=str(saved_path.absolute()), content_hash=digest,
                )
                return output.model_copy(update={"artifacts": (*output.artifacts, artifact)})
            finally:
                if report_dir is not None:
                    try:
                        if not report_dir.is_symlink() and report_dir.resolve(strict=True) == report_dir:
                            report_path.unlink(missing_ok=True)
                            report_dir.rmdir()
                    except OSError:
                        # Do not recursively delete unexpected files produced by the command.
                        pass

        command = registry.get(command_name)
        command_hash = None
        if isinstance(command, tuple) and command and all(isinstance(part, str) and part for part in command):
            hashed_value = command if pythonpath_relative is None else {"command": command, "pythonpath": pythonpath_relative}
            command_hash = sha256(json.dumps(hashed_value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        args = {"command_name": command_name, "command_sha256": command_hash}
        if verify_baseline:
            args["baseline_workspace_checked"] = True
        if pythonpath_relative is not None:
            args["pythonpath"] = pythonpath_relative
        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            return self.execute_agent_tool_call(
                run_id, worker_id, agent_execution_id=agent_execution_id,
                tool_name="run_command", args=args,
                registered_tools={"run_command"}, execute=execute,
                stop_event=stop_event, workspace_receipt=workspace_receipt,
            )

    def execute_verifier_git_diff(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        workspace_root: Path, artifact_root: Path, stop_event: Event,
    ) -> UUID:
        """Audit a Verifier's observed tracked diff against the Task's pinned base."""
        if stop_event is None:
            raise ValueError("A stop event is required")
        with self.session.begin():
            self.assert_execution_right(run_id, worker_id)
            base_commit = self.session.scalar(select(Task.base_commit).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
        if not base_commit:
            raise TaskStateConflictError("The Task has no pinned base commit")

        def execute(_: UUID) -> ToolCallResult:
            with self.session.begin():
                policy = self.assert_execution_right(run_id, worker_id)
                execution = self.session.get(AgentExecution, agent_execution_id)
                actual_base = self.session.scalar(select(Task.base_commit).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
                if execution.agent_role != AgentRole.VERIFIER.value.lower():
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="VERIFIER_ROLE_REQUIRED")
                if actual_base != base_commit:
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="TASK_BASE_CHANGED")
                timeout = policy.budget.tool_call_timeout_seconds
            try:
                result = inspect_workspace_diff(
                    workspace_root, base_commit, timeout_seconds=timeout, stop_event=stop_event,
                    artifact_root=artifact_root,
                )
            except WorkspaceUntrackedFilesError as error:
                return self._git_preview_result(error.preview, ToolCallStatus.FAILED, None, "WORKSPACE_UNTRACKED_FILES")
            except GitCommandFailed as error:
                return self._git_preview_result(error.preview, ToolCallStatus.FAILED, error.returncode, "GIT_DIFF_FAILED")
            except GitCommandTimedOut as error:
                return self._git_preview_result(error.preview, ToolCallStatus.TIMED_OUT, None, "GIT_DIFF_TIMED_OUT")
            if result.numstat.preview.stdout_truncated:
                return self._git_preview_result(result.numstat.preview, ToolCallStatus.FAILED, 0, "GIT_NUMSTAT_TOO_LARGE")
            if result.diff.preview.stdout_truncated:
                return self._git_preview_result(result.diff.preview, ToolCallStatus.FAILED, 0, "GIT_DIFF_TOO_LARGE")
            try:
                stats = parse_git_numstat(result.numstat.stdout)
            except ValueError:
                return self._git_preview_result(result.numstat.preview, ToolCallStatus.FAILED, 0, "GIT_NUMSTAT_INVALID")
            if stats.added_lines is None or stats.deleted_lines is None:
                return self._git_preview_result(result.diff.preview, ToolCallStatus.FAILED, 0, "GIT_BINARY_DIFF_UNSUPPORTED")
            if not result.diff.stdout or not stats.file_count:
                return self._git_preview_result(result.diff.preview, ToolCallStatus.FAILED, 0, "WORKSPACE_DIFF_EMPTY")
            if stop_event.is_set():
                raise GitExecutionCancelledError("Diff rejected after stop signal")
            artifact_id = uuid4()
            path, digest = write_artifact_file(
                artifact_root, artifact_id, BytesIO(result.diff.stdout), chunk_size=64 * 1024,
            )
            artifact = ToolArtifactResult(
                id=artifact_id, kind="workspace_diff", content_ref=str(path.absolute()), content_hash=digest,
            )
            numstat_id = uuid4()
            numstat_path, numstat_digest = write_artifact_file(
                artifact_root, numstat_id, BytesIO(result.numstat.stdout), chunk_size=64 * 1024,
            )
            numstat_artifact = ToolArtifactResult(
                id=numstat_id, kind="workspace_diff_numstat", content_ref=str(numstat_path.absolute()),
                content_hash=numstat_digest,
            )
            output = self._git_preview_result(result.diff.preview, ToolCallStatus.SUCCEEDED, 0, None)
            return output.model_copy(update={"artifacts": (*output.artifacts, artifact, numstat_artifact)})

        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            return self.execute_agent_tool_call(
                run_id, worker_id, agent_execution_id=agent_execution_id,
                tool_name="git_diff", args={"base_commit": base_commit},
                registered_tools={"git_diff"}, execute=execute, stop_event=stop_event,
                workspace_receipt=workspace_receipt,
            )

    def record_verifier_semantic_draft(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        selection: VerifierEvidenceSelection, review: SemanticReviewDraft, read_call_ids: tuple[UUID, ...],
        artifact_root: Path, max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
    ) -> UUID:
        """Persist a revalidated intermediate review, without changing Agent, Run, or Task state."""
        if stop_event is None or min(max_diff_bytes, max_patch_bytes) <= 0:
            raise ValueError("A stop event and positive evidence limits are required")
        selection = VerifierEvidenceSelection.model_validate(selection.model_dump())
        review = SemanticReviewDraft.model_validate(review.model_dump())
        read_call_ids = tuple(UUID(str(value)) for value in read_call_ids)
        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            with self.session.begin():
                self.assert_execution_right(run_id, worker_id)
                context, comparison = build_verifier_semantic_context(
                    self.session, run_id, agent_execution_id, selection=selection,
                    artifact_root=artifact_root, max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
                )
                validate_semantic_read_history(
                    self.session, run_id, agent_execution_id, diff_call_id=selection.diff_call_id,
                    read_call_ids=read_call_ids, review=review,
                )
                validate_semantic_review_citations(
                    self.session, run_id, agent_execution_id, diff_call_id=selection.diff_call_id,
                    artifact_root=artifact_root, max_diff_bytes=max_diff_bytes, review=review,
                )
                payload = {
                    "record_type": "semantic_review_draft", "schema_version": 1,
                    "run_id": str(run_id), "agent_execution_id": str(agent_execution_id),
                    "selection": selection.model_dump(mode="json"), "review": review.model_dump(mode="json"),
                    "test_comparison": comparison.model_dump(mode="json"),
                    "read_call_ids": [str(call_id) for call_id in read_call_ids],
                    "patch_artifact_id": context["patch_artifact_id"],
                    "diff_artifact_id": context["workspace_diff"]["artifact_id"],
                    "diff_numstat_artifact_id": context["workspace_diff"]["numstat_artifact_id"],
                    "evidence_coverage": context["evidence_coverage"],
                }
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(data) > 512 * 1024:
                    raise ValueError("Semantic draft exceeds its size limit")
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Semantic draft rejected after stop signal")
                artifact_id = uuid4()
                path, digest = write_artifact_file(artifact_root, artifact_id, BytesIO(data), chunk_size=64 * 1024)
                self.assert_execution_right(run_id, worker_id)
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Semantic draft rejected after stop signal")
                workspace_receipt.assert_held(self.session)
                self.session.add(Artifact(
                    id=artifact_id, agent_execution_id=agent_execution_id, tool_call_id=None,
                    kind="semantic_review_draft", content_ref=str(path.absolute()), content_hash=digest,
                ))
                self.session.flush()
        return artifact_id

    def run_independent_evaluation(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID, draft_artifact_id: UUID,
        workspace_root: Path, artifact_root: Path, image: str, command: tuple[str, ...],
        cases: tuple[PrivateCase, ...], suite_version: str, max_diff_bytes: int,
        max_patch_bytes: int, timeout_seconds: float, stop_event: Event,
        calibration_workspaces: tuple[Path, Path, str] | None = None,
    ) -> UUID:
        """Run controller-supplied cases and save a preliminary, evidence-bound receipt."""
        if not suite_version.strip() or stop_event is None or min(max_diff_bytes, max_patch_bytes) <= 0:
            raise ValueError("Suite version, stop event and positive evidence limits are required")
        cases = tuple(cases)
        command = tuple(command)
        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            def verify_context():
                self.assert_execution_right(run_id, worker_id)
                draft = load_semantic_review_draft(self.session, run_id, agent_execution_id,
                    artifact_id=draft_artifact_id, artifact_root=artifact_root)
                context, comparison = build_verifier_semantic_context(self.session, run_id, agent_execution_id,
                    selection=draft.selection, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
                if (comparison != draft.test_comparison or context["patch_artifact_id"] != str(draft.patch_artifact_id)
                        or context["workspace_diff"]["artifact_id"] != str(draft.diff_artifact_id)
                        or context["workspace_diff"]["numstat_artifact_id"] != str(draft.diff_numstat_artifact_id)):
                    raise VerificationEvidenceError("Independent evaluation draft evidence changed")
                validate_semantic_read_history(self.session, run_id, agent_execution_id,
                    diff_call_id=draft.selection.diff_call_id, read_call_ids=draft.read_call_ids, review=draft.review)
                validate_semantic_review_citations(self.session, run_id, agent_execution_id,
                    diff_call_id=draft.selection.diff_call_id, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, review=draft.review)
                return draft, context

            def verify_workspace(context):
                actual = inspect_workspace_diff(workspace_root, context["workspace_diff"]["base_commit"],
                    timeout_seconds=timeout_seconds, stop_event=stop_event, artifact_root=artifact_root)
                if (actual.diff.preview.stdout_truncated or
                        sha256(actual.diff.stdout).hexdigest() != context["workspace_diff"]["content_hash"]):
                    raise VerificationEvidenceError("Independent evaluation workspace differs from reviewed diff")

            with self.session.begin():
                draft, context = verify_context()
                workspace_receipt.assert_held(self.session)
            verify_workspace(context)
            calibration = None
            if calibration_workspaces is not None:
                baseline_root, corrected_root, corrected_commit = calibration_workspaces
                if (baseline_root.resolve(strict=True) == workspace_root.resolve(strict=True)
                        or corrected_root.resolve(strict=True) == workspace_root.resolve(strict=True)):
                    raise ValueError("Calibration workspaces must be separate from the candidate")
                def check_calibration_workspaces():
                    verify_baseline_workspace(baseline_root, context["workspace_diff"]["base_commit"],
                        timeout_seconds=timeout_seconds, stop_event=stop_event, artifact_root=artifact_root)
                    verify_baseline_workspace(corrected_root, corrected_commit,
                        timeout_seconds=timeout_seconds, stop_event=stop_event, artifact_root=artifact_root)
                check_calibration_workspaces()
                calibration = calibrate_stdio_suite(baseline_workspace=baseline_root, corrected_workspace=corrected_root,
                    image=image, command=command, cases=cases, timeout_seconds=timeout_seconds)
                check_calibration_workspaces()
                if calibration.outcome != "verified":
                    raise VerificationEvidenceError("Independent test suite calibration did not pass")
            result = evaluate_stdio_candidate(workspace=workspace_root, image=image, command=command,
                                              cases=cases, timeout_seconds=timeout_seconds)
            if calibration is not None and result.image_id != calibration.image_id:
                raise VerificationEvidenceError("Independent evaluation image differs from calibration")
            verify_workspace(context)
            with self.session.begin():
                current_draft, current_context = verify_context()
                if current_draft != draft or current_context["workspace_diff"]["content_hash"] != context["workspace_diff"]["content_hash"]:
                    raise VerificationEvidenceError("Independent evaluation evidence changed during execution")
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Independent evaluation stopped")
                workspace_receipt.assert_held(self.session)
                payload = {"record_type": "independent_evaluation", "schema_version": 1,
                    "run_id": str(run_id), "agent_execution_id": str(agent_execution_id),
                    "draft_artifact_id": str(draft_artifact_id), "patch_artifact_id": str(draft.patch_artifact_id),
                    "diff_artifact_id": str(draft.diff_artifact_id), "diff_sha256": context["workspace_diff"]["content_hash"],
                    "suite_version": suite_version, "outcome": result.outcome,
                    "planned_case_count": result.planned_case_count, "image_id": result.image_id,
                    "calibrated": calibration is not None,
                    "adapter_sha256": sha256(json.dumps(command, ensure_ascii=True,
                        separators=(",", ":")).encode("utf-8")).hexdigest()}
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                artifact_id = uuid4()
                path, digest = write_artifact_file(artifact_root, artifact_id, BytesIO(data), chunk_size=64 * 1024)
                self.session.add(Artifact(id=artifact_id, agent_execution_id=agent_execution_id, tool_call_id=None,
                    kind="independent_evaluation", content_ref=str(path.absolute()), content_hash=digest))
                self.session.flush()
        return artifact_id

    def assess_verification_readiness(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID, draft_artifact_id: UUID,
        artifact_root: Path, max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
        verification_config: VerificationConfig | None = None,
    ) -> VerificationReadiness:
        """Recheck an intermediate draft and report missing evidence without finalizing the Run."""
        if stop_event is None or min(max_diff_bytes, max_patch_bytes) <= 0:
            raise ValueError("A stop event and positive evidence limits are required")
        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            with self.session.begin():
                self.assert_execution_right(run_id, worker_id)
                draft = load_semantic_review_draft(
                    self.session, run_id, agent_execution_id, artifact_id=draft_artifact_id,
                    artifact_root=artifact_root,
                )
                if verification_config is not None:
                    config = VerificationConfig.model_validate(verification_config.model_dump())
                    task = self.session.scalar(select(Task).join(Run, Run.task_id == Task.id).where(Run.id == run_id))
                    call = self.session.get(ToolCall, draft.selection.current_call_id)
                    hashed_value = (config.command if config.pythonpath_relative is None else
                                    {"command": config.command, "pythonpath": config.pythonpath_relative})
                    digest = sha256(json.dumps(hashed_value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                    if (task is None or task.id != config.task_id or task.base_commit != config.base_commit
                            or draft.selection.command_name != config.command_name
                            or draft.selection.target_tests != config.target_tests or call is None
                            or call.args.get("command_sha256") != digest):
                        raise VerificationEvidenceError("Verification config does not match the recorded evidence")
                context, comparison = build_verifier_semantic_context(
                    self.session, run_id, agent_execution_id, selection=draft.selection,
                    artifact_root=artifact_root, max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes,
                )
                if (comparison != draft.test_comparison or context["patch_artifact_id"] != str(draft.patch_artifact_id)
                        or context["workspace_diff"]["artifact_id"] != str(draft.diff_artifact_id)
                        or context["workspace_diff"]["numstat_artifact_id"] != str(draft.diff_numstat_artifact_id)):
                    raise VerificationEvidenceError("Semantic draft no longer matches committed evidence")
                validate_semantic_read_history(
                    self.session, run_id, agent_execution_id, diff_call_id=draft.selection.diff_call_id,
                    read_call_ids=draft.read_call_ids, review=draft.review,
                )
                validate_semantic_review_citations(
                    self.session, run_id, agent_execution_id, diff_call_id=draft.selection.diff_call_id,
                    artifact_root=artifact_root, max_diff_bytes=max_diff_bytes, review=draft.review,
                )
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Verification readiness check was stopped")
                self.assert_execution_right(run_id, worker_id)
                workspace_receipt.assert_held(self.session)
                missing = ["external_connection_provenance", "baseline_workspace_attestation", "test_integrity"]
                if verification_config is not None and config.external_connections == "none":
                    missing.remove("external_connection_provenance")
                baseline_call = self.session.get(ToolCall, draft.selection.baseline_call_id)
                if baseline_call is not None and baseline_call.args.get("baseline_workspace_checked") is True:
                    missing.remove("baseline_workspace_attestation")
                if draft.review.result.value == "unavailable":
                    missing.append("semantic_review")
                if comparison.target_result.value == "unavailable":
                    missing.append("target_test_result")
                if comparison.regression_result.value == "unavailable":
                    missing.append("regression_result")
                return VerificationReadiness(missing_evidence=tuple(missing))

    def observe_recovery_history(self, run_id: UUID, worker_id: str, *, signature: str) -> RecoveryHistory:
        """Read Task-scoped counts without granting permission to retry."""
        if not isinstance(signature, str) or not signature.strip():
            raise ValueError("A nonblank failure signature is required")
        with self.session.begin():
            self.assert_execution_right(run_id, worker_id)
            run = self.session.get(Run, run_id)
            return self._read_recovery_history(run, signature)

    def _read_recovery_history(self, run: Run, signature: str) -> RecoveryHistory:
        run_count = self.session.scalar(select(func.count()).select_from(Run).where(Run.task_id == run.task_id))
        prior_matches = self.session.scalar(select(func.count(func.distinct(Run.id))).select_from(FailureRecord).join(
            Run, FailureRecord.run_id == Run.id).where(Run.task_id == run.task_id,
                Run.id != run.id, Run.status == RunStatus.FAILED, FailureRecord.signature == signature))
        has_record = exists().where(FailureRecord.run_id == Run.id)
        incomplete = self.session.scalar(select(func.count()).select_from(Run).where(
            Run.task_id == run.task_id, Run.status == RunStatus.FAILED, ~has_record))
        return RecoveryHistory(retries_started=max(0, run_count - 1),
            previous_signature_occurrences=prior_matches, failed_runs_without_record=incomplete)

    def prepare_recovery_evidence(self, run_id: UUID, worker_id: str, *, supervisor_execution_id: UUID,
        artifact_root: Path, max_diff_bytes: int, max_patch_bytes: int, stop_event: Event) -> dict:
        """Revalidate a committed classification and observe history without granting recovery."""
        with workspace_operation_lock(self.session.get_bind(), run_id, stop_event=stop_event,
                                      check_unsettled_mutations=True) as workspace_receipt:
            with self.session.begin():
                self.assert_execution_right(run_id, worker_id)
                execution = self.session.scalar(select(AgentExecution).where(
                    AgentExecution.id == supervisor_execution_id, AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                    AgentExecution.status == AgentExecutionStatus.SUCCEEDED))
                if execution is None or (execution.output or {}).get("outcome") != "FAILURE_CLASSIFICATION_PROPOSED":
                    raise VerificationEvidenceError("Committed failure classification is unavailable")
                try:
                    proposal = VerificationFailureProposal.model_validate({k: v for k, v in execution.output.items() if k != "outcome"})
                    data = execution.input
                    verifier_id, draft_id = UUID(data["verifier_execution_id"]), UUID(data["draft_artifact_id"])
                    independent_id = UUID(data["independent_artifact_id"]) if data.get("independent_artifact_id") else None
                except (ValueError, KeyError, TypeError, ValidationError):
                    raise VerificationEvidenceError("Failure classification is invalid") from None
                observations = build_failure_observations(self.session, run_id, verifier_id,
                    draft_artifact_id=draft_id, independent_artifact_id=independent_id, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
                if observations != data.get("observations"):
                    raise VerificationEvidenceError("Failure observations changed after classification")
                resolve_verification_failure_evidence(proposal, observations)
                signature = None
                history = None
                if proposal.evidence_refs == ("public_tests",):
                    draft = load_semantic_review_draft(self.session, run_id, verifier_id,
                                                       artifact_id=draft_id, artifact_root=artifact_root)
                    report = load_pytest_report_artifact(self.session, run_id, verifier_id,
                        tool_call_id=draft.selection.current_call_id, artifact_id=draft.selection.current_artifact_id,
                        artifact_root=artifact_root, expected_command_name=draft.selection.command_name)
                    signature = public_failure_signature(report, draft.test_comparison)
                    if signature is not None:
                        history = self._read_recovery_history(self.session.get(Run, run_id), signature)
                elif proposal.evidence_refs == ("semantic_review",):
                    draft = load_semantic_review_draft(self.session, run_id, verifier_id,
                                                       artifact_id=draft_id, artifact_root=artifact_root)
                    signature = semantic_failure_signature(draft.review)
                    if signature is not None:
                        history = self._read_recovery_history(self.session.get(Run, run_id), signature)
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Recovery observation stopped")
                self.assert_execution_right(run_id, worker_id)
                workspace_receipt.assert_held(self.session)
                return {"classification_id": str(supervisor_execution_id), "category": proposal.category.value,
                        "signature": signature, "history": history.model_dump(mode="json") if history else None}

    def commit_failure_classification(
        self, run_id: UUID, worker_id: str, *, supervisor_execution_id: UUID,
        proposal: VerificationFailureProposal, repairs: int, artifact_root: Path,
        max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
    ) -> None:
        """Commit a classification proposal only after its observations are revalidated."""
        proposal = VerificationFailureProposal.model_validate(proposal.model_dump())
        with workspace_operation_lock(self.session.get_bind(), run_id, stop_event=stop_event,
                                      check_unsettled_mutations=True) as workspace_receipt:
            with self.session.begin():
                self.assert_execution_right(run_id, worker_id)
                execution = self.session.scalar(select(AgentExecution).where(
                    AgentExecution.id == supervisor_execution_id, AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                    AgentExecution.status == AgentExecutionStatus.RUNNING))
                if execution is None:
                    raise AgentExecutionStateConflictError("Active Supervisor execution required")
                data = execution.input or {}
                try:
                    verifier_id = UUID(data["verifier_execution_id"])
                    draft_id = UUID(data["draft_artifact_id"])
                    independent_id = UUID(data["independent_artifact_id"]) if data.get("independent_artifact_id") else None
                except (ValueError, KeyError, TypeError):
                    raise VerificationEvidenceError("Supervisor input is invalid") from None
                observations = build_failure_observations(self.session, run_id, verifier_id,
                    draft_artifact_id=draft_id, independent_artifact_id=independent_id,
                    artifact_root=artifact_root, max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
                if observations != data.get("observations"):
                    raise VerificationEvidenceError("Failure observations changed during classification")
                resolve_verification_failure_evidence(proposal, observations)
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Failure classification stopped")
                self.assert_execution_right(run_id, worker_id)
                workspace_receipt.assert_held(self.session)
                usage = dict(execution.llm_usage or {})
                if repairs:
                    usage["output_repair_attempts"] = repairs
                execution.llm_usage = usage or None
                execution.output = {"outcome": "FAILURE_CLASSIFICATION_PROPOSED", **proposal.model_dump(mode="json")}
                execution.status = AgentExecutionStatus.SUCCEEDED

    def finalize_implementation_failure(
        self, run_id: UUID, worker_id: str, *, supervisor_execution_id: UUID,
        verification_config: VerificationConfig, workspace_root: Path, artifact_root: Path,
        max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
    ) -> UUID:
        """Commit a classified implementation failure and its initial recovery decision."""
        config = VerificationConfig.model_validate(verification_config.model_dump())
        if config.external_connections != "none" or stop_event is None or min(max_diff_bytes, max_patch_bytes) <= 0:
            raise VerificationEvidenceError("Task configuration or evidence limits are incomplete")
        with workspace_operation_lock(self.session.get_bind(), run_id, stop_event=stop_event,
                                      check_unsettled_mutations=True) as workspace_receipt:
            with self.session.begin():
                policy = self.assert_execution_right(run_id, worker_id)
                run = self.session.get(Run, run_id)
                task = self.session.scalar(select(Task).where(Task.id == run.task_id).with_for_update())
                if task is None or task.id != config.task_id or task.base_commit != config.base_commit or task.status != TaskStatus.RUNNING:
                    raise VerificationEvidenceError("Task or base does not match verification configuration")
                supervisor = self.session.scalar(select(AgentExecution).where(
                    AgentExecution.id == supervisor_execution_id, AgentExecution.run_id == run_id,
                    AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                    AgentExecution.status == AgentExecutionStatus.SUCCEEDED))
                if supervisor is None or (supervisor.output or {}).get("outcome") != "FAILURE_CLASSIFICATION_PROPOSED":
                    raise VerificationEvidenceError("Committed failure classification is unavailable")
                try:
                    proposal = VerificationFailureProposal.model_validate({k: v for k, v in supervisor.output.items() if k != "outcome"})
                    data = supervisor.input
                    verifier_id, draft_id = UUID(data["verifier_execution_id"]), UUID(data["draft_artifact_id"])
                    independent_id = UUID(data["independent_artifact_id"]) if data.get("independent_artifact_id") else None
                except (ValueError, KeyError, TypeError, ValidationError):
                    raise VerificationEvidenceError("Failure classification is invalid") from None
                if proposal.category != FailureCategory.IMPLEMENTATION_FAILURE:
                    raise RecoveryDecisionUnavailableError("Only implementation failure recovery is available")
                observations = build_failure_observations(self.session, run_id, verifier_id,
                    draft_artifact_id=draft_id, independent_artifact_id=independent_id, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
                if observations != data.get("observations"):
                    raise VerificationEvidenceError("Failure observations changed after classification")
                resolve_verification_failure_evidence(proposal, observations)
                draft = load_semantic_review_draft(self.session, run_id, verifier_id,
                    artifact_id=draft_id, artifact_root=artifact_root)
                context, comparison = build_verifier_semantic_context(self.session, run_id, verifier_id,
                    selection=draft.selection, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
                call = self.session.get(ToolCall, draft.selection.current_call_id)
                baseline_call = self.session.get(ToolCall, draft.selection.baseline_call_id)
                hashed = (config.command if config.pythonpath_relative is None else
                          {"command": config.command, "pythonpath": config.pythonpath_relative})
                digest = sha256(json.dumps(hashed, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if (draft.selection.command_name != config.command_name or draft.selection.target_tests != config.target_tests
                        or call is None or call.args.get("command_sha256") != digest or baseline_call is None
                        or baseline_call.args.get("baseline_workspace_checked") is not True
                        or preliminary_public_outcome(comparison, draft.review).value != "failed"):
                    raise VerificationEvidenceError("Configured tests, baseline or failure do not match")
                signature = None
                if proposal.evidence_refs == ("semantic_review",):
                    signature = semantic_failure_signature(draft.review)
                elif proposal.evidence_refs == ("public_tests",):
                    report = load_pytest_report_artifact(self.session, run_id, verifier_id,
                        tool_call_id=draft.selection.current_call_id, artifact_id=draft.selection.current_artifact_id,
                        artifact_root=artifact_root, expected_command_name=draft.selection.command_name)
                    signature = public_failure_signature(report, comparison)
                if signature is None:
                    raise RecoveryDecisionUnavailableError("A specific failure signature is unavailable")
                history = self._read_recovery_history(run, signature)
                limit_reached = (history.retries_started >= policy.budget.max_retries_per_task
                    or history.previous_signature_occurrences + 1 >= policy.budget.max_same_signature_occurrences)
                if not limit_reached and (history.failed_runs_without_record or history.previous_signature_occurrences):
                    raise RecoveryDecisionUnavailableError("Recovery history or a changed strategy needs review")
                assessment = assess_recovery_policy(policy.budget, RecoveryFacts(
                    category=proposal.category, retries_started=history.retries_started,
                    signature_occurrences=history.previous_signature_occurrences + 1, rediagnoses_started=None,
                    has_compliant_plan="apply_patch" in policy.permissions.allowed_tools_by_role[AgentRole.CODER],
                    has_new_information_or_strategy=history.previous_signature_occurrences == 0,
                    transient_retry_safe=False))
                action = "REVISE_IMPLEMENTATION" if assessment.decision == "allow_recovery" else "ESCALATE_TO_HUMAN"
                actual = inspect_workspace_diff(workspace_root, task.base_commit,
                    timeout_seconds=policy.budget.tool_call_timeout_seconds, stop_event=stop_event, artifact_root=artifact_root)
                if actual.diff.preview.stdout_truncated or sha256(actual.diff.stdout).hexdigest() != context["workspace_diff"]["content_hash"]:
                    raise VerificationEvidenceError("Workspace differs from reviewed diff")
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Failure finalization stopped")
                self.assert_execution_right(run_id, worker_id)
                workspace_receipt.assert_held(self.session)
                verifier = self.session.get(AgentExecution, verifier_id)
                pending = self.session.scalar(select(exists().where(ToolCall.run_id == run_id,
                    ToolCall.agent_execution_id == verifier_id,
                    ToolCall.status.in_((ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING)))))
                if pending:
                    raise AgentExecutionPendingToolsError("Verifier has pending tool calls")
                verification = Verification(run_id=run_id, result=VerificationResult.FAILED,
                    evidence={"scope": "trusted_v1", "draft_artifact_id": str(draft_id),
                        "supervisor_execution_id": str(supervisor_execution_id),
                        "independent_artifact_id": str(independent_id) if independent_id else None,
                        "selection": draft.selection.model_dump(mode="json"),
                        "test_comparison": comparison.model_dump(mode="json"),
                        "semantic_review": draft.review.model_dump(mode="json"),
                        "diff_statistics": context["workspace_diff"]["statistics"],
                        "policy_reasons": list(assessment.reasons)})
                self.session.add_all([verification, FailureRecord(run_id=run_id, category=proposal.category,
                    signature=signature, recovery_action=action)])
                self.session.flush()
                verifier.status = AgentExecutionStatus.SUCCEEDED
                verifier.output = {"outcome": "VERIFICATION_FAILED", "verification_id": str(verification.id)}
                run.status = RunStatus.FAILED
                run.finished_at = get_database_time(self.session)
                task.status = TaskStatus.RETRYING if action == "REVISE_IMPLEMENTATION" else TaskStatus.ESCALATED
                return verification.id

    def finalize_passing_verification(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID, draft_artifact_id: UUID,
        independent_artifact_id: UUID, verification_config: VerificationConfig, workspace_root: Path,
        artifact_root: Path, max_diff_bytes: int, max_patch_bytes: int, stop_event: Event,
    ) -> UUID:
        """Commit a passing V1 verification and state changes from revalidated evidence."""
        if stop_event is None or min(max_diff_bytes, max_patch_bytes) <= 0:
            raise ValueError("A stop event and positive evidence limits are required")
        config = VerificationConfig.model_validate(verification_config.model_dump())
        if config.external_connections != "none" or config.independent_suite_version is None:
            raise VerificationEvidenceError("Verified task configuration is incomplete")
        with workspace_operation_lock(self.session.get_bind(), run_id, stop_event=stop_event,
                                      check_unsettled_mutations=True) as workspace_receipt:
            with self.session.begin():
                policy = self.assert_execution_right(run_id, worker_id)
                run = self.session.get(Run, run_id)
                task = self.session.get(Task, run.task_id)
                if task is None or task.id != config.task_id or task.base_commit != config.base_commit or task.status != TaskStatus.RUNNING:
                    raise VerificationEvidenceError("Task or base does not match verification configuration")
                draft = load_semantic_review_draft(self.session, run_id, agent_execution_id,
                    artifact_id=draft_artifact_id, artifact_root=artifact_root)
                receipt = load_independent_evaluation(self.session, run_id, agent_execution_id,
                    artifact_id=independent_artifact_id, artifact_root=artifact_root)
                if (receipt.draft_artifact_id != draft_artifact_id or not receipt.calibrated
                        or receipt.outcome != "passed" or receipt.suite_version != config.independent_suite_version):
                    raise VerificationEvidenceError("A matching calibrated independent pass is required")
                call = self.session.get(ToolCall, draft.selection.current_call_id)
                hashed = (config.command if config.pythonpath_relative is None else
                          {"command": config.command, "pythonpath": config.pythonpath_relative})
                digest = sha256(json.dumps(hashed, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                baseline_call = self.session.get(ToolCall, draft.selection.baseline_call_id)
                if (draft.selection.command_name != config.command_name or draft.selection.target_tests != config.target_tests
                        or call is None or call.args.get("command_sha256") != digest
                        or baseline_call is None or baseline_call.args.get("baseline_workspace_checked") is not True):
                    raise VerificationEvidenceError("Configured tests or checked baseline do not match")
                context, comparison = build_verifier_semantic_context(self.session, run_id, agent_execution_id,
                    selection=draft.selection, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, max_patch_bytes=max_patch_bytes)
                if (comparison != draft.test_comparison or context["patch_artifact_id"] != str(draft.patch_artifact_id)
                        or context["workspace_diff"]["artifact_id"] != str(draft.diff_artifact_id)
                        or context["workspace_diff"]["numstat_artifact_id"] != str(draft.diff_numstat_artifact_id)
                        or context["workspace_diff"]["content_hash"] != receipt.diff_sha256):
                    raise VerificationEvidenceError("Verification evidence no longer matches")
                if preliminary_public_outcome(comparison, draft.review).value != "passed":
                    raise VerificationEvidenceError("Public checks and semantic review must pass")
                validate_semantic_read_history(self.session, run_id, agent_execution_id,
                    diff_call_id=draft.selection.diff_call_id, read_call_ids=draft.read_call_ids, review=draft.review)
                validate_semantic_review_citations(self.session, run_id, agent_execution_id,
                    diff_call_id=draft.selection.diff_call_id, artifact_root=artifact_root,
                    max_diff_bytes=max_diff_bytes, review=draft.review)
                actual = inspect_workspace_diff(workspace_root, task.base_commit,
                    timeout_seconds=policy.budget.tool_call_timeout_seconds, stop_event=stop_event, artifact_root=artifact_root)
                if actual.diff.preview.stdout_truncated or sha256(actual.diff.stdout).hexdigest() != receipt.diff_sha256:
                    raise VerificationEvidenceError("Workspace differs from independently reviewed diff")
                if stop_event.is_set():
                    raise GitExecutionCancelledError("Verification finalization stopped")
                self.assert_execution_right(run_id, worker_id)
                workspace_receipt.assert_held(self.session)
                execution = self.session.get(AgentExecution, agent_execution_id)
                pending = self.session.scalar(select(exists().where(ToolCall.run_id == run_id,
                    ToolCall.agent_execution_id == agent_execution_id,
                    ToolCall.status.in_((ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING)))))
                if pending:
                    raise AgentExecutionPendingToolsError("Verifier has pending tool calls")
                verification = Verification(run_id=run_id, result=VerificationResult.PASSED,
                    evidence={"scope": "trusted_v1", "draft_artifact_id": str(draft_artifact_id),
                        "independent_artifact_id": str(independent_artifact_id),
                        "selection": draft.selection.model_dump(mode="json"),
                        "test_comparison": comparison.model_dump(mode="json"),
                        "semantic_review": draft.review.model_dump(mode="json"),
                        "diff_statistics": context["workspace_diff"]["statistics"],
                        "external_connections_declaration": config.external_connections,
                        "suite_version": receipt.suite_version,
                        "limitations": ["trusted_task_materials", "external_changes_not_fully_excluded"]})
                self.session.add(verification)
                self.session.flush()
                execution.status = AgentExecutionStatus.SUCCEEDED
                execution.output = {"outcome": "VERIFICATION_PASSED", "verification_id": str(verification.id)}
                run.status = RunStatus.PASSED
                run.finished_at = get_database_time(self.session)
                task.status = TaskStatus.AWAITING_REVIEW
                return verification.id

    def checkpoint_blocked_verifier(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        draft_artifact_id: UUID, missing_evidence: tuple[str, ...], preliminary_public_outcome: str,
        external_connections_declaration: Literal["unknown", "none", "required"] = "unknown",
    ) -> None:
        """Keep an incomplete Verifier resumable while recording its preliminary result."""
        if not missing_evidence or preliminary_public_outcome not in ("passed", "failed", "blocked"):
            raise ValueError("A blocked checkpoint requires missing evidence and a public outcome")
        with self.session.begin():
            self.assert_execution_right(run_id, worker_id)
            execution = self.session.scalar(select(AgentExecution).where(
                AgentExecution.id == agent_execution_id, AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.RUNNING,
            ))
            artifact = self.session.scalar(select(Artifact.id).where(
                Artifact.id == draft_artifact_id, Artifact.agent_execution_id == agent_execution_id,
                Artifact.kind == "semantic_review_draft",
            ))
            if execution is None or artifact is None:
                raise AgentExecutionStateConflictError("Active Verifier and its draft are required")
            execution.output = {"outcome": "VERIFICATION_BLOCKED", "draft_artifact_id": str(draft_artifact_id),
                                "missing_evidence": list(missing_evidence),
                                "preliminary_public_outcome": preliminary_public_outcome,
                                "external_connections_declaration": external_connections_declaration}

    def execute_patch_path_check(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        workspace_root: Path, patch: bytes, stop_event: Event | None = None,
    ) -> UUID:
        """Audit a patch path check using the Coder's committed Planner handoff."""
        return self._execute_patch_request(
            run_id, worker_id, agent_execution_id=agent_execution_id,
            workspace_root=workspace_root, patch=patch, stop_event=stop_event, apply=False,
        )

    def execute_apply_patch(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        workspace_root: Path, patch: bytes, stop_event: Event, baseline_artifact_root: Path | None = None,
    ) -> UUID:
        """Check and apply a patch under one workspace lock."""
        if stop_event is None:
            raise ValueError("Patch application requires a stop event")
        return self._execute_patch_request(
            run_id, worker_id, agent_execution_id=agent_execution_id,
            workspace_root=workspace_root, patch=patch, stop_event=stop_event, apply=True,
            baseline_artifact_root=baseline_artifact_root,
        )

    def execute_apply_patch_artifact(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        workspace_root: Path, artifact_root: Path, artifact_id: UUID, max_bytes: int, stop_event: Event,
    ) -> UUID:
        """Apply a Coder-owned patch Artifact after verifying its contents."""
        if stop_event is None or max_bytes <= 0:
            raise ValueError("A stop event and positive Artifact size limit are required")
        return self._execute_patch_request(
            run_id, worker_id, agent_execution_id=agent_execution_id, workspace_root=workspace_root,
            patch=None, stop_event=stop_event, apply=True, artifact_root=artifact_root,
            artifact_id=artifact_id, max_bytes=max_bytes,
        )

    def _execute_patch_request(
        self, run_id: UUID, worker_id: str, *, agent_execution_id: UUID,
        workspace_root: Path, patch: bytes | None, stop_event: Event | None, apply: bool,
        artifact_root: Path | None = None, artifact_id: UUID | None = None, max_bytes: int = 0,
        baseline_artifact_root: Path | None = None,
    ) -> UUID:
        if artifact_id is None and (not isinstance(patch, bytes) or not patch):
            raise ValueError("Patch must contain bytes")

        def check(call_id: UUID) -> ToolCallResult:
            patch_bytes = patch
            with self.session.begin():
                execution = self.session.get(AgentExecution, agent_execution_id)
                handoff = self.session.get(Handoff, execution.handoff_id) if execution.handoff_id else None
                source = self.session.get(AgentExecution, handoff.from_agent_execution_id) if handoff else None
                approval = None
                if handoff is not None:
                    approval = self.session.scalar(select(AgentExecution.id).where(
                        AgentExecution.run_id == run_id,
                        AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
                        AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
                        AgentExecution.output["approved_handoff_id"].as_string() == str(handoff.id),
                    ))
                if (execution.agent_role != AgentRole.CODER.value.lower() or handoff is None
                        or handoff.to_agent_role != AgentRole.CODER.value.lower()
                        or source is None or source.run_id != run_id
                        or source.agent_role != AgentRole.PLANNER.value.lower()
                        or source.status != AgentExecutionStatus.SUCCEEDED or approval is None):
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="APPROVED_PLAN_UNAVAILABLE")
                if apply and baseline_artifact_root is not None:
                    try:
                        validate_coder_baseline(self.session, run_id, handoff_id=handoff.id,
                            artifact_root=baseline_artifact_root, exclude_call_id=call_id)
                    except BaselineUnavailableError:
                        return ToolCallResult(status=ToolCallStatus.DENIED, reason="BASELINE_UNAVAILABLE")
                try:
                    plan = PlannerOutput.model_validate(source.output).root
                except ValidationError:
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="APPROVED_PLAN_INVALID")
                if not isinstance(plan, PlanReady):
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="APPROVED_PLAN_UNAVAILABLE")
                approved_paths = set(plan.plan.target_paths)
                if artifact_id is not None:
                    artifact = self.session.scalar(select(Artifact).where(
                        Artifact.id == artifact_id,
                        Artifact.agent_execution_id == agent_execution_id,
                        Artifact.kind == "patch",
                    ))
                    if artifact is None:
                        return ToolCallResult(status=ToolCallStatus.DENIED, reason="PATCH_ARTIFACT_NOT_OWNED")
                    artifact_ref, artifact_hash = artifact.content_ref, artifact.content_hash
            if artifact_id is not None:
                try:
                    patch_bytes = read_verified_artifact(
                        artifact_root, artifact_id, content_ref=artifact_ref,
                        content_hash=artifact_hash, max_bytes=max_bytes,
                    )
                except (ArtifactVerificationError, OSError):
                    return ToolCallResult(status=ToolCallStatus.DENIED, reason="PATCH_ARTIFACT_INVALID")
            with self.session.begin():
                run = self.session.get(Run, run_id)
                if run.attempt_number > 1:
                    previous = self.session.scalar(select(Run).where(Run.task_id == run.task_id,
                        Run.attempt_number == run.attempt_number - 1, Run.status == RunStatus.FAILED))
                    verifications = (self.session.scalars(select(Verification).where(
                        Verification.run_id == previous.id, Verification.result == VerificationResult.FAILED)).all()
                        if previous is not None else [])
                    try:
                        if len(verifications) != 1:
                            raise ValueError("Previous verification is unavailable")
                        previous_call_id = UUID(verifications[0].evidence["selection"]["patch_call_id"])
                        previous_call = self.session.get(ToolCall, previous_call_id)
                        previous_coder = self.session.get(AgentExecution, previous_call.agent_execution_id)
                        previous_artifact_id = UUID(previous_coder.output["patch_artifact_id"])
                        previous_artifact = self.session.get(Artifact, previous_artifact_id)
                        if (previous_call.run_id != previous.id or previous_call.tool_name != "apply_patch"
                                or previous_call.status != ToolCallStatus.SUCCEEDED
                                or previous_coder.run_id != previous.id or previous_coder.agent_role != AgentRole.CODER.value.lower()
                                or previous_artifact is None or previous_artifact.agent_execution_id != previous_coder.id
                                or previous_artifact.kind != "patch"):
                            raise ValueError("Previous patch evidence is invalid")
                    except (ValueError, KeyError, TypeError, AttributeError):
                        return ToolCallResult(status=ToolCallStatus.DENIED, reason="PRIOR_FAILURE_EVIDENCE_UNAVAILABLE")
                    if sha256(patch_bytes).hexdigest() == previous_artifact.content_hash:
                        return ToolCallResult(status=ToolCallStatus.DENIED, reason="UNCHANGED_FAILED_PATCH")
            try:
                paths = validate_patch_paths(
                    workspace_root, patch_bytes, approved_paths=approved_paths, stop_event=stop_event,
                )
            except PatchFormatError as error:
                return ToolCallResult(status=ToolCallStatus.DENIED, reason="PATCH_FORMAT_INVALID",
                    stderr_excerpt=error.stderr.decode("utf-8", errors="replace").replace("\x00", ""))
            except WorkspacePathError:
                return ToolCallResult(status=ToolCallStatus.DENIED, reason="PATCH_PATH_DENIED")
            if apply:
                with self.session.begin():
                    policy = self.assert_execution_right(run_id, worker_id)
                    timeout = policy.budget.tool_call_timeout_seconds
                try:
                    apply_validated_patch(
                        workspace_root, patch_bytes, timeout_seconds=timeout, stop_event=stop_event,
                    )
                except GitCommandFailed as error:
                    return ToolCallResult(
                        status=ToolCallStatus.FAILED, exit_code=error.returncode, reason="PATCH_APPLY_FAILED",
                        stdout_excerpt=error.preview.stdout.decode("utf-8", errors="replace").replace("\x00", ""),
                        stderr_excerpt=error.preview.stderr.decode("utf-8", errors="replace").replace("\x00", ""),
                    )
            return ToolCallResult(status=ToolCallStatus.SUCCEEDED, stdout_excerpt="\n".join(sorted(paths)))

        tool_name = "apply_patch" if apply else "patch_path_check"
        args = (
            {"artifact_id": str(artifact_id), "recount": True}
            if artifact_id is not None else {"patch_sha256": sha256(patch).hexdigest(), "recount": True}
        )
        with workspace_operation_lock(
            self.session.get_bind(), run_id, stop_event=stop_event, check_unsettled_mutations=True,
        ) as workspace_receipt:
            return self.execute_agent_tool_call(
                run_id, worker_id, agent_execution_id=agent_execution_id,
                tool_name=tool_name, args=args,
                registered_tools={tool_name}, execute=check, stop_event=stop_event,
                workspace_receipt=workspace_receipt,
            )

    def execute_agent_tool_call(
        self, run_id: UUID, worker_id: str, *,
        agent_execution_id: UUID, tool_name: str, args: dict,
        registered_tools: Collection[str],
        execute: Callable[[UUID], ToolCallResult],
        stop_event: Event | None = None,
        workspace_receipt: WorkspaceLockReceipt | None = None,
    ) -> UUID:
        """Register an owned Agent's tool request and persist its guarded result."""
        with self.session.begin():
            _, policy = self._lock_and_check_execution_right(run_id, worker_id)
            execution = self.session.get(
                AgentExecution, agent_execution_id, populate_existing=True, with_for_update=True,
            )
            if execution is None or execution.run_id != run_id:
                raise RunExecutionDeniedError(
                    f"Agent execution {agent_execution_id} does not belong to Run {run_id}"
                )
            used_calls = self.session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id),
            )
            if used_calls >= policy.budget.max_tool_calls_per_run:
                raise ToolCallBudgetExceededError(f"Run {run_id} has exhausted its tool call budget")
            call_id = ToolCallRepository(self.session).add_requested(
                agent_execution_id, tool_name, args, run_id=run_id,
            )
            if execution.status != AgentExecutionStatus.RUNNING:
                # Both writes commit together, so no REQUESTED row is visible for this denial.
                self._record_tool_result(
                    self.session, call_id,
                    ToolCallResult(status=ToolCallStatus.DENIED, reason="AGENT_EXECUTION_NOT_RUNNING"),
                    agent_execution_id=agent_execution_id,
                )
                return call_id
            agent_role = AgentRole(execution.agent_role.upper())

        def execute_with_permission(call_id: UUID) -> ToolCallResult:
            reason = check_tool_permission(tool_name, agent_role, registered_tools, policy.permissions)
            if reason is not None:
                return ToolCallResult(status=ToolCallStatus.DENIED, reason=reason)
            return execute(call_id)

        def record_result(session: Session, call_id: UUID, output: ToolCallResult) -> None:
            if stop_event is not None and stop_event.is_set():
                raise GitExecutionCancelledError("Tool result rejected after stop signal")
            if workspace_receipt is not None:
                workspace_receipt.assert_held(session)
            self._record_tool_result(session, call_id, output, agent_execution_id=agent_execution_id)

        return self._execute_recorded_tool_call(
            run_id, worker_id, call_id,
            execute=execute_with_permission, record_result=record_result,
        )

    @staticmethod
    def _record_tool_result(
        session: Session, call_id: UUID, output: ToolCallResult, *, agent_execution_id: UUID | None = None,
    ) -> None:
        updated = ToolCallRepository(session).save_result(
            call_id, status=output.status, stdout_excerpt=output.stdout_excerpt,
            stderr_excerpt=output.stderr_excerpt, exit_code=output.exit_code,
            duration_ms=output.duration_ms, reason=output.reason,
        )
        if updated == 0:
            raise ToolCallStateConflictError(f"ToolCall {call_id} no longer accepts a result")
        session.add_all(
            Artifact(
                id=artifact.id, agent_execution_id=agent_execution_id, tool_call_id=call_id,
                kind=artifact.kind, content_ref=artifact.content_ref,
                content_hash=artifact.content_hash,
            )
            for artifact in output.artifacts
        )

    def execute_tool_call(
        self, run_id: UUID, worker_id: str, *,
        record_start: Callable[[Session], UUID],
        execute: Callable[[UUID], ToolOutput],
        record_result: Callable[[Session, UUID, ToolOutput], None],
    ) -> UUID:
        """Commit the start record, execute the tool, and commit its guarded result.

        Start with an idle Session. The record callbacks use the supplied
        Session and leave transaction control here. The execute callback
        receives the committed call ID and runs independently of that Session.
        """
        with self.session.begin():
            self.assert_execution_right(run_id, worker_id)
            call_id = record_start(self.session)

        return self._execute_recorded_tool_call(
            run_id, worker_id, call_id, execute=execute, record_result=record_result,
        )

    def _execute_recorded_tool_call(
        self, run_id: UUID, worker_id: str, call_id: UUID, *,
        execute: Callable[[UUID], ToolOutput],
        record_result: Callable[[Session, UUID, ToolOutput], None],
    ) -> UUID:
        output = execute(call_id)

        with self.session.begin():
            self.assert_execution_right(run_id, worker_id)
            record_result(self.session, call_id, output)
        return call_id

    def assert_execution_right(self, run_id: UUID, worker_id: str) -> PolicyConfig:
        """Check authority, hold the Run lock, and return the pinned Policy."""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-blank string")
        if not self.session.in_transaction():
            raise RuntimeError("Execution-right checks require a caller-owned transaction")
        _, policy = self._lock_and_check_execution_right(run_id, worker_id)
        return policy

    def mark_expired_run_tool_calls_unknown(self, run_id: UUID) -> int:
        """Mark unsettled calls after checking the locked Run's lease against database time."""
        with self.session.begin():
            run = self.run_repository.get_by_id_for_update(run_id)
            if run is None:
                raise RunExecutionDeniedError(f"Run {run_id} does not exist")
            if run.status != RunStatus.RUNNING or get_database_time(self.session) < run.lease_expires_at:
                raise RunLeaseNotExpiredError(f"Run {run_id} is not RUNNING with an expired lease")
            count = self._mark_pending_tool_calls_unknown(run_id)
        return count

    def get_expired_run_evidence(self, run_id: UUID) -> dict:
        """Read a detached snapshot of a currently expired Run and its ToolCalls."""
        with self.session.begin():
            run = self.run_repository.get_by_id_for_update(run_id)
            if run is None:
                raise RunExecutionDeniedError(f"Run {run_id} does not exist")
            observed_at = get_database_time(self.session)
            if run.status != RunStatus.RUNNING or observed_at < run.lease_expires_at:
                raise RunLeaseNotExpiredError(f"Run {run_id} is not RUNNING with an expired lease")
            calls = self.session.scalars(
                select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at, ToolCall.id),
            ).all()
            evidence = {
                "run_id": str(run.id),
                "task_id": str(run.task_id),
                "worker_id": run.worker_id,
                "lease_expires_at": run.lease_expires_at.isoformat(),
                "observed_at": observed_at.isoformat(),
                "tool_calls": [
                    {
                        "tool_call_id": str(call.id),
                        "tool_name": call.tool_name,
                        "status": call.status.value,
                        "reason": call.reason,
                        "exit_code": call.exit_code,
                    }
                    for call in calls
                ],
            }
        return evidence

    def close_expired_run(self, run_id: UUID, *, decision: FailureDecision) -> UUID:
        """Reject unverified recovery decisions until the control flow is integrated."""
        raise RecoveryDecisionUnavailableError("Supervisor and Policy verification are not yet available")

    def _commit_expired_run_decision(self, run_id: UUID, *, decision: FailureDecision) -> UUID:
        """Commit an already verified decision with the expired Run's terminal state."""
        with self.session.begin():
            run = self.run_repository.get_by_id_for_update(run_id)
            if run is None:
                raise RunExecutionDeniedError(f"Run {run_id} does not exist")
            now = get_database_time(self.session)
            if run.status != RunStatus.RUNNING or now < run.lease_expires_at:
                raise RunLeaseNotExpiredError(f"Run {run_id} is not RUNNING with an expired lease")
            self._mark_pending_tool_calls_unknown(run_id)
            run.status = RunStatus.FAILED
            run.finished_at = now
            failure = FailureRecord(
                run_id=run_id, category=decision.category, signature=decision.signature,
                recovery_action=decision.recovery_action,
            )
            self.session.add(failure)
            self.session.flush()
            failure_id = failure.id
        return failure_id

    def mark_next_expired_run_tool_calls_unknown(self) -> UUID | None:
        """Find one expired Run with pending tools and record their unknown outcomes."""
        with self.session.begin():
            pending = exists().where(
                ToolCall.run_id == Run.id,
                ToolCall.status.in_((ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING)),
            )
            run = self.session.scalar(
                select(Run).where(
                    Run.status == RunStatus.RUNNING,
                    Run.lease_expires_at <= func.clock_timestamp(),
                    pending,
                ).order_by(Run.lease_expires_at, Run.id).limit(1).with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            if get_database_time(self.session) < run.lease_expires_at:
                return None
            self._mark_pending_tool_calls_unknown(run.id)
            run_id = run.id
        return run_id

    def _mark_pending_tool_calls_unknown(self, run_id: UUID) -> int:
        updated = self.session.execute(
            update(ToolCall).where(
                ToolCall.run_id == run_id,
                ToolCall.status.in_((ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING)),
            ).values(status=ToolCallStatus.UNKNOWN_RESULT, reason="WORKER_LEASE_EXPIRED")
        )
        return updated.rowcount

    def _lock_and_check_execution_right(self, run_id: UUID, worker_id: str) -> tuple[datetime, PolicyConfig]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-blank string")
        run = self.run_repository.get_by_id_for_update(run_id)
        if run is None or run.status != RunStatus.RUNNING or run.worker_id != worker_id:
            raise RunExecutionDeniedError(f"Run {run_id} is not eligible for this worker")

        task = self.task_repository.get_by_id(run.task_id, populate_existing=True)
        if task is None:
            raise TaskNotFoundError(f"Task {run.task_id} not found")
        policy = PolicyConfig.model_validate(task.policy_snapshot)
        if policy.policy_version != task.policy_version:
            raise ValueError("Task policy_version does not match its policy_snapshot")
        hard_deadline = run.started_at + timedelta(seconds=policy.budget.max_run_duration_seconds)
        # Lock waits and Policy reads must finish before checking the current time.
        database_time = get_database_time(self.session)
        if database_time >= run.lease_expires_at or database_time >= hard_deadline:
            raise RunExecutionDeniedError(f"Run {run_id} lease or duration budget has expired")
        return hard_deadline, policy

    def _get_replay(
        self, task_id: UUID, worker_id: str, creation_idempotency_key: str,
    ) -> Run | None:
        run = self.run_repository.get_by_creation_idempotency_key(task_id, creation_idempotency_key)
        if run is not None and run.worker_id != worker_id:
            raise RunCreationConflictError(
                f"Run creation key for Task {task_id} is already assigned to another worker"
            )
        return run

    def _create_initial_run(self, task: Task, worker_id: str, creation_idempotency_key: str) -> Run:
        if task.status != TaskStatus.ELIGIBLE:
            raise TaskStateConflictError(
                f"Task {task.id} is {task.status.value}; initial Run requires ELIGIBLE"
            )
        if self.run_repository.has_for_task(task.id):
            raise TaskStateConflictError(f"Task {task.id} already has Run history; this is not an initial start")

        # Use this Task's saved Policy, never a newly generated default.
        policy = PolicyConfig.model_validate(task.policy_snapshot)
        if policy.policy_version != task.policy_version:
            raise ValueError("Task policy_version does not match its policy_snapshot")
        database_time = get_database_time(self.session)
        lease_seconds = min(
            self.lease_config.lease_duration_seconds, policy.budget.max_run_duration_seconds,
        )
        run = Run(
            task_id=task.id,
            attempt_number=1,
            status=RunStatus.RUNNING,
            creation_idempotency_key=creation_idempotency_key,
            worker_id=worker_id,
            started_at=database_time,
            last_heartbeat_at=database_time,
            lease_expires_at=database_time + timedelta(seconds=lease_seconds),
        )
        self.run_repository.add(run)
        task.status = TaskStatus.RUNNING
        self.session.flush()
        return run
