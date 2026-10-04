"""Persistent provenance and review-only Chinese draft helpers for AI Radar."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


PROMPT_VERSION = "ai-radar-douyin-minimal-v2"
ELIGIBLE = "eligible"
BLOCKED_FIRST_PARTY_SELF_PUBLICATION = "blocked_first_party_self_publication"

_RESEARCH_ASSET = re.compile(
    r"\b(?:research|papers?|stud(?:y|ies)|preprints?|arxiv|benchmarks?|datasets?|"
    r"technical reports?|experiments?|findings?|best paper)\b|"
    r"论文|研究|成果|数据集|基准|评测|实验|技术报告",
    re.I,
)
_FIRST_PARTY_RESEARCH = re.compile(
    r"\b(?:our|my)\s+(?:(?:new|latest|first|recent|best|award[- ]winning|full|own|joint|open|published)\s+){0,4}"
    r"(?:research|papers?|stud(?:y|ies)|preprints?|benchmarks?|datasets?|technical reports?|experiments?|findings?|work)\b|"
    r"\bwe(?:'re| are)?\s+sharing\s+(?:our\s+|new\s+)?research\b|"
    r"\b(?:we|i)\s+(?:train\w*|evaluat\w*|find|found|show\w*|demonstrat\w*)\b|"
    r"\b(?:we|i)\s+(?:present\w*|introduc\w*|releas\w*|publish\w*|propos\w*|open[- ]sourc\w*)"
    r".{0,80}\b(?:research|papers?|stud(?:y|ies)|preprints?|benchmarks?|datasets?|technical reports?)\b|"
    r"\b(?:we|i)\s+(?:authored|co-?authored|wrote)\s+(?:(?:this|our|my|the|a|new)\s+){0,3}"
    r"(?:papers?|stud(?:y|ies)|preprints?|technical reports?)\b|"
    r"(?:我们的|我的|本团队的|本实验室的).{0,100}(?:论文|研究|成果|数据集|基准|评测|实验|技术报告)|"
    r"(?:我们|我|本团队|本实验室).{0,40}(?:分享|发布|发表|提出|推出|开源|训练|开发|研究|发现|证明|展示|评估|撰写|参与)",
    re.I | re.S,
)
_EN_SELF_PUBLICATION = re.compile(
    r"\bwe(?:'ve| have)?\s+(?:shipped|launched|released|built|created|developed|started|founded|raised|grew|acquired|partnered|joined)\b|"
    r"\bwe(?:'re| are)\s+(?:shipping|launching|releasing|building|developing|hiring|working on (?:a|the) fix|making.{0,40}available)\b|"
    r"\bi(?:'ve| have)?\s+(?:joined|founded|started|launched|released)\b|"
    r"\b(?:our|my)\s+(?:team|company|startup|lab|product|platform|app|service|business|customers?|users?|roadmap|release|mission)\b",
    re.I | re.S,
)
_ZH_SELF_PUBLICATION = re.compile(
    r"(?:我们|我和[^，。！？\n]{1,30}|本团队|本公司|我们的团队).{0,50}"
    r"(?:发布|上线|推出|开源|构建|开发|训练|交付|迭代|修复|增长|融资|加入|创立|创办|收购|合作)|"
    r"(?:我们的|本团队的|本公司的|我们自家的).{0,30}"
    r"(?:产品|功能|模型|平台|服务|项目|公司|团队|客户|用户|业务|营收|融资|路线图)|"
    r"我(?:已|刚刚)?(?:加入|创立|创办|联合创立|发布|推出|上线)",
    re.I | re.S,
)
_ZH_IMPLICIT_SELF_PROJECT = re.compile(
    r"(?:^|[。！？\n])\s*(?:做个|做了个|在做|准备做|刚做完).{0,30}"
    r"(?:Skill|工具|应用|App|产品|项目|模型|插件).{0,100}"
    r"(?:迭代|发布|上线|开源|这几天发|过几天发)",
    re.I | re.S,
)
_SOURCE_MARKUP = re.compile(
    r"https?://\S+|\bt\.co/\S+|\b(?:www\.)?[A-Za-z0-9-]+\.(?:com|org|ai|io|dev|net)/\S+|"
    r"#[A-Za-z0-9_\u4e00-\u9fff]+|(?<![A-Za-z0-9_.+-])@[A-Za-z0-9_]+\b",
    re.I,
)


def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ai_radar_events(
        event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
        focus TEXT, confidence TEXT NOT NULL, canonical_source_url TEXT NOT NULL,
        canonical_source_text TEXT NOT NULL, verification_status TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        route_reason TEXT NOT NULL, event_json TEXT NOT NULL,
        draft_eligibility TEXT NOT NULL DEFAULT 'eligible',
        draft_block_reason TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS ai_radar_event_discoveries(
        event_id TEXT NOT NULL, discovery_post_id TEXT NOT NULL,
        discovery_handle TEXT NOT NULL, discovery_url TEXT NOT NULL,
        discovery_text TEXT NOT NULL, post_created_at TEXT NOT NULL,
        discovered_at TEXT NOT NULL, post_type TEXT NOT NULL,
        upstream_post_id TEXT NOT NULL, upstream_handle TEXT NOT NULL,
        upstream_url TEXT NOT NULL, upstream_text TEXT NOT NULL,
        external_urls_json TEXT NOT NULL, upstream_external_urls_json TEXT NOT NULL,
        post_json TEXT NOT NULL,
        PRIMARY KEY(event_id, discovery_post_id),
        FOREIGN KEY(event_id) REFERENCES ai_radar_events(event_id)
    );
    CREATE TABLE IF NOT EXISTS ai_radar_drafts(
        event_id TEXT NOT NULL, output_account_id TEXT NOT NULL,
        focus TEXT NOT NULL, input_hash TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, draft_zh TEXT NOT NULL,
        verification_status TEXT NOT NULL, review_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(event_id, output_account_id, input_hash),
        FOREIGN KEY(event_id) REFERENCES ai_radar_events(event_id)
    );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(ai_radar_events)")}
    if "draft_eligibility" not in columns:
        db.execute("ALTER TABLE ai_radar_events ADD COLUMN draft_eligibility TEXT NOT NULL DEFAULT 'eligible'")
    if "draft_block_reason" not in columns:
        db.execute("ALTER TABLE ai_radar_events ADD COLUMN draft_block_reason TEXT NOT NULL DEFAULT ''")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_text(event: dict[str, Any]) -> str:
    if event.get("post_type") == "retweet":
        return str(event.get("upstream_text") or event.get("text") or "").strip()
    return str(event.get("text") or event.get("upstream_text") or "").strip()


def original_post_url(event: dict[str, Any]) -> str:
    if event.get("post_type") in {"quote", "retweet"} and event.get("upstream_url"):
        return str(event["upstream_url"]).strip()
    return str(event.get("discovery_url") or event.get("url") or event.get("upstream_url") or "").strip()


def original_author_identifiers(event: dict[str, Any]) -> list[str]:
    if event.get("post_type") in {"quote", "retweet"} and event.get("upstream_url"):
        values = (event.get("upstream_author_name"), event.get("upstream_handle"))
    else:
        values = (event.get("author_name"), event.get("handle"))
    return list(dict.fromkeys(str(value).strip().lstrip("@") for value in values if str(value or "").strip()))


def original_post_handle(event: dict[str, Any]) -> str:
    if event.get("post_type") in {"quote", "retweet"} and event.get("upstream_handle"):
        return str(event["upstream_handle"]).strip().lstrip("@").lower()
    return str(event.get("handle") or "").strip().lstrip("@").lower()


def apply_first_party_affiliation(event: dict[str, Any], affiliations: dict[str, list[str]]) -> None:
    event["known_first_party_subjects"] = affiliations.get(original_post_handle(event), [])


def draft_eligibility(event: dict[str, Any]) -> tuple[str, str]:
    """Block first-party team, company, project, product, and research posts."""
    if event.get("post_type") in {"quote", "retweet"} and event.get("upstream_text"):
        text = str(event["upstream_text"])
    else:
        text = str(event.get("text") or event.get("upstream_text") or "")
    if _RESEARCH_ASSET.search(text) and _FIRST_PARTY_RESEARCH.search(text):
        return BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_research"
    lowered = text.lower()
    subjects = event.get("known_first_party_subjects") or []
    if "*" in subjects or any(str(subject).lower() in lowered for subject in subjects):
        return BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "known_first_party_affiliation"
    if _ZH_IMPLICIT_SELF_PROJECT.search(text):
        return BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_personal_project"
    if _EN_SELF_PUBLICATION.search(text) or _ZH_SELF_PUBLICATION.search(text):
        return BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_team_update"
    return ELIGIBLE, ""


def _source_posts(event: dict[str, Any], posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = set(event.get("source_post_ids") or [])
    by_id = {str(post.get("post_id") or ""): post for post in posts}
    selected = [by_id[post_id] for post_id in wanted if post_id in by_id]
    if not selected and event.get("post_id"):
        selected = [event]
    return selected


def persist_event(db: sqlite3.Connection, event: dict[str, Any], posts: list[dict[str, Any]]) -> None:
    """Upsert a stable event and append every raw discovery post linked to it."""
    event_id = str(event["event_id"])
    source_text = _source_text(event)
    eligibility, block_reason = draft_eligibility(event)
    event["draft_eligibility"] = eligibility
    event["draft_block_reason"] = block_reason
    first_seen = str(event.get("first_seen_at") or event.get("created_at") or _now())
    last_seen = str(event.get("last_seen_at") or event.get("created_at") or first_seen)
    db.execute(
        """INSERT INTO ai_radar_events(
             event_id,event_key,focus,confidence,canonical_source_url,
             canonical_source_text,verification_status,first_seen_at,last_seen_at,
             route_reason,event_json,draft_eligibility,draft_block_reason
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(event_id) DO UPDATE SET
             focus=excluded.focus, confidence=excluded.confidence,
             canonical_source_url=excluded.canonical_source_url,
             canonical_source_text=excluded.canonical_source_text,
             verification_status=excluded.verification_status,
             last_seen_at=MAX(ai_radar_events.last_seen_at, excluded.last_seen_at),
             route_reason=excluded.route_reason, event_json=excluded.event_json,
             draft_eligibility=excluded.draft_eligibility,
             draft_block_reason=excluded.draft_block_reason""",
        (
            event_id, str(event["event_key"]), event.get("focus"),
            str(event.get("confidence") or "low"), str(event.get("canonical_source_url") or ""),
            source_text, str(event.get("verification_status") or "needs_verification"),
            first_seen, last_seen, str(event.get("route_reason") or ""),
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            eligibility, block_reason,
        ),
    )
    discovered_at = _now()
    for post in _source_posts(event, posts):
        post_id = str(post.get("post_id") or "")
        if not post_id:
            continue
        db.execute(
            """INSERT OR IGNORE INTO ai_radar_event_discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, post_id, str(post.get("handle") or ""), str(post.get("url") or ""),
                str(post.get("text") or ""), str(post.get("created_at") or ""), discovered_at,
                str(post.get("post_type") or "original"), str(post.get("upstream_post_id") or ""),
                str(post.get("upstream_handle") or ""), str(post.get("upstream_url") or ""),
                str(post.get("upstream_text") or ""),
                json.dumps(post.get("external_urls") or [], ensure_ascii=False),
                json.dumps(post.get("upstream_external_urls") or [], ensure_ascii=False),
                json.dumps(post, ensure_ascii=False, sort_keys=True),
            ),
        )


