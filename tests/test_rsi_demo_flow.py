"""手动触发的构造实验演示：V0 → 候选 V1 → 一次预算内尝试 → 账目与未晋升结论。

Run it with::

    pytest tests/test_rsi_demo_flow.py -s

What it actually links, in one process but with two database identities:

* a temporary PostgreSQL cluster created for this run alone (two roles: one for the Worker side,
  one for the control side), with the project's real migration chain applied to it;
* ``ExperimentService``: fixed V0 reference, candidate cap, token/time limits, promotion
  thresholds, one registered candidate version;
* the stage driver (``drive_run_stages``) with the Worker role: Supervisor → Explorer → Planner,
  every model reply a stub;
* the trusted budget channel from ``experiment_channel``: every stub request is reserved,
  dispatched and settled through the control role, while the Worker role holds no ledger
  privilege;
* the ledger's own evidence rules: the attempt is registered as ``incomplete`` (there is no formal
  verification), acceptance is refused, and the candidate is rejected with a stated reason.

The second case runs the same experiment across **two processes**: the control process keeps the
ledger and serves one local session; the Worker child has its own database role, claims the Run
itself and drives the stages, and every stub request is reserved, dispatched and settled through
that session. Its DSN and the session key arrive on stdin; its environment carries no database
variables.

What it does not do: no real model, no real evaluation, no accepted candidate, no automatic loop.
The V0/V1 digests are constructed references, not manifests of a real deployed package, and the
Run is closed as FAILED by the test only so the ledger can register an ``incomplete`` result.
"""
import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from test_experiment_db_privileges import instance, migrated, granted

from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus, TaskStatus
from devflow_ai.experiment import ExperimentError, ExperimentLimits, ExperimentService
from devflow_ai.experiment_channel import TrustedBudgetClient, TrustedBudgetServer
from devflow_ai.model_gateway import ModelGateway, ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import AgentExecution, ExperimentReservation, Run, Task, ToolCall
from devflow_ai.orchestration import drive_run_stages
from devflow_ai.policy import build_default_policy
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker import RunDirectories, Worker

@dataclass(frozen=True)
class World:
    worker_factory: sessionmaker
    control_factory: sessionmaker
    worker_url: object
    control_url: object


@pytest.fixture(scope="module")
def world(granted):
    """Reuse the separately created PostgreSQL cluster and grant the demo's control access."""
    admin = create_engine(granted.admin_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(
                "GRANT INSERT, UPDATE ON tasks TO devflow_control_role"))
            connection.execute(text(
                "GRANT SELECT ON tool_calls TO devflow_control_role"))
        worker_engine = create_engine(granted.worker_url)
        control_engine = create_engine(granted.control_url)
        try:
            yield World(worker_factory=sessionmaker(bind=worker_engine),
                        control_factory=sessionmaker(bind=control_engine),
                        worker_url=granted.worker_url, control_url=granted.control_url)
        finally:
            worker_engine.dispose()
            control_engine.dispose()
    finally:
        admin.dispose()


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Demo repository facts.\n", encoding="utf-8")
    return workspace


def _task(world: World, workspace: Path, tmp_path: Path):
    """Admitted Task, created by the control side as a human decision would be."""
    policy = build_default_policy()
    policy.policy_version = "rsi-demo-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    with world.control_factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="RSI demo task", description="Constructed demo task",
            repo=str(workspace), idempotency_key=uuid4())
        task_id = task.id
    with world.control_factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit="a" * 40,
            admission_record=AdmissionRecord(reason="demo fixture", evidence=["dev-task:demo"]))
    return task_id


