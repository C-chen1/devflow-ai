from datetime import datetime, timedelta
from re import fullmatch
from uuid import UUID
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session
from devflow_ai.enums import RunStatus, TaskStatus, ToolCallStatus
from devflow_ai.models import Run, Task, ToolCall


class TaskRepository:
    def __init__(self, session: Session):
        self.session = session

    def add(self, task: Task):
        self.session.add(task)
        self.session.flush()
        return task

    def get_by_id(self, task_id: UUID, *, populate_existing: bool = False) -> Task | None:
        return self.session.get(Task, task_id, populate_existing=populate_existing)

    def get_by_idempotency_key(self, idempotency_key: UUID) -> Task | None:
        statement = select(Task).where(Task.idempotency_key == idempotency_key)
        return self.session.scalar(statement)

    def get_by_id_for_update(self, task_id: UUID) -> Task | None:
        """Lock a specified Task; None can mean absent or currently locked."""
        statement = (
            select(Task)
            .where(Task.id == task_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement)

    def get_next_eligible_for_update(self) -> Task | None:
        """Lock one initial-run candidate; the caller owns the claim transaction."""
        statement = (
            select(Task)
            .where(Task.status == TaskStatus.ELIGIBLE)
            .order_by(Task.created_at, Task.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement)

    def update_admission_status(
        self, task_id: UUID, target_status: TaskStatus, *, base_commit: str | None = None,
        admission_record: dict | None = None, claim_token: UUID | None = None,
    ) -> int:
        """Apply a PENDING admission transition; the caller owns the transaction."""
        if target_status not in (TaskStatus.ELIGIBLE, TaskStatus.DECLINED):
            raise ValueError("Admission target status must be ELIGIBLE or DECLINED")
        if target_status == TaskStatus.ELIGIBLE:
            if not isinstance(base_commit, str) or not fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_commit):
                raise ValueError("ELIGIBLE requires a full lowercase hexadecimal base_commit")
        elif base_commit is not None:
            raise ValueError("DECLINED does not take a base_commit")
        claim_filter = (or_(Task.admission_claim_token.is_(None), Task.admission_claim_expires_at <= func.clock_timestamp())
                        if claim_token is None else
                        (Task.admission_claim_token == claim_token) & (Task.admission_claim_expires_at > func.clock_timestamp()))
        statement = (
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.PENDING, claim_filter)
            .values(status=target_status, base_commit=base_commit, admission_record=admission_record,
                    admission_claim_token=None, admission_claim_owner=None, admission_claim_expires_at=None,
                    admission_intervention=None)
        )
        result = self.session.execute(statement)
        return result.rowcount


class RunRepository:
    def __init__(self, session: Session):
        self.session = session

    def add(self, run: Run) -> Run:
        self.session.add(run)
        self.session.flush()
        return run

    def get_by_id_for_update(self, run_id: UUID) -> Run | None:
        """Wait for and refresh a Run's row lock; the caller owns the transaction."""
        statement = (
            select(Run)
            .where(Run.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement)

    def renew_lease_if_owned(
        self, run_id: UUID, worker_id: str, *, hard_deadline: datetime,
        lease_duration_seconds: int,
    ) -> tuple[datetime, datetime] | None:
        """Update an eligible lease using the database wall clock; never commit."""
        database_time = func.clock_timestamp()
        statement = (
            update(Run)
            .where(
                Run.id == run_id,
                Run.status == RunStatus.RUNNING,
                Run.worker_id == worker_id,
                Run.lease_expires_at > database_time,
                database_time < hard_deadline,
            )
            .values(
                last_heartbeat_at=database_time,
                lease_expires_at=func.least(
                    database_time + timedelta(seconds=lease_duration_seconds), hard_deadline,
                ),
            )
            .returning(Run.last_heartbeat_at, Run.lease_expires_at)
            .execution_options(synchronize_session=False)
        )
        row = self.session.execute(statement).one_or_none()
        return None if row is None else (row.last_heartbeat_at, row.lease_expires_at)

    def has_for_task(self, task_id: UUID) -> bool:
        statement = select(Run.id).where(Run.task_id == task_id).limit(1)
        return self.session.scalar(statement) is not None

    def get_by_creation_idempotency_key(
        self, task_id: UUID, creation_idempotency_key: str,
    ) -> Run | None:
        statement = (
            select(Run)
            .where(
                Run.task_id == task_id,
                Run.creation_idempotency_key == creation_idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement)


class ToolCallRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_requested(
        self, agent_execution_id: UUID | None, tool_name: str, args: dict, *, run_id: UUID,
    ) -> UUID:
        """Flush a request and return its ID within the caller's transaction."""
        call = ToolCall(
            run_id=run_id,
            agent_execution_id=agent_execution_id,
            tool_name=tool_name,
            args=args,
            status=ToolCallStatus.REQUESTED,
        )
        self.session.add(call)
        self.session.flush()
        return call.id

    def save_result(
        self, call_id: UUID, *, status: ToolCallStatus,
        stdout_excerpt: str | None = None, stderr_excerpt: str | None = None,
        exit_code: int | None = None, duration_ms: int | None = None,
        reason: str | None = None,
    ) -> int:
        """Save a normal tool outcome while the call is still pending."""
        status = ToolCallStatus(status)
        if status not in (
            ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED,
            ToolCallStatus.DENIED, ToolCallStatus.TIMED_OUT,
        ):
            raise ValueError("Unsupported tool result status")

        statement = (
            update(ToolCall)
            .where(
                ToolCall.id == call_id,
                ToolCall.status.in_((ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING)),
            )
            .values(
                status=status,
                stdout_excerpt=stdout_excerpt,
                stderr_excerpt=stderr_excerpt,
                exit_code=exit_code,
                duration_ms=duration_ms,
                reason=reason,
            )
        )
        return self.session.execute(statement).rowcount
