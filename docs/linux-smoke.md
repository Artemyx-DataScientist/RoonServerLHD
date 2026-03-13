# Linux smoke flow

This project is intended to run on a Linux host with `systemd`. The steps below describe a short known-good validation path for a fresh checkout.

## 1. Prepare host prerequisites

Install Python 3.11+ and create a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

If you plan to process RAR archives, ensure `rarfile` can reach a compatible extractor from `PATH` on the host.

## 2. Prepare directories and config

Create a writable music root with incoming and temp directories:

```bash
mkdir -p /srv/roonhelper/music/Incoming
mkdir -p /srv/roonhelper/music/.roon_uploader_tmp
```

Create `/etc/roonhelper.env`:

```bash
MUSIC_ROOT=/srv/roonhelper/music
INCOMING_SUBDIR=Incoming
TEMP_SUBDIR=.roon_uploader_tmp
DB_PATH=/srv/roonhelper/state/app.db
MOUNT_VALIDATION_MODE=strict
```

## 3. Start API and worker manually

Run the API:

```bash
set -a
. /etc/roonhelper.env
set +a
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run the worker in a second shell:

```bash
set -a
. /etc/roonhelper.env
set +a
python -m worker.main
```

## 4. Validate happy path

Open `http://127.0.0.1:8000/`.

Create a task and upload a small directory of allowed audio files. The expected status flow is:

`CREATED -> UPLOADING -> READY_FOR_PROCESSING -> PROCESSING -> DONE`

Files should appear under:

```text
${MUSIC_ROOT}/Incoming/YYYY-MM-DD/<task_name>/
```

## 5. Validate password and tag-review flows

- Upload an encrypted archive and confirm the task moves to `NEED_PASSWORD`.
- Submit the password from the UI and confirm the worker resumes processing.
- Upload or extract an audio file with missing required tags and confirm the task moves to `NEED_TAGS`.
- Submit tags from the UI and confirm the task returns to `PROCESSING` and then completes.

## 6. Validate duplicate and skipped file reporting

- Re-upload a file that already exists by content and confirm the task ends in `DONE_WITH_DUPLICATES`.
- Check the task table in the UI and confirm duplicate or skipped reasons are displayed.

## 7. Validate cleanup timer

Install the sample units from `scripts/systemd/` and enable the cleanup timer:

```bash
sudo install -m 644 scripts/systemd/roon-api.service /etc/systemd/system/
sudo install -m 644 scripts/systemd/roon-worker.service /etc/systemd/system/
sudo install -m 644 scripts/systemd/roon-uploader-cleanup.service /etc/systemd/system/
sudo install -m 644 scripts/systemd/roon-uploader-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now roon-api.service roon-worker.service roon-uploader-cleanup.timer
```

After installation, confirm:

- `roon-api.service` is active
- `roon-worker.service` is active
- `roon-uploader-cleanup.timer` is active and has a next run scheduled
