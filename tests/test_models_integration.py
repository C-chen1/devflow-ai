import os,pytest
from devflow_ai.database import create_session_factory
from uuid import uuid4
from sqlalchemy import insert
from devflow_ai.models import Task
from devflow_ai.enums import TaskStatus
from sqlalchemy.exc import IntegrityError
from devflow_ai.repositories import TaskRepository


def test_task_status_defaults_to_pending(test_session_factory):
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")
    engine = test_session_factory.kw["bind"]
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                statement = insert(Task).values(idempotency_key=uuid4(), title="status default test",description="description default test",repo="repo default test",policy_version="db_test_only",policy_snapshot={"test_only":True}).returning(Task.status)
                result = connection.execute(statement)
                actual_status = result.scalar_one()
                assert actual_status == TaskStatus.PENDING
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

def test_task_reject_null_title(test_session_factory):
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")
    engine = test_session_factory.kw["bind"]
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                statement = insert(Task).values(idempotency_key=uuid4(), title=None, description="description default test",
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


def test_session_commit_persists_task(test_session_factory):
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")

    engine = test_session_factory.kw["bind"]
    task_id = None
    try:
        session_factory = create_session_factory(engine)
        try:
            with session_factory() as write_session:
                # Schema-test fixture only; not a complete business Policy.
                task = Task(
                    idempotency_key=uuid4(),
                    title="session persistence test",
                    description="verify committed Task is visible to a new Session",
                    repo="repo default test",
                    policy_version="db_test_only",
                    policy_snapshot={"test_only": True},
                )
                write_session.add(task)
                write_session.flush()
                task_id = task.id
                assert task_id is not None
                write_session.commit()

            with session_factory() as read_session:
                loaded_task = read_session.get(Task, task_id)
                assert loaded_task is not None
                assert loaded_task.id == task_id
                assert loaded_task.title == "session persistence test"
        finally:
            if task_id is not None:
                with session_factory() as cleanup_session:
                    cleanup_task = cleanup_session.get(Task, task_id)
                    if cleanup_task is not None:
                        cleanup_session.delete(cleanup_task)
                        cleanup_session.commit()
    finally:
        engine.dispose()


def test_session_failure_rolls_back_all_tasks(test_session_factory):
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")

    engine = test_session_factory.kw["bind"]
    task_id = None
    try:
        session_factory = create_session_factory(engine)
        try:
            with session_factory() as write_session:
                try:
                    # Schema-test fixtures only; not complete business Policies.
                    first_task = Task(
                        idempotency_key=uuid4(),
                        title="session rollback test",
                        description="verify an earlier flush is rolled back after failure",
                        repo="repo default test",
                        policy_version="db_test_only",
                        policy_snapshot={"test_only": True},
                    )
                    write_session.add(first_task)
                    write_session.flush()
                    task_id = first_task.id
                    assert task_id is not None

                    invalid_task = Task(
                        idempotency_key=uuid4(),
                        title=None,
                        description="trigger a title NOT NULL failure in the same transaction",
                        repo="repo default test",
                        policy_version="db_test_only",
                        policy_snapshot={"test_only": True},
                    )
                    write_session.add(invalid_task)
                    with pytest.raises(IntegrityError) as exc_info:
                        write_session.flush()
                    error_column = exc_info.value.orig.diag.column_name
                    assert error_column == "title"
                finally:
                    write_session.rollback()

            with session_factory() as read_session:
                loaded_task = read_session.get(Task, task_id)
                assert loaded_task is None
        finally:
            if task_id is not None:
                with session_factory() as cleanup_session:
                    cleanup_task = cleanup_session.get(Task, task_id)
                    if cleanup_task is not None:
                        cleanup_session.delete(cleanup_task)
                        cleanup_session.commit()
    finally:
        engine.dispose()


def test_task_repository_add_allows_caller_rollback(test_session_factory):
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")

    engine = test_session_factory.kw["bind"]
    task_id = None
    try:
        session_factory = create_session_factory(engine)
        try:
            with session_factory() as write_session:
                try:
                    # Schema-test fixture only; not a complete business Policy.
                    task = Task(
                        idempotency_key=uuid4(),
                        title="repository caller rollback test",
                        description="verify the repository does not commit the caller transaction",
                        repo="repo default test",
                        policy_version="db_test_only",
                        policy_snapshot={"test_only": True},
                    )
                    repository = TaskRepository(write_session)
                    added_task = repository.add(task)
                    task_id = task.id
                    assert added_task is task
                    assert task_id is not None
                finally:
                    write_session.rollback()

            with session_factory() as read_session:
                loaded_task = read_session.get(Task, task_id)
                assert loaded_task is None
        finally:
            if task_id is not None:
                with session_factory() as cleanup_session:
                    cleanup_task = cleanup_session.get(Task, task_id)
                    if cleanup_task is not None:
                        cleanup_session.delete(cleanup_task)
                        cleanup_session.commit()
    finally:
        engine.dispose()


def test_task_repository_get_by_id_returns_task_or_none(test_session_factory):
    database = os.environ.get("DEVFLOW_DB_NAME")
    if database != "devflow_test":
        pytest.skip("需要显式配置 devflow_test 数据库")

    engine = test_session_factory.kw["bind"]
    task_id = None
    try:
        session_factory = create_session_factory(engine)
        try:
            with session_factory() as write_session:
                # Schema-test fixture only; not a complete business Policy.
                task = Task(
                    idempotency_key=uuid4(),
                    title="repository lookup test",
                    description="verify lookup returns a committed Task or None",
                    repo="repo default test",
                    policy_version="db_test_only",
                    policy_snapshot={"test_only": True},
                )
                repository = TaskRepository(write_session)
                repository.add(task)
                task_id = task.id
                assert task_id is not None
                write_session.commit()

            with session_factory() as read_session:
                repository = TaskRepository(read_session)
                loaded_task = repository.get_by_id(task_id)
                assert loaded_task is not None
                assert loaded_task.id == task_id
                assert loaded_task.title == "repository lookup test"
        finally:
            if task_id is not None:
                with session_factory() as cleanup_session:
                    cleanup_task = cleanup_session.get(Task, task_id)
                    if cleanup_task is not None:
                        cleanup_session.delete(cleanup_task)
                        cleanup_session.commit()

        with session_factory() as read_session:
            repository = TaskRepository(read_session)
            loaded_task = repository.get_by_id(task_id)
            assert loaded_task is None
    finally:
        engine.dispose()
