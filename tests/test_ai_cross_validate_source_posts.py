from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_sources.ai_cross_validate_source_posts import cross_validate_ai, evaluate_ai_opinion
from market_sources.collect_big_source_posts import init_db


def insert_post(db, run_id: str, post_id: str, author_id: str, text: str) -> None:
    db.execute(
        """INSERT INTO source_posts(
            post_id,author_id,handle,text,created_at,url,is_reply,source_lists,is_retweet,is_quote,metrics
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            post_id, author_id, author_id, text, datetime.now(timezone.utc).isoformat(),
            f"https://x.com/{author_id}/status/{post_id}", 0, '["ai"]', 0, 0, "{}",
        ),
    )
    db.execute("INSERT INTO source_post_runs(run_id,post_id) VALUES(?,?)", (run_id, post_id))


class AiCrossValidateTest(unittest.TestCase):
    def test_ai_outputs_are_domain_labeled_and_crypto_only_post_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path, output, run_id = root / "source.sqlite3", root / "ai", "run-ai"
            with sqlite3.connect(db_path) as db:
                init_db(db)
                for index in range(4):
                    insert_post(
                        db, run_id, f"ai-{index}", f"ai-author-{index}",
                        "OpenAI released GPT-5 today. The benchmark improvement changes which coding workflows teams can automate.",
                    )
                insert_post(
                    db, run_id, "view", "ai-view",
                    "OpenAI's GPT-5 release matters because stronger benchmark results make AI coding adoption easier to measure. "
                    "If teams can replace a repeatable review step, the workflow value is larger than a launch-day demo.",
                )
                insert_post(
                    db, run_id, "btc", "crypto-author",
                    "Bitcoin ETF net inflows are rising because spot demand is absorbing leverage, so the next catalyst is liquidity.",
                )
                db.commit()

            result = cross_validate_ai(db_path, output, run_id=run_id)
            fact = json.loads((output / "fact_cards.json").read_text(encoding="utf-8"))
            opinions = json.loads((output / "opinion_cards.json").read_text(encoding="utf-8"))
            attention = json.loads((output / "attention_topics.json").read_text(encoding="utf-8"))
            discussion = json.loads((output / "discussion_topics.json").read_text(encoding="utf-8"))

            self.assertEqual(result["topic_domain"], "ai")
            self.assertEqual(fact["topic_domain"], "ai")
            self.assertTrue(fact["cards"])
            self.assertTrue(all(card["topic_domain"] == "ai" for card in fact["cards"]))
            self.assertEqual(opinions["opinions"][0]["topic_domain"], "ai")
            self.assertEqual(attention["topic_domain"], "ai")
            self.assertEqual(discussion["topic_domain"], "ai")
            self.assertIn("openai", [topic["key"] for topic in attention["hot"]])
            self.assertNotIn("bitcoin", [topic["key"] for topic in attention["hot"] + attention["niche"]])

    def test_requires_explicit_ai_anchor(self):
        row = {
            "text": "The market is changing because better distribution makes new products easier to adopt, so teams should watch retention.",
            "created_at": "2026-08-26T00:00:00+00:00",
            "source_lists": ["ai"],
            "is_reply": False,
        }
        self.assertEqual(evaluate_ai_opinion(row)["rejection"], "non_ai")


if __name__ == "__main__":
    unittest.main()
