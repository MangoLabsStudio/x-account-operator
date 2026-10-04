import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from market_sources import retail_attention


class RetailAttentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "xops.db"
        retail_attention.init_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_store_items_upserts_metrics_without_losing_first_seen(self):
        row = retail_attention.item(
            "bilibili", "BV1", item_type="video", title="财经视频",
            raw={"v": 1}, attention_score=10, metrics={"views": 100},
        )
        retail_attention.store_items(self.db_path, [row])
        with sqlite3.connect(self.db_path) as conn:
            first_seen = conn.execute(
                "SELECT first_seen_at FROM retail_items WHERE source='bilibili' AND external_id='BV1'"
            ).fetchone()[0]
        row["attention_score"] = 30
        row["metrics"] = {"views": 300}
        retail_attention.store_items(self.db_path, [row])
        with sqlite3.connect(self.db_path) as conn:
            stored = conn.execute(
                """SELECT first_seen_at,attention_score,metrics_json
                   FROM retail_items WHERE source='bilibili' AND external_id='BV1'"""
            ).fetchone()
        self.assertEqual(stored[0], first_seen)
        self.assertEqual(stored[1], 30)
        self.assertEqual(json.loads(stored[2])["views"], 300)

    def test_store_items_reports_unique_platform_ids(self):
        row = retail_attention.item(
            "douyin", "same", item_type="video", title="one", raw={}, attention_score=1,
        )
        self.assertEqual(retail_attention.store_items(self.db_path, [row, row]), 1)

    def test_bilibili_uses_most_viewed_daily_sort(self):
        with patch.object(retail_attention, "run_actor", return_value=[]) as actor:
            retail_attention.bilibili_items(self.db_path)
        payload = actor.call_args.args[3]
        self.assertEqual(payload["sortOrder"], "click")
        self.assertTrue(payload["pubtimeBegin"])

    def test_douyin_transcribes_highest_attention_video(self):
        search_rows = [
            {
                "videoId": "high", "caption": "高互动视频", "userName": "person",
                "likeCount": 1000, "commentCount": 300, "shareCount": 200,
            },
            {
                "videoId": "low", "caption": "低互动视频", "userName": "person",
                "likeCount": 10, "commentCount": 1, "shareCount": 0,
            },
        ]

        def actor(_db, _token, name, payload, _charge, cache_bucket=""):
            if name == retail_attention.DOUYIN_ACTOR:
                return search_rows
            self.assertEqual(name, retail_attention.DOUYIN_TRANSCRIPT_ACTOR)
            self.assertEqual(payload["videoUrl"], "https://www.douyin.com/video/high")
            self.assertEqual(cache_bucket, "")
            return [{"text": "这是完整口播", "segments": [{"start": 0, "end": 1, "text": "这是完整口播"}], "duration": 1}]

        with patch.dict(os.environ, {"XOPS_DOUYIN_TRANSCRIPT_MAX_RESULTS": "1"}), patch.object(
            retail_attention, "run_actor", side_effect=actor
        ):
            rows = retail_attention.douyin_items(self.db_path)

        by_id = {row["external_id"]: row for row in rows}
        self.assertEqual(by_id["high"]["body"], "这是完整口播")
        self.assertEqual(by_id["high"]["metrics"]["transcript_status"], "succeeded")
        self.assertEqual(by_id["high"]["raw"]["_transcript"]["segments"][0]["start"], 0)
        self.assertEqual(by_id["low"]["body"], "低互动视频")
        self.assertEqual(by_id["low"]["metrics"]["transcript_status"], "not_selected")

    def test_douyin_empty_transcript_keeps_caption(self):
        def actor(_db, _token, name, _payload, _charge, cache_bucket=""):
            if name == retail_attention.DOUYIN_ACTOR:
                return [{"videoId": "silent", "caption": "只有画面文字", "likeCount": 100}]
            return [{"text": "", "segments": [], "duration": 10, "errMsg": ""}]

        with patch.dict(os.environ, {"XOPS_DOUYIN_TRANSCRIPT_MAX_RESULTS": "1"}), patch.object(
            retail_attention, "run_actor", side_effect=actor
        ):
            row = retail_attention.douyin_items(self.db_path)[0]

        self.assertEqual(row["body"], "只有画面文字")
        self.assertEqual(row["metrics"]["transcript_status"], "empty")

    def test_douyin_transcript_selection_ignores_old_high_traffic_video(self):
        now = int(datetime.now(timezone.utc).timestamp())
        search_rows = [
            {"videoId": "old", "caption": "旧爆款", "likeCount": 100000, "createTime": now - 10 * 86400},
            {"videoId": "current", "caption": "本周热门", "likeCount": 1000, "createTime": now},
        ]

        def actor(_db, _token, name, payload, _charge, cache_bucket=""):
            if name == retail_attention.DOUYIN_ACTOR:
                return search_rows
            self.assertEqual(payload["videoUrl"], "https://www.douyin.com/video/current")
            return [{"text": "本周口播", "segments": [], "duration": 2}]

        with patch.dict(os.environ, {"XOPS_DOUYIN_TRANSCRIPT_MAX_RESULTS": "1"}), patch.object(
            retail_attention, "run_actor", side_effect=actor
        ):
            rows = retail_attention.douyin_items(self.db_path)

        by_id = {row["external_id"]: row for row in rows}
        self.assertEqual(by_id["current"]["body"], "本周口播")
        self.assertEqual(by_id["old"]["metrics"]["transcript_status"], "not_selected")

    def test_failed_source_uses_retry_backoff(self):
        run_id = retail_attention.start_run(self.db_path, "coingecko", "test")
        retail_attention.fail_run(self.db_path, run_id, "coingecko", RuntimeError("down"))
        self.assertNotIn("coingecko", retail_attention.due_sources(self.db_path, ("coingecko",)))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE retail_source_state SET updated_at=updated_at-1000 WHERE source='coingecko'"
            )
        self.assertIn("coingecko", retail_attention.due_sources(self.db_path, ("coingecko",)))

    def test_list_items_filters_source_and_type(self):
        retail_attention.store_items(self.db_path, [
            retail_attention.item(
                "reddit", "r1", item_type="personal_post", title="one", raw={}, attention_score=20,
            ),
            retail_attention.item(
                "coingecko", "c1", item_type="attention_signal", title="two", raw={}, attention_score=50,
            ),
        ])
        result = retail_attention.list_items(
            self.db_path, source="reddit", item_type="personal_post"
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["external_id"], "r1")

    def test_finish_run_keeps_metric_observations(self):
        run_id = retail_attention.start_run(self.db_path, "coingecko", "test")
        row = retail_attention.item(
            "coingecko", "btc:1", entity_id="btc", pool="hot_signal",
            item_type="attention_signal", title="BTC", raw={"rank": 1},
            attention_score=20, metrics={"rank": 1},
        )
        retail_attention.finish_run(self.db_path, run_id, "coingecko", [row])
        observations = retail_attention.list_observations(
            self.db_path, source="coingecko", external_id="btc:1"
        )
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["metrics"], {"rank": 1})
        self.assertEqual(observations[0]["raw"], {"rank": 1})

    def test_hot_pool_uses_latest_entity_snapshot_and_normalizes_by_source(self):
        published = datetime.now(timezone.utc).isoformat()
        old_published = "2026-08-28T00:00:00+00:00"
        retail_attention.store_items(self.db_path, [
            retail_attention.item(
                "coingecko", "btc:old", entity_id="btc", pool="hot_signal",
                item_type="attention_signal", title="BTC old", raw={},
                published_at=old_published, attention_score=5,
            ),
            retail_attention.item(
                "coingecko", "btc:new", entity_id="btc", pool="hot_signal",
                item_type="attention_signal", title="BTC new", raw={},
                published_at=published, attention_score=20,
            ),
            retail_attention.item(
                "coingecko", "eth:new", entity_id="eth", pool="hot_signal",
                item_type="attention_signal", title="ETH", raw={},
                published_at=published, attention_score=10,
            ),
            retail_attention.item(
                "eastmoney_rank", "a:new", entity_id="a", pool="hot_signal",
                item_type="attention_signal", title="A", raw={},
                published_at=published, attention_score=1,
            ),
        ])
        result = retail_attention.list_pool(self.db_path, "hot_signal")
        ids = {row["external_id"] for row in result["items"]}
        self.assertNotIn("btc:old", ids)
        self.assertEqual(ids, {"btc:new", "eth:new", "a:new"})
        scores = {row["external_id"]: row["normalized_score"] for row in result["items"]}
        self.assertEqual(scores["btc:new"], 100)
        self.assertEqual(scores["eth:new"], 0)
        self.assertEqual(scores["a:new"], 100)

    def test_viral_pool_excludes_obvious_institution_accounts(self):
        published = datetime.now(timezone.utc).isoformat()
        retail_attention.store_items(self.db_path, [
            retail_attention.item(
                "reddit", "person", entity_id="person", pool="viral_prior",
                item_type="personal_post", title="个人复盘", author="ordinary_trader",
                raw={}, published_at=published, attention_score=50,
            ),
            retail_attention.item(
                "reddit", "news", entity_id="news", pool="viral_prior",
                item_type="personal_post", title="快讯", author="Market News Official",
                raw={}, published_at=published, attention_score=100,
            ),
            retail_attention.item(
                "x", "irrelevant", entity_id="irrelevant", pool="viral_prior",
                item_type="personal_post", title="I am starting something new",
                body="I am starting something new", author="person",
                raw={}, published_at=published, attention_score=200,
            ),
        ])
        result = retail_attention.list_pool(self.db_path, "viral_prior")
        self.assertEqual([row["external_id"] for row in result["items"]], ["person"])

    def test_published_timestamp_supports_common_formats(self):
        expected = 1_700_000_000
        self.assertEqual(retail_attention.published_timestamp(expected * 1000), expected)
        self.assertEqual(
            retail_attention.published_timestamp("Tue, 14 Nov 2023 22:13:20 GMT"), expected
        )
        self.assertEqual(
            retail_attention.published_timestamp("2023-11-14T22:13:20+00:00"), expected
        )

    def test_finance_relevance_does_not_match_ticker_inside_words(self):
        self.assertFalse(retail_attention.finance_relevant("I am starting something new"))
        self.assertTrue(retail_attention.finance_relevant("ETH market liquidity is improving"))

    def test_init_db_migrates_existing_retail_items_table(self):
        old_path = Path(self.temp.name) / "old.db"
        with sqlite3.connect(old_path) as conn:
            conn.execute(
                """CREATE TABLE retail_items(
                       source TEXT,external_id TEXT,item_type TEXT,url TEXT,title TEXT,body TEXT,
                       author TEXT,community TEXT,language TEXT,published_at TEXT,
                       attention_score REAL,metrics_json TEXT,raw_json TEXT,
                       first_seen_at INTEGER,last_seen_at INTEGER,
                       PRIMARY KEY(source,external_id))"""
            )
        retail_attention.init_db(old_path)
        with sqlite3.connect(old_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(retail_items)")}
        self.assertTrue({"entity_id", "pool", "published_ts"}.issubset(columns))

    def test_x_items_reads_recent_personal_original_posts(self):
        source_db = self.db_path.parent / "market_source_posts.sqlite3"
        now = datetime.now(timezone.utc)
        with sqlite3.connect(source_db) as conn:
            conn.execute(
                """CREATE TABLE source_posts(
                       post_id TEXT PRIMARY KEY,handle TEXT,text TEXT,created_at TEXT,url TEXT,
                       is_reply INTEGER,is_retweet INTEGER,metrics TEXT)"""
            )
            rows = [
                (
                    "personal", "ordinary_trader", "我把比特币交易完整复盘了一遍", now.isoformat(),
                    "https://x.com/ordinary/status/personal", 0, 0,
                    json.dumps({"view_count": "10000", "favorite_count": 50,
                                "retweet_count": 10, "reply_count": 5}),
                ),
                (
                    "news", "MarketNewsOfficial", "机构快讯", now.isoformat(),
                    "https://x.com/news/status/news", 0, 0, "{}",
                ),
                (
                    "project", "Polymarket", "BREAKING: Bitcoin market moves higher",
                    now.isoformat(), "https://x.com/polymarket/status/project", 0, 0, "{}",
                ),
                (
                    "reply", "ordinary_trader", "回复", now.isoformat(),
                    "https://x.com/ordinary/status/reply", 1, 0, "{}",
                ),
                (
                    "old", "ordinary_trader", "过期内容",
                    (now - timedelta(days=4)).isoformat(),
                    "https://x.com/ordinary/status/old", 0, 0, "{}",
                ),
            ]
            conn.executemany("INSERT INTO source_posts VALUES(?,?,?,?,?,?,?,?)", rows)
        result = retail_attention.x_items(self.db_path)
        self.assertEqual([row["external_id"] for row in result], ["personal"])
        self.assertEqual(result[0]["pool"], "viral_prior")
        self.assertGreater(result[0]["attention_score"], 80)


class RetailAttentionAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_collect_source_marks_incomplete_payload_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "xops.db"
            with patch.dict(
                retail_attention.FETCHERS,
                {"coingecko": AsyncMock(return_value=[])},
            ):
                result = await retail_attention.collect_source(db_path, "coingecko", "test")
            self.assertEqual(result["status"], "failed")
            self.assertIn("最低完整性门槛", result["error"])

    async def test_collect_runs_sources_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "xops.db"
            rows = [
                retail_attention.item(
                    "coingecko", f"c{index}", item_type="attention_signal",
                    title=str(index), raw={}, attention_score=index,
                )
                for index in range(5)
            ]
            with patch.dict(
                retail_attention.FETCHERS,
                {
                    "coingecko": AsyncMock(return_value=rows),
                    "google_trends": AsyncMock(side_effect=RuntimeError("feed down")),
                },
            ):
                result = await retail_attention.collect(
                    db_path, ["coingecko", "google_trends"], "test"
                )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["failed"], 1)


if __name__ == "__main__":
    unittest.main()
