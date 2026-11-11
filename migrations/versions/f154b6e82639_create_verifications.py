"""Create Verification evidence records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f154b6e82639"
down_revision = "a43cb50f67e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    result = postgresql.ENUM("passed", "failed", name="verification_result", create_type=False)
    result.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "verifications",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", name="fk_verifications_run_id"), nullable=False),
        sa.Column("result", result, nullable=False),
        sa.Column("evidence", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("verifications")
    postgresql.ENUM(name="verification_result").drop(op.get_bind())
