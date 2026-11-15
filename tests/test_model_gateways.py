import pytest

from devflow_ai.model_gateway import ModelGateway, PlannerModelGateway
from devflow_ai.model_gateways import (
    DEFAULT_MAX_OUTPUT_TOKENS, estimate_prompt_tokens, model_gateways_from_env,
)


def _unused_generate(prompt, max_output_tokens):
    raise AssertionError("gateway assembly must not call the model")


def test_gateways_require_credentials(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEVFLOW_DEEPSEEK_MODEL", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        with model_gateways_from_env(None, "worker-1"):
            pass


def test_gateways_require_a_model_name(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("DEVFLOW_DEEPSEEK_MODEL", raising=False)
    with pytest.raises(ValueError, match="DEVFLOW_DEEPSEEK_MODEL"):
        with model_gateways_from_env(None, "worker-1"):
            pass


def test_gateways_are_built_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-chat")
    with model_gateways_from_env(None, "worker-1") as gateways:
        assert isinstance(gateways.planner, PlannerModelGateway)
        for role in ("supervisor", "explorer", "coder", "verifier"):
            gateway = getattr(gateways, role)
            assert isinstance(gateway, ModelGateway)
            assert gateway.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
            assert gateway.estimate_input_tokens is estimate_prompt_tokens
            assert gateway.worker_id == "worker-1"
        assert gateways.planner.worker_id == "worker-1"


def test_each_role_gets_its_own_gateway(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEVFLOW_DEEPSEEK_MODEL", "deepseek-chat")
    with model_gateways_from_env(None, "worker-1") as gateways:
        wrappers = [gateways.supervisor, gateways.explorer, gateways.coder,
                    gateways.verifier, gateways.planner]
    assert len({id(item) for item in wrappers}) == len(wrappers)


def test_gateways_accept_an_injected_generator():
    with model_gateways_from_env(None, "worker-1", generate=_unused_generate) as gateways:
        assert gateways.supervisor.generate is _unused_generate
        assert gateways.planner.generate is _unused_generate


def test_gateway_assembly_validates_its_inputs():
    with pytest.raises(ValueError, match="worker_id"):
        with model_gateways_from_env(None, " "):
            pass
    with pytest.raises(ValueError, match="max_output_tokens"):
        with model_gateways_from_env(None, "worker-1", max_output_tokens=0):
            pass
    with pytest.raises(ValueError, match="timeout_seconds"):
        with model_gateways_from_env(None, "worker-1", timeout_seconds=0):
            pass


def test_prompt_estimate_is_positive_and_grows_with_the_prompt():
    assert estimate_prompt_tokens("a") == 1
    assert estimate_prompt_tokens("abcde") == 2
    assert estimate_prompt_tokens("a" * 400) == 100
    with pytest.raises(ValueError):
        estimate_prompt_tokens("")
