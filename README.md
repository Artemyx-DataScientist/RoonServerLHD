# Roon Server Library Helper

FastAPI + worker skeleton for managing media-processing tasks. The service uses SQLite storage, environment/config-based settings, and simple stubs for task processing.

## Features
- FastAPI service with task CRUD stubs and settings endpoints.
- SQLite schema (`tasks`, `task_files`, `known_hashes`, `task_events`) initialized automatically.
- Task status state machine with guarded transitions.
- HTML page for creating tasks and viewing their status.
- Worker stub process kept alive for system integration.
- Configurable through environment variables and optional YAML/JSON config file (environment wins).
- Systemd unit samples for API and worker services.
- Pre-commit helper to ensure `docs/` files are valid UTF-8 without NUL bytes.

## Requirements
- Python 3.11+
- Recommended: create a virtual environment before installing dependencies

Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration
Configuration is loaded from environment variables with optional overrides from `config.yaml` (or any path set in `CONFIG_FILE`). Environment variables always take precedence. The `music_root` directory **must already exist**; the application refuses to create it.

| Environment variable | Description | Default |
| --- | --- | --- |
| `MUSIC_ROOT` | **Required unless set in config file.** Path to existing music root. | n/a |
| `INCOMING_SUBDIR` | Destination subdirectory under `music_root`. | `Incoming` |
| `TEMP_SUBDIR` | Temporary working subdirectory under `music_root`. | `.temp` |
| `ALLOWLIST` | Comma-separated allowlist (extensions). | empty list |
| `CLEANUP_DAYS` | Days until cleanup marker. | `30` |
| `MOUNT_VALIDATION_MODE` | `strict` or `relaxed`. | `strict` |
| `CONFIG_FILE` | Optional path to YAML/JSON config. | `./config.yaml` if present |

Config file keys mirror the environment variables. Example `config.example.yaml` is included.

## Running the API locally
1. Ensure `MUSIC_ROOT` points to an existing directory (no automatic creation).
2. Initialize environment and install deps.
3. Start uvicorn:
   ```bash
   MUSIC_ROOT=/mnt/music uvicorn app.main:app --reload
   ```
4. Open http://127.0.0.1:8000/ to use the HTML form. API endpoints live under `/api/...`.

## Worker stub
The worker keeps a process alive for system integration:
```bash
MUSIC_ROOT=/mnt/music python -m worker.main
```

## Systemd units
Sample units live in `scripts/systemd/`:
- `roon-api.service` starts `uvicorn app.main:app` with `WorkingDirectory=%h/RoonServerLHD`.
- `roon-worker.service` starts the worker stub.

Install example (adjust the working directory to your checkout path):
```bash
sudo install -m 644 scripts/systemd/roon-api.service /etc/systemd/system/
sudo install -m 644 scripts/systemd/roon-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now roon-api.service roon-worker.service
```
Use `/etc/roonhelper.env` to provide environment overrides for both units.

## Database schema
SQLite tables initialized on startup:
- `tasks`: id, name, status, created_at, updated_at, cleanup_after
- `task_files`: id, task_id, file_name, file_hash, size_bytes, created_at
- `known_hashes`: id, file_hash (unique), first_seen_task_id, created_at
- `task_events`: id, task_id, event, created_at

## Pre-commit check for docs
Run the UTF-8/NUL check before committing documentation changes:
```bash
scripts/pre_commit_doc_check.sh
```

## Definition of done (local)
- `uvicorn app.main:app` starts successfully.
- Task endpoints create/read tasks in SQLite.
- Settings endpoint reflects configuration and updates in-memory config.
- Music root validation fails fast if the path does not exist.
