from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@[A-Za-z0-9_]+")
SPECIAL_PATTERN = re.compile(r"(?:\$[A-Za-z][A-Za-z0-9]{1,14}|#[\w]+|0x[a-fA-F0-9]{8,})")
WORD_PATTERN = re.compile(r"[a-z][a-z0-9_-]{3,}")
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]{2,}")
STOPWORDS = {
    "that", "this", "with", "from", "have", "will", "your", "about", "just",
    "more", "what", "when", "they", "their", "there", "here", "been", "into",
    "https", "http", "com", "twitter", "crypto", "today", "thread", "update",
}
EVENT_PATTERN = re.compile(
    r"\b(?:launch(?:ed|es|ing)?|release(?:d|s)?|list(?:ed|ing)?|announce(?:d|s)?|"
    r"partner(?:ed|ship)?|integrat(?:e|ed|ion)|acquir(?:e|ed|es)|rais(?:e|ed|es)|"
    r"funding|private beta|public beta|deadline|airdrop|snapshot|"
    r"tokenomics|unlock(?:ed|s|ing)?|proposal|vote|lawsuit|sue[ds]?|filed|deploy(?:ed|s)?)\b|"
    r"上线|发布|融资|合作|收购|测试网上线|主网上线|开启测试网|内测|公测|截止|快照|空投|代币经济|解锁|"
    r"提案|投票|诉讼|起诉|挂牌|新增交易对|开放申请|正式推出",
    re.IGNORECASE,
)
NOISE_PATTERN = re.compile(
    r"\b(?:gm|gn|good morning|happy weekend|vouch|follow back|follow for follow|"
    r"giveaway|drop your wallet|whitelist|iykyk)\b",
    re.IGNORECASE,
)
OPINION_PATTERN = re.compile(
    r"\b(?:because|therefore|however|but|means|the reason|the real|i think|i believe|"
    r"in my view|my thesis|if .{1,80} then|is not .{1,80} but)\b|"
    r"因为|所以|但是|不过|意味着|我认为|我觉得|我的判断|关键在于|本质上|真正的问题|"
    r"不是.{1,40}而是|如果.{1,60}那么|逻辑是|原因是",
    re.IGNORECASE,
)
PROMO_PATTERN = re.compile(
    r"\b(?:giveaway|referral|use my code|sign up|join discord|whitelist|vouch|"
    r"private beta|early access|airdrop task|follow back|drop your wallet|"
    r"ape(?: in)?|send it|buy now|join now|limited spots|best (?:crypto|defi)|"
    r"top \d+ (?:crypto|defi)|how to (?:buy|earn|trade))\b|"
    r"\b(?:trading club|leaderboard|\d+ winners?|share the bounty|rank \d+)\b|"
    r"\b(?:dm me|direct message me|ref(?:erral)? link|drop (?:your )?(?:wallet|address)|"
    r"mint yours?|claim (?:your )?(?:airdrop|allocation)|register (?:for|to).{0,24}sale|"
    r"farm points|don['’]?t miss|please (?:rt|retweet|follow)|claim countdown|community rewards?|"
    r"something new is coming|coming this (?:week|month)|we['’]?re calling|"
    r"(?:subscribe|subscriber|subscription|paid group|private group|signal group)|"
    r"shout-out|link in (?:the )?next (?:post|tweet)|make sure to (?:follow|subscribe))\b|"
    r"\b(?:vote now|head on over|watch (?:the )?video|youtube(?: video)?|telegram|"
    r"join (?:the )?waitlist|new project|early crypto project|caught early|launch packs?|"
    r"created this|application link|register link|get in the rocket ship|running out of time)\b|"
    r"\bca\s*[:：]\s*(?:0x|[1-9A-HJ-NP-Za-km-z])|"
    r"\$[A-Za-z][A-Za-z0-9]{1,14}\s*(?:to|will hit|is going to)\s*\$?\d|"
    r"\b(?:next (?:big )?(?:play|gem)|easy (?:long|short)|guaranteed)\b|"
    r"抽奖|邀请码|返佣|注册链接|关注并转发|空投任务|冲就完了|赶紧买|无脑买|梭哈|进群|"
    r"下一个.{0,8}标的|下一个(?:百倍|千倍)|闭眼(?:买|冲)|排行榜|奖金池|瓜分|"
    r"撸一下|访问官网|完成注册|领取测试币|(?:领取|刷|赚|获得)积分|积分任务|"
    r"订阅区|会员区|付费群|信号群|社区奖励.{0,20}(?:领取|活动|倒计时)|蓝V代开|活动价|"
    r"欢迎收看|视频链接|免费转发器|开启提醒|申请链接|加入候补|早期项目",
    re.IGNORECASE,
)

