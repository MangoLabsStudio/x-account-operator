#!/usr/bin/env python3
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


EXPECTED_SLOTS = {
    *(f"news-{index:02d}" for index in range(1, 4)),
    *(f"evergreen-{index:02d}" for index in range(1, 8)),
}


def read_batch(batch_dir: Path):
    files = sorted(batch_dir.glob("*.json"))
    if not files:
        raise ValueError("batch directory has no JSON files")
    batch = {}
    for path in files:
        items = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(items, list) or len(items) != 10:
            raise ValueError(f"{path.name} must contain exactly 10 drafts")
        slots = {str(item.get("slot", "")) for item in items if isinstance(item, dict)}
        if slots != EXPECTED_SLOTS:
            raise ValueError(f"{path.name} has invalid slots")
        for item in items:
            if item.get("kind") not in {"news", "evergreen"}:
                raise ValueError(f"{path.name} has invalid kind")
            if not str(item.get("topic", "")).strip() or not str(item.get("body", "")).strip():
                raise ValueError(f"{path.name} has an empty topic or body")
            if not isinstance(item.get("sources", []), list):
                raise ValueError(f"{path.name} sources must be an array")
        batch[path.stem] = items
    return batch


def import_batch(batch_dir: Path, context_date: str):
    batch = read_batch(batch_dir)
    app.init_db()
    inserted = 0
    now = int(time.time())
    with app.db() as conn:
        personas = {
            row["slug"]: row["id"]
            for row in conn.execute("SELECT id,slug FROM personas").fetchall()
        }
        missing = sorted(set(batch) - set(personas))
        if missing:
            raise ValueError(f"unknown personas: {', '.join(missing)}")
        for slug, items in batch.items():
            for item in items:
                before = conn.total_changes
                conn.execute(
                    """INSERT INTO post_candidates(
                        persona_id,context_date,title,body,status,source,asset_id,notes,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(persona_id,context_date,source) DO NOTHING""",
                    (
                        personas[slug], context_date, item["topic"].strip(), item["body"].strip(),
                        "needs_review", f"initial_batch:{context_date}:{item['slot']}", "",
                        json.dumps({
                            "batch": batch_dir.name,
                            "kind": item["kind"],
                            "sources": item.get("sources", []),
                            "published": False,
                        }, ensure_ascii=False),
                        now, now,
                    ),
                )
                inserted += conn.total_changes - before
    return {"personas": len(batch), "drafts": sum(map(len, batch.values())), "inserted": inserted}


def main():
    parser = argparse.ArgumentParser(description="Import a review-only initial persona draft batch.")
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    print(json.dumps(import_batch(args.batch_dir, args.date), ensure_ascii=False))


if __name__ == "__main__":
    main()
