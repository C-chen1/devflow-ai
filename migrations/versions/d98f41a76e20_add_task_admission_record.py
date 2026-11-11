"""Store Task admission reasoning without inventing records for legacy Tasks."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d98f41a76e20"
down_revision = "f154b6e82639"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("admission_record", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "admission_record")
