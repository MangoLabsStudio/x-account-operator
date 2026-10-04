#!/usr/bin/env sh
set -eu

mkdir -p "${AI_RADAR_DATA_DIR:-/data/ai_radar_demo}"
exec uvicorn ai_radar_app:app --host 0.0.0.0 --port "$PORT"
