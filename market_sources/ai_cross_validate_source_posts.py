from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_sources.cross_validate_source_posts import (
    CAUSAL_PATTERN,
    EVENT_PATTERN,
    NOISE_PATTERN,
    OPINION_PATTERN,
    PERSONAL_TRADE_PATTERN,
    PROMO_PATTERN,
    TRUNCATED_PATTERN,
    DisjointSet,
    _as_datetime,
    _clean_opinion_text,
    _engagement,
    similarity,
    tokens,
)

AI_ANCHORS = {
    "openai": re.compile(r"\bopenai\b|\bchatgpt\b|\bgpt(?:-[0-9a-z.]+)?\b", re.I),
    "anthropic": re.compile(r"\banthropic\b|\bclaude(?:\s+code)?\b", re.I),
    "google_deepmind": re.compile(r"\bgoogle\s+deepmind\b|\bdeepmind\b|\bgemini\b", re.I),
    "xai": re.compile(r"\bxai\b|\bgrok\b", re.I),
    "meta_ai": re.compile(r"\bmeta\s+ai\b|\bllama(?:\s*[0-9.]+)?\b", re.I),
    "deepseek": re.compile(r"\bdeepseek\b", re.I),
    "qwen": re.compile(r"\bqwen\b|通义千问", re.I),
    "mistral": re.compile(r"\bmistral\b", re.I),
    "cohere": re.compile(r"\bcohere\b", re.I),
    "kimi": re.compile(r"\bmoonshot\b|\bkimi\b|月之暗面", re.I),
    "zhipu": re.compile(r"\bzhipu\b|\bglm(?:-[0-9a-z.]+)?\b|智谱", re.I),
    "hugging_face": re.compile(r"\bhugging\s*face\b|\bhuggingface\b", re.I),
    "ai_coding": re.compile(r"\b(?:cursor|windsurf|github\s+copilot|copilot|codex|opencode|aider)\b|\bai\s+coding\b|\bvibe\s+coding\b|AI 编程|代码助手", re.I),
    "ai_agents": re.compile(r"\b(?:ai\s+agents?|agentic\s+(?:ai|systems?|workflows?))\b|智能体", re.I),
    "llm": re.compile(r"\b(?:llm|large\s+language\s+models?|foundation\s+models?)\b|大模型|基础模型", re.I),
    "ai_infra": re.compile(r"\b(?:nvidia|cuda|gpu(?:s)?|inference|training\s+cluster|ai\s+chips?)\b|算力|推理(?:成本|芯片|集群)?|训练集群|AI 芯片", re.I),
    "open_source_ai": re.compile(r"\b(?:open[ -]?source\s+(?:ai|model|llm)|open\s+weights?)\b|开源(?:模型|大模型|权重)", re.I),
}

ATTENTION_ENTITIES = (
    ("openai", "OpenAI", AI_ANCHORS["openai"]),
    ("anthropic", "Anthropic / Claude", AI_ANCHORS["anthropic"]),
    ("google_deepmind", "Google DeepMind / Gemini", AI_ANCHORS["google_deepmind"]),
    ("xai", "xAI / Grok", AI_ANCHORS["xai"]),
    ("meta_ai", "Meta AI / Llama", AI_ANCHORS["meta_ai"]),
    ("deepseek", "DeepSeek", AI_ANCHORS["deepseek"]),
    ("qwen", "Qwen", AI_ANCHORS["qwen"]),
    ("mistral", "Mistral", AI_ANCHORS["mistral"]),
    ("cohere", "Cohere", AI_ANCHORS["cohere"]),
    ("kimi", "Kimi / Moonshot", AI_ANCHORS["kimi"]),
    ("zhipu", "Zhipu / GLM", AI_ANCHORS["zhipu"]),
    ("hugging_face", "Hugging Face", AI_ANCHORS["hugging_face"]),
    ("ai_coding", "AI Coding", AI_ANCHORS["ai_coding"]),
    ("ai_agents", "AI Agents", AI_ANCHORS["ai_agents"]),
    ("llm", "LLM / Foundation Models", AI_ANCHORS["llm"]),
    ("ai_infra", "AI Infra", AI_ANCHORS["ai_infra"]),
    ("open_source_ai", "Open-source AI", AI_ANCHORS["open_source_ai"]),
)
ENTITY_PATTERN_BY_KEY = {key: pattern for key, _, pattern in ATTENTION_ENTITIES}

