"""Track a pre-Run admission claim separately from Run leases."""
from alembic import op
import sqlalchemy as sa

revision = "a1d5c8b2e940"
down_revision = "f2e718a34d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("admission_claim_token", sa.Uuid(), nullable=True))
    op.add_column("tasks", sa.Column("admission_claim_owner", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("admission_claim_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_tasks_admission_claim_complete", "tasks",
        "(admission_claim_token IS NULL AND admission_claim_owner IS NULL AND admission_claim_expires_at IS NULL) OR "
        "(admission_claim_token IS NOT NULL AND admission_claim_owner IS NOT NULL AND admission_claim_expires_at IS NOT NULL)")


def downgrade() -> None:
    op.drop_constraint("ck_tasks_admission_claim_complete", "tasks", type_="check")
    op.drop_column("tasks", "admission_claim_expires_at")
    op.drop_column("tasks", "admission_claim_owner")
    op.drop_column("tasks", "admission_claim_token")