def persist_events(db: sqlite3.Connection, events: list[dict[str, Any]], posts: list[dict[str, Any]]) -> None:
    for event in events:
        persist_event(db, event, posts)


def refresh_draft_eligibility(db: sqlite3.Connection, affiliations: dict[str, list[str]] | None = None) -> int:
    """Re-evaluate stored events and remove blocked drafts from the review queue."""
    for event_id, event_json in db.execute("SELECT event_id,event_json FROM ai_radar_events"):
        event = json.loads(event_json)
        if affiliations is not None:
            apply_first_party_affiliation(event, affiliations)
        eligibility, block_reason = draft_eligibility(event)
        event["draft_eligibility"] = eligibility
        event["draft_block_reason"] = block_reason
        db.execute(
            """UPDATE ai_radar_events
               SET draft_eligibility=?,draft_block_reason=?,event_json=?
               WHERE event_id=?""",
            (eligibility, block_reason, json.dumps(event, ensure_ascii=False, sort_keys=True), event_id),
        )
    db.execute(
        """UPDATE ai_radar_drafts SET review_status='excluded_first_party_self_publication'
           WHERE review_status='excluded_first_party_research'"""
    )
    cursor = db.execute(
        """UPDATE ai_radar_drafts SET review_status='excluded_first_party_self_publication'
           WHERE review_status IN ('needs_review','needs_rewrite') AND event_id IN (
             SELECT event_id FROM ai_radar_events
             WHERE draft_eligibility<>'eligible'
           )"""
    )
    return cursor.rowcount


