"""Create FailureRecords for completed failed Runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a43cb50f67e9"
down_revision = "97f3a25cb190"
branch_labels = None
depends_on = None


def upgrade() -> None:
    category = postgresql.ENUM(
        "TRANSIENT_EXECUTION_FAILURE", "TOOL_INVOCATION_FAILURE", "ENVIRONMENT_FAILURE",
        "EXPLORATION_FAILURE", "PLANNING_FAILURE", "IMPLEMENTATION_FAILURE",
        "AGENT_CONTRACT_FAILURE", "POLICY_VIOLATION", "UNKNOWN_FAILURE",
        name="failure_category", create_type=False,
    )
    category.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "failure_records",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", name="fk_failure_records_run_id"), nullable=False),
        sa.Column("category", category, nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("recovery_action", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("failure_records")
    postgresql.ENUM(name="failure_category").drop(op.get_bind())
