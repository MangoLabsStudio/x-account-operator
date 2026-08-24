#!/usr/bin/env python3
import argparse
import concurrent.futures
import getpass
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path


API = "https://www.micuapi.ai/v1/chat/completions"

EMPTY_WAITING_PHRASES = (
    "继续观察",
    "等后续材料",
    "等待更多信息",
    "在那之前",
    "有待观察",
    "下一观察点",
    "还没有形成独立的交易条件",
    "尚未形成独立的交易条件",
    "还没有形成可执行条件",
    "尚未形成可执行条件",
)


VERIFIED_FACTS = {
    "btc_structure": [
        {"id": "BTC1", "statement": "截至2026-08-24 08:52 CST，Binance BTCUSDT现货为77441.82 USDT，24小时涨0.145%，高低为78052.85/75545.67，成交额约12.00亿USDT。", "source": "Binance public spot API"},
        {"id": "BTC2", "statement": "已完成的2026-08-17至08-23 UTC周K：开62900、高79500、低62751.1、收77734，周涨23.58%；该周现货成交额约122.04亿USDT，为此前四个完整周均值2.14倍。", "source": "Binance public spot klines"},
        {"id": "BTC3", "statement": "同一周，Binance BTCUSDT现货主动买入成交额占比约51.64%。", "source": "Binance public spot klines"},
        {"id": "BTC4", "statement": "BTCUSDT U本位永续的BTC计价OI从110943 BTC降至105531 BTC，约-4.88%；受币价变化影响，同窗USDT名义OI从69.76亿升至82.01亿，约+17.57%。两种口径不能混写，也不能据此单独判断新增多头或全面去杠杆。", "source": "Binance public futures API"},
        {"id": "BTC5", "statement": "最近8个Binance BTCUSDT永续资金费率周期均为0.0100%；过去7个完整日的合约主动买卖比前五日偏买方，最近两日为0.956和0.981。", "source": "Binance public futures API"}
    ],
    "native_meme": [
        {"id": "MEME1", "statement": "Hyperliquid官方公告将PURR描述为其首个原生现货发行；没有公开销售或计划用途。初始分配中500M给Points持有者，100M通过HIP-2进入链上订单簿，原HIP-2配置中另400M被销毁。", "source": "Hyperliquid official announcements and HIP docs"},
        {"id": "MEME2", "statement": "Coinbase将一个BASECAT资产加入listing roadmap只代表进入评估/路线图，不等于已经开放交易，也不等于Base官方背书或官方吉祥物。", "source": "Coinbase official listing roadmap"}
    ],
    "political_meme": [
        {"id": "POL1", "statement": "SEC在2026-08-18提出Regulation Crypto Assets，针对涉及crypto assets的某些investment contracts设计发行框架；它仍处于提案与征求意见阶段，并非已生效规则。", "source": "https://www.sec.gov/newsroom/press-releases/2026-76-sec-proposes-new-regulation-crypto-assets"},
        {"id": "POL2", "statement": "提案包含四年内累计最高500万美元的一项豁免，以及每12个月最高7500万美元、附带财务和持续报告要求的另一项豁免；评论期为Federal Register刊登后60天。", "source": "SEC proposal release 2026-76"},
        {"id": "POL3", "statement": "具体Meme币及其发行或销售方式是否涉及证券法仍需按事实判断，不能泛化为所有Meme币都不受证券法规制。", "source": "SEC 2025 meme coin staff statement and 2026 interpretation framework"}
    ],
    "rwa_tokenization": [
        {"id": "RWA1", "statement": "RWA产品通常把法律权利、发行与托管、合规转让限制、二级交易、定价和赎回拆成不同层；具体权利和路径取决于产品文件，资产上链本身不保证二级流动性。", "source": "Product structures and industry documentation"},
        {"id": "RWA2", "statement": "Franklin Templeton于2026-08-12向SEC staff提交no-action request；现有文件证明的是请求已经提交，不能写成SEC已经批准或已经发出no-action relief。", "source": "SEC incoming request PDF"}
    ],
    "hyperliquid_frontends": [
        {"id": "HL1", "statement": "Hyperliquid Builder Codes允许前端或应用在代用户提交的订单上附加builder地址和费用；用户须用主钱包预先批准该builder的最高费率，并可撤销。", "source": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes"},
        {"id": "HL2", "statement": "每个用户最多同时保留10个有效builder授权；builder须维持至少100 USDC永续账户价值。", "source": "Hyperliquid Builder Codes docs"},
        {"id": "HL3", "statement": "Builder fee上限为永续0.1%、现货1%；永续两侧都可收，现货买方不收。费用按订单设置并在链上处理。", "source": "Hyperliquid Builder Codes docs"},
        {"id": "HL4", "statement": "HyperCore包含链上永续和现货订单簿；HyperCore与HyperEVM由同一HyperBFT共识保障。具体跨层读写能力仍须按网络和当期上线状态确认。", "source": "Hyperliquid official HyperCore/HyperEVM docs"}
    ],
    "mechanism_products": [
        {"id": "SOL1", "statement": "SGP-0003提议把现行每签名5000 lamports、50%销毁/50%给leader的基础费，改为每笔2500 lamports且100%给leader的inclusion fee，再加按requested cost units收取且100%销毁的resource fee。", "source": "Solana SGP-0003 frozen proposal"},
        {"id": "SOL2", "statement": "Resource fee按requested而非consumed cost units收取，计划以1/10、1/4、1/2 lamport三档feature gate推进；priority fee规则不变。", "source": "Solana SIMD-0553/SGP-0003"},
        {"id": "SOL3", "statement": "SGP PR仍为Open，相关SIMD-0553已Merged；链上formal voting window为epoch 1021至1024。即使投票通过，也只是授权沿SIMD/feature-gate流程推进，不会自动改写主网费率。", "source": "Solana official governance repo and issue 141"},
        {"id": "PM1", "statement": "Polymarket的outcomes与outcomePrices一一对应；Gamma用于发现events/markets，CLOB提供token级报价和订单簿。", "source": "https://docs.polymarket.com/market-data/overview"},
        {"id": "PM2", "statement": "midpoint是最优bid和ask的平均；spread是best ask减best bid。若spread大于0.10美元，Polymarket界面会改显示last traded price，因此屏幕概率不一定是可立刻成交的价格。", "source": "https://docs.polymarket.com/concepts/prices-orderbook"},
        {"id": "PM3", "statement": "官方当前/price契约写明BUY返回best bid、SELL返回best ask。Gamma的lifetime volume/liquidity、24h/周/月/年volume以及CLOB/AMM拆分字段不能混称；判断可成交量应读订单簿。", "source": "Polymarket official API reference"}
    ]
}


TOPICS = {
    "acheng": ("native_meme", "把近期链原生猫系Meme讨论转成普通参与者能用的筛选逻辑；不装作买过。"),
    "ridehail-driver-zhao": ("btc_structure", "用普通人的风险承受视角解释本周BTC快速上涨与日内波动，不虚构载客或持仓。"),
    "college-student-linjia": ("rwa_tokenization", "从Franklin提交no-action request切入，讲清资产上链、合规准入和可交易流动性不是一件事。"),
    "atuo": ("hyperliquid_frontends", "判断Builder Codes为什么把交易执行和前端分发拆开，以及商业化成立还缺什么指标。"),
    "axu": ("btc_structure", "用Binance同窗数据解释BTC计价OI与USDT名义OI为何给出不同表象，并给出下一验证条件。"),
    "nanqiao": ("mechanism_products", "写Polymarket数据产品：接口200、展示概率和可执行报价为什么是三层。"),
    "qiliang": ("political_meme", "写事件交易如何把SEC提案事实、政策叙事和Meme注意力拆开，不写具体买卖。"),
    "aye": ("native_meme", "写链生态为什么不断寻找吉祥物Meme，以及官方关联、社区投射、流动性之间的错位。"),
    "xiaoman": ("mechanism_products", "写SGP-0003对开发者与生态激励的影响，强调投票、技术文档和主网激活是三个阶段。"),
    "maili": ("btc_structure", "写普通交易者如何理解本周强涨后的风险；只表达判断，不虚构自己的成交、仓位或收益。")
}


def call_gemini(key, persona, context):
    topic_id, assignment = TOPICS[persona["slug"]]
    payload = {
        "persona": persona,
        "assignment": assignment,
        "grok_research": context[topic_id],
        "verified_facts": VERIFIED_FACTS[topic_id],
    }
    prompt = """你是中文 Crypto KOL 编辑。现在由 Grok 完成了 X/Web 研究，你负责把它写成一条完整帖子。

信息权限：
1. verified_facts 是唯一可写成确定事实、日期或数字的材料。
2. grok_research 只用于理解行业背景、圈内默认认知、争议、反方和未知项。Grok 里的任何数字、日期、官方关系、价格、因果或事件，只要没有同时出现在 verified_facts，就不得写进正文。
3. X 上多个账号重复同一句话仍只是市场观点，不是事实。

写作要求：
- 只写一条220至450个中文字符的帖子，不要标题、编号、项目符号、hashtag、来源列表或免责声明。
- 文本必须完整：具体触发或问题背景 → 为什么会产生这个争议 → 该账号当前能成立的明确判断与现实后果。反证条件只有能增加判断价值时才写，不要突然抛一段抽象规则。
- 第一或第二句给判断，但不要使用“不是X而是Y”“先否定再肯定”“一方面另一方面”的模板。
- 保留足够 Crypto context，让业内读者感觉作者知道前情；同时只围绕一条主线。
- PERSONA只决定观察角度和语气，不是生活事实。严禁补造任何个人经历、场景、对话、朋友、乘客、外卖订单、学校课程、项目实测、持仓、成交、收益或亏损。
- 第一人称只允许分析立场，例如“我更关注”“我的判断是”“我会把X作为验证条件”；禁止“我今天看了/我试了/我买了/我卖了/我的仓位”。
- 不喊单，不诱导交易，不替项目背书；观点要有信息量，但不要堆数字和术语。
- 结尾必须落在当前判断、现实后果或具体动作。禁止用“继续观察”“等后续材料”“在那之前”“尚未形成交易条件”等等待句代替结论；素材不足时改选一个能下结论的角度。

只输出合法JSON：
{"persona_id":"...","text":"...","facts_used":["fact_id"],"stance":"一句话判断","risk_flags":[]}

INPUT:\n""" + json.dumps(payload, ensure_ascii=False)
    body = {
        "model": "gemini-3.1-pro-preview",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.85,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }
    last_error = None
    for attempt in range(2):
        request = urllib.request.Request(
            API,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
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
            parsed["persona_id"] = persona["slug"]
            if any(phrase in parsed.get("text", "") for phrase in EMPTY_WAITING_PHRASES):
                raise ValueError("draft used empty waiting language")
            return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            body["temperature"] = 0.2
            body["messages"][0]["content"] += "\n上次输出不合格：JSON必须合法，正文还必须给出当前结论，不能用等待后续或继续观察收尾。重新生成。"
    raise last_error


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Historical Gemini/Grok static-topic batch replay only."
    )
    parser.add_argument(
        "--legacy-static-topics",
        action="store_true",
        help="replay the fixed 2026-08-24 topic assignments",
    )
    args = parser.parse_args(argv)
    if not args.legacy_static_topics:
        parser.error(
            "static topic assignments are disabled. Generate from the app's "
            "daily attention_topics and Context Pack instead; pass "
            "--legacy-static-topics only to replay this historical batch."
        )

    root = Path(__file__).resolve().parents[1]
    grok_rows = json.loads((root / "generated/grok-context-2026-08-24.json").read_text())
    contexts = {row["id"]: {"text": row["text"], "citations": row["citations"]} for row in grok_rows}
    with sqlite3.connect(root / "data/xops.db") as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT slug, name, role, draft FROM personas ORDER BY id").fetchall()
    personas = []
    for row in rows:
        draft = json.loads(row["draft"])
        personas.append({"slug": row["slug"], "name": row["name"], "role": row["role"], "rules": draft})

    key = getpass.getpass("Gemini key: ")
    partial = root / "generated/gemini-from-grok-drafts-2026-08-24.partial.json"
    posts = {}
    if partial.exists():
        posts = {post["persona_id"]: post for post in json.loads(partial.read_text()).get("posts", [])}
    pending = [persona for persona in personas if persona["slug"] not in posts]
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(call_gemini, key, persona, contexts): persona["slug"] for persona in pending}
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
    output = root / "generated/gemini-from-grok-drafts-2026-08-24.json"
    output.write_text(json.dumps({"as_of": "2026-08-24 09:10 CST", "posts": ordered_posts}, ensure_ascii=False, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
