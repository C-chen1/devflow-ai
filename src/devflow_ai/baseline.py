"""Validate Coder baseline provenance and recorded workspace history."""
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from devflow_ai.context import ContextBuilder
from devflow_ai.enums import AgentExecutionStatus, AgentRole, ToolCallStatus
from devflow_ai.models import AgentExecution, Handoff, ToolCall
from devflow_ai.verifier import VerificationEvidenceError, load_pytest_report_artifact


class BaselineUnavailableError(ValueError):
    """The approved baseline cannot support a Coder execution."""


def validate_recorded_baseline(
    session: Session, run_id: UUID, *, planner_execution_id: UUID, baseline_id: UUID,
    artifact_root: Path, exclude_call_id: UUID | None = None,
) -> AgentExecution:
    """Validate a committed baseline report and the operations allowed after it."""
    baseline = session.scalar(select(AgentExecution).join(Handoff, AgentExecution.handoff_id == Handoff.id).where(
        AgentExecution.id == baseline_id, AgentExecution.run_id == run_id,
        AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
        AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
        Handoff.from_agent_execution_id == planner_execution_id,
        Handoff.to_agent_role == AgentRole.VERIFIER.value.lower(),
    ))
    if baseline is None or (baseline.output or {}).get("outcome") != "BASELINE_RECORDED":
        raise BaselineUnavailableError("Committed baseline is unavailable")
    approved = session.scalar(select(AgentExecution.id).where(
        AgentExecution.run_id == run_id, AgentExecution.agent_role == AgentRole.SUPERVISOR.value.lower(),
        AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
        AgentExecution.output["approved_handoff_id"].as_string() == str(baseline.handoff_id),
    ))
    if approved is None:
        raise BaselineUnavailableError("Baseline has no committed Supervisor approval")
    try:
        call_id = UUID(baseline.output["tool_call_id"])
        report_id = UUID(baseline.output["report_artifact_id"])
        load_pytest_report_artifact(session, run_id, baseline_id, tool_call_id=call_id,
            artifact_id=report_id, artifact_root=artifact_root, expected_command_name=baseline.input["command_name"])
        baseline_call = session.get(ToolCall, call_id)
        if baseline_call is None:
            raise BaselineUnavailableError("Baseline tool call is unavailable")
        query = select(ToolCall.id).where(
            ToolCall.run_id == run_id, ToolCall.id != call_id,
            ToolCall.tool_name.in_(("run_command", "apply_patch", "git_clone", "git_checkout")),
            ToolCall.status != ToolCallStatus.DENIED, ToolCall.created_at >= baseline_call.updated_at,
        )
        if exclude_call_id is not None:
            query = query.where(ToolCall.id != exclude_call_id)
        if session.scalar(query.limit(1)) is not None:
            raise BaselineUnavailableError("Workspace operation occurred after baseline")
    except BaselineUnavailableError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, VerificationEvidenceError) as error:
        raise BaselineUnavailableError("Verified baseline for the approved plan is unavailable") from error
    return baseline


def validate_coder_baseline(session: Session, run_id: UUID, *, handoff_id: UUID,
                            artifact_root: Path, exclude_call_id: UUID | None = None) -> dict:
    agent_input = ContextBuilder(session).build_coder_input(run_id, handoff_id=handoff_id)
    if agent_input is None:
        raise BaselineUnavailableError("Approved Planner result is unavailable")
    refs = agent_input["handoff"].get("evidence_refs", [])
    baseline_refs = [ref["baseline_verifier_id"] for ref in refs
                     if isinstance(ref, dict) and "baseline_verifier_id" in ref]
    try:
        if len(baseline_refs) != 1:
            raise ValueError("One baseline reference is required")
        validate_recorded_baseline(
            session, run_id, planner_execution_id=UUID(agent_input["planner_execution_id"]),
            baseline_id=UUID(baseline_refs[0]), artifact_root=artifact_root,
            exclude_call_id=exclude_call_id,
        )
    except (ValueError, KeyError, TypeError, AttributeError, VerificationEvidenceError) as error:
        raise BaselineUnavailableError("Verified baseline for the approved plan is unavailable") from error
    return agent_input