DISCUSSION_MECHANISMS = (
    ("release", "发布与能力变化", re.compile(r"\b(?:release[ds]?|launch(?:ed|es|ing)?|ship(?:ped|ping)?|roll(?:ed|out)|introduc(?:e|ed|es))\b|发布|上线|推出|开放", re.I)),
    ("benchmark", "Benchmark 与性能", re.compile(r"\b(?:benchmark|eval(?:uation)?|swe-bench|arena|score|latency|throughput)\b|评测|基准|跑分|性能|延迟|吞吐", re.I)),
    ("open_source", "开源与权重", re.compile(r"\b(?:open[ -]?source|open\s+weights?|weights?\s+(?:are|is)\s+open)\b|开源|开放权重", re.I)),
    ("adoption", "使用与商业化", re.compile(r"\b(?:adoption|users?|usage|retention|enterprise|customer|workflow|distribution)\b|用户|使用量|留存|企业客户|工作流|分发", re.I)),
    ("pricing", "定价与成本", re.compile(r"\b(?:pricing|price|subscription|api\s+cost|inference\s+cost|margin)\b|定价|订阅|API 成本|推理成本|毛利", re.I)),
    ("funding", "融资与组织", re.compile(r"\b(?:funding|raise[ds]?|valuation|acqui(?:re|red|sition)|hiring)\b|融资|估值|收购|招聘", re.I)),
    ("safety", "安全与治理", re.compile(r"\b(?:safety|alignment|security|policy|copyright|regulat(?:ion|or))\b|安全|对齐|版权|监管|治理", re.I)),
    ("infra", "算力与基础设施", re.compile(r"\b(?:gpu|cuda|chip|compute|datacenter|data\s+center|inference|training)\b|GPU|芯片|算力|数据中心|推理|训练", re.I)),
)

OPINION_MIN_SCORE = 11
OPINION_LIMIT = 200
ATTENTION_SAMPLE_LIMIT = 3
ATTENTION_MIN_AUTHORS = 4
DISCUSSION_HOT_MIN_AUTHORS = 4
VERIFIABLE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?(?:%|x|k|m|b)?\b|\b(?:model|release|benchmark|api|pricing|"
    r"funding|valuation|open[ -]?source|weights?|gpu|inference|training|enterprise)\b|"
    r"模型|发布|评测|API|定价|融资|估值|开源|权重|GPU|推理|训练|企业",
    re.I,
)


def _ai_tags(text: str) -> list[str]:
    return [f"ai:{key}" for key, pattern in AI_ANCHORS.items() if pattern.search(text)]


def _rejected_opinion(text: str, row: dict, score: int, tags: list[str], rejection: str) -> dict:
    return {
        "text": text,
        "created_at": row["created_at"],
        "source_lists": row["source_lists"],
        "quality_score": score,
        "tags": tags,
        "rejection": rejection,
        "topic_domain": "ai",
    }


def evaluate_ai_opinion(row: dict) -> dict:
    text = _clean_opinion_text(row["text"])
    item_tokens = tokens(text)
    tags = _ai_tags(text)
    if not tags:
        return _rejected_opinion(text, row, 0, [], "non_ai")
    if PERSONAL_TRADE_PATTERN.search(text):
        return _rejected_opinion(text, row, 0, tags, "personal_trade_or_pnl")
    if PROMO_PATTERN.search(text):
        return _rejected_opinion(text, row, 0, tags, "promotion_or_shill")
    if NOISE_PATTERN.search(text):
        return _rejected_opinion(text, row, 0, tags, "noise")
    if not 80 <= len(text) <= 1400 or len(item_tokens) < 8:
        return _rejected_opinion(text, row, 0, tags, "insufficient_context")

    causal_hits = len(CAUSAL_PATTERN.findall(text))
    verifiable_hits = len(VERIFIABLE_PATTERN.findall(text))
    length_score = 2 if len(text) < 160 else 4 if len(text) <= 520 else 3
    quality_score = min(verifiable_hits, 3) * 3 + min(causal_hits, 3) * 3 + min(len(tags), 2) * 2 + length_score
    if row.get("is_reply"):
        quality_score -= 7
        tags.append("penalty:reply")
    if TRUNCATED_PATTERN.search(text):
        quality_score -= 9
        tags.append("penalty:truncated")
    if not causal_hits and not OPINION_PATTERN.search(text):
        return _rejected_opinion(text, row, quality_score, tags, "no_reusable_reasoning")
    if quality_score < OPINION_MIN_SCORE:
        return _rejected_opinion(text, row, quality_score, tags, "below_quality_threshold")
    return {
        "status": "opinion_source",
        "source_ref": row.get("post_id", ""),
        "handle": row.get("handle", ""),
        "url": row.get("url", ""),
        "text": text,
        "created_at": row["created_at"],
        "source_lists": row["source_lists"],
        "score": quality_score,
        "quality_score": quality_score,
        "tags": tags,
        "rejection": None,
        "topic_domain": "ai",
        "reuse_rule": "Extract only the AI viewpoint and rebuild it with current evidence; never reuse author identity or personal experience.",
    }