def _scripted_generate(calls):
    """One stub for all five roles, dispatched on the prompt's role line."""
    decisions = iter([
        {"outcome": "EXPLORE", "reason": "Need repository evidence",
         "exploration_focus": "Inspect the repository"},
        {"outcome": "PLAN", "reason": "Evidence is enough", "next_goal": "Plan the change"},
        {"outcome": "BLOCKED", "reason": "Constructed demo stops here",
         "decision_required": "Stop"},
    ])
    reads = []

    def generate(prompt, max_output_tokens):
        if "You are DevFlow AI's Supervisor" in prompt:
            role, output = "SUPERVISOR", next(decisions)
        elif "You are DevFlow AI's Explorer" in prompt:
            role = "EXPLORER"
            reads.append(prompt)
            if len(reads) == 1:
                output = {"path": "README.md"}
            else:
                data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
                output = {"summary": "The README describes the demo repository.",
                          "read_evidence": {"path": "README.md",
                                            "tool_call_id": data["read"]["tool_call_id"]}}
        elif "You are DevFlow AI's Planner" in prompt:
            role = "PLANNER"
            output = {"outcome": "PLAN_READY", "plan": {
                "approach": "Change nothing in the demo", "target_paths": ["README.md"],
                "steps": ["Review the repository facts"], "scope_limits": "Only README.md",
                "verification": ["Run the public check"]}}
        else:
            role, output = "OTHER", {"outcome": "BLOCKED", "reason": "stub",
                                     "decision_required": "Stop"}
        calls.append(role)
        return ModelReply(output, ModelTokenUsage(input_tokens=10, output_tokens=5))

    return generate


