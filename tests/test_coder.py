from uuid import uuid4

import pytest
from pydantic import ValidationError

from devflow_ai.coder import (
    CoderBlocked, CoderOutput, CoderReadRequest, PatchReady, build_coder_prompt,
)


def test_coder_patch_output_references_an_artifact():
    artifact_id = uuid4()
    result = CoderOutput.model_validate({
        "outcome": "PATCH_READY", "patch_artifact_id": str(artifact_id), "summary": "Adjusted authorization",
    })
    assert isinstance(result.root, PatchReady)
    assert result.root.patch_artifact_id == artifact_id


def test_coder_blocked_output_describes_evidence_and_needed_decision():
    result = CoderOutput.model_validate({
        "outcome": "BLOCKED", "completed_work": "Inspected the plan",
        "blocker": "Another caller uses the affected function",
        "evidence_refs": [{"path": "auth.py", "line": 12}],
        "decision_required": "Decide whether the plan must change",
        "scope_impact": "May require changing another file",
    })
    assert isinstance(result.root, CoderBlocked)
    assert result.root.evidence_refs[0]["path"] == "auth.py"


@pytest.mark.parametrize("payload", [
    {"outcome": "PATCH_READY", "summary": "Done"},
    {"outcome": "PATCH_READY", "patch_artifact_id": str(uuid4()), "summary": " "},
    {
        "outcome": "BLOCKED", "completed_work": "Checked", "blocker": "Missing evidence",
        "evidence_refs": [], "decision_required": "Reassess", "scope_impact": "Unknown",
    },
    {
        "outcome": "BLOCKED", "completed_work": "Checked", "blocker": "Missing evidence",
        "evidence_refs": ["auth.py"], "decision_required": "Reassess", "scope_impact": "Unknown",
        "patch_artifact_id": str(uuid4()),
    },
])
def test_coder_output_rejects_incomplete_or_mixed_outcomes(payload):
    with pytest.raises(ValidationError):
        CoderOutput.model_validate(payload)


def test_a_coder_read_request_can_ask_for_a_line_range():
    """The Coder reads source too, so a large file is reached by a window."""
    request = CoderReadRequest.model_validate(
        {"outcome": "READ_FILE", "path": "core.py", "offset": 2530, "limit": 350})

    assert (request.path, request.offset, request.limit) == ("core.py", 2530, 350)
    # Omitting both keeps the old head-of-file read.
    plain = CoderReadRequest.model_validate({"outcome": "READ_FILE", "path": "core.py"})
    assert (plain.offset, plain.limit) == (None, None)
    with pytest.raises(ValidationError):
        CoderReadRequest.model_validate(
            {"outcome": "READ_FILE", "path": "core.py", "offset": 0})


def test_the_coder_prompt_offers_the_range_instead_of_the_same_head():
    prompt = build_coder_prompt({"issue": {"title": "t"}})

    assert "offset (1-based first line)" in prompt
    assert "instead of the file head again" in prompt
