"""Create handoffs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "62eae84cebd1"
down_revision = "4b79c6e21a50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "from_agent_execution_id", sa.Uuid(),
            sa.ForeignKey("agent_executions.id", name="fk_handoffs_from_agent_execution_id"),
            nullable=False,
        ),
        sa.Column("to_agent_role", sa.Text(), nullable=False),
        sa.Column("context", JSONB(none_as_null=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("handoffs")
