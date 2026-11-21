from uuid import UUID
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from devflow_ai.enums import AgentExecutionStatus, AgentRole, FailureCategory, ToolCallStatus, RunStatus, VerificationResult
from devflow_ai.artifacts import ArtifactVerificationError, read_verified_artifact
from devflow_ai.coder import CoderOutput, PatchReady
from devflow_ai.models import AgentExecution, Artifact, FailureRecord, Handoff, Run, Task, ToolCall, Verification
from devflow_ai.planner import PlanReady, PlannerOutput

MEMORY_RECALL_CONTEXT_KEY = "memory_recall_v1"


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
        entry = {
            "path": reference.path,
            "tool_call_id": str(reference.tool_call_id),
            # `truncated` keeps its meaning: this read is not the whole file. A ranged read
            # that stops short of the end, or starts after the first line, is truncated too.
            "truncated": call.reason == "OUTPUT_TRUNCATED",
        }
        _add_read_window(entry, call.args)
        evidence.append(entry)
    return evidence


# A prior exploration is re-sent with every later Planner call, so its previews share one
# character budget instead of being cut per file at an arbitrary size.
#
# Two different facts are reported separately, and neither implies the other:
#   `truncated`       the tool cut the read (reason=OUTPUT_TRUNCATED)
#   `preview_clipped` this prompt contains less than the read produced
# `preview_chars` is how much the read produced. A complete tool read therefore does *not*
# mean a model has seen the whole file — do not read `truncated: false` as "complete input".
PRIOR_PREVIEW_TOTAL_CHARS = 32_000


def _bound_preview_budget(entries: list[dict], budget: int) -> None:
    """Clip previews to a shared budget, most recent first, and say how much was left out.

    Only `preview_clipped`/`preview_chars` describe this clip. They never touch
    `truncated`, which belongs to the tool that performed the read.
    """
    remaining = budget
    for entry in entries:
        body = entry.get("preview")
        entry["preview_chars"] = 0 if body is None else len(body)
        if body is None:
            entry["preview_clipped"] = False
            continue
        keep = max(0, min(len(body), remaining))
        entry["preview"] = body[:keep]
        entry["preview_clipped"] = keep < len(body)
        remaining -= keep


def _without_previews(exploration: dict[str, object]) -> dict[str, object]:
    """Same references and findings, without the raw bodies."""
    def strip(entries):
        stripped = []
        for entry in entries:
            body = entry.get("preview")
            replaced = {key: value for key, value in entry.items() if key != "preview"}
            replaced["preview_chars"] = 0 if body is None else len(body)
            replaced["preview_omitted"] = body is not None
            stripped.append(replaced)
        return stripped

    result = dict(exploration)
    result["read_evidence"] = strip(exploration.get("read_evidence", []))
    if "prior_explorations" in exploration:
        result["prior_explorations"] = [
            {**prior, "read_evidence": strip(prior.get("read_evidence", []))}
            for prior in exploration["prior_explorations"]
        ]
    # What has already been read in this Run, merged per file. This is history, not
    # availability: it does not mean the content reached a model in full, and it must never
    # stop a re-read that is needed to fill clipped or truncated evidence.
    entries = [entry for group in [exploration.get("read_evidence", [])] + [
        prior.get("read_evidence", []) for prior in exploration.get("prior_explorations", [])]
        for entry in group]
    result["already_read"] = summarize_read_coverage(entries)
    result["preview_bodies"] = "omitted; the Planner receives them"
    return result


_READ_OBSERVED_KEYS = ("start_line", "end_line", "total_lines", "has_more",
                      "byte_truncated", "partial_line")


