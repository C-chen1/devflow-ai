"""可信侧实验预算通道（原型）：Worker 只发请求，账目由控制端会话写入。

The problem this prototype addresses: the existing ``ExperimentBudgetHook`` writes the experiment
ledger from whichever process calls it. A Worker identity that is denied the experiment tables
(the boundary verified in ``tests/test_experiment_db_privileges.py``) therefore cannot budget at
all — ``open_request`` is refused at ``experiments`` before anything else.

This module splits the hook's five methods across a request/response boundary:

* :class:`TrustedBudgetClient` is what a Worker-side gateway gets. It has the same five methods,
  holds **no** database session and cannot read or write the ledger. It only sends messages.
* :class:`TrustedBudgetServer` runs wherever the control-plane credentials are. It fixes the
  experiment, the candidate label and the operator when it is constructed, and every decision is
  delegated to the existing ``ExperimentService`` state machine. Candidate ownership is checked
  under the same experiment and reservation locks as each subsequent update.

What this is and is not:

* it is **in-process by default** (``send`` is just a callable). No network service, no RPC
  framework, and nothing in ``worker_run``, the scheduler or ``benchmark_run`` constructs one;
* it is **not authenticated**. The server fixes the experiment and candidate, so a caller cannot
  book against another experiment by claiming an ID, and the service still checks that the Run and
  AgentExecution belong to the experiment's declared Task. But nothing here proves *who* the caller
  is: a process that can reach the server can ask it to reserve. The real protection is that the
  caller has no database credentials of its own;
* idempotent accounting is **not** exactly-once model calling. A stable request identity stops a
  lost reservation response from booking twice, but a Worker that crashes after sending and
  retries with the same key may call the provider again; the ledger will show one request, and the
  usage is whatever the provider reports;
* it does not limit a caller that bypasses the gateway and calls the provider directly.

Conservative failure rules, matching the existing state machine:

* an unclear reservation answer means **do not call**;
* an unclear dispatch answer means **do not call** — the reservation may be ``reserved`` or
  ``dispatched`` on the server, and either way the caller must not assume it is free;
* after dispatch, a failed call or unknown usage ends as ``unknown``; a failed *recording* leaves a
  dispatched, unsettled reservation that blocks the next request;
* ``release`` is only for requests that provably never left the process; the service refuses it
  after dispatch and the client surfaces that refusal.
"""
from __future__ import annotations

import json
import os
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path
from typing import Callable, Mapping, Protocol
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.orm import Session, sessionmaker

from devflow_ai.experiment import ExperimentConflictError, ExperimentLimitError, ExperimentService

REQUEST_NAMESPACE = uuid5(NAMESPACE_URL, "devflow-ai/experiment-budget-request")
# One retry is enough to survive a lost response; the operations are idempotent by request key.
DEFAULT_ATTEMPTS = 2


class BudgetChannelError(RuntimeError):
    """The trusted side did not give an answer that the caller may act on."""


class BudgetTransport(Protocol):
    def __call__(self, message: Mapping[str, object]) -> Mapping[str, object]: ...


