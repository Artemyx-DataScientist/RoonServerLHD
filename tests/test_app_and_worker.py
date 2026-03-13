from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models import TaskStatus
from storage.db import Database
from worker.main import PasswordRequiredError, _collect_extracted_outcomes, _process_task


class FakeMutagenAudio:
    def __init__(self, tags: dict[str, list[str]] | None = None) -> None:
        self.tags = tags or {}

    def __setitem__(self, key: str, value: list[str]) -> None:
        self.tags[key] = value

    def save(self) -> None:
        return None


class AppAndWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir()
        (self.music_root / "Incoming").mkdir()
        (self.music_root / ".roon_uploader_tmp").mkdir()
        self.db_path = self.root / "storage" / "app.db"

        self._env_backup = {key: os.environ.get(key) for key in ("MUSIC_ROOT", "DB_PATH", "CONFIG_FILE")}
        os.environ["MUSIC_ROOT"] = str(self.music_root)
        os.environ["DB_PATH"] = str(self.db_path)
        os.environ.pop("CONFIG_FILE", None)

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tempdir.cleanup()

    def test_settings_roundtrip_updates_runtime_contract(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/settings")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("max_task_size_bytes", payload)
            self.assertIn("max_chunk_bytes", payload)

            new_db_path = self.root / "storage" / "runtime.db"
            update_response = client.post(
                "/api/settings",
                json={
                    "max_task_size_bytes": 4096,
                    "max_chunk_bytes": 1024,
                    "db_path": str(new_db_path),
                },
            )
            self.assertEqual(update_response.status_code, 200)
            updated = update_response.json()
            self.assertEqual(updated["max_task_size_bytes"], 4096)
            self.assertEqual(updated["max_chunk_bytes"], 1024)
            self.assertEqual(updated["db_path"], str(new_db_path))
            self.assertEqual(app.state.db.db_path, new_db_path)

    def test_duplicate_relative_path_returns_conflict(self) -> None:
        with TestClient(app) as client:
            task_response = client.post("/api/tasks", json={"name": "duplicates"})
            self.assertEqual(task_response.status_code, 200)
            task_id = task_response.json()["id"]

            first = client.post(
                f"/api/tasks/{task_id}/files",
                json={"relative_path": "album/song.flac", "size_bytes": 4, "original_name": "song.flac"},
            )
            self.assertEqual(first.status_code, 200)

            second = client.post(
                f"/api/tasks/{task_id}/files",
                json={"relative_path": "album/song.flac", "size_bytes": 4, "original_name": "song.flac"},
            )
            self.assertEqual(second.status_code, 409)
            self.assertIn("already registered", second.json()["detail"])

    def test_worker_requests_password_for_encrypted_archive(self) -> None:
        with TestClient(app):
            db = Database(self.db_path)
            task = db.create_task("archive", cleanup_days=30)
            task = db.update_status(task.id, TaskStatus.UPLOADING)
            assert task is not None

            record = db.create_task_file(task.id, "secret.zip", "secret.zip", 10)
            archive_path = self.music_root / ".roon_uploader_tmp" / str(task.id) / "secret.zip"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(b"placeholder")
            db.finalize_task_file(record.id, archive_path.stat().st_size)
            task = db.update_status(task.id, TaskStatus.READY_FOR_PROCESSING)
            assert task is not None
            task = db.update_status(task.id, TaskStatus.PROCESSING)
            assert task is not None

            class FakeZipInfo:
                filename = "song.flac"
                file_size = 10
                flag_bits = 0x1

                @staticmethod
                def is_dir() -> bool:
                    return False

            class FakeZipFile:
                def __enter__(self) -> "FakeZipFile":
                    return self

                def __exit__(self, exc_type, exc, tb) -> None:
                    return None

                @staticmethod
                def infolist() -> list[FakeZipInfo]:
                    return [FakeZipInfo()]

            with patch("worker.main.zipfile.ZipFile", return_value=FakeZipFile()):
                with self.assertRaises(PasswordRequiredError):
                    _collect_extracted_outcomes(db, app.state.config, task, record, [], password=None)

            updated_task = db.get_task(task.id)
            assert updated_task is not None
            self.assertEqual(updated_task.status, TaskStatus.NEED_PASSWORD)
            self.assertEqual(updated_task.context["password_for"], "secret.zip")

    def test_worker_transitions_to_need_tags_and_api_accepts_updates(self) -> None:
        with TestClient(app) as client:
            task_response = client.post("/api/tasks", json={"name": "tag-review"})
            self.assertEqual(task_response.status_code, 200)
            task_id = task_response.json()["id"]

            register_response = client.post(
                f"/api/tasks/{task_id}/files",
                json={"relative_path": "album/song.mp3", "size_bytes": 4, "original_name": "song.mp3"},
            )
            self.assertEqual(register_response.status_code, 200)
            file_id = register_response.json()["file_id"]

            chunk_response = client.post(
                f"/api/tasks/{task_id}/files/{file_id}/chunks",
                headers={"Content-Range": "bytes 0-3/4"},
                content=b"data",
            )
            self.assertEqual(chunk_response.status_code, 200)

            finalize_response = client.post(f"/api/tasks/{task_id}/files/{file_id}/finalize")
            self.assertEqual(finalize_response.status_code, 200)
            self.assertEqual(finalize_response.json()["status"], TaskStatus.READY_FOR_PROCESSING.value)

            db: Database = app.state.db
            task = db.update_status(task_id, TaskStatus.PROCESSING)
            assert task is not None

            with patch("worker.main.MutagenFile", return_value=FakeMutagenAudio()):
                _process_task(db, app.state.config, task)

            task_after_worker = db.get_task(task_id)
            assert task_after_worker is not None
            self.assertEqual(task_after_worker.status, TaskStatus.NEED_TAGS)
            self.assertEqual(len(task_after_worker.context["pending_tags"]), 1)

            with patch("app.main.MutagenFile", return_value=FakeMutagenAudio()):
                tags_response = client.get(f"/api/tasks/{task_id}/tags")
                self.assertEqual(tags_response.status_code, 200)
                tags_payload = tags_response.json()
                self.assertEqual(tags_payload["tracks"][0]["path"], "album/song.mp3")

                submit_response = client.post(
                    f"/api/tasks/{task_id}/tags",
                    json={
                        "tracks": [
                            {
                                "path": "album/song.mp3",
                                "title": "Song",
                                "artist": "Artist",
                                "album": "Album",
                                "year": "2024",
                            }
                        ]
                    },
                )
                self.assertEqual(submit_response.status_code, 200)
                self.assertEqual(submit_response.json()["status"], TaskStatus.PROCESSING.value)

            task_after_submit = db.get_task(task_id)
            assert task_after_submit is not None
            self.assertNotIn("pending_tags", task_after_submit.context)


if __name__ == "__main__":
    unittest.main()
