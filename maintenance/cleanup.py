from __future__ import annotations

import argparse
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app.config import AppConfig, ConfigError, _load_mapping_from_file, load_config

DEFAULT_TEMP_TTL_DAYS = 14
DEFAULT_INCOMING_TTL_DAYS = 30


@dataclass
class CleanupSettings:
    temp_ttl_days: int = DEFAULT_TEMP_TTL_DAYS
    incoming_ttl_days: int = DEFAULT_INCOMING_TTL_DAYS
    dry_run: bool = False
    log_level: str = "INFO"


@dataclass
class CleanupArgs:
    config_path: Optional[Path]
    force_dry_run: bool


def _parse_args() -> CleanupArgs:
    parser = argparse.ArgumentParser(description="Cleanup old Roon uploader data")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config file (YAML or JSON). Defaults to config.yaml if present.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without removing anything.",
    )
    args = parser.parse_args()
    return CleanupArgs(config_path=args.config, force_dry_run=args.dry_run)


def _default_config_path() -> Optional[Path]:
    candidate = Path("config.yaml")
    return candidate if candidate.exists() else None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_non_negative_int(value: object, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be non-negative")
    return parsed


def _load_file_values(config_path: Optional[Path]) -> Dict[str, object]:
    if not config_path:
        return {}
    return _load_mapping_from_file(config_path)


def _build_settings(file_values: Dict[str, object], force_dry_run: bool) -> CleanupSettings:
    temp_ttl_days = _coerce_non_negative_int(
        os.environ.get("TEMP_TTL_DAYS") or file_values.get("temp_ttl_days"),
        DEFAULT_TEMP_TTL_DAYS,
        "TEMP_TTL_DAYS",
    )
    incoming_ttl_days = _coerce_non_negative_int(
        os.environ.get("INCOMING_TTL_DAYS") or file_values.get("incoming_ttl_days"),
        DEFAULT_INCOMING_TTL_DAYS,
        "INCOMING_TTL_DAYS",
    )
    raw_dry_run = os.environ.get("CLEANUP_DRY_RUN")
    dry_run = force_dry_run or (
        _coerce_bool(raw_dry_run) if raw_dry_run is not None else _coerce_bool(file_values.get("cleanup_dry_run"))
    )
    log_level = (
        os.environ.get("CLEANUP_LOG_LEVEL")
        or str(file_values.get("cleanup_log_level", CleanupSettings.log_level))
    )
    return CleanupSettings(
        temp_ttl_days=temp_ttl_days,
        incoming_ttl_days=incoming_ttl_days,
        dry_run=dry_run,
        log_level=log_level,
    )


def _is_within_base(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _is_writable(path: Path) -> bool:
    try:
        probe = path / ".cleanup_write_probe"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _ensure_roots(app_config: AppConfig) -> Tuple[Path, Path]:
    music_root = app_config.music_root
    if not music_root.exists() or not music_root.is_dir():
        raise RuntimeError(f"music_root is not accessible: {music_root}")
    if not _is_writable(music_root):
        raise RuntimeError(f"music_root is not writable: {music_root}")

    incoming_root = music_root / app_config.incoming_subdir
    temp_root = music_root / app_config.temp_subdir

    for candidate in (incoming_root, temp_root):
        if not _is_within_base(music_root, candidate):
            raise RuntimeError(f"Path {candidate} is outside of music_root; aborting cleanup")

    return incoming_root, temp_root


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                logging.warning("Cannot read size for %s", child)
    return total


def _collect_temp_candidates(temp_root: Path, ttl_days: int, now: datetime) -> List[Path]:
    if not temp_root.exists():
        logging.info("Temp root %s does not exist; nothing to clean", temp_root)
        return []
    cutoff = now - timedelta(days=ttl_days)
    candidates: List[Path] = []
    for child in temp_root.iterdir():
        if child.is_symlink():
            logging.warning("Skipping symlink in temp directory: %s", child)
            continue
        if not child.is_dir():
            logging.debug("Skipping non-directory in temp directory: %s", child)
            continue
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime)
        except OSError:
            logging.warning("Cannot read mtime for %s", child)
            continue
        if mtime < cutoff:
            candidates.append(child)
    return candidates


def _collect_incoming_candidates(incoming_root: Path, ttl_days: int, now: datetime) -> List[Path]:
    if not incoming_root.exists():
        logging.info("Incoming root %s does not exist; nothing to clean", incoming_root)
        return []
    cutoff_date = (now - timedelta(days=ttl_days)).date()
    candidates: List[Path] = []
    for child in incoming_root.iterdir():
        if child.is_symlink():
            logging.warning("Skipping symlink in incoming directory: %s", child)
            continue
        if not child.is_dir():
            logging.debug("Skipping non-directory in incoming directory: %s", child)
            continue
        try:
            folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            logging.warning("Skipping non-date folder in incoming directory: %s", child)
            continue
        if folder_date < cutoff_date:
            candidates.append(child)
    return candidates


def _remove_directories(directories: Iterable[Path], dry_run: bool) -> Tuple[int, int]:
    removed = 0
    freed_bytes = 0
    for directory in directories:
        if dry_run:
            logging.info("[dry-run] Would delete %s", directory)
            continue
        size = _dir_size_bytes(directory)
        try:
            shutil.rmtree(directory)
            removed += 1
            freed_bytes += size
            logging.info("Deleted %s", directory)
        except OSError as exc:
            logging.error("Failed to delete %s: %s", directory, exc)
    return removed, freed_bytes


def _configure_logging(log_level: str) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)


def main() -> int:
    args = _parse_args()
    config_path = args.config_path or _default_config_path()

    try:
        file_values = _load_file_values(config_path)
        app_config = load_config(config_path)
        settings = _build_settings(file_values, args.force_dry_run)
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
        logging.error("Configuration error: %s", exc)
        return 1

    _configure_logging(settings.log_level)
    logging.info(
        "Starting cleanup with music_root=%s, temp_ttl_days=%s, incoming_ttl_days=%s, dry_run=%s",
        app_config.music_root,
        settings.temp_ttl_days,
        settings.incoming_ttl_days,
        settings.dry_run,
    )

    try:
        incoming_root, temp_root = _ensure_roots(app_config)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1

    now = datetime.now()

    temp_candidates = _collect_temp_candidates(temp_root, settings.temp_ttl_days, now)
    incoming_candidates = _collect_incoming_candidates(incoming_root, settings.incoming_ttl_days, now)

    logging.info("Temp cleanup candidates: %s", [str(path) for path in temp_candidates])
    logging.info("Incoming cleanup candidates: %s", [str(path) for path in incoming_candidates])

    temp_removed, temp_freed = _remove_directories(temp_candidates, settings.dry_run)
    incoming_removed, incoming_freed = _remove_directories(incoming_candidates, settings.dry_run)

    logging.info(
        "Cleanup complete: temp_removed=%s incoming_removed=%s freed_bytes=%s",
        temp_removed,
        incoming_removed,
        temp_freed + incoming_freed,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
