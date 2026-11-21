"""Run one benchmark task in a single explicit call and record what happened.

This entry assembles the trusted configuration from a task-pack manifest, claims one
Run, drives it stage by stage through the orchestration layer, and writes a run record.
The stage driver decides which Supervisor builder a stage needs; this module only
supplies configuration, limits and the record, and it never retries.

Limits are enforced here: a wall-clock deadline stops the hop, and the token ceiling is
checked against the Task's pinned Policy before anything starts. A limit that only lives
in a document is not a limit.

No automatic retry and no recovery loop: one call is one attempt. Hidden material never
enters this module; only the suite version is carried out of the manifest.
"""
from __future__ import annotations

import argparse
import base64
from hashlib import sha256
import json
import os
import sys
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Timer
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from devflow_ai.benchmark_task import BenchmarkTaskError, load_policy_snapshot
from devflow_ai.benchmark_record import (
    SCHEMA_VERSION, AttemptContext, AttemptKind, BenchmarkRunRecord, FormalEvidence,
    HumanReview, IndependentEvaluationEvidence, PublicCheckSummary, RunOutcome, write_run_record,
)
from devflow_ai.config import HeartbeatConfig
from devflow_ai.coordinator import RunCoordinator
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.execution_binding import ExecutionBindingError, verify_optional_binding
from devflow_ai.independent_evaluation import (
    IndependentEvaluationMaterial, MaterialPlacementError, PrivateCase,
    require_material_placement,
)
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.worker import Worker
from devflow_ai.enums import (
    AgentExecutionStatus, AgentRole, RunStatus, VerificationResult,
)
from devflow_ai.harness import GitExecutionCancelledError
from devflow_ai.models import AgentExecution, Artifact, Run, Task, Verification
from devflow_ai.orchestration import drive_run_stages, finalize_benchmark_run
from devflow_ai.policy import PolicyConfig
from devflow_ai.task_pack import load_manifest, optional_scope_binding_matches, to_verification_config
from devflow_ai.verifier import (
    VerificationEvidenceError, load_independent_evaluation, load_semantic_review_draft,
    preliminary_public_outcome,
)


class BenchmarkRunError(RuntimeError):
    """The run could not be started; nothing was recorded as an attempt."""


class BenchmarkLimits(BaseModel):
    """Per-attempt limits. V1 allows exactly one attempt and no automatic retry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_duration_seconds: int = Field(default=1200, gt=0, strict=True)
    max_model_tokens: int = Field(default=80_000, gt=0, strict=True)
    max_stages: int = Field(default=32, gt=0, strict=True)
    attempts: int = Field(default=1, strict=True)
    auto_retries: int = Field(default=0, strict=True)

    @model_validator(mode="after")
    def one_attempt_without_retry(self) -> "BenchmarkLimits":
        if self.attempts != 1 or self.auto_retries != 0:
            raise ValueError("V1 runs exactly one attempt with no automatic retry")
        return self


@dataclass(frozen=True)
class BenchmarkRunRequest:
    pack_dir: Path
    task_id: UUID
    operator: str
    # Absolute interpreter path for the registered public-check command. Required: the
    # harness refuses a relative executable, and an Agent must never choose it.
    python_executable: str
    workspaces_root: Path
    artifacts_root: Path
    record_path: Path
    # The file the operator named, when one was given. Kept as a path so the placement check
    # can run again where the Task's repository is known (before any Run is created).
    material_path: Path | None = None
    # A rehearsal proves the environment works; it is not a formal sample and must never
    # be counted as one. The marker lands in the record's notes so summaries can exclude it.
    attempt_kind: str = "formal"
    # Pinned execution inventory for the code this entry runs. Verified before the claim; the
    # verified digest is written to the Run in the claim transaction. Both or neither.
    execution_manifest: Path | None = None
    execution_sha256: str | None = None
    limits: BenchmarkLimits = field(default_factory=BenchmarkLimits)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_id_from_environment() -> str:
    """Record the model actually configured for the gateways, not a claim about it."""
    return (os.environ.get("DEVFLOW_DEEPSEEK_MODEL") or "").strip() or "unrecorded"


@dataclass(frozen=True)
class OutcomeDecision:
    """One attempt's stated result together with the evidence that label requires."""

    outcome: RunOutcome
    formal: FormalEvidence | None
    public_checks: PublicCheckSummary
    independent: IndependentEvaluationEvidence | None
    reason: str


def _summarize_draft(
    draft, verifier_execution_id: UUID, draft_id: str, *, claimed: str | None,
    notes: str,
) -> PublicCheckSummary:
    """Recompute the public outcome from one draft's comparison and semantic review.

    ``claimed`` is the checkpoint's own record, when there is one to compare against; a
    formal Verification has no such claim left (the Verifier output was replaced by its
    verdict), so the caller passes None and the recomputation stands on its own.
    """
    try:
        recomputed = preliminary_public_outcome(draft.test_comparison, draft.review).value
    except Exception:  # a draft that cannot be summarized is not evidence of a result
        return PublicCheckSummary(
            verifier_execution_id=str(verifier_execution_id), draft_artifact_id=draft_id,
            notes="The draft cannot be summarized into a public result.")
    if claimed is not None and (claimed not in ("passed", "failed") or recomputed != claimed):
        return PublicCheckSummary(
            verifier_execution_id=str(verifier_execution_id), draft_artifact_id=draft_id,
            notes=("The checkpoint's preliminary outcome does not match its own draft "
                   f"(recorded={claimed!r}, recomputed={recomputed!r})."))
    return PublicCheckSummary(
        verified_outcome=recomputed, verifier_execution_id=str(verifier_execution_id),
        draft_artifact_id=draft_id, notes=notes)


