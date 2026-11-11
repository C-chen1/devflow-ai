"""One explicit admission attempt for a caller-approved trusted repository."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import argparse
import json
import sys
from typing import Callable, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from devflow_ai.enums import TaskStatus
from devflow_ai.admission_claim import (
    AdmissionClaim, AdmissionClaimDeniedError, AdmissionClaimService, assess_recorded_timeout_limit,
)
from devflow_ai.harness import RepositoryHeadObservation, observe_repository_head
from devflow_ai.model_gateway import AdmissionModelGateway
from devflow_ai.admission_intervention import record_reached_limits
from devflow_ai.models import Task
from devflow_ai.policy import PolicyConfig
from devflow_ai.services import AdmissionRecord, TaskNotFoundError, TaskService, TaskStateConflictError
from devflow_ai.supervisor import (
    AdmissionDecision, AdmissionObservation, build_admission_supervisor_prompt, resolve_admission_evidence,
)


@dataclass(frozen=True)
class AdmissionAttemptResult:
    outcome: str
    reason: str


@dataclass(frozen=True)
class AdmissionRetryReadiness:
    status: str
    retry_at: datetime | None = None


def inspect_timeout_retry(task_id: UUID, *, session_factory: sessionmaker[Session]) -> AdmissionRetryReadiness:
    """Inspect retry timing under the Task lock; a receipt is not execution authorization."""
    with session_factory() as session, session.begin():
        task = session.scalar(select(Task).where(Task.id == task_id).with_for_update()
                              .execution_options(populate_existing=True))
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        if task.status != TaskStatus.PENDING:
            return AdmissionRetryReadiness("NOT_PENDING")
        now = session.scalar(select(func.clock_timestamp()))
        if task.admission_claim_token is not None and task.admission_claim_expires_at > now:
            return AdmissionRetryReadiness("IN_PROGRESS")
        budget = PolicyConfig.model_validate(task.policy_snapshot).budget
        if budget.max_admission_attempts is None or task.admission_attempt_count is None:
            return AdmissionRetryReadiness("UNKNOWN")
        if task.admission_attempt_count >= budget.max_admission_attempts:
            return AdmissionRetryReadiness("ATTEMPT_LIMIT_REACHED")
        limit_status = assess_recorded_timeout_limit(task.admission_blocked, budget)
        if limit_status != "BELOW_LIMIT":
            return AdmissionRetryReadiness(limit_status)
        delay = budget.admission_timeout_retry_delay_seconds
        if delay is None or task.admission_blocked_at is None:
            return AdmissionRetryReadiness("UNKNOWN")
        retry_at = task.admission_blocked_at + timedelta(seconds=delay)
        return AdmissionRetryReadiness("WAIT" if now < retry_at else "READY", retry_at)


def _record_blocked(task_id: UUID, *, session_factory: sessionmaker[Session],
                    kind: str, reason: str, missing_information: str | None = None,
                    retry_hint: Literal["transient", "needs_review"] = "needs_review",
                    claim: AdmissionClaim) -> AdmissionAttemptResult:
    """Only write a blocked result while the Task is still awaiting admission."""
    with session_factory() as session, session.begin():
        if claim.task_id != task_id:
            raise AdmissionClaimDeniedError("Admission claim belongs to a different Task")
        task = AdmissionClaimService(session).assert_claim(claim)
        previous = task.admission_blocked
        if retry_hint == "transient":
            if previous is None or previous.get("retry_hint") != "transient":
                count = 1
            else:
                previous_count = previous.get("consecutive_timeouts")
                count = previous_count + 1 if type(previous_count) is int and previous_count >= 0 else None
        else:
            count = 0
        result = session.execute(update(Task).where(
            Task.id == task_id, Task.status == TaskStatus.PENDING, Task.admission_claim_token == claim.token,
            Task.admission_claim_owner == claim.owner, Task.admission_claim_expires_at > func.clock_timestamp(),
        ).values(admission_blocked={"kind": kind, "reason": reason, "missing_information": missing_information,
                                    "retry_hint": retry_hint, "consecutive_timeouts": count},
                 admission_blocked_at=func.clock_timestamp()))
        if result.rowcount != 1:
            raise AdmissionClaimDeniedError("Admission claim expired before blocked result was saved")
        session.refresh(task)
        record_reached_limits(task, session.scalar(select(func.clock_timestamp())), current_attempt_finished=True)
    return AdmissionAttemptResult("BLOCKED", reason)


def attempt_admission(
    task_id: UUID, *, session_factory: sessionmaker[Session], gateway: AdmissionModelGateway,
    trusted_repository: Callable[[str], bool], timeout_seconds: int,
    observe: Callable[..., RepositoryHeadObservation] = observe_repository_head,
) -> AdmissionAttemptResult:
    """Claim and attempt one Task; callers establish repository trust."""
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    with session_factory() as session:
        claim = AdmissionClaimService(session).claim(
            task_id, owner=f"admission-{uuid4()}", lease_seconds=max(90, timeout_seconds + 60),
        )
    if claim is None:
        raise AdmissionClaimDeniedError("Task cannot be claimed for admission")
    return _run_with_claim(task_id, session_factory=session_factory, gateway=gateway,
                           trusted_repository=trusted_repository, timeout_seconds=timeout_seconds,
                           observe=observe, claim=claim)


def retry_timed_out_admission(
    task_id: UUID, *, session_factory: sessionmaker[Session], gateway: AdmissionModelGateway,
    trusted_repository: Callable[[str], bool], timeout_seconds: int,
    observe: Callable[..., RepositoryHeadObservation] = observe_repository_head,
) -> AdmissionAttemptResult | None:
    """Run one eligible timed-out retry, or return None when it cannot be claimed."""
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    with session_factory() as session:
        claim = AdmissionClaimService(session).claim(
            task_id, owner=f"admission-retry-{uuid4()}", lease_seconds=max(90, timeout_seconds + 60),
            automatic_retry=True,
        )
    if claim is None:
        return None
    return _run_with_claim(task_id, session_factory=session_factory, gateway=gateway,
                           trusted_repository=trusted_repository, timeout_seconds=timeout_seconds,
                           observe=observe, claim=claim)


def _run_with_claim(task_id: UUID, *, session_factory: sessionmaker[Session], gateway: AdmissionModelGateway,
                    trusted_repository: Callable[[str], bool], timeout_seconds: int,
                    observe: Callable[..., RepositoryHeadObservation], claim: AdmissionClaim) -> AdmissionAttemptResult:
    try:
        return _attempt_claimed(task_id, session_factory=session_factory, gateway=gateway,
                                trusted_repository=trusted_repository, timeout_seconds=timeout_seconds,
                                observe=observe, claim=claim)
    except Exception as error:
        if not isinstance(error, AdmissionClaimDeniedError):
            try:
                _record_blocked(task_id, session_factory=session_factory, kind="execution_error",
                                reason=f"Admission execution error: {type(error).__name__}", claim=claim)
            except Exception:
                pass  # Preserve the original failure; an expired claim must not write a result.
        raise
    finally:
        try:
            with session_factory() as session:
                AdmissionClaimService(session).release(claim)
        except AdmissionClaimDeniedError:
            pass  # Completion, expiry, or replacement must not clear another owner's claim.


def _attempt_claimed(task_id: UUID, *, session_factory: sessionmaker[Session], gateway: AdmissionModelGateway,
                     trusted_repository: Callable[[str], bool], timeout_seconds: int,
                     observe: Callable[..., RepositoryHeadObservation], claim: AdmissionClaim) -> AdmissionAttemptResult:
    with session_factory() as session:
        with session.begin():
            task = AdmissionClaimService(session).assert_claim(claim)
            issue = {"title": task.title, "description": task.description, "repo": task.repo}
    if not trusted_repository(issue["repo"]):
        return _record_blocked(task_id, session_factory=session_factory, kind="repository_not_trusted",
                               reason="Repository has not been approved as trusted", claim=claim)
    with session_factory() as session, session.begin():
        AdmissionClaimService(session).assert_claim(claim)
    observation = observe(issue["repo"], timeout_seconds=timeout_seconds)
    if observation.status != "observed" or observation.base_commit is None:
        return _record_blocked(task_id, session_factory=session_factory, kind="repository_check",
                               reason=f"Repository check: {observation.status}",
                               retry_hint="transient" if observation.status == "timeout" else "needs_review", claim=claim)
    observations = (
        AdmissionObservation(id="issue", description="The supplied Issue title and description were received"),
        AdmissionObservation(id="repository_head", description=f"Git returned HEAD {observation.base_commit}"),
    )
    agent_input = {"issue": issue, "observations": [item.model_dump() for item in observations]}
    raw = gateway.request(task_id, prompt=build_admission_supervisor_prompt(agent_input), claim=claim)
    if raw is None:
        return _record_blocked(task_id, session_factory=session_factory, kind="model_budget",
                               reason="Admission model budget unavailable", claim=claim)
    decision = (AdmissionDecision.model_validate_json(raw) if isinstance(raw, str)
                else AdmissionDecision.model_validate(raw))
    evidence = resolve_admission_evidence(decision, observations)
    if decision.root.outcome == "BLOCKED":
        return _record_blocked(task_id, session_factory=session_factory, kind="model_blocked",
                               reason=decision.root.reason, missing_information=decision.root.missing_information, claim=claim)
    if decision.root.outcome == "ELIGIBLE" and "repository_head" not in decision.root.evidence_refs:
        raise ValueError("Admission approval must reference the observed repository HEAD")
    status = TaskStatus(decision.root.outcome)
    record = AdmissionRecord(reason=decision.root.reason, evidence=list(evidence))
    with session_factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, status, admission_record=record,
            base_commit=observation.base_commit if status == TaskStatus.ELIGIBLE else None, claim=claim,
        )
    return AdmissionAttemptResult(status.value, decision.root.reason)


def main(argv: list[str] | None = None) -> int:
    """Explicitly attempt one Task; the operator identifies the trusted repository."""
    import httpx
    from devflow_ai.database import create_db_engine, create_session_factory
    from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter

    parser = argparse.ArgumentParser(description="Attempt admission for one Task and a trusted repository.")
    parser.add_argument("--task-id", type=UUID, required=True)
    parser.add_argument("--trusted-repo", required=True, help="Exact approved repository value for this Task.")
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = create_db_engine()
        factory = create_session_factory(engine)
        with httpx.Client() as client:
            adapter = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=30)
            gateway = AdmissionModelGateway(
                session_factory=factory, estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")),
                generate=adapter, max_output_tokens=300,
            )
            result = attempt_admission(
                args.task_id, session_factory=factory, gateway=gateway,
                trusted_repository=lambda repo: repo == args.trusted_repo, timeout_seconds=30,
            )
    except Exception as error:
        print(json.dumps({"event": "admission_attempt_failed", "task_id": str(args.task_id),
                          "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"event": "admission_attempt_finished", "task_id": str(args.task_id),
                      "outcome": result.outcome, "reason": result.reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
