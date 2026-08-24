from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://twitter241.p.rapidapi.com"
HOST = "twitter241.p.rapidapi.com"
MOTHER_POOL_PATH = Path(
    os.getenv(
        "XOPS_MOTHER_POOL_ACCOUNTS",
        Path(__file__).resolve().parents[1] / "configs" / "content_source_accounts.json",
    )
)
DEFAULT_ACCOUNTS_PATH = MOTHER_POOL_PATH
PAGE_SIZE = 20  # existing successful first-pass used this size; preserves continuation semantics
MAX_RETRIES = 3


def twitter241_api_key() -> str:
    """Read the sole credential from Keychain only at request time."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "codex.twitter241.rapidapi", "-a", "TWITTER241_RAPIDAPI_KEY", "-w"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("未配置 Twitter241 Keychain 凭据")


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _mother_pool_path(path: Path) -> Path:
    if path.resolve() != MOTHER_POOL_PATH.resolve():
        raise ValueError(f"必须使用唯一总信息源母池：{MOTHER_POOL_PATH}")
    return MOTHER_POOL_PATH


def _legacy_author_id(user_id: str) -> str:
    """Compatibility bridge for the pre-contract anonymous local cache only."""
    return f"anon_{hashlib.sha256(str(user_id).encode()).hexdigest()}"


def load_accounts(accounts_path: Path = DEFAULT_ACCOUNTS_PATH) -> list[dict]:
    accounts = json.loads(_mother_pool_path(accounts_path).read_text(encoding="utf-8"))
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("Mother pool must be a non-empty list")
    ids = set()
    for account in accounts:
        if not isinstance(account, dict) or not account.get("user_id") or not account.get("source_lists"):
            raise ValueError("Mother pool contains an invalid account record")
        if account["user_id"] in ids:
            raise ValueError(f"Mother pool contains duplicate user_id: {account['user_id']}")
        ids.add(account["user_id"])
    return accounts


def _metrics(item: dict, legacy: dict) -> dict:
    views = item.get("views") or {}
    return {
        name: value for name, value in {
            "reply_count": legacy.get("reply_count"),
            "retweet_count": legacy.get("retweet_count"),
            "favorite_count": legacy.get("favorite_count"),
            "quote_count": legacy.get("quote_count"),
            "bookmark_count": legacy.get("bookmark_count"),
            "view_count": views.get("count"),
        }.items() if value is not None
    }


def parse_posts(payload: dict, account: dict, since: datetime | None = None) -> list[dict]:
    """Return one timeline page's own posts, including replies, reposts and quotes."""
    posts = {}
    for item in _walk(payload):
        if not isinstance(item, dict) or item.get("__typename") != "Tweet":
            continue
        legacy = item.get("legacy") or {}
        author = (((item.get("core") or {}).get("user_results") or {}).get("result") or {})
        if str(author.get("rest_id") or "") != str(account["user_id"]):
            continue
        post_id = str(item.get("rest_id") or legacy.get("id_str") or "")
        created_raw = legacy.get("created_at")
        if not post_id or not created_raw:
            continue
        created_at = parsedate_to_datetime(created_raw).astimezone(timezone.utc)
        if since and created_at < since:
            continue
        note = (((item.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
        text = note or legacy.get("full_text") or legacy.get("text") or ""
        handle = str((author.get("core") or {}).get("screen_name") or account.get("handle") or "").lstrip("@")
        posts[post_id] = {
            "post_id": post_id,
            "author_id": str(account["user_id"]),
            "handle": handle,
            "text": text,
            "created_at": created_at.isoformat(),
            "url": f"https://x.com/{handle}/status/{post_id}" if handle else "",
            "is_reply": bool(legacy.get("in_reply_to_status_id_str")),
            "is_retweet": bool(legacy.get("retweeted_status_result")) or text.startswith("RT @"),
            "is_quote": bool(legacy.get("is_quote_status")) or bool(item.get("quoted_status_result")),
            "metrics": _metrics(item, legacy),
            "source_lists": account["source_lists"],
        }
    return list(posts.values())


def fetch_page(key: str, account: dict, cursor: str | None = None, count: int = PAGE_SIZE) -> dict:
    params = {"user": account["user_id"], "count": str(count)}
    if cursor:
        params["cursor"] = cursor
    request = Request(
        f"{BASE_URL}/user-tweets?{urlencode(params)}",
        headers={"x-rapidapi-host": HOST, "x-rapidapi-key": key},
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise TypeError("Twitter241 /user-tweets returned non-object JSON")
    return payload


def _fetch_with_retry(key: str, account: dict, cursor: str | None) -> tuple[dict, int]:
    for attempt in range(MAX_RETRIES):
        try:
            return fetch_page(key, account, cursor), attempt
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            if attempt + 1 == MAX_RETRIES:
                raise RuntimeError(f"{type(error).__name__}: {str(error)[:300]}") from error
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def fetch_account(
    key: str, account: dict, *, since: datetime, watermark: datetime | None,
    run_started_at: datetime, continuation_only: bool = False,
) -> dict:
    """Fetch newest-to-oldest; stop immediately after crossing the account watermark."""
    boundary = watermark or since
    cursor = None
    seen_cursors = set()
    posts: dict[str, dict] = {}
    pages = retries = 0
    while True:
        payload, used_retries = _fetch_with_retry(key, account, cursor)
        pages += 1
        retries += used_retries
        page_posts = parse_posts(payload, account)
        crossed = False
        for post in page_posts:
            if datetime.fromisoformat(post["created_at"]) < boundary:
                crossed = True
                break
            posts[post["post_id"]] = post
        if crossed or continuation_only and pages == 1 and len(page_posts) < PAGE_SIZE:
            break
        next_cursor = str(((payload.get("cursor") or {}).get("bottom") or "")).strip()
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return {
        "posts": list(posts.values()), "pages_fetched": pages, "retries": retries,
        "watermark_at": run_started_at.isoformat(),
    }


def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS source_posts(
            post_id TEXT PRIMARY KEY, author_id TEXT NOT NULL, handle TEXT NOT NULL, text TEXT NOT NULL,
            created_at TEXT NOT NULL, url TEXT NOT NULL, is_reply INTEGER NOT NULL, source_lists TEXT NOT NULL,
            is_retweet INTEGER NOT NULL DEFAULT 0, is_quote INTEGER NOT NULL DEFAULT 0, metrics TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS source_fetches(
            author_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
            handle TEXT NOT NULL DEFAULT '', watermark_at TEXT, pages_fetched INTEGER NOT NULL DEFAULT 0,
            retries INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS source_post_runs(run_id TEXT NOT NULL, post_id TEXT NOT NULL, PRIMARY KEY(run_id, post_id));
        CREATE TABLE IF NOT EXISTS source_fetch_attempts(
            run_id TEXT NOT NULL, author_id TEXT NOT NULL, handle TEXT NOT NULL, status TEXT NOT NULL,
            error TEXT, pages_fetched INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(run_id, author_id)
        );
    """)
    for column, declaration in (("is_retweet", "INTEGER NOT NULL DEFAULT 0"), ("is_quote", "INTEGER NOT NULL DEFAULT 0"), ("metrics", "TEXT NOT NULL DEFAULT '{}'")):
        _ensure_column(db, "source_posts", column, declaration)
    for column, declaration in (("handle", "TEXT NOT NULL DEFAULT ''"), ("watermark_at", "TEXT"), ("pages_fetched", "INTEGER NOT NULL DEFAULT 0"), ("retries", "INTEGER NOT NULL DEFAULT 0")):
        _ensure_column(db, "source_fetches", column, declaration)
    db.commit()


def _load_fetch_state(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return {str(row["author_id"]): row for row in db.execute("SELECT * FROM source_fetches")}


def _bootstrap_legacy_public_ids(db: sqlite3.Connection, accounts: list[dict]) -> None:
    """Migrate the prior local anonymous cache without calling the API again."""
    for account in accounts:
        author_id = str(account["user_id"])
        legacy_id = _legacy_author_id(author_id)
        if db.execute("SELECT 1 FROM source_fetches WHERE author_id=?", (author_id,)).fetchone():
            continue
        if not db.execute("SELECT 1 FROM source_fetches WHERE author_id=?", (legacy_id,)).fetchone():
            continue
        db.execute("UPDATE source_fetches SET author_id=?, handle=? WHERE author_id=?", (author_id, account.get("handle", ""), legacy_id))
        db.execute("UPDATE source_posts SET author_id=?, handle=COALESCE(NULLIF(handle,''),?) WHERE author_id=?", (author_id, account.get("handle", ""), legacy_id))


def _legacy_truncated_accounts(db: sqlite3.Connection, since: datetime) -> set[str]:
    return {
        str(author_id) for author_id, count in db.execute(
            "SELECT author_id, COUNT(*) FROM source_posts WHERE created_at>=? GROUP BY author_id HAVING COUNT(*)>=?",
            (since.isoformat(), PAGE_SIZE),
        )
    }


def _write_outputs(db: sqlite3.Connection, output_dir: Path, run_id: str, stats: dict) -> None:
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT p.* FROM source_posts p JOIN source_post_runs r ON r.post_id=p.post_id
        WHERE r.run_id=? ORDER BY p.created_at DESC
    """, (run_id,)).fetchall()
    posts = [{**dict(row), "is_reply": bool(row["is_reply"]), "is_retweet": bool(row["is_retweet"]),
              "is_quote": bool(row["is_quote"]), "metrics": json.loads(row["metrics"]),
              "source_lists": json.loads(row["source_lists"])} for row in rows]
    failures = [dict(row) for row in db.execute("""
        SELECT author_id,handle,error,pages_fetched,retries FROM source_fetch_attempts
        WHERE run_id=? AND status='error' ORDER BY handle
    """, (run_id,)).fetchall()]
    payload = {**stats, "generated_at": datetime.now(timezone.utc).isoformat(), "failures": failures, "posts": posts}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 每日大表信息源（运行快照）", "", f"运行：{run_id}",
             f"时间窗：{stats['window_start']} 至 {stats['window_end']}",
             f"母池：{stats['account_universe']}｜首屏已覆盖：{stats['accounts_skipped']}｜本轮续抓成功：{stats['accounts_fetched']}｜失败：{stats['accounts_failed']}｜覆盖率：{stats['coverage_rate']:.2%}｜新增：{stats['posts_new']}", ""]
    if failures:
        lines += ["## 失败账号", ""] + [f"- @{item['handle'] or item['author_id']}：{item['error']}" for item in failures] + [""]
    for post in posts:
        flags = "、".join(label for ok, label in ((post["is_reply"], "回复"), (post["is_retweet"], "转推"), (post["is_quote"], "引用")) if ok)
        lines += [f"## @{post['handle']}｜{post['created_at']}{'｜' + flags if flags else ''}", post["url"], post["text"], ""]
    (output_dir / "latest.md").write_text("\n".join(lines), encoding="utf-8")


def collect(
    accounts_path: Path = DEFAULT_ACCOUNTS_PATH, db_path: Path | None = None, output_dir: Path | None = None,
    *, key: str | None = None, hours: int = 30, workers: int = 8, resume_hours: int = 20,
) -> dict:
    if db_path is None or output_dir is None:
        raise ValueError("db_path and output_dir are required")
    accounts = load_accounts(accounts_path)
    key = key or twitter241_api_key()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    snapshot_dir = output_dir / "runs" / run_id
    stats = {"run_id": run_id, "account_universe": len(accounts), "accounts_fetched": 0, "accounts_skipped": 0,
             "accounts_failed": 0, "posts_seen": 0, "posts_new": 0, "window_start": since.isoformat(), "window_end": now.isoformat()}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        init_db(db)
        _bootstrap_legacy_public_ids(db, accounts)
        db.commit()
        state = _load_fetch_state(db)
        legacy_truncated = _legacy_truncated_accounts(db, since)
        recent_cutoff = now - timedelta(hours=resume_hours)
        pending = []
        for account in accounts:
            previous = state.get(str(account["user_id"]))
            if previous and previous["status"] == "ok" and previous["fetched_at"] and datetime.fromisoformat(previous["fetched_at"]).astimezone(timezone.utc) >= recent_cutoff:
                if previous["watermark_at"]:
                    stats["accounts_skipped"] += 1
                    continue
                if str(account["user_id"]) not in legacy_truncated:
                    # The verified all-account first pass is already complete; turn it into a watermark
                    # instead of charging a second full 4,684-account run.
                    db.execute("UPDATE source_fetches SET watermark_at=? WHERE author_id=?", (previous["fetched_at"], account["user_id"]))
                    stats["accounts_skipped"] += 1
                    continue
                pending.append((account, None, True))
                continue
            watermark = None
            if previous and previous["watermark_at"]:
                try:
                    watermark = datetime.fromisoformat(previous["watermark_at"]).astimezone(timezone.utc)
                except ValueError:
                    pass
            pending.append((account, watermark, False))
        db.commit()
        print(json.dumps({"source_accounts": len(accounts), "first_page_covered": stats["accounts_skipped"], "continuation_pending": len(pending)}), flush=True)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_account, key, account, since=since, watermark=watermark, run_started_at=now, continuation_only=continuation): account for account, watermark, continuation in pending}
            for future in as_completed(futures):
                account = futures[future]
                author_id, handle = str(account["user_id"]), account.get("handle", "")
                fetched_at = datetime.now(timezone.utc).isoformat()
                try:
                    result = future.result()
                    for post in result["posts"]:
                        existed = db.execute("SELECT 1 FROM source_posts WHERE post_id=?", (post["post_id"],)).fetchone()
                        db.execute("""INSERT INTO source_posts(post_id,author_id,handle,text,created_at,url,is_reply,source_lists,is_retweet,is_quote,metrics)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET author_id=excluded.author_id,handle=excluded.handle,text=excluded.text,created_at=excluded.created_at,url=excluded.url,is_reply=excluded.is_reply,source_lists=excluded.source_lists,is_retweet=excluded.is_retweet,is_quote=excluded.is_quote,metrics=excluded.metrics""",
                            (post["post_id"],post["author_id"],post["handle"],post["text"],post["created_at"],post["url"],int(post["is_reply"]),json.dumps(post["source_lists"]),int(post["is_retweet"]),int(post["is_quote"]),json.dumps(post["metrics"])))
                        db.execute("INSERT OR IGNORE INTO source_post_runs VALUES(?,?)", (run_id, post["post_id"]))
                        stats["posts_seen"] += 1
                        stats["posts_new"] += int(existed is None)
                    db.execute("""INSERT INTO source_fetches(author_id,fetched_at,status,error,handle,watermark_at,pages_fetched,retries)
                        VALUES(?,?, 'ok',NULL,?,?,?,?) ON CONFLICT(author_id) DO UPDATE SET fetched_at=excluded.fetched_at,status='ok',error=NULL,handle=excluded.handle,watermark_at=excluded.watermark_at,pages_fetched=excluded.pages_fetched,retries=excluded.retries""",
                        (author_id,fetched_at,handle,result["watermark_at"],result["pages_fetched"],result["retries"]))
                    db.execute("INSERT OR REPLACE INTO source_fetch_attempts VALUES(?,?,?, 'ok',NULL,?,?)", (run_id,author_id,handle,result["pages_fetched"],result["retries"]))
                    stats["accounts_fetched"] += 1
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                    message = f"{type(error).__name__}: {str(error)[:300]}"
                    db.execute("""INSERT INTO source_fetches(author_id,fetched_at,status,error,handle,watermark_at,pages_fetched,retries)
                        VALUES(?,?, 'error',?,?,NULL,0,?) ON CONFLICT(author_id) DO UPDATE SET fetched_at=excluded.fetched_at,status='error',error=excluded.error,handle=excluded.handle,pages_fetched=0,retries=excluded.retries""", (author_id,fetched_at,message,handle,MAX_RETRIES))
                    db.execute("INSERT OR REPLACE INTO source_fetch_attempts VALUES(?,?,?, 'error',?,0,?)", (run_id,author_id,handle,message,MAX_RETRIES))
                    stats["accounts_failed"] += 1
                db.commit()
        stats["coverage_rate"] = (stats["accounts_skipped"] + stats["accounts_fetched"]) / stats["account_universe"] if stats["account_universe"] else 0.0
        _write_outputs(db, snapshot_dir, run_id, stats)
    return {**stats, "snapshot_dir": str(snapshot_dir)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=30)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(collect(db_path=args.db, output_dir=args.output, hours=args.hours, workers=args.workers), ensure_ascii=False))


if __name__ == "__main__":
    main()
