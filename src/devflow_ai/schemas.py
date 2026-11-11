from uuid import UUID
from datetime import datetime

import pydantic

from devflow_ai.enums import TaskStatus


class TaskCreateRequest(pydantic.BaseModel):
    title: str
    description: str
    repo:str


class TaskCreateResponse(pydantic.BaseModel):
    id: UUID
    status: TaskStatus


class TaskDetailResponse(pydantic.BaseModel):
    id: UUID
    status: TaskStatus
    title: str
    description: str
    repo: str


class AdmissionInterventionResponse(pydantic.BaseModel):
    reason_codes: list[str]
    message: str
    recorded_at: datetime
    attempt_count: int | None
    consecutive_timeouts: int | None


class TaskAdmissionStatusResponse(pydantic.BaseModel):
    task_id: UUID
    status: TaskStatus
    intervention: AdmissionInterventionResponse | None
    last_blocked: dict | None
    last_blocked_at: datetime | None
