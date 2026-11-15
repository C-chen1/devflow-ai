"""Record the recovery action that actually started a Run.

Legacy recovery Runs keep NULL: their action cannot be reconstructed reliably, so
rediagnosis history stays unknown instead of being guessed.
"""
from alembic import op
import sqlalchemy as sa


revision = "e7a1c4d9b2f0"
down_revision = "c61af287b039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("recovery_action", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_runs_recovery_action",
        "runs",
        "recovery_action IS NULL OR recovery_action IN "
        "('REVISE_IMPLEMENTATION', 'REDIAGNOSE')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_recovery_action", "runs", type_="check")
    op.drop_column("runs", "recovery_action")