# A viewpoint is only reusable when its subject is unambiguously crypto.  Generic
# business, AI, equities, and personal-finance posts should never enter this pool.
CRYPTO_ANCHORS = {
    "bitcoin": re.compile(r"\b(?:bitcoin|btc)\b|比特币", re.IGNORECASE),
    "ethereum": re.compile(r"\b(?:ethereum|eth)\b|以太坊", re.IGNORECASE),
    "solana": re.compile(r"(?i:\bsolana\b)|\$SOL\b|solana:[A-Za-z0-9]+|索拉纳"),
    "onchain": re.compile(r"\b(?:onchain|blockchain|web3|defi|nft|dao)\b|链上|区块链|去中心化|公链", re.IGNORECASE),
    "markets": re.compile(
        r"\b(?:crypto|cryptocurrency|stablecoin|memecoin|altcoin|"
        r"perp(?:etual)?s?|dex|cex|airdrop)\b|加密货币|加密市场|稳定币|山寨币|"
        r"永续|去中心化交易所|中心化交易所|空投",
        re.IGNORECASE,
    ),
    "crypto_project": re.compile(
        r"\b(?:binance|coinbase|hyperliquid|pump\.fun|aave|uniswap|"
        r"jupiter|arbitrum|optimism|polymarket|tether|circle)\b|"
        r"\bbase\s+(?:chain|network|ecosystem)\b|base链|币安|欧易|okx|合约交易所",
        re.IGNORECASE,
    ),
}
PERSONAL_TRADE_PATTERN = re.compile(
    r"\b(?:i|we)\s+(?:just\s+)?(?:trade|traded|trading|bought|sold|aped|entered|exited|added|trimmed|"
    r"longed|shorted|held|hold|am holding|made|lost|own|owned)\b|"
    r"\b(?:personally|for me|my strategy)\b[\s\S]{0,80}\b(?:holding|trading|buying|selling|"
    r"dca(?:ing)?|adding|deployed)\b|"
    r"\bi['’]?m\s+(?:\d{1,3}\s*(?:-|–|to)\s*\d{1,3}%?\s+)?(?:deployed|long|short|holding|trading)\b|"
    r"\b(?:made a lot of money|holding it over the bear|trade with me|track my buys?)\b|"
    r"\bmy\s+(?:bag|positions?|entry|pnl|portfolio|holdings?|buys?)\b|"
    r"\bi\s+was\s+(?:adding|dca(?:ing)?)\b|"
    r"\bi\s+(?:can|could|will|would|am|was|have|had|keep|kept|started|stopped|"
    r"plan(?:ned)?|intend|prefer(?:red)?|decid(?:e|ed))\b[\s\S]{0,100}\b"
    r"(?:trade|trading|buy|buying|sell|selling|hold|holding|long|short|position|"
    r"deployed|stack|dca|entry|exit|hedge|calls?|puts?)\b|"
    r"\bi\s+(?:talked|posted|wrote|published|called)[\s\S]{0,80}\b"
    r"(?:trade|call|signal|entry|article|newsletter)\b|"
    r"\b(?:my tp|bought a bag|already have a (?:big )?bag|we buy again|"
    r"my personal position|my lp)\b|"
    r"我.{0,48}?(?:买了|卖了|冲了|梭了|上车|下车|开仓|平仓|加仓|减仓|持仓|换了|换成|"
    r"踏空|套住|套着|不想上|没上|不敢上|赚了|亏了|浮亏|抄底)|"
    r"(?:不想上|没上|不敢上|踏空|换了|换成)[\s\S]{0,240}?(?:我|让我|我的)|"
    r"(?:我自己的双币|我还有筹码|对冲我的现货|之前有抄底)|"
    r"这是我.{0,80}(?:发表|发布|写的|订阅|文章)|"
    r"我.{0,80}(?:提醒(?:大家|小伙伴)|和.{0,20}约定|曾经喊过|之前喊过)|"
    r"我.{0,80}(?:把\s*LP|调仓|止盈|再买|会hold|会\s*hold|保持\s*\d{1,3}\s*[-—至到]\s*\d{1,3}%|"
    r"调整.{0,20}比例)|"
    r"我的(?:仓位|持仓|成本|收益|盈亏|组合)|(?:赚了|浮亏|套着)|"
    r"(?:^|[。！？\n])(?:我)?(?:持仓|开仓|平仓|加仓|减仓|浮亏|亏了|赚了|踏空|抄底).{0,80}",
    re.IGNORECASE,
)
CAUSAL_PATTERN = re.compile(
    r"\b(?:because|therefore|however|while|unless|if|then|depends on|driven by|"
    r"priced in|as long as|rather than|which means|which is why|the key is|catalyst|"
    r"this matters|suggests?|indicates?|leads? to|results? in|due to|as a result|in turn)\b|"
    r"因为|所以|但是|不过|如果|那么|取决于|驱动|定价|意味着|关键在于|前提是|而不是|"
    r"逻辑是|原因是|催化剂|说明|反映|导致|对应|换句话说|相当于",
    re.IGNORECASE,
)
VERIFIABLE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?(?:%|x|k|m|b)?\b|\$[A-Za-z][A-Za-z0-9]{1,14}\b|"
    r"\b(?:mainnet|testnet|governance|proposal|vote|listing|listed|unlock|"
    r"funding|revenue|fees?|volume|open interest|etf|treasury|buyback)\b|"
    r"主网|测试网|治理|提案|投票|上线|上币|解锁|融资|收入|手续费|成交量|持仓量|现货ETF|回购",
    re.IGNORECASE,
)
TRUNCATED_PATTERN = re.compile(
    r"(?:\.\.\.|…|\[\.\.\.\]|查看更多|展开全文|\b(?:probably|unless|because|if|and|or|but|with|to|for|the|a|an))\s*$",
    re.IGNORECASE,
)
OPINION_MIN_SCORE = 12
OPINION_LIMIT = 200
ATTENTION_SAMPLE_LIMIT = 3
ATTENTION_MIN_AUTHORS = 5
DISCUSSION_HOT_MIN_AUTHORS = 5
ATTENTION_TOPIC_PATTERNS = (
    ("bitcoin_etf", "比特币 ETF", re.compile(r"(?:\b(?:bitcoin|btc)\b|比特币).{0,32}\betf\b|\betf\b.{0,32}(?:\b(?:bitcoin|btc)\b|比特币)", re.IGNORECASE)),
    ("ethereum_etf", "以太坊 ETF", re.compile(r"(?:\b(?:ethereum|eth)\b|以太坊).{0,32}\betf\b|\betf\b.{0,32}(?:\b(?:ethereum|eth)\b|以太坊)", re.IGNORECASE)),
    ("clarity_act", "CLARITY Act", re.compile(r"\bclarity(?:\s+act)?\b", re.IGNORECASE)),
    ("genius_act", "GENIUS Act", re.compile(r"\bgenius(?:\s+act)?\b", re.IGNORECASE)),
    ("mica", "MiCA", re.compile(r"\bmica\b", re.IGNORECASE)),
    ("solana", "Solana", re.compile(r"\bsolana\b|\$sol\b|索拉纳", re.IGNORECASE)),
    ("bitcoin", "Bitcoin", re.compile(r"\b(?:bitcoin|btc)\b|比特币", re.IGNORECASE)),
    ("ethereum", "Ethereum", re.compile(r"\b(?:ethereum|eth)\b|以太坊", re.IGNORECASE)),
    ("hyperliquid", "Hyperliquid", re.compile(r"\bhyperliquid\b|\$hype\b", re.IGNORECASE)),
    ("robinhood_chain", "Robinhood Chain", re.compile(r"\brobinhood\s+chain\b", re.IGNORECASE)),
    ("rwa", "RWA / 代币化资产", re.compile(r"\brwa\b|tokeni[sz]ed\s+(?:stocks?|equities|assets?|securities)|代币化(?:股票|证券|资产)", re.IGNORECASE)),
    ("stablecoin", "稳定币", re.compile(r"\bstablecoins?\b|稳定币", re.IGNORECASE)),
    ("meme", "Meme / Memecoin", re.compile(r"\bmeme\s*coins?\b|\bmemecoins?\b|\bmeme\b|迷因币", re.IGNORECASE)),
    ("ai_agent_payments", "AI Agent 支付", re.compile(r"(?:\bai\s+agents?\b|\bagentic\b).{0,48}(?:payments?|wallets?|stablecoins?)|(?:payments?|wallets?|stablecoins?).{0,48}(?:\bai\s+agents?\b|\bagentic\b)", re.IGNORECASE)),
    ("perp_dex", "Perp DEX", re.compile(r"\bperp(?:etual)?\s+dex\b|永续(?:合约)?\s*(?:dex|去中心化交易所)", re.IGNORECASE)),
    ("privacy_coins", "隐私币 / Zcash", re.compile(r"\b(?:zcash|zec|monero|xmr|privacy\s+coins?)\b|隐私币", re.IGNORECASE)),
    ("pump_fun", "Pump.fun", re.compile(r"\bpump\.fun\b|\$pump\b", re.IGNORECASE)),
    ("polymarket", "Polymarket", re.compile(r"\bpolymarket\b", re.IGNORECASE)),
    ("binance", "Binance", re.compile(r"\bbinance\b|币安", re.IGNORECASE)),
    ("coinbase", "Coinbase", re.compile(r"\bcoinbase\b", re.IGNORECASE)),
    ("base", "Base", re.compile(r"\bbase\s+(?:chain|network|ecosystem)\b|\$base\b|base链", re.IGNORECASE)),
    ("aave", "Aave", re.compile(r"\baave\b", re.IGNORECASE)),
    ("uniswap", "Uniswap", re.compile(r"\buniswap\b", re.IGNORECASE)),
    ("jupiter", "Jupiter", re.compile(r"\bjupiter\b", re.IGNORECASE)),
    ("tether", "Tether", re.compile(r"\b(?:tether|usdt)\b", re.IGNORECASE)),
    ("circle", "Circle", re.compile(r"\b(?:circle|usdc)\b", re.IGNORECASE)),
    ("sec", "SEC", re.compile(r"\bsec\b|美国证券交易委员会", re.IGNORECASE)),
    ("cftc", "CFTC", re.compile(r"\bcftc\b", re.IGNORECASE)),
)
PROPOSAL_PATTERN = re.compile(r"\b(?:[a-z]{1,6}-\d{2,})\b", re.IGNORECASE)
CASHTAG_PATTERN = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
CASHTAG_TOPIC_KEYS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "hype": "hyperliquid",
    "pump": "pump_fun",
    "jup": "jupiter",
    "base": "base",
    "usdt": "tether",
    "usdc": "circle",
}
DISCUSSION_ANCHORS = (
    ("market_structure", "价格与市场结构", re.compile(r"\b(?:price|weekly|week|volume|support|resistance|breakout|drawdown|rally|selloff|up|down)\b|价格|周线|成交(?:量|额)|支撑|阻力|突破|回撤|上涨|下跌", re.IGNORECASE)),
    ("etf_flows", "ETF 资金流", re.compile(r"\betf\b.{0,48}\b(?:inflow|outflow|flow|approval|redemption)\b|\b(?:inflow|outflow|flow|approval|redemption)\b.{0,48}\betf\b|ETF.{0,24}(?:流入|流出|申购|赎回|获批)", re.IGNORECASE)),
    ("tokenized_equities", "代币化股票与流动性", re.compile(r"\b(?:rwa|tokeni[sz]ed\s+(?:stocks?|equities|securities)|stock\s+tokens?|amm\s+liquidity)\b|代币化(?:股票|证券|资产)|真实(?:股票|证券)|股票代币|流动性(?:激励|做市)", re.IGNORECASE)),
    ("meme_ecosystem", "Meme 生态与流动性", re.compile(r"\b(?:meme(?:coin)?s?|launchpad|ecosystem|liquidity)\b.{0,48}\b(?:chain|launchpad|ecosystem|liquidity)\b|\b(?:chain|launchpad|ecosystem|liquidity)\b.{0,48}\b(?:meme(?:coin)?s?|launchpad|ecosystem|liquidity)\b|迷因币.{0,24}(?:生态|发射|流动性)|(?:生态|发射|流动性).{0,24}迷因币", re.IGNORECASE)),
    ("leverage", "杠杆与清算", re.compile(r"\b(?:funding|open interest|liquidation|leverage|basis)\b|资金费率|持仓量|爆仓|杠杆|基差", re.IGNORECASE)),
    ("fee_model", "费用与价值归属", re.compile(r"\b(?:fees?|fee switch|burn|resource fee)\b|手续费|费率|收费|销毁|资源费", re.IGNORECASE)),
    ("revenue_buyback", "收入与回购", re.compile(r"\b(?:revenue|buyback|treasury)\b|收入|回购|国库", re.IGNORECASE)),
    ("token_supply", "代币供给", re.compile(r"\b(?:unlock|vesting|tokenomics|issuance|supply)\b|解锁|归属|代币经济|发行|供给", re.IGNORECASE)),
    ("listing_launch", "上线与发行", re.compile(r"\b(?:listing|listed|tge|launch|mainnet|airdrop)\b|上币|上线|主网|空投|发币", re.IGNORECASE)),
    ("governance", "治理提案", re.compile(r"\b(?:proposal|vote|governance)\b|提案|投票|治理", re.IGNORECASE)),
    ("regulation", "监管进展", re.compile(r"\b(?:sec|cftc|regulat(?:ion|or)|bill|law|lawsuit)\b|监管|法案|诉讼|起诉", re.IGNORECASE)),
    (
        "stablecoin_payments",
        "稳定币与支付",
        re.compile(
            r"\b(?:payments?|merchants?|checkout|remittances?)\b"
            r"|\bcross[-\s]border.{0,24}\b(?:payments?|settlement|remittances?)\b"
            r"|\b(?:payments?|settlement|remittances?).{0,24}\bcross[-\s]border\b"
            r"|商户|收款|支付|汇款|跨境.{0,24}(?:支付|结算|收款|汇款)|(?:支付|结算|收款|汇款).{0,24}跨境",
            re.IGNORECASE,
        ),
    ),
)
ATTENTION_PATTERN_BY_KEY = {key: pattern for key, _, pattern in ATTENTION_TOPIC_PATTERNS}
DISCUSSION_PARENT_ALLOWLIST = {
    "market_structure": {"bitcoin", "ethereum", "solana", "hyperliquid", "perp_dex", "privacy_coins", "ticker:*"},
    "etf_flows": {"bitcoin_etf", "ethereum_etf"},
    "tokenized_equities": {"rwa", "robinhood_chain", "binance", "coinbase", "base", "uniswap", "circle", "stablecoin"},
    "meme_ecosystem": {"meme", "pump_fun", "solana", "base", "ticker:*"},
    "stablecoin_payments": {
        "stablecoin", "tether", "circle", "ai_agent_payments", "binance", "coinbase",
    },
    "revenue_buyback": {"hyperliquid", "pump_fun", "aave", "uniswap", "jupiter", "binance", "coinbase", "circle", "base", "robinhood_chain"},
}
BROAD_DISCUSSION_PARENTS = {"bitcoin", "ethereum", "solana", "meme", "stablecoin", "rwa", "privacy_coins"}


