"""Link a receiving AgentExecution to its Handoff."""

from alembic import op
import sqlalchemy as sa


revision = "97f3a25cb190"
down_revision = "62eae84cebd1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_executions", sa.Column("handoff_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_agent_executions_handoff_id", "agent_executions", "handoffs", ["handoff_id"], ["id"],
    )
    op.create_unique_constraint("uq_agent_executions_handoff_id", "agent_executions", ["handoff_id"])


def downgrade() -> None:
    op.drop_constraint("uq_agent_executions_handoff_id", "agent_executions", type_="unique")
    op.drop_constraint("fk_agent_executions_handoff_id", "agent_executions", type_="foreignkey")
    op.drop_column("agent_executions", "handoff_id")
