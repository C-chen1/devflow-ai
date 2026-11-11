import json
from uuid import uuid4

import devflow_ai.admission as admission
import devflow_ai.database as database
from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter


def test_admission_cli_provides_explicit_trust_and_disposes_engine(monkeypatch, capsys):
    class Engine:
        disposed = False
        def dispose(self):
            self.disposed = True
    engine = Engine()
    monkeypatch.setattr(database, "create_db_engine", lambda: engine)
    monkeypatch.setattr(database, "create_session_factory", lambda _: object())
    monkeypatch.setattr(DeepSeekPlannerAdapter, "from_env", lambda *args, **kwargs: object())
    seen = []
    def attempt(task_id, *, trusted_repository, **kwargs):
        seen.extend([trusted_repository("approved"), trusted_repository("another")])
        return admission.AdmissionAttemptResult("BLOCKED", "missing information")
    monkeypatch.setattr(admission, "attempt_admission", attempt)
    task_id = uuid4()
    assert admission.main(["--task-id", str(task_id), "--trusted-repo", "approved"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["task_id"] == str(task_id) and output["outcome"] == "BLOCKED"
    assert seen == [True, False] and engine.disposed


def test_admission_cli_reports_error_without_exception_details(monkeypatch, capsys):
    class Engine:
        disposed = False
        def dispose(self):
            self.disposed = True
    engine = Engine()
    monkeypatch.setattr(database, "create_db_engine", lambda: engine)
    monkeypatch.setattr(database, "create_session_factory", lambda _: object())
    def fail(*args, **kwargs):
        raise ValueError("private details")
    monkeypatch.setattr(DeepSeekPlannerAdapter, "from_env", fail)
    assert admission.main(["--task-id", str(uuid4()), "--trusted-repo", "approved"]) == 1
    output = capsys.readouterr()
    assert "private details" not in output.err
    assert json.loads(output.err)["error_type"] == "ValueError"
    assert engine.disposed