def build_ai_opinion_corpus(rows: list[dict], limit: int = OPINION_LIMIT) -> tuple[list[dict], dict[str, int]]:
    cards, rejected, seen = [], defaultdict(int), set()
    for row in rows:
        card = evaluate_ai_opinion(row)
        if card["rejection"]:
            rejected[card["rejection"]] += 1
            continue
        signature = " ".join(sorted(tokens(card["text"])))
        if signature in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(signature)
        cards.append(card)
    cards.sort(key=lambda card: (-card["quality_score"], card["created_at"]), reverse=False)
    return cards[:limit], dict(sorted(rejected.items()))


def build_ai_cards(rows: list[dict]) -> list[dict]:
    prepared = [
        (row, tokens(row["text"]))
        for row in rows
        if _ai_tags(row["text"])
        and not row.get("is_reply")
        and len(row["text"]) >= 24
        and len(tokens(row["text"])) >= 4
        and not NOISE_PATTERN.search(row["text"])
    ]
    usable, token_sets = [item[0] for item in prepared], [item[1] for item in prepared]
    inverted: dict[str, list[int]] = defaultdict(list)
    for index, item_tokens in enumerate(token_sets):
        for token in item_tokens:
            inverted[token].append(index)
    pairs = set()
    for indices in inverted.values():
        if 2 <= len(indices) <= 80:
            pairs.update(
                (left, right)
                for position, left in enumerate(indices)
                for right in indices[position + 1:]
                if usable[left].get("author_id") != usable[right].get("author_id")
            )
    groups = DisjointSet(len(usable))
    for left, right in pairs:
        score = similarity(token_sets[left], token_sets[right])
        if score >= 0.52:
            groups.union(left, right)
    clustered: dict[int, list[tuple[dict, set[str]]]] = defaultdict(list)
    for index, row in enumerate(usable):
        clustered[groups.find(index)].append((row, token_sets[index]))
    cards = []
    for members in clustered.values():
        authors = {row.get("author_id") for row, _ in members}
        if len(authors) < 2 or not any(EVENT_PATTERN.search(row["text"]) for row, _ in members):
            continue
        evidence = sorted((row for row, _ in members), key=lambda row: row["created_at"], reverse=True)
        representative = max(evidence, key=lambda row: len(tokens(row["text"])))
        cards.append({
            "status": "corroborated_candidate" if len(authors) >= 3 else "two_source_candidate",
            "topic_domain": "ai",
            "author_count": len(authors),
            "post_count": len(evidence),
            "source_lists": sorted({source for row in evidence for source in row["source_lists"]}),
            "representative_text": representative["text"],
            "representative_source_ref": representative.get("post_id", ""),
            "representative_handle": representative.get("handle", ""),
            "representative_url": representative.get("url", ""),
            "latest_at": evidence[0]["created_at"],
            "evidence": [{
                "source_ref": row.get("post_id", ""), "handle": row.get("handle", ""),
                "url": row.get("url", ""), "text": row["text"], "created_at": row["created_at"],
                "source_lists": row["source_lists"],
            } for row in evidence[:12]],
            "score": len(authors) * 4 + min(len(evidence), 12),
        })
    return sorted(cards, key=lambda card: (-card["score"], card["latest_at"]))


def _entities(text: str) -> list[tuple[str, str]]:
    return [(key, title) for key, title, pattern in ATTENTION_ENTITIES if pattern.search(text)]


