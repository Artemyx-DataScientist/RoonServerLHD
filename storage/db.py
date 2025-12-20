from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from app.models import (
    TaskEventRecord,
    TaskFileRecord,
    TaskRecord,
    TaskStatus,
    validate_transition,
)


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cleanup_after TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    relative_path TEXT NOT NULL,
                    original_name TEXT,
                    file_hash TEXT,
                    expected_size INTEGER NOT NULL DEFAULT 0,
                    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                    finalized INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(cursor, "task_files", "relative_path", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(cursor, "task_files", "original_name", "TEXT")
            self._ensure_column(cursor, "task_files", "expected_size", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "task_files", "uploaded_bytes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "task_files", "finalized", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "task_files", "size_bytes", "INTEGER")
            self._ensure_column(cursor, "task_files", "updated_at", "TEXT NOT NULL DEFAULT ''")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS known_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT NOT NULL UNIQUE,
                    first_seen_task_id INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    event TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _ensure_column(self, cursor: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        cleanup_after = (
            datetime.fromisoformat(row["cleanup_after"]).replace(tzinfo=timezone.utc)
            if row["cleanup_after"]
            else None
        )
        return TaskRecord(
            id=row["id"],
            name=row["name"],
            status=TaskStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc),
            updated_at=datetime.fromisoformat(row["updated_at"]).replace(tzinfo=timezone.utc),
            cleanup_after=cleanup_after,
        )

    def _row_to_event(self, row: sqlite3.Row) -> TaskEventRecord:
        return TaskEventRecord(
            id=row["id"],
            task_id=row["task_id"],
            event=row["event"],
            created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc),
        )

    def _row_to_task_file(self, row: sqlite3.Row) -> TaskFileRecord:
        updated_at_value = row["updated_at"] if row["updated_at"] else row["created_at"]
        return TaskFileRecord(
            id=row["id"],
            task_id=row["task_id"],
            relative_path=row["relative_path"],
            original_name=row["original_name"] or row["relative_path"],
            expected_size=row["expected_size"] or 0,
            uploaded_bytes=row["uploaded_bytes"] or 0,
            finalized=bool(row["finalized"]),
            created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc),
            updated_at=datetime.fromisoformat(updated_at_value).replace(tzinfo=timezone.utc),
            size_bytes=row["size_bytes"],
        )

    def create_task(self, name: str, cleanup_days: int) -> TaskRecord:
        now = datetime.now(timezone.utc)
        cleanup_after = now + timedelta(days=cleanup_days)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (name, status, created_at, updated_at, cleanup_after)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, TaskStatus.CREATED.value, now.isoformat(), now.isoformat(), cleanup_after.isoformat()),
            )
            task_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO task_events (task_id, event, created_at)
                VALUES (?, ?, ?)
                """,
                (task_id, "created", now.isoformat()),
            )
            conn.commit()
            return self.get_task(task_id)

    def get_task(self, task_id: int) -> Optional[TaskRecord]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def list_tasks(self) -> List[TaskRecord]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [self._row_to_task(row) for row in rows]

    def add_event(self, task_id: int, event: str, at: Optional[datetime] = None) -> TaskEventRecord:
        timestamp = at or datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
                (task_id, event, timestamp.isoformat()),
            )
            conn.commit()
            cursor.execute("SELECT * FROM task_events WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            assert row is not None
            return self._row_to_event(row)

    def list_events(self, task_id: int) -> List[TaskEventRecord]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            )
            rows = cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    def update_status(self, task_id: int, new_status: TaskStatus) -> Optional[TaskRecord]:
        task = self.get_task(task_id)
        if not task:
            return None
        validate_transition(task.status, new_status)
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?
                """,
                (new_status.value, now.isoformat(), task_id),
            )
            cursor.execute(
                "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
                (task_id, new_status.value.lower(), now.isoformat()),
            )
            conn.commit()
        return self.get_task(task_id)

    def add_known_hash(self, file_hash: str, task_id: Optional[int] = None) -> None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO known_hashes (file_hash, first_seen_task_id, created_at)
                VALUES (?, ?, ?)
                """,
                (file_hash, task_id, now.isoformat()),
            )
            conn.commit()

    def create_task_file(
        self,
        task_id: int,
        relative_path: str,
        original_name: str,
        expected_size: int,
    ) -> TaskFileRecord:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO task_files (
                    task_id, relative_path, original_name, expected_size, uploaded_bytes,
                    finalized, size_bytes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 0, 0, NULL, ?, ?)
                """,
                (
                    task_id,
                    relative_path,
                    original_name,
                    expected_size,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            conn.commit()
            cursor.execute("SELECT * FROM task_files WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            assert row is not None
            return self._row_to_task_file(row)

    def get_task_file(self, file_id: int) -> Optional[TaskFileRecord]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM task_files WHERE id = ?", (file_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task_file(row)

    def list_task_files(self, task_id: int) -> List[TaskFileRecord]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM task_files WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            )
            return [self._row_to_task_file(row) for row in cursor.fetchall()]

    def update_task_file_progress(self, file_id: int, uploaded_bytes: int) -> Optional[TaskFileRecord]:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE task_files SET uploaded_bytes = ?, updated_at = ? WHERE id = ?",
                (uploaded_bytes, now.isoformat(), file_id),
            )
            conn.commit()
        return self.get_task_file(file_id)

    def finalize_task_file(self, file_id: int, size_bytes: int) -> Optional[TaskFileRecord]:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE task_files
                SET finalized = 1, size_bytes = ?, updated_at = ?, uploaded_bytes = ?
                WHERE id = ?
                """,
                (size_bytes, now.isoformat(), size_bytes, file_id),
            )
            conn.commit()
        return self.get_task_file(file_id)

    def total_expected_size_for_task(self, task_id: int) -> int:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT SUM(expected_size) as total FROM task_files WHERE task_id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def all_files_finalized(self, task_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM task_files WHERE task_id = ? AND finalized = 0",
                (task_id,),
            )
            row = cursor.fetchone()
            pending = int(row[0]) if row else 0
            return pending == 0

