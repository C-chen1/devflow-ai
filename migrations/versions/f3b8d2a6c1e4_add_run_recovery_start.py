"""Record the actual start of the first recovery node, not just Run creation.

Legacy Runs keep both columns NULL. A REDIAGNOSE Run with no tracking marker
cannot prove whether its first node started, so rediagnosis history stays unknown.
"""
from alembic import op
import sqlalchemy as sa


revision = "f3b8d2a6c1e4"
down_revision = "e7a1c4d9b2f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("recovery_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("recovery_start_tracking", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_runs_recovery_start_tracking",
        "runs",
        "recovery_start_tracking IS NULL OR recovery_start_tracking = 'v1'",
    )
    op.create_check_constraint(
        "ck_runs_recovery_start_recorded",
        "runs",
        "recovery_started_at IS NULL OR recovery_start_tracking IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_recovery_start_recorded", "runs", type_="check")
    op.drop_constraint("ck_runs_recovery_start_tracking", "runs", type_="check")
    op.drop_column("runs", "recovery_start_tracking")
    op.drop_column("runs", "recovery_started_at")