class TrustedBudgetServer:
    """Control-plane side: fixed experiment, candidate and operator; writes the ledger itself."""

    def __init__(self, *, session_factory: sessionmaker[Session], experiment_id: UUID,
                 candidate_label: str | None = None, operator: str = "experiment-budget"):
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must be a non-blank string")
        self.session_factory = session_factory
        self.experiment_id = experiment_id
        self.candidate_label = candidate_label
        self.operator = operator

    def request_key_for(self, client_key: UUID) -> UUID:
        """The stable server-side identity of one logical request.

        Derived, not generated: a retried reservation message names the same request key, and
        ``ExperimentService.reserve`` is idempotent by that key, so a lost response cannot book a
        second reservation.
        """
        if not isinstance(client_key, UUID):
            raise ValueError("client_key must be a UUID")
        return uuid5(REQUEST_NAMESPACE, f"{self.experiment_id}:{client_key}")

    def handle(self, message: Mapping[str, object]) -> dict:
        """One request/response exchange; never raises for a caller-caused refusal."""
        try:
            operation = message.get("op")
            if operation == "reserve":
                return self._reserve(message)
            if operation in {"dispatch", "settle", "unknown", "release"}:
                self._apply_to_candidate(operation, message)
                return {"status": "ok"}
            return {"status": "error", "kind": "unknown_operation"}
        except ExperimentLimitError as error:
            return {"status": "refused", "reason": str(error)[:300]}
        except ExperimentConflictError as error:
            return {"status": "conflict", "reason": str(error)[:300]}
        except Exception as error:  # database refusals, state errors, anything else
            return {"status": "error", "kind": type(error).__name__}

    def _reserve(self, message: Mapping[str, object]) -> dict:
        client_key = message.get("client_key")
        if not isinstance(client_key, str):
            return {"status": "error", "kind": "client_key_missing"}
        try:
            request_key = self.request_key_for(UUID(client_key))
            run_id = _optional_uuid(message.get("run_id"))
            execution_id = _optional_uuid(message.get("agent_execution_id"))
            tokens = int(message.get("tokens", 1))
        except (TypeError, ValueError):
            return {"status": "error", "kind": "invalid_request"}
        with self.session_factory() as session:
            ExperimentService(session).reserve(
                experiment_id=self.experiment_id, request_key=request_key,
                tokens=max(1, tokens),
                candidate_label=self.candidate_label, run_id=run_id,
                agent_execution_id=execution_id)
            session.commit()
        return {"status": "granted", "request_key": str(request_key)}

    def _apply_to_candidate(self, operation: str, message: Mapping[str, object]) -> None:
        """Check candidate ownership while holding the same locks as the ledger update."""
        key = self._key(message)
        with self.session_factory() as session:
            service = ExperimentService(session)
            experiment = service._lock_experiment(self.experiment_id)
            reservation = service._lock_reservation(experiment, key)
            candidate_id = (None if self.candidate_label is None else
                            service._candidate(experiment, self.candidate_label).id)
            if reservation.candidate_id != candidate_id:
                raise ExperimentConflictError("Reservation belongs to a different candidate")
            if operation == "dispatch":
                service.mark_dispatched(experiment_id=experiment.id, request_key=key)
            elif operation == "settle":
                actual = int(message.get("input_tokens", 0)) + int(message.get("output_tokens", 0))
                service.settle(experiment_id=experiment.id, request_key=key,
                               actual_tokens=max(0, actual))
            elif operation == "unknown":
                service.settle_unknown(experiment_id=experiment.id, request_key=key,
                                       reason=str(message.get("reason", ""))[:200],
                                       recorded_by=self.operator)
            else:
                service.release(experiment_id=experiment.id, request_key=key,
                                reason=str(message.get("reason", ""))[:200],
                                confirmed_by=self.operator)
            session.commit()

    @staticmethod
    def _key(message: Mapping[str, object]) -> UUID:
        return UUID(str(message["request_key"]))


def _optional_uuid(value) -> UUID | None:
    return None if value in (None, "") else UUID(str(value))


class TrustedBudgetClient:
    """Worker side: the hook's five methods, no database session, no ledger access."""

    def __init__(self, *, send: BudgetTransport, attempts: int = DEFAULT_ATTEMPTS):
        if not callable(send):
            raise ValueError("send must be callable")
        if type(attempts) is not int or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        self._send = send
        self._attempts = attempts

    # The interface ``ModelGateway``/``PlannerModelGateway`` expect from an experiment budget.
    def open_request(self, *, estimated_tokens: int, run_id: UUID | None = None,
                     agent_execution_id: UUID | None = None) -> UUID | None:
        client_key = uuid4()
        response = self._exchange({
            "op": "reserve", "client_key": str(client_key),
            "tokens": int(estimated_tokens),
            "run_id": None if run_id is None else str(run_id),
            "agent_execution_id": (None if agent_execution_id is None
                                   else str(agent_execution_id)),
        })
        if response is None or response.get("status") != "granted":
            # Unclear, refused or broken: the caller must not send anything.
            return None
        return UUID(str(response["request_key"]))

    def mark_dispatched(self, request_key: UUID) -> None:
        self._require_ok({"op": "dispatch", "request_key": str(request_key)})

    def settle(self, request_key: UUID, usage) -> None:
        self._require_ok({"op": "settle", "request_key": str(request_key),
                          "input_tokens": int(usage.input_tokens),
                          "output_tokens": int(usage.output_tokens)})

    def mark_unknown(self, request_key: UUID, reason: str) -> None:
        self._require_ok({"op": "unknown", "request_key": str(request_key), "reason": reason})

    def release(self, request_key: UUID, reason: str) -> None:
        # The service refuses this after dispatch; that refusal is surfaced, not swallowed.
        self._require_ok({"op": "release", "request_key": str(request_key), "reason": reason})

    def _require_ok(self, message: Mapping[str, object]) -> None:
        response = self._exchange(message)
        if response is None:
            raise BudgetChannelError("The trusted budget side did not answer")
        if response.get("status") != "ok":
            raise BudgetChannelError(
                f"The trusted budget side refused: {response.get('status')}")

    def _exchange(self, message: Mapping[str, object]) -> Mapping[str, object] | None:
        """Retry the same message; ``None`` means no usable answer was obtained."""
        for _attempt in range(self._attempts):
            try:
                return self._send(message)
            except Exception:  # transport failure: unclear, never "refused"
                continue
        return None


