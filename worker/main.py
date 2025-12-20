from __future__ import annotations

import hashlib
import logging
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import py7zr
import rarfile
from mutagen import File as MutagenFile
from app.config import AppConfig, load_config
from app.models import TaskRecord, TaskStatus
from storage.db import Database

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("worker")

ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
MAX_ARCHIVE_FILES = 5000
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
HASH_BUFFER_SIZE = 1024 * 1024


class NeedPasswordError(Exception):
    """Raised when archive processing requires a password."""


@dataclass
class FileReport:
    source_path: Path
    relative_output: Path
    status: str
    reason: Optional[str] = None
    file_hash: Optional[str] = None


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
    normalized_allowlist = {item.lower() for item in allowlist}
    return path.suffix.lower().lstrip(".") in normalized_allowlist


def _safe_join(base: Path, member: Path) -> Path:
    target = base / member
    try:
        resolved = target.resolve()
    except FileNotFoundError:
        resolved = target.parent.resolve() / target.name
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"Refusing to write outside extraction root: {member}") from exc
    return resolved


def _handle_zip_archive(
    archive_path: Path,
    output_root: Path,
    allowlist: Sequence[str],
    password: Optional[str],
) -> Iterator[FileReport]:
    import zipfile

    with zipfile.ZipFile(archive_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError("Archive contains too many files")
        total_size = sum(info.file_size for info in members)
        if total_size > MAX_EXTRACTED_BYTES:
            raise RuntimeError("Archive exceeds extraction size limit")
        for info in members:
            requires_password = bool(info.flag_bits & 0x1)
            if requires_password and not password:
                raise NeedPasswordError("Password required for zip archive")
            member_path = Path(info.filename)
            if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
                logger.warning("Skipping unsafe path in archive: %s", member_path)
                continue
            is_symlink = stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK
            if info.is_dir() or is_symlink:
                continue
            if not _allowlist_matches(member_path, allowlist):
                yield FileReport(source_path=archive_path, relative_output=member_path, status="SKIPPED", reason="not_allowlisted")
                continue
            target_path = _safe_join(output_root, member_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info, "r", pwd=password.encode() if password else None) as src, target_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            except RuntimeError as exc:
                if "password" in str(exc).lower():
                    raise NeedPasswordError("Invalid or missing password for zip archive") from exc
                raise
            yield FileReport(source_path=target_path, relative_output=member_path, status="EXTRACTED")


def _handle_7z_archive(
    archive_path: Path,
    output_root: Path,
    allowlist: Sequence[str],
    password: Optional[str],
) -> Iterator[FileReport]:
    with py7zr.SevenZipFile(archive_path, mode="r", password=password) as archive:
        file_info = archive.list()
        regular_files = [item for item in file_info if not item.is_directory]
        if len(regular_files) > MAX_ARCHIVE_FILES:
            raise RuntimeError("Archive contains too many files")
        total_size = sum(item.uncompressed for item in regular_files if item.uncompressed)
        if total_size > MAX_EXTRACTED_BYTES:
            raise RuntimeError("Archive exceeds extraction size limit")
        try:
            extracted = archive.readall()
        except (py7zr.exceptions.PasswordRequired, py7zr.exceptions.WrongPassword) as exc:
            raise NeedPasswordError("Password required for 7z archive") from exc
        for name, bytes_io in extracted.items():
            member_path = Path(name)
            if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
                logger.warning("Skipping unsafe path in archive: %s", member_path)
                continue
            if not _allowlist_matches(member_path, allowlist):
                yield FileReport(source_path=archive_path, relative_output=member_path, status="SKIPPED", reason="not_allowlisted")
                continue
            target_path = _safe_join(output_root, member_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with target_path.open("wb") as dst:
                shutil.copyfileobj(bytes_io, dst)
            yield FileReport(source_path=target_path, relative_output=member_path, status="EXTRACTED")


def _handle_rar_archive(
    archive_path: Path,
    output_root: Path,
    allowlist: Sequence[str],
    password: Optional[str],
) -> Iterator[FileReport]:
    try:
        with rarfile.RarFile(archive_path, pwd=password) as archive:
            members = [item for item in archive.infolist() if item.is_file()]
            if len(members) > MAX_ARCHIVE_FILES:
                raise RuntimeError("Archive contains too many files")
            total_size = sum(item.file_size for item in members)
            if total_size > MAX_EXTRACTED_BYTES:
                raise RuntimeError("Archive exceeds extraction size limit")
            for member in members:
                if member.needs_password() and not password:
                    raise NeedPasswordError("Password required for rar archive")
                member_path = Path(member.filename)
                if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
                    logger.warning("Skipping unsafe path in archive: %s", member_path)
                    continue
                if member.is_symlink():
                    continue
                if not _allowlist_matches(member_path, allowlist):
                    yield FileReport(source_path=archive_path, relative_output=member_path, status="SKIPPED", reason="not_allowlisted")
                    continue
                target_path = _safe_join(output_root, member_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with archive.open(member) as src, target_path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                except rarfile.BadRarFile as exc:
                    if "password" in str(exc).lower():
                        raise NeedPasswordError("Invalid password for rar archive") from exc
                    raise
                yield FileReport(source_path=target_path, relative_output=member_path, status="EXTRACTED")
    except rarfile.NeedFirstVolume:
        raise RuntimeError("multi_volume_not_supported")
    except rarfile.RarCannotExec as exc:
        raise RuntimeError(f"rar_support_missing:{exc}")


def _process_archive(
    archive_path: Path,
    extract_root: Path,
    allowlist: Sequence[str],
    password: Optional[str],
) -> List[FileReport]:
    extract_root.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    if suffix == ".zip":
        return list(_handle_zip_archive(archive_path, extract_root, allowlist, password))
    if suffix == ".7z":
        return list(_handle_7z_archive(archive_path, extract_root, allowlist, password))
    if suffix == ".rar":
        return list(_handle_rar_archive(archive_path, extract_root, allowlist, password))
    raise RuntimeError(f"Unsupported archive type: {suffix}")


def _sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _gather_task_files(temp_dir: Path) -> Iterator[Path]:
    for file_path in temp_dir.rglob("*"):
        if file_path.is_file():
            yield file_path


def _plan_destination(task: TaskRecord, incoming_root: Path) -> Path:
    date_part = datetime.utcnow().strftime("%Y-%m-%d")
    safe_label = "".join(ch for ch in task.name if ch.isalnum() or ch in {"-", "_", " "}).strip()
    normalized_label = safe_label.replace(" ", "_")
    label = normalized_label or str(task.id)
    return incoming_root / date_part / label


def _read_tag_fields(file_path: Path) -> Dict[str, Optional[str]]:
    audio = MutagenFile(file_path, easy=True)
    if not audio or not audio.tags:
        return {"artist": None, "album": None, "title": None, "year": None}

    def _first_value(key: str) -> Optional[str]:
        values = audio.tags.get(key)
        if not values:
            return None
        return str(values[0]).strip() if str(values[0]).strip() else None

    year_value = _first_value("date") or _first_value("year")
    return {
        "artist": _first_value("artist"),
        "album": _first_value("album"),
        "title": _first_value("title"),
        "year": year_value,
    }


def _tags_incomplete(tag_values: Dict[str, Optional[str]]) -> bool:
    return not tag_values.get("artist") or not tag_values.get("album") or not tag_values.get("title")


def _process_task(
    db: Database,
    config: AppConfig,
    task: TaskRecord,
    incoming_root: Path,
) -> None:
    temp_dir = _task_temp_dir(config, task.id)
    if not temp_dir.exists():
        raise RuntimeError(f"Task temp directory missing: {temp_dir}")

    allowlist = config.allowlist
    extraction_root = temp_dir / "extract"
    file_reports: List[FileReport] = []
    password = str(task.context.get("password")) if task.context.get("password") else None

    for file_path in _gather_task_files(temp_dir):
        if extraction_root in file_path.parents:
            continue
        relative_to_task = file_path.relative_to(temp_dir)
        if file_path.suffix.lower() in ARCHIVE_EXTENSIONS:
            logger.info("Extracting archive %s", file_path)
            archive_base = extraction_root / relative_to_task.with_suffix("")
            try:
                extracted_reports = _process_archive(file_path, archive_base, allowlist, password)
                file_reports.extend(extracted_reports)
            except NeedPasswordError:
                db.update_task_context(task.id, {"password_required": True, "password": None})
                db.update_status(task.id, TaskStatus.NEED_PASSWORD)
                db.add_event(task.id, "need_password")
                return
            continue
        if not _allowlist_matches(file_path, allowlist):
            file_reports.append(
                FileReport(
                    source_path=file_path,
                    relative_output=relative_to_task,
                    status="SKIPPED",
                    reason="not_allowlisted",
                )
            )
            continue
        file_reports.append(
            FileReport(
                source_path=file_path,
                relative_output=relative_to_task,
                status="READY",
            )
        )

    duplicates_found = False
    destination_root = _plan_destination(task, incoming_root)
    prepared: List[FileReport] = []
    candidate_hashes: Dict[Path, str] = {}
    for report in file_reports:
        if report.status not in {"READY", "EXTRACTED"}:
            db.add_event(task.id, f"file_skipped:{report.relative_output}:{report.reason}")
            continue
        file_hash = _sha256sum(report.source_path)
        candidate_hashes[report.source_path] = file_hash
        report.file_hash = file_hash
        if db.hash_exists(file_hash):
            duplicates_found = True
            continue
        prepared.append(report)

    pending_tags: List[Dict[str, str]] = []
    for report in prepared:
        tag_details = _read_tag_fields(report.source_path)
        if _tags_incomplete(tag_details):
            pending_tags.append(
                {
                    "source": str(report.source_path.relative_to(temp_dir)),
                    "relative_output": str(report.relative_output),
                }
            )

    if pending_tags:
        db.clear_task_context_keys(task.id, {"password", "password_required"})
        db.update_task_context(task.id, {"pending_tags": pending_tags})
        db.update_status(task.id, TaskStatus.NEED_TAGS)
        db.add_event(task.id, "need_tags")
        return

    db.clear_task_context_keys(task.id, {"pending_tags", "password", "password_required"})

    for report in file_reports:
        if report.status not in {"READY", "EXTRACTED"}:
            continue
        file_hash = candidate_hashes.get(report.source_path)
        if not file_hash:
            continue
        if db.hash_exists(file_hash):
            duplicates_found = True
            db.add_event(task.id, f"duplicate:{report.relative_output}")
            continue
        db.add_known_hash(file_hash, task_id=task.id)
        target_path = destination_root / report.relative_output
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            report.source_path.replace(target_path)
        except OSError:
            shutil.copy2(report.source_path, target_path)
            report.source_path.unlink()
        db.add_event(task.id, f"stored:{report.relative_output}")

    final_status = TaskStatus.DONE_WITH_DUPLICATES if duplicates_found else TaskStatus.DONE
    db.update_status(task.id, final_status)
    db.add_event(task.id, f"completed:{len(prepared)}")


def main() -> None:
    config = _resolve_config()
    db = Database(config.db_path)
    db.initialize()
    incoming_root, _ = _validate_mounts(config)
    logger.info(
        "Worker started with music_root=%s incoming_root=%s temp_subdir=%s",
        config.music_root,
        incoming_root,
        config.temp_subdir,
    )
    try:
        while True:
            pending = db.list_tasks_by_status({TaskStatus.PROCESSING, TaskStatus.READY_FOR_PROCESSING})
            for task in pending:
                try:
                    if task.status == TaskStatus.READY_FOR_PROCESSING:
                        db.update_status(task.id, TaskStatus.PROCESSING)
                        db.add_event(task.id, "queued")
                    _process_task(db, config, task, incoming_root)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to process task %s", task.id)
                    db.add_event(task.id, f"error:{exc}")
                    db.update_status(task.id, TaskStatus.ERROR)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")


if __name__ == "__main__":
    main()
