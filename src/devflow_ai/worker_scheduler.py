"""Run one bounded attempt per registered Task, and wait when there is nothing to do.

This is the resident scheduler from ``docs/WORKER_SCHEDULER_PLAN.md``. It does not discover
work: the trusted side names the Tasks in a local manifest, and only those entries are ever
considered. Each entry goes through the one-shot ``worker_run`` path, so claiming, the Task
row lock, execution rights, the pinned Policy and the stage driver are the same code — this
module adds ordering and waiting, not a second executor.

What it deliberately does not do: retry an attempt, take over another worker's Run, replay
orphaned nodes, close a Run it did not start, or change a Task's configuration. A Task that
cannot be run says why and is dropped from this process; the remaining entries still run.

Stopping is cooperative. The signal reaches the stages of the task that is running, and the
loop stops claiming new work; nothing here interrupts a call already in flight.

Revocation timing: a resident round re-reads the manifest before **every** entry, so an
entry withdrawn while an earlier one was running is not claimed from the round's opening
snapshot. The read and the database claim are still two steps, not one transaction — a
manifest replaced in the microseconds between them is not observed by this process, and the
next read is the first chance to see it. What holds regardless: no claim is ever made
without a successful read immediately before it, and the Task's commit and Policy are
re-checked inside the claim's row lock by ``start_initial_run``.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy import select

from devflow_ai.benchmark_run import BenchmarkLimits
from devflow_ai.config import HeartbeatConfig
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import Run
from devflow_ai.worker import Worker
from devflow_ai.worker_run import (
    PreparedWorkerRun, WorkerRunBusyError, WorkerRunError, WorkerRunExecutionError,
    WorkerRunRequest, prepare_worker_run, run_worker_once, stop_on_termination_signal,
)

DEFAULT_POLL_INTERVAL_SECONDS = 5.0

# Per-entry states. ``pending`` can still be claimed; ``waiting`` lost a lock race and is
# retried after the poll interval; ``refused`` could not be run at all; ``handled`` and
# ``refused`` are concluded, and ``removed`` means the entry left the manifest unclaimed.
PENDING, WAITING, HANDLED = "pending", "waiting", "handled"
REFUSED, REMOVED = "refused", "removed"
CONCLUDED = (HANDLED, REFUSED)


class SchedulerManifestError(RuntimeError):
    """The manifest cannot be used; nothing was attempted."""


class SchedulerTaskEntry(BaseModel):
    """One registered Task: its id, its pack and the Policy file prepared for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    pack: str
    policy: str
    # Path to trusted independent-evaluation material, kept by the control plane outside the
    # repository. The manifest carries the path, never the material itself, and the loader
    # still checks the file's digest against the pack before it is used.
    material: str | None = None
    # Where this attempt's reviewable run record goes. One path per entry: the record is
    # created exclusively and never overwritten, so two entries may not share one.
    record: str | None = None


class SchedulerManifest(BaseModel):
    """The trusted task list. Unknown fields are rejected so it cannot carry extra material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    tasks: tuple[SchedulerTaskEntry, ...]

    @model_validator(mode="after")
    def entries_are_usable(self) -> "SchedulerManifest":
        if self.schema_version != 1:
            raise ValueError("Unsupported manifest schema version")
        seen = set()
        for entry in self.tasks:
            if entry.task_id in seen:
                raise ValueError("The manifest registers the same Task twice")
            seen.add(entry.task_id)
            if not entry.pack.strip() or not entry.policy.strip():
                raise ValueError("Every entry needs a pack and a policy path")
            if entry.material is not None and not entry.material.strip():
                raise ValueError("A material path, when given, must not be blank")
            if entry.record is not None and not entry.record.strip():
                raise ValueError("A record path, when given, must not be blank")
        return self


def load_scheduler_manifest(path: Path, *, allow_empty: bool = False) -> SchedulerManifest:
    """Read and validate one manifest; relative paths resolve against its own directory.

    ``allow_empty`` exists for the resident mode, where an operator may start with an empty
    list and register Tasks later. The bounded batch mode still requires at least one entry:
    a batch with nothing to do would otherwise be indistinguishable from a typo.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SchedulerManifestError(
            f"The manifest is unreadable: {type(error).__name__}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchedulerManifestError(
            f"The manifest is not valid JSON: {type(error).__name__}") from None
    try:
        manifest = SchedulerManifest.model_validate(payload)
    except ValidationError as error:
        # Only the field locations: a rejected manifest may contain anything, and echoing it
        # would copy whatever the file held into the logs.
        locations = ", ".join(".".join(str(part) for part in item["loc"])
                              for item in error.errors(include_input=False))
        raise SchedulerManifestError(
            f"The manifest is not valid; check: {locations}") from None
    if not manifest.tasks and not allow_empty:
        raise SchedulerManifestError("The manifest registers no Tasks")
    base = path.parent
    resolved = []
    for entry in manifest.tasks:
        resolved.append(SchedulerTaskEntry(
            task_id=entry.task_id,
            pack=str(_resolve(base, entry.pack)),
            policy=str(_resolve(base, entry.policy)),
            material=(str(_resolve(base, entry.material))
                      if entry.material is not None else None),
            record=(str(_resolve(base, entry.record))
                    if entry.record is not None else None)))
    records = [entry.record for entry in resolved if entry.record is not None]
    if len(set(records)) != len(records):
        # Each record is evidence for one attempt; a shared path would make the second
        # write collide (or worse, look like the first attempt's evidence).
        raise SchedulerManifestError(
            "The manifest names the same record path for more than one Task")
    return SchedulerManifest(schema_version=manifest.schema_version, tasks=tuple(resolved))


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (base / candidate)


