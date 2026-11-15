"""Rebuild a reviewable record for a Run that already finished, without running anything.

If a process exits after the formal transaction commits and before the record is written, the
attempt leaves durable evidence but no file a reviewer can read. This entry reads that
evidence back — the Run, its Verification, the draft and the independent receipt — and writes
a **new** record through the same classifier and the same exclusive create as the live entries.

It never starts a model, claims a Run, re-runs a Task, or changes a row: the only thing it
writes is the record file the operator names. Whatever cannot be recovered from persisted
evidence (the attempt's deadline, its kind if the operator does not declare one, the model, and
of course the human decision) is written as unrecovered rather than guessed.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from uuid import UUID

from devflow_ai.benchmark_record import AttemptKind, write_run_record
from devflow_ai.benchmark_run import compose_run_record
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.enums import RunStatus
from devflow_ai.models import Run, Task
from devflow_ai.task_pack import load_manifest


class BackfillError(RuntimeError):
    """The Run cannot be recorded from its persisted evidence; nothing was written."""


@dataclass(frozen=True)
class BackfillRequest:
    task_id: UUID
    run_id: UUID
    pack_dir: Path
    artifacts_root: Path
    record_path: Path
    operator: str
    # Never inferred from the rows: an unrecorded kind stays ``unknown``, which keeps a
    # rehearsal out of the formal sample even when the Run itself passed.
    attempt_kind: str = "unknown"


def backfill_record(request: BackfillRequest, *, session_factory) -> dict:
    """Write one reconstruction of an existing Run's record; read-only otherwise."""
    try:
        attempt_kind = AttemptKind(request.attempt_kind)
    except ValueError:
        raise BackfillError(
            "attempt_kind must be formal, rehearsal or unknown") from None
    if not isinstance(request.operator, str) or not request.operator.strip():
        raise BackfillError("operator must be a non-blank string")
    if Path(request.record_path).exists():
        raise BackfillError(
            "The record path already exists; a run record is evidence and is never "
            "overwritten")
    if not Path(request.artifacts_root).is_dir():
        raise BackfillError("artifacts-root must be an existing directory")
    try:
        manifest = load_manifest(request.pack_dir)
    except ValueError as error:
        raise BackfillError(f"Task pack is invalid: {type(error).__name__}") from None

    with session_factory() as session:
        task = session.get(Task, request.task_id)
        if task is None:
            raise BackfillError("Task does not exist")
        run = session.get(Run, request.run_id)
        if run is None:
            raise BackfillError("Run does not exist")
        if run.task_id != task.id:
            raise BackfillError("The Run does not belong to the named Task")
        if task.base_commit != manifest.base_commit:
            raise BackfillError("Task base commit does not match the task pack")
        if not task.policy_snapshot:
            raise BackfillError("Task has no pinned Policy snapshot")
        if run.status is RunStatus.RUNNING:
            # An attempt that is still going (or whose end nobody recorded) must not be
            # written down as finished evidence.
            raise BackfillError(
                "The Run is still RUNNING; its outcome and finish time are not recorded yet")
        if run.finished_at is None:
            raise BackfillError("The Run has no recorded finish time; refusing to invent one")
        run_status, attempt_index = run.status, run.attempt_number
        started_at = run.started_at.astimezone(timezone.utc).isoformat()
        finished_at = run.finished_at.astimezone(timezone.utc).isoformat()
        policy_version = task.policy_version
        suite_version = (manifest.independent_suite.suite_version
                         if manifest.independent_suite is not None else None)
        notes = (", backfill=true: rebuilt from persisted rows; the deadline, the model and "
                 "the human decision are not recovered")
        if attempt_kind is AttemptKind.UNKNOWN:
            notes += "; attempt_kind was not recorded anywhere and stays unknown"
        else:
            notes += "; attempt_kind was declared by the operator, not recovered from evidence"
        # The evaluation step itself is not reconstructed: without the live finalization
        # result the classifier can only report that no attempted evaluation is on record.
        record, reason, _ = compose_run_record(
            session, run.id, manifest=manifest, task_id=task.id,
            independent_suite_version=suite_version, policy_version=policy_version,
            operator=request.operator.strip(), attempt_kind=attempt_kind,
            artifact_root=Path(request.artifacts_root) / str(run.id),
            finalization=None, deadline_fired=False,
            artifacts_root=Path(request.artifacts_root), started_at=started_at,
            limits=None, model_id="unrecorded", attempt_index=attempt_index,
            finished_at=finished_at, extra_notes=notes)

    try:
        write_run_record(record, request.record_path)
    except (ValueError, OSError) as failure:
        # Another writer may have created the file between the check and the write.
        raise BackfillError(
            f"The record could not be written: {type(failure).__name__}") from None
    return {
        "outcome": record.outcome.value, "reason": reason,
        "record_path": str(request.record_path), "task_id": str(request.task_id),
        "run_id": str(request.run_id), "run_status": run_status.value,
        "attempt_index": attempt_index, "attempt_kind": record.attempt_kind.value,
        "formal_verification_id": (record.formal.verification_id
                                   if record.formal is not None else None),
        "human_review": record.human_review.conclusion,
    }


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "record_backfill_configuration_error", "detail": detail}),
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Write one backfilled record for an existing Run.

    Exit codes: 0 when a record was written, 2 when the inputs do not describe a finished,
    matching Run (nothing written), 1 when writing failed. No model is contacted and no Run
    is created, claimed or re-executed.
    """
    parser = argparse.ArgumentParser(
        description="Rebuild a Run's reviewable record from persisted evidence; never "
                    "re-runs the Task.")
    parser.add_argument("--task-id", required=True, help="UUID of the Task the Run belongs to.")
    parser.add_argument("--run-id", required=True, help="UUID of the finished Run.")
    parser.add_argument("--pack", required=True, help="Task-pack directory.")
    parser.add_argument("--artifacts-root", required=True,
                        help="Existing artifact root that contains this Run's directory.")
    parser.add_argument("--record", required=True,
                        help="Where to write the record; the file must not exist yet.")
    parser.add_argument("--operator", required=True,
                        help="Person or account performing the reconstruction.")
    parser.add_argument("--attempt-kind", choices=["formal", "rehearsal", "unknown"],
                        default="unknown",
                        help="Only set this if the kind is known independently: it is not "
                             "recovered from the Run, and an unknown kind stays out of the "
                             "formal sample.")
    args = parser.parse_args(argv)

    try:
        task_id, run_id = UUID(args.task_id), UUID(args.run_id)
    except (TypeError, ValueError):
        return _configuration_error("task-id and run-id must be UUIDs")

    engine = None
    try:
        if session_factory is None:
            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        result = backfill_record(
            BackfillRequest(
                task_id=task_id, run_id=run_id, pack_dir=Path(args.pack),
                artifacts_root=Path(args.artifacts_root), record_path=Path(args.record),
                operator=args.operator, attempt_kind=args.attempt_kind),
            session_factory=session_factory)
    except BackfillError as error:
        return _configuration_error(str(error))
    except Exception as error:
        print(json.dumps({"event": "record_backfill_failed",
                          "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps({"event": "record_backfill_written", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
