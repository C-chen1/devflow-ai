import devflow_ai.api as api_module
from fastapi.testclient import TestClient
from devflow_ai.api import app
from sqlalchemy.exc import SQLAlchemyError

def test_health_live():
    response = TestClient(app).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def fake_check_database_connection(engine):
    return 1

def test_health_ready_success(monkeypatch):
    set_test_db_env(monkeypatch)
    monkeypatch.setattr(api_module, "check_database_connection", fake_check_database_connection)
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

def fake_check_database_connection_failure(engine):
    raise SQLAlchemyError("simulate database failure")

def test_health_ready_database_unavailable(monkeypatch):
    set_test_db_env(monkeypatch)
    monkeypatch.setattr(api_module, "check_database_connection", fake_check_database_connection_failure)
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}

def set_test_db_env(monkeypatch):
    monkeypatch.setenv("DEVFLOW_DB_USER", "test_user")
    monkeypatch.setenv("DEVFLOW_DB_PASSWORD", "test_password")
    monkeypatch.setenv("DEVFLOW_DB_HOST", "localhost")
    monkeypatch.setenv("DEVFLOW_DB_PORT", "5434")
    monkeypatch.setenv("DEVFLOW_DB_NAME", "devflow_test")