@dataclass(frozen=True)
class SchedulerRequest:
    """Everything the operator states once for every registered Task."""

    manifest_path: Path
    operator: str
    python_executable: str
    workspaces_root: Path
    artifacts_root: Path
    attempt_kind: str = "formal"
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    # Batch mode (default) works through one manifest read and exits. Resident mode keeps
    # re-reading the same manifest until it is stopped.
    resident: bool = False
    limits: BenchmarkLimits = field(default_factory=BenchmarkLimits)
    max_read_bytes: int = 65536
    max_patch_bytes: int = 65536
    max_diff_bytes: int = 262144

    def __post_init__(self) -> None:
        interval = self.poll_interval_seconds
        # NaN and infinity would make the wait either return immediately or never, so they
        # are refused here rather than turning into an unexpected tight loop.
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not isfinite(interval) or interval <= 0):
            raise ValueError("poll_interval_seconds must be a finite positive number")


def _request_for(entry: SchedulerTaskEntry, request: SchedulerRequest) -> WorkerRunRequest:
    return WorkerRunRequest(
        pack_dir=Path(entry.pack), policy_path=Path(entry.policy), task_id=entry.task_id,
        operator=request.operator, python_executable=request.python_executable,
        workspaces_root=request.workspaces_root,
        artifacts_root=request.artifacts_root, attempt_kind=request.attempt_kind,
        material_path=(Path(entry.material) if entry.material is not None else None),
        record_path=(Path(entry.record) if entry.record is not None else None),
        limits=request.limits)


@dataclass
class _EntryRecord:
    task_id: UUID
    state: str = PENDING
    outcome: str | None = None
    reason: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    detail: str | None = None
    # What the entry looked like when it was judged: the pack and policy paths plus a digest
    # of both files. A refusal is re-checked when this changes, which is the operator's
    # signal that the configuration was fixed.
    signature: tuple | None = None
    present: bool = True
    # What the trusted evaluation did for this entry, exactly as the entry reported it.
    finalization: dict | None = None
    # The reviewable record this attempt produced, if the manifest asked for one.
    record_path: str | None = None
    record_written: bool = False
    record_outcome: str | None = None
    record_error: str | None = None

    def as_dict(self) -> dict:
        return {"task_id": str(self.task_id), "state": self.state,
                "outcome": self.outcome, "reason": self.reason, "run_id": self.run_id,
                "run_status": self.run_status, "detail": self.detail,
                "finalization": self.finalization, "record_path": self.record_path,
                "record_written": self.record_written,
                "record_outcome": self.record_outcome,
                "record_error": self.record_error, "registered": self.present}


