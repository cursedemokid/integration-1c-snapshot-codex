#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
HOST="${HOST:-service.example.invalid}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    printf 'Python executable not found: %s\n' "$PYTHON_BIN" >&2
    printf 'Create the virtual environment and install requirements first.\n' >&2
    exit 1
fi

exec "$PYTHON_BIN" -m uvicorn main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$WORKERS"
