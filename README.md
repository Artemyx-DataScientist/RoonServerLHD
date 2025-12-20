# Roon Server Library Helper

This project provides a small set of utilities for preparing uploads to a Roon music library. The `main.py` entrypoint reads configuration from environment variables or a JSON/YAML config file, validates that the target music library is available, and computes a destination folder for new uploads. Supporting scripts help with repository hygiene.

## Requirements
- Python 3.11+
- Optional: [PyYAML](https://pyyaml.org/) if you want to use YAML configuration files. JSON works out of the box.

## Configuration
Configuration can be supplied through environment variables, a config file, or a mix of both. Values read from the environment take precedence over the config file.

### Environment variables
- `MUSIC_ROOT` (required if not set in the config file): Path to the root of the music library.
- `INCOMING_SUBDIR`: Name of the subdirectory under `MUSIC_ROOT` that will receive uploads. Defaults to `Incoming`.
- `TEMP_SUBDIR`: Name of the temporary working directory under `MUSIC_ROOT`. Defaults to `.roon_uploader_tmp`.
- `ALLOWLIST_EXTENSIONS`: Comma-separated list of allowed file extensions. If omitted, a sensible default list of audio and document formats is used.
- `MOUNT_MODE`: Validation strictness for the music root mount. Accepts `strict` (default) or `relaxed`.
- `CONFIG_FILE`: Optional path to a JSON or YAML config file. If omitted, the app will look for `config.yaml` in the current working directory.

### Config file
If present, the config file is loaded before environment overrides. Only mappings are accepted; other top-level structures raise a `ConfigFileError`. Supported file types are YAML (`.yml`/`.yaml`) and JSON (`.json`). Example `config.yaml`:

```yaml
music_root: /mnt/music
incoming_subdir: Incoming
temp_subdir: .roon_uploader_tmp
allowlist:
  - flac
  - mp3
mount_mode: strict
```

### Mount validation
By default the application enforces that `music_root` resides on a dedicated mount. If `MOUNT_MODE` (or `mount_mode` in the config file) is set to `relaxed`, the check is skipped.

## Usage
1. Set the relevant environment variables (or create a config file).
2. Run the entrypoint:
   ```bash
   python main.py
   ```
3. The script ensures the incoming and temporary directories exist, then prints the resolved upload destination in the format `MUSIC_ROOT/Incoming/YYYY-MM-DD/upload-HHMMSS` (unless you customize the subfolder names).

## Supporting script: `scripts/check_no_nulls.py`
This helper scans provided files to ensure they contain valid UTF-8 text, no NUL bytes, and an acceptable ratio of non-printable characters. It exits with an error if any files fail the checks, making it suitable for use in pre-commit hooks.

Example invocation:
```bash
python scripts/check_no_nulls.py docs/README.md --nonprintable-threshold 0.02
```