def _independent_from_receipt(
    session, run_id: UUID, verifier_execution_id: UUID, receipt_id: str, draft_id: str | None,
    *, artifact_root: Path | None, suite_version: str | None, claimed: str | None,
) -> IndependentEvaluationEvidence:
    """Build evidence from one receipt, refusing anything that cannot be tied down."""
    def unavailable(reason: str, *, verified: bool = False) -> IndependentEvaluationEvidence:
        return IndependentEvaluationEvidence(
            outcome="unavailable", verified=verified, reason=reason,
            receipt_artifact_id=receipt_id, suite_version=suite_version)

    if artifact_root is None:
        return unavailable("receipt_unverifiable")
    try:
        receipt = load_independent_evaluation(
            session, run_id, verifier_execution_id, artifact_id=UUID(str(receipt_id)),
            artifact_root=artifact_root)
    except (VerificationEvidenceError, ValueError):
        return unavailable("receipt_unverifiable")
    if claimed is not None and receipt.outcome != claimed:
        return unavailable("independent_outcome_mismatch")
    if suite_version is None:
        return unavailable("suite_version_unpinned")
    if receipt.suite_version != suite_version:
        return unavailable("suite_version_mismatch")
    if draft_id is None or str(receipt.draft_artifact_id) != draft_id:
        return unavailable("receipt_draft_mismatch")
    if receipt.outcome == "failed" and not receipt.calibrated:
        return IndependentEvaluationEvidence(
            outcome="unavailable", verified=True, calibrated=False,
            receipt_artifact_id=receipt_id, suite_version=receipt.suite_version,
            reason="independent_failure_uncalibrated")
    return IndependentEvaluationEvidence(
        outcome=receipt.outcome, verified=True, calibrated=receipt.calibrated,
        receipt_artifact_id=receipt_id, suite_version=receipt.suite_version,
        reason=("independent_evaluation_unavailable" if receipt.outcome == "unavailable" else None))


def _read_verifier_checkpoint(session, run_id: UUID, *, artifact_root: Path | None):
    """Return ``(verifier_execution_id, draft_id, PublicCheckSummary)`` for the latest Verifier.

    The checkpoint's own claim is not evidence: the draft it cites is read back from disk,
    the public result is **recomputed** from that draft's test comparison and semantic review
    with the same function the Verifier used, and the two must agree. Only then is
    ``verified_outcome`` filled; a readable file alone proves nothing about the result.
    """
    row = session.execute(select(AgentExecution.id, AgentExecution.output).where(
        AgentExecution.run_id == run_id,
        AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
    ).order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc()).limit(1)).first()
    if row is None:
        return None, None, PublicCheckSummary(notes="No Verifier execution is recorded for this Run.")
    execution_id, output = row
    output = dict(output or {})
    if output.get("outcome") != "VERIFICATION_BLOCKED":
        return execution_id, None, PublicCheckSummary(
            verifier_execution_id=str(execution_id),
            notes="The Verifier recorded no preliminary public result.")
    draft_id = str(output.get("draft_artifact_id") or "").strip()
    recorded = output.get("preliminary_public_outcome")
    if artifact_root is None or not draft_id:
        return execution_id, draft_id or None, PublicCheckSummary(
            verifier_execution_id=str(execution_id), draft_artifact_id=draft_id or None,
            notes="The preliminary public result cannot be re-checked without its draft.")
    try:
        draft = load_semantic_review_draft(session, run_id, execution_id,
            artifact_id=UUID(draft_id), artifact_root=artifact_root)
    except (VerificationEvidenceError, ValueError):
        return execution_id, draft_id, PublicCheckSummary(
            verifier_execution_id=str(execution_id), draft_artifact_id=draft_id,
            notes="The Verifier checkpoint's draft no longer loads, so its public result is "
                  "not verifiable.")
    summary = _summarize_draft(
        draft, execution_id, draft_id, claimed=recorded,
        notes=("Recomputed from the checkpoint's draft and matching its recorded outcome; "
               "public check counts are not recovered from the stored report yet."))
    return execution_id, draft_id, summary


def _read_independent_evidence(
    session, run_id: UUID, verifier_execution_id: UUID | None, checkpoint_draft_id: str | None,
    finalization, *, artifact_root: Path | None, suite_version: str | None,
) -> IndependentEvaluationEvidence:
    """Turn the finalization result into evidence, and call it a verdict only when it is one.

    Three states are kept apart on purpose: an evaluation that was **attempted** and came
    back `unavailable`, one that was never attempted (skipped, or stopped before the trusted
    suite ran), and a receipt that was read back. A receipt that cannot be tied to the pinned
    suite version and to the same draft is not evidence of anything.
    """
    def unavailable(reason: str) -> IndependentEvaluationEvidence:
        return IndependentEvaluationEvidence(
            outcome="unavailable", reason=reason, suite_version=suite_version)

    if not isinstance(finalization, dict):
        return unavailable("finalization_not_recorded")
    kind = finalization.get("outcome")
    if kind in (None, "skipped"):
        return unavailable("evaluation_not_attempted")
    if kind == "finalization_error":
        return unavailable(str(finalization.get("reason") or "finalization_error"))
    if kind != "finalized":
        # not_finalized: the entry stopped before the trusted evaluation could run.
        return unavailable(str(finalization.get("reason") or str(kind)))
    claimed = finalization.get("independent_outcome")
    receipt_id = finalization.get("independent_artifact_id")
    if claimed not in ("passed", "failed", "unavailable"):
        return unavailable("independent_outcome_unknown")
    if verifier_execution_id is None or not receipt_id:
        return unavailable("receipt_reference_missing")
    return _independent_from_receipt(
        session, run_id, verifier_execution_id, str(receipt_id), checkpoint_draft_id,
        artifact_root=artifact_root, suite_version=suite_version, claimed=claimed)


