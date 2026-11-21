"""Explicit memory ingestion: one statement plus the formal record that supports it.

This is the smallest closed loop for reviewable memory, not a memory service framework:

* a candidate is supplied **explicitly** by a trusted caller; the service does not extract
  memories from runs. A separate opt-in Worker path can supply one candidate to Explorer;
* the source is re-checked in the database before anything is written: the Task, the Run, the
  trusted Verification and, for a reviewed memory, the accepted Task review must actually
  relate to each other and to the declared repository;
* **two facts are kept apart**. ``source_state`` records what the cited rows establish, and is
  derived from them rather than declared by the caller. ``content_status`` records what is
  known about the statement itself: this version has no mechanism that checks free text, so
  every memory is an ``unverified_candidate``. A trusted Verification passing, or a person
  accepting the Run, says nothing about whether the sentence is true, complete, or reviewed.
  Nothing here may be described as a verified fact or as human-reviewed memory;
* a repeated submission is idempotent through the unique identity, a different memory wearing
  the same identity is refused rather than overwritten, and a retired identity is not
  resurrected by replaying it;
* the cited rows are locked for the duration of the admission check (``FOR SHARE``) so an
  in-flight change cannot land between the check and the insert.

Boundaries this version does not cross: no project-wide scope (there is no project entity to
check against), no tenancy or permission model beyond the declared repository string — that
string is a scope label, not authentication — no reading of Artifact bytes (only its ownership
and recorded hash), no content checking of any kind, and no extraction from failed Runs or
Agent self-reports. Conflict and revision records are operator declarations, not fact checks.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from re import fullmatch
import sys
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from devflow_ai.database import get_database_time
from devflow_ai.enums import RunStatus, VerificationResult
from devflow_ai.models import (
    AgentExecution, Artifact, MemoryConflict, MemoryItem, MemoryReview, MemoryRevision, MemoryUse, Run, Task, TaskReview, ToolCall, Verification,
)

SUPPORTED_SCOPE_KINDS = ("repository",)
MAX_CONTENT_CHARS = 4000


def _is_known_benchmark(task: Task) -> bool:
    """Identify known benchmark packs; lack of a marker never proves evaluation safety.

    benchmark_task.create_benchmark_task records ``pack:<pack_id>`` in admission evidence.
    This is a narrow defense in depth, not an authoritative classification of every Task.
    """
    admission = task.admission_record
    evidence = admission.get("evidence") if isinstance(admission, dict) else None
    return isinstance(evidence, list) and any(
        isinstance(item, str) and item.startswith("pack:") for item in evidence)


def _reject_known_benchmark_target(task: Task) -> None:
    if _is_known_benchmark(task):
        raise MemoryScopeError("Memory preview and use are disabled for known benchmark Tasks")


class MemoryError(ValueError):
    """The memory cannot be ingested as submitted."""


class MemoryEvidenceError(MemoryError):
    """The cited formal record does not support this memory."""


class MemoryScopeError(MemoryError):
    """The declared scope contradicts the source record."""


class MemoryConflictError(MemoryError):
    """The identity already names a different memory."""


class MemoryStateError(MemoryError):
    """The memory is missing or in a state that forbids this operation."""


def _validate_environment_label(value: str) -> None:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 128 or any(ord(character) < 32 for character in value)):
        raise MemoryError("Environment label must be a trimmed, non-blank string of at most 128 characters")


@dataclass(frozen=True)
class RepositoryScope:
    """Where a memory claims to apply: one repository, exactly as the Task names it.

    Only ``repository`` scope is supported, because it is the only applicability that can be
    checked against a record this project already keeps. A repository string is not a tenancy
    or permission boundary — the caller is already trusted to read the memory it ingests.
    """

    repository: str
    kind: str = "repository"

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_SCOPE_KINDS:
            raise MemoryScopeError(
                f"Only {', '.join(SUPPORTED_SCOPE_KINDS)} scope can be verified; "
                f"{self.kind!r} is not supported")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise MemoryScopeError("A memory scope needs a non-blank repository")
        if self.repository != self.repository.strip():
            raise MemoryScopeError("A memory scope repository must not have surrounding space")


@dataclass(frozen=True)
class MemoryCandidate:
    """One explicit memory submission; every field is a claim to be verified."""

    identity_key: str
    scope: RepositoryScope
    source_kind: Literal["verification", "task_review"]
    task_id: UUID
    run_id: UUID
    verification_id: UUID
    content: str
    created_by: str
    task_review_id: UUID | None = None
    artifact_id: UUID | None = None
    # Optional cross-check of the cited artifact's recorded hash; the stored value always
    # comes from the Artifact row itself, never from this submission.
    artifact_sha256: str | None = None
    topic: str | None = None
    valid_until: datetime | None = None
    applicable_base_commit: str | None = None
    applicable_environment: str | None = None


@dataclass(frozen=True)
class MemoryRecallRequest:
    """Trusted operator's explicit selection for one development Explorer execution."""
    idempotency_key: UUID
    memory_id: UUID
    candidate_sha256: str
    operator: str
    purpose: str
    target_environment: str