def tokens(text: str) -> set[str]:
    clean = MENTION_PATTERN.sub(" ", URL_PATTERN.sub(" ", text.lower()))
    result = {token.lower() for token in SPECIAL_PATTERN.findall(clean)}
    result.update(word for word in WORD_PATTERN.findall(clean) if word not in STOPWORDS)
    for phrase in CJK_PATTERN.findall(clean):
        result.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return result


def similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


class DisjointSet:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def build_cards(rows: list[dict]) -> list[dict]:
    prepared = [(row, tokens(row["text"])) for row in rows]
    prepared = [
        (row, item_tokens)
        for row, item_tokens in prepared
        if not row["is_reply"]
        and len(row["text"]) >= 24
        and len(item_tokens) >= 4
        and not NOISE_PATTERN.search(row["text"])
    ]
    usable = [row for row, _ in prepared]
    token_sets = [item_tokens for _, item_tokens in prepared]
    inverted = defaultdict(list)
    for index, item_tokens in enumerate(token_sets):
        for token in item_tokens:
            inverted[token].append(index)

    pairs = set()
    for indices in inverted.values():
        if 2 <= len(indices) <= 80:
            for position, left in enumerate(indices):
                for right in indices[position + 1:]:
                    if usable[left]["author_id"] != usable[right]["author_id"]:
                        pairs.add((left, right))

    groups = DisjointSet(len(usable))
    for left, right in pairs:
        overlap = token_sets[left] & token_sets[right]
        score = similarity(token_sets[left], token_sets[right])
        special_overlap = any(token.startswith(("$", "#", "0x")) for token in overlap)
        if score >= 0.52 or (special_overlap and score >= 0.28):
            groups.union(left, right)

    clustered = defaultdict(list)
    for index, row in enumerate(usable):
        clustered[groups.find(index)].append((row, token_sets[index]))

    cards = []
    for members in clustered.values():
        authors = {row["author_id"] for row, _ in members}
        if len(authors) < 2 or not any(EVENT_PATTERN.search(row["text"]) for row, _ in members):
            continue
        source_lists = sorted({source for row, _ in members for source in row["source_lists"]})
        shared = set.intersection(*(item_tokens for _, item_tokens in members))
        signals = sorted(token for token in shared if token.startswith(("$", "#", "0x")))
        evidence = sorted(
            (row for row, _ in members), key=lambda row: row["created_at"], reverse=True
        )
        representative = max(evidence, key=lambda row: len(tokens(row["text"])))
        cards.append(
            {
                "status": "corroborated_candidate" if len(authors) >= 3 else "two_source_candidate",
                "author_count": len(authors),
                "post_count": len(evidence),
                "source_lists": source_lists,
                "signals": signals,
                "representative_text": representative["text"],
                "representative_source_ref": representative.get("post_id", ""),
                "representative_handle": representative.get("handle", ""),
                "representative_url": representative.get("url", ""),
                "latest_at": evidence[0]["created_at"],
                "evidence": [
                    {
                        "source_ref": row.get("post_id", ""),
                        "handle": row.get("handle", ""),
                        "url": row.get("url", ""),
                        "text": row["text"],
                        "created_at": row["created_at"],
                        "source_lists": row["source_lists"],
                    }
                    for row in evidence[:12]
                ],
                "score": len(authors) * 4 + min(len(evidence), 12) + len(source_lists) * 2,
            }
        )
    return sorted(cards, key=lambda card: (-card["score"], card["latest_at"]), reverse=False)