def classify_outcome(
    session, run_id: UUID, *, deadline_fired: bool, independent_suite_version: str | None,
    artifact_root: Path | None = None, finalization: dict | None = None,
) -> OutcomeDecision:
    """Derive the outcome from committed rows and their artifacts only.

    Each label needs its own evidence, and none of them may stand in for another:

    * ``formal_pass`` — the whole trusted chain loads back from disk (Verification row,
      Verifier draft, independent receipt), with the suite version the pack pins; the public
      and independent summaries are rebuilt from **those** artifacts, because the Verifier's
      checkpoint output is replaced by its verdict once the formal transaction commits.
    * ``failed`` — the trusted evaluation ran, was calibrated, and its verified receipt says
      failed; the recorded public pass is kept beside it rather than replaced by a label.
    * ``public_only_pass`` — the public result was recomputed from the checkpoint's draft and
      matches, the attempt is over, and the trusted evaluation was **attempted** and came
      back ``unavailable`` with a stated reason.
    * ``incomplete`` — the attempt is still going, the evaluation was never attempted, or
      either half lacks evidence that can be checked.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise BenchmarkRunError("Run does not exist")
    verifications = session.scalars(select(Verification).where(
        Verification.run_id == run_id)).all()
    formal = None
    formal_reason = ""
    formal_ids = None
    for item in (v for v in verifications if v.result == VerificationResult.PASSED):
        evidence = dict(item.evidence or {})
        suite_version = str(evidence.get("suite_version") or "").strip()
        draft_id = str(evidence.get("draft_artifact_id") or "").strip()
        receipt_id = str(evidence.get("independent_artifact_id") or "").strip()
        if evidence.get("scope") != "trusted_v1":
            formal_reason = "evidence_scope_untrusted"
            continue
        if not draft_id or not receipt_id or not suite_version:
            formal_reason = "evidence_references_missing"
            continue
        if independent_suite_version is None:
            # An unpinned pack cannot anchor a suite version to anything, so a receipt
            # naming any version would be accepted for no reason at all.
            formal_reason = "suite_version_unpinned"
            continue
        if suite_version != independent_suite_version:
            formal_reason = "suite_version_mismatch"
            continue
        if artifact_root is None:
            # Without the artifact root the references cannot be checked at all.
            formal_reason = "artifacts_unverifiable"
            continue
        verifier_execution_id = session.scalar(select(AgentExecution.id).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_role == AgentRole.VERIFIER.value.lower(),
            AgentExecution.status == AgentExecutionStatus.SUCCEEDED,
            AgentExecution.output["verification_id"].as_string() == str(item.id)))
        if verifier_execution_id is None:
            formal_reason = "verifier_execution_unavailable"
            continue
        try:
            draft = load_semantic_review_draft(
                session, run_id, verifier_execution_id,
                artifact_id=UUID(draft_id), artifact_root=artifact_root)
            receipt = load_independent_evaluation(
                session, run_id, verifier_execution_id,
                artifact_id=UUID(receipt_id), artifact_root=artifact_root)
        except (VerificationEvidenceError, ValueError):
            formal_reason = "artifacts_unverifiable"
            continue
        if (str(receipt.draft_artifact_id) != draft_id or receipt.outcome != "passed"
                or receipt.suite_version != suite_version):
            formal_reason = "receipt_mismatch"
            continue
        formal = FormalEvidence(
            verification_id=str(item.id), independent_suite_version=suite_version,
            independent_receipt_sha256=session.scalar(
                select(Artifact.content_hash).where(Artifact.id == UUID(receipt_id))))
        formal_ids = (verifier_execution_id, draft_id, receipt_id, draft, receipt)
        break

    if formal_ids is not None:
        # The formal Verification is the evidence; its draft and receipt are what the
        # accepted result was actually built from, so the summary is rebuilt from them.
        verifier_id, draft_id, receipt_id, draft, receipt = formal_ids
        public_checks = _summarize_draft(
            draft, verifier_id, draft_id, claimed=None,
            notes=("Recomputed from the draft the formal Verification cites; public check "
                   "counts are not recovered from the stored report yet."))
        if public_checks.verified_outcome != "passed":
            public_checks = PublicCheckSummary(
                verifier_execution_id=str(verifier_id), draft_artifact_id=draft_id,
                notes=("The draft cited by the formal Verification no longer recomputes to a "
                       "passed public result."))
        independent = IndependentEvaluationEvidence(
            outcome=receipt.outcome, verified=True, calibrated=receipt.calibrated,
            receipt_artifact_id=receipt_id, suite_version=receipt.suite_version)
    else:
        verifier_id, checkpoint_draft_id, public_checks = _read_verifier_checkpoint(
            session, run_id, artifact_root=artifact_root)
        independent = _read_independent_evidence(
            session, run_id, verifier_id, checkpoint_draft_id, finalization,
            artifact_root=artifact_root, suite_version=independent_suite_version)

    def decide(outcome: RunOutcome, reason: str, formal: FormalEvidence | None = None):
        return OutcomeDecision(outcome=outcome, formal=formal, public_checks=public_checks,
                               independent=independent, reason=reason)

    if deadline_fired:
        return decide(RunOutcome.TIMEOUT, "deadline_reached")
    if formal is not None:
        return decide(RunOutcome.FORMAL_PASS, "", formal=formal)
    if public_checks.verified_outcome == "passed" and independent is not None:
        if (independent.outcome == "failed" and independent.verified
                and independent.calibrated):
            return decide(RunOutcome.FAILED, "independent_evaluation_failed")
        if independent.outcome == "unavailable":
            if (independent.verified
                    and independent.reason == "independent_evaluation_unavailable"):
                return decide(RunOutcome.PUBLIC_ONLY_PASS,
                              "public_passed_independent_unavailable:"
                              + str(independent.reason))
            return decide(RunOutcome.INCOMPLETE,
                          f"public_passed_but_{independent.reason}")
    if run.status == RunStatus.RUNNING:
        return decide(RunOutcome.INCOMPLETE, "run_still_running")
    if run.status == RunStatus.FAILED:
        return decide(RunOutcome.FAILED, "run_failed")
    return decide(RunOutcome.INCOMPLETE, formal_reason or "no_final_evidence")


def compose_run_record(
    session, run_id, *, manifest, task_id, independent_suite_version: str | None,
    policy_version: str, operator: str, attempt_kind: AttemptKind, artifact_root: Path,
    finalization: dict | None, deadline_fired: bool, artifacts_root: Path, started_at: str,
    attempt_context: AttemptContext, limits: BenchmarkLimits | None = None,
    model_id: str | None = None, attempt_index: int = 1, finished_at: str | None = None,
    error: BaseException | None = None, entered_execution: bool = True,
    stage_notes: str = "", extra_notes: str = "",
) -> tuple[BenchmarkRunRecord, str, bool]:
    """Derive one attempt's outcome from committed rows and return its record.

    The outcome comes from ``classify_outcome`` and nowhere else, so a scheduled attempt and
    a benchmark attempt are labelled by the same rules: ``formal_pass`` only with a loadable
    trusted chain, ``failed`` only with a verified calibrated failure, ``timeout`` when the
    deadline fired, and ``incomplete`` for everything a reader could not confirm. A human
    decision is never filled in here — the record is what a reviewer is asked to read.

    ``attempt_context`` is what the caller observed about the attempt's end conditions. It is
    required and never inferred here: a caller that only has persisted rows passes
    ``AttemptContext.unknown(...)``, so the record states that the deadline and the
    finalization step were not recovered instead of writing "not fired" for them.

    Callers that only have persisted rows (a backfill of an earlier Run) pass no limits and no
    model: the record then says those facts were not recovered instead of guessing them.

    Returns the record, the stated reason and whether the deadline cancelled the execution.
    """
    decision = classify_outcome(
        session, run_id, deadline_fired=deadline_fired,
        independent_suite_version=independent_suite_version,
        artifact_root=artifact_root, finalization=finalization)
    outcome, formal, reason = decision.outcome, decision.formal, decision.reason
    deadline_cancelled = (error is not None and deadline_fired
                          and isinstance(error, GitExecutionCancelledError))
    if error is not None and not deadline_cancelled:
        # An interrupted attempt leaves evidence instead of vanishing, but it is not a
        # failure verdict: nothing proved the task failed.
        outcome, formal = RunOutcome.INCOMPLETE, None
        reason = f"execution_error:{type(error).__name__}"
    elif deadline_cancelled and not entered_execution:
        # The budget expired while the workspace was still being prepared.
        reason = "deadline_during_initialization"
    if limits is None:
        limits_note = "limits: not recovered by this entry"
    else:
        limits_note = (f"limits: duration={limits.max_duration_seconds}s, "
                       f"tokens={limits.max_model_tokens}, stages<={limits.max_stages}, "
                       f"attempts=1, auto_retries=0, deadline_fired={deadline_fired}")
    record = BenchmarkRunRecord(
        schema_version=SCHEMA_VERSION,
        pack_id=manifest.pack_id, base_commit=manifest.base_commit,
        attempt_index=attempt_index,
        task_id=str(task_id), run_id=str(run_id),
        started_at=started_at, finished_at=finished_at or _now(), outcome=outcome,
        model_id=model_id or _model_id_from_environment(), policy_version=policy_version,
        operator=operator, public_checks=decision.public_checks,
        independent_evaluation=decision.independent, formal=formal,
        human_review=HumanReview(),
        artifacts=(str(artifacts_root / str(run_id)),),
        attempt_kind=attempt_kind,
        attempt_context=attempt_context,
        notes=(limits_note
               + (f", reason={reason}" if reason else "")
               + stage_notes
               + (f", finalization={finalization.get('outcome')}"
                  + (f" ({finalization.get('reason')})" if finalization.get("reason") else "")
                  if finalization else "")
               + extra_notes))
    return record, reason, deadline_cancelled


def _verified_execution_sha256(request: BenchmarkRunRequest) -> str | None:
    """Verify the pinned execution manifest before the Task is claimed."""
    try:
        return verify_optional_binding(
            manifest_path=request.execution_manifest,
            expected_sha256=request.execution_sha256, require_complete=True)
    except ExecutionBindingError as error:
        raise BenchmarkRunError(f"Execution binding refused: {error}") from None


def run_one_task(
    request: BenchmarkRunRequest, *, session_factory, worker, gateways, heartbeat_config,
    max_read_bytes: int = 65536, max_patch_bytes: int | None = None,
    verifier_max_diff_bytes: int | None = None, drive=None,
    independent_evaluation=None,
) -> dict:
    """Claim one Run, drive it stage by stage under the limits, and write the record.

    ``drive`` may replace the stage driver for tests. The driver itself lives in the
    orchestration layer; this entry only supplies configuration, limits and the record.
    """
    try:
        attempt_kind = AttemptKind(request.attempt_kind)
    except ValueError:
        raise BenchmarkRunError(
            "attempt_kind must be formal, rehearsal or unknown") from None
    manifest = load_manifest(request.pack_dir)
    config = to_verification_config(
        manifest, task_id=request.task_id, public_tests_root=request.pack_dir,
        python_executable=request.python_executable)
    with session_factory() as session:
        task = session.get(Task, request.task_id)
        if task is None:
            raise BenchmarkRunError("Task does not exist")
        if task.base_commit != manifest.base_commit:
            raise BenchmarkRunError("Task base commit does not match the task pack")
        if not optional_scope_binding_matches(task.admission_record, manifest):
            raise BenchmarkRunError("Task scope binding does not match the task pack")
        admission_snapshot = deepcopy(task.admission_record)
        if request.material_path is not None:
            # Same rule as the worker entry, and it runs before the claim: the material must
            # not sit in the pack, the Run's roots, or a local checkout of this Task's repo.
            try:
                require_material_placement(
                    request.material_path, pack_dir=request.pack_dir,
                    workspaces_root=request.workspaces_root,
                    artifacts_root=request.artifacts_root, repository=task.repo)
            except MaterialPlacementError as error:
                raise BenchmarkRunError(str(error)) from None
        policy = PolicyConfig.model_validate(task.policy_snapshot)
        if policy.budget.max_llm_tokens_per_run > request.limits.max_model_tokens:
            raise BenchmarkRunError(
                "Task policy allows more tokens than this run's limit; refusing to start")

    execution_sha256 = _verified_execution_sha256(request)
    claim_key = f"benchmark:{request.task_id}:1"
    with session_factory() as session:
        try:
            run, created = RunCoordinator(session).start_initial_run(
                request.task_id, worker.worker_id, claim_key,
                expected_base_commit=manifest.base_commit, check_admission_record=True,
                expected_admission_record=admission_snapshot,
                execution_sha256=execution_sha256)
        except (ValueError, LookupError) as error:
            raise BenchmarkRunError(str(error) or "The Task cannot be claimed") from None
        run_id = run.id
        run_task_id = run.task_id
    if run_task_id != request.task_id:
        raise BenchmarkRunError("Claimed Run belongs to a different Task")
    if not created:
        # Same request, same attempt: re-running would be an automatic retry.
        return {"outcome": "existing_run", "run_id": str(run_id), "record_path": None,
                "execution_sha256": execution_sha256}
    started_at = _now()
    fired = {"deadline": False}
    entered = {"execution": False}
    stage_result: dict = {}
    # The budget covers the whole attempt, including workspace initialization, so the
    # timer starts here and signals an event the worker checks before and after it.
    deadline_stop = Event()
    execution_stop: dict = {"event": None}

    def expire():
        fired["deadline"] = True
        deadline_stop.set()
        # If a stage is already running, its own stop event must see the deadline too.
        if execution_stop["event"] is not None:
            execution_stop["event"].set()

    deadline_timer = Timer(request.limits.max_duration_seconds, expire)
    deadline_timer.start()

    def process(directories, actual_commit, stop):
        # The deadline must also stop the *execution's* stop event, or nothing downstream
        # would observe it once the stage loop starts.
        entered["execution"] = True
        execution_stop["event"] = stop
        if fired["deadline"]:
            stop.set()

        if drive is not None:
            drive(directories, stop)
        else:
            stage_result.update(drive_run_stages(
                session_factory, run_id, worker=worker, gateways=gateways,
                directories=directories, stop_event=stop, verification_config=config,
                max_stages=request.limits.max_stages, max_read_bytes=max_read_bytes,
                max_patch_bytes=max_patch_bytes,
                verifier_max_diff_bytes=verifier_max_diff_bytes))
        # The formal evaluation runs inside the same heartbeat, lease and deadline as the
        # stages: it needs a live stop event and must not outlive the attempt's budget.
        # A failure here is captured, not lost, so a record still gets written.
        if attempt_kind is not AttemptKind.FORMAL:
            # A rehearsal exercises the environment; committing a Verification or leaving the
            # Run as a formal pass would contradict the label the operator chose.
            stage_result["finalization"] = {"outcome": "skipped",
                                            "reason": "attempt_kind_not_formal"}
            return None
        try:
            stage_result["finalization"] = finalize_benchmark_run(
                worker, run_id, directories=directories, verification_config=config,
                material=independent_evaluation, max_diff_bytes=verifier_max_diff_bytes,
                max_patch_bytes=max_patch_bytes, stop_event=stop)
        except Exception as error:  # recorded below, then re-raised unchanged
            stage_result["finalization"] = {
                "outcome": "finalization_error", "reason": type(error).__name__}
            raise
        return None

    error = None
    try:
        worker.execute_run(
            run_id, workspaces_root=request.workspaces_root,
            artifacts_root=request.artifacts_root, heartbeat_config=heartbeat_config,
            process_run=process, external_stop_event=deadline_stop,
            execution_manifest=request.execution_manifest,
            execution_sha256=execution_sha256)
    except Exception as caught:  # recorded below, then re-raised unchanged
        error = caught
    finally:
        deadline_timer.cancel()
        deadline_fired = fired["deadline"]

    # A stage loop that never started a finalize call still reports why no formal
    # result was attempted. That placeholder describes the absence for the classifier; the
    # attempt context records the absence itself, not a step that ran.
    observed_finalization = stage_result.get("finalization")
    finalization = observed_finalization or {
        "outcome": "skipped",
        "reason": "not_attempted" if error is None else "execution_error"}

    with session_factory() as session:
        record, reason, deadline_cancelled = compose_run_record(
            session, run_id, manifest=manifest, task_id=request.task_id,
            independent_suite_version=config.independent_suite_version,
            policy_version=policy.policy_version, operator=request.operator,
            attempt_kind=attempt_kind, artifact_root=request.artifacts_root / str(run_id),
            finalization=finalization, deadline_fired=deadline_fired,
            attempt_context=AttemptContext.observed(
                deadline_fired=deadline_fired, finalization=observed_finalization,
                execution_sha256=execution_sha256, source="benchmark_attempt"),
            artifacts_root=request.artifacts_root, started_at=started_at,
            limits=request.limits, error=error,
            entered_execution=entered["execution"],
            stage_notes=(f", stages={stage_result.get('stages')}, "
                         f"stopped={stage_result.get('reason')}" if stage_result else ""))
    write_run_record(record, request.record_path)
    if error is not None and not deadline_cancelled:
        raise error
    return {"outcome": record.outcome.value, "run_id": str(run_id),
            "record_path": str(request.record_path), "deadline_fired": deadline_fired,
            "execution_sha256": execution_sha256,
            "attempt_kind": attempt_kind.value,
            "reason": reason, "stages": stage_result.get("stages"),
            "stopped": stage_result.get("reason"), "finalization": finalization}


MATERIAL_FIELDS = {"suite_version", "image", "command", "cases", "calibration", "timeout_seconds"}
CALIBRATION_FIELDS = {"baseline_workspace", "corrected_workspace", "expected_commit"}
CASE_FIELDS = {"stdin_base64", "stdout_base64"}


def load_independent_material(
    path: Path, *, expected_suite_version: str | None = None,
    expected_sha256: str | None = None,
) -> tuple[IndependentEvaluationMaterial, str]:
    """Load trusted evaluation material from a file the operator names at run time.

    The file's raw bytes are digested **before** anything is parsed and compared with the
    digest the pack pins: the digest identifies the exact bytes that were calibrated, so a
    different file — however plausible its content — must not be used. A missing expected
    digest is refused too: without one there is nothing to compare against, and "no digest"
    must never degrade into "any material will do".

    The bytes are then parsed once and never logged: the cases keep ``repr=False`` inside
    ``PrivateCase`` and never leave the material object. A declared suite version that
    disagrees with the pack is a configuration error as well.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise BenchmarkRunError(
            f"Independent material is unreadable: {type(error).__name__}") from None
    if expected_sha256 is None:
        raise BenchmarkRunError(
            "The pack does not pin a material digest; refusing to use an unverifiable file")
    actual_sha256 = sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BenchmarkRunError(
            "Independent material does not match the pack's materials_sha256")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchmarkRunError(
            f"Independent material is unreadable: {type(error).__name__}") from None
    if not isinstance(payload, dict) or set(payload) != MATERIAL_FIELDS:
        raise BenchmarkRunError("Independent material must have exactly the documented fields")
    suite_version = payload["suite_version"]
    if not isinstance(suite_version, str) or not suite_version.strip():
        raise BenchmarkRunError("Independent material needs a suite version")
    if expected_suite_version is not None and suite_version != expected_suite_version:
        raise BenchmarkRunError("Independent material does not match the pack's suite version")
    image, command = payload["image"], payload["command"]
    if not isinstance(image, str) or not image.strip():
        raise BenchmarkRunError("Independent material needs an image")
    if (not isinstance(command, list) or not command
            or any(not isinstance(part, str) or not part for part in command)):
        raise BenchmarkRunError("Independent material needs a non-empty adapter command")
    calibration = payload["calibration"]
    if not isinstance(calibration, dict) or set(calibration) != CALIBRATION_FIELDS:
        raise BenchmarkRunError("Independent material needs both calibration workspaces")
    commit = calibration["expected_commit"]
    if not isinstance(commit, str) or len(commit) != 40 or any(
            character not in "0123456789abcdef" for character in commit):
        raise BenchmarkRunError("Calibration needs the corrected commit as a 40-character hash")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise BenchmarkRunError("Independent material needs at least one case")
    decoded = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise BenchmarkRunError("Each case needs stdin_base64 and stdout_base64")
        try:
            decoded.append(PrivateCase(
                stdin=base64.b64decode(case["stdin_base64"], validate=True),
                expected_stdout=base64.b64decode(case["stdout_base64"], validate=True)))
        except (TypeError, ValueError):
            raise BenchmarkRunError("Case payloads must be base64") from None
    timeout_seconds = payload["timeout_seconds"]
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0):
        raise BenchmarkRunError("Each case timeout must be a positive number")
    try:
        material = IndependentEvaluationMaterial(
            image=image, command=tuple(command), cases=tuple(decoded),
            calibration_workspaces=(
                Path(calibration["baseline_workspace"]),
                Path(calibration["corrected_workspace"]), commit),
            timeout_seconds=float(timeout_seconds))
    except (TypeError, ValueError) as error:
        raise BenchmarkRunError(f"Independent material is invalid: {type(error).__name__}") from None
    return material, suite_version


