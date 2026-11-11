"""Persist and query pre-Run human intervention notices."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devflow_ai.enums import TaskStatus
from devflow_ai.models import Task
from devflow_ai.policy import PolicyConfig


def record_reached_limits(task: Task, now: datetime, *, current_attempt_finished: bool = False) -> None:
    """Call with the Task locked; do not report an unfinished live attempt as exhausted."""
    if task.status != TaskStatus.PENDING:
        return
    if (not current_attempt_finished and task.admission_claim_token is not None
            and task.admission_claim_expires_at > now):
        return
    budget = PolicyConfig.model_validate(task.policy_snapshot).budget
    blocked = task.admission_blocked or {}
    reasons = []
    if (budget.max_admission_attempts is not None and task.admission_attempt_count is not None
            and task.admission_attempt_count >= budget.max_admission_attempts):
        reasons.append("ADMISSION_ATTEMPT_LIMIT")
    timeouts = blocked.get("consecutive_timeouts")
    if (blocked.get("retry_hint") == "transient" and type(timeouts) is int
            and budget.max_admission_consecutive_timeouts is not None
            and timeouts >= budget.max_admission_consecutive_timeouts):
        reasons.append("ADMISSION_TIMEOUT_LIMIT")
    if not reasons:
        return
    existing = task.admission_intervention or {}
    if existing.get("reason_codes") != reasons:
        task.admission_intervention = {
            "reason_codes": reasons, "recorded_at": now.isoformat(),
            "message": "准入自动尝试已达到上限，需要人工处理。",
            "attempt_count": task.admission_attempt_count,
            "consecutive_timeouts": timeouts if type(timeouts) is int else None,
        }


def get_admission_status(session: Session, task_id: UUID, *, skip_locked: bool = False) -> dict | None:
    """Refresh and return the notice after any current claim has finished or expired."""
    with session.begin():
        task = session.scalar(select(Task).where(Task.id == task_id).with_for_update(skip_locked=skip_locked)
                              .execution_options(populate_existing=True))
        if task is None:
            return None
        now = session.scalar(select(func.clock_timestamp()))
        record_reached_limits(task, now)
        result = {"task_id": task.id, "status": task.status, "intervention": task.admission_intervention,
                  "last_blocked": task.admission_blocked, "last_blocked_at": task.admission_blocked_at}
    return result
