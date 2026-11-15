"""Execute one already-created Task's Run, as a worker, until it states its stop.

Three entry points overlap here, so their responsibilities are kept apart:

* ``worker`` claims one Run and does not execute anything;
* ``benchmark_run`` claims, drives and writes a benchmark record for one attempt;
* this entry claims and drives one **named** Task, writes no record and no Verification,
  and reports where the stages stopped.

It is deliberately bounded rather than a scheduler. The caller names the Task, the task
pack and the directories, and every one of those is checked *before* a Run is created —
picking a Task out of the database first and discovering later that its configuration is
missing would already have changed state. It never retries, never takes over or closes
another Run, and never reports a stop as a success: whatever the stages reach, the Run
keeps the state the database actually recorded.

The trusted pytest baseline still needs the Windows Job report capture, so a *formal*
attempt is refused on a platform that cannot produce it; ``rehearsal`` runs the same code
path for an environment check and is not a formal sample.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Timer
from uuid import UUID, uuid4

from sqlalchemy import select

from devflow_ai.benchmark_record import AttemptKind
from devflow_ai.benchmark_run import (
    BenchmarkLimits, BenchmarkRunError, _model_id_from_environment, _now,
    compose_run_record, load_independent_material, policy_snapshot_differences,
)
from devflow_ai.benchmark_record import write_run_record
from devflow_ai.benchmark_task import BenchmarkTaskError, load_policy_snapshot
from devflow_ai.config import HeartbeatConfig
from devflow_ai.coordinator import RunCoordinator, RunExecutionDeniedError, TaskClaimBusyError
from devflow_ai.database import create_db_engine, create_session_factory
from devflow_ai.independent_evaluation import (
    MaterialPlacementError, require_material_placement,
)
from devflow_ai.enums import RunStatus, TaskStatus
from devflow_ai.harness import GitExecutionCancelledError
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import Run, Task
from devflow_ai.orchestration import drive_run_stages, finalize_benchmark_run
from devflow_ai.policy import PolicyConfig
from devflow_ai.services import TaskNotFoundError
from devflow_ai.task_pack import TaskPackManifest, load_manifest, to_verification_config
from devflow_ai.verification_config import VerificationConfig, public_test_modules
from devflow_ai.worker import LinkedStopEvent, Worker


class WorkerRunError(RuntimeError):
    """The configuration or the Task is unusable; nothing was claimed."""


class WorkerRunBusyError(WorkerRunError):
    """The Task row is locked by another transaction; nothing was claimed.

    Kept distinct so a scheduler can wait and retry the *claim* without that counting as a
    retried attempt: no Run was created, so there is nothing to repeat.
    """


class WorkerRunExecutionError(RuntimeError):
    """The Run was claimed and execution failed; its recorded state is left as it is."""

    def __init__(self, run_id: UUID, error: BaseException, record: dict | None = None):
        # Only the Run id and the error's type name: no DSN, SQL parameters or secrets.
        super().__init__(f"Run {run_id} stopped on {type(error).__name__}")
        self.run_id = run_id
        self.error = error
        # The record this attempt produced, when one was requested: the caller must be able
        # to report it even though the attempt failed.
        self.record = record or {}


@dataclass(frozen=True)
class WorkerRunRequest:
    """Everything the caller must state explicitly before a Run may be created."""

    pack_dir: Path
    policy_path: Path
    task_id: UUID
    operator: str
    # Absolute interpreter for the registered public-check command: the harness refuses a
    # relative executable and an Agent must never choose it.
    python_executable: str
    workspaces_root: Path
    artifacts_root: Path
    attempt_kind: str = "formal"
    # Trusted independent-evaluation material, named explicitly by the control plane. It is
    # read once, bound to this Task's pack by digest, and never copied into a prompt, the
    # task pack, the image or a log.
    material_path: Path | None = None
    # Where the reviewable run record goes. Exclusive create: an existing file is refused
    # before anything is claimed, and it is never overwritten when the attempt is replayed.
    record_path: Path | None = None
    limits: BenchmarkLimits = field(default_factory=BenchmarkLimits)


@dataclass(frozen=True)
class PreparedWorkerRun:
    """The checked configuration, produced without touching any Run."""

    manifest: TaskPackManifest
    config: VerificationConfig
    policy: PolicyConfig
    claim_key: str
    attempt_kind: AttemptKind
    trusted_baseline_supported: bool
    # Loaded from the path the caller named; ``None`` means this attempt stops at the draft
    # and reports why instead of pretending to be accepted.
    material: object | None = None
    # Set when this Task already owns the Run this entry would create: the call is a replay
    # and must report that Run instead of executing it again.
    existing_run_id: UUID | None = None


def trusted_baseline_platform_supported() -> bool:
    """Whether this platform can record a trusted pytest baseline report.

    The report capture is a Windows Job; on every other platform a baseline dispatch ends
    as ``BASELINE_UNAVAILABLE``, so a formal attempt here would spend budget to learn that.
    """
    return os.name == "nt"


def prepare_worker_run(
    request: WorkerRunRequest, *, session_factory,
    platform_supports_trusted_baseline: bool | None = None,
) -> PreparedWorkerRun:
    """Check the pack, the Task, the commit, the Policy and the directories.

    Every check runs before the claim, and every failure names what is wrong without
    echoing file contents. A Task that is not ELIGIBLE, that already has Run history, or
    whose pinned Policy does not match the prepared file is refused rather than claimed
    and discovered later.
    """
    try:
        attempt_kind = AttemptKind(request.attempt_kind)
    except ValueError:
        raise WorkerRunError(
            "attempt_kind must be formal, rehearsal or unknown") from None
    if not isinstance(request.operator, str) or not request.operator.strip():
        raise WorkerRunError("operator must be a non-blank string")
    supported = (platform_supports_trusted_baseline
                 if platform_supports_trusted_baseline is not None
                 else trusted_baseline_platform_supported())
    if attempt_kind is AttemptKind.FORMAL and not supported:
        # Not a courtesy warning: on such a platform the baseline dispatch cannot produce a
        # trusted report, so the attempt would burn its budget to reach a stated stop.
        raise WorkerRunError(
            "A formal attempt needs the Windows Job pytest report capture, which this "
            "platform does not provide; re-run as --attempt-kind rehearsal to exercise the "
            "stages without claiming a formal result")
    for name, path in (("workspaces-root", request.workspaces_root),
                       ("artifacts-root", request.artifacts_root)):
        if not path.is_dir():
            raise WorkerRunError(f"{name} must be an existing directory")
    executable = request.python_executable
    if not isinstance(executable, str) or not executable.strip():
        raise WorkerRunError("python-executable must be a non-blank string")
    if not os.path.isabs(executable):
        raise WorkerRunError("python-executable must be an absolute path")
    if not os.path.exists(executable):
        raise WorkerRunError("python-executable does not exist")
    try:
        manifest = load_manifest(request.pack_dir)
    except ValueError as error:
        raise WorkerRunError(f"Task pack is invalid: {type(error).__name__}") from None
    try:
        prepared_policy = load_policy_snapshot(request.policy_path)
    except BenchmarkTaskError:
        raise WorkerRunError("Prepared Policy snapshot is unreadable") from None
    config = to_verification_config(
        manifest, task_id=request.task_id, public_tests_root=request.pack_dir,
        python_executable=executable)
    try:
        # The registered command is pointed at a copy of the pack's public tests, so a pack
        # that cannot supply them would fail only after the claim had been committed.
        public_test_modules(config)
    except ValueError as error:
        raise WorkerRunError(
            f"The task pack cannot supply the public tests: {error}") from None
    with session_factory() as session:
        task = session.get(Task, request.task_id)
        if task is None:
            raise WorkerRunError("Task does not exist")
        # Placement is checked with the Task's own repository in hand, before the file is
        # read: trusted material must not sit in the pack, the Run's roots or a local
        # checkout. A remote repository cannot place this file, which is reported rather
        # than assumed safe.
        material = None
        if request.material_path is not None:
            try:
                require_material_placement(
                    request.material_path, pack_dir=request.pack_dir,
                    workspaces_root=request.workspaces_root,
                    artifacts_root=request.artifacts_root, repository=task.repo)
            except MaterialPlacementError as error:
                raise WorkerRunError(str(error)) from None
            suite = manifest.independent_suite
            try:
                # The loader digests the file's raw bytes and compares them with the pack's
                # pinned digest before parsing, so material for another pack (or a tampered
                # file) is refused here — before a Run exists. A pack that pins no digest
                # cannot accept material at all.
                material, _ = load_independent_material(
                    request.material_path,
                    expected_suite_version=(
                        suite.suite_version if suite is not None else None),
                    expected_sha256=(suite.materials_sha256 if suite is not None else None))
            except BenchmarkRunError as error:
                raise WorkerRunError(str(error)) from None
        claim_key = f"worker:{request.task_id}:1"
        # A repeat of this same call must be reported, not refused: the Task is RUNNING
        # *because* this entry already claimed it, and re-running the stages would be a
        # second attempt. This check comes before the eligibility and snapshot checks so a
        # replay never depends on the Task still matching a prepared file.
        runs = session.execute(
            select(Run.id, Run.creation_idempotency_key)
            .where(Run.task_id == task.id)).all()
        for run_id, creation_key in runs:
            if creation_key == claim_key:
                return PreparedWorkerRun(
                    manifest=manifest, config=config, policy=prepared_policy,
                    claim_key=claim_key, attempt_kind=attempt_kind,
                    trusted_baseline_supported=supported, material=material,
                    existing_run_id=run_id)
        if request.record_path is not None and Path(request.record_path).exists():
            # A run record is evidence: refuse before claiming rather than discovering the
            # collision afterwards, and never overwrite what is already there. A replay of
            # a Task that already has its Run is detected above and reports that instead.
            raise WorkerRunError(
                "The record path already exists; a run record is evidence and is never "
                "overwritten")
        # Status first: "this Task already started" says more than "its commit differs", and
        # an unadmitted Task has no commit to compare at all.
        if task.status is not TaskStatus.ELIGIBLE:
            raise WorkerRunError(
                f"Task {task.id} is {task.status.value}; a worker run claims only an "
                "ELIGIBLE Task")
        if task.base_commit != manifest.base_commit:
            raise WorkerRunError("Task base commit does not match the task pack")
        if not task.policy_snapshot:
            raise WorkerRunError("Task has no pinned Policy snapshot")
        try:
            policy = PolicyConfig.model_validate(task.policy_snapshot)
        except ValueError:
            raise WorkerRunError("Task Policy snapshot is invalid") from None
        if policy.policy_version != task.policy_version:
            raise WorkerRunError("Task policy_version does not match its policy_snapshot")
        if policy.budget.max_llm_tokens_per_run > request.limits.max_model_tokens:
            raise WorkerRunError(
                "Task policy allows more tokens than this run's limit; refusing to start")
        differing = policy_snapshot_differences(task.policy_snapshot, prepared_policy)
        if differing:
            raise WorkerRunError(
                "Task policy snapshot differs from the prepared one in: "
                + ", ".join(differing))
        if runs:
            # Another creator owns this Task's single Run; this entry must not start a
            # second attempt under a different claim key.
            raise WorkerRunError(
                "This Task already has a Run; a second attempt needs a new Task so both "
                "attempts stay independent")
    return PreparedWorkerRun(
        manifest=manifest, config=config, policy=policy, claim_key=claim_key,
        attempt_kind=attempt_kind, trusted_baseline_supported=supported,
        material=material)


def _finalize_stage(
    worker, run_id, *, prepared: PreparedWorkerRun, directories, stage_stop,
    max_patch_bytes: int | None, verifier_max_diff_bytes: int | None,
) -> dict:
    """Hand a reviewed draft to the trusted entry, or say why that did not happen.

    Nothing here decides a result: ``finalize_benchmark_run`` keeps its own gates (draft
    present, public checks passed, pinned suite version, evidence limits) and the coordinator
    keeps the execution-right and stop checks. Every outcome — a non-formal attempt, no
    material, stop requested, public checks failed, evaluation unavailable, evaluation
    error — is reported as it is.
    """
    if prepared.attempt_kind is not AttemptKind.FORMAL:
        # Only a formal attempt may enter the trusted transaction. A rehearsal or an
        # unlabelled attempt exercises the same stages, but committing a Verification or
        # leaving the Run marked as a formal pass would make the label meaningless.
        return {"outcome": "skipped",
                "reason": "attempt_kind_not_formal"}
    if prepared.material is None:
        return {"outcome": "skipped", "reason": "no_trusted_material"}
    if stage_stop.is_set():
        # Starting a trusted evaluation after the operator asked to stop would produce a
        # result nobody is waiting for; the draft stays where it is for a later attempt.
        return {"outcome": "not_finalized", "reason": "stop_requested"}
    return finalize_benchmark_run(
        worker, run_id, directories=directories,
        verification_config=prepared.config, material=prepared.material,
        max_diff_bytes=verifier_max_diff_bytes, max_patch_bytes=max_patch_bytes,
        stop_event=stage_stop)


def _write_record(
    request, *, session_factory, run_id, prepared: PreparedWorkerRun, stage_result: dict,
    deadline_fired: bool, started_at: str, error, stage_notes: str,
) -> dict:
    """Write the reviewable record for this attempt, or say why there is none.

    The outcome comes from the same classifier the benchmark entry uses, so a scheduled
    attempt is labelled by the same rules; nothing here invents a result, and the human
    decision is left empty on purpose. An existing file is never overwritten — that would
    rewrite evidence — and the collision is reported instead of raised, because the attempt
    itself already happened.
    """
    if request.record_path is None:
        return {"written": False, "path": None, "outcome": None, "error": None}
    finalization = stage_result.get("finalization") or {
        "outcome": "skipped",
        "reason": "not_attempted" if error is None else "execution_error"}
    try:
        with session_factory() as session:
            record, _reason, _cancelled = compose_run_record(
                session, run_id, manifest=prepared.manifest,
                task_id=prepared.config.task_id,
                independent_suite_version=prepared.config.independent_suite_version,
                policy_version=prepared.policy.policy_version, operator=request.operator,
                attempt_kind=prepared.attempt_kind,
                artifact_root=request.artifacts_root / str(run_id),
                finalization=finalization, deadline_fired=deadline_fired,
                artifacts_root=request.artifacts_root, started_at=started_at,
                limits=request.limits, error=error, stage_notes=stage_notes)
        write_run_record(record, request.record_path)
    except (ValueError, OSError) as failure:
        return {"written": False, "path": str(request.record_path), "outcome": None,
                "error": f"{type(failure).__name__}: {failure}"}
    return {"written": True, "path": str(request.record_path),
            "outcome": record.outcome.value, "error": None}


def run_worker_once(
    request: WorkerRunRequest, *, session_factory, worker: Worker, gateways,
    heartbeat_config: HeartbeatConfig, prepared: PreparedWorkerRun | None = None,
    stop_event: Event | None = None, max_read_bytes: int = 65536,
    max_patch_bytes: int | None = 65536, verifier_max_diff_bytes: int | None = 262144,
    drive=None,
) -> dict:
    """Claim the Task's first Run, drive its stages, and report where it stopped.

    ``stop_event`` is the caller's stop signal (a terminal signal in the CLI, a test's own
    event). The attempt deadline sets the same signal, so one mechanism bounds the whole
    call, and both are visible to the workspace initialization *and* to every stage that
    runs afterwards — a stop raised mid-stage has to reach the driver, not just the code
    before it. Stopping stays cooperative: nothing here interrupts a call in flight.
    ``drive`` replaces the stage driver for tests that only exercise the claim.
    """
    prepared = (prepared if prepared is not None
                else prepare_worker_run(request, session_factory=session_factory))
    if stop_event is not None and stop_event.is_set():
        # Nothing has been claimed or changed yet: report the stop instead of creating a Run
        # that would only be abandoned.
        return {"outcome": "stopped", "reason": "stop_requested", "claimed": False,
                "stages": 0, "run_id": None, "task_id": str(request.task_id),
                "run_status": None, "deadline_fired": False, "stop_requested": True,
                "attempt_kind": prepared.attempt_kind.value,
                "trusted_baseline_supported": prepared.trusted_baseline_supported,
                "writes_record": False, "formal_acceptance": "not_performed",
                "record_path": None, "record_outcome": None,
                "record_error": None}
    if prepared.existing_run_id is not None:
        # This Task's Run already exists under this entry's claim key: report it rather than
        # driving a second attempt. The CLI reaches this through ``prepare_worker_run``, so
        # both the API and the command line give the same answer.
        with session_factory() as session:
            existing_status = session.scalar(select(Run.status).where(
                Run.id == prepared.existing_run_id))
        return {"outcome": "existing_run", "claimed": False,
                "run_id": str(prepared.existing_run_id), "task_id": str(request.task_id),
                "reason": "existing_run", "stages": 0,
                "run_status": (existing_status.value
                               if existing_status is not None else None),
                "deadline_fired": False, "stop_requested": False,
                "attempt_kind": prepared.attempt_kind.value,
                "trusted_baseline_supported": prepared.trusted_baseline_supported,
                "writes_record": False, "formal_acceptance": "not_performed",
                "record_path": None, "record_outcome": None,
                "record_error": None}
    with session_factory() as session:
        try:
            # The Task row is locked for this transaction and the prepared base commit and
            # Policy are re-checked inside it, so a Task edited between preparation and the
            # claim is refused instead of producing a Run built on stale values.
            run, created = RunCoordinator(session, worker.lease_config).start_initial_run(
                request.task_id, worker.worker_id, prepared.claim_key,
                expected_base_commit=prepared.manifest.base_commit,
                expected_policy=prepared.policy)
        except TaskClaimBusyError as error:
            raise WorkerRunBusyError(str(error)) from None
        except (ValueError, LookupError) as error:
            # Task not ELIGIBLE, changed since preparation, Run history present or missing
            # Task: no Run was created by this call.
            raise WorkerRunError(str(error) or "The Task cannot be claimed") from None
        run_id, run_task_id = run.id, run.task_id
    if run_task_id != request.task_id:
        raise WorkerRunError("The claimed Run belongs to a different Task")
    if not created:
        # Replaying the claim would drive a Run that already exists; report it instead of
        # starting a second attempt against the same Task.
        with session_factory() as session:
            existing_status = session.scalar(select(Run.status).where(Run.id == run_id))
        return {"outcome": "existing_run", "claimed": False, "run_id": str(run_id),
                "task_id": str(run_task_id), "reason": "existing_run", "stages": 0,
                "run_status": (existing_status.value
                               if existing_status is not None else None),
                "deadline_fired": False, "stop_requested": False,
                "attempt_kind": prepared.attempt_kind.value,
                "trusted_baseline_supported": prepared.trusted_baseline_supported,
                "writes_record": False, "formal_acceptance": "not_performed",
                "record_path": None, "record_outcome": None,
                "record_error": None}

    started_at = _now()
    fired = {"deadline": False}
    deadline_stop = Event()

    def expire():
        fired["deadline"] = True
        deadline_stop.set()

    timer = Timer(request.limits.max_duration_seconds, expire)
    timer.start()
    signal_stop = deadline_stop if stop_event is None else LinkedStopEvent(
        deadline_stop, stop_event)
    stage_result: dict = {}

    def process(directories, actual_commit, execution_stop):
        # One stop for the whole drive: the heartbeat's own failure signal *and* the caller's
        # stop/deadline. Handing the driver the heartbeat event alone would make a deadline
        # raised mid-stage invisible to it.
        stage_stop = LinkedStopEvent(execution_stop, signal_stop)
        if drive is not None:
            drive(directories, stage_stop)
        else:
            stage_result.update(drive_run_stages(
                session_factory, run_id, worker=worker, gateways=gateways,
                directories=directories, stop_event=stage_stop,
                verification_config=prepared.config,
                max_stages=request.limits.max_stages, max_read_bytes=max_read_bytes,
                max_patch_bytes=max_patch_bytes,
                verifier_max_diff_bytes=verifier_max_diff_bytes))
        # The trusted evaluation runs inside the same heartbeat, lease, execution right and
        # stop event as the stages: it needs a live stop event, and a result committed after
        # this call returned would no longer be covered by either.
        try:
            finalization = _finalize_stage(
                worker, run_id, prepared=prepared, directories=directories,
                stage_stop=stage_stop, max_patch_bytes=max_patch_bytes,
                verifier_max_diff_bytes=verifier_max_diff_bytes)
        except Exception as error:
            # The failure is named in the result *and* propagated: a trusted evaluation that
            # could not run is not a result, and the caller must see the real error.
            stage_result["finalization"] = {"outcome": "finalization_error",
                                            "reason": type(error).__name__}
            raise
        stage_result["finalization"] = finalization
        return None

    interrupted = {"mid_stage": False}
    execution = {"error": None, "entered": False}

    try:
        worker.execute_run(
            run_id, workspaces_root=request.workspaces_root,
            artifacts_root=request.artifacts_root, heartbeat_config=heartbeat_config,
            process_run=process, external_stop_event=signal_stop)
    except (GitExecutionCancelledError, RunExecutionDeniedError) as caught:
        # Two cooperative shapes mean "stop": a cancelled git/tool call, and a stage that
        # refused to start because the Run had been asked to stop. Either is a stop only
        # when this call raised the signal; with no signal set it stays a real failure.
        if not (fired["deadline"] or (stop_event is not None and stop_event.is_set())):
            execution["error"] = caught
        else:
            interrupted["mid_stage"] = True
    except Exception as caught:  # the Run keeps whatever state it reached
        execution["error"] = caught
    finally:
        timer.cancel()

    with session_factory() as session:
        run_status = session.scalar(select(Run.status).where(Run.id == run_id))
    if interrupted["mid_stage"]:
        # The stage driver unwound before it could name its own stop, so the reason comes
        # from the signal that caused it and the stage count is honestly unknown.
        reason = "deadline_reached" if fired["deadline"] else "stop_requested"
        stages = None
    else:
        reason = stage_result.get("reason")
        stages = stage_result.get("stages")
    record = _write_record(
        request, session_factory=session_factory, run_id=run_id, prepared=prepared,
        stage_result=stage_result, deadline_fired=fired["deadline"],
        started_at=started_at, error=execution["error"],
        stage_notes=(f", stages={stages}, stopped={stage_result.get('reason')}"
                     + (", interrupted_mid_stage=true" if interrupted["mid_stage"]
                        else "")))
    if execution["error"] is not None:
        # The Run keeps its state and the record keeps the failure; the caller still sees the
        # real error rather than a quiet success.
        raise WorkerRunExecutionError(run_id, execution["error"], record) from execution["error"]
    return {
        "outcome": stage_result.get("outcome", "stopped"), "claimed": True,
        "run_id": str(run_id), "task_id": str(run_task_id),
        "reason": reason, "stages": stages,
        "interrupted_mid_stage": interrupted["mid_stage"],
        "run_status": run_status.value if run_status is not None else None,
        "deadline_fired": fired["deadline"],
        "stop_requested": bool(stop_event is not None and stop_event.is_set()),
        "attempt_kind": prepared.attempt_kind.value,
        "trusted_baseline_supported": prepared.trusted_baseline_supported,
        "writes_record": record["written"],
        "record_path": record["path"],
        "record_outcome": record["outcome"],
        "record_error": record["error"],
        # A committed trusted Verification is the only thing that makes this "committed";
        # stopping at a draft stays "not_performed" and is reported as such.
        "formal_acceptance": ("committed" if run_status is RunStatus.PASSED
                              else "not_performed"),
        "finalization": stage_result.get("finalization"),
        "trusted_suite_version": prepared.config.independent_suite_version,
        "material_supplied": prepared.material is not None,
        "pack_id": prepared.manifest.pack_id,
        "base_commit": prepared.manifest.base_commit,
    }


@contextmanager
def stop_on_termination_signal():
    """Yield an event that is set when the process is asked to stop.

    Installing handlers only works on the main thread; a caller that cannot install them
    still gets an unset event, and the attempt deadline remains the bound.
    """
    event = Event()

    def handler(signum, frame):  # noqa: ARG001 - the signal number is not needed
        event.set()

    previous = {}
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, handler)
        except (ValueError, OSError):  # not the main thread, or unsupported
            continue
    try:
        yield event
    finally:
        for number, original in previous.items():
            try:
                signal.signal(number, original)
            except (ValueError, OSError):
                continue


def _configuration_error(detail: str) -> int:
    print(json.dumps({"event": "worker_run_configuration_error", "detail": detail}),
          file=sys.stderr)
    return 2


def main(
    argv: list[str] | None = None, *, session_factory=None, gateways_factory=None,
) -> int:
    """Execute one named Task's Run.

    Exit codes: 0 when the call ran to a stated stop (or found the Task's Run already
    claimed), 2 when the configuration or the Task is unusable and nothing was claimed,
    1 when execution failed after the claim. There is no retry flag to pass.
    """
    parser = argparse.ArgumentParser(
        description="Execute one named Task's Run with the existing stage driver; "
                    "one attempt, no retry, an optional run record.")
    parser.add_argument("--pack", required=True, help="Task-pack directory.")
    parser.add_argument("--task-id", required=True, help="UUID of the Task created from this pack.")
    parser.add_argument("--policy", required=True,
                        help="Prepared Policy snapshot; the Task's stored one must match it.")
    parser.add_argument("--operator", required=True, help="Person or account running the Run.")
    parser.add_argument("--python-executable", required=True,
                        help="Absolute interpreter used for the public checks.")
    parser.add_argument("--workspaces-root", required=True,
                        help="Existing directory for Run workspaces.")
    parser.add_argument("--artifacts-root", required=True,
                        help="Existing directory for Run artifacts.")
    parser.add_argument("--record",
                        help="Where to write this attempt's reviewable run record. The file "
                             "must not exist yet; it is created exclusively and never "
                             "overwritten, and a replay writes nothing.")
    parser.add_argument("--material",
                        help="Trusted independent-evaluation material (JSON). Read once, digest-"
                             "checked against the pack, and never logged; keep it outside the "
                             "repository. Without it the attempt stops at the review draft.")
    parser.add_argument("--attempt-kind", choices=["formal", "rehearsal"], default="formal",
                        help="'rehearsal' marks an environment check; a formal attempt is "
                             "refused where the trusted baseline cannot be recorded.")
    parser.add_argument("--worker-id",
                        help="Actor recorded on the Run; defaults to a fresh UUID-based id.")
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    parser.add_argument("--max-patch-bytes", type=int, default=65536)
    parser.add_argument("--max-diff-bytes", type=int, default=262144)
    parser.add_argument("--statement-timeout-seconds", type=int, default=5)
    parser.add_argument("--shutdown-timeout-seconds", type=int, default=5)
    parser.add_argument("--confirm-run", action="store_true",
                        help="Required: acknowledges that this spends the pinned budget.")
    args = parser.parse_args(argv)

    if not args.confirm_run:
        return _configuration_error("--confirm-run is required to start a real Run")
    if not args.operator.strip():
        return _configuration_error("operator must be a non-blank string")
    worker_id = args.worker_id if args.worker_id is not None else f"worker-{uuid4()}"
    if not worker_id.strip():
        return _configuration_error("worker-id must be a non-blank string")
    for name in ("max_read_bytes", "max_patch_bytes", "max_diff_bytes",
                 "statement_timeout_seconds", "shutdown_timeout_seconds"):
        value = getattr(args, name)
        if isinstance(value, bool) or value <= 0:
            return _configuration_error(f"{name.replace('_', '-')} must be a positive integer")
    for name in ("workspaces_root", "artifacts_root"):
        if not Path(getattr(args, name)).is_dir():
            return _configuration_error(f"{name.replace('_', '-')} must be an existing directory")
    try:
        task_id = UUID(str(args.task_id))
    except (TypeError, ValueError):
        return _configuration_error("task-id must be a UUID")

    request = WorkerRunRequest(
        pack_dir=Path(args.pack), policy_path=Path(args.policy), task_id=task_id,
        operator=args.operator, python_executable=args.python_executable,
        workspaces_root=Path(args.workspaces_root),
        artifacts_root=Path(args.artifacts_root), attempt_kind=args.attempt_kind,
        material_path=Path(args.material) if args.material else None,
        record_path=Path(args.record) if args.record else None)
    engine = None
    try:
        if session_factory is None:
            engine = create_db_engine()
            session_factory = create_session_factory(engine)
        prepared = prepare_worker_run(request, session_factory=session_factory)
        if args.attempt_kind == "rehearsal":
            print(json.dumps({"event": "worker_run_warning",
                              "detail": "This call is a rehearsal; it is not a formal sample "
                                        "and writes no benchmark record."}), file=sys.stderr)
        if prepared.material is None and prepared.config.independent_suite_version:
            print(json.dumps({"event": "worker_run_warning",
                              "detail": "No trusted material was supplied; this attempt will "
                                        "stop at the review draft."}), file=sys.stderr)
        if not prepared.trusted_baseline_supported:
            print(json.dumps({"event": "worker_run_warning",
                              "detail": "This platform cannot record the trusted pytest "
                                        "baseline; formal acceptance is not available here."}),
                  file=sys.stderr)
        with ExitStack() as stack:
            try:
                gateways = stack.enter_context(
                    gateways_factory(session_factory, worker_id)
                    if gateways_factory is not None
                    else model_gateways_from_env(session_factory, worker_id))
            except ValueError:
                return _configuration_error("Model gateways are not configured")
            with stop_on_termination_signal() as stop_event:
                result = run_worker_once(
                    request, session_factory=session_factory,
                    worker=Worker(session_factory, worker_id), gateways=gateways,
                    heartbeat_config=HeartbeatConfig(
                        statement_timeout_seconds=args.statement_timeout_seconds,
                        shutdown_timeout_seconds=args.shutdown_timeout_seconds),
                    prepared=prepared, stop_event=stop_event,
                    max_read_bytes=args.max_read_bytes, max_patch_bytes=args.max_patch_bytes,
                    verifier_max_diff_bytes=args.max_diff_bytes)
    except WorkerRunError as error:
        return _configuration_error(str(error))
    except WorkerRunExecutionError as error:
        print(json.dumps({
            "event": "worker_run_failed", "run_id": str(error.run_id),
            "error_type": type(error.error).__name__,
            "detail": "Execution failed; the Run keeps its recorded state",
        }), file=sys.stderr)
        return 1
    except Exception as error:
        print(json.dumps({"event": "worker_run_failed",
                          "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(json.dumps({
        "event": "worker_run_finished", **result, "operator": args.operator,
        "limits": {"max_duration_seconds": request.limits.max_duration_seconds,
                   "max_model_tokens": request.limits.max_model_tokens,
                   "max_stages": request.limits.max_stages,
                   "attempts": request.limits.attempts,
                   "auto_retries": request.limits.auto_retries},
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