def test_the_demo_runs_a_budgeted_attempt_and_refuses_promotion(world, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    workspace = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    task_id = _task(world, workspace, tmp_path)

    # 1. The experiment is fixed before any candidate exists: V0 reference, caps, thresholds.
    v0 = sha256(b"devflow-demo-v0").hexdigest()
    v1 = sha256(b"devflow-demo-v1").hexdigest()
    with world.control_factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"rsi-demo-{uuid4()}", base_execution_sha256=v0,
            task_ids=[task_id],
            limits=ExperimentLimits(max_candidates=2, max_total_model_tokens=10_000,
                                    max_total_seconds=1800, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="demo-operator")
    with world.control_factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="v1", execution_sha256=v1)

    # 2. One constructed attempt, claimed by the Worker identity with the candidate's version.
    worker = Worker(world.worker_factory, "demo-worker")
    with world.worker_factory() as session:
        run, created = RunCoordinator(session).start_initial_run(
            task_id, "demo-worker", f"demo-{uuid4()}", execution_sha256=v1)
        run_id = run.id
    assert created

    # 3. Stage drive with stub models; every request goes through the trusted budget channel.
    calls = []
    server = TrustedBudgetServer(session_factory=world.control_factory,
                                 experiment_id=experiment_id, candidate_label="v1",
                                 operator="control-operator")
    client = TrustedBudgetClient(send=server.handle)
    with model_gateways_from_env(world.worker_factory, "demo-worker",
                                 generate=_scripted_generate(calls), max_output_tokens=100,
                                 experiment_budget=client) as gateways:
        stages = drive_run_stages(
            world.worker_factory, run_id, worker=worker, gateways=gateways,
            directories=RunDirectories(workspace, artifacts), stop_event=Event(),
            verification_config=None, max_stages=8, max_read_bytes=4096)

    assert stages["reason"] == "supervisor_did_not_route"
    assert calls == ["SUPERVISOR", "EXPLORER", "EXPLORER", "SUPERVISOR", "PLANNER",
                     "SUPERVISOR"]

    # 4. The constructed attempt is closed as a failure: no verification was produced.
    with world.worker_factory.begin() as session:
        attempt = session.get(Run, run_id)
        attempt.status = RunStatus.FAILED
        attempt.finished_at = session.execute(text("SELECT now()")).scalar_one()
    with world.control_factory.begin() as session:
        recorded = ExperimentService(session).record_task_result(
            experiment_id=experiment_id, candidate_label="v1", task_id=task_id,
            run_id=run_id, outcome="incomplete", recorded_by="demo-operator")
    assert recorded["evidence_source"] == "declared"
    assert recorded["run_execution_sha256"] == v1

    # 5. Promotion is refused, and the candidate is rejected with a stated reason.
    with world.control_factory() as session:
        explanation = ExperimentService(session).explain_acceptance(experiment_id, "v1")
    assert explanation["acceptable"] is False and explanation["problems"]
    with world.control_factory.begin() as session:
        with pytest.raises(ExperimentError) as refused:
            ExperimentService(session).decide(
                experiment_id=experiment_id, candidate_label="v1", outcome="accepted",
                reason="demo: there is no formal verification", decided_by="demo-operator")
        assert "not available" in str(refused.value).lower()
    with world.control_factory.begin() as session:
        ExperimentService(session).decide(
            experiment_id=experiment_id, candidate_label="v1", outcome="rejected",
            reason="demo: constructed attempt only, no formal evidence",
            decided_by="demo-operator")

    # 6. The readable summary, checked against the database rows it describes.
    with world.control_factory() as session:
        final = ExperimentService(session).summary(experiment_id)
    candidate_entry = next(item for item in final["candidates"] if item["label"] == "v1")
    usage = final["usage"]
    with world.control_factory() as session:
        reservations = session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id)).all()
    with world.worker_factory() as session:
        charged = sum(
            entry["actual_usage"]["input_tokens"] + entry["actual_usage"]["output_tokens"]
            for execution in session.scalars(select(AgentExecution).where(
                AgentExecution.run_id == run_id))
            for entry in (execution.llm_usage or {}).get("model_requests", {}).values())
    summary = {
        "experiment_id": str(experiment_id),
        "versions": {"v0": v0, "v1": v1,
                     "note": "constructed references, not deployed package manifests"},
        "candidate": {"label": "v1", "state": candidate_entry["state"],
                      "outcome": candidate_entry["outcome"]},
        "attempt": {"task_id": str(task_id), "run_id": str(run_id),
                    "stage_stop_reason": stages["reason"], "stages": stages["stages"],
                    "outcome": recorded["outcome"]},
        "model_stub": {"calls": len(calls), "charged_tokens": charged},
        "budget": {"settled_tokens": usage["settled_tokens"],
                   "reserved_tokens": usage["reserved_tokens"],
                   "dispatched_unsettled": usage["dispatched_unsettled"],
                   "accounting_complete": usage["accounting_complete"],
                   "reservations": len(reservations)},
        "promotion": {"acceptable": explanation["acceptable"],
                      "problems": explanation["problems"]},
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    assert summary["model_stub"] == {"calls": 6, "charged_tokens": 90}
    assert summary["budget"] == {"settled_tokens": 90, "reserved_tokens": 0,
                                 "dispatched_unsettled": 0, "accounting_complete": True,
                                 "reservations": 6}
    assert {row.state for row in reservations} == {"settled"}
    assert explanation["distinct_formal_tasks"] == 0
    assert (candidate_entry["state"], candidate_entry["outcome"]) == ("concluded", "rejected")


def test_a_refused_budget_sends_no_model_request(world, tmp_path):
    """Key failure path: a ceiling that cannot cover one request means no stub call at all."""
    workspace = _workspace(tmp_path)
    task_id = _task(world, workspace, tmp_path)
    with world.control_factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"rsi-demo-refused-{uuid4()}", base_execution_sha256="c" * 64,
            task_ids=[task_id],
            limits=ExperimentLimits(max_candidates=1, max_total_model_tokens=1,
                                    max_total_seconds=600, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="demo-operator")
    with world.control_factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="v1", execution_sha256="d" * 64)
    worker = Worker(world.worker_factory, "demo-worker-refused")
    with world.worker_factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, "demo-worker-refused", f"demo-refused-{uuid4()}",
            execution_sha256="d" * 64)
        run_id = run.id
    calls = []
    server = TrustedBudgetServer(session_factory=world.control_factory,
                                 experiment_id=experiment_id, candidate_label="v1",
                                 operator="control-operator")
    gateway = ModelGateway(
        session_factory=world.worker_factory, worker_id="demo-worker-refused",
        estimate_input_tokens=lambda _: 5, max_output_tokens=10,
        generate=lambda prompt, limit: (calls.append(prompt),
                                        ModelReply("reply", ModelTokenUsage(1, 1)))[1],
        experiment_budget=TrustedBudgetClient(send=server.handle))
    with world.worker_factory() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output={})
        session.add(execution)
        session.flush()
        execution_id = execution.id

    assert gateway.request(run_id, agent_execution_id=execution_id, prompt="hello") is None
    assert calls == []
    with world.control_factory() as session:
        assert session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id)).all() == []


