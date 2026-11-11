"""HTTP entry point for DevFlow AI."""
from contextlib import asynccontextmanager
from fastapi import Depends,FastAPI,Header,HTTPException,Request,Response
from devflow_ai.database import create_db_engine,check_database_connection,create_session_factory
from devflow_ai.schemas import TaskAdmissionStatusResponse, TaskCreateRequest, TaskCreateResponse, TaskDetailResponse
from devflow_ai.admission_intervention import get_admission_status
from devflow_ai.services import IdempotencyConflictError, TaskService
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from uuid import UUID, uuid4
import logging
import json

logger = logging.getLogger("devflow_ai.api")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_db_engine()
    app.state.db_engine = engine
    session_factory = create_session_factory(engine)
    app.state.session_factory = session_factory
    yield
    engine.dispose()

app = FastAPI(title="DevFlow AI",
              lifespan = lifespan)

def get_db_session(request: Request):
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()

@app.post(
    "/tasks", response_model=TaskCreateResponse, status_code=201,
    responses={
        200: {"model": TaskCreateResponse, "description": "Existing Task reused"},
        409: {"description": "Submission key conflicts with existing Task content"},
    },
)
def create_task(
    task_request: TaskCreateRequest,
    response: Response,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(get_db_session),
):
    task_service = TaskService(session)
    try:
        task, created = task_service.create_task(
            title=task_request.title,
            description=task_request.description,
            repo=task_request.repo,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    response.status_code = 201 if created else 200
    return TaskCreateResponse(id=task.id, status=task.status)

@app.get("/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task(
    task_id: UUID,
    session: Session = Depends(get_db_session),
):
    task_service = TaskService(session)
    task = task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskDetailResponse(
        id=task.id,
        status=task.status,
        title=task.title,
        description=task.description,
        repo=task.repo,
    )

@app.get("/tasks/{task_id}/admission", response_model=TaskAdmissionStatusResponse)
def get_task_admission(task_id: UUID, session: Session = Depends(get_db_session)):
    result = get_admission_status(session, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@app.get("/health/live")
def health():
    return {"status": "ok"}

@app.get("/health/ready")
def health_ready():
    try:
        check_database_connection(app.state.db_engine)
        return {"status": "ok"}
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database unavailable")

@app.middleware("http")
async def request_context_middleware(request: Request,call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    log_data = {
        "event":"http_request_completed",
        "request_id":request.state.request_id,
        "status_code":response.status_code,
    }
    logger.info(json.dumps(log_data))
    return response

