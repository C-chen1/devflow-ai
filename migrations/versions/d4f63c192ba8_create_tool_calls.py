"""Create tool_calls.

Revision ID: d4f63c192ba8
Revises: c7e1a29b4d63
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4f63c192ba8"
down_revision = "c7e1a29b4d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status_type = postgresql.ENUM(
        "requested", "running", "succeeded", "failed", "denied", "timed_out",
        "unknown_result", name="tool_call_status", create_type=False,
    )
    status_type.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_execution_id", sa.Uuid(), sa.ForeignKey("agent_executions.id"), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("status", status_type, nullable=False),
        sa.Column("stdout_excerpt", sa.Text()),
        sa.Column("stderr_excerpt", sa.Text()),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("tool_calls")
    postgresql.ENUM(name="tool_call_status").drop(op.get_bind())