# -- the same demo, with the Worker in its own process -------------------------------------
#
# The control process keeps the experiment and the ledger credentials and serves one local
# session. The Worker child gets its own database role, its own DSN and the session key on stdin
# (never on the command line, never in the environment), claims the constructed Run, drives the
# same stages with stub models, and every request goes through the session for reserve/dispatch/
# settle. The model is a stub; the two processes, the two roles and the ledger are real.

WORKER_PROCESS = r"""
import json
import os
import sys
from pathlib import Path
from threading import Event
from uuid import UUID

handshake = json.loads(sys.stdin.readline())
from multiprocessing.connection import Client

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
from devflow_ai.enums import AgentExecutionStatus, AgentRole, RunStatus
from devflow_ai.experiment_channel import TrustedBudgetClient, connection_transport
from devflow_ai.model_gateway import ModelGateway, ModelReply
from devflow_ai.model_gateways import model_gateways_from_env
from devflow_ai.models import AgentExecution, Run
from devflow_ai.orchestration import drive_run_stages
from devflow_ai.worker import RunDirectories, Worker

db = handshake["db"]
engine = create_engine(URL.create(
    "postgresql+psycopg", username=db["username"], password=db["password"],
    database=db["database"], query={"host": db["host"], "port": str(db["port"])}))
factory = sessionmaker(bind=engine)
connection = Client(handshake["address"], family=handshake["family"],
                    authkey=bytes.fromhex(handshake["authkey"]))

with factory() as session:
    current_role = session.execute(text("SELECT current_user")).scalar_one()
with factory() as session:
    run, created = RunCoordinator(session).start_initial_run(
        UUID(handshake["task_id"]), handshake["worker_id"], handshake["claim_key"],
        execution_sha256=handshake["execution_sha256"])
    run_id = run.id

calls = []
decisions = iter(handshake["decisions"])
reads = []


def generate(prompt, max_output_tokens):
    if "You are DevFlow AI's Supervisor" in prompt:
        role, output = "SUPERVISOR", next(decisions)
    elif "You are DevFlow AI's Explorer" in prompt:
        role = "EXPLORER"
        reads.append(prompt)
        if len(reads) == 1:
            output = {"path": "README.md"}
        else:
            data = json.loads(prompt.split("\nInput data JSON:\n", 1)[1])
            output = {"summary": "The README describes the demo repository.",
                      "read_evidence": {"path": "README.md",
                                        "tool_call_id": data["read"]["tool_call_id"]}}
    elif "You are DevFlow AI's Planner" in prompt:
        role = "PLANNER"
        output = {"outcome": "PLAN_READY", "plan": {
            "approach": "Change nothing in the demo", "target_paths": ["README.md"],
            "steps": ["Review the repository facts"], "scope_limits": "Only README.md",
            "verification": ["Run the public check"]}}
    else:
        role, output = "OTHER", {"outcome": "BLOCKED", "reason": "stub",
                                 "decision_required": "Stop"}
    calls.append(role)
    return ModelReply(output, ModelTokenUsage(input_tokens=10, output_tokens=5))


client = TrustedBudgetClient(send=connection_transport(connection))
worker = Worker(factory, handshake["worker_id"])
workspace = Path(handshake["workspace"])
workspace.mkdir(parents=True, exist_ok=True)
(workspace / "README.md").write_text("Demo repository facts.\n", encoding="utf-8")
artifacts = Path(handshake["artifacts"])
artifacts.mkdir(parents=True, exist_ok=True)

report = {"run_id": str(run_id), "pid": os.getpid(), "role": current_role,
          "db_env_seen": sorted(k for k in os.environ if k.startswith("DEVFLOW_DB_"))}
if handshake["mode"] == "stages":
    with model_gateways_from_env(factory, handshake["worker_id"], generate=generate,
                                 max_output_tokens=100, experiment_budget=client) as gateways:
        stages = drive_run_stages(
            factory, run_id, worker=worker, gateways=gateways,
            directories=RunDirectories(workspace, artifacts), stop_event=Event(),
            verification_config=None, max_stages=8, max_read_bytes=4096)
    report["stages"] = stages
else:
    with factory.begin() as session:
        execution = AgentExecution(
            run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
            status=AgentExecutionStatus.RUNNING, input={}, output={})
        session.add(execution)
        session.flush()
        execution_id = execution.id
    gateway = ModelGateway(
        session_factory=factory, worker_id=handshake["worker_id"],
        estimate_input_tokens=lambda _: 5, generate=generate, max_output_tokens=10,
        experiment_budget=client)
    report["reply"] = gateway.request(run_id, agent_execution_id=execution_id,
                                      prompt="hello")
report["calls"] = calls
with factory.begin() as session:
    attempt = session.get(Run, run_id)
    attempt.status = RunStatus.FAILED
    attempt.finished_at = session.execute(text("SELECT now()")).scalar_one()
print(json.dumps(report))
"""


