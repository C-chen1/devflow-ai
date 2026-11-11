"""Limited Python environment observations, not an isolation or equivalence guarantee."""
from enum import StrEnum
from re import fullmatch
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from devflow_ai.planner import NonBlankText
from devflow_ai.python_environment_probe import collect_python_observation


class DistributionVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonBlankText
    version: NonBlankText

    @model_validator(mode="after")
    def require_normalized_name(self):
        if not fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.name):
            raise ValueError("Distribution name must be normalized")
        return self


class PythonEnvironmentObservation(BaseModel):
    """Selected facts about one Python process; caller must establish provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executable: NonBlankText
    python_version: NonBlankText
    python_bits: Literal[32, 64]
    dont_write_bytecode: bool | None = None
    implementation: NonBlankText
    os_system: NonBlankText
    os_release: NonBlankText
    distributions: tuple[DistributionVersion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_distributions(self):
        names = [item.name for item in self.distributions]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Distributions must have unique, sorted names")
        if "pytest" not in names:
            raise ValueError("pytest must be present in the observed environment")
        return self


class EnvironmentComparisonStatus(StrEnum):
    OBSERVED_FIELDS_MATCH = "observed_fields_match"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"


class EnvironmentComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnvironmentComparisonStatus
    changed_fields: tuple[str, ...] = ()


class PostgreSQLConnectionObservation(BaseModel):
    """Selected facts about an already-open connection, not effective permissions or data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database_name: NonBlankText
    session_user: NonBlankText
    effective_role: NonBlankText
    server_version: NonBlankText
    server_address: NonBlankText | None
    server_port: int | None = Field(ge=1, le=65535)


def observe_current_python_environment() -> PythonEnvironmentObservation:
    """Observe this Python process, not an unrelated pytest process or its complete environment."""
    return PythonEnvironmentObservation.model_validate(collect_python_observation())


def compare_python_environment_observations(
    baseline: PythonEnvironmentObservation | None, current: PythonEnvironmentObservation | None,
) -> EnvironmentComparison:
    """Compare recorded fields only; matching observations do not prove full equivalence."""
    if baseline is None or current is None:
        return EnvironmentComparison(status=EnvironmentComparisonStatus.UNAVAILABLE)
    fields = ("executable", "python_version", "python_bits", "dont_write_bytecode", "implementation", "os_system", "os_release", "distributions")
    differences = tuple(
        field for field in fields if getattr(baseline, field) is not None and getattr(current, field) is not None
        and getattr(baseline, field) != getattr(current, field)
    )
    if not differences and any(getattr(observation, field) is None for observation in (baseline, current) for field in fields):
        return EnvironmentComparison(status=EnvironmentComparisonStatus.UNAVAILABLE)
    return EnvironmentComparison(
        status=EnvironmentComparisonStatus.DIFFERENT if differences else EnvironmentComparisonStatus.OBSERVED_FIELDS_MATCH,
        changed_fields=differences,
    )


def observe_postgresql_connection(connection: Connection) -> PostgreSQLConnectionObservation:
    """Inspect the supplied connection only; do not create connections or read credentials."""
    if connection.dialect.name != "postgresql":
        raise ValueError("The supplied connection must use PostgreSQL")
    row = connection.execute(text(
        "SELECT current_database() AS database_name, session_user AS session_user, "
        "current_user AS effective_role, current_setting('server_version') AS server_version, "
        "CAST(inet_server_addr() AS text) AS server_address, inet_server_port() AS server_port"
    )).mappings().one()
    return PostgreSQLConnectionObservation.model_validate(dict(row))


def compare_postgresql_connection_observations(
    baseline: PostgreSQLConnectionObservation | None, current: PostgreSQLConnectionObservation | None,
) -> EnvironmentComparison:
    """Compare known connection facts; matching facts do not prove equal data or permissions."""
    if baseline is None or current is None:
        return EnvironmentComparison(status=EnvironmentComparisonStatus.UNAVAILABLE)
    fields = ("database_name", "session_user", "effective_role", "server_version", "server_address", "server_port")
    differences = tuple(
        field for field in fields if getattr(baseline, field) is not None and getattr(current, field) is not None
        and getattr(baseline, field) != getattr(current, field)
    )
    if differences:
        return EnvironmentComparison(status=EnvironmentComparisonStatus.DIFFERENT, changed_fields=differences)
    if any(getattr(observation, field) is None for observation in (baseline, current) for field in fields):
        return EnvironmentComparison(status=EnvironmentComparisonStatus.UNAVAILABLE)
    return EnvironmentComparison(status=EnvironmentComparisonStatus.OBSERVED_FIELDS_MATCH)
