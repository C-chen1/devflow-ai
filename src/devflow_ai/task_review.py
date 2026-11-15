"""Record one human review of a Task's formally passed Run.

The review is read from the run record the reviewer already produced: the digest of that
file, the reviewer's own timestamp and the Verification it cites are what get bound to the
Task. Nothing here invents evidence; a record that does not match the database is refused.

Exit codes: 0 recorded (or already recorded identically), 2 the submission is not usable,
1 the database refused the write.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import sys
from pathlib import Path
from uuid import UUID


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "task_review_configuration_error", "detail": detail}),
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    from devflow_ai.benchmark_record import RunOutcome, parse_run_record
    from devflow_ai.services import TaskReviewError, TaskReviewService

    parser = argparse.ArgumentParser(
        description="Record a human review of one Task and move the Task in one transaction.")
    parser.add_argument("--record", required=True,
                        help="Run record carrying the human decision; read, never modified.")
    parser.add_argument("--reviewer", required=True,
                        help="Must match the reviewer recorded in the record.")
    parser.add_argument("--conclusion", choices=["accepted", "rejected"],
                        help="Defaults to the conclusion already in the record.")
    parser.add_argument("--task-id", help="Defaults to the record's task_id.")
    parser.add_argument("--run-id", help="Defaults to the record's run_id.")
    parser.add_argument("--verification-id",
                        help="Defaults to the record's formal verification id.")
    args = parser.parse_args(argv)

    record_path = Path(args.record)
    try:
        raw = record_path.read_bytes()
    except OSError as error:
        return _configuration_error(f"Run record is unreadable: {type(error).__name__}")
    # One read, one set of bytes: the digest that gets bound and the record that gets checked
    # describe the same file even if it changes on disk while this runs.
    record_digest = sha256(raw).hexdigest()
    try:
        record = parse_run_record(raw)
    except ValueError as error:
        return _configuration_error(f"Run record is unusable: {error}")
    if record.outcome is not RunOutcome.FORMAL_PASS or record.formal is None:
        return _configuration_error("Only a formally passed record can be reviewed")
    conclusion = args.conclusion or record.human_review.conclusion
    if conclusion not in ("accepted", "rejected"):
        return _configuration_error("The record carries no human decision to record")
    if record.human_review.conclusion not in ("accepted", "rejected"):
        return _configuration_error("The record carries no human decision to record")
    if conclusion != record.human_review.conclusion:
        return _configuration_error(
            "The submitted conclusion differs from the one in the record")
    reviewer = (record.human_review.reviewer or "").strip()
    if not reviewer or reviewer != args.reviewer.strip():
        return _configuration_error(
            "The submitted reviewer does not match the reviewer in the record")
    reviewed_at = (record.human_review.reviewed_at or "").strip()
    if not reviewed_at:
        return _configuration_error("The record carries no review time")

    raw_ids = {
        "task": args.task_id or record.task_id,
        "run": args.run_id or record.run_id,
        "verification": args.verification_id or record.formal.verification_id,
    }
    # Every supplied id must agree with the record; an override may fill a gap, never replace
    # a value already bound in the evidence.
    for name, value in (("task", record.task_id), ("run", record.run_id),
                        ("verification", record.formal.verification_id)):
        override = {"task": args.task_id, "run": args.run_id,
                    "verification": args.verification_id}[name]
        if value is None or override is None:
            continue
        try:
            same = UUID(str(value)) == UUID(str(override))
        except (TypeError, ValueError):
            same = str(value) == str(override)
        if not same:
            return _configuration_error(
                f"The record's {name} id does not match the one supplied")
    identifiers = {}
    for name, value in raw_ids.items():
        if value is None:
            return _configuration_error(
                f"No {name} id: supply it explicitly when the record carries none")
        try:
            identifiers[name] = UUID(str(value))
        except (TypeError, ValueError):
            return _configuration_error(f"The {name} id is not a UUID")

    engine = None
    try:
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        with session_factory() as session:
            result = TaskReviewService(session).record(
                task_id=identifiers["task"], run_id=identifiers["run"],
                verification_id=identifiers["verification"],
                record_sha256=record_digest,
                conclusion=conclusion, reviewer=reviewer, reviewed_at=reviewed_at,
                independent_suite_version=record.formal.independent_suite_version,
            )
    except TaskReviewError as error:
        return _configuration_error(str(error))
    except ValueError as error:  # TaskNotFoundError and state conflicts inherit from ValueError
        return _configuration_error(str(error) or "The task cannot be reviewed")
    except Exception as error:  # a database problem is reported, not raised as a stack trace
        print(json.dumps({"event": "task_review_failed", "error_type": type(error).__name__}),
              file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps({"event": "task_review_recorded", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
