from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, ConfigError, load_config, update_config
from app.models import (
    SettingsResponse,
    SettingsUpdateRequest,
    TaskCreateRequest,
    TaskEventResponse,
    TaskEventRecord,
    TaskRecord,
    TaskResponse,
    TaskStatus,
)
from storage.db import Database


templates = Jinja2Templates(directory="templates")
app = FastAPI(title="Roon Server Helper API")


def _resolve_config() -> AppConfig:
    default_path = Path("config.yaml")
    config_path = default_path if default_path.exists() else None
    return load_config(config_path=config_path)


@app.on_event("startup")
def startup_event() -> None:
    config = _resolve_config()
    database = Database(config.db_path)
    database.initialize()
    app.state.config = config
    app.state.db = database


def get_config(request: Request) -> AppConfig:
    config: AppConfig = request.app.state.config
    return config


def get_db(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


def _task_to_response(task_record: TaskRecord) -> TaskResponse:
    return TaskResponse(
        id=task_record.id,
        name=task_record.name,
        status=task_record.status,
        created_at=task_record.created_at,
        updated_at=task_record.updated_at,
        cleanup_after=task_record.cleanup_after,
    )


def _event_to_response(event_record: TaskEventRecord) -> TaskEventResponse:
    return TaskEventResponse(
        id=event_record.id,
        task_id=event_record.task_id,
        event=event_record.event,
        created_at=event_record.created_at,
    )


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, db: Database = Depends(get_db)) -> HTMLResponse:
    tasks = [_task_to_response(task) for task in db.list_tasks()]
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "tasks": tasks},
    )


@app.post("/api/tasks", response_model=TaskResponse)
def create_task(
    payload: TaskCreateRequest,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> TaskResponse:
    task = db.create_task(name=payload.name, cleanup_days=config.cleanup_days)
    return _task_to_response(task)


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: int, db: Database = Depends(get_db)) -> TaskResponse:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task)


@app.get("/api/tasks/{task_id}/events", response_model=List[TaskEventResponse])
def get_task_events(task_id: int, db: Database = Depends(get_db)) -> List[TaskEventResponse]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return [_event_to_response(event) for event in db.list_events(task_id)]


@app.post("/api/tasks/{task_id}/process", response_model=TaskResponse)
def queue_task_processing(task_id: int, db: Database = Depends(get_db)) -> TaskResponse:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.NEW:
        raise HTTPException(status_code=400, detail="Task is not in NEW state")
    db.add_event(task_id, "queued")
    updated_task = db.update_status(task_id, TaskStatus.PROCESSING)
    assert updated_task is not None
    return _task_to_response(updated_task)


@app.get("/api/settings", response_model=SettingsResponse)
def read_settings(config: AppConfig = Depends(get_config)) -> SettingsResponse:
    return SettingsResponse(**config.as_dict())


@app.post("/api/settings", response_model=SettingsResponse)
def update_settings(
    request: SettingsUpdateRequest,
    app_config: AppConfig = Depends(get_config),
) -> SettingsResponse:
    updates: Dict[str, str] = {
        key: value
        for key, value in request.dict().items()
        if value is not None
    }
    try:
        new_config = update_config(app_config, updates)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    app.state.config = new_config
    return SettingsResponse(**new_config.as_dict())


@app.get("/health", response_model=Dict[str, str])
def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}

