import subprocess
import sys
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from devflow_ai.database import create_db_engine
from devflow_ai.runtime_environment import (
    DistributionVersion, EnvironmentComparisonStatus, PostgreSQLConnectionObservation, PythonEnvironmentObservation,
    compare_postgresql_connection_observations, compare_python_environment_observations,
    observe_current_python_environment, observe_postgresql_connection,
)
import devflow_ai.python_environment_probe as probe_module


def sample_observation(**changes):
    values = {
        "executable": "python-path", "python_version": "3.11.0", "python_bits": 64, "dont_write_bytecode": True, "implementation": "CPython",
        "os_system": "Windows", "os_release": "11",
        "distributions": (DistributionVersion(name="pytest", version="8.0"),),
    }
    values.update(changes)
    return PythonEnvironmentObservation(**values)


def test_current_observation_matches_separate_invocation_of_same_python():
    local = observe_current_python_environment()
    process = subprocess.run(
        [sys.executable, *(["-B"] if sys.dont_write_bytecode else []), "-c",
         Path(probe_module.__file__).read_text(encoding="utf-8")],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert process.returncode == 0
    child = PythonEnvironmentObservation.model_validate_json(process.stdout)
    comparison = compare_python_environment_observations(local, child)
    assert comparison.status == EnvironmentComparisonStatus.OBSERVED_FIELDS_MATCH
    assert "pytest" in {item.name for item in child.distributions}


@pytest.mark.parametrize(("field", "value"), [
    ("executable", "different-python"), ("python_version", "3.12.0"),
    ("implementation", "Other"), ("os_system", "Linux"), ("os_release", "10"),
    ("python_bits", 32),
    ("dont_write_bytecode", False),
    ("distributions", (DistributionVersion(name="pytest", version="9.0"),)),
])
def test_observation_reports_changed_recorded_field(field, value):
    result = compare_python_environment_observations(sample_observation(), sample_observation(**{field: value}))
    assert result.status == EnvironmentComparisonStatus.DIFFERENT
    assert result.changed_fields == (field,)


def test_missing_observation_is_unavailable():
    result = compare_python_environment_observations(sample_observation(), None)
    assert result.status == EnvironmentComparisonStatus.UNAVAILABLE
    legacy = sample_observation(dont_write_bytecode=None)
    assert compare_python_environment_observations(legacy, legacy).status == EnvironmentComparisonStatus.UNAVAILABLE


def test_duplicate_or_ambiguous_distributions_are_rejected():
    item = DistributionVersion(name="pytest", version="8")
    with pytest.raises(ValidationError, match="unique"):
        sample_observation(distributions=(item, item))
    with pytest.raises(ValidationError, match="pytest"):
        sample_observation(distributions=(DistributionVersion(name="other", version="1"),))
    with pytest.raises(ValidationError, match="normalized"):
        DistributionVersion(name="Py_Test", version="1")


def test_secrets_and_unobserved_environment_changes_are_not_misrepresented(monkeypatch):
    before = observe_current_python_environment()
    monkeypatch.setenv("DEVFLOW_TEST_PRIVATE_VALUE", "local-secret-sentinel")
    after = observe_current_python_environment()
    assert "local-secret-sentinel" not in after.model_dump_json()
    assert compare_python_environment_observations(before, after).status == EnvironmentComparisonStatus.OBSERVED_FIELDS_MATCH


def postgres_sample(**changes):
    values = {
        "database_name": "test_db", "session_user": "test_user", "effective_role": "test_user",
        "server_version": "16", "server_address": "127.0.0.1/32", "server_port": 5432,
    }
    values.update(changes)
    return PostgreSQLConnectionObservation(**values)


def test_postgresql_connection_comparison_detects_role_change_without_passwords():
    before = postgres_sample()
    after = postgres_sample(effective_role="read_only_user")
    result = compare_postgresql_connection_observations(before, after)
    assert result.status == EnvironmentComparisonStatus.DIFFERENT
    assert result.changed_fields == ("effective_role",)
    with pytest.raises(ValidationError, match="Extra inputs"):
        postgres_sample(password="do-not-store")


def test_missing_database_address_or_observation_is_not_a_match():
    before = postgres_sample(server_address=None)
    assert compare_postgresql_connection_observations(before, before).status == EnvironmentComparisonStatus.UNAVAILABLE
    assert compare_postgresql_connection_observations(before, None).status == EnvironmentComparisonStatus.UNAVAILABLE


def test_database_observation_rejects_non_postgresql_connection():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection, pytest.raises(ValueError, match="PostgreSQL"):
            observe_postgresql_connection(connection)
    finally:
        engine.dispose()


def test_database_observation_reads_two_existing_test_connections():
    if os.environ.get("DEVFLOW_DB_NAME") != "devflow_test":
        pytest.skip("Requires explicitly configured devflow_test")
    engine = create_db_engine()
    try:
        assert engine.url.database == "devflow_test"
        with engine.connect() as first_connection, engine.connect() as second_connection:
            first = observe_postgresql_connection(first_connection)
            second = observe_postgresql_connection(second_connection)
        assert first.database_name == second.database_name == "devflow_test"
        assert first.effective_role == second.effective_role
        comparison = compare_postgresql_connection_observations(first, second)
        if first.server_address is None or first.server_port is None:
            assert comparison.status == EnvironmentComparisonStatus.UNAVAILABLE
        else:
            assert comparison.status == EnvironmentComparisonStatus.OBSERVED_FIELDS_MATCH
        assert set(first.model_dump()) == {
            "database_name", "session_user", "effective_role", "server_version", "server_address", "server_port",
        }
    finally:
        engine.dispose()
