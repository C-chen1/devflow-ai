import os
from datetime import datetime, timezone
from sqlalchemy import URL,create_engine,text,select,func
from sqlalchemy.orm import Session, sessionmaker

def builder_database_url():
    database_name = require_env("DEVFLOW_DB_NAME")
    return URL.create(
        "postgresql+psycopg",
        username=require_env("DEVFLOW_DB_USER"),
        password=require_env("DEVFLOW_DB_PASSWORD"),
        host=require_env("DEVFLOW_DB_HOST"),
        port=require_env("DEVFLOW_DB_PORT"),
        database=database_name,
    )

def create_db_engine():
    engine = create_engine(builder_database_url(),
                           connect_args={"connect_timeout": 5})
    return engine

def check_database_connection(engine):
    with engine.connect() as connection:
        result = connection.execute(text("select 1"))
        return result.scalar_one()

def require_env(name):
    value = os.environ.get(name)
    if value is None or value == "":
        raise ValueError(f"{name} is required")
    return value

def create_session_factory(engine):
    session_factory = sessionmaker(bind=engine)
    return session_factory

def get_database_time(session: Session) -> datetime:
    """Read the database wall clock in UTC within the caller's transaction."""
    value = session.execute(select(func.clock_timestamp())).scalar_one()
    return value.astimezone(timezone.utc)
