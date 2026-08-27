import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from unittest.mock import patch

import market_sources.collect_big_source_posts as collector
import pytest
from market_sources.collect_big_source_posts import (
    MOTHER_POOL_PATH,
    _mother_pool_path,
    collect,
    fetch_account,
    init_db,
    load_accounts,
    parse_posts,
)
from market_sources.cross_validate_source_posts import cross_validate


SINCE = datetime(2026, 8, 23, tzinfo=timezone.utc)
ACCOUNT = {"user_id": "42", "handle": "configured_handle", "source_lists": ["mother-pool"]}
CREATED_AT = "Sun Aug 24 01:30:00 +0000 2026"


def tweet(post_id, author_id="42", *, note_text=None, full_text="legacy full text", text="legacy text"):
    item = {
        "__typename": "Tweet",
        "rest_id": post_id,
        "core": {"user_results": {"result": {"rest_id": author_id, "core": {"screen_name": "source_handle"}}}},
        "legacy": {"created_at": CREATED_AT, "full_text": full_text, "text": text},
    }
    if note_text is not None:
        item["note_tweet"] = {"note_tweet_results": {"result": {"text": note_text}}}
    return item


def test_parse_posts_prefers_note_tweet_and_keeps_original_post_reference():
    posts = parse_posts({"data": [tweet("123", note_text="full note tweet", full_text="truncated legacy")]}, ACCOUNT, SINCE)

    assert len(posts) == 1
    assert posts[0]["post_id"] == "123"
    assert posts[0]["author_id"] == "42"
    assert posts[0]["handle"] == "source_handle"
    assert posts[0]["text"] == "full note tweet"
    assert posts[0]["created_at"] == "2026-08-24T01:30:00+00:00"
    assert posts[0]["url"] == "https://x.com/source_handle/status/123"
    assert not posts[0]["is_reply"]
    assert not posts[0]["is_retweet"]
    assert not posts[0]["is_quote"]
    assert posts[0]["metrics"] == {}


def test_parse_posts_falls_back_to_legacy_and_filters_other_authors():
    payload = {
        "data": [
            tweet("124", full_text="legacy full text", text="legacy text"),
            tweet("125", author_id="someone-else", note_text="must not be collected"),
        ]
    }

    posts = parse_posts(payload, ACCOUNT, SINCE)

    assert len(posts) == 1
    assert posts[0]["post_id"] == "124"
    assert posts[0]["text"] == "legacy full text"
    assert posts[0]["handle"] == "source_handle"
    assert posts[0]["url"] == "https://x.com/source_handle/status/124"


