"""Second budget layer for model requests: the experiment-wide ceiling.

The per-Run budget (``ModelGateway``), the Task admission budget (``AdmissionModelGateway``)
and this layer are three different ceilings, and this module only adds the outermost one. It is
**opt-in**: a gateway constructed without a hook behaves exactly as before, and nothing in the
default wiring uses it yet.

What it guarantees, and how:

* a request is issued only after **both** reservations succeeded — this layer first, then the
  Run/admission layer; if either refuses, no call is made;
* the experiment reservation is taken before the inner one and given back **only** when no call
  could have happened (the inner layer refused, or reserving it raised);
* just before the provider call the reservation is marked ``dispatched``; from that point the
  accounting refuses to release it, so a crash, a failed settlement or a failed unknown-marking
  leaves a request that *may* have been sent, never a free slot;
* once a call was attempted, the reservation is never released: settlement records the measured
  usage, and missing or unknown usage ends as ``unknown`` — never as zero — which in turn stops
  further reservations in that experiment;
* the two layers are separate transactions, so this is not one atomic reservation. A process
  that dies between them leaves the experiment amount reserved with no call. A process that dies
  after the call but before settlement also leaves it reserved and accounting incomplete. Actual
  usage can exceed estimates, so a held reservation does not guarantee a hard spend limit.

What it does not do: measure usage itself (the provider's numbers are recorded as reported),
cover model calls that are not routed through a gateway, or make a candidate's execution
version verifiable. A request key is created per logical request, so one reservation cannot be
reused for a second call; there is no automatic retry of an external call.
"""
from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from devflow_ai.experiment import (
    ExperimentLimitError, ExperimentService,
)


class TokenUsage(Protocol):
    """What the gateway reports: the provider's input and output counts."""

    input_tokens: int
    output_tokens: int


class ExperimentBudgetHook:
    """Reserve, settle, and end one experiment-level reservation per model request.

    Each method opens its own transaction: the hook is called between phases of a request and
    must not hold a session across the provider call.
    """

    def __init__(
        self, *, session_factory: sessionmaker[Session], experiment_id: UUID,
        candidate_label: str | None = None, operator: str = "experiment-budget",
    ):
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must be a non-blank string")
        self.session_factory = session_factory
        self.experiment_id = experiment_id
        self.candidate_label = candidate_label
        self.operator = operator

    def open_request(
        self, *, estimated_tokens: int, run_id: UUID | None = None,
        agent_execution_id: UUID | None = None,
    ) -> UUID | None:
        """Take the experiment reservation, or say the experiment refuses this request.

        ``None`` means "do not call the model": the ceiling is reached, the deadline passed,
        or the accounting already holds usage nobody could quantify.
        """
        if type(estimated_tokens) is not int or estimated_tokens < 0:
            raise ValueError("estimated_tokens must be a non-negative integer")
        request_key = uuid4()
        with self.session_factory() as session:
            try:
                ExperimentService(session).reserve(
                    experiment_id=self.experiment_id, request_key=request_key,
                    tokens=max(1, estimated_tokens), candidate_label=self.candidate_label,
                    run_id=run_id, agent_execution_id=agent_execution_id)
                session.commit()
            except ExperimentLimitError:
                session.rollback()
                return None
        return request_key

    def mark_dispatched(self, request_key: UUID) -> None:
        """Move the reservation past the point of no return, before the provider call.

        After this the reservation can only be settled or ended as unknown; the accounting
        refuses to release a request that may already have been sent.
        """
        with self.session_factory() as session:
            ExperimentService(session).mark_dispatched(
                experiment_id=self.experiment_id, request_key=request_key)
            session.commit()

    def settle(self, request_key: UUID, usage: TokenUsage) -> None:
        actual = int(usage.input_tokens) + int(usage.output_tokens)
        with self.session_factory() as session:
            ExperimentService(session).settle(
                experiment_id=self.experiment_id, request_key=request_key,
                actual_tokens=max(0, actual))
            session.commit()

    def mark_unknown(self, request_key: UUID, reason: str) -> None:
        """The call may have happened and its usage cannot be quantified: keep it unknown."""
        with self.session_factory() as session:
            ExperimentService(session).settle_unknown(
                experiment_id=self.experiment_id, request_key=request_key, reason=reason,
                recorded_by=self.operator)
            session.commit()

    def release(self, request_key: UUID, reason: str) -> None:
        """Give the reservation back: only for a request that could not have been sent."""
        with self.session_factory() as session:
            ExperimentService(session).release(
                experiment_id=self.experiment_id, request_key=request_key, reason=reason,
                confirmed_by=self.operator)
            session.commit()
