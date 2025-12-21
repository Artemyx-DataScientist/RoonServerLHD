from __future__ import annotations

import hashlib
import logging
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath
from typing import BinaryIO, Dict, List, Optional, Sequence, Set, Tuple

import py7zr
import rarfile

from app.config import AppConfig, load_config
from app.models import TaskFileRecord, TaskRecord, TaskStatus
from storage.db import Database

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("worker")

HASH_BUFFER_SIZE = 1024 * 1024
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
MAX_EXTRACTED_FILES = 1000
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class PasswordRequiredError(RuntimeError):
    def __init__(self, archive_path: Path) -> None:
        super().__init__(f"Password required for {archive_path}")
        self.archive_path = archive_path


class MountUnavailableError(RuntimeError):
    def __init__(self, mount_path: Path) -> None:
        super().__init__(f"Mount unavailable: {mount_path}")
        self.mount_path = mount_path


@dataclass
class FileOutcome:
    record: Optional[TaskFileRecord]
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
    if config.mount_validation_mode == "strict" and not config.music_root.is_mount():
        raise MountUnavailableError(config.music_root)
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


def _record_temp_state(db: Database, temp_dir: Path, task_id: int, label: str) -> None:
    if not temp_dir.exists():
        return
    for path in sorted(temp_dir.rglob("*")):
        relative_path = path.relative_to(temp_dir)
        db.add_event(task_id, f"temp_leftover:{label}:{relative_path}")


def _cleanup_temp_dir(db: Database, temp_dir: Path, task_id: int) -> None:
    if not temp_dir.exists():
        return
    try:
        shutil.rmtree(temp_dir)
        db.add_event(task_id, "temp_cleaned")
    except OSError as exc:  # noqa: PERF203
        db.add_event(task_id, f"temp_cleanup_failed:{exc}")


def _allowlist_matches(path: Path, allowlist: Sequence[str]) -> bool:
    if not allowlist:
        return True
    normalized_allowlist = {item.lower().lstrip(".") for item in allowlist}
    return path.suffix.lower().lstrip(".") in normalized_allowlist


def _sanitize_member_path(member_path: str) -> Optional[Path]:
    if not member_path:
        return None
    candidate = PurePath(member_path)
    if candidate.is_absolute():
        return None
    filtered_parts = [part for part in candidate.parts if part not in {"..", ".", ""}]
    if not filtered_parts:
        return None
    return Path(*filtered_parts)


