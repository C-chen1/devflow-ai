"""create artifacts table

Revision ID: f83ab2d16c49
Revises: d4f63c192ba8
Create Date: 2026-11-04 20:02:16.344744

"""
import sqlalchemy as sa
from alembic import op


revision = "f83ab2d16c49"
down_revision = "d4f63c192ba8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_execution_id", sa.Uuid(), sa.ForeignKey("agent_executions.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("artifacts")
