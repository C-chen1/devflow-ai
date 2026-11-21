"""Add conservative, explicitly declared memory applicability and use context."""
from alembic import op
import sqlalchemy as sa

revision = "b41e9d2c8370"
down_revision = "a5810e46f2c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_items", sa.Column("applicable_base_commit", sa.Text(), nullable=True))
    op.add_column("memory_items", sa.Column("applicable_environment", sa.Text(), nullable=True))
    op.create_check_constraint("ck_memory_items_applicability_pair", "memory_items",
        "(applicable_base_commit IS NULL) = (applicable_environment IS NULL)")
    op.create_check_constraint("ck_memory_items_applicability_commit", "memory_items",
        "applicable_base_commit IS NULL OR applicable_base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'")
    op.create_check_constraint("ck_memory_items_applicability_environment", "memory_items",
        "applicable_environment IS NULL OR (applicable_environment = btrim(applicable_environment) "
        "AND char_length(applicable_environment) BETWEEN 1 AND 128)")
    op.add_column("memory_uses", sa.Column("target_base_commit", sa.Text(), nullable=True))
    op.add_column("memory_uses", sa.Column("target_environment", sa.Text(), nullable=True))
    op.create_check_constraint("ck_memory_uses_applicability_pair", "memory_uses",
        "(target_base_commit IS NULL) = (target_environment IS NULL)")
    op.create_check_constraint("ck_memory_uses_base_commit", "memory_uses",
        "target_base_commit IS NULL OR target_base_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'")
    op.create_check_constraint("ck_memory_uses_environment", "memory_uses",
        "target_environment IS NULL OR (target_environment = btrim(target_environment) "
        "AND char_length(target_environment) BETWEEN 1 AND 128)")


def downgrade() -> None:
    op.drop_constraint("ck_memory_uses_environment", "memory_uses", type_="check")
    op.drop_constraint("ck_memory_uses_base_commit", "memory_uses", type_="check")
    op.drop_constraint("ck_memory_uses_applicability_pair", "memory_uses", type_="check")
    op.drop_column("memory_uses", "target_environment")
    op.drop_column("memory_uses", "target_base_commit")
    op.drop_constraint("ck_memory_items_applicability_environment", "memory_items", type_="check")
    op.drop_constraint("ck_memory_items_applicability_commit", "memory_items", type_="check")
    op.drop_constraint("ck_memory_items_applicability_pair", "memory_items", type_="check")
    op.drop_column("memory_items", "applicable_environment")
    op.drop_column("memory_items", "applicable_base_commit")