def _child_environment() -> dict:
    """A deliberately small environment: no DEVFLOW_* variables, no database settings.

    ``PYTHONPATH`` is passed so the child can import the package under test from the same tree;
    it is not a credential. Everything about the database and the session arrives on stdin.
    """
    import os
    names = ("PATH", "SystemRoot", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "HOME",
             "USERPROFILE", "LANG", "LC_ALL")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


def _run_worker_process(world: World, *, experiment_id, task_id, execution_sha256,
                        workspace: Path, artifacts: Path, mode: str = "stages",
                        authkey: bytes | None = None):
    """Serve one session here and run the Worker in a separate interpreter."""
    import os
    import secrets
    import subprocess
    import sys
    import tempfile

    from devflow_ai.experiment_channel import (
        LOCAL_FAMILY, LocalBudgetSession, session_address,
    )

    key = authkey if authkey is not None else secrets.token_bytes(32)
    server = TrustedBudgetServer(session_factory=world.control_factory,
                                 experiment_id=experiment_id, candidate_label="v1",
                                 operator="control-operator")
    directory = Path(tempfile.mkdtemp(prefix="rsi-demo-"))
    handshake = {
        "address": session_address(directory), "family": LOCAL_FAMILY,
        "authkey": key.hex(),
        "db": {"username": world.worker_url.username, "password": world.worker_url.password,
               "database": world.worker_url.database,
               "host": world.worker_url.host, "port": world.worker_url.port},
        "worker_id": "two-process-demo-worker", "task_id": str(task_id),
        "claim_key": f"demo-two-process-{uuid4()}", "execution_sha256": execution_sha256,
        "workspace": str(workspace), "artifacts": str(artifacts),
        "mode": mode,
        "decisions": [
            {"outcome": "EXPLORE", "reason": "Need repository evidence",
             "exploration_focus": "Inspect the repository"},
            {"outcome": "PLAN", "reason": "Evidence is enough", "next_goal": "Plan the change"},
            {"outcome": "BLOCKED", "reason": "Constructed demo stops here",
             "decision_required": "Stop"},
        ],
    }
    child = None
    served = 0
    try:
        with LocalBudgetSession(server=server, address=handshake["address"],
                                authkey=key) as session:
            child = subprocess.Popen([sys.executable, "-c", WORKER_PROCESS],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     env=_child_environment())
            child.stdin.write(json.dumps(handshake) + "\n")
            child.stdin.close()
            child.stdin = None
            served_box = {}

            def serve():
                try:
                    served_box["served"] = session.serve_one()
                except Exception as error:  # authentication or transport failure
                    served_box["error"] = error

            served_thread = __import__("threading").Thread(target=serve, daemon=True)
            served_thread.start()
            import time
            deadline = time.monotonic() + 150
            while served_thread.is_alive() and time.monotonic() < deadline:
                served_thread.join(timeout=0.1)
                if child.poll() is not None and served_thread.is_alive():
                    served_thread.join(timeout=2)
                    break
            if served_thread.is_alive():
                raise AssertionError("The Worker or budget session ended without completing")
            if "error" in served_box:
                raise AssertionError("The budget session failed")
            served = served_box.get("served", 0)
            try:
                out, err = child.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                child.kill()
                out, err = child.communicate()
                raise AssertionError("The Worker child did not finish") from None
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        import shutil
        shutil.rmtree(directory, ignore_errors=True)
    payload = None
    for line in reversed((out or "").splitlines()):
        if line.strip().startswith("{"):
            payload = json.loads(line)
            break
    return {"returncode": child.returncode if child else None, "payload": payload,
            "served": served, "stderr": err}


