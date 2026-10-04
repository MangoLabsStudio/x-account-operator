from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .collect_viral_priors import (
    FINANCE_SUBREDDITS,
    REDDIT_ACTOR,
    china_relevant_finance_post,
    run_actor,
)


SOURCE_INTERVALS = {
    "x": 24 * 60 * 60,
    "eastmoney_rank": 10 * 60,
    "google_trends": 30 * 60,
    "coingecko": 30 * 60,
    "bilibili": 24 * 60 * 60,
    "reddit": 24 * 60 * 60,
    "douyin": 24 * 60 * 60,
}
MIN_EXPECTED_ITEMS = {
    "x": 1,
    "eastmoney_rank": 50,
    "google_trends": 0,
    "coingecko": 5,
    "bilibili": 5,
    "reddit": 5,
    "douyin": 5,
}
DEFAULT_SOURCES = tuple(SOURCE_INTERVALS)
BILIBILI_ACTOR = "zhorex~bilibili-scraper"
DOUYIN_ACTOR = "sian.agency~douyin-scraper"
DOUYIN_TRANSCRIPT_ACTOR = "apple_yang~douyin-transcripts-scraper"
DOUYIN_KEYWORDS = ("财经", "股票", "基金", "黄金", "比特币")
GOOGLE_FINANCE_TERMS = (
    "stock", "market", "bitcoin", "crypto", "ethereum", "solana", "fed", "rate",
    "inflation", "gold", "oil", "bank", "economy", "dollar", "tariff", "earnings",
    "mortgage", "recession", "股", "基金", "黄金", "原油", "银行", "央行", "降息",
    "美联储", "房价", "比特币", "加密", "债", "汇率", "人民币", "财经", "经济",
    "通胀", "就业", "关税",
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/125 Safari/537.36"
)
INSTITUTION_MARKERS = (
    "日报", "时报", "新闻", "证券报", "电视台", "广播", "官方", "研究院", "交易所",
    "委员会", "政府", "证券公司", "基金公司", "official", " news", "media", "reuters",
    "bloomberg", "financial times", "wall street journal", "coindesk", "cointelegraph",
    "binance", "coinbase", "krakenfx", "okx", "bybit", "decryptmedia", "theblock",
    "cnbc", "marketwatch", "yahoofinance", "forbes", "businessinsider", "techcrunch",
)
FINANCE_RELEVANCE_TERMS = (
    "bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "token", "stablecoin",
    "defi", "onchain", "airdrop", "airdrops", "stock", "stocks", "equity", "equities",
    "market", "markets", "trade", "trading", "invest", "investing", "investment", "investor",
    "portfolio", "option", "options", "futures", "earnings", "revenue", "valuation", "funding",
    "interest rate", "inflation", "fed ", "bank", "bond", "treasury", "gold", "oil",
    "economy", "tariff", "mortgage", "income", "wealth", "profit", "loss", "liquidity",
    "比特币", "以太坊", "索拉纳", "加密", "代币", "稳定币", "链上", "空投", "币圈",
    "股票", "股市", "美股", "港股", "a股", "基金", "债券", "国债", "黄金", "原油",
    "投资", "交易", "仓位", "期权", "合约", "财报", "估值", "融资", "利率", "降息",
    "加息", "通胀", "美联储", "央行", "银行", "经济", "关税", "房价", "收入", "财富",
    "赚钱", "亏损", "利润", "流动性", "牛市", "熊市",
)
PERSONAL_VOICE_PATTERN = re.compile(
    r"(^|\W)(i|i['’]?m|i['’]?ve|my|me|mine|imo|think|believe)(\W|$)"
    r"|我|我的|个人|觉得|认为|看来|复盘",
    re.I,
)


