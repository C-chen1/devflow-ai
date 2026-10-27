from devflow_ai.schemas import TaskCreateRequest
from pydantic import ValidationError
import pytest

def test_task_create_request_requires_repo():
    with pytest.raises(ValidationError) as exc_info:
        TaskCreateRequest(title="title", description="description")
    errors = exc_info.value.errors()
    assert errors[0]["loc"] ==("repo",)

def test_task_create_request_accepts_issue_fields():
    task_request = TaskCreateRequest(title="title", description="description", repo="repo")
    assert task_request.title == "title"
    assert task_request.description == "description"
    assert task_request.repo == "repo"