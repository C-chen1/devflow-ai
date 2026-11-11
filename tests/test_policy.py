import pytest
from pydantic import ValidationError
from devflow_ai.enums import AgentRole
from devflow_ai.policy import PolicyConfig, PolicyPermissions, build_default_policy


def build_empty_role_tools():
    allowed_tools_by_role = {}
    for role in AgentRole:
        allowed_tools_by_role[role.value] = []
    return allowed_tools_by_role


def test_policy_permissions_accepts_explicit_empty_lists():
    permissions = PolicyPermissions(allowed_tools_by_role=build_empty_role_tools())

    assert len(permissions.allowed_tools_by_role) == len(AgentRole)
    for role in AgentRole:
        assert permissions.allowed_tools_by_role[role] == []


def test_policy_permissions_requires_every_role():
    for missing_role in AgentRole:
        allowed_tools_by_role = build_empty_role_tools()
        del allowed_tools_by_role[missing_role.value]

        with pytest.raises(ValidationError, match=missing_role.value):
            PolicyPermissions(allowed_tools_by_role=allowed_tools_by_role)

    with pytest.raises(ValidationError, match="Missing role permissions"):
        PolicyPermissions(allowed_tools_by_role={})

    with pytest.raises(ValidationError, match="allowed_tools_by_role"):
        PolicyPermissions()


def test_policy_permissions_rejects_unknown_role():
    allowed_tools_by_role = build_empty_role_tools()
    allowed_tools_by_role["UNKNOWN_ROLE"] = []

    with pytest.raises(ValidationError) as exc_info:
        PolicyPermissions(allowed_tools_by_role=allowed_tools_by_role)

    errors = exc_info.value.errors()
    assert errors[0]["type"] == "enum"
    assert errors[0]["loc"][0] == "allowed_tools_by_role"
    assert errors[0]["loc"][1] == "UNKNOWN_ROLE"


def test_policy_config_combines_version_budget_and_permissions():
    config = PolicyConfig(
        policy_version="policy-test",
        budget={"max_retries_per_task": 0},
        permissions={"allowed_tools_by_role": build_empty_role_tools()},
    )

    assert config.policy_version == "policy-test"
    assert config.budget.max_retries_per_task == 0
    assert config.permissions.allowed_tools_by_role[AgentRole.CODER] == []


def test_policy_config_requires_version_budget_and_permissions():
    config_data = {
        "policy_version": "policy-test",
        "budget": {},
        "permissions": {"allowed_tools_by_role": build_empty_role_tools()},
    }
    for missing_field in ("policy_version", "budget", "permissions"):
        incomplete_config = config_data.copy()
        del incomplete_config[missing_field]

        with pytest.raises(ValidationError) as exc_info:
            PolicyConfig(**incomplete_config)

        errors = exc_info.value.errors()
        assert errors[0]["loc"] == (missing_field,)
        assert errors[0]["type"] == "missing"


def test_policy_config_applies_nested_validation():
    permissions = {"allowed_tools_by_role": build_empty_role_tools()}
    with pytest.raises(ValidationError) as exc_info:
        PolicyConfig(
            policy_version="policy-test",
            budget={"max_retries_per_task": -1},
            permissions=permissions,
        )
    assert exc_info.value.errors()[0]["loc"] == ("budget", "max_retries_per_task")

    del permissions["allowed_tools_by_role"][AgentRole.CODER.value]
    with pytest.raises(ValidationError, match="CODER"):
        PolicyConfig(policy_version="policy-test", budget={}, permissions=permissions)


def test_policy_config_rejects_unknown_fields():
    with pytest.raises(ValidationError) as exc_info:
        PolicyConfig(
            policy_version="policy-test",
            budget={},
            permissions={"allowed_tools_by_role": build_empty_role_tools()},
            unexpected=True,
        )
    assert exc_info.value.errors()[0]["loc"] == ("unexpected",)
    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_policy_config_rejects_blank_version():
    for policy_version in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError) as exc_info:
            PolicyConfig(
                policy_version=policy_version,
                budget={},
                permissions={"allowed_tools_by_role": build_empty_role_tools()},
            )
        assert exc_info.value.errors()[0]["loc"] == ("policy_version",)
        assert exc_info.value.errors()[0]["type"] == "value_error"


def test_policy_config_preserves_nonblank_version():
    policy_version = " policy-v1 "
    config = PolicyConfig(
        policy_version=policy_version,
        budget={},
        permissions={"allowed_tools_by_role": build_empty_role_tools()},
    )

    assert config.policy_version == policy_version


def test_default_policy_has_expected_budget_and_denies_tools():
    policy = build_default_policy()

    assert policy.policy_version == "bootstrap-no-tools-v6"
    assert policy.budget.model_dump() == {
        "max_retries_per_task": 3,
        "max_same_signature_occurrences": 3,
        "max_rediagnose_attempts": 2,
        "max_graph_steps_per_run": 30,
        "max_tool_calls_per_run": 60,
        "max_llm_tokens_per_run": 80000,
        "max_admission_llm_tokens_per_task": None,
        "max_admission_consecutive_timeouts": 3,
        "max_admission_attempts": 5,
        "admission_timeout_retry_delay_seconds": 60,
        "max_output_repairs_per_agent_execution": 1,
        "tool_call_timeout_seconds": 300,
        "max_run_duration_seconds": 1800,
    }
    assert policy.permissions.allowed_tools_by_role == {role: [] for role in AgentRole}


def test_legacy_snapshot_without_repair_budget_disables_repairs():
    snapshot = build_default_policy().model_dump(mode="json")
    snapshot["policy_version"] = "bootstrap-no-tools-v1"
    del snapshot["budget"]["max_output_repairs_per_agent_execution"]

    policy = PolicyConfig.model_validate(snapshot)

    assert policy.budget.max_output_repairs_per_agent_execution == 0


@pytest.mark.parametrize("repair_limit", [-1, 1.5, True])
def test_policy_rejects_invalid_output_repair_limit(repair_limit):
    snapshot = build_default_policy().model_dump(mode="json")
    snapshot["budget"]["max_output_repairs_per_agent_execution"] = repair_limit

    with pytest.raises(ValidationError, match="max_output_repairs_per_agent_execution"):
        PolicyConfig.model_validate(snapshot)


def test_default_policy_instances_do_not_share_mutable_permissions():
    first_policy = build_default_policy()
    second_policy = build_default_policy()

    first_policy.permissions.allowed_tools_by_role[AgentRole.CODER].append("example_tool")
    first_policy.budget.max_retries_per_task = 0

    assert first_policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] == []
    assert second_policy.permissions.allowed_tools_by_role[AgentRole.CODER] == []
    assert second_policy.budget.max_retries_per_task == 3
