from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from mutagen import File as MutagenFile

from app.config import AppConfig, ConfigError, load_config, update_config
from app.models import (
    ChunkUploadResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    TaskCreateRequest,
    TaskEventResponse,
    TaskEventRecord,
    TaskFileCreateRequest,
    TaskFileCreateResponse,
    TaskPasswordRequest,
    TaskRecord,
    TaskResponse,
    TaskStatus,
    TaskTagsResponse,
    TaskTagsUpdateRequest,
    TrackTag,
)
from storage.db import Database


templates = Jinja2Templates(directory="templates")
app = FastAPI(title="Roon Server Helper API")


def _resolve_config() -> AppConfig:
    default_path = Path("config.yaml")
    config_path = default_path if default_path.exists() else None
    return load_config(config_path=config_path)


def _task_temp_dir(config: AppConfig, task_id: int) -> Path:
    return config.music_root / config.temp_subdir / str(task_id)


def _task_uploads_dir(config: AppConfig, task_id: int) -> Path:
    return _task_temp_dir(config, task_id) / "uploads"


def _validate_relative_path(relative_path: str) -> str:
    normalized = relative_path.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="relative_path is required")
    path_obj = Path(normalized)
    if path_obj.is_absolute() or any(part == ".." for part in path_obj.parts):
        raise HTTPException(status_code=400, detail="Invalid relative_path")
    return normalized


def _read_tag_fields(file_path: Path, display_path: Optional[str] = None) -> TrackTag:
    audio = MutagenFile(file_path, easy=True)
    if not audio or not audio.tags:
        return TrackTag(path=display_path or str(file_path.name), artist=None, album=None, title=None, year=None)

    def _first_value(key: str) -> Optional[str]:
        values = audio.tags.get(key)
        if not values:
            return None
        return str(values[0]).strip() if str(values[0]).strip() else None

    year_value = _first_value("date") or _first_value("year")
    return TrackTag(
        path=display_path or str(file_path.name),
        artist=_first_value("artist"),
        album=_first_value("album"),
        title=_first_value("title"),
        year=year_value,
    )


def _tags_incomplete(tag: TrackTag) -> bool:
    return not tag.artist or not tag.album or not tag.title


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
    if task.status != TaskStatus.READY_FOR_PROCESSING:
        raise HTTPException(status_code=400, detail="Task is not ready for processing")
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


def _parse_content_range(header_value: str) -> tuple[int, int, int]:
    if not header_value or not header_value.startswith("bytes "):
        raise HTTPException(status_code=411, detail="Content-Range header missing or invalid")
    try:
        range_part, total_part = header_value.replace("bytes ", "", 1).split("/")
        start_str, end_str = range_part.split("-")
        start = int(start_str)
        end = int(end_str)
        total = int(total_part)
    except ValueError as exc:
        raise HTTPException(status_code=411, detail="Invalid Content-Range format") from exc
    if end < start or total <= 0 or end >= total:
        raise HTTPException(status_code=411, detail="Invalid Content-Range values")
    return start, end, total


def _ensure_task_for_upload(db: Database, task_id: int) -> TaskRecord:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {TaskStatus.CREATED, TaskStatus.UPLOADING, TaskStatus.READY_FOR_PROCESSING}:
        raise HTTPException(status_code=400, detail="Task is not accepting uploads")
    return task


