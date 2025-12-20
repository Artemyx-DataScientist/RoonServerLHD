from __future__ import annotations

import hashlib
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.config import AppConfig, load_config
from app.models import TaskFileRecord, TaskRecord, TaskStatus
from storage.db import Database

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("worker")

HASH_BUFFER_SIZE = 1024 * 1024
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}


@dataclass
class FileOutcome:
    record: TaskFileRecord
    source_path: Path
    relative_output: Path
    status: str
    reason: Optional[str] = None
    file_hash: Optional[str] = None
    destination: Optional[Path] = None


def _resolve_config() -> AppConfig:
    default_path = Path("config.yaml")
    return load_config(default_path if default_path.exists() else None)


def _is_writable(path: Path) -> bool:
    try:
        test_file = path / ".write_test"
        test_file.touch(exist_ok=True)
        test_file.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _validate_mounts(config: AppConfig) -> Tuple[Path, Path]:
    incoming_root = config.music_root / config.incoming_subdir
    temp_root = config.music_root / config.temp_subdir

    if not config.music_root.exists() or not config.music_root.is_dir():
        raise RuntimeError(f"music_root is not accessible: {config.music_root}")
    if not _is_writable(config.music_root):
        raise RuntimeError(f"music_root is not writable: {config.music_root}")

    for target in (incoming_root, temp_root):
        if config.mount_validation_mode == "strict":
            if not target.exists() or not target.is_dir():
                raise RuntimeError(f"Missing required directory: {target}")
            if not _is_writable(target):
                raise RuntimeError(f"Path is not writable: {target}")
        else:
            target.mkdir(parents=True, exist_ok=True)
            if not _is_writable(target):
                raise RuntimeError(f"Path is not writable: {target}")

    return incoming_root, temp_root


def _task_temp_dir(config: AppConfig, task_id: int) -> Path:
    return config.music_root / config.temp_subdir / str(task_id)


def _allowlist_matches(path: Path, allowlist: Sequence[str]) -> bool:
    if not allowlist:
        return True
    normalized_allowlist = {item.lower().lstrip(".") for item in allowlist}
    return path.suffix.lower().lstrip(".") in normalized_allowlist


def _sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _plan_destination(task: TaskRecord, incoming_root: Path) -> Path:
    date_part = datetime.utcnow().strftime("%Y-%m-%d")
    safe_label = "".join(ch for ch in task.name if ch.isalnum() or ch in {"-", "_", " "}).strip()
    normalized_label = safe_label.replace(" ", "_")
    label = normalized_label or str(task.id)
    return incoming_root / date_part / label


def _load_candidate_files(
    db: Database, config: AppConfig, task: TaskRecord, allowlist: Sequence[str]
) -> List[FileOutcome]:
    temp_dir = _task_temp_dir(config, task.id)
    if not temp_dir.exists():
        raise RuntimeError(f"Task temp directory missing: {temp_dir}")

    records = db.list_task_files(task.id)
    if not records:
        raise RuntimeError("No files registered for task")

    unfinalized = [record for record in records if not record.finalized]
    if unfinalized:
        pending = ", ".join(record.relative_path for record in unfinalized)
        raise RuntimeError(f"Not all files finalized: {pending}")

    outcomes: List[FileOutcome] = []
    for record in records:
        source_path = temp_dir / record.relative_path
        relative_output = Path(record.relative_path)
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError(f"Missing file on disk: {relative_output}")

        if source_path.suffix.lower() in ARCHIVE_EXTENSIONS:
            outcomes.append(
                FileOutcome(
                    record=record,
                    source_path=source_path,
                    relative_output=relative_output,
                    status="SKIPPED",
                    reason="archives not supported in worker v1",
                )
            )
            continue

        if not _allowlist_matches(source_path, allowlist):
            outcomes.append(
                FileOutcome(
                    record=record,
                    source_path=source_path,
                    relative_output=relative_output,
                    status="SKIPPED",
                    reason="extension not allowlisted",
                )
            )
            continue

        outcomes.append(
            FileOutcome(
                record=record,
                source_path=source_path,
                relative_output=relative_output,
                status="READY",
            )
        )

    return outcomes


def _process_task(db: Database, config: AppConfig, task: TaskRecord) -> None:
    incoming_root, _ = _validate_mounts(config)
    db.add_event(task.id, "mount_validated")
    allowlist = config.allowlist
    outcomes = _load_candidate_files(db, config, task, allowlist)

    duplicates_or_skips = False
    destination_root = _plan_destination(task, incoming_root)
    file_statuses: Dict[str, Dict[str, str]] = {}
    seen_hashes_in_task: Set[str] = set()

    for outcome in outcomes:
        relative_key = str(outcome.relative_output)
        if outcome.status == "SKIPPED":
            duplicates_or_skips = True
            file_statuses[relative_key] = {"status": outcome.status, "reason": outcome.reason or ""}
            db.add_event(task.id, f"file_skipped:{outcome.relative_output}:{outcome.reason}")
            continue

        db.add_event(task.id, f"hashing:{outcome.relative_output}")
        file_hash = _sha256sum(outcome.source_path)
        outcome.file_hash = file_hash

        if file_hash in seen_hashes_in_task or db.hash_exists(file_hash):
            duplicates_or_skips = True
            outcome.status = "DUPLICATE"
            file_statuses[relative_key] = {"status": outcome.status, "hash": file_hash}
            db.add_event(task.id, f"duplicate_detected:{outcome.relative_output}")
            continue

        seen_hashes_in_task.add(file_hash)
        db.add_known_hash(file_hash, task_id=task.id)
        db.update_task_file_hash(outcome.record.id, file_hash)
        target_path = destination_root / outcome.relative_output
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            outcome.source_path.replace(target_path)
        except OSError:
            shutil.copy2(outcome.source_path, target_path)
            outcome.source_path.unlink()

        outcome.status = "MOVED"
        outcome.destination = target_path
        file_statuses[relative_key] = {"status": outcome.status, "hash": file_hash}
        db.add_event(task.id, f"moved_to_incoming:{outcome.relative_output}")

    db.update_task_context(task.id, {"file_statuses": file_statuses})

    final_status = TaskStatus.DONE_WITH_DUPLICATES if duplicates_or_skips else TaskStatus.DONE
    db.update_status(task.id, final_status)
    db.add_event(task.id, f"task_completed:{final_status.value}")


def main() -> None:
    config = _resolve_config()
    db = Database(config.db_path)
    db.initialize()
    logger.info(
        "Worker started with music_root=%s incoming_subdir=%s temp_subdir=%s",
        config.music_root,
        config.incoming_subdir,
        config.temp_subdir,
    )
    try:
        while True:
            pending = db.list_tasks_by_status({TaskStatus.PROCESSING, TaskStatus.READY_FOR_PROCESSING})
            for task in pending:
                if task.status not in {TaskStatus.PROCESSING, TaskStatus.READY_FOR_PROCESSING}:
                    continue
                try:
                    db.add_event(task.id, "worker_started")
                    if task.status == TaskStatus.READY_FOR_PROCESSING:
                        db.update_status(task.id, TaskStatus.PROCESSING)
                        db.add_event(task.id, "queued")
                    _process_task(db, config, task)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to process task %s", task.id)
                    db.update_task_context(task.id, {"error_message": str(exc)})
                    db.add_event(task.id, f"task_failed:{exc}")
                    db.update_status(task.id, TaskStatus.ERROR)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")


if __name__ == "__main__":
    main()
