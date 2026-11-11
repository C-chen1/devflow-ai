from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeaseConfig(BaseModel):
    """Worker lease settings, separate from a Task's fixed Policy snapshot."""

    model_config = ConfigDict(extra="forbid")

    heartbeat_interval_seconds: int = Field(default=10, gt=0, strict=True)
    lease_duration_seconds: int = Field(default=30, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_lease_window(self) -> "LeaseConfig":
        if self.lease_duration_seconds <= self.heartbeat_interval_seconds:
            raise ValueError(
                "lease_duration_seconds must be greater than heartbeat_interval_seconds"
            )
        return self


class HeartbeatConfig(BaseModel):
    """Explicit runtime limits; no production defaults or Task Policy changes."""

    model_config = ConfigDict(extra="forbid")

    statement_timeout_seconds: int = Field(gt=0, strict=True)
    shutdown_timeout_seconds: int = Field(gt=0, strict=True)
