from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import shutil

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from mutagen import File as MutagenFile

from app.config import AppConfig, ConfigError, load_config, update_config
from app.models import (
    ChunkUploadResponse,
    DebugDiskState,
    DebugFileState,
    DebugTaskResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    TaskCreateRequest,
    TaskEventResponse,
    TaskEventRecord,
    TaskFileCreateRequest,
    TaskFileCreateResponse,
    TaskFileRecord,
    TaskPasswordRequest,
    TaskRecord,
    TaskResponse,
    TaskStatus,
    TaskSummaryResponse,
    TaskSummaryWithEvents,
    TaskTagsResponse,
    TaskTagsUpdateRequest,
    TrackTag,
)
from storage.db import Database
from fastapi.encoders import jsonable_encoder


templates = Jinja2Templates(directory="templates")
app = FastAPI(title="Roon Server Helper API")
STUCK_THRESHOLD_MINUTES = 15


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


def _part_file_path(config: AppConfig, task_id: int, file_id: int) -> Path:
    return _task_uploads_dir(config, task_id) / f"{file_id}.part"


def _sync_part_file_state(
    *,
    config: AppConfig,
    task_id: int,
    file_record: TaskFileRecord,
    db: Database,
    require_presence: bool = False,
) -> tuple[TaskFileRecord, Path, int]:
    part_path = _part_file_path(config, task_id, file_record.id)
    part_path.parent.mkdir(parents=True, exist_ok=True)

    if part_path.exists():
        current_size = part_path.stat().st_size
        if current_size > file_record.expected_size:
            raise HTTPException(status_code=400, detail="Uploaded data exceeds expected size on disk")
        if current_size != file_record.uploaded_bytes:
            db.update_task_file_progress(file_record.id, current_size)
            refreshed = db.get_task_file(file_record.id)
            assert refreshed is not None
            file_record = refreshed
        return file_record, part_path, current_size

    if file_record.uploaded_bytes > 0:
        raise HTTPException(status_code=400, detail="Recorded upload progress missing on disk")
    if require_presence:
        raise HTTPException(status_code=400, detail="No uploaded data for file")
    return file_record, part_path, 0


def _read_existing_bytes(part_path: Path, offset: int, length: int) -> bytes:
    with part_path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)


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
    tasks = [jsonable_encoder(_task_to_response(t)) for t in db.list_tasks()]
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


@app.get("/health", response_model=Dict[str, object])
def healthcheck(db: Database = Depends(get_db)) -> Dict[str, object]:
    heartbeat = db.last_worker_heartbeat()
    now = datetime.now(timezone.utc)
    heartbeat_age_seconds: Optional[float] = None
    worker_status = "unknown"
    if heartbeat:
        heartbeat_age_seconds = (now - heartbeat).total_seconds()
        worker_status = "stale" if heartbeat_age_seconds > 120 else "ok"
    return {
        "status": "ok",
        "worker_status": worker_status,
        "worker_heartbeat_at": heartbeat.isoformat() if heartbeat else None,
        "worker_heartbeat_age_seconds": heartbeat_age_seconds,
    }


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


def _ensure_uploading_status(task: TaskRecord, db: Database) -> TaskRecord:
    updated_task = task
    if task.status == TaskStatus.CREATED:
        updated_task = db.update_status(task.id, TaskStatus.UPLOADING) or task
    elif task.status == TaskStatus.READY_FOR_PROCESSING:
        updated_task = db.update_status(task.id, TaskStatus.UPLOADING) or task
        db.add_event(task.id, "returned_to_uploading")
    return updated_task


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

    task = _ensure_uploading_status(task, db)
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
        raise HTTPException(status_code=409, detail="File already finalized")

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

    file_record, part_path, current_size = _sync_part_file_state(
        config=config, task_id=task_id, file_record=file_record, db=db
    )

    if start < current_size:
        if end < current_size:
            existing = _read_existing_bytes(part_path, start, chunk_length)
            if existing != body:
                raise HTTPException(
                    status_code=409,
                    detail=f"Chunk content mismatch at offset {start}. Resume from {current_size}",
                )
            return ChunkUploadResponse(
                next_offset=current_size, complete=current_size >= file_record.expected_size
            )
        raise HTTPException(
            status_code=409,
            detail=f"Chunk overlaps existing data. Resume from {current_size}",
        )

    if start != current_size:
        raise HTTPException(status_code=409, detail=f"Mismatched offset. Resume from {current_size}")

    with part_path.open("r+b" if part_path.exists() else "wb") as handle:
        handle.seek(start)
        handle.write(body)

    new_size = part_path.stat().st_size
    if new_size > file_record.expected_size:
        raise HTTPException(status_code=400, detail="Uploaded data exceeds expected size")
    db.update_task_file_progress(file_id, new_size)
    _ensure_uploading_status(task, db)
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
        final_path = _task_temp_dir(config, task_id) / file_record.relative_path
        if not final_path.exists():
            raise HTTPException(status_code=400, detail="Finalized file missing on disk")
        raise HTTPException(status_code=409, detail="File already finalized")

    file_record, part_path, current_size = _sync_part_file_state(
        config=config, task_id=task_id, file_record=file_record, db=db, require_presence=True
    )
    if current_size != file_record.expected_size:
        raise HTTPException(
            status_code=400,
            detail=(
                "Incomplete upload: "
                f"uploaded {current_size} of expected {file_record.expected_size} bytes"
            ),
        )

    final_path = _task_temp_dir(config, task_id) / file_record.relative_path
    if final_path.exists():
        raise HTTPException(status_code=409, detail="Target file already exists")

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


