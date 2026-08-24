import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, payload, calls):
        self.payload = payload
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return FakeResponse(self.payload)


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            XOPS_DATA_DIR=self.temp.name,
            XOPS_DAILY_CONTEXT_ENABLED="false",
            XOPS_BASE_URL="http://127.0.0.1:8788",
        )
        import app
        self.app_module = importlib.reload(app)
        self.client = TestClient(self.app_module.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait_for_daily_run(self, context_date):
        for _ in range(100):
            response = self.client.get(f"/api/context/daily-runs/{context_date}")
            if response.status_code == 200 and response.json()["status"] != "running":
                return response
            time.sleep(0.01)
        self.fail(f"daily context run {context_date} did not finish")

    def create_editorial_run(self, context_date, status="approved", topics=None, market_state="已批准的市场变化"):
        topics = topics or [{
            "claim_key": "approved-market-change",
            "subject": "热点项目",
            "title": "热点项目出现新的市场变化",
            "core_claim": "公共题单的主张不等于任何人设已经表达。",
            "eligible": True,
        }]
        now = int(time.time())
        self.app_module.save_daily_context(
            context_date,
            self.app_module.DailyMarketContextIn(
                market_state=market_state,
                event_clusters="事件聚类",
                debates="市场分歧",
                evidence="可核验事实",
                unknowns="",
                raw_feed="",
                sources=[],
            ),
        )
        with self.app_module.db() as conn:
            cursor = conn.execute(
                """INSERT INTO daily_context_runs(
                    context_date,status,trigger,raw_cards,synthesis,created_at,updated_at,approved_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    context_date,
                    status,
                    "test",
                    json.dumps({"selected_topics": topics}, ensure_ascii=False),
                    "{}",
                    now,
                    now,
                    now if status == "approved" else None,
                ),
            )
        return cursor.lastrowid

    def run_editorial_pipeline(self, run_id):
        with self.app_module.db() as conn:
            context_date = conn.execute(
                "SELECT context_date FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        with patch.object(self.app_module, "shanghai_today", return_value=context_date):
            return asyncio.run(self.app_module.run_persona_editorial_pipeline(run_id))

    @staticmethod
    def editorial_decision(topic, status, *, claim_key="", core_claim="", score=4, why_me="人设有明确观察角度"):
        return {
            str(topic["claim_key"]): {
                "status": status,
                "notice": score,
                "authority": score,
                "tension": score,
                "marginal_value": score,
                "why_me": why_me if status == "WRITE" else "",
                "claim_key": claim_key,
                "core_claim": core_claim,
                "reason_code": status.lower(),
                "rationale": "测试决策",
                "open_loop": "",
            }
        }

    def insert_pending_editorial_write(self, run_id, context_date, topic, *, slug="acheng",
                                        claim_key="pending-claim", core_claim="待恢复的核心判断"):
        with self.app_module.db() as conn:
            persona = dict(conn.execute(
                "SELECT id,slug,draft FROM personas WHERE slug=?", (slug,)
            ).fetchone())
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
            run = conn.execute(
                "SELECT raw_cards,approval_revision FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()
            topics = self.app_module.json_value(run["raw_cards"], {}).get("selected_topics", [])
            daily["approval_revision"] = run["approval_revision"]
            stable_history = self.app_module.editorial_stable_claim_history(conn, context_date)
            input_payload = self.app_module.editorial_topic_input_payload(
                topic, daily, persona, {}, topics=topics, claim_history=stable_history
            )
            input_hash = self.app_module.editorial_topic_input_hash(
                topic, daily, persona, {}, topics=topics, claim_history=stable_history
            )
            now = int(time.time())
            cursor = conn.execute(
                """INSERT INTO persona_editorial_evaluations(
                    run_id,persona_id,topic_input_hash,input_json,topic_json,status,notice,authority,tension,marginal_value,
                    why_me,claim_key,core_claim,reason_code,rationale,open_loop,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, persona["id"], input_hash, json.dumps(input_payload, ensure_ascii=False),
                 json.dumps(topic, ensure_ascii=False), "WRITE", 5, 5, 4, 5, "这是该人设会说的",
                 claim_key, core_claim, "write", "测试", "", now, now),
            )
        return cursor.lastrowid

    @staticmethod
    def editorial_context_payload(*, life_context=None, thought_threads=None,
                                 expression_debt=None, real_feedback=None,
                                 available_asset_ids=None):
        return {
            "life_context": life_context or [],
            "thought_threads": thought_threads or [],
            "expression_debt": expression_debt or [],
            "real_feedback": real_feedback or [],
            "available_asset_ids": available_asset_ids or [],
        }

    def put_editorial_context(self, persona_id, payload):
        response = self.client.put(f"/api/personas/{persona_id}/editorial-context", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def approve_editorial_context(self, persona_id):
        response = self.client.post(f"/api/personas/{persona_id}/editorial-context/approve")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_health_is_public(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["daily_context_enabled"])
        self.assertEqual(response.json()["daily_context_run_time"], "08:15")

    def test_init_db_migrates_legacy_daily_run_approval_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "xops.db"
            conn = sqlite3.connect(legacy_path)
            conn.execute(
                """CREATE TABLE daily_context_runs (
                    id INTEGER PRIMARY KEY,context_date TEXT NOT NULL UNIQUE,status TEXT NOT NULL,
                    trigger TEXT NOT NULL,raw_manifest TEXT NOT NULL DEFAULT '{}',raw_cards TEXT NOT NULL DEFAULT '{}',
                    synthesis TEXT NOT NULL DEFAULT '{}',reviewer_notes TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
                    started_at INTEGER,completed_at INTEGER,approved_at INTEGER,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE post_candidates (
                    id INTEGER PRIMARY KEY,persona_id INTEGER NOT NULL,context_date TEXT NOT NULL,
                    title TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'needs_refresh',
                    source TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,UNIQUE(persona_id,context_date,source)
                )"""
            )
            conn.commit()
            conn.close()
            original_data_dir = self.app_module.DATA_DIR
            original_db_path = self.app_module.DB_PATH
            try:
                self.app_module.DATA_DIR = Path(directory)
                self.app_module.DB_PATH = legacy_path
                self.app_module.init_db()
                with self.app_module.db() as migrated:
                    columns = {
                        row["name"] for row in migrated.execute(
                            "PRAGMA table_info(daily_context_runs)"
                        ).fetchall()
                    }
                    candidate_columns = {
                        row["name"] for row in migrated.execute(
                            "PRAGMA table_info(post_candidates)"
                        ).fetchall()
                    }
                self.assertIn("approval_revision", columns)
                self.assertIn("asset_id", candidate_columns)
            finally:
                self.app_module.DATA_DIR = original_data_dir
                self.app_module.DB_PATH = original_db_path

    def test_dashboard_is_public(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_public_base_url_is_rendered_for_reverse_proxy(self):
        os.environ["XOPS_BASE_URL"] = "https://siriuszzz-api.uk/xops"
        for path in ("/personas", "/market"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("const base='https://siriuszzz-api.uk/xops'", response.text)
            self.assertNotIn("__BASE_URL__", response.text)

    def test_persona_center_loads_seeded_characters(self):
        self.assertEqual(self.client.get("/personas").status_code, 200)
        personas = self.client.get("/api/personas").json()
        slugs = [persona["slug"] for persona in personas]
        self.assertEqual(
            set(slugs),
            {
                "acheng",
                "ridehail-driver-zhao",
                "college-student-linjia",
                "atuo",
                "axu",
                "nanqiao",
                "qiliang",
                "aye",
                "xiaoman",
                "maili",
            },
        )
        self.assertNotIn("office-worker-zhou", slugs)
        self.assertNotIn("county-mom-xiaomei", slugs)
        self.assertNotIn("cc0-source-selection", slugs)
        student = next(persona for persona in personas if persona["slug"] == "college-student-linjia")
        self.assertEqual(student["display_name"], "桃桃还没下课")
        self.assertEqual(student["handle"], "@taotao_afterclass")

        atuo = next(persona for persona in personas if persona["slug"] == "atuo")
        self.assertEqual(atuo["display_name"], "阿拓Tuo")
        self.assertEqual(atuo["handle"], "@atuo_xyz")
        self.assertIn("atuo/avatar.png", atuo["avatar_url"])

        crypto_names = {
            persona["display_name"] for persona in personas if persona["slug"] in self.app_module.PERSONA_BIOS
        }
        self.assertEqual(
            crypto_names,
            {"阿拓Tuo", "AXU", "南桥研究所", "7Liang", "野生Aye", "小满 onchain", "Milly的交易手账"},
        )

        axu = next(persona for persona in personas if persona["slug"] == "axu")
        axu_detail = self.client.get(f"/api/personas/{axu['id']}").json()
        self.assertEqual(axu_detail["draft"]["config_revision"], 3)
        self.assertIn("看结构，也看人群", axu_detail["draft"]["identity"]["bio"])
        self.assertEqual(axu_detail["draft"]["voice"]["favorite_phrases"], "")
        self.assertIn("不设固定句式", axu_detail["draft"]["voice"]["syntax_patterns"])
        self.assertIn("不喊话", axu_detail["draft"]["voice"]["mobilization_style"])
        self.assertIn("不代表持有该 NFT", axu_detail["draft"]["visual"]["source_note"])
        self.assertEqual([asset["name"] for asset in axu_detail["assets"]], ["avatar"])

    def test_post_candidates_start_empty_and_are_available_by_persona(self):
        personas = self.client.get("/api/personas").json()
        acheng = next(persona for persona in personas if persona["slug"] == "acheng")
        self.assertEqual(
            self.client.get(f"/api/personas/{acheng['id']}/post-candidates").json(), []
        )
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_context_runs").fetchone()[0], 0)
            now = int(time.time())
            conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    acheng["id"],
                    "2026-08-24",
                    "新的候选内容",
                    "来自重新抓取母池后生成的候选。",
                    "needs_review",
                    "daily_context_run",
                    "未排期，未发布。",
                    now,
                    now,
                ),
            )
        candidates = self.client.get(f"/api/personas/{acheng['id']}/post-candidates").json()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "新的候选内容")
        self.assertEqual(candidates[0]["source"], "daily_context_run")
        self.assertEqual(self.client.get("/api/daily-post").status_code, 404)

    def test_unapproved_context_never_evaluates_or_generates_editorial_posts(self):
        run_id = self.create_editorial_run("2026-08-21", status="needs_review")
        evaluator = AsyncMock(side_effect=AssertionError("unapproved context must not be evaluated"))
        generated = AsyncMock(side_effect=AssertionError("unapproved context must not generate"))
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "evaluate_persona_editorial", evaluator
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.assertEqual(self.run_editorial_pipeline(run_id), [])
        evaluator.assert_not_awaited()
        generated.assert_not_awaited()
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM persona_editorial_evaluations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 0)

    def test_editorial_decisions_only_write_and_never_fill_a_quota(self):
        run_id = self.create_editorial_run("2026-08-22")
        generated = AsyncMock(return_value={"post": "只给真正值得写的人设生成。"})

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            topic = topics[0]
            if persona["slug"] == "acheng":
                return self.editorial_decision(
                    topic, "WRITE", claim_key="acheng-market-thesis", core_claim="这次变化先改变的是流动性预期。"
                )
            if persona["slug"] == "ridehail-driver-zhao":
                return self.editorial_decision(topic, "HOLD")
            return self.editorial_decision(topic, "IGNORE")

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao,college-student-linjia",
        }), patch.object(self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)), patch.object(
            self.app_module, "generate_persona_post", generated
        ), patch.object(self.app_module, "publish_persona", side_effect=AssertionError("pipeline must never publish")):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM persona_editorial_evaluations ORDER BY id"
            ).fetchall()]
            self.assertEqual(statuses, ["WRITE", "HOLD", "IGNORE"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)
            claims = conn.execute(
                "SELECT claim_key FROM topic_claim_history WHERE claim_key LIKE 'persona:%'"
            ).fetchall()
            self.assertEqual(len(claims), 1)

    def test_multiple_distinct_writes_same_persona_are_all_kept(self):
        topics = [
            {
                "claim_key": "security-topic", "subject": "安全", "title": "协议安全变化",
                "core_claim": "公共安全题", "eligible": True,
            },
            {
                "claim_key": "market-topic", "subject": "市场", "title": "市场结构变化",
                "core_claim": "公共市场题", "eligible": True,
            },
        ]
        run_id = self.create_editorial_run("2026-08-26", topics=topics)
        generated = AsyncMock(return_value={"post": "独立候选正文"})

        async def evaluator(_persona, _context, _daily, input_topics, _history, _today_count):
            return {
                **self.editorial_decision(
                    input_topics[0], "WRITE", claim_key="security-thesis",
                    core_claim="安全变化会先影响协议方的响应节奏。",
                ),
                **self.editorial_decision(
                    input_topics[1], "WRITE", claim_key="market-thesis",
                    core_claim="市场变化会先影响流动性结构。",
                ),
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM persona_editorial_evaluations WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()]
            self.assertEqual(statuses, ["WRITE", "WRITE"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 2)

    def test_editorial_same_claim_has_one_winner_but_different_claims_can_coexist(self):
        topic = {
            "claim_key": "hot-event",
            "subject": "热点事件",
            "title": "热点事件",
            "core_claim": "公共题单",
            "eligible": True,
        }
        run_id = self.create_editorial_run("2026-08-23", topics=[topic])
        generated = AsyncMock(return_value={"post": "候选正文"})

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            if persona["slug"] == "acheng":
                return self.editorial_decision(topics[0], "WRITE", claim_key="same-claim", core_claim="同一个核心判断", score=5)
            if persona["slug"] == "ridehail-driver-zhao":
                return self.editorial_decision(topics[0], "WRITE", claim_key="same-claim", core_claim="同一个核心判断", score=3)
            return self.editorial_decision(topics[0], "WRITE", claim_key="different-claim", core_claim="同热点的另一条具体判断", score=4)

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao,college-student-linjia",
        }), patch.object(self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)), patch.object(
            self.app_module, "generate_persona_post", generated
        ):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            decisions = [tuple(row) for row in conn.execute(
                "SELECT p.slug,e.status,e.reason_code FROM persona_editorial_evaluations e JOIN personas p ON p.id=e.persona_id ORDER BY p.slug"
            ).fetchall()]
            self.assertIn(("acheng", "WRITE", "write"), decisions)
            self.assertIn(("ridehail-driver-zhao", "HOLD", "cross_persona_collision"), decisions)
            self.assertIn(("college-student-linjia", "WRITE", "write"), decisions)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 2)

    def test_editorial_replay_recovers_pending_write_without_re_evaluation(self):
        topic = {
            "claim_key": "recovery-topic", "subject": "恢复", "title": "恢复", "core_claim": "公共题单", "eligible": True,
        }
        context_date = "2026-08-24"
        run_id = self.create_editorial_run(context_date, topics=[topic])
        self.insert_pending_editorial_write(
            run_id, context_date, topic,
            claim_key="recovery-claim", core_claim="已有 WRITE 但尚未生成正文。",
        )
        evaluator = AsyncMock(side_effect=AssertionError("existing input must not be re-evaluated"))
        generated = AsyncMock(return_value={"post": "恢复成功"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(self.app_module, "evaluate_persona_editorial", evaluator), patch.object(
            self.app_module, "generate_persona_post", generated
        ):
            self.run_editorial_pipeline(run_id)
            self.run_editorial_pipeline(run_id)
        evaluator.assert_not_awaited()
        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT candidate_id FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_editorial_material_input_change_allows_incremental_re_evaluation(self):
        run_id = self.create_editorial_run("2026-08-25")
        generated = AsyncMock(return_value={"post": "更新后的正文"})
        calls = []

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            calls.append(True)
            suffix = len(calls)
            return self.editorial_decision(
                topics[0], "WRITE", claim_key=f"incremental-{suffix}", core_claim=f"第 {suffix} 次输入的新增判断"
            )

        env = {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}
        with patch.dict(os.environ, env), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
            self.app_module.save_daily_context(
                "2026-08-25",
                self.app_module.DailyMarketContextIn(
                    market_state="实质新增的市场变化", event_clusters="事件聚类", debates="市场分歧",
                    evidence="新增可核验事实", unknowns="", raw_feed="", sources=[],
                ),
            )
            self.run_editorial_pipeline(run_id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(generated.await_count, 2)

    def test_daily_posts_api_returns_only_real_candidates_not_ten_queued_placeholders(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            persona = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()
            now = int(time.time())
            conn.execute(
                """INSERT INTO persona_editorial_evaluations(
                    id,run_id,persona_id,topic_input_hash,topic_json,status,notice,authority,tension,marginal_value,
                    why_me,claim_key,core_claim,reason_code,rationale,open_loop,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (123, run_id, persona["id"], "api-test", "{}", "WRITE", 5, 5, 5, 5,
                 "人设角度", "api-claim", "真实草稿", "write", "测试", "", now, now),
            )
            conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (persona["id"], context_date, "真实草稿", "只有真实候选会出现。", "needs_review", "persona_editorial:123", "未发布", now, now),
            )
        with patch.object(self.app_module, "shanghai_today", return_value=context_date):
            queue = self.client.get("/api/daily-posts").json()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["persona_slug"], "acheng")
        self.assertEqual(queue[0]["body"], "只有真实候选会出现。")
        self.assertNotIn("queued", [item["status"] for item in queue])

    def test_daily_posts_api_includes_review_only_initial_batch(self):
        context_date = self.app_module.shanghai_today()
        with self.app_module.db() as conn:
            persona = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()
            now = int(time.time())
            conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    persona["id"], context_date, "首批观点", "首批待审正文。", "needs_review",
                    f"initial_batch:{context_date}:evergreen-01", "未发布", now, now,
                ),
            )
        queue = self.client.get("/api/daily-posts").json()
        latest = self.client.get("/api/daily-post")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["body"], "首批待审正文。")
        self.assertEqual(queue[0]["status"], "needs_review")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["source"], f"initial_batch:{context_date}:evergreen-01")

    def test_persona_queues_advance_one_post_at_a_time(self):
        context_date = self.app_module.shanghai_today()
        with self.app_module.db() as conn:
            personas = {
                row["slug"]: row["id"]
                for row in conn.execute(
                    "SELECT id,slug FROM personas WHERE slug IN ('acheng','atuo')"
                ).fetchall()
            }
            now = int(time.time())
            first = conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (personas["acheng"], "2026-08-23", "阿成一", "阿成第一条。", "needs_review",
                 "initial_batch:2026-08-23:news-01", "", now, now),
            ).lastrowid
            second = conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (personas["acheng"], context_date, "阿成二", "阿成第二条。", "needs_review",
                 f"initial_batch:{context_date}:news-02", "", now, now),
            ).lastrowid
            conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (personas["atuo"], context_date, "阿拓一", "阿拓第一条。", "needs_review",
                 f"initial_batch:{context_date}:news-01", "", now, now),
            )

        queue = self.client.get("/api/daily-posts").json()
        self.assertEqual(len(queue), 3)
        acheng_queue = [item for item in queue if item["persona_slug"] == "acheng"]
        self.assertEqual([item["id"] for item in acheng_queue], [first, second])
        self.assertEqual([item["position"] for item in acheng_queue], [1, 2])
        self.assertEqual([item["is_head"] for item in acheng_queue], [True, False])
        self.assertTrue(all(item["remaining"] == 2 for item in acheng_queue))

        skipped = self.client.post(f"/api/post-candidates/{second}/published")
        self.assertEqual(skipped.status_code, 409)
        marked = self.client.post(f"/api/post-candidates/{first}/published")
        self.assertEqual(marked.json(), {"id": first, "status": "published"})

        queue = self.client.get("/api/daily-posts").json()
        acheng_queue = [item for item in queue if item["persona_slug"] == "acheng"]
        self.assertEqual([item["id"] for item in acheng_queue], [second])
        self.assertEqual(acheng_queue[0]["position"], 1)
        self.assertTrue(acheng_queue[0]["is_head"])
        self.assertEqual(acheng_queue[0]["remaining"], 1)
        self.assertNotEqual(self.client.get("/api/daily-post").json()["id"], first)
        self.assertEqual(self.client.post(f"/api/post-candidates/{first}/published").status_code, 200)
        with self.app_module.db() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM post_candidates WHERE id=?", (first,)).fetchone()[0],
                "published",
            )

    def test_initial_batch_import_is_review_only_and_idempotent(self):
        from scripts import import_initial_drafts

        batch_dir = Path(self.temp.name) / "batch"
        batch_dir.mkdir()
        items = []
        for index in range(1, 4):
            items.append({
                "slot": f"news-{index:02d}", "kind": "news", "topic": f"时事 {index}",
                "body": f"时事待审正文 {index}", "sources": ["https://example.com/news"],
            })
        for index in range(1, 8):
            items.append({
                "slot": f"evergreen-{index:02d}", "kind": "evergreen", "topic": f"观点 {index}",
                "body": f"观点待审正文 {index}", "sources": [],
            })
        (batch_dir / "acheng.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )

        first = import_initial_drafts.import_batch(batch_dir, self.app_module.shanghai_today())
        second = import_initial_drafts.import_batch(batch_dir, self.app_module.shanghai_today())

        self.assertEqual(first, {"personas": 1, "drafts": 10, "inserted": 10})
        self.assertEqual(second["inserted"], 0)
        with self.app_module.db() as conn:
            rows = conn.execute(
                "SELECT status,source,notes FROM post_candidates ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["status"] == "needs_review" for row in rows))
        self.assertTrue(all(row["source"].startswith("initial_batch:") for row in rows))
        self.assertTrue(all(json.loads(row["notes"])["published"] is False for row in rows))

    def test_editorial_fingerprint_tracks_semantic_inputs_only(self):
        with self.app_module.db() as conn:
            persona = dict(conn.execute(
                "SELECT id,slug,draft FROM personas WHERE slug='acheng'"
            ).fetchone())
        topic = {"claim_key": "fingerprint-topic", "title": "输入变化"}
        daily = {
            "context_date": "2026-08-24", "market_state": "状态", "event_clusters": "事件",
            "debates": "分歧", "evidence": "证据", "unknowns": "", "sources": [{"url": "a"}],
            "updated_at": 1,
        }
        context = {"prior_views": "旧判断", "updated_at": 1}
        first = self.app_module.editorial_topic_input_hash(topic, daily, persona, context)
        self.assertEqual(
            first,
            self.app_module.editorial_topic_input_hash(
                topic, {**daily, "updated_at": 2}, persona, {**context, "updated_at": 2}
            ),
        )
        self.assertNotEqual(
            first,
            self.app_module.editorial_topic_input_hash(
                topic, {**daily, "sources": [{"url": "b"}]}, persona, context
            ),
        )
        self.assertNotEqual(
            first,
            self.app_module.editorial_topic_input_hash(
                topic, daily, persona, {**context, "prior_views": "新判断"}
            ),
        )

    def test_editorial_claim_history_downgrades_duplicate_write(self):
        decisions = {
            "topic": {
                "status": "WRITE", "claim_key": "same-thesis", "core_claim": "同一个核心判断",
                "reason_code": "write", "rationale": "", "notice": 5, "authority": 5,
                "tension": 5, "marginal_value": 5,
            }
        }
        result = self.app_module.apply_editorial_claim_history(
            2,
            decisions,
            [{
                "claim_key": "persona:1:same-thesis", "core_claim": "同一个核心判断",
                "status": "drafted", "source": "persona_editorial:1",
            }],
        )
        self.assertEqual(result["topic"]["status"], "IGNORE")
        self.assertEqual(result["topic"]["reason_code"], "historical_duplicate")

    def test_default_editorial_pass_does_not_backfill_old_approved_days(self):
        self.create_editorial_run("2020-01-01")
        evaluator = AsyncMock(side_effect=AssertionError("historical runs must not be backfilled"))
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "evaluate_persona_editorial", evaluator
        ):
            self.assertEqual(asyncio.run(self.app_module.run_persona_editorial_pipeline()), [])
        evaluator.assert_not_awaited()

    def test_explicit_historical_run_does_not_backfill_new_evaluations(self):
        run_id = self.create_editorial_run("2020-01-03")
        evaluator = AsyncMock(side_effect=AssertionError("historical runs must not be evaluated"))
        generated = AsyncMock(side_effect=AssertionError("historical runs must not generate new drafts"))
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "evaluate_persona_editorial", evaluator
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.assertEqual(asyncio.run(self.app_module.run_persona_editorial_pipeline(run_id)), [])
        evaluator.assert_not_awaited()
        generated.assert_not_awaited()
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM persona_editorial_evaluations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 0)

    def test_editorial_reapproval_updates_formal_context(self):
        context_date = "2026-08-19"
        self.create_editorial_run(context_date, market_state="第一次批准")
        reviewed = self.client.put(
            f"/api/context/daily-runs/{context_date}/review",
            json={"market_state": "第二次批准", "sources": []},
        )
        self.assertEqual(reviewed.status_code, 200)
        approved = self.client.post(f"/api/context/daily-runs/{context_date}/approve")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/context/daily/{context_date}").json()["market_state"],
            "第二次批准",
        )

    def test_editorial_evaluator_failure_does_not_block_other_personas(self):
        run_id = self.create_editorial_run("2026-08-18")
        generated = AsyncMock(return_value={"post": "后续人设仍然生成。"})

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            if persona["slug"] == "acheng":
                raise RuntimeError("one persona failed")
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="second-persona-thesis", core_claim="第二个人设的独立判断"
            )

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_editorial_collision_is_transitive(self):
        run_id = self.create_editorial_run("2026-08-17")
        now = int(time.time())
        with self.app_module.db() as conn:
            personas = conn.execute("SELECT id,slug FROM personas ORDER BY id LIMIT 3").fetchall()
            values = [
                (personas[0]["id"], "claim-a", "核心甲", 5),
                (personas[1]["id"], "claim-a", "核心乙", 4),
                (personas[2]["id"], "claim-c", "核心乙", 3),
            ]
            for index, (persona_id, claim_key, core_claim, score) in enumerate(values):
                conn.execute(
                    """INSERT INTO persona_editorial_evaluations(
                        run_id,persona_id,topic_input_hash,topic_json,status,notice,authority,tension,marginal_value,
                        why_me,claim_key,core_claim,reason_code,rationale,open_loop,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, persona_id, f"transitive-{index}", "{}", "WRITE", score, score, score, score,
                     "匹配", claim_key, core_claim, "write", "", "", now, now),
                )
        self.app_module.resolve_persona_editorial_collisions(run_id)
        with self.app_module.db() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM persona_editorial_evaluations WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()]
        self.assertEqual(statuses.count("WRITE"), 1)
        self.assertEqual(statuses.count("HOLD"), 2)

    def test_historical_pending_write_recovers_without_historical_backfill(self):
        context_date = "2020-01-02"
        topic = {
            "claim_key": "historical-pending", "subject": "恢复", "title": "历史待恢复",
            "core_claim": "这条已经完成编辑判断。", "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        self.insert_pending_editorial_write(run_id, context_date, topic)
        evaluator = AsyncMock(side_effect=AssertionError("historical context must not be re-evaluated"))
        generated = AsyncMock(return_value={"post": "只恢复已有 WRITE。"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(self.app_module, "evaluate_persona_editorial", evaluator), patch.object(
            self.app_module, "generate_persona_post", generated
        ):
            asyncio.run(self.app_module.run_persona_editorial_pipeline())
            asyncio.run(self.app_module.run_persona_editorial_pipeline())
        evaluator.assert_not_awaited()
        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_pending_recovery_arbitrates_collisions_before_generation(self):
        context_date = "2020-01-04"
        topic = {
            "claim_key": "recovery-collision", "subject": "恢复", "title": "恢复撞题",
            "core_claim": "公共恢复题", "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        self.insert_pending_editorial_write(
            run_id, context_date, topic, slug="acheng",
            claim_key="same-recovery-claim", core_claim="第一种措辞",
        )
        self.insert_pending_editorial_write(
            run_id, context_date, topic, slug="ridehail-driver-zhao",
            claim_key="same-recovery-claim", core_claim="第二种措辞",
        )
        evaluator = AsyncMock(side_effect=AssertionError("historical recovery must not evaluate"))
        generated = AsyncMock(return_value={"post": "只生成仲裁赢家"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
        }), patch.object(self.app_module, "evaluate_persona_editorial", evaluator), patch.object(
            self.app_module, "generate_persona_post", generated
        ):
            asyncio.run(self.app_module.run_persona_editorial_pipeline(run_id))
            asyncio.run(self.app_module.run_persona_editorial_pipeline(run_id))

        evaluator.assert_not_awaited()
        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            decisions = [tuple(row) for row in conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()]
            self.assertEqual(sum(status == "WRITE" for status, _reason in decisions), 1)
            self.assertIn(("HOLD", "cross_persona_collision"), decisions)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_later_claim_memory_supersedes_older_pending_duplicate(self):
        old_date = "2026-08-01"
        new_date = "2026-08-02"
        old_topic = {
            "claim_key": "old-pending-duplicate", "subject": "跨日重复", "title": "旧待恢复",
            "core_claim": "公共旧主张", "eligible": True,
        }
        old_run = self.create_editorial_run(old_date, topics=[old_topic])
        old_id = self.insert_pending_editorial_write(
            old_run, old_date, old_topic,
            claim_key="old-pending-claim", core_claim="跨日相同核心判断",
        )
        new_topic = {
            "claim_key": "newer-duplicate", "subject": "跨日重复", "title": "较新候选",
            "core_claim": "公共新主张", "eligible": True,
        }
        new_run = self.create_editorial_run(new_date, topics=[new_topic])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="newer-claim", core_claim="跨日相同核心判断"
            )

        generated = AsyncMock(return_value={"post": "较新的候选"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(new_run)
            asyncio.run(self.app_module.run_persona_editorial_pipeline())
        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            old = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE id=?", (old_id,)
            ).fetchone()
            self.assertEqual(tuple(old), ("HOLD", "historical_duplicate_before_generation"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_changed_context_supersedes_pending_write_before_generation(self):
        context_date = "2026-08-16"
        topic = {
            "claim_key": "changed-input", "subject": "变化", "title": "Context 变化",
            "core_claim": "旧公共主张", "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic], market_state="旧市场状态")
        old_id = self.insert_pending_editorial_write(
            run_id, context_date, topic, claim_key="old-pending", core_claim="旧输入下的判断"
        )
        self.app_module.save_daily_context(
            context_date,
            self.app_module.DailyMarketContextIn(
                market_state="新市场状态", event_clusters="新事件", debates="新分歧",
                evidence="新证据", unknowns="", raw_feed="", sources=[],
            ),
        )
        generated = AsyncMock(return_value={"post": "只使用新 Context 的正文。"})

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="new-input-claim", core_claim="新输入下的判断"
            )

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
        self.assertEqual(generated.await_count, 1)
        self.assertIn("新市场状态", generated.await_args.args[1].facts)
        self.assertNotIn("旧市场状态", generated.await_args.args[1].facts)
        with self.app_module.db() as conn:
            old = conn.execute(
                "SELECT status,reason_code,candidate_id FROM persona_editorial_evaluations WHERE id=?",
                (old_id,),
            ).fetchone()
            self.assertEqual(tuple(old), ("HOLD", "input_changed_before_generation", None))

    def test_reopening_approved_context_hides_and_supersedes_old_candidate(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "reopen-topic", "subject": "重审", "title": "重新审核",
            "core_claim": "第一次判断", "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="reopen-first", core_claim="第一次人设判断"
            )

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "第一次草稿"})
        ):
            self.run_editorial_pipeline(run_id)
        self.assertEqual(self.client.get("/api/daily-post").status_code, 200)
        reviewed = self.client.put(
            f"/api/context/daily-runs/{context_date}/review",
            json={"market_state": "重新审核后的 Context", "sources": []},
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(self.client.get("/api/daily-post").status_code, 404)
        self.assertEqual(self.client.get("/api/daily-posts").json(), [])
        with self.app_module.db() as conn:
            evaluation = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchone()
            candidate = conn.execute("SELECT status FROM post_candidates").fetchone()
            self.assertEqual(tuple(evaluation), ("HOLD", "context_revised"))
            self.assertEqual(candidate["status"], "superseded")

    def test_reapproval_same_content_uses_a_new_approval_revision(self):
        context_date = "2026-08-12"
        topic = {
            "claim_key": "same-content-reapproval", "subject": "重批", "title": "同内容重批",
            "core_claim": "同一份正式内容", "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        calls = []

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            calls.append(True)
            return self.editorial_decision(
                topics[0], "WRITE", claim_key=f"revision-{len(calls)}",
                core_claim=f"第 {len(calls)} 个审批周期的判断"
            )

        generated = AsyncMock(side_effect=[{"post": "第一版"}, {"post": "第二版"}])
        env = {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}
        with patch.dict(os.environ, env), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
            daily = self.client.get(f"/api/context/daily/{context_date}").json()
            reviewed = self.client.put(
                f"/api/context/daily-runs/{context_date}/review",
                json={key: daily[key] for key in (
                    "market_state", "event_clusters", "debates", "evidence", "unknowns", "sources"
                )},
            )
            self.assertEqual(reviewed.status_code, 200)
            approved = self.client.post(f"/api/context/daily-runs/{context_date}/approve")
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["approval_revision"], 1)
            self.run_editorial_pipeline(run_id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM post_candidates ORDER BY id"
            ).fetchall()]
        self.assertEqual(statuses, ["superseded", "needs_review"])

    def test_context_change_during_generation_discards_stale_result(self):
        context_date = "2026-08-11"
        topic = {
            "claim_key": "mid-generation-change", "subject": "并发变化", "title": "生成中变化",
            "core_claim": "生成前的公共主张", "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic], market_state="生成前状态")

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="mid-generation-claim", core_claim="生成前的人设判断"
            )

        async def generated(_persona_id, _request):
            self.app_module.save_daily_context(
                context_date,
                self.app_module.DailyMarketContextIn(
                    market_state="生成期间改写", event_clusters="新事件", debates="新分歧",
                    evidence="新证据", unknowns="", raw_feed="", sources=[],
                ),
            )
            return {"post": "这条结果必须丢弃"}

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", AsyncMock(side_effect=generated)):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            evaluation = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchone()
            self.assertEqual(tuple(evaluation), ("HOLD", "input_changed_during_generation"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 0)

    def test_approval_rolls_back_formal_context_and_status_together(self):
        context_date = "2026-08-15"
        now = int(time.time())
        with self.app_module.db() as conn:
            conn.execute(
                """INSERT INTO daily_context_runs(
                    context_date,status,trigger,raw_cards,synthesis,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (context_date, "needs_review", "test", "{}", json.dumps({"market_state": "待批准"}), now, now),
            )
        original = self.app_module.save_daily_context_row

        def fail_after_upsert(conn, date, request, timestamp):
            original(conn, date, request, timestamp)
            raise RuntimeError("simulated crash")

        with patch.object(self.app_module, "save_daily_context_row", side_effect=fail_after_upsert):
            with self.assertRaises(RuntimeError):
                self.app_module.approve_daily_context_run(context_date)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM daily_context_runs WHERE context_date=?", (context_date,)
            ).fetchone()[0], "needs_review")
            self.assertIsNone(conn.execute(
                "SELECT id FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())

    def test_editorial_generation_facts_are_bounded_valid_json(self):
        context_date = "2026-08-14"
        topic = {
            "claim_key": "large-input", "subject": "大输入", "title": "大输入",
            "core_claim": "公共主张", "material_delta": "变化" * 5000,
            "audience_value": "价值" * 5000, "eligible": True,
        }
        run_id = self.create_editorial_run(
            context_date, topics=[topic], market_state="状态" * 5000
        )

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="bounded-facts", core_claim="大输入下的明确判断"
            )

        generated = AsyncMock(return_value={"post": "正文"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
        facts = generated.await_args.args[1].facts
        self.assertLessEqual(len(facts), 8000)
        self.assertIsInstance(json.loads(facts), dict)

    def test_editorial_writer_failure_does_not_block_later_candidates(self):
        topic = {
            "claim_key": "writer-failure", "subject": "失败隔离", "title": "失败隔离",
            "core_claim": "公共主张", "eligible": True,
        }
        run_id = self.create_editorial_run("2026-08-13", topics=[topic])

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key=f"writer-{persona['slug']}",
                core_claim=f"{persona['slug']} 的独立判断"
            )

        generated = AsyncMock(side_effect=[
            self.app_module.HTTPException(502, "first failed"), {"post": "第二条仍生成"},
        ])
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_persona_draft_and_published_version(self):
        personas = self.client.get("/api/personas").json()
        persona_id = personas[0]["id"]
        persona = self.client.get(f"/api/personas/{persona_id}").json()
        persona["draft"]["voice"]["tone"] = "真实、短句、不过度包装"

        saved = self.client.put(
            f"/api/personas/{persona_id}", json={"data": persona["draft"]}
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["status"], "draft")

        published = self.client.post(f"/api/personas/{persona_id}/publish")
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["version"], 1)

        refreshed = self.client.get(f"/api/personas/{persona_id}").json()
        self.assertEqual(refreshed["status"], "published")
        self.assertEqual(refreshed["versions"][0]["version"], 1)

    def test_persona_prompt_preview_uses_voice(self):
        persona = self.client.get("/api/personas/1").json()
        response = self.client.post(
            "/api/personas/1/prompt-preview", json={"data": persona["draft"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("文风与口风", response.json()["prompt"])
        self.assertIn(persona["draft"]["voice"]["tone"], response.json()["prompt"])
        self.assertIn("偶尔使用", persona["draft"]["voice"]["style_guide"])
        self.assertIn("不设固定句式", response.json()["prompt"])
        self.assertIn("兄弟们，这个可以冲了", response.json()["prompt"])
        self.assertIn("禁止整段复用", response.json()["prompt"])

    def test_generate_post_requires_facts(self):
        response = self.client.post("/api/personas/1/generate-post", json={"facts": ""})
        self.assertEqual(response.status_code, 422)

    def test_context_crud_and_pack_assembly(self):
        pump = self.client.get("/api/context/projects/pump-fun")
        self.assertEqual(pump.status_code, 200)
        self.assertIn("不包含实时", pump.json()["current_state"])

        project = self.client.put(
            "/api/context/projects/test-protocol",
            json={
                "name": "Test Protocol",
                "aliases": ["TEST"],
                "audience_baseline": "读者知道基础链上概念",
                "native_context": "用户关注真实使用成本",
                "market_structure": "流动性与估值分开看",
                "recurring_debates": "收入能否持续",
                "current_state": "运营者填写的当前状态",
                "sources": [{"url": "https://example.com"}],
            },
        )
        self.assertEqual(project.status_code, 200)
        self.assertEqual(project.json()["aliases"], ["TEST"])

        persona_context = self.client.put(
            "/api/personas/1/context",
            json={
                "prior_views": "此前认为收入和代币捕获应分开验证。",
                "watchlist": "Test Protocol",
                "unresolved": "真实用户留存",
                "forbidden_claims": "不得写成持仓",
            },
        )
        self.assertEqual(persona_context.status_code, 200)

        self.assertEqual(
            self.client.post(
                "/api/personas/1/context-packs",
                json={"topic": "测试主题", "project_slugs": ["test-protocol"]},
            ).status_code,
            422,
        )
        daily = self.client.put(
            "/api/context/daily/2026-08-24",
            json={
                "market_state": "市场在等待新催化。",
                "event_clusters": "测试协议出现新讨论。",
                "debates": "收入和估值是否匹配。",
                "evidence": "输入来源显示活动上升。",
                "unknowns": "留存尚未确认。",
                "raw_feed": "RAW_AUTHOR_EXPERIENCE_SHOULD_NOT_REACH_PACK",
                "sources": [{"url": "https://x.com/example"}],
            },
        )
        self.assertEqual(daily.status_code, 200)

        missing = self.client.post(
            "/api/personas/1/context-packs",
            json={"topic": "测试主题", "project_slugs": ["does-not-exist"]},
        )
        self.assertEqual(missing.status_code, 422)

        pack = self.client.post(
            "/api/personas/1/context-packs",
            json={
                "topic": "测试主题",
                "project_slugs": ["test-protocol"],
                "operator_notes": "只讨论收入持续性。",
            },
        )
        self.assertEqual(pack.status_code, 200)
        self.assertEqual(pack.json()["context_date"], "2026-08-24")
        self.assertEqual(pack.json()["content"]["project_dossiers"][0]["slug"], "test-protocol")
        self.assertEqual(pack.json()["content"]["discussion_topics"], [])
        self.assertEqual(pack.json()["content"]["attention_topics"], [])
        self.assertEqual(pack.json()["content"]["opportunity_questions"], [])
        self.assertIsNone(pack.json()["content"]["selected_opportunity_question"])
        self.assertEqual(pack.json()["content"]["editorial_questions"], [])
        self.assertIsNone(pack.json()["content"]["selected_editorial_question"])
        self.assertEqual(pack.json()["content"]["topic_attention"]["status"], "custom_or_niche")
        self.assertIn("此前认为", pack.json()["content"]["account_continuity"]["prior_views"])
        self.assertNotIn("raw_feed", pack.json()["content"]["daily_market"])
        self.assertNotIn("RAW_AUTHOR_EXPERIENCE_SHOULD_NOT_REACH_PACK", json.dumps(pack.json()["content"]))
        updated = self.client.put(
            f"/api/context-packs/{pack.json()['id']}", json={"operator_notes": "不要写成推荐。"}
        )
        self.assertEqual(updated.json()["content"]["operator_notes"], "不要写成推荐。")
        edited_content = updated.json()["content"]
        edited_content["unknowns"] = "等待低热度窗口验证。"
        edited = self.client.put(
            f"/api/context-packs/{pack.json()['id']}", json=edited_content
        )
        self.assertEqual(edited.json()["content"]["unknowns"], "等待低热度窗口验证。")
        invalid = self.client.put(
            f"/api/context-packs/{pack.json()['id']}", json={"content": edited_content}
        )
        self.assertEqual(invalid.status_code, 422)

    def test_context_pack_isolation_and_generation_prompt(self):
        self.client.put(
            "/api/context/daily/2026-08-24",
            json={
                "market_state": "Meme 交易活跃",
                "unknowns": "持续性未知",
                "raw_feed": "RAW_NOISE_MUST_NOT_REACH_PROMPT",
            },
        )
        first = self.client.post(
            "/api/personas/1/context-packs",
            json={"topic": "Pump.fun", "project_slugs": ["pump-fun"]},
        ).json()
        second_persona = self.client.get("/api/personas").json()[1]["id"]
        wrong = self.client.post(
            f"/api/personas/{second_persona}/generate-post",
            json={"context_pack_id": first["id"]},
        )
        self.assertEqual(wrong.status_code, 422)

        calls = []
        payload = {"choices": [{"message": {"content": "这是一条基于语境的观察。"}}]}
        factory = lambda **_kwargs: FakeAsyncClient(payload, calls)
        with patch.object(self.app_module, "llm_api_key", return_value="test"), patch.object(
            self.app_module.httpx, "AsyncClient", factory
        ):
            response = self.client.post(
                "/api/personas/1/generate-post",
                json={"context_pack_id": first["id"], "facts": "补充事实"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context_pack_id"], first["id"])
        prompt = calls[0]["kwargs"]["json"]["messages"][0]["content"]
        self.assertIn("每日市场状态", prompt)
        self.assertIn("项目长期语境", prompt)
        self.assertIn("账号连续性", prompt)
        self.assertIn("未知与过期提示", prompt)
        self.assertIn("读者机会题", prompt)
        self.assertIn("观点 / 乐子题", prompt)
        self.assertIn("当天可写讨论议题", prompt)
        self.assertIn("当天父级热度地图", prompt)
        self.assertIn("选题：\nPump.fun", prompt)
        self.assertIn("Pump.fun", prompt)
        self.assertIn("把因果链讲完整", prompt)
        self.assertIn("不能用", prompt)
        self.assertIn("无信息占位收尾", prompt)
        self.assertIn("结尾必须交付当下成立的判断", prompt)
        self.assertIn("再看正式文本", prompt)
        self.assertNotIn("RAW_NOISE_MUST_NOT_REACH_PROMPT", prompt)

    def test_generation_rejects_empty_waiting_language(self):
        calls = []
        payload = {"choices": [{"message": {"content": "我会关注，再看正式文本。"}}]}
        factory = lambda **_kwargs: FakeAsyncClient(payload, calls)
        with patch.object(self.app_module, "llm_api_key", return_value="test"), patch.object(
            self.app_module.httpx, "AsyncClient", factory
        ):
            response = self.client.post(
                "/api/personas/1/generate-post",
                json={"facts": "项目刚公布一项明确费用调整。"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Post 缺少当前结论")
        self.assertEqual(len(calls), 2)

    def test_daily_synthesis_returns_unsaved_preview(self):
        calls = []
        content = json.dumps(
            {
                "market_state": "市场等待流动性方向。",
                "event_clusters": "Pump.fun 讨论增加。",
                "debates": "收入能否变成持久价值仍有分歧。",
                "evidence": "原始信息中有链接和公开数据线索。",
                "unknowns": "没有完整留存数据。",
                "sources": [{"url": "https://x.com/example"}],
            },
            ensure_ascii=False,
        )
        payload = {"choices": [{"message": {"content": content}}]}
        factory = lambda **_kwargs: FakeAsyncClient(payload, calls)
        with patch.object(self.app_module, "llm_api_key", return_value="test"), patch.object(
            self.app_module.httpx, "AsyncClient", factory
        ):
            response = self.client.post(
                "/api/context/daily/2026-08-24/synthesize", json={"raw_feed": "一条母池推文"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unknowns"], "没有完整留存数据。")
        self.assertEqual(response.json()["date"], "2026-08-24")
        self.assertEqual(self.client.get("/api/context/daily/2026-08-24").status_code, 404)
        prompt = calls[0]["kwargs"]["json"]["messages"][0]["content"]
        self.assertIn("确认事实、市场解读、分歧和未知", prompt)

    def test_daily_card_synthesis_never_turns_opinions_into_facts(self):
        calls = []
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "market_state": "BTC rallied after the announcement.",
                                "event_clusters": "The protocol launched a new product.",
                                "debates": [
                                    {"topic": "Pump.fun", "view": "这轮讨论集中在收入是否能持续。"},
                                    {"topic": "SGP-0003", "view": "这个冷门提案不该进入主线。"},
                                ],
                                "evidence": "Several traders said so.",
                                "unknowns": "Unknown.",
                                "sources": [{"url": "https://outside.example"}],
                            }
                        )
                    }
                }
            ]
        }
        cards = {
            "fact_cards": [],
            "opinion_cards": [{"text": "这只是本轮观点", "source_lists": ["crypto"]}],
            "discussion_topics": [{"title": "Pump.fun｜交易活动", "unique_authors": 12, "post_count": 20}],
            "attention_topics": [{"title": "Solana", "unique_authors": 40, "post_count": 80}],
            "excluded_niche_topics": [{"title": "SGP-0003", "unique_authors": 2, "post_count": 2}],
            "coverage": {"cross_validate": {"opinion_cards": 200}},
        }
        factory = lambda **_kwargs: FakeAsyncClient(payload, calls)
        with patch.object(self.app_module, "llm_api_key", return_value="test"), patch.object(
            self.app_module.httpx, "AsyncClient", factory
        ):
            synthesis = asyncio.run(self.app_module.synthesize_daily_cards("2026-08-24", cards))
        self.assertIn("讨论面与注意力结构", synthesis["market_state"])
        self.assertIn("筛出 200 条观点卡", synthesis["market_state"])
        self.assertIn("使用其中 1 条受控样本", synthesis["market_state"])
        self.assertNotIn("BTC rallied", synthesis["market_state"])
        self.assertIn("未产出通过多源验证的事实卡", synthesis["evidence"])
        self.assertIn("不能作为事实证据", synthesis["evidence"])
        self.assertIn("24 小时母池讨论热度（非事实确认）", synthesis["event_clusters"])
        self.assertIn("Pump.fun｜交易活动（12 位作者、20 条帖子）", synthesis["event_clusters"])
        self.assertNotIn("Solana", synthesis["event_clusters"])
        self.assertNotIn("protocol launched", synthesis["event_clusters"])
        self.assertIn("Pump.fun", synthesis["debates"])
        self.assertNotIn("SGP-0003", synthesis["debates"])
        self.assertEqual(synthesis["sources"], [{"source_list": "crypto"}])
        prompt = calls[0]["kwargs"]["json"]["messages"][0]["content"]
        self.assertIn("所有字段必须使用中文", prompt)
        self.assertIn("discussion_topics", prompt)
        self.assertIn("父级市场地图", prompt)
        self.assertIn("只有 discussion_topics 为空时", prompt)

    def test_controlled_cards_keeps_top_twenty_discussion_and_attention_topics(self):
        cards = self.app_module.controlled_cards(
            [],
            [],
            {},
            [{"title": f"热点 {index}", "key": f"hot-{index}", "unique_authors": index} for index in range(25)],
            None,
            [{"title": f"可写议题 {index}", "key": f"hot-{index}:listing", "unique_authors": index} for index in range(25)],
        )
        self.assertEqual(len(cards["discussion_topics"]), 20)
        self.assertEqual(cards["discussion_topics"][0]["title"], "可写议题 0")
        self.assertEqual(len(cards["attention_topics"]), 20)
        self.assertEqual(cards["attention_topics"][0]["title"], "热点 0")

    def test_topic_selection_policy_and_history_are_persisted(self):
        policy = self.app_module.topic_selection_policy()
        self.assertIn("历史", "".join(policy["required_gates"]))
        self.assertEqual(policy["slate_guidance"]["dedupe_unit"], "去重单位是核心主张，不是事件、项目、币种或题材。")
        self.assertIn("不设", policy["content_inspiration"]["rule"])
        claims = self.app_module.recent_topic_claims()
        self.assertIn(
            "hyperliquid-builder-codes-distribution",
            {item["claim_key"] for item in claims},
        )

    def test_screened_topics_reject_known_claim_and_keep_material_delta(self):
        cards = {
            "discussion_topics": [{"key": "hyperliquid:market_structure"}],
            "claim_history": [
                {
                    "claim_key": "hyperliquid-builder-codes-distribution",
                    "core_claim": "钱包成为交易分发渠道。",
                }
            ],
        }
        selected, rejected = self.app_module.bounded_selected_topics(
            {
                "selected_topics": [
                    {
                        "claim_key": "hyperliquid-builder-codes-distribution",
                        "subject": "Hyperliquid",
                        "title": "Builder Codes 让钱包成为分发渠道",
                        "core_claim": "钱包成为交易分发渠道。",
                        "content_type": "research",
                        "kind": "competition",
                        "source_topic_keys": ["hyperliquid:market_structure"],
                        "fact_basis": "当天成交数据",
                        "opinion_basis": "平台化观点",
                        "material_delta": "只有数字更新",
                        "audience_value": "理解平台战略",
                        "why_now": "当天讨论",
                        "persona_fit": ["atuo"],
                    },
                    {
                        "claim_key": "hyperliquid-builder-margin-compression",
                        "subject": "Hyperliquid",
                        "title": "Builder 分成开始压低协议净收入",
                        "core_claim": "新增分成使成交增长与协议净收入出现背离。",
                        "content_type": "research",
                        "kind": "unit_economics",
                        "source_topic_keys": ["hyperliquid:market_structure"],
                        "fact_basis": "成交与净收入变化",
                        "opinion_basis": "飞轮可能被稀释",
                        "material_delta": "新数据改变了收入增长判断",
                        "audience_value": "重新评估价值归属",
                        "why_now": "收入背离首次出现",
                        "persona_fit": ["xiaoman"],
                    },
                ],
                "rejected_topics": [],
            },
            cards,
        )
        self.assertEqual([item["claim_key"] for item in selected], ["hyperliquid-builder-margin-compression"])
        self.assertEqual(rejected[0]["reason_code"], "historical_duplicate")

    def test_screened_topic_can_use_high_quality_opinion_as_source(self):
        cards = {
            "discussion_topics": [],
            "opinion_cards": [{"source_ref": "123", "text": "当天高质量观点"}],
            "claim_history": [],
        }
        selected, rejected = self.app_module.bounded_selected_topics(
            {
                "selected_topics": [{
                    "claim_key": "meme-daily-close",
                    "subject": "Meme 交易",
                    "title": "二段胜率低，日结比猜龙头更重要",
                    "core_claim": "样本显示二段交易的赔率明显差于日内兑现。",
                    "content_type": "opportunity",
                    "kind": "trade_process",
                    "source_topic_keys": ["opinion:123"],
                    "fact_basis": "母池作者的两日样本",
                    "opinion_basis": "日结优先",
                    "material_delta": "新增样本给出赔率差异",
                    "audience_value": "改变短线兑现纪律",
                    "why_now": "当天 Meme 轮动加速",
                    "persona_fit": ["aye"],
                }],
                "rejected_topics": [],
            },
            cards,
        )
        self.assertEqual([item["claim_key"] for item in selected], ["meme-daily-close"])
        self.assertEqual(rejected, [])

    def test_screened_editorial_can_use_evergreen_inspiration(self):
        policy = self.app_module.topic_selection_policy()
        selected, rejected = self.app_module.bounded_selected_topics(
            {
                "selected_topics": [{
                    "claim_key": "livermore-overtrading",
                    "subject": "交易耐心",
                    "title": "真正难的不是看对，而是看对以后别乱动",
                    "core_claim": "过度交易会让人主动丢掉原本正确的趋势判断。",
                    "content_type": "editorial",
                    "kind": "trading_philosophy",
                    "source_topic_keys": ["evergreen:livermore-trend-and-patience"],
                    "fact_basis": "公开方法论转述，不使用直接引语。",
                    "opinion_basis": "耐心本身是交易能力。",
                    "material_delta": "结合人设形成独立表达。",
                    "audience_value": "重新理解过度交易。",
                    "why_now": "人设当下确实有这个表达冲动。",
                    "persona_fit": ["maili"],
                }],
                "rejected_topics": [],
            },
            {"topic_selection_policy": policy, "claim_history": []},
        )
        self.assertEqual([item["claim_key"] for item in selected], ["livermore-overtrading"])
        self.assertEqual(rejected, [])

    def test_opportunity_questions_are_deterministic_and_conservative(self):
        topics = [
            {"key": "rwa:tokenized_equities", "title": "RWA｜代币化股票与流动性", "mechanism": {"key": "tokenized_equities"}, "unique_authors": 4, "post_count": 6, "sample_posts": [{"text": "LP APY 200%"}]},
            {"key": "bitcoin:market_structure", "title": "Bitcoin｜价格与市场结构", "mechanism": {"key": "market_structure"}, "unique_authors": 5, "post_count": 8},
            {"key": "hyperliquid:revenue_buyback", "title": "Hyperliquid｜收入与回购", "mechanism": {"key": "revenue_buyback"}, "unique_authors": 3, "post_count": 5},
            {"key": "stablecoin:stablecoin_payments", "title": "稳定币｜稳定币与支付", "mechanism": {"key": "stablecoin_payments"}, "unique_authors": 9, "post_count": 12},
        ]
        questions = self.app_module.build_opportunity_questions(topics)
        self.assertEqual([item["kind"] for item in questions], ["liquidity_activity", "short_term_trade", "trend_position"])
        self.assertEqual(questions[0]["source_topic_keys"], ["rwa:tokenized_equities"])
        self.assertTrue(questions[0]["eligible"])
        self.assertEqual(questions[0]["status"], "needs_live_research")
        self.assertEqual(questions[0]["title"], "小资金 LP｜代币化股票池现在有没有活动可以冲？")
        self.assertEqual(questions[1]["title"], "短线交易｜BTC 这波还有没有参与空间？")
        self.assertEqual([item["priority"] for item in questions], [1, 2, 3])
        self.assertIsInstance(questions[0]["research_brief"], list)

    def test_editorial_questions_are_hot_specific_and_do_not_invent_people(self):
        topics = [
            {"key": "bitcoin:market_structure", "title": "Bitcoin｜价格与市场结构", "parent": {"title": "Bitcoin"}, "mechanism": {"key": "market_structure"}, "unique_authors": 8, "post_count": 12, "sample_posts": [{"source_ref": "btc-1", "text": "BTC price action"}]},
            {"key": "rwa:tokenized_equities", "title": "RWA｜代币化股票与流动性", "parent": {"title": "RWA"}, "mechanism": {"key": "tokenized_equities"}, "unique_authors": 6, "post_count": 9},
            {"key": "stablecoin:stablecoin_payments", "title": "稳定币｜稳定币与支付", "parent": {"title": "稳定币"}, "mechanism": {"key": "stablecoin_payments"}, "unique_authors": 3, "post_count": 5, "cross_list_count": 2},
            {"key": "hyperliquid:revenue_buyback", "title": "Hyperliquid｜收入与回购", "parent": {"title": "Hyperliquid"}, "mechanism": {"key": "revenue_buyback"}, "unique_authors": 5, "post_count": 8, "public_actor": {"name": "基金会", "action_in_samples": True}},
            {"key": "solana:market_structure", "title": "Solana｜价格与市场结构", "parent": {"title": "Solana"}, "mechanism": {"key": "market_structure"}, "unique_authors": 9, "post_count": 12},
            {"key": "meme:meme_ecosystem", "title": "Meme｜生态", "mechanism": {"key": "meme_ecosystem"}, "unique_authors": 1, "post_count": 3},
        ]
        questions = self.app_module.build_editorial_questions(topics)
        self.assertEqual(
            [item["kind"] for item in questions],
            ["trading_philosophy", "wealth_view", "ct_culture", "wealth_view", "public_strategy_read"],
        )
        self.assertIn("BTC", questions[0]["title"])
        self.assertIn("突然觉得自己看懂了市场", questions[0]["title"])
        self.assertIn("高 APY", questions[1]["title"])
        self.assertIn("用户还需要知道自己用了 Crypto", questions[2]["title"])
        self.assertIn("回购能力", questions[3]["title"])
        self.assertEqual(questions[0]["source_sample_refs"], ["btc-1"])
        self.assertEqual(questions[0]["status"], "editorial_ready")
        self.assertTrue(questions[0]["eligible"])
        self.assertNotIn("人物", " ".join(item["title"] for item in questions if item["kind"] != "public_strategy_read"))
        self.assertEqual([item["priority"] for item in questions], [1, 2, 3, 4, 5])

    def test_research_questions_are_hot_specific_varied_and_bounded(self):
        topics = [
            {"key": "rwa:tokenized_equities", "title": "RWA｜代币化股票与流动性", "mechanism": {"key": "tokenized_equities"}, "unique_authors": 6, "post_count": 9},
            {"key": "stablecoin:stablecoin_payments", "title": "稳定币｜稳定币支付", "mechanism": {"key": "stablecoin_payments"}, "unique_authors": 5, "post_count": 8},
            {"key": "hyperliquid:revenue_buyback", "title": "Hyperliquid｜收入与回购", "parent": {"title": "Hyperliquid"}, "mechanism": {"key": "revenue_buyback"}, "unique_authors": 5, "post_count": 8},
            {"key": "bitcoin:market_structure", "title": "Bitcoin｜价格与市场结构", "parent": {"title": "Bitcoin"}, "mechanism": {"key": "market_structure"}, "unique_authors": 8, "post_count": 12},
            {"key": "solana:fee_model", "title": "Solana｜费用模型", "parent": {"title": "Solana"}, "mechanism": {"key": "fee_model"}, "unique_authors": 4, "post_count": 6},
            {"key": "rwa:regulation", "title": "RWA｜监管与准入", "mechanism": {"key": "regulation"}, "unique_authors": 3, "post_count": 5},
            {"key": "cold:regulation", "title": "冷门｜监管与准入", "mechanism": {"key": "regulation"}, "unique_authors": 1, "post_count": 1},
        ]
        questions = self.app_module.build_research_questions(topics)
        self.assertEqual(
            [item["kind"] for item in questions],
            ["industry_structure", "adoption", "competition", "adoption", "unit_economics", "valuation", "market_structure", "cycle", "unit_economics", "thesis_check", "thesis_check"],
        )
        self.assertEqual(questions[0]["title"], "行业研究｜代币化股票的流动性，到底靠什么撑起来？")
        self.assertEqual(questions[0]["source_topic_keys"], ["rwa:tokenized_equities"])
        self.assertEqual(questions[0]["status"], "needs_live_research")
        self.assertTrue(questions[0]["eligible"])
        self.assertEqual([item["priority"] for item in questions], list(range(1, 12)))
        self.assertTrue(all(len(item["research_brief"]) >= 2 for item in questions))
        self.assertTrue(all("冷门" not in item["title"] for item in questions))
        self.assertEqual(set(item["kind"] for item in questions), set(self.app_module.RESEARCH_QUESTION_KINDS))
        self.assertTrue(all(sum(item["kind"] == kind for item in questions) <= 2 for kind in self.app_module.RESEARCH_QUESTION_KINDS))

    def test_research_questions_use_topic_specific_hype_and_sol_angles(self):
        topics = [
            {"key": "hyperliquid:market_structure", "title": "Hyperliquid｜价格与市场结构", "mechanism": {"key": "market_structure"}, "unique_authors": 6, "post_count": 6},
            {"key": "solana:market_structure", "title": "Solana｜价格与市场结构", "mechanism": {"key": "market_structure"}, "unique_authors": 5, "post_count": 7},
        ]
        questions = self.app_module.build_research_questions(topics)
        titles = [item["title"] for item in questions]
        self.assertTrue(any("Builder Codes" in title and "改变了 Hyperliquid 哪一层" in title for title in titles))
        self.assertTrue(any("SOL 的市场热度" in title for title in titles))
        self.assertFalse(any("HYPE 这轮行情是谁在定价" in title for title in titles))

    def test_research_titles_do_not_smuggle_in_unverified_market_claims(self):
        topics = [
            {"key": "rwa:tokenized_equities", "title": "RWA｜代币化股票与流动性", "mechanism": {"key": "tokenized_equities"}, "unique_authors": 6, "post_count": 9},
            {"key": "stablecoin:stablecoin_payments", "title": "稳定币｜稳定币支付", "mechanism": {"key": "stablecoin_payments"}, "unique_authors": 5, "post_count": 8},
            {"key": "hyperliquid:revenue_buyback", "title": "Hyperliquid｜收入与回购", "parent": {"title": "Hyperliquid"}, "mechanism": {"key": "revenue_buyback"}, "unique_authors": 5, "post_count": 8},
            {"key": "bitcoin:market_structure", "title": "Bitcoin｜价格与市场结构", "parent": {"title": "Bitcoin"}, "mechanism": {"key": "market_structure"}, "unique_authors": 8, "post_count": 12},
            {"key": "hyperliquid:market_structure", "title": "Hyperliquid｜价格与市场结构", "parent": {"title": "Hyperliquid"}, "mechanism": {"key": "market_structure"}, "unique_authors": 6, "post_count": 6},
            {"key": "solana:market_structure", "title": "Solana｜价格与市场结构", "parent": {"title": "Solana"}, "mechanism": {"key": "market_structure"}, "unique_authors": 5, "post_count": 7},
        ]
        titles = [item["title"] for item in self.app_module.build_research_questions(topics)]
        self.assertGreaterEqual(len(titles), 10)
        self.assertTrue(all(
            phrase not in title
            for title in titles
            for phrase in self.app_module.RESEARCH_TITLE_BANNED_PHRASES
        ))
        self.assertFalse(any("还是" in title for title in titles))
        self.assertFalse(any("哪些公开数据" in title for title in titles))
        self.assertFalse(any("分别在显示什么" in title for title in titles))

    def test_old_daily_run_rebuilds_stale_research_titles(self):
        topic = {
            "key": "bitcoin:market_structure",
            "title": "Bitcoin｜价格与市场结构",
            "parent": {"title": "Bitcoin"},
            "mechanism": {"key": "market_structure"},
            "unique_authors": 8,
            "post_count": 12,
        }
        row = {
            "context_date": "2026-08-24",
            "raw_manifest": "{}",
            "raw_cards": json.dumps(
                {
                    "discussion_topics": [topic],
                    "research_questions": [{"title": "市场结构｜BTC 这轮行情是谁在定价？"}],
                },
                ensure_ascii=False,
            ),
            "synthesis": json.dumps(
                {"research_questions": [{"title": "市场结构｜BTC 这轮行情是谁在定价？"}]},
                ensure_ascii=False,
            ),
        }
        rebuilt = self.app_module.daily_context_run_dict(row)
        titles = [item["title"] for item in rebuilt["raw_cards"]["research_questions"]]
        self.assertNotIn("市场结构｜BTC 这轮行情是谁在定价？", titles)
        self.assertEqual(titles, [item["title"] for item in rebuilt["synthesis"]["research_questions"]])

    def test_daily_card_synthesis_scopes_model_output_to_this_run(self):
        cards = {
            "fact_cards": [{"representative_text": "本轮事实卡", "source_lists": ["list-a"]}],
            "opinion_cards": [{"text": "本轮观点卡", "source_lists": ["list-b"]}],
            "coverage": {},
        }
        synthesis = self.app_module.bounded_daily_card_synthesis(
            {
                "market_state": "The market moved.",
                "event_clusters": "This event happened.",
                "debates": "People disagree.",
                "unknowns": "Unknown.",
                "sources": [{"url": "https://outside.example"}],
            },
            cards,
        )
        self.assertTrue(synthesis["market_state"].startswith("本轮母池的讨论面与注意力结构："))
        self.assertTrue(synthesis["event_clusters"].startswith("以下仅归纳本轮卡片提到的事件与话题："))
        self.assertTrue(synthesis["debates"].startswith("以下仅归纳本轮卡片中的解读与分歧："))
        self.assertIn("本轮有 1 条事实候选卡", synthesis["evidence"])
        self.assertEqual(synthesis["sources"], [{"source_list": "list-a"}, {"source_list": "list-b"}])

    def test_daily_context_run_is_idempotent_reviewable_and_independent_of_writes(self):
        calls = []
        run_date = self.app_module.shanghai_today()

        def collect(_accounts, _db, output, **kwargs):
            calls.append(("collect", kwargs["key"]))
            Path(output).mkdir(parents=True, exist_ok=True)
            return {"account_universe": 2, "accounts_fetched": 2, "posts_seen": 4}

        def cross_validate(_db, output, **_kwargs):
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True)
            (output / "fact_cards.json").write_text(
                json.dumps({"cards": [{"status": "candidate", "representative_text": "事实卡", "author_count": 2}]}),
                encoding="utf-8",
            )
            (output / "opinion_cards.json").write_text(
                json.dumps(
                    {
                        "opinions": [
                            {"text": f"观点卡 {index}", "score": 8}
                            for index in range(130)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (output / "attention_topics.json").write_text(
                json.dumps(
                    {
                        "topics": [
                            {
                                "title": f"热点主题 {index}",
                                "key": f"hot-{index}",
                                "unique_authors": 10 - index % 3,
                                "post_count": 20 - index % 5,
                            }
                            for index in range(25)
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (output / "discussion_topics.json").write_text(
                json.dumps(
                    {
                        "hot": [
                            {
                                "title": f"热点主题 {index}｜具体机制",
                                "key": f"hot-{index}:mechanism",
                                "mechanism": {"key": "market_structure", "title": "价格与市场结构"},
                                "unique_authors": 10 - index % 3,
                                "post_count": 20 - index % 5,
                            }
                            for index in range(25)
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "source_posts": 4,
                "fact_cards": 1,
                "opinion_cards": 130,
                "attention_topics": 25,
                "discussion_topics": 25,
            }

        content = json.dumps(
            {
                "market_state": "市场有新事件。",
                "event_clusters": "事件被讨论。",
                "debates": "可持续性有分歧。",
                "evidence": "来自事实卡。",
                "unknowns": "覆盖有限。",
                "sources": [],
                "selected_topics": [
                    {
                        "claim_key": "new-opportunity",
                        "subject": "热点主题 0",
                        "title": "热点主题 0 出现新的参与条件",
                        "core_claim": "新的参与条件改变了原有机会判断。",
                        "content_type": "opportunity",
                        "kind": "short_term_trade",
                        "source_topic_keys": ["hot-0:mechanism"],
                        "fact_basis": "事实卡",
                        "opinion_basis": "观点卡",
                        "material_delta": "新的条件改变了判断",
                        "audience_value": "改变参与动作",
                        "why_now": "今日讨论",
                        "persona_fit": ["acheng"]
                    },
                    {
                        "claim_key": "new-editorial",
                        "subject": "热点主题 1",
                        "title": "热点主题 1 的新分歧改变了市场解释",
                        "core_claim": "新分歧推翻了旧解释。",
                        "content_type": "editorial",
                        "kind": "trading_philosophy",
                        "source_topic_keys": ["hot-1:mechanism"],
                        "fact_basis": "事实卡",
                        "opinion_basis": "观点卡",
                        "material_delta": "出现新的反方证据",
                        "audience_value": "改变市场理解",
                        "why_now": "今日讨论",
                        "persona_fit": ["aye"]
                    },
                    {
                        "claim_key": "new-research",
                        "subject": "热点主题 2",
                        "title": "热点主题 2 的收入归属已经改变",
                        "core_claim": "新机制改变了收入归属。",
                        "content_type": "research",
                        "kind": "unit_economics",
                        "source_topic_keys": ["hot-2:mechanism"],
                        "fact_basis": "事实卡",
                        "opinion_basis": "观点卡",
                        "material_delta": "收入归属发生变化",
                        "audience_value": "改变项目判断",
                        "why_now": "今日讨论",
                        "persona_fit": ["xiaoman"]
                    }
                ],
                "rejected_topics": []
            },
            ensure_ascii=False,
        )
        payload = {"choices": [{"message": {"content": content}}]}
        factory = lambda **_kwargs: FakeAsyncClient(payload, [])
        sources = SimpleNamespace(collect=collect, cross_validate=cross_validate)
        with patch.object(self.app_module, "market_sources_module", return_value=sources), patch.object(
            self.app_module, "twitter241_api_key", return_value="runtime-key"
        ), patch.object(self.app_module, "llm_api_key", return_value="test"), patch.object(
            self.app_module.httpx, "AsyncClient", factory
        ):
            started = self.client.post(
                f"/api/context/daily-runs/{run_date}/run"
            )
            self.assertTrue(started.json()["started"])
            first = self.wait_for_daily_run(run_date)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["status"], "needs_review")
            self.assertEqual(self.client.get(f"/api/context/daily/{run_date}").status_code, 404)
            self.assertEqual(first.json()["raw_cards"]["fact_cards"][0]["representative_text"], "事实卡")
            self.assertEqual(len(first.json()["raw_cards"]["opinion_cards"]), 130)
            self.assertEqual(len(first.json()["raw_cards"]["discussion_topics"]), 20)
            self.assertEqual(len(first.json()["raw_cards"]["opportunity_questions"]), 1)
            self.assertEqual(len(first.json()["raw_cards"]["editorial_questions"]), 1)
            self.assertEqual(len(first.json()["raw_cards"]["research_questions"]), 1)
            self.assertEqual(len(first.json()["raw_cards"]["question_candidates"]["opportunity"]), 20)
            self.assertEqual(len(first.json()["raw_cards"]["attention_topics"]), 20)
            question_title = first.json()["raw_cards"]["opportunity_questions"][0]["title"]
            editorial_title = first.json()["raw_cards"]["editorial_questions"][0]["title"]
            research_title = first.json()["raw_cards"]["research_questions"][0]["title"]

            duplicate = self.client.post(
                f"/api/context/daily-runs/{run_date}/run"
            )
            self.assertFalse(duplicate.json()["started"])
            self.assertEqual(len(calls), 1)

            run_id = first.json()["id"]
            reviewed = self.client.put(
                f"/api/context/daily-runs/{run_date}/review",
                json={"market_state": "人工修订", "sources": [{"url": "https://example.com"}]},
            )
            self.assertEqual(reviewed.status_code, 200)
            self.assertEqual(reviewed.json()["synthesis"]["market_state"], "人工修订")
            self.assertEqual(len(reviewed.json()["synthesis"]["opportunity_questions"]), 1)
            self.assertEqual(len(reviewed.json()["synthesis"]["editorial_questions"]), 1)
            self.assertEqual(len(reviewed.json()["synthesis"]["research_questions"]), 1)
            approved = self.client.post(f"/api/context/daily-runs/{run_date}/approve")
            self.assertEqual(approved.json()["status"], "approved")
            after_approval = self.client.post(
                f"/api/context/daily-runs/{run_date}/run"
            )
            self.assertEqual(after_approval.json()["status"], "approved")
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                self.client.get(f"/api/context/daily/{run_date}").json()["market_state"], "人工修订"
            )
            pack = self.client.post(
                "/api/personas/1/context-packs",
                json={"topic": question_title},
            ).json()
            self.assertEqual(len(pack["content"]["attention_topics"]), 10)
            self.assertEqual(len(pack["content"]["opportunity_questions"]), 1)
            self.assertEqual(pack["content"]["topic_attention"]["status"], "hot")
            self.assertEqual(pack["content"]["topic_attention"]["selection_source"], "discussion_topics")
            self.assertEqual(pack["content"]["selected_opportunity_question"]["kind"], "short_term_trade")
            self.assertEqual(len(pack["content"]["discussion_topics"]), 1)

            selected_pack = self.client.post(
                "/api/personas/1/context-packs",
                json={"topic": question_title},
            ).json()["content"]
            self.assertEqual(selected_pack["selected_opportunity_question"]["title"], question_title)
            self.assertEqual(len(selected_pack["opportunity_questions"]), 1)
            self.assertEqual(len(selected_pack["discussion_topics"]), 1)

            editorial_pack = self.client.post(
                "/api/personas/1/context-packs",
                json={"topic": editorial_title},
            ).json()["content"]
            self.assertEqual(editorial_pack["selected_editorial_question"]["title"], editorial_title)
            self.assertEqual(editorial_pack["opportunity_questions"], [])
            self.assertEqual(len(editorial_pack["editorial_questions"]), 1)
            self.assertEqual(len(editorial_pack["discussion_topics"]), 1)
            self.assertEqual(editorial_pack["topic_attention"]["status"], "hot")

            research_pack = self.client.post(
                "/api/personas/1/context-packs",
                json={"topic": research_title},
            ).json()["content"]
            self.assertEqual(research_pack["selected_research_question"]["title"], research_title)
            self.assertEqual(len(research_pack["research_questions"]), 1)
            self.assertEqual(research_pack["opportunity_questions"], [])
            self.assertEqual(research_pack["editorial_questions"], [])
            self.assertEqual(research_pack["attention_topics"], [])
            self.assertEqual(len(research_pack["discussion_topics"]), 1)

    def test_daily_context_source_posts_returns_paginated_artifact_only_after_completion(self):
        run_date = self.app_module.shanghai_today()
        run, _ = self.app_module.create_daily_context_run(run_date, "manual")
        pending = self.client.get(f"/api/context/daily-runs/{run_date}/source-posts")
        self.assertEqual(pending.status_code, 404)
        self.app_module.update_daily_context_run(run["id"], status="failed")

        def collect(_accounts, _db, output, **_kwargs):
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True)
            (output / "latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-08-24T00:00:00+00:00",
                        "since": "2026-08-23T00:00:00+00:00",
                        "account_universe": 4684,
                        "accounts_covered": 4680,
                        "accounts_fetched": 4600,
                        "accounts_skipped": 80,
                        "accounts_failed": 4,
                        "posts": [
                            {
                                "post_id": "post_one",
                                "author_id": "anon_one",
                                "handle": "",
                                "text": "第一条原帖",
                                "created_at": "2026-08-24T00:00:00+00:00",
                                "url": "",
                                "is_reply": False,
                                "source_lists": ["crypto"],
                                "internal_secret": "must not be exposed",
                            },
                            {
                                "post_id": "post_two",
                                "author_id": "anon_two",
                                "handle": "",
                                "text": "第二条原帖",
                                "created_at": "2026-08-23T23:00:00+00:00",
                                "url": "",
                                "is_reply": True,
                                "source_lists": ["crypto", "trading"],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {"accounts_fetched": 1, "posts_seen": 2}

        def cross_validate(_db, output, **_kwargs):
            output = Path(output)
            (output / "fact_cards.json").write_text(
                json.dumps({"cards": [{"representative_text": "事实卡", "author_count": 2}]}),
                encoding="utf-8",
            )
            (output / "opinion_cards.json").write_text(json.dumps({"opinions": []}), encoding="utf-8")
            return {"source_posts": 2, "fact_cards": 1, "opinion_cards": 0}

        payload = {"choices": [{"message": {"content": json.dumps({"unknowns": "覆盖有限。"})}}]}
        factory = lambda **_kwargs: FakeAsyncClient(payload, [])
        with patch.object(
            self.app_module,
            "market_sources_module",
            return_value=SimpleNamespace(collect=collect, cross_validate=cross_validate),
        ), patch.object(self.app_module, "twitter241_api_key", return_value="runtime-key"), patch.object(
            self.app_module, "llm_api_key", return_value="test"
        ), patch.object(self.app_module.httpx, "AsyncClient", factory):
            self.client.post(f"/api/context/daily-runs/{run_date}/retry")
            self.wait_for_daily_run(run_date)

        page = self.client.get(f"/api/context/daily-runs/{run_date}/source-posts?limit=1&offset=1")
        self.assertEqual(page.status_code, 200)
        body = page.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["coverage"]["account_universe"], 4684)
        self.assertEqual(body["posts"], [{
            "post_id": "post_two",
            "author_id": "anon_two",
            "handle": "",
            "text": "第二条原帖",
            "created_at": "2026-08-23T23:00:00+00:00",
            "url": "",
            "is_reply": True,
            "source_lists": ["crypto", "trading"],
        }])
        self.assertNotIn("internal_secret", self.client.get(
            f"/api/context/daily-runs/{run_date}/source-posts"
        ).text)

    def test_daily_context_run_failure_keeps_manifest_and_can_retry(self):
        run_date = self.app_module.shanghai_today()
        def fail_collect(*_args, **_kwargs):
            raise RuntimeError("Twitter241 unavailable")

        with patch.object(
            self.app_module,
            "market_sources_module",
            return_value=SimpleNamespace(collect=fail_collect, cross_validate=lambda *_args, **_kwargs: {}),
        ), patch.object(self.app_module, "twitter241_api_key", return_value="runtime-key"):
            started = self.client.post(
                f"/api/context/daily-runs/{run_date}/run"
            )
            self.assertTrue(started.json()["started"])
            failed = self.wait_for_daily_run(run_date)
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["status"], "failed")
        self.assertIn("Twitter241 unavailable", failed.json()["error"])
        self.assertEqual(failed.json()["raw_manifest"]["failed_stage"], "setup")

        def collect(_accounts, _db, output, **_kwargs):
            Path(output).mkdir(parents=True, exist_ok=True)
            return {"accounts_fetched": 1, "posts_seen": 1}

        def cross_validate(_db, output, **_kwargs):
            output = Path(output)
            (output / "fact_cards.json").write_text(
                json.dumps({"cards": [{"representative_text": "重跑后的事实", "author_count": 2}]}),
                encoding="utf-8",
            )
            (output / "opinion_cards.json").write_text(json.dumps({"opinions": []}), encoding="utf-8")
            return {"source_posts": 1, "fact_cards": 1, "opinion_cards": 0}

        payload = {"choices": [{"message": {"content": json.dumps({"unknowns": "无卡片"})}}]}
        factory = lambda **_kwargs: FakeAsyncClient(payload, [])
        with patch.object(
            self.app_module,
            "market_sources_module",
            return_value=SimpleNamespace(collect=collect, cross_validate=cross_validate),
        ), patch.object(self.app_module, "twitter241_api_key", return_value="runtime-key"), patch.object(
            self.app_module, "llm_api_key", return_value="test"
        ), patch.object(self.app_module.httpx, "AsyncClient", factory):
            self.client.post(f"/api/context/daily-runs/{run_date}/retry")
            retried = self.wait_for_daily_run(run_date)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["status"], "needs_review")

    def test_daily_context_run_rejects_empty_cards_without_calling_llm(self):
        run_date = self.app_module.shanghai_today()
        def collect(_accounts, _db, output, **_kwargs):
            Path(output).mkdir(parents=True, exist_ok=True)
            return {"accounts_fetched": 1, "posts_seen": 1}

        def cross_validate(_db, output, **_kwargs):
            output = Path(output)
            (output / "fact_cards.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
            (output / "opinion_cards.json").write_text(json.dumps({"opinions": []}), encoding="utf-8")
            return {"source_posts": 1, "fact_cards": 0, "opinion_cards": 0}

        with patch.object(
            self.app_module,
            "market_sources_module",
            return_value=SimpleNamespace(collect=collect, cross_validate=cross_validate),
        ), patch.object(self.app_module, "twitter241_api_key", return_value="runtime-key"), patch.object(
            self.app_module, "synthesize_daily_cards", side_effect=AssertionError("LLM should not run")
        ):
            self.client.post(f"/api/context/daily-runs/{run_date}/run")
            response = self.wait_for_daily_run(run_date)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertIn("未产出可用事实或观点卡", response.json()["error"])

    def test_daily_context_scheduler_uses_shanghai_run_time(self):
        class BeforeSchedule:
            @classmethod
            def now(cls, _timezone):
                return datetime(2026, 8, 24, 8, 14, tzinfo=ZoneInfo("Asia/Shanghai"))

        class AfterSchedule:
            @classmethod
            def now(cls, _timezone):
                return datetime(2026, 8, 24, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

        with patch.object(self.app_module, "daily_context_scheduler_enabled", return_value=True), patch.object(
            self.app_module, "daily_context_schedule", return_value=(8, 15)
        ), patch.object(self.app_module, "queue_daily_context") as queue, patch.object(
            self.app_module, "datetime", BeforeSchedule
        ):
            asyncio.run(self.app_module.run_due_daily_context())
            queue.assert_not_called()

        with patch.object(self.app_module, "daily_context_scheduler_enabled", return_value=True), patch.object(
            self.app_module, "daily_context_schedule", return_value=(8, 15)
        ), patch.object(self.app_module, "queue_daily_context") as queue, patch.object(
            self.app_module, "datetime", AfterSchedule
        ):
            asyncio.run(self.app_module.run_due_daily_context())
            queue.assert_called_once_with("2026-08-24", "schedule")

        response = self.client.post("/api/context/daily-runs/1999-01-01/run")
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/context/daily-runs/1999-01-01/retry")
        self.assertEqual(response.status_code, 422)

    def test_daily_context_run_rejects_stale_cards_when_every_account_fetch_fails(self):
        run_date = self.app_module.shanghai_today()
        def collect(_accounts, _db, output, **_kwargs):
            Path(output).mkdir(parents=True, exist_ok=True)
            return {
                "account_universe": 2,
                "accounts_fetched": 0,
                "accounts_skipped": 0,
                "accounts_failed": 2,
                "posts_seen": 0,
            }

        def cross_validate(_db, output, **_kwargs):
            output = Path(output)
            (output / "fact_cards.json").write_text(
                json.dumps({"cards": [{"representative_text": "旧卡片"}]}), encoding="utf-8"
            )
            (output / "opinion_cards.json").write_text(json.dumps({"opinions": []}), encoding="utf-8")
            return {"source_posts": 1, "fact_cards": 1, "opinion_cards": 0}

        with patch.object(
            self.app_module,
            "market_sources_module",
            return_value=SimpleNamespace(collect=collect, cross_validate=cross_validate),
        ), patch.object(self.app_module, "twitter241_api_key", return_value="runtime-key"), patch.object(
            self.app_module, "synthesize_daily_cards", side_effect=AssertionError("LLM should not run")
        ):
            self.client.post(f"/api/context/daily-runs/{run_date}/run")
            response = self.wait_for_daily_run(run_date)
        self.assertEqual(response.json()["status"], "failed")
        self.assertIn("账号抓取全部失败", response.json()["error"])

    def test_three_curated_asset_collections_are_ready(self):
        personas = {persona["slug"]: persona for persona in self.client.get("/api/personas").json()}
        expected = {
            "acheng": 40,
            "ridehail-driver-zhao": 40,
            "college-student-linjia": 10,
        }
        for slug, count in expected.items():
            persona = self.client.get(f"/api/personas/{personas[slug]['id']}").json()
            self.assertEqual(len(persona["assets"]), count)
            self.assertTrue(persona["asset_collection"]["ready"])

        acheng_avatar = self.client.get(f"/api/personas/{personas['acheng']['id']}").json()["avatar_url"]
        driver_avatar = self.client.get(f"/api/personas/{personas['ridehail-driver-zhao']['id']}").json()["avatar_url"]
        self.assertIn("avatar-x-v4-natural-meituan.png", acheng_avatar)
        self.assertIn("avatar-x-v3-natural.png", driver_avatar)

        acheng = self.client.get(f"/api/personas/{personas['acheng']['id']}").json()
        response = self.client.post(
            f"/api/personas/{personas['acheng']['id']}/prompt-preview",
            json={"data": acheng["draft"]},
        )
        self.assertIn("## 已连接素材", response.json()["prompt"])
        self.assertIn("acheng:01-income-closeup.jpeg", response.json()["prompt"])

        student = self.client.get(f"/api/personas/{personas['college-student-linjia']['id']}").json()
        self.assertIn("real-reference-core-10", student["avatar_url"])
        self.assertNotIn("状态：已排除", student["draft"]["identity"]["profile"])
        self.assertEqual(student["draft"]["visual"]["master_prompt"], "")

    def test_editorial_context_draft_is_not_in_engine_input_until_approved(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        marker = "DRAFT_PRIVATE_LIFE_MUST_NOT_REACH_ENGINE"
        payload = self.editorial_context_payload(life_context=[{
            "id": "life-draft", "angle": marker, "core_claim": "私有生活判断",
            "first_person_allowed": True,
        }])

        saved = self.put_editorial_context(acheng["id"], payload)
        self.assertEqual(saved["approval_revision"], 0)
        self.assertEqual(saved["draft"], payload)
        self.assertIn(saved.get("approved"), (None, {}))

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic, "WRITE", claim_key=f"draft-{index}", core_claim=f"草稿期公共判断 {index}"
                )[str(topic["claim_key"])]
                for index, topic in enumerate(topics)
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "草稿期正文"})
        ):
            self.run_editorial_pipeline(run_id)

        with self.app_module.db() as conn:
            before = [row[0] for row in conn.execute(
                "SELECT input_json FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchall()]
        self.assertTrue(before)
        self.assertNotIn(marker, "\n".join(before))

        approved = self.approve_editorial_context(acheng["id"])
        self.assertEqual(approved["approval_revision"], 1)
        self.assertEqual(approved["approved"], payload)
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "批准后正文"})
        ):
            self.run_editorial_pipeline(run_id)

        with self.app_module.db() as conn:
            after = [row[0] for row in conn.execute(
                "SELECT input_json FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchall()]
        self.assertGreater(len(after), len(before))
        self.assertIn(marker, "\n".join(after))

    def test_editorial_context_reapproval_revisions_fingerprint_and_supersedes_current_draft(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        first = self.editorial_context_payload(life_context=[{
            "id": "life-v1", "angle": "LIFE_CONTEXT_REVISION_ONE", "core_claim": "第一版私人判断",
            "first_person_allowed": False,
        }])
        second = self.editorial_context_payload(life_context=[{
            "id": "life-v2", "angle": "LIFE_CONTEXT_REVISION_TWO", "core_claim": "第二版私人判断",
            "first_person_allowed": False,
        }])
        self.put_editorial_context(acheng["id"], first)
        self.assertEqual(self.approve_editorial_context(acheng["id"])["approval_revision"], 1)
        calls = []

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            calls.append(True)
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic, "WRITE" if topic.get("source_kind") == "life" else "IGNORE",
                    claim_key=f"life-revision-{len(calls)}-{index}",
                    core_claim=f"审批版本 {len(calls)} 的独立判断 {index}",
                )[str(topic["claim_key"])]
                for index, topic in enumerate(topics)
            }

        generated = AsyncMock(side_effect=[{"post": "第一版草稿"}, {"post": "第二版草稿"}])
        env = {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}
        with patch.dict(os.environ, env), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
            self.put_editorial_context(acheng["id"], second)
            # Saving a draft must not replace the approved snapshot or trigger a new input.
            self.run_editorial_pipeline(run_id)
            self.assertEqual(len(calls), 1)
            approved = self.approve_editorial_context(acheng["id"])
            self.assertEqual(approved["approval_revision"], 2)
            self.assertEqual(self.client.get("/api/daily-posts").json(), [])
            self.run_editorial_pipeline(run_id)

        self.assertEqual(len(calls), 2)
        with self.app_module.db() as conn:
            rows = conn.execute(
                "SELECT status,input_json FROM persona_editorial_evaluations WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            candidates = [row[0] for row in conn.execute(
                "SELECT status FROM post_candidates ORDER BY id"
            ).fetchall()]
        self.assertIn("LIFE_CONTEXT_REVISION_ONE", "\n".join(row[1] for row in rows))
        self.assertIn("LIFE_CONTEXT_REVISION_TWO", "\n".join(row[1] for row in rows))
        self.assertIn("superseded", candidates)
        self.assertIn("needs_review", candidates)

    def test_editorial_context_only_mature_private_topics_reach_their_persona(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        personas = {item["slug"]: item for item in self.client.get("/api/personas").json()}
        acheng = personas["acheng"]
        driver = personas["ridehail-driver-zhao"]
        payload = self.editorial_context_payload(
            thought_threads=[
                {"id": "thought-ready", "status": "ready", "angle": "THREAD_READY", "core_claim": "成熟想法"},
                {"id": "thought-draft", "status": "draft", "angle": "THREAD_DRAFT", "core_claim": "未成熟想法"},
                {"id": "thought-expressed", "status": "expressed", "angle": "THREAD_EXPRESSED", "core_claim": "已表达想法"},
            ],
            expression_debt=[
                {"id": "debt-ready", "status": "ready", "angle": "DEBT_READY", "core_claim": "应表达判断"},
                {"id": "debt-done", "status": "expressed", "angle": "DEBT_DONE", "core_claim": "已结清判断"},
            ],
            life_context=[
                {"id": "life-angle", "angle": "LIFE_ANGLE", "core_claim": "生活角度", "first_person_allowed": False},
                {"id": "life-no-angle", "note": "LIFE_NO_ANGLE", "first_person_allowed": False},
            ],
            real_feedback=[
                {"id": "feedback-angle", "angle": "FEEDBACK_ANGLE", "core_claim": "真实反馈角度"},
                {"id": "feedback-no-angle", "text": "FEEDBACK_NO_ANGLE"},
            ],
        )
        self.put_editorial_context(acheng["id"], payload)
        self.approve_editorial_context(acheng["id"])
        self.put_editorial_context(driver["id"], self.editorial_context_payload(thought_threads=[{
            "id": "driver-only", "status": "ready", "angle": "OTHER_PERSONA_PRIVATE_TOPIC", "core_claim": "只属于司机",
        }]))
        self.approve_editorial_context(driver["id"])

        captured_topics = []

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            captured_topics.extend(topics)
            return {
                str(topic["claim_key"]): self.editorial_decision(topic, "IGNORE")[str(topic["claim_key"])]
                for topic in topics
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ):
            self.run_editorial_pipeline(run_id)

        serialized = json.dumps(captured_topics, ensure_ascii=False)
        for marker in ("THREAD_READY", "DEBT_READY", "LIFE_ANGLE", "FEEDBACK_ANGLE"):
            self.assertIn(marker, serialized)
        for marker in (
            "THREAD_DRAFT", "THREAD_EXPRESSED", "DEBT_DONE", "LIFE_NO_ANGLE",
            "FEEDBACK_NO_ANGLE", "OTHER_PERSONA_PRIVATE_TOPIC",
        ):
            self.assertNotIn(marker, serialized)

    def test_editorial_context_first_person_evidence_only_comes_from_allowed_life_item(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        selected_asset = self.client.get(f"/api/personas/{acheng['id']}").json()["assets"][0]["id"]
        payload = self.editorial_context_payload(
            life_context=[
                {
                    "id": "life-allowed", "angle": "LIFE_ALLOWED_ANGLE", "core_claim": "允许的一手经历",
                    "first_person_allowed": True, "text": "LIFE_ALLOWED_FIRST_PERSON_EVIDENCE",
                },
                {
                    "id": "life-blocked", "angle": "LIFE_BLOCKED_ANGLE", "core_claim": "不允许的一手经历",
                    "first_person_allowed": False, "text": "LIFE_BLOCKED_FIRST_PERSON_EVIDENCE",
                },
            ],
            thought_threads=[{
                "id": "thought-not-life", "status": "ready", "angle": "THOUGHT_CANNOT_SUPPORT_FIRST_PERSON", "core_claim": "观点不是经历",
                "first_person_allowed": True,
            }],
            real_feedback=[{
                "id": "feedback-not-life", "angle": "FEEDBACK_CANNOT_SUPPORT_FIRST_PERSON", "core_claim": "反馈不是经历",
                "first_person_allowed": True,
            }],
            available_asset_ids=[selected_asset],
        )
        self.put_editorial_context(acheng["id"], payload)
        self.approve_editorial_context(acheng["id"])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            chosen = next(topic for topic in topics if "LIFE_ALLOWED_ANGLE" in json.dumps(topic, ensure_ascii=False))
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic,
                    "WRITE" if topic is chosen else "IGNORE",
                    claim_key="allowed-life-claim",
                    core_claim="只允许由已批准生活记录支持的第一人称判断",
                )[str(topic["claim_key"])]
                for topic in topics
            }

        observed = []

        async def generated(_persona_id, request):
            observed.append(request.facts)
            return {"post": "我曾经有过这段经过。"}

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", AsyncMock(side_effect=generated)):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(len(observed), 1)
        facts = observed[0]
        self.assertIn("LIFE_ALLOWED_FIRST_PERSON_EVIDENCE", facts)
        self.assertNotIn("LIFE_BLOCKED_FIRST_PERSON_EVIDENCE", facts)
        self.assertNotIn("THOUGHT_CANNOT_SUPPORT_FIRST_PERSON", facts)
        self.assertNotIn("FEEDBACK_CANNOT_SUPPORT_FIRST_PERSON", facts)
        self.assertIn("first_person_allowed", facts)

    def test_editorial_context_assets_are_persona_scoped_and_daily_api_uses_approved_selection(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        personas = {item["slug"]: item for item in self.client.get("/api/personas").json()}
        acheng = self.client.get(f"/api/personas/{personas['acheng']['id']}").json()
        driver = self.client.get(f"/api/personas/{personas['ridehail-driver-zhao']['id']}").json()
        selected_asset = acheng["assets"][1]["id"]
        foreign_asset = driver["assets"][0]["id"]
        invalid = self.client.put(
            f"/api/personas/{acheng['id']}/editorial-context",
            json=self.editorial_context_payload(available_asset_ids=[foreign_asset]),
        )
        self.assertEqual(invalid.status_code, 422)
        self.put_editorial_context(acheng["id"], self.editorial_context_payload(available_asset_ids=[selected_asset]))
        self.approve_editorial_context(acheng["id"])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic, "WRITE", claim_key=f"asset-{index}", core_claim=f"素材选择判断 {index}"
                )[str(topic["claim_key"])]
                for index, topic in enumerate(topics)
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "有选定素材的草稿"})
        ):
            self.run_editorial_pipeline(run_id)

        selected_url = next(asset["url"] for asset in acheng["assets"] if asset["id"] == selected_asset)
        items = self.client.get("/api/daily-posts").json()
        self.assertTrue(items)
        self.assertTrue(all(item["image_url"] == selected_url for item in items))
        self.assertTrue(all(foreign_asset not in json.dumps(item, ensure_ascii=False) for item in items))

    def test_historical_editorial_context_pending_write_recovers_once_and_marks_thread_expressed(self):
        context_date = "2020-01-07"
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        payload = self.editorial_context_payload(thought_threads=[{
            "id": "historical-thread", "status": "ready", "angle": "HISTORICAL_READY_THREAD", "core_claim": "历史已成熟判断",
        }])
        self.put_editorial_context(acheng["id"], payload)
        self.approve_editorial_context(acheng["id"])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            chosen = next(topic for topic in topics if "HISTORICAL_READY_THREAD" in json.dumps(topic, ensure_ascii=False))
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic,
                    "WRITE" if topic is chosen else "IGNORE",
                    claim_key="historical-thread-write",
                    core_claim="这条历史成熟判断只应表达一次。",
                )[str(topic["claim_key"])]
                for topic in topics
            }

        generated = AsyncMock(side_effect=[
            self.app_module.HTTPException(502, "first attempt failed"),
            {"post": "恢复后的唯一草稿"},
        ])
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
            asyncio.run(self.app_module.run_persona_editorial_pipeline())
            asyncio.run(self.app_module.run_persona_editorial_pipeline())

        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)
        context = self.client.get(f"/api/personas/{acheng['id']}/editorial-context").json()
        thread = next(item for item in context["approved"]["thought_threads"] if item["id"] == "historical-thread")
        self.assertEqual(thread["status"], "ready")
        self.assertIn("thought:historical-thread", context["expressed_source_ids"])

    def test_private_editorial_topic_does_not_require_a_public_market_topic(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps({"selected_topics": []}), run_id),
            )
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        self.put_editorial_context(acheng["id"], self.editorial_context_payload(expression_debt=[{
            "id": "private-only", "status": "ready", "core_claim": "没有公共热点也值得表达的独立判断",
        }]))
        self.approve_editorial_context(acheng["id"])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            self.assertEqual([topic["source_kind"] for topic in topics], ["expression_debt"])
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="private-only-claim",
                core_claim="私人题和公共题使用同一套编辑判断。",
            )

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "这是一条私人题候选。"})
        ):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 1)

    def test_unapproved_first_person_experience_is_rejected_before_candidate_insert(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        self.put_editorial_context(acheng["id"], self.editorial_context_payload(life_context=[{
            "id": "life-no-experience", "angle": "只能表达判断", "core_claim": "这只是一个判断",
            "first_person_allowed": False,
        }]))
        self.approve_editorial_context(acheng["id"])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            decisions = {}
            for topic in topics:
                if topic.get("source_kind") == "life":
                    decisions.update(self.editorial_decision(
                        topic, "WRITE", claim_key="unsupported-experience",
                        core_claim="这个判断不能伪装成亲历。",
                    ))
                else:
                    decisions.update(self.editorial_decision(topic, "IGNORE"))
            return decisions

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "我买了以后才发现这个问题。"})
        ):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            evaluation = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE claim_key='unsupported-experience'"
            ).fetchone()
            self.assertEqual(tuple(evaluation), ("HOLD", "unsupported_first_person_experience"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 0)

    def test_expressed_private_thread_is_not_reintroduced_on_a_later_run(self):
        first_date = "2026-08-20"
        second_date = "2026-08-21"
        first_run = self.create_editorial_run(first_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        marker = "EXPRESSED_THREAD_MUST_NOT_RETURN"
        self.put_editorial_context(acheng["id"], self.editorial_context_payload(thought_threads=[{
            "id": "once-only-thread", "status": "ready", "angle": marker,
            "core_claim": "这条成熟想法只表达一次",
        }]))
        self.approve_editorial_context(acheng["id"])

        async def first_evaluator(_persona, _context, _daily, topics, _history, _today_count):
            decisions = {}
            for topic in topics:
                if topic.get("source_kind") == "thought":
                    decisions.update(self.editorial_decision(
                        topic, "WRITE", claim_key="once-only-claim",
                        core_claim="这条成熟想法已经形成草稿。",
                    ))
                else:
                    decisions.update(self.editorial_decision(topic, "IGNORE"))
            return decisions

        env = {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}
        with patch.dict(os.environ, env), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=first_evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "第一次唯一草稿"})
        ):
            self.run_editorial_pipeline(first_run)

        second_run = self.create_editorial_run(second_date)
        seen = []

        async def second_evaluator(_persona, _context, _daily, topics, _history, _today_count):
            seen.extend(topics)
            return {
                str(topic["claim_key"]): self.editorial_decision(topic, "IGNORE")[str(topic["claim_key"])]
                for topic in topics
            }

        with patch.dict(os.environ, env), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=second_evaluator)
        ):
            self.run_editorial_pipeline(second_run)
        self.assertNotIn(marker, json.dumps(seen, ensure_ascii=False))

    def test_editorial_context_reapproval_during_evaluation_discards_stale_decision(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        first = self.editorial_context_payload(expression_debt=[{
            "id": "race-v1", "status": "ready", "core_claim": "RACE_CONTEXT_V1",
        }])
        second = self.editorial_context_payload(expression_debt=[{
            "id": "race-v2", "status": "ready", "core_claim": "RACE_CONTEXT_V2",
        }])
        self.put_editorial_context(acheng["id"], first)
        self.approve_editorial_context(acheng["id"])
        calls = 0

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            nonlocal calls
            calls += 1
            chosen = next(topic for topic in topics if topic.get("source_kind") == "expression_debt")
            if calls == 1:
                self.app_module.put_persona_editorial_context(
                    acheng["id"], self.app_module.PersonaEditorialContextIn(**second)
                )
                self.app_module.approve_persona_editorial_context(acheng["id"])
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic, "WRITE" if topic is chosen else "IGNORE",
                    claim_key=f"race-claim-{calls}", core_claim=f"竞态判断 {calls}",
                )[str(topic["claim_key"])]
                for topic in topics
            }

        generated = AsyncMock(return_value={"post": "只允许新版本正文"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
            with self.app_module.db() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persona_editorial_evaluations").fetchone()[0], 0)
            self.run_editorial_pipeline(run_id)

        self.assertEqual(calls, 2)
        self.assertEqual(generated.await_count, 1)
        with self.app_module.db() as conn:
            rows = conn.execute("SELECT input_json FROM persona_editorial_evaluations").fetchall()
        self.assertNotIn("RACE_CONTEXT_V1", "\n".join(row[0] for row in rows))
        self.assertIn("RACE_CONTEXT_V2", "\n".join(row[0] for row in rows))

    def test_private_claim_memory_never_reaches_another_persona(self):
        first_run = self.create_editorial_run("2026-08-18")
        personas = {item["slug"]: item for item in self.client.get("/api/personas").json()}
        marker = "ACHENG_PRIVATE_MEMORY_MUST_NOT_LEAK"
        self.put_editorial_context(personas["acheng"]["id"], self.editorial_context_payload(
            expression_debt=[{"id": "acheng-private", "core_claim": marker, "status": "ready"}]
        ))
        self.approve_editorial_context(personas["acheng"]["id"])

        async def first_evaluator(_persona, _context, _daily, topics, _history, _today_count):
            decisions = {}
            for topic in topics:
                if topic.get("source_kind") == "expression_debt":
                    decisions.update(self.editorial_decision(
                        topic, "WRITE", claim_key="acheng-private-claim", core_claim=marker,
                    ))
                else:
                    decisions.update(self.editorial_decision(topic, "IGNORE"))
            return decisions

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=first_evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "阿成的私人候选"})
        ):
            self.run_editorial_pipeline(first_run)

        second_run = self.create_editorial_run("2026-08-19")
        seen_history = []

        async def second_evaluator(_persona, _context, _daily, topics, history, _today_count):
            seen_history.extend(history)
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic, "WRITE", claim_key=f"driver-independent-{index}", core_claim=marker,
                )[str(topic["claim_key"])]
                for index, topic in enumerate(topics)
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "ridehail-driver-zhao",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=second_evaluator)
        ):
            self.run_editorial_pipeline(second_run)
        self.assertNotIn(marker, json.dumps(seen_history, ensure_ascii=False))
        with self.app_module.db() as conn:
            inputs = conn.execute(
                "SELECT input_json,status,reason_code FROM persona_editorial_evaluations WHERE run_id=?",
                (second_run,),
            ).fetchall()
        self.assertNotIn(marker, "\n".join(row[0] for row in inputs))
        self.assertTrue(all(tuple(row[1:]) == ("IGNORE", "historical_duplicate") for row in inputs))

    def test_large_approved_private_context_is_bounded_and_does_not_stop_later_personas(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        personas = {item["slug"]: item for item in self.client.get("/api/personas").json()}
        self.put_editorial_context(personas["acheng"]["id"], self.editorial_context_payload(
            life_context=[{
                "id": "large-life", "status": "ready", "angle": "长证据生活题",
                "core_claim": "长证据仍然必须形成有界写作输入", "first_person_allowed": True,
                "evidence": [f"证据 {index} " + "长" * 950 for index in range(20)],
            }]
        ))
        self.approve_editorial_context(personas["acheng"]["id"])

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            decisions = {}
            for topic in topics:
                write = topic.get("source_kind") == "life" or (
                    persona["slug"] == "ridehail-driver-zhao" and not topic.get("source_kind")
                )
                decisions.update(self.editorial_decision(
                    topic, "WRITE" if write else "IGNORE",
                    claim_key=f"bounded-{persona['slug']}-{len(decisions)}",
                    core_claim=f"{persona['slug']} 的独立有界判断 {len(decisions)}",
                ))
            return decisions

        facts = []

        async def generated(_persona_id, request):
            facts.append(request.facts)
            return {"post": "有界输入生成的正文"}

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", AsyncMock(side_effect=generated)):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(len(facts), 2)
        self.assertTrue(all(len(item) <= 8000 for item in facts))
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 2)

    def test_first_person_guard_catches_time_trade_and_account_holdings(self):
        blocked = {"first_person_allowed": False}
        for post in (
            "我上周抄底 BTC，最后多赚两万。",
            "我账户里现在还有 3 个 BTC。",
            "我手里现在还有 3 个 BTC。",
            "本人账户里现在还有 3 个 BTC。",
            "我这个月做空 ETH 挣了两万。",
            "我用这个协议三个月了。",
        ):
            with self.subTest(post=post):
                self.assertTrue(self.app_module.unauthorized_first_person_experience(post, blocked))
        self.assertFalse(self.app_module.unauthorized_first_person_experience(
            "我认为这次买入条件并不完整。", blocked
        ))
        self.assertFalse(self.app_module.unauthorized_first_person_experience(
            "我上周抄底 BTC，最后多赚两万。", {"first_person_allowed": True}
        ))

    def test_get_and_idempotent_reapprove_do_not_supersede_expressed_candidate(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        payload = self.editorial_context_payload(thought_threads=[{
            "id": "idempotent-thread", "status": "ready", "angle": "幂等重批测试",
            "core_claim": "读取派生消费态不能改写正式 Context",
        }])
        self.put_editorial_context(acheng["id"], payload)
        self.approve_editorial_context(acheng["id"])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            decisions = {}
            for topic in topics:
                if topic.get("source_kind") == "thought":
                    decisions.update(self.editorial_decision(
                        topic, "WRITE", claim_key="idempotent-thread-claim",
                        core_claim="派生状态不能污染审批内容。",
                    ))
                else:
                    decisions.update(self.editorial_decision(topic, "IGNORE"))
            return decisions

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(
            self.app_module, "generate_persona_post", AsyncMock(return_value={"post": "保留的候选"})
        ):
            self.run_editorial_pipeline(run_id)

        current = self.client.get(f"/api/personas/{acheng['id']}/editorial-context").json()
        self.assertEqual(current["draft"], payload)
        self.assertEqual(current["approved"], payload)
        self.assertIn("thought:idempotent-thread", current["expressed_source_ids"])
        self.put_editorial_context(acheng["id"], current["draft"])
        approved = self.approve_editorial_context(acheng["id"])
        self.assertEqual(approved["approval_revision"], 1)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM post_candidates").fetchone()[0], "needs_review")

if __name__ == "__main__":
    unittest.main()
