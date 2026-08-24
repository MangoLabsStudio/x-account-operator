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
            if "asset_id" in item and not isinstance(item["asset_id"], str):
                raise ValueError(f"{path.name} asset_id must be a string")
        batch[path.stem] = items
    return batch


def import_batch(batch_dir: Path, context_date: str, *, legacy_archive: bool = False):
    """Archive an old static batch without making it a queue candidate.

    Static seed drafts predate the editorial pipeline. They are retained only
    as traceable historical material and must never be able to enter the
    publish queue.
    """
    if not legacy_archive:
        raise ValueError(
            "static initial drafts are archive-only; pass legacy_archive=True explicitly"
        )
    batch = read_batch(batch_dir)
    app.init_db()
    inserted = 0
    updated = 0
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
            valid_asset_ids = {asset["id"] for asset in app.persona_assets(slug)}
            for item in items:
                asset_id = item.get("asset_id", "").strip()
                if asset_id and asset_id not in valid_asset_ids:
                    raise ValueError(f"{slug} has invalid asset_id: {asset_id}")
                source = f"legacy_initial_batch:{context_date}:{item['slot']}"
                existing = conn.execute(
                    "SELECT asset_id FROM post_candidates WHERE persona_id=? AND context_date=? AND source=?",
                    (personas[slug], context_date, source),
                ).fetchone()
                before = conn.total_changes
                conn.execute(
                    """INSERT INTO post_candidates(
                        persona_id,context_date,title,body,status,source,asset_id,notes,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(persona_id,context_date,source) DO NOTHING""",
                    (
                        personas[slug], context_date, item["topic"].strip(), item["body"].strip(),
                        "superseded", source, asset_id,
                        json.dumps({
                            "batch": batch_dir.name,
                            "kind": item["kind"],
                            "sources": item.get("sources", []),
                            "legacy_import": True,
                            "publishable": False,
                            "archived_at_import": True,
                        }, ensure_ascii=False),
                        now, now,
                    ),
                )
                changed = conn.total_changes - before
                if existing is None:
                    inserted += changed
                else:
                    updated += changed
    return {
        "personas": len(batch),
        "drafts": sum(map(len, batch.values())),
        "inserted": inserted,
        "updated": updated,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Archive a legacy static persona batch. It can never enter the publish queue."
    )
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--legacy-archive",
        action="store_true",
        help="required acknowledgement that static drafts are historical archive material only",
    )
    args = parser.parse_args()
    print(json.dumps(
        import_batch(args.batch_dir, args.date, legacy_archive=args.legacy_archive),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