def _topic_summary(topic: dict, now: datetime) -> dict:
    rows = sorted(topic["rows"], key=lambda row: row["created_at"], reverse=True)
    author_keys = {str(row.get("author_id") or row.get("handle") or row.get("post_id")) for row in rows}
    recent = [row for row in rows if _as_datetime(row["created_at"]) >= now - timedelta(hours=6)]
    recent_authors = {str(row.get("author_id") or row.get("handle") or row.get("post_id")) for row in recent}
    engagements = [_engagement(row.get("metrics")) for row in rows]
    return {
        **{key: value for key, value in topic.items() if key != "rows"},
        "topic_domain": "ai",
        "unique_authors": len(author_keys), "post_count": len(rows), "latest_at": rows[0]["created_at"],
        "recent_6h_authors": len(recent_authors), "recent_6h_posts": len(recent),
        "source_lists": sorted({source for row in rows for source in row.get("source_lists", [])}),
        "cross_list_count": len({source for row in rows for source in row.get("source_lists", [])}),
        "engagement_total": sum(value for value, _ in engagements),
        "engagement_coverage": {"posts_with_metrics": sum(1 for _, available in engagements if available), "post_count": len(rows)},
        "sample_refs": [row.get("post_id", "") for row in rows[:ATTENTION_SAMPLE_LIMIT]],
        "sample_posts": [{"source_ref": row.get("post_id", ""), "created_at": row["created_at"], "text": row["text"]} for row in rows[:ATTENTION_SAMPLE_LIMIT]],
    }


def build_ai_attention_topics(rows: list[dict], now: datetime | None = None) -> dict[str, list[dict]]:
    now = (now or max((_as_datetime(row["created_at"]) for row in rows), default=datetime.now(timezone.utc))).astimezone(timezone.utc)
    grouped: dict[str, dict] = {}
    for row in rows:
        if _as_datetime(row["created_at"]) < now - timedelta(hours=24) or row.get("is_reply") or row.get("is_retweet") or NOISE_PATTERN.search(row["text"]):
            continue
        for key, title in _entities(row["text"]):
            grouped.setdefault(key, {"key": key, "title": title, "rows": []})["rows"].append(row)
    topics = [_topic_summary(topic, now) for topic in grouped.values()]
    topics.sort(key=lambda topic: (-topic["unique_authors"], -topic["post_count"], -topic["recent_6h_authors"], -topic["engagement_total"], topic["latest_at"]))
    return {"hot": [topic for topic in topics if topic["unique_authors"] >= ATTENTION_MIN_AUTHORS], "niche": [topic for topic in topics if topic["unique_authors"] < ATTENTION_MIN_AUTHORS]}


def build_ai_discussion_topics(rows: list[dict], now: datetime | None = None) -> dict[str, list[dict]]:
    now = (now or max((_as_datetime(row["created_at"]) for row in rows), default=datetime.now(timezone.utc))).astimezone(timezone.utc)
    grouped: dict[str, dict] = {}
    for row in rows:
        if _as_datetime(row["created_at"]) < now - timedelta(hours=24) or row.get("is_reply") or row.get("is_retweet") or NOISE_PATTERN.search(row["text"]) or PROMO_PATTERN.search(row["text"]) or PERSONAL_TRADE_PATTERN.search(row["text"]):
            continue
        sentences = [part for part in re.split(r"[.!?。！？\n]+", row["text"]) if part.strip()]
        for entity_key, entity_title in _entities(row["text"]):
            entity_pattern = ENTITY_PATTERN_BY_KEY[entity_key]
            for mechanism_key, mechanism_title, mechanism_pattern in DISCUSSION_MECHANISMS:
                if any(entity_pattern.search(sentence) and mechanism_pattern.search(sentence) for sentence in sentences):
                    key = f"{entity_key}:{mechanism_key}"
                    grouped.setdefault(key, {"key": key, "title": f"{entity_title}｜{mechanism_title}", "parent": {"key": entity_key, "title": entity_title}, "mechanism": {"key": mechanism_key, "title": mechanism_title}, "rows": []})["rows"].append(row)
    topics = [_topic_summary(topic, now) for topic in grouped.values()]
    topics.sort(key=lambda topic: (-topic["unique_authors"], -topic["post_count"], -topic["recent_6h_authors"], -topic["engagement_total"], topic["latest_at"]))
    return {"hot": [topic for topic in topics if topic["unique_authors"] >= DISCUSSION_HOT_MIN_AUTHORS], "niche": [topic for topic in topics if topic["unique_authors"] < DISCUSSION_HOT_MIN_AUTHORS]}


