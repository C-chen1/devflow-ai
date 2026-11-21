"""Record one operator-declared review per candidate; never modify the candidate."""
from alembic import op
import sqlalchemy as sa

revision = "d18e42f70a91"
down_revision = "c4a7e2f19b30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_reviews",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("memory_id", sa.Uuid(as_uuid=True), sa.ForeignKey("memory_items.id"), nullable=False),
        sa.Column("candidate_sha256", sa.Text(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("memory_id", name="uq_memory_reviews_memory_id"),
        sa.CheckConstraint("conclusion IN ('accepted', 'rejected')", name="ck_memory_reviews_conclusion"),
        sa.CheckConstraint("candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_memory_reviews_sha256"),
        sa.CheckConstraint("char_length(btrim(reviewer)) > 0", name="ck_memory_reviews_reviewer"),
        sa.CheckConstraint("char_length(btrim(reason)) BETWEEN 1 AND 4000", name="ck_memory_reviews_reason"),
    )


def downgrade() -> None:
    op.drop_table("memory_reviews")
