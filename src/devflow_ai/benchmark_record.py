"""Run records for benchmark attempts: evidence, not prose.

A record states what happened, which trusted evidence locates it, and whether a human
accepted the result. ``extra="forbid"`` only rejects fields the schema does not define;
it does not stop anyone from pasting hidden content into a free-text field such as
``notes``. Isolation of hidden material is therefore guaranteed by the trusted control
plane holding and injecting it — never by this schema, and never by validation.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMMIT_PATTERN = r"^[0-9a-f]{40}$"

# Version 2 introduced: attempt_kind, independent_evaluation (incl. `verified`), correction,
# task_id/run_id and public_checks.verified_*. Version 3 adds attempt_context. A file without
# the field is read as version 1: the older fields keep their meaning, the newer ones are simply
# absent, defaults apply, and the record is reported as unversioned rather than assumed to carry
# the newer contract.
SCHEMA_VERSION = 3


class RunOutcome(StrEnum):
    """The four verdicts a run can reach, plus an explicit "not concluded" state.

    ``incomplete`` exists so a run that is still going, blocked, or interrupted is not
    quietly filed as a failure: only a verdict backed by the matching evidence may use
    one of the four final states.
    """

    FORMAL_PASS = "formal_pass"
    PUBLIC_ONLY_PASS = "public_only_pass"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INCOMPLETE = "incomplete"


class AttemptKind(StrEnum):
    """What kind of attempt this row describes.

    ``unknown`` is the default for records written before the field existed. It is not a
    synonym for ``formal``: an attempt whose kind nobody recorded must not be counted as a
    formal sample, so both ``rehearsal`` and ``unknown`` stay out of the success rate
    unless a reader explicitly asks for them.
    """

    FORMAL = "formal"
    REHEARSAL = "rehearsal"
    UNKNOWN = "unknown"


class AttemptContext(BaseModel):
    """What the process that ran the attempt knew about how it ended.

    Two facts decide a verdict that the database rows cannot answer afterwards: whether the
    attempt's deadline fired, and whether a finalization result existed. Nothing persists the
    deadline, and a finalization dict only lives in the running process, so the attempt's own
    entry is the only witness. Every field therefore has an explicit ``unknown`` value: a
    record written before this field existed, or a reconstruction that cannot see the original
    context, reads as unknown rather than as "no deadline, no finalization".

    ``source`` names who observed it — the entry that ran the attempt, or a reconstruction.
    It is a label, not a proof: a reader still decides whether that source was in a position
    to know.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deadline: Literal["fired", "not_fired", "unknown"] = "unknown"
    finalization: Literal["recorded", "not_recorded", "unknown"] = "unknown"
    finalization_outcome: str | None = Field(default=None, min_length=1, max_length=100)
    # The execution manifest digest this attempt ran under, when the entry verified one. It ties
    # the record to the version claim: a record copied from another version disagrees here.
    execution_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    source: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def only_a_recorded_finalization_has_an_outcome(self) -> "AttemptContext":
        if self.finalization != "recorded" and self.finalization_outcome is not None:
            raise ValueError(
                "attempt_context.finalization_outcome is only meaningful when a finalization "
                "was recorded")
        return self

    @classmethod
    def observed(
        cls, *, deadline_fired: bool, finalization: dict | None, source: str,
        execution_sha256: str | None = None,
    ) -> "AttemptContext":
        """Build the context from what the running entry itself just observed."""
        recorded = isinstance(finalization, dict)
        outcome = None
        if recorded:
            raw = finalization.get("outcome")
            if isinstance(raw, str) and raw.strip():
                outcome = raw.strip()[:100]
        return cls(
            deadline="fired" if deadline_fired else "not_fired",
            finalization="recorded" if recorded else "not_recorded",
            finalization_outcome=outcome, execution_sha256=execution_sha256, source=source)

    @classmethod
    def unknown(cls, *, source: str, execution_sha256: str | None = None) -> "AttemptContext":
        """Build the context for a writer that did not see the attempt itself.

        ``execution_sha256`` may still be passed when the *persisted* Run row records one: that
        is a stored fact, not a reconstruction, and the caller says so through ``source``.
        """
        return cls(deadline="unknown", finalization="unknown",
                   execution_sha256=execution_sha256, source=source)


class PublicCheckSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_failed: int | None = Field(default=None, ge=0)
    target_passed: int | None = Field(default=None, ge=0)
    # What the Verifier checkpoint actually recorded, and where to re-check it. A count of
    # zero failures without these references would be a claim with nothing behind it.
    verified_outcome: Literal["passed", "failed", "unavailable"] | None = None
    verifier_execution_id: str | None = None
    draft_artifact_id: str | None = None
    notes: str = ""


class IndependentEvaluationEvidence(BaseModel):
    """What the trusted evaluation did, or why it could not run.

    ``unavailable`` must carry a reason: "we could not evaluate" is information, but only
    if it says why. A ``failed`` outcome is evidence of a verdict and therefore needs a
    calibrated suite and a receipt that could be read back from disk.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["passed", "failed", "unavailable"]
    # True only when a receipt was read back from disk and matched both the pinned suite
    # version and the draft being judged. Without that, the claimed outcome is a claim.
    verified: bool = False
    calibrated: bool = False
    receipt_artifact_id: str | None = None
    suite_version: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def an_unavailable_needs_a_reason(self) -> "IndependentEvaluationEvidence":
        if self.outcome == "unavailable" and not (self.reason or "").strip():
            raise ValueError("An unavailable evaluation needs a stated reason")
        return self


class FormalEvidence(BaseModel):
    """References to the trusted formal result, never the hidden material itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_id: str = Field(min_length=1)
    independent_suite_version: str = Field(min_length=1)
    independent_receipt_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class HumanReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conclusion: Literal["pending", "accepted", "rejected"] = "pending"
    reviewer: str | None = None
    reviewed_at: str | None = None

    @model_validator(mode="after")
    def an_acceptance_needs_a_named_reviewer(self) -> "HumanReview":
        """An acceptance without who and when is not evidence."""
        if self.conclusion != "accepted":
            return self
        if not (self.reviewer or "").strip() or not (self.reviewed_at or "").strip():
            raise ValueError("An accepted review requires a reviewer and a review time")
        return self


class RecordCorrection(BaseModel):
    """A traceable repair of one record's derived summary.

    The original record and the human-reviewed copy are evidence and are never rewritten;
    a correction is a third file that points back at both by digest and states what the new
    summary was derived from. It does not change the verdict or the review.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    corrects_sha256: str = Field(pattern=SHA256_PATTERN)
    reviewed_sha256: str = Field(pattern=SHA256_PATTERN)
    basis: str = Field(min_length=1)
    corrected_at: str = Field(min_length=1)
    corrected_by: str = Field(min_length=1)


class BenchmarkRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, strict=True)
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    base_commit: str = Field(pattern=COMMIT_PATTERN)
    # Which Task and Run this record describes. Optional so older records still parse, but
    # when present a reader (or a correction) can reach the database rows without guessing.
    task_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    attempt_index: int = Field(ge=1)
    # Defaulting to unknown keeps old records readable without assuming they were formal.
    attempt_kind: AttemptKind = AttemptKind.UNKNOWN
    # Absent on records written before the field existed, which reads as "unknown" rather than
    # as a statement about the deadline or the finalization step.
    attempt_context: AttemptContext | None = None
    started_at: str = Field(min_length=1)
    finished_at: str = Field(min_length=1)
    outcome: RunOutcome
    model_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    public_checks: PublicCheckSummary = Field(default_factory=PublicCheckSummary)
    independent_evaluation: IndependentEvaluationEvidence | None = None
    formal: FormalEvidence | None = None
    human_review: HumanReview = Field(default_factory=HumanReview)
    correction: RecordCorrection | None = None
    artifacts: tuple[str, ...] = ()
    notes: str = ""

    @model_validator(mode="after")
    def require_the_evidence_a_claim_needs(self) -> "BenchmarkRunRecord":
        if self.outcome is RunOutcome.FORMAL_PASS and self.formal is None:
            raise ValueError("A formal pass requires its verification references")
        if self.outcome in (RunOutcome.FORMAL_PASS, RunOutcome.PUBLIC_ONLY_PASS) \
                and not self.artifacts:
            raise ValueError("A passing run must point at its artifacts")
        if self.outcome is RunOutcome.PUBLIC_ONLY_PASS:
            # "Public checks passed but the trusted evaluation could not run" is only
            # honest with both halves recorded: the verified public result and the reason
            # the evaluation was unavailable.
            if self.public_checks.verified_outcome != "passed":
                raise ValueError(
                    "A public-only pass requires the verified public result to be passed")
            if (self.independent_evaluation is None
                    or self.independent_evaluation.outcome != "unavailable"
                    or not self.independent_evaluation.verified
                    or not (self.independent_evaluation.reason or "").strip()):
                raise ValueError(
                    "A public-only pass requires a verified attempt that came back unavailable, "
                    "with a stated reason")
        if (self.outcome is RunOutcome.FAILED and self.independent_evaluation is not None
                and self.independent_evaluation.outcome == "failed"
                and not (self.independent_evaluation.verified
                         and self.independent_evaluation.calibrated)):
            raise ValueError(
                "An independent failure needs a verified, calibrated receipt to be a verdict")
        return self

    @property
    def counts_as_solved(self) -> bool:
        """Only a formal pass a human accepted counts as a solved task.

        The attempt also has to be a formal one: a rehearsal that happened to end in a
        formal pass still is not a formal sample.
        """
        return (self.outcome is RunOutcome.FORMAL_PASS
                and self.attempt_kind is AttemptKind.FORMAL
                and self.human_review.conclusion == "accepted")


def write_run_record(record: BenchmarkRunRecord, path: Path) -> Path:
    """Create one record with an exclusive create; a run record is evidence.

    Exclusive creation, not a prior existence check: two writers racing on the same
    path must not be able to overwrite each other's evidence.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError:
        raise ValueError(f"Refusing to overwrite an existing run record: {path.name}") from None
    return path