def _add_read_window(entry: dict, args) -> None:
    """Copy the requested and the observed read window into one evidence entry.

    The request (``offset``/``limit``) says what was asked for; the observed values say what
    was actually read. They are kept apart on purpose, and an unknown value stays ``None`` —
    a read that never scanned the whole file does not get a made-up total.
    """
    if not isinstance(args, dict):
        return
    if isinstance(args.get("offset"), int):
        entry["offset"] = args["offset"]
    if isinstance(args.get("limit"), int):
        entry["limit"] = args["limit"]
    observed = args.get("observed")
    if isinstance(observed, dict):
        for key in _READ_OBSERVED_KEYS:
            if key in observed:
                entry[key] = observed[key]


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    """Merge overlapping or adjacent line ranges so coverage is one statement per file."""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def summarize_read_coverage(entries) -> list[dict]:
    """Turn individual read windows into one per-file coverage summary.

    A list of every window leaves the model comparing intervals by eye; merged ranges say
    directly what is already in hand, which is what stops a re-read of a window that is
    already covered. Unknown totals stay ``None``.
    """
    grouped: dict[str, dict] = {}
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        group = grouped.setdefault(path, {
            "path": path, "covered_ranges": [], "reads": 0, "tool_call_ids": [],
            "truncated": False, "total_lines": None,
        })
        group["reads"] += 1
        call_id = entry.get("tool_call_id")
        if isinstance(call_id, str):
            group["tool_call_ids"].append(call_id)
        if entry.get("truncated") is True:
            group["truncated"] = True
        if isinstance(entry.get("total_lines"), int):
            group["total_lines"] = entry["total_lines"]
        start, end = entry.get("start_line"), entry.get("end_line")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            # A byte-limited preview may contain only a prefix of its last line.
            # Do not present that line as fully covered to subsequent agents.
            complete_end = end - 1 if entry.get("partial_line") is True else end
            if complete_end >= start:
                group["covered_ranges"].append([start, complete_end])
    summaries = []
    for path in sorted(grouped):
        group = grouped[path]
        group["covered_ranges"] = _merge_ranges(group["covered_ranges"])
        summaries.append(group)
    return summaries


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
        if len(failures) != 1 or len(verifications) != 1:
            raise ValueError("Previous failure recovery evidence is unavailable")
        failure, verification = failures[0], verifications[0]
        action = run.recovery_action or failure.recovery_action
        if action == "REVISE_IMPLEMENTATION":
            if failure.category != FailureCategory.IMPLEMENTATION_FAILURE:
                raise ValueError("Previous implementation recovery evidence is unavailable")
        elif action == "REDIAGNOSE":
            if failure.category != FailureCategory.UNKNOWN_FAILURE:
                raise ValueError("Previous rediagnosis recovery evidence is unavailable")
        else:
            raise ValueError("Previous recovery action is unavailable")
        evidence = verification.evidence or {}
        return {"previous_run_id": str(previous.id), "verification_id": str(verification.id),
            "failure_record_id": str(failure.id), "category": failure.category.value,
            "recovery_action": action, "test_comparison": evidence.get("test_comparison"),
            "semantic_review": evidence.get("semantic_review"),
            "policy_reasons": list(evidence.get("policy_reasons") or [])}

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
            evidence.append({**reference, "preview": preview, "ends_with_newline": ends_with_newline,
                             "preview_chars": 0 if preview is None else len(preview),
                             "preview_clipped": False})
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
                # `truncated` stays the tool's own verdict about the read; the builder's clip is
                # reported separately so a Planner cannot mistake it for one — and so a complete
                # read is not misread as "the Planner has the whole file".
                previews.append({**reference, "preview": preview,
                                 "truncated": reference["truncated"]})
            previous.append({"explorer_execution_id": str(previous_id),
                             "explorer_output": earlier["explorer_output"], "read_evidence": previews})
        if previous:
            # The list runs oldest-to-newest; fill the budget from the newest backwards so a
            # later re-read is not starved by an older one.
            _bound_preview_budget(
                [entry for prior in reversed(previous) for entry in prior["read_evidence"]],
                PRIOR_PREVIEW_TOTAL_CHARS)
            context["prior_explorations"] = previous
        return {**context, "read_evidence": evidence}

    def build_supervisor_exploration(
        self, run_id: UUID, *, explorer_execution_id: UUID,
    ) -> dict[str, object] | None:
        """Build the routing view: the same references and findings, no preview bodies.

        A Supervisor decides where to route, not how to implement; sending it the raw
        previews only repeated the largest part of the prompt that the Planner needs once.
        The references, the tool's truncation verdict and the char counts stay in place, so
        nothing about the evidence is claimed that is not true.
        """
        exploration = self.build_planner_input(run_id, explorer_execution_id=explorer_execution_id)
        return None if exploration is None else _without_previews(exploration)

    def build_already_read(self, run_id: UUID, *, limit: int = 20) -> list[dict[str, object]]:
        """List the read windows this Run has already covered, newest first.

        The Explorer needs this most: a second exploration that asks for a window already in
        hand spends budget to learn nothing. Only committed, successful reads are listed, and
        only their window — never the file contents.
        """
        rows = self.session.execute(
            select(ToolCall.id, ToolCall.args, ToolCall.reason).where(
                ToolCall.run_id == run_id,
                ToolCall.tool_name == "read_file",
                ToolCall.status == ToolCallStatus.SUCCEEDED,
            ).order_by(ToolCall.created_at.desc(), ToolCall.id.desc()).limit(limit)
        ).all()
        seen, windows = set(), []
        for call_id, args, reason in rows:
            if not isinstance(args, dict) or not isinstance(args.get("path"), str):
                continue
            offset = args.get("offset") if isinstance(args.get("offset"), int) else None
            line_limit = args.get("limit") if isinstance(args.get("limit"), int) else None
            key = (args["path"], offset, line_limit)
            if key in seen:
                continue
            seen.add(key)
            entry: dict[str, object] = {
                "path": args["path"], "tool_call_id": str(call_id),
                "truncated": reason == "OUTPUT_TRUNCATED",
            }
            _add_read_window(entry, args)
            windows.append(entry)
        return summarize_read_coverage(windows)

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
