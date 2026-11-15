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

from devflow_ai.models import AgentExecution, Run


class UsageReportError(RuntimeError):
    """The Run cannot be reported on at all."""


def _entry_tokens(entry: dict) -> tuple[int, int, int, int]:
    """Return (settled input, settled output, unsettled count, reserved tokens)."""
    actual = entry.get("actual_usage")
    if actual is None:
        reserved = entry.get("reserved_tokens")
        return 0, 0, 1, reserved if isinstance(reserved, int) else 0
    return (int(actual.get("input_tokens") or 0), int(actual.get("output_tokens") or 0), 0, 0)


def summarize_usage(executions) -> dict:
    """Sum committed usage by role.

    ``executions`` yields ``(role, llm_usage)`` pairs. A usage blob without the
    ``model_requests`` mapping is legacy and cannot be charged reliably, so it is counted
    as one legacy execution and excluded from the token totals.
    """
    roles: dict[str, dict] = {}
    legacy = 0
    for role, usage in executions:
        row = roles.setdefault(role, {
            "role": role, "executions": 0, "requests": 0, "input_tokens": 0,
            "output_tokens": 0, "unsettled_requests": 0, "reserved_tokens": 0,
        })
        row["executions"] += 1
        if usage is None:
            continue
        requests = usage.get("model_requests")
        if requests is None:
            legacy += 1
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
        "note": ("Committed usage only; an unsettled request is counted apart because it may "
                 "still have been billed. Prompts and completions are never read here."),
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


def compare_usage(first: dict, second: dict) -> dict:
    """Compare two reports by role, including roles that only one Run used."""
    first_rows = {row["role"]: row for row in first["roles"]}
    second_rows = {row["role"]: row for row in second["roles"]}
    roles = []
    for role in sorted(set(first_rows) | set(second_rows)):
        before = first_rows.get(role, {}).get("total_tokens", 0)
        after = second_rows.get(role, {}).get("total_tokens", 0)
        roles.append({
            "role": role, "first_tokens": before, "second_tokens": after,
            "delta": after - before,
            "change": (None if before == 0 else round((after - before) / before, 4)),
        })
    total_before = first["total_tokens"]
    total_after = second["total_tokens"]
    return {
        "first_run_id": first["run_id"], "second_run_id": second["run_id"],
        "roles": roles,
        "total": {
            "first_tokens": total_before, "second_tokens": total_after,
            "delta": total_after - total_before,
            "change": (None if total_before == 0 else
                       round((total_after - total_before) / total_before, 4)),
        },
        "caveat": ("A smaller second total only says this attempt cost fewer tokens; it does "
                   "not by itself prove the task went further. Read it together with the "
                   "record's outcome and stage count."),
    }


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report committed model usage for one Run, optionally comparing two.")
    parser.add_argument("--run", required=True, help="Run ID to report on.")
    parser.add_argument("--compare", help="Earlier Run ID; the report then compares both.")
    args = parser.parse_args(argv)
    try:
        run_id = UUID(str(args.run))
        compare_id = UUID(str(args.compare)) if args.compare else None
    except (TypeError, ValueError):
        print(json.dumps({"event": "usage_report_configuration_error",
                          "detail": "Run IDs must be UUIDs"}), file=sys.stderr)
        return 2

    engine = None
    try:
        if session_factory is None:
            from devflow_ai.database import create_db_engine, create_session_factory

            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        with session_factory() as session:
            report = load_run_usage(session, run_id)
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
    print(json.dumps({"event": "run_usage", **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
