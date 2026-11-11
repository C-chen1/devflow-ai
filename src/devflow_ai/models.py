from datetime import datetime
from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column
from uuid import UUID
from sqlalchemy import CheckConstraint, Uuid, Text, Enum,DateTime,func,UniqueConstraint,ForeignKey,ForeignKeyConstraint,Integer,Index,text
from devflow_ai.enums import AgentExecutionStatus, FailureCategory, RunStatus, TaskStatus, ToolCallStatus, VerificationResult
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass

class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        CheckConstraint(
            "status <> 'ELIGIBLE' OR (base_commit IS NOT NULL "
            "AND base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$')",
            name="ck_tasks_eligible_base_commit",
        ),
        CheckConstraint(
            "(admission_claim_token IS NULL AND admission_claim_owner IS NULL AND admission_claim_expires_at IS NULL) OR "
            "(admission_claim_token IS NOT NULL AND admission_claim_owner IS NOT NULL AND admission_claim_expires_at IS NOT NULL)",
            name="ck_tasks_admission_claim_complete",
        ),
        CheckConstraint("admission_attempt_count IS NULL OR admission_attempt_count >= 0",
                        name="ck_tasks_admission_attempt_count_nonnegative"),
    )
    id : Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True,server_default = func.gen_random_uuid())
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title : Mapped[str] = mapped_column(Text, nullable=False)
    description : Mapped[str] = mapped_column(Text, nullable=False)
    repo : Mapped[str] = mapped_column(Text, nullable=False)
    base_commit: Mapped[str | None] = mapped_column(Text)
    admission_record: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    admission_usage: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    admission_blocked: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    admission_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admission_claim_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    admission_claim_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    admission_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admission_attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admission_intervention: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status : Mapped[TaskStatus] = mapped_column(Enum(TaskStatus,name ="task_status"),nullable=False,server_default= TaskStatus.PENDING.value)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_snapshot : Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    created_at : Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False,server_default=func.now())
    updated_at : Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False,server_default=func.now(),onupdate=func.now())


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_runs_task_attempt_number"),
        UniqueConstraint(
            "task_id", "creation_idempotency_key", name="uq_runs_task_creation_idempotency_key"
        ),
        Index(
            "uq_runs_one_running_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), nullable=False
    )
    creation_idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentExecution(Base):
    __tablename__ = "agent_executions"
    __table_args__ = (
        UniqueConstraint("id", "run_id", name="uq_agent_executions_id_run"),
        UniqueConstraint("handoff_id", name="uq_agent_executions_handoff_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    handoff_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("handoffs.id", name="fk_agent_executions_handoff_id"), nullable=True,
    )
    agent_role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[AgentExecutionStatus] = mapped_column(
        Enum(
            AgentExecutionStatus, name="agent_execution_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
    )
    input: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    llm_usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("id", "agent_execution_id", name="uq_tool_calls_id_agent_execution"),
        ForeignKeyConstraint(
            ["agent_execution_id", "run_id"],
            ["agent_executions.id", "agent_executions.run_id"],
            name="fk_tool_calls_agent_execution_run",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", name="fk_tool_calls_run_id"), nullable=False
    )
    agent_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_executions.id"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        Enum(
            ToolCallStatus, name="tool_call_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
    )
    stdout_excerpt: Mapped[str | None] = mapped_column(Text)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "agent_execution_id IS NOT NULL OR tool_call_id IS NOT NULL",
            name="ck_artifacts_has_owner",
        ),
        ForeignKeyConstraint(
            ["tool_call_id", "agent_execution_id"],
            ["tool_calls.id", "tool_calls.agent_execution_id"],
            name="fk_artifacts_tool_call_agent_execution",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_executions.id"), nullable=True
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tool_calls.id", name="fk_artifacts_tool_call_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Verification(Base):
    __tablename__ = "verifications"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", name="fk_verifications_run_id"), nullable=False)
    result: Mapped[VerificationResult] = mapped_column(
        Enum(VerificationResult, name="verification_result", values_callable=lambda values: [v.value for v in values]),
        nullable=False,
    )
    evidence: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FailureRecord(Base):
    __tablename__ = "failure_records"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", name="fk_failure_records_run_id"), nullable=False)
    category: Mapped[FailureCategory] = mapped_column(
        Enum(FailureCategory, name="failure_category"), nullable=False,
    )
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    recovery_action: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    from_agent_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_executions.id", name="fk_handoffs_from_agent_execution_id"), nullable=False
    )
    to_agent_role: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
