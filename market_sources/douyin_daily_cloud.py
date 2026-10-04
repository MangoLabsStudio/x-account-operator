"""Cloud-only daily Douyin source-to-post pipeline.

It keeps the raw audio and ASR on Railway's persistent volume and returns only
finished posts.  The caller owns persona assignment and the public API write.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

TAGS = {
    "finance": ("财经", "投资理财", "美股", "股票", "基金", "黄金", "宏观经济", "财富管理"),
    "ai": ("人工智能", "AI", "AIGC", "科技", "芯片"),
    "other": ("商业", "生意", "创业", "职场", "个人成长"),
}
SUPPLEMENTAL_TAGS = {
    "finance": ("经济", "金融", "股市"),
    "ai": ("AI科普", "大模型", "机器人", "算力"),
    "other": ("商业思维", "搞钱", "自我提升"),
}
FALLBACK_TAGS = {
    "finance": ("投资", "理财", "价值投资"),
    "ai": ("AI应用", "AI创业", "科技趋势", "英伟达"),
    "other": ("财富认知", "社会观察", "副业"),
}
FINAL_TAGS = {
    "finance": ("股票投资", "投资思维", "理财知识"),
    "ai": ("AI知识", "AI教程", "人工智能科普"),
    "other": ("创业经验", "职场成长"),
}
LATE_TAGS = {
    "finance": ("财经", "投资理财", "美股", "股票", "基金", "黄金", "宏观经济", "经济", "金融", "投资"),
    "ai": ("人工智能", "AI", "AIGC", "科技", "芯片", "AI科普", "大模型", "机器人", "算力", "AI应用"),
    "other": ("商业", "创业", "职场"),
}
LATE_TAGS_2 = {
    "finance": ("理财", "股市", "价值投资", "股票投资", "投资思维", "财富管理", "理财知识", "黄金"),
    "ai": ("AI创业", "科技趋势", "英伟达", "AI知识", "AI教程", "人工智能科普", "AI", "科技"),
    "other": ("创业经验", "职场成长"),
}
DELIVERY_TARGETS = {"finance": 28, "ai": 20, "other": 12}
PREP_TARGETS = {"finance": 35, "ai": 30, "other": 15}
MAX_ATTEMPTS = 6
RETRY_DELAYS = (300, 1800, 7200, 21600, 7200, 21600)
ORG = re.compile("新华社|央视|日报|晚报|电视台|融媒体|财经网|证券时报|官方|发布|新闻")
BAD = re.compile("剧情|短剧|动画|影视|游戏解说|舞蹈|音乐|开箱|实测|展示|演示|制作过程|提纯|拆解|修复|变装|现场画面")
AI_WORDS = re.compile("AI|人工智能|大模型|模型|机器人|芯片|英伟达|算力|Agent|Claude|DeepSeek|半导体", re.I)
VISUAL = re.compile("画面中|注意看|看屏幕|如图|下图|这个画面|你看这个|这里")
EDITORIAL = re.compile("口播里提到|视频里说|视频没有展开|原稿认为|作者表示|根据转写")
DISCLAIM = re.compile("不构成.{0,8}投资建议|仅供参考|投资有风险")
BASE = "https://api.tikhub.io/api/v1/douyin"
WHISPER = None

PROMPT = """你是抖音口播转中文帖子的文字编辑。只返回 JSON：{{\"usable\":true或false,\"reason\":\"不合格时原因\",\"final\":\"最终正文\"}}。
正文必须完全脱离视频画面独立成立。实物加工、拆解、产品演示、看图分析、依赖手势或“这个/这里/注意看/你看”等画面指代的来源直接 unusable，不要强行改写。
合格时原始 ASR 是骨架，能不改的原句、语序、段落、反问、口语和第一人称一律不改。只改明显 ASR 错字、标点、口吃与无意义噪声；删除互动引导、身份绑定前情、历史战绩、旧文章和熟人关系。原稿没有“我”绝不能新增第一人称。
不得新增事实、数字、经历、因果、解释、比喻、情绪总结或结论；不设最低字数。不要补风险收束、两面平衡、中性总结、免责声明、通用方法论或编辑旁白。保留原稿已有的单边情绪和判断。不要标题、标签或改动说明。
来源元数据：{meta}
原始 ASR：
{transcript}"""


def enabled() -> bool:
    return os.getenv("XOPS_DOUYIN_DAILY_ENABLED", "false").lower() == "true"


def schedule() -> tuple[int, int]:
    try:
        hour, minute = map(int, os.getenv("XOPS_DOUYIN_DAILY_RUN_TIME", "02:15").split(":", 1))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    return 2, 15


def select_delivery(complete):
    prepared = [post for category in PREP_TARGETS for post in complete[category]]
    ids = [post["source_aweme_id"] for post in prepared]
    counts = {
        category: len({post["source_aweme_id"] for post in complete[category]})
        for category in PREP_TARGETS
    }
    if len(ids) != len(set(ids)):
        raise RuntimeError("合格成稿存在重复 source_aweme_id")
    if any(counts[category] < PREP_TARGETS[category] for category in PREP_TARGETS):
        raise RuntimeError(f"合格成稿不足：{counts}")
    for category in PREP_TARGETS:
        complete[category].sort(key=lambda post: post["play_count"], reverse=True)
    posts = [post for category in DELIVERY_TARGETS for post in complete[category][:DELIVERY_TARGETS[category]]]
    if len(posts) != 60 or len({post["source_aweme_id"] for post in posts}) != 60:
        raise RuntimeError("最终批次必须是 60 个唯一 source_aweme_id")
    return prepared, posts


def validate_assignments(assignments):
    if len(assignments) != 20 or set(assignments.values()) != {3}:
        raise RuntimeError("最终批次必须分配给 20 个人设且每人 3 条")


def retry_ready(run, now):
    return int(run["attempts"] or 0) < MAX_ATTEMPTS and int(run["next_retry_at"] or 0) <= now


def retry_delay(attempts):
    return RETRY_DELAYS[min(max(int(attempts), 1) - 1, len(RETRY_DELAYS) - 1)]


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS douyin_daily_runs (
      batch_date TEXT PRIMARY KEY, status TEXT NOT NULL, fetched INTEGER NOT NULL DEFAULT 0,
      transcribed INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '', started_at INTEGER NOT NULL, completed_at INTEGER,
      attempts INTEGER NOT NULL DEFAULT 0, next_retry_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS douyin_raw_asr (
      source_aweme_id TEXT PRIMARY KEY, batch_date TEXT NOT NULL, category TEXT NOT NULL,
      tags_json TEXT NOT NULL, play_count INTEGER NOT NULL, source_url TEXT NOT NULL,
      audio_path TEXT NOT NULL, transcript TEXT NOT NULL, final_body TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(douyin_daily_runs)")}
    if "attempts" not in columns:
        conn.execute("ALTER TABLE douyin_daily_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if "next_retry_at" not in columns:
        conn.execute("ALTER TABLE douyin_daily_runs ADD COLUMN next_retry_at INTEGER")


async def api(client, token, path, *, body=None, query=None, method="POST"):
    error = None
    for attempt in range(4):
        try:
            response = await client.request(method, BASE + "/" + path, params=query, json=body,
                headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"}, timeout=60)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 200:
                raise RuntimeError(payload.get("message_zh") or str(payload))
            return payload.get("data") or {}
        except (httpx.HTTPError, RuntimeError) as exc:
            error = exc
            if attempt == 3:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise error


async def discover(client, token, used, tags, date_type=3):
    rows = {}
    async def fetch(category, tag):
        try:
            data = await api(client, token, "index/fetch_item_query", query={
                "query": "#" + tag, "category_id": "0", "date_type": date_type,
                "label_type": 0, "duration_type": 0,
            })
        except Exception:
            return category, tag, []
        return category, tag, (data.get("data") or [])[:8]
    results = await asyncio.gather(*(fetch(category, tag) for category, category_tags in tags.items() for tag in category_tags))
    for category, tag, items in results:
        for item in items:
            ident = str(item.get("itemId") or "")
            title = str(item.get("title") or "")
            if not ident or ident in used or not (20 <= int(item.get("duration") or 0) <= 240):
                continue
            if ORG.search(str(item.get("nickname") or "")) or BAD.search(title):
                continue
            row = rows.setdefault(ident, {**item, "tags": [], "category": category})
            row["tags"].append(tag)
            if category == "ai": row["category"] = "ai"
    ids = list(rows)
    stats = {}
    for i in range(0, len(ids), 10):
        data = await api(client, token, "app/v3/fetch_video_statistics", method="GET", query={"aweme_ids": ",".join(ids[i:i + 10])})
        stats.update({str(x.get("aweme_id")): x for x in data.get("statistics_list", [])})
    pools = {key: [] for key in PREP_TARGETS}
    for ident, row in rows.items():
        row["play_count"] = int((stats.get(ident) or {}).get("play_count") or 0)
        if row["category"] == "ai" and not AI_WORDS.search(str(row.get("title") or "")):
            continue
        pools[row["category"]].append(row)
    for pool in pools.values(): pool.sort(key=lambda row: row["play_count"], reverse=True)
    return pools


async def details(client, token, rows):
    result = {}
    for i in range(0, len(rows), 10):
        batch = [row["itemId"] for row in rows[i:i + 10]]
        try:
            data = await api(client, token, "app/v3/fetch_multi_video", body=batch)
            result.update({str(x.get("aweme_id")): x for x in (data.get("aweme_details") or [])})
        except Exception:
            for item_id in batch:
                try:
                    data = await api(client, token, "app/v3/fetch_multi_video", body=[item_id])
                    result.update({str(x.get("aweme_id")): x for x in (data.get("aweme_details") or [])})
                except Exception:
                    continue
    return result


async def download_and_transcribe(client, row, detail, root):
    audio = root / "audio" / f"{row['itemId']}.mp4"
    audio.parent.mkdir(parents=True, exist_ok=True)
    if not audio.exists() or audio.stat().st_size < 100_000:
        url = (((detail.get("video") or {}).get("play_addr") or {}).get("url_list") or [None])[0]
        if not url: return "", ""
        response = await client.get(url, headers={"Referer": "https://www.douyin.com/", "User-Agent": "Mozilla/5.0"}, timeout=180)
        response.raise_for_status(); audio.write_bytes(response.content)
    global WHISPER
    if WHISPER is None:
        from faster_whisper import WhisperModel
        WHISPER = WhisperModel(os.getenv("XOPS_DOUYIN_WHISPER_MODEL", "small"), device="cpu", compute_type="int8", download_root=str(root.parent / "models"))
    segments, _ = WHISPER.transcribe(str(audio), language="zh", vad_filter=True)
    text = "\n".join(s.text.strip() for s in segments if s.text.strip())
    return str(audio), text


async def rewrite(client, row, text, keys, base, model):
    meta = json.dumps({"Tag": row["tags"], "标题": row.get("title", ""), "昵称": row.get("nickname", "")}, ensure_ascii=False)
    for attempt in range(3):
        try:
            response = await client.post(base + "/chat/completions", headers={"Authorization": f"Bearer {keys[hash(row['itemId']) % len(keys)]}", "Content-Type": "application/json"}, json={"model": model, "messages": [{"role": "user", "content": PROMPT.format(meta=meta, transcript=text)}], "temperature": 0.1, "max_tokens": 8192, "response_format": {"type": "json_object"}}, timeout=180)
            response.raise_for_status(); content = response.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"): content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(content); final = str(result.get("final") or "").strip()
            if not result.get("usable") or len(final) <= 20 or VISUAL.search(final) or EDITORIAL.search(final) or DISCLAIM.search(final): return ""
            return final
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
    return ""


async def run(conn, batch_date: str, data_dir: Path, used_ids: set[str]):
    init_db(conn); now = int(time.time())
    conn.execute(
        """INSERT INTO douyin_daily_runs(batch_date,status,started_at,attempts)
           VALUES(?,?,?,1)
           ON CONFLICT(batch_date) DO UPDATE SET
             status='running',error='',started_at=excluded.started_at,completed_at=NULL,
             attempts=douyin_daily_runs.attempts+1,next_retry_at=NULL""",
        (batch_date, "running", now),
    )
    conn.commit()
    token = os.getenv("TIKHUB_TOKEN", "").strip(); keys = [os.getenv(f"XOPS_GEMINI_API_KEY_{i}", "").strip() for i in range(1, 6)]; keys = [k for k in keys if k] or [os.getenv("XOPS_GEMINI_API_KEY", "").strip()]
    if not token or not keys[0]: raise RuntimeError("TikHub 或编辑模型凭证未配置")
    backlog = {key: [] for key in PREP_TARGETS}
    for row in conn.execute(
        """SELECT * FROM douyin_raw_asr
           WHERE final_body<>'' AND (
             source_aweme_id NOT IN (SELECT source_aweme_id FROM douyin_finished_posts)
             OR source_aweme_id IN (SELECT source_aweme_id FROM douyin_finished_posts WHERE batch_date=?)
           )""",
        (batch_date,),
    ):
        if row["category"] not in backlog:
            continue
        backlog[row["category"]].append({
            "source_aweme_id": row["source_aweme_id"], "tag": json.loads(row["tags_json"])[0], "body": row["final_body"],
            "source_url": row["source_url"], "category": row["category"], "tags": json.loads(row["tags_json"]),
            "play_count": row["play_count"], "audio_path": row["audio_path"], "transcript": row["transcript"],
        })
        used_ids.add(str(row["source_aweme_id"]))
    rejected = conn.execute("SELECT source_aweme_id FROM douyin_raw_asr WHERE batch_date=? AND final_body=''", (batch_date,)).fetchall()
    used_ids.update(str(row[0]) for row in rejected)
    root = data_dir / "douyin_daily" / batch_date
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        processed_today = conn.execute("SELECT COUNT(*) FROM douyin_raw_asr WHERE batch_date=?", (batch_date,)).fetchone()[0]
        attempts = conn.execute("SELECT attempts FROM douyin_daily_runs WHERE batch_date=?", (batch_date,)).fetchone()[0]
        phase = LATE_TAGS_2 if attempts >= 6 else LATE_TAGS if attempts >= 5 else FINAL_TAGS if processed_today >= 100 else FALLBACK_TAGS if processed_today >= 60 else SUPPLEMENTAL_TAGS if any(backlog.values()) else TAGS
        tags = {category: phase[category] if len(backlog[category]) < PREP_TARGETS[category] else () for category in PREP_TARGETS}
        pools = await discover(client, token, used_ids, tags, date_type=7 if attempts >= 5 else 3) if any(len(backlog[c]) < PREP_TARGETS[c] for c in PREP_TARGETS) else {c: [] for c in PREP_TARGETS}
        candidates = [row for c in PREP_TARGETS for row in pools[c][:max(4, 2 * (PREP_TARGETS[c] - len(backlog[c])))]]
        raw = await details(client, token, candidates)
        complete = backlog; seen_accounts = Counter()
        for row in candidates:
            try:
                c = row["category"]
                if len(complete[c]) >= PREP_TARGETS[c] or seen_accounts[(c, row.get("nickname", ""))] >= 2: continue
                detail = raw.get(str(row["itemId"]));
                if not detail: continue
                cached = conn.execute("SELECT audio_path,transcript FROM douyin_raw_asr WHERE source_aweme_id=?", (str(row["itemId"]),)).fetchone()
                if cached and cached["transcript"]:
                    audio_path, transcript = cached["audio_path"], cached["transcript"]
                else:
                    audio_path, transcript = await download_and_transcribe(client, row, detail, root)
                if not transcript: continue
                conn.execute("INSERT OR REPLACE INTO douyin_raw_asr(source_aweme_id,batch_date,category,tags_json,play_count,source_url,audio_path,transcript,final_body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (str(row["itemId"]), batch_date, c, json.dumps(row["tags"], ensure_ascii=False), row["play_count"], f"https://www.douyin.com/video/{row['itemId']}", audio_path, transcript, "", now, int(time.time())))
                conn.execute("UPDATE douyin_daily_runs SET fetched=?,transcribed=? WHERE batch_date=?", (len(candidates), conn.execute("SELECT COUNT(*) FROM douyin_raw_asr WHERE batch_date=? AND transcript<>''", (batch_date,)).fetchone()[0], batch_date)); conn.commit()
                final = await rewrite(client, row, transcript, keys, os.getenv("XOPS_GEMINI_BASE_URL", "https://www.micuapi.ai/v1").rstrip("/"), os.getenv("XOPS_GEMINI_MODEL", "gemini-3.1-pro-preview-low"))
                if not final: continue
                seen_accounts[(c, row.get("nickname", ""))] += 1
                complete[c].append({"source_aweme_id": str(row["itemId"]), "tag": row["tags"][0], "body": final, "source_url": f"https://www.douyin.com/video/{row['itemId']}", "category": c, "tags": row["tags"], "play_count": row["play_count"], "audio_path": audio_path, "transcript": transcript})
                conn.execute("UPDATE douyin_raw_asr SET final_body=?,updated_at=? WHERE source_aweme_id=?", (final, int(time.time()), str(row["itemId"]))); conn.commit()
            except Exception:
                continue
    prepared, posts = select_delivery(complete)
    conn.execute("DELETE FROM douyin_finished_posts WHERE batch_date=?", (batch_date,))
    for post in prepared:
        conn.execute("INSERT OR REPLACE INTO douyin_raw_asr(source_aweme_id,batch_date,category,tags_json,play_count,source_url,audio_path,transcript,final_body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (post["source_aweme_id"], batch_date, post["category"], json.dumps(post["tags"],ensure_ascii=False), post["play_count"], post["source_url"], post["audio_path"], post["transcript"], post["body"], now, now))
    for position, post in enumerate(posts, 1):
        conn.execute("INSERT INTO douyin_finished_posts(batch_date,position,tag,body,source_aweme_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (batch_date, position, post["tag"], post["body"], post["source_aweme_id"], post["source_url"], now, now))
    conn.execute("UPDATE douyin_daily_runs SET status='completed',fetched=?,transcribed=?,finished=?,completed_at=?,next_retry_at=NULL WHERE batch_date=?", (len(candidates), len(prepared), len(prepared), int(time.time()), batch_date))
    return posts
