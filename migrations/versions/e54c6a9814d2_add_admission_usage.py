"""Track pre-Run admission model reservations on each Task."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e54c6a9814d2"
down_revision = "d98f41a76e20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("admission_usage", postgresql.JSONB(), nullable=False,
                                      server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    op.drop_column("tasks", "admission_usage")
