"""Exercise the actual Alembic entry point in a fresh, isolated PostgreSQL schema."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.schema import CreateSchema, DropSchema

from devflow_ai.database import create_db_engine


def test_memory_migrations_alembic_upgrade_downgrade_upgrade(monkeypatch):
    if os.environ.get("DEVFLOW_DB_NAME") != "devflow_test":
        pytest.skip("Requires explicitly configured devflow_test")
    schema = f"memory_migration_test_{uuid4().hex}"
    admin = create_db_engine()
    created = False
    scoped = None
    try:
        with admin.begin() as connection:
            assert connection.scalar(text("SELECT current_database()")) == "devflow_test"
            connection.execute(CreateSchema(schema))
        created = True
        # No public fallback: even Alembic's version-table lookup must stay inside this schema.
        monkeypatch.setenv("PGOPTIONS", f"-csearch_path={schema}")
        scoped = create_db_engine()
        with scoped.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == "devflow_test"
            assert connection.scalar(text("SELECT current_schema()")) == schema
            assert connection.scalar(text("SELECT current_schemas(false)")) == [schema]
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        scripts = ScriptDirectory.from_config(config)
        head = scripts.get_current_head()
        assert head is not None and scripts.get_revision("a5810e46f2c9") is not None
        config.config_file_name = None  # retain pytest logging handlers across migration calls
        command.upgrade(config, "head")
        task_id = uuid4()
        with scoped.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
            connection.execute(text(
                "INSERT INTO tasks (id, idempotency_key, title, description, repo, policy_version, policy_snapshot) "
                "VALUES (:id, :key, 'fixture', 'fixture', 'fixture', 'test-only', CAST('{}' AS jsonb))"),
                {"id": task_id, "key": uuid4()})
        command.downgrade(config, "b7f2c31d8a90")
        with scoped.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "b7f2c31d8a90"
            assert connection.scalar(text("SELECT count(*) FROM tasks WHERE id = :id"), {"id": task_id}) == 1
            assert connection.scalar(text("SELECT to_regclass('memory_items')")) is None
            assert connection.scalar(text("SELECT to_regclass('experiments')")) is None
        command.upgrade(config, "head")
        with scoped.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
            assert connection.scalar(text("SELECT count(*) FROM tasks WHERE id = :id"), {"id": task_id}) == 1
            for table in ("memory_items", "memory_reviews", "memory_conflicts", "memory_revisions",
                          "memory_uses", "experiments", "experiment_tasks", "experiment_candidates",
                          "experiment_reservations", "experiment_task_results"):
                assert connection.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is not None
    finally:
        if scoped is not None:
            scoped.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