def _clean_opinion_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", MENTION_PATTERN.sub("", URL_PATTERN.sub("", text))).strip()


def _crypto_tags(text: str) -> list[str]:
    return [f"crypto:{name}" for name, pattern in CRYPTO_ANCHORS.items() if pattern.search(text)]


def _rejected_opinion(
    *,
    text: str,
    row: dict,
    quality_score: int,
    tags: list[str],
    rejection: str,
) -> dict:
    return {
        "text": text,
        "created_at": row["created_at"],
        "source_lists": row["source_lists"],
        "quality_score": quality_score,
        "tags": tags,
        "rejection": rejection,
    }


def evaluate_opinion(row: dict) -> dict:
    """Classify one source post without carrying author identity into the output."""
    text = _clean_opinion_text(row["text"])
    item_tokens = tokens(text)
    tags = _crypto_tags(text)
    is_reply = bool(row.get("is_reply"))
    truncated = bool(TRUNCATED_PATTERN.search(text))

    if not tags:
        return _rejected_opinion(
            text=text, row=row, quality_score=0, tags=[], rejection="non_crypto"
        )
    if PERSONAL_TRADE_PATTERN.search(text):
        return _rejected_opinion(
            text=text, row=row, quality_score=0, tags=tags, rejection="personal_trade_or_pnl"
        )
    if PROMO_PATTERN.search(text):
        return _rejected_opinion(
            text=text, row=row, quality_score=0, tags=tags, rejection="promotion_or_shill"
        )
    if NOISE_PATTERN.search(text):
        return _rejected_opinion(
            text=text, row=row, quality_score=0, tags=tags, rejection="noise"
        )
    if not 80 <= len(text) <= 1400 or len(item_tokens) < 8:
        return _rejected_opinion(
            text=text, row=row, quality_score=0, tags=tags, rejection="insufficient_context"
        )

    causal_hits = len(CAUSAL_PATTERN.findall(text))
    verifiable_hits = len(VERIFIABLE_PATTERN.findall(text))
    # Named protocols/tickers are independently checkable market objects too.
    concrete_anchor_count = sum(
        1 for tag in tags if tag not in {"crypto:markets", "crypto:onchain"}
    )
    length_score = 2 if len(text) < 160 else 4 if len(text) <= 520 else 3
    quality_score = (
        min(verifiable_hits, 3) * 3
        + min(causal_hits, 3) * 3
        + min(concrete_anchor_count, 2) * 2
        + length_score
    )
    if is_reply:
        quality_score -= 7
        tags.append("penalty:reply")
    if truncated:
        quality_score -= 9
        tags.append("penalty:truncated")

    if not causal_hits and not OPINION_PATTERN.search(text):
        return _rejected_opinion(
            text=text,
            row=row,
            quality_score=quality_score,
            tags=tags,
            rejection="no_reusable_reasoning",
        )
    if quality_score < OPINION_MIN_SCORE:
        return _rejected_opinion(
            text=text,
            row=row,
            quality_score=quality_score,
            tags=tags,
            rejection="below_quality_threshold",
        )

    return {
        "status": "opinion_source",
        "source_ref": row.get("post_id", ""),
        "handle": row.get("handle", ""),
        "url": row.get("url", ""),
        "text": text,
        "created_at": row["created_at"],
        "source_lists": row["source_lists"],
        # Keep score for the current app contract while exposing the explicit name.
        "score": quality_score,
        "quality_score": quality_score,
        "tags": tags,
        "rejection": None,
        "reuse_rule": "Extract only the market viewpoint and rebuild it with current evidence; never reuse personal trade, PnL, or original-author experience.",
    }


