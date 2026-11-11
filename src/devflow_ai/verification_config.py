"""Public verification settings supplied by a trusted operator, not an Agent."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NonBlank = Annotated[str, StringConstraints(min_length=1)]


class VerificationConfig(BaseModel):
    """Bind one public test command and target set to a specific Task and base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    base_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    command_name: NonBlank
    command: tuple[NonBlank, ...] = Field(min_length=1)
    target_tests: tuple[NonBlank, ...] = Field(min_length=1)
    pythonpath_relative: NonBlank | None = None
    external_connections: Literal["unknown", "none", "required"] = "unknown"
    independent_suite_version: NonBlank | None = None

    @field_validator("command_name", "pythonpath_relative", "independent_suite_version")
    @classmethod
    def nonblank_field(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Value must not be blank")
        return value

    @model_validator(mode="after")
    def unique_targets(self):
        if (any(not value.strip() for value in (*self.command, *self.target_tests))
                or len(set(self.target_tests)) != len(self.target_tests)):
            raise ValueError("Command and unique target tests must be nonblank")
        return self
