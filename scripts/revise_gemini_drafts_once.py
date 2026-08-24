#!/usr/bin/env python3
import concurrent.futures
import getpass
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

from run_gemini_from_grok_once import API, TOPICS, VERIFIED_FACTS


CORRECTIONS = {
    "acheng": "保留普通用户的时间成本和筛选视角；最多用PURR与BASECAT做一次对比。不要把写进官方机制等同于值得买，不讨论锁仓技术细节，不写我记上了或大家掂量。",
    "ridehail-driver-zhao": "删除空头踩踏、机构真金白银、杠杆出清期等无法证明的因果。不要用最近两日买卖比推导方向。说明数据只能证明行情强、成交放大、BTC计价OI未同步扩张，不能分辨谁在平仓。开车类比最多一句。",
    "college-student-linjia": "删除我刚开始学时踩坑、今天才弄懂等虚构经历。可准确写Franklin提交no-action request但尚非SEC批准。不要写绝大多数资产仅机构可转或周转量接近零。用学习型语言讲发行、准入、成交、赎回是不同层。",
    "atuo": "删除省去自建后端、必然费率战、价值必然同质化等确定推论。保留Builder Codes授权与费率机制；判断落在共享流动性下前端必须用产品、服务和分发证明留存，并明确仍需真实数据验证。",
    "axu": "禁止判断是多头还是空头平仓，禁止把BTC计价OI叫真实杠杆，禁止说USDT名义OI纯粹由价格撑起。准确说明两种口径为何能同时一降一升，以及它们不能回答什么。",
    "nanqiao": "删除很多工具经常对不上、200往往是Gamma快照、下单频频报错等无证据说法。准确区分Gamma目录、CLOB订单簿、UI展示规则与可执行报价；语言像产品经理，不像API文档。",
    "qiliang": "删除市场错误绑定、吸引大量投机、资金炒作宏大叙事、政策真空流动性等脑补心理。只写SEC提案事实与事件交易如何观察文本、评论反馈、后续口径的差异；60天从Federal Register刊登起算。",
    "aye": "删除注意力枯竭、疯狂复制、社区幻觉、散户在赌等全称判断和贬损。解释动物符号为何易传播，并用BASECAT路线图与PURR原生发行说明官方关系有层级；最后看流动性和分布，不喊单。",
    "xiaoman": "删除倒逼、必然、大幅增加、摸底、一定高估等预测。准确写formal voting、SGP PR Open、SIMD Merged与feature gate是不同阶段；从生态视角提出需要观察的实际适配。",
    "maili": "删除空头挤压、机构入场、多空僵持、追涨意愿下降、杠杆撤退和去杠杆回撤等归因。像诚实手账一样把能确认和不能确认分开；不虚构个人成交或仓位。"
}


def request_post(key, persona, old_post, context):
    topic_id, assignment = TOPICS[persona["slug"]]
    payload = {
        "persona": persona,
        "assignment": assignment,
        "grok_research": context[topic_id],
        "verified_facts": VERIFIED_FACTS[topic_id],
        "previous_draft": old_post,
        "required_corrections": CORRECTIONS[persona["slug"]],
    }
    prompt = """你是第二遍中文 Crypto 编辑。上一版已经有足够背景，但出现了事实越界或过度归因。请重写，不要做局部同义词替换。

硬规则：
1. verified_facts 是唯一可以写成确定事实、日期和数字的材料。Grok 只提供背景、观点、反方和问题，不得复制其中未核数字、项目关系或因果。
2. required_corrections 必须全部执行。不能从价格、OI、资金费率、交易量推导未被证实的多空主体、资金来源或市场心理。
3. PERSONA只决定语言和观察角度，不得补造生活场景、学习经历、项目实测、朋友对话、持仓、成交、盈亏。
4. 写成一条260至450中文字符的完整帖子：具体背景 → 争议为何出现 → 明确但有限的判断 → 可验证的后续。只讲一条主线。
5. 不写标题、编号、项目符号、hashtag、来源列表、免责声明。避免“不是X而是Y”“一方面另一方面”“本质是”“必然”“大家都在”“有人说”等模板或全称判断。
6. 不需要把人设职业硬塞进比喻。第一人称只能表达分析立场。

只输出合法JSON：
{"persona_id":"...","text":"...","facts_used":["fact_id"],"stance":"一句话判断","risk_flags":[]}

INPUT:\n""" + json.dumps(payload, ensure_ascii=False)
    body = {
        "model": "gemini-3.1-pro-preview",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.65,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }
    last_error = None
    for attempt in range(2):
        request = urllib.request.Request(
            API,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
        content = result["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list) and len(parsed) == 1:
                parsed = parsed[0]
            parsed["persona_id"] = persona["slug"]
            return parsed
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = exc
            body["temperature"] = 0.2
            body["messages"][0]["content"] += "\n上次输出不是合法的单个JSON对象。重新输出，字符串中的换行必须转义。"
    raise last_error


def main():
    root = Path(__file__).resolve().parents[1]
    grok_rows = json.loads((root / "generated/grok-context-2026-08-24.json").read_text())
    contexts = {row["id"]: {"text": row["text"], "citations": row["citations"]} for row in grok_rows}
    first = json.loads((root / "generated/gemini-from-grok-drafts-2026-08-24.json").read_text())
    old_posts = {post["persona_id"]: post for post in first["posts"]}
    with sqlite3.connect(root / "data/xops.db") as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT slug, name, role, draft FROM personas ORDER BY id").fetchall()
    personas = [{"slug": row["slug"], "name": row["name"], "role": row["role"], "rules": json.loads(row["draft"])} for row in rows]

    key = getpass.getpass("Gemini key: ")
    partial = root / "generated/gemini-from-grok-drafts-2026-08-24-v2.partial.json"
    posts = {}
    if partial.exists():
        posts = {post["persona_id"]: post for post in json.loads(partial.read_text()).get("posts", [])}
    pending = [persona for persona in personas if persona["slug"] not in posts]
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(request_post, key, persona, old_posts[persona["slug"]], contexts): persona["slug"] for persona in pending}
        for job in concurrent.futures.as_completed(jobs):
            slug = jobs[job]
            try:
                post = job.result()
            except Exception as exc:
                errors[slug] = str(exc)
                print(f"failed {slug}: {exc}")
                continue
            posts[slug] = post
            partial.write_text(json.dumps({"posts": list(posts.values())}, ensure_ascii=False, indent=2) + "\n")
            print(f"done {post['persona_id']} chars={len(post['text'])}")
    if errors:
        raise RuntimeError(json.dumps(errors, ensure_ascii=False))
    order = [persona["slug"] for persona in personas]
    ordered_posts = sorted(posts.values(), key=lambda post: order.index(post["persona_id"]))
    output = root / "generated/gemini-from-grok-drafts-2026-08-24-v2.json"
    output.write_text(json.dumps({"as_of": "2026-08-24 09:20 CST", "posts": ordered_posts}, ensure_ascii=False, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