def output_account_for_event(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    focus = event.get("focus")
    if not focus:
        return None
    matches = [item for item in config.get("output_accounts", []) if item.get("focus") == focus]
    if len(matches) != 1:
        raise ValueError(f"focus {focus!r} must have exactly one output account")
    return matches[0]


def build_draft_prompt(event: dict[str, Any], output_account: dict[str, Any]) -> str:
    source = _source_text(event)
    author_ids = original_author_identifiers(event)
    author_note = "、".join(author_ids) if author_ids else "未提供；仍需删除文中作为来源身份出现的作者姓名或昵称"
    host = urlparse(str(event.get("canonical_source_url") or "")).netloc.removeprefix("www.")
    subject_hint = ""
    if re.match(r"https?://\S+", source) and host and host.lower() not in {"x.com", "twitter.com", "t.co"}:
        subject_hint = f"\n原稿开头短链接对应的主体名：{host}。只把它当主体名称，不要输出 URL。\n"
    return (
        "你是 X 原帖转中文帖子的文字编辑。只返回严格 JSON：{\"draft_zh\":\"最终正文\"}。\n\n"
        "按本地抖音洗稿使用的最小改写规则处理：\n"
        "1. 原始文字稿就是骨架。能不改的原句、语序、段落、叙事顺序、反问、重复节奏和口语表达一律不改。\n"
        "2. 中文原稿只修明确错字、标点、口吃和无意义噪声；英文原稿翻成自然中文，但必须保留原信息顺序、段落、人称、语气和不确定性。\n"
        "3. 只删除关注点赞、评论互动、来源链接、话题标签，以及不影响核心内容的账号身份前情、历史战绩、自证材料、旧文章和熟人关系。\n"
        "4. 人称逐句继承。原稿有第一人称就保留；原稿没有第一人称时绝不能新增。\n"
        "5. 不得新增或删失事实、数字、日期、经历、因果、观点、例子、解释、比喻、限制、更正或结论。不得扩写情绪，也不得补总结。\n"
        "6. 保留原稿已有的单边情绪、惊讶、讽刺、重复和判断，不得改成中性新闻稿、风险教育、两面平衡或通用方法论。\n"
        "7. 原帖作者、发现账号和转发者身份只用于内部追溯。正文不得出现原帖作者的姓名、昵称、handle、个人履历或‘谁发了这条内容’的介绍；不得把 @ 去掉后留下裸 handle。删除‘某人发帖/写博客/采访/表示’这类来源引介后，直接保留原有事实、观点和必要限定。公司、产品、项目或论文名称如果本身是事实对象可以保留，但不能写成来源归因。\n"
        "8. 不设最低字数，不为了完整、专业或顺滑而加开头、结尾、标题、标签、免责声明或 CTA。\n\n"
        f"{subject_hint}"
        f"本条原始 X 原帖作者身份（仅供删除，正文不得输出）：{author_note}\n"
        f"原始文字稿：\n{source}"
    )


def parse_draft_response(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    result = json.loads(content)
    if not isinstance(result, dict) or "draft_zh" not in result:
        raise ValueError("draft response must contain draft_zh")
    draft = result["draft_zh"]
    if not isinstance(draft, str) or not draft.strip():
        raise ValueError("draft_zh must be a non-empty string")
    return draft.strip()


def normalize_draft_markup(draft: str) -> str:
    cleaned = _SOURCE_MARKUP.sub("", draft)
    return "\n".join(re.sub(r"[ \t]+", " ", line).rstrip() for line in cleaned.splitlines()).strip()


def remove_source_author_identifiers(draft: str, identifiers: list[str]) -> str:
    for identity in identifiers:
        identity = str(identity or "").strip().lstrip("@")
        if len(identity) >= 3:
            draft = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(identity)}(?![A-Za-z0-9_])", "", draft, flags=re.I)
    return "\n".join(re.sub(r"[ \t]+", " ", line).rstrip() for line in draft.splitlines()).strip()


def draft_violations(
    draft: str,
    output_account_name: str = "",
    source_text: str = "",
    source_author_identifiers: list[str] | None = None,
) -> list[str]:
    failures = []
    first_person = re.compile(r"(?:我们|咱们|(?<![自忘无])我)|\b(?:I|we|my|our|me|us)\b", re.I)
    if source_text and first_person.search(draft) and not first_person.search(source_text):
        failures.append("added_first_person")
    if _SOURCE_MARKUP.search(draft):
        failures.append("source_markup")
    for identity in source_author_identifiers or []:
        identity = str(identity or "").strip().lstrip("@")
        if len(identity) < 3:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(identity)}(?![A-Za-z0-9_])", draft, re.I):
            failures.append("source_author_identity")
            break
    if output_account_name and output_account_name in draft:
        failures.append("self_reference")
    if re.search(r"原作者|信源账号|有人发帖|本账号(?:报道|关注到)|作者表示|根据转写|口播里提到|视频里说|这篇(?:帖子|内容)(?:认为|提到)", draft):
        failures.append("attribution_tone")
    return failures


