from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    NEW = "NEW"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_ALLOWED_TRANSITIONS: Dict[TaskStatus, List[TaskStatus]] = {
    TaskStatus.NEW: [TaskStatus.PROCESSING, TaskStatus.CANCELLED],
    TaskStatus.PROCESSING: [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
    TaskStatus.COMPLETED: [],
    TaskStatus.FAILED: [],
    TaskStatus.CANCELLED: [],
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target == current:
        return
    allowed_targets = _ALLOWED_TRANSITIONS.get(current, [])
    if target not in allowed_targets:
        raise ValueError(f"Invalid state transition from {current} to {target}")


@dataclass
class TaskRecord:
    id: int
    name: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    cleanup_after: Optional[datetime]


@dataclass
class TaskEventRecord:
    id: int
    task_id: int
    event: str
    created_at: datetime


class TaskCreateRequest(BaseModel):
    name: str = Field(..., example="Import classical collection")


class TaskResponse(BaseModel):
    id: int
    name: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    cleanup_after: Optional[datetime]


class TaskEventResponse(BaseModel):
    id: int
    task_id: int
    event: str
    created_at: datetime


class SettingsResponse(BaseModel):
    music_root: str
    incoming_subdir: str
    temp_subdir: str
    allowlist: List[str]
    cleanup_days: int
    mount_validation_mode: str
    db_path: str


class SettingsUpdateRequest(BaseModel):
    music_root: Optional[str] = None
    incoming_subdir: Optional[str] = None
    temp_subdir: Optional[str] = None
    allowlist: Optional[List[str]] = None
    cleanup_days: Optional[int] = None
    mount_validation_mode: Optional[str] = None
    db_path: Optional[str] = None

