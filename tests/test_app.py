import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

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


class FakeGitHubResponse:
    text = '<a aria-label="4687 users starred this repository">4.7k</a> 4687 users starred this repository'

    def raise_for_status(self):
        return None


class FakeGitHubClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return FakeGitHubResponse()


class FakeHealthClient:
    def __init__(self, statuses, calls):
        self.statuses = list(statuses)
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return FakeResponse({}, self.statuses.pop(0))


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._operator_token = os.environ.pop("XOPS_OPERATOR_TOKEN", None)
        os.environ.update(
            XOPS_DATA_DIR=self.temp.name,
            XOPS_DAILY_CONTEXT_ENABLED="false",
            XOPS_AI_SOURCE_ENABLED="false",
            XOPS_DAILY_POST_TARGET_PER_PERSONA="0",
            XOPS_BASE_URL="http://127.0.0.1:8788",
        )
        import app
        self.app_module = importlib.reload(app)
        self._real_enrich_persona_editorial_context = self.app_module.enrich_persona_editorial_context
        self._real_write_persona_editorial_gemini = self.app_module.write_persona_editorial_gemini
        self._real_critique_persona_editorial_draft = self.app_module.critique_persona_editorial_draft
        self._real_ensure_editorial_angle_expansion = self.app_module.ensure_editorial_angle_expansion
        self.client = TestClient(self.app_module.app)
        self.client.__enter__()

        # Existing editorial behavior tests predate the formal Grok -> Gemini
        # route.  Keep their mocked ``generate_persona_post`` seam meaningful
        # while the route itself is covered by focused tests below.
        async def legacy_grok(_topic, _facts, _daily):
            return {
                "text": "测试用市场背景。", "citations": ["https://example.com/context"],
                "tool_usage": ["x_search", "web_search"], "model": "grok-test",
            }

        async def legacy_gemini(_persona, topic, facts, _grok, _writer_context, _rewrite="", **_kwargs):
            generated = await self.app_module.generate_persona_post(
                0,
                self.app_module.PostGenerationIn(
                    facts=json.dumps({"topic": topic, "verified_facts": facts}, ensure_ascii=False)
                ),
            )
            text = generated["post"]
            if len(text) < 80 and not self.app_module.unauthorized_first_person_experience(text, _writer_context):
                text += "补充文字只为覆盖原有状态流转测试所需长度，不增加任何外部事实，也不影响该测试关注的生成、冲突和候选入库边界。此处保持语义中性，确保旧断言仍只验证对应流程。"
            return {"text": text, "facts_used_ids": [], "stance": "测试判断", "model": "gemini-test"}

        async def legacy_critic(*_args, **_kwargs):
            return {"verdict": "PASS", "reasons": [], "rewrite_instruction": ""}

        async def legacy_angle_expansion(_run_id, cards, _daily):
            return [
                item for item in cards.get("selected_topics", [])
                if isinstance(item, dict) and item.get("claim_key")
            ]

        self._default_post_generator = patch.object(
            self.app_module,
            "generate_persona_post",
            AsyncMock(return_value={"post": "这是一条用于旧编辑测试的完整候选正文，保留原有断言所需的生成边界，并不代表正式输出。它补足了足够具体的语境和判断，让旧测试只验证状态流转，不会触发正式管线的长度或模板拒绝规则。"}),
        )
        self._legacy_grok = patch.object(self.app_module, "enrich_persona_editorial_context", AsyncMock(side_effect=legacy_grok))
        self._legacy_gemini = patch.object(self.app_module, "write_persona_editorial_gemini", AsyncMock(side_effect=legacy_gemini))
        self._legacy_critic = patch.object(self.app_module, "critique_persona_editorial_draft", AsyncMock(side_effect=legacy_critic))
        self._legacy_healthcheck = patch.object(
            self.app_module, "ensure_editorial_providers_ready", AsyncMock(return_value=None)
        )
        self._legacy_angle_expansion = patch.object(
            self.app_module, "ensure_editorial_angle_expansion",
            AsyncMock(side_effect=legacy_angle_expansion),
        )
        self._default_post_generator.start()
        self._legacy_grok.start()
        self._legacy_gemini.start()
        self._legacy_critic.start()
        self._legacy_healthcheck.start()
        self._legacy_angle_expansion.start()

    def tearDown(self):
        self._legacy_angle_expansion.stop()
        self._legacy_healthcheck.stop()
        self._legacy_critic.stop()
        self._legacy_gemini.stop()
        self._legacy_grok.stop()
        self._default_post_generator.stop()
        self.client.__exit__(None, None, None)
        self.temp.cleanup()
        if self._operator_token is not None:
            os.environ["XOPS_OPERATOR_TOKEN"] = self._operator_token

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
                    json.dumps(self.signal_cards(topics), ensure_ascii=False),
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
        thesis = {}
        if status == "WRITE":
            thesis = {
                "thesis_type": "ASSERTION",
                "claim_nature": "opinion",
                "primary_subject": {
                    "type": "topic",
                    "id": str(topic.get("subject") or topic.get("title") or topic["claim_key"]),
                },
                "relation": "judges",
                "primary_claim": core_claim,
                "primary_claim_count": 1,
                "scope": {"statement": str(topic.get("specific_tension") or topic.get("title") or topic["claim_key"])},
                "persona_lens_id": "__AUTO__",
                "supporting_basis": [],
                "reader_payoff": {"type": "judgment", "statement": core_claim},
                "falsifier": "",
                "source_delta": why_me,
                "novelty": {"recent_persona_collision": False, "cross_persona_collision": False},
                "provenance_source": "approved_input",
            }
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
                "reader_conclusion": core_claim,
                "thesis": thesis,
                "reason_code": status.lower(),
                "rationale": "测试决策",
                "open_loop": "",
            }
        }

    def thesis_contract(self, topic, *, slug="acheng", claim="这次变化先改变普通用户的执行成本。",
                        relation="changes", lens=None, claim_count=1, fact_ids=None):
        lens = lens or self.app_module.persona_thesis_profile(slug)["allowed_lenses"][0]
        contract = {
            "contract_version": self.app_module.THESIS_CONTRACT_VERSION,
            "topic_id": topic["claim_key"],
            "persona_id": slug,
            "thesis_type": "ASSERTION",
            "claim_nature": "opinion",
            "primary_subject": {"type": "topic", "id": str(topic.get("subject") or topic.get("title"))},
            "relation": relation,
            "primary_claim": claim,
            "primary_claim_count": claim_count,
            "scope": {"statement": str(topic.get("specific_tension") or topic.get("title"))},
            "persona_lens_id": lens,
            "supporting_basis": ([{
                "role": "factual_premise", "claim": "已批准事实前提", "fact_ids": fact_ids,
            }] if fact_ids is not None else []),
            "reader_payoff": {"type": "judgment", "statement": claim},
            "falsifier": "",
            "source_delta": "把公共 Topic 转成该人设独有的执行判断。",
            "novelty": {"recent_persona_collision": False, "cross_persona_collision": False},
            "provenance_source": "approved_input",
        }
        contract["thesis_id"] = self.app_module.thesis_contract_id(contract)
        return contract

    @staticmethod
    def mother_topic(claim_key="btc-mother", source_key="discussion:btc"):
        return {
            "claim_key": claim_key,
            "subject": "Bitcoin",
            "title": "BTC 资金结构",
            "core_claim": "BTC 的资金结构值得继续展开。",
            "content_type": "research",
            "material_delta": "市场分歧从涨跌转向资金来源。",
            "audience_value": "帮助读者判断这轮行情由谁推动。",
            "why_now": "母池今天集中讨论资金结构。",
            "source_topic_keys": [source_key],
            "source_refs": ["x:btc:1"],
            "source_topic_title": "BTC 资金结构",
            "eligible": True,
        }

    @staticmethod
    def signal_cards(topics):
        signals = []
        for topic in topics:
            source_keys = topic.get("source_topic_keys", [])
            source_key = str(
                source_keys[0] if source_keys
                else topic.get("parent_seed_key") or topic.get("claim_key")
            )
            signals.append({
                "key": source_key.removeprefix("ai:"),
                "title": topic.get("source_topic_title") or topic.get("title") or source_key,
                "topic_domain": topic.get("topic_domain", "crypto"),
                "parent": {"title": topic.get("subject") or topic.get("title") or source_key},
                "mechanism": {"title": "测试机制"},
                "unique_authors": 3,
                "post_count": 3,
                "sample_refs": topic.get("source_refs", []),
                "sample_posts": [
                    {"source_ref": ref, "text": topic.get("title", source_key)}
                    for ref in topic.get("source_refs", [])
                ],
            })
        return {
            "selected_topics": topics,
            "discussion_topics": signals,
            "discovery_topics": [],
        }

    @staticmethod
    def expanded_angle(seed_key, claim_key, family="industry_evaluation", core_claim=None):
        return {
            "parent_seed_key": seed_key,
            "claim_key": claim_key,
            "subject": "Bitcoin",
            "title": f"{claim_key} 的具体判断",
            "core_claim": core_claim or f"{claim_key} 说明资金来源正在改变山寨币的筛选方式。",
            "angle_family": family,
            "specific_tension": "指数上涨与山寨币赚钱效应没有同步。",
            "non_obvious_delta": "判断重点从 BTC 涨幅转向资金是否外溢。",
            "audience_value": "改变读者选择交易对象的方式。",
            "why_worth_saying": "同一轮上涨里不同资产的受益顺序并不相同。",
            "why_now": "母池今天集中讨论资金结构。",
            "statement_mode": "opinion",
            "persona_fit": ["axu"],
            "action_setup": "已有可核验的参与条件。",
            "action_trigger": "条件满足后才执行。",
            "action_invalidation": "条件失效则停止。",
            "action_consequence": "执行后承担对应成本和结果。",
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
            editorial_context = self.app_module.approved_persona_editorial_context(
                conn, persona["id"], persona["slug"], run_id
            )
            topics = self.app_module.persona_editorial_topics(
                persona,
                self.app_module.editorial_public_topics(
                    self.app_module.json_value(run["raw_cards"], {})
                ),
                editorial_context,
            )
            structure = self.app_module.editorial_content_structure(topic)
            topic = {**topic, "structure_id": structure["id"], "style_recipe": structure}
            daily["approval_revision"] = run["approval_revision"]
            stable_history = self.app_module.editorial_stable_claim_history(conn, context_date)
            input_payload = self.app_module.editorial_topic_input_payload(
                topic, daily, persona, {}, topics=topics, claim_history=stable_history,
                editorial_context=editorial_context,
            )
            input_hash = self.app_module.editorial_topic_input_hash(
                topic, daily, persona, {}, topics=topics, claim_history=stable_history,
                editorial_context=editorial_context,
            )
            now = int(time.time())
            thesis = self.app_module.legacy_persona_thesis_contract(topic, {
                "core_claim": core_claim,
                "claim_key": claim_key,
                "reader_conclusion": core_claim,
                "why_me": "这是该人设会说的",
                "persona_slug": slug,
            })
            cursor = conn.execute(
                """INSERT INTO persona_editorial_evaluations(
                    run_id,persona_id,topic_input_hash,input_json,topic_json,status,notice,authority,tension,marginal_value,
                    why_me,claim_key,core_claim,reason_code,rationale,thesis_json,thesis_state,
                    open_loop,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, persona["id"], input_hash, json.dumps(input_payload, ensure_ascii=False),
                 json.dumps(topic, ensure_ascii=False), "WRITE", 5, 5, 4, 5, "这是该人设会说的",
                 claim_key, core_claim, "write", "测试",
                 json.dumps(thesis, ensure_ascii=False), "THESIS_APPROVED", "", now, now),
            )
        return cursor.lastrowid

    def insert_formal_queue_candidate(self, run_id, context_date, topic, *, slug="acheng",
                                      title="正式候选", body="这是一条已完成正式编辑链路的待审正文。",
                                      created_at=None):
        queued_topic = {**topic, "claim_key": f"{topic['claim_key']}-queue-{time.time_ns()}"}
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date, queued_topic, slug=slug,
            claim_key=f"{slug}-queue-{time.time_ns()}", core_claim=title,
        )
        now = created_at or int(time.time())
        with self.app_module.db() as conn:
            persona_id = conn.execute(
                "SELECT id FROM personas WHERE slug=?", (slug,)
            ).fetchone()[0]
            candidate_id = conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    persona_id, context_date, title, body, "needs_review",
                    self.app_module.persona_editorial_candidate_source(evaluation_id),
                    "{}", now, now,
                ),
            ).lastrowid
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET candidate_id=?,thesis_state='CANDIDATE_READY' WHERE id=?""",
                (candidate_id, evaluation_id),
            )
        return candidate_id, evaluation_id

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
        self.assertFalse(response.json()["operator_auth_enabled"])
        self.assertEqual(response.json()["daily_context_run_time"], "08:15")
        self.assertEqual(response.json()["gemini_pool_configured_slots"], 0)

        with patch.dict(os.environ, {
            "XOPS_GEMINI_API_KEY_1": "dummy-a",
            "XOPS_GEMINI_API_KEY_2": "dummy-b",
        }):
            self.assertEqual(self.client.get("/health").json()["gemini_pool_configured_slots"], 2)

    def test_operator_token_protects_all_api_writes_but_not_reads(self):
        writes = [
            ("PUT", "/api/context/daily-runs/2026-08-24/review"),
            ("POST", "/api/context/daily-runs/2026-08-24/approve"),
            ("POST", "/api/context/daily-runs/2026-08-24/run"),
            ("POST", "/api/daily-posts/generate"),
            ("POST", "/api/daily-posts/regenerate"),
            ("POST", "/api/persona-editorial-evaluations/1/retry"),
            ("POST", "/api/post-candidates/1/rewrite"),
            ("POST", "/api/post-candidates/1/published"),
        ]
        with patch.dict(os.environ, {"XOPS_OPERATOR_TOKEN": "operator-test-token"}):
            self.assertTrue(self.client.get("/health").json()["operator_auth_enabled"])
            self.assertEqual(self.client.get("/api/personas").status_code, 200)
            self.assertEqual(self.client.get("/api/daily-posts").status_code, 200)
            for method, path in writes:
                self.assertEqual(self.client.request(method, path, json={}).status_code, 401, path)
            self.assertEqual(
                self.client.post(
                    "/api/persona-editorial-evaluations/999999/retry",
                    headers={"X-Ops-Token": "operator-test-token"},
                ).status_code,
                404,
            )

    def test_daily_post_generation_endpoint_queues_approved_run_once(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        processed = []

        async def pipeline(received_run_id):
            processed.append(received_run_id)
            await asyncio.sleep(0.05)
            return [received_run_id]

        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "run_persona_editorial_pipeline", side_effect=pipeline
        ):
            first = self.client.post("/api/daily-posts/generate")
            second = self.client.post("/api/daily-posts/generate")
            for _ in range(20):
                if processed:
                    break
                time.sleep(0.01)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["started"])
        self.assertEqual(first.json()["run_id"], run_id)
        self.assertEqual(first.json()["poll_url"], "/api/daily-posts")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["started"])
        self.assertEqual(second.json()["status"], "already_running")
        self.assertEqual(processed, [run_id])

    def test_daily_post_generation_endpoint_requires_approved_today_context(self):
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "queue_daily_post_generation"
        ) as queue:
            missing = self.client.post("/api/daily-posts/generate")
            self.assertEqual(missing.status_code, 404)
            self.create_editorial_run(self.app_module.shanghai_today(), status="needs_review")
            unapproved = self.client.post("/api/daily-posts/generate")

        self.assertEqual(unapproved.status_code, 409)
        queue.assert_not_called()

    def test_daily_post_regeneration_supersedes_batch_and_starts_new_revision(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            persona_id = conn.execute("SELECT id FROM personas ORDER BY id LIMIT 1").fetchone()[0]
            now = int(time.time())
            evaluation_id = conn.execute(
                """INSERT INTO persona_editorial_evaluations(
                    run_id,persona_id,topic_input_hash,input_json,topic_json,status,
                    claim_key,core_claim,created_at,updated_at
                ) VALUES(?,?,?,?,?,'WRITE',?,?,?,?)""",
                (
                    run_id, persona_id, "old-hash", "{}", "{}",
                    "old-claim", "旧稿判断", now, now,
                ),
            ).lastrowid
            source = self.app_module.persona_editorial_candidate_source(evaluation_id)
            candidate_id = conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,created_at,updated_at
                ) VALUES(?,?,?,?,'needs_review',?,?,?)""",
                (persona_id, context_date, "旧稿判断", "旧稿正文", source, now, now),
            ).lastrowid
            conn.execute(
                "UPDATE persona_editorial_evaluations SET candidate_id=? WHERE id=?",
                (candidate_id, evaluation_id),
            )
            conn.execute(
                """INSERT INTO topic_claim_history(
                    claim_key,persona_id,subject,core_claim,context_date,source,status,created_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,'drafted',?,?)""",
                ("persona:test:old", persona_id, "旧题", "旧稿判断", context_date, source, now, now),
            )

        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "queue_daily_post_generation", return_value=True
        ) as queue:
            response = self.client.post("/api/daily-posts/regenerate")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["superseded"], 1)
        self.assertTrue(response.json()["started"])
        queue.assert_called_once_with(run_id)
        with self.app_module.db() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM post_candidates WHERE id=?", (candidate_id,)).fetchone()[0],
                "superseded",
            )
            evaluation = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
            self.assertEqual(tuple(evaluation), ("HOLD", "manual_regeneration"))
            self.assertEqual(
                conn.execute("SELECT status FROM topic_claim_history WHERE source=?", (source,)).fetchone()[0],
                "superseded",
            )
            self.assertEqual(
                conn.execute("SELECT approval_revision FROM daily_context_runs WHERE id=?", (run_id,)).fetchone()[0],
                1,
            )

    def test_daily_post_scheduler_queues_only_approved_today_run(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "queue_daily_post_generation", return_value=True
        ) as queue:
            self.assertTrue(asyncio.run(self.app_module.run_due_daily_post()))

        queue.assert_called_once_with(run_id)

    def test_daily_post_scheduler_approves_completed_context_before_queueing(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date, status="needs_review")
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true"}), patch.object(
            self.app_module, "queue_daily_post_generation", return_value=True
        ) as queue:
            self.assertTrue(asyncio.run(self.app_module.run_due_daily_post()))

        run = self.client.get(f"/api/context/daily-runs/{context_date}").json()
        self.assertEqual(run["status"], "approved")
        self.assertEqual(run["approval_revision"], 1)
        queue.assert_called_once_with(run_id)

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
                    evaluation_columns = {
                        row["name"] for row in migrated.execute(
                            "PRAGMA table_info(persona_editorial_evaluations)"
                        ).fetchall()
                    }
                self.assertIn("approval_revision", columns)
                self.assertIn("asset_id", candidate_columns)
                self.assertIn("generation_stage", evaluation_columns)
                self.assertIn("generation_state", evaluation_columns)
            finally:
                self.app_module.DATA_DIR = original_data_dir
                self.app_module.DB_PATH = original_db_path

    def test_dashboard_is_public(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("只重写这条", response.text)
        self.assertIn("setInterval", response.text)

    def test_seed_personas_migrates_taotao_name_without_resetting_edits(self):
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT draft FROM personas WHERE slug='college-student-linjia'"
            ).fetchone()
            draft = json.loads(row["draft"])
            draft["voice"]["tone"] = "保留这条人工编辑"
            draft["identity"]["name"] = "林佳"
            conn.execute(
                "UPDATE personas SET name='林佳',draft=? WHERE slug='college-student-linjia'",
                (json.dumps(draft, ensure_ascii=False),),
            )

        self.app_module.seed_personas()
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT name,draft FROM personas WHERE slug='college-student-linjia'"
            ).fetchone()
        migrated = json.loads(row["draft"])
        self.assertEqual(row["name"], "桃桃还没下课")
        self.assertEqual(migrated["identity"]["name"], "桃桃还没下课")
        self.assertEqual(migrated["voice"]["tone"], "保留这条人工编辑")

    def test_seed_personas_migrates_ai_display_id_without_resetting_edits(self):
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT draft FROM personas WHERE slug='hegong-afterwork'"
            ).fetchone()
            draft = json.loads(row["draft"])
            draft["voice"]["tone"] = "保留 AI 人设人工编辑"
            draft["identity"]["name"] = "何工下班后"
            draft["identity"]["profile"] = "# 何工下班后\n\n- 显示名：何工下班后"
            conn.execute(
                "UPDATE personas SET name='何工下班后',draft=? WHERE slug='hegong-afterwork'",
                (json.dumps(draft, ensure_ascii=False),),
            )

        self.app_module.seed_personas()
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT slug,name,draft FROM personas WHERE slug='hegong-afterwork'"
            ).fetchone()
        migrated = json.loads(row["draft"])
        self.assertEqual(row["slug"], "hegong-afterwork")
        self.assertEqual(row["name"], "Patch")
        self.assertEqual(migrated["identity"]["name"], "Patch")
        self.assertNotIn("何工下班后", migrated["identity"]["profile"])
        self.assertEqual(migrated["voice"]["tone"], "保留 AI 人设人工编辑")

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
        original_slugs = {
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
        }
        ai_slugs = {
            "hegong-afterwork",
            "zhaojie-process",
            "linxue-model",
            "xiaocheng-product",
            "ada-builds",
            "susu-multimodal",
            "zhangshifu-ai",
            "lianglaoban-ai",
            "mojie-eval",
            "wenwen-ai-industry",
        }
        self.assertTrue(original_slugs.issubset(slugs))
        self.assertTrue(ai_slugs.issubset(slugs))
        self.assertNotIn("office-worker-zhou", slugs)
        self.assertNotIn("county-mom-xiaomei", slugs)
        self.assertNotIn("cc0-source-selection", slugs)
        student = next(persona for persona in personas if persona["slug"] == "college-student-linjia")
        self.assertEqual(student["name"], "桃桃还没下课")
        self.assertEqual(student["display_name"], "桃桃还没下课")
        self.assertEqual(student["handle"], "@taotao_afterclass")

        atuo = next(persona for persona in personas if persona["slug"] == "atuo")
        self.assertEqual(atuo["display_name"], "阿拓Tuo")
        self.assertEqual(atuo["handle"], "@atuo_xyz")
        self.assertIn("atuo/avatar.png", atuo["avatar_url"])

        crypto_slugs = {"atuo", "axu", "nanqiao", "qiliang", "aye", "xiaoman", "maili"}
        crypto_names = {persona["display_name"] for persona in personas if persona["slug"] in crypto_slugs}
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

        expected_ai_ids = {
            "hegong-afterwork": "Patch",
            "zhaojie-process": "小顾",
            "linxue-model": "一觉",
            "xiaocheng-product": "一川",
            "ada-builds": "Ada",
            "susu-multimodal": "麦冬",
            "zhangshifu-ai": "未读",
            "lianglaoban-ai": "老闻",
            "mojie-eval": "白盒",
            "wenwen-ai-industry": "慢变量",
        }
        ai_personas = [persona for persona in personas if persona["slug"] in ai_slugs]
        self.assertEqual(
            {persona["slug"]: persona["display_name"] for persona in ai_personas},
            expected_ai_ids,
        )
        self.assertTrue(all(persona["handle"] == "" for persona in ai_personas))
        self.assertTrue(all(persona["avatar_url"].endswith("/avatar.svg") for persona in ai_personas))

        ai_bios = []
        ai_roles = []
        ai_voice_configs = []
        for persona in ai_personas:
            detail = self.client.get(f"/api/personas/{persona['id']}").json()
            draft = detail["draft"]
            self.assertEqual(persona["name"], expected_ai_ids[persona["slug"]])
            self.assertEqual(draft["identity"]["name"], expected_ai_ids[persona["slug"]])
            self.assertEqual(draft["config_revision"], 3)
            self.assertEqual(draft["content"]["topic_domain"], "ai")
            ai_bios.append(draft["identity"]["bio"])
            ai_roles.append(draft["identity"]["role"])
            ai_voice_configs.append(draft["voice"].get("tone") or draft["voice"].get("style_guide"))
        self.assertTrue(all(ai_bios))
        self.assertTrue(all(ai_roles))
        self.assertTrue(all(ai_voice_configs))
        self.assertEqual(len(set(ai_bios)), len(ai_slugs))
        self.assertGreater(len(set(ai_roles)), 1)
        self.assertGreater(len(set(ai_voice_configs)), 1)

    def test_daily_post_default_queue_includes_all_personas(self):
        expected = {
            "acheng", "ridehail-driver-zhao", "college-student-linjia", "atuo", "axu",
            "nanqiao", "qiliang", "aye", "xiaoman", "maili",
            "hegong-afterwork", "zhaojie-process", "linxue-model", "xiaocheng-product",
            "ada-builds", "susu-multimodal", "zhangshifu-ai", "lianglaoban-ai",
            "mojie-eval", "wenwen-ai-industry",
        }
        with patch.dict(os.environ):
            os.environ.pop("XOPS_DAILY_POST_PERSONAS", None)
            os.environ.pop("XOPS_DAILY_POST_PERSONA", None)
            self.assertTrue(expected.issubset(self.app_module.daily_post_persona_slugs()))

    def test_ai_persona_does_not_receive_default_crypto_public_topics(self):
        with self.app_module.db() as conn:
            ai_persona = dict(conn.execute(
                "SELECT id,slug,draft FROM personas WHERE slug='hegong-afterwork'"
            ).fetchone())
        crypto_topic = {
            "claim_key": "btc-market-width",
            "scope": "public",
            "title": "BTC 市场宽度",
            "core_claim": "BTC 走强不等于山寨全面扩散。",
        }
        explicit_crypto_topic = {**crypto_topic, "claim_key": "sol-liquidity", "topic_domain": "crypto"}
        ai_topic = {
            "claim_key": "model-release-workflow",
            "scope": "public",
            "topic_domain": "ai",
            "title": "模型更新改变工作流",
            "core_claim": "模型能力提升只有减少真实步骤才值得迁移。",
        }
        routed = self.app_module.persona_editorial_topics(
            ai_persona,
            [crypto_topic, explicit_crypto_topic, ai_topic],
            {},
        )
        self.assertEqual([topic["claim_key"] for topic in routed], [ai_topic["claim_key"]])
        self.assertEqual(routed[0]["topic_domain"], "ai")
        self.assertTrue(routed[0]["style_recipe"])

    def test_ai_persona_current_input_uses_same_domain_filtered_topic_batch(self):
        context_date = "2026-08-25"
        crypto_topic = {
            "claim_key": "btc-market-width", "scope": "public",
            "title": "BTC 市场宽度", "core_claim": "BTC 走强不等于山寨全面扩散。",
        }
        ai_topic = {
            "claim_key": "model-workflow", "scope": "public", "topic_domain": "ai",
            "title": "模型与工作流", "core_claim": "模型能力只有减少真实步骤才值得迁移。",
        }
        run_id = self.create_editorial_run(context_date, topics=[crypto_topic, ai_topic])
        with self.app_module.db() as conn:
            persona = conn.execute(
                "SELECT id FROM personas WHERE slug='hegong-afterwork'"
            ).fetchone()
            payload = self.app_module.current_editorial_input_payload(
                conn,
                {
                    "run_id": run_id,
                    "persona_id": persona["id"],
                    "topic_json": json.dumps(ai_topic, ensure_ascii=False),
                },
                context_date,
            )
        self.assertEqual([topic["claim_key"] for topic in payload["topic_batch"]], ["model-workflow"])
        self.assertEqual(payload["topic_batch"][0]["topic_domain"], "ai")
        self.assertTrue(payload["topic_batch"][0]["style_recipe"])

    def test_ai_topic_domain_survives_mother_topic_angle_expansion(self):
        parent = {
            **self.mother_topic("ai-model-release", "discussion:ai-models"),
            "topic_domain": "ai",
        }
        mothers = self.app_module.editorial_mother_topics(self.signal_cards([parent]))
        self.assertEqual(mothers[0]["topic_domain"], "ai")
        self.assertEqual(mothers[0]["seed_key"], "ai:discussion:ai-models")
        angle = self.expanded_angle(
            mothers[0]["seed_key"], "workflow-not-benchmark", "project_evaluation",
            "模型发布的产品价值取决于它是否减少真实工作步骤，而不是榜单名次。",
        )
        topics, rejected = self.app_module.bounded_editorial_angles(
            {"angles": [angle], "rejected_angles": []}, mothers, []
        )
        self.assertEqual(rejected, [])
        self.assertEqual(topics[0]["topic_domain"], "ai")

    def test_mother_topics_come_directly_from_signal_lanes(self):
        selected = self.mother_topic("prewritten-claim", "other:selected")
        cards = self.signal_cards([self.mother_topic()])
        cards["selected_topics"] = [selected]
        cards["discovery_topics"] = [{
            "key": "project:launch",
            "title": "早期项目发布",
            "topic_domain": "crypto",
            "unique_authors": 1,
            "post_count": 2,
            "sample_refs": ["x:project:1"],
            "sample_posts": [{"source_ref": "x:project:1", "text": "项目发布原帖"}],
        }]

        mothers = self.app_module.editorial_mother_topics(cards)

        self.assertEqual([item["seed_key"] for item in mothers], [
            "discussion:btc", "project:launch",
        ])
        self.assertEqual([item["source_lane"] for item in mothers], ["hot", "discovery"])
        self.assertTrue(all("selection_hints" not in item for item in mothers))
        self.assertNotIn("other:selected", {item["seed_key"] for item in mothers})

    def test_mother_topics_reserve_half_the_pool_for_each_domain(self):
        cards = {"discussion_topics": [], "discovery_topics": [], "selected_topics": []}
        for index in range(12):
            cards["discussion_topics"].append({
                "key": f"crypto:{index}", "title": f"Crypto {index}",
                "topic_domain": "crypto", "unique_authors": 3, "post_count": 3,
            })
        for index in range(8):
            cards["discovery_topics"].append({
                "key": f"ai:{index}", "title": f"AI {index}",
                "topic_domain": "ai", "unique_authors": 2, "post_count": 2,
            })

        mothers = self.app_module.signal_editorial_mother_topics(cards)

        self.assertEqual(len(mothers), 16)
        self.assertEqual(sum(item["topic_domain"] == "crypto" for item in mothers), 8)
        self.assertEqual(sum(item["topic_domain"] == "ai" for item in mothers), 8)
        cards["hot_topic_pool"] = {
            "mother_topics": [{"seed_key": "older:topic", "topic_domain": "crypto"}],
        }
        self.assertEqual(len(self.app_module.editorial_mother_topics(cards)), 16)

    def test_hot_topic_pool_keeps_today_plus_previous_two_days(self):
        today = "2026-08-28"
        for context_date, claim_key, source_key in (
            ("2026-08-27", "hot-one-day", "discussion:one-day"),
            ("2026-08-26", "hot-two-days", "discussion:two-days"),
            ("2026-08-25", "expired-three-days", "discussion:expired"),
        ):
            self.create_editorial_run(
                context_date,
                topics=[self.mother_topic(claim_key, source_key)],
            )

        with self.app_module.db() as conn:
            pool = self.app_module.rolling_hot_topic_pool(conn, today)

        self.assertEqual(pool["retention_days"], 3)
        self.assertEqual(pool["window_start"], "2026-08-26")
        keys = {item["seed_key"] for item in pool["mother_topics"]}
        self.assertIn("discussion:one-day", keys)
        self.assertIn("discussion:two-days", keys)
        self.assertNotIn("discussion:expired", keys)
        ages = {item["seed_key"]: item["hot_pool_age_days"] for item in pool["mother_topics"]}
        self.assertEqual(ages["discussion:one-day"], 1)
        self.assertEqual(ages["discussion:two-days"], 2)

    def test_hot_topic_pool_preserves_verified_news_facts(self):
        old_date, today = "2026-08-27", "2026-08-28"
        topic = self.mother_topic("hot-fact", "fact:x:hot:1")
        topic["source_refs"] = ["x:hot:1"]
        run_id = self.create_editorial_run(old_date, topics=[topic])
        fact_card = {
            "id": "fact-hot-1",
            "topic_domain": "crypto",
            "status": "verified",
            "source_ref": "x:hot:1",
            "representative_text": "项目在前一日发布了已核验的产品更新。",
        }
        with self.app_module.db() as conn:
            cards = self.app_module.json_value(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0], {})
            cards["fact_cards"] = [fact_card]
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
            pool = self.app_module.rolling_hot_topic_pool(conn, today)

        current_cards = {"selected_topics": [], "fact_cards": [], "hot_topic_pool": pool}
        mothers = self.app_module.editorial_mother_topics(current_cards)
        self.assertEqual(mothers[0]["seed_key"], "fact:x:hot:1")
        facts = self.app_module.editorial_verified_facts(
            current_cards,
            {
                "topic_domain": "crypto",
                "source_topic_keys": ["fact:x:hot:1"],
                "source_refs": ["x:hot:1"],
            },
            {},
        )
        self.assertEqual(facts["facts"][0]["text"], fact_card["representative_text"])
        self.assertTrue(facts["requires_fact_ids"])

    def test_reusable_topics_restore_unused_angle_but_skip_claimed_history(self):
        old_date, today = "2026-08-20", "2026-08-21"
        old_run = self.create_editorial_run(old_date)
        reusable = {
            **self.expanded_angle("discussion:btc", "unused-backlog-angle"),
            "scope": "public", "topic_domain": "crypto",
        }
        blocked = {
            **self.expanded_angle("discussion:btc", "blocked-backlog-angle"),
            "scope": "public", "topic_domain": "crypto",
            "core_claim": "这条已经进入待审核候选，不能被重新放回题池。",
        }
        with self.app_module.db() as conn:
            cards = self.app_module.json_value(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (old_run,)
            ).fetchone()[0], {})
            cards["editorial_angle_expansion"] = {
                "status": "ready", "expanded_topics": [reusable, blocked],
            }
            now = int(time.time())
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), old_run),
            )
            conn.execute(
                """INSERT INTO topic_claim_history(
                    claim_key,persona_id,subject,core_claim,context_date,source,status,created_at,last_seen_at
                ) VALUES(?,NULL,?,?,?,'persona_editorial_grok_gemini:99','drafted',?,?)""",
                ("blocked-backlog-claim", "历史候选", blocked["core_claim"], today, now, now),
            )
            topics = self.app_module.reusable_editorial_topics(conn, today, {"selected_topics": []})

        restored = next(item for item in topics if item["claim_key"] == "unused-backlog-angle")
        self.assertEqual(restored["reusable_origin"], "backlog")
        self.assertEqual(restored["reusable_from_context_date"], old_date)
        self.assertNotIn("blocked-backlog-angle", {item["claim_key"] for item in topics})

    def test_empty_hot_slate_uses_domain_specific_evergreen_candidates(self):
        context_date = "2026-08-21"
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps({"selected_topics": [], "domains": {"crypto": {}}}), run_id),
            )
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
        topics = asyncio.run(self._real_ensure_editorial_angle_expansion(
            run_id, {"selected_topics": [], "domains": {"crypto": {}}}, daily,
        ))
        self.assertTrue(topics)
        self.assertTrue(all(item["reusable_origin"] == "evergreen" for item in topics))
        self.assertEqual({item["topic_domain"] for item in topics}, {"crypto", "ai"})
        for topic in topics:
            self.assertTrue(self.app_module.editorial_content_structure(topic)["id"])

    def test_content_structure_changes_with_topic_and_validates_explicit_structure_id(self):
        opportunity = self.app_module.editorial_content_structure({"angle_family": "opportunity"})
        industry = self.app_module.editorial_content_structure({"angle_family": "industry_evaluation"})
        self.assertEqual(opportunity["id"], "participation_opportunity")
        self.assertEqual(industry["id"], "industry_structure")
        self.assertNotEqual(opportunity["hook_options"], industry["hook_options"])
        self.assertNotEqual(opportunity["cta"], industry["cta"])

        mothers = self.app_module.editorial_mother_topics(
            self.signal_cards([self.mother_topic()])
        )
        valid = self.expanded_angle("discussion:btc", "explicit-structure", "opportunity")
        valid["structure_id"] = "market_trade_setup"
        invalid = self.expanded_angle("discussion:btc", "invalid-structure")
        invalid["structure_id"] = "not-a-real-structure"
        mismatch = self.expanded_angle(
            "discussion:btc", "mismatched-structure", "project_evaluation"
        )
        mismatch["structure_id"] = "philosophy_wealth"
        topics, rejected = self.app_module.bounded_editorial_angles(
            {"angles": [valid, invalid, mismatch], "rejected_angles": []}, mothers, []
        )
        self.assertEqual([topic["claim_key"] for topic in topics], ["explicit-structure"])
        self.assertEqual(topics[0]["structure_id"], "market_trade_setup")
        self.assertEqual(
            [item["reason_code"] for item in rejected],
            ["invalid_content_structure", "content_structure_mismatch"],
        )

    def test_content_structure_is_machine_enforced_before_body_assembly(self):
        structures = self.app_module.editorial_content_structure_catalog()
        for structure_id in structures:
            structure = self.app_module.editorial_content_structure({
                "structure_id": structure_id,
            })
            self.assertTrue(structure["section_order"])
            self.assertEqual(structure["section_order"][-1], "cta")
            self.assertTrue(set(structure["required_sections"]).issubset(
                structure["section_order"]
            ))
            self.assertIn(structure["cta_mode"], self.app_module.EDITORIAL_CTA_MODES)

        trade = self.app_module.editorial_content_structure({
            "structure_id": "market_trade_setup",
        })
        trade_sections = {key: f"{key} 内容" for key in trade["section_order"]}
        trade_sections["cta"] = "如果确认条件成立，可以考虑按计划执行。"
        text, saved, annotations = self.app_module.assemble_editorial_sections(
            {"sections": trade_sections}, trade,
        )
        self.assertEqual(text.split("\n\n"), [
            trade_sections[key] for key in trade["section_order"]
        ])
        self.assertEqual(saved, trade_sections)
        self.assertEqual([item["section"] for item in annotations], trade["section_order"])

        fallback_text, _, _ = self.app_module.assemble_editorial_sections(
            {"sections": trade_sections, "reasoning_shape": ["not-configured"]}, trade,
        )
        self.assertEqual(fallback_text, text)

        missing_cta = {**trade_sections, "cta": ""}
        with self.assertRaisesRegex(RuntimeError, "必填内容段"):
            self.app_module.assemble_editorial_sections({"sections": missing_cta}, trade)

        news = self.app_module.editorial_content_structure({
            "structure_id": "news_explainer",
        })
        news_sections = {key: f"{key} 内容" for key in news["required_sections"]}
        news_sections["cta"] = "现在就去试。"
        with self.assertRaisesRegex(RuntimeError, "禁止 CTA"):
            self.app_module.assemble_editorial_sections({"sections": news_sections}, news)

    def test_same_public_topic_uses_same_structure_for_two_ai_personas(self):
        with self.app_module.db() as conn:
            personas = [dict(row) for row in conn.execute(
                "SELECT id,slug,draft FROM personas WHERE slug IN ('hegong-afterwork','zhaojie-process')"
            ).fetchall()]
        topic = {
            "claim_key": "same-ai-public-topic", "scope": "public", "topic_domain": "ai",
            "title": "同一条 AI 公共题", "core_claim": "同一公共题不应因人设改变内容结构。",
            "angle_family": "project_evaluation", "structure_id": "project_product_evaluation",
        }
        left = self.app_module.persona_editorial_topics(personas[0], [topic], {})
        right = self.app_module.persona_editorial_topics(personas[1], [topic], {})
        self.assertEqual(left[0]["structure_id"], "project_product_evaluation")
        self.assertEqual(left[0]["style_recipe"], right[0]["style_recipe"])

    def test_editorial_persona_card_removes_voice_level_hook_and_cta_rules(self):
        with self.app_module.db() as conn:
            persona = dict(conn.execute(
                "SELECT id,slug,draft FROM personas WHERE slug='hegong-afterwork'"
            ).fetchone())
        card = self.app_module.editorial_persona_card(persona)
        self.assertTrue(card["voice"])
        self.assertNotIn("examples", card)
        for key in (
            "opening_rules", "ending_rules", "narrative_order",
            "mobilization_style", "mobilization_patterns", "style_guide",
            "sentence_style", "favorite_phrases", "market_action_boundary",
        ):
            self.assertNotIn(key, card["voice"])
        self.assertNotIn("market_role", card["identity"])
        self.assertNotIn("profile", card["identity"])

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

    def test_angle_expansion_persists_before_persona_evaluation(self):
        parent = {**self.mother_topic(), "source_refs": ["x:btc:1", "x:btc:2"]}
        run_id = self.create_editorial_run("2026-08-21", topics=[parent])
        researched = {
            "text": "BTC 资金来源与山寨币外溢仍有争议。",
            "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"],
            "model": "grok-test",
        }
        angles = {
            "angles": [
                self.expanded_angle("discussion:btc", "btc-industry-angle"),
                self.expanded_angle(
                    "discussion:btc", "btc-trading-angle", "trading_philosophy",
                    "BTC 上涨时先判断资金会不会外溢，比照着指数追山寨币更重要。",
                ),
            ],
            "rejected_angles": [],
            "_model": "gemini-test",
        }
        seen = []

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            seen.extend(topics)
            decisions = {}
            for topic in topics:
                if topic["claim_key"] == "btc-industry-angle":
                    decisions.update(self.editorial_decision(
                        topic, "WRITE", claim_key="acheng-btc-industry-angle",
                        core_claim="这轮 BTC 上涨先改变的是山寨币筛选标准。",
                    ))
                else:
                    decisions.update(self.editorial_decision(topic, "IGNORE"))
            return decisions

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "ensure_editorial_angle_expansion",
            new=self._real_ensure_editorial_angle_expansion,
        ), patch.object(
            self.app_module, "research_editorial_angle_context_grok",
            AsyncMock(return_value=researched),
        ) as grok, patch.object(
            self.app_module, "expand_editorial_angles_gemini",
            AsyncMock(return_value=angles),
        ) as gemini, patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator),
        ):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(grok.await_count, 1)
        self.assertEqual(gemini.await_count, 1)
        self.assertEqual({item["claim_key"] for item in seen}, {"btc-industry-angle", "btc-trading-angle"})
        with self.app_module.db() as conn:
            cards = self.app_module.json_value(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0], {})
        stage = cards["editorial_angle_expansion"]
        self.assertEqual(stage["status"], "ready")
        self.assertEqual(len(stage["expanded_topics"]), 2)
        self.assertEqual(cards["selected_topics"][0]["claim_key"], "btc-mother")
        for topic in stage["expanded_topics"]:
            self.assertEqual(topic["source_topic_keys"], ["discussion:btc"])
            self.assertEqual(topic["source_refs"], ["x:btc:1", "x:btc:2"])
        with self.app_module.db() as conn:
            evaluation = conn.execute(
                """SELECT status,candidate_id FROM persona_editorial_evaluations
                   WHERE run_id=? AND json_extract(topic_json,'$.claim_key')='btc-industry-angle'""",
                (run_id,),
            ).fetchone()
            self.assertEqual(evaluation["status"], "WRITE")
            self.assertIsNotNone(evaluation["candidate_id"])

    def test_failed_mother_context_does_not_block_other_angles(self):
        btc = self.mother_topic()
        sol = {
            **self.mother_topic("sol-mother", "discussion:sol"),
            "subject": "Solana",
            "title": "SOL 供给结构",
            "source_topic_title": "SOL 供给结构",
            "source_refs": ["x:sol:1"],
        }
        run_id = self.create_editorial_run("2026-08-21", topics=[btc, sol])
        researched = {
            "text": "BTC 有实时语境，SOL 查询失败。",
            "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"],
            "model": "grok-test",
            "failed_seed_keys": ["discussion:sol"],
        }
        expanded_mothers = []

        async def expand(mothers, _daily, _research, _history):
            expanded_mothers.extend(mothers)
            return {
                "angles": [self.expanded_angle("discussion:btc", "btc-survives-sol-failure")],
                "rejected_angles": [],
                "_model": "gemini-test",
            }

        async def evaluator(_persona, _context, _daily, topics, _history, _count):
            return {
                key: value
                for topic in topics
                for key, value in self.editorial_decision(topic, "IGNORE").items()
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "ensure_editorial_angle_expansion",
            new=self._real_ensure_editorial_angle_expansion,
        ), patch.object(
            self.app_module, "research_editorial_angle_context_grok",
            AsyncMock(return_value=researched),
        ), patch.object(
            self.app_module, "expand_editorial_angles_gemini", AsyncMock(side_effect=expand),
        ), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator),
        ):
            self.run_editorial_pipeline(run_id)
        self.assertEqual([item["seed_key"] for item in expanded_mothers], ["discussion:btc"])
        with self.app_module.db() as conn:
            cards = self.app_module.json_value(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0], {})
        stage = cards["editorial_angle_expansion"]
        self.assertEqual([item["claim_key"] for item in stage["expanded_topics"]], [
            "btc-survives-sol-failure"
        ])
        self.assertIn("context_unavailable", {
            item["reason_code"] for item in stage["rejected_angles"]
        })

    def test_angle_expansion_rejects_duplicate_common_knowledge_and_no_conclusion(self):
        parent = self.mother_topic()
        mothers = self.app_module.editorial_mother_topics(self.signal_cards([parent]))
        accepted = self.expanded_angle("discussion:btc", "specific-angle")
        duplicate = self.expanded_angle(
            "discussion:btc", "duplicate-angle", core_claim=accepted["core_claim"],
        )
        common = self.expanded_angle(
            "discussion:btc", "common-angle", "trading_philosophy", "投资有风险",
        )
        waiting = self.expanded_angle(
            "discussion:btc", "waiting-angle", core_claim="这件事还要继续观察，等待更多信息。",
        )
        topics, rejected = self.app_module.bounded_editorial_angles(
            {"angles": [accepted, duplicate, common, waiting], "rejected_angles": []}, mothers, [],
        )
        self.assertEqual([item["claim_key"] for item in topics], ["specific-angle"])
        self.assertEqual(
            {item["reason_code"] for item in rejected},
            {"semantic_duplicate", "no_conclusion"},
        )

    def test_angle_expansion_rejects_unverified_numbers_but_allows_protocol_ids(self):
        mothers = self.app_module.editorial_mother_topics(
            self.signal_cards([self.mother_topic()])
        )
        safe = self.expanded_angle(
            "discussion:btc", "protocol-ids-angle",
            core_claim=(
                "TermMax S1、x402、L2、ERC-20、EIP-1559、GP-0003、SIMD-0096 和 BEP-20 的竞争重点"
                "正在从概念转向产品分发。"
            ),
        )
        numeric = [
            self.expanded_angle(
                "discussion:btc", "numeric-count-angle",
                core_claim="这个市场已有 204 万持有人，头部占比达到 77%。",
            ),
            self.expanded_angle(
                "discussion:btc", "numeric-apy-angle", core_claim="这个活动的 APY 是 12%。",
            ),
            self.expanded_angle(
                "discussion:btc", "numeric-date-angle", core_claim="这个产品将在 2026 年上线。",
            ),
            self.expanded_angle(
                "discussion:btc", "numeric-price-angle", core_claim="这个资产已经涨到 $120。",
            ),
        ]
        topics, rejected = self.app_module.bounded_editorial_angles(
            {"angles": [safe, *numeric], "rejected_angles": []}, mothers, [],
        )
        self.assertEqual([item["claim_key"] for item in topics], ["protocol-ids-angle"])
        self.assertEqual(
            [item["reason_code"] for item in rejected], ["unverified_numeric_angle"] * 4,
        )

    def test_ready_angle_expansion_resumes_without_researching(self):
        parent = self.mother_topic()
        run_id = self.create_editorial_run("2026-08-21", topics=[parent])
        researched = {
            "text": "实时语境", "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        }
        angles = {
            "angles": [self.expanded_angle("discussion:btc", "btc-resume-angle")],
            "rejected_angles": [], "_model": "gemini-test",
        }
        evaluator = AsyncMock(side_effect=[RuntimeError("temporary evaluator failure"), {
            "btc-resume-angle": self.editorial_decision(
                {"claim_key": "btc-resume-angle"}, "IGNORE"
            )["btc-resume-angle"]
        }])
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "ensure_editorial_angle_expansion",
            new=self._real_ensure_editorial_angle_expansion,
        ), patch.object(
            self.app_module, "research_editorial_angle_context_grok",
            AsyncMock(return_value=researched),
        ) as grok, patch.object(
            self.app_module, "expand_editorial_angles_gemini",
            AsyncMock(return_value=angles),
        ) as gemini, patch.object(
            self.app_module, "evaluate_persona_editorial", evaluator,
        ):
            self.run_editorial_pipeline(run_id)
            self.run_editorial_pipeline(run_id)
        self.assertEqual(grok.await_count, 1)
        self.assertEqual(gemini.await_count, 1)
        self.assertEqual(evaluator.await_count, 2)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchone()[0], 1)

    def test_angle_expansion_failure_retries_without_falling_back_to_mother_topic(self):
        parent = self.mother_topic()
        run_id = self.create_editorial_run("2026-08-21", topics=[parent])
        researched = {
            "text": "实时语境", "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        }
        research = AsyncMock(side_effect=[RuntimeError("temporary Grok failure"), researched])
        expansion = AsyncMock(return_value={
            "angles": [self.expanded_angle("discussion:btc", "btc-retried-angle")],
            "rejected_angles": [], "_model": "gemini-test",
        })
        evaluator = AsyncMock(side_effect=lambda _persona, _context, _daily, topics, _history, _count: {
            key: value
            for topic in topics
            for key, value in self.editorial_decision(topic, "IGNORE").items()
        })
        env = {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "1",
        }
        floor = patch.object(self.app_module, "ensure_daily_persona_draft_floor")
        with patch.dict(os.environ, env), patch.object(
            self.app_module, "ensure_editorial_angle_expansion",
            new=self._real_ensure_editorial_angle_expansion,
        ), patch.object(
            self.app_module, "research_editorial_angle_context_grok", research,
        ), patch.object(
            self.app_module, "expand_editorial_angles_gemini", expansion,
        ), patch.object(
            self.app_module, "evaluate_persona_editorial", evaluator,
        ), floor as draft_floor:
            self.run_editorial_pipeline(run_id)
            draft_floor.assert_not_called()
            with self.app_module.db() as conn:
                cards = self.app_module.json_value(conn.execute(
                    "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
                ).fetchone()[0], {})
                self.assertEqual(cards["editorial_angle_expansion"]["status"], "retry_wait")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
                ).fetchone()[0], 0)
                cards["editorial_angle_expansion"]["next_retry_at"] = 0
                conn.execute(
                    "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                    (json.dumps(cards, ensure_ascii=False), run_id),
                )
            self.run_editorial_pipeline(run_id)
        self.assertEqual(research.await_count, 2)
        self.assertEqual(expansion.await_count, 1)
        self.assertEqual(evaluator.await_count, 1)

    def test_angle_expansion_allows_explicit_zero_without_filling_lenses(self):
        mothers = self.app_module.editorial_mother_topics(
            self.signal_cards([self.mother_topic()])
        )
        topics, rejected = self.app_module.bounded_editorial_angles({
            "angles": [],
            "rejected_angles": [{
                "parent_seed_key": "discussion:btc",
                "title": "BTC 资金结构",
                "core_claim": "",
                "reason_code": "no_worthwhile_angle",
                "reason": "今天没有比已有讨论更进一步的结论。",
            }],
        }, mothers, [])
        self.assertEqual(topics, [])
        self.assertEqual(rejected[0]["reason_code"], "no_worthwhile_angle")

    def test_invalid_angle_response_retries_and_reuses_research(self):
        parent = self.mother_topic()
        context_date = "2026-08-21"
        run_id = self.create_editorial_run(context_date, topics=[parent])
        with self.app_module.db() as conn:
            run = conn.execute(
                "SELECT approval_revision FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
        daily["approval_revision"] = run["approval_revision"]
        research = AsyncMock(return_value={
            "text": "实时语境", "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        })
        expansion = AsyncMock(side_effect=[{}, {
            "angles": [self.expanded_angle("discussion:btc", "btc-valid-after-retry")],
            "rejected_angles": [], "_model": "gemini-test",
        }])
        with patch.object(
            self.app_module, "research_editorial_angle_context_grok", research,
        ), patch.object(
            self.app_module, "expand_editorial_angles_gemini", expansion,
        ):
            self.assertIsNone(asyncio.run(self._real_ensure_editorial_angle_expansion(
                run_id, self.signal_cards([parent]), daily,
            )))
            response = self.client.post(
                f"/api/context/daily-runs/{context_date}/retry-angle-expansion"
            )
            self.assertEqual(response.status_code, 200)
            topics = asyncio.run(self._real_ensure_editorial_angle_expansion(
                run_id, self.signal_cards([parent]), daily,
            ))
        self.assertEqual([item["claim_key"] for item in topics], ["btc-valid-after-retry"])
        self.assertEqual(research.await_count, 1)
        self.assertEqual(expansion.await_count, 2)

    def test_concurrent_angle_expansion_has_one_provider_owner(self):
        parent = self.mother_topic()
        context_date = "2026-08-21"
        run_id = self.create_editorial_run(context_date, topics=[parent])
        with self.app_module.db() as conn:
            run = conn.execute(
                "SELECT approval_revision FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
        daily["approval_revision"] = run["approval_revision"]

        async def slow_research(*_args):
            await asyncio.sleep(0.03)
            return {
                "text": "实时语境", "citations": ["https://example.com/btc"],
                "tool_usage": ["x_search", "web_search"], "model": "grok-test",
            }

        research = AsyncMock(side_effect=slow_research)
        expansion = AsyncMock(return_value={
            "angles": [self.expanded_angle("discussion:btc", "btc-single-owner")],
            "rejected_angles": [], "_model": "gemini-test",
        })

        async def run_two():
            return await asyncio.gather(*(
                self._real_ensure_editorial_angle_expansion(
                    run_id, self.signal_cards([parent]), daily,
                ) for _ in range(2)
            ))

        with patch.object(
            self.app_module, "research_editorial_angle_context_grok", research,
        ), patch.object(
            self.app_module, "expand_editorial_angles_gemini", expansion,
        ):
            results = asyncio.run(run_two())
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(research.await_count, 1)
        self.assertEqual(expansion.await_count, 1)

    def test_stale_angle_stage_cannot_generate_pending_write(self):
        parent = self.mother_topic()
        context_date = "2026-08-21"
        run_id = self.create_editorial_run(context_date, topics=[parent])
        with self.app_module.db() as conn:
            run = conn.execute(
                "SELECT approval_revision FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
        daily["approval_revision"] = run["approval_revision"]
        researched = {
            "text": "实时语境", "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        }
        expanded = self.expanded_angle("discussion:btc", "btc-stale-angle")
        with patch.object(
            self.app_module, "research_editorial_angle_context_grok",
            AsyncMock(return_value=researched),
        ), patch.object(
            self.app_module, "expand_editorial_angles_gemini",
            AsyncMock(return_value={
                "angles": [expanded], "rejected_angles": [], "_model": "gemini-test",
            }),
        ):
            topics = asyncio.run(self._real_ensure_editorial_angle_expansion(
                run_id, self.signal_cards([parent]), daily,
            ))
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date, topics[0], claim_key="stale-write",
            core_claim="旧角度不应在新 revision 下继续生成。",
        )
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE daily_context_runs SET approval_revision=approval_revision+1 WHERE id=?",
                (run_id,),
            )
        writer = AsyncMock(side_effect=AssertionError("stale stage must stop before Grok"))
        with patch.object(self.app_module, "enrich_persona_editorial_context", writer):
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(
                run_id, context_date,
            ))
        writer.assert_not_awaited()
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT status,reason_code,candidate_id FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        self.assertEqual((row["status"], row["reason_code"], row["candidate_id"]), (
            "HOLD", "input_changed_before_generation", None,
        ))

    def test_existing_evaluated_run_keeps_legacy_public_topics(self):
        context_date = "2026-08-21"
        parent = self.mother_topic()
        run_id = self.create_editorial_run(context_date, topics=[parent])
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date, parent, claim_key="legacy-angle", core_claim="旧运行已经完成评估。",
        )
        research = AsyncMock(side_effect=AssertionError("legacy run must not expand"))
        with self.app_module.db() as conn:
            before_hash = conn.execute(
                "SELECT topic_input_hash FROM persona_editorial_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()[0]
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "ensure_editorial_angle_expansion",
            new=self._real_ensure_editorial_angle_expansion,
        ), patch.object(
            self.app_module, "research_editorial_angle_context_grok", research,
        ):
            self.run_editorial_pipeline(run_id)
        research.assert_not_awaited()
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT topic_input_hash FROM persona_editorial_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
            cards = self.app_module.json_value(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0], {})
        self.assertEqual(row[0], before_hash)
        self.assertNotIn("editorial_angle_expansion", cards)

    def test_held_legacy_evaluations_do_not_block_direct_mother_rebuild(self):
        context_date = "2026-08-21"
        parent = self.mother_topic()
        run_id = self.create_editorial_run(context_date, topics=[parent])
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date, parent, claim_key="held-legacy", core_claim="旧评估已撤回。",
        )
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE persona_editorial_evaluations SET status='HOLD' WHERE id=?",
                (evaluation_id,),
            )
            run = conn.execute(
                "SELECT approval_revision FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
        daily["approval_revision"] = run["approval_revision"]
        research = AsyncMock(return_value={
            "text": "实时语境", "citations": ["https://example.com/btc"],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        })
        expansion = AsyncMock(return_value={
            "angles": [self.expanded_angle("discussion:btc", "rebuilt-angle")],
            "rejected_angles": [], "_model": "gemini-test",
        })
        with patch.object(
            self.app_module, "research_editorial_angle_context_grok", research,
        ), patch.object(
            self.app_module, "expand_editorial_angles_gemini", expansion,
        ):
            topics = asyncio.run(self._real_ensure_editorial_angle_expansion(
                run_id, self.signal_cards([parent]), daily,
            ))

        self.assertEqual([item["claim_key"] for item in topics], ["rebuilt-angle"])
        research.assert_awaited_once()

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

    def test_editorial_same_public_topic_has_one_winner(self):
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
            self.assertIn(("ridehail-driver-zhao", "IGNORE", "DUPLICATED_BY_STRONGER_PERSONA"), decisions)
            self.assertIn(("college-student-linjia", "WRITE", "write"), decisions)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 2)

    def test_public_topic_has_one_winner_even_when_evaluator_rewrites_claim_key(self):
        topic = {
            "claim_key": "one-public-topic", "subject": "热点事件", "title": "热点事件",
            "core_claim": "公共题单", "eligible": True,
        }
        run_id = self.create_editorial_run("2026-08-23", topics=[topic])
        generated = AsyncMock(return_value={"post": "公共题只保留一个人设的候选正文。"})

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            suffix = "acheng" if persona["slug"] == "acheng" else "zhao"
            return self.editorial_decision(
                topics[0], "WRITE", claim_key=f"rewritten-{suffix}",
                core_claim=f"{suffix} 的独立角度，但都来自同一公共题。",
                score=5 if suffix == "acheng" else 4,
            )

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            rows = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            self.assertEqual(sum(row["status"] == "WRITE" for row in rows), 2)
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
        topic = {"claim_key": "api-test", "title": "真实草稿", "fact_basis": ["已核验事实"], "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        self.insert_formal_queue_candidate(
            run_id, context_date, topic, title="真实草稿", body="只有完成正式链路的候选会出现。"
        )
        with patch.object(self.app_module, "shanghai_today", return_value=context_date):
            queue = self.client.get("/api/daily-posts").json()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["persona_slug"], "acheng")
        self.assertEqual(queue[0]["body"], "只有完成正式链路的候选会出现。")
        self.assertNotIn("queued", [item["status"] for item in queue])

    def test_daily_posts_api_hides_unpublished_candidates_from_prior_days(self):
        topic = {"claim_key": "daily-only", "title": "当天草稿", "fact_basis": ["已核验事实"], "eligible": True}
        today = self.app_module.shanghai_today()
        today_run = self.create_editorial_run(today, topics=[topic])
        old_run = self.create_editorial_run("2026-08-01", topics=[topic])
        self.insert_formal_queue_candidate(today_run, today, topic, title="当天草稿", body="今天的候选。")
        self.insert_formal_queue_candidate(old_run, "2026-08-01", topic, title="旧草稿", body="旧候选。")

        queue = self.client.get("/api/daily-posts").json()

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["context_date"], today)

    def test_candidate_feedback_rewrites_only_one_post_with_saved_context(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "rewrite-one", "title": "单条反馈", "core_claim": "只改这一条", "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        candidate_id, evaluation_id = self.insert_formal_queue_candidate(
            run_id, context_date, topic, title="单条反馈", body="这是需要重写的旧稿。"
        )
        state = {
            "topic": topic,
            "persona": {"slug": "acheng", "name": "阿坤在跑单", "card": {}, "continuity": {}},
            "writer_context": {
                "source_kind": "market", "source_id": "", "source_item": None,
                "first_person_allowed": False, "available_assets": [],
            },
            "verified_facts": {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False},
            "grok": {"text": "已保存的背景。", "citations": [], "tool_usage": ["x_search", "web_search"], "model": "grok-test"},
        }
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE persona_editorial_evaluations SET generation_stage='candidate_ready',generation_state=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), evaluation_id),
            )
        writer = AsyncMock(return_value={
            "text": "上一稿的问题不是信息不够，而是句子像一份自动生成的总结。现在只保留一个判断：反馈应该落到当前这条稿子上，已经完成的搜索、其他人设和其他候选都不需要重新运行。这样修改一次就能立刻看到结果。",
            "facts_used_ids": [], "stance": "只改当前稿", "model": "gemini-test",
        })
        with patch.object(self.app_module, "write_persona_editorial_gemini", writer):
            response = self.client.post(
                f"/api/post-candidates/{candidate_id}/rewrite",
                json={"feedback_code": "too_ai", "note": "少一点总结腔"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(writer.await_count, 1)
        self.assertIn("反馈应该落到当前这条稿子上", response.json()["body"])
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT body,notes FROM post_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            generation_state = json.loads(conn.execute(
                "SELECT generation_state FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()[0])
            other_count = conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0]
        self.assertEqual(other_count, 1)
        notes = json.loads(row["notes"])
        self.assertEqual(notes["feedback_history"][0]["feedback_code"], "too_ai")
        self.assertEqual(notes["gemini"]["structure_id"], "news_explainer")
        self.assertEqual(notes["critic"]["verdict"], "PASS")
        self.assertEqual(generation_state["topic"]["style_recipe"]["id"], "news_explainer")
        self.assertEqual(writer.await_args.args[1]["style_recipe"]["id"], "news_explainer")
        self.assertIn("上一稿", writer.await_args.args[-1])

    def test_daily_queue_excludes_initial_and_legacy_sources_and_requires_approved_write(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "formal-queue", "title": "正式候选", "fact_basis": ["已核验事实"], "eligible": True}
        approved_run = self.create_editorial_run(context_date, topics=[topic])
        formal_id, evaluation_id = self.insert_formal_queue_candidate(
            approved_run, context_date, topic, title="正式候选", body="正式链路完成后的候选。"
        )
        unapproved_run = self.create_editorial_run("2026-08-01", status="needs_review", topics=[topic])
        unapproved_id, _ = self.insert_formal_queue_candidate(
            unapproved_run, "2026-08-01", topic, title="未批准", body="不应进入队列。"
        )
        with self.app_module.db() as conn:
            persona = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()
            now = int(time.time())
            for source in (f"initial_batch:{context_date}:evergreen-01", "persona_editorial:legacy", "manual:legacy"):
                conn.execute(
                    """INSERT INTO post_candidates(
                        persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (persona["id"], context_date, "旧候选", "不应进入队列。", "needs_review", source, "{}", now, now),
                )
        queue = self.client.get("/api/daily-posts").json()
        latest = self.client.get("/api/daily-post")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["id"], formal_id)
        self.assertEqual(queue[0]["source"], self.app_module.persona_editorial_candidate_source(evaluation_id))
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["id"], formal_id)
        self.assertNotEqual(unapproved_id, formal_id)

    def test_persona_queues_advance_one_post_at_a_time(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "queue-topic", "title": "队列题", "fact_basis": ["已核验事实"], "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        now = int(time.time())
        first, _ = self.insert_formal_queue_candidate(
            run_id, context_date, topic, slug="acheng", title="阿成一", body="阿成第一条。", created_at=now
        )
        second, _ = self.insert_formal_queue_candidate(
            run_id, context_date, topic, slug="acheng", title="阿成二", body="阿成第二条。", created_at=now + 1
        )
        self.insert_formal_queue_candidate(
            run_id, context_date, topic, slug="atuo", title="阿拓一", body="阿拓第一条。", created_at=now
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

    def test_formal_pipeline_does_not_assume_three_news_plus_seven_evergreen_slots(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "single-topic", "subject": "唯一题目", "title": "唯一题目",
            "core_claim": "只有这一条值得写", "fact_basis": ["唯一已核验事实"], "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(topics[0], "WRITE", claim_key="one-only", core_claim="只生成一条候选")

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "1",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            rows = conn.execute("SELECT source FROM post_candidates").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["source"].startswith("persona_editorial_grok_gemini:"))

    def test_daily_pipeline_backfills_three_review_drafts_when_no_hot_topic_exists(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps({"selected_topics": []}), run_id),
            )

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return {
                str(topic["claim_key"]): self.editorial_decision(topic, "IGNORE")[str(topic["claim_key"])]
                for topic in topics
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "3",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ):
            self.run_editorial_pipeline(run_id)

        with self.app_module.db() as conn:
            rows = conn.execute(
                """SELECT c.status,c.source,e.topic_json
                   FROM post_candidates c
                   JOIN personas p ON p.id=c.persona_id
                   JOIN persona_editorial_evaluations e ON c.source=? || e.id
                   WHERE p.slug=? AND c.context_date=? ORDER BY c.id""",
                ("persona_editorial_grok_gemini:", "acheng", context_date),
            ).fetchall()

        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "needs_review" for row in rows))
        self.assertTrue(all(row["source"].startswith("persona_editorial_grok_gemini:") for row in rows))
        topics = [json.loads(row["topic_json"]) for row in rows]
        self.assertTrue(all(topic.get("scope") == "persona" for topic in topics))
        self.assertTrue(all(topic.get("source_kind") == "daily_supplement" for topic in topics))
        self.assertEqual(self.client.get("/api/daily-posts").status_code, 200)
        self.assertEqual(self.app_module.enrich_persona_editorial_context.await_count, 3)
        self.assertEqual(self.app_module.write_persona_editorial_gemini.await_count, 3)
        self.assertEqual(self.app_module.critique_persona_editorial_draft.await_count, 3)

    def test_daily_fallback_bank_has_one_week_of_domain_specific_slack(self):
        cards = self.app_module.fallback_editorial_cards()
        self.assertGreaterEqual(sum(card["topic_domain"] == "crypto" for card in cards), 24)
        self.assertGreaterEqual(sum(card["topic_domain"] == "ai" for card in cards), 24)
        self.assertTrue(any(card.get("source_mode") == "paraphrase" for card in cards))
        self.assertTrue(any(card.get("source_mode") == "approved_editorial" for card in cards))
        structures = self.app_module.editorial_content_structure_catalog()
        self.assertTrue(all(
            not card.get("structure_id") or card["structure_id"] in structures for card in cards
        ))

    def test_every_persona_has_a_valid_daily_supplement_thesis(self):
        with self.app_module.db() as conn:
            personas = [dict(row) for row in conn.execute("SELECT * FROM personas ORDER BY id")]
        for persona in personas:
            topics = self.app_module.daily_persona_supplement_topics(persona, "2026-08-28")
            self.assertTrue(any(
                not self.app_module.thesis_contract_errors(
                    topic, persona["slug"],
                    self.app_module.daily_supplement_decision(persona, topic)["thesis"],
                )
                for topic in topics
            ), persona["slug"])

    def test_daily_three_draft_target_is_idempotent_for_the_same_persona_and_day(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps({"selected_topics": []}), run_id),
            )

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return {
                str(topic["claim_key"]): self.editorial_decision(topic, "IGNORE")[str(topic["claim_key"])]
                for topic in topics
            }

        env = {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "3",
        }
        with patch.dict(os.environ, env), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ):
            self.run_editorial_pipeline(run_id)
            self.run_editorial_pipeline(run_id)

        with self.app_module.db() as conn:
            rows = conn.execute(
                """SELECT c.status,e.topic_json
                   FROM post_candidates c
                   JOIN personas p ON p.id=c.persona_id
                   JOIN persona_editorial_evaluations e ON c.source=? || e.id
                   WHERE p.slug=? AND c.context_date=? ORDER BY c.id""",
                ("persona_editorial_grok_gemini:", "acheng", context_date),
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "needs_review" for row in rows))
        self.assertTrue(all(json.loads(row["topic_json"]).get("source_kind") == "daily_supplement" for row in rows))
        self.assertEqual(self.app_module.enrich_persona_editorial_context.await_count, 3)
        self.assertEqual(self.app_module.write_persona_editorial_gemini.await_count, 3)

    def test_daily_target_caps_hot_topics_across_repeated_runs(self):
        context_date = self.app_module.shanghai_today()
        topics = [
            {
                "claim_key": f"hot-{index}", "subject": f"热点 {index}",
                "title": f"热点 {index}", "core_claim": f"热点判断 {index}",
                "eligible": True,
            }
            for index in range(4)
        ]
        run_id = self.create_editorial_run(context_date, topics=topics)

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return {
                str(topic["claim_key"]): self.editorial_decision(
                    topic, "WRITE", claim_key=f"persona-{topic['claim_key']}",
                    core_claim=f"人设判断 {topic['claim_key']}",
                )[str(topic["claim_key"])]
                for topic in topics
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "3",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ):
            self.run_editorial_pipeline(run_id)
            self.run_editorial_pipeline(run_id)

        with self.app_module.db() as conn:
            visible = conn.execute(
                """SELECT COUNT(*) FROM post_candidates c JOIN personas p ON p.id=c.persona_id
                   WHERE p.slug=? AND c.context_date=? AND c.status='needs_review'""",
                ("acheng", context_date),
            ).fetchone()[0]
        self.assertEqual(visible, 3)

    def test_concurrent_generation_never_inserts_a_fourth_visible_draft(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        for index in range(4):
            topic = {
                "claim_key": f"concurrent-{index}", "subject": f"并发题目 {index}",
                "title": f"并发题目 {index}", "core_claim": f"并发判断 {index}",
                "eligible": True,
            }
            self.insert_pending_editorial_write(
                run_id, context_date, topic,
                claim_key=f"concurrent-persona-{index}",
                core_claim=f"并发人设判断 {index}",
            )

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "3",
            "XOPS_EDITORIAL_GENERATION_CONCURRENCY": "5",
        }):
            asyncio.run(
                self.app_module.generate_pending_persona_editorial_candidates(
                    run_id, context_date
                )
            )

        with self.app_module.db() as conn:
            visible = conn.execute(
                """SELECT COUNT(*) FROM post_candidates c JOIN personas p ON p.id=c.persona_id
                   WHERE p.slug=? AND c.context_date=?
                     AND c.status IN ('needs_review','queued','published')""",
                ("acheng", context_date),
            ).fetchone()[0]
            capped = conn.execute(
                """SELECT COUNT(*) FROM persona_editorial_evaluations
                   WHERE run_id=? AND status='HOLD' AND reason_code='daily_target_reached'""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(visible, 3)
        self.assertEqual(capped, 1)

    def test_daily_posts_api_never_exposes_a_partial_generation_batch(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        topic = {
            "claim_key": "atomic-output", "subject": "完整输出",
            "title": "完整输出", "core_claim": "只展示完整推文批次", "eligible": True,
        }
        env = {
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "3",
        }
        with patch.dict(os.environ, env):
            for index in range(3):
                self.insert_formal_queue_candidate(
                    run_id, context_date, topic, slug="acheng",
                    title=f"阿成推文 {index}", body=f"阿成完整推文正文 {index}",
                )
            for index in range(2):
                self.insert_formal_queue_candidate(
                    run_id, context_date, topic, slug="ridehail-driver-zhao",
                    title=f"老任推文 {index}", body=f"老任完整推文正文 {index}",
                )
            partial = self.client.get("/api/daily-posts").json()
            self.assertEqual(len(partial), 5)

            self.insert_formal_queue_candidate(
                run_id, context_date, topic, slug="ridehail-driver-zhao",
                title="老任推文 2", body="老任完整推文正文 2",
            )
            posts = self.client.get("/api/daily-posts").json()

        self.assertEqual(len(posts), 6)
        self.assertTrue(all(post["body"] for post in posts))
        self.assertEqual({post["persona_slug"] for post in posts}, {
            "acheng", "ridehail-driver-zhao",
        })

    def test_daily_supplement_reuses_a_successful_claim_after_seven_day_cooldown(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        first_date = (today - timedelta(days=8)).isoformat()
        second_date = today.isoformat()
        card = {
            "id": "cooldown-method", "eligible": True, "topic_domain": "crypto",
            "subject": "仓位纪律", "title": "把错误成本放在第一位",
            "core_claim": "先把能承受的错误成本写清楚，才谈得上长期留在场内。",
            "specific_tension": "热闹的机会往往把退出条件藏在最不起眼的位置。",
            "non_obvious_delta": "判断质量要看能否执行，而不是复盘时能否说通。",
            "source_name": "公开投资方法论", "source_url": "https://example.com/method",
            "source_locator": "方法论摘要", "source_mode": "paraphrase",
            "method": "先定义可承受损失，再决定是否参与。",
            "structure_id": "philosophy_wealth",
        }

        def create_empty_run(context_date):
            run_id = self.create_editorial_run(context_date)
            with self.app_module.db() as conn:
                conn.execute(
                    "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                    (json.dumps({"selected_topics": []}), run_id),
                )
            return run_id

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "1",
        }), patch.object(self.app_module, "fallback_editorial_cards", return_value=[card]):
            first_run = create_empty_run(first_date)
            self.run_editorial_pipeline(first_run)
            with self.app_module.db() as conn:
                conn.execute(
                    "UPDATE post_candidates SET status='published' WHERE context_date=?", (first_date,)
                )
            second_run = create_empty_run(second_date)
            self.run_editorial_pipeline(second_run)

        with self.app_module.db() as conn:
            rows = conn.execute(
                """SELECT c.context_date,c.status,e.topic_json,e.core_claim
                   FROM post_candidates c
                   JOIN personas p ON p.id=c.persona_id
                   JOIN persona_editorial_evaluations e ON c.source=? || e.id
                   WHERE p.slug=? ORDER BY c.context_date,c.id""",
                ("persona_editorial_grok_gemini:", "acheng"),
            ).fetchall()
        self.assertEqual([row["context_date"] for row in rows], [first_date, second_date])
        self.assertEqual([row["status"] for row in rows], ["published", "needs_review"])
        self.assertTrue(all(
            json.loads(row["topic_json"]).get("source_kind") == "daily_supplement" for row in rows
        ))
        self.assertEqual(rows[0]["core_claim"], rows[1]["core_claim"])

    def test_daily_supplement_claim_stays_blocked_inside_seven_day_cooldown(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        first_date = (today - timedelta(days=7)).isoformat()
        second_date = today.isoformat()
        card = {
            "id": "cooldown-method", "eligible": True, "topic_domain": "crypto",
            "subject": "仓位纪律", "title": "把错误成本放在第一位",
            "core_claim": "先把能承受的错误成本写清楚，才谈得上长期留在场内。",
            "specific_tension": "热闹的机会往往把退出条件藏在最不起眼的位置。",
            "non_obvious_delta": "判断质量要看能否执行，而不是复盘时能否说通。",
            "source_name": "公开投资方法论", "source_url": "https://example.com/method",
            "source_locator": "方法论摘要", "source_mode": "paraphrase",
            "method": "先定义可承受损失，再决定是否参与。",
            "structure_id": "philosophy_wealth",
        }

        def create_empty_run(context_date):
            run_id = self.create_editorial_run(context_date)
            with self.app_module.db() as conn:
                conn.execute(
                    "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                    (json.dumps({"selected_topics": []}), run_id),
                )
            return run_id

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "1",
        }), patch.object(self.app_module, "fallback_editorial_cards", return_value=[card]):
            first_run = create_empty_run(first_date)
            self.run_editorial_pipeline(first_run)
            with self.app_module.db() as conn:
                conn.execute(
                    "UPDATE post_candidates SET status='published' WHERE context_date=?", (first_date,)
                )
            second_run = create_empty_run(second_date)
            self.run_editorial_pipeline(second_run)

        with self.app_module.db() as conn:
            rows = conn.execute(
                """SELECT c.context_date,c.status,e.topic_json
                   FROM post_candidates c
                   JOIN personas p ON p.id=c.persona_id
                   JOIN persona_editorial_evaluations e ON c.source=? || e.id
                   WHERE p.slug=? ORDER BY c.context_date,c.id""",
                ("persona_editorial_grok_gemini:", "acheng"),
            ).fetchall()
        self.assertEqual([row["context_date"] for row in rows], [first_date])
        self.assertEqual(rows[0]["status"], "published")
        self.assertEqual(json.loads(rows[0]["topic_json"]).get("source_kind"), "daily_supplement")

    def test_daily_supplement_reopens_old_false_positive_first_person_guard_hold(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "guard-recovery-judgment", "title": "把判断和经历分开",
            "core_claim": "先写清楚判断错在哪里，才配谈机会。",
            "topic_domain": "crypto", "scope": "persona", "source_kind": "daily_supplement",
            "source_id": "guard-recovery", "source_url": "https://example.com/method",
            "source_locator": "方法论摘要", "source_mode": "paraphrase",
            "method": "先定义错误条件，再评估机会。",
        }
        run_id = self.create_editorial_run(context_date)
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date, topic, claim_key="guard-recovery-judgment",
            core_claim=topic["core_claim"],
        )
        draft = {
            "text": "对我来说，最值得防的不是错过一波热度，而是把还没有验证的叙事当成确定性。市场一热，大家都在比谁先给答案；我更关注的是，进场之前有没有先写清楚什么情况算自己判断错了。能承认这一步，才有资格继续谈机会。",
            "facts_used_ids": [], "stance": "先定义错误条件", "model": "gemini-test",
        }
        state = {
            "draft": draft,
            "draft_failures": ["虚构或未授权的第一人称经历"],
            "writer_context": {"first_person_allowed": False},
            "verified_facts": {"facts": []},
            "critic": {"verdict": "REJECT"},
            "rewrite": {"text": "旧重写稿"},
            "rewrite_failures": ["虚构或未授权的第一人称经历"],
            "final_critic": {"verdict": "REJECT"},
            "deterministic_guard_revision": 1,
        }
        with self.app_module.db() as conn:
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',reason_code='grok_gemini_critic_reject',rationale='旧守卫误杀',
                       generation_stage='failed',generation_state=? WHERE id=?""",
                (json.dumps(state, ensure_ascii=False), evaluation_id),
            )

        self.app_module.reopen_daily_supplement_guard_rejections(run_id)

        with self.app_module.db() as conn:
            row = conn.execute(
                """SELECT status,reason_code,rationale,generation_stage,generation_state
                   FROM persona_editorial_evaluations WHERE id=?""", (evaluation_id,)
            ).fetchone()
        reopened = json.loads(row["generation_state"])
        self.assertEqual((row["status"], row["reason_code"], row["rationale"], row["generation_stage"]),
                         ("WRITE", "", "", "draft_ready"))
        self.assertEqual(reopened["draft_failures"], [])
        self.assertEqual(reopened["deterministic_guard_revision"], self.app_module.EDITORIAL_DETERMINISTIC_GUARD_REVISION)
        self.assertFalse({"critic", "rewrite", "rewrite_failures", "final_critic"} & set(reopened))

    def test_daily_supplement_fake_trade_hold_is_not_reopened(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "guard-recovery-fake-trade", "title": "伪造交易经历不能恢复",
            "core_claim": "方法论不能补造交易经历。",
            "topic_domain": "crypto", "scope": "persona", "source_kind": "daily_supplement",
            "source_id": "guard-fake-trade", "source_url": "https://example.com/method",
            "source_locator": "方法论摘要", "source_mode": "paraphrase",
            "method": "先定义错误条件，再评估机会。",
        }
        run_id = self.create_editorial_run(context_date)
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date, topic, claim_key="guard-recovery-fake-trade",
            core_claim=topic["core_claim"],
        )
        state = {
            "draft": {
                "text": "我买入这条之后就一直盯着盘面，后来又在账户里加过仓，所以这次最想提醒大家别只看热度。真正麻烦的从来不是消息慢半拍，而是在还没想清楚退出条件时就把仓位推上去。方法论可以讨论，但不能把不存在的交易经历写成事实。",
                "facts_used_ids": [], "stance": "不能伪造交易经历", "model": "gemini-test",
            },
            "draft_failures": ["虚构或未授权的第一人称经历"],
            "writer_context": {"first_person_allowed": False},
            "verified_facts": {"facts": []},
            "critic": {"verdict": "REJECT"},
            "deterministic_guard_revision": 1,
        }
        with self.app_module.db() as conn:
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',reason_code='grok_gemini_critic_reject',rationale='第一人称越界',
                       generation_stage='failed',generation_state=? WHERE id=?""",
                (json.dumps(state, ensure_ascii=False), evaluation_id),
            )

        self.app_module.reopen_daily_supplement_guard_rejections(run_id)

        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT status,reason_code,generation_stage,generation_state FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        retained = json.loads(row["generation_state"])
        self.assertEqual((row["status"], row["reason_code"], row["generation_stage"]),
                         ("HOLD", "grok_gemini_critic_reject", "failed"))
        self.assertEqual(retained["draft_failures"], ["虚构或未授权的第一人称经历"])
        self.assertIn("critic", retained)

    def test_formal_pipeline_uses_local_gate_before_queueing(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "formal-audit", "subject": "热点协议", "title": "热点协议的新变化",
            "core_claim": "这个变化改变了用户比较机会成本的方式。",
            "fact_basis": ["项目官方在 2026-08-24 更新了公开规则。"], "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(
                topics[0], "WRITE", claim_key="formal-audit-claim", core_claim="这次不是旧规则的重复。"
            )

        grok = AsyncMock(return_value={
            "text": "X 上讨论集中在新规则改变了原有比较基准。",
            "citations": ["https://x.com/example/status/1", "https://example.com/official"],
            "tool_usage": ["x_search", "web_search"], "model": "grok-4.6",
        })
        writer = AsyncMock(return_value={
            "text": "这次变化真正值得看的是，原来大家比较的是表面收益，现在得把规则本身带来的成本一起算进去。参与之前先把原来的判断基准换掉，再决定是否参与。真正会拉开差距的，不是消息看得更快，而是能不能把这一层成本落到参与选择里。",
            "facts_used_ids": [],
            "stance": "先重算判断基准", "style_id": "news_explainer",
            "model": "gemini-3.1-pro-preview",
        })
        critic = AsyncMock(return_value={"verdict": "PASS", "reasons": ["主题具体且有判断"], "rewrite_instruction": ""})
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "enrich_persona_editorial_context", grok), patch.object(
            self.app_module, "write_persona_editorial_gemini", writer
        ), patch.object(self.app_module, "critique_persona_editorial_draft", critic):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(grok.await_count, 1)
        self.assertEqual(writer.await_count, 1)
        self.assertEqual(critic.await_count, 1)
        with self.app_module.db() as conn:
            row = conn.execute("SELECT source,notes FROM post_candidates").fetchone()
        self.assertTrue(row["source"].startswith("persona_editorial_grok_gemini:"))
        audit = json.loads(row["notes"])
        self.assertEqual(audit["topic"]["claim_key"], "formal-audit")
        self.assertEqual(audit["grok"]["model"], "grok-4.6")
        self.assertEqual(audit["grok"]["citations"], ["https://x.com/example/status/1", "https://example.com/official"])
        self.assertEqual(audit["grok"]["tool_usage"], ["x_search", "web_search"])
        self.assertTrue(audit["grok"]["context_hash"])
        self.assertEqual(audit["verified_facts"], {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False})
        self.assertEqual(audit["facts_used_ids"], [])
        self.assertEqual(audit["stance"], "先重算判断基准")
        self.assertEqual(audit["gemini"], {
            "model": "gemini-3.1-pro-preview", "attempts": 1,
            "structure_id": "news_explainer", "structure_revision": 4,
        })
        self.assertEqual(audit["critic"]["verdict"], "PASS")
        self.assertEqual(audit["critic"]["mode"], "llm_critic")

    def test_formal_critic_rejection_holds_evaluation_and_creates_no_candidate(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "critic-reject", "subject": "热点协议", "title": "没有信息量的题目",
            "core_claim": "不应强行凑稿。", "fact_basis": ["已核验事实。"], "eligible": True,
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(topics[0], "WRITE", claim_key="critic-reject-claim", core_claim="这个观点没有新信息")

        grok = AsyncMock(return_value={
            "text": "市场背景。", "citations": [], "tool_usage": ["x_search", "web_search"], "model": "grok-4.6",
        })
        writer = AsyncMock(return_value={
            "text": "这一条看起来很像帖子，但没有可供读者使用的新冲突，只是把大家已经知道的背景换了一种顺序再说一遍，因此不能进入正式候选。",
            "facts_used_ids": [], "stance": "", "model": "gemini-3.1-pro-preview",
        })
        critic = AsyncMock(return_value={
            "verdict": "REJECT", "reasons": ["只有常识，没有新的冲突或判断"], "rewrite_instruction": "补足题目中真正的新变化。",
        })
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_EDITORIAL_ALWAYS_CRITIQUE": "true",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "enrich_persona_editorial_context", grok), patch.object(
            self.app_module, "write_persona_editorial_gemini", writer
        ), patch.object(self.app_module, "critique_persona_editorial_draft", critic):
            self.run_editorial_pipeline(run_id)

        self.assertEqual(writer.await_count, 2)
        self.assertEqual(critic.await_count, 2)
        with self.app_module.db() as conn:
            evaluation = conn.execute(
                "SELECT status,reason_code,candidate_id FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchone()
            candidate_count = conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0]
        self.assertEqual(tuple(evaluation), ("HOLD", "grok_gemini_critic_reject", None))
        self.assertEqual(candidate_count, 0)

    def test_formal_generation_uses_bounded_concurrency_without_duplicate_candidates(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        for index in range(4):
            topic = {
                "claim_key": f"parallel-{index}", "title": f"并发题目 {index}",
                "core_claim": f"并发核心判断 {index}", "eligible": True,
            }
            self.insert_pending_editorial_write(
                run_id, context_date, topic,
                claim_key=f"parallel-claim-{index}", core_claim=f"并发核心判断 {index}",
            )
        active = peak = 0

        async def grok(*_args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return {"text": "并发测试背景。", "citations": [], "tool_usage": ["x_search", "web_search"], "model": "grok-test"}

        writer = AsyncMock(return_value={
            "text": "这是一条用于验证并发入库边界的完整测试正文，它保持具体判断且不添加外部事实，让测试只验证同一批正式生成不会重复插入候选。这里补足测试语境，确保长度门槛不会把并发边界测试误判成需要重写的内容。",
            "facts_used_ids": [], "stance": "并发测试判断", "model": "gemini-test",
        })
        critic = AsyncMock(return_value={"verdict": "PASS", "reasons": [], "unsupported_claims": [], "rewrite_instruction": ""})
        with patch.dict(os.environ, {"XOPS_EDITORIAL_GENERATION_CONCURRENCY": "2"}), patch.object(
            self.app_module, "enrich_persona_editorial_context", AsyncMock(side_effect=grok)
        ), patch.object(self.app_module, "write_persona_editorial_gemini", writer), patch.object(
            self.app_module, "critique_persona_editorial_draft", critic
        ):
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(run_id, context_date))
            self.assertEqual(self.app_module.editorial_generation_concurrency(), 2)
        self.assertEqual(peak, 2)
        self.assertEqual(writer.await_count, 4)
        with self.app_module.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 4)
        with patch.dict(os.environ, {"XOPS_EDITORIAL_GENERATION_CONCURRENCY": "bad"}):
            self.assertEqual(self.app_module.editorial_generation_concurrency(), 3)
        with patch.dict(os.environ, {"XOPS_EDITORIAL_GENERATION_CONCURRENCY": "0"}):
            self.assertEqual(self.app_module.editorial_generation_concurrency(), 1)
        with patch.dict(os.environ, {"XOPS_EDITORIAL_GENERATION_CONCURRENCY": "9"}):
            self.assertEqual(self.app_module.editorial_generation_concurrency(), 5)

    def test_non_ai_generation_passes_the_same_nonempty_topic_structure_to_writer_and_critic(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "crypto-opportunity-structure", "title": "小资金流动性活动",
            "topic_domain": "crypto", "angle_family": "opportunity",
            "core_claim": "参与条件合适时可以把收益和成本一起粗算后参与。",
            "action_setup": "活动规则与成本已经明确。",
            "action_trigger": "粗算后的正向空间覆盖资金占用。",
            "action_invalidation": "规则、成本或退出条件变化。",
            "action_consequence": "条件成立时小资金可以参与。",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        self.insert_pending_editorial_write(run_id, context_date, topic, slug="atuo")
        writer = AsyncMock(return_value={
            "text": "这类流动性活动真正值得看的是，收益不只来自表面数字，而是来自你能否接受资金占用和参与条件。把这两件事放在一起粗算，条件合适的小资金可以参与，不必把它做成一份操作手册。",
            "facts_used_ids": [], "stance": "条件合适可以参与", "model": "gemini-test",
        })
        critic = AsyncMock(return_value={
            "verdict": "PASS", "reasons": [], "unsupported_claims": [],
            "rewrite_instruction": "", "model": "gemini-test", "mode": "llm_critic",
        })
        with patch.object(
            self.app_module, "enrich_persona_editorial_context", AsyncMock(return_value={
                "text": "背景。", "citations": [], "tool_usage": ["x_search"], "model": "grok-test",
            })
        ), patch.object(
            self.app_module, "write_persona_editorial_gemini", writer
        ), patch.object(self.app_module, "critique_persona_editorial_draft", critic):
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(run_id, context_date))

        writer_topic = writer.await_args.args[1]
        critic_topic = critic.await_args.args[1]
        self.assertEqual(writer_topic["topic_domain"], "crypto")
        self.assertEqual(writer_topic["structure_id"], "participation_opportunity")
        self.assertTrue(writer_topic["style_recipe"])
        self.assertEqual(writer_topic["style_recipe"], critic_topic["style_recipe"])
        self.assertEqual(writer_topic["style_recipe"]["id"], "participation_opportunity")

    def test_persona_evaluation_uses_bounded_concurrency_and_isolates_runtime_errors(self):
        run_id = self.create_editorial_run("2026-08-26")
        active = peak = 0

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.03)
                if persona["slug"] == "ridehail-driver-zhao":
                    raise RuntimeError("one persona evaluator failed")
                if persona["slug"] == "acheng":
                    return self.editorial_decision(topics[0], "HOLD")
                return self.editorial_decision(
                    topics[0], "WRITE",
                    claim_key=f"{persona['slug']}-parallel-claim",
                    core_claim=f"{persona['slug']} 的独立判断。",
                )
            finally:
                active -= 1

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true",
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao,college-student-linjia",
            "XOPS_EDITORIAL_EVALUATION_CONCURRENCY": "2",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ) as evaluate, patch.object(
            self.app_module, "generate_pending_persona_editorial_candidates", AsyncMock(return_value=None)
        ) as generate:
            self.run_editorial_pipeline(run_id)
            self.assertEqual(self.app_module.editorial_evaluation_concurrency(), 2)

        self.assertEqual(peak, 2)
        self.assertLessEqual(peak, 2)
        self.assertEqual(evaluate.await_count, 3)
        generate.assert_awaited()
        with self.app_module.db() as conn:
            rows = [tuple(row) for row in conn.execute(
                """SELECT p.slug,e.status FROM persona_editorial_evaluations e
                   JOIN personas p ON p.id=e.persona_id
                   WHERE e.run_id=? ORDER BY p.slug""",
                (run_id,),
            ).fetchall()]
        self.assertEqual(rows, [
            ("acheng", "HOLD"),
            ("college-student-linjia", "WRITE"),
        ])

    def test_editorial_evaluation_concurrency_bounds(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.app_module.editorial_evaluation_concurrency(), 5)
        with patch.dict(os.environ, {"XOPS_EDITORIAL_EVALUATION_CONCURRENCY": "bad"}):
            self.assertEqual(self.app_module.editorial_evaluation_concurrency(), 5)
        with patch.dict(os.environ, {"XOPS_EDITORIAL_EVALUATION_CONCURRENCY": "0"}):
            self.assertEqual(self.app_module.editorial_evaluation_concurrency(), 1)
        with patch.dict(os.environ, {"XOPS_EDITORIAL_EVALUATION_CONCURRENCY": "9"}):
            self.assertEqual(self.app_module.editorial_evaluation_concurrency(), 5)

    def test_formal_generation_resumes_from_persisted_context_after_writer_failure(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        evaluation_id = self.insert_pending_editorial_write(
            run_id, context_date,
            {"claim_key": "resume-stage", "title": "断点生成", "core_claim": "只补失败阶段"},
        )
        grok = AsyncMock(return_value={
            "text": "已完成的背景不应重复搜索。", "citations": [],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        })
        writer = AsyncMock(side_effect=[
            RuntimeError("temporary writer outage"),
            {
                "text": "这次重试只应该继续正文生成，已经完成的市场搜索不需要再付一次成本。断点真正节省的不是几行代码，而是上游偶发失败时，不会把十条内容全部推回起点。每一条只补自己缺失的阶段，反馈也就不用再等整批重跑。",
                "facts_used_ids": [], "stance": "只补失败阶段", "model": "gemini-test",
            },
        ])
        with patch.object(
            self.app_module, "enrich_persona_editorial_context", grok
        ), patch.object(self.app_module, "write_persona_editorial_gemini", writer):
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(run_id, context_date))
            with self.app_module.db() as conn:
                pending = conn.execute(
                    "SELECT generation_stage,generation_state,reason_code FROM persona_editorial_evaluations WHERE id=?",
                    (evaluation_id,),
                ).fetchone()
            self.assertEqual(pending["generation_stage"], "draft_generating")
            self.assertTrue(json.loads(pending["generation_state"])["grok"])
            self.assertEqual(pending["reason_code"], "formal_generation_retryable")
            self.assertEqual(
                self.client.post(f"/api/persona-editorial-evaluations/{evaluation_id}/retry").status_code,
                200,
            )
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(run_id, context_date))
        self.assertEqual(grok.await_count, 1)
        self.assertEqual(writer.await_count, 2)
        with self.app_module.db() as conn:
            completed = conn.execute(
                "SELECT generation_stage,candidate_id FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        self.assertEqual(completed["generation_stage"], "candidate_ready")
        self.assertIsNotNone(completed["candidate_id"])

    def test_formal_generation_reuses_daily_mother_topic_research(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "reuse-daily-research", "title": "复用每日研究",
            "core_claim": "写稿不再重复搜索", "parent_seed_key": "mother-ai-1",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        with self.app_module.db() as conn:
            run = conn.execute(
                "SELECT raw_cards,approval_revision FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()
            cards = json.loads(run["raw_cards"])
            daily = self.app_module.daily_context_dict(conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
            ).fetchone())
            daily["approval_revision"] = run["approval_revision"]
            cards["editorial_angle_expansion"] = {
                "status": "ready",
                "input_hash": self.app_module.editorial_angle_input_hash(
                    self.app_module.editorial_mother_topics(cards), daily
                ),
                "expanded_topics": [topic],
                "research": {
                "contexts": [{
                    "seed_key": "mother-ai-1", "background": "已研究背景",
                    "current_debate": "已研究争议", "strongest_for": "正方",
                    "strongest_against": "反方", "second_order_effect": "二阶影响",
                    "stale_or_common": "旧常识",
                }],
                "citations": ["https://example.com/official"],
                "tool_usage": ["x_search", "web_search"], "model": "grok-daily",
            }}
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
        self.insert_pending_editorial_write(run_id, context_date, topic)
        grok = AsyncMock(side_effect=AssertionError("daily research must be reused"))
        writer = AsyncMock(return_value={
            "text": "当天母题已经完成过一次搜索，写稿阶段直接取出对应争议和背景即可。这样每个人设仍然可以给出不同判断，但不会为了同一条前情再次调用 Grok。研究只做一次，生成失败时也只补正文这一段。",
            "facts_used_ids": [], "stance": "研究只做一次", "model": "gemini-test",
        })
        with patch.object(
            self.app_module, "enrich_persona_editorial_context", grok
        ), patch.object(self.app_module, "write_persona_editorial_gemini", writer):
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(run_id, context_date))
        grok.assert_not_awaited()
        with self.app_module.db() as conn:
            candidate = conn.execute("SELECT notes FROM post_candidates").fetchone()
            evaluation = dict(conn.execute(
                "SELECT status,reason_code,rationale,generation_stage FROM persona_editorial_evaluations WHERE run_id=?",
                (run_id,),
            ).fetchone())
        self.assertIsNotNone(candidate, evaluation)
        audit = json.loads(candidate[0])
        self.assertEqual(audit["grok"]["source"], "daily_mother_topic_research")

    def test_generation_critic_reviews_draft_and_targeted_rewrite(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        self.insert_pending_editorial_write(
            run_id, context_date,
            {"claim_key": "targeted-review", "title": "只审问题稿", "core_claim": "本地门槛先行"},
        )
        writer = AsyncMock(side_effect=[
            {"text": "太短。", "facts_used_ids": [], "stance": "", "model": "gemini-test"},
            {
                "text": "本地规则能直接发现长度、模板句和第一人称越界，就没有必要让第二个模型把十条稿子重新读一遍。只有真正触发问题的那一条才进入主编环节，拿到定向修改意见后再重写一次。这样省掉大部分二审调用，同时保留人工审核入口。",
                "facts_used_ids": [], "stance": "只审问题稿", "model": "gemini-test",
            },
        ])
        critic = AsyncMock(side_effect=[
            {
                "verdict": "REJECT", "reasons": ["正文过短"],
                "unsupported_claims": [], "rewrite_instruction": "补足具体判断。",
                "model": "gemini-test", "mode": "llm_critic",
            },
            {
                "verdict": "PASS", "reasons": [], "unsupported_claims": [],
                "rewrite_instruction": "", "model": "gemini-test", "mode": "llm_critic",
            },
        ])
        with patch.object(
            self.app_module, "enrich_persona_editorial_context", AsyncMock(return_value={
                "text": "背景。", "citations": [], "tool_usage": ["x_search", "web_search"], "model": "grok-test",
            })
        ), patch.object(
            self.app_module, "write_persona_editorial_gemini", writer
        ), patch.object(self.app_module, "critique_persona_editorial_draft", critic):
            asyncio.run(self.app_module.generate_pending_persona_editorial_candidates(run_id, context_date))
        self.assertEqual(writer.await_count, 2)
        self.assertEqual(critic.await_count, 2)
        with self.app_module.db() as conn:
            audit = json.loads(conn.execute("SELECT notes FROM post_candidates").fetchone()[0])
        self.assertEqual(audit["critic"]["mode"], "llm_critic")

    def test_editorial_verified_facts_never_promotes_free_form_or_opinion_basis(self):
        facts = self.app_module.editorial_verified_facts(
            {
                "fact_cards": [{
                    "status": "verified", "representative_source_ref": "fact:official-card",
                    "representative_text": "只允许这条经过验证的事实。",
                }],
            },
            {
                "source_refs": ["opinion:popular-thread"],
                "fact_basis": ["模型或编辑自由写入的事实，绝不能升级。"],
                "content_type": "editorial",
            },
            {},
        )
        self.assertEqual(facts, {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False})

    def test_editorial_verified_facts_require_exact_verified_fact_card_provenance(self):
        raw_cards = {
            "fact_cards": [
                {
                    "status": "verified", "representative_source_ref": "fact-card-42",
                    "representative_text": "官网公告确认的事实。",
                    "evidence": [{"source_ref": "official:announcement-42"}],
                },
                {
                    "status": "two_source_candidate", "representative_source_ref": "fact-card-unverified",
                    "representative_text": "还不能写成事实。",
                },
            ],
        }
        facts = self.app_module.editorial_verified_facts(
            raw_cards,
            {"source_refs": ["fact:fact-card-42", "fact:fact-card-unverified"]},
            {},
        )
        self.assertEqual(facts["schema"], "facts_used_ids")
        self.assertTrue(facts["requires_fact_ids"])
        self.assertEqual(facts["facts"], [{
            "id": "fact:fact-card-42", "text": "官网公告确认的事实。",
            "source_refs": ["fact-card-42", "official:announcement-42"], "status": "verified",
        }])

    def test_editorial_verified_facts_accepts_exact_referenced_source_post(self):
        raw_cards = {
            "discussion_topics": [{
                "sample_posts": [
                    {
                        "source_ref": "2092025531684532245",
                        "text": "项目宣布 TVL 已达到 2 亿美元。",
                        "url": "https://x.com/project/status/2092025531684532245",
                        "handle": "project",
                        "created_at": "2026-08-28T01:00:00Z",
                    },
                    {
                        "source_ref": "not-selected",
                        "text": "这条没有被选题引用。",
                    },
                ],
            }],
        }
        topic = {"source_refs": ["2092025531684532245"]}
        facts = self.app_module.editorial_verified_facts(raw_cards, topic, {})
        self.assertEqual(facts["facts"], [{
            "id": "tweet:2092025531684532245",
            "text": "项目宣布 TVL 已达到 2 亿美元。",
            "source_refs": [
                "2092025531684532245",
                "https://x.com/project/status/2092025531684532245",
            ],
            "status": "source_reported",
            "actor": "project",
            "action": "published",
            "object": "项目宣布 TVL 已达到 2 亿美元。",
            "observed_at": "2026-08-28T01:00:00Z",
            "epistemic_status": "SOURCE_REPORTED",
        }])
        payload = self.app_module.compile_reality_payload(raw_cards, topic, facts, {})
        self.assertEqual(payload["concrete_facts"][0]["epistemic_status"], "SOURCE_REPORTED")
        self.assertEqual(payload["source_dependent_anchors"][0]["kind"], "SOURCE_REPORTED_FACT")
        self.assertEqual(payload["primary_observation"]["fact_ids"], ["tweet:2092025531684532245"])

    def test_editorial_verified_facts_reads_referenced_post_from_source_database(self):
        with sqlite3.connect(self.app_module.DAILY_CONTEXT_SOURCE_DB) as conn:
            conn.execute(
                """CREATE TABLE source_posts(
                    post_id TEXT PRIMARY KEY,handle TEXT,text TEXT,created_at TEXT,url TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO source_posts VALUES(?,?,?,?,?)",
                (
                    "2092315006826398115", "source_account",
                    "原帖写明活动池规模为 5000 万美元。",
                    "2026-08-28T02:00:00Z",
                    "https://x.com/source_account/status/2092315006826398115",
                ),
            )
        facts = self.app_module.editorial_verified_facts(
            {}, {"source_refs": ["2092315006826398115"]}, {}
        )
        self.assertEqual(facts["facts"][0]["id"], "tweet:2092315006826398115")
        self.assertEqual(facts["facts"][0]["text"], "原帖写明活动池规模为 5000 万美元。")
        self.assertEqual(facts["facts"][0]["actor"], "source_account")
        self.assertTrue(facts["requires_fact_ids"])

    def test_gemini_parser_enforces_facts_used_ids_contract(self):
        calls = []
        payload = {
            "choices": [{"message": {"content": json.dumps({
                "sections": {
                    "hook": "公告已经改变了参与规则。",
                    "project_context": "这是一个用于团队开发流程的开源项目。",
                    "pain": "旧做法把时间耗在重复整理和交接上。",
                    "mechanism": "仓库把输入、处理与输出边界拆成可复用模块。",
                    "fit": "它更适合已经有明确工作流的小团队。",
                    "close": "真正有价值的是先重算原来的成本假设，再决定是否采用。",
                    "cta": "适合这类团队时，可以先看仓库说明再试。",
                },
                "facts_used_ids": ["fact:2092315006826398115"], "stance": "重算成本假设",
            }, ensure_ascii=False)}}],
        }
        verified_facts = {
            "schema": "facts_used_ids", "requires_fact_ids": True,
            "facts": [{
                "id": "tweet:2092315006826398115", "text": "原帖明确写出的公告。",
                "source_refs": ["2092315006826398115"], "status": "source_reported",
            }],
        }
        with patch.dict(os.environ, {"XOPS_GEMINI_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(payload, calls)
        ):
            style_recipe = self.app_module.editorial_content_structure({
                "structure_id": "open_source_discovery"
            })
            result = asyncio.run(self._real_write_persona_editorial_gemini(
                {"slug": "hegong-afterwork"}, {
                    "title": "题目", "topic_domain": "ai", "style_recipe": style_recipe,
                }, verified_facts,
                {"text": "背景", "citations": []}, {"source_kind": "market", "source_id": "", "source_item": None, "first_person_allowed": False},
            ))
        self.assertEqual(result["facts_used_ids"], ["tweet:2092315006826398115"])
        self.assertEqual(result["stance"], "重算成本假设")
        self.assertEqual(result["style_id"], "open_source_discovery")
        self.assertEqual(list(result["sections"]), style_recipe["section_order"])
        self.assertEqual(result["text"].split("\n\n"), [
            result["sections"][key] for key in style_recipe["section_order"]
            if result["sections"][key]
        ])
        self.assertIn("facts_used_ids", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("status=source_reported", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("中文 AI KOL 编辑", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("Hook 后用一句话完成对象定位", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("开头必须选一个最强信号做 Hook", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("open_source_discovery", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("一句说清仓库替谁省掉什么工作", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("不强制所有帖子套同一套三拍结构", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("产品评论、人物评价和行业分析应收在一个鲜明判断上", calls[0]["kwargs"]["json"]["messages"][0]["content"])

    def test_gemini_api_keys_reads_configured_keychain_accounts_or_env_fallback(self):
        self.app_module._cached_gemini_api_keys.cache_clear()
        def security_run(command, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout={"slot-a": "dummy-gemini-a", "slot-b": "dummy-gemini-b"}[command[command.index("-a") + 1]],
            )

        with patch.dict(os.environ, {
            "XOPS_GEMINI_KEYCHAIN_ACCOUNTS": "slot-a, slot-b",
            "XOPS_GEMINI_API_KEY": "env-fallback-key",
        }, clear=True), patch.object(self.app_module.subprocess, "run", side_effect=security_run) as run:
            self.assertEqual(self.app_module.gemini_api_keys(), ["dummy-gemini-a", "dummy-gemini-b"])
            self.assertEqual(self.app_module.gemini_api_keys(), ["dummy-gemini-a", "dummy-gemini-b"])
        self.assertEqual(run.call_count, 2)

        with patch.dict(os.environ, {
            "XOPS_GEMINI_KEYCHAIN_ACCOUNTS": "",
            "XOPS_GEMINI_API_KEY": "env-fallback-key",
        }, clear=True), patch.object(self.app_module.subprocess, "run") as run:
            self.assertEqual(self.app_module.gemini_api_keys(), ["env-fallback-key"])
        run.assert_not_called()

        with patch.dict(os.environ, {
            "XOPS_GEMINI_API_KEY": "legacy-key",
            "XOPS_GEMINI_API_KEY_1": "pool-key-a",
            "XOPS_GEMINI_API_KEY_2": "pool-key-b",
        }, clear=True), patch.object(self.app_module.subprocess, "run") as run:
            self.assertEqual(self.app_module.gemini_api_keys(), ["pool-key-a", "pool-key-b"])
        run.assert_not_called()
        self.app_module._cached_gemini_api_keys.cache_clear()

    def test_gemini_request_key_serializes_each_key_but_uses_the_pool(self):
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        config = {"signature": "dummy-pool-signature", "keys": ["dummy-gemini-a", "dummy-gemini-b"]}
        active = {key: 0 for key in config["keys"]}
        peak = {key: 0 for key in config["keys"]}
        used = set()

        async def worker():
            async with self.app_module.gemini_request_key(config) as key:
                used.add(key)
                active[key] += 1
                peak[key] = max(peak[key], active[key])
                await asyncio.sleep(0.01)
                active[key] -= 1

        async def run_workers():
            await asyncio.gather(*(worker() for _ in range(6)))

        try:
            asyncio.run(run_workers())
        finally:
            self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.assertEqual(used, set(config["keys"]))
        self.assertEqual(peak, {key: 1 for key in config["keys"]})

    def test_angle_expansion_splits_mothers_into_parallel_gemini_batches(self):
        calls = []
        payload = {"choices": [{"message": {"content": json.dumps({
            "angles": [], "rejected_angles": [],
        }, ensure_ascii=False)}}]}
        mothers = [
            {"seed_key": f"seed-{index}", "title": f"母题 {index}", "topic_domain": "ai"}
            for index in range(11)
        ]
        grok = {
            "contexts": [
                {"seed_key": f"seed-{index}", "background": f"background-{index}"}
                for index in range(11)
            ]
        }
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.app_module._cached_gemini_api_keys.cache_clear()
        with patch.dict(os.environ, {
            "XOPS_GEMINI_KEYCHAIN_ACCOUNTS": "",
            "XOPS_GEMINI_API_KEY_1": "dummy-a",
            "XOPS_GEMINI_API_KEY_2": "dummy-b",
            "XOPS_GEMINI_API_KEY_3": "dummy-c",
        }, clear=True), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(payload, calls)
        ):
            result = asyncio.run(self.app_module.expand_editorial_angles_gemini(
                mothers, {}, grok, [],
            ))
        prompts = [call["kwargs"]["json"]["messages"][0]["content"] for call in calls]
        self.assertEqual(len(prompts), 6)
        self.assertEqual(result["angles"], [])
        for index in range(11):
            self.assertEqual(sum(f'"seed_key": "seed-{index}"' in prompt for prompt in prompts), 1)
            self.assertEqual(sum(
                f'"background": "background-{index}"' in prompt for prompt in prompts
            ), 1)
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.app_module._cached_gemini_api_keys.cache_clear()

    def test_angle_expansion_retries_only_a_malformed_gemini_batch(self):
        calls = []
        payload = {"choices": [{"message": {"content": "{}"}}]}
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.app_module._cached_gemini_api_keys.cache_clear()
        with patch.dict(os.environ, {
            "XOPS_GEMINI_KEYCHAIN_ACCOUNTS": "", "XOPS_GEMINI_API_KEY": "dummy",
        }, clear=True), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(payload, calls)
        ), patch.object(
            self.app_module, "chat_completion_json", side_effect=[
                json.JSONDecodeError("bad json", "", 0),
                {"angles": [], "rejected_angles": []},
            ],
        ):
            result = asyncio.run(self.app_module.expand_editorial_angles_gemini(
                [{"seed_key": "seed-1", "title": "母题", "topic_domain": "ai"}],
                {}, {"contexts": [{"seed_key": "seed-1", "background": "背景"}]}, [],
            ))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["kwargs"]["json"]["temperature"], 0.55)
        self.assertEqual(calls[1]["kwargs"]["json"]["temperature"], 0.2)
        self.assertEqual(result["angles"], [])
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.app_module._cached_gemini_api_keys.cache_clear()

    def test_angle_expansion_isolates_a_failed_batch(self):
        calls = []
        payload = {"choices": [{"message": {"content": "{}"}}]}
        mothers = [
            {"seed_key": f"seed-{index}", "title": f"母题 {index}", "topic_domain": "ai"}
            for index in range(3)
        ]
        grok = {"contexts": [
            {"seed_key": f"seed-{index}", "background": "背景"} for index in range(3)
        ]}
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.app_module._cached_gemini_api_keys.cache_clear()
        with patch.dict(os.environ, {
            "XOPS_GEMINI_KEYCHAIN_ACCOUNTS": "", "XOPS_GEMINI_API_KEY": "dummy",
        }, clear=True), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(payload, calls)
        ), patch.object(
            self.app_module, "chat_completion_json", side_effect=[
                json.JSONDecodeError("bad json", "", 0),
                json.JSONDecodeError("bad json", "", 0),
                {"angles": [], "rejected_angles": []},
            ],
        ):
            result = asyncio.run(self.app_module.expand_editorial_angles_gemini(
                mothers, {}, grok, [],
            ))
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [item["parent_seed_key"] for item in result["rejected_angles"]],
            ["seed-0", "seed-1"],
        )
        self.assertTrue(all(
            item["reason_code"] == "context_unavailable"
            for item in result["rejected_angles"]
        ))
        self.app_module.EDITORIAL_GEMINI_KEY_POOLS.clear()
        self.app_module._cached_gemini_api_keys.cache_clear()

    def test_gemini_provider_signature_does_not_expose_key(self):
        dummy_key = "dummy-gemini-secret"
        with patch.dict(os.environ, {
            "XOPS_GEMINI_KEYCHAIN_ACCOUNTS": "",
            "XOPS_GEMINI_API_KEY": dummy_key,
        }, clear=True):
            config = self.app_module.editorial_provider_config("GEMINI")
        self.assertNotEqual(config["signature"], dummy_key)
        self.assertNotIn(dummy_key, config["signature"])

    def test_gemini_healthcheck_uses_healthy_fallback_before_batch(self):
        calls = []
        self.app_module.EDITORIAL_PROVIDER_HEALTH.clear()
        self.app_module.EDITORIAL_PROVIDER_MODEL_OVERRIDES.clear()
        with patch.dict(os.environ, {
            "XOPS_GEMINI_API_KEY": "test-key",
            "XOPS_GEMINI_MODEL": "gemini-unavailable",
            "XOPS_GEMINI_FALLBACK_MODEL": "gemini-healthy",
        }), patch.object(
            self.app_module.httpx, "AsyncClient",
            return_value=FakeHealthClient([503, 200], calls),
        ):
            config = asyncio.run(self.app_module.ensure_editorial_provider_ready("GEMINI"))
        self.assertEqual(config["model"], "gemini-healthy")
        self.assertEqual(
            [call["kwargs"]["json"]["model"] for call in calls],
            ["gemini-unavailable", "gemini-healthy"],
        )

    def test_github_traction_is_verified_from_matching_official_repo(self):
        topic = {
            "title": "ai-memory 怎么让 Codex 接住 Claude Code 的上下文",
            "topic_domain": "ai",
        }
        with patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeGitHubClient()
        ):
            facts = asyncio.run(self.app_module.enrich_verified_facts_with_github_traction(
                topic,
                {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False},
                {"citations": ["https://github.com/akitaonrails/ai-memory"]},
            ))
        self.assertTrue(facts["requires_fact_ids"])
        self.assertEqual(len(facts["facts"]), 1)
        self.assertIn("4,687 个 Star", facts["facts"][0]["text"])
        self.assertEqual(
            facts["facts"][0]["source_refs"],
            ["https://github.com/akitaonrails/ai-memory"],
        )

    def test_github_traction_ignores_repo_not_named_in_topic(self):
        facts = asyncio.run(self.app_module.enrich_verified_facts_with_github_traction(
            {"title": "另一个项目", "topic_domain": "ai"},
            {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False},
            {"citations": ["https://github.com/akitaonrails/ai-memory"]},
        ))
        self.assertEqual(facts["facts"], [])

    def test_critic_parser_requires_unsupported_claims_string_array(self):
        calls = []
        payload = {
            "choices": [{"message": {"content": json.dumps({
                "verdict": "PASS", "reasons": [], "rewrite_instruction": "",
            }, ensure_ascii=False)}}],
        }
        with patch.dict(os.environ, {"XOPS_GEMINI_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(payload, calls)
        ):
            with self.assertRaisesRegex(RuntimeError, "unsupported_claims"):
                asyncio.run(self._real_critique_persona_editorial_draft(
                    {"slug": "hegong-afterwork"}, {
                        "title": "题目", "topic_domain": "ai",
                        "style_recipe": self.app_module.editorial_content_structure({
                            "structure_id": "project_product_evaluation"
                        }),
                    },
                    {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False},
                    {"text": "背景", "citations": []},
                    {"source_kind": "market", "source_id": "", "source_item": None,
                     "first_person_allowed": False, "available_assets": []},
                    {"text": "这不是一条会被实际发布的测试正文，但它足够长，用于验证主编输出的事实审查字段是否严格存在。"}, [],
                ))
        self.assertIn("中文 AI 内容主编", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("最迟前三句", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("没有在开头使用最强信号做 Hook", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("project_product_evaluation", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("关键取舍", calls[0]["kwargs"]["json"]["messages"][0]["content"])
        self.assertIn("不能退回统一说明文模板", calls[0]["kwargs"]["json"]["messages"][0]["content"])

    def test_editorial_critic_is_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(self.app_module.editorial_always_critique())

    def test_ai_preview_concurrency_is_bounded_and_has_safe_default(self):
        import scripts.run_ai_persona_preview_batch as preview

        with patch.dict(os.environ, {
            "XOPS_EDITORIAL_RESEARCH_CONCURRENCY": "9",
            "XOPS_EDITORIAL_GENERATION_CONCURRENCY": "bad",
        }):
            self.assertEqual(
                preview.bounded_concurrency("XOPS_EDITORIAL_RESEARCH_CONCURRENCY", 4), 5
            )
            self.assertEqual(
                preview.bounded_concurrency("XOPS_EDITORIAL_GENERATION_CONCURRENCY", 5), 5
            )
        with patch.dict(os.environ, {"XOPS_EDITORIAL_RESEARCH_CONCURRENCY": "0"}):
            self.assertEqual(
                preview.bounded_concurrency("XOPS_EDITORIAL_RESEARCH_CONCURRENCY", 4), 1
            )

    def test_ai_preview_local_pass_checkpoint_must_be_recriticized(self):
        import scripts.run_ai_persona_preview_batch as preview

        item = preview.TOPICS[0]
        topic = preview.topic_for(item)
        style = topic["style_recipe"]
        output_dir = Path(self.temp.name) / "preview"
        output_dir.mkdir()
        draft = {
            "text": "这是一条只用于验证旧本地 PASS 不会被新流程直接复用的完整测试正文。" * 8,
            "facts_used_ids": ["official:hegong-afterwork:1"],
            "stance": "测试判断",
            "style_id": style["id"],
            "model": "gemini-old",
        }
        grok = {
            "text": "测试背景", "citations": [item["source_url"]],
            "tool_usage": ["x_search", "web_search"], "model": "grok-test",
        }
        facts = preview.verified_facts(item)
        local_critic = {"verdict": "PASS", "mode": "local_first_pass", "model": ""}
        checkpoint = {
            "slug": item["slug"], "title": item["title"],
            "pipeline_revision": preview.PIPELINE_REVISION,
            "style_revision": style["revision"], "style_id": style["id"],
            "content_revision": item.get("content_revision", 1),
            "grok": grok, "verified_facts": facts,
            "draft": draft, "failures": [], "critic": local_critic,
            "result": {**item, "draft": draft, "critic": local_critic},
        }
        preview.write_json(output_dir / f"{item['slug']}.json", checkpoint)
        critic = AsyncMock(return_value={
            "verdict": "PASS", "reasons": [], "unsupported_claims": [],
            "rewrite_instruction": "", "mode": "llm_critic", "model": "gemini-test",
        })
        writer = AsyncMock()
        with patch.object(preview.app, "write_persona_editorial_gemini", writer), patch.object(
            preview.app, "critique_persona_editorial_draft", critic
        ):
            result = asyncio.run(preview.generate_one(
                item, asyncio.Semaphore(1), asyncio.Semaphore(1), output_dir
            ))
        writer.assert_not_awaited()
        critic.assert_awaited_once()
        self.assertEqual(result["critic"]["mode"], "llm_critic")

    def test_manual_fact_promotion_requires_eligible_ref_and_selected_topic_reference(self):
        raw_cards = {"fact_cards": [
            {
                "status": "two_source_candidate", "representative_source_ref": "post-42",
                "representative_text": "两个独立来源都在讨论的具体变化。",
                "representative_url": "https://x.com/example/status/42",
                "evidence": [{"source_ref": "post-43", "url": "https://x.com/example/status/43"}],
            },
            {
                "status": "official_primary", "representative_source_ref": "official-1",
                "representative_text": "未来由正式来源验证的事实。",
            },
        ]}
        reviewed = self.app_module.reviewed_fact_cards(raw_cards, ["post-42"], 123, [{
            "source_ref": "post-42", "verification_url": "https://project.example/announcement",
            "verification_note": "项目官网公告确认该变化。",
        }])
        promoted = reviewed["fact_cards"][0]
        self.assertEqual(promoted["status"], "verified")
        self.assertEqual(
            {key: promoted[key] for key in ("original_status", "verified_by", "verified_at")},
            {"original_status": "two_source_candidate", "verified_by": "daily_context_reviewer", "verified_at": 123},
        )
        facts = self.app_module.editorial_verified_facts(reviewed, {"source_refs": ["post-42"]}, {})
        self.assertEqual(facts["facts"][0]["id"], "fact:post-42")
        self.assertEqual(
            self.app_module.editorial_verified_facts(reviewed, {"source_refs": ["post-43"]}, {})["facts"], []
        )
        with self.assertRaises(self.app_module.HTTPException) as caught:
            self.app_module.reviewed_fact_cards(raw_cards, ["post-43"], 123, [{
                "source_ref": "post-43", "verification_url": "https://project.example/announcement",
                "verification_note": "项目官网公告确认该变化。",
            }])
        self.assertEqual(caught.exception.status_code, 422)
        with self.assertRaises(self.app_module.HTTPException) as caught:
            self.app_module.reviewed_fact_cards(raw_cards, ["official-1"], 123, [{
                "source_ref": "official-1", "verification_url": "https://project.example/announcement",
                "verification_note": "项目官网公告确认该变化。",
            }])
        self.assertEqual(caught.exception.status_code, 422)
        for verification_url in ("https://x.com/example/status/42", "https://mobile.twitter.com/example/status/42"):
            with self.assertRaises(self.app_module.HTTPException) as caught:
                self.app_module.reviewed_fact_cards(raw_cards, ["post-42"], 123, [{
                    "source_ref": "post-42", "verification_url": verification_url,
                    "verification_note": "项目官网公告确认该变化。",
                }])
            self.assertEqual(caught.exception.status_code, 422)

    def test_grok_requires_x_web_tool_evidence_and_citation(self):
        calls = []
        payload = {
            "output": [
                {"type": "x_search_call"}, {"type": "web_search_call"},
                {"type": "message", "content": [{
                    "type": "output_text", "text": "热点的争议集中在规则变化。",
                    "annotations": [{"url": "https://example.com/official"}],
                }]},
            ],
        }
        self.app_module.EDITORIAL_GROK_CONTEXT_CACHE.clear()
        with patch.dict(os.environ, {"XOPS_GROK_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(payload, calls)
        ):
            result = asyncio.run(self._real_enrich_persona_editorial_context(
                {"title": "热点", "scope": "public", "topic_domain": "ai"}, {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False},
                {"context_date": "2026-08-24"},
            ))
        self.assertEqual(result["tool_usage"], ["web_search", "x_search"])
        self.assertEqual(result["citations"], ["https://example.com/official"])
        self.assertEqual(
            [tool["type"] for tool in calls[0]["kwargs"]["json"]["tools"]], ["x_search", "web_search"]
        )
        self.assertEqual(calls[0]["kwargs"]["json"]["max_output_tokens"], 1000)
        self.assertIn("中文 AI 编辑", calls[0]["kwargs"]["json"]["input"])

        self.app_module.EDITORIAL_GROK_CONTEXT_CACHE.clear()
        missing_citation = {"output": [{"type": "x_search_call"}, {"type": "web_search_call"}, {
            "type": "message", "content": [{"type": "output_text", "text": "没有引用。"}],
        }]}
        with patch.dict(os.environ, {"XOPS_GROK_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeAsyncClient(missing_citation, [])
        ):
            with self.assertRaisesRegex(RuntimeError, "X/Web 搜索或引用证据"):
                asyncio.run(self._real_enrich_persona_editorial_context(
                    {"title": "热点", "scope": "public"},
                    {"schema": "facts_used_ids", "facts": [], "requires_fact_ids": False},
                    {"context_date": "2026-08-24"},
                ))

    def test_grok_angle_research_must_cover_every_mother_seed(self):
        mothers = [
            {"seed_key": "seed-a", "subject": "A", "title": "A"},
            {"seed_key": "seed-b", "subject": "B", "title": "B"},
        ]

        def payload(contexts):
            return {
                "output": [
                    {"type": "x_search_call"}, {"type": "web_search_call"},
                    {"type": "message", "content": [{
                        "type": "output_text",
                        "text": json.dumps({"contexts": contexts}, ensure_ascii=False),
                        "annotations": [{"url": "https://example.com/official"}],
                    }]},
                ],
            }

        complete = [{
            "seed_key": key,
            "background": f"{key} 背景",
            "current_debate": "当前争议",
            "strongest_for": "最强支持理由",
            "strongest_against": "最强反对理由",
            "second_order_effect": "二阶影响",
            "stale_or_common": "已经说烂的常识",
        } for key in ("seed-a", "seed-b")]
        self.app_module.EDITORIAL_GROK_CONTEXT_CACHE.clear()
        with patch.dict(os.environ, {"XOPS_GROK_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient",
            return_value=FakeAsyncClient(payload(complete), []),
        ):
            result = asyncio.run(self.app_module.research_editorial_angle_context_grok_batch(
                mothers, {"context_date": "2026-08-24"},
            ))
        self.assertEqual({item["seed_key"] for item in result["contexts"]}, {"seed-a", "seed-b"})

        self.app_module.EDITORIAL_GROK_CONTEXT_CACHE.clear()
        with patch.dict(os.environ, {"XOPS_GROK_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient",
            return_value=FakeAsyncClient(payload(complete[:1]), []),
        ):
            with self.assertRaisesRegex(RuntimeError, "逐题覆盖"):
                asyncio.run(self.app_module.research_editorial_angle_context_grok_batch(
                    mothers, {"context_date": "2026-08-24"},
                ))

        incomplete = [dict(item) for item in complete]
        incomplete[1]["current_debate"] = ""
        self.app_module.EDITORIAL_GROK_CONTEXT_CACHE.clear()
        with patch.dict(os.environ, {"XOPS_GROK_API_KEY": "test-key"}), patch.object(
            self.app_module.httpx, "AsyncClient",
            return_value=FakeAsyncClient(payload(incomplete), []),
        ):
            with self.assertRaisesRegex(RuntimeError, "缺少可用"):
                asyncio.run(self.app_module.research_editorial_angle_context_grok_batch(
                    mothers, {"context_date": "2026-08-24"},
                ))

    def test_grok_angle_research_isolates_each_mother_topic(self):
        mothers = [
            {"seed_key": f"seed-{index}", "subject": str(index), "title": str(index)}
            for index in range(7)
        ]

        async def research_batch(batch, _daily):
            contexts = [{"seed_key": item["seed_key"]} for item in batch]
            text = json.dumps({"contexts": contexts}, ensure_ascii=False)
            return {
                "text": text,
                "contexts": contexts,
                "citations": [f"https://example.com/{batch[0]['seed_key']}"],
                "tool_usage": ["x_search", "web_search"],
                "model": "grok-test",
            }

        batches = AsyncMock(side_effect=research_batch)
        with patch.object(
            self.app_module, "research_editorial_angle_context_grok_batch", batches,
        ):
            result = asyncio.run(self.app_module.research_editorial_angle_context_grok(
                mothers, {"context_date": "2026-08-24"},
            ))
        self.assertEqual([len(call.args[0]) for call in batches.await_args_list], [1] * 7)
        self.assertEqual({item["seed_key"] for item in result["contexts"]}, {
            item["seed_key"] for item in mothers
        })
        self.assertEqual(result["batches"], 7)

    def test_grok_angle_research_retries_then_reports_only_failed_seed(self):
        mothers = [
            {"seed_key": f"seed-{index}", "subject": str(index), "title": str(index)}
            for index in range(3)
        ]
        calls = {}

        async def research_batch(batch, _daily):
            key = batch[0]["seed_key"]
            calls[key] = calls.get(key, 0) + 1
            if key == "seed-1":
                raise RuntimeError("provider timeout")
            contexts = [{"seed_key": key}]
            text = json.dumps({"contexts": contexts}, ensure_ascii=False)
            return {
                "text": text,
                "contexts": contexts,
                "citations": [f"https://example.com/{key}"],
                "tool_usage": ["x_search", "web_search"],
                "model": "grok-test",
            }

        with patch.object(
            self.app_module, "research_editorial_angle_context_grok_batch",
            AsyncMock(side_effect=research_batch),
        ):
            result = asyncio.run(self.app_module.research_editorial_angle_context_grok(
                mothers, {"context_date": "2026-08-24"},
            ))
        self.assertEqual(calls, {"seed-0": 1, "seed-1": 2, "seed-2": 1})
        self.assertEqual(result["failed_seed_keys"], ["seed-1"])
        self.assertEqual({item["seed_key"] for item in result["contexts"]}, {"seed-0", "seed-2"})

    def test_unverified_numeric_gate_allows_protocol_identifiers_only(self):
        identifiers = (
            "TermMax S1、x402、L2、ERC-20、EIP-1559、GP-0003、SIMD-0096 和 BEP-20 都是协议语境中的标识。"
            "这里讨论的是参与方式和产品结构，"
            "没有把价格、比例、日期或数量写成已经确认的市场事实，因此仍然可以进入主编审核。"
        )
        self.assertNotIn(
            "无已核事实时出现数字、日期或价格式断言",
            self.app_module.deterministic_editorial_style_failures(
                identifiers, {"first_person_allowed": False}, {"facts": []}
            ),
        )
        numeric_claim = identifiers + " 这个活动的 APY 是 12%。"
        self.assertIn(
            "无已核事实时出现数字、日期或价格式断言",
            self.app_module.deterministic_editorial_style_failures(
                numeric_claim, {"first_person_allowed": False}, {"facts": []}
            ),
        )

    def test_verified_github_traction_must_be_used_in_opening_hook(self):
        facts = {"facts": [{
            "id": "fact:github-ai-memory",
            "text": "截至 2026-08-26，GitHub 上的 ai-memory 仓库有 4,687 Stars。",
        }]}
        body = (
            "ai-memory 是给编码 Agent 做跨会话项目记忆的开源工具。"
            "它让 Claude Code 换到 Codex 后继续工作，不必重新解释架构和失败方案。"
            "这个项目目前在 GitHub 有 4,687 个 Star，说明已经得到不少开发者关注。"
        )
        self.assertIn(
            "已核 GitHub 热度信号未在开头用作 Hook",
            self.app_module.deterministic_editorial_style_failures(
                body, {"first_person_allowed": False}, facts
            ),
        )
        hooked = (
            "截至 8 月 26 日，ai-memory 在 GitHub 已经拿到 4,687 个 Star。"
            "它是给 Claude Code、Codex 这类编码 Agent 做跨会话项目记忆的开源工具。"
            "换个 Agent 以后，项目也不用从头解释。"
        )
        self.assertNotIn(
            "已核 GitHub 热度信号未在开头用作 Hook",
            self.app_module.deterministic_editorial_style_failures(
                hooked, {"first_person_allowed": False}, facts
            ),
        )

    def test_published_legacy_candidate_is_never_superseded_or_replaced(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "published-legacy", "title": "旧稿", "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            persona_id = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()[0]
            legacy_id = conn.execute(
                """INSERT INTO post_candidates(persona_id,context_date,title,body,status,source,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (persona_id, context_date, "已发布旧稿", "旧稿。", "published", f"persona_editorial:{evaluation_id}", "{}", 1, 1),
            ).lastrowid
            conn.execute("UPDATE persona_editorial_evaluations SET candidate_id=? WHERE id=?", (legacy_id, evaluation_id))
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            row = conn.execute("SELECT status FROM post_candidates WHERE id=?", (legacy_id,)).fetchone()
            candidates = conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0]
        self.assertEqual(row["status"], "published")
        self.assertEqual(candidates, 1)

    def test_needs_review_legacy_is_kept_until_formal_pass_then_swapped(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "swap-legacy", "title": "旧稿替换", "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            persona_id = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()[0]
            legacy_id = conn.execute(
                """INSERT INTO post_candidates(persona_id,context_date,title,body,status,source,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (persona_id, context_date, "待审旧稿", "旧稿。", "needs_review", f"persona_editorial:{evaluation_id}", "{}", 1, 1),
            ).lastrowid
            conn.execute("UPDATE persona_editorial_evaluations SET candidate_id=? WHERE id=?", (legacy_id, evaluation_id))
        grok = AsyncMock(return_value={"text": "背景。", "citations": [], "tool_usage": ["x_search", "web_search"], "model": "grok-test"})
        writer = AsyncMock(return_value={
            "text": "规则发生变化后，过去那套直觉已经不够用。先把收益、成本和时间窗口放进同一个比较框架，才知道这件事对参与者有没有真实意义。真正的分水岭，是能不能在消息最热的时候仍然按同一套比较方法做选择，而不是拿情绪替代判断。",
            "facts_used_ids": [], "stance": "先重算比较框架", "model": "gemini-test",
        })
        critic = AsyncMock(return_value={"verdict": "PASS", "reasons": [], "rewrite_instruction": ""})
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}), patch.object(
            self.app_module, "enrich_persona_editorial_context", grok
        ), patch.object(self.app_module, "write_persona_editorial_gemini", writer), patch.object(
            self.app_module, "critique_persona_editorial_draft", critic
        ):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            legacy = conn.execute("SELECT status FROM post_candidates WHERE id=?", (legacy_id,)).fetchone()
            replacement = conn.execute(
                "SELECT id,source FROM post_candidates WHERE source=?", (self.app_module.persona_editorial_candidate_source(evaluation_id),)
            ).fetchone()
        self.assertEqual(legacy["status"], "superseded")
        self.assertIsNotNone(replacement)

    def test_transient_provider_failure_stays_write_and_retries(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "retryable-provider", "title": "可重试", "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])

        async def evaluator(_persona, _context, _daily, topics, _history, _today_count):
            return self.editorial_decision(topics[0], "WRITE", claim_key="retryable-claim", core_claim="等待同一条正式判断重试")

        transient = AsyncMock(side_effect=RuntimeError("temporary provider outage"))
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "enrich_persona_editorial_context", transient):
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            pending = conn.execute(
                "SELECT id,status,reason_code,candidate_id,generation_attempts,next_retry_at,generation_max_attempts "
                "FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)
            ).fetchone()
        self.assertEqual(
            (pending["status"], pending["reason_code"], pending["candidate_id"]),
            ("WRITE", "formal_generation_retryable", None),
        )
        self.assertEqual((pending["generation_attempts"], pending["generation_max_attempts"]), (1, 3))
        self.assertGreater(pending["next_retry_at"], int(time.time()))

        writer = AsyncMock(return_value={
            "text": "这次不该急着用情绪替代判断，先把公开信息放回原来的比较框架，才能看出变化究竟改变了什么。面对短期讨论最容易犯的错，是只盯着热度却忘了比较成本和时间窗口，最后把噪音当成方向。",
            "facts_used_ids": [], "stance": "等待可验证信息", "model": "gemini-test",
        })
        with patch.dict(os.environ, {"XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng"}), patch.object(
            self.app_module, "enrich_persona_editorial_context", AsyncMock(return_value={
                "text": "恢复后的背景。", "citations": [], "tool_usage": ["x_search", "web_search"], "model": "grok-test",
            })
        ), patch.object(self.app_module, "write_persona_editorial_gemini", writer), patch.object(
            self.app_module, "critique_persona_editorial_draft", AsyncMock(return_value={"verdict": "PASS", "reasons": [], "rewrite_instruction": ""})
        ):
            self.run_editorial_pipeline(run_id)
            writer.assert_not_awaited()
            reset = self.client.post(f"/api/persona-editorial-evaluations/{pending['id']}/retry")
            self.assertEqual(reset.status_code, 200)
            self.run_editorial_pipeline(run_id)
        with self.app_module.db() as conn:
            completed = conn.execute("SELECT status,candidate_id FROM persona_editorial_evaluations WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(completed["status"], "WRITE")
        self.assertIsNotNone(completed["candidate_id"])

    def test_retry_backoff_exhaustion_is_persisted_and_manual_reset_reopens_it(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "retry-limit", "title": "重试上限", "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE persona_editorial_evaluations SET generation_max_attempts=2 WHERE id=?", (evaluation_id,)
            )
            self.app_module.mark_persona_editorial_generation_retryable(
                conn, evaluation_id, RuntimeError("first temporary failure")
            )
            first = conn.execute(
                "SELECT status,generation_attempts,next_retry_at FROM persona_editorial_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
            self.app_module.mark_persona_editorial_generation_retryable(
                conn, evaluation_id, RuntimeError("second temporary failure")
            )
            exhausted = conn.execute(
                "SELECT status,reason_code,generation_attempts,next_retry_at,generation_max_attempts "
                "FROM persona_editorial_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
        self.assertEqual((first["status"], first["generation_attempts"]), ("WRITE", 1))
        self.assertIsNotNone(first["next_retry_at"])
        self.assertEqual(tuple(exhausted), ("HOLD", "formal_generation_retry_exhausted", 2, None, 2))

        reset = self.client.post(f"/api/persona-editorial-evaluations/{evaluation_id}/retry")
        self.assertEqual(reset.json(), {
            "id": evaluation_id, "status": "WRITE", "generation_attempts": 0, "next_retry_at": None,
        })
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT status,reason_code,generation_attempts,next_retry_at FROM persona_editorial_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
        self.assertEqual(tuple(row), ("WRITE", "formal_generation_manual_retry", 0, None))

    def test_required_public_angle_never_loses_its_thesis_on_generation_failure(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "required-retry", "title": "已批准公共观点",
            "core_claim": "正文失败只能重写，不能撤销观点。",
            "parent_seed_key": "mother:required", "scope": "public",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET reason_code='required_public_angle',generation_max_attempts=1 WHERE id=?""",
                (evaluation_id,),
            )
            self.app_module.mark_persona_editorial_generation_retryable(
                conn, evaluation_id, RuntimeError("writer failed")
            )
            row = conn.execute(
                """SELECT status,thesis_state,reason_code,generation_attempts,next_retry_at,generation_state
                   FROM persona_editorial_evaluations WHERE id=?""",
                (evaluation_id,),
            ).fetchone()

        self.assertEqual(tuple(row)[:4], (
            "WRITE", "THESIS_APPROVED", "required_public_angle", 1,
        ))
        self.assertIsNotNone(row["next_retry_at"])
        self.assertIn("writer failed", json.loads(row["generation_state"])["retry_instruction"])

    def test_pipeline_reopens_a_previously_rejected_required_public_angle(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "required-reopen", "title": "必须恢复的观点",
            "core_claim": "旧版正文被拒不能永久撤销 Thesis。",
            "parent_seed_key": "mother:reopen", "scope": "public", "topic_domain": "crypto",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        with self.app_module.db() as conn:
            cards = json.loads(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0])
            cards["editorial_angle_expansion"] = {
                "status": "ready", "expanded_topics": [topic], "rejected_angles": [],
            }
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',thesis_state='THESIS_HOLD',reason_code='grok_gemini_critic_reject'
                   WHERE id=?""",
                (evaluation_id,),
            )
        with patch.dict(os.environ, {"XOPS_DAILY_POST_PERSONAS": "acheng"}):
            self.app_module.reopen_required_public_angle_rejections(run_id)
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT status,thesis_state,reason_code,next_retry_at FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()

        self.assertEqual(tuple(row), (
            "WRITE", "THESIS_APPROVED", "required_public_angle", None,
        ))

    def test_source_fact_policy_upgrade_reopens_old_grounding_failure_once(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "source-fact-reopen", "title": "原帖事实策略升级",
            "core_claim": "旧事实策略导致的 Grounding 失败需要重新写。",
            "parent_seed_key": "mother:source-fact", "scope": "public",
            "topic_domain": "crypto",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        with self.app_module.db() as conn:
            cards = json.loads(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0])
            cards["editorial_angle_expansion"] = {
                "status": "ready", "expanded_topics": [topic], "rejected_angles": [],
            }
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',reason_code='UNSUPPORTED_FACT',generation_state=?
                   WHERE id=?""",
                (json.dumps({"source_fact_policy_version": 1}), evaluation_id),
            )
        self.app_module.reopen_required_public_angle_rejections(run_id)
        with self.app_module.db() as conn:
            reopened = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',reason_code='UNSUPPORTED_FACT',generation_state=?
                   WHERE id=?""",
                (json.dumps({
                    "source_fact_policy_version": self.app_module.EDITORIAL_SOURCE_FACT_POLICY_VERSION,
                }), evaluation_id),
            )
        self.assertEqual(tuple(reopened), ("WRITE", "required_public_angle"))
        self.app_module.reopen_required_public_angle_rejections(run_id)
        with self.app_module.db() as conn:
            current = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        self.assertEqual(tuple(current), ("HOLD", "UNSUPPORTED_FACT"))

    def test_pipeline_does_not_reopen_required_angle_from_an_old_revision(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "stale-required-reopen", "title": "旧审批周期观点",
            "core_claim": "旧审批周期的评估不能在手动重跑后恢复。",
            "parent_seed_key": "mother:stale-reopen", "scope": "public",
            "topic_domain": "crypto",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        with self.app_module.db() as conn:
            cards = json.loads(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0])
            cards["editorial_angle_expansion"] = {
                "status": "ready", "expanded_topics": [topic], "rejected_angles": [],
            }
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',reason_code='manual_regeneration' WHERE id=?""",
                (evaluation_id,),
            )
            conn.execute(
                "UPDATE daily_context_runs SET approval_revision=approval_revision+1 WHERE id=?",
                (run_id,),
            )

        with patch.dict(os.environ, {"XOPS_DAILY_POST_PERSONAS": "acheng"}):
            self.app_module.reopen_required_public_angle_rejections(run_id)

        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("HOLD", "manual_regeneration"))

    def test_daily_draft_count_ignores_pending_write_from_an_old_revision(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        with self.app_module.db() as conn:
            persona_id = conn.execute(
                "SELECT id FROM personas WHERE slug='acheng'"
            ).fetchone()[0]
        self.insert_pending_editorial_write(
            run_id, context_date,
            {"claim_key": "stale-count", "title": "旧稿", "eligible": True},
        )
        with self.app_module.db() as conn:
            conn.execute(
                "UPDATE daily_context_runs SET approval_revision=approval_revision+1 WHERE id=?",
                (run_id,),
            )
            self.assertEqual(
                self.app_module.daily_persona_draft_count(conn, persona_id, context_date), 0,
            )

    def test_manual_retry_allows_unpublished_legacy_candidate(self):
        context_date = self.app_module.shanghai_today()
        topic = {"claim_key": "legacy-retry", "title": "旧稿复位", "eligible": True}
        run_id = self.create_editorial_run(context_date, topics=[topic])
        evaluation_id = self.insert_pending_editorial_write(run_id, context_date, topic)
        with self.app_module.db() as conn:
            persona_id = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()[0]
            legacy_id = conn.execute(
                """INSERT INTO post_candidates(persona_id,context_date,title,body,status,source,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (persona_id, context_date, "旧待审稿", "未发布旧稿。", "needs_review", f"persona_editorial:{evaluation_id}", "{}", 1, 1),
            ).lastrowid
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET candidate_id=?,status='HOLD',reason_code='formal_generation_retry_exhausted',
                       generation_attempts=3,next_retry_at=NULL WHERE id=?""",
                (legacy_id, evaluation_id),
            )
        response = self.client.post(f"/api/persona-editorial-evaluations/{evaluation_id}/retry")
        self.assertEqual(response.status_code, 200)
        with self.app_module.db() as conn:
            evaluation = conn.execute(
                "SELECT status,generation_attempts,next_retry_at,candidate_id FROM persona_editorial_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
            legacy = conn.execute("SELECT status FROM post_candidates WHERE id=?", (legacy_id,)).fetchone()
        self.assertEqual(tuple(evaluation), ("WRITE", 0, None, legacy_id))
        self.assertEqual(legacy["status"], "needs_review")

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

    def test_high_fit_public_hold_is_promoted_without_forcing_low_fit_topics(self):
        public = {
            "claim_key": "public-angle", "core_claim": "这个公共角度已经通过质量门。",
            "scope": "public",
        }
        private = {
            "claim_key": "private-angle", "core_claim": "私人角度仍尊重编辑判断。",
            "scope": "persona",
        }
        result = self.app_module.validate_persona_editorial_decisions({"decisions": [
            {
                "topic_claim_key": "public-angle", "status": "HOLD",
                "notice": 4, "authority": 4, "tension": 3, "marginal_value": 3,
                "why_me": "该人设有明确的观察位置。",
                "reason_code": "editorial_hold",
            },
            {
                "topic_claim_key": "private-angle", "status": "HOLD",
                "notice": 5, "authority": 5, "tension": 5, "marginal_value": 5,
                "why_me": "私人题不自动升格。",
            },
        ]}, [public, private])

        self.assertEqual(result["public-angle"]["status"], "HOLD")
        self.assertEqual(result["public-angle"]["reason_code"], "thesis_required_before_write")
        self.assertEqual(result["private-angle"]["status"], "HOLD")

    def test_every_approved_public_angle_is_forced_into_one_persona_thesis(self):
        topics = [
            {
                "claim_key": "public-angle-one", "title": "第一个观点",
                "core_claim": "第一个公共观点已经通过角度质量门。",
                "specific_tension": "它与市场常见解释存在明确冲突。",
                "scope": "public", "topic_domain": "crypto", "parent_seed_key": "mother-one",
            },
            {
                "claim_key": "public-angle-two", "title": "第二个观点",
                "core_claim": "第二个公共观点同样必须形成最终 Thesis。",
                "specific_tension": "它要求另一种清晰判断。",
                "scope": "public", "topic_domain": "crypto", "parent_seed_key": "mother-two",
            },
        ]
        held = {"decisions": [
            {
                "topic_claim_key": topic["claim_key"], "status": "HOLD",
                "notice": 0, "authority": 0, "tension": 0, "marginal_value": 0,
                "why_me": "", "reason_code": "unsupported",
            }
            for topic in topics
        ]}
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_PERSONAS": "acheng,ridehail-driver-zhao",
        }):
            first = self.app_module.validate_persona_editorial_decisions(
                held, topics, "acheng"
            )
            second = self.app_module.validate_persona_editorial_decisions(
                held, topics, "ridehail-driver-zhao"
            )

        self.assertEqual(first["public-angle-one"]["status"], "WRITE")
        self.assertEqual(first["public-angle-one"]["reason_code"], "required_public_angle")
        self.assertEqual(second["public-angle-two"]["status"], "WRITE")
        self.assertEqual(second["public-angle-two"]["reason_code"], "required_public_angle")
        self.assertEqual(
            self.app_module.thesis_contract_errors(
                topics[0], "acheng", first["public-angle-one"]["thesis"]
            ),
            [],
        )

    def test_ready_angle_stage_cannot_be_hidden_by_fallback_posts(self):
        context_date = "2026-08-28"
        topic = {
            "claim_key": "must-reach-thesis", "title": "必须进入 Thesis 的观点",
            "core_claim": "这个已批准观点不能被日常补位稿掩盖。",
            "scope": "public", "topic_domain": "crypto",
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        with self.app_module.db() as conn:
            cards = json.loads(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0])
            cards["editorial_angle_expansion"] = {
                "status": "ready", "expanded_topics": [topic], "rejected_angles": [],
            }
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
            self.assertEqual(
                self.app_module.uncovered_public_angle_keys(conn, run_id),
                ["must-reach-thesis"],
            )
            self.assertFalse(self.app_module.daily_post_output_ready(conn, context_date))

    def test_grounded_candidate_is_visible_while_other_angles_remain_uncovered(self):
        context_date = self.app_module.shanghai_today()
        ready_topic = {
            "claim_key": "ready-grounded", "title": "已完成观点",
            "core_claim": "已完成观点可以先进入审核。", "scope": "public",
        }
        pending_topic = {
            "claim_key": "pending-grounding", "title": "仍在校验",
            "core_claim": "未完成观点不能遮住合格稿。", "scope": "public",
        }
        run_id = self.create_editorial_run(context_date, topics=[ready_topic, pending_topic])
        with self.app_module.db() as conn:
            cards = json.loads(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0])
            cards["editorial_angle_expansion"] = {
                "status": "ready", "expanded_topics": [ready_topic, pending_topic],
                "rejected_angles": [],
            }
            conn.execute(
                "UPDATE daily_context_runs SET raw_cards=? WHERE id=?",
                (json.dumps(cards, ensure_ascii=False), run_id),
            )
        self.insert_formal_queue_candidate(
            run_id, context_date, ready_topic, slug="acheng",
            title="已完成观点", body="这是一条已经通过完整校验的推文正文。",
        )
        posts = self.client.get("/api/daily-posts").json()
        self.assertEqual([post["title"] for post in posts], ["已完成观点"])

    def test_high_fit_hold_promotion_still_requires_real_authority(self):
        topic = {
            "claim_key": "macro-angle", "core_claim": "宏观判断需要匹配的人设。", "scope": "public",
        }
        result = self.app_module.validate_persona_editorial_decisions({"decisions": [{
            "topic_claim_key": "macro-angle", "status": "HOLD",
            "notice": 5, "authority": 2, "tension": 5, "marginal_value": 5,
            "why_me": "只能泛泛评论。",
        }]}, [topic])

        self.assertEqual(result["macro-angle"]["status"], "HOLD")

    def test_high_fit_hold_promotion_preserves_hard_rejection(self):
        topic = {
            "claim_key": "conflicted-angle", "core_claim": "这条存在事实冲突。", "scope": "public",
        }
        result = self.app_module.validate_persona_editorial_decisions({"decisions": [{
            "topic_claim_key": "conflicted-angle", "status": "HOLD",
            "notice": 5, "authority": 5, "tension": 5, "marginal_value": 5,
            "why_me": "人设本身匹配。", "reason_code": "fact_conflict",
            "rationale": "已核材料存在冲突。",
        }]}, [topic])

        self.assertEqual(result["conflicted-angle"]["status"], "HOLD")
        self.assertEqual(result["conflicted-angle"]["reason_code"], "fact_conflict")

    def test_high_fit_hold_without_explicit_reason_is_not_promoted(self):
        topic = {
            "claim_key": "implicit-rejection", "core_claim": "缺少拒绝原因时保持 HOLD。", "scope": "public",
        }
        result = self.app_module.validate_persona_editorial_decisions({"decisions": [{
            "topic_claim_key": "implicit-rejection", "status": "HOLD",
            "notice": 5, "authority": 5, "tension": 5, "marginal_value": 5,
            "why_me": "人设匹配，但事实存在冲突。", "rationale": "已核材料存在冲突。",
        }]}, [topic])

        self.assertEqual(result["implicit-rejection"]["status"], "HOLD")

    def test_marginal_threshold_only_filters_low_value_after_five_posts(self):
        def decision(value):
            return {
                "status": "WRITE", "marginal_value": value,
                "notice": 3, "authority": 3, "tension": 3,
            }

        before_five = {"topic": decision(2)}
        at_five = {"weak": decision(2), "useful": decision(3)}

        self.app_module.apply_editorial_marginal_threshold(before_five, 4)
        self.app_module.apply_editorial_marginal_threshold(at_five, 5)

        self.assertEqual(before_five["topic"]["status"], "WRITE")
        self.assertEqual(at_five["weak"]["status"], "HOLD")
        self.assertEqual(at_five["weak"]["reason_code"], "insufficient_marginal_value")
        self.assertEqual(at_five["useful"]["status"], "WRITE")

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

    def test_only_published_legacy_persona_claims_participate_in_history(self):
        with self.app_module.db() as conn:
            persona_id = conn.execute(
                "SELECT id FROM personas WHERE slug='acheng'"
            ).fetchone()[0]
            rows = [
                ("legacy-published", "persona_editorial:published", "published"),
                ("legacy-needs-review", "persona_editorial:needs-review", "needs_review"),
                ("legacy-superseded", "persona_editorial:superseded", "superseded"),
            ]
            for index, (claim_key, source, status) in enumerate(rows, start=1):
                conn.execute(
                    """INSERT INTO post_candidates(
                        persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (persona_id, "2026-08-20", claim_key, "旧稿。", status, source, "{}", index, index),
                )
                conn.execute(
                    """INSERT INTO topic_claim_history(
                        claim_key,persona_id,subject,core_claim,context_date,source,status,created_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (claim_key, persona_id, "旧主张", claim_key, "2026-08-20", source, "drafted", index, index),
                )
            history = self.app_module.editorial_stable_claim_history(
                conn, "2026-08-24", persona_id
            )

        self.assertEqual(
            {item["claim_key"] for item in history if item["claim_key"].startswith("legacy-")},
            {"legacy-published"},
        )
        recent = self.app_module.recent_topic_claims()
        self.assertIn("legacy-published", {item["claim_key"] for item in recent})
        self.assertNotIn("legacy-needs-review", {item["claim_key"] for item in recent})
        self.assertNotIn("legacy-superseded", {item["claim_key"] for item in recent})
        with self.app_module.db() as conn:
            self.assertTrue(self.app_module.editorial_claim_already_drafted(
                conn, {"id": 999, "core_claim": "legacy-published"}
            ))
            self.assertFalse(self.app_module.editorial_claim_already_drafted(
                conn, {"id": 999, "core_claim": "legacy-needs-review"}
            ))
            statuses = dict(conn.execute(
                "SELECT source,status FROM post_candidates WHERE source LIKE 'persona_editorial:%'"
            ).fetchall())
        self.assertEqual(statuses["persona_editorial:published"], "published")
        self.assertEqual(statuses["persona_editorial:needs-review"], "needs_review")
        self.assertEqual(statuses["persona_editorial:superseded"], "superseded")

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
        self.assertEqual(statuses.count("WRITE"), 3)
        self.assertEqual(statuses.count("HOLD"), 0)

    def test_revalidation_keeps_completed_candidate_ready(self):
        context_date = self.app_module.shanghai_today()
        topic = self.mother_topic()
        run_id = self.create_editorial_run(context_date, topics=[topic])
        candidate_id, evaluation_id = self.insert_formal_queue_candidate(
            run_id, context_date, topic
        )
        with self.app_module.db() as conn:
            raw_cards = self.app_module.json_value(conn.execute(
                "SELECT raw_cards FROM daily_context_runs WHERE id=?", (run_id,)
            ).fetchone()[0], {})

        self.app_module.validate_run_persona_theses(run_id, raw_cards)
        self.app_module.resolve_persona_editorial_collisions(run_id)

        with self.app_module.db() as conn:
            evaluation = conn.execute(
                "SELECT status,thesis_state,candidate_id FROM persona_editorial_evaluations WHERE id=?",
                (evaluation_id,),
            ).fetchone()
            candidate = conn.execute(
                "SELECT status FROM post_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        self.assertEqual(dict(evaluation), {
            "status": "WRITE",
            "thesis_state": "CANDIDATE_READY",
            "candidate_id": candidate_id,
        })
        self.assertEqual(candidate["status"], "needs_review")

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
        self.assertEqual(generated.await_count, 2)
        with self.app_module.db() as conn:
            decisions = [tuple(row) for row in conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()]
            self.assertEqual(sum(status == "WRITE" for status, _reason in decisions), 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM post_candidates").fetchone()[0], 2)

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
            self.assertEqual(tuple(old), ("HOLD", "RECENT_PERSONA_THESIS_COLLISION"))
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
        grok_daily = self.app_module.enrich_persona_editorial_context.await_args.args[2]
        self.assertIn("新市场状态", grok_daily["market_state"])
        self.assertNotIn("旧市场状态", grok_daily["market_state"])
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
        first_topic = {
            "claim_key": "writer-failure", "subject": "失败隔离", "title": "失败隔离",
            "core_claim": "公共主张", "eligible": True,
        }
        second_topic = {
            "claim_key": "writer-later", "subject": "后续候选", "title": "后续候选",
            "core_claim": "另一条公共主张", "eligible": True,
        }
        run_id = self.create_editorial_run("2026-08-13", topics=[first_topic, second_topic])

        async def evaluator(persona, _context, _daily, topics, _history, _today_count):
            write_index = 0 if persona["slug"] == "acheng" else 1
            return {
                **self.editorial_decision(
                    topics[write_index], "WRITE", claim_key=f"writer-{persona['slug']}",
                    core_claim=f"{persona['slug']} 的独立判断"
                ),
                **self.editorial_decision(topics[1 - write_index], "IGNORE"),
            }

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
        self.assertIn("selected_topics 最多 15 条", prompt)
        self.assertEqual(calls[0]["kwargs"]["json"]["max_tokens"], 8000)
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

    def test_controlled_cards_drops_niche_exclusions_before_opinion_sources(self):
        cards = self.app_module.controlled_cards(
            [],
            [
                {"source_ref": f"opinion-{index}", "text": "有具体因果关系的观点" * 12}
                for index in range(10)
            ],
            {},
            niche_topics=[
                {"title": f"冷门 {index}", "key": f"niche-{index}", "unique_authors": 1, "post_count": 1}
                for index in range(100)
            ],
            limit=3000,
        )

        self.assertGreater(len(cards["opinion_cards"]), 0)
        self.assertLess(len(cards["excluded_niche_topics"]), 40)

    def test_controlled_cards_separates_discovery_topics_from_noise(self):
        cards = self.app_module.controlled_cards(
            [],
            [],
            {},
            niche_topics=[
                {
                    "title": "早期项目发现",
                    "key": "project:launch",
                    "unique_authors": 2,
                    "post_count": 2,
                    "engagement_total": 80,
                },
                {
                    "title": "单一低信号帖子",
                    "key": "noise:mention",
                    "unique_authors": 1,
                    "post_count": 1,
                    "engagement_total": 3,
                },
                {
                    "title": "同一题材连续出现",
                    "key": "project:repeat",
                    "unique_authors": 1,
                    "post_count": 2,
                    "engagement_total": 4,
                },
            ],
        )

        self.assertEqual(
            [item["key"] for item in cards["discovery_topics"]],
            ["project:launch", "project:repeat"],
        )
        self.assertEqual([item["key"] for item in cards["excluded_niche_topics"]], ["noise:mention"])

    def test_topic_selection_policy_and_history_are_persisted(self):
        policy = self.app_module.topic_selection_policy()
        self.assertIn("历史", "".join(policy["required_gates"]))
        self.assertIn("发现池", policy["principle"])
        self.assertIn("discovery", policy["selection_lanes"])
        self.assertEqual(policy["slate_guidance"]["dedupe_unit"], "去重单位是核心主张，不是事件、项目、币种或题材。")
        self.assertIn("不设", policy["content_inspiration"]["rule"])
        self.assertIn("开头两句内用一句话说明", "".join(policy["draft_quality_gates"]))
        self.assertIn("最强信号做 Hook", "".join(policy["draft_quality_gates"]))
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

    def test_screened_topic_can_use_discovery_topic_as_source(self):
        cards = {
            "discussion_topics": [],
            "discovery_topics": [{"key": "project:launch", "title": "早期项目发现"}],
            "claim_history": [],
        }
        selected, rejected = self.app_module.bounded_selected_topics(
            {
                "selected_topics": [{
                    "claim_key": "project-early-distribution",
                    "subject": "新项目",
                    "title": "这个项目先争开发者，而不是先争用户",
                    "core_claim": "它的首轮分发方式表明开发者采用比零售获客更优先。",
                    "content_type": "research",
                    "kind": "project_discovery",
                    "source_topic_keys": ["project:launch"],
                    "fact_basis": "两位作者讨论了同一产品发布。",
                    "opinion_basis": "先做开发者分发更容易形成工具生态。",
                    "material_delta": "发布机制首次公开。",
                    "audience_value": "帮助读者判断项目真正争夺的市场。",
                    "why_now": "产品刚发布。",
                    "persona_fit": ["atuo"],
                }],
                "rejected_topics": [],
            },
            cards,
        )

        self.assertEqual([item["claim_key"] for item in selected], ["project-early-distribution"])
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
        validation_run_ids = []
        run_date = self.app_module.shanghai_today()

        def collect(_accounts, _db, output, **kwargs):
            calls.append(("collect", kwargs["key"], kwargs["resume_hours"]))
            Path(output).mkdir(parents=True, exist_ok=True)
            return {"run_id": "run-current", "account_universe": 2, "accounts_fetched": 2, "posts_seen": 4}

        def cross_validate(_db, output, **_kwargs):
            validation_run_ids.append(_kwargs["run_id"])
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
            self.assertEqual(calls, [("collect", "runtime-key", 0)])
            self.assertEqual(validation_run_ids, ["run-current"])

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

    def test_dual_pool_daily_run_keeps_domains_separate_and_routes_ai_topics(self):
        run_date = self.app_module.shanghai_today()
        run, _ = self.app_module.create_daily_context_run(run_date, "manual")
        calls = []

        def collect(_accounts, _db, output, **kwargs):
            domain = kwargs["topic_domain"]
            calls.append((domain, Path(output)))
            snapshot = Path(output) / "runs" / f"{domain}-run"
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "fact_cards.json").write_text(json.dumps({"cards": [{
                "status": "two_source_candidate", "representative_text": f"{domain} 事实候选",
                "representative_source_ref": f"{domain}-fact", "source_lists": [domain],
            }]}), encoding="utf-8")
            (snapshot / "opinion_cards.json").write_text(json.dumps({"opinions": [{
                "source_ref": f"{domain}-opinion", "text": f"{domain} 观点候选", "source_lists": [domain],
            }]}), encoding="utf-8")
            (snapshot / "attention_topics.json").write_text(json.dumps({"topics": [], "niche": []}), encoding="utf-8")
            (snapshot / "discussion_topics.json").write_text(json.dumps({"hot": []}), encoding="utf-8")
            return {
                "run_id": f"{domain}-run", "snapshot_dir": str(snapshot),
                "account_universe": 1, "accounts_fetched": 1, "accounts_skipped": 0,
                "accounts_failed": 0, "posts_seen": 2, "topic_domain": domain,
            }

        def validate(_db, _output, **kwargs):
            return {"run_id": kwargs["run_id"], "source_posts": 2, "fact_cards": 1, "opinion_cards": 1}

        async def synthesize(_date, cards):
            domain = cards["topic_domain"]
            return {
                "market_state": f"{domain} 市场语境", "event_clusters": "", "debates": "", "evidence": "", "unknowns": "", "sources": [],
                "selected_topics": [{
                    "claim_key": f"{domain}-public-topic", "topic_domain": domain,
                    "subject": f"{domain} 主题", "title": f"{domain} 的可写主题",
                    "core_claim": f"{domain} 有一个独立的可写判断。", "content_type": "editorial",
                    "source_topic_keys": [f"opinion:{domain}-opinion"], "source_refs": [f"{domain}-opinion"],
                }],
                "rejected_topics": [],
            }

        sources = SimpleNamespace(collect=collect, cross_validate=validate, cross_validate_ai=validate)
        with patch.dict(os.environ, {"XOPS_AI_SOURCE_ENABLED": "true"}), patch.object(
            self.app_module, "market_sources_module", return_value=sources
        ), patch.object(self.app_module, "twitter241_api_key", return_value="runtime-key"), patch.object(
            self.app_module, "synthesize_daily_cards", AsyncMock(side_effect=synthesize)
        ):
            asyncio.run(self.app_module.execute_daily_context_run(run["id"]))

        self.assertEqual([domain for domain, _ in calls], ["crypto", "ai"])
        self.assertNotEqual(calls[0][1], calls[1][1])
        completed = self.app_module.get_daily_context_run(run["id"])
        self.assertEqual(completed["status"], "needs_review")
        self.assertEqual(set(completed["raw_cards"]["domains"]), {"crypto", "ai"})
        self.assertEqual(
            {item["topic_domain"] for item in completed["raw_cards"]["selected_topics"]}, {"crypto", "ai"}
        )
        with self.app_module.db() as conn:
            ai_persona = dict(conn.execute("SELECT * FROM personas WHERE slug='hegong-afterwork'").fetchone())
            crypto_persona = dict(conn.execute("SELECT * FROM personas WHERE slug='acheng'").fetchone())
        ai_topics = self.app_module.persona_editorial_topics(
            ai_persona, completed["raw_cards"]["selected_topics"], {}
        )
        crypto_topics = self.app_module.persona_editorial_topics(
            crypto_persona, completed["raw_cards"]["selected_topics"], {}
        )
        self.assertEqual([item["topic_domain"] for item in ai_topics], ["ai"])
        self.assertEqual([item["topic_domain"] for item in crypto_topics], ["crypto"])

    def test_daily_context_source_posts_returns_paginated_artifact_only_after_completion(self):
        run_date = self.app_module.shanghai_today()
        run, _ = self.app_module.create_daily_context_run(run_date, "manual")
        pending = self.client.get(f"/api/context/daily-runs/{run_date}/source-posts")
        self.assertEqual(pending.status_code, 404)
        self.app_module.update_daily_context_run(run["id"], status="failed")

        def collect(_accounts, _db, output, **_kwargs):
            snapshot = Path(output) / "runs" / "run-source-posts"
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / "latest.json").write_text(
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
            return {
                "run_id": "run-source-posts",
                "snapshot_dir": str(snapshot),
                "accounts_fetched": 1,
                "posts_seen": 2,
            }

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

    def test_daily_context_source_posts_can_read_ai_domain_artifact(self):
        run_date = self.app_module.shanghai_today()
        run, _ = self.app_module.create_daily_context_run(run_date, "manual")
        artifact = Path(self.temp.name) / "ai-source-posts"
        artifact.mkdir()
        (artifact / "latest.json").write_text(json.dumps({
            "topic_domain": "ai", "generated_at": "2026-08-26T00:00:00+00:00",
            "account_universe": 1, "accounts_fetched": 1, "accounts_skipped": 0,
            "accounts_failed": 0, "posts": [{
                "post_id": "ai-post", "author_id": "ai-author", "handle": "ai_source",
                "text": "AI 原帖", "created_at": "2026-08-26T00:00:00+00:00",
                "url": "https://x.com/ai_source/status/ai-post", "is_reply": False,
                "source_lists": ["ai"],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        self.app_module.update_daily_context_run(
            run["id"], status="needs_review", raw_manifest=json.dumps({
                "domains": {"ai": {"output": str(artifact), "status": "ready"}},
            }, ensure_ascii=False),
        )
        response = self.client.get(f"/api/context/daily-runs/{run_date}/source-posts?topic_domain=ai")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["topic_domain"], "ai")
        self.assertEqual(response.json()["posts"][0]["post_id"], "ai-post")
        self.assertEqual(
            self.client.get(f"/api/context/daily-runs/{run_date}/source-posts?topic_domain=other").status_code,
            422,
        )

    def test_daily_context_run_failure_keeps_manifest_and_can_retry(self):
        run_date = self.app_module.shanghai_today()
        retry_resume_hours = []
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
            retry_resume_hours.append(_kwargs["resume_hours"])
            Path(output).mkdir(parents=True, exist_ok=True)
            return {"run_id": "run-retry", "accounts_fetched": 1, "posts_seen": 1}

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
        self.assertEqual(retry_resume_hours, [20])

    def test_daily_context_run_rejects_empty_cards_without_calling_llm(self):
        run_date = self.app_module.shanghai_today()
        def collect(_accounts, _db, output, **_kwargs):
            Path(output).mkdir(parents=True, exist_ok=True)
            return {"run_id": "run-empty-cards", "accounts_fetched": 1, "posts_seen": 1}

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
                "run_id": "run-all-failed",
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
            "acheng": 3,
            "ridehail-driver-zhao": 3,
            "college-student-linjia": 4,
        }
        for slug, count in expected.items():
            persona = self.client.get(f"/api/personas/{personas[slug]['id']}").json()
            self.assertEqual(len(persona["assets"]), count)
            self.assertTrue(persona["asset_collection"]["ready"])
            self.assertTrue(all(self.client.get(asset["url"]).status_code == 200 for asset in persona["assets"]))

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
        self.assertIn("acheng:16-night-route-selfie.jpg", response.json()["prompt"])

        student = self.client.get(f"/api/personas/{personas['college-student-linjia']['id']}").json()
        self.assertIn("publishable-web/04-outdoor-black-skirt.jpg", student["avatar_url"])
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

    def test_private_topic_can_choose_content_structure_independent_of_source_kind(self):
        raw = self.editorial_context_payload(thought_threads=[
            {
                "id": "thought-industry", "status": "ready", "core_claim": "行业结构判断",
                "structure_id": "industry_structure",
            },
            {
                "id": "thought-howto", "status": "ready", "core_claim": "实用讲解",
                "structure_id": "practical_explainer",
            },
        ])
        validated = self.app_module.validate_persona_editorial_context_input(raw, set())
        normalized = self.app_module.normalize_persona_editorial_context(validated)
        topics = self.app_module.build_persona_private_topics(normalized)
        self.assertEqual(
            [self.app_module.editorial_content_structure(topic)["id"] for topic in topics],
            ["industry_structure", "practical_explainer"],
        )

        invalid = self.editorial_context_payload(thought_threads=[{
            "id": "thought-invalid", "status": "ready", "core_claim": "无效结构",
            "structure_id": "persona-owned-hook",
        }])
        with self.assertRaises(self.app_module.HTTPException) as raised:
            self.app_module.validate_persona_editorial_context_input(invalid, set())
        self.assertEqual(raised.exception.status_code, 422)

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

    def test_publishable_assets_backfill_daily_supplement_candidates(self):
        context_date = self.app_module.shanghai_today()
        run_id = self.create_editorial_run(context_date)
        acheng = next(item for item in self.client.get("/api/personas").json() if item["slug"] == "acheng")
        assets = self.client.get(f"/api/personas/{acheng['id']}").json()["assets"]
        topic = {
            "claim_key": "approved-multi-asset-supplement",
            "source_kind": "daily_supplement",
            "source_id": "fallback-test",
            "title": "补位观点",
            "core_claim": "补位稿应能使用已批准的人设素材。",
        }
        candidate_id, _ = self.insert_formal_queue_candidate(
            run_id, context_date, topic, slug="acheng", title="多素材补位稿"
        )
        with self.app_module.db() as conn:
            self.app_module.attach_publishable_assets_to_daily_supplements(conn, context_date)

        with self.app_module.db() as conn:
            asset_id = conn.execute(
                "SELECT asset_id FROM post_candidates WHERE id=?", (candidate_id,)
            ).fetchone()[0]
        self.assertIn(asset_id, {asset["id"] for asset in assets})
        items = self.client.get("/api/daily-posts").json()
        self.assertEqual(items[0]["asset_id"], asset_id)
        self.assertTrue(items[0]["image_url"])

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

        generated = AsyncMock(return_value={"post": "恢复后的唯一草稿"})
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "generate_persona_post", generated):
            self.run_editorial_pipeline(run_id)
            asyncio.run(self.app_module.run_persona_editorial_pipeline())
            asyncio.run(self.app_module.run_persona_editorial_pipeline())

        self.assertEqual(generated.await_count, 1)
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
            self.assertEqual(tuple(evaluation), ("HOLD", "grok_gemini_critic_reject"))
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
        private_claim = marker + "_THESIS"
        self.put_editorial_context(personas["acheng"]["id"], self.editorial_context_payload(
            expression_debt=[{"id": "acheng-private", "core_claim": marker, "status": "ready"}]
        ))
        self.approve_editorial_context(personas["acheng"]["id"])

        async def first_evaluator(_persona, _context, _daily, topics, _history, _today_count):
            decisions = {}
            for topic in topics:
                if topic.get("source_kind") == "expression_debt":
                    decisions.update(self.editorial_decision(
                        topic, "WRITE", claim_key="acheng-private-claim", core_claim=private_claim,
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
                    topic, "WRITE", claim_key=f"driver-independent-{index}", core_claim=private_claim,
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
        for post in (
            "我发现，真正要看的不是消息有多热，而是有没有新的资金愿意接。",
            "我更关注这个工具到底替用户省掉了哪一步。",
            "我会先看谁被迫交易、谁能持续买入，再判断故事值不值得听。",
        ):
            with self.subTest(post=post):
                self.assertFalse(self.app_module.unauthorized_first_person_experience(post, blocked))
        self.assertTrue(self.app_module.unauthorized_first_person_experience(
            "我这种边跑单边学的人，不能把别人的判断当自己的经历。", blocked
        ))
        self.assertFalse(self.app_module.unauthorized_first_person_experience(
            "没有证伪条件的观点，最后只能让读者自己承担代价。", blocked
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

    def test_thesis_case_1_topic_is_not_thesis(self):
        topic = {"claim_key": "topic-not-thesis", "subject": "产品", "title": "产品变化", "core_claim": "产品更新值得讨论"}
        contract = self.thesis_contract(topic, claim=topic["core_claim"])
        self.assertIn(
            "INFORMATION_DELTA_ZERO",
            self.app_module.thesis_contract_errors(topic, "acheng", contract),
        )

    def test_thesis_case_2_multiple_primary_claims_are_rejected(self):
        topic = {"claim_key": "one-center", "subject": "市场", "title": "市场变化", "core_claim": "公共题"}
        contract = self.thesis_contract(topic, claim_count=2)
        self.assertIn(
            "MULTIPLE_PRIMARY_CLAIMS",
            self.app_module.thesis_contract_errors(topic, "acheng", contract),
        )

    def test_thesis_case_3_source_paraphrase_is_rejected(self):
        topic = {"claim_key": "source-paraphrase", "subject": "项目", "title": "项目动作", "core_claim": "项目刚完成产品升级"}
        contract = self.thesis_contract(topic, claim=topic["core_claim"])
        contract["source_delta"] = "复述原始来源"
        self.assertIn(
            "INFORMATION_DELTA_ZERO",
            self.app_module.thesis_contract_errors(topic, "acheng", contract),
        )

    def test_thesis_case_4_invalid_persona_lens_is_rejected(self):
        topic = {"claim_key": "lens-check", "subject": "产品", "title": "产品动作", "core_claim": "公共题"}
        contract = self.thesis_contract(topic, lens="institutional_authority")
        self.assertIn(
            "PERSONA_LENS_INVALID",
            self.app_module.thesis_contract_errors(topic, "acheng", contract),
        )

    def test_thesis_case_5_same_topic_same_meaning_collides(self):
        topic = {"claim_key": "same-topic", "subject": "市场", "title": "同一热点", "core_claim": "公共题"}
        left = self.thesis_contract(topic, claim="这次变化会先提高普通用户的执行成本。")
        right = self.thesis_contract(
            topic, slug="ridehail-driver-zhao", claim="这次变化先提高普通用户的执行成本。",
        )
        self.assertTrue(self.app_module.thesis_semantic_collision(left, right))

    def test_thesis_case_6_same_topic_different_meaning_is_allowed(self):
        topic = {"claim_key": "same-topic-two", "subject": "市场", "title": "同一热点", "core_claim": "公共题"}
        left = self.thesis_contract(topic, claim="这次变化会先提高普通用户的执行成本。")
        right = self.thesis_contract(
            topic, slug="axu", claim="这次变化正在重新分配做市商之间的流动性。", relation="redistributes",
        )
        self.assertFalse(self.app_module.thesis_semantic_collision(left, right))

    def test_thesis_case_7_draft_drift_is_rejected(self):
        adherence = self.app_module.validate_thesis_adherence_result({
            "verdict": "PASS", "reason_codes": ["THESIS_DRIFT"],
            "spans": [{"text": "结尾撤销了原判断", "classification": "QUALIFIES_THESIS"}],
        })
        self.assertEqual(adherence["verdict"], "REJECT")
        self.assertIn("THESIS_DRIFT", adherence["reason_codes"])

    def test_thesis_case_8_writer_new_claim_is_rejected(self):
        adherence = self.app_module.validate_thesis_adherence_result({
            "verdict": "PASS", "reason_codes": [],
            "spans": [{"text": "未经证实的新因果", "classification": "UNSUPPORTED_NEW_CLAIM"}],
        })
        self.assertEqual(adherence["reason_codes"], ["UNSUPPORTED_NEW_CLAIM"])

    def test_thesis_case_9_excessive_off_thesis_is_rejected(self):
        adherence = self.app_module.validate_thesis_adherence_result({
            "verdict": "PASS", "reason_codes": [],
            "spans": [
                {"text": "中心", "classification": "SUPPORTS_THESIS"},
                {"text": "跑题一", "classification": "TANGENT"},
                {"text": "跑题二", "classification": "TANGENT"},
                {"text": "跑题三", "classification": "TANGENT"},
            ],
        })
        self.assertIn("OFF_THESIS", adherence["reason_codes"])

    def test_thesis_case_10_hold_and_ignore_never_call_writer(self):
        context_date = self.app_module.shanghai_today()
        topics = [
            {"claim_key": "hold-topic", "subject": "A", "title": "A", "core_claim": "A"},
            {"claim_key": "ignore-topic", "subject": "B", "title": "B", "core_claim": "B"},
        ]
        run_id = self.create_editorial_run(context_date, topics=topics)
        writer = AsyncMock(side_effect=AssertionError("HOLD/IGNORE must not call writer"))

        async def evaluator(_persona, _context, _daily, current, _history, _count):
            return {
                **self.editorial_decision(current[0], "HOLD"),
                **self.editorial_decision(current[1], "IGNORE"),
            }

        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
            "XOPS_DAILY_POST_TARGET_PER_PERSONA": "0",
        }), patch.object(
            self.app_module, "evaluate_persona_editorial", AsyncMock(side_effect=evaluator)
        ), patch.object(self.app_module, "write_persona_editorial_gemini", writer):
            self.run_editorial_pipeline(run_id)
        writer.assert_not_awaited()

    def test_thesis_case_11_quantity_pressure_never_bypasses_gate(self):
        topic = {"claim_key": "quota-cannot-bypass", "subject": "市场", "title": "市场", "core_claim": "公共题"}
        invalid = self.thesis_contract(topic, claim_count=2)
        with patch.dict(os.environ, {"XOPS_DAILY_POST_TARGET_PER_PERSONA": "99"}):
            self.assertIn(
                "MULTIPLE_PRIMARY_CLAIMS",
                self.app_module.thesis_contract_errors(topic, "acheng", invalid),
            )

    def test_thesis_case_12_structure_cannot_mutate_frozen_thesis(self):
        topic = {"claim_key": "structure-boundary", "subject": "产品", "title": "产品", "core_claim": "公共题", "angle_family": "project_evaluation"}
        contract = self.thesis_contract(topic)
        before = json.dumps(contract, ensure_ascii=False, sort_keys=True)
        structure = self.app_module.editorial_content_structure(topic, contract)
        self.assertEqual(before, json.dumps(contract, ensure_ascii=False, sort_keys=True))
        self.assertIn("required_semantic_slots", structure)
        self.assertIn("allowed_reasoning_shapes", structure)

    def test_thesis_case_13_repair_cannot_weaken_thesis(self):
        topic = {"claim_key": "repair-boundary", "subject": "市场", "title": "市场", "core_claim": "公共题"}
        contract = self.thesis_contract(topic, claim="这次变化会先提高普通用户的执行成本。")
        instruction = self.app_module.thesis_repair_instruction(
            {"reason_codes": ["THESIS_DRIFT"]}, contract
        )
        self.assertIn(contract["primary_claim"], instruction)
        self.assertNotIn("重新选择", instruction)

    def grounding_fixture(self, *, claim_type="DESCRIPTIVE", anchors=1, mechanisms=False,
                          epistemic_status="KNOWN"):
        payload = {
            "version": self.app_module.REALITY_PAYLOAD_VERSION,
            "reality_payload_id": "reality:test",
            "topic_id": "grounding:test",
            "grounding_mode": "LIVE_RESEARCH",
            "primary_observation": {"statement": "现实观察", "fact_ids": ["fact:1"], "source_ids": ["source:1"], "observed_at": "2026-08-28"},
            "concrete_facts": [], "observed_behaviors": [],
            "mechanisms": ([{
                "input": "A", "transformation": "B", "output": "C",
                "supporting_fact_ids": ["fact:1"], "confidence": "verified",
            }] if mechanisms else []),
            "frictions": [], "counter_signals": [], "uncertainties": [],
            "consensus_evidence": [],
            "source_dependent_anchors": [
                {
                    "reality_ref": f"fact:{index + 1}", "statement": f"现实观察 {index + 1}",
                    "source_ids": [f"source:{index + 1}"], "kind": "VERIFIED_FACT",
                    "epistemic_status": epistemic_status,
                }
                for index in range(anchors)
            ],
        }
        topic = {
            "claim_key": "grounding:test", "title": "现实约束测试",
            "claim_type": claim_type, "angle_family": "market_cognition",
        }
        thesis = {
            "thesis_id": "thesis:test", "thesis_type": "ASSERTION",
            "primary_claim": "现实观察改变了当前判断。", "falsifier": "观察消失",
        }
        return payload, self.app_module.compile_grounding_contract(topic, thesis, payload)

    def grounding_draft(self, paragraphs):
        return {
            "text": "\n\n".join(item["text"] for item in paragraphs),
            "paragraphs": paragraphs,
            "grounding_contract_version": self.app_module.GROUNDING_CONTRACT_VERSION,
        }

    def test_grounding_case_1_live_topic_without_material_fact_fails_closed(self):
        payload, contract = self.grounding_fixture(anchors=0)
        self.assertIn("INSUFFICIENT_REALITY_PAYLOAD", contract["preflight_reason_codes"])
        self.assertIn("LOW_SOURCE_DEPENDENCE", contract["preflight_reason_codes"])

    def test_grounding_case_2_one_fact_cannot_support_abstract_expansion(self):
        payload, contract = self.grounding_fixture()
        draft = self.grounding_draft([
            {"section": "signal_context", "text": "现实观察 1。", "job": "EVIDENCE", "thesis_relation": "SUPPORT", "reality_refs": ["fact:1"]},
            {"section": "close", "text": "所以整个行业都会被永久改变。", "job": "CONCLUSION", "thesis_relation": "SUPPORT", "reality_refs": []},
        ])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("LOW_REALITY_CONTRIBUTION", review["reason_codes"])

    def test_grounding_case_3_synthetic_consensus_is_rejected(self):
        payload, contract = self.grounding_fixture()
        draft = self.grounding_draft([{
            "section": "hook", "text": "市场普遍认为这个判断已经成立。", "job": "CLAIM",
            "thesis_relation": "SUPPORT", "reality_refs": ["fact:1"],
        }])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("UNSUPPORTED_CONSENSUS_CLAIM", review["reason_codes"])

    def test_grounding_case_4_analogy_cannot_complete_causal_proof(self):
        payload, contract = self.grounding_fixture(claim_type="CAUSAL", mechanisms=True)
        draft = self.grounding_draft([{
            "section": "gap", "text": "它就像股票回购，所以结果一定相同。", "job": "MECHANISM",
            "thesis_relation": "EXPLAIN", "reality_refs": [],
        }])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("ANALOGY_AS_EVIDENCE", review["reason_codes"])

    def test_grounding_case_5_causal_claim_without_mechanism_returns_to_research(self):
        _payload, contract = self.grounding_fixture(claim_type="CAUSAL")
        self.assertIn("MECHANISM_GAP", contract["preflight_reason_codes"])

    def test_grounding_research_requires_cited_fetchable_source_and_exact_excerpt(self):
        candidate = {
            "gap_code": "MECHANISM_GAP",
            "statement": "官方仓库页面公开显示项目的 Star 计数。",
            "source_url": "https://github.com/example/project",
            "source_kind": "github", "published_at": "2026-08-28",
            "support_role": "mechanism",
            "evidence_excerpt": "4687 users starred this repository",
            "mechanism": {"input": "公开仓库", "transformation": "用户点击 Star", "output": "页面累计计数"},
        }
        with patch.object(
            self.app_module.httpx, "AsyncClient", return_value=FakeGitHubClient()
        ):
            verified = asyncio.run(self.app_module.verify_grounding_research_candidates(
                [candidate], [candidate["source_url"]],
            ))
        self.assertEqual(len(verified["verified"]), 1)
        uncited = asyncio.run(self.app_module.verify_grounding_research_candidates(
            [candidate], [],
        ))
        self.assertEqual(uncited["verified"], [])
        self.assertEqual(uncited["rejected"][0]["reason"], "invalid_or_uncited_source")

    def test_verified_grok_mechanism_research_resolves_preflight_gap(self):
        payload, contract = self.grounding_fixture(claim_type="CAUSAL")
        self.assertIn("MECHANISM_GAP", contract["preflight_reason_codes"])
        research = {"verified_evidence": [{
            "statement": "官方文档说明输入经过资源计费后形成新的费用输出。",
            "source_url": "https://example.com/official-mechanism",
            "source_kind": "official_documentation", "published_at": "2026-08-28",
            "support_role": "mechanism", "verification_status": "source_fetched_excerpt_matched",
            "mechanism": {"input": "资源请求", "transformation": "按请求单位计费", "output": "资源费用"},
        }]}
        merged = self.app_module.merge_reality_research(payload, research)
        topic = {"claim_key": "grounding:test", "claim_type": "CAUSAL"}
        thesis = {
            "thesis_id": "thesis:test", "thesis_type": "EXPLANATION",
            "primary_claim": "资源请求通过计费机制改变费用。", "falsifier": "机制未启用",
        }
        updated = self.app_module.compile_grounding_contract(topic, thesis, merged)
        self.assertNotIn("MECHANISM_GAP", updated["preflight_reason_codes"])
        self.assertEqual(merged["mechanisms"][-1]["supporting_fact_ids"][0][:9], "research:")

    def test_grounding_case_6_number_without_reasoning_contribution_does_not_pass(self):
        payload, contract = self.grounding_fixture()
        payload["source_dependent_anchors"][0]["statement"] = "观察值为 42%。"
        draft = self.grounding_draft([
            {"section": "signal_context", "text": "观察值为 42%。", "job": "EVIDENCE", "thesis_relation": "SUPPORT", "reality_refs": ["fact:1"]},
            {"section": "close", "text": "耐心最重要。", "job": "CONCLUSION", "thesis_relation": "SUPPORT", "reality_refs": []},
        ])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("LOW_REALITY_CONTRIBUTION", review["reason_codes"])

    def test_grounding_case_7_may_indicate_cannot_be_upgraded_to_proves(self):
        payload, contract = self.grounding_fixture(epistemic_status="INFERRED")
        draft = self.grounding_draft([{
            "section": "close", "text": "这个信号证明了结论必然成立。", "job": "CONCLUSION",
            "thesis_relation": "SUPPORT", "reality_refs": ["fact:1"],
        }])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("CLAIM_STRENGTH_UPGRADE", review["reason_codes"])

    def test_grounding_case_8_unknown_must_remain_unknown(self):
        payload, contract = self.grounding_fixture()
        payload["uncertainties"] = [{"reality_ref": "uncertainty:0", "question": "机制是否持续？", "status": "UNKNOWN"}]
        contract = self.app_module.compile_grounding_contract(
            {"claim_key": "grounding:test", "claim_type": "DESCRIPTIVE"},
            {"thesis_id": "thesis:test", "thesis_type": "ASSERTION", "primary_claim": "判断"}, payload,
        )
        draft = self.grounding_draft([{
            "section": "close", "text": "现实观察支持当前判断。", "job": "CONCLUSION",
            "thesis_relation": "SUPPORT", "reality_refs": ["fact:1"],
        }])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("UNCERTAINTY_DROPPED", review["reason_codes"])

    def test_grounding_case_9_generic_background_budget_is_enforced_by_structure(self):
        payload, contract = self.grounding_fixture()
        draft = self.grounding_draft([
            {"section": "signal_context", "text": "这是很长的通用背景。" * 30, "job": "CONTEXT", "thesis_relation": "EXPLAIN", "reality_refs": []},
            {"section": "close", "text": "现实观察支持判断。", "job": "CONCLUSION", "thesis_relation": "SUPPORT", "reality_refs": ["fact:1"]},
        ])
        review = self.app_module.validate_editorial_grounding(
            draft, payload, contract, self.app_module.editorial_content_structure({"structure_id": "market_cognition"})
        )
        self.assertIn("EXCESSIVE_GENERIC_BACKGROUND", review["reason_codes"])

    def test_grounding_case_10_research_failure_never_reaches_writer_or_editor(self):
        context_date = self.app_module.shanghai_today()
        topic = {
            "claim_key": "live-without-source", "title": "缺少现实依据的实时题",
            "core_claim": "不能靠语言补齐研究。", "eligible": True,
            "source_kind": "market", "source_refs": [],
        }
        run_id = self.create_editorial_run(context_date, topics=[topic])
        self.insert_pending_editorial_write(run_id, context_date, topic)
        writer, editor = AsyncMock(), AsyncMock()
        with patch.dict(os.environ, {
            "XOPS_DAILY_POST_ENABLED": "true", "XOPS_DAILY_POST_PERSONAS": "acheng",
        }), patch.object(self.app_module, "write_persona_editorial_gemini", writer), patch.object(
            self.app_module, "critique_persona_editorial_draft", editor
        ), patch.object(
            self.app_module, "research_reality_payload_gaps_grok", AsyncMock(return_value={
                "status": "no_verified_evidence", "verified_evidence": [],
                "rejected_evidence": [], "citations": [], "tool_usage": [], "model": "grok-test",
            })
        ):
            self.run_editorial_pipeline(run_id)
        writer.assert_not_awaited()
        editor.assert_not_awaited()
        with self.app_module.db() as conn:
            row = conn.execute(
                "SELECT status,reason_code FROM persona_editorial_evaluations WHERE run_id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row["status"], "HOLD")
        self.assertEqual(row["reason_code"], "INSUFFICIENT_REALITY_PAYLOAD")

if __name__ == "__main__":
    unittest.main()
