import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path


class LegacyInitialDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            XOPS_DATA_DIR=self.temp.name,
            XOPS_DAILY_CONTEXT_ENABLED="false",
        )
        import app
        from scripts import import_initial_drafts, supersede_initial_batch_drafts

        self.app = importlib.reload(app)
        self.importer = importlib.reload(import_initial_drafts)
        self.superseder = importlib.reload(supersede_initial_batch_drafts)

    def tearDown(self):
        self.temp.cleanup()

    def batch_dir(self):
        path = Path(self.temp.name) / "batch"
        path.mkdir()
        items = [
            {
                "slot": f"news-{index:02d}",
                "kind": "news",
                "topic": f"时事 {index}",
                "body": f"时事正文 {index}",
                "sources": [],
            }
            for index in range(1, 4)
        ] + [
            {
                "slot": f"evergreen-{index:02d}",
                "kind": "evergreen",
                "topic": f"观点 {index}",
                "body": f"观点正文 {index}",
                "sources": [],
            }
            for index in range(1, 8)
        ]
        (path / "acheng.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return path

    def test_static_import_requires_archive_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, "archive-only"):
            self.importer.import_batch(self.batch_dir(), "2026-08-24")

    def test_static_import_is_superseded_and_never_queueable(self):
        result = self.importer.import_batch(
            self.batch_dir(), "2026-08-24", legacy_archive=True
        )
        self.assertEqual(result, {"personas": 1, "drafts": 10, "inserted": 10, "updated": 0})
        with self.app.db() as conn:
            rows = conn.execute(
                "SELECT status,source,notes FROM post_candidates ORDER BY id"
            ).fetchall()
        self.assertTrue(all(row["status"] == "superseded" for row in rows))
        self.assertTrue(all(row["source"].startswith("legacy_initial_batch:") for row in rows))
        self.assertTrue(all(json.loads(row["notes"])["publishable"] is False for row in rows))

    def test_superseder_leaves_published_and_nonlegacy_rows_untouched(self):
        self.app.init_db()
        now = int(time.time())
        with self.app.db() as conn:
            persona_id = conn.execute("SELECT id FROM personas WHERE slug='acheng'").fetchone()[0]
            for status, source in (
                ("needs_review", "initial_batch:2026-08-24:news-01"),
                ("published", "initial_batch:2026-08-24:news-02"),
                ("needs_review", "persona_editorial:1"),
            ):
                conn.execute(
                    """INSERT INTO post_candidates(
                        persona_id,context_date,title,body,status,source,notes,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (persona_id, "2026-08-24", source, "正文", status, source, "{}", now, now),
                )

        self.assertEqual(
            self.superseder.supersede_initial_batch_drafts(),
            {"matched": 1, "superseded": 0, "applied": False},
        )
        self.assertEqual(
            self.superseder.supersede_initial_batch_drafts(apply=True),
            {"matched": 1, "superseded": 1, "applied": True},
        )
        with self.app.db() as conn:
            rows = conn.execute(
                "SELECT source,status FROM post_candidates ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(row["source"], row["status"]) for row in rows],
            [
                ("initial_batch:2026-08-24:news-01", "superseded"),
                ("initial_batch:2026-08-24:news-02", "published"),
                ("persona_editorial:1", "needs_review"),
            ],
        )