@app.post("/api/tasks/{task_id}/files", response_model=TaskFileCreateResponse)
def register_task_file(
    task_id: int,
    payload: TaskFileCreateRequest,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> TaskFileCreateResponse:
    task = _ensure_task_for_upload(db, task_id)
    relative_path = _validate_relative_path(payload.relative_path)
    total_size = db.total_expected_size_for_task(task_id)
    if total_size + payload.size_bytes > config.max_task_size_bytes:
        raise HTTPException(status_code=400, detail="Task size limit exceeded")

    uploads_dir = _task_uploads_dir(config, task_id)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    file_record = db.create_task_file(
        task_id=task_id,
        relative_path=relative_path,
        original_name=payload.original_name or Path(relative_path).name,
        expected_size=payload.size_bytes,
    )
    part_path = uploads_dir / f"{file_record.id}.part"
    part_path.touch(exist_ok=True)

    if task.status == TaskStatus.CREATED:
        db.update_status(task_id, TaskStatus.UPLOADING)
    db.add_event(task_id, f"file_registered:{file_record.id}")
    return TaskFileCreateResponse(file_id=file_record.id, max_chunk_bytes=config.max_chunk_bytes)


@app.post("/api/tasks/{task_id}/files/{file_id}/chunks", response_model=ChunkUploadResponse)
async def upload_task_file_chunk(
    task_id: int,
    file_id: int,
    request: Request,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> ChunkUploadResponse:
    task = _ensure_task_for_upload(db, task_id)
    file_record = db.get_task_file(file_id)
    if not file_record or file_record.task_id != task_id:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.finalized:
        raise HTTPException(status_code=400, detail="File already finalized")

    content_range = request.headers.get("Content-Range")
    start, end, total = _parse_content_range(content_range)
    if total != file_record.expected_size:
        raise HTTPException(status_code=400, detail="Size mismatch with registration")
    body = await request.body()
    chunk_length = end - start + 1
    if len(body) != chunk_length:
        raise HTTPException(status_code=400, detail="Body length does not match Content-Range")
    if chunk_length > config.max_chunk_bytes:
        raise HTTPException(status_code=400, detail="Chunk exceeds max_chunk_bytes")
    if start + chunk_length > file_record.expected_size:
        raise HTTPException(status_code=400, detail="Chunk exceeds registered file size")

    part_path = _task_uploads_dir(config, task_id) / f"{file_id}.part"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    current_size = part_path.stat().st_size if part_path.exists() else 0

    if current_size > file_record.expected_size:
        raise HTTPException(status_code=400, detail="Uploaded data exceeds expected size")
    if start < current_size:
        return ChunkUploadResponse(next_offset=current_size, complete=current_size >= file_record.expected_size)
    if start != current_size:
        raise HTTPException(status_code=409, detail=f"Mismatched offset. Resume from {current_size}")

    with part_path.open("r+b" if part_path.exists() else "wb") as handle:
        handle.seek(start)
        handle.write(body)

    new_size = part_path.stat().st_size
    db.update_task_file_progress(file_id, new_size)
    if task.status == TaskStatus.CREATED:
        db.update_status(task_id, TaskStatus.UPLOADING)
    db.add_event(task_id, f"chunk:{file_id}:{chunk_length}")
    return ChunkUploadResponse(next_offset=new_size, complete=new_size >= file_record.expected_size)


@app.post("/api/tasks/{task_id}/files/{file_id}/finalize", response_model=TaskResponse)
def finalize_task_file(
    task_id: int,
    file_id: int,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> TaskResponse:
    task = _ensure_task_for_upload(db, task_id)
    file_record = db.get_task_file(file_id)
    if not file_record or file_record.task_id != task_id:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.finalized:
        return _task_to_response(task)

    part_path = _task_uploads_dir(config, task_id) / f"{file_id}.part"
    if not part_path.exists():
        raise HTTPException(status_code=400, detail="No uploaded data for file")
    current_size = part_path.stat().st_size
    if current_size != file_record.expected_size:
        raise HTTPException(status_code=400, detail="Uploaded size does not match expected")

    final_path = _task_temp_dir(config, task_id) / file_record.relative_path
    final_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.replace(final_path)

    db.finalize_task_file(file_id, current_size)
    db.add_event(task_id, f"file_finalized:{file_id}")
    if db.all_files_finalized(task_id):
        db.update_status(task_id, TaskStatus.READY_FOR_PROCESSING)
        db.add_event(task_id, "ready_for_processing")
    updated_task = db.get_task(task_id)
    assert updated_task is not None
    return _task_to_response(updated_task)


@app.post("/api/tasks/{task_id}/password", response_model=TaskResponse)
def submit_task_password(
    task_id: int,
    payload: TaskPasswordRequest,
    db: Database = Depends(get_db),
) -> TaskResponse:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.NEED_PASSWORD:
        raise HTTPException(status_code=400, detail="Task is not waiting for a password")

    db.update_task_context(task_id, {"password": payload.password, "password_required": False})
    updated_task = db.update_status(task_id, TaskStatus.PROCESSING)
    if not updated_task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(updated_task)


def _task_pending_tags(task: TaskRecord) -> List[Dict[str, str]]:
    raw_pending = task.context.get("pending_tags") if task.context else None
    if not raw_pending:
        return []
    normalized: List[Dict[str, str]] = []
    for entry in raw_pending:
        source = entry.get("source") if isinstance(entry, dict) else None
        relative_output = entry.get("relative_output") if isinstance(entry, dict) else None
        if source and relative_output:
            normalized.append({"source": str(source), "relative_output": str(relative_output)})
    return normalized


@app.get("/api/tasks/{task_id}/tags", response_model=TaskTagsResponse)
def get_pending_tags(
    task_id: int,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> TaskTagsResponse:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.NEED_TAGS:
        raise HTTPException(status_code=400, detail="Task is not waiting for tags")
    pending = _task_pending_tags(task)
    if not pending:
        raise HTTPException(status_code=400, detail="No pending tags recorded")

    temp_dir = _task_temp_dir(config, task_id)
    tracks: List[TrackTag] = []
    for entry in pending:
        source_path = temp_dir / Path(entry["source"])
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"Source file missing: {entry['source']}")
        relative_output = entry["relative_output"]
        track_tags = _read_tag_fields(source_path, display_path=relative_output)
        tracks.append(track_tags)

    return TaskTagsResponse(tracks=tracks)


def _apply_tag_updates(
    source_path: Path,
    updated_values: TrackTag,
    batch_artist: Optional[str],
    batch_album: Optional[str],
    batch_year: Optional[str],
) -> None:
    audio = MutagenFile(source_path, easy=True)
    if not audio:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format for {source_path.name}")

    current = _read_tag_fields(source_path, display_path=updated_values.path)
    artist = updated_values.artist or batch_artist or current.artist
    album = updated_values.album or batch_album or current.album
    title = updated_values.title or current.title
    year = updated_values.year or batch_year or current.year

    if not artist or not album or not title:
        raise HTTPException(status_code=400, detail=f"Missing mandatory tags for {updated_values.path}")

    audio["artist"] = [artist]
    audio["album"] = [album]
    audio["title"] = [title]
    if year:
        audio["date"] = [year]
    audio.save()


@app.post("/api/tasks/{task_id}/tags", response_model=TaskResponse)
def update_pending_tags(
    task_id: int,
    payload: TaskTagsUpdateRequest,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> TaskResponse:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.NEED_TAGS:
        raise HTTPException(status_code=400, detail="Task is not waiting for tags")

    pending = _task_pending_tags(task)
    if not pending:
        raise HTTPException(status_code=400, detail="No pending tags recorded")

    pending_map = {entry["relative_output"]: entry for entry in pending}
    temp_dir = _task_temp_dir(config, task_id)

    for track in payload.tracks:
        if track.path not in pending_map:
            raise HTTPException(status_code=400, detail=f"Unexpected track path: {track.path}")
        source_relative = Path(pending_map[track.path]["source"])
        source_path = temp_dir / source_relative
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"Source file missing: {track.path}")
        _apply_tag_updates(
            source_path,
            TrackTag(path=track.path, artist=track.artist, album=track.album, title=track.title, year=track.year),
            payload.batch_artist,
            payload.batch_album,
            payload.batch_year,
        )

    db.clear_task_context_keys(task_id, {"pending_tags"})
    updated_task = db.update_status(task_id, TaskStatus.PROCESSING)
    if not updated_task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(updated_task)