def parse_run_record(data: bytes) -> BenchmarkRunRecord:
    """Parse one record from bytes already read, refusing a version this code does not know.

    Callers that also digest the file pass the same bytes here, so the digest and the parsed
    content cannot describe two different files.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Run record is not valid JSON: {getattr(error, 'msg', '')}".strip()) from None
    if not isinstance(payload, dict):
        raise ValueError("Run record must be a JSON object")
    version = payload.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("Run record has an invalid schema_version")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"Run record schema_version {version} is newer than this code ({SCHEMA_VERSION})")
    return BenchmarkRunRecord.model_validate(payload)


def load_run_record(path: Path) -> BenchmarkRunRecord:
    """Read one record from disk; ``parse_run_record`` holds the version policy."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ValueError(f"Run record is unreadable: {type(error).__name__}") from None
    return parse_run_record(data)


def is_formal_sample(record: BenchmarkRunRecord) -> bool:
    """Only an attempt the recorder marked as formal enters the formal sample."""
    return record.attempt_kind is AttemptKind.FORMAL


def _attempt_identity(record: BenchmarkRunRecord) -> tuple:
    """One attempt, however many files describe it."""
    if record.run_id:
        return ("run", record.run_id)
    return ("attempt", record.pack_id, record.base_commit, record.attempt_index,
            record.started_at)


_IMMUTABLE_RECORD_FIELDS = (
    "pack_id", "base_commit", "task_id", "run_id", "attempt_index", "attempt_kind",
    "started_at", "finished_at", "outcome", "model_id", "policy_version", "operator",
    "formal", "artifacts", "attempt_context",
)


def _immutable_record_differences(first: BenchmarkRunRecord, second: BenchmarkRunRecord) -> list[str]:
    return [name for name in _IMMUTABLE_RECORD_FIELDS if getattr(first, name) != getattr(second, name)]


