"""Pre-Run claim primitives for admission and bounded recovery."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.policy import PolicyBudget, PolicyConfig
from devflow_ai.admission_intervention import record_reached_limits


def assess_recorded_timeout_limit(blocked: dict | None, budget: PolicyBudget) -> str:
    """Assess only the timeout limit; this does not authorize or schedule a retry."""
    if not isinstance(blocked, dict) or blocked.get("retry_hint") != "transient":
        return "NOT_TRANSIENT"
    count = blocked.get("consecutive_timeouts")
    limit = budget.max_admission_consecutive_timeouts
    if type(count) is not int or count < 1 or limit is None:
        return "UNKNOWN"
    return "LIMIT_REACHED" if count >= limit else "BELOW_LIMIT"


@dataclass(frozen=True)
class AdmissionClaim:
    task_id: UUID
    owner: str
    token: UUID
    expires_at: datetime


class AdmissionClaimDeniedError(RuntimeError):
    """The claim is absent, expired, or no longer matches the Task."""


class AdmissionClaimService:
    def __init__(self, session: Session):
        self.session = session

    def claim(self, task_id: UUID, *, owner: str, lease_seconds: int,
              automatic_retry: bool = False, automatic_initial: bool = False,
              automatic_recovery: bool = False) -> AdmissionClaim | None:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be non-blank")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        modes = (automatic_retry, automatic_initial, automatic_recovery)
        if any(type(mode) is not bool for mode in modes) or sum(modes) > 1:
            raise ValueError("automatic claim modes must be mutually exclusive booleans")
        with self.session.begin():
            task = self.session.scalar(select(Task).where(Task.id == task_id).with_for_update(skip_locked=True)
                                       .execution_options(populate_existing=True))
            if task is None or task.status != TaskStatus.PENDING:
                return None
            now = self.session.scalar(select(func.clock_timestamp()))
            if task.admission_claim_token is not None and task.admission_claim_expires_at > now:
                return None
            if automatic_initial or automatic_recovery:
                budget = PolicyConfig.model_validate(task.policy_snapshot).budget
                if (budget.max_admission_attempts is None or task.admission_attempt_count is None
                        or task.admission_attempt_count >= budget.max_admission_attempts
                        or budget.max_admission_llm_tokens_per_task in (None, 0)
                        or task.admission_blocked is not None):
                    record_reached_limits(task, now)
                    return None
                if automatic_initial and (task.admission_attempt_count != 0 or task.admission_claim_token is not None):
                    return None
                if automatic_recovery and (task.admission_attempt_count < 1 or task.admission_claim_token is None
                        or budget.admission_timeout_retry_delay_seconds is None
                        or now < task.admission_claim_expires_at + timedelta(seconds=budget.admission_timeout_retry_delay_seconds)):
                    return None
            if automatic_retry:
                budget = PolicyConfig.model_validate(task.policy_snapshot).budget
                if (budget.max_admission_attempts is None or task.admission_attempt_count is None
                        or task.admission_attempt_count >= budget.max_admission_attempts
                        or budget.max_admission_llm_tokens_per_task in (None, 0)
                        or assess_recorded_timeout_limit(task.admission_blocked, budget) != "BELOW_LIMIT"
                        or budget.admission_timeout_retry_delay_seconds is None or task.admission_blocked_at is None
                        or now < task.admission_blocked_at + timedelta(seconds=budget.admission_timeout_retry_delay_seconds)):
                    record_reached_limits(task, now)
                    return None
            token = uuid4()
            expires_at = now + timedelta(seconds=lease_seconds)
            task.admission_claim_token = token
            task.admission_claim_owner = owner
            task.admission_claim_expires_at = expires_at
            task.admission_intervention = None
            if task.admission_attempt_count is not None:
                task.admission_attempt_count += 1
            receipt = AdmissionClaim(task_id=task_id, owner=owner, token=token, expires_at=expires_at)
        return receipt

    def assert_claim(self, claim: AdmissionClaim) -> Task:
        """Lock the Task within caller's transaction and reject a stale claim."""
        if not self.session.in_transaction():
            raise RuntimeError("assert_claim requires an existing transaction")
        task = self.session.scalar(select(Task).where(Task.id == claim.task_id).with_for_update()
                                   .execution_options(populate_existing=True))
        now = self.session.scalar(select(func.clock_timestamp()))
        if (task is None or task.status != TaskStatus.PENDING or task.admission_claim_token != claim.token
                or task.admission_claim_owner != claim.owner or task.admission_claim_expires_at is None
                or task.admission_claim_expires_at <= now):
            raise AdmissionClaimDeniedError("Admission claim is not current")
        return task

    def release(self, claim: AdmissionClaim) -> None:
        with self.session.begin():
            task = self.assert_claim(claim)
            task.admission_claim_token = None
            task.admission_claim_owner = None
            task.admission_claim_expires_at = None
