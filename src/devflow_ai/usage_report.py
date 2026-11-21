"""Read-only token accounting for one Run, and a comparison of two Runs.

A benchmark attempt's cost is stored per AgentExecution, not per record: each model
request keeps the reservation it was granted and, once the provider replies, the usage it
reported. This module sums that by role so two attempts can be compared like for like.

It is deliberately read-only: no reservation, settlement, lease or status is touched, and
no prompt, completion or hidden material is read — only the numbers already committed.
An unsettled reservation is reported separately rather than guessed at, because a request
that never reported usage may still have been billed.
"""
from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from devflow_ai.models import AgentExecution, Run, Task


class UsageReportError(RuntimeError):
    """The Run cannot be reported on at all."""


def _entry_tokens(entry: dict) -> tuple[int, int, int, int]:
    """Return (settled input, settled output, unsettled count, reserved tokens)."""
    actual = entry.get("actual_usage")
    if actual is None:
        reserved = entry.get("reserved_tokens")
        return 0, 0, 1, reserved if isinstance(reserved, int) else 0
    if not isinstance(actual, dict) or any(
            type(actual.get(key)) is not int or actual[key] < 0
            for key in ("input_tokens", "output_tokens")):
        raise UsageReportError("Recorded actual usage has missing or invalid token counts")
    return actual["input_tokens"], actual["output_tokens"], 0, 0


def summarize_usage(executions) -> dict:
    """Sum committed usage by role.

    ``executions`` yields ``(role, llm_usage)`` pairs. A usage blob without the
    ``model_requests`` mapping is legacy and cannot be charged reliably. A NULL usage
    also cannot distinguish no calls from missing data; both are marked unknown.
    """
    roles: dict[str, dict] = {}
    legacy = 0
    for role, usage in executions:
        row = roles.setdefault(role, {
            "role": role, "executions": 0, "requests": 0, "input_tokens": 0,
            "output_tokens": 0, "unsettled_requests": 0, "reserved_tokens": 0,
            "unknown_usage_executions": 0,
        })
        row["executions"] += 1
        if usage is None:
            row["unknown_usage_executions"] += 1
            continue
        requests = usage.get("model_requests")
        if requests is None:
            legacy += 1
            row["unknown_usage_executions"] += 1
            continue
        for entry in requests.values():
            input_tokens, output_tokens, unsettled, reserved = _entry_tokens(entry)
            row["requests"] += 1
            row["input_tokens"] += input_tokens
            row["output_tokens"] += output_tokens
            row["unsettled_requests"] += unsettled
            row["reserved_tokens"] += reserved
    for row in roles.values():
        row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
    ordered = sorted(roles.values(), key=lambda item: (-item["total_tokens"], item["role"]))
    return {
        "roles": ordered,
        "total_tokens": sum(row["total_tokens"] for row in ordered),
        "total_requests": sum(row["requests"] for row in ordered),
        "unsettled_requests": sum(row["unsettled_requests"] for row in ordered),
        "reserved_tokens": sum(row["reserved_tokens"] for row in ordered),
        "legacy_executions_without_usage": legacy,
        "unknown_usage_executions": sum(row["unknown_usage_executions"] for row in ordered),
        "note": ("Committed AgentExecution usage only; Task admission and other costs are not "
                 "included. Unsettled or missing usage remains unknown. Prompts and completions "
                 "are never read here."),
    }


def load_run_usage(session: Session, run_id: UUID) -> dict:
    """Read one Run's committed usage by role; changes nothing."""
    run = session.get(Run, run_id)
    if run is None:
        raise UsageReportError("Run does not exist")
    rows = session.execute(
        select(AgentExecution.agent_role, AgentExecution.llm_usage).where(
            AgentExecution.run_id == run_id)
    ).all()
    return {"run_id": str(run_id), "status": run.status.value,
            "attempt_number": run.attempt_number, **summarize_usage(rows)}