def test_the_demo_runs_across_two_processes_and_two_roles(world, tmp_path):
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    task_id = _task(world, workspace, tmp_path)
    v0 = sha256(b"devflow-demo-v0").hexdigest()
    v1 = sha256(b"devflow-demo-v1").hexdigest()
    with world.control_factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"rsi-demo-two-process-{uuid4()}", base_execution_sha256=v0,
            task_ids=[task_id],
            limits=ExperimentLimits(max_candidates=2, max_total_model_tokens=10_000,
                                    max_total_seconds=1800, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="demo-operator")
    with world.control_factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="v1", execution_sha256=v1)

    outcome = _run_worker_process(world, experiment_id=experiment_id, task_id=task_id,
                                  execution_sha256=v1, workspace=workspace,
                                  artifacts=artifacts)

    assert outcome["returncode"] == 0, "The Worker child failed"
    report = outcome["payload"]
    # Two processes, two database roles.
    assert report["pid"] != os.getpid()
    assert report["role"] == "devflow_worker_role"
    assert report["db_env_seen"] == []
    # The stages really ran, with stub models, and the driver stopped where it was told to.
    assert report["stages"]["reason"] == "supervisor_did_not_route"
    assert report["calls"] == ["SUPERVISOR", "EXPLORER", "EXPLORER", "SUPERVISOR",
                               "PLANNER", "SUPERVISOR"]
    # Database evidence, read by the control side: the Run, its stages and the ledger.
    with world.control_factory() as session:
        run = session.get(Run, UUID(report["run_id"]))
        assert run is not None and run.task_id == task_id
        assert run.execution_sha256 == v1
        executions = session.scalars(select(AgentExecution).where(
            AgentExecution.run_id == run.id)).all()
        roles = {execution.agent_role for execution in executions}
        run_usage = sum(
            entry["actual_usage"]["input_tokens"] + entry["actual_usage"]["output_tokens"]
            for execution in executions
            for entry in (execution.llm_usage or {}).get("model_requests", {}).values())
        tool_names = {call.tool_name for call in session.scalars(
            select(ToolCall).where(ToolCall.run_id == run.id))}
    assert {"supervisor", "explorer", "planner"} <= roles
    assert "read_file" in tool_names
    # reserve + dispatch + settle for every stub call, all through the one session.
    assert outcome["served"] == 3 * len(report["calls"])
    with world.control_factory() as session:
        summary = ExperimentService(session).summary(experiment_id)
        reservations = session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id)).all()
    assert len(reservations) == 6 and {row.state for row in reservations} == {"settled"}
    assert summary["usage"]["settled_tokens"] == 6 * 15
    assert run_usage == summary["usage"]["settled_tokens"]
    # The Worker role still holds no ledger privilege.
    admin = create_engine(world.control_url)
    try:
        with admin.connect() as connection:
            assert connection.scalar(text(
                "SELECT has_table_privilege('devflow_worker_role', "
                "'experiment_reservations', 'INSERT')")) is False
    finally:
        admin.dispose()

    # The attempt is registered as incomplete, acceptance is refused, the candidate is rejected.
    with world.control_factory.begin() as session:
        recorded = ExperimentService(session).record_task_result(
            experiment_id=experiment_id, candidate_label="v1", task_id=task_id,
            run_id=UUID(report["run_id"]), outcome="incomplete", recorded_by="demo-operator")
    assert recorded["run_execution_sha256"] == v1
    with world.control_factory() as session:
        explanation = ExperimentService(session).explain_acceptance(experiment_id, "v1")
    assert explanation["acceptable"] is False
    with world.control_factory.begin() as session:
        with pytest.raises(ExperimentError):
            ExperimentService(session).decide(
                experiment_id=experiment_id, candidate_label="v1", outcome="accepted",
                reason="demo: constructed attempt only", decided_by="demo-operator")
    with world.control_factory.begin() as session:
        ExperimentService(session).decide(
            experiment_id=experiment_id, candidate_label="v1", outcome="rejected",
            reason="demo: constructed attempt only, no formal evidence",
            decided_by="demo-operator")
    with world.control_factory() as session:
        final = ExperimentService(session).summary(experiment_id)
    entry = next(item for item in final["candidates"] if item["label"] == "v1")
    assert (entry["state"], entry["outcome"]) == ("concluded", "rejected")
    print(json.dumps({
        "experiment_id": str(experiment_id),
        "versions": {"v0": v0, "v1": v1, "note": "constructed references"},
        "processes": {"worker": report["pid"], "control": os.getpid(),
                      "worker_database_role": report["role"]},
        "candidate": {"label": "v1", "state": entry["state"], "outcome": entry["outcome"]},
        "attempt": {"task_id": str(task_id), "run_id": report["run_id"],
                    "stage_stop_reason": report["stages"]["reason"],
                    "stages": report["stages"]["stages"], "outcome": recorded["outcome"]},
        "model_stub": {"calls": len(report["calls"]), "charged_tokens": run_usage},
        "budget": {"settled_tokens": final["usage"]["settled_tokens"],
                   "reservations": len(reservations),
                   "session_messages": outcome["served"]},
        "promotion": {"acceptable": explanation["acceptable"],
                      "problems": explanation["problems"]},
    }, indent=2, ensure_ascii=False))


def test_a_refusing_session_stops_the_worker_before_any_model_call(world, tmp_path):
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    task_id = _task(world, workspace, tmp_path)
    with world.control_factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"rsi-demo-refused-process-{uuid4()}", base_execution_sha256="c" * 64,
            task_ids=[task_id],
            limits=ExperimentLimits(max_candidates=1, max_total_model_tokens=1,
                                    max_total_seconds=600, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="demo-operator")
    with world.control_factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="v1", execution_sha256="d" * 64)

    outcome = _run_worker_process(world, experiment_id=experiment_id, task_id=task_id,
                                  execution_sha256="d" * 64, workspace=workspace,
                                  artifacts=artifacts, mode="single_request")

    assert outcome["returncode"] == 0, "The Worker child failed"
    assert outcome["payload"]["reply"] is None and outcome["payload"]["calls"] == []
    with world.control_factory() as session:
        assert session.scalars(select(ExperimentReservation).where(
            ExperimentReservation.experiment_id == experiment_id)).all() == []