def now_ts() -> int:
    return int(time.time())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def time_bucket(seconds: int) -> str:
    return str(now_ts() // seconds)


def published_timestamp(value) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 10_000_000_000 else number
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return published_timestamp(int(text))
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        try:
            return int(parsedate_to_datetime(text).timestamp())
        except (TypeError, ValueError):
            return 0


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS retail_source_runs (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                fetched INTEGER NOT NULL DEFAULT 0,
                stored INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                started_at INTEGER NOT NULL,
                completed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_retail_source_runs_source
            ON retail_source_runs(source, started_at DESC);
            CREATE TABLE IF NOT EXISTS retail_source_state (
                source TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_run_id INTEGER,
                last_success_at INTEGER,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retail_items (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                pool TEXT NOT NULL DEFAULT 'raw',
                item_type TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                community TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                published_ts INTEGER NOT NULL DEFAULT 0,
                attention_score REAL NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                raw_json TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                PRIMARY KEY(source, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_retail_items_recent
            ON retail_items(last_seen_at DESC, attention_score DESC);
            CREATE INDEX IF NOT EXISTS idx_retail_items_source_recent
            ON retail_items(source, last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS retail_item_observations (
                id INTEGER PRIMARY KEY,
                run_id INTEGER REFERENCES retail_source_runs(id),
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                attention_score REAL NOT NULL,
                metrics_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                UNIQUE(run_id, source, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_retail_observations_item
            ON retail_item_observations(source, external_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS apify_runs(
                input_key TEXT PRIMARY KEY, actor TEXT NOT NULL, input_json TEXT NOT NULL,
                run_id TEXT, dataset_id TEXT, status TEXT NOT NULL, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(retail_items)")}
        if "entity_id" not in columns:
            conn.execute("ALTER TABLE retail_items ADD COLUMN entity_id TEXT NOT NULL DEFAULT ''")
        if "pool" not in columns:
            conn.execute("ALTER TABLE retail_items ADD COLUMN pool TEXT NOT NULL DEFAULT 'raw'")
        if "published_ts" not in columns:
            conn.execute("ALTER TABLE retail_items ADD COLUMN published_ts INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_retail_items_pool_recent
               ON retail_items(pool, published_ts DESC, attention_score DESC)"""
        )


def configured_sources() -> tuple[str, ...]:
    raw = os.getenv("XOPS_RETAIL_ATTENTION_SOURCES", "").strip()
    if not raw:
        return DEFAULT_SOURCES
    sources = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = [source for source in sources if source not in SOURCE_INTERVALS]
    if unknown:
        raise RuntimeError(f"未知散户注意力来源: {', '.join(unknown)}")
    return sources


def enabled() -> bool:
    return os.getenv("XOPS_RETAIL_ATTENTION_ENABLED", "false").lower() == "true"


def due_sources(db_path: Path, sources: tuple[str, ...] | None = None) -> list[str]:
    init_db(db_path)
    current = now_ts()
    with connect(db_path) as conn:
        rows = {
            row[0]: {
                "last_success_at": row[1], "status": row[2],
                "failures": row[3], "updated_at": row[4],
            }
            for row in conn.execute(
                """SELECT source,last_success_at,status,consecutive_failures,updated_at
                   FROM retail_source_state"""
            ).fetchall()
        }
    due = []
    for source in sources or configured_sources():
        state = rows.get(source)
        if not state:
            due.append(source)
            continue
        if state["status"] == "failed":
            retry_after = min(3600, 60 * (2 ** min(int(state["failures"]), 6)))
            if current - int(state["updated_at"]) >= retry_after:
                due.append(source)
            continue
        if not state["last_success_at"] or current - int(state["last_success_at"]) >= SOURCE_INTERVALS[source]:
            due.append(source)
    return due


async def get_json(client: httpx.AsyncClient, url: str, **kwargs) -> dict:
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} 未返回 JSON 对象")
    return payload


def traffic_number(value: str) -> int:
    match = re.search(r"([\d,.]+)\s*([KMB万]?)", value or "", re.I)
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "万": 10_000}
    return int(number * scale.get(match.group(2).upper(), 1))


def item(
    source: str,
    external_id: str,
    *,
    item_type: str,
    title: str,
    raw: dict,
    entity_id: str = "",
    pool: str = "raw",
    url: str = "",
    body: str = "",
    author: str = "",
    community: str = "",
    language: str = "",
    published_at: str = "",
    attention_score: float = 0,
    metrics: dict | None = None,
) -> dict:
    return {
        "source": source,
        "external_id": external_id,
        "entity_id": entity_id or external_id,
        "pool": pool,
        "item_type": item_type,
        "url": url,
        "title": title,
        "body": body,
        "author": author,
        "community": community,
        "language": language,
        "published_at": published_at,
        "attention_score": round(float(attention_score), 4),
        "metrics": metrics or {},
        "raw": raw,
    }


async def fetch_eastmoney_rank(client: httpx.AsyncClient) -> list[dict]:
    response = await client.post(
        "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
        json={
            "appId": "appId01",
            "globalId": "786e4c21-70dc-435a-93bb-38",
            "marketType": "",
            "pageNo": 1,
            "pageSize": 100,
        },
    )
    response.raise_for_status()
    rows = response.json().get("data") or []
    result = []
    for row in rows:
        code = str(row.get("sc") or "")
        rank = int(row.get("rk") or 0)
        if not code or not rank:
            continue
        result.append(item(
            "eastmoney_rank", f"{code}:{time_bucket(600)}", entity_id=code,
            pool="hot_signal", item_type="attention_signal",
            title=f"东方财富人气榜 #{rank} {code}",
            url=f"https://guba.eastmoney.com/rank/stock?code={code[2:]}",
            language="zh", published_at=now_iso(),
            attention_score=max(1, 101 - rank), metrics={"rank": rank}, raw=row,
        ))
    return result


async def fetch_coingecko(client: httpx.AsyncClient) -> list[dict]:
    payload = await get_json(client, "https://api.coingecko.com/api/v3/search/trending")
    result = []
    for index, wrapper in enumerate(payload.get("coins") or [], 1):
        row = wrapper.get("item") or {}
        external_id = str(row.get("id") or "")
        if not external_id:
            continue
        data = row.get("data") or {}
        result.append(item(
            "coingecko", f"{external_id}:{time_bucket(1800)}", entity_id=external_id,
            pool="hot_signal", item_type="attention_signal",
            title=f"CoinGecko Trending #{index} {row.get('name') or external_id}",
            url=f"https://www.coingecko.com/en/coins/{external_id}", language="en",
            published_at=now_iso(), attention_score=max(1, 21 - index),
            metrics={
                "rank": index,
                "symbol": row.get("symbol"),
                "market_cap_rank": row.get("market_cap_rank"),
                "price": data.get("price"),
            },
            raw=row,
        ))
    return result


async def fetch_google_trends(client: httpx.AsyncClient) -> list[dict]:
    namespace = {"ht": "https://trends.google.com/trending/rss"}
    result = []
    for geo in ("HK", "TW", "US"):
        response = await client.get(f"https://trends.google.com/trending/rss?geo={geo}")
        response.raise_for_status()
        root = ET.fromstring(response.text)
        for node in root.findall("./channel/item"):
            title = (node.findtext("title") or "").strip()
            news = [
                (child.findtext("ht:news_item_title", default="", namespaces=namespace) or "").strip()
                for child in node.findall("ht:news_item", namespace)
            ]
            searchable = " ".join([title, *news]).lower()
            if not any(term.lower() in searchable for term in GOOGLE_FINANCE_TERMS):
                continue
            published = (node.findtext("pubDate") or "").strip()
            traffic_text = node.findtext("ht:approx_traffic", default="", namespaces=namespace) or ""
            traffic = traffic_number(traffic_text)
            external_id = hashlib.sha256(f"{geo}\n{title}\n{published}".encode()).hexdigest()[:24]
            result.append(item(
                "google_trends", external_id, entity_id=title.lower(), pool="hot_signal",
                item_type="search_trend", title=title,
                url=f"https://trends.google.com/trending?geo={geo}", body="\n".join(news),
                community=geo, language="zh" if geo in {"HK", "TW"} else "en",
                published_at=published, attention_score=max(1, traffic),
                metrics={"approx_traffic": traffic, "geo": geo},
                raw={"title": title, "news": news, "published_at": published, "geo": geo},
            ))
    return result


def bilibili_items(db_path: Path) -> list[dict]:
    max_results = max(5, min(int(os.getenv("XOPS_BILIBILI_MAX_RESULTS", "30")), 100))
    begin = datetime.now(timezone.utc).date().isoformat()
    with connect(db_path) as conn:
        rows = run_actor(
            conn,
            None,
            BILIBILI_ACTOR,
            {
                "mode": "search",
                "searchQuery": "财经",
                "searchAliases": ["股票", "投资理财", "基金", "黄金", "比特币"],
                "sortOrder": "click",
                "pubtimeBegin": begin,
                "maxResults": max_results,
                "includeComments": False,
                "deltaMode": True,
                "deltaStateKey": "xops-retail-finance-v1",
            },
            max_results * 0.021,
            cache_bucket=datetime.now(timezone.utc).date().isoformat(),
        )
    result = []
    for row in rows:
        if row.get("type") != "video":
            continue
        bvid = str(row.get("bvid") or "")
        if not bvid:
            continue
        views = int(row.get("viewCount") or 0)
        likes = int(row.get("likeCount") or 0)
        comments = int(row.get("replyCount") or 0)
        favorites = int(row.get("favoriteCount") or 0)
        shares = int(row.get("shareCount") or 0)
        result.append(item(
            "bilibili", bvid, entity_id=bvid, pool="viral_prior",
            item_type="video", title=str(row.get("title") or ""),
            url=str(row.get("url") or f"https://www.bilibili.com/video/{bvid}"),
            body=str(row.get("description") or ""), author=str(row.get("authorName") or ""),
            community=str(row.get("category") or "财经"), language="zh",
            published_at=str(row.get("publishDate") or ""),
            attention_score=views * 0.01 + likes + comments * 2 + shares * 3 + favorites * 0.2,
            metrics={
                "views": views, "likes": likes, "comments": comments,
                "shares": shares, "favorites": favorites, "danmaku": int(row.get("danmakuCount") or 0),
            },
            raw=row,
        ))
    return result


def reddit_items(db_path: Path) -> list[dict]:
    with connect(db_path) as conn:
        raw = run_actor(
            conn,
            None,
            REDDIT_ACTOR,
            {
                "subreddits": FINANCE_SUBREDDITS,
                "maxPostsPerSubreddit": 12,
                "sort": "top",
                "timeFilter": "week",
                "includeComments": False,
            },
            0.28,
            cache_bucket=datetime.now(timezone.utc).date().isoformat(),
        )
    result = []
    for row in raw:
        if not china_relevant_finance_post(row):
            continue
        external_id = str(row.get("id") or "").removeprefix("t3_")
        url = str(row.get("permalink") or row.get("full_link") or row.get("url") or "")
        if url.startswith("/"):
            url = "https://www.reddit.com" + url
        score = int(row.get("score") or 0)
        comments = int(row.get("num_comments") or 0)
        result.append(item(
            "reddit", external_id, entity_id=external_id, pool="viral_prior",
            item_type="personal_post",
            title=str(row.get("title") or ""), body=str(row.get("selftext") or ""),
            url=url, author=str(row.get("author") or ""),
            community=str(row.get("_subreddit") or row.get("subreddit") or ""), language="en",
            published_at=str(row.get("createdAt") or ""), attention_score=score + comments * 2,
            metrics={"score": score, "comments": comments, "upvote_ratio": row.get("upvote_ratio")},
            raw=row,
        ))
    return result


def douyin_items(db_path: Path) -> list[dict]:
    items_by_id = {}
    keywords = tuple(
        part.strip()
        for part in os.getenv("XOPS_DOUYIN_KEYWORDS", ",".join(DOUYIN_KEYWORDS)).split(",")
        if part.strip()
    )
    with connect(db_path) as conn:
        for keyword in keywords:
            rows = run_actor(
                conn,
                None,
                DOUYIN_ACTOR,
                {"operation": "searchVideo", "keyword": keyword, "maxPages": 1},
                0.12,
                cache_bucket=datetime.now(timezone.utc).date().isoformat(),
            )
            for row in rows:
                external_id = str(row.get("aweme_id") or row.get("awemeId") or row.get("videoId") or "")
                if not external_id:
                    continue
                views = int(row.get("play_count") or row.get("playCount") or 0)
                likes = int(row.get("digg_count") or row.get("diggCount") or row.get("likeCount") or 0)
                comments = int(row.get("comment_count") or row.get("commentCount") or 0)
                shares = int(row.get("share_count") or row.get("shareCount") or 0)
                collects = int(row.get("collect_count") or row.get("collectCount") or 0)
                text = str(row.get("desc") or row.get("caption") or "")
                items_by_id[external_id] = item(
                    "douyin", external_id, entity_id=external_id, pool="viral_prior",
                    item_type="video", title=text, body=text,
                    url=str(row.get("url") or row.get("share_url") or row.get("videoPageUrl") or f"https://www.douyin.com/video/{external_id}"),
                    author=str(row.get("author_nickname") or row.get("authorName") or row.get("userName") or ""),
                    community=keyword, language="zh",
                    published_at=str(row.get("create_time") or row.get("createTime") or row.get("postedAt") or ""),
                    attention_score=views * 0.005 + likes + comments * 2 + shares * 3 + collects * 0.2,
                    metrics={
                        "views": views, "likes": likes, "comments": comments,
                        "shares": shares, "collects": collects,
                        "transcript_status": "not_selected",
                    },
                    raw=row,
                )

    result = list(items_by_id.values())
    max_transcripts = max(0, min(int(os.getenv("XOPS_DOUYIN_TRANSCRIPT_MAX_RESULTS", "10")), 30))
    cutoff = now_ts() - 7 * 24 * 60 * 60
    current = [
        row for row in result
        if not published_timestamp(row["published_at"])
        or published_timestamp(row["published_at"]) >= cutoff
    ]
    selected = sorted(current, key=lambda row: row["attention_score"], reverse=True)[:max_transcripts]

    def transcribe(row: dict) -> tuple[str, dict]:
        with connect(db_path) as conn:
            rows = run_actor(
                conn,
                None,
                DOUYIN_TRANSCRIPT_ACTOR,
                {"videoUrl": f"https://www.douyin.com/video/{row['external_id']}"},
                float(os.getenv("XOPS_DOUYIN_TRANSCRIPT_MAX_CHARGE_USD", "0.08")),
            )
        transcript = rows[0] if rows else {}
        text = str(transcript.get("text") or "").strip()
        return row["external_id"], {
            "status": "succeeded" if text else "empty",
            "text": text,
            "segments": transcript.get("segments") or [],
            "duration": transcript.get("duration") or 0,
            "error": str(transcript.get("errMsg") or ""),
        }

    workers = min(max(1, int(os.getenv("XOPS_DOUYIN_TRANSCRIPT_CONCURRENCY", "3"))), len(selected))
    if selected:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(transcribe, row): row for row in selected}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    _, transcript = future.result()
                except Exception as error:
                    transcript = {
                        "status": "failed", "text": "", "segments": [],
                        "duration": 0, "error": str(error)[:500],
                    }
                if transcript["text"]:
                    row["body"] = transcript["text"]
                row["metrics"].update({
                    "transcript_status": transcript["status"],
                    "transcript_chars": len(transcript["text"]),
                    "duration_seconds": transcript["duration"],
                })
                row["raw"] = {**row["raw"], "_transcript": transcript}
    return result


def integer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def finance_relevant(text: str) -> bool:
    value = text.lower()
    words = set(re.findall(r"[a-z0-9$]+", value))
    return any(
        term in value if not term.isascii() or " " in term else term in words
        for term in FINANCE_RELEVANCE_TERMS
    )


def x_items(db_path: Path) -> list[dict]:
    source_db = db_path.parent / "market_source_posts.sqlite3"
    if not source_db.exists():
        raise RuntimeError(f"X 母池数据库不存在: {source_db}")
    hours = max(1, int(os.getenv("XOPS_X_VIRAL_HOURS", "72")))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT post_id,handle,text,created_at,url,metrics
               FROM source_posts
               WHERE created_at>=? AND is_reply=0 AND is_retweet=0
               ORDER BY created_at DESC""",
            (cutoff,),
        ).fetchall()
    result = []
    for stored in rows:
        row = dict(stored)
        if (
            looks_institutional(row["handle"])
            or not finance_relevant(row["text"])
            or not PERSONAL_VOICE_PATTERN.search(row["text"])
        ):
            continue
        metrics = json.loads(row["metrics"] or "{}")
        views = integer(metrics.get("view_count"))
        likes = integer(metrics.get("favorite_count"))
        replies = integer(metrics.get("reply_count"))
        reposts = integer(metrics.get("retweet_count"))
        quotes = integer(metrics.get("quote_count"))
        bookmarks = integer(metrics.get("bookmark_count"))
        result.append(item(
            "x", row["post_id"], entity_id=row["post_id"], pool="viral_prior",
            item_type="personal_post", title=row["text"], body=row["text"],
            url=row["url"], author=row["handle"], community="x", language="",
            published_at=row["created_at"],
            attention_score=(
                views * 0.001 + likes + replies * 1.5 + reposts * 2
                + quotes * 2 + bookmarks * 0.5
            ),
            metrics={
                "views": views, "likes": likes, "replies": replies,
                "reposts": reposts, "quotes": quotes, "bookmarks": bookmarks,
            },
            raw=row,
        ))
    result.sort(key=lambda row: row["attention_score"], reverse=True)
    limit = max(1, min(int(os.getenv("XOPS_X_VIRAL_MAX_RESULTS", "1000")), 5000))
    return result[:limit]


FETCHERS = {
    "eastmoney_rank": fetch_eastmoney_rank,
    "google_trends": fetch_google_trends,
    "coingecko": fetch_coingecko,
}


def store_items(db_path: Path, items: list[dict], run_id: int | None = None) -> int:
    current = now_ts()
    with connect(db_path) as conn:
        for row in items:
            conn.execute(
                """INSERT INTO retail_items(
                       source,external_id,entity_id,pool,item_type,url,title,body,author,community,
                       language,published_at,published_ts,attention_score,metrics_json,raw_json,
                       first_seen_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source,external_id) DO UPDATE SET
                       entity_id=excluded.entity_id,pool=excluded.pool,item_type=excluded.item_type,
                       url=excluded.url,title=excluded.title,
                       body=excluded.body,author=excluded.author,community=excluded.community,
                       language=excluded.language,published_at=excluded.published_at,
                       published_ts=excluded.published_ts,
                       attention_score=excluded.attention_score,metrics_json=excluded.metrics_json,
                       raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at""",
                (
                    row["source"], row["external_id"], row.get("entity_id") or row["external_id"],
                    row.get("pool") or "raw", row["item_type"], row["url"], row["title"],
                    row["body"], row["author"], row["community"], row["language"], row["published_at"],
                    published_timestamp(row["published_at"]),
                    row["attention_score"], json.dumps(row["metrics"], ensure_ascii=False),
                    json.dumps(row["raw"], ensure_ascii=False), current, current,
                ),
            )
            if run_id is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO retail_item_observations(
                           run_id,source,external_id,observed_at,attention_score,metrics_json,raw_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        run_id, row["source"], row["external_id"], current,
                        row["attention_score"], json.dumps(row["metrics"], ensure_ascii=False),
                        json.dumps(row["raw"], ensure_ascii=False),
                    ),
                )
    return len({(row["source"], row["external_id"]) for row in items})


def start_run(db_path: Path, source: str, trigger: str) -> int:
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO retail_source_runs(source,trigger,status,started_at) VALUES(?,?,?,?)",
            (source, trigger, "running", now_ts()),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO retail_source_state(source,status,last_run_id,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET
               status=excluded.status,last_run_id=excluded.last_run_id,updated_at=excluded.updated_at""",
            (source, "running", run_id, now_ts()),
        )
    return run_id


def finish_run(db_path: Path, run_id: int, source: str, items: list[dict]) -> dict:
    stored = store_items(db_path, items, run_id)
    completed = now_ts()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE retail_source_runs SET status='succeeded',fetched=?,stored=?,completed_at=? WHERE id=?",
            (len(items), stored, completed, run_id),
        )
        conn.execute(
            """UPDATE retail_source_state SET status='succeeded',last_success_at=?,
               consecutive_failures=0,last_error='',updated_at=? WHERE source=?""",
            (completed, completed, source),
        )
    return {"run_id": run_id, "source": source, "status": "succeeded", "fetched": len(items), "stored": stored}


def fail_run(db_path: Path, run_id: int, source: str, error: Exception) -> dict:
    message = str(error)[:1000]
    completed = now_ts()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE retail_source_runs SET status='failed',error=?,completed_at=? WHERE id=?",
            (message, completed, run_id),
        )
        conn.execute(
            """UPDATE retail_source_state SET status='failed',
               consecutive_failures=consecutive_failures+1,last_error=?,updated_at=? WHERE source=?""",
            (message, completed, source),
        )
    return {"run_id": run_id, "source": source, "status": "failed", "error": message}


async def collect_source(db_path: Path, source: str, trigger: str = "manual") -> dict:
    init_db(db_path)
    run_id = start_run(db_path, source, trigger)
    try:
        if source == "x":
            rows = await asyncio.to_thread(x_items, db_path)
        elif source == "reddit":
            rows = await asyncio.to_thread(reddit_items, db_path)
        elif source == "douyin":
            rows = await asyncio.to_thread(douyin_items, db_path)
        elif source == "bilibili":
            rows = await asyncio.to_thread(bilibili_items, db_path)
        else:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"},
            ) as client:
                rows = await FETCHERS[source](client)
        if len(rows) < MIN_EXPECTED_ITEMS[source]:
            raise RuntimeError(
                f"{source} 返回 {len(rows)} 条，低于最低完整性门槛 {MIN_EXPECTED_ITEMS[source]}"
            )
        return finish_run(db_path, run_id, source, rows)
    except Exception as error:
        return fail_run(db_path, run_id, source, error)


async def collect(db_path: Path, sources: list[str] | None = None, trigger: str = "manual") -> dict:
    selected = list(dict.fromkeys(sources or configured_sources()))
    unknown = [source for source in selected if source not in SOURCE_INTERVALS]
    if unknown:
        raise ValueError(f"未知散户注意力来源: {', '.join(unknown)}")
    semaphore = asyncio.Semaphore(max(1, int(os.getenv("XOPS_RETAIL_ATTENTION_CONCURRENCY", "3"))))

    async def one(source: str) -> dict:
        async with semaphore:
            return await collect_source(db_path, source, trigger)

    results = await asyncio.gather(*(one(source) for source in selected))
    return {
        "status": "succeeded" if all(row["status"] == "succeeded" for row in results) else "partial",
        "sources": results,
        "succeeded": sum(row["status"] == "succeeded" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
    }


def recover_interrupted(db_path: Path) -> None:
    init_db(db_path)
    current = now_ts()
    with connect(db_path) as conn:
        rows = conn.execute("SELECT id,source FROM retail_source_runs WHERE status='running'").fetchall()
        for run_id, source in rows:
            message = "服务重启前抓取中断，可安全重跑"
            conn.execute(
                "UPDATE retail_source_runs SET status='failed',error=?,completed_at=? WHERE id=?",
                (message, current, run_id),
            )
            conn.execute(
                """UPDATE retail_source_state SET status='failed',
                   consecutive_failures=consecutive_failures+1,last_error=?,updated_at=? WHERE source=?""",
                (message, current, source),
            )


def source_status(db_path: Path) -> list[dict]:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        states = {row["source"]: dict(row) for row in conn.execute("SELECT * FROM retail_source_state")}
        counts = {
            row["source"]: row["count"]
            for row in conn.execute("SELECT source,COUNT(*) count FROM retail_items GROUP BY source")
        }
    result = []
    for source in configured_sources():
        state = states.get(source, {})
        result.append({
            "source": source,
            "enabled": True,
            "interval_seconds": SOURCE_INTERVALS[source],
            "items": counts.get(source, 0),
            "status": state.get("status", "never_run"),
            "last_run_id": state.get("last_run_id"),
            "last_success_at": state.get("last_success_at"),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "last_error": state.get("last_error", ""),
        })
    result.extend([
        {"source": "wechat", "enabled": False, "status": "authorization_required", "items": 0},
        {"source": "wechat_channels", "enabled": False, "status": "authorization_required", "items": 0},
        {"source": "stocktwits", "enabled": False, "status": "cloudflare_blocked", "items": 0},
        {"source": "xueqiu", "enabled": False, "status": "session_required", "items": 0},
        {"source": "tradingview", "enabled": False, "status": "browser_adapter_required", "items": 0},
    ])
    return result


def list_runs(db_path: Path, source: str = "", limit: int = 50) -> list[dict]:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if source:
            rows = conn.execute(
                "SELECT * FROM retail_source_runs WHERE source=? ORDER BY id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM retail_source_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def list_items(
    db_path: Path,
    *,
    source: str = "",
    item_type: str = "",
    hours: int = 168,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    init_db(db_path)
    conditions = ["last_seen_at>=?"]
    params: list[object] = [now_ts() - max(1, hours) * 3600]
    if source:
        conditions.append("source=?")
        params.append(source)
    if item_type:
        conditions.append("item_type=?")
        params.append(item_type)
    where = " AND ".join(conditions)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(f"SELECT COUNT(*) FROM retail_items WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT source,external_id,item_type,url,title,body,author,community,language,
                       published_at,attention_score,metrics_json,first_seen_at,last_seen_at
                FROM retail_items WHERE {where}
                ORDER BY attention_score DESC,last_seen_at DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        data = dict(row)
        data["metrics"] = json.loads(data.pop("metrics_json"))
        items.append(data)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def looks_institutional(author: str) -> bool:
    value = f" {author.strip().lower()} "
    return bool(value.strip()) and any(marker in value for marker in INSTITUTION_MARKERS)


def normalize_scores(items: list[dict]) -> None:
    by_source: dict[str, list[dict]] = {}
    for row in items:
        by_source.setdefault(row["source"], []).append(row)
    for rows in by_source.values():
        ordered = sorted(rows, key=lambda row: row["raw_score"], reverse=True)
        total = len(ordered)
        for index, row in enumerate(ordered):
            score = 100 if total == 1 else 100 * (total - index - 1) / (total - 1)
            row["normalized_score"] = round(score, 1)


def list_pool(db_path: Path, pool: str, *, hours: int = 72, limit: int = 100) -> dict:
    if pool not in {"hot_signal", "viral_prior"}:
        raise ValueError(f"未知内容池: {pool}")
    init_db(db_path)
    cutoff = now_ts() - max(1, hours) * 3600
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT source,external_id,entity_id,item_type,url,title,body,author,community,
                      language,published_at,published_ts,attention_score,metrics_json,
                      first_seen_at,last_seen_at
               FROM retail_items
               WHERE pool=? AND COALESCE(NULLIF(published_ts,0),last_seen_at)>=?
               ORDER BY COALESCE(NULLIF(published_ts,0),last_seen_at) DESC,last_seen_at DESC""",
            (pool, cutoff),
        ).fetchall()
    items = []
    latest: dict[tuple[str, str], dict] = {}
    for stored in rows:
        row = dict(stored)
        if pool == "viral_prior":
            if looks_institutional(row["author"]):
                continue
            if row["source"] == "x" and (
                not finance_relevant(row["body"])
                or not PERSONAL_VOICE_PATTERN.search(row["body"])
            ):
                continue
        row["metrics"] = json.loads(row.pop("metrics_json"))
        row["raw_score"] = row.pop("attention_score")
        if pool == "hot_signal":
            key = (row["source"], row["entity_id"])
            if key in latest:
                continue
            latest[key] = row
        items.append(row)
    normalize_scores(items)
    items.sort(
        key=lambda row: (
            row["normalized_score"], row["published_ts"], row["last_seen_at"]
        ),
        reverse=True,
    )
    return {"pool": pool, "hours": hours, "total": len(items), "items": items[:limit]}


def list_observations(
    db_path: Path, *, source: str = "", external_id: str = "", limit: int = 100
) -> list[dict]:
    init_db(db_path)
    conditions = []
    params: list[object] = []
    if source:
        conditions.append("source=?")
        params.append(source)
    if external_id:
        conditions.append("external_id=?")
        params.append(external_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT id,run_id,source,external_id,observed_at,attention_score,
                       metrics_json,raw_json
                FROM retail_item_observations {where}
                ORDER BY id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()
    result = []
    for stored in rows:
        row = dict(stored)
        row["metrics"] = json.loads(row.pop("metrics_json"))
        row["raw"] = json.loads(row.pop("raw_json"))
        result.append(row)
    return result
