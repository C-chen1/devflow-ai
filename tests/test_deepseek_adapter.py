import httpx
import pytest

from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter, DeepSeekResponseError


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 12}, {"prompt_tokens": 12, "completion_tokens": -1}])
def test_deepseek_preserves_output_when_usage_cannot_be_settled(monkeypatch, usage):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "configured-model")

    def respond(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}], "usage": usage})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=5)
        reply = adapter("Generate JSON.", 10)

    assert reply.output == "   "
    assert reply.usage is None


@pytest.mark.parametrize(("status_code", "error"), [(401, httpx.HTTPStatusError), (200, DeepSeekResponseError)])
def test_deepseek_transport_errors_do_not_look_like_planner_output(status_code, error):
    transport = httpx.MockTransport(lambda _: httpx.Response(status_code, json={"choices": []}))
    with httpx.Client(transport=transport) as client:
        adapter = DeepSeekPlannerAdapter(client, api_key="test-key", model="configured-model", timeout_seconds=5)
        with pytest.raises(error):
            adapter("Generate JSON.", 10)
