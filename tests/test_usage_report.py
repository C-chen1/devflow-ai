import json
import pytest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import devflow_ai.usage_report as usage_report_module
from devflow_ai.usage_report import (
    UsageReportError, compare_usage, load_run_usage, load_task_usage, main, summarize_usage,
)


def _settled(request_id, input_tokens, output_tokens):
    return (request_id, {"worker_id": "w", "reserved_tokens": input_tokens + 4096,
                         "actual_usage": {"input_tokens": input_tokens,
                                          "output_tokens": output_tokens}})


def _unsettled(request_id, reserved):
    return (request_id, {"worker_id": "w", "reserved_tokens": reserved, "actual_usage": None})


def test_usage_is_summed_by_role_and_unsettled_requests_are_kept_apart():
    report = summarize_usage([
        ("supervisor", {"model_requests": dict([_settled("r1", 300, 20), _settled("r2", 200, 10)])}),
        ("explorer", {"model_requests": dict([_settled("r3", 100, 5), _unsettled("r4", 4096)])}),
    ])

    supervisor, explorer = report["roles"]
    assert (supervisor["role"], supervisor["requests"], supervisor["input_tokens"],
            supervisor["output_tokens"], supervisor["total_tokens"]) == (
        "supervisor", 2, 500, 30, 530)
    assert (explorer["requests"], explorer["total_tokens"]) == (2, 105)
    # An unsettled request may still have been billed, so it is not folded into the total.
    assert explorer["unsettled_requests"] == 1 and explorer["reserved_tokens"] == 4096
    assert report["total_tokens"] == 635
    assert report["unsettled_requests"] == 1


def test_legacy_usage_without_requests_is_counted_but_not_charged():
    report = summarize_usage([("coder", {"prompt": "legacy"}), ("coder", None)])

    row = report["roles"][0]
    assert row["executions"] == 2 and row["requests"] == 0 and row["total_tokens"] == 0
    assert report["legacy_executions_without_usage"] == 1


def test_comparison_reports_roles_only_one_run_used():
    first = {"run_id": "first", **summarize_usage([
        ("supervisor", {"model_requests": dict([_settled("a", 300, 20)])})])}
    second = {"run_id": "second", **summarize_usage([
        ("supervisor", {"model_requests": dict([_settled("b", 100, 10)])}),
        ("planner", {"model_requests": dict([_settled("c", 50, 5)])}),
    ])}

    comparison = compare_usage(first, second)

    rows = {row["role"]: row for row in comparison["roles"]}
    assert rows["supervisor"] == {"role": "supervisor", "first_tokens": 320,
                                  "second_tokens": 110, "delta": -210, "change": -0.6562}
    assert rows["planner"]["first_tokens"] == 0 and rows["planner"]["change"] is None
    # The total also includes the role only the second Run used: 110 + 55 against 320.
    assert comparison["total"] == {"first_tokens": 320, "second_tokens": 165,
                                   "delta": -155, "change": -0.4844}
    # A cheaper attempt is not the same claim as a better one.
    assert "does not by itself prove" in comparison["caveat"]


def test_comparison_does_not_treat_unsettled_or_missing_usage_as_zero():
    first = {"run_id": "first", **summarize_usage([
        ("supervisor", {"model_requests": dict([_settled("a", 300, 20)])}),
        ("explorer", {"model_requests": dict([_unsettled("b", 100)])}),
    ])}
    second = {"run_id": "second", **summarize_usage([
        ("supervisor", {"model_requests": dict([_settled("c", 100, 10)])}),
        ("explorer", None),
    ])}
    comparison = compare_usage(first, second)
    roles = {row["role"]: row for row in comparison["roles"]}
    assert roles["supervisor"]["delta"] == -210
    assert roles["explorer"]["delta"] is None and roles["explorer"]["change"] is None
    assert comparison["total"]["delta"] is None and comparison["total"]["change"] is None
    assert not comparison["recorded_agent_usage_complete"]


@pytest.mark.parametrize("actual", [{"input_tokens": 10}, {"input_tokens": -1, "output_tokens": 1},
                                    {"input_tokens": True, "output_tokens": 1}])
def test_malformed_settled_usage_is_not_counted_as_zero(actual):
    with pytest.raises(UsageReportError, match="invalid token counts"):
        summarize_usage([("coder", {"model_requests": {"r": {"actual_usage": actual}}})])


def test_legacy_usage_makes_role_and_total_comparison_incomplete():
    first = {"run_id": "first", **summarize_usage([("coder", {"prompt": "legacy"})])}
    second = {"run_id": "second", **summarize_usage([("coder", {"model_requests": {}})])}
    comparison = compare_usage(first, second)
    assert first["unknown_usage_executions"] == 1
    assert comparison["roles"][0]["delta"] is None
    assert comparison["total"]["delta"] is None


def test_an_unknown_run_is_a_configuration_error(monkeypatch, capsys):
    def missing(session, run_id):
        raise UsageReportError("Run does not exist")

    monkeypatch.setattr(usage_report_module, "load_run_usage", missing)

    assert main(["--run", str(uuid4())], session_factory=lambda: nullcontext(None)) == 2
    assert "Run does not exist" in capsys.readouterr().err


