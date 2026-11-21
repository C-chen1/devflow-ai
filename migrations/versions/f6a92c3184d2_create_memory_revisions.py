"""Record explicit candidate replacement relationships."""
from alembic import op
import sqlalchemy as sa

revision = "f6a92c3184d2"
down_revision = "ec624a31d740"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("memory_revisions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("previous_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_items.id"), nullable=False),
        sa.Column("next_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_items.id"), nullable=False),
        sa.Column("previous_sha256", sa.Text(), nullable=False), sa.Column("next_sha256", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("previous_id", name="uq_memory_revisions_previous"),
        sa.UniqueConstraint("next_id", name="uq_memory_revisions_next"),
        sa.CheckConstraint("previous_id <> next_id", name="ck_memory_revisions_distinct"),
        sa.CheckConstraint("previous_sha256 ~ '^[0-9a-f]{64}$' AND next_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_memory_revisions_digests"),
        sa.CheckConstraint("length(btrim(operator)) > 0 AND length(btrim(reason)) BETWEEN 1 AND 4000",
                           name="ck_memory_revisions_declaration"))


def downgrade() -> None:
    op.drop_table("memory_revisions")
