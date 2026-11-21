"""Add the explicit memory-item table.

A memory item is one statement plus the formal record it points at: the Task, the Run, the
trusted Verification and, for a reviewed memory, the Task review. It is deliberately not a
second source of truth for any of them and this migration touches no existing table.

Two independent facts are stored. ``source_state`` records what the cited rows establish
(``trusted_passed`` / ``review_accepted``) and is derived from them. ``content_status`` records
what is known about the statement itself; this version has no way to check free text, so a
check constraint pins it to ``unverified_candidate`` — the row is a candidate memory, not a
verified fact and not something a human reviewed.

``identity_key`` is unique, so a repeated submission cannot create a second row. ``scope_kind``
is limited to ``repository`` because that is the only applicability this version can check
against the source Task's own repository. The retirement columns are all-or-nothing.
"""
from alembic import op
import sqlalchemy as sa


revision = "c4a7e2f19b30"
down_revision = "b7f2c31d8a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_value", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_state", sa.Text(), nullable=False),
        sa.Column("content_status", sa.Text(),
                  server_default=sa.text("'unverified_candidate'"), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("verification_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_review_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("artifact_sha256", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by", sa.Text(), nullable=True),
        sa.Column("retire_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_memory_items_task_id"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_memory_items_run_id"),
        sa.ForeignKeyConstraint(
            ["verification_id"], ["verifications.id"],
            name="fk_memory_items_verification_id"),
        sa.ForeignKeyConstraint(
            ["task_review_id"], ["task_reviews.id"],
            name="fk_memory_items_task_review_id"),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], name="fk_memory_items_artifact_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_memory_items_identity_key"),
        sa.CheckConstraint("char_length(btrim(identity_key)) > 0",
                           name="ck_memory_items_identity_key"),
        sa.CheckConstraint("scope_kind IN ('repository')", name="ck_memory_items_scope_kind"),
        sa.CheckConstraint("char_length(btrim(scope_value)) > 0",
                           name="ck_memory_items_scope_value"),
        sa.CheckConstraint(
            "source_kind IN ('verification', 'task_review')",
            name="ck_memory_items_source_kind"),
        sa.CheckConstraint(
            "source_state IN ('trusted_passed', 'review_accepted')",
            name="ck_memory_items_source_state"),
        sa.CheckConstraint(
            "(source_kind = 'task_review') = (task_review_id IS NOT NULL)",
            name="ck_memory_items_review_binding"),
        sa.CheckConstraint(
            "source_kind = 'verification' OR source_state = 'review_accepted'",
            name="ck_memory_items_review_state"),
        sa.CheckConstraint(
            "source_kind = 'task_review' OR source_state = 'trusted_passed'",
            name="ck_memory_items_verification_state"),
        sa.CheckConstraint(
            "content_status = 'unverified_candidate'",
            name="ck_memory_items_content_status"),
        sa.CheckConstraint(
            "(artifact_id IS NULL) = (artifact_sha256 IS NULL)",
            name="ck_memory_items_artifact_binding"),
        sa.CheckConstraint(
            "artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_memory_items_artifact_sha256"),
        sa.CheckConstraint(
            "char_length(btrim(content)) BETWEEN 1 AND 4000", name="ck_memory_items_content"),
        sa.CheckConstraint("(topic IS NULL) = (valid_until IS NULL)",
                           name="ck_memory_items_topic_expiry"),
        sa.CheckConstraint("topic IS NULL OR (topic = btrim(topic) AND char_length(topic) BETWEEN 1 AND 100)",
                           name="ck_memory_items_topic"),
        sa.CheckConstraint("char_length(btrim(created_by)) > 0",
                           name="ck_memory_items_created_by"),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_memory_items_status"),
        sa.CheckConstraint(
            "(status = 'retired') = (retired_at IS NOT NULL)",
            name="ck_memory_items_retirement"),
        sa.CheckConstraint(
            "(status = 'retired') = (retired_by IS NOT NULL)",
            name="ck_memory_items_retired_by"),
        sa.CheckConstraint(
            "(status = 'retired') = (retire_reason IS NOT NULL)",
            name="ck_memory_items_retire_reason"),
    )
    op.create_index("ix_memory_items_scope", "memory_items", ["scope_kind", "scope_value"])
    op.create_index("ix_memory_items_task_id", "memory_items", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_items_task_id", table_name="memory_items")
    op.drop_index("ix_memory_items_scope", table_name="memory_items")
    op.drop_table("memory_items")
