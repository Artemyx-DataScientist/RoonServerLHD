#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOC_DIR="$REPO_ROOT/docs"

if [ ! -d "$DOC_DIR" ]; then
  echo "docs directory not found at $DOC_DIR" >&2
  exit 1
fi

mapfile -t DOC_FILES < <(find "$DOC_DIR" -type f -print)

if [ ${#DOC_FILES[@]} -eq 0 ]; then
  echo "No documentation files found to check."
  exit 0
fi

python "$REPO_ROOT/scripts/check_no_nulls.py" "${DOC_FILES[@]}"
