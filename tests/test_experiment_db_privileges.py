"""候选 Worker 身份与控制端身份的真实数据库权限边界。

Every test runs against a new temporary PostgreSQL cluster: pgserver in a temporary directory,
or a dedicated disposable Docker container when pgserver is unavailable. A fresh database and
two roles use test-only passwords and the project's real migration chain. No shared cluster is
modified; the fixture removes its own temporary cluster.

What is being checked is a boundary, not a sandbox: the Worker identity can do the Run-side work a
constructed attempt needs, and cannot write (or read) the experiment ledger; the control identity
can. The experiment budget hook is exercised under the Worker identity on purpose — it writes the
ledger directly today, so it is expected to fail there, and that failure is the finding.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

try:
    import pgserver
except ImportError:
    pgserver = None
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
from devflow_ai.enums import (
    AgentExecutionStatus, AgentRole, TaskStatus,
)
from devflow_ai.experiment import ExperimentLimits, ExperimentService
from devflow_ai.experiment_channel import (
    LOCAL_FAMILY, BudgetChannelError, LocalBudgetSession, TrustedBudgetClient,
    TrustedBudgetServer, session_address,
)
from devflow_ai.model_gateway import ModelGateway, ModelReply
from devflow_ai.experiment_budget import ExperimentBudgetHook
from devflow_ai.policy import build_default_policy
from devflow_ai.models import AgentExecution, ExperimentReservation, Run, Task
from devflow_ai.services import AdmissionRecord, TaskService
from devflow_ai.worker import Worker

DATABASE = "devflow_priv"
WORKER_ROLE = "devflow_worker_role"
CONTROL_ROLE = "devflow_control_role"
EXPERIMENT_TABLES = ("experiments", "experiment_tasks", "experiment_candidates",
                     "experiment_reservations", "experiment_task_results",
                     "experiment_records")
WORKER_RUN_TABLES = ("tasks", "runs", "agent_executions", "tool_calls", "handoffs",
                     "artifacts")
ROOT = Path(__file__).resolve().parents[1]


class DockerPostgres:
    """A disposable PostgreSQL container, used when embedded pgserver is unavailable."""

    def __init__(self):
        self.password = secrets.token_urlsafe(16)
        env = {**os.environ, "POSTGRES_PASSWORD": self.password}
        result = subprocess.run(
            ["docker", "run", "--detach", "--rm", "--pull=never", "--publish",
             "127.0.0.1::5432", "--env", "POSTGRES_PASSWORD", "postgres:17.11"],
            env=env, capture_output=True, text=True, check=True)
        self.container_id = result.stdout.strip()
        try:
            port = subprocess.run(
                ["docker", "port", self.container_id, "5432/tcp"],
                capture_output=True, text=True, check=True).stdout.strip()
            self.port = int(port.rsplit(":", 1)[1])
            for attempt in range(60):
                probe = subprocess.run(
                    ["docker", "exec", self.container_id, "pg_isready", "-U", "postgres"],
                    capture_output=True, text=True)
                if probe.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("temporary PostgreSQL did not become ready")
        except BaseException:
            self.cleanup()
            raise

    def ensure_postgres_running(self):
        pass

    def get_uri(self):
        return f"postgresql://postgres@127.0.0.1:{self.port}/postgres"

    def psql(self, sql):
        subprocess.run(
            ["docker", "exec", "-i", self.container_id, "psql", "-U", "postgres",
             "-v", "ON_ERROR_STOP=1"], input=sql, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def cleanup(self):
        subprocess.run(["docker", "rm", "--force", self.container_id],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


@dataclass(frozen=True)
class Instance:
    """The temporary cluster, the database on it, and the two identities used below."""

    server: object
    data_dir: Path
    host: str
    port: int
    admin_url: URL
    worker_url: URL
    control_url: URL

    def factory(self, url: URL):
        return sessionmaker(bind=create_engine(url))


def _url(server, user: str, password: str) -> URL:
    parsed = make_url(server.get_uri())
    return URL.create("postgresql+psycopg", username=user, password=password,
                      host=parsed.query.get("host") or parsed.host,
                      port=parsed.port or 5432, database=DATABASE)


@pytest.fixture(scope="module")
def instance(tmp_path_factory):
    if pgserver is None and not shutil.which("docker"):
        pytest.skip("pgserver or Docker is required for an independent temporary PostgreSQL")
    if pgserver is None:
        for command in (["docker", "info"], ["docker", "image", "inspect", "postgres:17.11"]):
            check = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode:
                pytest.skip("Docker engine and local postgres:17.11 image are required")
    root = tmp_path_factory.mktemp("pgpriv")
    admin_password = secrets.token_urlsafe(16)
    worker_password = secrets.token_urlsafe(16)
    control_password = secrets.token_urlsafe(16)
    server = (pgserver.get_server(str(root / "pgdata"), cleanup_mode=None)
              if pgserver is not None else DockerPostgres())
    server.ensure_postgres_running()
    try:
        server.psql(f"ALTER USER postgres PASSWORD '{admin_password}';")
        server.psql(f"CREATE DATABASE {DATABASE};")
        server.psql(f"CREATE ROLE {WORKER_ROLE} LOGIN PASSWORD '{worker_password}';")
        server.psql(f"CREATE ROLE {CONTROL_ROLE} LOGIN PASSWORD '{control_password}';")
        yield Instance(server=server, data_dir=root / "pgdata",
                       host=(make_url(server.get_uri()).query.get("host")
                             or make_url(server.get_uri()).host),
                       port=make_url(server.get_uri()).port or 5432,
                       admin_url=_url(server, "postgres", admin_password),
                       worker_url=_url(server, WORKER_ROLE, worker_password),
                       control_url=_url(server, CONTROL_ROLE, control_password))
    finally:
        server.cleanup()


@pytest.fixture(scope="module")
def migrated(instance):
    """Apply the project's real migration chain to this temporary database."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DEVFLOW_DB_NAME", DATABASE)
    monkeypatch.setenv("DEVFLOW_DB_USER", "postgres")
    monkeypatch.setenv("DEVFLOW_DB_PASSWORD", instance.admin_url.password)
    monkeypatch.setenv("DEVFLOW_DB_HOST", instance.host)
    monkeypatch.setenv("DEVFLOW_DB_PORT", str(instance.port))
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.get_main_option("script_location")  # load configuration before suppressing fileConfig
        config.config_file_name = None  # migrations must not reset pytest's logging handlers
        command.upgrade(config, "head")
    finally:
        monkeypatch.undo()
    return instance