def _is_within_base(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


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


def _prepare_extract_root(temp_dir: Path, archive_relative: Path) -> Path:
    return temp_dir / "extract" / archive_relative.with_suffix("")


def _request_password(db: Database, task: TaskRecord, archive_relative: Path) -> None:
    db.update_task_context(
        task.id,
        {
            "password_required": True,
            "password": None,
            "password_for": str(archive_relative),
        },
    )
    db.update_status(task.id, TaskStatus.NEED_PASSWORD)
    db.add_event(task.id, f"need_password:{archive_relative}")
    raise PasswordRequiredError(archive_relative)


def _ensure_extraction_limits(current_count: int, current_size: int, info_size: int) -> None:
    if current_count + 1 > MAX_EXTRACTED_FILES:
        raise RuntimeError("Extraction aborted: too many files")
    if current_size + info_size > MAX_EXTRACTED_BYTES:
        raise RuntimeError("Extraction aborted: total extracted size limit exceeded")


def _write_stream_to_file(source: BinaryIO, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        shutil.copyfileobj(source, handle)


def _collect_extracted_outcomes(
    db: Database,
    config: AppConfig,
    task: TaskRecord,
    record: TaskFileRecord,
    allowlist: Sequence[str],
    password: Optional[str],
) -> List[FileOutcome]:
    temp_dir = _task_temp_dir(config, task.id)
    archive_path = temp_dir / record.relative_path
    extraction_root = _prepare_extract_root(temp_dir, Path(record.relative_path))
    if extraction_root.exists():
        shutil.rmtree(extraction_root)
    db.add_event(task.id, f"extracting:{record.relative_path}")
    outcomes: List[FileOutcome] = []
    extracted_count = 0
    extracted_size = 0

    def add_skipped(reason: str, relative_output: Path) -> None:
        outcomes.append(
            FileOutcome(
                record=None,
                source_path=extraction_root / relative_output,
                relative_output=Path("extract") / Path(record.relative_path).with_suffix("") / relative_output,
                status="SKIPPED",
                reason=reason,
            )
        )
        db.add_event(task.id, f"extract_skipped:{record.relative_path}:{relative_output}:{reason}")

    try:
        if archive_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if info.flag_bits & 0x1 and not password:
                        _request_password(db, task, Path(record.relative_path))
                    safe_name = _sanitize_member_path(info.filename)
                    if not safe_name:
                        db.add_event(task.id, f"extract_unsafe_path:{record.relative_path}:{info.filename}")
                        continue
                    destination = extraction_root / safe_name
                    if not _is_within_base(extraction_root, destination):
                        db.add_event(task.id, f"extract_unsafe_path:{record.relative_path}:{info.filename}")
                        continue
                    file_size = info.file_size or 0
                    if (extraction_root / safe_name).suffix.lower() in ARCHIVE_EXTENSIONS:
                        add_skipped("nested_archives_not_processed", safe_name)
                        continue
                    if not _allowlist_matches(Path(safe_name), allowlist):
                        add_skipped("extension_not_allowlisted", safe_name)
                        continue
                    is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
                    if is_symlink:
                        add_skipped("symlink_entries_not_allowed", safe_name)
                        continue
                    _ensure_extraction_limits(extracted_count, extracted_size, file_size)
                    extracted_count += 1
                    extracted_size += file_size
                    try:
                        with archive.open(info, pwd=password.encode() if password else None) as source:
                            _write_stream_to_file(source, destination)
                    except RuntimeError as exc:
                        message = str(exc).lower()
                        if "password" in message:
                            _request_password(db, task, Path(record.relative_path))
                        raise
                    rel_output = Path("extract") / Path(record.relative_path).with_suffix("") / safe_name
                    full_output = extraction_root / safe_name
                    outcomes.append(
                        FileOutcome(
                            record=None,
                            source_path=full_output,
                            relative_output=rel_output,
                            status="READY",
                        )
                    )
        elif archive_path.suffix.lower() == ".7z":
            try:
                with py7zr.SevenZipFile(archive_path, mode="r", password=password) as archive:
                    for info in archive.list():
                        if info.is_directory:
                            continue
                        safe_name = _sanitize_member_path(info.filename)
                        if not safe_name:
                            db.add_event(task.id, f"extract_unsafe_path:{record.relative_path}:{info.filename}")
                            continue
                        destination = extraction_root / safe_name
                        if not _is_within_base(extraction_root, destination):
                            db.add_event(task.id, f"extract_unsafe_path:{record.relative_path}:{info.filename}")
                            continue
                        file_size = int(info.uncompressed) if info.uncompressed is not None else 0
                        if getattr(info, "is_symlink", False):
                            add_skipped("symlink_entries_not_allowed", safe_name)
                            continue
                        if destination.suffix.lower() in ARCHIVE_EXTENSIONS:
                            add_skipped("nested_archives_not_processed", safe_name)
                            continue
                        if not _allowlist_matches(Path(safe_name), allowlist):
                            add_skipped("extension_not_allowlisted", safe_name)
                            continue
                        _ensure_extraction_limits(extracted_count, extracted_size, file_size)
                        extracted_count += 1
                        extracted_size += file_size
                        try:
                            content = archive.read([info.filename]).get(info.filename)
                        except (py7zr.exceptions.PasswordRequired, py7zr.exceptions.WrongPassword):
                            _request_password(db, task, Path(record.relative_path))
                        if content is None:
                            add_skipped("failed_to_read_member", safe_name)
                            continue
                        _write_stream_to_file(content, destination)
                        rel_output = Path("extract") / Path(record.relative_path).with_suffix("") / safe_name
                        outcomes.append(
                            FileOutcome(
                                record=None,
                                source_path=destination,
                                relative_output=rel_output,
                                status="READY",
                            )
                        )
            except (py7zr.exceptions.PasswordRequired, py7zr.exceptions.WrongPassword):
                _request_password(db, task, Path(record.relative_path))
        elif archive_path.suffix.lower() == ".rar":
            try:
                with rarfile.RarFile(archive_path, mode="r") as archive:
                    for info in archive.infolist():
                        if info.is_dir():
                            continue
                        if info.needs_password() and not password:
                            _request_password(db, task, Path(record.relative_path))
                        safe_name = _sanitize_member_path(info.filename)
                        if not safe_name:
                            db.add_event(task.id, f"extract_unsafe_path:{record.relative_path}:{info.filename}")
                            continue
                        destination = extraction_root / safe_name
                        if not _is_within_base(extraction_root, destination):
                            db.add_event(task.id, f"extract_unsafe_path:{record.relative_path}:{info.filename}")
                            continue
                        file_size = info.file_size or 0
                        if getattr(info, "is_symlink", False):
                            add_skipped("symlink_entries_not_allowed", safe_name)
                            continue
                        if destination.suffix.lower() in ARCHIVE_EXTENSIONS:
                            add_skipped("nested_archives_not_processed", safe_name)
                            continue
                        if not _allowlist_matches(Path(safe_name), allowlist):
                            add_skipped("extension_not_allowlisted", safe_name)
                            continue
                        _ensure_extraction_limits(extracted_count, extracted_size, file_size)
                        extracted_count += 1
                        extracted_size += file_size
                        try:
                            with archive.open(info, pwd=password) as source:
                                _write_stream_to_file(source, destination)
                        except (rarfile.BadRarFile, rarfile.PasswordRequired, rarfile.RarWrongPassword):
                            _request_password(db, task, Path(record.relative_path))
                        rel_output = Path("extract") / Path(record.relative_path).with_suffix("") / safe_name
                        outcomes.append(
                            FileOutcome(
                                record=None,
                                source_path=destination,
                                relative_output=rel_output,
                                status="READY",
                            )
                        )
            except (rarfile.BadRarFile, rarfile.PasswordRequired, rarfile.RarWrongPassword) as exc:
                message = str(exc).lower()
                if "password" in message:
                    _request_password(db, task, Path(record.relative_path))
                raise
        else:
            add_skipped("unsupported_archive", Path(record.relative_path))
    finally:
        if outcomes:
            db.add_event(task.id, f"extracted:{record.relative_path}:{len(outcomes)}")

    return outcomes


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
    password = str(task.context.get("password")) if task.context.get("password") else None
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
                    status="ARCHIVE_SOURCE",
                    reason="archive_extracted",
                )
            )
            extracted_outcomes = _collect_extracted_outcomes(
                db, config, task, record, allowlist, password
            )
            outcomes.extend(extracted_outcomes)
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
    try:
        outcomes = _load_candidate_files(db, config, task, allowlist)
    except PasswordRequiredError:
        return

    duplicates_or_skips = False
    destination_root = _plan_destination(task, incoming_root)
    file_statuses: Dict[str, Dict[str, str]] = {}
    seen_hashes_in_task: Set[str] = set()

    for outcome in outcomes:
        relative_key = str(outcome.relative_output)
        if outcome.status == "ARCHIVE_SOURCE":
            file_statuses[relative_key] = {"status": outcome.status, "reason": outcome.reason or ""}
            db.add_event(task.id, f"archive_ignored:{outcome.relative_output}")
            continue
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
        if outcome.record:
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
    _cleanup_temp_dir(db, _task_temp_dir(config, task.id), task.id)


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
            db.record_worker_heartbeat()
            pending = db.list_tasks_by_status({TaskStatus.PROCESSING, TaskStatus.READY_FOR_PROCESSING})
            for task in pending:
                if task.status not in {TaskStatus.PROCESSING, TaskStatus.READY_FOR_PROCESSING}:
                    continue
                db.add_event(task.id, "worker_started")
                temp_dir = _task_temp_dir(config, task.id)
                try:
                    if task.status == TaskStatus.READY_FOR_PROCESSING:
                        db.update_status(task.id, TaskStatus.PROCESSING)
                        db.add_event(task.id, "queued")
                    _process_task(db, config, task)
                    db.add_event(task.id, "worker_finished")
                except MountUnavailableError as exc:
                    logger.exception("Mount unavailable for task %s", task.id)
                    db.add_event(task.id, "mount_unavailable")
                    _record_temp_state(db, temp_dir, task.id, "mount_unavailable")
                    db.update_task_context(task.id, {"error_message": str(exc)})
                    db.add_event(task.id, f"worker_failed:{exc}")
                    db.update_status(task.id, TaskStatus.ERROR)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to process task %s", task.id)
                    _record_temp_state(db, temp_dir, task.id, "error")
                    db.update_task_context(task.id, {"error_message": str(exc)})
                    db.add_event(task.id, f"worker_failed:{exc}")
                    db.update_status(task.id, TaskStatus.ERROR)
                else:
                    _record_temp_state(db, temp_dir, task.id, "post_process")
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")


if __name__ == "__main__":
    main()
