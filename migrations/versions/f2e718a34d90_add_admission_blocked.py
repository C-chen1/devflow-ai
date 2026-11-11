"""Remember the most recent completed blocked admission attempt."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f2e718a34d90"
down_revision = "e54c6a9814d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("admission_blocked", postgresql.JSONB(), nullable=True))
    op.add_column("tasks", sa.Column("admission_blocked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "admission_blocked_at")
    op.drop_column("tasks", "admission_blocked")
