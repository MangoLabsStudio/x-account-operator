#!/usr/bin/env python3
"""Retire legacy static drafts without deleting audit history or published rows."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def supersede_initial_batch_drafts(*, apply: bool = False):
    """Return or apply the exact legacy rows eligible for retirement."""
    app.init_db()
    with app.db() as conn:
        matched = conn.execute(
            """SELECT COUNT(*) FROM post_candidates
               WHERE status='needs_review' AND source LIKE 'initial_batch:%'"""
        ).fetchone()[0]
        if apply and matched:
            conn.execute(
                """UPDATE post_candidates
                   SET status='superseded',updated_at=?
                   WHERE status='needs_review' AND source LIKE 'initial_batch:%'""",
                (int(time.time()),),
            )
    return {"matched": matched, "superseded": matched if apply else 0, "applied": apply}


def main():
    parser = argparse.ArgumentParser(
        description="Supersede only unpublished legacy initial-batch drafts; default is dry-run."
    )
    parser.add_argument("--apply", action="store_true", help="perform the status update")
    args = parser.parse_args()
    print(json.dumps(supersede_initial_batch_drafts(apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