def deduplicate_records(loaded, *, include_superseded: bool = False) -> tuple[list, dict]:
    """Pick one record per attempt and say what was dropped.

    Originals, human-reviewed copies and corrected copies all describe the same attempt, and
    a correction names both of its sources by digest. Prefer, in order: the copy nothing
    supersedes, then a corrected copy, then a copy carrying a human decision. Copies must
    agree on immutable attempt fields, and a correction may supersede only files for the
    same attempt. Anything ambiguous is reported as a conflict and excluded. Missing
    referenced files and claims in a record still require trusted external verification.
    """
    loaded = list(loaded)
    # Public callers can supply a previously parsed object. Re-read each file once and
    # verify that the exact bytes used for its digest still describe that object.
    digests = {}
    seen_paths = set()
    for path, record in loaded:
        canonical = Path(path).resolve()
        if canonical in seen_paths:
            raise ValueError("The same run record path was supplied more than once")
        seen_paths.add(canonical)
        try:
            raw = Path(path).read_bytes()
            current = parse_run_record(raw)
        except (OSError, ValueError) as error:
            raise ValueError(f"Run record cannot be read consistently: {Path(path).name}") from error
        if current != record:
            raise ValueError(f"Run record changed after it was read: {Path(path).name}")
        digests[path] = sha256(raw).hexdigest()
    if include_superseded:
        return [record for _, record in loaded], {
            "superseded_excluded": 0, "conflicts": [], "conflicts_excluded": 0}
    groups: dict[tuple, list] = {}
    for path, record in loaded:
        groups.setdefault(_attempt_identity(record), []).append((path, record))
    digest_identities: dict[str, set[tuple]] = {}
    for path, record in loaded:
        digest_identities.setdefault(digests[path], set()).add(_attempt_identity(record))
    superseded: dict[tuple, set[str]] = {}
    invalid_links: set[tuple] = set()
    for _, record in loaded:
        if record.correction is None:
            continue
        identity = _attempt_identity(record)
        references = (record.correction.corrects_sha256, record.correction.reviewed_sha256)
        if any(digest_identities.get(digest, set()) - {identity} for digest in references):
            invalid_links.add(identity)
            continue
        superseded.setdefault(identity, set()).update(references)
    chosen, dropped, conflicts = [], 0, []
    for identity, items in groups.items():
        if identity in invalid_links:
            conflicts.append({"identity": list(identity), "files": sorted(p.name for p, _ in items),
                              "reason": "a correction references a different attempt"})
            continue
        differences = sorted({name for _, record in items[1:]
                              for name in _immutable_record_differences(items[0][1], record)})
        if differences:
            conflicts.append({"identity": list(identity), "files": sorted(p.name for p, _ in items),
                              "reason": "copies disagree on immutable fields: " + ", ".join(differences)})
            continue
        live = [(path, record) for path, record in items if digests[path] not in superseded.get(identity, set())]
        dropped += len(items) - len(live)
        if len(live) == 1:
            chosen.append(live[0][1])
            continue
        if not live:
            conflicts.append({"identity": list(identity), "files": sorted(p.name for p, _ in items),
                              "reason": "every copy is superseded by a file not in this set"})
            continue
        corrected = [(path, record) for path, record in live if record.correction is not None]
        if len(corrected) == 1:
            chosen.append(corrected[0][1])
            dropped += len(live) - 1
            continue
        if len(corrected) > 1:
            conflicts.append({"identity": list(identity),
                              "files": sorted(p.name for p, _ in corrected),
                              "reason": "more than one correction claims the same attempt"})
            continue
        decided = [(path, record) for path, record in live
                   if record.human_review.conclusion != "pending"]
        if len(decided) == 1:
            chosen.append(decided[0][1])
            dropped += len(live) - 1
            continue
        conflicts.append({"identity": list(identity), "files": sorted(p.name for p, _ in live),
                          "reason": "copies of one attempt cannot be ordered"})
    return chosen, {"superseded_excluded": dropped, "conflicts": conflicts,
                    "conflicts_excluded": len(conflicts)}


