import importlib.util
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devflow_ai.database import create_db_engine
from devflow_ai.models import Task
from devflow_ai.repositories import TaskRepository


def load_revision(filename):
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_idempotency_migration_preserves_legacy_rows():
    if os.environ.get("DEVFLOW_DB_NAME") != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")

    engine = create_db_engine()
    # The random name contains only a trusted prefix and UUID hex digits.
    schema = f"migration_test_{uuid4().hex}"
    old_revision = load_revision("41ff63fe9d8a_create_tasks_table.py")
    new_revision = load_revision("a3d12f2f7ab9_add_task_idempotency_key.py")
    base_commit_revision = load_revision("c3a71bd82ef0_pin_task_base_commit.py")
    task_revisions = [load_revision(name) for name in (
        "d98f41a76e20_add_task_admission_record.py", "e54c6a9814d2_add_admission_usage.py",
        "f2e718a34d90_add_admission_blocked.py", "a1d5c8b2e940_add_admission_claim.py",
        "b72d31e9c504_add_admission_attempt_count.py", "c61af287b039_add_admission_intervention.py",
    )]
    try:
        assert engine.url.database == "devflow_test"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one() == "devflow_test"
            connection.rollback()
            transaction = connection.begin()
            try:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
                context = MigrationContext.configure(connection)
                with Operations.context(context):
                    old_revision.upgrade()
                    insert_legacy = text(
                        "INSERT INTO tasks (title, description, repo, policy_version, policy_snapshot) "
                        "VALUES (:title, :description, :repo, :version, CAST(:snapshot AS jsonb))"
                    )
                    connection.execute(insert_legacy, [
                        {"title": "legacy A", "description": "first issue", "repo": "repo-a",
                         "version": "legacy", "snapshot": '{"legacy": true}'},
                        {"title": "legacy B", "description": "second issue", "repo": "repo-b",
                         "version": "legacy", "snapshot": '{"legacy": true}'},
                    ])
                    legacy_rows = connection.execute(text(
                        "SELECT id, title, description, repo, status, policy_version, policy_snapshot "
                        "FROM tasks ORDER BY title"
                    )).mappings().all()

                    new_revision.upgrade()
                    rows = connection.execute(text(
                        "SELECT id, title, description, repo, status, policy_version, policy_snapshot, "
                        "idempotency_key FROM tasks ORDER BY title"
                    )).mappings().all()
                    assert len(rows) == 2
                    assert [dict(row) | {"idempotency_key": None} for row in rows] == [
                        dict(row) | {"idempotency_key": None} for row in legacy_rows
                    ]
                    keys = [row["idempotency_key"] for row in rows]
                    assert all(isinstance(key, UUID) for key in keys)
                    assert len(set(keys)) == 2

                    # Existing ELIGIBLE rows must be reconciled before migration.
                    with pytest.raises(IntegrityError) as unpinned:
                        with connection.begin_nested():
                            connection.execute(text("UPDATE tasks SET status = 'ELIGIBLE'"))
                            base_commit_revision.upgrade()
                    assert unpinned.value.orig.diag.constraint_name == "ck_tasks_eligible_base_commit"

                    base_commit_revision.upgrade()
                    for revision in task_revisions:
                        revision.upgrade()
                    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                        repository = TaskRepository(session)
                        for row in rows:
                            loaded_task = repository.get_by_idempotency_key(row["idempotency_key"])
                            assert loaded_task is not None
                            assert loaded_task.id == row["id"]
                            assert loaded_task.title == row["title"]
                            assert loaded_task.idempotency_key == row["idempotency_key"]
                            assert loaded_task.base_commit is None
                        assert repository.get_by_idempotency_key(uuid4()) is None
                    for revision in reversed(task_revisions):
                        revision.downgrade()
                    base_commit_revision.downgrade()

                    columns = {column["name"]: column for column in inspect(connection).get_columns("tasks", schema=schema)}
                    assert columns["idempotency_key"]["nullable"] is False
                    assert columns["idempotency_key"]["default"] is None
                    assert any(
                        constraint["name"] == "uq_tasks_idempotency_key"
                        and constraint["column_names"] == ["idempotency_key"]
                        for constraint in inspect(connection).get_unique_constraints("tasks", schema=schema)
                    )

                    insert_new = text(
                        "INSERT INTO tasks (title, description, repo, policy_version, policy_snapshot, idempotency_key) "
                        "VALUES ('new', 'new issue', 'repo-new', 'test', '{}'::jsonb, :key)"
                    )
                    connection.execute(insert_new, {"key": uuid4()})
                    with pytest.raises(IntegrityError) as duplicate:
                        with connection.begin_nested():
                            connection.execute(insert_new, {"key": keys[0]})
                    assert duplicate.value.orig.sqlstate == "23505"
                    assert duplicate.value.orig.diag.constraint_name == "uq_tasks_idempotency_key"
                    with pytest.raises(IntegrityError) as missing:
                        with connection.begin_nested():
                            connection.execute(insert_new, {"key": None})
                    assert missing.value.orig.sqlstate == "23502"
                    assert missing.value.orig.diag.column_name == "idempotency_key"

                    new_revision.downgrade()
                    assert "idempotency_key" not in {
                        column["name"] for column in inspect(connection).get_columns("tasks", schema=schema)
                    }
                    restored_rows = connection.execute(text(
                        "SELECT id, title, description, repo, status, policy_version, policy_snapshot "
                        "FROM tasks WHERE title LIKE 'legacy %' ORDER BY title"
                    )).mappings().all()
                    assert [dict(row) for row in restored_rows] == [dict(row) for row in legacy_rows]
                    new_revision.upgrade()
                    assert connection.execute(text(
                        "SELECT count(DISTINCT idempotency_key) FROM tasks"
                    )).scalar_one() == 3

                # Verify the ORM describes the same named unique and non-null key.
                assert not Task.__table__.c.idempotency_key.nullable
                assert Task.__table__.c.idempotency_key.server_default is None
                assert any(constraint.name == "uq_tasks_idempotency_key" for constraint in Task.__table__.constraints)
            finally:
                # PostgreSQL transactional DDL removes this isolated schema and its test rows.
                transaction.rollback()
            assert not inspect(connection).has_schema(schema)
    finally:
        engine.dispose()
