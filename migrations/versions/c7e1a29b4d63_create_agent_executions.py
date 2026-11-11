"""Create agent_executions.

Revision ID: c7e1a29b4d63
Revises: b312290a866d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c7e1a29b4d63"
down_revision = "b312290a866d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status_type = postgresql.ENUM(
        "running", "succeeded", "failed", name="agent_execution_status", create_type=False,
    )
    status_type.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "agent_executions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("status", status_type, nullable=False),
        sa.Column("input", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("output", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("llm_usage", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("agent_executions")
    postgresql.ENUM(name="agent_execution_status").drop(op.get_bind())