class MemoryService:
    """Ingest, read and retire explicit memories for one repository scope."""

    def __init__(self, session: Session):
        self.session = session

    # -- ingestion -------------------------------------------------------------------

    def ingest(self, candidate: MemoryCandidate) -> dict:
        """Persist one candidate, or refuse it with the reason it cannot be admitted.

        Validation and insertion share one transaction, so what was checked is what the row
        describes. The insert is an ``ON CONFLICT DO NOTHING`` on the identity, so two writers
        racing on the same identity cannot both create a row and neither can silently replace
        the other: the loser reads the committed row and compares the whole submission.
        """
        self._validate_submission(candidate)
        with self.session.begin():
            # Match TaskReview's Task -> Run lock order. The INSERT also acquires foreign-key
            # locks on Task, so postponing the Task lock until then could deadlock a reviewer
            # that already holds Task and is waiting for Run. Protect repo during admission.
            task = self.session.scalar(select(Task).where(
                Task.id == candidate.task_id).with_for_update(read=True))
            if task is None:
                raise MemoryEvidenceError("The Task does not exist")
            if _is_known_benchmark(task):
                raise MemoryScopeError("Memory ingestion is disabled for known benchmark source Tasks")
            run = self.session.scalar(
                select(Run).where(Run.id == candidate.run_id).with_for_update(read=True))
            if run is None:
                raise MemoryEvidenceError("The Run does not belong to this Task")
            if run.task_id != task.id:
                raise MemoryEvidenceError("The Run does not belong to this Task")
            if run.status != RunStatus.PASSED:
                raise MemoryEvidenceError(
                    f"The Run is {run.status.value}; only a passed Run supports a memory")
            if task.repo != candidate.scope.repository:
                raise MemoryScopeError(
                    "The declared repository does not match the Task's repository")
            verification = self.session.scalar(select(Verification).where(
                Verification.id == candidate.verification_id,
                Verification.run_id == run.id).with_for_update(read=True))
            if verification is None:
                raise MemoryEvidenceError("The Verification does not belong to this Run")
            if verification.result != VerificationResult.PASSED:
                raise MemoryEvidenceError("The Verification did not pass")
            evidence = dict(verification.evidence or {})
            if evidence.get("scope") != "trusted_v1":
                raise MemoryEvidenceError("The Verification is not trusted-scope evidence")
            if not str(evidence.get("suite_version") or "").strip():
                raise MemoryEvidenceError(
                    "The Verification does not record which independent suite it ran")
            source_state, review_id = self._derive_source_state(
                candidate, task=task, run=run, verification=verification)
            artifact_sha256 = self._cited_artifact_hash(candidate, run=run)
            values = {
                "identity_key": candidate.identity_key,
                "scope_kind": candidate.scope.kind,
                "scope_value": candidate.scope.repository,
                "source_kind": candidate.source_kind,
                "task_id": task.id,
                "run_id": run.id,
                "verification_id": verification.id,
                "task_review_id": review_id,
                "artifact_id": candidate.artifact_id,
                "artifact_sha256": artifact_sha256,
                "source_state": source_state,
                # No mechanism checks the statement, so it is recorded as what it is.
                "content_status": "unverified_candidate",
                "content": candidate.content.strip(),
                "topic": candidate.topic,
                "valid_until": candidate.valid_until,
                "applicable_base_commit": candidate.applicable_base_commit,
                "applicable_environment": candidate.applicable_environment,
                "created_by": candidate.created_by.strip(),
            }
            inserted = self.session.scalar(
                postgres_insert(MemoryItem).values(**values)
                .on_conflict_do_nothing(index_elements=["identity_key"])
                .returning(MemoryItem.id))
            if inserted is None:
                existing = self.session.scalar(select(MemoryItem).where(
                    MemoryItem.identity_key == candidate.identity_key))
                if existing is None:
                    raise MemoryConflictError(
                        "The identity is already taken but its row cannot be read back")
                differing = [name for name, submitted, stored in (
                    ("scope_kind", values["scope_kind"], existing.scope_kind),
                    ("scope_value", values["scope_value"], existing.scope_value),
                    ("source_kind", values["source_kind"], existing.source_kind),
                    ("source_state", values["source_state"], existing.source_state),
                    ("task", values["task_id"], existing.task_id),
                    ("run", values["run_id"], existing.run_id),
                    ("verification", values["verification_id"], existing.verification_id),
                    ("task_review", review_id, existing.task_review_id),
                    ("artifact", candidate.artifact_id, existing.artifact_id),
                    ("artifact_sha256", artifact_sha256, existing.artifact_sha256),
                    ("content", values["content"], existing.content),
                    ("topic", values["topic"], existing.topic),
                    ("valid_until", values["valid_until"], existing.valid_until),
                    ("applicable_base_commit", values["applicable_base_commit"], existing.applicable_base_commit),
                    ("applicable_environment", values["applicable_environment"], existing.applicable_environment),
                    ("created_by", values["created_by"], existing.created_by),
                ) if submitted != stored]
                if differing:
                    raise MemoryConflictError(
                        "This identity already names a different memory: "
                        + ", ".join(differing))
                if existing.status == "retired":
                    # Reinstating a retired memory by replaying the original submission would
                    # silently undo the withdrawal; a replacement needs its own identity.
                    raise MemoryStateError(
                        "This identity is retired; a replacement memory needs a new identity")
                return self._describe(existing, idempotent=True)
            created = self.session.get(MemoryItem, inserted)
            if created is None:
                raise MemoryError("The memory was written but cannot be read back")
            return self._describe(created, idempotent=False)

    def _validate_submission(self, candidate: MemoryCandidate) -> None:
        if (candidate.applicable_base_commit is None) != (candidate.applicable_environment is None):
            raise MemoryError("Applicable base commit and environment must be provided together")
        if candidate.applicable_base_commit is not None and (not isinstance(candidate.applicable_base_commit, str)
                or fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", candidate.applicable_base_commit) is None):
            raise MemoryError("Applicable base commit must be a full lowercase commit ID")
        if candidate.applicable_environment is not None:
            _validate_environment_label(candidate.applicable_environment)
        if (candidate.topic is None) != (candidate.valid_until is None):
            raise MemoryError("Topic and expiry must be provided together")
        if candidate.topic is not None and (not isinstance(candidate.topic, str) or
                not candidate.topic.strip() or candidate.topic != candidate.topic.strip() or
                len(candidate.topic) > 100):
            raise MemoryError("Topic must be a non-blank trimmed string of at most 100 characters")
        if candidate.valid_until is not None and (not isinstance(candidate.valid_until, datetime)
                or candidate.valid_until.utcoffset() is None):
            raise MemoryError("Expiry must be a timezone-aware datetime")
        if not isinstance(candidate.identity_key, str) or not candidate.identity_key.strip():
            raise MemoryError("A memory needs a non-blank identity")
        if candidate.identity_key != candidate.identity_key.strip():
            raise MemoryError("A memory identity must not have surrounding space")
        if candidate.source_kind not in ("verification", "task_review"):
            raise MemoryError("source_kind must be verification or task_review")
        if not isinstance(candidate.content, str) or not candidate.content.strip():
            raise MemoryError("A memory needs non-blank content")
        # The stored value is the stripped statement, so the limit applies to what is stored.
        if len(candidate.content.strip()) > MAX_CONTENT_CHARS:
            raise MemoryError(
                f"A memory statement is limited to {MAX_CONTENT_CHARS} characters")
        if not isinstance(candidate.created_by, str) or not candidate.created_by.strip():
            raise MemoryError("A memory needs a creator")
        if candidate.source_kind == "verification" and candidate.task_review_id is not None:
            raise MemoryEvidenceError(
                "A verification-sourced memory must not cite a review; cite the review as its "
                "own source instead")
        if candidate.source_kind == "task_review" and candidate.task_review_id is None:
            raise MemoryEvidenceError("A review-sourced memory must cite the review it used")
        if candidate.artifact_sha256 is not None:
            digest = candidate.artifact_sha256
            if candidate.artifact_id is None:
                raise MemoryEvidenceError("An artifact hash is meaningless without its artifact")
            if not isinstance(digest, str) or len(digest) != 64 \
                    or any(character not in "0123456789abcdef" for character in digest):
                raise MemoryError("An artifact hash must be a full sha256 in lower-case hex")

    def _derive_source_state(
        self, candidate: MemoryCandidate, *, task: Task, run: Run, verification: Verification,
    ) -> tuple[str, UUID | None]:
        """Derive what the cited rows establish; the caller cannot assert any of it."""
        if candidate.source_kind == "verification":
            return "trusted_passed", None
        review = self.session.scalar(select(TaskReview).where(
            TaskReview.id == candidate.task_review_id).with_for_update(read=True))
        if review is None:
            raise MemoryEvidenceError("The cited Task review does not exist")
        if review.task_id != task.id or review.run_id != run.id \
                or review.verification_id != verification.id:
            raise MemoryEvidenceError(
                "The cited Task review does not belong to this Task, Run and Verification")
        if review.conclusion != "accepted":
            raise MemoryEvidenceError(
                f"The Task review concluded {review.conclusion!r}, not accepted")
        return "review_accepted", review.id

    def _cited_artifact_hash(self, candidate: MemoryCandidate, *, run: Run) -> str | None:
        """Check the cited artifact's ownership and recorded hash; never read its bytes."""
        if candidate.artifact_id is None:
            return None
        artifact = self.session.scalar(select(Artifact).where(
            Artifact.id == candidate.artifact_id).with_for_update(read=True))
        if artifact is None:
            raise MemoryEvidenceError("The cited artifact does not exist")
        owner_run_id = None
        if artifact.agent_execution_id is not None:
            owner_run_id = self.session.scalar(select(AgentExecution.run_id).where(
                AgentExecution.id == artifact.agent_execution_id))
        elif artifact.tool_call_id is not None:
            owner_run_id = self.session.scalar(select(ToolCall.run_id).where(
                ToolCall.id == artifact.tool_call_id))
        if owner_run_id != run.id:
            raise MemoryEvidenceError("The cited artifact does not belong to this Run")
        if candidate.artifact_sha256 is not None \
                and candidate.artifact_sha256 != artifact.content_hash:
            raise MemoryEvidenceError(
                "The cited artifact's recorded hash does not match the submission")
        return artifact.content_hash

    # -- reading ---------------------------------------------------------------------

    def inspect_candidates(self, *, scope: RepositoryScope, target_task_id: UUID,
                           topic: str, limit: int = 20, target_environment: str | None = None) -> list[dict]:
        """Preview candidates for a trusted operator; never inject into a model or evaluation.

        All items are unverified. Same-topic items are returned together without asserting
        they conflict. Target Task ID exclusion does not establish benchmark independence.
        """
        if not isinstance(topic, str) or not topic.strip() or topic != topic.strip():
            raise MemoryError("A non-blank exact topic is required")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise MemoryError("Limit must be between 1 and 100")
        if target_environment is not None:
            _validate_environment_label(target_environment)
        with self.session.begin():
            task = self.session.scalar(select(Task).where(
                Task.id == target_task_id).with_for_update(read=True))
            if task is None or task.repo != scope.repository:
                raise MemoryScopeError("Target Task is missing or outside the declared scope")
            _reject_known_benchmark_target(task)
            # Discover a bounded snapshot, then lock source Tasks before candidates. This
            # preserves Task -> memory ordering used by ingestion and avoids lock inversion.
            candidates = self.session.execute(select(MemoryItem.id, MemoryItem.task_id).where(
                MemoryItem.scope_kind == scope.kind,
                MemoryItem.scope_value == scope.repository,
                MemoryItem.task_id != target_task_id,
                MemoryItem.status == "active",
                MemoryItem.topic == topic,
                MemoryItem.valid_until > get_database_time(self.session),
            ).order_by(MemoryItem.id).limit(limit + 1)).all()
            if len(candidates) > limit:
                raise MemoryError("Topic exceeds preview limit; narrow or increase the limit")
            source_ids = sorted({source_id for _, source_id in candidates}, key=lambda value: value.int)
            sources = {source.id: source for source in self.session.scalars(select(Task).where(
                Task.id.in_(source_ids)).order_by(Task.id).with_for_update(read=True)).all()} if source_ids else {}
            candidate_sources = dict(candidates)
            rows = self.session.scalars(select(MemoryItem).where(
                MemoryItem.id.in_(candidate_sources), MemoryItem.scope_kind == scope.kind,
                MemoryItem.scope_value == scope.repository, MemoryItem.task_id != target_task_id,
                MemoryItem.status == "active", MemoryItem.topic == topic,
            ).order_by(MemoryItem.id).with_for_update(read=True)).all() if candidate_sources else []
            if any(row.task_id != candidate_sources[row.id] or row.task_id not in sources for row in rows):
                raise MemoryStateError("Candidate source changed during preview")
            rows = [row for row in rows if not _is_known_benchmark(sources[row.task_id])]
            # The query may have waited for row locks after taking its cutoff time.
            # Keep the limit check first so filtering cannot conceal a truncated result.
            checked_at = get_database_time(self.session)
            rows = [row for row in rows if row.valid_until > checked_at]
            ids = [row.id for row in rows]
            conflicts = self.session.scalars(select(MemoryConflict).where(
                MemoryConflict.status == "open",
                or_(MemoryConflict.first_id.in_(ids), MemoryConflict.second_id.in_(ids)))
                .order_by(MemoryConflict.id)).all() if ids else []
            revisions = self.session.scalars(select(MemoryRevision).where(or_(
                MemoryRevision.previous_id.in_(ids), MemoryRevision.next_id.in_(ids)))
                .order_by(MemoryRevision.id)).all() if ids else []
            reviews = {review.memory_id: review for review in self.session.scalars(
                select(MemoryReview).where(MemoryReview.memory_id.in_(ids))).all()} if ids else {}
            result = []
            for row in rows:
                item = self._display_candidate(row)
                item["open_conflict_ids"] = [str(c.id) for c in conflicts if row.id in (c.first_id, c.second_id)]
                item["revision_ids"] = [str(r.id) for r in revisions if row.id in (r.previous_id, r.next_id)]
                review = reviews.get(row.id)
                review_state = ("missing" if review is None else "stale"
                    if review.candidate_sha256 != item["candidate_sha256"] else review.conclusion)
                has_successor = any(revision.previous_id == row.id for revision in revisions)
                base_match = (None if row.applicable_base_commit is None or task.base_commit is None
                              else row.applicable_base_commit == task.base_commit)
                environment_match = (None if row.applicable_environment is None or target_environment is None
                                     else row.applicable_environment == target_environment)
                item["candidate_checks"] = {
                    "review_state": review_state,
                    "review_id": str(review.id) if review is not None else None,
                    "has_open_conflict": bool(item["open_conflict_ids"]),
                    "has_declared_successor": has_successor,
                    "target_base_commit": task.base_commit,
                    "base_commit_matches": base_match,
                    "declared_target_environment": target_environment,
                    "environment_declaration_matches": environment_match,
                    # This is a precheck only: a live Run, permissions, applicable conditions,
                    # actual use and evaluation independence are not established here.
                    "eligible_to_request_use": review_state == "accepted"
                        and not item["open_conflict_ids"] and not has_successor
                        and base_match is True and environment_match is True,
                }
                result.append(item)
            return result

    def read(
        self, *, scope: RepositoryScope, memory_id: UUID | None = None,
        identity_key: str | None = None,
    ) -> dict | None:
        """Read one memory by its identity, inside the caller's declared scope.

        Exactly one identity is required. A missing memory, a retired memory and a memory that
        belongs to another scope all answer ``None``: the caller cannot tell an identity it may
        not see from one that does not exist, and this is not a search.
        """
        if (memory_id is None) == (identity_key is None):
            raise MemoryError("Pass exactly one of memory_id or identity_key")
        with self.session.begin():
            query = select(MemoryItem).where(
                MemoryItem.scope_kind == scope.kind,
                MemoryItem.scope_value == scope.repository,
                MemoryItem.status == "active",
            )
            if memory_id is not None:
                query = query.where(MemoryItem.id == memory_id)
            else:
                if not isinstance(identity_key, str) or not identity_key.strip():
                    raise MemoryError("An identity key must be a non-blank string")
                query = query.where(MemoryItem.identity_key == identity_key.strip())
            row = self.session.scalar(query)
            if row is None:
                return None
            return self._display_candidate(row)

    # -- retirement ------------------------------------------------------------------

    def record_use(self, *, idempotency_key: UUID, scope: RepositoryScope, task_id: UUID,
                   run_id: UUID, memory_id: UUID, candidate_sha256: str,
                   operator: str, purpose: str, target_environment: str | None = None,
                   _include_candidate: bool = False) -> dict:
        """Record an operator declaration, not proof of injection or independent evaluation."""
        if not all(isinstance(value, UUID) for value in (idempotency_key, task_id, run_id, memory_id)):
            raise MemoryError("Use and target identifiers must be UUIDs")
        if not isinstance(operator, str) or not operator.strip() or not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 1000:
            raise MemoryError("An operator and purpose of at most 1000 characters are required")
        if not isinstance(candidate_sha256, str) or len(candidate_sha256) != 64 or any(
                c not in "0123456789abcdef" for c in candidate_sha256):
            raise MemoryError("Candidate digest must be a lowercase SHA-256")
        if target_environment is not None:
            _validate_environment_label(target_environment)
        with self.session.begin():
            # Finalization locks Run before updating Task. Acquire Run first to avoid
            # holding Task while waiting for finalization. Review locks Task first;
            # NOWAIT breaks that opposing wait rather than forming another cycle.
            run = self.session.scalar(select(Run).where(
                Run.id == run_id, Run.task_id == task_id).with_for_update(read=True))
            if run is None:
                raise MemoryScopeError("Run does not belong to target Task")
            try:
                task = self.session.scalar(select(Task).where(
                    Task.id == task_id).with_for_update(read=True, nowait=True))
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "55P03":
                    raise MemoryStateError("Target Task is busy; retry the declaration") from None
                raise
            if task is None or task.repo != scope.repository:
                raise MemoryScopeError("Target Task missing or outside the declared scope")
            _reject_known_benchmark_target(task)
            source_id = self.session.scalar(select(MemoryItem.task_id).where(
                MemoryItem.id == memory_id, MemoryItem.scope_kind == scope.kind,
                MemoryItem.scope_value == scope.repository))
            source = self.session.scalar(select(Task).where(Task.id == source_id).with_for_update(read=True))
            if source is None:
                raise MemoryScopeError("Candidate missing or outside the declared scope")
            if _is_known_benchmark(source):
                raise MemoryScopeError("Memory use is disabled for known benchmark source Tasks")
            item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update(read=True))
            if item is None or (item.scope_kind, item.scope_value) != (scope.kind, scope.repository):
                raise MemoryScopeError("Candidate missing or outside the declared scope")
            if item.task_id != source_id:
                raise MemoryStateError("Candidate source changed during use declaration")
            values = {"idempotency_key": idempotency_key, "memory_id": memory_id, "task_id": task_id,
                      "run_id": run_id, "candidate_sha256": candidate_sha256,
                      "operator": operator.strip(), "purpose": purpose.strip(),
                      "target_environment": target_environment}
            existing = self.session.scalar(select(MemoryUse).where(MemoryUse.idempotency_key == idempotency_key))
            if existing is not None:
                if _include_candidate:
                    raise MemoryStateError("A historical declaration cannot prepare a new recall")
                return self._use_replay(existing, values)
            if run.status != RunStatus.RUNNING:
                raise MemoryStateError("A new use declaration requires a running Run")
            if item.task_id == task_id:
                raise MemoryScopeError("A candidate from the target Task cannot be declared for cross-task use")
            if item.status != "active" or item.topic is None or item.valid_until is None or item.valid_until <= get_database_time(self.session):
                raise MemoryStateError("Candidate must be active, scoped by topic and not expired")
            if (item.applicable_base_commit is None or item.applicable_environment is None
                    or target_environment is None or task.base_commit is None):
                raise MemoryStateError("Candidate and target need explicit applicability declarations")
            if item.applicable_base_commit != task.base_commit or item.applicable_environment != target_environment:
                raise MemoryScopeError("Candidate applicability does not match the target declaration")
            if self._candidate_digest(item) != candidate_sha256:
                raise MemoryConflictError("Candidate changed since it was displayed")
            review = self.session.scalar(select(MemoryReview).where(
                MemoryReview.memory_id == item.id).with_for_update(read=True))
            if review is None or review.conclusion != "accepted" or review.candidate_sha256 != candidate_sha256:
                raise MemoryEvidenceError("A matching accepted candidate review is required")
            conflict = self.session.scalar(select(MemoryConflict.id).where(MemoryConflict.status == "open",
                or_(MemoryConflict.first_id == item.id, MemoryConflict.second_id == item.id)).limit(1))
            if conflict is not None:
                raise MemoryStateError("Candidate has a declared unresolved conflict")
            successor = self.session.scalar(select(MemoryRevision.id).where(MemoryRevision.previous_id == item.id).limit(1))
            if successor is not None:
                raise MemoryStateError("Candidate has a declared successor")
            inserted = self.session.scalar(postgres_insert(MemoryUse).values(
                **values, target_base_commit=task.base_commit, review_id=review.id)
                .on_conflict_do_nothing(index_elements=["idempotency_key"]).returning(MemoryUse.id))
            if inserted is None:
                existing = self.session.scalar(select(MemoryUse).where(MemoryUse.idempotency_key == idempotency_key))
                if existing is None:
                    raise MemoryConflictError("Use identity already exists but cannot be read")
                if _include_candidate:
                    raise MemoryStateError("A historical declaration cannot prepare a new recall")
                return self._use_replay(existing, values)
            use = self._describe_use(self.session.get(MemoryUse, inserted), idempotent=False)
            if _include_candidate:
                return {"use": use, "candidate": self._display_candidate(item)}
            return use

    def prepare_recall(self, *, idempotency_key: UUID, scope: RepositoryScope, task_id: UUID,
                       run_id: UUID, memory_id: UUID, candidate_sha256: str, operator: str,
                       purpose: str, target_environment: str) -> dict:
        """Return one unverified candidate with a new operator declaration after commit.

        This trusted-operator operation is not an Agent context, permission check, actual
        model-use receipt, or evidence of benchmark independence. A replay never returns the
        candidate: callers must submit a new identity and recheck the current state.
        """
        return self.record_use(idempotency_key=idempotency_key, scope=scope, task_id=task_id,
            run_id=run_id, memory_id=memory_id, candidate_sha256=candidate_sha256,
            operator=operator, purpose=purpose, target_environment=target_environment,
            _include_candidate=True)

    @staticmethod
    def _use_replay(row: MemoryUse, values: dict) -> dict:
        if any(getattr(row, key) != value for key, value in values.items()):
            raise MemoryConflictError("Use identity already names a different declaration")
        return MemoryService._describe_use(row, idempotent=True)

    def read_use(self, *, scope: RepositoryScope, use_id: UUID) -> dict | None:
        with self.session.begin():
            row = self.session.scalar(select(MemoryUse).join(Task, Task.id == MemoryUse.task_id).join(
                MemoryItem, MemoryItem.id == MemoryUse.memory_id).where(MemoryUse.id == use_id,
                Task.repo == scope.repository, MemoryItem.scope_kind == scope.kind,
                MemoryItem.scope_value == scope.repository))
            return None if row is None else self._describe_use(row, idempotent=False)

    def list_task_uses(self, *, scope: RepositoryScope, task_id: UUID) -> list[dict]:
        """List this Task's historical operator declarations, including retired candidates."""
        with self.session.begin():
            rows = self.session.execute(select(MemoryUse, MemoryItem).join(
                Task, Task.id == MemoryUse.task_id).join(
                MemoryItem, MemoryItem.id == MemoryUse.memory_id).where(
                Task.id == task_id, Task.repo == scope.repository,
                MemoryItem.scope_kind == scope.kind, MemoryItem.scope_value == scope.repository,
            ).order_by(MemoryUse.created_at, MemoryUse.id)).all()
            return [{**self._describe_use(use, idempotent=False),
                     "candidate_identity": item.identity_key, "candidate_status": item.status,
                     "source_task_id": str(item.task_id), "source_run_id": str(item.run_id),
                     "source_verification_id": str(item.verification_id)} for use, item in rows]

    @staticmethod
    def _describe_use(row: MemoryUse, *, idempotent: bool) -> dict:
        return {"id": str(row.id), "idempotency_key": str(row.idempotency_key),
                "memory_id": str(row.memory_id), "review_id": str(row.review_id),
                "task_id": str(row.task_id), "run_id": str(row.run_id), "candidate_sha256": row.candidate_sha256,
                "operator": row.operator, "purpose": row.purpose,
                "target_base_commit": row.target_base_commit, "target_environment": row.target_environment,
                "created_at": row.created_at.astimezone(timezone.utc).isoformat(),
                "kind": "operator_declared", "idempotent": idempotent}

    def declare_revision(self, *, scope: RepositoryScope, previous_id: UUID, next_id: UUID,
                         previous_sha256: str, next_sha256: str, operator: str, reason: str) -> dict:
        """Link two existing candidates; this assertion neither retires nor verifies either one."""
        if not isinstance(previous_id, UUID) or not isinstance(next_id, UUID) or previous_id == next_id:
            raise MemoryError("Two distinct candidate IDs are required")
        if not isinstance(operator, str) or not operator.strip() or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 4000:
            raise MemoryError("An operator and reason of at most 4000 characters are required")
        with self.session.begin():
            items = {}
            for memory_id in sorted((previous_id, next_id), key=lambda value: value.int):
                item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update())
                if item is None or (item.scope_kind, item.scope_value) != (scope.kind, scope.repository):
                    raise MemoryScopeError("Candidate missing or outside the declared scope")
                items[memory_id] = item
            previous, successor = items[previous_id], items[next_id]
            if successor.status != "active":
                raise MemoryStateError("The successor must be active")
            if previous.created_at >= successor.created_at:
                raise MemoryError("The successor must have a later creation time")
            if (self._candidate_digest(previous), self._candidate_digest(successor)) != (previous_sha256, next_sha256):
                raise MemoryConflictError("Candidate changed since it was displayed")
            existing = self.session.scalars(select(MemoryRevision).where(or_(
                MemoryRevision.previous_id == previous_id, MemoryRevision.next_id == next_id))).all()
            if existing:
                if len(existing) == 1:
                    row = existing[0]
                    if (row.previous_id, row.next_id, row.previous_sha256, row.next_sha256, row.operator, row.reason) == (
                            previous_id, next_id, previous_sha256, next_sha256, operator.strip(), reason.strip()):
                        return self._describe_revision(row, idempotent=True)
                raise MemoryConflictError("Candidate already has a different predecessor or successor")
            row = MemoryRevision(previous_id=previous_id, next_id=next_id, previous_sha256=previous_sha256,
                                 next_sha256=next_sha256, operator=operator.strip(), reason=reason.strip())
            self.session.add(row)
            self.session.flush()
            return self._describe_revision(row, idempotent=False)

    def read_revisions(self, *, scope: RepositoryScope, memory_id: UUID) -> list[dict]:
        with self.session.begin():
            item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id,
                MemoryItem.scope_kind == scope.kind, MemoryItem.scope_value == scope.repository))
            if item is None:
                return []
            rows = self.session.scalars(select(MemoryRevision).where(or_(MemoryRevision.previous_id == memory_id,
                MemoryRevision.next_id == memory_id)).order_by(MemoryRevision.created_at, MemoryRevision.id)).all()
            result = []
            for row in rows:
                other_id = row.next_id if row.previous_id == memory_id else row.previous_id
                other = self.session.get(MemoryItem, other_id)
                if other is None or (other.scope_kind, other.scope_value) != (scope.kind, scope.repository):
                    continue
                previous, successor = (item, other) if row.previous_id == memory_id else (other, item)
                data = self._describe_revision(row, idempotent=False)
                data["snapshots_match"] = (self._candidate_digest(previous), self._candidate_digest(successor)) == (
                    row.previous_sha256, row.next_sha256)
                data["candidate_statuses"] = [previous.status, successor.status]
                result.append(data)
            return result

    @staticmethod
    def _describe_revision(row: MemoryRevision, *, idempotent: bool) -> dict:
        return {"id": str(row.id), "previous_id": str(row.previous_id), "next_id": str(row.next_id),
                "previous_sha256": row.previous_sha256, "next_sha256": row.next_sha256,
                "operator": row.operator, "reason": row.reason,
                "created_at": row.created_at.astimezone(timezone.utc).isoformat(), "idempotent": idempotent}

    def declare_conflict(self, *, scope: RepositoryScope, first_id: UUID, second_id: UUID,
                         first_sha256: str, second_sha256: str, reporter: str, reason: str) -> dict:
        """Record a declared conflict; neither source nor content becomes verified."""
        if not isinstance(first_id, UUID) or not isinstance(second_id, UUID) or first_id == second_id:
            raise MemoryError("Two distinct candidate IDs are required")
        if not isinstance(reporter, str) or not reporter.strip() or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 4000:
            raise MemoryError("A reporter and a reason of at most 4000 characters are required")
        pairs = sorted(((first_id, first_sha256), (second_id, second_sha256)), key=lambda pair: pair[0].int)
        with self.session.begin():
            items = []
            for memory_id, digest in pairs:
                item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update())
                if item is None or (item.scope_kind, item.scope_value) != (scope.kind, scope.repository):
                    raise MemoryScopeError("Candidate missing or outside the declared scope")
                if item.status != "active":
                    raise MemoryStateError("A retired candidate cannot be declared in a new conflict")
                if self._candidate_digest(item) != digest:
                    raise MemoryConflictError("Candidate changed since it was displayed")
                items.append(item)
            existing = self.session.scalar(select(MemoryConflict).where(
                MemoryConflict.first_id == items[0].id, MemoryConflict.second_id == items[1].id))
            if existing is not None:
                if (existing.first_sha256, existing.second_sha256, existing.reporter, existing.reason) != (
                        pairs[0][1], pairs[1][1], reporter.strip(), reason.strip()):
                    raise MemoryConflictError("These candidates already have a different conflict declaration")
                return self._describe_conflict(existing, idempotent=True)
            conflict = MemoryConflict(first_id=items[0].id, second_id=items[1].id,
                first_sha256=pairs[0][1], second_sha256=pairs[1][1], reporter=reporter.strip(), reason=reason.strip())
            self.session.add(conflict)
            self.session.flush()
            return self._describe_conflict(conflict, idempotent=False)

    def resolve_conflict(self, *, scope: RepositoryScope, conflict_id: UUID,
                         operator: str, resolution: str) -> dict:
        if not isinstance(operator, str) or not operator.strip() or not isinstance(resolution, str) or not 1 <= len(resolution.strip()) <= 4000:
            raise MemoryError("An operator and resolution of at most 4000 characters are required")
        with self.session.begin():
            initial = self.session.get(MemoryConflict, conflict_id)
            if initial is None:
                raise MemoryScopeError("Conflict missing or outside the declared scope")
            items = []
            for memory_id in (initial.first_id, initial.second_id):
                item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update())
                if item is None or (item.scope_kind, item.scope_value) != (scope.kind, scope.repository):
                    raise MemoryScopeError("Conflict missing or outside the declared scope")
                items.append(item)
            conflict = self.session.scalar(select(MemoryConflict).where(
                MemoryConflict.id == conflict_id).with_for_update().execution_options(populate_existing=True))
            if conflict is None:
                raise MemoryScopeError("Conflict missing or outside the declared scope")
            if (self._candidate_digest(items[0]), self._candidate_digest(items[1])) != (conflict.first_sha256, conflict.second_sha256):
                raise MemoryConflictError("A candidate changed since the conflict was declared")
            if conflict.status == "resolved":
                if (conflict.resolved_by, conflict.resolution) != (operator.strip(), resolution.strip()):
                    raise MemoryConflictError("Conflict already has a different resolution")
                return self._describe_conflict(conflict, idempotent=True)
            conflict.status = "resolved"
            conflict.resolved_at = get_database_time(self.session)
            conflict.resolved_by = operator.strip()
            conflict.resolution = resolution.strip()
            return self._describe_conflict(conflict, idempotent=False)

    def read_conflicts(self, *, scope: RepositoryScope, memory_id: UUID) -> list[dict]:
        with self.session.begin():
            item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id,
                MemoryItem.scope_kind == scope.kind, MemoryItem.scope_value == scope.repository))
            if item is None:
                return []
            records = self.session.scalars(select(MemoryConflict).where(or_(
                MemoryConflict.first_id == item.id, MemoryConflict.second_id == item.id))
                .order_by(MemoryConflict.created_at, MemoryConflict.id)).all()
            result = []
            for record in records:
                other_id = record.second_id if record.first_id == item.id else record.first_id
                other = self.session.get(MemoryItem, other_id)
                if other is None or (other.scope_kind, other.scope_value) != (scope.kind, scope.repository):
                    continue
                first, second = (item, other) if record.first_id == item.id else (other, item)
                description = self._describe_conflict(record, idempotent=False)
                description["snapshots_match"] = (self._candidate_digest(first), self._candidate_digest(second)) == (
                    record.first_sha256, record.second_sha256)
                description["candidate_statuses"] = [first.status, second.status]
                result.append(description)
            return result

    @staticmethod
    def _describe_conflict(row: MemoryConflict, *, idempotent: bool) -> dict:
        return {"id": str(row.id), "first_id": str(row.first_id), "second_id": str(row.second_id),
                "first_sha256": row.first_sha256, "second_sha256": row.second_sha256,
                "reporter": row.reporter, "reason": row.reason, "status": row.status,
                "created_at": row.created_at.astimezone(timezone.utc).isoformat(),
                "resolved_at": row.resolved_at.astimezone(timezone.utc).isoformat() if row.resolved_at else None,
                "resolved_by": row.resolved_by, "resolution": row.resolution, "idempotent": idempotent}

    def review(self, *, memory_id: UUID, scope: RepositoryScope, candidate_sha256: str,
               conclusion: str, reviewer: str, reason: str) -> dict:
        """Record a trusted operator's declaration about the exact candidate shown to them.

        This does not authenticate the named reviewer, verify the claim, or enable recall.
        Corrections need a new candidate; this initial review is not overwritten.
        """
        if conclusion not in ("accepted", "rejected"):
            raise MemoryError("Conclusion must be accepted or rejected")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise MemoryError("Review needs a reviewer")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 4000:
            raise MemoryError("Review needs a reason of at most 4000 characters")
        if not isinstance(candidate_sha256, str) or len(candidate_sha256) != 64 or any(
                c not in "0123456789abcdef" for c in candidate_sha256):
            raise MemoryError("Candidate digest must be a lowercase SHA-256")
        with self.session.begin():
            item = self.session.scalar(select(MemoryItem).where(
                MemoryItem.id == memory_id).with_for_update())
            if item is None or item.scope_kind != scope.kind or item.scope_value != scope.repository:
                raise MemoryScopeError("Memory is missing or outside the declared scope")
            if item.status != "active":
                raise MemoryStateError("A retired memory cannot be reviewed")
            if self._candidate_digest(item) != candidate_sha256:
                raise MemoryConflictError("Candidate changed since it was displayed")
            existing = self.session.scalar(select(MemoryReview).where(MemoryReview.memory_id == item.id))
            values = (candidate_sha256, conclusion, reviewer.strip(), reason.strip())
            if existing is not None:
                if values != (existing.candidate_sha256, existing.conclusion, existing.reviewer, existing.reason):
                    raise MemoryConflictError("Memory already has a different review")
                return self._describe_review(existing, idempotent=True)
            review = MemoryReview(memory_id=item.id, candidate_sha256=candidate_sha256,
                                  conclusion=conclusion, reviewer=reviewer.strip(), reason=reason.strip())
            self.session.add(review)
            self.session.flush()
            return self._describe_review(review, idempotent=False)

    def read_review(self, *, memory_id: UUID, scope: RepositoryScope) -> dict | None:
        with self.session.begin():
            item = self.session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id,
                MemoryItem.scope_kind == scope.kind, MemoryItem.scope_value == scope.repository))
            if item is None:
                return None
            review = self.session.scalar(select(MemoryReview).where(MemoryReview.memory_id == item.id))
            if review is None:
                return None
            result = self._describe_review(review, idempotent=False)
            result["candidate_matches"] = review.candidate_sha256 == self._candidate_digest(item)
            result["candidate_status"] = item.status
            return result

    @staticmethod
    def _describe_review(row: MemoryReview, *, idempotent: bool) -> dict:
        return {"id": str(row.id), "memory_id": str(row.memory_id),
                "candidate_sha256": row.candidate_sha256, "conclusion": row.conclusion,
                "reviewer": row.reviewer, "reason": row.reason,
                "created_at": row.created_at.astimezone(timezone.utc).isoformat(),
                "idempotent": idempotent}

    @staticmethod
    def _candidate_digest(row: MemoryItem) -> str:
        data = MemoryService._describe(row, idempotent=False)
        data.pop("idempotent")
        data.pop("status")
        version = 2 if row.applicable_base_commit is not None else 1
        if version == 1:
            data.pop("applicable_base_commit")
            data.pop("applicable_environment")
        encoded = json.dumps({"version": version, "candidate": data}, sort_keys=True,
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _display_candidate(row: MemoryItem) -> dict:
        result = MemoryService._describe(row, idempotent=False)
        result["candidate_sha256"] = MemoryService._candidate_digest(row)
        result["candidate_digest_version"] = 2 if row.applicable_base_commit is not None else 1
        return result

    def retire(
        self, memory_id: UUID, *, scope: RepositoryScope, operator: str, reason: str,
    ) -> dict:
        """Deactivate one memory inside its declared scope.

        The scope must match the stored one, for the same reason reads do: retiring by bare
        identifier would let a caller withdraw a memory it cannot even read. This is a scoped
        contract, not an authorization check — the repository string is not an identity.
        """
        if not isinstance(operator, str) or not operator.strip():
            raise MemoryError("Retirement needs an operator")
        if not isinstance(reason, str) or not reason.strip():
            raise MemoryError("Retirement needs a reason")
        with self.session.begin():
            row = self.session.scalar(select(MemoryItem).where(
                MemoryItem.id == memory_id).with_for_update())
            if row is None:
                raise MemoryStateError("The memory does not exist")
            if row.scope_kind != scope.kind or row.scope_value != scope.repository:
                raise MemoryScopeError("The memory does not belong to this scope")
            if row.status == "retired":
                raise MemoryStateError("The memory is already retired")
            row.status = "retired"
            row.retired_at = get_database_time(self.session)
            row.retired_by = operator.strip()
            row.retire_reason = reason.strip()
            return self._describe(row, idempotent=False)

    @staticmethod
    def _describe(row: MemoryItem, *, idempotent: bool) -> dict:
        return {
            "id": str(row.id),
            "identity_key": row.identity_key,
            "scope_kind": row.scope_kind,
            "scope_value": row.scope_value,
            "source_kind": row.source_kind,
            "source_state": row.source_state,
            "content_status": row.content_status,
            "content": row.content,
            "topic": row.topic,
            "valid_until": row.valid_until.astimezone(timezone.utc).isoformat() if row.valid_until else None,
            "applicable_base_commit": row.applicable_base_commit,
            "applicable_environment": row.applicable_environment,
            "task_id": str(row.task_id),
            "run_id": str(row.run_id),
            "verification_id": str(row.verification_id),
            "task_review_id": str(row.task_review_id) if row.task_review_id else None,
            "artifact_id": str(row.artifact_id) if row.artifact_id else None,
            "artifact_sha256": row.artifact_sha256,
            "created_by": row.created_by,
            "created_at": row.created_at.astimezone(timezone.utc).isoformat(),
            "status": row.status,
            "idempotent": idempotent,
        }


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "memory_configuration_error", "detail": detail}),
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Ingest, read or retire one explicit memory.

    Exit codes: 0 on success, 2 when the submission is refused (nothing was written), 1 when
    the database itself failed. No model is contacted and no Agent context is touched.
    """
    parser = argparse.ArgumentParser(
        description="Ingest, read or retire one explicit memory item; never invents one.")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Record one candidate against a formal record.")
    ingest.add_argument("--identity", required=True, help="Stable identity of this memory.")
    ingest.add_argument("--scope-repository", required=True,
                        help="Repository this memory claims to apply to, as the Task names it.")
    ingest.add_argument("--source-kind", required=True,
                        choices=["verification", "task_review"])
    ingest.add_argument("--task-id", required=True)
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--verification-id", required=True)
    ingest.add_argument("--task-review-id")
    ingest.add_argument("--artifact-id", help="Optional supporting artifact of the same Run.")
    ingest.add_argument("--artifact-sha256",
                        help="Optional expected hash; must match the artifact's recorded hash.")
    ingest.add_argument("--content", help="The memory statement itself.")
    ingest.add_argument("--content-file", help="Read the statement from a UTF-8 file instead.")
    ingest.add_argument("--topic", help="Exact topic for operator preview; requires --valid-until.")
    ingest.add_argument("--valid-until", help="Expiry as a timezone-aware ISO 8601 timestamp.")
    ingest.add_argument("--applicable-base-commit", help="Exact applicable base commit; requires environment.")
    ingest.add_argument("--applicable-environment", help="Operator-declared environment label.")
    ingest.add_argument("--created-by", required=True, help="Who is recording this memory.")

    read = commands.add_parser("read", help="Read one memory inside a declared scope.")
    read.add_argument("--scope-repository", required=True)
    read.add_argument("--memory-id")
    read.add_argument("--identity")

    preview = commands.add_parser("preview", help="Preview unverified candidates for a trusted operator.")
    preview.add_argument("--scope-repository", required=True)
    preview.add_argument("--target-task-id", required=True)
    preview.add_argument("--topic", required=True)
    preview.add_argument("--limit", type=int, default=20)
    preview.add_argument("--target-environment", help="Operator-declared target environment label.")

    retire = commands.add_parser("retire", help="Deactivate one memory.")
    retire.add_argument("--scope-repository", required=True)
    retire.add_argument("--memory-id", required=True)
    retire.add_argument("--operator", required=True)
    retire.add_argument("--reason", required=True)

    review = commands.add_parser("review", help="Record an operator-declared candidate review.")
    review.add_argument("--scope-repository", required=True)
    review.add_argument("--memory-id", required=True)
    review.add_argument("--candidate-sha256", required=True)
    review.add_argument("--conclusion", choices=["accepted", "rejected"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    read_review = commands.add_parser("read-review", help="Read a candidate review.")
    read_review.add_argument("--scope-repository", required=True)
    read_review.add_argument("--memory-id", required=True)

    conflict = commands.add_parser("declare-conflict", help="Record an operator-declared conflict.")
    conflict.add_argument("--scope-repository", required=True)
    for option in ("first-id", "second-id", "first-sha256", "second-sha256", "reporter", "reason"):
        conflict.add_argument("--" + option, required=True)
    resolve = commands.add_parser("resolve-conflict", help="Record a conflict resolution.")
    for option in ("scope-repository", "conflict-id", "operator", "resolution"):
        resolve.add_argument("--" + option, required=True)
    read_conflicts = commands.add_parser("read-conflicts", help="Read declared conflicts for a candidate.")
    read_conflicts.add_argument("--scope-repository", required=True)
    read_conflicts.add_argument("--memory-id", required=True)
    revision = commands.add_parser("declare-revision", help="Link two candidate versions.")
    for option in ("scope-repository", "previous-id", "next-id", "previous-sha256", "next-sha256", "operator", "reason"):
        revision.add_argument("--" + option, required=True)
    read_revisions = commands.add_parser("read-revisions", help="Read adjacent candidate versions.")
    read_revisions.add_argument("--scope-repository", required=True)
    read_revisions.add_argument("--memory-id", required=True)
    use = commands.add_parser("record-use", help="Record an operator-declared development use.")
    for option in ("scope-repository", "idempotency-key", "task-id", "run-id", "memory-id",
                   "candidate-sha256", "operator", "purpose"):
        use.add_argument("--" + option, required=True)
    use.add_argument("--target-environment", help="Required for new declarations; not an environment observation.")
    prepare = commands.add_parser("prepare-recall", help="Prepare one candidate for a trusted operator.")
    for option in ("scope-repository", "idempotency-key", "task-id", "run-id", "memory-id",
                   "candidate-sha256", "operator", "purpose", "target-environment"):
        prepare.add_argument("--" + option, required=True)
    read_use = commands.add_parser("read-use", help="Read an operator-declared use.")
    read_use.add_argument("--scope-repository", required=True)
    read_use.add_argument("--use-id", required=True)
    list_uses = commands.add_parser("list-uses", help="List operator declarations for a Task.")
    list_uses.add_argument("--scope-repository", required=True)
    list_uses.add_argument("--task-id", required=True)

    args = parser.parse_args(argv)

    def uuid_or_error(name: str, value: str | None):
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            raise MemoryError(f"{name} must be a UUID") from None

    engine = None
    try:
        if args.command == "ingest":
            try:
                valid_until = datetime.fromisoformat(args.valid_until) if args.valid_until else None
            except ValueError:
                raise MemoryError("valid-until must be an ISO 8601 timestamp") from None
            content = args.content
            if content is None and args.content_file:
                try:
                    content = Path(args.content_file).read_text(encoding="utf-8")
                except OSError as error:
                    return _configuration_error(
                        f"The content file is unreadable: {type(error).__name__}")
            if content is None:
                return _configuration_error("Provide --content or --content-file")
            candidate = MemoryCandidate(
                identity_key=args.identity,
                scope=RepositoryScope(repository=args.scope_repository),
                source_kind=args.source_kind,
                task_id=uuid_or_error("task-id", args.task_id),
                run_id=uuid_or_error("run-id", args.run_id),
                verification_id=uuid_or_error("verification-id", args.verification_id),
                task_review_id=uuid_or_error("task-review-id", args.task_review_id),
                artifact_id=uuid_or_error("artifact-id", args.artifact_id),
                artifact_sha256=args.artifact_sha256,
                content=content,
                topic=args.topic,
                valid_until=valid_until,
                applicable_base_commit=args.applicable_base_commit,
                applicable_environment=args.applicable_environment,
                created_by=args.created_by,
            )
        elif args.command == "read":
            scope = RepositoryScope(repository=args.scope_repository)
            memory_id = uuid_or_error("memory-id", args.memory_id)
            identity = args.identity
        elif args.command == "preview":
            scope = RepositoryScope(repository=args.scope_repository)
            target_task_id = uuid_or_error("target-task-id", args.target_task_id)
        elif args.command == "declare-conflict":
            scope = RepositoryScope(repository=args.scope_repository)
            first_id = uuid_or_error("first-id", args.first_id)
            second_id = uuid_or_error("second-id", args.second_id)
        elif args.command == "resolve-conflict":
            scope = RepositoryScope(repository=args.scope_repository)
            conflict_id = uuid_or_error("conflict-id", args.conflict_id)
        elif args.command == "declare-revision":
            scope = RepositoryScope(repository=args.scope_repository)
            previous_id = uuid_or_error("previous-id", args.previous_id)
            next_id = uuid_or_error("next-id", args.next_id)
        elif args.command in ("record-use", "prepare-recall"):
            scope = RepositoryScope(repository=args.scope_repository)
            use_key = uuid_or_error("idempotency-key", args.idempotency_key)
            task_id = uuid_or_error("task-id", args.task_id)
            run_id = uuid_or_error("run-id", args.run_id)
            memory_id = uuid_or_error("memory-id", args.memory_id)
        elif args.command == "read-use":
            scope = RepositoryScope(repository=args.scope_repository)
            use_id = uuid_or_error("use-id", args.use_id)
        elif args.command == "list-uses":
            scope = RepositoryScope(repository=args.scope_repository)
            task_id = uuid_or_error("task-id", args.task_id)
        else:
            scope = RepositoryScope(repository=args.scope_repository)
            memory_id = uuid_or_error("memory-id", args.memory_id)
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        with session_factory() as session:
            service = MemoryService(session)
            if args.command == "ingest":
                result = service.ingest(candidate)
            elif args.command == "read":
                result = service.read(scope=scope, memory_id=memory_id, identity_key=identity)
            elif args.command == "preview":
                result = service.inspect_candidates(scope=scope, target_task_id=target_task_id,
                                                    topic=args.topic, limit=args.limit,
                                                    target_environment=args.target_environment)
            elif args.command == "review":
                result = service.review(memory_id=memory_id, scope=scope,
                    candidate_sha256=args.candidate_sha256, conclusion=args.conclusion,
                    reviewer=args.reviewer, reason=args.reason)
            elif args.command == "read-review":
                result = service.read_review(memory_id=memory_id, scope=scope)
            elif args.command == "declare-conflict":
                result = service.declare_conflict(scope=scope, first_id=first_id, second_id=second_id,
                    first_sha256=args.first_sha256, second_sha256=args.second_sha256,
                    reporter=args.reporter, reason=args.reason)
            elif args.command == "resolve-conflict":
                result = service.resolve_conflict(scope=scope, conflict_id=conflict_id,
                    operator=args.operator, resolution=args.resolution)
            elif args.command == "read-conflicts":
                result = service.read_conflicts(scope=scope, memory_id=memory_id)
            elif args.command == "declare-revision":
                result = service.declare_revision(scope=scope, previous_id=previous_id, next_id=next_id,
                    previous_sha256=args.previous_sha256, next_sha256=args.next_sha256,
                    operator=args.operator, reason=args.reason)
            elif args.command == "read-revisions":
                result = service.read_revisions(scope=scope, memory_id=memory_id)
            elif args.command == "record-use":
                result = service.record_use(idempotency_key=use_key, scope=scope, task_id=task_id,
                    run_id=run_id, memory_id=memory_id, candidate_sha256=args.candidate_sha256,
                    operator=args.operator, purpose=args.purpose, target_environment=args.target_environment)
            elif args.command == "prepare-recall":
                result = service.prepare_recall(idempotency_key=use_key, scope=scope, task_id=task_id,
                    run_id=run_id, memory_id=memory_id, candidate_sha256=args.candidate_sha256,
                    operator=args.operator, purpose=args.purpose, target_environment=args.target_environment)
            elif args.command == "read-use":
                result = service.read_use(scope=scope, use_id=use_id)
            elif args.command == "list-uses":
                result = service.list_task_uses(scope=scope, task_id=task_id)
            else:
                result = service.retire(
                    memory_id, scope=scope, operator=args.operator, reason=args.reason)
    except MemoryError as error:
        return _configuration_error(str(error))
    except Exception as error:  # a database problem is reported, not raised as a stack trace
        print(json.dumps({"event": "memory_failed", "error_type": type(error).__name__}),
              file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    if args.command == "preview":
        print(json.dumps({"event": "memory_candidate_preview", "candidates": result}))
        return 0
    if args.command == "read-conflicts":
        print(json.dumps({"event": "memory_conflicts_read", "conflicts": result}))
        return 0
    if args.command == "read-revisions":
        print(json.dumps({"event": "memory_revisions_read", "revisions": result}))
        return 0
    if args.command == "list-uses":
        print(json.dumps({"event": "memory_uses_listed", "uses": result}))
        return 0
    if result is None:
        print(json.dumps({"event": "memory_not_found"}))
        return 0
    event = {"ingest": "memory_ingested", "read": "memory_read", "review": "memory_review_recorded",
             "declare-conflict": "memory_conflict_declared", "resolve-conflict": "memory_conflict_resolved",
             "declare-revision": "memory_revision_declared",
             "record-use": "memory_use_recorded", "prepare-recall": "memory_recall_prepared",
             "read-use": "memory_use_read",
             "read-review": "memory_review_read", "retire": "memory_retired"}[args.command]
    print(json.dumps({"event": event, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
