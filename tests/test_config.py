import pytest
from pydantic import ValidationError

from devflow_ai.config import LeaseConfig


def test_lease_config_defaults():
    config = LeaseConfig()
    assert config.heartbeat_interval_seconds == 10
    assert config.lease_duration_seconds == 30


def test_lease_config_accepts_custom_valid_window():
    config = LeaseConfig.model_validate({
        "heartbeat_interval_seconds": 15,
        "lease_duration_seconds": 45,
    })
    assert isinstance(config, LeaseConfig)
    assert config.heartbeat_interval_seconds == 15
    assert config.lease_duration_seconds == 45


@pytest.mark.parametrize("field", ["heartbeat_interval_seconds", "lease_duration_seconds"])
@pytest.mark.parametrize("value", [0, -1])
def test_lease_config_rejects_non_positive_seconds(field, value):
    with pytest.raises(ValidationError) as exc_info:
        LeaseConfig(**{field: value})
    assert any(
        error["loc"] == (field,) and error["type"] == "greater_than"
        for error in exc_info.value.errors()
    )


@pytest.mark.parametrize("field", ["heartbeat_interval_seconds", "lease_duration_seconds"])
@pytest.mark.parametrize("value", [1.5, "10", True, None])
def test_lease_config_rejects_non_integer_inputs(field, value):
    with pytest.raises(ValidationError) as exc_info:
        LeaseConfig(**{field: value})
    assert any(
        error["loc"] == (field,) and error["type"] == "int_type"
        for error in exc_info.value.errors()
    )


@pytest.mark.parametrize("values", [
    {"heartbeat_interval_seconds": 10, "lease_duration_seconds": 10},
    {"heartbeat_interval_seconds": 10, "lease_duration_seconds": 5},
    {"heartbeat_interval_seconds": 40},
    {"lease_duration_seconds": 5},
])
def test_lease_config_rejects_equal_or_shorter_lease_including_defaults(values):
    with pytest.raises(ValidationError, match="lease_duration_seconds must be greater than heartbeat_interval_seconds"):
        LeaseConfig.model_validate(values)


def test_lease_config_rejects_unknown_fields():
    with pytest.raises(ValidationError) as exc_info:
        LeaseConfig(heartbeat_interval_second=10)
    assert any(
        error["loc"] == ("heartbeat_interval_second",) and error["type"] == "extra_forbidden"
        for error in exc_info.value.errors()
    )
