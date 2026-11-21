from datetime import datetime
from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Uuid, Text, Enum,DateTime,func,UniqueConstraint,ForeignKey,ForeignKeyConstraint,Integer,Index,text
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
        CheckConstraint(
            "recovery_action IS NULL OR recovery_action IN "
            "('REVISE_IMPLEMENTATION', 'REDIAGNOSE')",
            name="ck_runs_recovery_action",
        ),
        CheckConstraint(
            "recovery_start_tracking IS NULL OR recovery_start_tracking = 'v1'",
            name="ck_runs_recovery_start_tracking",
        ),
        CheckConstraint(
            "recovery_started_at IS NULL OR recovery_start_tracking IS NOT NULL",
            name="ck_runs_recovery_start_recorded",
        ),
        CheckConstraint(
            "execution_sha256 IS NULL OR execution_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_runs_execution_sha256",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL on initial Runs and on legacy recovery Runs created before the marker existed.
    recovery_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The durable event for an actually started recovery Run. NULL means either
    # not started yet (tracking v1) or an untracked legacy Run (tracking NULL).
    recovery_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_start_tracking: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The execution manifest digest this Run was started from, written by the entry that
    # verified it inside the claim transaction. NULL means unknown: either a Run from before
    # the column existed or one started without a manifest. It is never backfilled.
    execution_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class TaskReview(Base):
    """One human review of a Task's formally passed Run.

    Append-only: a review is evidence, so it is never rewritten. The record it points at is
    identified by digest, which is also what makes a repeated submission idempotent. Both
    times are kept on purpose: ``reviewed_at`` is the human's own timestamp from the record,
    ``created_at`` is when this row was written, taken from the database.
    """

    __tablename__ = "task_reviews"
    __table_args__ = (
        UniqueConstraint("task_id", "record_sha256", name="uq_task_reviews_task_record"),
        CheckConstraint("conclusion IN ('accepted', 'rejected')", name="ck_task_reviews_conclusion"),
        CheckConstraint("record_sha256 ~ '^[0-9a-f]{64}$'", name="ck_task_reviews_record_sha256"),
        Index("ix_task_reviews_task_id", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", name="fk_task_reviews_task_id"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", name="fk_task_reviews_run_id"), nullable=False)
    verification_id: Mapped[UUID] = mapped_column(
        ForeignKey("verifications.id", name="fk_task_reviews_verification_id"), nullable=False)
    record_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


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


class MemoryItem(Base):
    """One explicit memory candidate and the formal record it points at.

    Two different things are kept apart on purpose:

    * ``source_state`` says what the *cited records* establish — that a trusted Verification
      passed, or that a Task review accepted the Run. It is checked from the rows, never taken
      from the caller;
    * ``content_status`` says what is known about the *statement itself*. This version has no
      mechanism that checks a free-text statement, so it is always ``unverified_candidate``:
      the row records a candidate memory, not a verified fact and not something a human
      reviewed. An accepted Task review accepted the Run's patch, not this sentence.

    ``identity_key`` is the caller's stable name for the memory and is unique, so repeating a
    submission cannot create a second row. ``scope_kind``/``scope_value`` state where the
    memory claims to apply and are checked against the source Task before the row is written.
    Retirement keeps the row and its references.
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_memory_items_identity_key"),
        CheckConstraint("char_length(btrim(identity_key)) > 0",
                        name="ck_memory_items_identity_key"),
        CheckConstraint("scope_kind IN ('repository')", name="ck_memory_items_scope_kind"),
        CheckConstraint("char_length(btrim(scope_value)) > 0",
                        name="ck_memory_items_scope_value"),
        CheckConstraint(
            "source_kind IN ('verification', 'task_review')",
            name="ck_memory_items_source_kind",
        ),
        CheckConstraint(
            "source_state IN ('trusted_passed', 'review_accepted')",
            name="ck_memory_items_source_state",
        ),
        CheckConstraint(
            "(source_kind = 'task_review') = (task_review_id IS NOT NULL)",
            name="ck_memory_items_review_binding",
        ),
        CheckConstraint(
            "source_kind = 'verification' OR source_state = 'review_accepted'",
            name="ck_memory_items_review_state",
        ),
        CheckConstraint(
            "source_kind = 'task_review' OR source_state = 'trusted_passed'",
            name="ck_memory_items_verification_state",
        ),
        CheckConstraint(
            "content_status = 'unverified_candidate'",
            name="ck_memory_items_content_status",
        ),
        CheckConstraint(
            "(artifact_id IS NULL) = (artifact_sha256 IS NULL)",
            name="ck_memory_items_artifact_binding",
        ),
        CheckConstraint(
            "artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_memory_items_artifact_sha256",
        ),
        CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 4000", name="ck_memory_items_content",
        ),
        CheckConstraint("(topic IS NULL) = (valid_until IS NULL)", name="ck_memory_items_topic_expiry"),
        CheckConstraint("(applicable_base_commit IS NULL) = (applicable_environment IS NULL)",
                        name="ck_memory_items_applicability_pair"),
        CheckConstraint("applicable_base_commit IS NULL OR applicable_base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
                        name="ck_memory_items_applicability_commit"),
        CheckConstraint("applicable_environment IS NULL OR (applicable_environment = btrim(applicable_environment) "
                        "AND char_length(applicable_environment) BETWEEN 1 AND 128)",
                        name="ck_memory_items_applicability_environment"),
        CheckConstraint("topic IS NULL OR (topic = btrim(topic) AND char_length(topic) BETWEEN 1 AND 100)",
                        name="ck_memory_items_topic"),
        CheckConstraint("char_length(btrim(created_by)) > 0",
                        name="ck_memory_items_created_by"),
        CheckConstraint("status IN ('active', 'retired')", name="ck_memory_items_status"),
        CheckConstraint(
            "(status = 'retired') = (retired_at IS NOT NULL)",
            name="ck_memory_items_retirement",
        ),
        CheckConstraint(
            "(status = 'retired') = (retired_by IS NOT NULL)",
            name="ck_memory_items_retired_by",
        ),
        CheckConstraint(
            "(status = 'retired') = (retire_reason IS NOT NULL)",
            name="ck_memory_items_retire_reason",
        ),
        Index("ix_memory_items_scope", "scope_kind", "scope_value"),
        Index("ix_memory_items_task_id", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_value: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # What the cited records establish; derived from them, never declared by the caller.
    source_state: Mapped[str] = mapped_column(Text, nullable=False)
    # What is known about the statement itself: always unverified in this version.
    content_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unverified_candidate'"))
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", name="fk_memory_items_task_id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", name="fk_memory_items_run_id"), nullable=False
    )
    verification_id: Mapped[UUID] = mapped_column(
        ForeignKey("verifications.id", name="fk_memory_items_verification_id"), nullable=False
    )
    task_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_reviews.id", name="fk_memory_items_task_review_id"), nullable=True
    )
    artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifacts.id", name="fk_memory_items_artifact_id"), nullable=True
    )
    artifact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applicable_base_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicable_environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    retire_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemoryReview(Base):
    """One operator-declared review of a specific immutable candidate snapshot."""
    __tablename__ = "memory_reviews"
    __table_args__ = (
        UniqueConstraint("memory_id", name="uq_memory_reviews_memory_id"),
        CheckConstraint("conclusion IN ('accepted', 'rejected')", name="ck_memory_reviews_conclusion"),
        CheckConstraint("candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_memory_reviews_sha256"),
        CheckConstraint("char_length(btrim(reviewer)) > 0", name="ck_memory_reviews_reviewer"),
        CheckConstraint("char_length(btrim(reason)) BETWEEN 1 AND 4000", name="ck_memory_reviews_reason"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True,
                                    server_default=func.gen_random_uuid())
    memory_id: Mapped[UUID] = mapped_column(ForeignKey("memory_items.id"), nullable=False)
    candidate_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())


class MemoryConflict(Base):
    """An operator-declared conflict between two specific candidate snapshots."""
    __tablename__ = "memory_conflicts"
    __table_args__ = (
        UniqueConstraint("first_id", "second_id", name="uq_memory_conflicts_pair"),
        CheckConstraint("first_id < second_id", name="ck_memory_conflicts_order"),
        CheckConstraint("first_sha256 ~ '^[0-9a-f]{64}$' AND second_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_memory_conflicts_digests"),
        CheckConstraint("length(btrim(reporter)) > 0 AND length(btrim(reason)) BETWEEN 1 AND 4000",
                        name="ck_memory_conflicts_declaration"),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_memory_conflicts_status"),
        CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL) AND "
                        "(status = 'resolved') = (resolved_by IS NOT NULL) AND "
                        "(status = 'resolved') = (resolution IS NOT NULL)", name="ck_memory_conflicts_resolution"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    first_id: Mapped[UUID] = mapped_column(ForeignKey("memory_items.id"), nullable=False)
    second_id: Mapped[UUID] = mapped_column(ForeignKey("memory_items.id"), nullable=False)
    first_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    second_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    reporter: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)


class MemoryRevision(Base):
    """A declared replacement relationship between two candidate snapshots."""
    __tablename__ = "memory_revisions"
    __table_args__ = (
        UniqueConstraint("previous_id", name="uq_memory_revisions_previous"),
        UniqueConstraint("next_id", name="uq_memory_revisions_next"),
        CheckConstraint("previous_id <> next_id", name="ck_memory_revisions_distinct"),
        CheckConstraint("previous_sha256 ~ '^[0-9a-f]{64}$' AND next_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_memory_revisions_digests"),
        CheckConstraint("length(btrim(operator)) > 0 AND length(btrim(reason)) BETWEEN 1 AND 4000",
                        name="ck_memory_revisions_declaration"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    previous_id: Mapped[UUID] = mapped_column(ForeignKey("memory_items.id"), nullable=False)
    next_id: Mapped[UUID] = mapped_column(ForeignKey("memory_items.id"), nullable=False)
    previous_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    next_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MemoryUse(Base):
    """An operator-declared use, not proof of model context or evaluation independence."""
    __tablename__ = "memory_uses"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_memory_uses_idempotency"),
        CheckConstraint("candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_memory_uses_digest"),
        CheckConstraint("(target_base_commit IS NULL) = (target_environment IS NULL)",
                        name="ck_memory_uses_applicability_pair"),
        CheckConstraint("target_base_commit IS NULL OR target_base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
                        name="ck_memory_uses_base_commit"),
        CheckConstraint("target_environment IS NULL OR (target_environment = btrim(target_environment) "
                        "AND char_length(target_environment) BETWEEN 1 AND 128)",
                        name="ck_memory_uses_environment"),
        CheckConstraint("length(btrim(operator)) > 0 AND length(btrim(purpose)) BETWEEN 1 AND 1000",
                        name="ck_memory_uses_declaration"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    memory_id: Mapped[UUID] = mapped_column(ForeignKey("memory_items.id"), nullable=False)
    review_id: Mapped[UUID] = mapped_column(ForeignKey("memory_reviews.id"), nullable=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    candidate_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    target_base_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Experiment(Base):
    """Trusted-side limits for one controlled RSI experiment.

    This row is the thing a later promotion is measured against, so the limits are frozen at
    open time: the service never updates them. It records declared intent and accounting, not
    evidence that any candidate is better.
    """
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_experiments_identity_key"),
        CheckConstraint("char_length(btrim(identity_key)) > 0",
                        name="ck_experiments_identity_key"),
        CheckConstraint("base_execution_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_experiments_base_execution_sha256"),
        CheckConstraint("max_candidates BETWEEN 1 AND 100", name="ck_experiments_max_candidates"),
        CheckConstraint("max_total_model_tokens > 0", name="ck_experiments_max_total_model_tokens"),
        CheckConstraint("max_total_seconds BETWEEN 1 AND 2592000",
                        name="ck_experiments_max_total_seconds"),
        CheckConstraint("promotion_min_formal_passes >= 1",
                        name="ck_experiments_promotion_min_formal_passes"),
        CheckConstraint("promotion_max_failures >= 0",
                        name="ck_experiments_promotion_max_failures"),
        CheckConstraint("reserved_tokens >= 0", name="ck_experiments_reserved_tokens"),
        CheckConstraint("settled_tokens >= 0", name="ck_experiments_settled_tokens"),
        CheckConstraint("settled_tokens <= max_total_model_tokens OR over_budget",
                        name="ck_experiments_settled_within_budget"),
        CheckConstraint("char_length(btrim(created_by)) > 0", name="ck_experiments_created_by"),
        CheckConstraint("deadline_at > created_at", name="ck_experiments_deadline"),
        CheckConstraint("(closed_at IS NULL) = (closed_reason IS NULL)",
                        name="ck_experiments_closure"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    base_execution_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    max_candidates: Mapped[int] = mapped_column(Integer, nullable=False)
    max_total_model_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_total_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    promotion_min_formal_passes: Mapped[int] = mapped_column(Integer, nullable=False)
    promotion_max_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    settled_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    over_budget: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExperimentTask(Base):
    """The declared task set, with the target baseline each Task pinned at open time."""
    __tablename__ = "experiment_tasks"
    __table_args__ = (
        UniqueConstraint("experiment_id", "task_id", name="uq_experiment_tasks_task"),
        CheckConstraint("base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
                        name="ck_experiment_tasks_base_commit"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", name="fk_experiment_tasks_experiment_id"), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    base_commit: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExperimentRecord(Base):
    """A run record the trusted control plane fixed for one attempt (one Run).

    Only the digest is stored: the file stays outside the database and is never copied. The
    ledger compares the bytes it reads against the registration of *that Run*, so "this is the
    record the control plane fixed for this attempt" becomes checkable — while the record's
    *author* still is not proven. One Run can have at most one fixed record, and one digest
    cannot be fixed for two Runs of the same experiment.
    """
    __tablename__ = "experiment_records"
    __table_args__ = (
        UniqueConstraint("experiment_id", "run_id", name="uq_experiment_records_run"),
        UniqueConstraint("experiment_id", "record_sha256", name="uq_experiment_records_digest"),
        CheckConstraint("record_sha256 ~ '^[0-9a-f]{64}$'", name="ck_experiment_records_sha256"),
        CheckConstraint("char_length(btrim(registered_by)) > 0",
                        name="ck_experiment_records_registered_by"),
        Index("ix_experiment_records_candidate", "experiment_id", "candidate_id", "task_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", name="fk_experiment_records_experiment_id"), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiment_candidates.id", name="fk_experiment_records_candidate_id"),
        nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    record_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    registered_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExperimentCandidate(Base):
    """One candidate execution version inside an experiment."""
    __tablename__ = "experiment_candidates"
    __table_args__ = (
        UniqueConstraint("experiment_id", "label", name="uq_experiment_candidates_label"),
        CheckConstraint("char_length(btrim(label)) BETWEEN 1 AND 200",
                        name="ck_experiment_candidates_label"),
        CheckConstraint("execution_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_experiment_candidates_execution_sha256"),
        CheckConstraint("state IN ('prepared', 'concluded')", name="ck_experiment_candidates_state"),
        CheckConstraint("outcome IS NULL OR outcome IN ('accepted', 'rejected', 'abandoned')",
                        name="ck_experiment_candidates_outcome"),
        CheckConstraint("formal_passes >= 0", name="ck_experiment_candidates_formal_passes"),
        CheckConstraint("non_formal_results >= 0", name="ck_experiment_candidates_non_formal_results"),
        CheckConstraint("(state = 'concluded') = (outcome IS NOT NULL)",
                        name="ck_experiment_candidates_concluded_outcome"),
        CheckConstraint("(outcome IS NULL) = (reason IS NULL)",
                        name="ck_experiment_candidates_reason"),
        CheckConstraint("(outcome IS NULL) = (decided_by IS NULL)",
                        name="ck_experiment_candidates_decided_by"),
        CheckConstraint("(state = 'concluded') = (concluded_at IS NOT NULL)",
                        name="ck_experiment_candidates_concluded_at"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id",
                        name="ck_experiment_candidates_parent"),
        Index("ix_experiment_candidates_experiment_id", "experiment_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", name="fk_experiment_candidates_experiment_id"), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    execution_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("experiment_candidates.id", name="fk_experiment_candidates_parent_id"), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'prepared'"))
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    formal_passes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    non_formal_results: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentReservation(Base):
    """A model-token reservation, later settled with the real amount."""
    __tablename__ = "experiment_reservations"
    __table_args__ = (
        UniqueConstraint("experiment_id", "request_key", name="uq_experiment_reservations_request"),
        CheckConstraint("tokens > 0", name="ck_experiment_reservations_tokens"),
        CheckConstraint("state IN ('reserved', 'settled', 'released', 'unknown')",
                        name="ck_experiment_reservations_state"),
        CheckConstraint("actual_tokens IS NULL OR actual_tokens >= 0",
                        name="ck_experiment_reservations_actual_tokens"),
        CheckConstraint("(state = 'settled') = (actual_tokens IS NOT NULL)",
                        name="ck_experiment_reservations_settled_tokens"),
        CheckConstraint("(state = 'settled') = (settled_at IS NOT NULL)",
                        name="ck_experiment_reservations_settled_at"),
        CheckConstraint("stage IN ('reserved', 'dispatched')",
                        name="ck_experiment_reservations_stage"),
        CheckConstraint("(stage = 'dispatched') = (dispatched_at IS NOT NULL)",
                        name="ck_experiment_reservations_dispatched_at"),
        CheckConstraint("(state IN ('released', 'unknown')) = (ended_at IS NOT NULL)",
                        name="ck_experiment_reservations_ended_at"),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)",
                        name="ck_experiment_reservations_ended_by"),
        CheckConstraint("(ended_at IS NULL) = (ended_reason IS NULL)",
                        name="ck_experiment_reservations_ended_reason"),
        CheckConstraint("ended_by IS NULL OR char_length(btrim(ended_by)) > 0",
                        name="ck_experiment_reservations_ended_by_text"),
        CheckConstraint("ended_reason IS NULL OR char_length(btrim(ended_reason)) BETWEEN 1 AND 1000",
                        name="ck_experiment_reservations_ended_reason_text"),
        Index("ix_experiment_reservations_experiment_id", "experiment_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", name="fk_experiment_reservations_experiment_id"), nullable=False)
    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("experiment_candidates.id", name="fk_experiment_reservations_candidate_id"),
        nullable=True)
    request_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    agent_execution_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'reserved'"))
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'reserved'"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentTaskResult(Base):
    """One attempt's recorded verdict. Unique per (candidate, task, Run) and per Run."""
    __tablename__ = "experiment_task_results"
    __table_args__ = (
        UniqueConstraint("candidate_id", "task_id", "run_id", name="uq_experiment_task_results_attempt"),
        # One Run belongs to at most one candidate: an attempt may not be counted twice.
        UniqueConstraint("experiment_id", "run_id", name="uq_experiment_task_results_run"),
        CheckConstraint(
            "outcome IN ('formal_pass', 'failed', 'public_only_pass', 'incomplete', 'timeout')",
            name="ck_experiment_task_results_outcome"),
        CheckConstraint("char_length(btrim(recorded_by)) > 0",
                        name="ck_experiment_task_results_recorded_by"),
        CheckConstraint("evidence_source IN ('declared', 'classified')",
                        name="ck_experiment_task_results_evidence_source"),
        CheckConstraint("decision_reason IS NULL OR char_length(decision_reason) <= 1000",
                        name="ck_experiment_task_results_decision_reason"),
        CheckConstraint("verification_id IS NULL OR evidence_source = 'classified'",
                        name="ck_experiment_task_results_verification_source"),
        CheckConstraint(
            "(evidence_source = 'classified' AND outcome = 'formal_pass') "
            "= (verification_id IS NOT NULL)",
            name="ck_experiment_task_results_formal_verification"),
        CheckConstraint("(evidence_source = 'classified') = (deadline_context IS NOT NULL)",
                        name="ck_experiment_task_results_deadline_context"),
        CheckConstraint(
            "deadline_context IS NULL OR deadline_context IN ('fired', 'not_fired', 'unknown')",
            name="ck_experiment_task_results_deadline_context_value"),
        CheckConstraint("(evidence_source = 'classified') = (context_source IS NOT NULL)",
                        name="ck_experiment_task_results_context_source"),
        CheckConstraint(
            "context_source IS NULL OR char_length(btrim(context_source)) BETWEEN 1 AND 200",
            name="ck_experiment_task_results_context_source_text"),
        CheckConstraint("context_evidence IN ('record', 'declared')",
                        name="ck_experiment_task_results_context_evidence"),
        CheckConstraint("record_sha256 IS NULL OR record_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_experiment_task_results_record_sha256"),
        CheckConstraint("(context_evidence = 'record') = (record_sha256 IS NOT NULL)",
                        name="ck_experiment_task_results_record_binding"),
        CheckConstraint(
            "attempt_kind IS NULL OR attempt_kind IN ('formal', 'rehearsal', 'unknown')",
            name="ck_experiment_task_results_attempt_kind"),
        CheckConstraint("(context_evidence = 'record') = (attempt_kind IS NOT NULL)",
                        name="ck_experiment_task_results_attempt_kind_binding"),
        CheckConstraint(
            "record_provenance IS NULL "
            "OR record_provenance IN ('caller_supplied', 'control_plane_registered')",
            name="ck_experiment_task_results_record_provenance"),
        CheckConstraint("(context_evidence = 'record') = (record_provenance IS NOT NULL)",
                        name="ck_experiment_task_results_record_provenance_binding"),
        CheckConstraint(
            "run_execution_sha256 IS NULL OR run_execution_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_experiment_task_results_run_execution_sha256"),
        CheckConstraint(
            "workspace_base_commit IS NULL "
            "OR workspace_base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'",
            name="ck_experiment_task_results_workspace_base_commit"),
        Index("ix_experiment_task_results_candidate_id", "candidate_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", name="fk_experiment_task_results_experiment_id"), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiment_candidates.id", name="fk_experiment_task_results_candidate_id"),
        nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'declared'"))
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    deadline_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_evidence: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'declared'"))
    record_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    record_provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_execution_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_base_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
