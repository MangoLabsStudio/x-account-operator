"""Pure routing and deduplication for the standalone AI Radar demo."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FOCUS_ORDER = (
    "policy_safety_legal",
    "business_enterprise",
    "ai_for_science",
    "robotics_embodied",
    "chips_compute",
    "creative_multimodal",
    "research_open_source",
    "ai_coding",
    "agents_automation",
    "frontier_models",
)
PATTERNS = {
    "policy_safety_legal": r"\bregulations?\b|\bregulators?\b|\blegislation\b|\blawmakers?\b|\bnew law\b|\bai law\b|\blawsuits?\b|\bsue[sd]?\b|\bftc\b|attorneys? general|\bcourts?\b|\bcopyright\b|\bantitrust\b|\bexport controls?\b|\bsanctions?\b|\bcompliance\b|\bsafety incidents?\b|\bdsa\b|\bai act\b|\bfrontier act\b|\bbills?\b|安全事故|监管|法案|起诉|诉讼|法院|版权|反垄断|出口管制|合规",
    "ai_for_science": r"\bproteins?\b|\bgenom(?:e|ics|ic)?\b|\brna\b|\bantibod(?:y|ies)\b|drug discovery|\bbiology\b|\bbiological\b|\bclinical\b|\bmedical\b|\bchemistry\b|\bmolecules?\b|materials science|\bphysics\b|生物医药|蛋白|药物发现|医疗模型|临床|化学|分子|材料科学|物理学",
    "robotics_embodied": r"\brobots?\b|\brobotics\b|\bhumanoids?\b|embodied ai|\bvla\b|\bros(?:\s*2)?\b|robot manipulation|具身智能|具身机器人|机器人|人形机器人",
    "chips_compute": r"\bgpu\b|\btpu\b|\bnpu\b|\blpu\b|\bhbm\b|\bcuda\b|\brubin\b|\binference\b|inferencing|chip|semiconductor|data cent(?:er|re)|compute|算力|芯片|显卡|数据中心|推理成本",
    "creative_multimodal": r"image generation|text-to-image|video generation|text-to-video|generative (?:image|video|audio) model|3d model|\b3dgs\b|worldgen|gaussian splatting|3d scene|comfyui|midjourney|\bflux\b|\bsora\b|\bvfx\b|ai film|生图|视频生成|图像生成|音频生成|文生图|文生视频|三维生成|世界生成模型|AI 影视",
    "ai_coding": r"coding agent|code agent|codebase|\bide\b|\bcli\b|terminal|programming|software engineering|github copilot|\bcursor\b|claude code|\bcodex\b|代码|编程|开发工具|命令行",
    "agents_automation": r"agentic|\bagents?\b|\bmcp\b|model context protocol|skill(?:s)?\b|computer use|browser agent|workflow automation|multi-agent|自动化|智能体|工作流",
    "research_open_source": r"arxiv|paper|preprint|benchmark|eval(?:uation)?|dataset|github|hugging ?face|open[- ]source|open weights|weights released|technical report|论文|评测|数据集|开源|权重|技术报告",
    "business_enterprise": r"funding|raised \$|acquisition|acquire[ds]?|merger|revenue|contract|enterprise|customer|layoff|hiring|partnership|head of product|vp of product|融资|收购|并购|合同|企业|客户(?!端)|裁员|招聘|营收|挖走|离职|入职|人事变动|产品负责人",
    "frontier_models": r"model release|new model|foundation model|frontier model|reasoning model|context window|\bapi\b|\bpricing\b|(?:model|api|token|subscription) prices?|subscription|rate limit|usage limit|feature rollout|early access|model access|模型发布|新模型|基础模型|接口|模型价格|API ?价格|上下文|推理模型|产品更新|功能|订阅|额度|限额",
}

GENERAL_SOURCES = {"techmeme", "thepandaily"}
AI_CONTEXT_PATTERN = re.compile(
    r"\bai\b|artificial intelligence|\bllms?\b|chatgpt|openai|anthropic|claude|gemini|deepseek|qwen|kimi|"
    r"codex|copilot|machine learning|neural|agents?|robot|gpu|tpu|hbm|semiconductor|data cent(?:er|re)|"
    r"人工智能|大模型|模型|智能体|机器人|芯片|算力|数据中心|机器学习|深度学习",
    re.I,
)

RESEARCH_STRONG_PATTERN = re.compile(
    r"\barxiv\b|\bpapers?\b|\bpreprints?\b|\bbenchmarks?\b|\bevals?\b|\bdatasets?\b|technical report|open weights|weights released|论文|评测|数据集|技术报告|开放权重",
    re.I,
)

SOURCE_DEFAULTS = {
    "_akhaliq": "research_open_source",
    "huggingpapers": "research_open_source",
    "dair_ai": "research_open_source",
    "jiqizhixin": "research_open_source",
    "lerobothf": "robotics_embodied",
    "xrobohub": "robotics_embodied",
    "biologyaidaily": "ai_for_science",
    "semianalysis_": "chips_compute",
    "insideaipolicy": "policy_safety_legal",
    "codeglitch": "ai_coding",
    "lentils80": "frontier_models",
    "testingcatalog": "frontier_models",
}

def normalize_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip().rstrip("/")
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"ref", "s", "t"}]
    host = parts.netloc.lower().removeprefix("www.")
    if host in {"twitter.com", "mobile.twitter.com"}:
        host = "x.com"
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), urlencode(query), ""))

def _signature(value: str) -> str:
    value = re.sub(r"https?://\S+", "", value.lower())
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value).strip()
    return hashlib.sha256(value[:500].encode()).hexdigest()[:20]


def _best_text(post: dict[str, Any]) -> str:
    upstream = str(post.get("upstream_text") or "").strip()
    visible = re.sub(r"https?://\S+", "", upstream).strip()
    return upstream if len(visible) >= 40 else str(post.get("text") or upstream)

def event_key(post: dict[str, Any]) -> str:
    for value in post.get("upstream_external_urls") or []:
        if normalized := normalize_url(value):
            return f"url:{normalized}"
    if upstream_id := str(post.get("upstream_post_id") or ""):
        return f"x:{upstream_id}"
    for value in post.get("external_urls") or []:
        if normalized := normalize_url(value):
            return f"url:{normalized}"
    return f"text:{_signature(_best_text(post))}"

def classify_event(post: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(post.get(name) or "") for name in ("upstream_text", "text", "external_urls", "upstream_external_urls"))
    handle = str(post.get("handle") or "").lower()
    if handle in GENERAL_SOURCES and not AI_CONTEXT_PATTERN.search(text):
        return {"focus": None, "confidence": "low", "secondary_tags": []}
    hits = {focus: len(re.findall(PATTERNS[focus], text, re.I)) for focus in FOCUS_ORDER}
    matches = [focus for focus in FOCUS_ORDER if hits[focus]]
    default = SOURCE_DEFAULTS.get(handle)
    verticals = ("creative_multimodal", "robotics_embodied", "chips_compute")
    vertical = max(verticals, key=lambda focus: hits[focus] + int(default == focus))
    vertical_score = hits[vertical] + int(default == vertical)
    specialist_score = max(hits["research_open_source"], hits["ai_coding"], hits["agents_automation"])
    if hits["policy_safety_legal"]:
        primary = "policy_safety_legal"
    elif hits["business_enterprise"]:
        primary = "business_enterprise"
    elif hits["ai_for_science"]:
        primary = "ai_for_science"
    elif vertical_score > specialist_score:
        primary = vertical
    elif RESEARCH_STRONG_PATTERN.search(text):
        primary = "research_open_source"
    elif hits["ai_coding"] or hits["agents_automation"]:
        primary = max(("ai_coding", "agents_automation"), key=lambda focus: hits[focus])
    elif hits["research_open_source"]:
        primary = "research_open_source"
    elif default:
        primary = default
    elif hits["frontier_models"]:
        primary = "frontier_models"
    else:
        return {"focus": None, "confidence": "low", "secondary_tags": []}
    secondary = [focus for focus in matches if focus != primary]
    if default and default != primary and default not in secondary:
        secondary.append(default)
    confidence = "medium" if not hits.get(primary) else "high" if len(text) >= 80 or len(matches) > 1 else "medium"
    return {"focus": primary, "confidence": confidence, "secondary_tags": secondary}


def canonical_source_url(post: dict[str, Any]) -> str:
    for field in ("upstream_external_urls", "upstream_url", "external_urls", "url"):
        values = post.get(field) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            if normalized := normalize_url(str(value)):
                return normalized
    return ""

def deduplicate_events(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in posts:
        if not post.get("is_reply"):
            groups[event_key(post)].append(post)
    events = []
    for key, group in groups.items():
        group.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        event = dict(group[0])
        route = classify_event(event)
        event.update(route)
        event.update({
            "event_id": hashlib.sha256(key.encode()).hexdigest()[:20],
            "event_key": key,
            "primary_focus": route["focus"],
            "canonical_source_url": canonical_source_url(event),
            "discovery_url": event.get("url", ""),
            "discovery_sources": sorted({item.get("handle", "") for item in group if item.get("handle")}),
            "source_post_ids": [item.get("post_id", "") for item in group],
            "discovery_count": len(group),
            "first_seen_at": min((item.get("created_at", "") for item in group), default=""),
            "last_seen_at": max((item.get("created_at", "") for item in group), default=""),
            "discovery_only": all(item.get("source_tier", "S3") in {"S3", "S4"} for item in group),
            "verification_status": "needs_verification" if all(item.get("source_tier", "S3") in {"S3", "S4"} for item in group) else "source_available",
            "route_reason": f"matched:{route['focus']}" if route["focus"] else "no_focus_match",
        })
        events.append(event)
    return sorted(events, key=lambda item: item.get("created_at", ""), reverse=True)
