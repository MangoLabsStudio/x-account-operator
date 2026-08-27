from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from market_sources.collect_big_source_posts import init_db
from market_sources.cross_validate_source_posts import (
    build_attention_topics,
    build_discussion_topics,
    cross_validate,
    evaluate_opinion,
)
from market_sources.run_daily import run_daily


def insert_run_post(db, run_id: str, post_id: str, author_id: str, text: str) -> None:
    db.execute(
        """INSERT INTO source_posts(
            post_id,author_id,handle,text,created_at,url,is_reply,source_lists,is_retweet,is_quote,metrics
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            post_id,
            author_id,
            author_id,
            text,
            datetime.now(timezone.utc).isoformat(),
            f"https://x.com/{author_id}/status/{post_id}",
            0,
            '["crypto"]',
            0,
            0,
            "{}",
        ),
    )
    db.execute("INSERT INTO source_post_runs(run_id,post_id) VALUES(?,?)", (run_id, post_id))


class RunSnapshotValidationTest(unittest.TestCase):
    def test_cross_validate_reads_only_requested_run_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "sources.sqlite3"
            output = root / "cards"
            with sqlite3.connect(db_path) as db:
                init_db(db)
                for index in range(2):
                    insert_run_post(
                        db,
                        "old-run",
                        f"old-{index}",
                        f"old-author-{index}",
                        "OLD_SHARED_SENTINEL Coinbase launched tokenized equities on Base today.",
                    )
                    insert_run_post(
                        db,
                        "new-run",
                        f"new-{index}",
                        f"new-author-{index}",
                        "NEW_SNAPSHOT_SENTINEL Coinbase launched tokenized equities on Base today.",
                    )
                db.commit()

            result = cross_validate(db_path, output, run_id="new-run")
            fact_payload = json.loads((output / "fact_cards.json").read_text(encoding="utf-8"))

            self.assertEqual(result["run_id"], "new-run")
            self.assertEqual(result["source_posts"], 2)
            self.assertEqual(fact_payload["run_id"], "new-run")
            self.assertEqual(fact_payload["source_post_count"], 2)
            self.assertEqual(
                {item["source_ref"] for item in fact_payload["cards"][0]["evidence"]},
                {"new-0", "new-1"},
            )
            self.assertNotIn(
                "OLD_SHARED_SENTINEL",
                "".join(path.read_text(encoding="utf-8") for path in output.glob("*.json")),
            )

    def test_cross_validate_empty_run_does_not_fallback_to_shared_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "sources.sqlite3"
            output = root / "cards"
            with sqlite3.connect(db_path) as db:
                init_db(db)
                insert_run_post(
                    db,
                    "old-run",
                    "old-1",
                    "old-author",
                    "BTC launched an old shared event today.",
                )
                db.commit()

            result = cross_validate(db_path, output, run_id="empty-new-run")

            self.assertEqual(result["source_posts"], 0)
            self.assertEqual(
                json.loads((output / "fact_cards.json").read_text(encoding="utf-8"))["source_post_count"],
                0,
            )
            self.assertEqual(
                json.loads((output / "opinion_cards.json").read_text(encoding="utf-8"))["source_post_count"],
                0,
            )

    def test_run_daily_forwards_collection_run_id(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def validate(_db, output, **kwargs):
                calls.append((Path(output), kwargs["run_id"]))
                return {"run_id": kwargs["run_id"], "source_posts": 0}

            with patch(
                "market_sources.run_daily.collect",
                return_value={"run_id": "new-run", "snapshot_dir": str(root / "snapshot")},
            ), patch("market_sources.run_daily.cross_validate", side_effect=validate):
                run_daily(
                    db_path=root / "db.sqlite3",
                    output_dir=root / "output",
                    key="runtime-key",
                )

            self.assertEqual(calls, [(root / "snapshot", "new-run")])


def row(text: str, *, is_reply: bool = False) -> dict:
    return {
        "text": text,
        "created_at": "2026-08-24T00:00:00+00:00",
        "source_lists": ["crypto"],
        "is_reply": is_reply,
    }


class FreshOpinionCorpusFilterTest(unittest.TestCase):
    def test_keeps_crypto_viewpoint_with_checkable_context_and_causal_logic(self):
        card = evaluate_opinion(row(
            "BTC 的短线方向仍取决于现货 ETF 的净流入能否延续。过去两周 3% 的回撤没有"
            "明显压低资金费率，因此如果 ETF 申购保持稳定，市场更像是在消化杠杆而不是进入新一轮去风险。"
        ))

        self.assertIsNone(card["rejection"])
        self.assertGreaterEqual(card["quality_score"], 12)
        self.assertIn("crypto:bitcoin", card["tags"])
        self.assertEqual(card["quality_score"], card["score"])

    def test_rejects_non_crypto_promotion_personal_trade_and_reply_truncation(self):
        cases = [
            (
                "AI 产品的留存率不是看注册量，而是看用户是否在第二周还愿意把工作流迁进去。"
                "如果协作场景没有被解决，投放预算只会把流失放大。",
                False,
                "non_crypto",
            ),
            (
                "$SOL will 100x this month. Ape in now, join our Discord and use my code before the"
                " whitelist closes. This is the best crypto trade you will see all year.",
                False,
                "promotion_or_shill",
            ),
            (
                "I bought BTC this morning and doubled my position after the ETF headline. My entry is"
                " clean, my PnL is already green, and I will keep holding until the next breakout.",
                False,
                "personal_trade_or_pnl",
            ),
            (
                "BTC 的方向仍取决于现货 ETF 净流入和永续资金费率。过去两周 3% 的回撤没有"
                "改变持仓量结构，因此如果申购继续回升，市场更可能先修复流动性而不是继续踩踏……",
                True,
                "below_quality_threshold",
            ),
        ]
        for text, is_reply, rejection in cases:
            with self.subTest(rejection=rejection):
                card = evaluate_opinion(row(text, is_reply=is_reply))
                self.assertEqual(card["rejection"], rejection)

        reply = evaluate_opinion(row(cases[-1][0], is_reply=True))
        self.assertIn("penalty:reply", reply["tags"])
        self.assertIn("penalty:truncated", reply["tags"])

    def test_requires_real_crypto_anchor_not_ticker_ai_token_or_plain_base(self):
        cases = [
            "$TSLA is up 3% because delivery estimates improved, so the next catalyst is margins.",
            "The AI token has a new product launch, but adoption still depends on distribution.",
            "The base case is that revenue improves if users return after the product update.",
            "Use goal mode to get a few more days of Sol from your coding quota.",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(evaluate_opinion(row(text))["rejection"], "non_crypto")

    def test_rejects_personal_trading_holdings_and_pnl_language(self):
        cases = [
            "BTC personally my strategy is holding and trading around the ETF flow this quarter.",
            "I'm 60-70% deployed in ETH after the latest ETF flow update.",
            "I made a lot of money holding BTC over the bear market and will keep the position.",
            "For me BTC maximalism peaked years ago; since then I could hold, buy and sell Bitcoin freely.",
            "I published this BTC signal in my subscriber article and reminded members before the move.",
            "My TP was early on ETH; we buy again after the next dump.",
            "I bought a bag of SOL and already have a big bag of ETH.",
            "Trade with me and track my buys while I was adding to BTC every week.",
            "BTC 我自己的双币策略还有筹码，对冲我的现货；之前有抄底，现在有浮亏也套着。",
            "这是我在订阅区发表的 BTC 文章，当时我提醒小伙伴注意这个见底信号。",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    evaluate_opinion(row(text))["rejection"], "personal_trade_or_pnl"
                )

    def test_rejects_promotion_cta_and_incentive_language(self):
        cases = [
            "BTC looks strong, DM me for the ref link and drop your wallet address now.",
            "Mint yours, claim your airdrop allocation, and register for sale before the deadline.",
            "Farm BTC points now, don't miss the claim countdown, and please RT and follow.",
            "BTC community rewards are live; claim countdown starts now.",
            "BTC 社区奖励活动价，撸一下，访问官网完成注册领取测试币和积分。",
            "蓝V代开，BTC 活动价只限今天，社区奖励马上结束。",
            "Early crypto project on Base, caught early; join the waitlist and watch the video.",
            "BTC 欢迎收看视频链接，加入候补并开启提醒。",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    evaluate_opinion(row(text))["rejection"], "promotion_or_shill"
                )


def attention_row(
    post_id: str,
    author_id: str,
    text: str,
    created_at: str,
    *,
    source_lists: list[str] | None = None,
    metrics: dict | None = None,
) -> dict:
    return {
        "post_id": post_id,
        "author_id": author_id,
        "text": text,
        "created_at": created_at,
        "source_lists": source_lists or ["list_a"],
        "metrics": metrics,
        "is_reply": False,
        "is_retweet": False,
    }


class AttentionTopicsTest(unittest.TestCase):
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)

    def test_multi_author_topic_beats_single_author_technical_post(self):
        rows = [
            attention_row("p1", "a1", "$PUMP discussion is moving from launch chatter to fee capture.", "2026-08-24T11:00:00+00:00"),
            attention_row("p2", "a2", "$PUMP attention is still focused on creator incentives.", "2026-08-24T10:00:00+00:00"),
            attention_row("p3", "a3", "Pump.fun changed how teams think about distribution.", "2026-08-24T09:00:00+00:00"),
            attention_row("p5", "a4", "$PUMP fees are now part of the market discussion.", "2026-08-24T08:00:00+00:00"),
            attention_row("p6", "a5", "Pump.fun activity is drawing attention again.", "2026-08-24T07:00:00+00:00"),
            attention_row("p4", "tech", "SGP-0003 changes Solana resource fee accounting in detail.", "2026-08-24T11:30:00+00:00", metrics={"like_count": 9999}),
            attention_row("p7", "tech2", "SGP-0003 changes the requested resource fee on Solana.", "2026-08-24T10:30:00+00:00"),
        ]

        topics = build_attention_topics(rows, self.now)

        self.assertEqual(topics["hot"][0]["key"], "pump_fun")
        self.assertEqual(topics["hot"][0]["unique_authors"], 5)
        self.assertNotIn("proposal:sgp-0003", [topic["key"] for topic in topics["hot"]])
        self.assertIn("proposal:sgp-0003", [topic["key"] for topic in topics["niche"]])
        sgp = next(topic for topic in topics["niche"] if topic["key"] == "proposal:sgp-0003")
        self.assertEqual(sgp["unique_authors"], 2)

    def test_same_author_reposts_do_not_make_a_hot_topic(self):
        rows = [
            attention_row("p1", "same", "BTC market structure is tightening.", "2026-08-24T11:00:00+00:00"),
            attention_row("p2", "same", "BTC liquidity is still the central issue.", "2026-08-24T10:00:00+00:00"),
            attention_row("p3", "same", "BTC needs fresh spot demand to break out.", "2026-08-24T09:00:00+00:00"),
        ]

        topics = build_attention_topics(rows, self.now)

        self.assertEqual(topics["hot"], [])
        self.assertEqual(topics["niche"][0]["key"], "bitcoin")
        self.assertEqual(topics["niche"][0]["unique_authors"], 1)
        self.assertEqual(topics["niche"][0]["post_count"], 3)

    def test_recent_cross_list_and_engagement_break_ties_in_order(self):
        rows = [
            attention_row("h1", "h1", "$HYPE discussion is accelerating.", "2026-08-24T11:00:00+00:00", source_lists=["list_a", "list_b"]),
            attention_row("h2", "h2", "Hyperliquid volumes are attracting attention.", "2026-08-24T10:00:00+00:00", source_lists=["list_a"]),
            attention_row("j1", "j1", "$JUP discussion is accelerating.", "2026-08-24T11:00:00+00:00", source_lists=["list_a", "list_b"]),
            attention_row("j2", "j2", "Jupiter volumes are attracting attention.", "2026-08-24T10:00:00+00:00"),
            attention_row("a1", "a1", "$AAA discussion is accelerating.", "2026-08-24T11:00:00+00:00", metrics={"like_count": 100}),
            attention_row("a2", "a2", "$AAA volumes are attracting attention.", "2026-08-24T10:00:00+00:00", metrics={"retweet_count": 20}),
        ]
        for key, label in (("h", "$HYPE"), ("j", "$JUP"), ("a", "$AAA")):
            rows.extend(
                attention_row(f"{key}{index}", f"{key}{index}", f"{label} has another independent market view {index}.", f"2026-08-24T0{9-index}:00:00+00:00")
                for index in range(3, 6)
            )

        hot = build_attention_topics(rows, self.now)["hot"]

        self.assertEqual([topic["key"] for topic in hot[:3]], ["hyperliquid", "jupiter", "ticker:aaa"])
        self.assertEqual(hot[2]["engagement_total"], 120)
        self.assertEqual(hot[2]["engagement_coverage"]["posts_with_metrics"], 2)

    def test_posts_older_than_24_hours_do_not_contribute(self):
        rows = [
            attention_row("old1", "a1", "$PUMP was discussed yesterday.", "2026-08-23T11:59:00+00:00"),
            attention_row("old2", "a2", "Pump.fun was discussed yesterday.", "2026-08-23T11:58:00+00:00"),
            attention_row("new1", "a1", "$PUMP is active today.", "2026-08-24T11:00:00+00:00"),
        ]

        topics = build_attention_topics(rows, self.now)

        self.assertEqual(topics["hot"], [])
        self.assertEqual(topics["niche"][0]["post_count"], 1)


class DiscussionTopicsTest(unittest.TestCase):
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)

    def test_entity_plus_mechanism_with_five_authors_is_writable(self):
        rows = [
            attention_row(
                f"etf{index}",
                f"author{index}",
                f"Bitcoin ETF recorded fresh net inflow in session {index}.",
                f"2026-08-24T{7 + index:02d}:00:00+00:00",
            )
            for index in range(5)
        ]
        rows.extend(
            attention_row(
                f"btc{index}",
                f"btc_author{index}",
                "Bitcoin remains the largest crypto asset by attention.",
                "2026-08-24T08:00:00+00:00",
            )
            for index in range(5)
        )

        topics = build_discussion_topics(rows, self.now)

        self.assertEqual(topics["hot"][0]["key"], "bitcoin_etf:etf_flows")
        self.assertEqual(topics["hot"][0]["parent"]["key"], "bitcoin_etf")
        self.assertEqual(topics["hot"][0]["mechanism"]["key"], "etf_flows")
        self.assertEqual(topics["hot"][0]["unique_authors"], 5)
        self.assertEqual(len(topics["hot"]), 1)
        self.assertEqual(topics["niche"], [])
        self.assertFalse(any(topic["key"] == "bitcoin" for topic in topics["hot"]))

    def test_two_author_topics_need_a_second_heat_signal(self):
        rows = [
            attention_row(
                f"sol{index}",
                f"sol_author{index}",
                "Solana mainnet launch is changing how applications think about distribution.",
                f"2026-08-24T{8 + index:02d}:00:00+00:00",
            )
            for index in range(4)
        ]
        rows.extend(
            [
                attention_row("sgp1", "sgp_author1", "SGP-0003 changes Solana resource fee accounting.", "2026-08-24T11:00:00+00:00"),
                attention_row("sgp2", "sgp_author2", "SGP-0003 is a Solana governance proposal about resource fees.", "2026-08-24T10:00:00+00:00"),
            ]
        )

        topics = build_discussion_topics(rows, self.now)

        self.assertIn("solana:listing_launch", [topic["key"] for topic in topics["hot"]])
        self.assertIn("solana:fee_model", [topic["key"] for topic in topics["niche"]])
        self.assertIn("solana:governance", [topic["key"] for topic in topics["niche"]])

    def test_two_author_cross_list_topic_is_hot(self):
        rows = [
            attention_row(
                "sol1", "sol_author1",
                "Solana price is changing the market structure.",
                "2026-08-24T11:00:00+00:00", source_lists=["list_a"],
            ),
            attention_row(
                "sol2", "sol_author2",
                "Solana price is changing the market structure.",
                "2026-08-24T10:00:00+00:00", source_lists=["list_b"],
            ),
        ]

        topics = build_discussion_topics(rows, self.now)

        self.assertIn("solana:market_structure", [topic["key"] for topic in topics["hot"]])

    def test_market_structure_and_tokenized_equities_are_concrete_hot_topics(self):
        rows = []
        for index in range(5):
            rows.append(
                attention_row(
                    f"btc{index}",
                    f"btc_author{index}",
                    f"BTC price is testing weekly support with spot volume rising {index}.",
                    f"2026-08-24T{7 + index:02d}:00:00+00:00",
                )
            )
            rows.append(
                attention_row(
                    f"rwa{index}",
                    f"rwa_author{index}",
                    f"Robinhood Chain is adding tokenized equities with AMM liquidity {index}.",
                    f"2026-08-24T{7 + index:02d}:00:00+00:00",
                )
            )

        hot = build_discussion_topics(rows, self.now)["hot"]

        self.assertIn("bitcoin:market_structure", [topic["key"] for topic in hot])
        self.assertIn("robinhood_chain:tokenized_equities", [topic["key"] for topic in hot])

    def test_stablecoin_payments_requires_payment_semantics(self):
        rwa_rows = [
            attention_row(
                f"rwa{index}",
                f"rwa_author{index}",
                f"Robinhood tokenized equities USDC AMM LP incentive campaign {index}.",
                f"2026-08-24T{7 + index:02d}:00:00+00:00",
            )
            for index in range(5)
        ]
        payment_rows = [
            attention_row(
                f"pay{index}",
                f"pay_author{index}",
                f"Binance Pay lets USDC holders pay merchants at checkout {index}.",
                f"2026-08-24T{7 + index:02d}:30:00+00:00",
            )
            for index in range(5)
        ]

        rwa_topics = build_discussion_topics(rwa_rows, self.now)
        rwa_keys = [topic["key"] for group in rwa_topics.values() for topic in group]
        payment_topics = build_discussion_topics(payment_rows, self.now)
        payment_keys = [topic["key"] for group in payment_topics.values() for topic in group]

        self.assertFalse(any(key.endswith(":stablecoin_payments") for key in rwa_keys))
        self.assertIn("binance:stablecoin_payments", payment_keys)


if __name__ == "__main__":
    unittest.main()
