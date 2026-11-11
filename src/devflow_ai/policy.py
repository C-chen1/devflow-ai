from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from devflow_ai.enums import AgentRole


class PolicyBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries_per_task: int = Field(default=3, ge=0)
    max_same_signature_occurrences: int = Field(default=3, ge=1)
    max_rediagnose_attempts: int = Field(default=2, ge=0)
    max_graph_steps_per_run: int = Field(default=30, ge=0)
    max_tool_calls_per_run: int = Field(default=60, ge=0)
    max_llm_tokens_per_run: int = Field(default=80000, ge=0)
    # Old snapshots lack this field; None leaves model-based admission unavailable.
    max_admission_llm_tokens_per_task: int | None = Field(default=None, ge=0, strict=True)
    max_admission_consecutive_timeouts: int | None = Field(default=None, ge=1, strict=True)
    max_admission_attempts: int | None = Field(default=None, ge=1, strict=True)
    admission_timeout_retry_delay_seconds: int | None = Field(default=None, gt=0, strict=True)
    max_output_repairs_per_agent_execution: int = Field(default=0, ge=0, strict=True)
    tool_call_timeout_seconds: int = Field(default=300, gt=0)
    max_run_duration_seconds: int = Field(default=1800, gt=0)


class PolicyPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools_by_role: dict[AgentRole, list[str]]

    @model_validator(mode="after")
    def validate_role_completeness(self):
        missing_roles = []
        for role in AgentRole:
            if role not in self.allowed_tools_by_role:
                missing_roles.append(role.value)
        if missing_roles:
            raise ValueError(f"Missing role permissions: {missing_roles}")
        return self


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    budget: PolicyBudget
    permissions: PolicyPermissions

    @field_validator("policy_version")
    @classmethod
    def validate_policy_version(cls, value: str):
        if not value.strip():
            raise ValueError("policy_version must not be blank")
        return value


def build_default_policy() -> PolicyConfig:
    allowed_tools_by_role = {}
    for role in AgentRole:
        allowed_tools_by_role[role] = []

    budget = PolicyBudget(max_output_repairs_per_agent_execution=1, max_admission_consecutive_timeouts=3,
                          max_admission_attempts=5,
                          admission_timeout_retry_delay_seconds=60)
    permissions = PolicyPermissions(allowed_tools_by_role=allowed_tools_by_role)
    policy = PolicyConfig(
        policy_version="bootstrap-no-tools-v6",
        budget=budget,
        permissions=permissions,
    )
    return policy


def build_policy_snapshot(default_policy: dict):
    policy_snapshot = deepcopy(default_policy)
    return policy_snapshot