def run_scheduler(
    request: SchedulerRequest, *, session_factory, worker_id: str, gateways,
    heartbeat_config: HeartbeatConfig, stop_event: Event | None = None,
    run_once=None, wait=None, prepare=None, report=None,
) -> dict:
    """Work through the manifest until it is concluded (batch) or until stopped (resident).

    ``run_once``, ``prepare``, ``wait`` and ``report`` default to the real implementations;
    tests replace them to observe ordering without touching a provider. ``report`` receives
    state transitions, so an operator can see a resident scheduler waiting for its manifest
    to be repaired.
    """
    prepare = prepare or (lambda req: prepare_worker_run(req, session_factory=session_factory))
    run_once = run_once or run_worker_once
    wait = wait or _wait_for_next_round
    stop_event = stop_event if stop_event is not None else Event()
    report = report or (lambda event: None)
    worker = Worker(session_factory, worker_id)
    shared = dict(session_factory=session_factory, worker=worker, gateways=gateways,
                  heartbeat_config=heartbeat_config, stop_event=stop_event,
                  run_once=run_once, prepare=prepare)
    if request.resident:
        return _run_resident(request, records={}, wait=wait, report=report, **shared)
    manifest = load_scheduler_manifest(request.manifest_path)
    records = {entry.task_id: _EntryRecord(task_id=entry.task_id) for entry in manifest.tasks}
    rounds = waits = 0
    while True:
        if stop_event.is_set():
            return _summary(records, rounds, waits, stop_requested=True, mode="batch")
        for entry in manifest.tasks:
            if stop_event.is_set():
                break
            record = records[entry.task_id]
            # A ``waiting`` entry lost a lock race, not an attempt: it is tried again next
            # round. ``handled``/``refused``/``removed`` are concluded and never revisited.
            if record.state not in (PENDING, WAITING):
                continue
            _run_entry(entry, request, record=record, **shared)
        rounds += 1
        if stop_event.is_set():
            return _summary(records, rounds, waits, stop_requested=True, mode="batch")
        if all(record.state in CONCLUDED for record in records.values()):
            # Every registered Task is concluded for this process: claiming is finished, and
            # staying alive would only wait for work this entry is not allowed to discover.
            return _summary(records, rounds, waits, stop_requested=False, mode="batch")
        waits += 1
        wait(stop_event, request.poll_interval_seconds)


def _run_resident(
    request: SchedulerRequest, *, session_factory, worker, gateways, heartbeat_config,
    stop_event, run_once, prepare, wait, report, records,
) -> dict:
    """Keep re-reading the manifest and claim only what the latest successful read lists.

    An unreadable or invalid manifest pauses claiming: the previous list is *not* reused, so
    a half-written or deleted file cannot keep old authorisation alive. That read happens
    again **before every entry**, because a round's entries are not claimed back to back:
    the operator can revoke or rewrite the file while an earlier entry is still running, and
    the next claim must use the newest list rather than the one the round started with.
    """
    rounds = waits = 0
    paused = {"active": False, "rounds": 0, "errors": 0, "detail": None}
    registered: set | None = None
    while True:
        if stop_event.is_set():
            return _summary(records, rounds, waits, stop_requested=True, mode="resident",
                            paused=paused)
        try:
            manifest = load_scheduler_manifest(request.manifest_path, allow_empty=True)
            failure = None
        except SchedulerManifestError as error:
            failure = str(error) or "The manifest is unavailable"
        if failure is not None:
            paused["rounds"] += 1
            _note_pause(paused, failure, report)
        else:
            _note_resume(paused, manifest, report)
            current = _reconcile(records, manifest)
            if registered != set(current):
                registered = set(current)
                report({"event": "worker_scheduler_registered", "tasks": len(current)})
            attempted = set()
            while not stop_event.is_set():
                entry = _next_claimable(manifest, records, attempted)
                if entry is None:
                    break
                # Re-read before touching the database: the authorisation for this entry may
                # have been withdrawn while the previous one was running.
                try:
                    fresh = load_scheduler_manifest(request.manifest_path, allow_empty=True)
                    failure = None
                except SchedulerManifestError as error:
                    failure = str(error) or "The manifest is unavailable"
                if failure is not None:
                    # Nothing is claimed from the old list; the round ends in a pause.
                    _note_pause(paused, failure, report)
                    break
                _note_resume(paused, fresh, report)
                current = _reconcile(records, fresh)
                if registered != set(current):
                    registered = set(current)
                    report({"event": "worker_scheduler_registered", "tasks": len(current)})
                latest = current.get(entry.task_id)
                if latest is None or (latest.pack, latest.policy) != (entry.pack, entry.policy):
                    # The entry was revoked or rewritten: take the fresh version on the next
                    # pass instead of claiming the stale one.
                    attempted.add(entry.task_id)
                    continue
                record = records[latest.task_id]
                if record.state not in (PENDING, WAITING):
                    attempted.add(entry.task_id)
                    continue
                attempted.add(entry.task_id)
                _run_entry(latest, request, record=record, session_factory=session_factory,
                           worker=worker, gateways=gateways,
                           heartbeat_config=heartbeat_config, stop_event=stop_event,
                           run_once=run_once, prepare=prepare)
        rounds += 1
        if stop_event.is_set():
            break
        waits += 1
        wait(stop_event, request.poll_interval_seconds)
    return _summary(records, rounds, waits, stop_requested=True, mode="resident", paused=paused)