def load_task_usage(session: Session, task_id: UUID) -> dict:
    """Report recorded admission once and each Run separately; this is not a budget gate."""
    # A single statement gives PostgreSQL one snapshot for Task, Runs and executions.
    rows = session.execute(select(Task.id, Task.admission_usage, Run.id, Run.attempt_number,
        Run.status, AgentExecution.id, AgentExecution.agent_role, AgentExecution.llm_usage)
        .select_from(Task).outerjoin(Run, Run.task_id == Task.id)
        .outerjoin(AgentExecution, AgentExecution.run_id == Run.id)
        .where(Task.id == task_id).order_by(Run.attempt_number, AgentExecution.id)).all()
    if not rows:
        raise UsageReportError("Task does not exist")
    admission = summarize_usage([("admission", {"model_requests": rows[0][1]})])
    runs = {}
    for _, _, run_id, attempt, status, execution_id, role, usage in rows:
        if run_id is None:
            continue
        run = runs.setdefault(run_id, {"attempt_number": attempt, "status": status.value, "executions": []})
        if execution_id is not None:
            run["executions"].append((role, usage))
    reports = [{"run_id": str(run_id), "attempt_number": run["attempt_number"], "status": run["status"],
                **summarize_usage(run["executions"])} for run_id, run in runs.items()]
    return {
        "task_id": str(task_id), "admission": admission, "runs": reports,
        "recorded_settled_tokens": admission["total_tokens"] + sum(run["total_tokens"] for run in reports),
        "unsettled_requests": admission["unsettled_requests"] + sum(run["unsettled_requests"] for run in reports),
        "unknown_usage_executions": sum(run["unknown_usage_executions"] for run in reports),
        "note": ("Recorded admission and AgentExecution usage only; legacy admission records or other "
                 "costs may be missing. Unsettled or unknown usage is not included in the settled total. "
                 "This read-only report reflects one database statement and can become stale; "
                 "it is not an experiment budget or complete billing statement."),
    }


def compare_usage(first: dict, second: dict) -> dict:
    """Compare settled usage; never present an incomplete total as a cost difference."""
    first_rows = {row["role"]: row for row in first["roles"]}
    second_rows = {row["role"]: row for row in second["roles"]}
    def complete(row: dict | None) -> bool:
        return row is None or (row.get("unsettled_requests") == 0 and
                               row.get("unknown_usage_executions") == 0)

    roles = []
    for role in sorted(set(first_rows) | set(second_rows)):
        before = first_rows.get(role, {}).get("total_tokens", 0)
        after = second_rows.get(role, {}).get("total_tokens", 0)
        comparable = complete(first_rows.get(role)) and complete(second_rows.get(role))
        roles.append({
            "role": role, "first_tokens": before, "second_tokens": after,
            "delta": after - before if comparable else None,
            "change": (None if not comparable or before == 0 else round((after - before) / before, 4)),
        })
    total_before = first["total_tokens"]
    total_after = second["total_tokens"]
    usage_complete = all(complete(row) for row in (*first_rows.values(), *second_rows.values()))
    return {
        "first_run_id": first["run_id"], "second_run_id": second["run_id"],
        "roles": roles,
        "total": {
            "first_tokens": total_before, "second_tokens": total_after,
            "delta": total_after - total_before if usage_complete else None,
            "change": (None if not usage_complete or total_before == 0 else
                       round((total_after - total_before) / total_before, 4)),
        },
        "recorded_agent_usage_complete": usage_complete,
        "caveat": ("Totals contain settled AgentExecution usage only and exclude Task admission "
                   "and other costs; missing or unsettled usage prevents a cost "
                   "difference. This is a snapshot and a Run may incur later usage. A smaller settled "
                   "total does not by itself prove the task went further. "
                   "Read it together with the record's outcome and stage count."),
    }


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report recorded model usage for a Task or Run, optionally comparing Runs.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run", help="Run ID to report on.")
    selection.add_argument("--task", help="Task ID, including recorded admission and all Runs.")
    parser.add_argument("--compare", help="Earlier Run ID; the report then compares both.")
    args = parser.parse_args(argv)
    if args.task and args.compare:
        print(json.dumps({"event": "usage_report_configuration_error",
                          "detail": "--compare requires --run"}), file=sys.stderr)
        return 2
    try:
        run_id = UUID(str(args.run)) if args.run else None
        task_id = UUID(str(args.task)) if args.task else None
        compare_id = UUID(str(args.compare)) if args.compare else None
    except (TypeError, ValueError):
        print(json.dumps({"event": "usage_report_configuration_error",
                          "detail": "Task and Run IDs must be UUIDs"}), file=sys.stderr)
        return 2

    engine = None
    try:
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        with session_factory() as session:
            report = load_task_usage(session, task_id) if task_id else load_run_usage(session, run_id)
            comparison = (compare_usage(load_run_usage(session, compare_id), report)
                          if compare_id is not None else None)
    except UsageReportError as error:
        print(json.dumps({"event": "usage_report_configuration_error",
                          "detail": str(error)}), file=sys.stderr)
        return 2
    except Exception as error:  # a database problem is reported, not raised as a stack trace
        print(json.dumps({"event": "usage_report_failed", "error_type": type(error).__name__}),
              file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    if comparison is not None:
        print(json.dumps({"event": "run_usage_comparison", **comparison}))
        return 0
    print(json.dumps({"event": "task_usage" if task_id else "run_usage", **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
