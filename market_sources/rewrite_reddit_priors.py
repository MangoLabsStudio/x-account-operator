from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

import httpx

from app import chat_completion_json, editorial_provider_config, gemini_request_key


def source_text(row: sqlite3.Row) -> str:
    return "\n\n".join(part for part in (row["title"].strip(), row["body"].strip()) if part)


def prompt_for(source: str, instruction: str = "") -> str:
    prompt = f"""把下面的 Reddit 内容改写成可以直接发布的自然中文。只输出 JSON：
{{"text":"完整中文成稿","source_emotion":"原文主导情绪","target_emotion":"成稿主导情绪"}}

硬性要求：
1. 这不是摘要。原文里的事实、数字、时间、人物关系、前提、疑问、转折、例子和不确定性都不能减少。
2. 原文有愤怒、恐惧、荒诞、兴奋、嘲讽、焦虑或好奇时，中文必须保持同等或更强的可感知情绪；不能洗成新闻通稿、说明书或四平八稳的分析。
3. 允许重排句序、压缩重复表达和改成中文互联网口语，但不能改变结论、加强事实确定性或添加原文没有的信息。
4. 不得出现“原作者、博主、这篇帖子、Reddit 用户、外媒称”等来源转述身份。直接把内容说出来。
5. 原文只有标题时，只改写标题表达的内容，不得自行补背景。原文是提问时，保留问题的冲突感和开放性。
6. 不设固定字数；信息完整优先。不要写标题、标签、来源、风险提示或免责声明。
7. 医疗、法律和金融术语必须准确，不能把医疗诱导昏迷写成医院致害，也不能把“安全”强化成“万无一失”。
8. 原文里的 EDIT、更正和补充也属于信息；既有错误数字又有更正数字时，必须写清更正关系。
9. 不写“他/她”这类翻译腔；已知姓名就用姓名，不知道性别就用“当事人”。
10. 目标读者是中国财经用户。首次出现中国普通投资者不熟悉的术语时，用一句短中文解释；不得虚构中国案例、人民币价格或本地政策。

原文：
{source}"""
    if instruction:
        prompt += f"\n\n上一稿审查未通过，必须修复：{instruction}"
    return prompt


def critic_prompt(source: str, draft: str) -> str:
    return f"""逐项对照原文和中文改写，只输出 JSON：
{{"verdict":"PASS或REWRITE","missing_information":["被删减或被改错的信息"],"emotion_loss":["被削弱的情绪或冲突"],"instruction":"定向重写要求"}}

PASS 必须同时满足：原文全部有效信息均保留；事实确定性没有升级；没有新增事实；主导情绪和冲突强度没有降低；正文没有任何来源转述腔。只要一项不满足就返回 REWRITE。

原文：
{source}

中文改写：
{draft}"""


async def complete(config: dict, prompt: str) -> dict:
    async with httpx.AsyncClient(timeout=240) as client:
        for attempt in range(3):
            async with gemini_request_key(config) as key:
                response = await client.post(
                    config["base_url"] + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": config["model"],
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.75,
                        "max_tokens": 8000,
                    },
                )
            if response.status_code in {429, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            try:
                return chat_completion_json(response.json())
            except (ValueError, KeyError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Gemini 未返回有效 JSON")


async def rewrite_one(config: dict, row: sqlite3.Row) -> dict:
    source = source_text(row)
    instruction = ""
    for _ in range(3):
        result = await complete(config, prompt_for(source, instruction))
        text = str(result.get("text") or "")
        text = text.replace("我看到网上有人说", "我在网上看到一种说法")
        text = text.replace("我也看到有人说", "我也看到另一种说法")
        result["text"] = text
        critic = await complete(config, critic_prompt(source, text))
        forbidden = [term for term in (
            "原作者", "博主", "这篇帖子", "Reddit 用户", "外媒称", "他/她"
        ) if term in text]
        if critic.get("verdict") == "PASS" and not forbidden:
            break
        instruction = str(critic.get("instruction") or critic)
        if forbidden:
            instruction += f"；删除这些转述腔或翻译腔：{','.join(forbidden)}"
    if critic.get("verdict") != "PASS" or forbidden:
        raise RuntimeError(f"Reddit 改写未通过硬约束：{row['external_id']}")
    return {
        "external_id": row["external_id"],
        "url": row["url"],
        "community": row["community"],
        "source": source,
        "text": str(result.get("text") or "").strip(),
        "information_units": result.get("information_units") or [],
        "source_emotion": result.get("source_emotion") or "",
        "target_emotion": result.get("target_emotion") or "",
        "critic": critic,
    }


async def rewrite_safe(config: dict, row: sqlite3.Row) -> dict:
    try:
        return {"item": await rewrite_one(config, row)}
    except Exception as error:
        return {"external_id": row["external_id"], "error": str(error)}


async def run(db_path: Path, partial_path: Path, model: str, refresh: set[str], selected_ids: set[str]):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT external_id,url,title,body,community
                 FROM viral_priors WHERE platform='reddit'
                 ORDER BY CAST(json_extract(metrics_json,'$.score') AS INTEGER) DESC"""
        ).fetchall()
    if selected_ids:
        rows = [row for row in rows if row["external_id"] in selected_ids]
    completed = {
        item["external_id"]: item
        for item in json.loads(partial_path.read_text())
    } if partial_path.exists() else {}
    pending = [
        row for row in rows
        if row["external_id"] not in completed or row["external_id"] in refresh
    ]
    config = editorial_provider_config("GEMINI") if pending else None
    if config:
        config["model"] = model
    tasks = [rewrite_safe(config, row) for row in pending]
    failures = []
    for task in asyncio.as_completed(tasks):
        result = await task
        if "error" in result:
            failures.append(result)
            continue
        item = result["item"]
        completed[item["external_id"]] = item
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path.write_text(json.dumps(list(completed.values()), ensure_ascii=False, indent=2) + "\n")
    results = []
    for row in rows:
        if row["external_id"] not in completed:
            continue
        item = completed[row["external_id"]]
        item["url"] = row["url"]
        results.append(item)
    return results, failures


def write_outputs(items: list[dict], output_dir: Path, run_date: str = "") -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"reddit-viral-rewrites-{run_date or date.today().isoformat()}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    blocks = []
    for index, item in enumerate(items, 1):
        status = item["critic"].get("verdict", "UNKNOWN")
        blocks.append(
            f"## {index}. {item['community']} · {status}\n\n"
            f"原文：\n\n{item['source']}\n\n"
            f"中文成稿：\n\n{item['text']}\n\n"
            f"[来源]({item['url']})"
        )
    md_path.write_text("\n\n---\n\n".join(blocks) + "\n")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/viral_priors.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("generated"))
    parser.add_argument("--model", default="gemini-2.5-pro")
    parser.add_argument("--refresh", default="")
    parser.add_argument("--ids", default="")
    parser.add_argument("--run-date", default="")
    args = parser.parse_args()
    run_date = args.run_date or date.today().isoformat()
    partial_path = args.output_dir / f"reddit-viral-rewrites-{run_date}.partial.json"
    items, failures = asyncio.run(run(
        args.db, partial_path, args.model,
        {item.strip() for item in args.refresh.split(",") if item.strip()},
        {item.strip() for item in args.ids.split(",") if item.strip()},
    ))
    json_path, md_path = write_outputs(items, args.output_dir, run_date)
    print(json.dumps({
        "count": len(items),
        "passed": sum(item["critic"].get("verdict") == "PASS" for item in items),
        "failed": failures,
        "json": str(json_path),
        "markdown": str(md_path),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