def build_opinion_corpus(rows: list[dict], limit: int = OPINION_LIMIT) -> tuple[list[dict], dict[str, int]]:
    cards = []
    rejected = defaultdict(int)
    seen = set()
    for row in rows:
        card = evaluate_opinion(row)
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


def build_opinion_cards(rows: list[dict], limit: int = OPINION_LIMIT) -> list[dict]:
    """Compatibility wrapper for consumers that only need accepted viewpoints."""
    return build_opinion_corpus(rows, limit)[0]


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _topic_entities(text: str) -> list[tuple[str, str]]:
    entities = [(key, title) for key, title, pattern in ATTENTION_TOPIC_PATTERNS if pattern.search(text)]
    keys = {key for key, _ in entities}
    if "bitcoin_etf" in keys:
        entities = [item for item in entities if item[0] != "bitcoin"]
    if "ethereum_etf" in keys:
        entities = [item for item in entities if item[0] != "ethereum"]
    for proposal in PROPOSAL_PATTERN.findall(text):
        key = f"proposal:{proposal.lower()}"
        entities.append((key, proposal.upper()))
    for ticker in CASHTAG_PATTERN.findall(text):
        ticker = ticker.lower()
        key = CASHTAG_TOPIC_KEYS.get(ticker, f"ticker:{ticker}")
        if key not in {item[0] for item in entities}:
            entities.append((key, f"${ticker.upper()}"))
    return list(dict.fromkeys(entities))