def test_init_db_preserves_existing_public_post_reference():
    with sqlite3.connect(":memory:") as db:
        init_db(db)
        db.execute(
            """INSERT INTO source_posts(
                post_id,author_id,handle,text,created_at,url,is_reply,source_lists
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("126", "anon_author", "source_handle", "original", "2026-08-24T01:30:00+00:00",
             "https://x.com/source_handle/status/126", 0, '["mother-pool"]'),
        )
        init_db(db)

        assert db.execute(
            "SELECT post_id, handle, url FROM source_posts WHERE post_id='126'"
        ).fetchone() == ("126", "source_handle", "https://x.com/source_handle/status/126")


def test_parse_posts_keeps_reply_repost_quote_and_metrics():
    item = tweet("127", full_text="RT @other source text")
    item["legacy"].update({
        "in_reply_to_status_id_str": "1",
        "retweeted_status_result": {"result": {}},
        "is_quote_status": True,
        "reply_count": 2,
        "retweet_count": 3,
        "favorite_count": 4,
        "quote_count": 5,
    })
    item["views"] = {"count": "6"}
    post = parse_posts({"data": [item]}, ACCOUNT, SINCE)[0]
    assert post["is_reply"]
    assert post["is_retweet"]
    assert post["is_quote"]
    assert post["metrics"] == {
        "reply_count": 2, "retweet_count": 3, "favorite_count": 4,
        "quote_count": 5, "view_count": "6",
    }


def test_fetch_account_uses_bottom_cursor_until_a_post_is_older_than_watermark(monkeypatch):
    first = {"result": {"data": [tweet("128")]}, "cursor": {"bottom": "next"}}
    old = tweet("129")
    old["legacy"]["created_at"] = "Sat Aug 22 23:59:00 +0000 2026"
    second = {"result": {"data": [old]}, "cursor": {"bottom": "later"}}
    calls = []

    def fake_page(_key, _account, cursor=None, count=20):
        calls.append(cursor)
        return first if cursor is None else second

    monkeypatch.setattr("market_sources.collect_big_source_posts.fetch_page", fake_page)
    result = fetch_account(
        "runtime-key", ACCOUNT, since=SINCE,
        watermark=SINCE, run_started_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert calls == [None, "next"]
    assert result["pages_fetched"] == 2
    assert [post["post_id"] for post in result["posts"]] == ["128"]


def test_rejects_non_mother_pool_path(tmp_path):
    assert _mother_pool_path(MOTHER_POOL_PATH) == MOTHER_POOL_PATH
    try:
        _mother_pool_path(tmp_path / "other.json")
    except ValueError as error:
        assert "唯一总信息源母池" in str(error)
    else:
        raise AssertionError("expected a forced mother-pool path error")


def test_accepts_only_configured_ai_pool_path(monkeypatch, tmp_path):
    ai_pool = tmp_path / "ai_content_source_accounts.json"
    ai_pool.write_text(json.dumps([ACCOUNT]), encoding="utf-8")
    monkeypatch.setattr(collector, "AI_POOL_PATH", ai_pool)

    assert load_accounts(topic_domain="ai") == [ACCOUNT]
    with pytest.raises(ValueError, match="AI 信源池路径"):
        load_accounts(tmp_path / "not-ai.json", topic_domain="ai")


class ResumedRunSnapshotTest(unittest.TestCase):
    def test_skipped_account_reuses_only_its_latest_successful_run_posts(self):
        account = {"user_id": "42", "handle": "source_handle", "source_lists": ["mother-pool"]}
        fetched_post = {
            "post_id": "fresh-1",
            "author_id": "42",
            "handle": "source_handle",
            "text": "Coinbase launched tokenized equities on Base today with new trading access.",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "url": "https://x.com/source_handle/status/fresh-1",
            "is_reply": False,
            "is_retweet": False,
            "is_quote": False,
            "metrics": {},
            "source_lists": ["mother-pool"],
        }
        stale_post = {
            **fetched_post,
            "post_id": "stale-1",
            "text": "STALE_WINDOW_SENTINEL Coinbase launched an old tokenized equities event.",
            "created_at": "2026-08-20T00:00:00+00:00",
            "url": "https://x.com/source_handle/status/stale-1",
        }
        fetch_result = {
            "posts": [fetched_post, stale_post],
            "pages_fetched": 1,
            "retries": 0,
            "watermark_at": datetime.now(timezone.utc).isoformat(),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "sources.sqlite3"
            output = root / "cards"
            with patch(
                "market_sources.collect_big_source_posts.load_accounts",
                return_value=[account],
            ), patch(
                "market_sources.collect_big_source_posts.fetch_account",
                return_value=fetch_result,
            ) as fetch:
                first = collect(Path("ignored.json"), db_path, output, key="runtime-key", workers=1)
                second = collect(
                    Path("ignored.json"), db_path, output, key="runtime-key", workers=1,
                    topic_domain="ai",
                )

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(first["topic_domain"], "crypto")
            self.assertEqual(second["topic_domain"], "ai")
            self.assertEqual(first["posts_new"], 2)
            self.assertEqual(second["accounts_skipped"], 1)
            self.assertEqual(second["posts_seen"], 1)
            self.assertEqual(second["posts_new"], 0)
            self.assertEqual(second["posts_reused"], 1)
            second_snapshot = Path(second["snapshot_dir"])
            self.assertEqual(
                [post["post_id"] for post in json.loads((second_snapshot / "latest.json").read_text())["posts"]],
                ["fresh-1"],
            )
            self.assertEqual("ai", json.loads((second_snapshot / "latest.json").read_text())["topic_domain"])
            self.assertNotIn(
                "STALE_WINDOW_SENTINEL",
                (second_snapshot / "latest.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                cross_validate(db_path, second_snapshot, run_id=second["run_id"])["source_posts"],
                1,
            )
