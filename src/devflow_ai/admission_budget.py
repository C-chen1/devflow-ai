"""Durable model request accounting for Task admission before a Run exists."""
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from devflow_ai.coordinator import ModelRequestReservation, ModelTokenUsage
from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.policy import PolicyConfig
from devflow_ai.services import TaskNotFoundError, TaskStateConflictError
from devflow_ai.admission_claim import AdmissionClaim, AdmissionClaimDeniedError, AdmissionClaimService


class AdmissionUsageConflictError(ValueError):
    """The model usage does not match a reservation or conflicts with a settlement."""


class AdmissionBudgetService:
    def __init__(self, session: Session):
        self.session = session

    def _lock_task(self, task_id: UUID) -> Task:
        task = self.session.scalar(select(Task).where(Task.id == task_id).with_for_update()
                                   .execution_options(populate_existing=True))
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        return task

    def reserve(self, task_id: UUID, *, estimated_input_tokens: int,
                max_output_tokens: int, claim: AdmissionClaim | None = None) -> ModelRequestReservation | None:
        if type(estimated_input_tokens) is not int or estimated_input_tokens < 0:
            raise ValueError("estimated input tokens must be a non-negative integer")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max output tokens must be a positive integer")
        with self.session.begin():
            if claim is not None:
                if claim.task_id != task_id:
                    raise AdmissionClaimDeniedError("Admission claim belongs to a different Task")
                AdmissionClaimService(self.session).assert_claim(claim)
            task = self._lock_task(task_id)
            if claim is None and task.admission_claim_token is not None:
                if task.admission_claim_expires_at > self.session.scalar(select(func.clock_timestamp())):
                    raise AdmissionClaimDeniedError("Task has an active admission claim")
            if task.status != TaskStatus.PENDING:
                raise TaskStateConflictError("Admission model requests require a PENDING Task")
            policy = PolicyConfig.model_validate(task.policy_snapshot)
            remaining = policy.budget.max_admission_llm_tokens_per_task
            if remaining is None:
                return None
            for entry in task.admission_usage.values():
                actual = entry["actual_usage"]
                remaining -= entry["reserved_tokens"] if actual is None else (
                    actual["input_tokens"] + actual["output_tokens"]
                )
            output_limit = min(max_output_tokens, remaining - estimated_input_tokens)
            if output_limit <= 0:
                return None
            reservation = ModelRequestReservation(request_id=uuid4(), max_output_tokens=output_limit)
            usage = {**task.admission_usage, str(reservation.request_id): {
                "reserved_tokens": estimated_input_tokens + output_limit, "actual_usage": None,
            }}
            if claim is None:
                task.admission_usage = usage
            else:
                result = self.session.execute(update(Task).where(
                    Task.id == task_id, Task.status == TaskStatus.PENDING,
                    Task.admission_claim_token == claim.token, Task.admission_claim_owner == claim.owner,
                    Task.admission_claim_expires_at > func.clock_timestamp(),
                ).values(admission_usage=usage))
                if result.rowcount != 1:
                    raise AdmissionClaimDeniedError("Admission claim expired before reservation")
        return reservation

    def settle(self, task_id: UUID, *, request_id: UUID, actual_usage: ModelTokenUsage) -> None:
        reported = ModelTokenUsage.model_validate(actual_usage.model_dump()).model_dump()
        with self.session.begin():
            task = self._lock_task(task_id)
            entry = task.admission_usage.get(str(request_id))
            if entry is None:
                raise AdmissionUsageConflictError("No matching admission reservation")
            if entry["actual_usage"] == reported:
                return
            if entry["actual_usage"] is not None:
                raise AdmissionUsageConflictError("Admission request already settled differently")
            task.admission_usage = {**task.admission_usage, str(request_id): {
                **entry, "actual_usage": reported,
            }}
