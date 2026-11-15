import pytest
from pydantic import ValidationError

from devflow_ai.planner import NeedsEvidence, PlannerOutput, PlanReady, build_planner_prompt


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


def test_the_planner_prompt_separates_a_tool_truncation_from_a_clipped_prompt():
    """A complete read is not the same thing as "the Planner has seen the whole file"."""
    prompt = build_planner_prompt({"issue": {"title": "t", "description": "d"}, "read_evidence": []})

    assert "`truncated` means the tool cut the read" in prompt
    assert "`preview_clipped` means the content actually placed in this prompt was cut" in prompt
    assert "A complete tool read does not mean you have seen all of it" in prompt
    # And the way out is an explicit evidence request, not a guess.
    assert "NEEDS_EVIDENCE with the gap and the affected decision" in prompt
    # A gap inside a file is named as lines, not as "send the whole file again".
    assert "name the file and the lines you need" in prompt
