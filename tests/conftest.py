import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateSchema, DropSchema

from devflow_ai.database import create_db_engine, create_session_factory


@pytest.fixture
def test_session_factory():
    if os.environ.get("DEVFLOW_DB_NAME") != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")

    engine = create_db_engine()
    schema = f"service_test_{uuid4().hex}"
    schema_created = False
    try:
        assert engine.url.database == "devflow_test"
        with engine.begin() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one() == "devflow_test"
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            context = MigrationContext.configure(connection)
            for filename in (
                "41ff63fe9d8a_create_tasks_table.py", "a3d12f2f7ab9_add_task_idempotency_key.py",
                "c3a71bd82ef0_pin_task_base_commit.py",
                "d98f41a76e20_add_task_admission_record.py",
                "e54c6a9814d2_add_admission_usage.py",
                "f2e718a34d90_add_admission_blocked.py",
                "a1d5c8b2e940_add_admission_claim.py",
                "b72d31e9c504_add_admission_attempt_count.py",
                "c61af287b039_add_admission_intervention.py",
            ):
                path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / filename
                spec = importlib.util.spec_from_file_location(path.stem, path)
                revision = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(revision)
                with Operations.context(context):
                    revision.upgrade()
        schema_created = True
        scoped_engine = engine.execution_options(schema_translate_map={None: schema})
        yield create_session_factory(scoped_engine)
    finally:
        try:
            if schema_created:
                # Only this fixture's UUID-named schema is removed; public data is untouched.
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
                    assert not inspect(connection).has_schema(schema)
        finally:
            engine.dispose()


@pytest.fixture
def run_session_factory(test_session_factory):
    """Apply the actual Run migration inside this test's isolated schema."""
    engine = test_session_factory.kw["bind"]
    schema = engine.get_execution_options()["schema_translate_map"][None]
    path = Path(__file__).resolve().parents[1] / "migrations/versions/b312290a866d_create_runs_table.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with engine.begin() as connection:
        connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
    return test_session_factory

