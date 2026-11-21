"""Trusted-side accounting for one controlled RSI experiment.

This module records what an experiment is allowed to spend and what it observed. It does not
run agents, select tasks, generate candidates, apply patches, or decide that a candidate is
better on its own. It is the bookkeeping a promotion decision can be checked against, and
nothing more.

The four declarations it freezes at open time are the ones a later claim would otherwise be
able to invent after the fact:

* the **base** execution version (``execution_manifest`` digest of the system being improved);
* the **declared task set** — every candidate must be measured on those tasks, and a result
  for any other task is refused;
* the **candidate cap** and the **total token budget**, which is shared across candidates and
  enforced by reserve-then-settle, so two candidates cannot each spend the whole budget;
* the **wall-clock budget** and the **promotion thresholds** (minimum formal passes, maximum
  failures).

What is deliberately *not* claimed here:

* a result row is a **recorded verdict**, not a proof that a candidate is better. Two sources
  are kept apart on purpose: ``evidence_source = 'declared'`` is what the trusted caller says,
  and ``evidence_source = 'classified'`` is what ``benchmark_run.classify_outcome`` derived from
  the committed rows and the artifacts on disk. Only the classified kind can carry a
  ``verification_id``, and only the classified kind counts towards the promotion thresholds;
* ``record_classified_result`` **reuses** that classifier rather than adding a second one: it
  refuses to invent an outcome, records whatever the shared rules produce (including
  ``incomplete`` when the trusted chain cannot be loaded back), and never reads evaluation
  material into a model context, the log, or the database — it keeps the Verification id and
  the classifier's short reason, nothing else. The attempt context it classifies under cannot
  be read back from the Run row, so it comes from the run record the attempt's own entry wrote
  (``record_path``, digested and stored) — and that record must agree with the classifier or
  the conflict is reported. A verdict recorded without a record is a caller declaration: it is
  stored as such and is never promotable;
* one Run belongs to at most one candidate of an experiment, and every attempt keeps its own
  row. The promotion thresholds therefore count **distinct declared Tasks** with a classified
  formal pass, not the number of pass rows: repeating one Task may not manufacture coverage.
  A promotable pass also needs the verified target baseline recorded on its row, so an attempt
  whose checkout cannot be shown to be the declared baseline is not a promotion sample;
* token amounts are **declared by the trusted caller** on reserve and on settle. The service
  enforces the aggregate ceiling and keeps the declared numbers; it does not measure model
  usage itself. A reservation that produced usage nobody can quantify ends as ``unknown`` with
  who/why recorded — unknown is not zero, and an experiment with unknown or unsettled
  reservations reports incomplete accounting;
* a candidate's ``execution_sha256`` is still **declared by the caller**, but a Run can now
  record the digest its entry verified against a pinned manifest. Results are only promotable
  when the Run records the same digest, and a Run that provably ran a different one is refused
  rather than attributed. What that proves is narrow — this process verified those bytes, not
  that a trusted third party watched it run — and ``summary`` says which case each candidate is
  in (``verified_runs`` / ``no_verified_run``);
* ``accepted`` decisions stay disabled even after the recorded thresholds pass. The checks
  behind them now cover overrun, accounting completeness, task coverage and distinct-Task
  thresholds over classifier-derived passes — but the candidate's execution version and the
  Run-to-candidate attribution are still declarations, and this service cannot make them facts;
* no scheduler, benchmark entry, worker or agent is wired to this module yet. Nothing starts an
  experiment automatically, and no RSI loop is implemented.

Mutations are flushed, not committed: the transaction boundary belongs to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from re import fullmatch
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devflow_ai.database import get_database_time
from devflow_ai.enums import RunStatus, ToolCallStatus, VerificationResult
from devflow_ai.models import (
    AgentExecution, Experiment, ExperimentCandidate, ExperimentRecord, ExperimentReservation,
    ExperimentTask, ExperimentTaskResult, Run, Task, ToolCall, Verification,
)

RESULT_OUTCOMES = ("formal_pass", "failed", "public_only_pass", "incomplete", "timeout")
EVIDENCE_SOURCES = ("declared", "classified")
# How the classified entry learns what the classifier needs about the attempt's end conditions.
# "unknown" is a recorded value, not a default: the classifier checks the deadline *before* the
# formal chain, so silently passing "not fired" could turn a timed-out attempt into a pass.
DEADLINE_CONTEXTS = ("fired", "not_fired", "unknown")
DECISION_OUTCOMES = ("accepted", "rejected", "abandoned")
FAILURE_OUTCOMES = ("failed", "timeout")
FORMAL_EVIDENCE_SCOPE = "trusted_v1"
_SHA256 = r"[0-9a-f]{64}"


class ExperimentError(ValueError):
    """The request cannot be recorded as submitted."""


class ExperimentStateError(ExperimentError):
    """The experiment or candidate is missing or in a state that forbids the operation."""


class ExperimentLimitError(ExperimentError):
    """A declared cap (candidates, tokens, deadline) would be exceeded."""


class ExperimentConflictError(ExperimentError):
    """The identity already names different content, or a recorded verdict would be rewritten."""


class ExperimentEvidenceError(ExperimentError):
    """The cited attempt does not support the verdict being recorded."""


def _require_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{field} must be a non-blank string")
    if value != value.strip():
        raise ExperimentError(f"{field} must not have surrounding space")
    if len(value) > maximum:
        raise ExperimentError(f"{field} must be at most {maximum} characters")
    if any(ord(character) < 32 for character in value):
        raise ExperimentError(f"{field} must not contain control characters")
    return value


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not fullmatch(_SHA256, value):
        raise ExperimentError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


def _require_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise ExperimentError(f"{field} must be a UUID") from None


@dataclass(frozen=True)
class ExperimentLimits:
    """The caps and thresholds frozen when an experiment is opened.

    ``max_total_model_tokens`` is shared by every candidate; it is not a per-candidate cap.
    """

    max_candidates: int
    max_total_model_tokens: int
    max_total_seconds: int
    promotion_min_formal_passes: int
    promotion_max_failures: int

    def __post_init__(self) -> None:
        for field, floor, ceiling in (
            ("max_candidates", 1, 100),
            ("max_total_model_tokens", 1, None),
            ("max_total_seconds", 1, 2_592_000),
            ("promotion_min_formal_passes", 1, None),
            ("promotion_max_failures", 0, None),
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ExperimentLimitError(f"{field} must be an integer")
            if value < floor or (ceiling is not None and value > ceiling):
                raise ExperimentLimitError(f"{field} is outside the permitted range")


class ExperimentService:
    """Record experiment limits, candidates, spend and per-attempt verdicts.

    Every mutating method takes the experiment row ``FOR UPDATE`` first and any candidate or
    reservation row second, so concurrent callers queue on one lock order and the token total
    cannot be oversubscribed by two transactions that each read a stale balance.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- opening and reading

    def open(
        self, *, identity_key: str, base_execution_sha256: str, task_ids, limits: ExperimentLimits,
        created_by: str,
    ) -> UUID:
        """Open an experiment, or return the one this exact submission already opened.

        ``identity_key`` is the idempotency identity. Replaying it with the same declaration
        returns the existing experiment; replaying it with a different declaration is refused,
        because two different limit sets under one identity would make the accounting
        unauditable.
        """
        identity = _require_text(identity_key, "identity_key", 200)
        base = _require_sha256(base_execution_sha256, "base_execution_sha256")
        operator = _require_text(created_by, "created_by", 200)
        if not isinstance(limits, ExperimentLimits):
            raise ExperimentError("limits must be an ExperimentLimits instance")
        declared = self._normalise_task_ids(task_ids)
        baselines = self._pinned_task_baselines(declared)
        deadline_at = get_database_time(self.session) + timedelta(seconds=limits.max_total_seconds)
        statement = (
            postgres_insert(Experiment)
            .values(
                identity_key=identity, base_execution_sha256=base,
                max_candidates=limits.max_candidates,
                max_total_model_tokens=limits.max_total_model_tokens,
                max_total_seconds=limits.max_total_seconds,
                promotion_min_formal_passes=limits.promotion_min_formal_passes,
                promotion_max_failures=limits.promotion_max_failures,
                created_by=operator, deadline_at=deadline_at)
            .on_conflict_do_nothing(index_elements=["identity_key"])
            .returning(Experiment.id))
        created = self.session.execute(statement).scalar_one_or_none()
        if created is None:
            return self._replay_open(identity, base, declared, baselines, limits, operator)
        for task_id in declared:
            self.session.execute(
                postgres_insert(ExperimentTask)
                .values(experiment_id=created, task_id=task_id,
                        base_commit=baselines[task_id])
                .on_conflict_do_nothing(index_elements=["experiment_id", "task_id"]))
        self.session.flush()
        return created

    def _replay_open(
        self, identity: str, base: str, declared: tuple[UUID, ...],
        baselines: dict[UUID, str], limits: ExperimentLimits, operator: str,
    ) -> UUID:
        experiment = self.session.scalar(
            select(Experiment).where(Experiment.identity_key == identity))
        if experiment is None:  # pragma: no cover - the conflict named an existing row
            raise ExperimentStateError("Experiment identity is not readable after a conflict")
        mismatches = []
        if experiment.base_execution_sha256 != base:
            mismatches.append("base_execution_sha256")
        if experiment.created_by != operator:
            mismatches.append("created_by")
        for field in ("max_candidates", "max_total_model_tokens", "max_total_seconds",
                      "promotion_min_formal_passes", "promotion_max_failures"):
            if getattr(experiment, field) != getattr(limits, field):
                mismatches.append(field)
        if self.task_ids(experiment.id) != declared:
            mismatches.append("task_ids")
        elif self.task_baselines(experiment.id) != baselines:
            # The same identity with a Task whose baseline has since changed is a different
            # declaration, not a replay.
            mismatches.append("task_baselines")
        if mismatches:
            raise ExperimentConflictError(
                "This identity is already open with a different declaration: "
                + ", ".join(sorted(mismatches)))
        return experiment.id

    @staticmethod
    def _normalise_task_ids(task_ids) -> tuple[UUID, ...]:
        if isinstance(task_ids, (str, bytes)) or not hasattr(task_ids, "__iter__"):
            raise ExperimentError("task_ids must be an iterable of UUIDs")
        values = [_require_uuid(item, "task_id") for item in task_ids]
        if not values:
            raise ExperimentError("An experiment needs at least one declared Task")
        if len(set(values)) != len(values):
            raise ExperimentError("Declared Task ids must be unique")
        return tuple(sorted(values, key=str))

    def read(self, experiment_id) -> Experiment:
        experiment = self.session.scalar(select(Experiment).where(
            Experiment.id == _require_uuid(experiment_id, "experiment_id")))
        if experiment is None:
            raise ExperimentStateError("Experiment does not exist")
        return experiment

    def task_ids(self, experiment_id) -> tuple[UUID, ...]:
        rows = self.session.scalars(select(ExperimentTask.task_id).where(
            ExperimentTask.experiment_id == _require_uuid(experiment_id, "experiment_id")))
        return tuple(sorted(rows.all(), key=str))

    def task_baselines(self, experiment_id) -> dict[UUID, str]:
        """The target baseline frozen for each declared Task when the experiment opened."""
        rows = self.session.execute(select(ExperimentTask.task_id, ExperimentTask.base_commit)
                                    .where(ExperimentTask.experiment_id == _require_uuid(
                                        experiment_id, "experiment_id")))
        return dict(rows.all())

    def _pinned_task_baselines(self, task_ids: tuple[UUID, ...]) -> dict[UUID, str]:
        """Every declared Task must exist and already have a pinned base commit.

        An experiment compares candidates on a task set. If a task's target baseline is not
        pinned yet, nothing measured on it is comparable, so opening is refused rather than
        recording the baseline later — which the frozen declaration could then contradict.
        """
        rows = dict(self.session.execute(
            select(Task.id, Task.base_commit).where(Task.id.in_(task_ids))).all())
        missing = [str(item) for item in task_ids if item not in rows]
        if missing:
            raise ExperimentError("Declared Task(s) do not exist: " + ", ".join(missing))
        unpinned = [str(item) for item in task_ids if not rows[item]]
        if unpinned:
            raise ExperimentError(
                "Declared Task(s) have no pinned base commit: " + ", ".join(unpinned))
        return {item: rows[item] for item in task_ids}

    # ---------------------------------------------------------------- candidates

    def admit_candidate(
        self, *, experiment_id, label: str, execution_sha256: str,
        parent_label: str | None = None,
    ) -> UUID:
        """Register one candidate execution version under the frozen candidate cap.

        Admitting a candidate declares new work, so it is refused once the deadline has passed.
        Replaying a candidate that was already admitted still returns it.
        """
        name = _require_text(label, "label", 200)
        digest = _require_sha256(execution_sha256, "execution_sha256")
        experiment = self._lock_open_experiment(experiment_id)
        parent = None
        if parent_label is not None:
            parent = self._candidate(
                experiment, _require_text(parent_label, "parent_label", 200)).id
        existing = self.session.scalar(select(ExperimentCandidate).where(
            ExperimentCandidate.experiment_id == experiment.id,
            ExperimentCandidate.label == name))
        if existing is not None:
            if existing.execution_sha256 != digest or existing.parent_id != parent:
                raise ExperimentConflictError(
                    "This candidate label already names a different execution version")
            return existing.id
        self._require_before_deadline(experiment, "a new candidate")
        count = self.session.scalar(select(func.count()).select_from(ExperimentCandidate).where(
            ExperimentCandidate.experiment_id == experiment.id)) or 0
        if count >= experiment.max_candidates:
            raise ExperimentLimitError(
                f"Candidate limit reached ({experiment.max_candidates}); no further candidate "
                "can be admitted to this experiment")
        statement = (
            postgres_insert(ExperimentCandidate)
            .values(experiment_id=experiment.id, label=name, execution_sha256=digest,
                    parent_id=parent)
            .on_conflict_do_nothing(index_elements=["experiment_id", "label"])
            .returning(ExperimentCandidate.id))
        created = self.session.execute(statement).scalar_one_or_none()
        if created is None:  # lost the race to an identical concurrent admission
            return self.admit_candidate(
                experiment_id=experiment.id, label=name, execution_sha256=digest,
                parent_label=parent_label)
        self.session.flush()
        return created

    def _candidate(self, experiment: Experiment, label: str) -> ExperimentCandidate:
        candidate = self.session.scalar(select(ExperimentCandidate).where(
            ExperimentCandidate.experiment_id == experiment.id,
            ExperimentCandidate.label == label))
        if candidate is None:
            raise ExperimentStateError(f"Candidate {label!r} is not part of this experiment")
        return candidate

    def _lock_experiment(self, experiment_id) -> Experiment:
        experiment = self.session.scalar(select(Experiment).where(
            Experiment.id == _require_uuid(experiment_id, "experiment_id")).with_for_update())
        if experiment is None:
            raise ExperimentStateError("Experiment does not exist")
        return experiment

    def _lock_open_experiment(self, experiment_id) -> Experiment:
        experiment = self._lock_experiment(experiment_id)
        if experiment.closed_at is not None:
            raise ExperimentStateError(
                f"Experiment is closed ({experiment.closed_reason}); no further accounting "
                "is accepted")
        return experiment

    def _require_before_deadline(self, experiment: Experiment, what: str) -> None:
        now = get_database_time(self.session)
        if now >= experiment.deadline_at:
            raise ExperimentLimitError(
                f"Experiment deadline has passed; {what} is refused")

    # ---------------------------------------------------------------- token budget

    def reserve(self, *, experiment_id, request_key, tokens: int, candidate_label: str | None = None,
                run_id=None, agent_execution_id=None) -> dict:
        """Reserve tokens from the experiment-wide budget before a model call is made.

        The reservation is idempotent by ``request_key``. Admission requires the outstanding
        reservations plus everything already settled plus this request to fit the budget, so
        two candidates cannot both spend the full ceiling. ``run_id``/``agent_execution_id``
        only record which attempt asked; they do not authorise anything.
        """
        key = _require_uuid(request_key, "request_key")
        amount = self._require_tokens(tokens, "tokens")
        attempt_run = None if run_id is None else _require_uuid(run_id, "run_id")
        attempt_execution = (None if agent_execution_id is None
                             else _require_uuid(agent_execution_id, "agent_execution_id"))
        experiment = self._lock_open_experiment(experiment_id)
        self._require_before_deadline(experiment, "a token reservation")
        self._require_attempt_belongs(experiment, attempt_run, attempt_execution)
        candidate_id = None
        if candidate_label is not None:
            candidate = self._candidate(experiment, _require_text(candidate_label, "candidate_label", 200))
            if candidate.state != "prepared":
                raise ExperimentStateError(
                    f"Candidate {candidate.label!r} is concluded; it cannot reserve further tokens")
            candidate_id = candidate.id
        existing = self.session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment.id,
            ExperimentReservation.request_key == key).with_for_update())
        if existing is not None:
            if (existing.tokens != amount or existing.candidate_id != candidate_id
                    or existing.run_id != attempt_run
                    or existing.agent_execution_id != attempt_execution):
                raise ExperimentConflictError(
                    "This request_key already reserves a different amount, candidate or attempt")
            return self._reservation(experiment, existing)
        if self._has_unknown_usage(experiment):
            raise ExperimentLimitError("Unknown usage prevents new token reservations")
        if self._dispatched_unsettled(experiment):
            raise ExperimentLimitError(
                "A model request that may already have been sent is not settled yet; the "
                "experiment admits one such request at a time")
        committed = experiment.settled_tokens + experiment.reserved_tokens
        if committed + amount > experiment.max_total_model_tokens:
            raise ExperimentLimitError(
                "Experiment token budget exhausted: "
                f"{committed} settled/reserved of {experiment.max_total_model_tokens}"
                f", request {amount}")
        statement = (
            postgres_insert(ExperimentReservation)
            .values(experiment_id=experiment.id, candidate_id=candidate_id, request_key=key,
                    run_id=attempt_run, agent_execution_id=attempt_execution, tokens=amount)
            .on_conflict_do_nothing(index_elements=["experiment_id", "request_key"])
            .returning(ExperimentReservation.id))
        created = self.session.execute(statement).scalar_one_or_none()
        if created is None:
            # The experiment row lock should already serialise callers, so this is a
            # defensive re-read rather than an expected branch.
            return self.reserve(experiment_id=experiment.id, request_key=key, tokens=amount,
                                candidate_label=candidate_label)
        experiment.reserved_tokens += amount
        self.session.flush()
        return {
            "experiment_id": str(experiment.id), "request_key": str(key), "state": "reserved",
            "tokens": amount, "settled_tokens": experiment.settled_tokens,
            "reserved_tokens": experiment.reserved_tokens,
            "remaining_tokens": max(
                0, experiment.max_total_model_tokens
                - experiment.settled_tokens - experiment.reserved_tokens),
        }

    def _require_attempt_belongs(
        self, experiment: Experiment, run_id: UUID | None, agent_execution_id: UUID | None,
    ) -> None:
        """The Run and AgentExecution have to be part of this experiment's declared work.

        These identifiers are recorded to attribute a request, so they are checked rather than
        trusted: a Run of another Task, or an execution of another Run, would otherwise put one
        candidate's spend under a label that does not describe it.
        """
        if agent_execution_id is not None and run_id is None:
            raise ExperimentError("agent_execution_id requires run_id")
        if run_id is None:
            return
        task_id = self.session.scalar(select(Run.task_id).where(Run.id == run_id))
        if task_id is None:
            raise ExperimentEvidenceError("Run does not exist")
        if task_id not in set(self.task_ids(experiment.id)):
            raise ExperimentStateError(
                "This Run belongs to a Task outside the experiment's declared task set")
        if agent_execution_id is not None:
            owner = self.session.scalar(
                select(AgentExecution.run_id).where(AgentExecution.id == agent_execution_id))
            if owner is None or owner != run_id:
                raise ExperimentEvidenceError(
                    "This AgentExecution does not belong to the recorded Run")

    def mark_dispatched(self, *, experiment_id, request_key) -> dict:
        """Record that the request may now be on its way to the provider.

        This is the line between "we can prove nothing was sent" and "we cannot". After it, the
        reservation may only end as settled or unknown; releasing it would turn a possible
        spend into free budget.
        """
        key = _require_uuid(request_key, "request_key")
        experiment = self._lock_experiment(experiment_id)
        reservation = self._lock_reservation(experiment, key)
        if reservation.stage == "dispatched":
            return self._reservation(experiment, reservation)
        if reservation.state != "reserved":
            raise ExperimentStateError(
                f"This reservation is {reservation.state}; there is nothing to dispatch")
        if experiment.closed_at is not None:
            raise ExperimentLimitError("Experiment is closed; this request cannot be dispatched")
        self._require_before_deadline(experiment, "a model request")
        if (experiment.over_budget or self._has_unknown_usage(experiment)
                or experiment.settled_tokens + experiment.reserved_tokens
                > experiment.max_total_model_tokens):
            raise ExperimentLimitError("Experiment budget is over limit or usage is unknown")
        # Checked here as well as at reservation time: two requests may both get their
        # reservations before either dispatches, and only one of them may reach the provider.
        if self._dispatched_unsettled(experiment, exclude_key=key):
            raise ExperimentLimitError(
                "Another model request that may already have been sent is not settled yet; "
                "this request cannot be dispatched")
        reservation.stage = "dispatched"
        reservation.dispatched_at = get_database_time(self.session)
        self.session.flush()
        return self._reservation(experiment, reservation)

    def settle(self, *, experiment_id, request_key, actual_tokens: int) -> dict:
        """Record the amount actually used against a reservation.

        If the real amount pushes the settled total past the declared ceiling the experiment is
        marked ``over_budget`` instead of being silently rewritten: the spend happened, and a
        promotion decision may not be taken from an over-budget run.
        """
        key = _require_uuid(request_key, "request_key")
        actual = self._require_tokens(actual_tokens, "actual_tokens", allow_zero=True)
        # Settling completes spend that was already authorised, so it stays available after
        # close (a crashed reservation would otherwise leave the accounting permanently open).
        # The deadline does not block it either: the call it describes already happened.
        experiment = self._lock_experiment(experiment_id)
        reservation = self._lock_reservation(experiment, key)
        if reservation.state == "settled":
            if reservation.actual_tokens != actual:
                raise ExperimentConflictError(
                    "This request_key is already settled with a different amount")
            return self._reservation(experiment, reservation)
        if reservation.state in ("released", "unknown"):
            raise ExperimentStateError(
                f"This reservation is {reservation.state} and cannot be settled")
        experiment.reserved_tokens = max(0, experiment.reserved_tokens - reservation.tokens)
        experiment.settled_tokens += actual
        if experiment.settled_tokens > experiment.max_total_model_tokens:
            experiment.over_budget = True
        reservation.state = "settled"
        reservation.actual_tokens = actual
        reservation.settled_at = get_database_time(self.session)
        self.session.flush()
        return self._reservation(experiment, reservation)

    def release(self, *, experiment_id, request_key, reason: str, confirmed_by: str) -> dict:
        """Return a reservation to the budget, on a declaration that no usage occurred.

        Releasing is not the same as "the call failed": a caller that cannot say whether usage
        happened must end the reservation as ``unknown`` instead. Both endings record who
        decided and why, so the difference is still visible afterwards.
        """
        key = _require_uuid(request_key, "request_key")
        note = _require_text(reason, "reason", 1000)
        operator = _require_text(confirmed_by, "confirmed_by", 200)
        experiment = self._lock_experiment(experiment_id)
        reservation = self._lock_reservation(experiment, key)
        if reservation.state == "released":
            if (reservation.ended_reason, reservation.ended_by) == (note, operator):
                return self._reservation(experiment, reservation)
            raise ExperimentConflictError(
                "This reservation was already released with a different declaration")
        if reservation.state in ("settled", "unknown"):
            raise ExperimentStateError(
                f"This reservation is {reservation.state} and cannot be released")
        if reservation.stage != "reserved":
            raise ExperimentStateError(
                "This request may already have been sent; mark its usage unknown instead of "
                "releasing it")
        self._end_reservation(experiment, reservation, "released", note, operator)
        return self._reservation(experiment, reservation)

    def settle_unknown(self, *, experiment_id, request_key, reason: str, recorded_by: str) -> dict:
        """End a reservation whose usage cannot be quantified.

        The amount is **not** counted as zero: the reservation leaves the outstanding balance,
        the settled total is left alone, and the experiment reports incomplete accounting. A
        later promotion decision is refused on that basis rather than treating an unknown as a
        free call.
        """
        key = _require_uuid(request_key, "request_key")
        note = _require_text(reason, "reason", 1000)
        operator = _require_text(recorded_by, "recorded_by", 200)
        experiment = self._lock_experiment(experiment_id)
        reservation = self._lock_reservation(experiment, key)
        if reservation.state == "unknown":
            if (reservation.ended_reason, reservation.ended_by) == (note, operator):
                return self._reservation(experiment, reservation)
            raise ExperimentConflictError(
                "This reservation was already ended as unknown with a different declaration")
        if reservation.state in ("settled", "released"):
            raise ExperimentStateError(
                f"This reservation is {reservation.state} and cannot be marked unknown")
        self._end_reservation(experiment, reservation, "unknown", note, operator)
        return self._reservation(experiment, reservation)

    def _lock_reservation(self, experiment: Experiment, key: UUID) -> ExperimentReservation:
        reservation = self.session.scalar(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment.id,
            ExperimentReservation.request_key == key).with_for_update())
        if reservation is None:
            raise ExperimentStateError("No reservation exists for this request_key")
        return reservation

    def _end_reservation(
        self, experiment: Experiment, reservation: ExperimentReservation, state: str,
        reason: str, operator: str,
    ) -> None:
        experiment.reserved_tokens = max(0, experiment.reserved_tokens - reservation.tokens)
        reservation.state = state
        reservation.ended_by = operator
        reservation.ended_reason = reason
        reservation.ended_at = get_database_time(self.session)
        self.session.flush()

    @staticmethod
    def _require_tokens(value: object, field: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExperimentError(f"{field} must be an integer")
        if value < 0 or (value == 0 and not allow_zero):
            raise ExperimentError(f"{field} must be a positive number of tokens")
        return value

    def _dispatched_unsettled(self, experiment: Experiment, *, exclude_key=None) -> int:
        """Requests that may have reached the provider and are still not accounted for.

        This architecture cannot tell a request that is genuinely in flight from one whose
        process died after sending it, so both are counted the same way: at most one may exist
        per experiment. Until it is settled or marked unknown, no other request may be sent —
        otherwise a crash would leave free budget behind and the next request would spend it.
        """
        statement = select(func.count()).select_from(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment.id,
            ExperimentReservation.state == "reserved",
            ExperimentReservation.stage == "dispatched")
        if exclude_key is not None:
            statement = statement.where(ExperimentReservation.request_key != exclude_key)
        return int(self.session.scalar(statement) or 0)

    def _has_unknown_usage(self, experiment: Experiment) -> bool:
        return bool(self.session.scalar(select(func.count()).select_from(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment.id,
            ExperimentReservation.state == "unknown")))

    def _reservation(self, experiment: Experiment, reservation: ExperimentReservation) -> dict:
        return {
            "experiment_id": str(experiment.id),
            "request_key": str(reservation.request_key),
            "candidate_id": None if reservation.candidate_id is None else str(reservation.candidate_id),
            "state": reservation.state,
            "stage": reservation.stage,
            "tokens": reservation.tokens,
            "actual_tokens": reservation.actual_tokens,
            "ended_by": reservation.ended_by,
            "ended_reason": reservation.ended_reason,
            "settled_tokens": experiment.settled_tokens,
            "reserved_tokens": experiment.reserved_tokens,
            "over_budget": experiment.over_budget,
            "remaining_tokens": None if self._has_unknown_usage(experiment) else max(
                0, experiment.max_total_model_tokens
                - experiment.settled_tokens - experiment.reserved_tokens),
        }

    # ---------------------------------------------------------------- results and decisions

    def record_task_result(
        self, *, experiment_id, candidate_label: str, task_id, run_id, outcome: str,
        recorded_by: str,
    ) -> dict:
        """Record one finished attempt's verdict as the caller's declaration.

        This is ``evidence_source = 'declared'``: the trusted caller states the verdict, and
        the service only checks the preconditions the database can settle — the Run exists,
        belongs to the declared Task and is finished, and for ``formal_pass`` that a passed
        Verification carrying the trusted scope and its draft/receipt/suite references is
        recorded. **Declared formal passes do not count towards the promotion thresholds**;
        use :meth:`record_classified_result` for a verdict the shared classifier derived.
        """
        if outcome not in RESULT_OUTCOMES:
            raise ExperimentError(
                f"outcome must be one of {', '.join(RESULT_OUTCOMES)}")
        operator = _require_text(recorded_by, "recorded_by", 200)
        task = _require_uuid(task_id, "task_id")
        run = _require_uuid(run_id, "run_id")
        experiment = self._lock_open_experiment(experiment_id)
        candidate = self._prepared_candidate(experiment, candidate_label)
        self._require_declared_task(experiment, task)
        attempt = self._require_finished_attempt(task, run)
        self._require_same_execution_version(candidate, attempt)
        if outcome == "formal_pass":
            self._require_recorded_formal_verification(run)
        return self._insert_result(
            experiment=experiment, candidate=candidate, task_id=task, run_id=run,
            outcome=outcome, recorded_by=operator, evidence_source="declared",
            decision_reason=None, verification_id=None,
            run_execution_sha256=attempt.execution_sha256,
            workspace_base_commit=self._workspace_base_commit(run, task))

    def register_record(
        self, *, experiment_id, candidate_label: str, task_id, record_path,
        registered_by: str,
    ) -> dict:
        """Fix one run record for one attempt of one candidate.

        Only the digest is stored; the file stays where the control plane keeps it. The Run is
        taken from the same bytes that are digested, so the registration is bound to **that
        attempt**: one Run can have at most one fixed record, and the same bytes cannot be fixed
        for two Runs of an experiment. It cannot tell who wrote the file, and it does not verify
        the verdict — ``record_classified_result`` still classifies independently.

        Registration has to happen **before** the attempt is recorded. A result row freezes the
        provenance it was written with, and rewriting it afterwards would rewrite evidence, so
        the later order is refused with an actionable message instead of being silently ignored.
        """
        operator = _require_text(registered_by, "registered_by", 200)
        task = _require_uuid(task_id, "task_id")
        experiment = self._lock_open_experiment(experiment_id)
        candidate = self._prepared_candidate(experiment, candidate_label)
        self._require_declared_task(experiment, task)
        record, digest = self._read_attempt_record(record_path, task_id=task)
        run_id = _require_uuid(record.run_id, "run_id")
        already = self.session.scalar(select(ExperimentTaskResult).where(
            ExperimentTaskResult.experiment_id == experiment.id,
            ExperimentTaskResult.run_id == run_id))
        if already is not None and not (
                already.record_provenance == "control_plane_registered"
                and already.record_sha256 == digest):
            raise ExperimentStateError(
                "This attempt is already recorded without this fixed record; registering it now "
                "would not change that result. Fix the record before recording the attempt, or "
                "leave this result as non-promotable evidence")
        statement = (
            postgres_insert(ExperimentRecord)
            .values(experiment_id=experiment.id, candidate_id=candidate.id, task_id=task,
                    run_id=run_id, record_sha256=digest, registered_by=operator)
            .on_conflict_do_nothing()
            .returning(ExperimentRecord.id))
        created = self.session.execute(statement).scalar_one_or_none()
        if created is None:
            existing = self.session.scalar(select(ExperimentRecord).where(
                ExperimentRecord.experiment_id == experiment.id,
                ExperimentRecord.run_id == run_id))
            if existing is None:
                # The digest itself is already fixed for another attempt.
                raise ExperimentConflictError(
                    "These record bytes are already fixed for another attempt in this experiment")
            if (existing.candidate_id != candidate.id or existing.task_id != task
                    or existing.record_sha256 != digest):
                raise ExperimentConflictError(
                    "This attempt already has a fixed record, and it is not this file")
            operator = existing.registered_by
        else:
            self.session.flush()
        return {"experiment_id": str(experiment.id), "candidate": candidate.label,
                "task_id": str(task), "run_id": str(run_id),
                "record_sha256": digest, "registered_by": operator, "registered": True}

    def _record_origin(
        self, experiment: Experiment, candidate: ExperimentCandidate, task_id: UUID,
        run_id: UUID, record_sha256: str,
    ) -> str:
        """Is this record the one the control plane fixed for **this Run**?

        The question is asked per attempt, never per Task: two Runs of the same Task may carry
        different records without affecting each other. A record fixed for this Run but with
        other bytes, or fixed for another Run of the experiment, is a cross-reference and is
        refused. Nothing fixed at all keeps the older ``caller_supplied`` provenance, which is
        recorded but never promotable.
        """
        registered = self.session.scalar(select(ExperimentRecord).where(
            ExperimentRecord.experiment_id == experiment.id,
            ExperimentRecord.run_id == run_id))
        if registered is not None:
            if (registered.candidate_id != candidate.id or registered.task_id != task_id
                    or registered.record_sha256 != record_sha256):
                raise ExperimentConflictError(
                    "The control plane fixed a different record for this attempt")
            return "control_plane_registered"
        elsewhere = self.session.scalar(select(ExperimentRecord).where(
            ExperimentRecord.experiment_id == experiment.id,
            ExperimentRecord.record_sha256 == record_sha256))
        if elsewhere is not None:
            raise ExperimentConflictError(
                "This run record is fixed for another attempt in this experiment")
        return "caller_supplied"

    def record_classified_result(
        self, *, experiment_id, candidate_label: str, task_id, run_id, artifacts_root,
        independent_suite_version: str, context_source: str, recorded_by: str,
        deadline_context: str | None = None, record_path=None,
        finalization: dict | None = None,
    ) -> dict:
        """Record the verdict the **shared classifier** derives for one finished attempt.

        This reuses ``benchmark_run.classify_outcome`` — the same rules the benchmark entry,
        the scheduler record and the backfill entry use — instead of accepting an outcome from
        the caller. Whatever it returns is what gets recorded, including ``incomplete`` when
        the trusted chain cannot be loaded back from disk, so a public-only pass can never be
        promoted into ``formal_pass`` here. Only the Verification id and the classifier's short
        reason are stored; nothing reads evaluation material into a model context, a log line
        or the database.

        **The attempt context comes from a persisted run record when one exists.** The
        classifier takes a ``deadline_fired`` flag and an optional ``finalization`` dict, and
        neither can be read back from the Run row. So the caller supplies ``record_path``: the
        ``BenchmarkRunRecord`` the attempt's own entry wrote, which now carries an
        ``attempt_context`` observed by the process that ran it. The record is read from the
        exact bytes that are digested, must name this Run (and this Task when it names one),
        must not contradict the pinned suite version, must not name a different execution
        version than the Run it describes, and **must agree with the classifier**: a record
        stating one outcome while the committed evidence yields another is a conflict to
        resolve, not something to average out. ``record_sha256`` is stored so the ledger points
        at the bytes it used.

        Without a record the verdict can still be recorded, but only as ``deadline_context``
        plus a stated ``context_source``; that is a declaration (``context_evidence =
        'declared'``), it never counts towards the promotion thresholds, and the caller may not
        leave both sources out. ``unknown`` is passed to the classifier as "not fired" (the only
        safe value) and keeps the result out of the promotable set either way, because a fired
        deadline outranks a formal pass.

        Only a **formal** attempt can ever be promotable: a rehearsal record is kept as
        evidence, but its verdict is not a promotion sample. Recording a result does not
        establish which candidate executed the Run either — that attribution remains a
        declaration (see ``summary``).
        """
        if deadline_context is not None and deadline_context not in DEADLINE_CONTEXTS:
            raise ExperimentError(
                f"deadline_context must be one of {', '.join(DEADLINE_CONTEXTS)}; state "
                "unknown rather than leaving it implied")
        if finalization is not None and not isinstance(finalization, dict):
            raise ExperimentError("finalization must be the recorded finalization dict or None")
        if record_path is None and deadline_context is None:
            raise ExperimentError(
                "Supply the run record that holds the attempt's context, or state the deadline "
                "context explicitly; neither is inferred")
        source = _require_text(context_source, "context_source", 200)
        operator = _require_text(recorded_by, "recorded_by", 200)
        task = _require_uuid(task_id, "task_id")
        run = _require_uuid(run_id, "run_id")
        suite = _require_text(independent_suite_version, "independent_suite_version", 200)
        root = self._require_artifacts_root(artifacts_root)
        experiment = self._lock_open_experiment(experiment_id)
        candidate = self._prepared_candidate(experiment, candidate_label)
        self._require_declared_task(experiment, task)
        record = None
        record_sha256 = None
        record_provenance = None
        attempt_kind = None
        if record_path is not None:
            record, record_sha256 = self._read_attempt_record(
                record_path, task_id=task, run_id=run)
            recorded_deadline = (record.attempt_context.deadline
                                 if record.attempt_context is not None else "unknown")
            if deadline_context is not None and deadline_context != recorded_deadline:
                raise ExperimentConflictError(
                    "The stated deadline context contradicts the run record: "
                    f"record={recorded_deadline!r}, stated={deadline_context!r}")
            deadline = recorded_deadline
            evidence = "record"
            recorded_suite = (record.independent_evaluation.suite_version
                              if record.independent_evaluation is not None else None)
            if recorded_suite is not None and recorded_suite != suite:
                raise ExperimentConflictError(
                    "The run record names a different independent suite version than the pin")
            attempt_kind = record.attempt_kind.value
            # The digest pins the bytes and the registration says whether they are the ones the
            # control plane fixed; neither attests who produced the file.
            record_provenance = self._record_origin(
                experiment, candidate, task, run, record_sha256)
        else:
            deadline = deadline_context
            evidence = "declared"
        # A timed-out attempt usually leaves the Run row RUNNING — the worker stops rather
        # than rewriting a Run it did not finish — so that is the one case where a verdict may
        # be recorded for an unfinished Run. The classifier turns it into ``timeout`` anyway.
        attempt = self._require_finished_attempt(
            task, run, allow_unfinished=deadline == "fired")
        self._require_same_execution_version(candidate, attempt)
        workspace_base_commit = self._workspace_base_commit(run, task)
        if record is not None and record.attempt_context is not None:
            recorded_execution = record.attempt_context.execution_sha256
            if recorded_execution is not None and recorded_execution != attempt.execution_sha256:
                # The record was written for another execution version (or copied between
                # attempts): its verdict cannot stand in for this Run's.
                raise ExperimentConflictError(
                    "The run record names a different execution version than the Run it "
                    "describes")
        from devflow_ai.benchmark_run import classify_outcome
        decision = classify_outcome(
            self.session, run, deadline_fired=deadline == "fired",
            independent_suite_version=suite, artifact_root=root, finalization=finalization)
        outcome = str(decision.outcome.value)
        if outcome not in RESULT_OUTCOMES:  # pragma: no cover - the classifier's vocabulary
            raise ExperimentStateError(f"Classifier produced an unknown outcome {outcome!r}")
        if record is not None and record.outcome.value != outcome:
            raise ExperimentConflictError(
                f"The run record states {record.outcome.value!r} while the committed evidence "
                f"yields {outcome!r}; resolve the discrepancy before recording a verdict")
        verification_id = None
        if decision.formal is not None:
            verification_id = _require_uuid(
                decision.formal.verification_id, "verification_id")
        reason = (decision.reason or "").strip()[:1000] or None
        recorded = self._insert_result(
            experiment=experiment, candidate=candidate, task_id=task, run_id=run,
            outcome=outcome, recorded_by=operator, evidence_source="classified",
            decision_reason=reason, verification_id=verification_id,
            deadline_context=deadline, context_source=source,
            context_evidence=evidence, record_sha256=record_sha256,
            record_provenance=record_provenance, attempt_kind=attempt_kind,
            run_execution_sha256=attempt.execution_sha256,
            workspace_base_commit=workspace_base_commit)
        recorded["classifier_reason"] = reason
        recorded["record_outcome"] = None if record is None else record.outcome.value
        recorded["attempt_kind"] = attempt_kind
        recorded["finalization_supplied"] = finalization is not None
        return recorded

    def _read_attempt_record(self, record_path, *, task_id: UUID,
                             run_id: UUID | None = None):
        """Read one run record from the exact bytes that are digested, bound to this attempt."""
        from hashlib import sha256

        from devflow_ai.benchmark_record import parse_run_record
        path = Path(record_path)
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ExperimentEvidenceError(
                f"Run record is unreadable: {type(error).__name__}") from None
        try:
            record = parse_run_record(raw)
        except ValueError as error:
            raise ExperimentEvidenceError(f"Run record is not usable: {error}") from None
        if record.run_id is None:
            raise ExperimentEvidenceError(
                "Run record does not name a Run; it cannot be bound to an attempt")
        recorded_run = _require_uuid(record.run_id, "run_id")
        if run_id is not None and recorded_run != run_id:
            raise ExperimentEvidenceError(
                "Run record does not describe this Run; it cannot stand in for its context")
        if record.task_id is not None and record.task_id != str(task_id):
            raise ExperimentEvidenceError(
                "Run record does not describe this Task; it cannot stand in for its context")
        if run_id is None:
            # Registration is not tied to a Run the caller names, so the record's own Run has
            # to be found and has to belong to the declared Task.
            stored = self.session.get(Run, recorded_run)
            if stored is None or stored.task_id != task_id:
                raise ExperimentEvidenceError(
                    "Run record names a Run that does not belong to this Task")
        return record, sha256(raw).hexdigest()

    @staticmethod
    def _require_artifacts_root(artifacts_root) -> Path:
        root = Path(artifacts_root)
        if not root.is_dir():
            raise ExperimentEvidenceError(
                "artifacts_root must be an existing directory of recorded artifacts; without "
                "it the trusted chain cannot be loaded back and no verdict can be derived")
        return root

    def _prepared_candidate(self, experiment: Experiment, label: str) -> ExperimentCandidate:
        candidate = self._candidate(experiment, _require_text(label, "candidate_label", 200))
        if candidate.state != "prepared":
            raise ExperimentStateError(
                f"Candidate {candidate.label!r} is concluded; its record cannot gain results")
        return candidate

    def _workspace_base_commit(self, run_id: UUID, task_id: UUID) -> str | None:
        """The base commit the attempt's checkout actually verified, if a record exists.

        ``execute_worker_git_checkout`` refuses a checkout whose HEAD differs from the pinned
        commit and stores the verified value on its ToolCall row. Reading it back is evidence
        about the **target repository**, which is a different fact from the DevFlow execution
        version: both matter for comparing candidates, neither substitutes for the other. No
        row means unknown for this Run; a row that disagrees with the Task's pin is a conflict.
        """
        recorded = (self.session.scalar(
            select(ToolCall.stdout_excerpt).where(
                ToolCall.run_id == run_id,
                ToolCall.tool_name == "git_checkout",
                ToolCall.status == ToolCallStatus.SUCCEEDED,
            ).order_by(ToolCall.created_at.desc(), ToolCall.id).limit(1)) or "").strip()
        if not recorded:
            return None
        pinned = self.session.scalar(select(Task.base_commit).where(Task.id == task_id))
        if pinned is not None and recorded != pinned:
            raise ExperimentConflictError(
                "This Run's verified checkout is at a different base commit than the Task "
                "pins; refusing to file it as that baseline")
        return recorded

    def _require_same_execution_version(
        self, candidate: ExperimentCandidate, run: Run,
    ) -> None:
        """Refuse to attribute a Run that provably ran a different execution version.

        A Run without a recorded digest is *unknown*, not a mismatch: the result is still
        recorded, and the promotion rule keeps it out of the promotable set. Only a Run that
        names a different digest is a conflict, because that would silently move one
        candidate's attempt into another candidate's record.
        """
        if run.execution_sha256 is not None and run.execution_sha256 != candidate.execution_sha256:
            raise ExperimentConflictError(
                "This Run ran a different execution version than the candidate names "
                f"(run={run.execution_sha256}, candidate={candidate.execution_sha256}); "
                "refusing to attribute it")

    def _require_declared_task(self, experiment: Experiment, task_id: UUID) -> None:
        frozen = self.task_baselines(experiment.id)
        if task_id not in frozen:
            raise ExperimentStateError(
                "This Task is not part of the experiment's declared task set")
        current = self.session.scalar(select(Task.base_commit).where(Task.id == task_id))
        if current != frozen[task_id]:
            raise ExperimentConflictError(
                "This Task's base commit changed after the experiment opened; results from it "
                "are no longer comparable to the declared baseline")

    def _require_finished_attempt(
        self, task_id: UUID, run_id: UUID, *, allow_unfinished: bool = False
    ) -> Run:
        run = self.session.get(Run, run_id)
        if run is None:
            raise ExperimentEvidenceError("Run does not exist")
        if run.task_id != task_id:
            raise ExperimentEvidenceError("Run does not belong to the recorded Task")
        if run.finished_at is None and not allow_unfinished:
            raise ExperimentEvidenceError(
                "Run has not finished; a result may only describe a finished attempt")
        return run

    def _require_recorded_formal_verification(self, run_id: UUID) -> None:
        run = self.session.get(Run, run_id)
        if run.status != RunStatus.PASSED:
            raise ExperimentEvidenceError(
                f"Run status is {run.status.value}, which cannot support a formal_pass")
        verifications = self.session.scalars(select(Verification).where(
            Verification.run_id == run_id,
            Verification.result == VerificationResult.PASSED)).all()
        reason = "no passed Verification is recorded for this Run"
        for item in verifications:
            evidence = dict(item.evidence or {})
            if evidence.get("scope") != FORMAL_EVIDENCE_SCOPE:
                reason = "the recorded Verification is not scoped to the trusted evaluation"
                continue
            draft = str(evidence.get("draft_artifact_id") or "").strip()
            receipt = str(evidence.get("independent_artifact_id") or "").strip()
            suite = str(evidence.get("suite_version") or "").strip()
            if not draft or not receipt or not suite:
                reason = "the recorded Verification lacks draft, receipt or suite references"
                continue
            return
        raise ExperimentEvidenceError(
            f"formal_pass is not recorded: {reason}. Declared results never satisfy the "
            "promotion thresholds; use record_classified_result for the derived verdict")

    def _insert_result(
        self, *, experiment: Experiment, candidate: ExperimentCandidate, task_id: UUID,
        run_id: UUID, outcome: str, recorded_by: str, evidence_source: str,
        decision_reason: str | None, verification_id: UUID | None,
        deadline_context: str | None = None, context_source: str | None = None,
        context_evidence: str = "declared", record_sha256: str | None = None,
        record_provenance: str | None = None, attempt_kind: str | None = None,
        run_execution_sha256: str | None = None, workspace_base_commit: str | None = None,
    ) -> dict:
        if evidence_source not in EVIDENCE_SOURCES:
            raise ExperimentError(
                f"evidence_source must be one of {', '.join(EVIDENCE_SOURCES)}")
        statement = (
            postgres_insert(ExperimentTaskResult)
            .values(experiment_id=experiment.id, candidate_id=candidate.id, task_id=task_id,
                    run_id=run_id, outcome=outcome, recorded_by=recorded_by,
                    evidence_source=evidence_source, decision_reason=decision_reason,
                    verification_id=verification_id, deadline_context=deadline_context,
                    context_source=context_source, context_evidence=context_evidence,
                    record_sha256=record_sha256, record_provenance=record_provenance,
                    attempt_kind=attempt_kind, run_execution_sha256=run_execution_sha256,
                    workspace_base_commit=workspace_base_commit)
            .on_conflict_do_nothing(index_elements=["candidate_id", "task_id", "run_id"])
            .returning(ExperimentTaskResult.id))
        try:
            # The savepoint keeps a violated constraint from poisoning the caller's
            # transaction; the (experiment, run) constraint is a real insert conflict rather
            # than a replay, so it is reported instead of being swallowed.
            with self.session.begin_nested():
                created = self.session.execute(statement).scalar_one_or_none()
        except IntegrityError as error:
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint != "uq_experiment_task_results_run":
                # Anything else is a defect or an invalid row, not an attribution problem.
                raise
            owner = self.session.scalar(
                select(ExperimentCandidate.label).join(
                    ExperimentTaskResult,
                    ExperimentTaskResult.candidate_id == ExperimentCandidate.id).where(
                    ExperimentTaskResult.experiment_id == experiment.id,
                    ExperimentTaskResult.run_id == run_id))
            raise ExperimentConflictError(
                "This Run is already recorded in this experiment for candidate "
                f"{owner!r}; one Run cannot be attributed to two candidates") from None
        if created is None:
            existing = self.session.scalar(select(ExperimentTaskResult).where(
                ExperimentTaskResult.candidate_id == candidate.id,
                ExperimentTaskResult.task_id == task_id,
                ExperimentTaskResult.run_id == run_id))
            if existing is None:  # pragma: no cover - the conflict named an existing row
                raise ExperimentStateError("Attempt result is not readable after a conflict")
            recorded = (existing.outcome, existing.evidence_source, existing.verification_id,
                        existing.deadline_context, existing.context_source,
                        existing.context_evidence, existing.record_sha256,
                        existing.record_provenance, existing.attempt_kind,
                        existing.run_execution_sha256, existing.workspace_base_commit)
            submitted = (outcome, evidence_source, verification_id, deadline_context,
                         context_source, context_evidence, record_sha256, record_provenance,
                         attempt_kind, run_execution_sha256, workspace_base_commit)
            if recorded != submitted:
                raise ExperimentConflictError(
                    f"This attempt is already recorded as {existing.outcome!r} from "
                    f"{existing.evidence_source!r} evidence; a recorded verdict is not rewritten")
        else:
            self._recount(candidate)
        self.session.flush()
        return {
            "experiment_id": str(experiment.id), "candidate": candidate.label,
            "task_id": str(task_id), "run_id": str(run_id), "outcome": outcome,
            "evidence_source": evidence_source,
            "verification_id": None if verification_id is None else str(verification_id),
            "deadline_context": deadline_context,
            "context_source": context_source,
            "context_evidence": context_evidence,
            "record_sha256": record_sha256,
            "record_provenance": record_provenance,
            "attempt_kind": attempt_kind,
            "run_execution_sha256": run_execution_sha256,
            "workspace_base_commit": workspace_base_commit,
            "formal_passes": candidate.formal_passes,
            "non_formal_results": candidate.non_formal_results,
            "tasks_with_formal_pass": len(self._classified_formal_tasks(candidate)),
            "tasks_with_failure": len(self._failed_tasks(candidate)),
        }

    def _classified_formal_tasks(self, candidate: ExperimentCandidate) -> set[UUID]:
        """Declared Tasks with at least one **promotable** classifier-derived formal pass.

        Four conditions, all recorded in the row: the classifier produced the verdict, the
        attempt's context came from a persisted run record rather than a caller's statement,
        the record says the attempt was **formal** (a rehearsal is evidence, not a sample), and
        its deadline state is known (a fired deadline outranks a formal pass, so an unknown one
        cannot be counted). The Task's verified target baseline is checked separately by
        :meth:`_baseline_condition`. Tasks are counted distinctly: a repeated attempt on one
        Task is coverage of that Task only.
        """
        return {row.task_id for row in self._promotable_formal_rows(candidate)}

    def _promotable_formal_rows(self, candidate: ExperimentCandidate):
        """The rows that could stand as a formal sample, before any baseline check.

        One place defines the criteria so the promotion gate, the summary and the diagnosis
        cannot drift apart.
        """
        return list(self.session.scalars(select(ExperimentTaskResult).where(
            ExperimentTaskResult.candidate_id == candidate.id,
            ExperimentTaskResult.outcome == "formal_pass",
            ExperimentTaskResult.evidence_source == "classified",
            ExperimentTaskResult.context_evidence == "record",
            ExperimentTaskResult.attempt_kind == "formal",
            ExperimentTaskResult.deadline_context != "unknown",
            # The digest frozen on the row when it was written, not whatever the Run says now:
            # a later change to the Run cannot re-label an attempt.
            ExperimentTaskResult.run_execution_sha256 == candidate.execution_sha256)).all())

    def _failed_tasks(self, candidate: ExperimentCandidate) -> set[UUID]:
        return set(self.session.scalars(select(ExperimentTaskResult.task_id).where(
            ExperimentTaskResult.candidate_id == candidate.id,
            ExperimentTaskResult.outcome.in_(FAILURE_OUTCOMES))).all())

    def _recorded_tasks(self, candidate: ExperimentCandidate) -> set[UUID]:
        return set(self.session.scalars(select(ExperimentTaskResult.task_id).where(
            ExperimentTaskResult.candidate_id == candidate.id)).all())

    def _outcome_counts(self, candidate: ExperimentCandidate) -> dict[str, int]:
        """Every recorded outcome, counted separately so a pass never hides a non-verdict."""
        counts = {name: 0 for name in RESULT_OUTCOMES}
        for outcome, count in self.session.execute(
            select(ExperimentTaskResult.outcome, func.count()).where(
                ExperimentTaskResult.candidate_id == candidate.id
            ).group_by(ExperimentTaskResult.outcome)).all():
            counts[str(outcome)] = int(count)
        return counts

    def _results_with_unknown_context(self, candidate: ExperimentCandidate) -> int:
        """Results classified without a known deadline context: honest, but not promotable."""
        return int(self.session.scalar(
            select(func.count()).select_from(ExperimentTaskResult).where(
                ExperimentTaskResult.candidate_id == candidate.id,
                ExperimentTaskResult.evidence_source == "classified",
                ExperimentTaskResult.deadline_context == "unknown")) or 0)

    def _results_with_declared_context(self, candidate: ExperimentCandidate) -> int:
        """Results whose context is the caller's statement rather than a run record."""
        return int(self.session.scalar(
            select(func.count()).select_from(ExperimentTaskResult).where(
                ExperimentTaskResult.candidate_id == candidate.id,
                ExperimentTaskResult.evidence_source == "classified",
                ExperimentTaskResult.context_evidence == "declared")) or 0)

    def _recount(self, candidate: ExperimentCandidate) -> None:
        """Refresh the attempt counters shown in the summary.

        These are **attempt** counts, kept for display. They are deliberately not what the
        promotion thresholds compare against: those count distinct declared Tasks with a
        classifier-derived pass, so retrying one Task cannot inflate coverage.
        """
        totals = dict(self.session.execute(
            select(ExperimentTaskResult.outcome, func.count()).where(
                ExperimentTaskResult.candidate_id == candidate.id
            ).group_by(ExperimentTaskResult.outcome)).all())
        candidate.formal_passes = int(totals.get("formal_pass", 0))
        candidate.non_formal_results = int(sum(totals.values()) - candidate.formal_passes)

    def decide(
        self, *, experiment_id, candidate_label: str, outcome: str, reason: str,
        decided_by: str,
    ) -> dict:
        """Record the decision on one candidate, with the reason it was taken.

        ``accepted`` is disabled even when the recorded thresholds are met. The checks that run
        first now require: no overrun, no unsettled or unknown-usage reservation, a result for
        every declared Task, enough **distinct** declared Tasks with a *classifier-derived*
        formal pass, failures within the declared maximum, and a verified target baseline on
        every promotable pass. It stays disabled because
        execution-version binding and Run-to-candidate attribution are still declarations rather
        than verified facts.
        """
        if outcome not in DECISION_OUTCOMES:
            raise ExperimentError(f"outcome must be one of {', '.join(DECISION_OUTCOMES)}")
        note = _require_text(reason, "reason", 4000)
        operator = _require_text(decided_by, "decided_by", 200)
        experiment = self._lock_open_experiment(experiment_id)
        candidate = self.session.scalar(select(ExperimentCandidate).where(
            ExperimentCandidate.experiment_id == experiment.id,
            ExperimentCandidate.label == _require_text(candidate_label, "candidate_label", 200)
        ).with_for_update())
        if candidate is None:
            raise ExperimentStateError(f"Candidate {candidate_label!r} is not part of this experiment")
        if candidate.state == "concluded":
            if (candidate.outcome, candidate.reason, candidate.decided_by) == (outcome, note, operator):
                return self._candidate_summary(experiment, candidate)
            raise ExperimentConflictError(
                f"Candidate {candidate.label!r} is already concluded as {candidate.outcome!r}")
        if outcome == "accepted":
            self._require_promotion_thresholds(experiment, candidate)
            raise ExperimentStateError(
                "Acceptance is unavailable until the candidate's execution version and the "
                "Run-to-candidate attribution are verified facts rather than declarations")
        candidate.state = "concluded"
        candidate.outcome = outcome
        candidate.reason = note
        candidate.decided_by = operator
        candidate.concluded_at = get_database_time(self.session)
        self.session.flush()
        return self._candidate_summary(experiment, candidate)

    def _promotion_checks(
        self, experiment: Experiment, candidate: ExperimentCandidate
    ) -> dict:
        """The necessary conditions, evaluated once so every reader sees the same answer.

        ``decide`` raises from this list and :meth:`explain_acceptance` prints it. Neither the
        list nor a fully green result makes acceptance available: see the ``blocked_by`` notes
        in :meth:`explain_acceptance`.
        """
        declared = set(self.task_ids(experiment.id))
        formal_tasks = self._classified_formal_tasks(candidate) & declared
        failed_tasks = self._failed_tasks(candidate) & declared
        missing = declared - self._recorded_tasks(candidate)
        gaps = self._accounting_gaps(experiment)

        def condition(name: str, met: bool, detail: str) -> dict:
            return {"condition": name, "met": met, "detail": "" if met else detail}

        return {
            "distinct_formal_tasks": len(formal_tasks),
            "distinct_failed_tasks": len(failed_tasks),
            "declared_tasks": len(declared),
            "conditions": [
                condition("budget_within_limit", not experiment.over_budget,
                          "experiment is over budget"),
                condition("accounting_complete", not gaps,
                          "accounting is incomplete: " + "; ".join(gaps)),
                condition("every_declared_task_recorded", not missing,
                          f"{len(missing)} declared Task(s) have no result"),
                condition("distinct_formal_passes_meet_minimum",
                          len(formal_tasks) >= experiment.promotion_min_formal_passes,
                          f"distinct Tasks with a classified formal pass {len(formal_tasks)} < "
                          f"{experiment.promotion_min_formal_passes}"),
                condition("failures_within_maximum",
                          len(failed_tasks) <= experiment.promotion_max_failures,
                          f"distinct Tasks with a failure {len(failed_tasks)} > "
                          f"{experiment.promotion_max_failures}"),
                self._baseline_condition(experiment, candidate),
                self._record_origin_condition(candidate),
            ],
        }

    def _record_origin_condition(self, candidate: ExperimentCandidate) -> dict:
        """Every promotable pass must come from a record the control plane fixed.

        Without a registration there is no way to tell the record's origin, so the pass is
        kept as evidence and excluded here. Nothing promotable makes the condition vacuously
        true — the other conditions already say there is nothing to promote.
        """
        rows = self._promotable_formal_rows(candidate)
        unregistered = [row for row in rows
                        if row.record_provenance != "control_plane_registered"]
        detail = ("" if not unregistered else
                  f"{len(unregistered)} formal pass(es) come from a run record the control "
                  "plane did not fix")
        return {"condition": "promotable_passes_have_registered_records",
                "met": not unregistered, "detail": detail}

    def _baseline_condition(self, experiment: Experiment,
                            candidate: ExperimentCandidate) -> dict:
        """Every promotable formal pass needs a verified target baseline that matches.

        A missing value is "unknown" (no verified checkout was on record) and a different value
        is a contradiction; neither may stand in for "the candidate was measured on the declared
        baseline". No promotable pass at all makes this condition vacuously true — the other
        conditions already say there is nothing to promote.
        """
        frozen = self.task_baselines(experiment.id)
        rows = self._promotable_formal_rows(candidate)
        missing = [row for row in rows if row.workspace_base_commit is None]
        different = [row for row in rows
                     if row.workspace_base_commit is not None
                     and row.workspace_base_commit != frozen.get(row.task_id)]
        problems = []
        if missing:
            problems.append(
                f"{len(missing)} formal pass(es) have no verified target baseline")
        if different:
            problems.append(
                f"{len(different)} formal pass(es) ran on a baseline that differs from the "
                "declared one")
        return {"condition": "promotable_passes_have_verified_baseline",
                "met": not problems, "detail": "; ".join(problems)}

    def explain_acceptance(self, experiment_id, candidate_label: str) -> dict:
        """Read-only: which necessary conditions hold, and what acceptance still needs.

        Nothing is written and nothing is concluded. This exists so an operator does not have
        to guess why a candidate cannot be promoted — and so a fully green list is not read as
        permission: ``acceptable`` stays false and ``blocked_by`` names the requirements this
        version cannot satisfy with recorded evidence.
        """
        experiment = self.read(experiment_id)
        candidate = self._candidate(
            experiment, _require_text(candidate_label, "candidate_label", 200))
        checks = self._promotion_checks(experiment, candidate)
        return {
            "experiment_id": str(experiment.id),
            "candidate": candidate.label,
            "candidate_state": candidate.state,
            "declared_tasks": checks["declared_tasks"],
            "distinct_formal_tasks": checks["distinct_formal_tasks"],
            "distinct_failed_tasks": checks["distinct_failed_tasks"],
            "conditions": checks["conditions"],
            "problems": [item["detail"] for item in checks["conditions"]
                         if not item["met"]],
            "observed": {
                "execution_binding": ("verified_runs"
                                      if checks["distinct_formal_tasks"] else "no_verified_run"),
                "results_with_workspace_baseline": self.session.scalar(
                    select(func.count()).select_from(ExperimentTaskResult).where(
                        ExperimentTaskResult.candidate_id == candidate.id,
                        ExperimentTaskResult.workspace_base_commit.is_not(None))) or 0,
            },
            "acceptable": False,
            "blocked_by": [
                "the execution version is verified by the running process itself, not attested",
                "the run record's bytes are fixed but its author is unverified "
                "(record_provenance = caller_supplied)",
                "Run-to-candidate attribution is still a caller declaration",
            ],
        }

    def _require_promotion_thresholds(
        self, experiment: Experiment, candidate: ExperimentCandidate
    ) -> None:
        checks = self._promotion_checks(experiment, candidate)
        problems = [item["detail"] for item in checks["conditions"] if not item["met"]]
        if problems:
            raise ExperimentLimitError(
                "Promotion is not available: " + "; ".join(problems))

    def _accounting_gaps(self, experiment: Experiment) -> list[str]:
        """What stops the recorded spend from being a complete account of the experiment."""
        outstanding = int(self.session.scalar(
            select(func.count()).select_from(ExperimentReservation).where(
                ExperimentReservation.experiment_id == experiment.id,
                ExperimentReservation.state == "reserved")) or 0)
        unknown = int(self.session.scalar(
            select(func.count()).select_from(ExperimentReservation).where(
                ExperimentReservation.experiment_id == experiment.id,
                ExperimentReservation.state == "unknown")) or 0)
        dispatched = int(self.session.scalar(
            select(func.count()).select_from(ExperimentReservation).where(
                ExperimentReservation.experiment_id == experiment.id,
                ExperimentReservation.state == "reserved",
                ExperimentReservation.stage == "dispatched")) or 0)
        gaps = []
        if outstanding:
            gaps.append(f"{outstanding} reservation(s) not settled or released")
        if dispatched:
            gaps.append(
                f"{dispatched} request(s) may have been sent and are not settled")
        if unknown:
            gaps.append(f"{unknown} reservation(s) with unknown usage")
        return gaps

    def close(self, *, experiment_id, reason: str) -> dict:
        """Close the experiment.

        Afterwards no new reservation, result or decision is accepted; settling, releasing or
        marking an already-authorised reservation's usage unknown is still allowed so the
        accounting can be completed.
        """
        note = _require_text(reason, "reason", 1000)
        experiment = self.session.scalar(select(Experiment).where(
            Experiment.id == _require_uuid(experiment_id, "experiment_id")).with_for_update())
        if experiment is None:
            raise ExperimentStateError("Experiment does not exist")
        if experiment.closed_at is not None:
            if experiment.closed_reason != note:
                raise ExperimentConflictError(
                    f"Experiment is already closed ({experiment.closed_reason})")
            return self.summary(experiment.id)
        experiment.closed_at = get_database_time(self.session)
        experiment.closed_reason = note
        self.session.flush()
        return self.summary(experiment.id)

    # ---------------------------------------------------------------- reporting

    def _candidate_summary(self, experiment: Experiment, candidate: ExperimentCandidate) -> dict:
        results = self.session.scalars(select(ExperimentTaskResult).where(
            ExperimentTaskResult.candidate_id == candidate.id).order_by(
            ExperimentTaskResult.task_id, ExperimentTaskResult.created_at)).all()
        parent = None
        if candidate.parent_id is not None:
            parent = self.session.scalar(select(ExperimentCandidate.label).where(
                ExperimentCandidate.id == candidate.parent_id))
        promotable = self._classified_formal_tasks(candidate)
        return {
            "candidate_id": str(candidate.id), "label": candidate.label,
            "execution_sha256": candidate.execution_sha256, "parent": parent,
            "state": candidate.state, "outcome": candidate.outcome,
            "reason": candidate.reason, "decided_by": candidate.decided_by,
            "formal_passes": candidate.formal_passes,
            "non_formal_results": candidate.non_formal_results,
            # Distinct-Task counts; only the promotable one is compared with a threshold.
            "tasks_with_classified_formal_pass": len(promotable),
            # "verified_runs" means at least one formal pass came from a Run that itself
            # records the candidate's execution digest. Anything else is a caller's claim.
            "execution_binding": ("verified_runs" if promotable else "no_verified_run"),
            # The target-repository baseline each attempt's checkout actually verified. This is
            # a separate fact from the execution binding above and is reported, not required.
            "workspace_baseline": self._workspace_baseline_state(results),
            "results_with_workspace_baseline": sum(
                1 for item in results if item.workspace_base_commit),
            "record_origin": self._record_origin_state(results),
            "results_with_registered_record": sum(
                1 for item in results
                if item.record_provenance == "control_plane_registered"),
            "tasks_with_failure": len(self._failed_tasks(candidate)),
            "tasks_with_result": len(self._recorded_tasks(candidate)),
            "results_with_unknown_deadline_context":
                self._results_with_unknown_context(candidate),
            "results_with_declared_context": self._results_with_declared_context(candidate),
            # public_only_pass and incomplete are recorded evidence, not acceptance; they are
            # counted here so a reader never has to infer them from a pass count.
            "outcome_counts": self._outcome_counts(candidate),
            "results": [
                {"task_id": str(item.task_id), "run_id": str(item.run_id),
                 "outcome": item.outcome, "evidence_source": item.evidence_source,
                 "verification_id": (None if item.verification_id is None
                                     else str(item.verification_id)),
                 "deadline_context": item.deadline_context,
                 "context_source": item.context_source,
                 "context_evidence": item.context_evidence,
                 "record_sha256": item.record_sha256,
                 "record_provenance": item.record_provenance,
                 "attempt_kind": item.attempt_kind,
                 "run_execution_sha256": item.run_execution_sha256,
                 "workspace_base_commit": item.workspace_base_commit,
                 "decision_reason": item.decision_reason,
                 "recorded_by": item.recorded_by}
                for item in results],
        }

    @staticmethod
    def _record_origin_state(results) -> str:
        recorded = [item for item in results if item.context_evidence == "record"]
        if not recorded:
            return "no_record"
        registered = sum(1 for item in recorded
                         if item.record_provenance == "control_plane_registered")
        if registered == 0:
            return "unregistered"
        return "registered" if registered == len(recorded) else "partial"

    @staticmethod
    def _workspace_baseline_state(results) -> str:
        recorded = sum(1 for item in results if item.workspace_base_commit)
        if not results or recorded == 0:
            return "unverified"
        return "recorded" if recorded == len(results) else "partial"

    def summary(self, experiment_id) -> dict:
        """A read-only view of the recorded declarations, evidence and accounting.

        This is what a reviewer reads. It reports what was declared and recorded; it evaluates
        nothing, and no candidate is described as better than another. ``accounting_complete``
        is false while a reservation is unsettled or its usage is unknown. Each result names
        where its context came from (``context_evidence``, and ``record_provenance`` when a
        record was read — a caller-supplied file whose digest fixes its bytes, not its author),
        which execution digest its Run recorded, and each candidate summarises that as
        ``verified_runs`` or ``no_verified_run``.
        """
        experiment = self.read(experiment_id)
        candidates = [
            self._candidate_summary(experiment, item)
            for item in self.session.scalars(select(ExperimentCandidate).where(
                ExperimentCandidate.experiment_id == experiment.id).order_by(
                ExperimentCandidate.created_at, ExperimentCandidate.label)).all()]
        outstanding = self.session.scalar(select(func.count()).select_from(
            ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment.id,
            ExperimentReservation.state == "reserved")) or 0
        unknown = self.session.scalar(select(func.count()).select_from(
            ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment.id,
            ExperimentReservation.state == "unknown")) or 0
        gaps = self._accounting_gaps(experiment)
        return {
            "experiment_id": str(experiment.id),
            "identity_key": experiment.identity_key,
            "base_execution_sha256": experiment.base_execution_sha256,
            "created_by": experiment.created_by,
            "created_at": experiment.created_at.astimezone(timezone.utc).isoformat(),
            "deadline_at": experiment.deadline_at.astimezone(timezone.utc).isoformat(),
            "closed_at": (experiment.closed_at.astimezone(timezone.utc).isoformat()
                          if experiment.closed_at is not None else None),
            "closed_reason": experiment.closed_reason,
            # Per candidate below: a candidate digest is only "verified_runs" when a Run
            # records the same digest. Nothing here attests that the process was trusted.
            "candidate_execution_binding": "per_candidate",
            "limits": {
                "max_candidates": experiment.max_candidates,
                "max_total_model_tokens": experiment.max_total_model_tokens,
                "max_total_seconds": experiment.max_total_seconds,
                "promotion_min_formal_passes": experiment.promotion_min_formal_passes,
                "promotion_max_failures": experiment.promotion_max_failures,
            },
            "usage": {
                "reserved_tokens": experiment.reserved_tokens,
                "settled_tokens": experiment.settled_tokens,
                "over_budget": experiment.over_budget,
                "remaining_tokens": None if unknown else max(
                    0, experiment.max_total_model_tokens
                    - experiment.settled_tokens - experiment.reserved_tokens),
                "outstanding_reservations": int(outstanding),
                # Reserved requests whose call may already have reached the provider.
                "dispatched_unsettled": int(self.session.scalar(
                    select(func.count()).select_from(ExperimentReservation).where(
                        ExperimentReservation.experiment_id == experiment.id,
                        ExperimentReservation.state == "reserved",
                        ExperimentReservation.stage == "dispatched")) or 0),
                "unknown_usage_reservations": int(unknown),
                "accounting_complete": not gaps,
                "accounting_gaps": gaps,
            },
            "tasks": [str(item) for item in self.task_ids(experiment.id)],
            # The target baseline frozen per Task at open time.
            "task_baselines": {str(key): value
                               for key, value in sorted(
                                   self.task_baselines(experiment.id).items(), key=lambda pair: str(pair[0]))},
            "candidates": candidates,
        }
