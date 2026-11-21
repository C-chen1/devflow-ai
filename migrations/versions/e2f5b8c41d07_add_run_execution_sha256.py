"""Record the execution manifest digest a Run was started from.

The column is nullable on purpose: Runs created before this migration, and Runs started by an
entry that was given no manifest, keep NULL — "unknown", never a guessed value. History is not
backfilled: the digest has to be earned by verifying the artifacts at start time.
"""
from alembic import op
import sqlalchemy as sa

revision = "e2f5b8c41d07"
down_revision = "d9b3c7e1a250"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("execution_sha256", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_runs_execution_sha256", "runs",
        "execution_sha256 IS NULL OR execution_sha256 ~ '^[0-9a-f]{64}$'")


def downgrade() -> None:
    op.drop_constraint("ck_runs_execution_sha256", "runs", type_="check")
    op.drop_column("runs", "execution_sha256")
