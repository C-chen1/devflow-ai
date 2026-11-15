"""Control-plane entry points that need no model gateway.

An operator uses these to inspect Runs whose worker stopped renewing, and to close
the ones that must not continue. Closing derives its decision from durable history,
so it needs no model; the recovery Run is deliberately left for a worker that does
have model gateways. These commands never change Policy, never migrate the database
and never print configuration, SQL parameters or full exceptions.
"""
import argparse
import json
import sys
from contextlib import ExitStack
from math import isfinite
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from devflow_ai.coordinator import RunCoordinator, RunLeaseNotExpiredError
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.model_gateways import ModelGateways, model_gateways_from_env
from devflow_ai.worker import Worker
from devflow_ai.workspace_lock import WorkspaceLockUnavailableError, WorkspaceQuarantinedError

DEFAULT_MAX_READ_BYTES = 65536


def _compact_scan(result: dict) -> dict:
    """Keep only what an operator needs from one reclaim result, JSON-ready."""
    record = {
        "run_id": str(result["run_id"]) if result.get("run_id") is not None else None,
        "outcome": result.get("outcome"),
        "recovery_action": result.get("recovery_action"),
        "policy_reasons": list(result.get("policy_reasons") or ()),
    }
    rediagnosis = result.get("rediagnosis")
    if isinstance(rediagnosis, dict):
        record["rediagnosis_outcome"] = rediagnosis.get("outcome")
    return record


def run_recovery_loop_with_backoff(
    worker, gateways: ModelGateways, *, workspaces_root: Path, artifacts_root: Path,
    run_for_seconds: float, poll_interval_seconds: float, max_consecutive_failures: int,
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES, stop_event: Event | None = None,
    on_result=None,
) -> dict:
    """Reclaim expired Runs repeatedly for a bounded time, surviving transient faults.

    Unlike the bounded sweep, this loop tolerates a failing scan: a control-plane
    sweeper that dies on the first database hiccup is worse than one that retries.
    The tolerance is still bounded — after ``max_consecutive_failures`` consecutive
    failures the error propagates rather than spinning forever — and the whole run is
    capped by ``run_for_seconds`` so the command cannot become an unsupervised
    service. A successful scan resets the counter.
    """
    from time import monotonic

    if not isfinite(run_for_seconds) or run_for_seconds <= 0:
        raise ValueError("run_for_seconds must be finite and positive")
    if not isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be finite and positive")
    if isinstance(max_consecutive_failures, bool) or max_consecutive_failures <= 0:
        raise ValueError("max_consecutive_failures must be a positive integer")
    stop = stop_event if stop_event is not None else Event()
    deadline = monotonic() + run_for_seconds
    scans = 0
    failures = []
    consecutive = 0
    while not stop.is_set():
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            result = worker.reclaim_next_expired_run(
                workspaces_root=workspaces_root, artifacts_root=artifacts_root,
                supervisor_gateway=gateways.supervisor, explorer_gateway=gateways.explorer,
                planner_gateway=gateways.planner, recovery_max_read_bytes=max_read_bytes,
                stop_event=stop,
            )
        except Exception as error:
            consecutive += 1
            failures.append({"error_type": type(error).__name__})
            if consecutive >= max_consecutive_failures:
                raise
            # Linear backoff: the point is to stop hammering a broken dependency.
            stop.wait(min(poll_interval_seconds * consecutive, remaining))
            continue
        consecutive = 0
        scans += 1
        if on_result is not None:
            on_result(_compact_scan(result))
        stop.wait(min(poll_interval_seconds, remaining))
    return {"scans": scans, "failures": failures,
            "elapsed_seconds": round(run_for_seconds - max(0.0, deadline - monotonic()), 3)}

def _compact_takeover(result: dict) -> dict:
    """Keep only what an operator needs from one takeover, JSON-ready."""
    record = {
        "run_id": str(result["run_id"]) if result.get("run_id") is not None else None,
        "outcome": result.get("outcome"),
        "worker_id": result.get("worker_id"),
        "previous_worker_id": result.get("previous_worker_id"),
        "unknown_tool_calls": result.get("unknown_tool_calls"),
        "orphaned_executions": list(result.get("orphaned_executions") or ()),
    }
    continued = result.get("continued")
    if isinstance(continued, dict):
        record["continued_outcome"] = continued.get("outcome")
    return record