def _task_last_event(db: Database, task_id: int) -> Optional[TaskEventRecord]:
    return db.last_event(task_id)


def _task_recent_events(db: Database, task_id: int, limit: int) -> List[TaskEventRecord]:
    return db.list_recent_events(task_id, limit)


def _is_task_stuck(task: TaskRecord, last_event_at: Optional[datetime]) -> bool:
    if task.status in {
        TaskStatus.DONE,
        TaskStatus.DONE_WITH_DUPLICATES,
        TaskStatus.ERROR,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }:
        return False

    reference_time = last_event_at or task.updated_at or task.created_at
    age = datetime.now(timezone.utc) - reference_time
    return age > timedelta(minutes=STUCK_THRESHOLD_MINUTES)


def _task_to_summary(
    task_record: TaskRecord, last_event: Optional[TaskEventRecord], recent_events: List[TaskEventRecord]
) -> TaskSummaryWithEvents:
    last_event_at = last_event.created_at if last_event else None
    return TaskSummaryWithEvents(
        task=TaskSummaryResponse(**_task_to_response(task_record).dict(), last_event_at=last_event_at),
        recent_events=[_event_to_response(event) for event in recent_events],
        is_stuck=_is_task_stuck(task_record, last_event_at),
    )


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _build_debug_files(
    config: AppConfig, task_id: int, file_records: List[TaskFileRecord]
) -> List[DebugFileState]:
    results: List[DebugFileState] = []
    for record in file_records:
        final_path = _task_temp_dir(config, task_id) / record.relative_path
        final_exists = final_path.exists()
        final_size_on_disk = final_path.stat().st_size if final_exists and final_path.is_file() else None

        part_path = _task_uploads_dir(config, task_id) / f"{record.id}.part"
        part_exists = part_path.exists()
        part_size_on_disk = part_path.stat().st_size if part_exists and part_path.is_file() else None
        results.append(
            DebugFileState(
                id=record.id,
                relative_path=record.relative_path,
                expected_size=record.expected_size,
                uploaded_bytes=record.uploaded_bytes,
                finalized=record.finalized,
                disk_path=str(final_path),
                disk_exists=final_exists,
                disk_size_bytes=final_size_on_disk,
                file_hash=record.file_hash,
                part_path=str(part_path),
                part_exists=part_exists,
                part_size_bytes=part_size_on_disk,
                final_path=str(final_path),
                final_exists=final_exists,
                final_size_bytes=final_size_on_disk,
            )
        )
    return results


def _build_disk_state(config: AppConfig, task_id: int) -> DebugDiskState:
    temp_dir = _task_temp_dir(config, task_id)
    destination_root = config.music_root / config.incoming_subdir
    temp_size = _dir_size_bytes(temp_dir) if temp_dir.exists() else 0
    destination_exists = destination_root.exists()
    destination_size = _dir_size_bytes(destination_root) if destination_exists else 0
    music_root_usage = shutil.disk_usage(config.music_root)
    return DebugDiskState(
        temp_dir=str(temp_dir),
        temp_dir_exists=temp_dir.exists(),
        temp_dir_size_bytes=temp_size,
        destination_root=str(destination_root),
        destination_exists=destination_exists,
        destination_size_bytes=destination_size,
        music_root_free_bytes=music_root_usage.free,
        music_root_total_bytes=music_root_usage.total,
    )


@app.get("/api/tasks", response_model=List[TaskResponse])
def list_tasks(db: Database = Depends(get_db)) -> List[TaskResponse]:
    return [_task_to_response(task) for task in db.list_tasks()]


@app.get("/api/tasks/overview", response_model=List[TaskSummaryWithEvents])
def task_overview(db: Database = Depends(get_db)) -> List[TaskSummaryWithEvents]:
    tasks = db.list_tasks()
    overview: List[TaskSummaryWithEvents] = []
    for task in tasks:
        last_event = _task_last_event(db, task.id)
        recent_events = _task_recent_events(db, task.id, 10)
        overview.append(_task_to_summary(task, last_event, recent_events))
    return overview


@app.get("/api/tasks/{task_id}/debug", response_model=DebugTaskResponse)
def task_debug(task_id: int, db: Database = Depends(get_db), config: AppConfig = Depends(get_config)) -> DebugTaskResponse:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    last_event = _task_last_event(db, task_id)
    events = _task_recent_events(db, task_id, 50)
    files = db.list_task_files(task_id)
    debug_files = _build_debug_files(config, task_id, files)
    disk_state = _build_disk_state(config, task_id)
    return DebugTaskResponse(
        task=_task_to_response(task),
        last_event_at=last_event.created_at if last_event else None,
        recent_events=[_event_to_response(event) for event in events],
        files=debug_files,
        disk=disk_state,
        db_path=str(db.db_path),
        db_exists=db.db_path.exists(),
    )

