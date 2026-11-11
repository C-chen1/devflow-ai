"""Link Artifacts to ToolCalls with matching AgentExecution ownership.

Revision ID: ab6e1c30f42d
Revises: f83ab2d16c49
"""

import sqlalchemy as sa
from alembic import op


revision = "ab6e1c30f42d"
down_revision = "f83ab2d16c49"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("tool_call_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_tool_calls_id_agent_execution", "tool_calls", ["id", "agent_execution_id"],
    )
    op.create_foreign_key(
        "fk_artifacts_tool_call_agent_execution",
        "artifacts",
        "tool_calls",
        ["tool_call_id", "agent_execution_id"],
        ["id", "agent_execution_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_artifacts_tool_call_agent_execution", "artifacts", type_="foreignkey")
    op.drop_constraint("uq_tool_calls_id_agent_execution", "tool_calls", type_="unique")
    op.drop_column("artifacts", "tool_call_id")
