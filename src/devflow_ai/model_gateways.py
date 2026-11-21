"""Assemble the model gateways a worker needs, from the environment.

The control plane and any background loop need the same role gateways, so the
assembly lives here once instead of being rebuilt per entry point. Credentials come
from the environment only: ``DEEPSEEK_API_KEY`` and ``DEVFLOW_DEEPSEEK_MODEL``. No
key is ever printed, logged or stored.

``experiment_budget`` is an explicit, optional second ceiling (see
:mod:`devflow_ai.experiment_budget`). Passing one hook wires the **same** experiment
ledger into all five role gateways, so their requests draw on one experiment-wide
budget; omitting it keeps the V1 behaviour exactly (no experiment rows are touched).
Nothing here builds a hook from the environment: a caller that wants the experiment
layer has to construct it and pass it in, which keeps the default entry points
outside an experiment.
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
    Four characters per token is a rough placeholder, not a guaranteed upper bound or a
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


EXPERIMENT_HOOK_METHODS = ("open_request", "mark_dispatched", "settle", "mark_unknown",
                           "release")


def _validate_experiment_budget(experiment_budget) -> None:
    """Refuse anything that is not shaped like one :class:`ExperimentBudgetHook`.

    The assembly checks only that the five methods are callable. It cannot verify the hook's
    experiment identity, candidate, session factory, or behaviour; the caller must supply the
    intended trusted hook.
    """
    if experiment_budget is None:
        return
    missing = [name for name in EXPERIMENT_HOOK_METHODS
               if not callable(getattr(experiment_budget, name, None))]
    if missing:
        raise ValueError(
            "experiment_budget must provide " + ", ".join(missing) + "()")


@contextmanager
def model_gateways_from_env(
    session_factory: sessionmaker[Session], worker_id: str, *,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
    generate: Callable[[str, int], ModelReply] | None = None,
    experiment_budget=None,
):
    """Yield one gateway per model-calling role for one worker, sharing one HTTP client.

    A caller may inject ``client`` or ``generate`` to avoid the network; production
    callers pass neither. Missing credentials raise ``ValueError`` so the CLI can
    report a configuration error without contacting the provider.

    ``experiment_budget`` is optional and defaults to ``None``: one hook, shared by all five
    role gateways, adds an experiment-wide reservation before each request's Run/Policy
    reservation. The hook holds no per-role state, so sharing one instance is what makes the
    five roles use the same configuration. Separate hooks may still share a ledger if they name
    the same experiment; this assembly cannot verify the intended experiment identity.
    """
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-blank string")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) \
            or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    _validate_experiment_budget(experiment_budget)

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
                max_output_tokens=max_output_tokens, experiment_budget=experiment_budget,
            ),
            explorer=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens, experiment_budget=experiment_budget,
            ),
            planner=PlannerModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens, experiment_budget=experiment_budget,
            ),
            coder=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens, experiment_budget=experiment_budget,
            ),
            verifier=ModelGateway(
                session_factory=session_factory, worker_id=worker_id,
                estimate_input_tokens=estimate_prompt_tokens, generate=generate,
                max_output_tokens=max_output_tokens, experiment_budget=experiment_budget,
            ),
        )
    finally:
        if owned_client is not None:
            owned_client.close()
