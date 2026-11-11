"""Give each ToolCall a Run owner and allow Worker-owned calls.

Revision ID: e8c27b14a693
Revises: c3a71bd82ef0
"""

import sqlalchemy as sa
from alembic import op


revision = "e8c27b14a693"
down_revision = "c3a71bd82ef0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tool_calls", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE tool_calls AS tc
        SET run_id = ae.run_id
        FROM agent_executions AS ae
        WHERE tc.agent_execution_id = ae.id
    """)
    op.alter_column("tool_calls", "run_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key("fk_tool_calls_run_id", "tool_calls", "runs", ["run_id"], ["id"])
    op.create_unique_constraint(
        "uq_agent_executions_id_run", "agent_executions", ["id", "run_id"],
    )
    op.create_foreign_key(
        "fk_tool_calls_agent_execution_run",
        "tool_calls", "agent_executions", ["agent_execution_id", "run_id"], ["id", "run_id"],
    )
    op.alter_column("tool_calls", "agent_execution_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # The old schema cannot represent Worker-owned calls. Fail before dropping their ownership.
    op.alter_column("tool_calls", "agent_execution_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("fk_tool_calls_agent_execution_run", "tool_calls", type_="foreignkey")
    op.drop_constraint("uq_agent_executions_id_run", "agent_executions", type_="unique")
    op.drop_constraint("fk_tool_calls_run_id", "tool_calls", type_="foreignkey")
    op.drop_column("tool_calls", "run_id")
