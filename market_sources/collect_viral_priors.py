from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API = "https://api.apify.com/v2"
REDDIT_ACTOR = "clearpath~reddit-subreddit-posts-scraper"
DOUYIN_ACTOR = "bovi~douyin-scraper"
DOUYIN_SEARCH_ACTOR = "sian.agency~douyin-scraper"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
FINANCE_SUBREDDITS = [
    "investing", "stocks", "ValueInvesting", "wallstreetbets", "Daytrading",
    "options", "CryptoCurrency", "Bitcoin", "FinancialIndependence", "Entrepreneur",
]
US_LOCAL_TERMS = {
    "401k", "401(k)", "roth ira", "rollover ira", "simple ira", "medicare",
    "medicaid", "social security", "student loan", "hospital bill", "health insurance",
    "credit score", "tax deduction", "tax bracket", "property tax", "section 8", "cobra insurance",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apify_token() -> str:
    if os.getenv("APIFY_TOKEN", "").strip():
        return os.environ["APIFY_TOKEN"].strip()
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "codex.apify", "-a", "APIFY_TOKEN", "-w"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("未配置 APIFY_TOKEN") from None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("未配置 Apify Keychain 凭据")


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS apify_runs(
            input_key TEXT PRIMARY KEY, actor TEXT NOT NULL, input_json TEXT NOT NULL,
            run_id TEXT, dataset_id TEXT, status TEXT NOT NULL, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS viral_priors(
            platform TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, language TEXT NOT NULL DEFAULT '',
            community TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', metrics_json TEXT NOT NULL,
            raw_json TEXT NOT NULL, collected_at TEXT NOT NULL,
            PRIMARY KEY(platform, external_id)
        );
    """)
    db.commit()


def request(token: str, method: str, path: str, body: dict | None = None, timeout: int = 90):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def run_actor(
    db: sqlite3.Connection,
    token: str | None,
    actor: str,
    payload: dict,
    max_charge: float,
    cache_bucket: str = "",
) -> list[dict]:
    token = token or apify_token()
    input_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    input_key = hashlib.sha256(f"{actor}\n{cache_bucket}\n{input_json}".encode()).hexdigest()
    row = db.execute(
        "SELECT run_id,dataset_id,status FROM apify_runs WHERE input_key=?", (input_key,)
    ).fetchone()
    if row and row[2] == "SUBMITTING" and not row[0]:
        raise RuntimeError("上次付费请求提交结果不确定，拒绝盲目重试")
    if row and row[2] == "SUCCEEDED":
        return request(token, "GET", f"/datasets/{row[1]}/items?clean=true&format=json", timeout=120)
    if row and row[2] in {"FAILED", "ABORTED", "TIMED-OUT"}:
        raise RuntimeError(f"Apify run {row[0]} 已以 {row[2]} 结束，下个采集窗口再提交")

    run_id = row[0] if row else None
    if not run_id:
        stamp = now()
        db.execute(
            "INSERT OR REPLACE INTO apify_runs VALUES(?,?,?,?,?,?,?,?,?)",
            (input_key, actor, input_json, None, None, "SUBMITTING", None, stamp, stamp),
        )
        db.commit()
        query = urlencode({"maxTotalChargeUsd": max_charge})
        run = request(token, "POST", f"/actors/{actor}/runs?{query}", payload)["data"]
        run_id = run["id"]
        db.execute(
            "UPDATE apify_runs SET run_id=?,dataset_id=?,status=?,updated_at=? WHERE input_key=?",
            (run_id, run.get("defaultDatasetId"), run["status"], now(), input_key),
        )
        db.commit()

    deadline = monotonic() + max(
        60, int(os.getenv("XOPS_APIFY_POLL_TIMEOUT_SECONDS", "900"))
    )
    while monotonic() < deadline:
        run = request(token, "GET", f"/actor-runs/{run_id}", timeout=30)["data"]
        status = run["status"]
        db.execute(
            "UPDATE apify_runs SET dataset_id=?,status=?,error=?,updated_at=? WHERE input_key=?",
            (run.get("defaultDatasetId"), status, run.get("statusMessage"), now(), input_key),
        )
        db.commit()
        if status in TERMINAL:
            if status != "SUCCEEDED":
                raise RuntimeError(f"Apify run {run_id} ended with {status}: {run.get('statusMessage', '')}")
            return request(
                token, "GET", f"/datasets/{run['defaultDatasetId']}/items?clean=true&format=json", timeout=120
            )
        sleep(5)
    raise RuntimeError(f"Apify run {run_id} 仍在运行，稍后从同一 run 继续")


def integer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def ingest_reddit(db: sqlite3.Connection, items: list[dict]) -> int:
    count = 0
    for item in items:
        if item.get("_type") != "post" or item.get("stickied") or item.get("over_18"):
            continue
        external_id = str(item.get("id") or "").removeprefix("t3_")
        if not external_id:
            continue
        url = item.get("permalink") or item.get("full_link") or item.get("url") or ""
        if str(url).startswith("/"):
            url = "https://www.reddit.com" + str(url)
        db.execute(
            """INSERT OR REPLACE INTO viral_priors VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "reddit", external_id, str(url), str(item.get("title") or ""),
                str(item.get("selftext") or ""), str(item.get("languageCode") or ""),
                str(item.get("_subreddit") or item.get("subreddit") or ""),
                str(item.get("author") or ""), str(item.get("createdAt") or ""),
                json.dumps({
                    "score": integer(item.get("score")),
                    "upvote_ratio": item.get("upvote_ratio"),
                    "comments": integer(item.get("num_comments")),
                    "crossposts": integer(item.get("num_crossposts")),
                    "subreddit_subscribers": integer(item.get("subreddit_subscribers")),
                }),
                json.dumps(item, ensure_ascii=False), now(),
            ),
        )
        count += 1
    db.commit()
    return count


