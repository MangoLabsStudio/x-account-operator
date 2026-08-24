#!/usr/bin/env python3
import concurrent.futures
import getpass
import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import PERSONA_PUBLIC_PROFILE, build_editorial_questions, build_opportunity_questions, build_research_questions


API = "https://www.micuapi.ai/v1"
TZ = ZoneInfo("Asia/Shanghai")

ASSIGNMENTS = {
    "acheng": "opportunity:rwa:tokenized_equities:liquidity_activity",
    "ridehail-driver-zhao": "research:stablecoin:stablecoin_payments:adoption",
    "college-student-linjia": "research:rwa:tokenized_equities:adoption",
    "atuo": "research:hyperliquid:market_structure:competition",
    "axu": "research:bitcoin:market_structure:market_structure",
    "nanqiao": "research:stablecoin:stablecoin_payments:competition",
    "qiliang": "opportunity:hyperliquid:market_structure:short_term_trade",
    "aye": "editorial:bitcoin:market_structure:trading_philosophy",
    "xiaoman": "research:hyperliquid:revenue_buyback:unit_economics",
    "maili": "opportunity:solana:market_structure:short_term_trade",
}

WRITING_BRIEFS = {
    "acheng": "主题：小资金做代币化股票 LP，到底值不值得冲？像普通人算一笔小账，只讲赚什么、筛池子的两个门槛和为什么不能把费率当固定收益。朴素、有一点跑单式算账感。",
    "ridehail-driver-zhao": "主题：出国旅游时，稳定币扫码为什么可能比换汇和刷卡更顺？从时间、手续费和商户是否需要懂 Crypto 三件事讲，像见过各种支付麻烦的中年人下判断，但不得编乘客或行程。",
    "college-student-linjia": "主题：年轻人为什么会买一种并不等于真股票的代币？抓住‘不是股东却有人用’的反差，解释 24 小时交易这个真实需求。像刚把一个反常识问题想明白，不写小研报。",
    "atuo": "主题：Hyperliquid 为什么愿意把前端手续费分给钱包？从增长和平台战略解释它如何用利润换分发，明确这是在争夺交易基础设施的位置。像产品增长操盘手拆策略。",
    "axu": "主题：BTC 这轮上涨究竟是一次挤空，还是现货行情已经接棒？只选 ETF 流入、清算、资金费率三个指标给结论和失效信号。冷静、结构化，但不要写成行情播报。",
    "nanqiao": "主题：稳定币支付这门生意，真正替谁省了钱？只研究跨境商户这一个客户，拆清原来的成本、产品替代了哪一层、谁因此赚钱。像一段有结论的行业研究摘要。",
    "qiliang": "主题：HYPE 第一段已经涨完，空仓的人现在怎么参与？直接给交易结论、等什么位置、什么情况作废。果断讲赔率，不复述整套项目叙事。",
    "aye": "主题：为什么 BTC 一涨，所有人都突然‘早就看懂了’？写价格如何制造解释、解释又如何制造追涨，用一点嘲讽和网络感。重点是人群，不是技术分析。",
    "xiaoman": "主题：HYPE 回购飞轮以后到底靠什么继续转？把交易费买盘和即将加入的 USDC 利息串成一条因果链，最后只留下两个值得盯的链上指标。像长期跟踪生态的人。",
    "maili": "主题：SOL 回到 94 附近，是第二次上车机会还是接飞刀？写普通交易者此刻真正纠结的两边，再用 92 和 100 两个位置做决定。像交易手账，但不得虚构已买、持仓或过去操作。",
}

WAITING_PHRASES = (
    "继续观察", "等待更多", "等后续", "在那之前", "有待观察",
    "尚未形成", "还没有形成", "我会持续关注", "我会关注",
)

FAKE_EXPERIENCE_PHRASES = (
    "接到一位乘客", "路上想了想", "我之前以为", "眼看着它",
    "犹豫没敢追", "我打算", "我准备", "我已经买", "我自己复盘",
)


def request_json(path, key, payload, timeout=240):
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code not in (429, 503) or attempt == 3:
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
            time.sleep(10 * (attempt + 1))


def output_text(body):
    parts, citations = [], []
    for item in body.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
            for annotation in content.get("annotations", []):
                url = annotation.get("url") or annotation.get("url_citation", {}).get("url")
                if url and url not in citations:
                    citations.append(url)
    return "\n".join(parts).strip(), citations


