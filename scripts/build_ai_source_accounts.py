#!/usr/bin/env python3
"""Build the AI timeline account pool from the local AI influence shortlist."""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "tmp" / "ai_influence_landscape_20260804" / "social_selected.json"
DEFAULT_CRYPTO_ACCOUNTS = ROOT / "configs" / "content_source_accounts.json"
DEFAULT_OUTPUT = ROOT / "configs" / "ai_content_source_accounts.json"
BASE_URL = "https://twitter241.p.rapidapi.com"
HOST = "twitter241.p.rapidapi.com"
SOURCE_LIST = "ai_influence_landscape_20260804"


def twitter241_api_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "codex.twitter241.rapidapi", "-a", "TWITTER241_RAPIDAPI_KEY", "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("未配置 Twitter241 Keychain 凭据")


def handle_from_url(value: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.netloc.lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError(f"不是有效的 X 账号链接：{value}")
    handle = parsed.path.strip("/").split("/", 1)[0].lstrip("@")
    if not handle:
        raise ValueError(f"X 账号链接缺少 handle：{value}")
    return handle


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def user_id_from_payload(payload: dict) -> str:
    for item in _walk(payload):
        if not isinstance(item, dict):
            continue
        candidate = item.get("result") if isinstance(item.get("result"), dict) else item
        if candidate.get("__typename") == "User" and candidate.get("rest_id"):
            return str(candidate["rest_id"])
    raise RuntimeError("Twitter241 /user response has no user id")


def lookup_user_id(key: str, handle: str) -> str:
    request = Request(
        f"{BASE_URL}/user?{urlencode({'username': handle})}",
        headers={"x-rapidapi-host": HOST, "x-rapidapi-key": key},
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise TypeError("Twitter241 /user returned non-object JSON")
    return user_id_from_payload(payload)


def _accounts_by_handle(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"账号池不是列表：{path}")
    return {
        str(row.get("handle", "")).lstrip("@").lower(): row
        for row in rows
        if isinstance(row, dict) and row.get("user_id") and row.get("handle")
    }


def source_handles(path: Path) -> tuple[list[str], list[dict]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("AI influence source must be a list")
    handles: list[str] = []
    failures: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        try:
            handle = handle_from_url(row["x_url"])
        except (KeyError, TypeError, ValueError) as error:
            failures.append({"index": index, "x_url": row.get("x_url") if isinstance(row, dict) else None, "error": str(error)})
            continue
        key = handle.lower()
        if key not in seen:
            seen.add(key)
            handles.append(handle)
    return handles, failures


def build(
    source_path: Path = DEFAULT_SOURCE,
    crypto_accounts_path: Path = DEFAULT_CRYPTO_ACCOUNTS,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    workers: int = 6,
) -> dict:
    handles, failures = source_handles(source_path)
    cached = _accounts_by_handle(output_path)
    crypto = _accounts_by_handle(crypto_accounts_path)
    resolved: dict[str, dict] = {}
    pending: list[str] = []
    reused_existing = reused_crypto = 0

    for handle in handles:
        known = cached.get(handle.lower()) or crypto.get(handle.lower())
        if known:
            resolved[handle.lower()] = {"user_id": str(known["user_id"]), "handle": handle, "source_lists": [SOURCE_LIST]}
            if handle.lower() in cached:
                reused_existing += 1
            else:
                reused_crypto += 1
        else:
            pending.append(handle)

    looked_up = 0
    if pending:
        key = twitter241_api_key()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(lookup_user_id, key, handle): handle for handle in pending}
            for future in as_completed(futures):
                handle = futures[future]
                try:
                    resolved[handle.lower()] = {"user_id": future.result(), "handle": handle, "source_lists": [SOURCE_LIST]}
                    looked_up += 1
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, json.JSONDecodeError) as error:
                    failures.append({"handle": handle, "error": f"{type(error).__name__}: {str(error)[:300]}"})

    accounts: list[dict] = []
    seen_ids: set[str] = set()
    duplicate_user_ids = 0
    for handle in handles:
        account = resolved.get(handle.lower())
        if not account:
            continue
        if account["user_id"] in seen_ids:
            duplicate_user_ids += 1
            continue
        seen_ids.add(account["user_id"])
        accounts.append(account)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "source_handles": len(handles),
        "accounts_written": len(accounts),
        "reused_existing": reused_existing,
        "reused_crypto": reused_crypto,
        "looked_up": looked_up,
        "duplicate_user_ids": duplicate_user_ids,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--crypto-accounts", type=Path, default=DEFAULT_CRYPTO_ACCOUNTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.crypto_accounts, args.output, workers=args.workers), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
