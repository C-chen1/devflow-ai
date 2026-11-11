"""Pin the Task's Git base commit at admission.

Revision ID: c3a71bd82ef0
Revises: ab6e1c30f42d
"""

import sqlalchemy as sa
from alembic import op


revision = "c3a71bd82ef0"
down_revision = "ab6e1c30f42d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("base_commit", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_tasks_eligible_base_commit",
        "tasks",
        "status <> 'ELIGIBLE' OR (base_commit IS NOT NULL "
        "AND base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_eligible_base_commit", "tasks", type_="check")
    op.drop_column("tasks", "base_commit")
