from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

DEFAULT_ALLOWLIST: Set[str] = {
    "flac",
    "mp3",
    "m4a",
    "aac",
    "alac",
    "ogg",
    "opus",
    "wav",
    "aiff",
    "dsf",
    "dff",
    "ape",
    "wv",
    "cue",
    "m3u",
    "m3u8",
    "jpg",
    "jpeg",
    "png",
    "webp",
    "txt",
    "pdf",
}


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    NEED_PASSWORD = "NEED_PASSWORD"
    NEED_TAGS = "NEED_TAGS"
    DONE = "DONE"
    DONE_WITH_DUPLICATES = "DONE_WITH_DUPLICATES"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class ConfigFileError(RuntimeError):
    """Raised when a configuration file cannot be parsed or validated."""

    def __init__(self, path: Path, message: str):
        super().__init__(message)
        self.path: Path = path
        self.user_message: str = f"Failed to load config file '{path}': {message}"


@dataclass
class Settings:
    music_root: Path
    incoming_subdir: str = "Incoming"
    temp_subdir: str = ".roon_uploader_tmp"
    allowlist: Set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWLIST))

    mount_validation_mode: "MountValidationMode" = field(
        default_factory=lambda: MountValidationMode.STRICT
    )

    @classmethod
    def from_sources(
        cls,
        env: Optional[Mapping[str, str]] = None,
        config_path: Optional[Path] = None,
    ) -> "Settings":
        env_mapping: Mapping[str, str] = env or os.environ

        config_data: Dict[str, Any] = load_config_file(
            determine_config_path(config_path=config_path, env=env_mapping)
        )

        music_root_env: Optional[str] = env_mapping.get("MUSIC_ROOT")
        if music_root_env:
            music_root = Path(music_root_env)
        else:
            music_root_raw: Optional[str] = _config_value(config_data, "music_root")
            if not music_root_raw:
                raise ValueError(
                    "MUSIC_ROOT environment variable or music_root in config file is required to locate the library root."
                )
            music_root = Path(music_root_raw)

        incoming_subdir = env_mapping.get(
            "INCOMING_SUBDIR", _config_value(config_data, "incoming_subdir", "Incoming")
        )
        temp_subdir = env_mapping.get(
            "TEMP_SUBDIR", _config_value(config_data, "temp_subdir", ".roon_uploader_tmp")
        )

        allowlist_env: Optional[str] = env_mapping.get("ALLOWLIST_EXTENSIONS")
        allowlist_config: Optional[Any] = _config_value(config_data, "allowlist")
        allowlist = _parse_allowlist(allowlist_env, allowlist_config)

        mount_mode_raw: Optional[str] = env_mapping.get(
            "MOUNT_MODE", _config_value(config_data, "mount_mode")
        )
        mount_validation_mode = MountValidationMode.from_raw(mount_mode_raw)

        return cls(
            music_root=music_root.expanduser().resolve(),
            incoming_subdir=incoming_subdir,
            temp_subdir=temp_subdir,
            allowlist=allowlist,
            mount_validation_mode=mount_validation_mode,
        )

    @property
    def incoming_root(self) -> Path:
        return self.music_root / self.incoming_subdir

    @property
    def temp_root(self) -> Path:
        return self.music_root / self.temp_subdir


class MountValidationMode(str, Enum):
    STRICT = "strict"
    RELAXED = "relaxed"

    @classmethod
    def from_raw(cls, raw: Optional[str]) -> "MountValidationMode":
        if not raw:
            return cls.STRICT
        normalized = raw.strip().lower()
        if normalized == cls.RELAXED.value:
            return cls.RELAXED
        return cls.STRICT


def _parse_allowlist(env_raw: Optional[str], config_value: Optional[Any]) -> Set[str]:
    if env_raw:
        return {ext.strip().lower() for ext in env_raw.split(",") if ext.strip()}
    if isinstance(config_value, str):
        return {ext.strip().lower() for ext in config_value.split(",") if ext.strip()}
    if isinstance(config_value, Iterable):
        return {str(ext).strip().lower() for ext in config_value if str(ext).strip()}
    return set(DEFAULT_ALLOWLIST)