def research_topic(topic, questions, key, as_of):
    compact_topic = {
        "key": topic["key"],
        "title": topic["title"],
        "unique_authors": topic.get("unique_authors"),
        "post_count": topic.get("post_count"),
        "samples": [
            {"created_at": item.get("created_at"), "text": str(item.get("text", ""))[:600]}
            for item in topic.get("sample_posts", [])[:3]
        ],
    }
    compact_questions = [
        {"id": item["id"], "title": item["title"], "kind": item["kind"]}
        for item in questions
    ]
    prompt = f"""你是 Crypto 市场研究员。时间截点：{as_of}。

必须使用 X Search 和 Web Search，给中文 KOL 写作者补齐下面议题的实时语境。母池帖子只说明有人讨论，不能当作事实。请自行查找项目官方文档、公告、交易所/行情/链上数据和原始 X 帖。

议题：{json.dumps(compact_topic, ensure_ascii=False)}
本议题要服务的题目：{json.dumps(compact_questions, ensure_ascii=False)}

输出必须包含：
1. 圈内默认知道的前情与为什么今天在讨论；
2. 已核事实：每条写时间、数字口径、primary URL；
3. 当前主流观点与反方，注明这是观点；
4. 对每个题目分别给一个可成立的明确结论和最有用的解释；
5. 不能确认、不得写进正文的内容。

不要给泛泛风险提示，不要用“继续观察”代替结论。机会题要说明赚的是什么、适合谁、最简单的参与判断；不需要把完整交易路径全部计算出来。全文控制在 1400 个中文字符以内。"""
    body = request_json(
        "responses",
        key,
        {
            "model": "grok-4.6",
            "input": prompt,
            "tools": [
                {"type": "x_search", "from_date": "2026-08-23T00:00:00Z"},
                {"type": "web_search"},
            ],
            "max_output_tokens": 4000,
        },
    )
    text, citations = output_text(body)
    if not text:
        raise RuntimeError(f"Grok returned no text for {topic['key']}")
    return {"topic_key": topic["key"], "text": text, "citations": citations}