@pytest.fixture(scope="module")
def granted(migrated):
    """Grant each identity only what its side of the boundary needs."""
    admin = create_engine(migrated.admin_url)
    with admin.begin() as connection:
        connection.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}, {CONTROL_ROLE}"))
        # The Worker identity: the Run-side tables a constructed attempt touches.
        connection.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(WORKER_RUN_TABLES)} "
            f"TO {WORKER_ROLE}"))
        connection.execute(text(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {WORKER_ROLE}"))
        # The control identity: the experiment ledger, plus reading Tasks and Runs.
        connection.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(EXPERIMENT_TABLES)} "
            f"TO {CONTROL_ROLE}"))
        connection.execute(text(
            f"GRANT SELECT ON tasks, runs, agent_executions TO {CONTROL_ROLE}"))
        connection.execute(text(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {CONTROL_ROLE}"))
    engine = create_engine(migrated.worker_url)
    try:
        yield migrated
    finally:
        admin.dispose()
        engine.dispose()


def _create_task(instance: Instance, *, repo: str, base_commit: str, claimable: bool = False):
    """A Task admitted by the control side, as a human decision would be.

    Unless ``claimable`` is set it is left in review state, so a later ``claim_once`` cannot pick
    up a leftover fixture instead of the Task under test.
    """
    policy = build_default_policy()
    policy.policy_version = "privilege-test-v1"
    policy.permissions.allowed_tools_by_role[AgentRole.EXPLORER] = ["read_file"]
    factory = instance.factory(instance.admin_url)
    with factory() as session:
        task, _ = TaskService(session, policy_factory=lambda: policy).create_task(
            title="Privilege task", description="Constructed fixture", repo=repo,
            idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        TaskService(session).apply_admission_decision(
            task_id, TaskStatus.ELIGIBLE, base_commit=base_commit,
            admission_record=AdmissionRecord(reason="fixture", evidence=["fixture"]))
    if not claimable:
        with factory.begin() as session:
            session.get(Task, task_id).status = TaskStatus.AWAITING_REVIEW
    return task_id


def _source_repo(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    import subprocess

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("Repository facts\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    git("init", "--quiet")
    git("add", "README.md")
    git("-c", "user.name=Privilege Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline")
    return repo, git("rev-parse", "HEAD")


def _assert_permission_denied(engine, statement: str, parameters: dict | None = None):
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError) as refused:
        with engine.begin() as connection:
            connection.execute(text(statement), parameters or {})
    # Reported without the DSN or any role password.
    assert "permission denied" in str(refused.value).lower()


def test_the_worker_identity_cannot_write_or_read_the_experiment_ledger(granted):
    worker = create_engine(granted.worker_url)
    try:
        for table in EXPERIMENT_TABLES:
            _assert_permission_denied(worker, f"INSERT INTO {table} (id) VALUES (gen_random_uuid())")
            _assert_permission_denied(worker, f"UPDATE {table} SET id = id")
            _assert_permission_denied(worker, f"DELETE FROM {table}")
            _assert_permission_denied(worker, f"SELECT count(*) FROM {table}")
    finally:
        worker.dispose()


def test_the_control_identity_can_write_experiment_records(granted):
    factory = granted.factory(granted.control_url)
    repo_task = _create_task(granted, repo="https://example.invalid/repo", base_commit="a" * 40)
    with factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"privilege-{uuid4()}", base_execution_sha256="b" * 64,
            task_ids=[repo_task],
            limits=ExperimentLimits(max_candidates=1, max_total_model_tokens=1000,
                                    max_total_seconds=600, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="control-operator")
    with factory() as session:
        assert ExperimentService(session).read(experiment_id).max_total_model_tokens == 1000
    # The same rows are unreadable to the Worker identity.
    worker = create_engine(granted.worker_url)
    try:
        _assert_permission_denied(worker, "SELECT count(*) FROM experiments")
    finally:
        worker.dispose()


def test_roles_do_not_inherit_and_public_grants_are_not_a_back_door(granted):
    admin = create_engine(granted.admin_url)
    try:
        with admin.connect() as connection:
            assert connection.scalar(text(
                "SELECT pg_has_role(:worker, :control, 'member')"),
                {"worker": WORKER_ROLE, "control": CONTROL_ROLE}) is False
            for table in EXPERIMENT_TABLES:
                assert connection.scalar(text(
                    "SELECT has_table_privilege(:role, :table, 'INSERT')"),
                    {"role": WORKER_ROLE, "table": table}) is False
                assert connection.scalar(text(
                    "SELECT has_table_privilege(:role, :table, 'SELECT')"),
                    {"role": WORKER_ROLE, "table": table}) is False
            assert connection.scalar(text(
                "SELECT has_schema_privilege(:role, 'public', 'CREATE')"),
                {"role": WORKER_ROLE}) is False
            assert connection.scalar(text("SELECT count(*) FROM pg_default_acl")) == 0
            # PUBLIC must not be the way around the two roles.
            for table in (*EXPERIMENT_TABLES, *WORKER_RUN_TABLES):
                assert connection.scalar(text(
                    "SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE grantee = 'PUBLIC' AND table_schema = 'public' "
                    "AND table_name = :table"), {"table": table}) == 0
        # SET ROLE is the other way to borrow privileges; the Worker is not a member.
        worker = create_engine(granted.worker_url)
        try:
            with worker.connect() as connection:
                assert connection.scalar(text("SELECT current_user")) == WORKER_ROLE
                with pytest.raises(Exception) as refused:
                    connection.execute(text(f"SET ROLE {CONTROL_ROLE}"))
                assert "permission denied" in str(refused.value).lower()
        finally:
            worker.dispose()
    finally:
        admin.dispose()


def test_the_worker_identity_can_claim_and_execute_a_constructed_run(
        granted, tmp_path, monkeypatch):
    factory = granted.factory(granted.worker_url)
    repo, base_commit = _source_repo(tmp_path, monkeypatch)
    task_id = _create_task(granted, repo=str(repo), base_commit=base_commit, claimable=True)
    worker = Worker(factory, "worker-identity")
    try:
        run_id = worker.claim_once().run_id
        workspaces, artifacts = tmp_path / "workspaces", tmp_path / "artifacts"
        workspaces.mkdir()
        artifacts.mkdir()
        from threading import Event

        from devflow_ai.config import HeartbeatConfig

        stage = {}

        def process_run(directories, actual_commit, stop_event):
            stage.update({"workspace": str(directories.workspace_root),
                          "commit": actual_commit,
                          "files": sorted(path.name for path in
                                          directories.workspace_root.iterdir())})
            return {"outcome": "stub_stage", **stage}

        result = worker.execute_run(
            run_id, workspaces_root=workspaces, artifacts_root=artifacts,
            heartbeat_config=HeartbeatConfig(statement_timeout_seconds=10,
                                             shutdown_timeout_seconds=5),
            process_run=process_run, external_stop_event=Event())

        assert result["outcome"] == "stub_stage"
        assert stage["commit"] == base_commit and "README.md" in stage["files"]
        with factory() as session:
            from devflow_ai.models import Run
            run = session.get(Run, run_id)
            assert run is not None and run.task_id == task_id
    finally:
        worker.session_factory.kw["bind"].dispose()


def test_the_experiment_budget_hook_fails_under_the_worker_identity(granted):
    admin_factory = granted.factory(granted.admin_url)
    task_id = _create_task(granted, repo="https://example.invalid/repo", base_commit="a" * 40)
    with admin_factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"hook-{uuid4()}", base_execution_sha256="b" * 64,
            task_ids=[task_id],
            limits=ExperimentLimits(max_candidates=1, max_total_model_tokens=1000,
                                    max_total_seconds=600, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="control-operator")
    worker_factory = granted.factory(granted.worker_url)
    hook = ExperimentBudgetHook(session_factory=worker_factory, experiment_id=experiment_id,
                                candidate_label="candidate-a", operator="worker-identity")

    with pytest.raises(Exception) as refused:
        hook.open_request(estimated_tokens=10)

    # The failure is the database refusing the ledger access, not a test double. The message
    # names the table the accounting tried to touch, which is what the report has to state.
    message = str(refused.value).lower()
    assert "permission denied" in message
    assert any(table in message for table in EXPERIMENT_TABLES)


def test_the_cluster_is_a_new_instance_not_the_shared_test_database(granted):
    with create_engine(granted.admin_url).connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == DATABASE
        expected_port = 5432 if isinstance(granted.server, DockerPostgres) else granted.port
        assert connection.scalar(text("SELECT current_setting('port')")) == str(expected_port)
        actual_data_dir = connection.scalar(text("SHOW data_directory"))
    if isinstance(granted.server, DockerPostgres):
        shared_port = os.environ.get("DEVFLOW_DB_PORT")
        if shared_port:
            assert granted.port != int(shared_port)
    else:
        assert Path(actual_data_dir).resolve() == granted.data_dir.resolve()
    shared_name = os.environ.get("DEVFLOW_DB_NAME")
    assert shared_name != DATABASE


# -- the trusted budget channel prototype ------------------------------------------------
#
# The Worker-side client holds no session; every decision is made by the control-plane server
# through the existing ExperimentService state machine. Model replies are stubs: what is real is
# the two database identities, the ledger rows and the number of gateway calls.

class _FlakyTransport:
    """In-process transport that can pretend an answer was lost, before or after the server saw it."""

    def __init__(self, server, *, operation: str, failures: int, after_serving: bool):
        self.server = server
        self.operation = operation
        self.remaining = failures
        self.after_serving = after_serving
        self.seen = []

    def __call__(self, message):
        self.seen.append(message["op"])
        if message["op"] == self.operation and self.remaining > 0:
            self.remaining -= 1
            if self.after_serving:
                self.server.handle(message)
            raise RuntimeError("transport lost the answer")
        return self.server.handle(message)


def _experiment_for(control_factory, task_id, *, tokens: int = 1000, candidate: str | None = "candidate-a"):
    with control_factory.begin() as session:
        experiment_id = ExperimentService(session).open(
            identity_key=f"channel-{uuid4()}", base_execution_sha256="b" * 64,
            task_ids=[task_id],
            limits=ExperimentLimits(max_candidates=2, max_total_model_tokens=tokens,
                                    max_total_seconds=3600, promotion_min_formal_passes=1,
                                    promotion_max_failures=0),
            created_by="control-operator")
    if candidate is not None:
        with control_factory.begin() as session:
            ExperimentService(session).admit_candidate(
                experiment_id=experiment_id, label=candidate, execution_sha256="1" * 64)
    return experiment_id


def _worker_attempt(instance, tmp_path, monkeypatch):
    """A Task the Worker identity may claim, one Run, and one running AgentExecution."""
    worker_factory = instance.factory(instance.worker_url)
    repo, base_commit = _source_repo(tmp_path, monkeypatch)
    task_id = _create_task(instance, repo=str(repo), base_commit=base_commit, claimable=True)
    worker = Worker(worker_factory, "channel-worker")
    run_id = worker.claim_once().run_id
    with worker_factory.begin() as session:
        execution = AgentExecution(run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                                   status=AgentExecutionStatus.RUNNING, input={}, output={})
        session.add(execution)
        session.flush()
        execution_id = execution.id
    return worker_factory, task_id, run_id, execution_id


def _gateway(worker_factory, client, calls, *, usage=(10, 5)):
    def generate(prompt, max_output_tokens):
        calls.append(prompt)
        if usage is None:
            return ModelReply("reply", None)
        return ModelReply("reply", ModelTokenUsage(input_tokens=usage[0], output_tokens=usage[1]))

    return ModelGateway(session_factory=worker_factory, worker_id="channel-worker",
                        estimate_input_tokens=lambda _: 5, generate=generate,
                        max_output_tokens=10, experiment_budget=client)


def _reservations(instance, experiment_id):
    admin = create_engine(instance.admin_url)
    try:
        with admin.connect() as connection:
            return connection.execute(text(
                "SELECT request_key, state, stage, actual_tokens FROM experiment_reservations "
                "WHERE experiment_id = :id"), {"id": experiment_id}).all()
    finally:
        admin.dispose()


def test_a_worker_side_client_books_through_the_trusted_side(granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    client = TrustedBudgetClient(send=server.handle)
    calls = []

    reply = _gateway(worker_factory, client, calls).request(
        run_id, agent_execution_id=execution_id, prompt="hello")

    assert reply == "reply" and len(calls) == 1
    rows = _reservations(granted, experiment_id)
    assert len(rows) == 1 and rows[0].state == "settled" and rows[0].actual_tokens == 15
    # The Worker identity still cannot read the ledger it just contributed to.
    worker = create_engine(granted.worker_url)
    try:
        _assert_permission_denied(worker, "SELECT count(*) FROM experiment_reservations")
    finally:
        worker.dispose()


def test_a_lost_reservation_answer_does_not_book_or_send_twice(granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    transport = _FlakyTransport(server, operation="reserve", failures=1, after_serving=True)
    calls = []

    reply = _gateway(worker_factory, TrustedBudgetClient(send=transport), calls).request(
        run_id, agent_execution_id=execution_id, prompt="hello")

    assert reply == "reply" and len(calls) == 1
    rows = _reservations(granted, experiment_id)
    assert len(rows) == 1 and rows[0].state == "settled" and rows[0].actual_tokens == 15
    assert transport.seen.count("reserve") == 2


def test_an_unclear_dispatch_answer_stops_the_send_and_keeps_the_hold(
        granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    transport = _FlakyTransport(server, operation="dispatch", failures=2, after_serving=True)
    calls = []

    with pytest.raises(BudgetChannelError):
        _gateway(worker_factory, TrustedBudgetClient(send=transport), calls).request(
            run_id, agent_execution_id=execution_id, prompt="hello")

    assert calls == []
    rows = _reservations(granted, experiment_id)
    assert len(rows) == 1 and rows[0].stage == "dispatched" and rows[0].state == "reserved"
    # The hold blocks the next request rather than pretending the slot is free.
    fresh = TrustedBudgetClient(send=server.handle)
    assert fresh.open_request(estimated_tokens=15, run_id=run_id,
                              agent_execution_id=execution_id) is None


def test_a_refused_reservation_sends_nothing(granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id, tokens=1)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    calls = []

    assert _gateway(worker_factory, TrustedBudgetClient(send=server.handle), calls).request(
        run_id, agent_execution_id=execution_id, prompt="hello") is None

    assert calls == [] and _reservations(granted, experiment_id) == []


def test_unknown_usage_stops_later_requests_through_channel(granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a")
    client = TrustedBudgetClient(send=server.handle)
    calls = []
    assert _gateway(worker_factory, client, calls, usage=None).request(
        run_id, agent_execution_id=execution_id, prompt="hello") == "reply"
    assert len(calls) == 1
    assert _reservations(granted, experiment_id)[0].state == "unknown"
    assert client.open_request(estimated_tokens=15, run_id=run_id,
                               agent_execution_id=execution_id) is None


def test_concurrent_channel_dispatch_allows_only_one(granted, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a")
    client = TrustedBudgetClient(send=server.handle)
    keys = [client.open_request(estimated_tokens=15, run_id=run_id,
                                agent_execution_id=execution_id) for _ in range(2)]
    assert all(keys)
    barrier = Barrier(2)

    def dispatch(key):
        barrier.wait(timeout=10)
        try:
            client.mark_dispatched(key)
            return "ok"
        except BudgetChannelError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(dispatch, keys)) == ["ok", "refused"]
    assert sum(row.stage == "dispatched" for row in _reservations(granted, experiment_id)) == 1


def test_a_settle_that_cannot_be_recorded_blocks_the_next_request(
        granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    transport = _FlakyTransport(server, operation="settle", failures=2, after_serving=False)
    calls = []

    with pytest.raises(BudgetChannelError):
        _gateway(worker_factory, TrustedBudgetClient(send=transport), calls).request(
            run_id, agent_execution_id=execution_id, prompt="hello")

    # The model was called; the accounting was not recorded, so the request keeps blocking.
    assert len(calls) == 1
    rows = _reservations(granted, experiment_id)
    assert len(rows) == 1 and rows[0].stage == "dispatched" and rows[0].state == "reserved"
    fresh = TrustedBudgetClient(send=server.handle)
    assert fresh.open_request(estimated_tokens=15, run_id=run_id,
                              agent_execution_id=execution_id) is None


def test_the_trusted_side_refuses_another_experiments_request_key(
        granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    first = _experiment_for(control_factory, task_id)
    second = _experiment_for(control_factory, task_id, candidate=None)
    server_one = TrustedBudgetServer(session_factory=control_factory, experiment_id=first,
                                     candidate_label="candidate-a", operator="control-operator")
    server_two = TrustedBudgetServer(session_factory=control_factory, experiment_id=second,
                                     operator="control-operator")

    key = TrustedBudgetClient(send=server_one.handle).open_request(
        estimated_tokens=15, run_id=run_id, agent_execution_id=execution_id)
    assert key is not None
    with pytest.raises(BudgetChannelError):
        TrustedBudgetClient(send=server_two.handle).mark_dispatched(key)

    assert [row.state for row in _reservations(granted, first)] == ["reserved"]
    assert _reservations(granted, second) == []


def test_the_trusted_side_refuses_another_candidates_request_key(
        granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    with control_factory.begin() as session:
        ExperimentService(session).admit_candidate(
            experiment_id=experiment_id, label="candidate-b", execution_sha256="2" * 64)
    first = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                candidate_label="candidate-a")
    second = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-b")
    key = TrustedBudgetClient(send=first.handle).open_request(
        estimated_tokens=15, run_id=run_id, agent_execution_id=execution_id)
    assert key is not None
    for operation in ("dispatch", "settle", "unknown", "release"):
        result = second.handle({"op": operation, "request_key": str(key),
                                "input_tokens": 1, "output_tokens": 2, "reason": "test"})
        assert result["status"] == "conflict"
    rows = _reservations(granted, experiment_id)
    assert len(rows) == 1 and rows[0].state == "reserved" and rows[0].stage == "reserved"


def test_the_server_cannot_run_on_worker_credentials(granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    # A worker-side process that tries to host the trusted side itself is refused by the database.
    worker_server = TrustedBudgetServer(session_factory=worker_factory,
                                       experiment_id=experiment_id,
                                       candidate_label="candidate-a",
                                       operator="worker-identity")
    calls = []

    assert _gateway(worker_factory, TrustedBudgetClient(send=worker_server.handle),
                    calls).request(run_id, agent_execution_id=execution_id,
                                   prompt="hello") is None
    assert calls == [] and _reservations(granted, experiment_id) == []


def test_a_request_naming_another_tasks_run_is_refused(granted, tmp_path, monkeypatch):
    worker_factory, task_id, run_id, execution_id = _worker_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    client = TrustedBudgetClient(send=server.handle)

    assert client.open_request(estimated_tokens=15, run_id=uuid4(),
                               agent_execution_id=execution_id) is None
    assert client.open_request(estimated_tokens=15, run_id=run_id,
                               agent_execution_id=uuid4()) is None
    assert _reservations(granted, experiment_id) == []


# -- two processes, two database roles, one local session ---------------------------------
#
# The Worker runs in its own interpreter with its own database role. It receives the session key
# and its DSN on stdin (never on the command line, never in the environment), and it can only ask
# the trusted side for budget decisions. The control process keeps its own session and never hands
# it over. The model is a stub; what is real is the two processes, the two roles and the ledger.

CHILD_BOOTSTRAP = r"""
import json
import os
import sys
from uuid import UUID

from multiprocessing.connection import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

handshake = json.loads(sys.stdin.readline())
assert not any(name.startswith(('DEVFLOW_DB_', 'PG')) for name in os.environ)
try:
    connection = Client(handshake["address"], family=handshake["family"],
                        authkey=bytes.fromhex(handshake["authkey"]))
except Exception as error:
    print(json.dumps({"outcome": "connect_failed", "error": type(error).__name__}))
    raise SystemExit(2)

from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
from devflow_ai.experiment_channel import TrustedBudgetClient, connection_transport
from devflow_ai.model_gateway import ModelGateway, ModelReply

from sqlalchemy.engine import URL

db = handshake["db"]
engine = create_engine(URL.create(
    "postgresql+psycopg", username=db["username"], password=db["password"],
    database=db["database"], query={"host": db["host"], "port": str(db["port"])}))
factory = sessionmaker(bind=engine)
calls = []


def generate(prompt, max_output_tokens):
    calls.append(prompt)
    return ModelReply("reply", ModelTokenUsage(input_tokens=10, output_tokens=5))


gateway = ModelGateway(
    session_factory=factory, worker_id=handshake["worker_id"],
    estimate_input_tokens=lambda _: 5, generate=generate, max_output_tokens=10,
    experiment_budget=TrustedBudgetClient(send=connection_transport(connection)))
try:
    reply = gateway.request(UUID(handshake["run_id"]),
                            agent_execution_id=UUID(handshake["execution_id"]), prompt="hello")
    print(json.dumps({"outcome": "done", "reply": reply, "calls": len(calls)}))
except Exception as error:
    print(json.dumps({"outcome": "failed", "error": type(error).__name__,
                      "calls": len(calls)}))
    raise SystemExit(1)
"""


def _control_side_attempt(instance, tmp_path, monkeypatch, *, worker_id="two-process-worker"):
    """Task, Run and AgentExecution created by the control identity for that worker id."""
    admin_factory = instance.factory(instance.admin_url)
    repo, base_commit = _source_repo(tmp_path, monkeypatch)
    task_id = _create_task(instance, repo=str(repo), base_commit=base_commit, claimable=True)
    with admin_factory() as session:
        run, _ = RunCoordinator(session).start_initial_run(
            task_id, worker_id, f"two-process-{uuid4()}")
        run_id = run.id
    with admin_factory.begin() as session:
        execution = AgentExecution(run_id=run_id, agent_role=AgentRole.SUPERVISOR.value.lower(),
                                   status=AgentExecutionStatus.RUNNING, input={}, output={})
        session.add(execution)
        session.flush()
        execution_id = execution.id
    return task_id, run_id, execution_id


def _run_session(instance, *, experiment_id, run_id, execution_id, tamper_key=False):
    """Serve one session in this process while the Worker child runs in its own."""
    control_factory = instance.factory(instance.control_url)
    server = TrustedBudgetServer(session_factory=control_factory, experiment_id=experiment_id,
                                 candidate_label="candidate-a", operator="control-operator")
    authkey = secrets.token_bytes(32)
    directory = Path(tempfile.mkdtemp(prefix="dfbudget-"))
    address = session_address(directory)
    handshake = {
        "address": address, "family": LOCAL_FAMILY, "authkey": authkey.hex(),
        # The Worker's own connection settings and the session key travel on stdin, not on the
        # command line and not through the environment.
        "db": {"username": instance.worker_url.username,
               "password": instance.worker_url.password,
               "database": instance.worker_url.database,
               "host": instance.host, "port": instance.port},
        "worker_id": "two-process-worker", "run_id": str(run_id),
        "execution_id": str(execution_id),
    }
    if tamper_key:
        handshake["authkey"] = secrets.token_bytes(32).hex()
    child = None
    served = 0
    auth_failure = None
    try:
        with LocalBudgetSession(server=server, address=address, authkey=authkey) as session:
            allowed = ("PATH", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "WINDIR",
                       "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL")
            child_env = {name: os.environ[name] for name in allowed if name in os.environ}
            child = subprocess.Popen([sys.executable, "-c", CHILD_BOOTSTRAP], env=child_env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8")
            child.stdin.write(json.dumps(handshake) + "\n")
            child.stdin.close()
            child.stdin = None  # hand-shaken once; communicate() must not flush it again
            try:
                served = session.serve_one()
            except Exception as error:  # a client without the session key
                auth_failure = error
            out, err = child.communicate(timeout=180)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    payload = None
    for line in reversed((out or "").splitlines()):
        if line.strip().startswith("{"):
            payload = json.loads(line)
            break
    return {"returncode": child.returncode if child else None, "payload": payload,
            "served": served, "auth_failure": auth_failure, "stderr": err}


def test_two_processes_complete_one_budgeted_request(granted, tmp_path, monkeypatch):
    task_id, run_id, execution_id = _control_side_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)

    outcome = _run_session(granted, experiment_id=experiment_id, run_id=run_id,
                           execution_id=execution_id)

    assert outcome["returncode"] == 0, "Worker subprocess did not complete"
    assert outcome["payload"] == {"outcome": "done", "reply": "reply", "calls": 1}
    assert outcome["served"] == 3  # reserve, dispatch, settle
    rows = _reservations(granted, experiment_id)
    assert len(rows) == 1 and rows[0].state == "settled" and rows[0].actual_tokens == 15
    # The Worker role still holds no privilege on the ledger it just contributed to.
    admin = create_engine(granted.admin_url)
    try:
        with admin.connect() as connection:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert connection.scalar(text(
                    "SELECT has_table_privilege(:role, 'experiment_reservations', :privilege)"),
                    {"role": WORKER_ROLE, "privilege": privilege}) is False
    finally:
        admin.dispose()


def test_a_session_key_that_does_not_match_is_refused(granted, tmp_path, monkeypatch):
    task_id, run_id, execution_id = _control_side_attempt(granted, tmp_path, monkeypatch)
    control_factory = granted.factory(granted.control_url)
    experiment_id = _experiment_for(control_factory, task_id)

    outcome = _run_session(granted, experiment_id=experiment_id, run_id=run_id,
                           execution_id=execution_id, tamper_key=True)

    assert outcome["auth_failure"] is not None
    assert outcome["returncode"] == 2
    # A session with no key at all is refused before any connection is attempted.
    from devflow_ai.experiment_channel import connect_session
    with pytest.raises(ValueError):
        connect_session(address="unused", authkey=b"")
    assert outcome["payload"]["outcome"] == "connect_failed"
    assert outcome["payload"]["error"] == "AuthenticationError"
    # Nothing was booked and nothing was called.
    assert _reservations(granted, experiment_id) == []