def _config_value(config: Mapping[str, Any], key: str, default: Optional[Any] = None) -> Optional[Any]:
    return config.get(key, default)


def determine_config_path(config_path: Optional[Path], env: Mapping[str, str]) -> Optional[Path]:
    if config_path:
        return config_path.expanduser().resolve()

    env_path: Optional[str] = env.get("CONFIG_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()

    default_candidate: Path = Path.cwd() / "config.yaml"
    return default_candidate if default_candidate.exists() else None


def load_config_file(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}

    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}

    suffix = resolved.suffix.lower()
    if suffix in {".yml", ".yaml"}:
        yaml_spec = importlib.util.find_spec("yaml")
        if yaml_spec is None:  # pragma: no cover - environment dependent
            raise ConfigFileError(
                resolved, "PyYAML is required to parse YAML configuration files."
            )
        yaml = importlib.import_module("yaml")  # type: ignore
        try:
            with resolved.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            raise ConfigFileError(resolved, f"Invalid YAML content: {exc}") from exc
    elif suffix == ".json":
        try:
            with resolved.open("r", encoding="utf-8") as handle:
                data = json.load(handle) or {}
        except json.JSONDecodeError as exc:
            raise ConfigFileError(resolved, f"Invalid JSON content: {exc}") from exc
    else:
        raise ConfigFileError(resolved, f"Unsupported config file extension: {suffix}")

    if not isinstance(data, dict):
        raise ConfigFileError(resolved, "Configuration file must contain a mapping at the root level.")
    return data


def resolve_mountpoint(path: Path) -> Path:
    resolved_path: Path = path.resolve()
    for candidate in (resolved_path,) + tuple(resolved_path.parents):
        if os.path.ismount(candidate):
            return candidate
    return resolved_path.anchor


def validate_music_root(settings: Settings) -> Path:
    if not settings.music_root.exists():
        raise FileNotFoundError(f"music_root is not available: {settings.music_root}")
    if not settings.music_root.is_dir():
        raise NotADirectoryError(f"music_root must be a directory: {settings.music_root}")
    if not os.access(settings.music_root, os.R_OK | os.W_OK):
        raise PermissionError(f"Insufficient permissions for music_root: {settings.music_root}")

    mount_point: Path = resolve_mountpoint(settings.music_root)
    if settings.mount_validation_mode == MountValidationMode.STRICT and mount_point == Path("/"):
        raise RuntimeError(
            "music_root does not appear to be on a dedicated mount; ensure the media drive is mounted correctly."
        )
    return mount_point


def ensure_working_directories(settings: Settings) -> None:
    validate_music_root(settings)

    for path in (settings.incoming_root, settings.temp_root):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.R_OK | os.W_OK):
            raise PermissionError(f"Insufficient permissions for path: {path}")


def destination_for_upload(settings: Settings, label: Optional[str] = None, when: Optional[datetime] = None) -> Path:
    current_time: datetime = when or datetime.now()
    folder_date: str = current_time.strftime("%Y-%m-%d")
    upload_label: str = label or current_time.strftime("upload-%H%M%S")
    return settings.incoming_root / folder_date / upload_label


def summarize_task_files(files: Iterable[Path], allowlist: Set[str]) -> Dict[str, List[Path]]:
    accepted: List[Path] = []
    skipped: List[Path] = []

    for file_path in files:
        extension: str = file_path.suffix.lower().lstrip(".")
        (accepted if extension in allowlist else skipped).append(file_path)

    return {"accepted": accepted, "skipped": skipped}


def main() -> None:
    try:
        settings = Settings.from_sources()
    except ConfigFileError as exc:
        raise SystemExit(exc.user_message) from exc

    ensure_working_directories(settings)

    destination = destination_for_upload(settings)
    print(f"Resolved destination: {destination}")


if __name__ == "__main__":
    main()
