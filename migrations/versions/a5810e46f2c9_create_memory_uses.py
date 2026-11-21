"""Add operator-declared memory use records."""
from alembic import op
import sqlalchemy as sa

revision = "a5810e46f2c9"
down_revision = "f6a92c3184d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("memory_uses",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("idempotency_key", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("memory_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_items.id"), nullable=False),
        sa.Column("review_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_reviews.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("candidate_sha256", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False), sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("idempotency_key", name="uq_memory_uses_idempotency"),
        sa.CheckConstraint("candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_memory_uses_digest"),
        sa.CheckConstraint("length(btrim(operator)) > 0 AND length(btrim(purpose)) BETWEEN 1 AND 1000",
                           name="ck_memory_uses_declaration"))


def downgrade() -> None:
    op.drop_table("memory_uses")
