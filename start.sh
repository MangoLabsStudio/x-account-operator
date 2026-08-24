#!/usr/bin/env sh
set -eu

mkdir -p "$XOPS_DATA_DIR"
if [ ! -f "$XOPS_DATA_DIR/xops.db" ] && [ -f seed_data/xops.db ]; then
  cp seed_data/xops.db "$XOPS_DATA_DIR/xops.db"
fi

exec uvicorn app:app --host 0.0.0.0 --port "$PORT"
