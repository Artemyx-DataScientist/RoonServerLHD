from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(Exception):
    """Raised when configuration values are invalid."""


def _load_mapping_from_file(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    if config_path.suffix.lower() in {".yaml", ".yml"}:
        loader = yaml.safe_load
    elif config_path.suffix.lower() == ".json":
        loader = json.load
    else:
        raise ConfigError("Unsupported config format. Use JSON or YAML.")

    with config_path.open("r", encoding="utf-8") as handle:
        data = loader(handle)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a mapping at the top level.")
    return data


@dataclass
class AppConfig:
    music_root: Path
    incoming_subdir: str
    temp_subdir: str
    max_task_size_bytes: int = 10 * 1024 * 1024 * 1024
    max_chunk_bytes: int = 5 * 1024 * 1024
    allowlist: List[str] = field(default_factory=list)
    cleanup_days: int = 30
    mount_validation_mode: str = "strict"
    db_path: Path = Path("storage/app.db")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "music_root": str(self.music_root),
            "incoming_subdir": self.incoming_subdir,
            "temp_subdir": self.temp_subdir,
            "max_task_size_bytes": self.max_task_size_bytes,
            "max_chunk_bytes": self.max_chunk_bytes,
            "allowlist": self.allowlist,
            "cleanup_days": self.cleanup_days,
            "mount_validation_mode": self.mount_validation_mode,
            "db_path": str(self.db_path),
        }


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name.upper(), default)


def _coerce_allowlist(value: Optional[Any]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _coerce_cleanup_days(value: Optional[Any]) -> int:
    if value is None or value == "":
        return 30
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("cleanup_days must be an integer") from exc


def _coerce_positive_int(value: Optional[Any], name: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive")
    return parsed


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    env_config_path = _get_env("CONFIG_FILE")
    resolved_config_path = Path(env_config_path) if env_config_path else config_path

    file_values: Dict[str, Any] = {}
    if resolved_config_path:
        file_values = _load_mapping_from_file(resolved_config_path)

    music_root_value = _get_env("MUSIC_ROOT", file_values.get("music_root"))
    if not music_root_value:
        raise ConfigError("music_root must be set via MUSIC_ROOT or config file")
    music_root = Path(music_root_value)
    if not music_root.exists():
        raise ConfigError(f"music_root is not accessible: {music_root}")
    if not music_root.is_dir():
        raise ConfigError("music_root must point to a directory")

    incoming_subdir = _get_env("INCOMING_SUBDIR", file_values.get("incoming_subdir", "Incoming"))
    temp_subdir = _get_env("TEMP_SUBDIR", file_values.get("temp_subdir", ".roon_uploader_tmp"))
    allowlist = _coerce_allowlist(_get_env("ALLOWLIST", file_values.get("allowlist")))
    cleanup_days = _coerce_cleanup_days(_get_env("CLEANUP_DAYS", file_values.get("cleanup_days")))
    mount_validation_mode = _get_env("MOUNT_VALIDATION_MODE", file_values.get("mount_validation_mode", "strict"))
    max_task_size_bytes = _coerce_positive_int(
        _get_env("MAX_TASK_SIZE_BYTES", file_values.get("max_task_size_bytes")),
        "max_task_size_bytes",
        AppConfig.max_task_size_bytes,
    )
    max_chunk_bytes = _coerce_positive_int(
        _get_env("MAX_CHUNK_BYTES", file_values.get("max_chunk_bytes")),
        "max_chunk_bytes",
        AppConfig.max_chunk_bytes,
    )

    if mount_validation_mode not in {"strict", "relaxed"}:
        raise ConfigError("mount_validation_mode must be either 'strict' or 'relaxed'")

    db_path_value = file_values.get("db_path")
    db_path = Path(db_path_value) if db_path_value else AppConfig.db_path

    return AppConfig(
        music_root=music_root,
        incoming_subdir=str(incoming_subdir),
        temp_subdir=str(temp_subdir),
        max_task_size_bytes=max_task_size_bytes,
        max_chunk_bytes=max_chunk_bytes,
        allowlist=allowlist,
        cleanup_days=cleanup_days,
        mount_validation_mode=str(mount_validation_mode),
        db_path=db_path,
    )


def update_config(base_config: AppConfig, updates: Dict[str, Any]) -> AppConfig:
    merged = base_config.as_dict()
    merged.update({key: value for key, value in updates.items() if value is not None})
    updated_config = load_config_from_mapping(merged)
    return updated_config


def load_config_from_mapping(values: Dict[str, Any]) -> AppConfig:
    music_root_value = values.get("music_root")
    if not music_root_value:
        raise ConfigError("music_root must be provided")
    music_root = Path(music_root_value)
    if not music_root.exists():
        raise ConfigError(f"music_root is not accessible: {music_root}")
    if not music_root.is_dir():
        raise ConfigError("music_root must point to a directory")

    mount_validation_mode = values.get("mount_validation_mode", "strict")
    if mount_validation_mode not in {"strict", "relaxed"}:
        raise ConfigError("mount_validation_mode must be either 'strict' or 'relaxed'")

    return AppConfig(
        music_root=music_root,
        incoming_subdir=str(values.get("incoming_subdir", "Incoming")),
        temp_subdir=str(values.get("temp_subdir", ".roon_uploader_tmp")),
        max_task_size_bytes=_coerce_positive_int(
            values.get("max_task_size_bytes"),
            "max_task_size_bytes",
            AppConfig.max_task_size_bytes,
        ),
        max_chunk_bytes=_coerce_positive_int(
            values.get("max_chunk_bytes"),
            "max_chunk_bytes",
            AppConfig.max_chunk_bytes,
        ),
        allowlist=_coerce_allowlist(values.get("allowlist")),
        cleanup_days=_coerce_cleanup_days(values.get("cleanup_days")),
        mount_validation_mode=mount_validation_mode,
        db_path=Path(values.get("db_path", "storage/app.db")),
    )
