from collections.abc import Callable
from typing import Literal
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field
from devflow_ai.enums import RunStatus, TaskStatus, VerificationResult
from devflow_ai.models import Run, Task, TaskReview, Verification
from devflow_ai.policy import PolicyConfig, build_default_policy, build_policy_snapshot
from devflow_ai.repositories import TaskRepository
from devflow_ai.admission_claim import AdmissionClaim, AdmissionClaimService, AdmissionClaimDeniedError


class IdempotencyConflictError(ValueError):
    """A submission key was reused with different Issue content."""


class TaskNotFoundError(LookupError):
    """The requested Task does not exist."""


class TaskStateConflictError(ValueError):
    """The Task's current state does not allow the requested operation."""


class AdmissionRecord(BaseModel):
    """Reason and observed facts supplied with an admission decision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1)
    evidence: list[str]


class TaskService:
    def __init__(self, session: Session, *, policy_factory: Callable[[], PolicyConfig] | None = None):
        self.session = session
        self.task_repository = TaskRepository(session)
        self.policy_factory = policy_factory if policy_factory is not None else build_default_policy

    def create_task(
        self, title: str, description: str, repo: str, idempotency_key: UUID,
    ) -> tuple[Task, bool]:
        try:
            with self.session.begin():
                existing_task = self.task_repository.get_by_idempotency_key(idempotency_key)
                if existing_task is not None:
                    self._check_same_issue(existing_task, title, description, repo)
                    task = existing_task
                    created = False
                else:
                    supplied_policy = self.policy_factory()
                    if not isinstance(supplied_policy, PolicyConfig):
                        raise TypeError("The trusted policy factory must return PolicyConfig")
                    policy = PolicyConfig.model_validate(supplied_policy.model_dump())
                    policy_data = policy.model_dump(mode="json")
                    policy_snapshot = build_policy_snapshot(policy_data)
                    task = Task(
                        title=title,
                        description=description,
                        repo=repo,
                        idempotency_key=idempotency_key,
                        policy_version=policy.policy_version,
                        policy_snapshot=policy_snapshot,
                        admission_attempt_count=0,
                    )
                    self.task_repository.add(task)
                    created = True
        except IntegrityError as error:
            if not self._is_idempotency_key_conflict(error):
                raise
            # The failed transaction has left its context and rolled back.
            with self.session.begin():
                existing_task = self.task_repository.get_by_idempotency_key(idempotency_key)
                if existing_task is None:
                    raise
                self._check_same_issue(existing_task, title, description, repo)
                task = existing_task
                created = False

        return task, created

    @staticmethod
    def _check_same_issue(task: Task, title: str, description: str, repo: str) -> None:
        if (task.title, task.description, task.repo) != (title, description, repo):
            raise IdempotencyConflictError(
                "Idempotency key is already used for different task content"
            )

    @staticmethod
    def _is_idempotency_key_conflict(error: IntegrityError) -> bool:
        diagnostic = getattr(error.orig, "diag", None)
        return (
            getattr(error.orig, "sqlstate", None) == "23505"
            and getattr(diagnostic, "constraint_name", None) == "uq_tasks_idempotency_key"
        )

    def get_task(self, task_id: UUID) -> Task | None:
        return self.task_repository.get_by_id(task_id)

    def apply_admission_decision(
        self, task_id: UUID, target_status: TaskStatus, *, admission_record: AdmissionRecord,
        base_commit: str | None = None, claim: AdmissionClaim | None = None,
    ) -> Task:
        """Persist a supplied admission decision; this method does not make one."""
        if not isinstance(admission_record, AdmissionRecord):
            raise TypeError("admission_record must be an AdmissionRecord")
        record = AdmissionRecord.model_validate(admission_record.model_dump()).model_dump(mode="json")
        with self.session.begin():
            if claim is not None:
                if claim.task_id != task_id:
                    raise AdmissionClaimDeniedError("Admission claim belongs to a different Task")
                AdmissionClaimService(self.session).assert_claim(claim)
            claim_args = {"claim_token": claim.token} if claim is not None else {}
            affected_rows = self.task_repository.update_admission_status(
                task_id, target_status, base_commit=base_commit, admission_record=record, **claim_args,
            )
            task = self.task_repository.get_by_id(task_id, populate_existing=True)
            if task is None:
                raise TaskNotFoundError(f"Task {task_id} not found")
            if affected_rows == 0:
                if task.status == TaskStatus.PENDING:
                    raise AdmissionClaimDeniedError("Task has an active or expired admission claim")
                raise TaskStateConflictError(
                    f"Task {task_id} is {task.status.value}; admission requires PENDING"
                )
        return task


class TaskReviewError(ValueError):
    """The review cannot be recorded as submitted."""


class TaskReviewConflictError(TaskReviewError):
    """The Task already carries a different review."""


class TaskReviewEvidenceError(TaskReviewError):
    """The Run or the Verification does not support this review."""


class TaskReviewService:
    """Record one human review and move the Task in the same transaction.

    The review is bound to a specific Task, Run, formal Verification and run-record digest.
    A repeated submission of the same record is idempotent; a different record or a different
    conclusion is refused instead of guessed. The Task row is locked for the whole decision,
    so two reviewers cannot both win.
    """

    def __init__(self, session: Session):
        self.session = session
        self.task_repository = TaskRepository(session)

    def record(
        self, *, task_id: UUID, run_id: UUID, verification_id: UUID, record_sha256: str,
        conclusion: Literal["accepted", "rejected"], reviewer: str, reviewed_at: str,
        independent_suite_version: str | None,
    ) -> dict:
        """Record one review, binding it to the evidence it claims to have reviewed.

        The record names the independent suite version it was produced against; the
        Verification it cites carries the suite version the trusted evaluation actually ran.
        They must agree — a review that cannot say which suite it saw is not evidence, and the
        check runs on every submission, including an idempotent replay, so a replay cannot slip
        past it.
        """
        if conclusion not in ("accepted", "rejected"):
            raise TaskReviewError("A review conclusion must be accepted or rejected")
        if not reviewer.strip() or not reviewed_at.strip():
            raise TaskReviewError("A review needs a reviewer and a review time")
        if not isinstance(record_sha256, str) or len(record_sha256) != 64 \
                or any(character not in "0123456789abcdef" for character in record_sha256):
            raise TaskReviewError("A record digest must be a full sha256 in lower-case hex")
        declared_suite = (independent_suite_version or "").strip()
        if not declared_suite:
            raise TaskReviewEvidenceError(
                "The review does not declare the independent suite version it was made against")
        with self.session.begin():
            task = self.session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None:
                raise TaskNotFoundError(f"Task {task_id} does not exist")
            run = self.session.scalar(select(Run).where(
                Run.id == run_id, Run.task_id == task_id).with_for_update())
            if run is None:
                raise TaskReviewEvidenceError("The Run does not belong to this Task")
            if run.status != RunStatus.PASSED:
                raise TaskReviewEvidenceError(
                    f"The Run is {run.status.value}, not a passed attempt")
            verification = self.session.scalar(select(Verification).where(
                Verification.id == verification_id, Verification.run_id == run_id))
            if verification is None:
                raise TaskReviewEvidenceError("The Verification does not belong to this Run")
            if verification.result != VerificationResult.PASSED:
                raise TaskReviewEvidenceError("The Verification did not pass")
            evidence = verification.evidence or {}
            if evidence.get("scope") != "trusted_v1":
                raise TaskReviewEvidenceError(
                    "The Verification is not trusted-scope evidence")
            recorded_suite = str(evidence.get("suite_version") or "").strip()
            if not recorded_suite:
                raise TaskReviewEvidenceError(
                    "The Verification does not record which independent suite it ran")
            if recorded_suite != declared_suite:
                raise TaskReviewEvidenceError(
                    "The review declares suite version "
                    f"{declared_suite!r} but the Verification records {recorded_suite!r}")
            existing = self.session.scalar(select(TaskReview).where(
                TaskReview.task_id == task_id, TaskReview.record_sha256 == record_sha256))
            if existing is not None:
                # Idempotent only for the same submission. The digest identifies the record,
                # but the rest of what was bound to it must match too, or this is a different
                # review wearing the same record.
                differing = [
                    name for name, submitted, stored in (
                        ("run", run_id, existing.run_id),
                        ("verification", verification_id, existing.verification_id),
                        ("conclusion", conclusion, existing.conclusion),
                        ("reviewer", reviewer.strip(), existing.reviewer),
                        ("reviewed_at", reviewed_at.strip(), existing.reviewed_at),
                    ) if submitted != stored
                ]
                if differing:
                    raise TaskReviewConflictError(
                        "This record was already reviewed with different content: "
                        + ", ".join(differing))
                return self._result(existing, task, idempotent=True)
            other = self.session.scalar(select(TaskReview).where(TaskReview.task_id == task_id))
            if other is not None:
                raise TaskReviewConflictError("This Task was already reviewed with another record")
            if task.status != TaskStatus.AWAITING_REVIEW:
                raise TaskStateConflictError(
                    f"Task {task_id} is {task.status.value}, not awaiting review")
            review = TaskReview(
                task_id=task_id, run_id=run_id, verification_id=verification_id,
                record_sha256=record_sha256, conclusion=conclusion,
                reviewer=reviewer.strip(), reviewed_at=reviewed_at.strip(),
            )
            self.session.add(review)
            self.session.flush()
            task.status = (TaskStatus.COMPLETED if conclusion == "accepted"
                           else TaskStatus.REJECTED)
            return self._result(review, task, idempotent=False)

    @staticmethod
    def _result(review: TaskReview, task: Task, *, idempotent: bool) -> dict:
        return {"review_id": str(review.id), "conclusion": review.conclusion,
                "task_status": task.status.value, "idempotent": idempotent}
