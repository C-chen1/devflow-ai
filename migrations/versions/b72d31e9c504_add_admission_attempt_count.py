"""Track newly recorded pre-Run attempts without inventing legacy history."""
from alembic import op
import sqlalchemy as sa

revision = "b72d31e9c504"
down_revision = "a1d5c8b2e940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("admission_attempt_count", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_tasks_admission_attempt_count_nonnegative", "tasks",
                               "admission_attempt_count IS NULL OR admission_attempt_count >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_tasks_admission_attempt_count_nonnegative", "tasks", type_="check")
    op.drop_column("tasks", "admission_attempt_count")
