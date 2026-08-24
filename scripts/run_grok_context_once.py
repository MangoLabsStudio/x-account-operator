#!/usr/bin/env python3
import concurrent.futures
import getpass
import json
import sys
import urllib.request
from pathlib import Path


BASE = "https://www.micuapi.ai/v1/responses"


def research(item, key):
    payload = {
        "model": "grok-4.6",
        "input": item["prompt"] + "\n\n请用中文输出，控制在1200字以内。",
        "tools": [
            {"type": "x_search", "from_date": "2026-08-10T00:00:00Z"},
            {"type": "web_search"},
        ],
        "max_output_tokens": 4000,
    }
    request = urllib.request.Request(
        BASE,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)

    text = []
    citations = []
    for output in body.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text":
                text.append(content.get("text", ""))
            for annotation in content.get("annotations", []):
                url = annotation.get("url") or annotation.get("url_citation", {}).get("url")
                if url and url not in citations:
                    citations.append(url)
    return {
        "id": item["id"],
        "personas": item["personas"],
        "text": "\n".join(text).strip(),
        "citations": citations,
        "response_id": body.get("id"),
        "model": body.get("model", "grok-4.6"),
    }


def main():
    root = Path(__file__).resolve().parents[1]
    prompts = json.loads((root / "generated/grok-context-prompts-2026-08-24.json").read_text())
    key = getpass.getpass("Grok key: ")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(research, item, key): item["id"] for item in prompts}
        for job in concurrent.futures.as_completed(jobs):
            result = job.result()
            results.append(result)
            print(f"done {result['id']} chars={len(result['text'])} citations={len(result['citations'])}")
    results.sort(key=lambda row: [item["id"] for item in prompts].index(row["id"]))
    output = root / "generated/grok-context-2026-08-24.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
