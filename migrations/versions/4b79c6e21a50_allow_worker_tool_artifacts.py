"""Allow Artifacts to belong directly to a Worker ToolCall.

Revision ID: 4b79c6e21a50
Revises: e8c27b14a693
"""

import sqlalchemy as sa
from alembic import op


revision = "4b79c6e21a50"
down_revision = "e8c27b14a693"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key("fk_artifacts_tool_call_id", "artifacts", "tool_calls", ["tool_call_id"], ["id"])
    op.create_check_constraint(
        "ck_artifacts_has_owner", "artifacts",
        "agent_execution_id IS NOT NULL OR tool_call_id IS NOT NULL",
    )
    op.alter_column("artifacts", "agent_execution_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # The old schema requires an Agent owner. Refuse to downgrade if a tool-only Artifact exists.
    op.alter_column("artifacts", "agent_execution_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("ck_artifacts_has_owner", "artifacts", type_="check")
    op.drop_constraint("fk_artifacts_tool_call_id", "artifacts", type_="foreignkey")