def _next_claimable(manifest, records, attempted: set):
    """The first entry in manifest order that still needs an attempt in this round."""
    for entry in manifest.tasks:
        if entry.task_id in attempted:
            continue
        record = records.get(entry.task_id)
        if record is not None and record.state in (PENDING, WAITING):
            return entry
    return None


def _reconcile(records: dict, manifest) -> dict:
    """Match the records to one manifest read and return the current entries by Task id."""
    current = {entry.task_id: entry for entry in manifest.tasks}
    for task_id, record in records.items():
        record.present = task_id in current
        if task_id not in current and record.state in (PENDING, WAITING):
            # Removed before it was claimed: it must not be claimed from memory later.
            record.state = REMOVED
            record.outcome = "removed"
    for entry in manifest.tasks:
        signature = _entry_signature(entry)
        record = records.get(entry.task_id)
        if record is None or record.state == REMOVED:
            record = _EntryRecord(task_id=entry.task_id, signature=signature)
            records[entry.task_id] = record
        if record.state == REFUSED and record.signature != signature:
            # The pack or Policy file changed: that is the operator's repair signal, so this
            # entry is checked again once per change, not once per poll.
            record.state = PENDING
            record.outcome = record.reason = record.detail = None
        record.signature = signature
    return current


def _note_pause(paused: dict, detail: str, report) -> None:
    """Count one failed manifest read and report the transition into the paused state."""
    paused["errors"] += 1
    paused["detail"] = detail
    if not paused["active"]:
        paused["active"] = True
        report({"event": "worker_scheduler_paused", "detail": detail})


def _note_resume(paused: dict, manifest, report) -> None:
    """Report the transition out of the paused state after a successful read."""
    if paused["active"]:
        paused["active"] = False
        report({"event": "worker_scheduler_resumed", "tasks": len(manifest.tasks)})


def _entry_signature(entry: SchedulerTaskEntry) -> tuple:
    """Identify the inputs a refusal depended on: the paths and the files' bytes.

    The material file's digest is part of it too: replacing the material is a configuration
    change in exactly the same sense as editing the pack.
    """
    return (entry.pack, entry.policy, entry.material, entry.record,
            _file_digest(Path(entry.pack) / "task.json"),
            _file_digest(Path(entry.policy)),
            _file_digest(Path(entry.material)) if entry.material is not None else None)


