from __future__ import annotations

import argparse
import json
from pathlib import Path

from .collect_big_source_posts import DEFAULT_ACCOUNTS_PATH, collect, twitter241_api_key
from .cross_validate_source_posts import cross_validate


def run_daily(
    *,
    db_path: Path,
    output_dir: Path,
    key: str | None = None,
    accounts_path: Path = DEFAULT_ACCOUNTS_PATH,
    hours: int = 30,
    workers: int = 8,
    resume_hours: int = 20,
) -> dict:
    collection = collect(
        accounts_path,
        db_path,
        output_dir,
        key=key,
        hours=hours,
        workers=workers,
        resume_hours=resume_hours,
    )
    run_id = str(collection.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("抓取结果缺少 run_id")
    validation = cross_validate(
        db_path,
        Path(collection.get("snapshot_dir", output_dir)),
        run_id=run_id,
        hours=hours,
    )
    return {"collection": collection, "validation": validation}


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect and validate the fixed X mother pool")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=30)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--resume-hours", type=int, default=20)
    args = parser.parse_args()
    result = run_daily(
        db_path=args.db,
        output_dir=args.output,
        key=twitter241_api_key(),
        hours=args.hours,
        workers=args.workers,
        resume_hours=args.resume_hours,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