def reclaim_expired_runs(
    session_factory, gateways: ModelGateways, *, worker_id: str, workspaces_root: Path,
    artifacts_root: Path, max_scans: int, max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> dict:
    """Run the single-scan reclaim at most ``max_scans`` times and report each one.

    Bounded on purpose: this is a control-plane action an operator can reason about,
    not a service. It stops early when a scan finds nothing, so an idle system costs
    one scan. Errors propagate instead of being swallowed into a partial report.
    """
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-blank string")
    if isinstance(max_scans, bool) or not isinstance(max_scans, int) or max_scans <= 0:
        raise ValueError("max_scans must be a positive integer")
    if isinstance(max_read_bytes, bool) or not isinstance(max_read_bytes, int) \
            or max_read_bytes <= 0:
        raise ValueError("max_read_bytes must be a positive integer")
    worker = Worker(session_factory, worker_id)
    records = []
    for _ in range(max_scans):
        result = worker.reclaim_next_expired_run(
            workspaces_root=workspaces_root, artifacts_root=artifacts_root,
            supervisor_gateway=gateways.supervisor, explorer_gateway=gateways.explorer,
            planner_gateway=gateways.planner, recovery_max_read_bytes=max_read_bytes,
            stop_event=Event(),
        )
        records.append(_compact_scan(result))
        if result.get("outcome") == "idle":
            break
    return {"scans": len(records), "results": records}


def inspect_expired_run(session_factory, run_id: UUID) -> dict:
    """Return the read-only takeover report for one Run."""
    with session_factory() as session:
        return RunCoordinator(session).inspect_expired_run(run_id)


def close_expired_runs(session_factory, worker_id: str, *, max_runs: int) -> dict:
    """Close up to ``max_runs`` expired Runs without starting any recovery Run.

    ``find_next_expired_run_id`` only observes; the close re-checks the Run under
    its row lock, so a Run another closer already handled is skipped instead of
    being closed twice. A Run that disappears between discovery and the close is
    reported in ``skipped``, never retried here.
    """
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-blank string")
    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs <= 0:
        raise ValueError("max_runs must be a positive integer")
    closed, skipped = [], []
    for _ in range(max_runs):
        with session_factory() as session:
            run_id = RunCoordinator(session).find_next_expired_run_id()
        if run_id is None:
            break
        try:
            with session_factory() as session:
                result = RunCoordinator(session).close_expired_run_with_derived_decision(run_id)
        except RunLeaseNotExpiredError:
            skipped.append(str(run_id))
            continue
        closed.append({
            "run_id": str(run_id), "task_id": str(result["task_id"]),
            "failure_id": str(result["failure_id"]),
            "recovery_action": result["recovery_action"],
            "signature": result["signature"],
            "policy_reasons": list(result["policy_reasons"]),
        })
    return {"closed": closed, "skipped": skipped}


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or close expired Runs; no Agent execution and no model call.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect-expired-run", metavar="RUN_ID",
                      help="Report whether this Run may be taken over or must be closed.")
    mode.add_argument("--close-expired-runs", action="store_true",
                      help="Close expired Runs without starting their recovery Run.")
    mode.add_argument("--reclaim-expired-runs", action="store_true",
                      help="Close expired Runs and start their recovery Run; needs model gateways.")
    mode.add_argument("--run-recovery-loop", action="store_true",
                      help="Reclaim expired Runs for a bounded time with backoff; needs model gateways.")
    mode.add_argument("--take-over-expired-run", metavar="RUN_ID",
                      help="Take over one expired Run and continue it if no node was left running.")
    parser.add_argument("--max-runs", type=int, default=1,
                        help="With --close-expired-runs: close at most this many Runs.")
    parser.add_argument("--max-scans", type=int, default=1,
                        help="With --reclaim-expired-runs: perform at most this many scans.")
    parser.add_argument("--run-for-seconds", type=float,
                        help="With --run-recovery-loop: total wall-clock budget (required).")
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0,
                        help="With --run-recovery-loop: wait between scans.")
    parser.add_argument("--max-consecutive-failures", type=int, default=3,
                        help="With --run-recovery-loop: give up after this many failures in a row.")
    parser.add_argument("--workspaces-root", help="With --reclaim-expired-runs: existing directory.")
    parser.add_argument("--artifacts-root", help="With --reclaim-expired-runs: existing directory.")
    parser.add_argument("--max-read-bytes", type=int, default=DEFAULT_MAX_READ_BYTES,
                        help="With --reclaim-expired-runs: per-read limit for recovery.")
    parser.add_argument("--worker-id",
                        help="Actor recorded for the command; defaults to a fresh identifier.")
    args = parser.parse_args(argv)

    run_id = None
    raw_run_id = (args.inspect_expired_run if args.inspect_expired_run is not None
                  else args.take_over_expired_run)
    if raw_run_id is not None:
        try:
            run_id = UUID(str(raw_run_id))
        except (ValueError, TypeError, AttributeError):
            print(json.dumps({
                "event": "control_configuration_error",
                "detail": "Run ID must be a UUID",
            }), file=sys.stderr)
            return 2
    worker_id = args.worker_id if args.worker_id is not None else f"control-{uuid4()}"
    if not worker_id.strip():
        print(json.dumps({
            "event": "control_configuration_error",
            "detail": "worker_id must be a non-blank string",
        }), file=sys.stderr)
        return 2
    if args.close_expired_runs and (isinstance(args.max_runs, bool) or args.max_runs <= 0):
        print(json.dumps({
            "event": "control_configuration_error",
            "detail": "max_runs must be a positive integer",
        }), file=sys.stderr)
        return 2
    if args.reclaim_expired_runs or args.run_recovery_loop or args.take_over_expired_run is not None:
        for name in ("max_scans", "max_read_bytes"):
            value = getattr(args, name)
            if isinstance(value, bool) or value <= 0:
                print(json.dumps({
                    "event": "control_configuration_error",
                    "detail": f"{name} must be a positive integer",
                }), file=sys.stderr)
                return 2
        roots = {}
        for name, value in (("workspaces_root", args.workspaces_root),
                            ("artifacts_root", args.artifacts_root)):
            if value is None or not Path(value).is_dir():
                print(json.dumps({
                    "event": "control_configuration_error",
                    "detail": f"{name.replace('_', '-')} must be an existing directory",
                }), file=sys.stderr)
                return 2
            roots[name] = Path(value)
    if args.run_recovery_loop:
        if args.run_for_seconds is None or not isfinite(args.run_for_seconds) \
                or args.run_for_seconds <= 0:
            print(json.dumps({
                "event": "control_configuration_error",
                "detail": "run-for-seconds must be finite and positive",
            }), file=sys.stderr)
            return 2
        if not isfinite(args.poll_interval_seconds) or args.poll_interval_seconds <= 0:
            print(json.dumps({
                "event": "control_configuration_error",
                "detail": "poll-interval-seconds must be finite and positive",
            }), file=sys.stderr)
            return 2
        if isinstance(args.max_consecutive_failures, bool) or args.max_consecutive_failures <= 0:
            print(json.dumps({
                "event": "control_configuration_error",
                "detail": "max-consecutive-failures must be a positive integer",
            }), file=sys.stderr)
            return 2

    engine = None
    try:
        if session_factory is None:
            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        if args.inspect_expired_run is not None:
            payload = {"event": "control_run_inspected", **inspect_expired_run(session_factory, run_id)}
        elif args.close_expired_runs:
            sweep = close_expired_runs(session_factory, worker_id, max_runs=args.max_runs)
            payload = {"event": "control_expired_runs_closed", "worker_id": worker_id,
                       "max_runs": args.max_runs, **sweep}
        else:
            # Only entering the gateway context is a configuration concern; everything
            # after it is a runtime fault and keeps its own exit code.
            with ExitStack() as stack:
                try:
                    gateways = stack.enter_context(
                        model_gateways_from_env(session_factory, worker_id))
                except ValueError:
                    print(json.dumps({
                        "event": "control_configuration_error",
                        "detail": "Model gateways are not configured",
                    }), file=sys.stderr)
                    return 2
                if args.reclaim_expired_runs:
                    sweep = reclaim_expired_runs(
                        session_factory, gateways, worker_id=worker_id,
                        workspaces_root=roots["workspaces_root"],
                        artifacts_root=roots["artifacts_root"],
                        max_scans=args.max_scans, max_read_bytes=args.max_read_bytes)
                    payload = {"event": "control_expired_runs_reclaimed", "worker_id": worker_id,
                               "max_scans": args.max_scans, **sweep}
                elif args.take_over_expired_run is not None:
                    try:
                        taken = Worker(session_factory, worker_id).take_over_expired_run_and_resume(
                            run_id,
                            workspaces_root=roots["workspaces_root"],
                            artifacts_root=roots["artifacts_root"],
                            supervisor_gateway=gateways.supervisor,
                            explorer_gateway=gateways.explorer,
                            planner_gateway=gateways.planner,
                            max_read_bytes=args.max_read_bytes, stop_event=Event())
                    except RunLeaseNotExpiredError:
                        print(json.dumps({
                            "event": "control_run_not_expired", "run_id": str(run_id),
                            "detail": "Run is not RUNNING with an expired lease",
                        }), file=sys.stderr)
                        return 1
                    except (WorkspaceQuarantinedError, WorkspaceLockUnavailableError) as error:
                        print(json.dumps({
                            "event": "control_takeover_refused", "run_id": str(run_id),
                            "error_type": type(error).__name__,
                            "detail": "Workspace is not free of in-flight work",
                        }), file=sys.stderr)
                        return 1
                    payload = {"event": "control_expired_run_taken_over",
                               "worker_id": worker_id, **_compact_takeover(taken)}
                else:
                    loop = run_recovery_loop_with_backoff(
                        Worker(session_factory, worker_id), gateways,
                        workspaces_root=roots["workspaces_root"],
                        artifacts_root=roots["artifacts_root"],
                        run_for_seconds=args.run_for_seconds,
                        poll_interval_seconds=args.poll_interval_seconds,
                        max_consecutive_failures=args.max_consecutive_failures,
                        max_read_bytes=args.max_read_bytes,
                        on_result=lambda record: print(json.dumps(
                            {"event": "control_recovery_result", **record})),
                    )
                    payload = {"event": "control_recovery_loop_finished",
                               "worker_id": worker_id,
                               "run_for_seconds": args.run_for_seconds, **loop}
    except Exception as error:
        # CLI boundary: report a nonzero failure without exposing DSNs or SQL.
        print(json.dumps({
            "event": "control_command_failed",
            "error_type": type(error).__name__,
            "detail": "Control command failed",
        }), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