def _engagement(metrics: object) -> tuple[int, bool]:
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except json.JSONDecodeError:
            return 0, False
    if not isinstance(metrics, dict) or not metrics:
        return 0, False

    total = 0

    def visit(value: object, name: str = "") -> None:
        nonlocal total
        if isinstance(value, dict):
            for child_name, child in value.items():
                visit(child, str(child_name).lower())
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if any(token in name for token in ("like", "favorite", "retweet", "repost", "reply", "comment", "quote", "bookmark")):
                total += int(value)

    visit(metrics)
    return total, True


def build_attention_topics(rows: list[dict], now: datetime | None = None) -> dict[str, list[dict]]:
    """Group original posts by concrete crypto entities before editorial selection."""
    if now is None:
        now = max((_as_datetime(row["created_at"]) for row in rows), default=datetime.now(timezone.utc))
    now = now.astimezone(timezone.utc)
    window_start = now - timedelta(hours=24)
    usable = []
    for row in rows:
        if (
            _as_datetime(row["created_at"]) < window_start
            or row.get("is_reply")
            or row.get("is_retweet")
            or NOISE_PATTERN.search(row["text"])
        ):
            continue
        entities = _topic_entities(row["text"])
        if entities:
            usable.append((row, entities))
    if not usable:
        return {"hot": [], "niche": []}

    recent_cutoff = now - timedelta(hours=6)
    grouped: dict[str, dict] = {}
    for row, entities in usable:
        for key, title in entities:
            topic = grouped.setdefault(key, {"key": key, "title": title, "rows": []})
            topic["rows"].append(row)

    topics = []
    for topic in grouped.values():
        topic_rows = sorted(topic["rows"], key=lambda row: row["created_at"], reverse=True)
        author_keys = {
            str(row.get("author_id") or row.get("handle") or row.get("post_id"))
            for row in topic_rows
        }
        recent_rows = [row for row in topic_rows if _as_datetime(row["created_at"]) >= recent_cutoff]
        recent_authors = {
            str(row.get("author_id") or row.get("handle") or row.get("post_id"))
            for row in recent_rows
        }
        source_lists = sorted({source for row in topic_rows for source in row.get("source_lists", [])})
        engagements = [_engagement(row.get("metrics")) for row in topic_rows]
        topics.append(
            {
                "key": topic["key"],
                "title": topic["title"],
                "unique_authors": len(author_keys),
                "post_count": len(topic_rows),
                "latest_at": topic_rows[0]["created_at"],
                "recent_6h_authors": len(recent_authors),
                "recent_6h_posts": len(recent_rows),
                "source_lists": source_lists,
                "cross_list_count": len(source_lists),
                "engagement_total": sum(value for value, _ in engagements),
                "engagement_coverage": {
                    "posts_with_metrics": sum(1 for _, available in engagements if available),
                    "post_count": len(topic_rows),
                },
                "sample_refs": [row.get("post_id", "") for row in topic_rows[:ATTENTION_SAMPLE_LIMIT]],
                "sample_posts": [
                    {
                        "source_ref": row.get("post_id", ""),
                        "created_at": row["created_at"],
                        "text": row["text"],
                    }
                    for row in topic_rows[:ATTENTION_SAMPLE_LIMIT]
                ],
            }
        )
    topics.sort(
        key=lambda topic: (
            -topic["unique_authors"],
            -topic["post_count"],
            -topic["recent_6h_authors"],
            -topic["recent_6h_posts"],
            -topic["cross_list_count"],
            -topic["engagement_total"],
            topic["latest_at"],
        )
    )
    return {
        "hot": [topic for topic in topics if topic["unique_authors"] >= ATTENTION_MIN_AUTHORS],
        "niche": [topic for topic in topics if topic["unique_authors"] < ATTENTION_MIN_AUTHORS],
    }


def _discussion_pairs(text: str) -> list[tuple[str, str, str, str]]:
    parents = [item for item in _topic_entities(text) if not item[0].startswith("proposal:")]
    if any(key.startswith("ticker:") for key, _ in parents):
        parents = [item for item in parents if item[0].startswith("ticker:") or item[0] not in BROAD_DISCUSSION_PARENTS]
    sentences = [part for part in re.split(r"[.!?。！？\n]+", text) if part.strip()]
    pairs = []
    for parent_key, parent_title in parents:
        parent_pattern = ATTENTION_PATTERN_BY_KEY.get(parent_key)
        if parent_key.startswith("ticker:"):
            parent_pattern = re.compile(rf"\${re.escape(parent_key.split(':', 1)[1])}\b", re.IGNORECASE)
        if parent_pattern is None:
            continue
        for anchor_key, anchor_title, anchor_pattern in DISCUSSION_ANCHORS:
            allowed = DISCUSSION_PARENT_ALLOWLIST.get(anchor_key)
            if (
                allowed is not None
                and parent_key not in allowed
                and not ("ticker:*" in allowed and parent_key.startswith("ticker:"))
            ):
                continue
            if any(parent_pattern.search(sentence) and anchor_pattern.search(sentence) for sentence in sentences):
                pairs.append((parent_key, parent_title, anchor_key, anchor_title))
    return pairs


