import pytest
from pydantic import ValidationError

from devflow_ai.planner import NeedsEvidence, PlannerOutput, PlanReady


PLAN = {
    "approach": "Check permission at the shared service boundary",
    "target_paths": ["auth.py"],
    "steps": ["Require permission before modifying the task"],
    "scope_limits": "Keep the API and other services unchanged",
    "verification": ["Check that both request and CLI callers require permission"],
}
EVIDENCE_GAP = {
    "evidence_gap": "Identify which entry points invoke the service and where they check permission",
    "affected_decision": "Choose between the request boundary and the service boundary",
}


@pytest.mark.parametrize(("payload", "expected_type"), [
    ({"outcome": "PLAN_READY", "plan": PLAN}, PlanReady),
    ({"outcome": "NEEDS_EVIDENCE", **EVIDENCE_GAP}, NeedsEvidence),
])
def test_planner_output_uses_outcome_to_select_contract(payload, expected_type):
    result = PlannerOutput.model_validate(payload)
    assert isinstance(result.root, expected_type)
    assert result.model_dump(mode="json") == payload


@pytest.mark.parametrize(("payload", "error_type"), [
    ({"plan": PLAN}, "union_tag_not_found"),
    ({"outcome": "FAILED"}, "union_tag_invalid"),
    ({"outcome": "PLAN_READY"}, "missing"),
    ({"outcome": "PLAN_READY", "plan": {**PLAN, "target_paths": []}}, "too_short"),
    ({"outcome": "NEEDS_EVIDENCE", "evidence_gap": "Find callers"}, "missing"),
    ({"outcome": "NEEDS_EVIDENCE", **EVIDENCE_GAP, "affected_decision": " "}, "string_pattern_mismatch"),
    ({"outcome": "NEEDS_EVIDENCE", **EVIDENCE_GAP, "plan": PLAN}, "extra_forbidden"),
    ({"outcome": "PLAN_READY", "plan": PLAN, **EVIDENCE_GAP}, "extra_forbidden"),
])
def test_planner_output_rejects_invalid_or_mixed_branches(payload, error_type):
    with pytest.raises(ValidationError) as exc_info:
        PlannerOutput.model_validate(payload)
    assert error_type in {error["type"] for error in exc_info.value.errors()}
