#!/usr/bin/env python3
import argparse
import getpass
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from app import (
    bounded_selected_topics,
    init_db,
    record_topic_claims,
    recent_topic_claims,
    topic_selection_policy,
)


API = "https://www.micuapi.ai/v1/responses"


def request_grok(key, prompt):
    request = urllib.request.Request(
        API,
        data=json.dumps(
            {
                "model": "grok-4.6",
                "input": prompt,
                "max_output_tokens": 7000,
            },
            ensure_ascii=False,
        ).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=360) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    parts = []
    for item in body.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    text = "\n".join(parts).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def compact_topic(topic):
    compact = {
        key: topic[key]
        for key in ("key", "title", "unique_authors", "post_count", "latest_at")
        if key in topic
    }
    compact["sample_posts"] = [
        {"created_at": item.get("created_at"), "text": str(item.get("text", ""))[:400]}
        for item in topic.get("sample_posts", [])[:2]
        if isinstance(item, dict)
    ]
    return compact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    init_db()
    with sqlite3.connect(root / "data/xops.db") as db:
        db.row_factory = sqlite3.Row
        if args.date:
            run = db.execute(
                "SELECT * FROM daily_context_runs WHERE context_date=?", (args.date,)
            ).fetchone()
        else:
            run = db.execute(
                "SELECT * FROM daily_context_runs ORDER BY context_date DESC LIMIT 1"
            ).fetchone()
    if not run:
        raise RuntimeError("no daily context run")

    cards = json.loads(run["raw_cards"])
    claim_history = [
        item for item in recent_topic_claims()
        if item.get("context_date") != run["context_date"]
    ]
    contexts_path = root / f"generated/persona-hot-contexts-{run['context_date']}.partial.json"
    contexts = json.loads(contexts_path.read_text(encoding="utf-8")) if contexts_path.exists() else []
    input_data = {
        "date": run["context_date"],
        "selection_policy": topic_selection_policy(),
        "claim_history": claim_history,
        "discussion_topics": [compact_topic(item) for item in cards.get("discussion_topics", [])],
        "opinion_cards": [
            {
                **{key: item[key] for key in ("source_ref", "text", "score", "created_at", "reuse_rule") if key in item},
                "source_key": f"opinion:{item['source_ref']}",
            }
            for item in sorted(
                cards.get("opinion_cards", []), key=lambda item: -int(item.get("score") or 0)
            )[:30]
        ],
        "live_research_contexts": [
            {
                "topic_key": item.get("topic_key"),
                "text": str(item.get("text", ""))[:1600],
                "citations": item.get("citations", [])[:8],
            }
            for item in contexts
            if isinstance(item, dict)
        ],
    }
    prompt = f"""你是 Crypto 内容总编。重新过滤今天的事实、观点和选题。

输入：{json.dumps(input_data, ensure_ascii=False)}

严格执行 selection_policy，并把 claim_history 当成整个团队已经讲过的内容。尤其注意：
- 热点只是研究入口，不等于值得写。
- 去重单位是核心主张，不是事件。同一热点允许拆成互不重叠的研究、机会和评论角度。
- 研究结论与历史语义相同，即使数字创新高也要拒绝；只有新证据真正改变旧结论才可重写。
- 圈内常识、百科解释、答案显而易见的问题全部拒绝。
- 评论题可以写人物评价、交易哲学、财富观和市场情绪，但必须有当天语境、明确立场，不能写永远正确的空话。
- 通常保留 8–15 个互不重复的核心主张；不是给十个人设硬凑十题。
- live_research_contexts 中“不能确认”的内容不能升级为事实；观点必须继续标为观点。
- 每个入选题必须让读者获得此前不知道的关系、计算、冲突、机会或结论变化。

只输出 JSON：
{{
  "filtered_facts":[{{"fact":"","time":"","primary_sources":[""],"why_it_matters":""}}],
  "filtered_opinions":[{{"view":"","support":"","counterpoint":"","source_topic_keys":[""]}}],
  "selected_topics":[{{
    "claim_key":"稳定英文短标识","subject":"","title":"直接写出新结论或冲突，不写泛问句",
    "core_claim":"","content_type":"opportunity|editorial|research","kind":"",
    "source_topic_keys":[""],"fact_basis":"","opinion_basis":"","material_delta":"相对历史新增什么",
    "audience_value":"会改变什么判断或行动","why_now":"","persona_fit":[""]
  }}],
  "rejected_topics":[{{"title":"","core_claim":"","reason_code":"policy 中的 code","reason":"","source_topic_keys":[""]}}]
}}
"""
    if args.input:
        result = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        key = getpass.getpass("Grok key: ")
        result = request_grok(key, prompt)
        del key
    result.setdefault("filtered_facts", cards.get("screened_facts", []))
    result.setdefault("filtered_opinions", cards.get("screened_opinions", []))

    bounded_cards = {
        "discussion_topics": cards.get("discussion_topics", []),
        "fact_cards": cards.get("fact_cards", []),
        "opinion_cards": cards.get("opinion_cards", []),
        "topic_selection_policy": topic_selection_policy(),
        "claim_history": claim_history,
    }
    selected, rejected = bounded_selected_topics(result, bounded_cards)
    result["selected_topics"] = selected
    result["rejected_topics"] = rejected

    cards["topic_selection_policy"] = topic_selection_policy()
    cards["screened_facts"] = result.get("filtered_facts", [])
    cards["screened_opinions"] = result.get("filtered_opinions", [])
    cards["selected_topics"] = selected
    cards["rejected_topics"] = result["rejected_topics"]
    cards["question_candidates"] = {
        "opportunity": cards.get("opportunity_questions", []),
        "editorial": cards.get("editorial_questions", []),
        "research": cards.get("research_questions", []),
    }
    cards["opportunity_questions"] = [item for item in selected if item["content_type"] == "opportunity"]
    cards["editorial_questions"] = [item for item in selected if item["content_type"] == "editorial"]
    cards["research_questions"] = [item for item in selected if item["content_type"] == "research"]

    synthesis = json.loads(run["synthesis"])
    synthesis["selected_topics"] = selected
    synthesis["rejected_topics"] = result["rejected_topics"]
    synthesis["opportunity_questions"] = cards["opportunity_questions"]
    synthesis["editorial_questions"] = cards["editorial_questions"]
    synthesis["research_questions"] = cards["research_questions"]
    with sqlite3.connect(root / "data/xops.db") as db:
        db.execute(
            "UPDATE daily_context_runs SET raw_cards=?,synthesis=? WHERE id=?",
            (
                json.dumps(cards, ensure_ascii=False),
                json.dumps(synthesis, ensure_ascii=False),
                run["id"],
            ),
        )
    record_topic_claims(selected, run["context_date"], "manual_rescreen")
    output = root / f"generated/screened-topic-slate-{run['context_date']}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
