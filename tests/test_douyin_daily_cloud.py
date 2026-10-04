import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from market_sources import douyin_daily_cloud as daily


def post(category, index):
    return {
        "source_aweme_id": f"{category}-{index}",
        "tag": "AI" if category == "ai" else "财经" if category == "finance" else "创业",
        "body": "完整且独立成立的最终正文",
        "source_url": f"https://www.douyin.com/video/{category}-{index}",
        "category": category,
        "tags": [category],
        "play_count": 1000 - index,
        "audio_path": "audio.mp4",
        "transcript": "完整口播文字",
    }


class DouyinDailyCloudTest(unittest.TestCase):
    def connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE douyin_finished_posts(
            id INTEGER PRIMARY KEY,batch_date TEXT,position INTEGER,tag TEXT,body TEXT,
            source_aweme_id TEXT UNIQUE,source_url TEXT,persona_id INTEGER,
            match_score INTEGER DEFAULT 0,match_reason TEXT DEFAULT '',assigned_at INTEGER,
            created_at INTEGER,updated_at INTEGER
        )""")
        daily.init_db(conn)
        return conn

    def complete(self):
        return {
            category: [post(category, index) for index in range(target)]
            for category, target in daily.PREP_TARGETS.items()
        }

    def test_incomplete_28_or_41_does_not_replace_existing_batch(self):
        for existing in (28, 41):
            with self.subTest(existing=existing):
                conn = self.connection()
                for index in range(existing):
                    conn.execute(
                        "INSERT INTO douyin_finished_posts(batch_date,position,tag,body,source_aweme_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        ("2026-09-11", index + 1, "财经", "旧正文", f"old-{index}", "https://example.com", 1, 1),
                    )
                conn.commit()
                empty = {category: [] for category in daily.PREP_TARGETS}
                with patch.dict(os.environ, {"TIKHUB_TOKEN": "fixture", "XOPS_GEMINI_API_KEY": "fixture"}), patch.object(
                    daily, "discover", AsyncMock(return_value=empty)
                ), patch.object(daily, "details", AsyncMock(return_value={})):
                    with self.assertRaisesRegex(RuntimeError, "合格成稿不足"):
                        asyncio.run(daily.run(conn, "2026-09-11", Path(tempfile.mkdtemp()), {f"old-{i}" for i in range(existing)}))
                rows = conn.execute("SELECT source_aweme_id FROM douyin_finished_posts ORDER BY position").fetchall()
                self.assertEqual([row[0] for row in rows], [f"old-{i}" for i in range(existing)])

    def test_duplicate_source_is_rejected(self):
        complete = self.complete()
        complete["ai"][0]["source_aweme_id"] = complete["finance"][0]["source_aweme_id"]
        with self.assertRaisesRegex(RuntimeError, "重复 source_aweme_id"):
            daily.select_delivery(complete)

    def test_complete_80_selects_exact_category_mix_and_60_unique(self):
        prepared, posts = daily.select_delivery(self.complete())
        self.assertEqual(len(prepared), 80)
        self.assertEqual(len(posts), 60)
        self.assertEqual(len({row["source_aweme_id"] for row in posts}), 60)
        self.assertEqual(
            {category: sum(row["category"] == category for row in posts) for category in daily.DELIVERY_TARGETS},
            daily.DELIVERY_TARGETS,
        )

    def test_current_day_finished_posts_are_reused_but_historical_posts_are_excluded(self):
        conn = self.connection()
        complete = self.complete()
        now = 1
        for category, rows in complete.items():
            for row in rows:
                conn.execute(
                    "INSERT INTO douyin_raw_asr(source_aweme_id,batch_date,category,tags_json,play_count,source_url,audio_path,transcript,final_body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (row["source_aweme_id"], "2026-09-11", category, f'["{row["tag"]}"]', row["play_count"], row["source_url"], row["audio_path"], row["transcript"], row["body"], now, now),
                )
        for index, row in enumerate(complete["finance"][:10], 1):
            conn.execute(
                "INSERT INTO douyin_finished_posts(batch_date,position,tag,body,source_aweme_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("2026-09-11", index, row["tag"], row["body"], row["source_aweme_id"], row["source_url"], now, now),
            )
        historical = post("finance", 999)
        historical["source_aweme_id"] = "historical-finished"
        conn.execute(
            "INSERT INTO douyin_raw_asr(source_aweme_id,batch_date,category,tags_json,play_count,source_url,audio_path,transcript,final_body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (historical["source_aweme_id"], "2026-09-10", "finance", '["财经"]', 999999, historical["source_url"], historical["audio_path"], historical["transcript"], historical["body"], now, now),
        )
        conn.execute(
            "INSERT INTO douyin_finished_posts(batch_date,position,tag,body,source_aweme_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("2026-09-10", 1, historical["tag"], historical["body"], historical["source_aweme_id"], historical["source_url"], now, now),
        )
        conn.commit()
        used = {row[0] for row in conn.execute("SELECT source_aweme_id FROM douyin_finished_posts")}
        with patch.dict(os.environ, {"TIKHUB_TOKEN": "fixture", "XOPS_GEMINI_API_KEY": "fixture"}):
            posts = asyncio.run(daily.run(conn, "2026-09-11", Path(tempfile.mkdtemp()), used))
        ids = {row["source_aweme_id"] for row in posts}
        self.assertTrue({row["source_aweme_id"] for row in complete["finance"][:10]} <= ids)
        self.assertNotIn("historical-finished", ids)
        self.assertEqual(len(posts), 60)

    def test_retry_budget_is_bounded_and_delayed(self):
        self.assertEqual([daily.retry_delay(i) for i in range(1, 7)], [300, 1800, 7200, 21600, 7200, 21600])
        self.assertTrue(daily.retry_ready({"attempts": 4, "next_retry_at": 0}, 100))
        self.assertFalse(daily.retry_ready({"attempts": 6, "next_retry_at": 0}, 100))
        self.assertFalse(daily.retry_ready({"attempts": 2, "next_retry_at": 101}, 100))
        self.assertTrue(daily.retry_ready({"attempts": 2, "next_retry_at": 100}, 100))

    def test_late_attempts_expand_to_seven_days_and_rotate_tags(self):
        for previous_attempts, expected in ((4, daily.LATE_TAGS), (5, daily.LATE_TAGS_2)):
            with self.subTest(previous_attempts=previous_attempts):
                conn = self.connection()
                conn.execute(
                    "INSERT INTO douyin_daily_runs(batch_date,status,started_at,attempts) VALUES(?,?,?,?)",
                    ("2026-09-16", "failed", 1, previous_attempts),
                )
                empty = {category: [] for category in daily.PREP_TARGETS}
                with patch.dict(os.environ, {"TIKHUB_TOKEN": "fixture", "XOPS_GEMINI_API_KEY": "fixture"}), patch.object(
                    daily, "discover", AsyncMock(return_value=empty)
                ) as discover, patch.object(daily, "details", AsyncMock(return_value={})):
                    with self.assertRaisesRegex(RuntimeError, "合格成稿不足"):
                        asyncio.run(daily.run(conn, "2026-09-16", Path(tempfile.mkdtemp()), set()))
                self.assertEqual(discover.call_args.kwargs["date_type"], 7)
                self.assertEqual(discover.call_args.args[3], expected)

    def test_discover_passes_seven_day_filter_to_tikhub(self):
        seen = []

        async def fake_api(_client, _token, _path, *, query=None, **_kwargs):
            seen.append(query["date_type"])
            return {"data": []}

        with patch.object(daily, "api", fake_api):
            asyncio.run(daily.discover(None, "fixture", set(), {"finance": ("财经",), "ai": (), "other": ()}, date_type=7))
        self.assertEqual(seen, [7])

    def test_queue_does_not_exceed_retry_budget_after_restart(self):
        import app

        original_path = app.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            app.DB_PATH = Path(directory) / "xops.db"
            try:
                app.init_db()
                with app.db() as conn:
                    daily.init_db(conn)
                    conn.execute(
                        "INSERT INTO douyin_daily_runs(batch_date,status,started_at,attempts,next_retry_at) VALUES(?,?,?,?,?)",
                        ("2026-09-11", "running", 1, daily.MAX_ATTEMPTS, 0),
                    )
                self.assertFalse(app.queue_douyin_daily("2026-09-11"))
                with app.db() as conn:
                    run = conn.execute("SELECT status,attempts FROM douyin_daily_runs WHERE batch_date=?", ("2026-09-11",)).fetchone()
                self.assertEqual((run["status"], run["attempts"]), ("failed", daily.MAX_ATTEMPTS))
            finally:
                app.DB_PATH = original_path

    def test_assignment_requires_20_personas_with_three_each(self):
        daily.validate_assignments({f"persona-{i}": 3 for i in range(20)})
        for assignments in (
            {f"persona-{i}": 3 for i in range(19)},
            {**{f"persona-{i}": 3 for i in range(19)}, "persona-19": 2},
        ):
            with self.assertRaisesRegex(RuntimeError, "20 个人设"):
                daily.validate_assignments(assignments)

    def test_bad_persona_distribution_rolls_back_batch_replacement(self):
        import app

        original_path = app.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            app.DB_PATH = Path(directory) / "xops.db"
            try:
                app.init_db()
                with app.db() as conn:
                    for index in range(28):
                        conn.execute(
                            "INSERT INTO douyin_finished_posts(batch_date,position,tag,body,source_aweme_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                            ("2026-09-11", index + 1, "财经", "旧正文", f"old-{index}", "https://example.com", 1, 1),
                        )

                class FakeModule:
                    @staticmethod
                    def init_db(conn):
                        daily.init_db(conn)

                    @staticmethod
                    async def run(conn, batch_date, _data_dir, _used):
                        conn.execute("DELETE FROM douyin_finished_posts WHERE batch_date=?", (batch_date,))
                        for index in range(60):
                            conn.execute(
                                "INSERT INTO douyin_finished_posts(batch_date,position,tag,body,source_aweme_id,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                (batch_date, index + 1, "财经", "新正文", f"new-{index}", "https://example.com", 2, 2),
                            )
                        return [post("finance", index) for index in range(60)]

                    validate_assignments = staticmethod(daily.validate_assignments)
                    retry_delay = staticmethod(daily.retry_delay)

                wrong = {**{f"persona-{i}": 3 for i in range(19)}, "persona-19": 2}
                with patch.object(app, "douyin_daily_module", return_value=FakeModule), patch.object(
                    app, "assign_douyin_finished_posts", return_value=wrong
                ):
                    with self.assertRaisesRegex(RuntimeError, "20 个人设"):
                        asyncio.run(app.execute_douyin_daily("2026-09-11"))
                with app.db() as conn:
                    ids = [row[0] for row in conn.execute(
                        "SELECT source_aweme_id FROM douyin_finished_posts WHERE batch_date=? ORDER BY position",
                        ("2026-09-11",),
                    )]
                self.assertEqual(ids, [f"old-{index}" for index in range(28)])
            finally:
                app.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
