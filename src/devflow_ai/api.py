"""HTTP entry point for DevFlow AI."""
from contextlib import asynccontextmanager
from fastapi import FastAPI,HTTPException,Request
from devflow_ai.database import create_db_engine,check_database_connection
from sqlalchemy.exc import SQLAlchemyError
from uuid import uuid4
import logging
import json

logger = logging.getLogger("devflow_ai.api")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_db_engine()
    app.state.db_engine = engine
    yield
    engine.dispose()

app = FastAPI(title="DevFlow AI",
              lifespan = lifespan)

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