def policy_snapshot_differences(stored_snapshot: dict, prepared: PolicyConfig) -> list[str]:
    """Return the top-level Policy sections where the Task and the file disagree.

    The Task keeps the Policy it was created with; the operator keeps the file. If these
    ever differ, every later limit check would be about a different policy than the one
    being reviewed, so the differences are named instead of compared field by field.
    """
    stored = PolicyConfig.model_validate(stored_snapshot).model_dump()
    prepared_dump = prepared.model_dump()
    return sorted(key for key in set(stored) | set(prepared_dump)
                  if stored.get(key) != prepared_dump.get(key))


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "benchmark_run_configuration_error", "detail": detail}),
          file=sys.stderr)
    return 2


def main(
    argv: list[str] | None = None, *, session_factory=None, gateways_factory=None,
) -> int:
    """Run exactly one benchmark attempt and report where its evidence went.

    Exit codes: 0 when an attempt was recorded (whatever its outcome), 2 when the
    configuration is unusable and nothing was attempted, 1 when the attempt failed at
    runtime. Always one attempt: there is no retry flag to pass.
    """
    parser = argparse.ArgumentParser(
        description="Run one benchmark attempt under the pinned limits; one attempt, no retry.")
    parser.add_argument("--pack", required=True, help="Task-pack directory.")
    parser.add_argument("--task-id", required=True, help="UUID of the Task created from this pack.")
    parser.add_argument("--operator", required=True, help="Person or account running the attempt.")
    parser.add_argument("--python-executable", required=True,
                        help="Absolute interpreter used for the public checks.")
    parser.add_argument("--workspaces-root", required=True, help="Existing directory for Run workspaces.")
    parser.add_argument("--artifacts-root", required=True, help="Existing directory for Run artifacts.")
    parser.add_argument("--record", required=True, help="Where the run record must be written.")
    parser.add_argument("--material", help="Trusted independent-evaluation material (JSON). "
                                          "Read once, never logged; keep it outside the repository.")
    parser.add_argument("--policy", help="Prepared Policy snapshot; when given, the Task's "
                                         "stored snapshot must match it exactly.")
    parser.add_argument("--worker-id", default="benchmark-worker",
                        help="Actor recorded on the Run; defaults to benchmark-worker.")
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    parser.add_argument("--max-patch-bytes", type=int, default=65536)
    parser.add_argument("--max-diff-bytes", type=int, default=262144)
    parser.add_argument("--statement-timeout-seconds", type=int, default=5)
    parser.add_argument("--shutdown-timeout-seconds", type=int, default=5)
    parser.add_argument("--attempt-kind", choices=["formal", "rehearsal"], default="formal",
                        help="'rehearsal' marks an environment check; summaries exclude it by "
                             "default so a rehearsal never enters the formal sample.")
    parser.add_argument("--execution-manifest",
                        help="Pinned file inventory for the code this entry runs (JSON from "
                             "`python -m devflow_ai.execution_manifest create`). Verified "
                             "against the running package before the Task is claimed.")
    parser.add_argument("--execution-sha256",
                        help="The pinned manifest digest, kept separately from the manifest "
                             "file. Supplied together with --execution-manifest; the verified "
                             "digest is written to the Run.")
    parser.add_argument("--confirm-run", action="store_true",
                        help="Required: acknowledges that this spends the pinned budget on a real run.")
    args = parser.parse_args(argv)

    if not args.confirm_run:
        return _configuration_error("--confirm-run is required to start a real attempt")
    if not args.worker_id.strip():
        return _configuration_error("worker-id must be a non-blank string")
    if not args.operator.strip():
        return _configuration_error("operator must be a non-blank string")
    for name in ("max_read_bytes", "max_patch_bytes", "max_diff_bytes",
                 "statement_timeout_seconds", "shutdown_timeout_seconds"):
        value = getattr(args, name)
        if isinstance(value, bool) or value <= 0:
            return _configuration_error(f"{name.replace('_', '-')} must be a positive integer")
    for name in ("workspaces_root", "artifacts_root"):
        if not Path(getattr(args, name)).is_dir():
            return _configuration_error(f"{name.replace('_', '-')} must be an existing directory")
    if Path(args.record).exists():
        # Refuse before spending anything: a run record is evidence and is never overwritten,
        # and "the file already exists" must not be reported as this attempt's record.
        return _configuration_error(
            "The record path already exists; a run record is evidence and is never overwritten")
    try:
        task_id = UUID(str(args.task_id))
    except (TypeError, ValueError):
        return _configuration_error("task-id must be a UUID")
    try:
        manifest = load_manifest(Path(args.pack))
    except ValueError as error:
        return _configuration_error(f"Task pack is invalid: {type(error).__name__}")

    material = None
    suite_version = manifest.independent_suite.suite_version if manifest.independent_suite else None
    material_sha256 = (manifest.independent_suite.materials_sha256
                       if manifest.independent_suite is not None else None)
    if args.material:
        try:
            # The roots are known without a database; the Task's repository is checked again
            # with the Task in hand, before the claim. A remote repository cannot place a
            # local file, which is stated as a limit rather than assumed safe.
            require_material_placement(
                Path(args.material), pack_dir=Path(args.pack),
                workspaces_root=Path(args.workspaces_root),
                artifacts_root=Path(args.artifacts_root), repository=None)
        except MaterialPlacementError as error:
            return _configuration_error(str(error))
        try:
            material, material_version = load_independent_material(
                Path(args.material), expected_suite_version=suite_version,
                expected_sha256=material_sha256)
        except BenchmarkRunError as error:
            return _configuration_error(str(error))
        suite_version = material_version
    elif suite_version is not None:
        print(json.dumps({"event": "benchmark_run_warning",
                          "detail": "No independent material was supplied; the formal gate will "
                                    "refuse to finalize this attempt."}), file=sys.stderr)
    if args.attempt_kind == "rehearsal":
        print(json.dumps({"event": "benchmark_run_warning",
                          "detail": "This attempt is marked as a rehearsal; summaries exclude it "
                                    "from the formal sample."}), file=sys.stderr)
    if manifest.external_connections == "unknown":
        print(json.dumps({"event": "benchmark_run_warning",
                          "detail": "external_connections is unknown; formal acceptance cannot "
                                    "pass until a trusted verifier declares it."}), file=sys.stderr)

    request = BenchmarkRunRequest(
        pack_dir=Path(args.pack), task_id=task_id, operator=args.operator,
        python_executable=args.python_executable,
        workspaces_root=Path(args.workspaces_root), artifacts_root=Path(args.artifacts_root),
        record_path=Path(args.record), attempt_kind=args.attempt_kind,
        material_path=Path(args.material) if args.material else None,
        execution_manifest=(Path(args.execution_manifest)
                            if args.execution_manifest else None),
        execution_sha256=args.execution_sha256)
    engine = None
    try:
        if session_factory is None:
            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        # A benchmark Task carries exactly one attempt. Say so plainly instead of letting the
        # claim key return the first Run: the caller would see "existing_run" and might believe
        # a second, independent attempt had happened. This check is a courtesy; the claim in
        # ``run_one_task`` stays the authority, so a session that cannot answer is not fatal.
        try:
            with session_factory() as session:
                existing_run = session.scalar(
                    select(Run.id).where(Run.task_id == task_id).limit(1))
        except Exception:
            existing_run = None
        if existing_run is not None:
            return _configuration_error(
                "This Task already has a Run; a second attempt needs a new Task so both "
                "records stay independent")
        with ExitStack() as stack:
            try:
                gateways = stack.enter_context(
                    gateways_factory(session_factory, args.worker_id) if gateways_factory is not None
                    else model_gateways_from_env(session_factory, args.worker_id))
            except ValueError:
                return _configuration_error("Model gateways are not configured")
            if args.policy:
                try:
                    prepared = load_policy_snapshot(Path(args.policy))
                except BenchmarkTaskError:
                    return _configuration_error("Prepared Policy snapshot is unreadable")
                with session_factory() as session:
                    task = session.get(Task, task_id)
                    if task is None or task.policy_snapshot is None:
                        return _configuration_error("Task does not exist or has no pinned Policy")
                    differing = policy_snapshot_differences(task.policy_snapshot, prepared)
                if differing:
                    return _configuration_error(
                        "Task policy snapshot differs from the prepared one in: "
                        + ", ".join(differing))
            else:
                print(json.dumps({"event": "benchmark_run_warning",
                                  "detail": "No --policy given; the Task's stored snapshot was "
                                            "not compared with the prepared file."}),
                      file=sys.stderr)
            result = run_one_task(
                request, session_factory=session_factory,
                worker=Worker(session_factory, args.worker_id), gateways=gateways,
                heartbeat_config=HeartbeatConfig(
                    statement_timeout_seconds=args.statement_timeout_seconds,
                    shutdown_timeout_seconds=args.shutdown_timeout_seconds),
                max_read_bytes=args.max_read_bytes, max_patch_bytes=args.max_patch_bytes,
                verifier_max_diff_bytes=args.max_diff_bytes,
                independent_evaluation=material)
    except BenchmarkRunError as error:
        return _configuration_error(str(error))
    except Exception as error:  # the record, when written, is the attempt's evidence
        print(json.dumps({
            "event": "benchmark_run_failed", "error_type": type(error).__name__,
            "record_written": Path(args.record).exists(), "record_path": str(args.record),
        }), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps({
        "event": "benchmark_run_finished", "outcome": result.get("outcome"),
        "run_id": result.get("run_id"), "record_path": result.get("record_path"),
        "reason": result.get("reason"), "stages": result.get("stages"),
        "stopped": result.get("stopped"), "finalization": result.get("finalization"),
        "deadline_fired": result.get("deadline_fired"),
        "suite_version": suite_version, "operator": args.operator,
        "attempt_kind": args.attempt_kind,
        "external_connections": manifest.external_connections,
        "limits": {"max_duration_seconds": request.limits.max_duration_seconds,
                   "max_model_tokens": request.limits.max_model_tokens,
                   "max_stages": request.limits.max_stages,
                   "attempts": request.limits.attempts,
                   "auto_retries": request.limits.auto_retries},
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
