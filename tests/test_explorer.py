import json
from uuid import uuid4

from devflow_ai.coordinator import ReadFileReceipt
from devflow_ai.enums import AgentExecutionStatus, ToolCallStatus
from devflow_ai.explorer import explore_one_file


def _receipt(*, preview="line 20\nline 21\n", truncated=True):
    return ReadFileReceipt(
        tool_call_id=uuid4(), status=ToolCallStatus.SUCCEEDED, preview=preview,
        truncated=truncated, artifact_ids=(), reason="OUTPUT_TRUNCATED" if truncated else None,
    )


def _model_turns(*outputs):
    turns = iter(outputs)

    def request_model(prompt):
        return next(turns)

    return request_model


def test_the_explorer_can_ask_for_a_line_range_and_it_reaches_the_read():
    """A large file is reached by asking for a window, not by reading the head again."""
    receipt = _receipt()
    calls = []

    def read_file(path, offset=None, limit=None):
        calls.append((path, offset, limit))
        return receipt

    prompts = []

    def request_model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"path": "core.py", "offset": 20, "limit": 5}
        return {"summary": "Option is defined around line 20.",
                "read_evidence": {"path": "core.py", "tool_call_id": str(receipt.tool_call_id)}}

    result = explore_one_file({"issue": {"title": "t"}}, read_file, request_model)

    assert calls == [("core.py", 20, 5)]
    assert result.status == AgentExecutionStatus.SUCCEEDED
    # The summarising turn is told which window it is looking at and that it is partial.
    assert '"offset": 20' in prompts[1] and '"limit": 5' in prompts[1]
    assert '"truncated": true' in prompts[1]
    # And the instruction offers the range option rather than only "pick a path".
    assert "offset" in prompts[0] and "limit" in prompts[0]
    # It is also told which ranges are already covered, so it can ask for a different one.
    assert "covered line ranges" in prompts[0]


def test_a_read_without_a_range_still_works():
    receipt = _receipt(preview="def f():\n", truncated=False)
    calls = []

    def read_file(path, offset=None, limit=None):
        calls.append((path, offset, limit))
        return receipt

    result = explore_one_file(
        {"issue": {"title": "t"}}, read_file,
        _model_turns(
            {"path": "core.py"},
            {"summary": "Small file.",
             "read_evidence": {"path": "core.py", "tool_call_id": str(receipt.tool_call_id)}}))

    assert calls == [("core.py", None, None)]
    assert result.status == AgentExecutionStatus.SUCCEEDED


def test_an_invalid_range_is_rejected_before_any_read():
    calls = []

    def read_file(path, offset=None, limit=None):
        calls.append((path, offset, limit))
        return _receipt()

    result = explore_one_file(
        {"issue": {"title": "t"}}, read_file,
        _model_turns({"path": "core.py", "offset": 0}))

    assert result.status == AgentExecutionStatus.FAILED
    assert result.output == {"reason": "EXPLORER_OUTPUT_INVALID"}
    assert calls == []


def test_the_explorer_prompt_shows_the_request_schema_with_the_optional_window():
    prompts = []

    def request_model(prompt):
        prompts.append(prompt)
        return {"path": "core.py", "offset": 1, "limit": 1}

    explore_one_file({"issue": {"title": "t"}}, lambda *a, **k: _receipt(), request_model)

    schema = prompts[0].split("Output schema JSON:\n", 1)[1].split("\nInput data JSON:", 1)[0]
    payload = json.loads(schema)
    assert {"path", "offset", "limit"} <= set(payload["properties"])


def test_the_summarising_turn_sees_the_window_that_was_actually_read():
    """The request says what was asked for; the receipt says what arrived."""
    receipt = ReadFileReceipt(
        tool_call_id=uuid4(), status=ToolCallStatus.SUCCEEDED, preview="line 2530\n",
        truncated=True, artifact_ids=(), reason="OUTPUT_TRUNCATED",
        requested_offset=2530, requested_limit=350,
        start_line=2530, end_line=2879, total_lines=5000, has_more=True,
        byte_truncated=False, partial_line=False,
    )
    prompts = []

    def request_model(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"path": "core.py", "offset": 2530, "limit": 350}
        return {"summary": "Option is defined at line 2641.",
                "read_evidence": {"path": "core.py", "tool_call_id": str(receipt.tool_call_id)}}

    explore_one_file({"issue": {"title": "t"}}, lambda *a, **k: receipt, request_model)

    context = json.loads(prompts[1].split("Input data JSON:\n", 1)[1])["read"]
    assert (context["offset"], context["limit"]) == (2530, 350)
    assert (context["start_line"], context["end_line"], context["total_lines"]) == (2530, 2879, 5000)
    assert (context["has_more"], context["byte_truncated"], context["partial_line"]) == (
        True, False, False)