def summarize_run_records(
    records: list[BenchmarkRunRecord], *, include_non_formal: bool = False,
) -> dict:
    """Count outcomes and report the sample size next to any success figure.

    Rehearsals and records whose kind nobody recorded stay out of the sample unless asked
    for: mixing either into the success count would overstate the sample without adding a
    real formal attempt. Both are counted separately so the exclusion is visible.
    """
    non_formal = [item for item in records if not is_formal_sample(item)]
    if not include_non_formal:
        records = [item for item in records if is_formal_sample(item)]
    outcomes = {item.value: 0 for item in RunOutcome}
    for record in records:
        outcomes[record.outcome.value] += 1
    solved = sum(1 for record in records if record.counts_as_solved)
    pending = sum(
        1 for record in records
        if record.outcome is RunOutcome.FORMAL_PASS
        and record.human_review.conclusion == "pending")
    rejected = sum(
        1 for record in records
        if record.outcome is RunOutcome.FORMAL_PASS
        and record.human_review.conclusion == "rejected")
    return {
        "sample_size": len(records),
        # A file that does not declare a version reads as version 1, so that is what this
        # counts. Records from an older but declared schema are counted separately: reporting
        # them as unversioned would overstate what the file said.
        "records_without_schema_version": sum(
            1 for record in records if record.schema_version <= 1),
        "records_below_current_schema": sum(
            1 for record in records if 1 < record.schema_version < SCHEMA_VERSION),
        "non_formal_excluded": (0 if include_non_formal else len(non_formal)),
        "rehearsals_excluded": (0 if include_non_formal else sum(
            1 for item in non_formal if item.attempt_kind is AttemptKind.REHEARSAL)),
        "unknown_kind_excluded": (0 if include_non_formal else sum(
            1 for item in non_formal if item.attempt_kind is AttemptKind.UNKNOWN)),
        "solved": solved,
        "solved_over_sample": f"{solved}/{len(records)}",
        "formal_pass_pending_review": pending,
        "formal_pass_rejected": rejected,
        "outcomes": outcomes,
        "note": ("Only a formal pass accepted by a human review, on an attempt recorded as "
                 "formal, counts as solved; formal passes still awaiting review, those a "
                 "reviewer rejected, incomplete attempts, rehearsals and attempts of unknown "
                 "kind are reported separately, never folded into the success count. "
                 "The verification references in a record are the recorder's claim: a "
                 "trusted process must confirm them against the formal verification and "
                 "its receipt before the record is counted."),
    }


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "run_record_configuration_error", "detail": detail}), file=sys.stderr)
    return 2


def corrected_record(
    record: BenchmarkRunRecord, reviewed: BenchmarkRunRecord, *,
    public_checks: PublicCheckSummary, independent: IndependentEvaluationEvidence | None,
    basis: str, corrected_by: str, corrected_at: str,
    corrects_sha256: str, reviewed_sha256: str, note: str = "",
) -> BenchmarkRunRecord:
    """Build the corrected copy from the reviewed one, keeping its human decision.

    Only the derived summary is replaced. The verdict, the attempt identity and the review
    come from the reviewed copy unchanged, and the correction block names both source files
    by digest so a reader can walk back to them.
    """
    differing = _immutable_record_differences(record, reviewed)
    if differing:
        raise ValueError(
            "The reviewed copy differs from the original in: " + ", ".join(differing))
    if reviewed.human_review.conclusion not in ("accepted", "rejected"):
        raise ValueError(
            "A correction keeps an existing human decision; the reviewed copy has none")
    if record.outcome is not reviewed.outcome:
        raise ValueError("A correction cannot change the outcome")
    payload = {
        **reviewed.model_dump(mode="json"),
        "schema_version": SCHEMA_VERSION,
        "public_checks": public_checks.model_dump(mode="json"),
        "independent_evaluation": (None if independent is None
                                   else independent.model_dump(mode="json")),
        "correction": {
            "corrects_sha256": corrects_sha256, "reviewed_sha256": reviewed_sha256,
            "basis": basis, "corrected_at": corrected_at, "corrected_by": corrected_by,
        },
        "notes": (reviewed.notes + (" | " if reviewed.notes and note else "") + note).strip(),
    }
    return BenchmarkRunRecord.model_validate(payload)