def refresh_draft_content_rules(db: sqlite3.Connection) -> int:
    """Queue existing drafts for rewrite only when they still expose source identity."""
    queued = 0
    rows = db.execute(
        """SELECT d.event_id,d.output_account_id,d.input_hash,d.draft_zh,e.event_json
           FROM ai_radar_drafts d JOIN ai_radar_events e ON e.event_id=d.event_id
           WHERE d.review_status='needs_review'"""
    ).fetchall()
    for event_id, account_id, input_hash, draft, event_json in rows:
        event = json.loads(event_json)
        violations = draft_violations(
            draft,
            source_text=_source_text(event),
            source_author_identifiers=original_author_identifiers(event),
        )
        if "source_author_identity" not in violations:
            continue
        cursor = db.execute(
            """UPDATE ai_radar_drafts SET review_status='needs_rewrite'
               WHERE event_id=? AND output_account_id=? AND input_hash=?""",
            (event_id, account_id, input_hash),
        )
        queued += cursor.rowcount
    return queued


def draft_input_hash(event: dict[str, Any], output_account: dict[str, Any]) -> str:
    value = {
        "event_id": event["event_id"], "focus": event.get("focus"),
        "account": output_account.get("slug") or output_account.get("id") or output_account.get("handle"),
        "source": _source_text(event), "source_url": event.get("canonical_source_url"),
        "prompt_version": PROMPT_VERSION,
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def persist_draft(db: sqlite3.Connection, event: dict[str, Any], config: dict[str, Any], draft_zh: str, *, model: str = "") -> bool:
    """Store a review-only draft once; it is never a publishable record."""
    if draft_eligibility(event)[0] != ELIGIBLE:
        return False
    account = output_account_for_event(event, config)
    if account is None:
        return False
    input_hash = draft_input_hash(event, account)
    account_id = str(account.get("slug") or account.get("id") or account.get("handle") or "")
    existing = db.execute(
        "SELECT review_status FROM ai_radar_drafts WHERE event_id=? AND output_account_id=? AND input_hash=?",
        (str(event["event_id"]), account_id, input_hash),
    ).fetchone()
    if existing and existing[0] == "needs_review":
        return False
    db.execute(
        """UPDATE ai_radar_drafts SET review_status='superseded'
           WHERE event_id=? AND review_status='needs_review'
             AND (output_account_id<>? OR input_hash<>?)""",
        (
            str(event["event_id"]),
            account_id,
            input_hash,
        ),
    )
    cursor = db.execute(
        """INSERT INTO ai_radar_drafts VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(event_id,output_account_id,input_hash) DO UPDATE SET
             model=excluded.model,draft_zh=excluded.draft_zh,
             verification_status=excluded.verification_status,
             review_status=excluded.review_status,created_at=excluded.created_at""",
        (
            str(event["event_id"]), account_id,
            str(event["focus"]), input_hash, PROMPT_VERSION, model, draft_zh,
            "needs_verification", "needs_review", _now(),
        ),
    )
    return cursor.rowcount == 1