# -- a local, single-session transport ------------------------------------------------------
#
# The in-process ``send`` callable above is enough to test the protocol, but it puts the Worker
# and the trusted side in one interpreter. These helpers give the same protocol one local
# connection: a Unix socket (or a Windows named pipe) whose address lives in a temporary
# directory, with ``multiprocessing.connection``'s keyed handshake as the session credential.
#
# What this gives: the two sides are separate processes, the trusted side never hands its database
# session (or its credentials) to the other one, and a client without the session key cannot talk
# to the listener at all. What it does not give: any defence against another process running as
# the same user that can read the key or connect to the socket, transport encryption, replay
# protection beyond the keyed handshake, or a sandbox. The key is a shared secret, nothing more.

LOCAL_FAMILY = "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def session_address(directory: Path | str, *, prefix: str = "devflow-budget") -> str:
    """A fresh local endpoint name; the directory should be private to this session."""
    token = uuid4().hex[:12]
    if LOCAL_FAMILY == "AF_PIPE":
        return rf"\\.\pipe\{prefix}-{token}"
    return str(Path(directory) / f"{prefix}-{token}.sock")


MAX_MESSAGE_BYTES = 65536


def _send_json(connection, payload: Mapping[str, object]) -> None:
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise BudgetChannelError("Budget message is too large")
    connection.send_bytes(data)


def _receive_json(connection) -> dict:
    try:
        raw = connection.recv_bytes(MAX_MESSAGE_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise BudgetChannelError("Malformed budget message") from error
    if not isinstance(payload, dict):
        raise BudgetChannelError("Budget message must be an object")
    return payload


def connection_transport(connection, *, timeout_seconds: float = 5.0) -> BudgetTransport:
    """Turn one connection into the ``send`` callable ``TrustedBudgetClient`` expects.

    A malformed answer raises, so the client treats it as an unclear answer and refuses to let
    the caller send anything.
    """

    def send(message: Mapping[str, object]) -> Mapping[str, object]:
        try:
            _send_json(connection, {"request": dict(message)})
            if not connection.poll(timeout_seconds):
                raise BudgetChannelError("The trusted budget side did not answer in time")
            reply = _receive_json(connection)
            if not isinstance(reply.get("status"), str):
                raise BudgetChannelError("Malformed answer from the trusted side")
            return reply
        except Exception:
            # A late answer cannot safely be associated with a later request on this stream.
            connection.close()
            raise

    return send


def connect_session(*, address: str, authkey: bytes, family: str = LOCAL_FAMILY):
    """Worker side: open the local session, or raise ``AuthenticationError`` for a wrong key."""
    if not isinstance(authkey, (bytes, bytearray)) or not authkey:
        raise ValueError("authkey must be non-empty bytes")
    return Client(address, family=family, authkey=bytes(authkey))


class LocalBudgetSession:
    """Control-plane side: serve exactly one bound session over one local connection.

    The experiment and candidate come from the ``TrustedBudgetServer`` this wraps; the client
    cannot name them, and the listener only exists while this context manager is open.
    """

    def __init__(self, *, server: TrustedBudgetServer, address: str, authkey: bytes,
                 family: str = LOCAL_FAMILY):
        if not isinstance(authkey, (bytes, bytearray)) or not authkey:
            raise ValueError("authkey must be non-empty bytes")
        self.server = server
        self.address = address
        self.authkey = bytes(authkey)
        self.family = family
        self._listener: Listener | None = None

    def __enter__(self) -> "LocalBudgetSession":
        self._listener = Listener(self.address, family=self.family, authkey=self.authkey)
        return self

    def __exit__(self, *_exc) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def serve_one(self) -> int:
        """Accept one client and answer until it disconnects; returns the message count.

        A client with the wrong key never reaches ``handle``: the keyed handshake refuses it and
        the error surfaces here, so an unauthenticated caller cannot book anything.
        """
        if self._listener is None:
            raise RuntimeError("The session is not open")
        connection = self._listener.accept()
        messages = 0
        try:
            while True:
                try:
                    envelope = _receive_json(connection)
                except (EOFError, OSError, BudgetChannelError):
                    break
                messages += 1
                request = envelope.get("request") if isinstance(envelope, dict) else None
                if not isinstance(request, dict):
                    _send_json(connection, {"status": "error", "kind": "malformed_request"})
                    continue
                _send_json(connection, self.server.handle(request))
        finally:
            connection.close()
        return messages