def _correct_record(args, *, session_factory=None) -> int:
    """Re-derive one record's summary from its Run, and write it as a new, linked file."""
    from uuid import UUID

    from devflow_ai.benchmark_run import classify_outcome

    raw_run_id = args.run
    try:
        original_bytes = Path(args.record).read_bytes()
        reviewed_bytes = Path(args.reviewed).read_bytes()
        records = (parse_run_record(original_bytes), parse_run_record(reviewed_bytes))
    except (OSError, ValueError) as error:
        return _configuration_error(f"Run record is unreadable: {type(error).__name__}")
    original, reviewed = records
    if raw_run_id is None:
        raw_run_id = original.run_id
    if raw_run_id is None:
        return _configuration_error(
            "The record carries no run_id; pass --run so the evidence can be re-read")
    try:
        run_id = UUID(str(raw_run_id))
    except (TypeError, ValueError):
        return _configuration_error("run must be a UUID")
    for label, item in (("record", original), ("reviewed", reviewed)):
        if item.run_id is not None:
            try:
                if UUID(item.run_id) != run_id:
                    return _configuration_error(f"{label} run_id does not match the selected Run")
            except ValueError:
                return _configuration_error(f"{label} run_id is not a UUID")
    differing = _immutable_record_differences(original, reviewed)
    if differing:
        return _configuration_error("The reviewed copy differs from the original in: " + ", ".join(differing))
    if original.run_id is None and original.formal is None:
        return _configuration_error("A record without run_id needs formal verification evidence to bind a Run")
    expected_task_id = None
    if original.task_id is not None:
        try:
            expected_task_id = UUID(original.task_id)
        except ValueError:
            return _configuration_error("record task_id is not a UUID")
    artifacts_root = Path(args.artifacts_root)
    if not artifacts_root.is_dir():
        return _configuration_error("artifacts-root must be an existing directory")
    paths = {"record": Path(args.record), "reviewed": Path(args.reviewed), "out": Path(args.out)}
    if paths["out"].resolve() in (paths["record"].resolve(), paths["reviewed"].resolve()):
        return _configuration_error("The corrected copy must be a new file")
    suite_version = args.suite_version or (
        original.formal.independent_suite_version if original.formal is not None else None)

    engine = None
    try:
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        with session_factory() as session:
            if expected_task_id is not None:
                from devflow_ai.models import Run
                bound_run = session.get(Run, run_id)
                if bound_run is None or bound_run.task_id != expected_task_id:
                    return _configuration_error("record task_id does not match the selected Run")
            decision = classify_outcome(
                session, run_id, deadline_fired=False,
                independent_suite_version=suite_version, artifact_root=artifacts_root)
    except Exception as error:  # an unreadable Run is a reported failure, not a traceback
        print(json.dumps({"event": "run_record_correction_failed",
                          "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    if decision.outcome.value != original.outcome.value:
        return _configuration_error(
            "The re-derived outcome differs from the record; a correction cannot change it")
    if original.formal is not None and (decision.formal is None
            or decision.formal.verification_id != original.formal.verification_id
            or decision.formal.independent_suite_version != original.formal.independent_suite_version
            or (original.formal.independent_receipt_sha256 is not None and
                decision.formal.independent_receipt_sha256 != original.formal.independent_receipt_sha256)):
        return _configuration_error("The selected Run's formal verification does not match the record")
    try:
        if paths["record"].read_bytes() != original_bytes or paths["reviewed"].read_bytes() != reviewed_bytes:
            return _configuration_error("A source record changed during correction; no copy was written")
    except OSError:
        return _configuration_error("A source record became unavailable during correction")
    try:
        corrected = corrected_record(
            original, reviewed, public_checks=decision.public_checks,
            independent=decision.independent, basis=args.basis,
            corrected_by=args.corrected_by, corrected_at=args.corrected_at,
            corrects_sha256=sha256(original_bytes).hexdigest(),
            reviewed_sha256=sha256(reviewed_bytes).hexdigest(),
            note=(f"corrected {args.corrected_at} by {args.corrected_by}: "
                  f"summary re-derived from {args.basis}"))
    except ValueError as error:
        return _configuration_error(str(error))
    try:
        write_run_record(corrected, paths["out"])
    except ValueError as error:
        return _configuration_error(str(error))
    print(json.dumps({
        "event": "run_record_corrected", "outcome": corrected.outcome.value,
        "public_outcome": corrected.public_checks.verified_outcome,
        "independent_outcome": (corrected.independent_evaluation.outcome
                                if corrected.independent_evaluation is not None else None),
        "human_review": corrected.human_review.conclusion,
        "counts_as_solved": corrected.counts_as_solved, "out": str(paths["out"])}))
    return 0


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Record a human review of one attempt, or summarize a set of records.

    A review is written to a **new** file: the original record is evidence and is never
    rewritten. The reviewed copy carries the same verdict plus who accepted it and when,
    so ``counts_as_solved`` and the summary reflect the human decision. Summarize the
    reviewed copies, not the originals, or accepted reviews never count.
    """
    parser = argparse.ArgumentParser(
        description="Record a human review of a benchmark run record, or summarize records.")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="Write a reviewed copy of one record.")
    review.add_argument("--record", required=True, help="Existing run record to review.")
    review.add_argument("--out", help="New file to write; defaults to <record>.reviewed.json.")
    review.add_argument("--conclusion", required=True,
                        choices=["accepted", "rejected", "pending"])
    review.add_argument("--reviewer", help="Required when the conclusion is accepted.")
    review.add_argument("--at", dest="reviewed_at",
                        help="UTC timestamp of the review; required when accepted.")
    correct = commands.add_parser(
        "correct", help="Write a traceable correction of one record's derived summary.")
    correct.add_argument("--record", required=True, help="Original record (never rewritten).")
    correct.add_argument("--reviewed", required=True,
                         help="Copy that already carries the human decision.")
    correct.add_argument("--out", required=True, help="New file for the corrected copy.")
    correct.add_argument("--run",
                         help="Run ID to re-read evidence from; defaults to the record's run_id.")
    correct.add_argument("--artifacts-root", required=True, help="Artifact root of that Run.")
    correct.add_argument("--suite-version",
                         help="Pinned suite version; defaults to the record's formal evidence.")
    correct.add_argument("--basis", required=True,
                         help="What the corrected summary is derived from.")
    correct.add_argument("--corrected-by", required=True)
    correct.add_argument("--at", dest="corrected_at", required=True, help="UTC timestamp.")
    summarize = commands.add_parser("summarize", help="Count outcomes across reviewed records.")
    summarize.add_argument("records", nargs="+", help="Reviewed run records (JSON).")
    summarize.add_argument("--include-non-formal", action="store_true",
                           help="Count rehearsals and attempts of unknown kind too; they are "
                                "excluded from the formal sample by default.")
    summarize.add_argument("--include-superseded", action="store_true",
                           help="Count a record that a corrected copy supersedes; by default "
                                "only the corrected copy is counted.")
    args = parser.parse_args(argv)

    if args.command == "summarize":
        loaded = []
        for path in args.records:
            try:
                loaded.append((Path(path), load_run_record(Path(path))))
            except (OSError, ValueError) as error:
                print(json.dumps({"event": "run_record_unreadable", "path": Path(path).name,
                                  "error_type": type(error).__name__}), file=sys.stderr)
                return 1
        # One attempt, one sample: originals, reviewed copies and corrected copies all
        # describe the same attempt, and a correction names both of its sources.
        try:
            records, dedupe = deduplicate_records(
                loaded, include_superseded=args.include_superseded)
        except (OSError, ValueError) as error:
            print(json.dumps({"event": "run_record_unreadable", "error_type": type(error).__name__}),
                  file=sys.stderr)
            return 1
        print(json.dumps({"event": "run_records_summarized", **dedupe,
                          **summarize_run_records(
                              records, include_non_formal=args.include_non_formal)}))
        return 0

    if args.command == "correct":
        return _correct_record(args, session_factory=session_factory)

    try:
        record = load_run_record(Path(args.record))
    except (OSError, ValueError) as error:
        return _configuration_error(f"Run record is unreadable: {type(error).__name__}")
    payload = {
        **record.model_dump(mode="json"),
        "human_review": {"conclusion": args.conclusion, "reviewer": args.reviewer,
                         "reviewed_at": args.reviewed_at},
    }
    try:
        reviewed = BenchmarkRunRecord.model_validate(payload)
    except ValidationError:
        return _configuration_error(
            "An accepted review needs both a reviewer and a review time")
    out = Path(args.out) if args.out else Path(args.record).with_suffix(".reviewed.json")
    if out.resolve() == Path(args.record).resolve():
        return _configuration_error("The reviewed copy must be a different file")
    try:
        write_run_record(reviewed, out)
    except ValueError as error:
        return _configuration_error(str(error))
    print(json.dumps({
        "event": "run_record_reviewed", "outcome": reviewed.outcome.value,
        "conclusion": reviewed.human_review.conclusion,
        "counts_as_solved": reviewed.counts_as_solved, "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