def build_discussion_topics(rows: list[dict], now: datetime | None = None) -> dict[str, list[dict]]:
    """Find concrete, repeat-discussed entity-and-mechanism topics suitable for writing."""
    if now is None:
        now = max((_as_datetime(row["created_at"]) for row in rows), default=datetime.now(timezone.utc))
    now = now.astimezone(timezone.utc)
    window_start = now - timedelta(hours=24)
    grouped: dict[str, dict] = {}
    for row in rows:
        if (
            _as_datetime(row["created_at"]) < window_start
            or row.get("is_reply")
            or row.get("is_retweet")
            or NOISE_PATTERN.search(row["text"])
            or PROMO_PATTERN.search(row["text"])
            or PERSONAL_TRADE_PATTERN.search(row["text"])
        ):
            continue
        for parent_key, parent_title, anchor_key, anchor_title in _discussion_pairs(row["text"]):
            key = f"{parent_key}:{anchor_key}"
            topic = grouped.setdefault(
                key,
                {
                    "key": key,
                    "title": f"{parent_title}｜{anchor_title}",
                    "parent": {"key": parent_key, "title": parent_title},
                    "mechanism": {"key": anchor_key, "title": anchor_title},
                    "rows": [],
                },
            )
            topic["rows"].append(row)
    if not grouped:
        return {"hot": [], "niche": []}

    recent_cutoff = now - timedelta(hours=6)
    topics = []
    for topic in grouped.values():
        topic_rows = sorted(topic["rows"], key=lambda row: row["created_at"], reverse=True)
        author_keys = {
            str(row.get("author_id") or row.get("handle") or row.get("post_id"))
            for row in topic_rows
        }
        recent_rows = [row for row in topic_rows if _as_datetime(row["created_at"]) >= recent_cutoff]
        recent_authors = {
            str(row.get("author_id") or row.get("handle") or row.get("post_id"))
            for row in recent_rows
        }
        source_lists = sorted({source for row in topic_rows for source in row.get("source_lists", [])})
        engagements = [_engagement(row.get("metrics")) for row in topic_rows]
        topics.append(
            {
                "key": topic["key"],
                "title": topic["title"],
                "parent": topic["parent"],
                "mechanism": topic["mechanism"],
                "unique_authors": len(author_keys),
                "post_count": len(topic_rows),
                "latest_at": topic_rows[0]["created_at"],
                "recent_6h_authors": len(recent_authors),
                "recent_6h_posts": len(recent_rows),
                "source_lists": source_lists,
                "cross_list_count": len(source_lists),
                "engagement_total": sum(value for value, _ in engagements),
                "engagement_coverage": {
                    "posts_with_metrics": sum(1 for _, available in engagements if available),
                    "post_count": len(topic_rows),
                },
                "sample_refs": [row.get("post_id", "") for row in topic_rows[:ATTENTION_SAMPLE_LIMIT]],
                "sample_posts": [
                    {
                        "source_ref": row.get("post_id", ""),
                        "created_at": row["created_at"],
                        "text": row["text"],
                    }
                    for row in topic_rows[:ATTENTION_SAMPLE_LIMIT]
                ],
            }
        )
    topics.sort(
        key=lambda topic: (
            -topic["unique_authors"],
            -topic["post_count"],
            -topic["recent_6h_authors"],
            -topic["recent_6h_posts"],
            -topic["cross_list_count"],
            -topic["engagement_total"],
            topic["latest_at"],
        )
    )
    return {
        "hot": [topic for topic in topics if topic["unique_authors"] >= DISCUSSION_HOT_MIN_AUTHORS],
        "niche": [topic for topic in topics if topic["unique_authors"] < DISCUSSION_HOT_MIN_AUTHORS],
    }


