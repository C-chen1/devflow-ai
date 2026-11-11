from uuid import UUID
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from devflow_ai.enums import AgentExecutionStatus, AgentRole, ToolCallStatus, RunStatus, VerificationResult
from devflow_ai.artifacts import ArtifactVerificationError, read_verified_artifact
from devflow_ai.coder import CoderOutput, PatchReady
from devflow_ai.models import AgentExecution, Artifact, FailureRecord, Handoff, Run, Task, ToolCall, Verification
from devflow_ai.planner import PlanReady, PlannerOutput


class AgentReadEvidenceError(ValueError):
    """The submitted reference does not match an eligible read in this execution."""


class VerifierPatchEvidenceError(ValueError):
    """A proposed patch is not verified evidence of an applied Coder result."""


class ReadEvidenceRef(BaseModel):
    """An Agent's citation of a completed read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    tool_call_id: UUID


def resolve_read_evidence(
    session: Session, run_id: UUID, agent_execution_id: UUID, references: tuple[ReadEvidenceRef, ...],
) -> list[dict[str, str | bool]]:
    if not references:
        return []
    calls = {
        call.id: call
        for call in session.execute(
            select(ToolCall.id, ToolCall.args, ToolCall.reason).where(
                ToolCall.id.in_({reference.tool_call_id for reference in references}),
                ToolCall.run_id == run_id,
                ToolCall.agent_execution_id == agent_execution_id,
                ToolCall.tool_name == "read_file",
                ToolCall.status == ToolCallStatus.SUCCEEDED,
            )
        )
    }
    evidence = []
    for reference in references:
        call = calls.get(reference.tool_call_id)
        if call is None or not isinstance(call.args, dict) or call.args.get("path") != reference.path:
            raise AgentReadEvidenceError(
                f"ToolCall {reference.tool_call_id} is not a matching successful read in this execution"
            )
        evidence.append({
            "path": reference.path,
            "tool_call_id": str(reference.tool_call_id),
            "truncated": call.reason == "OUTPUT_TRUNCATED",
        })
    return evidence


class ContextBuilder:
    def __init__(self, session: Session):
        self.session = session

    def build_issue_context(self, run_id: UUID) -> dict[str, object]:
        title, description, base_commit = self.session.execute(
            select(Task.title, Task.description, Task.base_commit)
            .join(Run, Run.task_id == Task.id)
            .where(Run.id == run_id)
        ).one()
        return {"issue": {"title": title, "description": description}, "base_commit": base_commit}

    def build_retry_context(self, run_id: UUID) -> dict[str, object] | None:
        """Recover the prior formal failure for a new Run, without private test contents."""
        run = self.session.get(Run, run_id)
        if run is None:
            raise ValueError("Run is unavailable")
        if run.attempt_number == 1:
            return None
        previous = self.session.scalar(select(Run).where(Run.task_id == run.task_id,
            Run.attempt_number == run.attempt_number - 1, Run.status == RunStatus.FAILED))
        if previous is None:
            raise ValueError("Previous failed Run is unavailable")
        failures = self.session.scalars(select(FailureRecord).where(FailureRecord.run_id == previous.id)).all()
        verifications = self.session.scalars(select(Verification).where(Verification.run_id == previous.id,
            Verification.result == VerificationResult.FAILED)).all()
        if len(failures) != 1 or len(verifications) != 1 or failures[0].recovery_action != "REVISE_IMPLEMENTATION":
            raise ValueError("Previous implementation recovery evidence is unavailable")
        failure, verification = failures[0], verifications[0]
        evidence = verification.evidence or {}
        return {"previous_run_id": str(previous.id), "verification_id": str(verification.id),
            "failure_record_id": str(failure.id), "category": failure.category.value,
            "recovery_action": failure.recovery_action, "test_comparison": evidence.get("test_comparison"),
            "semantic_review": evidence.get("semantic_review")}

    def build_coder_input(self, run_id: UUID, *, handoff_id: UUID) -> dict[str, object] | None:
        handoff = self.session.scalar(
            select(Handoff).join(AgentExecution, Handoff.from_agent_execution_id == AgentExecution.id)
            .where(
                Handoff.id == handoff_id, Handoff.to_agent_role == AgentRole.CODER.value.lower(),
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.PLANNER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            )
        )
        if handoff is None:
            return None
        approval = self.session.scalar(select(AgentExecution.id).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
            AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            AgentExecution.output["approved_handoff_id"].as_string() == str(handoff_id),
        ))
        source = self.session.get(AgentExecution, handoff.from_agent_execution_id)
        if approval is None:
            return None
        plan = PlannerOutput.model_validate(source.output).root
        if not isinstance(plan, PlanReady):
            return None
        context = {
            **self.build_issue_context(run_id),
            "planner_execution_id": str(source.id),
            "plan": plan.plan.model_dump(mode="json"),
            "handoff": handoff.context,
        }
        retry_context = self.build_retry_context(run_id)
        if retry_context is not None:
            context["retry_context"] = retry_context
        explorer_ref = (source.input or {}).get("explorer_execution_id")
        if explorer_ref is not None:
            try:
                explorer_id = UUID(explorer_ref)
            except (ValueError, TypeError, AttributeError):
                return None
            exploration = self.build_supervisor_exploration(run_id, explorer_execution_id=explorer_id)
            if exploration is None:
                return None
            context["explorer_evidence"] = exploration["read_evidence"]
            if exploration.get("prior_explorations"):
                context["prior_explorations"] = exploration["prior_explorations"]
        return context

    def build_verifier_patch_input(
        self, run_id: UUID, *, coder_execution_id: UUID, patch_call_id: UUID,
        artifact_root: Path, max_patch_bytes: int,
    ) -> dict[str, object]:
        """Load a successful Coder's verified patch and matching applied ToolCall."""
        execution = self.session.scalar(select(AgentExecution).where(
            AgentExecution.id == coder_execution_id, AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.CODER.value.lower(),
            AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
        ))
        if execution is None:
            raise VerifierPatchEvidenceError("Successful Coder execution is unavailable")
        try:
            output = CoderOutput.model_validate(execution.output).root
        except ValidationError as error:
            raise VerifierPatchEvidenceError("Coder output is invalid") from error
        if not isinstance(output, PatchReady):
            raise VerifierPatchEvidenceError("Coder did not produce a patch")
        artifact = self.session.scalar(select(Artifact).where(
            Artifact.id == output.patch_artifact_id,
            Artifact.agent_execution_id == coder_execution_id,
            Artifact.kind == "patch",
        ))
        call = self.session.scalar(select(ToolCall).where(
            ToolCall.id == patch_call_id, ToolCall.run_id == run_id,
            ToolCall.agent_execution_id == coder_execution_id, ToolCall.tool_name == "apply_patch",
            ToolCall.status == ToolCallStatus.SUCCEEDED,
        ))
        if artifact is None or call is None or call.args not in (
            {"patch_sha256": artifact.content_hash}, {"artifact_id": str(artifact.id)},
            {"patch_sha256": artifact.content_hash, "recount": True}, {"artifact_id": str(artifact.id), "recount": True},
        ):
            raise VerifierPatchEvidenceError("Patch Artifact does not match a successful apply call")
        try:
            patch = read_verified_artifact(
                artifact_root, artifact.id, content_ref=artifact.content_ref,
                content_hash=artifact.content_hash, max_bytes=max_patch_bytes,
            ).decode("utf-8")
        except (ArtifactVerificationError, OSError, UnicodeDecodeError) as error:
            raise VerifierPatchEvidenceError("Patch Artifact cannot be verified or decoded") from error
        return {
            **self.build_issue_context(run_id),
            "coder_execution_id": str(coder_execution_id), "patch_tool_call_id": str(patch_call_id),
            "patch_artifact_id": str(artifact.id), "patch_sha256": artifact.content_hash, "patch": patch,
        }

    def build_planner_input(
        self, run_id: UUID, *, explorer_execution_id: UUID,
    ) -> dict[str, object] | None:
        exploration = self.build_planner_exploration(run_id, explorer_execution_id=explorer_execution_id)
        if exploration is None:
            return None
        context = {
            **self.build_issue_context(run_id),
            **exploration,
        }
        retry_context = self.build_retry_context(run_id)
        if retry_context is not None:
            context["retry_context"] = retry_context
        evidence = []
        for reference in context["read_evidence"]:
            call = self.session.execute(select(ToolCall.stdout_excerpt).where(
                ToolCall.id == UUID(reference["tool_call_id"]), ToolCall.run_id == run_id,
                ToolCall.agent_execution_id == explorer_execution_id, ToolCall.tool_name == "read_file",
                ToolCall.status == ToolCallStatus.SUCCEEDED,
            )).one_or_none()
            if call is None:
                raise AgentReadEvidenceError("Explorer read evidence is no longer available")
            preview = call.stdout_excerpt
            ends_with_newline = (preview.endswith("\n") if preview is not None and not reference["truncated"] else None)
            evidence.append({**reference, "preview": preview, "ends_with_newline": ends_with_newline})
        selected = self.session.get(AgentExecution, explorer_execution_id)
        previous_ids = self.session.scalars(select(AgentExecution.id).where(
            AgentExecution.run_id == run_id, AgentExecution.agent_role == AgentRole.EXPLORER.value.lower(),
            AgentExecution.status == AgentExecutionStatus.SUCCEEDED, AgentExecution.id != explorer_execution_id,
            AgentExecution.created_at <= selected.created_at,
        ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(2)).all()
        previous = []
        for previous_id in reversed(previous_ids):
            earlier = self.build_planner_exploration(run_id, explorer_execution_id=previous_id)
            previews = []
            for reference in earlier["read_evidence"]:
                preview = self.session.scalar(select(ToolCall.stdout_excerpt).where(
                    ToolCall.id == UUID(reference["tool_call_id"]), ToolCall.run_id == run_id,
                    ToolCall.agent_execution_id == previous_id, ToolCall.tool_name == "read_file",
                    ToolCall.status == ToolCallStatus.SUCCEEDED))
                clipped = preview is not None and len(preview) > 6000
                previews.append({**reference, "preview": preview[:6000] if preview is not None else None,
                                 "truncated": reference["truncated"] or clipped})
            previous.append({"explorer_execution_id": str(previous_id),
                             "explorer_output": earlier["explorer_output"], "read_evidence": previews})
        if previous:
            context["prior_explorations"] = previous
        return {**context, "read_evidence": evidence}

    def build_supervisor_exploration(
        self, run_id: UUID, *, explorer_execution_id: UUID,
    ) -> dict[str, object] | None:
        """Use the same verified Explorer previews available to Planner."""
        return self.build_planner_input(run_id, explorer_execution_id=explorer_execution_id)

    def build_planner_exploration(
        self, run_id: UUID, *, explorer_execution_id: UUID,
    ) -> dict[str, object] | None:
        """Build the exploration portion of Planner input from a selected execution."""
        execution = self.session.execute(
            select(AgentExecution.output).where(
                AgentExecution.id == explorer_execution_id,
                AgentExecution.run_id == run_id,
                AgentExecution.agent_role == AgentRole.EXPLORER.value.lower(),
                AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            )
        ).one_or_none()
        if execution is None:
            return None

        report = dict(execution.output or {})
        references = tuple(
            ReadEvidenceRef(path=entry["path"], tool_call_id=entry["tool_call_id"])
            for entry in report.pop("read_evidence", [])
        )
        return {
            "explorer_execution_id": str(explorer_execution_id),
            "explorer_output": report,
            "read_evidence": resolve_read_evidence(self.session, run_id, explorer_execution_id, references),
        }
