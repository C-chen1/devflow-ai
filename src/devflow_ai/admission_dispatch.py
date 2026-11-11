"""Bounded, explicit one-at-a-time admission discovery for approved repositories."""
from dataclasses import dataclass
from datetime import datetime
import argparse
import json
import sys
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from devflow_ai.admission import AdmissionAttemptResult, _run_with_claim
from devflow_ai.admission_claim import AdmissionClaimService
from devflow_ai.admission_intervention import get_admission_status
from devflow_ai.enums import TaskStatus
from devflow_ai.harness import RepositoryHeadObservation, observe_repository_head
from devflow_ai.model_gateway import AdmissionModelGateway
from devflow_ai.models import Task


@dataclass(frozen=True)
class AdmissionScanCursor:
    created_at: datetime
    task_id: UUID


@dataclass(frozen=True)
class AdmissionDispatchResult:
    task_id: UUID | None
    attempt: AdmissionAttemptResult | None
    next_cursor: AdmissionScanCursor | None
    scanned_count: int


def dispatch_one_admission(
    *, session_factory: sessionmaker[Session], gateway: AdmissionModelGateway,
    trusted_repositories: frozenset[str], timeout_seconds: int, page_size: int = 25,
    after: AdmissionScanCursor | None = None,
    observe: Callable[..., RepositoryHeadObservation] = observe_repository_head,
) -> AdmissionDispatchResult:
    """Scan one bounded page; at most one trusted Task is claimed and processed."""
    if not isinstance(trusted_repositories, frozenset) or any(
        not isinstance(repo, str) or not repo.strip() for repo in trusted_repositories
    ):
        raise ValueError("trusted_repositories must be a frozenset of non-blank strings")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if not trusted_repositories:
        return AdmissionDispatchResult(None, None, None, 0)
    with session_factory() as session:
        query = select(Task.id, Task.created_at, Task.admission_blocked,
                       Task.admission_claim_token, Task.admission_attempt_count).where(
            Task.status == TaskStatus.PENDING, Task.repo.in_(trusted_repositories),
        ).order_by(Task.created_at, Task.id).limit(page_size)
        if after is not None:
            query = query.where(tuple_(Task.created_at, Task.id) > tuple_(after.created_at, after.task_id))
        candidates = session.execute(query).all()
    for index, candidate in enumerate(candidates):
        cursor = AdmissionScanCursor(candidate.created_at, candidate.id)
        with session_factory() as session:
            status = get_admission_status(session, candidate.id, skip_locked=True)
        if status is None or status["intervention"] is not None:
            continue
        if candidate.admission_blocked is not None:
            mode = {"automatic_retry": True}
        elif candidate.admission_attempt_count == 0 and candidate.admission_claim_token is None:
            mode = {"automatic_initial": True}
        elif candidate.admission_attempt_count and candidate.admission_claim_token is not None:
            mode = {"automatic_recovery": True}
        else:
            continue
        with session_factory() as session:
            claim = AdmissionClaimService(session).claim(candidate.id, owner=f"admission-dispatch-{uuid4()}",
                lease_seconds=max(90, timeout_seconds + 60), **mode)
        if claim is None:
            continue
        result = _run_with_claim(candidate.id, session_factory=session_factory, gateway=gateway,
            trusted_repository=trusted_repositories.__contains__, timeout_seconds=timeout_seconds,
            observe=observe, claim=claim)
        return AdmissionDispatchResult(candidate.id, result, cursor, index + 1)
    next_cursor = AdmissionScanCursor(candidates[-1].created_at, candidates[-1].id) if len(candidates) == page_size else None
    return AdmissionDispatchResult(None, None, next_cursor, len(candidates))


def main(argv: list[str] | None = None) -> int:
    """Run one bounded scan; polling and cursor persistence remain caller responsibilities."""
    import httpx
    from devflow_ai.database import create_db_engine, create_session_factory
    from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter

    parser = argparse.ArgumentParser(description="Scan one page and attempt at most one trusted Task admission.")
    parser.add_argument("--trusted-repo", action="append", required=True)
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--after-created-at")
    parser.add_argument("--after-task-id", type=UUID)
    args = parser.parse_args(argv)
    if bool(args.after_created_at) != bool(args.after_task_id):
        parser.error("--after-created-at and --after-task-id must be supplied together")
    try:
        timestamp = datetime.fromisoformat(args.after_created_at) if args.after_created_at else None
        if timestamp is not None and timestamp.utcoffset() is None:
            raise ValueError("cursor time must include a timezone")
        cursor = AdmissionScanCursor(timestamp, args.after_task_id) if timestamp is not None else None
    except ValueError:
        parser.error("--after-created-at must be an ISO timestamp with timezone")
    engine = None
    try:
        engine = create_db_engine()
        factory = create_session_factory(engine)
        with httpx.Client() as client:
            adapter = DeepSeekPlannerAdapter.from_env(client, timeout_seconds=30)
            gateway = AdmissionModelGateway(session_factory=factory,
                estimate_input_tokens=lambda prompt: len(prompt.encode("utf-8")), generate=adapter,
                max_output_tokens=300)
            result = dispatch_one_admission(session_factory=factory, gateway=gateway,
                trusted_repositories=frozenset(args.trusted_repo), timeout_seconds=30,
                page_size=args.page_size, after=cursor)
    except Exception as error:
        print(json.dumps({"event": "admission_dispatch_failed", "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    next_cursor = ({"created_at": result.next_cursor.created_at.isoformat(), "task_id": str(result.next_cursor.task_id)}
                   if result.next_cursor is not None else None)
    print(json.dumps({"event": "admission_scan_completed", "scanned_count": result.scanned_count,
                      "task_id": str(result.task_id) if result.task_id else None,
                      "outcome": result.attempt.outcome if result.attempt else None,
                      "reason": result.attempt.reason if result.attempt else None, "next_cursor": next_cursor},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
