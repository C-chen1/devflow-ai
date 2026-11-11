from collections.abc import Callable
from uuid import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field
from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
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