def _file_digest(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _run_entry(
    entry: SchedulerTaskEntry, request: SchedulerRequest, *, session_factory, worker,
    gateways, heartbeat_config, stop_event, record: _EntryRecord, prepare, run_once,
) -> None:
    """Run at most one attempt for one entry, recording why it stopped being considered."""
    entry_request = _request_for(entry, request)
    try:
        prepared: PreparedWorkerRun = prepare(entry_request)
        result = run_once(
            entry_request, session_factory=session_factory, worker=worker,
            gateways=gateways, heartbeat_config=heartbeat_config,
            prepared=prepared, stop_event=stop_event,
            max_read_bytes=request.max_read_bytes, max_patch_bytes=request.max_patch_bytes,
            verifier_max_diff_bytes=request.max_diff_bytes)
    except WorkerRunBusyError as error:
        # Another worker holds the Task row right now. Nothing was claimed and no attempt was
        # made, so this entry waits for the next round instead of being given up on.
        record.state = WAITING
        record.outcome = "waiting"
        record.detail = str(error) or "The Task row is locked"
        return
    except WorkerRunError as error:
        # A configuration or state problem: this Task cannot be run, and saying so must not
        # stop the other entries. In resident mode a changed pack or Policy file re-opens it.
        record.state = REFUSED
        record.outcome = "refused"
        record.reason = "configuration_unusable"
        record.detail = str(error) or "The Task cannot be run"
        return
    except WorkerRunExecutionError as error:
        # The Run was claimed and execution failed; its recorded state is left untouched and
        # nothing is retried or cleaned up here. The state is read back so the report says
        # what actually happened to the Run instead of leaving it blank.
        record.state = HANDLED
        record.outcome = "failed"
        record.reason = "execution_error"
        record.run_id = str(error.run_id)
        record.run_status = _read_run_status(session_factory, error.run_id)
        record.detail = type(error.error).__name__
        produced = error.record or {}
        record.record_path = produced.get("path")
        record.record_written = bool(produced.get("written"))
        record.record_outcome = produced.get("outcome")
        record.record_error = produced.get("error")
        return
    record.outcome = result.get("outcome")
    record.reason = result.get("reason")
    record.run_id = result.get("run_id")
    record.run_status = result.get("run_status")
    record.finalization = result.get("finalization")
    record.record_path = result.get("record_path")
    record.record_written = bool(result.get("writes_record"))
    record.record_outcome = result.get("record_outcome")
    record.record_error = result.get("record_error")
    # Claimed or already claimed by an earlier process: either way this Task has had its
    # attempt, and the loop must not execute it again.
    record.state = HANDLED


def _read_run_status(session_factory, run_id) -> str | None:
    """Read one Run's status without changing anything."""
    try:
        with session_factory() as session:
            status = session.scalar(select(Run.status).where(Run.id == run_id))
    except Exception:  # a report must survive a database hiccup
        return None
    return status.value if status is not None else None


def _wait_for_next_round(stop_event: Event, seconds: float) -> bool:
    """Idle wait that wakes as soon as the stop signal is set."""
    return stop_event.wait(seconds)


def _summary(records, rounds: int, waits: int, *, stop_requested: bool, mode: str,
             paused: dict | None = None) -> dict:
    entries = [record.as_dict() for record in records.values()]
    counts: dict[str, int] = {}
    for record in records.values():
        key = record.outcome or "not_attempted"
        counts[key] = counts.get(key, 0) + 1
    paused = paused or {}
    committed = any(record.run_status == "PASSED" for record in records.values())
    return {
        "outcome": "stopped" if stop_requested else "idle_exit",
        "mode": mode,
        "stop_requested": stop_requested, "rounds": rounds, "waited_rounds": waits,
        "entries": entries, "counts": counts,
        "manifest_paused": bool(paused.get("active")),
        "manifest_paused_rounds": paused.get("rounds", 0),
        "manifest_errors": paused.get("errors", 0),
        "manifest_error": paused.get("detail"),
        # A record is written only when the manifest asked for one, and always through the
        # same exclusive-create path. A trusted, committed Verification is the only thing
        # that makes an acceptance label honest; stopping at a draft is not one.
        "writes_record": any(record.record_written for record in records.values()),
        "formal_acceptance": ("committed" if committed else "not_performed"),
    }


def _report_to_stderr(event: dict) -> None:
    """Emit one scheduler state transition; never includes manifest contents."""
    print(json.dumps(event, default=str), file=sys.stderr)


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "worker_scheduler_configuration_error", "detail": detail}),
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None, *, session_factory=None, gateways_factory=None) -> int:
    """Schedule registered Tasks, in bounded batch mode by default or resident with ``--watch``.

    Exit codes: 0 when the loop concluded (per-Task failures are reported in the summary),
    2 when the manifest or the shared configuration is unusable and nothing was attempted,
    1 when the scheduler itself failed. There is no retry flag: an attempt is never repeated.
    """
    parser = argparse.ArgumentParser(
        description="Schedule one bounded attempt per registered Task; no retry, no takeover.")
    parser.add_argument("--manifest", required=True,
                        help="Trusted task manifest: only the Tasks named here are considered.")
    parser.add_argument("--operator", required=True,
                        help="Person or account running these attempts.")
    parser.add_argument("--python-executable", required=True,
                        help="Absolute interpreter used for the public checks.")
    parser.add_argument("--workspaces-root", required=True,
                        help="Existing directory for Run workspaces.")
    parser.add_argument("--artifacts-root", required=True,
                        help="Existing directory for Run artifacts.")
    parser.add_argument("--attempt-kind", choices=["formal", "rehearsal"], default="formal",
                        help="Applied to every registered Task; a formal attempt is refused "
                             "where the trusted baseline cannot be recorded.")
    parser.add_argument("--watch", action="store_true",
                        help="Resident mode: keep re-reading the manifest at the poll interval "
                             "instead of exiting once every registered Task is concluded. "
                             "The batch mode stays the default.")
    parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
                        help="Wait between rounds: idle wait in batch mode, manifest re-read "
                             "interval in resident mode. Must be a finite positive number.")
    parser.add_argument("--worker-id", help="Actor recorded on the Runs; defaults to a fresh id.")
    parser.add_argument("--max-duration-seconds", type=int, default=1200)
    parser.add_argument("--max-model-tokens", type=int, default=80000)
    parser.add_argument("--max-stages", type=int, default=32)
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    parser.add_argument("--max-patch-bytes", type=int, default=65536)
    parser.add_argument("--max-diff-bytes", type=int, default=262144)
    parser.add_argument("--statement-timeout-seconds", type=int, default=5)
    parser.add_argument("--shutdown-timeout-seconds", type=int, default=5)
    parser.add_argument("--confirm-run", action="store_true",
                        help="Required: acknowledges that registered Tasks will spend budget.")
    args = parser.parse_args(argv)

    if not args.confirm_run:
        return _configuration_error("--confirm-run is required to start registered Runs")
    if not args.operator.strip():
        return _configuration_error("operator must be a non-blank string")
    worker_id = args.worker_id if args.worker_id is not None else f"scheduler-{uuid4()}"
    if not worker_id.strip():
        return _configuration_error("worker-id must be a non-blank string")
    for name in ("max_read_bytes", "max_patch_bytes", "max_diff_bytes", "max_stages",
                 "max_model_tokens", "max_duration_seconds",
                 "statement_timeout_seconds", "shutdown_timeout_seconds"):
        value = getattr(args, name)
        if isinstance(value, bool) or value <= 0:
            return _configuration_error(f"{name.replace('_', '-')} must be a positive integer")
    if (not isfinite(args.poll_interval_seconds)) or args.poll_interval_seconds <= 0:
        # NaN and infinity parse as floats: NaN would never wait meaningfully and infinity
        # would hang the loop, so both are refused rather than accepted as an interval.
        return _configuration_error(
            "poll-interval-seconds must be a finite positive number")
    for name in ("workspaces_root", "artifacts_root"):
        if not Path(getattr(args, name)).is_dir():
            return _configuration_error(f"{name.replace('_', '-')} must be an existing directory")
    try:
        # An empty list is a legitimate starting point only for resident mode; a batch with
        # nothing to do is indistinguishable from a mistyped manifest.
        manifest = load_scheduler_manifest(Path(args.manifest), allow_empty=args.watch)
    except SchedulerManifestError as error:
        return _configuration_error(str(error))

    try:
        request = SchedulerRequest(
            manifest_path=Path(args.manifest), operator=args.operator,
            python_executable=args.python_executable,
            workspaces_root=Path(args.workspaces_root),
            artifacts_root=Path(args.artifacts_root), attempt_kind=args.attempt_kind,
            poll_interval_seconds=args.poll_interval_seconds, resident=args.watch,
            max_read_bytes=args.max_read_bytes, max_patch_bytes=args.max_patch_bytes,
            max_diff_bytes=args.max_diff_bytes,
            limits=BenchmarkLimits(
                max_duration_seconds=args.max_duration_seconds,
                max_model_tokens=args.max_model_tokens, max_stages=args.max_stages))
    except ValueError as error:
        return _configuration_error(str(error))
    engine = None
    try:
        if session_factory is None:
            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        with ExitStack() as stack:
            try:
                gateways = stack.enter_context(
                    gateways_factory(session_factory, worker_id)
                    if gateways_factory is not None
                    else model_gateways_from_env(session_factory, worker_id))
            except ValueError:
                return _configuration_error("Model gateways are not configured")
            with stop_on_termination_signal() as stop_event:
                summary = run_scheduler(
                    request, session_factory=session_factory, worker_id=worker_id,
                    gateways=gateways, stop_event=stop_event,
                    report=_report_to_stderr,
                    heartbeat_config=HeartbeatConfig(
                        statement_timeout_seconds=args.statement_timeout_seconds,
                        shutdown_timeout_seconds=args.shutdown_timeout_seconds))
    except SchedulerManifestError as error:
        return _configuration_error(str(error))
    except Exception as error:
        print(json.dumps({"event": "worker_scheduler_failed",
                          "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps({
        "event": "worker_scheduler_finished", **summary,
        "manifest": str(request.manifest_path), "operator": args.operator,
        "worker_id": worker_id, "attempt_kind": args.attempt_kind,
        "resident": args.watch,
        "registered_tasks_at_start": len(manifest.tasks),
        "limits": {"max_duration_seconds": request.limits.max_duration_seconds,
                   "max_model_tokens": request.limits.max_model_tokens,
                   "max_stages": request.limits.max_stages,
                   "attempts": request.limits.attempts,
                   "auto_retries": request.limits.auto_retries},
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
