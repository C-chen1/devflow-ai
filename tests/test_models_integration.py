import os,pytest
from devflow_ai.database import create_db_engine
from sqlalchemy import insert
from devflow_ai.models import Task
from devflow_ai.enums import TaskStatus
from sqlalchemy.exc import IntegrityError


def test_task_status_defaults_to_pending():
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                statement = insert(Task).values(title="status default test",description="description default test",repo="repo default test",policy_version="db_test_only",policy_snapshot={"test_only":True}).returning(Task.status)
                result = connection.execute(statement)
                actual_status = result.scalar_one()
                assert actual_status == TaskStatus.PENDING
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

def test_task_reject_null_title():
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                statement = insert(Task).values(title=None, description="description default test",
                                                repo="repo default test", policy_version="db_test_only",
                                                policy_snapshot={"test_only": True}).returning(Task.status)
                with pytest.raises(IntegrityError) as exc_info:
                    connection.execute(statement)
                error_column = exc_info.value.orig.diag.column_name
                assert error_column == "title"
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
