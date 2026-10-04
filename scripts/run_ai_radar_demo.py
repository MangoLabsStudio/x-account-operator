#!/usr/bin/env python3
"""Run the independent, read-only AI focus radar once."""
from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_sources.ai_focus_router import deduplicate_events
from market_sources.ai_draft_pipeline import (
    ELIGIBLE,
    PROMPT_VERSION,
    apply_first_party_affiliation,
    build_draft_prompt,
    draft_eligibility,
    draft_violations,
    draft_input_hash,
    init_db as init_draft_db,
    normalize_draft_markup,
    original_author_identifiers,
    original_post_url,
    output_account_for_event,
    parse_draft_response,
    persist_draft,
    persist_events,
    refresh_draft_content_rules,
    refresh_draft_eligibility,
    remove_source_author_identifiers,
)
from market_sources.collect_big_source_posts import BASE_URL, HOST, _walk, fetch_page, twitter241_api_key


def runtime_paths(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    data_dir = os.getenv("AI_RADAR_DATA_DIR", "").strip()
    if data_dir:
        root = Path(data_dir)
        return root, root / "ai_radar_demo.sqlite3", root / "output"
    return ROOT / "data" / "ai_radar_demo", ROOT / config["db_path"], ROOT / config["output_dir"]


def _request(key: str, endpoint: str, **params: str) -> dict[str, Any]:
    request = Request(f"{BASE_URL}/{endpoint}?{urlencode(params)}", headers={"x-rapidapi-host": HOST, "x-rapidapi-key": key})
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise TypeError("Twitter241 returned non-object JSON")
    return payload


def llm_api_key() -> str:
    key = os.getenv("XOPS_LLM_API_KEY", "").strip()
    if key:
        return key
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "codex-deepseek-api-key", "-a", "deepseek", "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("未配置 AI Radar 改写模型")


def rewrite_event(key: str, event: dict[str, Any], account: dict[str, Any], rewrite: dict[str, Any]) -> str:
    prompt = build_draft_prompt(event, account)
    source = str(
        (event.get("upstream_text") or event.get("text") or "")
        if event.get("post_type") == "retweet"
        else (event.get("text") or event.get("upstream_text") or "")
    ).strip()

    def complete(value: str) -> str:
        body = json.dumps({
            "model": rewrite.get("model", "deepseek-chat"),
            "messages": [{"role": "user", "content": value}],
            "temperature": float(rewrite.get("temperature", 0.2)),
            "max_tokens": int(rewrite.get("max_tokens", 4096)),
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode()
        endpoint = os.getenv("XOPS_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        request = Request(
            endpoint, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=90) as response:
            payload = json.loads(response.read())
        return str(payload["choices"][0]["message"]["content"]).strip()

    content = complete(prompt)
    try:
        draft = parse_draft_response(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        draft = parse_draft_response(complete(prompt + "\n上一次返回格式不合格。这次只返回一个含 draft_zh 的 JSON 对象。"))
    draft = normalize_draft_markup(draft)
    account_name = str(account.get("name") or "")
    author_ids = original_author_identifiers(event)
    violations = draft_violations(draft, account_name, source, author_ids)
    if violations:
        repair = "、".join(violations)
        draft = parse_draft_response(complete(
            prompt
            + f"\n上一稿违反了：{repair}。按原稿逐句最小修改；原稿有第一人称要保留，"
              "原稿没有则不能新增；删除来源标记和编辑旁白。"
            + f"\n上一稿：{draft}"
        ))
        draft = normalize_draft_markup(draft)
    remaining = draft_violations(draft, account_name, source, author_ids)
    if "source_author_identity" in remaining:
        draft = remove_source_author_identifiers(draft, author_ids)
        remaining = draft_violations(draft, account_name, source, author_ids)
    if remaining:
        raise ValueError("draft violations: " + ",".join(remaining))
    return draft


def lookup_user_id(key: str, handle: str) -> str:
    wanted = handle.lstrip("@").lower()
    fallback = ""
    for item in _walk(_request(key, "user", username=wanted)):
        if not isinstance(item, dict) or item.get("__typename") != "User":
            continue
        screen_name = str((item.get("core") or {}).get("screen_name") or (item.get("legacy") or {}).get("screen_name") or "").lower()
        if item.get("rest_id") and not fallback:
            fallback = str(item["rest_id"])
        if screen_name == wanted and item.get("rest_id"):
            return str(item["rest_id"])
    if fallback:
        return fallback
    raise LookupError(f"Twitter241 could not resolve @{handle}")


def _author(tweet: dict[str, Any]) -> tuple[str, str, str]:
    result = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = result.get("core") or {}
    legacy = result.get("legacy") or {}
    return (
        str(result.get("rest_id") or ""),
        str(core.get("screen_name") or legacy.get("screen_name") or ""),
        str(core.get("name") or legacy.get("name") or ""),
    )


def _text(tweet: dict[str, Any]) -> str:
    note = (((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
    legacy = tweet.get("legacy") or {}
    return str(note or legacy.get("full_text") or legacy.get("text") or "")


def _urls(tweet: dict[str, Any]) -> list[str]:
    entries = list(((tweet.get("legacy") or {}).get("entities") or {}).get("urls") or [])
    entries += list(((((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("entity_set") or {}).get("urls") or [])
    return list(dict.fromkeys(str(item.get("expanded_url") or item.get("unwound_url") or item.get("url")) for item in entries if item.get("expanded_url") or item.get("unwound_url") or item.get("url")))


def parse_payload(payload: dict[str, Any], account: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
    posts: dict[str, dict[str, Any]] = {}
    for tweet in _walk(payload):
        if not isinstance(tweet, dict) or tweet.get("__typename") != "Tweet":
            continue
        author_id, handle, author_name = _author(tweet)
        if author_id != str(account["user_id"]):
            continue
        legacy = tweet.get("legacy") or {}
        post_id, created = str(tweet.get("rest_id") or legacy.get("id_str") or ""), legacy.get("created_at")
        if not post_id or not created or legacy.get("in_reply_to_status_id_str"):
            continue
        created_at = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
        if created_at < since:
            continue
        quote = ((tweet.get("quoted_status_result") or {}).get("result"))
        retweet = ((legacy.get("retweeted_status_result") or {}).get("result"))
        upstream = quote if isinstance(quote, dict) else retweet if isinstance(retweet, dict) else None
        kind = "quote" if isinstance(quote, dict) else "retweet" if isinstance(retweet, dict) else "original"
        upstream_legacy = (upstream or {}).get("legacy") or {}
        upstream_id, upstream_handle, upstream_author_name = _author(upstream or {})
        upstream_post_id = str((upstream or {}).get("rest_id") or upstream_legacy.get("id_str") or "")
        handle = handle or account["handle"]
        posts[post_id] = {
            "post_id": post_id, "handle": handle, "author_name": author_name, "text": _text(tweet),
            "created_at": created_at.isoformat(), "url": f"https://x.com/{handle}/status/{post_id}", "is_reply": False,
            "post_type": kind, "external_urls": _urls(tweet), "upstream_post_id": upstream_post_id if upstream else "",
            "upstream_handle": upstream_handle if upstream else "", "upstream_author_name": upstream_author_name if upstream else "",
            "upstream_url": f"https://x.com/{upstream_handle}/status/{upstream_post_id}" if upstream_post_id else "",
            "upstream_text": _text(upstream or {}),
            "upstream_external_urls": _urls(upstream) if upstream else [], "source_tier": "S3",
        }
    return list(posts.values())


def fetch_account(key: str, account: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
    posts: dict[str, dict[str, Any]] = {}
    cursor = None
    seen_cursors = set()
    seen_timeline_posts = set()
    while True:
        payload = fetch_page(key, account, cursor=cursor, count=40)
        page_posts = parse_payload(payload, account, since)
        posts.update((post["post_id"], post) for post in page_posts)

        fresh_recent = False
        for tweet in _walk(payload):
            if not isinstance(tweet, dict) or tweet.get("__typename") != "Tweet":
                continue
            author_id, _, _ = _author(tweet)
            legacy = tweet.get("legacy") or {}
            post_id = str(tweet.get("rest_id") or legacy.get("id_str") or "")
            created = str(legacy.get("created_at") or "")
            if author_id != str(account["user_id"]) or not post_id or not created:
                continue
            created_at = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
            if created_at >= since and post_id not in seen_timeline_posts:
                fresh_recent = True
            seen_timeline_posts.add(post_id)
        if not fresh_recent:
            break

        next_cursor = str(((payload.get("cursor") or {}).get("bottom") or "")).strip()
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return list(posts.values())


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ai_radar_posts(post_id TEXT PRIMARY KEY, handle TEXT NOT NULL, created_at TEXT NOT NULL, post_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ai_radar_fetches(handle TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
    """)
    init_draft_db(db)


def _display_text(event: dict[str, Any]) -> str:
    upstream = str(event.get("upstream_text") or "").strip()
    visible = re.sub(r"https?://\S+", "", upstream).strip()
    return upstream if len(visible) >= 40 else str(event.get("text") or upstream).strip()


def resolve_accounts(config: dict[str, Any], key: str, cache_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    cached = {}
    if cache_path.exists():
        cached = {item["handle"].lower(): item["user_id"] for item in json.loads(cache_path.read_text(encoding="utf-8"))}
    accounts, failures = [], []
    for source in config["source_accounts"]:
        account = dict(source)
        account["user_id"] = account.get("user_id") or cached.get(account["handle"].lower())
        if not account.get("user_id"):
            try:
                account["user_id"] = lookup_user_id(key, account["handle"])
            except (OSError, ValueError, LookupError, TypeError, RuntimeError) as error:
                failures.append({"handle": account["handle"], "error": str(error)})
                continue
        accounts.append(account)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps([{"handle": item["handle"], "user_id": item["user_id"]} for item in accounts], ensure_ascii=False, indent=2), encoding="utf-8")
    return accounts, failures


def persona_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    focus_titles = {item["id"]: item.get("title", item["id"]) for item in config.get("focuses", [])}
    return [
        {
            "id": str(account["slug"]),
            "name": str(account.get("name") or account["slug"]),
            "profile_handle": str(account.get("profile_handle") or ""),
            "profile_handle_is_internal_only": bool(account.get("profile_handle_is_internal_only", True)),
            "bio": str(account.get("bio") or ""),
            "focus": str(account.get("focus") or ""),
            "focus_title": str(focus_titles.get(account.get("focus"), account.get("focus") or "")),
        }
        for account in config.get("output_accounts", [])
    ]


def prepare_events(events: list[dict[str, Any]], posts: list[dict[str, Any]], config: dict[str, Any]) -> None:
    by_id = {str(post.get("post_id") or ""): post for post in posts}
    for event in events:
        apply_first_party_affiliation(event, config.get("first_party_affiliations", {}))
        event["source_posts"] = [by_id[post_id] for post_id in event.get("source_post_ids", []) if post_id in by_id]
        account = output_account_for_event(event, config)
        event["output_account"] = (
            {key: account.get(key, "") for key in ("slug", "name", "focus", "voice", "profile_handle", "profile_handle_is_internal_only", "bio")}
            if account else None
        )


def pending_drafts(db: sqlite3.Connection, events: list[dict[str, Any]], config: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result = []
    for account in config.get("output_accounts", []):
        candidates = [event for event in events if event.get("focus") == account.get("focus")]
        candidates.sort(key=lambda item: ({"high": 2, "medium": 1}.get(item.get("confidence"), 0), item.get("created_at", "")), reverse=True)
        for event in candidates:
            if draft_eligibility(event)[0] != ELIGIBLE:
                continue
            input_hash = draft_input_hash(event, account)
            existing = db.execute(
                """SELECT review_status FROM ai_radar_drafts
                   WHERE event_id=? AND output_account_id=? AND input_hash=?""",
                (event["event_id"], account["slug"], input_hash),
            ).fetchone()
            if existing and existing[0] != "needs_rewrite":
                continue
            result.append((event, account))
    return result


def generate_drafts(db: sqlite3.Connection, events: list[dict[str, Any]], config: dict[str, Any]) -> tuple[int, list[dict[str, str]]]:
    rewrite = config.get("rewrite", {})
    if not rewrite.get("enabled", True):
        return 0, []
    db.execute(
        "UPDATE ai_radar_drafts SET review_status='superseded' WHERE review_status='needs_review' AND prompt_version<>?",
        (PROMPT_VERSION,),
    )
    db.commit()
    tasks = pending_drafts(db, events, config)
    if not tasks:
        return 0, []
    try:
        key = llm_api_key()
    except RuntimeError as error:
        return 0, [{"stage": "rewrite", "error": str(error)}]
    generated, failures = [], []
    with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as executor:
        futures = {
            executor.submit(rewrite_event, key, event, account, rewrite): (event, account)
            for event, account in tasks
        }
        for future in as_completed(futures):
            event, account = futures[future]
            try:
                generated.append((event, future.result()))
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as error:
                failures.append({
                    "stage": "rewrite", "event_id": str(event["event_id"]),
                    "output_account": str(account["slug"]), "error": str(error)[:300],
                })
    created = sum(
        persist_draft(db, event, config, draft, model=str(rewrite.get("model") or "deepseek-chat"))
        for event, draft in generated
    )
    db.commit()
    return created, failures


def load_drafts(db: sqlite3.Connection, config: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = {item["id"]: item for item in persona_profiles(config)}
    rows = db.execute(
        """SELECT d.event_id,d.output_account_id,d.focus,d.draft_zh,d.verification_status,
                  d.review_status,d.model,d.created_at,e.event_json
           FROM ai_radar_drafts d JOIN ai_radar_events e ON e.event_id=d.event_id
           WHERE d.review_status='needs_review' AND e.draft_eligibility='eligible'
           ORDER BY d.created_at DESC"""
    ).fetchall()
    drafts, seen = [], set()
    for row in rows:
        event_id, account_id = str(row[0]), str(row[1])
        if event_id in seen:
            continue
        seen.add(event_id)
        event = json.loads(row[8])
        source_url = original_post_url(event)
        draft_with_source = str(row[3]) + (f"\n\n原帖：[查看原帖](<{source_url}>)" if source_url else "")
        drafts.append({
            "event_id": event_id, "output_account_id": account_id,
            "output_account_name": profiles.get(account_id, {}).get("name", account_id),
            "persona": profiles.get(account_id), "focus": row[2],
            "draft_zh": row[3], "original_post_url": source_url, "draft_with_source": draft_with_source,
            "verification_status": row[4], "review_status": row[5],
            "model": row[6], "created_at": row[7], "event": event,
        })
    return drafts


def render(events: list[dict[str, Any]], failures: list[dict[str, str]], config: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    (output_dir / "latest.json").write_text(json.dumps({"generated_at": generated_at, "event_count": len(events), "failure_count": len(failures), "failures": failures, "events": events}, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {focus["id"]: sum(item.get("focus") == focus["id"] for item in events) for focus in config["focuses"]}
    unclassified = [item for item in events if not item.get("focus")]
    lines = ["# AI 垂直资讯 Radar Demo", "", f"生成时间：{generated_at}", f"事件：{len(events)}｜已分类：{len(events) - len(unclassified)}｜待判断：{len(unclassified)}｜失败账号：{len(failures)}", ""]
    for focus in config["focuses"]:
        chosen = [item for item in events if item.get("focus") == focus["id"]]
        lines += [f"## {focus['title']}（{len(chosen)}）", ""]
        for event in chosen:
            note = "仅发现线索，需回溯原始信源" if event["discovery_only"] else "可进一步核验"
            lines += [f"### @{event.get('handle', '')}", _display_text(event), f"发现：{event.get('discovery_url', '')}", f"上游：{event.get('canonical_source_url', '')}", f"来源：{', '.join(event['discovery_sources'])}｜{note}", ""]
    lines += [f"## 未分类（{len(unclassified)}）", ""]
    for event in unclassified:
        lines += [f"### @{event.get('handle', '')}", _display_text(event), f"发现：{event.get('discovery_url', '')}", ""]
    markdown = "\n".join(lines)
    (output_dir / "latest.md").write_text(markdown, encoding="utf-8")
    nav = "".join(f"<a href='#{focus['id']}'>{html.escape(focus['title'])}<b>{counts[focus['id']]}</b></a>" for focus in config["focuses"])
    sections = []
    for focus in config["focuses"]:
        chosen = [item for item in events if item.get("focus") == focus["id"]]
        cards = "".join(
            f"<article><div class='meta'>@{html.escape(str(item.get('handle') or ''))} · {html.escape(str(item.get('post_type') or ''))} · {html.escape(str(item.get('confidence') or ''))}</div>"
            f"<p>{html.escape(_display_text(item))}</p><div class='links'><a href='{html.escape(str(item.get('discovery_url') or ''))}'>发现帖</a><a href='{html.escape(str(item.get('canonical_source_url') or ''))}'>上游源</a></div></article>"
            for item in chosen
        ) or "<p class='empty'>本轮没有候选</p>"
        sections.append(f"<section id='{focus['id']}'><h2>{html.escape(focus['title'])}<span>{len(chosen)}</span></h2>{cards}</section>")
    html_page = f"""<!doctype html><meta charset='utf-8'><title>AI 垂直资讯 Radar</title><style>
body{{margin:0;background:#f4f5f2;color:#17211b;font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}main{{max-width:1120px;margin:auto;padding:28px}}h1{{margin:0 0 8px}}.summary{{color:#56645b}}nav{{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0}}nav a{{background:#fff;border:1px solid #dce3dd;border-radius:999px;padding:8px 12px;color:#244c36;text-decoration:none}}nav b{{margin-left:7px}}section{{margin:28px 0}}h2{{display:flex;justify-content:space-between;border-bottom:1px solid #ccd6ce;padding-bottom:8px}}article{{background:white;border:1px solid #dde4de;border-radius:12px;padding:14px 16px;margin:10px 0}}article p{{white-space:pre-wrap;line-height:1.55}}.meta,.empty{{color:#6b766e}}.links{{display:flex;gap:14px}}.links a{{color:#17633a}}</style><main><h1>AI 垂直资讯 Radar Demo</h1><div class='summary'>{html.escape(generated_at)} · {len(events)} 个事件 · {len(events)-len(unclassified)} 个已分类 · {len(unclassified)} 个待判断 · {len(failures)} 个失败账号</div><nav>{nav}</nav>{''.join(sections)}</main>"""
    (output_dir / "latest.html").write_text(html_page, encoding="utf-8")
    by_focus = output_dir / "by_focus"; by_focus.mkdir(exist_ok=True)
    for focus in config["focuses"]:
        chosen = [item for item in events if item.get("focus") == focus["id"]]
        (by_focus / f"{focus['id']}.json").write_text(json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")
        (by_focus / f"{focus['id']}.md").write_text(f"# {focus['title']}\n\n" + "\n\n".join(_display_text(item) for item in chosen) + "\n", encoding="utf-8")
    (by_focus / "unclassified.json").write_text(json.dumps(unclassified, ensure_ascii=False, indent=2), encoding="utf-8")
    (by_focus / "unclassified.md").write_text("# 未分类\n\n" + "\n\n".join(_display_text(item) for item in unclassified) + "\n", encoding="utf-8")


def render_drafts(drafts: list[dict[str, Any]], failures: list[dict[str, str]], config: dict[str, Any], output_dir: Path) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    personas = persona_profiles(config)
    profiles = {item["id"]: item for item in personas}
    for draft in drafts:
        draft["persona"] = profiles.get(draft["output_account_id"])
        draft["output_account_name"] = (draft["persona"] or {}).get("name", draft["output_account_id"])
    payload = {
        "generated_at": generated_at, "draft_count": len(drafts),
        "rewrite_failure_count": len(failures), "rewrite_failures": failures,
        "personas": personas, "drafts": drafts,
    }
    (output_dir / "drafts.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# AI Radar 人物账号待审草稿", "", f"生成时间：{generated_at}", f"待审草稿：{len(drafts)}｜改写失败：{len(failures)}", "", "人物 handle 仅为本系统人物标识，未注册或运营 X 账号。", ""]
    sections = []
    nav = ["<button class='filter active' data-filter='all'>全部 <b>%d</b></button>" % len(drafts)]
    profile_cards = []
    by_account = output_dir / "by_account"
    by_account.mkdir(exist_ok=True)
    for account in config.get("output_accounts", []):
        profile = profiles[str(account["slug"])]
        chosen = [item for item in drafts if item["output_account_id"] == account["slug"]]
        lines += [f"## {profile['name']}（{len(chosen)}）", "", profile["bio"], "", f"Focus：{profile['focus_title']}", ""]
        cards = []
        for item in chosen:
            event = item["event"]
            lines += [
                f"### {profile['name']} · {profile['focus_title']}", item["draft_zh"],
                f"原帖：[查看原帖](<{item['original_post_url']}>)",
                f"状态：{item['review_status']}｜{item['verification_status']}",
            ]
            lines.append("")
            source_url = html.escape(item["original_post_url"], quote=True)
            cards.append(
                "<article>"
                f"<div class='author'><strong>{html.escape(profile['name'])}</strong><span>@{html.escape(profile['profile_handle'])}</span></div>"
                f"<p class='bio'>{html.escape(profile['bio'])}</p>"
                f"<div class='focus'>{html.escape(profile['focus_title'])}</div>"
                f"<div class='badge'>{html.escape(item['review_status'])} · {html.escape(item['verification_status'])}</div>"
                f"<p class='draft'>{html.escape(item['draft_zh'])}</p>"
                f"<p class='source'>原帖：<a href='{source_url}' target='_blank' rel='noopener'>查看原帖</a></p>"
                f"<small>{html.escape(item['event_id'])}</small></article>"
            )
        nav.append(f"<button class='filter' data-filter='{html.escape(account['slug'], quote=True)}'>{html.escape(profile['name'])} <b>{len(chosen)}</b></button>")
        profile_cards.append(
            f"<a class='persona-card' href='#{html.escape(account['slug'], quote=True)}' data-persona-link='{html.escape(account['slug'], quote=True)}'>"
            f"<div><strong>{html.escape(profile['name'])}</strong><span>@{html.escape(profile['profile_handle'])}</span></div>"
            f"<p>{html.escape(profile['bio'])}</p><small>{html.escape(profile['focus_title'])} · {len(chosen)} 条待审</small></a>"
        )
        sections.append(
            f"<section class='persona-section' data-persona='{html.escape(account['slug'], quote=True)}' id='{html.escape(account['slug'], quote=True)}'>"
            f"<header><div><h2>{html.escape(profile['name'])}<span>@{html.escape(profile['profile_handle'])}</span></h2><p>{html.escape(profile['bio'])}</p><small>{html.escape(profile['focus_title'])}</small></div><b>{len(chosen)} 条待审</b></header>"
            + ("".join(cards) or "<p class='empty'>暂无待审草稿</p>") + "</section>"
        )
        (by_account / f"{account['slug']}.json").write_text(json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")
        (by_account / f"{account['slug']}.md").write_text(
            f"# {profile['name']}\n\n{profile['bio']}\n\nFocus: {profile['focus_title']}\n\n" + "\n\n".join(item["draft_with_source"] for item in chosen) + "\n",
            encoding="utf-8",
        )
    (output_dir / "drafts.md").write_text("\n".join(lines), encoding="utf-8")
    page = f"""<!doctype html><meta charset='utf-8'><title>AI Radar · 人物账号待审草稿</title><style>
body{{margin:0;background:#f4f5f2;color:#17211b;font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}main{{max-width:1120px;margin:auto;padding:28px}}h1{{margin:0 0 8px}}.summary,.empty,small{{color:#66736b}}.notice{{margin:12px 0;color:#66736b;font-size:13px}}nav{{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0}}button.filter{{appearance:none;background:#fff;border:1px solid #dce3dd;border-radius:999px;padding:8px 12px;color:#244c36;cursor:pointer;font:inherit}}button.filter.active{{background:#244c36;color:#fff}}nav b{{margin-left:7px}}.persona-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:20px 0 30px}}.persona-card{{background:#fff;border:1px solid #dde4de;border-radius:12px;color:inherit;padding:14px;text-decoration:none}}.persona-card div,.author{{display:flex;align-items:baseline;gap:8px}}.persona-card span,.author span{{color:#66736b;font-size:13px}}.persona-card p{{line-height:1.45;margin:9px 0}}section{{margin:34px 0}}section>header{{display:flex;justify-content:space-between;gap:18px;border-bottom:1px solid #ccd6ce;padding-bottom:12px}}h2{{margin:0}}h2 span{{color:#66736b;font-size:14px;font-weight:400;margin-left:8px}}header p{{margin:7px 0;line-height:1.5;max-width:700px}}article{{background:#fff;border:1px solid #dde4de;border-radius:12px;padding:16px;margin:10px 0}}.bio{{color:#66736b;margin:8px 0;line-height:1.45}}.focus{{display:inline-block;background:#edf3ed;border-radius:999px;color:#244c36;font-size:12px;margin:2px 0 10px;padding:4px 8px}}.badge{{display:inline-block;background:#fff2c9;color:#745600;border-radius:999px;padding:4px 8px;font-size:12px;margin-left:6px}}.draft{{font-size:17px;line-height:1.65;white-space:pre-wrap}}.source{{border-top:1px solid #edf0ed;margin:14px 0 10px;padding-top:10px;overflow-wrap:anywhere}}.source a{{color:#17633a}}@media(max-width:600px){{main{{padding:18px}}section>header{{display:block}}}}</style><main><h1>AI Radar · 人物账号待审草稿</h1><div class='summary'>{html.escape(generated_at)} · {len(drafts)} 条待审 · {len(failures)} 条改写失败</div><div class='notice'>人物 handle 仅为本系统人物标识，未注册或运营 X 账号。</div><nav>{''.join(nav)}</nav><div class='persona-grid'>{''.join(profile_cards)}</div>{''.join(sections)}</main><script>document.querySelectorAll('.filter').forEach(button=>button.onclick=()=>{{const value=button.dataset.filter;document.querySelectorAll('.filter').forEach(item=>item.classList.toggle('active',item===button));document.querySelectorAll('.persona-section').forEach(section=>section.hidden=value!=='all'&&section.dataset.persona!==value);if(value!=='all')document.getElementById(value)?.scrollIntoView({{behavior:'smooth',block:'start'}})}});document.querySelectorAll('[data-persona-link]').forEach(link=>link.onclick=()=>{{document.querySelector(`.filter[data-filter="${{link.dataset.personaLink}}"]`)?.click()}})</script>"""
    (output_dir / "drafts.html").write_text(page, encoding="utf-8")


def run(config: dict[str, Any], key: str, *, hours: int | None = None) -> dict[str, int]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours or int(config["hours"]))
    root, db_path, output_dir = runtime_paths(config)
    accounts, failure_items = resolve_accounts(config, key, root / "accounts.json")
    posts = []
    with ThreadPoolExecutor(max_workers=int(config.get("workers", 6))) as executor:
        futures = {executor.submit(fetch_account, key, account, since): account for account in accounts}
        for future in as_completed(futures):
            account = futures[future]
            try:
                posts.extend(future.result())
            except (OSError, ValueError, LookupError, TypeError, RuntimeError) as error:
                account["error"] = str(error)
                failure_items.append({"handle": account["handle"], "error": str(error)})
    events = deduplicate_events(posts)
    prepare_events(events, posts, config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        init_db(db)
        now = datetime.now(timezone.utc).isoformat()
        for post in posts:
            db.execute("INSERT OR REPLACE INTO ai_radar_posts VALUES(?,?,?,?)", (post["post_id"], post["handle"], post["created_at"], json.dumps(post, ensure_ascii=False)))
        for account in accounts:
            db.execute("INSERT OR REPLACE INTO ai_radar_fetches VALUES(?,?,?,?)", (account["handle"], now, "error" if account.get("error") else "ok", account.get("error")))
        persist_events(db, events, posts)
        excluded_drafts = refresh_draft_eligibility(db, config.get("first_party_affiliations", {}))
        rewrites_queued = refresh_draft_content_rules(db)
        db.commit()
        created_drafts, rewrite_failures = generate_drafts(db, events, config)
        drafts = load_drafts(db, config)
    render(events, failure_items, config, output_dir)
    render_drafts(drafts, rewrite_failures, config, output_dir)
    return {
        "configured_accounts": len(config["source_accounts"]), "resolved_accounts": len(accounts),
        "failed": len(failure_items), "posts": len(posts), "events": len(events),
        "new_drafts": created_drafts, "excluded_drafts": excluded_drafts,
        "rewrites_queued": rewrites_queued,
        "drafts": len(drafts), "rewrite_failed": len(rewrite_failures),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the standalone AI Radar demo once.")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/ai_radar_demo.json"); parser.add_argument("--hours", type=int)
    parser.add_argument("--no-rewrite", action="store_true", help="Capture and persist events without calling the rewrite model.")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    lock_path = runtime_paths(config)[0] / "run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("AI Radar demo is already running") from error
        if args.no_rewrite:
            config.setdefault("rewrite", {})["enabled"] = False
        print(json.dumps(run(config, twitter241_api_key(), hours=args.hours), ensure_ascii=False))

if __name__ == "__main__":
    main()
