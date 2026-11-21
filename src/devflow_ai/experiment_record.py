"""Trusted-side command line for one experiment ledger entry.

Two commands, both narrow on purpose:

* ``register-record`` fixes the digest of one run record for one attempt of one candidate, so
  the ledger can later tell whether the bytes it reads are the ones the control plane fixed.
  Only the digest is stored; the file is never copied.
* ``record-result`` attributes one **finished** attempt to a candidate by running the shared
  classifier over the committed rows and the artifacts on disk. It does not run agents, call a
  model, evaluate anything, or create a Run; it writes one result row (or refuses).
* ``summary`` prints the ledger's own view for a reviewer: limits, usage, per-candidate results
  and the context/version evidence behind each one.

Neither command can accept a verdict from the operator. The only way a result says
``formal_pass`` is ``benchmark_run.classify_outcome`` reaching that conclusion — the same rules
the benchmark entry, the scheduler record and the backfill entry use. ``declared`` results and
results without a run record stay outside the promotable set.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from devflow_ai.experiment import (
    DEADLINE_CONTEXTS, ExperimentError, ExperimentService,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write or read one controlled-experiment ledger entry; never runs a task.")
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser(
        "record-result",
        help="attribute one finished attempt using the shared classifier")
    record.add_argument("--experiment", required=True, help="Experiment UUID")
    record.add_argument("--candidate", required=True, help="Candidate label in that experiment")
    record.add_argument("--task-id", required=True, help="Declared Task UUID")
    record.add_argument("--run-id", required=True, help="Finished Run UUID of that Task")
    record.add_argument("--artifacts-root", required=True,
                        help="Directory holding this Run's recorded artifacts")
    record.add_argument("--suite-version", required=True,
                        help="Pinned independent-suite version this pack declares")
    record.add_argument("--record", help="Run record the attempt's own entry wrote. Preferred: "
                                         "its observed context is used and its bytes digested.")
    record.add_argument("--deadline-context", choices=list(DEADLINE_CONTEXTS),
                        help="Only when no record is supplied: what the caller knows about the "
                             "attempt's deadline. Never inferred.")
    record.add_argument("--context-source", default="operator_statement",
                        help="Where the stated context came from (label only, at most 200 chars)")
    record.add_argument("--operator", required=True, help="Person or account recording this")

    register = commands.add_parser(
        "register-record",
        help="fix the digest of one run record for one attempt of one candidate")
    register.add_argument("--experiment", required=True, help="Experiment UUID")
    register.add_argument("--candidate", required=True, help="Candidate label in that experiment")
    register.add_argument("--task-id", required=True, help="Declared Task UUID")
    register.add_argument("--record", required=True,
                          help="Run record file whose digest is fixed; only the digest is stored")
    register.add_argument("--operator", required=True, help="Person or account fixing it")

    summary = commands.add_parser("summary", help="print the ledger's view of one experiment")
    summary.add_argument("--experiment", required=True, help="Experiment UUID")

    explain = commands.add_parser(
        "explain-acceptance",
        help="read-only: which necessary conditions a candidate meets, and what is missing")
    explain.add_argument("--experiment", required=True, help="Experiment UUID")
    explain.add_argument("--candidate", required=True, help="Candidate label in that experiment")
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parser().parse_args(argv)
    engine = None
    try:
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        if args.command == "register-record":
            with session_factory() as session:
                payload = ExperimentService(session).register_record(
                    experiment_id=args.experiment, candidate_label=args.candidate,
                    task_id=args.task_id, record_path=args.record,
                    registered_by=args.operator)
                session.commit()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "summary":
            with session_factory() as session:
                payload = ExperimentService(session).summary(args.experiment)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "explain-acceptance":
            with session_factory() as session:
                payload = ExperimentService(session).explain_acceptance(
                    args.experiment, args.candidate)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        with session_factory() as session:
            recorded = ExperimentService(session).record_classified_result(
                experiment_id=args.experiment, candidate_label=args.candidate,
                task_id=args.task_id, run_id=args.run_id,
                artifacts_root=Path(args.artifacts_root),
                independent_suite_version=args.suite_version,
                deadline_context=(None if args.record else args.deadline_context),
                context_source=args.context_source, record_path=args.record,
                recorded_by=args.operator)
            session.commit()
        print(json.dumps(recorded, ensure_ascii=False, indent=2))
        return 0
    except ExperimentError as error:
        # The ledger refused: nothing was written. The message names the rule, not the data.
        print(json.dumps({"event": "experiment_record_refused",
                          "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(json.dumps({"event": "experiment_record_error",
                          "detail": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
