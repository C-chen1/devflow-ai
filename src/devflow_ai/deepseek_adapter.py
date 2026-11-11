import os
import httpx
from pydantic import ValidationError
from devflow_ai.coordinator import ModelTokenUsage
from devflow_ai.model_gateway import ModelReply
from math import isfinite


class DeepSeekResponseError(RuntimeError):
    """The HTTP response did not contain a usable completion envelope."""


class DeepSeekPlannerAdapter:
    """Send one JSON-mode completion through a caller-owned HTTP client."""

    def __init__(self, client: httpx.Client, *, api_key: str, model: str, timeout_seconds: float):
        if not api_key.strip() or not model.strip():
            raise ValueError("DeepSeek API key and model must be provided")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("DeepSeek timeout must be finite and positive")
        self.client = client
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls, client: httpx.Client, *, timeout_seconds: float) -> "DeepSeekPlannerAdapter":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        model = os.environ.get("DEVFLOW_DEEPSEEK_MODEL", "").strip()
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek adapter")
        if not model:
            raise ValueError("DEVFLOW_DEEPSEEK_MODEL is required for the DeepSeek adapter")
        return cls(
            client, api_key=api_key, model=model, timeout_seconds=timeout_seconds,
        )

    def __call__(self, prompt: str, max_output_tokens: int) -> ModelReply:
        if "json" not in prompt.lower():
            raise ValueError("DeepSeek JSON mode requires the prompt to mention JSON")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")

        response = self.client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "max_tokens": max_output_tokens,
                "thinking": {"type": "disabled"},
                "stream": False,
            },
            timeout=self.timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, TypeError, KeyError, IndexError):
            raise DeepSeekResponseError("Invalid DeepSeek completion envelope") from None
        if content is not None and not isinstance(content, str):
            raise DeepSeekResponseError("Invalid DeepSeek completion content")

        usage = payload.get("usage")
        actual_usage = None
        if isinstance(usage, dict):
            try:
                actual_usage = ModelTokenUsage(
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                )
            except ValidationError:
                pass  # Incomplete usage leaves the original reservation in place.

        return ModelReply(output=content if content is not None else "", usage=actual_usage)