def test_the_cli_prints_one_json_object(monkeypatch, capsys):
    def report(session, run_id):
        return {"run_id": str(run_id), "status": "RUNNING", "attempt_number": 1,
                "roles": [], "total_tokens": 0}

    monkeypatch.setattr(usage_report_module, "load_run_usage", report)

    assert main(["--run", str(uuid4())], session_factory=lambda: nullcontext(None)) == 0
    assert json.loads(capsys.readouterr().out.strip())["event"] == "run_usage"


def test_bad_identifiers_are_refused_before_touching_a_database(capsys):
    assert main(["--run", "not-a-uuid"], session_factory=lambda: nullcontext(None)) == 2
    assert "UUIDs" in capsys.readouterr().err


def test_load_run_usage_reads_role_and_usage_only():
    """The loader asks for role and usage; it never loads prompts or outputs."""
    run_id = uuid4()

    class _Status:
        value = "RUNNING"

    class _Run:
        status = _Status()
        attempt_number = 1

    class _Result:
        @staticmethod
        def all():
            return [("supervisor", {"model_requests": dict([_settled("r1", 10, 1)])})]

    class _Session:
        def get(self, model, key):
            return _Run()

        def execute(self, statement):
            return _Result()

    report = load_run_usage(_Session(), run_id)

    assert report["run_id"] == str(run_id) and report["total_tokens"] == 11
    assert report["status"] == "RUNNING" and report["attempt_number"] == 1


def test_task_usage_counts_admission_once_and_separates_all_runs(worker_session_factory, capsys):
    from devflow_ai.enums import AgentExecutionStatus, RunStatus
    from devflow_ai.models import AgentExecution, Run, Task
    from devflow_ai.services import TaskService

    factory = worker_session_factory
    with factory() as session:
        task, _ = TaskService(session).create_task(title="usage fixture", description="test",
            repo="example", idempotency_key=uuid4())
        task_id = task.id
    with factory.begin() as session:
        task = session.get(Task, task_id)
        task.admission_usage = dict([_settled("admission", 10, 2), _unsettled("pending", 20)])
        for attempt, status, usage in (
            (1, RunStatus.FAILED, {"model_requests": dict([_settled("first", 30, 4)])}),
            (2, RunStatus.PASSED, None),
        ):
            run = Run(task_id=task_id, attempt_number=attempt, status=status,
                creation_idempotency_key=str(uuid4()), worker_id="fixture",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
            session.add(run)
            session.flush()
            session.add(AgentExecution(run_id=run.id, agent_role="coder", status=AgentExecutionStatus.SUCCEEDED,
                input={}, llm_usage=usage))
    with factory() as session:
        report = load_task_usage(session, task_id)
    assert report["admission"]["total_tokens"] == 12
    assert [run["attempt_number"] for run in report["runs"]] == [1, 2]
    assert report["recorded_settled_tokens"] == 46
    assert report["unsettled_requests"] == 1 and report["unknown_usage_executions"] == 1
    assert main(["--task", str(task_id)], session_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["recorded_settled_tokens"] == 46
    assert main(["--task", str(task_id), "--compare", str(uuid4())], session_factory=factory) == 2


def test_task_usage_stays_consistent_when_a_run_is_added_during_read(worker_session_factory, monkeypatch):
    from devflow_ai.enums import AgentExecutionStatus, RunStatus
    from devflow_ai.models import AgentExecution, Run
    from devflow_ai.services import TaskService

    factory = worker_session_factory
    with factory() as session:
        task, _ = TaskService(session).create_task(title="usage fixture", description="test",
            repo="example", idempotency_key=uuid4())
        task_id = task.id
    def add_run(attempt):
        with factory.begin() as session:
            run = Run(task_id=task_id, attempt_number=attempt, status=RunStatus.FAILED,
                creation_idempotency_key=str(uuid4()), worker_id="fixture",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
            session.add(run)
            session.flush()
            session.add(AgentExecution(run_id=run.id, agent_role="coder", status=AgentExecutionStatus.SUCCEEDED,
                input={}, llm_usage={"model_requests": dict([_settled(str(attempt), 10, 1)])}))
    add_run(1)
    with factory() as session:
        original = session.execute
        inserted = []
        def execute_and_add(statement, *args, **kwargs):
            result = original(statement, *args, **kwargs)
            sql = str(statement)
            if not inserted and ("FROM runs" in sql or "LEFT OUTER JOIN runs" in sql):
                inserted.append(True)
                add_run(2)
            return result
        monkeypatch.setattr(session, "execute", execute_and_add)
        report = load_task_usage(session, task_id)
    assert inserted and [run["attempt_number"] for run in report["runs"]] == [1]
    assert report["recorded_settled_tokens"] == 11
    with factory() as session:
        later = load_task_usage(session, task_id)
    assert [run["attempt_number"] for run in later["runs"]] == [1, 2]
    assert later["recorded_settled_tokens"] == 22


def test_task_usage_supports_tasks_without_runs(worker_session_factory):
    from devflow_ai.services import TaskService

    factory = worker_session_factory
    with factory() as session:
        task, _ = TaskService(session).create_task(title="usage fixture", description="test",
            repo="example", idempotency_key=uuid4())
        task_id = task.id
    with factory() as session:
        report = load_task_usage(session, task_id)
        assert report["runs"] == [] and report["recorded_settled_tokens"] == 0
    with factory() as session:
        with pytest.raises(UsageReportError, match="Task does not exist"):
            load_task_usage(session, uuid4())
