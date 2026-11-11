import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import devflow_ai.admission_dispatch as dispatch
import devflow_ai.database as database
from devflow_ai.deepseek_adapter import DeepSeekPlannerAdapter


def test_dispatch_cli_emits_cursor_and_disposes_resources(monkeypatch, capsys):
    class Engine:
        disposed = False
        def dispose(self):
            self.disposed = True
    engine = Engine()
    task_id = uuid4()
    cursor = dispatch.AdmissionScanCursor(datetime(2026, 1, 1, tzinfo=timezone.utc), task_id)
    monkeypatch.setattr(database, "create_db_engine", lambda: engine)
    monkeypatch.setattr(database, "create_session_factory", lambda _: object())
    monkeypatch.setattr(DeepSeekPlannerAdapter, "from_env", lambda *a, **k: object())
    def run(**kwargs):
        assert kwargs["trusted_repositories"] == frozenset({"repo1", "repo2"})
        assert kwargs["after"] == cursor
        return dispatch.AdmissionDispatchResult(None, None, cursor, 1)
    monkeypatch.setattr(dispatch, "dispatch_one_admission", run)
    assert dispatch.main(["--trusted-repo", "repo1", "--trusted-repo", "repo2", "--page-size", "1",
        "--after-created-at", cursor.created_at.isoformat(), "--after-task-id", str(task_id)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["next_cursor"]["task_id"] == str(task_id)
    assert output["scanned_count"] == 1 and engine.disposed


def test_dispatch_cli_rejects_incomplete_cursor():
    with pytest.raises(SystemExit) as result:
        dispatch.main(["--trusted-repo", "repo", "--after-task-id", str(uuid4())])
    assert result.value.code == 2
