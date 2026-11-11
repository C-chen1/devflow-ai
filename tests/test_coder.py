from uuid import uuid4

import pytest
from pydantic import ValidationError

from devflow_ai.coder import CoderBlocked, CoderOutput, PatchReady


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
