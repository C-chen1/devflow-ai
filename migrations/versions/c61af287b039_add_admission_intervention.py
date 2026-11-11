"""Persist the pre-Run human intervention notice on its Task."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c61af287b039"
down_revision = "b72d31e9c504"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("admission_intervention", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "admission_intervention")
