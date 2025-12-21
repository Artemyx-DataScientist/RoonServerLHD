# Roon Server Library Helper

FastAPI + worker skeleton for managing media-processing tasks. The service uses SQLite storage, environment/config-based settings, and simple stubs for task processing.

## Features
- FastAPI service with task CRUD and resumable uploads (chunked, resumable via offsets).
- SQLite schema (`tasks`, `task_files`, `known_hashes`, `task_events`) initialized automatically.
- Task status state machine with guarded transitions.
- HTML page for creating tasks, uploading files with progress, and viewing their status.
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
| `TEMP_SUBDIR` | Temporary working subdirectory under `music_root`. | `.roon_uploader_tmp` |
| `MAX_TASK_SIZE_BYTES` | Maximum total registered bytes per task. | `10737418240` (10 GiB) |
| `MAX_CHUNK_BYTES` | Maximum accepted chunk size. | `5242880` (5 MiB) |
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
   - The HTML page supports selecting multiple files (or a directory) and uploads in chunks with progress.

## Worker stub
The worker keeps a process alive for system integration:
```bash
MUSIC_ROOT=/mnt/music python -m worker.main
```

## Deployment layout
The production layout is designed for atomic releases and easy rollback:

```
/opt/roonhelper/
  ├── releases/
  │    ├── 2025-01-01_1200/
  │    └── ...
  ├── current -> releases/2025-01-01_1200
  ├── shared/
  │    ├── config.yaml
  │    └── storage/
  └── venv/
```

- `releases/` holds timestamped checkouts (no in-place edits).
- `current` is a symlink to the active release. Switching the symlink is atomic and does not affect already running processes.
- `shared/` contains configuration and persistent data (e.g., `storage/app.db`) that must survive restarts and upgrades.
- `venv/` hosts the Python virtual environment shared by both services.

## Preparing the host
1. Create a dedicated system user and prepare paths:
   ```bash
   sudo useradd --system --home /opt/roonhelper --shell /usr/sbin/nologin roonhelper
   sudo mkdir -p /opt/roonhelper/releases /opt/roonhelper/shared/storage
   sudo chown -R roonhelper:roonhelper /opt/roonhelper
   sudo python3 -m venv /opt/roonhelper/venv
   sudo /opt/roonhelper/venv/bin/pip install --upgrade pip
   ```
2. Populate `/opt/roonhelper/shared/config.yaml` (copy `config.example.yaml` as a starting point) and point `db_path` to `/opt/roonhelper/shared/storage/app.db`.
3. Copy `scripts/roonhelper.env.example` to `/etc/roonhelper/roonhelper.env` and update values (`MUSIC_ROOT`, `CONFIG_FILE=/opt/roonhelper/shared/config.yaml`, `DB_PATH=/opt/roonhelper/shared/storage/app.db`, etc.).

## systemd units
Sample units live in `scripts/systemd/`:
- `roon-uploader-api.service` runs `uvicorn app.main:app`.
- `roon-uploader-worker.service` runs the worker and waits for the API to be up.

Install and enable the services:
```bash
sudo install -m 644 scripts/systemd/roon-uploader-api.service /etc/systemd/system/
sudo install -m 644 scripts/systemd/roon-uploader-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now roon-uploader-api.service roon-uploader-worker.service
```

The worker waits for the API port (127.0.0.1:8000) before starting; ensure `nc` (netcat) is installed (e.g., `sudo apt-get install netcat-openbsd`).

Both units load `/etc/roonhelper/roonhelper.env` for environment overrides and log to journald. Check status/logs with:
```bash
systemctl status roon-uploader-api.service roon-uploader-worker.service
journalctl -u roon-uploader-api.service -u roon-uploader-worker.service -f
```

## Updating without downtime
1. Prepare a new release directory:
   ```bash
   ts=$(date +%Y-%m-%d_%H%M)
   REPO_URL=<git URL for this project>
   sudo git clone "$REPO_URL" /opt/roonhelper/releases/$ts
   sudo /opt/roonhelper/venv/bin/pip install -r /opt/roonhelper/releases/$ts/requirements.txt
   ```
2. Point `current` to the new release atomically:
   ```bash
   sudo ln -sfn /opt/roonhelper/releases/$ts /opt/roonhelper/current
   ```
3. Restart services to pick up the new code (running processes continue using the old release until restart):
   ```bash
   sudo systemctl restart roon-uploader-api.service roon-uploader-worker.service
   ```

## Rollback
1. List available releases under `/opt/roonhelper/releases/` and pick a known-good timestamp.
2. Point `current` back to that directory:
   ```bash
   sudo ln -sfn /opt/roonhelper/releases/2025-01-01_1200 /opt/roonhelper/current
   sudo systemctl restart roon-uploader-api.service roon-uploader-worker.service
   ```

Because data and config live under `/opt/roonhelper/shared/`, switching releases and restarting does not drop active tasks or database state.

## Database schema
SQLite tables initialized on startup:
- `tasks`: id, name, status, created_at, updated_at, cleanup_after
- `task_files`: id, task_id, relative_path, original_name, expected_size, uploaded_bytes, finalized, size_bytes, created_at, updated_at
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