def parse_json_text(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def write_post(persona, question, context, key):
    prompt = f"""你是中文 Crypto KOL 编辑。请把研究 Context 写成一条能直接进入人工审核的帖子。

账号人设：{json.dumps(persona, ensure_ascii=False)}
本次题目：{json.dumps(question, ensure_ascii=False)}
本次编辑 Brief：{WRITING_BRIEFS[persona['slug']]}
Grok 实时研究：{json.dumps(context, ensure_ascii=False)}

要求：
- 只输出 JSON：{{"text":"...","stance":"...","sources_used":["..."]}}。
- 正文 240—420 个中文字符，不写编号、项目符号、来源列表、免责声明或 hashtag。
- 一篇只讲 Brief 中的一个主题。第一句必须让读者立刻知道在讨论什么，并给出明确判断；删掉与这个判断无关的 Context。
- 最多使用三个数字。不要按“背景—数据—风险—总结”的研报模板写，也不要十个人都用相同结构。
- 个性来自选择什么、怎么判断和怎么比喻，不来自硬塞职业场景、口头禅或虚构故事。
- 机会题必须说清赚什么、为什么现在、什么情况下可以参与；不需要把全部路径算完。不能用“都不做”当结论。
- 研究题必须给一个清楚结论；观点/乐子题要有人味和具体反差，不强塞操作建议。
- 只使用 Context 中有来源支撑的事实和数字。X 观点只能写成观点，不得升级为事实。
- PERSONA 只决定视角和语言，严禁虚构亲历、持仓、成交、收益、朋友、乘客、订单、课程或项目内部信息。
- 本次没有提供任何个人经历或交易记录。不得为了贴人设编造“接到乘客、上课时、跑单时、之前没敢追、我准备买、我打算开仓”等情节，也不要照抄人设文件里的示例口头禅。
- 少写提示和兜底。风险只有会改变读者决定时才写一句。
- 禁止用“继续观察、等后续、在那之前、尚未形成条件”收尾。结尾落在结论、现实后果或具体参与判断。
- 去掉 AI 味：不用“不是X而是Y”开头，不用三段排比，不用“本质上、值得注意的是、核心逻辑、结构性”。"""
    body = request_json(
        "chat/completions",
        key,
        {
            "model": "gemini-3.1-pro-preview",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8,
            "max_tokens": 5000,
            "response_format": {"type": "json_object"},
        },
    )
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    result = parse_json_text(content)
    text = result.get("text", "").strip()
    if (
        not 180 <= len(text) <= 650
        or any(phrase in text for phrase in WAITING_PHRASES)
        or any(phrase in text for phrase in FAKE_EXPERIENCE_PHRASES)
    ):
        raise RuntimeError(f"Gemini draft failed validation for {persona['slug']}")
    return {
        "persona_id": persona["slug"],
        "persona_name": persona["display_name"],
        "question_id": question["id"],
        "question_title": question["title"],
        "text": text,
        "stance": result.get("stance", ""),
        "sources_used": result.get("sources_used", []),
    }


def main():
    root = Path(__file__).resolve().parents[1]
    with sqlite3.connect(root / "data/xops.db") as db:
        db.row_factory = sqlite3.Row
        run = db.execute(
            "SELECT context_date,raw_cards FROM daily_context_runs ORDER BY context_date DESC LIMIT 1"
        ).fetchone()
        persona_rows = db.execute("SELECT slug,name,role,draft FROM personas ORDER BY id").fetchall()
    if not run:
        raise RuntimeError("no daily context run")

    cards = json.loads(run["raw_cards"])
    topics = {item["key"]: item for item in cards.get("discussion_topics", [])}
    discussion_topics = cards.get("discussion_topics", [])
    question_rows = build_opportunity_questions(discussion_topics)
    question_rows.extend(build_editorial_questions(discussion_topics))
    question_rows.extend(build_research_questions(discussion_topics))
    questions = {item["id"]: item for item in question_rows}
    missing = [item for item in ASSIGNMENTS.values() if item not in questions]
    if missing:
        raise RuntimeError(f"missing questions: {missing}")

    personas = {}
    for row in persona_rows:
        personas[row["slug"]] = {
            "slug": row["slug"],
            "name": row["name"],
            "display_name": PERSONA_PUBLIC_PROFILE[row["slug"]]["display_name"],
            "role": row["role"],
            "rules": json.loads(row["draft"]),
        }

    grouped = {}
    for slug, question_id in ASSIGNMENTS.items():
        question = questions[question_id]
        topic_key = question["source_topic_keys"][0]
        grouped.setdefault(topic_key, []).append(question)

    as_of = datetime.now(TZ).strftime("%Y-%m-%d %H:%M CST")
    context_partial = root / f"generated/persona-hot-contexts-{run['context_date']}.partial.json"
    contexts = {}
    if context_partial.exists():
        contexts = {
            item["topic_key"]: item
            for item in json.loads(context_partial.read_text(encoding="utf-8"))
        }
    grok_key = getpass.getpass("Grok key: ")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        jobs = {
            pool.submit(research_topic, topics[key], grouped[key], grok_key, as_of): key
            for key in grouped if key not in contexts
        }
        errors = {}
        for job in concurrent.futures.as_completed(jobs):
            key = jobs[job]
            try:
                contexts[key] = job.result()
            except Exception as exc:
                errors[key] = str(exc)
                print(f"grok failed {key}: {exc}")
                continue
            context_partial.write_text(
                json.dumps(list(contexts.values()), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"grok {key}: {len(contexts[key]['text'])} chars, {len(contexts[key]['citations'])} citations")
    del grok_key
    if errors:
        raise RuntimeError(json.dumps(errors, ensure_ascii=False))

    gemini_key = getpass.getpass("Gemini key: ")
    posts_partial = root / f"generated/persona-hot-posts-{run['context_date']}.partial.json"
    posts = json.loads(posts_partial.read_text(encoding="utf-8")) if posts_partial.exists() else []
    for post in posts:
        post["persona_name"] = PERSONA_PUBLIC_PROFILE[post["persona_id"]]["display_name"]
    completed = {item["persona_id"] for item in posts}
    for slug, question_id in ASSIGNMENTS.items():
        if slug in completed:
            continue
        question = questions[question_id]
        topic_key = question["source_topic_keys"][0]
        post = write_post(personas[slug], question, contexts[topic_key], gemini_key)
        posts.append(post)
        posts_partial.write_text(
            json.dumps(posts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"gemini {post['persona_id']}: {len(post['text'])} chars")
    del gemini_key

    order = list(ASSIGNMENTS)
    posts.sort(key=lambda item: order.index(item["persona_id"]))
    payload = {"as_of": as_of, "context_date": run["context_date"], "contexts": list(contexts.values()), "posts": posts}
    output = root / f"generated/persona-hot-batch-{run['context_date']}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    for post in posts:
        print(f"\n## {post['persona_name']}\n{post['text']}")


if __name__ == "__main__":
    main()
