import pytest
from devflow_ai.database import builder_database_url


def test_database_url_requires_name(monkeypatch):
    monkeypatch.delenv("DEVFLOW_DB_NAME", raising=False)
    with pytest.raises(ValueError,match = "DEVFLOW_DB_NAME"):
        builder_database_url()

def test_database_url_rejects_empty_name(monkeypatch):
    monkeypatch.setenv("DEVFLOW_DB_NAME", "")
    with pytest.raises(ValueError,match = "DEVFLOW_DB_NAME"):
        builder_database_url()

def test_database_url_rejects_empty_password(monkeypatch):
    monkeypatch.setenv("DEVFLOW_DB_PASSWORD", "")
    monkeypatch.setenv("DEVFLOW_DB_HOST", "localhost")
    monkeypatch.setenv("DEVFLOW_DB_PORT", "5434")
    monkeypatch.setenv("DEVFLOW_DB_NAME", "devflow_test")
    monkeypatch.setenv("DEVFLOW_DB_USER", "test_user")
    with pytest.raises(ValueError,match = "DEVFLOW_DB_PASSWORD"):
        builder_database_url()