def _write_markdown(output_dir: Path, name: str, title: str, topics: list[dict]) -> None:
    lines = [f"# {title}", ""]
    for index, topic in enumerate(topics, 1):
        lines.extend([f"## {index}. {topic['title']}｜{topic['unique_authors']} 位作者｜{topic['post_count']} 条原帖", *[f"- {sample['created_at']}：{sample['text']}" for sample in topic["sample_posts"]], ""])
    (output_dir / name).write_text("\n".join(lines), encoding="utf-8")


def cross_validate_ai(db_path: Path, output_dir: Path, *, run_id: str, hours: int = 30) -> dict:
    run_id = run_id.strip()
    if not run_id:
        raise ValueError("run_id is required")
    generated_at = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT p.* FROM source_posts p JOIN source_post_runs r ON r.post_id=p.post_id WHERE r.run_id=? ORDER BY p.created_at DESC""", (run_id,)).fetchall()
    posts = [{**dict(row), "is_reply": bool(row["is_reply"]), "is_retweet": bool(row["is_retweet"]), "source_lists": json.loads(row["source_lists"]), "metrics": json.loads(row["metrics"]) if row["metrics"] else None} for row in rows]
    cards = build_ai_cards(posts)
    opinions, rejections = build_ai_opinion_corpus(posts)
    attention, discussion = build_ai_attention_topics(posts, generated_at), build_ai_discussion_topics(posts, generated_at)
    output_dir.mkdir(parents=True, exist_ok=True)
    shared = {"run_id": run_id, "generated_at": generated_at.isoformat(), "topic_domain": "ai", "source_post_count": len(posts)}
    fact_payload = {**shared, "since": (generated_at - timedelta(hours=hours)).isoformat(), "rule": "Similar original AI posts from at least two distinct authors; multi-source mention is not final factual confirmation.", "card_count": len(cards), "cards": cards, "attention_topics": attention, "discussion_topics": discussion}
    (output_dir / "fact_cards.json").write_text(json.dumps(fact_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "fact_cards.md").write_text("\n".join(["# 每日 AI 多源事实候选", "", *[f"## {index}. {card['status']}｜{card['author_count']} 位作者\n{card['representative_text']}\n" for index, card in enumerate(cards, 1)]]), encoding="utf-8")
    opinion_payload = {**shared, "opinion_count": len(opinions), "opinions": opinions, "opinion_filter": {"requires": ["ai_anchor", "verifiable_information_or_causal_reasoning"], "excludes": ["promotion_or_shill", "personal_trade_or_pnl", "non_ai"]}, "opinion_rejection_counts": rejections}
    (output_dir / "opinion_cards.json").write_text(json.dumps(opinion_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "opinion_cards.md").write_text("\n".join(["# 每日 AI 高质量观点候选", "", *[f"## {index}. 评分 {card['score']}\n{card['text']}\n" for index, card in enumerate(opinions, 1)]]), encoding="utf-8")
    attention_payload = {**shared, "window_start": (generated_at - timedelta(hours=24)).isoformat(), "window_end": generated_at.isoformat(), "hours": 24, "topics": attention["hot"], "hot": attention["hot"], "niche": attention["niche"]}
    discussion_payload = {**shared, "window_start": (generated_at - timedelta(hours=24)).isoformat(), "window_end": generated_at.isoformat(), "hours": 24, "hot": discussion["hot"], "niche": discussion["niche"]}
    (output_dir / "attention_topics.json").write_text(json.dumps(attention_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "discussion_topics.json").write_text(json.dumps(discussion_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(output_dir, "attention_topics.md", "今日 AI 母池讨论热点", attention["hot"])
    _write_markdown(output_dir, "discussion_topics.md", "今日 AI 可写讨论议题", discussion["hot"])
    return {"run_id": run_id, "source_posts": len(posts), "fact_cards": len(cards), "opinion_cards": len(opinions), "attention_topics": len(attention["hot"]), "niche_topics": len(attention["niche"]), "discussion_topics": len(discussion["hot"]), "niche_discussion_topics": len(discussion["niche"]), "opinion_rejections": rejections, "topic_domain": "ai"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--hours", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(cross_validate_ai(args.db, args.output, run_id=args.run_id, hours=args.hours)))


if __name__ == "__main__":
    main()