def cross_validate(db_path: Path, output_dir: Path, hours: int = 30) -> dict:
    generated_at = datetime.now(timezone.utc)
    since = generated_at - timedelta(hours=hours)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM source_posts WHERE created_at>=? ORDER BY created_at DESC",
            (since.isoformat(),),
        ).fetchall()
    posts = [
        {
            **dict(row),
            "is_reply": bool(row["is_reply"]),
            "source_lists": json.loads(row["source_lists"]),
            "metrics": json.loads(row["metrics"]) if row["metrics"] else None,
        }
        for row in rows
    ]
    cards = build_cards(posts)
    opinions, opinion_rejections = build_opinion_corpus(posts)
    attention_topics = build_attention_topics(posts, generated_at)
    discussion_topics = build_discussion_topics(posts, generated_at)
    payload = {
        "generated_at": generated_at.isoformat(),
        "since": since.isoformat(),
        "rule": "Similar original posts from at least two distinct authors; multi-source mention is not final factual confirmation.",
        "source_post_count": len(posts),
        "card_count": len(cards),
        "cards": cards,
        "opinion_count": len(opinions),
        "opinions": opinions,
        "opinion_filter": {
            "version": "fresh_corpus_filter_design",
            "limit": OPINION_LIMIT,
            "minimum_quality_score": OPINION_MIN_SCORE,
            "requires": ["crypto_anchor", "verifiable_information_or_causal_reasoning"],
            "excludes": ["promotion_or_shill", "personal_trade_or_pnl", "non_crypto"],
            "penalties": {"reply": 7, "truncated": 9},
        },
        "opinion_rejection_counts": opinion_rejections,
        "attention_topics": attention_topics,
        "discussion_topics": discussion_topics,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "fact_cards.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 每日多源事实候选",
        "",
        "规则：至少两个不同作者发布相似内容。多人转述不等于最终事实，发帖前仍需查看原始证据。",
        "",
    ]
    for index, card in enumerate(cards, 1):
        lines.extend(
            [
                f"## {index}. {card['status']}｜{card['author_count']} 位作者｜{card['post_count']} 条证据",
                card["representative_text"],
                "",
                *[f"- {item['created_at']}：{item['text']}" for item in card["evidence"]],
                "",
            ]
        )
    (output_dir / "fact_cards.md").write_text("\n".join(lines), encoding="utf-8")
    opinion_lines = [
        "# 每日高质量观点候选",
        "",
        "规则：只保留观点，按人设重新表达；不得提及或推断原作者。",
        "",
    ]
    for index, card in enumerate(opinions, 1):
        opinion_lines.extend(
            [
                f"## {index}. 评分 {card['score']}",
                card["text"],
                "",
            ]
        )
    (output_dir / "opinion_cards.json").write_text(
        json.dumps(
            {
                "generated_at": payload["generated_at"],
                "source_post_count": len(posts),
                "opinion_count": len(opinions),
                "opinions": opinions,
                "opinion_filter": payload["opinion_filter"],
                "opinion_rejection_counts": opinion_rejections,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "opinion_cards.md").write_text("\n".join(opinion_lines), encoding="utf-8")
    attention_payload = {
        "generated_at": payload["generated_at"],
        "window_start": (generated_at - timedelta(hours=24)).isoformat(),
        "window_end": payload["generated_at"],
        "hours": 24,
        "source_post_count": len(posts),
        "topics": attention_topics["hot"],
        "hot": attention_topics["hot"],
        "niche": attention_topics["niche"],
    }
    (output_dir / "attention_topics.json").write_text(
        json.dumps(attention_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    attention_lines = [
        "# 今日母池讨论热点",
        "",
        f"默认选题只从 hot 中挑：至少 {ATTENTION_MIN_AUTHORS} 位不同作者在 24 小时内讨论。低于门槛的技术帖保留在 niche，不进入日常主选题。",
        "",
    ]
    for index, topic in enumerate(attention_topics["hot"], 1):
        attention_lines.extend(
            [
                f"## {index}. {topic['title']}｜{topic['unique_authors']} 位作者｜{topic['post_count']} 条原帖",
                f"近 6 小时：{topic['recent_6h_authors']} 位作者 / {topic['recent_6h_posts']} 条｜跨列表：{topic['cross_list_count']}｜互动：{topic['engagement_total']}",
                *[f"- {sample['created_at']}：{sample['text']}" for sample in topic["sample_posts"]],
                "",
            ]
        )
    if attention_topics["niche"]:
        attention_lines.extend(["## Niche（不默认用于主选题）", ""])
        attention_lines.extend(
            f"- {topic['title']}｜{topic['post_count']} 条原帖 / {topic['unique_authors']} 位作者"
            for topic in attention_topics["niche"]
        )
    (output_dir / "attention_topics.md").write_text(
        "\n".join(attention_lines), encoding="utf-8"
    )
    discussion_payload = {
        "generated_at": payload["generated_at"],
        "window_start": (generated_at - timedelta(hours=24)).isoformat(),
        "window_end": payload["generated_at"],
        "hours": 24,
        "source_post_count": len(posts),
        "hot": discussion_topics["hot"],
        "niche": discussion_topics["niche"],
    }
    (output_dir / "discussion_topics.json").write_text(
        json.dumps(discussion_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    discussion_lines = [
        "# 今日可写讨论议题",
        "",
        f"默认成稿只从 hot 中挑：实体必须与具体事件或机制共现，并至少有 {DISCUSSION_HOT_MIN_AUTHORS} 位不同作者讨论。",
        "",
    ]
    for index, topic in enumerate(discussion_topics["hot"], 1):
        discussion_lines.extend(
            [
                f"## {index}. {topic['title']}｜{topic['unique_authors']} 位作者｜{topic['post_count']} 条原帖",
                f"近 6 小时：{topic['recent_6h_authors']} 位作者 / {topic['recent_6h_posts']} 条｜跨列表：{topic['cross_list_count']}",
                *[f"- {sample['created_at']}：{sample['text']}" for sample in topic["sample_posts"]],
                "",
            ]
        )
    if discussion_topics["niche"]:
        discussion_lines.extend(["## Niche（不默认用于成稿）", ""])
        discussion_lines.extend(
            f"- {topic['title']}｜{topic['post_count']} 条原帖 / {topic['unique_authors']} 位作者"
            for topic in discussion_topics["niche"]
        )
    (output_dir / "discussion_topics.md").write_text(
        "\n".join(discussion_lines), encoding="utf-8"
    )
    return {
        "source_posts": len(posts),
        "fact_cards": len(cards),
        "opinion_cards": len(opinions),
        "attention_topics": len(attention_topics["hot"]),
        "niche_topics": len(attention_topics["niche"]),
        "discussion_topics": len(discussion_topics["hot"]),
        "niche_discussion_topics": len(discussion_topics["niche"]),
        "opinion_rejections": opinion_rejections,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(cross_validate(args.db, args.output, args.hours)))


if __name__ == "__main__":
    main()
