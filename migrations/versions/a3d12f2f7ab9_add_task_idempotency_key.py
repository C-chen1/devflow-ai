"""Add a Task submission idempotency key.

Revision ID: a3d12f2f7ab9
Revises: 41ff63fe9d8a
"""
from alembic import op
import sqlalchemy as sa


revision = "a3d12f2f7ab9"
down_revision = "41ff63fe9d8a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("idempotency_key", sa.Uuid(), nullable=True))
    op.execute(sa.text("UPDATE tasks SET idempotency_key = gen_random_uuid() WHERE idempotency_key IS NULL"))
    op.alter_column("tasks", "idempotency_key", existing_type=sa.Uuid(), nullable=False)
    op.create_unique_constraint("uq_tasks_idempotency_key", "tasks", ["idempotency_key"])


def downgrade() -> None:
    op.drop_constraint("uq_tasks_idempotency_key", "tasks", type_="unique")
    op.drop_column("tasks", "idempotency_key")
