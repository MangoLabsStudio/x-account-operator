from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "configs" / "ai_radar_demo.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
DATA_DIR = Path(os.getenv("AI_RADAR_DATA_DIR", ROOT / "data" / "ai_radar_demo"))
OUTPUT_DIR = DATA_DIR / "output"
INTERVAL_SECONDS = int(os.getenv("AI_RADAR_INTERVAL_MINUTES", CONFIG.get("schedule_minutes", 120))) * 60
STATE = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_run_ok": None,
    "last_result": None,
}


def run_radar() -> None:
    STATE["running"] = True
    STATE["last_started_at"] = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_ai_radar_demo.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    STATE["last_result"] = None
    if result.returncode == 0 and result.stdout.strip():
        try:
            STATE["last_result"] = json.loads(result.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            pass
    STATE["last_run_ok"] = result.returncode == 0
    STATE["last_finished_at"] = datetime.now(timezone.utc).isoformat()
    STATE["running"] = False


def scheduler(stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        run_radar()
        stop.wait(max(0, INTERVAL_SECONDS - (time.monotonic() - started)))


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = threading.Event()
    thread = None
    if os.getenv("AI_RADAR_SCHEDULER_ENABLED", "true").lower() == "true":
        thread = threading.Thread(target=scheduler, args=(stop,), daemon=True)
        thread.start()
    yield
    stop.set()
    if thread:
        thread.join(timeout=5)


app = FastAPI(title="AI Radar Demo", lifespan=lifespan)


def draft_count() -> int:
    path = OUTPUT_DIR / "drafts.json"
    if not path.exists():
        return 0
    return int(json.loads(path.read_text(encoding="utf-8")).get("draft_count", 0))


def persona_profiles() -> list[dict[str, object]]:
    focus_titles = {item["id"]: item.get("title", item["id"]) for item in CONFIG.get("focuses", [])}
    return [
        {
            "id": account["slug"],
            "name": account.get("name", account["slug"]),
            "profile_handle": account.get("profile_handle", ""),
            "profile_handle_is_internal_only": bool(account.get("profile_handle_is_internal_only", True)),
            "bio": account.get("bio", ""),
            "focus": account.get("focus", ""),
            "focus_title": focus_titles.get(account.get("focus"), account.get("focus", "")),
        }
        for account in CONFIG.get("output_accounts", [])
    ]


@app.get("/health")
def health():
    return {"status": "ok", "draft_count": draft_count(), **STATE}


@app.get("/api/drafts")
def drafts():
    profiles = persona_profiles()
    profiles_by_focus = {str(item["focus"]): item for item in profiles}
    path = OUTPUT_DIR / "drafts.json"
    if not path.exists():
        return {"generated_at": None, "draft_count": 0, "personas": profiles, "drafts": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    fields = (
        "output_account_name", "focus", "draft_zh", "original_post_url",
        "verification_status", "review_status", "created_at",
    )
    draft_items = []
    for item in payload.get("drafts", []):
        draft = {key: item.get(key) for key in fields}
        draft["persona"] = profiles_by_focus.get(str(item.get("focus") or ""))
        draft_items.append(draft)
    counts = {str(item["id"]): 0 for item in profiles}
    for item in draft_items:
        if item["persona"]:
            counts[str(item["persona"]["id"])] += 1
    for item in profiles:
        item["draft_count"] = counts[str(item["id"])]
    return JSONResponse({
        "generated_at": payload.get("generated_at"),
        "draft_count": payload.get("draft_count", 0),
        "personas": profiles, "drafts": draft_items,
    })


@app.get("/", response_class=HTMLResponse)
@app.get("/drafts", response_class=HTMLResponse)
def draft_page():
    path = OUTPUT_DIR / "drafts.html"
    if path.exists():
        return FileResponse(path)
    return HTMLResponse("<h1>AI Radar 正在生成第一批草稿</h1>", status_code=202)
