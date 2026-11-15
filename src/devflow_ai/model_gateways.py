"""Assemble the model gateways a worker needs, from the environment.

The control plane and any background loop need the same three gateways, so the
assembly lives here once instead of being rebuilt per entry point. Credentials come
from the environment only: ``DEEPSEEK_API_KEY`` and ``DEVFLOW_DEEPSEEK_MODEL``. No
key is ever printed, logged or stored.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from typing import Callable

import httpx
from sqlalchemy.orm import Session, sessionmaker

from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter
from devflow_ai.model_gateway import ModelGateway, ModelReply, PlannerModelGateway

DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TIMEOUT_SECONDS = 60.0


def estimate_prompt_tokens(prompt: str) -> int:
    """Return a rough prompt size for budget reservation.

    This only reserves budget; the provider's reported usage settles the request.
    Four characters per token is a deliberately conservative placeholder, not a
    claim about the model's tokenizer.
    """
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    return max(1, (len(prompt) + 3) // 4)


@dataclass(frozen=True)
class ModelGateways:
    """One gateway per role that calls a model.

    The wrappers are role-agnostic, but the graph asks for them by role. Sharing one
    wrapper across roles would be indistinguishable in code and misleading in review, so
    each role gets its own named gateway over the same adapter.
    """

    supervisor: ModelGateway
    explorer: ModelGateway
    planner: PlannerModelGateway
    coder: ModelGateway
    verifier: ModelGateway


@contextmanager
def model_gateways_from_env(
    session_factory: sessionmaker[Session], worker_id: str, *,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
    generate: Callable[[str, int], ModelReply] | None = None,
):
    """Yield the three gateways for one worker, sharing one HTTP client.

    A caller may inject ``client`` or ``generate`` to avoid the network; production
    callers pass neither. Missing credentials raise ``ValueError`` so the CLI can
    report a configuration error without contacting the provider.
    """
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-blank string")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) \
            or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")

    owned_client = None
    try:
        if generate is None:
            if client is None:
                owned_client = httpx.Client()
                client = owned_client
            generate = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=timeout_seconds)
        yield ModelGateways(
            supervisor=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens,
            ),
            explorer=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens,
            ),
            planner=PlannerModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens,
            ),
            coder=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens,
            ),
            verifier=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens,
            ),
        )
    finally:
        if owned_client is not None:
            owned_client.close()