def china_relevant_finance_post(item: dict) -> bool:
    if item.get("_type") != "post" or not item.get("is_self"):
        return False
    text = f"{item.get('title') or ''}\n{item.get('selftext') or ''}".strip()
    if len(text) < 120:
        return False
    lowered = text.lower()
    return not any(term in lowered for term in US_LOCAL_TERMS)


def collect_reddit_finance(db_path: Path) -> dict:
    token = apify_token()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        init_db(db)
        raw = run_actor(db, token, REDDIT_ACTOR, {
            "subreddits": FINANCE_SUBREDDITS,
            "maxPostsPerSubreddit": 8,
            "sort": "top",
            "timeFilter": "week",
            "includeComments": False,
        }, 0.18)
        selected = [item for item in raw if china_relevant_finance_post(item)]
        return {
            "fetched": len(raw),
            "selected": ingest_reddit(db, selected),
            "subreddits": FINANCE_SUBREDDITS,
        }


def ingest_douyin(db: sqlite3.Connection, items: list[dict]) -> int:
    count = 0
    for item in items:
        external_id = str(item.get("aweme_id") or item.get("awemeId") or item.get("videoId") or "")
        if not external_id:
            continue
        db.execute(
            """INSERT OR REPLACE INTO viral_priors VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "douyin", external_id,
                str(item.get("url") or item.get("share_url") or item.get("videoPageUrl") or f"https://www.douyin.com/video/{external_id}"),
                str(item.get("desc") or item.get("caption") or ""),
                str(item.get("desc") or item.get("caption") or ""), "zh",
                "", str(item.get("author_nickname") or item.get("authorName") or item.get("userName") or ""),
                str(item.get("create_time") or item.get("createTime") or item.get("postedAt") or ""),
                json.dumps({
                    "plays": integer(item.get("play_count") or item.get("playCount")),
                    "likes": integer(item.get("digg_count") or item.get("diggCount") or item.get("likeCount")),
                    "comments": integer(item.get("comment_count") or item.get("commentCount")),
                    "shares": integer(item.get("share_count") or item.get("shareCount")),
                    "collects": integer(item.get("collect_count") or item.get("collectCount")),
                    "followers": integer(item.get("followerCount")),
                }),
                json.dumps(item, ensure_ascii=False), now(),
            ),
        )
        count += 1
    db.commit()
    return count


def collect_sample(db_path: Path) -> dict:
    token = apify_token()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        init_db(db)
        reddit = run_actor(db, token, REDDIT_ACTOR, {
            "subreddits": ["AskReddit", "technology", "personalfinance"],
            "maxPostsPerSubreddit": 5,
            "sort": "top",
            "timeFilter": "week",
            "includeComments": False,
        }, 0.06)
        reddit_count = ingest_reddit(db, reddit)

        trends = run_actor(db, token, DOUYIN_ACTOR, {
            "mode": "hot_search",
            "maxItems": 10,
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": "CN",
            },
        }, 0.03)
        words = [str(item.get("word") or "") for item in trends if item.get("word")][:3]
        videos = run_actor(db, token, DOUYIN_SEARCH_ACTOR, {
            "operation": "searchVideo",
            "keyword": "AI",
            "maxPages": 1,
        }, 0.12)
        douyin_count = ingest_douyin(db, videos)
        return {"reddit": reddit_count, "douyin": douyin_count, "douyin_keywords": words}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/viral_priors.sqlite3"))
    parser.add_argument("--mode", choices=("sample", "finance"), default="sample")
    args = parser.parse_args()
    result = collect_reddit_finance(args.db) if args.mode == "finance" else collect_sample(args.db)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
