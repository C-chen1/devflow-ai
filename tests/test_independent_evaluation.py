from pathlib import Path
import os

import pytest

from devflow_ai.independent_evaluation import PrivateCase, calibrate_stdio_suite, evaluate_stdio_candidate


@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="Requires an explicitly enabled Docker probe and postgres:17.11 image")
def test_independent_controller_runs_cases_without_copying_them_to_candidate(tmp_path: Path):
    candidate = tmp_path / "adapter.pl"
    candidate.write_text('while (<STDIN>) { print uc($_); }', encoding="utf-8")
    def evaluate(cases, command=("perl", "/candidate/adapter.pl")):
        return evaluate_stdio_candidate(workspace=tmp_path, image="postgres:17.11", command=command,
                                        cases=cases, timeout_seconds=15)
    cases = (PrivateCase(b"alpha\n", b"ALPHA\n"), PrivateCase(b"beta\n", b"BETA\n"))
    assert evaluate(cases).outcome == "passed"
    assert evaluate((PrivateCase(b"alpha\n", b"incorrect\n"),)).outcome == "failed"
    assert evaluate(cases, command=("perl", "/candidate/missing.pl")).outcome == "unavailable"
    timeout = evaluate_stdio_candidate(workspace=tmp_path, image="postgres:17.11",
        command=("perl", "-e", "sleep 10"), cases=cases, timeout_seconds=1)
    assert timeout.outcome == "unavailable"
    assert "alpha" not in repr(cases[0]) and "ALPHA" not in repr(cases[0])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["adapter.pl"]


@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="Requires an explicitly enabled Docker probe and postgres:17.11 image")
def test_suite_calibration_checks_both_versions(tmp_path: Path):
    baseline, corrected = tmp_path / "baseline", tmp_path / "corrected"
    baseline.mkdir()
    corrected.mkdir()
    (baseline / "adapter.pl").write_text('print "wrong\\n";', encoding="utf-8")
    (corrected / "adapter.pl").write_text('print "right\\n";', encoding="utf-8")
    cases = (PrivateCase(b"", b"right\n"),)
    args = {"image": "postgres:17.11", "command": ("perl", "/candidate/adapter.pl"), "cases": cases}
    result = calibrate_stdio_suite(baseline_workspace=baseline, corrected_workspace=corrected, **args)
    assert (result.outcome, result.baseline_outcome, result.corrected_outcome) == ("verified", "failed", "passed")
    assert calibrate_stdio_suite(baseline_workspace=corrected, corrected_workspace=baseline, **args).outcome == "invalid"
    with pytest.raises(ValueError, match="distinct"):
        calibrate_stdio_suite(baseline_workspace=baseline, corrected_workspace=baseline, **args)
