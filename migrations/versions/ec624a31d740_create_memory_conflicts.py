"""Add explicitly declared conflicts between candidate snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "ec624a31d740"
down_revision = "d18e42f70a91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("memory_conflicts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("first_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_items.id"), nullable=False),
        sa.Column("second_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_items.id"), nullable=False),
        sa.Column("first_sha256", sa.Text(), nullable=False), sa.Column("second_sha256", sa.Text(), nullable=False),
        sa.Column("reporter", sa.Text(), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)), sa.Column("resolved_by", sa.Text()),
        sa.Column("resolution", sa.Text()),
        sa.UniqueConstraint("first_id", "second_id", name="uq_memory_conflicts_pair"),
        sa.CheckConstraint("first_id < second_id", name="ck_memory_conflicts_order"),
        sa.CheckConstraint("first_sha256 ~ '^[0-9a-f]{64}$' AND second_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_memory_conflicts_digests"),
        sa.CheckConstraint("length(btrim(reporter)) > 0 AND length(btrim(reason)) BETWEEN 1 AND 4000",
                           name="ck_memory_conflicts_declaration"),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_memory_conflicts_status"),
        sa.CheckConstraint("(status = 'resolved') = (resolved_at IS NOT NULL) AND "
                           "(status = 'resolved') = (resolved_by IS NOT NULL) AND "
                           "(status = 'resolved') = (resolution IS NOT NULL)",
                           name="ck_memory_conflicts_resolution"))


def downgrade() -> None:
    op.drop_table("memory_conflicts")
