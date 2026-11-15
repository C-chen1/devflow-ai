"""Add the append-only task review table.

A review is evidence: it names the Task, the Run, the formal Verification and the run
record (by digest), and it is never rewritten. ``reviewed_at`` comes from the human's own
record; ``created_at`` is the database's timestamp for when the row was written.
"""
from alembic import op
import sqlalchemy as sa


revision = "b7f2c31d8a90"
down_revision = "f3b8d2a6c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_reviews",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("verification_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("record_sha256", sa.Text(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_task_reviews_task_id"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_task_reviews_run_id"),
        sa.ForeignKeyConstraint(
            ["verification_id"], ["verifications.id"], name="fk_task_reviews_verification_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "record_sha256", name="uq_task_reviews_task_record"),
        sa.CheckConstraint("conclusion IN ('accepted', 'rejected')", name="ck_task_reviews_conclusion"),
        sa.CheckConstraint("record_sha256 ~ '^[0-9a-f]{64}$'", name="ck_task_reviews_record_sha256"),
    )
    op.create_index("ix_task_reviews_task_id", "task_reviews", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_reviews_task_id", table_name="task_reviews")
    op.drop_table("task_reviews")
