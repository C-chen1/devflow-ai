import json
from contextlib import nullcontext
from uuid import uuid4

import devflow_ai.usage_report as usage_report_module
from devflow_ai.usage_report import (
    UsageReportError, compare_usage, load_run_usage, main, summarize_usage,
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
