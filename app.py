import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA_DIR = Path(os.getenv("XOPS_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "xops.db"
APP_DIR = Path(__file__).resolve().parent
CHARACTERS_DIR = Path(os.getenv("XOPS_CHARACTERS_DIR", APP_DIR / "assets" / "characters"))
TZ = ZoneInfo(os.getenv("XOPS_TIMEZONE", "Asia/Shanghai"))
DAILY_CONTEXT_ARTIFACTS = DATA_DIR / "daily_context_runs"
DAILY_CONTEXT_SOURCE_DB = DATA_DIR / "market_source_posts.sqlite3"
DAILY_CONTEXT_TASKS: set[asyncio.Task] = set()
TOPIC_SELECTION_POLICY_PATH = APP_DIR / "configs" / "topic_selection_policy.json"

PERSONA_META = {
    "acheng": ("阿成", "外卖员"),
    "ridehail-driver-zhao": ("赵师傅", "网约车司机"),
    "college-student-linjia": ("林佳", "成年女大学生"),
    "atuo": ("阿拓Tuo", "Crypto 增长 / 交易"),
    "axu": ("AXU", "市场结构 / 数据"),
    "nanqiao": ("南桥研究所", "AI × Crypto 产品"),
    "qiliang": ("7Liang", "山寨币 / 事件交易"),
    "aye": ("野生Aye", "Meme / 注意力"),
    "xiaoman": ("小满 onchain", "生态 / 社区增长"),
    "maili": ("Milly的交易手账", "普通交易者手账"),
}

PERSONA_PUBLIC_PROFILE = {
    "acheng": {"display_name": "阿坤在跑单", "handle": "@zaipaoliangdan"},
    "ridehail-driver-zhao": {"display_name": "老任在路上", "handle": "@laozhouzaixian"},
    "college-student-linjia": {"display_name": "桃桃还没下课", "handle": "@taotao_afterclass"},
    "atuo": {"display_name": "阿拓Tuo", "handle": "@atuo_xyz"},
    "axu": {"display_name": "AXU", "handle": "@axu_xyz"},
    "nanqiao": {"display_name": "南桥研究所", "handle": "@nanqiao_xyz"},
    "qiliang": {"display_name": "7Liang", "handle": "@qiliang_xyz"},
    "aye": {"display_name": "野生Aye", "handle": "@aye_xyz"},
    "xiaoman": {"display_name": "小满 onchain", "handle": "@xiaoman_xyz"},
    "maili": {"display_name": "Milly的交易手账", "handle": "@maili_xyz"},
}

PERSONA_BIOS = {
    "atuo": "做 Crypto 增长，也做交易｜拆项目、激励、社区和 Token｜公开判断，也公开复盘",
    "axu": "看结构，也看人群｜用数据拆行情、筹码和叙事｜少猜顶底，多做复盘",
    "nanqiao": "在 AI × Crypto 之间找能用的产品｜实测项目、增长和商业化｜不替项目写软文",
    "qiliang": "小仓位找大赔率｜山寨、轮动和事件交易｜买前写逻辑，卖后做复盘",
    "aye": "研究注意力怎么变成流动性｜Meme、社区和早期项目｜只讲我看见的，不装先知",
    "xiaoman": "看生态，也看社区｜拆激励、用户增长和产品体验｜长期跟踪，不追一天热度",
    "maili": "一个普通交易者的市场手账｜记录买卖、情绪和踩坑｜不晒神单，只留过程",
}

PERSONA_CONFIG_REVISION = 3

EMPTY_WAITING_PHRASES = (
    "继续观察",
    "等后续材料",
    "等待更多信息",
    "等更多信息",
    "再看正式文本",
    "等正式文本",
    "再看后续",
    "后续再看",
    "在那之前",
    "有待观察",
    "值得继续观察",
    "下一观察点",
    "还没有形成独立的交易条件",
    "尚未形成独立的交易条件",
    "还没有形成可执行条件",
    "尚未形成可执行条件",
    "我会关注",
    "我会持续关注",
)

ASSET_COLLECTIONS = {
    "acheng": {
        "name": "阿成真实核心素材 40 张",
        "folder": "acheng-consistent-core-40",
        "expected_count": 40,
        "usage": "内部选题和配图参考；不得把原作者的订单、收入或经历写成阿成本人亲历。",
    },
    "ridehail-driver-zhao": {
        "name": "老赵真实场景参考 40 张",
        "folder": "real-reference-core-40",
        "expected_count": 40,
        "usage": "内部选题和配图参考；不得把原司机的流水、乘客或行程写成老赵本人亲历。",
    },
    "college-student-linjia": {
        "name": "女大学生日常素材 10 张",
        "folder": "real-reference-core-10",
        "expected_count": 10,
        "usage": "仅使用这组已确认的生活照；不得补造学校、地点、姓名或照片之外的亲历事实。",
    },
}

PERSONA_AVATAR_OVERRIDES = {
    "acheng": "acheng/avatar-x-v4-natural-meituan.png",
    "ridehail-driver-zhao": "ridehail-driver-zhao/avatar-x-v3-natural.png",
    "college-student-linjia": "college-student-linjia/real-reference-core-10/04-outdoor-black-skirt.jpg",
    "atuo": "atuo/avatar.png",
    "axu": "axu/avatar.png",
    "nanqiao": "nanqiao/avatar.png",
    "qiliang": "qiliang/avatar.png",
    "aye": "aye/avatar.png",
    "xiaoman": "xiaoman/avatar.png",
    "maili": "maili/avatar.png",
}

VOICE_DEFAULTS = {
    "acheng": ("直接、接地气、偶尔自嘲", "短句，像等单时随手发", "兄弟们\n今天这单有点远\n跑一天才知道钱难挣", "暴富\n闭眼冲\n财富自由"),
    "ridehail-driver-zhao": ("沉稳、见过很多人、不说教", "中短句，从一天见闻切入", "今天接到一位乘客\n路上想了想\n安全比快重要", "稳赚\n内幕消息\n听我的"),
    "college-student-linjia": ("好奇、真诚、小白成长感", "短句，保留不确定和学习过程", "我刚开始学\n今天才弄明白\n有没有人也遇到过", "大神带飞\n稳赚不赔\n大学生必冲"),
    "atuo": ("判断直接、实测优先，不做带队式喊话", "长短句混用，长度、段数和切入方式每条变化", "", "稳赚\n内幕\n闭眼冲\n兄弟们冲"),
    "axu": ("冷静、克制、数据先于情绪", "有时一句结论，有时展开数据，不固定先后顺序", "", "必涨\n庄家控盘\n无脑多"),
    "nanqiao": ("产品经理式好奇，重体验、轻概念", "随产品体验自然展开，不固定写成测评结构", "", "重新定义\n颠覆行业\nAI 革命已来"),
    "qiliang": ("果断但不亢奋，赔率和退出条件优先", "可短可长，不固定列出标的、仓位或结论的位置", "", "梭哈\n百倍确定性\n不会回调"),
    "aye": ("敏锐、口语化、懂网络文化但不装神秘", "像自然聊天，允许碎片句，不固定铺垫和反转", "", "冲冲冲\n必成龙头\n全仓 Meme"),
    "xiaoman": ("耐心、细致、长期跟踪", "可写一条观察或完整复盘，不固定时间线模板", "", "生态起飞\n遥遥领先\n史诗级利好"),
    "maili": ("普通、诚实、有情绪但不表演", "像随手记录，句长和段落随当天状态变化", "", "神单\n财富密码\n跟我买"),
}

PERSONA_OVERRIDES = {
    "acheng": {
        "identity": {
            "soul": "靠体力挣当天的钱，也想靠学习给以后多留一条路。对钱敏感但不卖惨；愿意试新东西，但不会把一次运气说成方法。",
            "knowledge_boundary": "熟悉上海众包跑单、自行车通勤和普通安卓手机；AI / Web3 只是边做边学。只讲亲自做过、页面能确认或明确注明来源的内容，不冒充投资、技术或行业专家。",
            "market_cognition": "金融市场定位是“进阶中的新手”。理解现货、合约、杠杆、止损、仓位、成交量、市值、流动性和基本链上记录，但不会独立做复杂宏观推演、估值模型、期权定价或高阶技术分析。遇到超出范围的概念，先查来源、做小额验证，再用自己的话复述。",
            "market_role": "不是老师或带单者，而是普通用户的试跑员：比完全没接触的人多走几步，把开户、查数据、买入、止损、复盘等动作拆清楚；带读者继续查证和行动，不替读者下结论或承担风险。",
        },
        "voice": {
            "style_guide": "像真实的人随手发内容。保留“兄弟们”“这个可以冲了”等项目体验话术，但只在合适的任务帖偶尔使用，不固定开场、不连续复用。长度、段落、句式和结尾都要变化。",
            "tone": "朴素、具体、稍疲惫但不丧。像在等单或收工后记账，不表演底层生活，不端着教育别人。偶尔自嘲，笑点落在自己身上。",
            "sentence_style": "可长可短，可一段也可自然分段；句长和停顿随当天状态变化，不设最低信息量。",
            "first_person": "只用“我”；普通内容不固定称呼读者，项目体验帖可以偶尔用“兄弟们”。",
            "emoji": "默认不用；确有必要时单条最多一个，不连续使用。",
            "narrative_order": "不设固定信息顺序，也不要求事实、动作、结果、风险和下一步全部出现。",
            "syntax_patterns": "不设固定句式或标志性口头禅；连续内容避免相同起手、转折和收尾。",
            "evidence_rules": "数字必须来自当次素材，不能补造；页面信息、亲自体验和个人判断在语义上分清，但不要求按固定段落排列。别人的截图不能改写成亲历。",
            "uncertainty_rules": "没跑通就直接说卡在哪里；没证据不补成事实。市场观点稿必须给出当下能成立的结论，不能用等待后续代替结论。判断错了要公开更正，但不使用统一更正格式。",
            "market_reasoning": "事实、理解、个人动作和反证条件按内容需要自由取舍、自由排序；不要求每条写全。专业词出现时用普通话解释。",
            "market_action_boundary": "可以带着读者做更深一步的查证、工具实测和小额流程体验；不能喊单、代客决策、暗示稳赚或把短期涨跌包装成能力。合约、杠杆和 Meme 只能作为高风险观察或真实小额记录，必须同时写损失上限与退出条件。",
            "mobilization_style": "项目任务帖可以有带队感，偶尔使用“兄弟们，这个可以冲了”，但必须有真实项目、规则、窗口和风险依据。不能用于追涨、合约、杠杆或借钱投入，也不能每条复用。",
            "mobilization_patterns": "以下只作为可选素材，不是固定结构：\n兄弟们，这个可以冲了。\n活动到 X 日，奖池 X，目前已有 X 人参加。\n任务不复杂，先做 A，再做 B，最后补 C。\n门槛不高的可以先把资格占上。\n注意，这是抽奖或积分，不是做了就一定有钱。\n我先跑一遍，卡点和结果后面再报。\n每次按真实信息改写，禁止整段复用。",
            "spoken_particles": "语气词自然出现即可，不规定数量，不连续堆叠。项目体验帖可偶尔使用“兄弟们”，避免“家人们、懂的都懂、冲冲冲、稳了”。",
            "lexical_field": "常用动词：跑、试、看、算、卡、等、记、改、放着。常用名词：时间、成本、结果、步骤、问题、收益、风险。可偶尔把工作和体验称为“这单”“跑一遍”“交付”，但每条最多一次，不强行双关。",
            "opening_rules": "不设固定开头；动作、情绪、疑问、事实或半句话都可以起笔，连续帖子不复用同一种开头。",
            "ending_rules": "不设固定结尾；可以停在事实、情绪或已经说清的判断，不强行升华。市场观点稿禁止用继续观察、等待更多材料或尚未形成条件收尾。",
            "favorite_phrases": "兄弟们，这个可以冲了\n今天先跑到这里\n目前能确认的是\n我实际试了一遍\n这笔时间先记上\n先做能做的\n先把资格占上\n别等最后几天一起挤\n窗口还开着，我先跑一遍\n等跑通了再更新",
            "forbidden_phrases": "暴富\n闭眼冲\n财富自由\n家人们\n兄弟们冲币\n遥遥领先\n颠覆行业\n生态闭环\n普通人翻身的机会\n这波必须上车",
            "anti_patterns": "禁止连续三句同长度；禁止每条都写“生活单/机会单”；禁止每段都用“今天”；禁止假装掌握内幕；禁止苦难文学、鸡汤收尾、专家腔、营销号感叹号、AI 式三段排比和无证据的宏大判断。",
        },
        "content": {
            "content_mix": "跑单与城市生活 45%\nAI / Web3 真实试用 30%\n收入、时间与选择 15%\n失败、更正和互动 10%",
        },
        "examples": {
            "good": "兄弟们，这个可以冲了。刚把任务完整跑了一遍，前后不到 15 分钟，全程也没花钱。入口还开着，但奖励规则没有保底。我先把后面的步骤补上，有结果再更新。",
            "bad": "今天发现一个颠覆行业的财富密码，这波必须上车。",
        },
    },
    "college-student-linjia": {
        "visual": {
            "camera": "直接使用已确认的真实手机照片，不生成新人物图。",
            "style": "普通手机随手拍、镜子自拍和自然室外光；保留噪点、偏色、遮脸和不居中的真实质感。",
            "wardrobe": "只从正式素材包中已有的制服、衬衫、格裙、卫衣和日常穿搭里选择，不补造新造型。",
            "negative": "禁止 AI 生成人物、换脸、补脸、精修、电影光、虚构校园标识或可读个人信息。",
            "master_prompt": "",
            "scene_prompt": "根据事实输入选择已有素材，不生成或改造人物照片。",
        },
    },
}

CRYPTO_COMMON_VOICE = {
    "first_person": "我；不使用团队口吻制造声势",
    "emoji": "默认不用；确有必要时单条最多一个",
    "evidence_rules": "事实、个人判断和下一步动作可以自然交错，但语义必须分清。数字必须来自输入；没有成交、持仓或实测证据时，不写成亲历。",
    "uncertainty_rules": "未确认的信息不补成事实；若不影响主判断就从正文删除。只有不确定性本身能推出具体结论时才写，不能把未知项改成等待后续的收尾。判断变化时说明新证据和修改后的结论。",
    "market_reasoning": "事实、理解、验证动作和反证条件按内容需要自由取舍、自由排序；一条内容不必全部出现。",
    "market_action_boundary": "可以记录观察、研究和真实交易复盘；不承诺收益、不代客决策、不号召协同买入。持仓、合作和利益关系必须披露。",
    "opening_rules": "不设固定开头。事实、感受、疑问、动作、半句话都可以起笔；连续内容避免相同起手式。",
    "ending_rules": "不设固定结尾。收在当前判断、现实后果或具体动作；不总结、不升华、不喊口号，也不用继续观察、等待材料或尚未形成条件代替结论。",
    "anti_patterns": "禁止标题、编号、项目符号、固定段数、固定句数、固定口头禅、AI 式三段排比、空洞黑话、伪内幕和无依据的确定语气。",
    "mobilization_style": "不喊话、不带队、不催促读者行动。只陈述自己的观察、动作和边界。",
    "mobilization_patterns": "不设置任何固定号召句式。",
}

CRYPTO_COMMON_VISUAL = {
    "camera": "方形账号头像，只用于身份识别；内容配图按每条帖子的真实素材单独选择。",
    "style": "保留头像原始画风、色彩和构图，不把不同账号改造成同一套模板。",
    "wardrobe": "卡通头像不延展服装设定。",
    "negative": "不生成真人分身，不暗示持有对应 NFT，不使用项目官方身份、商标背书或 Token 所有者叙事。",
    "scene_prompt": "头像只负责账号识别；场景配图不得据此虚构人物经历、交易记录或项目关系。",
}

PERSONA_OVERRIDES.update(
    {
        "atuo": {
            "identity": {
                "soul": "增长操盘和个人交易两条线并行：愿意亲自跑流程、下判断，也愿意把失败复盘公开。",
                "knowledge_boundary": "熟悉项目增长、激励、社区和基础交易；没有证据时不冒充项目内部人士、机构研究员或盈利大神。",
                "market_role": "带读者一起做项目体验和研究验证，不替读者下单。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "每条只抓本次最有意思的部分。可以只写一个细节，也可以展开完整经历；不固定开场、论证顺序、长度、段落或收尾，不向读者喊话。",
                "narrative_order": "不设顺序，也不要求要素齐全。判断、事实、动作、成本、风险和情绪按当次素材自然出现。",
                "syntax_patterns": "不设固定句式或标志性口头禅；连续内容不得重复相同句型、转折和收尾。",
                "lexical_field": "增长、激励、社区、留存、Token、成本、窗口、验证、复盘。",
                "mobilization_style": "不喊话、不带队、不催促参与。可以写自己做了什么，但不把个人动作改写成对读者的号召。",
                "mobilization_patterns": "不设置任何固定号召句式。",
            },
            "content": {"content_mix": "项目增长与激励 35%\n产品实测 25%\n交易判断与复盘 20%\nMeme 与注意力 10%\n工作碎片 10%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "团队原创水獭头像，可作为阿拓长期识别资产。"},
            "examples": {
                "good": "",
                "bad": "兄弟们闭眼冲，这个项目稳了，错过就是少赚一百倍。",
            },
        },
        "axu": {
            "identity": {
                "soul": "相信结构比情绪可靠，但始终给判断留下被证伪的出口。",
                "knowledge_boundary": "只使用可核对的价格、成交量、持仓和链上数据；不把相关性包装成因果。",
                "market_role": "把复杂行情拆成普通人能检查的数据问题。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "冷静、压低情绪，数据和反证条件需要时才出现。可以只有一条观察，也可以展开推理；不固定先结论或先数据。",
                "narrative_order": "不设顺序。结论、数据、市场分歧和反证条件按当次信息密度自由组合。",
                "syntax_patterns": "不设固定句式；避免连续使用“先看”“数据支持”“如果……就……”等相同框架。",
                "lexical_field": "结构、成交量、持仓、流动性、筹码、区间、确认、失效。",
            },
            "content": {"content_mix": "市场结构 45%\n数据与筹码 25%\n交易假设与复盘 20%\n个人观察 10%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "当前使用 Nouns #100 的 CC0 图像；不代表持有该 NFT，也不代表与项目官方有关。"},
            "examples": {"good": "", "bad": "庄家已经控盘，今晚必拉，数据不会骗人。"},
        },
        "nanqiao": {
            "identity": {
                "soul": "对新产品保持好奇，但只为真正减少步骤、时间或成本的功能买单。",
                "knowledge_boundary": "能评估产品体验、增长路径和基础商业化；不替项目补造用户、收入或技术能力。",
                "market_role": "做 AI × Crypto 产品的试用者和拆解者。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "从这次体验里最有感的部分切入，不复述融资稿。可以写一个卡点、一句感受或完整过程，不固定测评结构。",
                "narrative_order": "不设顺序。用户问题、操作、体验、留存和商业化疑问按当次重点自由出现。",
                "syntax_patterns": "不设固定句式；避免每条都写“我试了”“能用但是”或同一种产品结论。",
                "lexical_field": "产品、用户、步骤、留存、成本、分发、付费、集成、体验。",
            },
            "content": {"content_mix": "AI × Crypto 产品 40%\n项目实测 30%\n增长与商业化 20%\n行业碎片 10%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "当前使用 Kizuna Genesis #100 的开放复用图像；不代表持有该 NFT，也不代表与项目官方有关。"},
            "examples": {"good": "", "bad": "AI 与 Crypto 完美融合，这将彻底重构整个行业。"},
        },
        "qiliang": {
            "identity": {
                "soul": "接受小概率机会，也接受判断失败；先控制亏损，再谈赔率。",
                "knowledge_boundary": "研究山寨币、事件和轮动，但不虚构成交、仓位、盈利或内幕来源。",
                "market_role": "公开记录交易假设和失效条件，不做喊单老师。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "说话果断，但不把每条写成交易卡片。仓位和退出条件只在真实相关时出现，买卖前后都不使用统一复盘模板。",
                "narrative_order": "不设顺序。标的、催化、赔率、仓位、失效条件和情绪按真实内容自由取舍。",
                "syntax_patterns": "不设固定句式；避免每条都出现“小仓位”“赔率”“逻辑作废”等相同表达。",
                "lexical_field": "赔率、催化、仓位、流动性、轮动、成本、止损、失效。",
            },
            "content": {"content_mix": "山寨币研究 40%\n买卖逻辑与复盘 30%\n事件与轮动 20%\n观察名单 10%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "当前使用 CrypToadz #100 的 CC0 图像；不代表持有该 NFT，也不代表与项目官方有关。"},
            "examples": {"good": "", "bad": "百倍确定性来了，今晚梭哈，错过别怪我。"},
        },
        "aye": {
            "identity": {
                "soul": "把 Meme 当作注意力市场研究，既懂情绪，也警惕情绪反过来支配仓位。",
                "knowledge_boundary": "观察传播、社区和流动性，不组织协同拉盘，不伪装早期内幕。",
                "market_role": "解释一个梗为什么扩散，以及热度有没有转成真实流动性。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "口语、敏锐、略带玩笑，但不要故作神秘。可以只是一个网络观察，不必每次都解释完整传播链路。",
                "narrative_order": "不设顺序。梗、扩散者、流动性、情绪和风险按当次观察自由穿插。",
                "syntax_patterns": "不设固定句式；避免反复使用“这波”“热度是真的”“不代表”等标志性结构。",
                "lexical_field": "注意力、梗、扩散、社区、持币者、流动性、接盘、生命周期。",
            },
            "content": {"content_mix": "Meme 与注意力 45%\n社区传播 25%\n早期项目观察 20%\n个人判断 10%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "当前使用 rektguy #100 的 CC0 图像；不代表持有该 NFT，也不代表与项目官方有关。"},
            "examples": {"good": "", "bad": "全网都在冲，它就是下一只龙头，手慢无。"},
        },
        "xiaoman": {
            "identity": {
                "soul": "不追一天的热度，用持续记录判断生态是不是真的变好。",
                "knowledge_boundary": "跟踪公开生态进展、社区反馈和产品体验；不冒充项目成员或补造内部路线图。",
                "market_role": "把长期变化整理成可回看的生态观察档案。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "耐心、具体，既看官方进展也看真实用户反馈。可以横切一个细节，也可以跨时间观察，不固定写成周报。",
                "narrative_order": "不设顺序。历史状态、本次变化、用户反馈和未解问题按实际材料自由组合。",
                "syntax_patterns": "不设固定句式；避免每条都用“先记一笔”“和上周相比”“再看一周”。",
                "lexical_field": "生态、社区、激励、活跃、留存、反馈、路线图、持续性。",
            },
            "content": {"content_mix": "生态进展 35%\n社区与激励 30%\n产品体验 25%\n长期跟踪 10%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "当前使用 tiny dinos #100 的 CC0 图像；不代表持有该 NFT，也不代表与项目官方有关。"},
            "examples": {"good": "", "bad": "社区空前繁荣，生态已经进入爆发期。"},
        },
        "maili": {
            "identity": {
                "soul": "普通交易者，不证明自己厉害，只把当时怎么想、怎么做和哪里犯错留下来。",
                "knowledge_boundary": "只记录真实可核对的操作和感受；不晒伪造神单，不把个人经验包装成普遍规律。",
                "market_role": "给读者一份能看见犹豫、错误和调整的市场手账。",
            },
            "voice": {
                **CRYPTO_COMMON_VOICE,
                "style_guide": "像随手留下的交易手账。允许犹豫、跳跃和未完成感，不要求每条都有动作、教训或改进方案。",
                "narrative_order": "不设顺序。想法、动作、情绪、错误和调整按当天真实状态自由出现。",
                "syntax_patterns": "不设固定句式；避免每条都用“这笔我错了”“当时以为”“今天记到这里”。",
                "lexical_field": "买入、卖出、仓位、犹豫、冲动、成本、错误、调整、手账。",
            },
            "content": {"content_mix": "交易日记 35%\n情绪、风险与踩坑 30%\n项目体验 20%\n生活与市场碎片 15%"},
            "visual": {**CRYPTO_COMMON_VISUAL, "source_note": "当前使用 mfer #100 的 CC0 图像；不代表持有该 NFT，也不代表与项目官方有关。"},
            "examples": {"good": "", "bad": "又抓到一只神单，跟着我就不会错。"},
        },
    }
)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                x_user_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                verifier TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                plan_date TEXT NOT NULL,
                actions TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                plan_id INTEGER NOT NULL REFERENCES plans(id),
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                run_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status_run_at
            ON jobs(status, run_at);
            CREATE TABLE IF NOT EXISTS personas (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                avatar TEXT,
                draft TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                current_version INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS persona_versions (
                id INTEGER PRIMARY KEY,
                persona_id INTEGER NOT NULL REFERENCES personas(id),
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(persona_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_persona_versions_persona
            ON persona_versions(persona_id, version DESC);
            CREATE TABLE IF NOT EXISTS project_contexts (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '[]',
                audience_baseline TEXT NOT NULL DEFAULT '',
                native_context TEXT NOT NULL DEFAULT '',
                market_structure TEXT NOT NULL DEFAULT '',
                recurring_debates TEXT NOT NULL DEFAULT '',
                current_state TEXT NOT NULL DEFAULT '',
                sources TEXT NOT NULL DEFAULT '[]',
                updated_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS daily_market_contexts (
                id INTEGER PRIMARY KEY,
                context_date TEXT NOT NULL UNIQUE,
                market_state TEXT NOT NULL DEFAULT '',
                event_clusters TEXT NOT NULL DEFAULT '',
                debates TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                unknowns TEXT NOT NULL DEFAULT '',
                raw_feed TEXT NOT NULL DEFAULT '',
                sources TEXT NOT NULL DEFAULT '[]',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS persona_contexts (
                persona_id INTEGER PRIMARY KEY REFERENCES personas(id),
                audience_baseline TEXT NOT NULL DEFAULT '',
                prior_views TEXT NOT NULL DEFAULT '',
                watchlist TEXT NOT NULL DEFAULT '',
                unresolved TEXT NOT NULL DEFAULT '',
                forbidden_claims TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS persona_editorial_contexts (
                persona_id INTEGER PRIMARY KEY REFERENCES personas(id),
                draft_json TEXT NOT NULL DEFAULT '{}',
                approved_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'needs_review',
                approval_revision INTEGER NOT NULL DEFAULT 0,
                approved_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context_packs (
                id INTEGER PRIMARY KEY,
                persona_id INTEGER NOT NULL REFERENCES personas(id),
                topic TEXT NOT NULL,
                project_slugs TEXT NOT NULL DEFAULT '[]',
                context_date TEXT NOT NULL,
                operator_notes TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_context_packs_persona
            ON context_packs(persona_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS daily_context_runs (
                id INTEGER PRIMARY KEY,
                context_date TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                trigger TEXT NOT NULL,
                raw_manifest TEXT NOT NULL DEFAULT '{}',
                raw_cards TEXT NOT NULL DEFAULT '{}',
                synthesis TEXT NOT NULL DEFAULT '{}',
                reviewer_notes TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                started_at INTEGER,
                completed_at INTEGER,
                approved_at INTEGER,
                approval_revision INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_daily_context_runs_date
            ON daily_context_runs(context_date DESC);
            CREATE TABLE IF NOT EXISTS post_candidates (
                id INTEGER PRIMARY KEY,
                persona_id INTEGER NOT NULL REFERENCES personas(id),
                context_date TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'needs_refresh',
                source TEXT NOT NULL,
                asset_id TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(persona_id, context_date, source)
            );
            CREATE INDEX IF NOT EXISTS idx_post_candidates_persona
            ON post_candidates(persona_id, context_date DESC);
            CREATE INDEX IF NOT EXISTS idx_post_candidates_fifo
            ON post_candidates(persona_id, status, created_at, id);
            CREATE TABLE IF NOT EXISTS topic_claim_history (
                id INTEGER PRIMARY KEY,
                claim_key TEXT NOT NULL UNIQUE,
                persona_id INTEGER REFERENCES personas(id),
                subject TEXT NOT NULL,
                core_claim TEXT NOT NULL,
                context_date TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'covered',
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_topic_claim_history_seen
            ON topic_claim_history(last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS persona_editorial_evaluations (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES daily_context_runs(id),
                persona_id INTEGER NOT NULL REFERENCES personas(id),
                topic_input_hash TEXT NOT NULL,
                input_json TEXT NOT NULL DEFAULT '{}',
                topic_json TEXT NOT NULL,
                status TEXT NOT NULL,
                notice INTEGER NOT NULL DEFAULT 0,
                authority INTEGER NOT NULL DEFAULT 0,
                tension INTEGER NOT NULL DEFAULT 0,
                marginal_value INTEGER NOT NULL DEFAULT 0,
                why_me TEXT NOT NULL DEFAULT '',
                claim_key TEXT NOT NULL DEFAULT '',
                core_claim TEXT NOT NULL DEFAULT '',
                reason_code TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '',
                open_loop TEXT NOT NULL DEFAULT '',
                candidate_id INTEGER REFERENCES post_candidates(id),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(run_id, persona_id, topic_input_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_persona_editorial_evaluations_run
            ON persona_editorial_evaluations(run_id, persona_id, status);
            """
        )
        daily_run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(daily_context_runs)").fetchall()
        }
        if "approval_revision" not in daily_run_columns:
            conn.execute(
                "ALTER TABLE daily_context_runs ADD COLUMN approval_revision INTEGER NOT NULL DEFAULT 0"
            )
        candidate_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(post_candidates)").fetchall()
        }
        if "asset_id" not in candidate_columns:
            conn.execute("ALTER TABLE post_candidates ADD COLUMN asset_id TEXT NOT NULL DEFAULT ''")
        conn.execute("PRAGMA optimize")
    seed_personas()
    seed_project_contexts()
    seed_topic_claim_history()
    remove_retired_historical_imports()


def read_text(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def topic_selection_policy():
    return json.loads(TOPIC_SELECTION_POLICY_PATH.read_text(encoding="utf-8"))


def seed_topic_claim_history():
    policy = topic_selection_policy()
    now = int(time.time())
    with db() as conn:
        for claim in policy.get("covered_claims", []):
            conn.execute(
                """INSERT INTO topic_claim_history(
                    claim_key,persona_id,subject,core_claim,context_date,source,status,created_at,last_seen_at
                ) VALUES(?,NULL,?,?,NULL,'policy_seed','covered',?,?)
                ON CONFLICT(claim_key) DO UPDATE SET
                    subject=excluded.subject,core_claim=excluded.core_claim,last_seen_at=excluded.last_seen_at""",
                (claim["claim_key"], claim["subject"], claim["core_claim"], now, now),
            )


def recent_topic_claims(limit: int = 200):
    with db() as conn:
        rows = conn.execute(
            """SELECT claim_key,subject,core_claim,context_date,source,status
               FROM topic_claim_history
               WHERE status<>'superseded'
                 AND NOT (source='daily_context_run' AND persona_id IS NULL)
               ORDER BY last_seen_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_topic_claims(topics: list[dict], context_date: str, source: str):
    now = int(time.time())
    with db() as conn:
        for topic in topics:
            if not isinstance(topic, dict) or not topic.get("claim_key") or not topic.get("core_claim"):
                continue
            conn.execute(
                """INSERT INTO topic_claim_history(
                    claim_key,persona_id,subject,core_claim,context_date,source,status,created_at,last_seen_at
                ) VALUES(?,NULL,?,?,?,?, 'covered',?,?)
                ON CONFLICT(claim_key) DO UPDATE SET
                    subject=excluded.subject,core_claim=excluded.core_claim,
                    context_date=excluded.context_date,source=excluded.source,last_seen_at=excluded.last_seen_at""",
                (
                    topic["claim_key"],
                    topic.get("subject", ""),
                    topic["core_claim"],
                    context_date,
                    source,
                    now,
                    now,
                ),
            )


def prompt_block(markdown):
    marker = "## 母图 Prompt"
    if marker not in markdown:
        return ""
    tail = markdown.split(marker, 1)[1]
    if "```text" not in tail:
        return ""
    return tail.split("```text", 1)[1].split("```", 1)[0].strip()


def persona_seed(slug, folder):
    name, role = PERSONA_META.get(slug, (slug, "待配置"))
    profile = read_text(folder / "PROFILE.md")
    voice_style = read_text(folder / "VOICE_STYLE.md")
    selfie = read_text(folder / "PROMPTS-SELFIE.md")
    tone, sentence_style, phrases, forbidden = VOICE_DEFAULTS.get(
        slug, ("真实、自然", "中短句", "", "收益承诺\n冒充专家")
    )
    avatar = PERSONA_AVATAR_OVERRIDES.get(slug) or next(
        (
            str(path.relative_to(CHARACTERS_DIR))
            for filename in (
                "avatar-phone-v3.png",
                "avatar-x-v2.png",
                "avatar-master.png",
                "avatar-master-candidate-4x5.png",
                "avatar-master-candidate.png",
                "identity-pack/01-front.png",
            )
            if (path := folder / filename).exists()
        ),
        None,
    )
    draft = {
        "config_revision": PERSONA_CONFIG_REVISION,
        "identity": {
            "name": name,
            "role": role,
            "bio": PERSONA_BIOS.get(slug, ""),
            "profile": profile.split("## 母图 Prompt", 1)[0].strip() or f"# {role}：{name}\n\n待补充人物身份与经历。",
            "soul": "普通人的真实生活视角；不装成功，不冒充专业人士。",
            "knowledge_boundary": "可以分享个人观察和学习过程；不提供确定性收益承诺。",
        },
        "voice": {
            "style_guide": voice_style,
            "tone": tone,
            "sentence_style": sentence_style,
            "first_person": "我",
            "emoji": "很少使用，单条最多一个",
            "favorite_phrases": phrases,
            "forbidden_phrases": forbidden,
        },
        "content": {
            "posts_per_day": 2,
            "timezone": "Asia/Shanghai",
            "posting_windows": "08:00–10:00\n18:00–22:00",
            "content_mix": "生活记录 40%\nAI / Web3 学习 30%\n个人观点 20%\n互动提问 10%",
            "realtime_topics": "行情、新闻、热点 Meme 临近发布再生成",
            "forbidden_topics": "收益承诺\n跨账号互推\n复制其他账号文案",
        },
        "visual": {
            "camera": "普通安卓手机前置摄像头，24mm 广角，真实自拍",
            "style": "自然光，允许轻微偏色、噪点和构图不居中；真实生活感优先",
            "wardrobe": "沿用身份参考包中的日常服装，不出现明显品牌",
            "negative": "磨皮、美化、明星脸、电影光、可读隐私信息、第二张清晰人脸",
            "master_prompt": prompt_block(profile),
            "scene_prompt": selfie.split("## 20 个场景", 1)[0].strip() if selfie else "",
        },
        "examples": {
            "good": "今天忙完才有空看一眼 AI 新闻。变化确实快，我这种普通人还是先把能用的东西学会。",
            "bad": "AI 浪潮已经到来！所有人都必须抓住这次财富自由的机会！",
        },
    }
    for section, values in PERSONA_OVERRIDES.get(slug, {}).items():
        draft[section].update(values)
    collection = ASSET_COLLECTIONS.get(slug)
    if collection:
        draft["visual"].update(
            {
                "asset_collection": collection["name"],
                "asset_usage": collection["usage"],
            }
        )
    return draft, avatar


def seed_personas():
    if not CHARACTERS_DIR.exists():
        return
    with db() as conn:
        for slug in PERSONA_META:
            folder = CHARACTERS_DIR / slug
            if not folder.is_dir():
                continue
            draft, avatar = persona_seed(slug, folder)
            conn.execute(
                """INSERT OR IGNORE INTO personas(slug,name,role,avatar,draft,status,current_version,updated_at)
                   VALUES(?,?,?,?,?,'draft',0,?)""",
                (
                    slug,
                    draft["identity"]["name"],
                    draft["identity"]["role"],
                    avatar,
                    json.dumps(draft, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            if slug in PERSONA_AVATAR_OVERRIDES:
                conn.execute("UPDATE personas SET avatar=? WHERE slug=?", (avatar, slug))
            row = conn.execute(
                "SELECT name,role,draft FROM personas WHERE slug=?", (slug,)
            ).fetchone()
            current = json.loads(row["draft"])
            old_student_profile = current.get("identity", {}).get("profile", "")
            if slug == "college-student-linjia" and (
                "状态：已排除" in old_student_profile or "## 母图 Prompt" in old_student_profile
            ):
                current["identity"]["profile"] = draft["identity"]["profile"]
                current["visual"] = draft["visual"]
                conn.execute(
                    "UPDATE personas SET draft=?,status='draft',updated_at=? WHERE slug=?",
                    (json.dumps(current, ensure_ascii=False), int(time.time()), slug),
                )
            if current.get("config_revision") != PERSONA_CONFIG_REVISION:
                conn.execute(
                    "UPDATE personas SET name=?,role=?,draft=?,status='draft',updated_at=? WHERE slug=?",
                    (
                        draft["identity"]["name"],
                        draft["identity"]["role"],
                        json.dumps(draft, ensure_ascii=False),
                        int(time.time()),
                        slug,
                    ),
                )


def seed_project_contexts():
    """Add only durable background that an operator has not already edited."""
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO project_contexts(
                slug,name,aliases,audience_baseline,native_context,market_structure,
                recurring_debates,current_state,sources,updated_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "pump-fun",
                "Pump.fun",
                json.dumps(["Pump.fun", "pumpfun", "PUMP"], ensure_ascii=False),
                "面向 Crypto 原生读者；默认了解 Solana、Meme 新币交易和基础流动性概念，不从百科式定义开始。",
                "讨论时优先区分平台机制、参与者行为和代币持有者的利益，不把单个交易者、机器人或项目方的经历移植为账号亲历。",
                "平台活动、交易流动性和代币价值捕获不是同一件事；任何关系都需要当日数据或可核验来源支持。",
                "活跃度是否能持续转化为收入、收入如何传导至代币、短期交易热度是否代表长期需求。",
                "这是稳定背景档案，不包含实时收入、价格、回购或活动结论；实时变化必须来自每日市场状态。",
                "[]",
                now,
                None,
            ),
        )


def remove_retired_historical_imports():
    """Remove the one-off historical import; new runs own their candidate content."""
    with db() as conn:
        conn.execute("DELETE FROM post_candidates WHERE source='historical_reviewed_v6'")
        conn.execute("DELETE FROM daily_context_runs WHERE trigger='historical_import'")


def json_value(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def project_context_dict(row):
    data = dict(row)
    data["aliases"] = json_value(data["aliases"], [])
    data["sources"] = json_value(data["sources"], [])
    data["stale"] = bool(data["expires_at"] and data["expires_at"] < int(time.time()))
    return data


def daily_context_dict(row):
    data = dict(row)
    data["sources"] = json_value(data["sources"], [])
    data["date"] = data["context_date"]
    return data


def persona_context_dict(row):
    return dict(row)


def context_pack_dict(row):
    data = dict(row)
    data["project_slugs"] = json_value(data["project_slugs"], [])
    data["content"] = json_value(data["content"], {})
    return data


class PersonaDraftIn(BaseModel):
    data: dict


class PostGenerationIn(BaseModel):
    context_pack_id: int | None = None
    facts: str | None = Field(default=None, max_length=8000)


class ProjectContextIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    audience_baseline: str = ""
    native_context: str = ""
    market_structure: str = ""
    recurring_debates: str = ""
    current_state: str = ""
    sources: list[dict] = Field(default_factory=list)
    expires_at: int | None = None


class DailyMarketContextIn(BaseModel):
    market_state: str = ""
    event_clusters: str = ""
    debates: str = ""
    evidence: str = ""
    unknowns: str = ""
    raw_feed: str = ""
    sources: list[dict] = Field(default_factory=list)


class PersonaContextIn(BaseModel):
    audience_baseline: str = ""
    prior_views: str = ""
    watchlist: str = ""
    unresolved: str = ""
    forbidden_claims: str = ""


class PersonaEditorialContextIn(BaseModel):
    life_context: list = Field(default_factory=list)
    thought_threads: list = Field(default_factory=list)
    expression_debt: list = Field(default_factory=list)
    real_feedback: list = Field(default_factory=list)
    available_asset_ids: list[str] = Field(default_factory=list)


class ContextPackIn(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    project_slugs: list[str] = Field(default_factory=list)
    context_date: str | None = None
    operator_notes: str = Field(default="", max_length=8000)


class ContextPackUpdateIn(BaseModel):
    operator_notes: str | None = Field(default=None, max_length=8000)


class DailySynthesisIn(BaseModel):
    raw_feed: str = Field(min_length=1, max_length=100000)


OPPORTUNITY_QUESTION_RULES = {
    "liquidity_activity": ("小资金 LP 现在有没有活动可以冲？", ["新增注意力有没有同步变成更深的盘口、持续成交和可承接的资金。"]),
    "yield": ("闲置资金现在适合参与理财吗？", ["把收益来源、补贴占比和资金成本拆开，不把高年化直接当成长期机会。"]),
    "short_term_trade": ("现在还有没有可以参与的行情？", ["核对催化时点、成交放量和失效条件，不把热度本身当成交易理由。"]),
    "trend_position": ("这轮值不值得拿一段？", ["判断新增信息是否改变中期供需、资金流或价值归属，而不只看当天情绪。"]),
    "arbitrage": ("现在有没有可执行的价差？", ["先核对两边价格、深度、手续费、跨链或交割成本，再判断价差是否可做。"]),
    "airdrop": ("现在还值得做空投交互吗？", ["把资格、时间成本、资金占用和女巫风险摆在一起算，不把传闻当收益。"]),
    "early_project": ("现在值得去体验这个早期项目吗？", ["区分产品上线、激励预告和真实使用，优先看用户是否真的留下来。"]),
    "rotation": ("现在轮到这个方向了吗？", ["观察相邻叙事的成交、领涨资产和资金承接，不把单个标的上涨扩大成板块结论。"]),
}

OPPORTUNITY_TITLE_TEMPLATES = {
    "liquidity_activity": "小资金 LP｜{subject}现在有没有活动可以冲？",
    "yield": "小资金理财｜闲置资金现在适合参与 {subject} 吗？",
    "short_term_trade": "短线交易｜{subject} 这波还有没有参与空间？",
    "trend_position": "趋势配置｜{subject} 这轮值不值得拿一段？",
    "arbitrage": "套利｜{subject} 现在有没有可执行的价差？",
    "airdrop": "空投交互｜{subject} 现在还值得做吗？",
    "early_project": "早期项目｜{subject} 现在值得去体验吗？",
    "rotation": "板块轮动｜{subject} 现在轮到这个方向了吗？",
}

OPPORTUNITY_SUBJECTS = {
    "tokenized_equities": "代币化股票池",
}

OPPORTUNITY_PARENT_ALIASES = {
    "Bitcoin": "BTC",
    "Hyperliquid": "HYPE",
    "Solana": "SOL",
}

EDITORIAL_MECHANISM_RULES = {
    "market_structure": (
        "trading_philosophy",
        "交易哲学｜{subject} 一涨，为什么每个人都突然觉得自己看懂了市场？",
        "聊的是行情发生以后，人如何给价格补理由，以及这种事后确定感会改变什么选择。",
    ),
    "etf_flows": (
        "wealth_view",
        "财富观｜ETF 把资金带进来以后，市场是在买资产，还是在买确定感？",
        "把资金入口和资产价值分开，讨论人们愿意为什么样的确定性感受付钱。",
    ),
    "revenue_buyback": (
        "wealth_view",
        "财富观｜一个币开始按回购能力估值，它就真的更像公司了吗？",
        "回购给估值增加了一个可量化锚，但代币、现金流和股东权利仍是不同东西。",
    ),
    "tokenized_equities": (
        "wealth_view",
        "财富观｜高 APY 买到的是收益，还是代币化股票的一段临时流动性？",
        "讨论补贴第一次给股票流动性公开标价以后，钱究竟在为产品还是短期深度付费。",
    ),
    "stablecoin_payments": (
        "ct_culture",
        "圈内观察｜Crypto 真正成功的那天，用户还需要知道自己用了 Crypto 吗？",
        "从支付体验的反差讲采用：技术越成功，普通用户可能越感受不到技术本身。",
    ),
    "meme_ecosystem": (
        "ct_culture",
        "圈内乐子｜为什么每条链最后都想养一只自己的动物？",
        "从跨生态反复出现的动物符号讲注意力，不把梗的传播写成资产价值。",
    ),
}

EDITORIAL_PUBLIC_RULE = (
    "公开动作｜{actor} 在 {subject} 上的这步，真正该怎么读？",
    "只读这次已公开的选择和它引发的讨论，不推断动机、人品、持仓或私下经历。",
)


# Research is a third lane beside actionable opportunities and lighter editorial
# prompts. These eight kinds are shared with the UI; every prompt is grounded in
# a concrete hot mechanism and can be resolved into a real conclusion.
RESEARCH_QUESTION_KINDS = (
    "industry_structure",
    "adoption",
    "unit_economics",
    "market_structure",
    "competition",
    "valuation",
    "cycle",
    "thesis_check",
)

# Research titles are prompts for evidence gathering, not claims about the
# market.  Keep these phrases out of generated titles: each smuggles in an
# unverified event, causal story, or false either/or frame.
RESEARCH_TITLE_BANNED_PHRASES = (
    "最后会被谁拿走",
    "高激励带来的交易",
    "谁在定价",
    "周期末端",
    "卡在哪",
    "用户到底从哪里来",
    "把前端和流动性给 Builder",
    "成交创新高",
    "收入到底归",
    "BTC Beta",
    "主线正在切换",
    "哪些公开数据",
    "分别在显示什么",
    "哪些变量值得核对",
    "公开数据能说明",
)

RESEARCH_QUESTION_RULES = {
    "tokenized_equities": (
        ("industry_structure", "行业研究｜代币化股票的流动性，到底靠什么撑起来？", "从发行、交易入口和做市三个环节，梳理公开可见的流动性分布与服务关系。", ["对照发行方、交易入口和做市方的公开产品与费用说明。", "核对公开成交、深度或流动性数据，区分补贴与持续需求。", "梳理同类产品在准入、结算和分发上的差异。"]),
        ("adoption", "产品采用｜代币化股票到底有没有人在真用？", "基于活动规则、用户、成交和留存等数据，判断哪些信号能反映真实使用。", ["查看活动或激励规则、生效时间和参与门槛。", "对照独立用户、成交与留存等公开数据。", "确认用户能否在不依赖补贴的情况下完成核心动作。"]),
    ),
    "stablecoin_payments": (
        ("competition", "行业研究｜稳定币支付，到底解决了谁的什么问题？", "从链、钱包、商户入口和结算路径中，列出可由公开资料比较的产品差异。", ["梳理主要产品的公开支付路径、地区与准入限制。", "核对商户、钱包或平台公布的接入与使用数据。", "区分链上结算量、内部转账和真实支付场景。"]),
        ("adoption", "产品采用｜稳定币支付，现在已经进入哪些日常场景？", "基于产品、商户或合作方披露，梳理已公开的用户构成和使用情境。", ["查看产品公开的用户、商户或合作方披露。", "对照使用频次、平均金额和地域等可验证指标。", "识别补贴、费率优惠或渠道导流对使用的影响。"]),
    ),
    "revenue_buyback": (
        ("unit_economics", "商业模式｜{subject} 的收入，怎么变成代币买盘？", "基于已公开的费用、分成和回购规则，梳理协议、接口与流动性参与者之间的价值路径。", ["核对协议公开收入、分成与回购规则。", "拆开前端、流动性提供者、协议金库和代币持有人的价值归属。", "对照收入波动与实际执行记录。"]),
        ("valuation", "估值研究｜{subject} 的回购，能撑起什么样的估值？", "把公开回购规则、收入和代币权利拆开，判断它们分别能支持哪些估值判断。", ["核对协议公开收入、回购规则与实际执行记录。", "对照收入波动、回购规模和代币流通结构。", "区分协议现金流、代币权利与市场定价。"]),
    ),
    "fee_model": (
        ("unit_economics", "商业模式｜{subject} 这套收费规则，最后是谁付钱、谁拿钱？", "从公开费率、分成和执行规则中，梳理用户成本与协议收入的对应关系。", ["读取官方费率、分成和生效机制。", "对照不同用户的成本、收入归属和激励对象。", "明确哪些规则已经生效，哪些仍只是提案或测试。"]),
        ("thesis_check", "论点核验｜{subject} 的费率设计，用户到底在为什么付钱？", "把计费、分配和执行拆开，列出评估用户体验时可以验证的指标。", ["读取原始提案、规格或官方更新。", "画出费用、排序或资源分配路径。", "确认上线条件与实际启用状态。"]),
    ),
    "market_structure": (
        ("market_structure", "市场结构｜{subject} 这轮波动，现货和合约有没有走在一起？", "用不同市场的公开数据并列描述当前成交、杠杆和资金变化，避免单一指标下结论。", ["对照现货成交、永续资金费率和持仓量等公开数据。", "核对主要交易场所或 ETF 的公开资金流与成交。", "区分单时段波动和持续性的结构变化。"]),
        ("cycle", "市场观察｜{subject} 的买盘质量到底怎么样？", "结合资金流、杠杆和相对强弱，描述当前可观察到的市场状态，而不预设周期位置。", ["对照现货、衍生品与资金流的公开数据。", "查看主流资产与高弹性资产的相对表现。", "区分趋势、短期挤压和情绪变化的可见信号。"]),
    ),
    "leverage": (("market_structure", "市场结构｜{subject} 这轮波动，现货和杠杆有没有走在一起？", "并列观察未平仓量、资金费率、清算与现货成交，描述它们之间的关系。", ["对照未平仓量、资金费率、清算与现货成交。", "查看主要市场的持仓集中度或多空结构。", "把短期挤压与真实买盘分开。"]),),
    "token_supply": (("thesis_check", "论点核验｜{subject} 的供给变化，最后会落到多少可卖筹码上？", "从供应、解锁、流通和持仓结构中，列出可验证的观察变量。", ["核对官方代币经济、解锁或治理文件。", "对照实际流通量、锁仓和主要持仓变化。", "区分已执行的供给变化和未落地的提案。"]),),
    "governance": (("thesis_check", "治理研究｜{subject} 从投票通过到真正生效，中间还隔着什么？", "从提案、投票、权限和部署条件中，梳理公开可验证的治理流程。", ["读取原始提案、投票和执行条件。", "拆出治理权限、参数改动与实际部署之间的差别。", "核对历史同类提案是否真的进入主网或产品。"]),),
    "regulation": (("thesis_check", "规则研究｜{subject} 这次规则变化，谁能做、谁不能做？", "基于一手公告，梳理适用地区、对象与时间安排。", ["优先读取监管、交易所或项目方的一手公告。", "明确适用地区、对象、生效日期与过渡安排。", "区分已落地规则、征求意见和媒体解读。"]),),
    "listing_launch": (("adoption", "产品采用｜{subject} 上线以后，到底有没有人留下来用？", "从上线范围、用户、交易或交互等公开数据中，梳理目前能确认的使用情况。", ["核对官方上线范围、资格和产品路径。", "对照独立用户、交易或交互等公开使用数据。", "区分首发流量、任务激励和持续留存。"]),),
}

RESEARCH_TOPIC_RULES = {
    "hyperliquid:market_structure": (
        (
            "competition",
            "竞争研究｜Builder Codes 到底改变了 Hyperliquid 哪一层？",
            "从 Builder 机制的公开说明和使用数据中，梳理它涉及的产品、分发与撮合环节。",
            ["核对 Builder Codes、撮合与流动性共享的官方机制。", "对照 Builder 成交、费用和活跃前端分布。", "比较传统交易所与开放前端网络的用户、流动性和收入归属。"],
        ),
        (
            "thesis_check",
            "论点核验｜Builder 数据，够不够支撑“Hyperliquid 正在平台化”？",
            "区分不同数据能确认的使用、分发和产品变化，避免由单一成交指标推断平台定位。",
            ["核对 Builder 成交和收入的原始数据与时间范围。", "区分新增用户、已有交易迁移和补贴带来的成交。", "检查第三方前端是否形成持续留存与独立产品能力。"],
        ),
    ),
    "solana:market_structure": (
        (
            "market_structure",
            "市场结构｜SOL 的市场热度，有没有传到链上？",
            "并列观察相对价格、现货和衍生品数据，描述共同变化与独立变化。",
            ["对照 SOL/BTC、SOL/ETH 与 SOL/USDT 的相对表现。", "核对现货成交、永续持仓和资金费率。", "比较同一窗口内 Solana 生态的链上活动与资金流。"],
        ),
        (
            "cycle",
            "市场观察｜SOL 这段表现，怎么和大盘 Beta 分开看？",
            "比较相对价格、成交和生态活动的时间变化，避免先把现象归因到单一叙事。",
            ["比较 BTC、ETH、SOL 的相对强弱和成交扩张。", "查看回撤时 SOL 的超额收益能否保留。", "对照生态资金流与全市场风险偏好的变化。"],
        ),
    ),
}


def build_research_questions(discussion_topics: list[dict]) -> list[dict]:
    """Turn only concrete hot topics into conclusion-oriented research prompts."""
    questions = []
    kind_counts: dict[str, int] = {}
    for topic in discussion_topics:
        if not isinstance(topic, dict) or not topic.get("key") or not topic.get("title"):
            continue
        authors = int(topic.get("unique_authors") or 0)
        posts = int(topic.get("post_count") or 0)
        if authors < 2 or posts < 2:
            continue
        mechanism = topic.get("mechanism", {}) if isinstance(topic.get("mechanism"), dict) else {}
        mechanism_key = str(mechanism.get("key") or str(topic["key"]).rsplit(":", 1)[-1])
        rules = RESEARCH_TOPIC_RULES.get(str(topic["key"]), RESEARCH_QUESTION_RULES.get(mechanism_key, ()))
        parent = topic.get("parent", {}) if isinstance(topic.get("parent"), dict) else {}
        subject = OPPORTUNITY_SUBJECTS.get(
            mechanism_key,
            str(parent.get("title") or str(topic["title"]).split("｜", 1)[0]),
        )
        subject = OPPORTUNITY_PARENT_ALIASES.get(subject, subject)
        for kind, template, question, research_brief in rules:
            # Keep a daily list varied: each lane receives at most two items.
            if kind_counts.get(kind, 0) >= 2:
                continue
            title = template.format(subject=subject)
            if any(phrase in title for phrase in RESEARCH_TITLE_BANNED_PHRASES):
                continue
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            questions.append(
                {
                    "id": f"research:{topic['key']}:{kind}",
                    "title": title,
                    "question": question,
                    "kind": kind,
                    "source_topic_keys": [str(topic["key"])],
                    "source_topic_title": str(topic["title"]),
                    "why_now": f"24 小时内有 {authors} 位不同作者、{posts} 条原帖围绕这个具体议题讨论。",
                    "research_brief": research_brief,
                    "status": "needs_live_research",
                    "priority": len(questions) + 1,
                    "eligible": True,
                }
            )
    return questions


def editorial_kinds(topic: dict) -> list[str]:
    """Map a hot, concrete discussion topic to safe non-action editorial angles."""
    mechanism = topic.get("mechanism", {}) if isinstance(topic.get("mechanism"), dict) else {}
    key = str(mechanism.get("key") or str(topic.get("key", "")).rsplit(":", 1)[-1])
    rule = EDITORIAL_MECHANISM_RULES.get(key)
    kinds = [rule[0]] if rule else []
    actor = topic.get("public_actor") if isinstance(topic.get("public_actor"), dict) else {}
    if (
        isinstance(actor.get("name"), str)
        and actor["name"].strip()
        and actor.get("action_in_samples") is True
    ):
        kinds.append("public_strategy_read")
    return kinds


def build_editorial_questions(discussion_topics: list[dict]) -> list[dict]:
    """Build lightweight commentary prompts from the day's hot discussion only."""
    candidates = []
    for topic in discussion_topics:
        if not isinstance(topic, dict) or not topic.get("key") or not topic.get("title"):
            continue
        authors = int(topic.get("unique_authors") or 0)
        posts = int(topic.get("post_count") or 0)
        if authors < 2 or posts < 2:
            continue
        mechanism = topic.get("mechanism", {}) if isinstance(topic.get("mechanism"), dict) else {}
        parent = topic.get("parent", {}) if isinstance(topic.get("parent"), dict) else {}
        mechanism_key = str(mechanism.get("key") or str(topic["key"]).rsplit(":", 1)[-1])
        subject = OPPORTUNITY_PARENT_ALIASES.get(
            OPPORTUNITY_SUBJECTS.get(
                mechanism_key,
                str(parent.get("title") or str(topic["title"]).split("｜", 1)[0]),
            ),
            str(parent.get("title") or str(topic["title"]).split("｜", 1)[0]),
        )
        actor = topic.get("public_actor") if isinstance(topic.get("public_actor"), dict) else {}
        sample_refs = [
            str(item.get("source_ref"))
            for item in topic.get("sample_posts", [])
            if isinstance(item, dict) and item.get("source_ref")
        ][:3]
        for kind in editorial_kinds(topic):
            if kind == "public_strategy_read" and not actor.get("name"):
                continue
            if kind == "ct_culture" and not (authors >= 3 or int(topic.get("cross_list_count") or 0) >= 2):
                continue
            if kind == "public_strategy_read":
                title_template, framing = EDITORIAL_PUBLIC_RULE
            else:
                _, title_template, framing = EDITORIAL_MECHANISM_RULES[mechanism_key]
            candidates.append(
                {
                    "id": f"editorial:{topic['key']}:{kind}",
                    "title": title_template.format(subject=subject, actor=str(actor.get("name", "")).strip()),
                    "question": framing,
                    "kind": kind,
                    "source_topic_keys": [str(topic["key"])],
                    "source_topic_title": str(topic["title"]),
                    "source_sample_refs": sample_refs,
                    "why_now": f"24 小时内有 {authors} 位不同作者、{posts} 条原帖围绕这个具体议题讨论。",
                    "status": "editorial_ready",
                    "priority": 0,
                    "eligible": True,
                }
            )
    # A daily feed needs a little variety. Keep at most two wealth angles and one
    # of the more general formats, taking the already heat-sorted source topics.
    limits = {
        "trading_philosophy": 1,
        "wealth_view": 2,
        "public_strategy_read": 2,
        "ct_culture": 2,
    }
    selected = []
    used = {kind: 0 for kind in limits}
    for question in candidates:
        kind = question["kind"]
        if used[kind] >= limits[kind]:
            continue
        used[kind] += 1
        question["priority"] = len(selected) + 1
        selected.append(question)
    return selected

def opportunity_kind(topic: dict) -> str | None:
    mechanism = topic.get("mechanism", {}) if isinstance(topic.get("mechanism"), dict) else {}
    key = str(mechanism.get("key") or str(topic.get("key", "")).rsplit(":", 1)[-1])
    strong_mapping = {
        "market_structure": "short_term_trade",
        "leverage": "short_term_trade",
        "etf_flows": "trend_position",
        "regulation": "trend_position",
        "fee_model": "trend_position",
        "revenue_buyback": "trend_position",
        "tokenized_equities": "liquidity_activity",
        "token_supply": "rotation",
        "meme_ecosystem": "rotation",
        "listing_launch": "liquidity_activity",
        "governance": "early_project",
        "stablecoin_payments": None,
    }
    if key in strong_mapping:
        return strong_mapping[key]
    details = " ".join(
        [str(topic.get("title", ""))]
        + [str(item.get("text", "")) for item in topic.get("sample_posts", []) if isinstance(item, dict)]
    ).lower()
    if "airdrop" in details or "空投" in details:
        return "airdrop"
    if re.search(r"\b(?:apy|apr|yield)\b|年化|收益率", details):
        return "yield"
    if re.search(r"\b(?:arbitrage|spread)\b|套利|价差", details):
        return "arbitrage"
    if re.search(r"\b(?:tge|mainnet|launch)\b|主网|发币", details):
        return "early_project"
    return None


def build_opportunity_questions(discussion_topics: list[dict]) -> list[dict]:
    questions = []
    for topic in discussion_topics:
        if not isinstance(topic, dict) or not topic.get("key") or not topic.get("title"):
            continue
        kind = opportunity_kind(topic)
        if not kind:
            continue
        question, research_brief = OPPORTUNITY_QUESTION_RULES[kind]
        authors = int(topic.get("unique_authors") or 0)
        posts = int(topic.get("post_count") or 0)
        source_title = str(topic["title"])
        mechanism = topic.get("mechanism", {}) if isinstance(topic.get("mechanism"), dict) else {}
        mechanism_key = str(mechanism.get("key") or str(topic["key"]).rsplit(":", 1)[-1])
        parent = topic.get("parent", {}) if isinstance(topic.get("parent"), dict) else {}
        subject = OPPORTUNITY_SUBJECTS.get(
            mechanism_key,
            str(parent.get("title") or source_title.split("｜", 1)[0]),
        )
        subject = OPPORTUNITY_PARENT_ALIASES.get(subject, subject)
        questions.append(
            {
                "id": f"opportunity:{topic['key']}:{kind}",
                "title": OPPORTUNITY_TITLE_TEMPLATES[kind].format(subject=subject),
                "question": question,
                "kind": kind,
                "source_topic_keys": [str(topic["key"])],
                "source_topic_title": source_title,
                "why_now": f"24 小时内有 {authors} 位不同作者、{posts} 条原帖围绕这个具体议题讨论。",
                "research_brief": research_brief,
                "status": "needs_live_research",
                "priority": len(questions) + 1,
                "eligible": authors >= 2 and posts >= 2,
            }
        )
    return questions


def daily_context_run_dict(row):
    data = dict(row)
    for key in ("raw_manifest", "raw_cards", "synthesis"):
        data[key] = json_value(data[key], {})
    raw_cards = data["raw_cards"] if isinstance(data["raw_cards"], dict) else {}
    discussion_topics = raw_cards.get("discussion_topics", []) if isinstance(raw_cards, dict) else []
    selected_topics = raw_cards.get("selected_topics", []) if isinstance(raw_cards, dict) else []
    if isinstance(raw_cards, dict) and "selected_topics" in raw_cards and isinstance(selected_topics, list):
        raw_cards["opportunity_questions"] = [
            item for item in selected_topics if item.get("content_type") == "opportunity"
        ]
        raw_cards["editorial_questions"] = [
            item for item in selected_topics if item.get("content_type") == "editorial"
        ]
        raw_cards["research_questions"] = [
            item for item in selected_topics if item.get("content_type") == "research"
        ]
    questions = raw_cards.get("opportunity_questions") if isinstance(raw_cards, dict) else None
    if not isinstance(questions, list):
        questions = build_opportunity_questions(discussion_topics if isinstance(discussion_topics, list) else [])
        if isinstance(raw_cards, dict):
            raw_cards["opportunity_questions"] = questions
    editorial_questions = raw_cards.get("editorial_questions") if isinstance(raw_cards, dict) else None
    if not isinstance(editorial_questions, list):
        editorial_questions = build_editorial_questions(
            discussion_topics if isinstance(discussion_topics, list) else []
        )
        if isinstance(raw_cards, dict):
            raw_cards["editorial_questions"] = editorial_questions
    research_questions = raw_cards.get("research_questions") if isinstance(raw_cards, dict) else None
    if not isinstance(raw_cards, dict) or "selected_topics" not in raw_cards:
        research_questions = build_research_questions(
            discussion_topics if isinstance(discussion_topics, list) else []
        )
    if isinstance(raw_cards, dict):
        raw_cards["research_questions"] = research_questions
    synthesis = data["synthesis"] if isinstance(data["synthesis"], dict) else {}
    if "opportunity_questions" not in synthesis:
        synthesis["opportunity_questions"] = questions
    if "editorial_questions" not in synthesis:
        synthesis["editorial_questions"] = editorial_questions
    synthesis["research_questions"] = research_questions
    data["source_manifest"] = data["raw_manifest"]
    data["draft_context"] = {
        "context_date": data["context_date"],
        "date": data["context_date"],
        "market_state": synthesis.get("market_state", ""),
        "event_clusters": synthesis.get("event_clusters", ""),
        "debates": synthesis.get("debates", ""),
        "evidence": synthesis.get("evidence", ""),
        "unknowns": synthesis.get("unknowns", ""),
        "raw_feed": "",
        "sources": synthesis.get("sources", []),
        "opportunity_questions": questions,
        "editorial_questions": editorial_questions,
        "research_questions": research_questions,
    }
    data["date"] = data["context_date"]
    return data


def shanghai_today():
    return datetime.now(TZ).date().isoformat()


def daily_context_schedule():
    value = os.getenv("XOPS_DAILY_CONTEXT_RUN_TIME", "08:15")
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    return 8, 15


def daily_context_scheduler_enabled():
    return os.getenv("XOPS_DAILY_CONTEXT_ENABLED", "false").lower() == "true"


def twitter241_api_key():
    key = os.getenv("TWITTER241_RAPIDAPI_KEY")
    if key:
        return key
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "codex.twitter241.rapidapi",
                "-a",
                "TWITTER241_RAPIDAPI_KEY",
                "-w",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("未配置 Twitter241 凭据") from None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError("未配置 Twitter241 凭据")


def market_sources_module():
    """The collector is vendored with this app; no cross-project import at runtime."""
    try:
        import market_sources

        return market_sources
    except ModuleNotFoundError:
        raise RuntimeError("市场母池采集模块未安装")


def daily_context_paths(context_date: str):
    root = DAILY_CONTEXT_ARTIFACTS / context_date
    return {
        "root": root,
        "accounts": Path(
            os.getenv(
                "XOPS_MOTHER_POOL_ACCOUNTS",
                APP_DIR / "configs" / "content_source_accounts.json",
            )
        ),
        "source_db": DAILY_CONTEXT_SOURCE_DB,
        "output": root / "cards",
    }


def read_card_file(path: Path, key: str):
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get(key, []) if isinstance(data, dict) and isinstance(data.get(key), list) else []


def normalize_topic(value) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def topic_attention(topic: str, attention_topics: list[dict]) -> dict:
    target = normalize_topic(topic)
    for item in attention_topics:
        if not isinstance(item, dict):
            continue
        for key in ("title", "key"):
            candidate = normalize_topic(item.get(key))
            if candidate and target and (candidate in target or target in candidate):
                return {"status": "hot", "selection_source": "discussion_topics", "matched_topic": item}
    return {"status": "custom_or_niche"}


def selected_opportunity_question(topic: str, questions: list[dict]) -> dict | None:
    target = normalize_topic(topic)
    for question in questions:
        if not isinstance(question, dict):
            continue
        title = normalize_topic(question.get("title"))
        if target and title and (target in title or title in target):
            return question
    return None


def selected_editorial_question(topic: str, questions: list[dict]) -> dict | None:
    return selected_opportunity_question(topic, questions)


def selected_research_question(topic: str, questions: list[dict]) -> dict | None:
    return selected_opportunity_question(topic, questions)


def controlled_cards(
    facts: list[dict],
    opinions: list[dict],
    coverage: dict,
    attention_topics: list[dict] | None = None,
    niche_topics: list[dict] | None = None,
    discussion_topics: list[dict] | None = None,
    opportunity_questions: list[dict] | None = None,
    editorial_questions: list[dict] | None = None,
    research_questions: list[dict] | None = None,
    selection_policy: dict | None = None,
    claim_history: list[dict] | None = None,
    limit: int = 90000,
):
    """Keep synthesis grounded in cards, never in the full raw social feed."""
    ordered_niches = sorted(
        (item for item in (niche_topics or []) if isinstance(item, dict)),
        key=lambda item: (
            not str(item.get("key", "")).startswith("proposal:"),
            -int(item.get("unique_authors") or 0),
        ),
    )

    def compact(card):
        allowed = {
            key: card[key]
            for key in (
                "status",
                "representative_text",
                "author_count",
                "post_count",
                "source_lists",
                "source_ref",
                "handle",
                "url",
                "representative_source_ref",
                "representative_handle",
                "representative_url",
                "score",
                "text",
                "created_at",
                "reuse_rule",
                "title",
                "key",
                "unique_authors",
                "recent_6h_posts",
                "recent_6h_authors",
                "cross_list_count",
                "engagement_total",
                "engagement_coverage",
                "latest_at",
                "sample_posts",
                "samples",
                "id",
                "question",
                "kind",
                "source_topic_keys",
                "source_topic_title",
                "source_sample_refs",
                "why_now",
                "research_brief",
                "priority",
                "eligible",
            )
            if key in card
        }
        evidence = card.get("evidence")
        if isinstance(evidence, list):
            allowed["evidence"] = [
                {
                    key: item[key]
                    for key in ("source_ref", "handle", "url", "text", "created_at", "source_lists")
                    if key in item
                }
                for item in evidence[:4]
                if isinstance(item, dict)
            ]
        return allowed

    payload = {
        "coverage": coverage,
        "topic_selection_policy": selection_policy or {},
        "claim_history": [item for item in (claim_history or [])[:200] if isinstance(item, dict)],
        "discussion_topics": [compact(card) for card in (discussion_topics or [])[:20] if isinstance(card, dict)],
        "opportunity_questions": [compact(card) for card in (opportunity_questions or [])[:20] if isinstance(card, dict)],
        "editorial_questions": [compact(card) for card in (editorial_questions or [])[:8] if isinstance(card, dict)],
        "research_questions": [compact(card) for card in (research_questions or [])[:20] if isinstance(card, dict)],
        "attention_topics": [compact(card) for card in (attention_topics or [])[:20] if isinstance(card, dict)],
        "excluded_niche_topics": [
            {
                key: card[key]
                for key in ("title", "key", "unique_authors", "post_count")
                if key in card
            }
            for card in ordered_niches
        ],
        "fact_cards": [compact(card) for card in facts[:120] if isinstance(card, dict)],
        "opinion_cards": [compact(card) for card in opinions[:120] if isinstance(card, dict)],
    }
    while len(json.dumps(payload, ensure_ascii=False)) > limit:
        if payload["opinion_cards"]:
            payload["opinion_cards"].pop()
        elif payload["claim_history"]:
            payload["claim_history"].pop()
        elif payload["excluded_niche_topics"]:
            payload["excluded_niche_topics"].pop()
        elif payload["fact_cards"]:
            payload["fact_cards"].pop()
        elif payload["discussion_topics"]:
            payload["discussion_topics"].pop()
        elif payload["opportunity_questions"]:
            payload["opportunity_questions"].pop()
        elif payload["editorial_questions"]:
            payload["editorial_questions"].pop()
        elif payload["research_questions"]:
            payload["research_questions"].pop()
        elif payload["attention_topics"]:
            payload["attention_topics"].pop()
        else:
            break
    return payload


def llm_api_key():
    key = os.getenv("XOPS_LLM_API_KEY")
    if key:
        return key
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "codex-deepseek-api-key", "-a", "deepseek", "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    raise HTTPException(503, "未配置 Post 生成模型")


async def synthesize_daily_cards(context_date: str, cards: dict):
    prompt = (
        "你是 Crypto 市场研究编辑。以下是经过筛选的事实候选卡、观点候选卡和覆盖统计。"
        "只依据这些卡片生成当天市场理解，所有字段必须使用中文，输出 JSON 对象。\n"
        "字段必须是 market_state,event_clusters,debates,evidence,unknowns,sources,selected_topics,rejected_topics。\n"
        "discussion_topics 是实体与具体机制共同出现的可写议题，按讨论热度排序，是内容选题的主轴；"
        "attention_topics 只是父级市场地图，不能单独替代一个具体选题。"
        "opportunity_questions、editorial_questions 和 research_questions 只是研究入口，不是最终可写选题。"
        "必须按照 topic_selection_policy 逐条筛选，并把 claim_history 视为全账号已覆盖历史。"
        "热点不等于可写；数字刷新不等于观点更新。与历史主张语义相同且没有 material delta 的研究题必须拒绝。"
        "去重单位是核心主张，不是事件或项目：同一热点下互不重叠的研究、机会和评论角度可以分别保留。"
        "评论题可以复用当天事件背景，但必须有鲜明立场和非显而易见的表达。"
        "圈内读者不需要当天材料也能回答的常识题必须拒绝。按 slate_guidance 形成足够丰富但不凑数的题单。\n"
        "selected_topics 每项必须包含 claim_key,subject,title,core_claim,content_type,kind,source_topic_keys,"
        "fact_basis,opinion_basis,material_delta,audience_value,why_now,persona_fit。"
        "content_type 只能是 opportunity、editorial、research；source_topic_keys 必须来自 discussion_topics，或使用输入卡片的 opinion:<source_ref> / fact:<source_ref>。"
        "editorial 还可以从 content_inspiration 自由取材，并使用 evergreen:<key>；这些只是灵感，不是固定栏目、配额或轮换表。"
        "每天想到什么写什么，可以全是热点，也可以全是交易哲学；只有确实有话可说才选。名人内容遵守 quote_rule。"
        "title 必须直接包含新的结论或冲突，不能只是泛问‘为什么、有没有人用、意味着什么’。"
        "fact_basis 只写输入事实候选能支持的内容；opinion_basis 必须明确是观点；material_delta 必须说明相对历史到底新增了什么。\n"
        "rejected_topics 每项必须包含 title,core_claim,reason_code,reason,source_topic_keys；reason_code 必须来自 policy 的 reject_codes。"
        "单篇质量高、官方材料完整，都不能替代真实讨论度：冷门技术机制不得因为容易分析而挤占热点。\n"
        "excluded_niche_topics 是低于日常选题门槛的排除清单；其中的提案、项目或事件不得出现在 market_state、event_clusters 或 debates。"
        "event_clusters 优先按 discussion_topics 原样归纳具体讨论议题及热度；只有 discussion_topics 为空时，才可用 attention_topics 概括父级市场地图。不能从一张观点卡扩写出新的事件簇。\n"
        "market_state 只能写本轮母池的讨论面和注意力结构，不能把卡片内容写成已经发生的市场事实。"
        "event_clusters 和 debates 只能提炼本轮输入卡片，不得引入历史轮次、模型常识或外部事件。"
        "事实候选卡不是最终事实：evidence 只保留卡片里可追溯的多源线索；"
        "观点候选卡只能用于提炼市场分歧，不能伪装为事实，也不得复述原作者个人交易、持仓或生活经历。"
        "覆盖不足必须写入 unknowns。sources 只列卡片已有的来源线索。\n\n"
        f"日期：{context_date}\n受控卡片：\n{json.dumps(cards, ensure_ascii=False)}"
    )
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            os.getenv("XOPS_LLM_BASE_URL", "https://api.deepseek.com") + "/chat/completions",
            headers={"Authorization": f"Bearer {llm_api_key()}"},
            json={
                "model": os.getenv("XOPS_LLM_MODEL", "deepseek-chat"),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
        )
        response.raise_for_status()
    result = json.loads(response.json()["choices"][0]["message"]["content"])
    if not isinstance(result, dict):
        raise ValueError("每日市场状态不是 JSON 对象")
    return bounded_daily_card_synthesis(result, cards)


def chinese_synthesis_text(value, fallback: str):
    text = synthesis_text(value).strip()
    return text if re.search(r"[\u3400-\u9fff]", text) else fallback


def bounded_selected_topics(result: dict, cards: dict):
    source_topics = {
        str(item.get("key")): str(item.get("title") or item.get("key"))
        for item in cards.get("discussion_topics", [])
        if isinstance(item, dict) and item.get("key")
    }
    for item in cards.get("opinion_cards", []):
        if isinstance(item, dict) and item.get("source_ref"):
            source_topics[f"opinion:{item['source_ref']}"] = "母池高质量观点"
    for item in cards.get("fact_cards", []):
        if not isinstance(item, dict):
            continue
        source_ref = item.get("source_ref") or item.get("representative_source_ref")
        if source_ref:
            source_topics[f"fact:{source_ref}"] = "母池事实候选"
    policy = cards.get("topic_selection_policy", {})
    if isinstance(policy, dict):
        for item in policy.get("evergreen_inspirations", []):
            if isinstance(item, dict) and item.get("key"):
                source_topics[f"evergreen:{item['key']}"] = str(item.get("label") or "长期灵感")
    source_keys = set(source_topics)
    history_keys = {
        str(item.get("claim_key"))
        for item in cards.get("claim_history", [])
        if isinstance(item, dict) and item.get("claim_key")
    }
    selected = []
    rejected = [
        item for item in result.get("rejected_topics", [])[:30]
        if isinstance(item, dict) and item.get("title") and item.get("reason_code")
    ]
    for item in result.get("selected_topics", [])[:16]:
        if not isinstance(item, dict):
            continue
        keys = [str(key) for key in item.get("source_topic_keys", []) if str(key) in source_keys]
        required = ("claim_key", "subject", "title", "core_claim", "material_delta", "audience_value")
        if not keys or any(not str(item.get(key, "")).strip() for key in required):
            continue
        if item["claim_key"] in history_keys:
            rejected.append(
                {
                    "title": item["title"],
                    "core_claim": item["core_claim"],
                    "reason_code": "historical_duplicate",
                    "reason": "核心主张的 claim_key 已存在于全账号历史。",
                    "source_topic_keys": keys,
                }
            )
            continue
        content_type = item.get("content_type")
        if content_type not in {"opportunity", "editorial", "research"}:
            continue
        selected.append(
            {
                **item,
                "id": f"{content_type}:screened:{item['claim_key']}",
                "source_topic_keys": keys,
                "source_topic_title": source_topics[keys[0]],
                "question": item["core_claim"],
                "research_brief": [item["material_delta"], item["audience_value"]],
                "status": "needs_live_research",
                "eligible": True,
                "priority": len(selected) + 1,
            }
        )
    return selected, rejected


def without_niche_topics(value, cards: dict):
    markers = []
    for item in cards.get("excluded_niche_topics", []):
        if not isinstance(item, dict):
            continue
        markers.extend(
            normalize_topic(item.get(key))
            for key in ("title", "key")
            if len(normalize_topic(item.get(key))) >= 4
        )

    def excluded(item) -> bool:
        text = normalize_topic(json.dumps(item, ensure_ascii=False))
        return any(marker in text for marker in markers)

    if isinstance(value, list):
        return [item for item in value if not excluded(item)]
    return "" if excluded(value) else value


def card_source_hints(cards: dict):
    """Return only source identifiers actually attached to this run's cards."""
    hints = []
    seen = set()
    for card_type in ("fact_cards", "opinion_cards"):
        for card in cards.get(card_type, []):
            if not isinstance(card, dict):
                continue
            source_ref = str(card.get("source_ref") or card.get("representative_source_ref") or "")
            url = str(card.get("url") or card.get("representative_url") or "")
            handle = str(card.get("handle") or card.get("representative_handle") or "")
            if source_ref and source_ref not in seen:
                seen.add(source_ref)
                hints.append({"source_ref": source_ref, "handle": handle, "url": url})
            source_lists = card.get("source_lists", [])
            if not isinstance(source_lists, list):
                continue
            for source in source_lists:
                key = str(source)
                if key and key not in seen:
                    seen.add(key)
                    hints.append({"source_list": key})
    return hints


def bounded_daily_card_synthesis(result: dict, cards: dict):
    """Apply deterministic boundaries after the model response.

    A run with no fact cards may still be useful as an attention snapshot, but it
    may never be presented as evidence or an already-confirmed market event.
    """
    facts = [card for card in cards.get("fact_cards", []) if isinstance(card, dict)]
    opinions = [card for card in cards.get("opinion_cards", []) if isinstance(card, dict)]
    discussion_topics = [card for card in cards.get("discussion_topics", []) if isinstance(card, dict)]
    attention_topics = [card for card in cards.get("attention_topics", []) if isinstance(card, dict)]
    opportunity_questions = [card for card in cards.get("opportunity_questions", []) if isinstance(card, dict)]
    if not opportunity_questions:
        opportunity_questions = build_opportunity_questions(discussion_topics)
    editorial_questions = [card for card in cards.get("editorial_questions", []) if isinstance(card, dict)]
    if not editorial_questions:
        editorial_questions = build_editorial_questions(discussion_topics)
    research_questions = build_research_questions(discussion_topics)
    selected_topics, rejected_topics = bounded_selected_topics(result, cards)
    selected_opportunities = [item for item in selected_topics if item["content_type"] == "opportunity"]
    selected_editorials = [item for item in selected_topics if item["content_type"] == "editorial"]
    selected_research = [item for item in selected_topics if item["content_type"] == "research"]
    fact_count = len(facts)
    opinion_count = len(opinions)
    coverage = cards.get("coverage", {}) if isinstance(cards.get("coverage"), dict) else {}
    cross_validate = (
        coverage.get("cross_validate", {})
        if isinstance(coverage.get("cross_validate"), dict)
        else {}
    )
    total_opinion_count = int(
        cross_validate.get("opinion_cards") or coverage.get("opinion_cards") or opinion_count
    )
    if not fact_count:
        event_topics = discussion_topics or attention_topics
        attention_summary = "、".join(
            f"{card.get('title') or card.get('key')}（{card.get('unique_authors', 0)} 位作者、{card.get('post_count', 0)} 条帖子）"
            for card in event_topics[:10]
        )
        market_state = chinese_synthesis_text(
            result.get("market_state"), "本轮观点卡不足以进一步概括注意力结构。"
        )
        event_clusters = attention_summary or "本轮观点卡未形成达到讨论门槛的话题聚类。"
        debates = chinese_synthesis_text(
            without_niche_topics(result.get("debates"), cards),
            "本轮热点尚未形成可进一步概括的多方分歧。",
        )
        return {
            "market_state": (
                f"本轮母池筛出 {total_opinion_count} 条观点卡，"
                + (
                    f"本次综合使用其中 {opinion_count} 条受控样本；"
                    if total_opinion_count != opinion_count
                    else ""
                )
                + "未产出可多源核验的事实卡。"
                f"以下只表示讨论面与注意力结构，不表示事件已经确认：{market_state}"
            ),
            "event_clusters": f"24 小时母池讨论热度（非事实确认）：{event_clusters}",
            "debates": f"本轮观点卡中的解读与分歧（非一致市场结论）：{debates}",
            "evidence": "本轮未产出通过多源验证的事实卡；观点卡不能作为事实证据。",
            "unknowns": (
                "缺少可多源核验的事实卡，无法确认讨论对应事件的真实性、时间和影响范围。"
                + chinese_synthesis_text(result.get("unknowns"), "")
            ),
            "sources": card_source_hints(cards),
            "selected_topics": selected_topics,
            "rejected_topics": rejected_topics,
            "opportunity_questions": selected_opportunities,
            "editorial_questions": selected_editorials,
            "research_questions": selected_research,
        }

    market_state = chinese_synthesis_text(
        result.get("market_state"), "本轮事实卡与观点卡尚不足以形成更细的讨论面摘要。"
    )
    event_clusters = chinese_synthesis_text(
        result.get("event_clusters"), "本轮卡片未形成可进一步概括的事件与话题聚类。"
    )
    debates = chinese_synthesis_text(
        without_niche_topics(result.get("debates"), cards),
        "本轮卡片未形成可进一步概括的市场解读分歧。",
    )
    return {
        "market_state": f"本轮母池的讨论面与注意力结构：{market_state}",
        "event_clusters": f"以下仅归纳本轮卡片提到的事件与话题：{event_clusters}",
        "debates": f"以下仅归纳本轮卡片中的解读与分歧：{debates}",
        "evidence": f"本轮有 {fact_count} 条事实候选卡；证据仅限这些卡片附带的多源线索，发布前仍需复核。",
        "unknowns": chinese_synthesis_text(
            result.get("unknowns"), "本轮卡片覆盖有限，尚不能确认讨论的完整背景、时效性和因果关系。"
        ),
        "sources": card_source_hints(cards),
        "selected_topics": selected_topics,
        "rejected_topics": rejected_topics,
        "opportunity_questions": selected_opportunities,
        "editorial_questions": selected_editorials,
        "research_questions": selected_research,
    }


def get_daily_context_run(run_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM daily_context_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Daily context run not found")
    return daily_context_run_dict(row)


def create_daily_context_run(context_date: str, trigger: str):
    now = int(time.time())
    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM daily_context_runs WHERE context_date=?", (context_date,)
        ).fetchone()
        if existing:
            if existing["status"] == "queued":
                conn.execute(
                    """UPDATE daily_context_runs
                       SET status='running', trigger=?, started_at=?, completed_at=NULL, updated_at=?
                       WHERE id=?""",
                    (trigger, now, now, existing["id"]),
                )
                existing = conn.execute(
                    "SELECT * FROM daily_context_runs WHERE id=?", (existing["id"],)
                ).fetchone()
                return daily_context_run_dict(existing), True
            return daily_context_run_dict(existing), False
        cursor = conn.execute(
            """INSERT INTO daily_context_runs(
                context_date,status,trigger,started_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?)""",
            (context_date, "running", trigger, now, now, now),
        )
        row = conn.execute("SELECT * FROM daily_context_runs WHERE id=?", (cursor.lastrowid,)).fetchone()
    return daily_context_run_dict(row), True


def update_daily_context_run(run_id: int, **values):
    if not values:
        return get_daily_context_run(run_id)
    values["updated_at"] = int(time.time())
    columns = ", ".join(f"{column}=?" for column in values)
    with db() as conn:
        conn.execute(
            f"UPDATE daily_context_runs SET {columns} WHERE id=?",
            (*values.values(), run_id),
        )
        row = conn.execute("SELECT * FROM daily_context_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Daily context run not found")
    return daily_context_run_dict(row)


def daily_post_persona_slugs():
    value = os.getenv(
        "XOPS_DAILY_POST_PERSONAS",
        os.getenv(
            "XOPS_DAILY_POST_PERSONA",
            "acheng,ridehail-driver-zhao,college-student-linjia,atuo,axu,nanqiao,qiliang,aye,xiaoman,maili",
        ),
    )
    return list(dict.fromkeys(slug.strip() for slug in value.split(",") if slug.strip()))


def persona_editorial_enabled():
    return os.getenv("XOPS_DAILY_POST_ENABLED", "false").lower() == "true"


EDITORIAL_CONTEXT_KEYS = (
    "life_context", "thought_threads", "expression_debt", "real_feedback",
)
EDITORIAL_CONTEXT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")


def empty_persona_editorial_context():
    return {
        "life_context": [],
        "thought_threads": [],
        "expression_debt": [],
        "real_feedback": [],
        "available_asset_ids": [],
    }


def editorial_item_id(kind: str, item: dict | str):
    raw = str(item.get("id", "") if isinstance(item, dict) else "").strip().lower()
    if EDITORIAL_CONTEXT_ID.fullmatch(raw):
        return raw
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{kind}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:12]}"


def editorial_text(item: dict, key: str, limit: int = 2000):
    return str(item.get(key, "") or "").strip()[:limit]


def editorial_text_list(item: dict, key: str, limit: int = 20):
    value = item.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise HTTPException(422, f"{key} must be an array")
    return [str(entry).strip()[:1000] for entry in value[:limit] if str(entry).strip()]


def normalize_persona_editorial_context(value: dict):
    result = empty_persona_editorial_context()
    for kind in EDITORIAL_CONTEXT_KEYS:
        raw_items = value.get(kind, [])
        if not isinstance(raw_items, list):
            raise HTTPException(422, f"{kind} must be an array")
        if len(raw_items) > 80:
            raise HTTPException(422, f"{kind} has too many items")
        seen = set()
        for raw in raw_items:
            if not isinstance(raw, (dict, str)):
                raise HTTPException(422, f"{kind} items must be objects or strings")
            item = raw if isinstance(raw, dict) else {}
            item_id = editorial_item_id(kind.removesuffix("_context").removesuffix("s"), raw)
            if item_id in seen:
                raise HTTPException(422, f"duplicate {kind} id: {item_id}")
            seen.add(item_id)
            asset_ids = editorial_text_list(item, "asset_ids")
            if kind == "life_context":
                fact = (
                    editorial_text(item, "fact") or editorial_text(item, "text")
                    or editorial_text(item, "note") or editorial_text(item, "angle")
                    or editorial_text(item, "core_claim")
                    if isinstance(raw, dict) else str(raw).strip()[:2000]
                )
                if not fact:
                    raise HTTPException(422, "life_context needs fact, text, note, angle or core_claim")
                angle = editorial_text(item, "angle")
                core_claim = editorial_text(item, "core_claim") or angle
                status = editorial_text(item, "status", 20) or ("ready" if core_claim else "context")
                normalized = {
                    "id": item_id,
                    "fact": fact,
                    "angle": angle,
                    "core_claim": core_claim,
                    "status": status,
                    "first_person_allowed": bool(item.get("first_person_allowed", False)),
                    "source": editorial_text(item, "source", 1000),
                    "observed_at": editorial_text(item, "observed_at", 80),
                    "expires_at": editorial_text(item, "expires_at", 80),
                    "evidence": editorial_text_list(item, "evidence"),
                    "asset_ids": asset_ids,
                }
            elif kind == "thought_threads":
                text = str(raw).strip()[:2000] if isinstance(raw, str) else ""
                current_view = (
                    editorial_text(item, "current_view") or editorial_text(item, "core_claim")
                    or editorial_text(item, "angle") or editorial_text(item, "text") or text
                )
                observation = (
                    editorial_text(item, "observation") or editorial_text(item, "text")
                    or editorial_text(item, "angle") or text
                )
                status = editorial_text(item, "status", 20) or ("ready" if current_view else "open")
                if status == "ready" and not current_view:
                    raise HTTPException(422, "ready thought thread needs current_view")
                normalized = {
                    "id": item_id,
                    "title": editorial_text(item, "title", 300) or current_view[:120],
                    "angle": editorial_text(item, "angle"),
                    "observation": observation,
                    "tension": editorial_text(item, "tension"),
                    "current_view": current_view,
                    "status": status,
                    "evidence": editorial_text_list(item, "evidence"),
                    "counterevidence": editorial_text_list(item, "counterevidence"),
                    "asset_ids": asset_ids,
                }
            elif kind == "expression_debt":
                core_claim = (
                    editorial_text(item, "core_claim") or editorial_text(item, "angle")
                    if isinstance(raw, dict) else str(raw).strip()[:2000]
                )
                if not core_claim:
                    raise HTTPException(422, "expression_debt needs core_claim or angle")
                status = editorial_text(item, "status", 20) or "ready"
                normalized = {
                    "id": item_id,
                    "core_claim": core_claim,
                    "angle": editorial_text(item, "angle"),
                    "why_now": editorial_text(item, "why_now"),
                    "status": status,
                    "evidence": editorial_text_list(item, "evidence"),
                    "asset_ids": asset_ids,
                }
            else:
                summary = (
                    editorial_text(item, "summary") or editorial_text(item, "text")
                    or editorial_text(item, "angle") or editorial_text(item, "core_claim")
                    if isinstance(raw, dict) else str(raw).strip()[:2000]
                )
                if not summary:
                    raise HTTPException(422, "real_feedback needs summary, text, angle or core_claim")
                core_claim = editorial_text(item, "core_claim") or editorial_text(item, "angle")
                status = editorial_text(item, "status", 20) or ("ready" if core_claim else "context")
                normalized = {
                    "id": item_id,
                    "summary": summary,
                    "angle": editorial_text(item, "angle"),
                    "source": editorial_text(item, "source", 1000),
                    "received_at": editorial_text(item, "received_at", 80),
                    "core_claim": core_claim,
                    "status": status,
                    "quote_allowed": bool(item.get("quote_allowed", False)),
                    "quote": editorial_text(item, "quote") if item.get("quote_allowed") else "",
                    "asset_ids": asset_ids,
                }
            result[kind].append(normalized)
    assets = value.get("available_asset_ids", [])
    if not isinstance(assets, list):
        raise HTTPException(422, "available_asset_ids must be an array")
    result["available_asset_ids"] = list(dict.fromkeys(
        str(asset_id).strip() for asset_id in assets[:100] if str(asset_id).strip()
    ))
    return result


def validate_persona_editorial_context_input(value: dict, valid_asset_ids: set[str]):
    allowed_statuses = {"context", "open", "ready", "draft", "expressed", "archived"}
    if len(json.dumps(value, ensure_ascii=False)) > 100000:
        raise HTTPException(422, "editorial context is too large")
    selected = value.get("available_asset_ids", [])
    if not isinstance(selected, list):
        raise HTTPException(422, "available_asset_ids must be an array")
    selected_ids = [str(asset_id).strip() for asset_id in selected if str(asset_id).strip()]
    if len(selected_ids) != len(set(selected_ids)):
        raise HTTPException(422, "available_asset_ids contains duplicates")
    if not set(selected_ids).issubset(valid_asset_ids):
        raise HTTPException(422, "available_asset_ids contains an asset from another persona")
    for kind in EDITORIAL_CONTEXT_KEYS:
        items = value.get(kind, [])
        if not isinstance(items, list) or len(items) > 80:
            raise HTTPException(422, f"{kind} must be an array with at most 80 items")
        ids = set()
        for item in items:
            if not isinstance(item, (dict, str)):
                raise HTTPException(422, f"{kind} items must be objects or strings")
            item_id = editorial_item_id(kind.removesuffix("_context").removesuffix("s"), item)
            if item_id in ids:
                raise HTTPException(422, f"duplicate {kind} id: {item_id}")
            ids.add(item_id)
            if isinstance(item, dict):
                status = str(item.get("status", "") or "")
                if status and status not in allowed_statuses:
                    raise HTTPException(422, f"{kind}.status is invalid")
                item_assets = item.get("asset_ids", [])
                if not isinstance(item_assets, list):
                    raise HTTPException(422, f"{kind}.asset_ids must be an array")
                if not set(map(str, item_assets)).issubset(set(selected_ids)):
                    raise HTTPException(422, f"{kind}.asset_ids must be selected first")
    result = json.loads(json.dumps(value, ensure_ascii=False))
    for item in result.get("real_feedback", []):
        if isinstance(item, dict) and not item.get("quote_allowed"):
            item.pop("quote", None)
    return result


def persona_editorial_context_dict(row):
    if not row:
        return {
            "status": "needs_review",
            "approval_revision": 0,
            "approved_at": None,
            "created_at": None,
            "updated_at": None,
            "draft": empty_persona_editorial_context(),
            "approved": {},
        }
    value = dict(row)
    return {
        "persona_id": value["persona_id"],
        "status": value["status"],
        "approval_revision": value["approval_revision"],
        "approved_at": value["approved_at"],
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
        "draft": json_value(value["draft_json"], empty_persona_editorial_context()),
        "approved": json_value(value["approved_json"], empty_persona_editorial_context()),
    }


def expressed_editorial_source_ids(conn, persona_id: int, exclude_run_id: int | None = None):
    rows = conn.execute(
        """SELECT e.run_id,e.topic_json FROM persona_editorial_evaluations e
           JOIN post_candidates c ON c.id=e.candidate_id
           WHERE e.persona_id=? AND e.status='WRITE' AND c.status<>'superseded'
             AND (? IS NULL OR e.run_id<>?)""",
        (persona_id, exclude_run_id, exclude_run_id),
    ).fetchall()
    result = set()
    for row in rows:
        topic = json_value(row["topic_json"], {})
        if topic.get("source_kind") in {"thought", "expression_debt"} and topic.get("source_id"):
            result.add(f"{topic['source_kind']}:{topic['source_id']}")
    return result


def approved_persona_editorial_context(conn, persona_id: int, slug: str,
                                        exclude_run_id: int | None = None):
    expressed = expressed_editorial_source_ids(conn, persona_id, exclude_run_id)
    row = conn.execute(
        "SELECT * FROM persona_editorial_contexts WHERE persona_id=?", (persona_id,)
    ).fetchone()
    if not row or not row["approval_revision"]:
        return {
            **empty_persona_editorial_context(), "approval_revision": 0,
            "available_assets": [], "expressed_source_ids": sorted(expressed),
        }
    approved = normalize_persona_editorial_context(
        json_value(row["approved_json"], empty_persona_editorial_context())
    )
    assets = {item["id"]: item for item in persona_assets(slug)}
    selected = [assets[asset_id] for asset_id in approved.get("available_asset_ids", []) if asset_id in assets]
    return {
        **empty_persona_editorial_context(),
        **approved,
        "approval_revision": row["approval_revision"],
        "available_assets": selected,
        "expressed_source_ids": sorted(expressed),
    }


def build_persona_private_topics(editorial_context: dict):
    selected_assets = set(editorial_context.get("available_asset_ids", []))
    expressed = set(editorial_context.get("expressed_source_ids", []))
    topics = []

    def add(kind: str, item: dict, title: str, core_claim: str, **extra):
        if (
            item.get("status") != "ready" or not core_claim
            or f"{kind}:{item['id']}" in expressed
        ):
            return
        topics.append({
            "claim_key": f"private:{kind}:{item['id']}",
            "subject": title[:160],
            "title": title[:300],
            "core_claim": core_claim[:2000],
            "content_type": "persona_private",
            "scope": "persona",
            "source_kind": kind,
            "source_id": item["id"],
            "source_refs": [f"{kind}:{item['id']}"],
            "asset_ids": [asset_id for asset_id in item.get("asset_ids", []) if asset_id in selected_assets],
            **extra,
        })

    for item in editorial_context.get("life_context", []):
        add(
            "life", item, item.get("angle") or item.get("fact", "")[:120],
            item.get("core_claim") or item.get("angle", ""), angle=item.get("angle", ""),
            fact_basis=[item.get("fact", "")], evidence=item.get("evidence", []),
            first_person_allowed=bool(item.get("first_person_allowed")),
        )
    for item in editorial_context.get("thought_threads", []):
        add(
            "thought", item, item.get("title") or item.get("current_view", "")[:120],
            item.get("current_view", ""), angle=item.get("angle", ""),
            observation=item.get("observation", ""),
            tension=item.get("tension", ""), evidence=item.get("evidence", []),
            counterevidence=item.get("counterevidence", []), first_person_allowed=False,
        )
    for item in editorial_context.get("expression_debt", []):
        add(
            "expression_debt", item, item.get("core_claim", "")[:120], item.get("core_claim", ""),
            angle=item.get("angle", ""), why_now=item.get("why_now", ""), evidence=item.get("evidence", []),
            first_person_allowed=False,
        )
    for item in editorial_context.get("real_feedback", []):
        add(
            "feedback", item, item.get("summary", "")[:120], item.get("core_claim", ""),
            angle=item.get("angle", ""), feedback_summary=item.get("summary", ""), source=item.get("source", ""),
            quote=item.get("quote", "") if item.get("quote_allowed") else "",
            first_person_allowed=False,
        )
    return topics


def editorial_persona_card(persona: dict):
    draft = json_value(persona.get("draft"), {})
    content = dict(draft.get("content", {}))
    for key in ("posts_per_day", "posting_windows", "content_mix"):
        content.pop(key, None)
    return {
        "identity": draft.get("identity", {}),
        "voice": draft.get("voice", {}),
        "content": content,
        "examples": draft.get("examples", {}),
    }


def editorial_persona_context(persona_context: dict):
    return {
        key: (persona_context or {}).get(key, "")
        for key in ("audience_baseline", "prior_views", "watchlist", "unresolved", "forbidden_claims")
    }


def editorial_daily_input(daily: dict):
    return {
        key: daily.get(key, "" if key != "sources" else [])
        for key in (
            "context_date", "approval_revision", "market_state", "event_clusters", "debates",
            "evidence", "unknowns", "sources"
        )
    }


def editorial_claim_memory(claim_history: list[dict]):
    return [
        {
            key: item.get(key, "")
            for key in ("claim_key", "core_claim", "context_date", "status")
        }
        for item in claim_history[:80]
        if isinstance(item, dict)
    ]


def editorial_topic_input_payload(topic: dict, daily: dict, persona: dict, persona_context: dict,
                                   topics: list[dict] | None = None,
                                   claim_history: list[dict] | None = None,
                                   editorial_context: dict | None = None):
    approved_context = editorial_context or persona.get("_editorial_context") or {
        **empty_persona_editorial_context(), "approval_revision": 0,
        "available_assets": [], "expressed_source_ids": [],
    }
    return {
        "evaluator_revision": EDITORIAL_EVALUATOR_REVISION,
        "topic": topic,
        "topic_batch": topics or [topic],
        "daily": editorial_daily_input(daily),
        "persona_card": editorial_persona_card(persona),
        "persona_context": editorial_persona_context(persona_context),
        "approved_editorial_context": approved_context,
        "claim_memory": editorial_claim_memory(claim_history or []),
    }


def editorial_topic_input_hash(topic: dict, daily: dict, persona: dict, persona_context: dict,
                                topics: list[dict] | None = None,
                                claim_history: list[dict] | None = None,
                                editorial_context: dict | None = None):
    payload = editorial_topic_input_payload(
        topic, daily, persona, persona_context, topics, claim_history, editorial_context
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_editorial_claim(value):
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", str(value or "").lower())


def editorial_score(item: dict):
    return sum(int(item.get(key, 0) or 0) for key in ("notice", "authority", "tension", "marginal_value"))


EDITORIAL_CLAIM_KEY = re.compile(r"^[a-z0-9][a-z0-9:_-]{2,119}$")
EDITORIAL_EVALUATOR_REVISION = 2


def validate_persona_editorial_decisions(result, topics: list[dict]):
    """Validate the evaluator's pure JSON output without inferring a claim for it."""
    raw = result.get("decisions", result) if isinstance(result, dict) else result
    if not isinstance(raw, list):
        raise ValueError("人设编辑评估不是 decisions 数组")
    allowed = {str(item.get("claim_key", "")): item for item in topics if isinstance(item, dict)}
    decisions = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        topic_key = str(item.get("topic_claim_key", ""))
        if topic_key not in allowed or topic_key in decisions:
            continue
        status = str(item.get("status", "")).upper()
        if status not in {"WRITE", "HOLD", "IGNORE"}:
            continue
        scores = {}
        try:
            for key in ("notice", "authority", "tension", "marginal_value"):
                scores[key] = max(0, min(5, int(item.get(key, 0))))
        except (TypeError, ValueError):
            continue
        claim_key = str(item.get("claim_key", "")).strip().lower()
        core_claim = str(item.get("core_claim", "")).strip()
        if status == "WRITE":
            if not claim_key or not core_claim or not str(item.get("why_me", "")).strip():
                status = "HOLD"
            elif not EDITORIAL_CLAIM_KEY.fullmatch(claim_key):
                status = "HOLD"
        decisions[topic_key] = {
            "status": status,
            **scores,
            "why_me": str(item.get("why_me", "")).strip(),
            "claim_key": claim_key,
            "core_claim": core_claim,
            "reason_code": (
                "invalid_claim_key"
                if str(item.get("status", "")).upper() == "WRITE" and claim_key
                and not EDITORIAL_CLAIM_KEY.fullmatch(claim_key)
                else str(item.get("reason_code", "")).strip() or (
                    "write" if status == "WRITE" else "editorial_hold"
                )
            ),
            "rationale": str(item.get("rationale", "")).strip(),
            "open_loop": str(item.get("open_loop", "")).strip(),
            "topic_claim_key": topic_key,
        }
    for topic in topics:
        key = str(topic.get("claim_key", ""))
        if key not in decisions:
            decisions[key] = {
                "status": "IGNORE", "notice": 0, "authority": 0, "tension": 0,
                "marginal_value": 0, "why_me": "", "claim_key": "", "core_claim": "",
                "reason_code": "evaluator_missing", "rationale": "评估器未返回该题。", "open_loop": "",
                "topic_claim_key": key,
            }
    return decisions


def apply_editorial_claim_history(persona_id: int, decisions: dict, claim_history: list[dict]):
    history_claims = set()
    for item in claim_history:
        if not isinstance(item, dict) or item.get("status") == "superseded":
            continue
        claim = normalize_editorial_claim(item.get("core_claim"))
        if claim:
            history_claims.add(claim)
    for decision in decisions.values():
        if decision["status"] != "WRITE":
            continue
        claim = normalize_editorial_claim(decision["core_claim"])
        if claim and claim in history_claims:
            decision["status"] = "IGNORE"
            decision["reason_code"] = "historical_duplicate"
            decision["rationale"] = "核心主张已存在于可用 Claim Memory。"
    return decisions


def apply_editorial_marginal_threshold(decisions: dict, today_count: int):
    minimum = 5 if today_count >= 5 else 4 if today_count >= 3 else 0
    if not minimum:
        return decisions
    for decision in decisions.values():
        if decision["status"] == "WRITE" and decision["marginal_value"] < minimum:
            decision["status"] = "HOLD"
            decision["reason_code"] = "insufficient_marginal_value"
            decision["rationale"] = "当天已有候选后，这条主张的边际价值不足。"
    return decisions


async def evaluate_persona_editorial(persona: dict, persona_context: dict, daily: dict,
                                     topics: list[dict], claim_history: list[dict], today_count: int):
    """Return one validated WRITE/HOLD/IGNORE decision per input topic for a persona."""
    if not topics:
        return {}
    prompt = (
        "你是 Persona Editorial Engine 的编辑判断器。只输出 JSON：{\"decisions\":[...]}。"
        "每个输入 topic 必须恰好有一项，topic_claim_key 必须等于输入的 claim_key。"
        "逐题独立判断：notice/authority/tension/marginal_value 均为 0-5 整数；"
        "status 只能 WRITE/HOLD/IGNORE；why_me 说明为什么该人设此刻有资格说；"
        "WRITE 必须有新的 claim_key 和非显而易见 core_claim。HOLD 是内部状态，不是正文，绝不以等待后续凑稿。"
        "逐题独立决定，可有多条 WRITE，也可以全部 HOLD 或 IGNORE；不设数量上下限。"
        "同一热点只有不同核心主张才值得写；不要复述常识、冷门机制或已覆盖的主张。"
        "approved_editorial_context 里，life_context 只有 first_person_allowed=true 的当前题目能支持具体亲历；"
        "thought_threads 只是观点种子，real_feedback 只是受众信号，素材只证明图片可用，三者都不能证明亲历。"
        "expression_debt 是成熟但未表达的候选，不是必须 WRITE 的欠稿数量。"
        "today_accepted_count 不是配额，0 合法；数量越高越要求更强的边际价值。"
        "不编造持仓、经历、交易、收益或事实。\n\n"
        f"人设：{json.dumps({'slug': persona['slug'], 'card': editorial_persona_card(persona)}, ensure_ascii=False)}\n"
        f"人设连续性：{json.dumps(editorial_persona_context(persona_context), ensure_ascii=False)}\n"
        f"已批准的人设编辑语境：{json.dumps(persona.get('_editorial_context') or {}, ensure_ascii=False)}\n"
        f"已批准市场语境：{json.dumps(editorial_daily_input(daily), ensure_ascii=False)}\n"
        f"已覆盖主张：{json.dumps(editorial_claim_memory(claim_history), ensure_ascii=False)}\n"
        f"待评估 topics：{json.dumps(topics, ensure_ascii=False)}"
    )
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                os.getenv("XOPS_LLM_BASE_URL", "https://api.deepseek.com") + "/chat/completions",
                headers={"Authorization": f"Bearer {llm_api_key()}"},
                json={
                    "model": os.getenv("XOPS_LLM_MODEL", "deepseek-chat"),
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                },
            )
            response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
        return validate_persona_editorial_decisions(result, topics)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("人设编辑评估失败") from error


def write_persona_editorial_evaluations(run_id: int, persona_id: int,
                                        inputs: list[tuple[dict, str, dict]], decisions: dict):
    now = int(time.time())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT status,context_date FROM daily_context_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run or run["status"] != "approved":
            return
        for topic, input_hash, input_payload in inputs:
            current_payload = current_editorial_input_payload(
                conn,
                {
                    "run_id": run_id,
                    "persona_id": persona_id,
                    "topic_json": json.dumps(topic, ensure_ascii=False),
                },
                run["context_date"],
            )
            if current_payload != input_payload:
                continue
            decision = decisions[str(topic.get("claim_key", ""))]
            conn.execute(
                """INSERT INTO persona_editorial_evaluations(
                    run_id,persona_id,topic_input_hash,input_json,topic_json,status,notice,authority,tension,marginal_value,
                    why_me,claim_key,core_claim,reason_code,rationale,open_loop,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,persona_id,topic_input_hash) DO NOTHING""",
                (
                    run_id, persona_id, input_hash, json.dumps(input_payload, ensure_ascii=False),
                    json.dumps(topic, ensure_ascii=False), decision["status"],
                    decision["notice"], decision["authority"], decision["tension"], decision["marginal_value"],
                    decision["why_me"], decision["claim_key"], decision["core_claim"], decision["reason_code"],
                    decision["rationale"], decision["open_loop"], now, now,
                ),
            )


def resolve_persona_editorial_collisions(run_id: int):
    with db() as conn:
        rows = conn.execute(
            """SELECT e.*,p.slug FROM persona_editorial_evaluations e
               JOIN personas p ON p.id=e.persona_id WHERE e.run_id=? AND e.status='WRITE'""",
            (run_id,),
        ).fetchall()
        items = [dict(row) for row in rows]
        links = {item["id"]: {item["id"]} for item in items}
        for index, item in enumerate(items):
            item_key = str(item["claim_key"]).strip().lower()
            item_claim = normalize_editorial_claim(item["core_claim"])
            for other in items[index + 1:]:
                same_key = item_key and item_key == str(other["claim_key"]).strip().lower()
                same_claim = item_claim and item_claim == normalize_editorial_claim(other["core_claim"])
                if same_key or same_claim:
                    links[item["id"]].add(other["id"])
                    links[other["id"]].add(item["id"])
        now = int(time.time())
        losers = set()
        by_id = {item["id"]: item for item in items}
        unseen = set(by_id)
        while unseen:
            pending = [unseen.pop()]
            component = set(pending)
            while pending:
                linked = links[pending.pop()] - component
                component.update(linked)
                pending.extend(linked)
                unseen.difference_update(linked)
            if len(component) < 2:
                continue
            matches = [by_id[evaluation_id] for evaluation_id in component]
            matches.sort(key=lambda item: (-editorial_score(item), item["slug"]))
            for item in matches[1:]:
                losers.add(item["id"])
        for evaluation_id in losers:
            conn.execute(
                """UPDATE persona_editorial_evaluations SET status='HOLD',reason_code='cross_persona_collision',
                   rationale='与更匹配人设的核心主张重复。',updated_at=? WHERE id=?""",
                (now, evaluation_id),
            )
            conn.execute(
                "UPDATE post_candidates SET status='superseded',updated_at=? WHERE id=(SELECT candidate_id FROM persona_editorial_evaluations WHERE id=?)",
                (now, evaluation_id),
            )
            conn.execute(
                "UPDATE topic_claim_history SET status='superseded',last_seen_at=? WHERE source=?",
                (now, f"persona_editorial:{evaluation_id}"),
            )


def record_persona_editorial_claim(conn, evaluation: dict, context_date: str):
    claim_key = f"persona:{evaluation['persona_id']}:{evaluation['claim_key']}"
    now = int(time.time())
    conn.execute(
        """INSERT INTO topic_claim_history(
            claim_key,persona_id,subject,core_claim,context_date,source,status,created_at,last_seen_at
        ) VALUES(?,?,?,?,?,?, 'drafted',?,?)
        ON CONFLICT(claim_key) DO UPDATE SET core_claim=excluded.core_claim,context_date=excluded.context_date,
            source=excluded.source,status='drafted',last_seen_at=excluded.last_seen_at""",
        (
            claim_key, evaluation["persona_id"], str(json_value(evaluation["topic_json"], {}).get("subject", "")),
            evaluation["core_claim"], context_date, f"persona_editorial:{evaluation['id']}", now, now,
        ),
    )


def supersede_persona_editorial_evaluation(conn, evaluation_id: int, reason_code: str, rationale: str):
    now = int(time.time())
    conn.execute(
        """UPDATE persona_editorial_evaluations
           SET status='HOLD',reason_code=?,rationale=?,updated_at=? WHERE id=?""",
        (reason_code, rationale, now, evaluation_id),
    )
    conn.execute(
        "UPDATE post_candidates SET status='superseded',updated_at=? WHERE id=(SELECT candidate_id FROM persona_editorial_evaluations WHERE id=?)",
        (now, evaluation_id),
    )
    conn.execute(
        "UPDATE topic_claim_history SET status='superseded',last_seen_at=? WHERE source=?",
        (now, f"persona_editorial:{evaluation_id}"),
    )


def editorial_stable_claim_history(conn, context_date: str, persona_id: int | None = None):
    rows = conn.execute(
        """SELECT h.claim_key,h.persona_id,h.subject,h.core_claim,h.context_date,h.source,h.status,
                  e.topic_json
           FROM topic_claim_history h
           LEFT JOIN persona_editorial_evaluations e ON h.source=('persona_editorial:' || e.id)
           WHERE h.status<>'superseded'
             AND NOT (h.source='daily_context_run' AND h.persona_id IS NULL)
             AND (h.context_date IS NULL OR h.context_date<?)
           ORDER BY h.last_seen_at DESC LIMIT 200""",
        (context_date,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["_scope"] = json_value(item.pop("topic_json"), {}).get("scope", "public")
        if item["_scope"] == "persona" and persona_id is not None and item["persona_id"] != persona_id:
            continue
        result.append(item)
        if len(result) == 80:
            break
    return result


def current_editorial_input_payload(conn, evaluation: dict, context_date: str):
    run = conn.execute(
        "SELECT status,raw_cards,approval_revision FROM daily_context_runs WHERE id=?",
        (evaluation["run_id"],),
    ).fetchone()
    if not run or run["status"] != "approved":
        return None
    daily_row = conn.execute(
        "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
    ).fetchone()
    persona = conn.execute(
        "SELECT id,slug,draft FROM personas WHERE id=?", (evaluation["persona_id"],)
    ).fetchone()
    if not daily_row or not persona:
        return None
    context_row = conn.execute(
        "SELECT * FROM persona_contexts WHERE persona_id=?", (evaluation["persona_id"],)
    ).fetchone()
    editorial_context = approved_persona_editorial_context(
        conn, evaluation["persona_id"], persona["slug"], evaluation["run_id"]
    )
    daily = daily_context_dict(daily_row)
    daily["approval_revision"] = run["approval_revision"]
    cards = json_value(run["raw_cards"], {})
    public_topics = [
        item for item in cards.get("selected_topics", [])
        if isinstance(item, dict) and item.get("claim_key")
    ]
    topics = public_topics + build_persona_private_topics(editorial_context)
    return editorial_topic_input_payload(
        json_value(evaluation["topic_json"], {}),
        daily,
        dict(persona),
        persona_context_dict(context_row) if context_row else {},
        topics=topics,
        claim_history=editorial_stable_claim_history(
            conn, context_date, evaluation["persona_id"]
        ),
        editorial_context=editorial_context,
    )


def editorial_claim_already_drafted(conn, evaluation: dict):
    claim = normalize_editorial_claim(evaluation.get("core_claim"))
    if not claim:
        return False
    rows = conn.execute(
        """SELECT core_claim FROM topic_claim_history
           WHERE status<>'superseded' AND source<>?
             AND NOT (source='daily_context_run' AND persona_id IS NULL)""",
        (f"persona_editorial:{evaluation['id']}",),
    ).fetchall()
    return any(normalize_editorial_claim(row["core_claim"]) == claim for row in rows)


def editorial_writer_context(input_snapshot: dict, topic: dict):
    context = input_snapshot.get("approved_editorial_context", {})
    kind = str(topic.get("source_kind", ""))
    source_id = str(topic.get("source_id", ""))
    key = {
        "life": "life_context",
        "thought": "thought_threads",
        "expression_debt": "expression_debt",
        "feedback": "real_feedback",
    }.get(kind)
    source_item = None
    if key:
        source_item = next(
            (item for item in context.get(key, []) if str(item.get("id", "")) == source_id),
            None,
        )
    source_item = compact_editorial_source_item(source_item)
    assets = [
        {
            field: str(asset.get(field, ""))[:240]
            for field in ("id", "name", "group", "url")
        }
        for asset in context.get("available_assets", [])[:5]
    ]
    return {
        "source_kind": kind or "market",
        "source_id": source_id,
        "source_item": source_item,
        "first_person_allowed": bool(
            kind == "life" and source_item and source_item.get("first_person_allowed")
        ),
        "available_assets": assets,
    }


def compact_editorial_source_item(item: dict | None, text_limit: int = 400,
                                   list_limit: int = 5, list_text_limit: int = 160):
    if not item:
        return None
    result = {}
    for key, value in item.items():
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:text_limit]
        elif isinstance(value, list):
            result[key] = [str(entry)[:list_text_limit] for entry in value[:list_limit]]
    return result


def minimal_editorial_writer_context(writer_context: dict):
    return {
        "source_kind": writer_context["source_kind"],
        "source_id": writer_context["source_id"],
        "source_item": compact_editorial_source_item(
            writer_context.get("source_item"), 180, 2, 100
        ),
        "first_person_allowed": writer_context["first_person_allowed"],
    }


def editorial_candidate_asset_id(input_snapshot: dict, topic: dict):
    context = input_snapshot.get("approved_editorial_context", {})
    available = {item.get("id") for item in context.get("available_assets", [])}
    requested = [asset_id for asset_id in topic.get("asset_ids", []) if asset_id in available]
    if requested:
        return requested[0]
    selected = [asset_id for asset_id in context.get("available_asset_ids", []) if asset_id in available]
    return selected[0] if len(selected) == 1 else ""


SAFE_FIRST_PERSON_OPINION_LEADS = (
    "我认为", "我觉得", "我的判断是", "我的判断", "我的理解是", "我的理解",
    "我倾向于", "我倾向", "我更关心", "在我看来",
)


def unauthorized_first_person_experience(post: str, writer_context: dict):
    if writer_context.get("first_person_allowed"):
        return False
    remaining = post
    for phrase in SAFE_FIRST_PERSON_OPINION_LEADS:
        remaining = remaining.replace(phrase, "")
    return any(pronoun in remaining for pronoun in ("我", "本人", "自己"))


async def generate_pending_persona_editorial_candidates(run_id: int, context_date: str):
    with db() as conn:
        run = conn.execute(
            "SELECT status FROM daily_context_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run or run["status"] != "approved":
            return
        rows = conn.execute(
            """SELECT e.*,p.slug,p.name,p.draft FROM persona_editorial_evaluations e
               JOIN personas p ON p.id=e.persona_id
               WHERE e.run_id=? AND e.status='WRITE' AND e.candidate_id IS NULL
               ORDER BY e.id""",
            (run_id,),
        ).fetchall()
    for row in rows:
        evaluation = dict(row)
        source = f"persona_editorial:{evaluation['id']}"
        with db() as conn:
            snapshot = json_value(evaluation.get("input_json"), {})
            if snapshot != current_editorial_input_payload(conn, evaluation, context_date):
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "input_changed_before_generation",
                    "评估输入已变化，旧 WRITE 不再生成正文。",
                )
                continue
            if editorial_claim_already_drafted(conn, evaluation):
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "historical_duplicate_before_generation",
                    "评估完成后已有其他候选覆盖相同核心主张。",
                )
                continue
            existing_candidate = conn.execute(
                "SELECT id FROM post_candidates WHERE persona_id=? AND context_date=? AND source=?",
                (evaluation["persona_id"], context_date, source),
            ).fetchone()
            if existing_candidate:
                conn.execute(
                    "UPDATE persona_editorial_evaluations SET candidate_id=?,updated_at=? WHERE id=?",
                    (existing_candidate["id"], int(time.time()), evaluation["id"]),
                )
                recovered = dict(conn.execute(
                    "SELECT * FROM persona_editorial_evaluations WHERE id=?", (evaluation["id"],)
                ).fetchone())
                record_persona_editorial_claim(conn, recovered, context_date)
                continue
        topic = json_value(evaluation["topic_json"], {})
        topic_fields = (
            "claim_key", "subject", "title", "core_claim", "content_type", "material_delta",
            "audience_value", "why_now", "fact_basis", "opinion_basis", "source_topic_keys",
            "scope", "source_kind", "source_id", "source_refs", "angle", "asset_ids",
            "first_person_allowed",
        )
        compact_topic = {}
        for key in topic_fields:
            if key not in topic:
                continue
            value = topic[key]
            if isinstance(value, str):
                compact_topic[key] = value[:350]
            elif isinstance(value, list):
                compact_topic[key] = [str(item)[:120] for item in value[:10]]
            else:
                compact_topic[key] = value
        compact_daily = dict(snapshot["daily"])
        for key in ("market_state", "event_clusters", "debates", "evidence", "unknowns"):
            compact_daily[key] = str(compact_daily.get(key, ""))[:500]
        compact_daily["sources"] = list(compact_daily.get("sources") or [])[:5]
        writer_context = editorial_writer_context(snapshot, topic)
        facts = json.dumps({
            "date": context_date, "topic": compact_topic,
            "editorial_claim": evaluation["core_claim"][:600], "daily_market": compact_daily,
            "why_this_persona": evaluation["why_me"][:400], "rationale": evaluation["rationale"][:400],
            "approved_persona_context": writer_context,
        }, ensure_ascii=False)
        if len(facts) > 7900:
            compact_daily["sources"] = []
            compact_daily["evidence"] = compact_daily["evidence"][:200]
            facts = json.dumps({
                "date": context_date, "topic": compact_topic,
                "editorial_claim": evaluation["core_claim"][:600], "daily_market": compact_daily,
                "why_this_persona": evaluation["why_me"][:400], "rationale": evaluation["rationale"][:400],
                "approved_persona_context": writer_context,
            }, ensure_ascii=False)
        if len(facts) > 7900:
            facts = json.dumps({
                "date": context_date,
                "topic": {
                    key: str(topic.get(key, ""))[:300]
                    for key in ("claim_key", "title", "core_claim", "material_delta", "audience_value")
                },
                "editorial_claim": evaluation["core_claim"][:500],
                "daily_market": {
                    key: str(snapshot["daily"].get(key, ""))[:300]
                    for key in ("market_state", "event_clusters", "debates", "evidence", "unknowns")
                },
                "why_this_persona": evaluation["why_me"][:250],
                "approved_persona_context": minimal_editorial_writer_context(writer_context),
            }, ensure_ascii=False)
        if len(facts) > 7900:
            with db() as conn:
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "writer_context_too_large",
                    "写作输入超过受控长度，未生成正文。",
                )
            continue
        try:
            generated = await generate_persona_post(
                evaluation["persona_id"], PostGenerationIn(facts=facts)
            )
        except HTTPException:
            continue
        if unauthorized_first_person_experience(generated["post"], writer_context):
            with db() as conn:
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "unsupported_first_person_experience",
                    "生成稿出现未获生活事实支持的具体第一人称经历。",
                )
            continue
        now = int(time.time())
        with db() as conn:
            current = conn.execute(
                "SELECT * FROM persona_editorial_evaluations WHERE id=?", (evaluation["id"],)
            ).fetchone()
            if not current or current["candidate_id"] is not None or current["status"] != "WRITE":
                continue
            if snapshot != current_editorial_input_payload(conn, dict(current), context_date):
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "input_changed_during_generation",
                    "正文生成期间评估输入发生变化，生成结果已丢弃。",
                )
                continue
            if editorial_claim_already_drafted(conn, dict(current)):
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "historical_duplicate_during_generation",
                    "正文生成期间已有其他候选覆盖相同核心主张。",
                )
                continue
            conn.execute(
                """INSERT INTO post_candidates(
                    persona_id,context_date,title,body,status,source,asset_id,notes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(persona_id,context_date,source) DO NOTHING""",
                (
                    evaluation["persona_id"], context_date, evaluation["core_claim"], generated["post"],
                    "needs_review", source, editorial_candidate_asset_id(snapshot, topic),
                    f"Persona Editorial Engine 评估 {evaluation['id']}；未发布。", now, now,
                ),
            )
            candidate = conn.execute(
                "SELECT id FROM post_candidates WHERE persona_id=? AND context_date=? AND source=?",
                (evaluation["persona_id"], context_date, source),
            ).fetchone()
            if not candidate:
                continue
            conn.execute(
                "UPDATE persona_editorial_evaluations SET candidate_id=?,updated_at=? WHERE id=?",
                (candidate["id"], now, evaluation["id"]),
            )
            saved = dict(conn.execute(
                "SELECT * FROM persona_editorial_evaluations WHERE id=?", (evaluation["id"],)
            ).fetchone())
            record_persona_editorial_claim(conn, saved, context_date)


async def recover_pending_persona_editorial_candidates(run_id: int | None = None):
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT r.id AS run_id,r.context_date
               FROM daily_context_runs r
               JOIN persona_editorial_evaluations e ON e.run_id=r.id
               WHERE r.status='approved' AND e.status='WRITE' AND e.candidate_id IS NULL
                 AND (? IS NULL OR r.id=?)
               ORDER BY r.context_date DESC"""
            , (run_id, run_id)
        ).fetchall()
    recovered = []
    for row in rows:
        resolve_persona_editorial_collisions(row["run_id"])
        await generate_pending_persona_editorial_candidates(row["run_id"], row["context_date"])
        recovered.append(row["run_id"])
    return recovered


async def run_persona_editorial_pipeline(run_id: int | None = None):
    """Evaluate approved context only; WRITE creates a review-only candidate, never a post."""
    if not persona_editorial_enabled():
        return []
    processed = await recover_pending_persona_editorial_candidates(run_id)
    with db() as conn:
        if run_id is None:
            runs = conn.execute(
                """SELECT * FROM daily_context_runs
                   WHERE status='approved' AND context_date=? ORDER BY context_date DESC""",
                (shanghai_today(),),
            ).fetchall()
        else:
            runs = conn.execute(
                """SELECT * FROM daily_context_runs
                   WHERE id=? AND status='approved' AND context_date=?""",
                (run_id, shanghai_today()),
            ).fetchall()
    for run_row in runs:
        run = daily_context_run_dict(run_row)
        cards = run["raw_cards"] if isinstance(run["raw_cards"], dict) else {}
        public_topics = [
            item for item in cards.get("selected_topics", [])
            if isinstance(item, dict) and item.get("claim_key")
        ]
        with db() as conn:
            daily_row = conn.execute(
                "SELECT * FROM daily_market_contexts WHERE context_date=?", (run["context_date"],)
            ).fetchone()
            slugs = daily_post_persona_slugs()
            if not slugs:
                continue
            personas = conn.execute(
                "SELECT id,slug,draft FROM personas WHERE slug IN (%s) ORDER BY id" % ",".join("?" * len(slugs)),
                slugs,
            ).fetchall()
            history = [dict(row) for row in conn.execute(
                """SELECT h.claim_key,h.persona_id,h.subject,h.core_claim,h.context_date,h.source,h.status
                   FROM topic_claim_history h
                   WHERE h.status<>'superseded'
                     AND NOT (h.source='daily_context_run' AND h.persona_id IS NULL)
                   ORDER BY h.last_seen_at DESC LIMIT 200"""
            ).fetchall()]
        if not daily_row:
            continue
        daily = daily_context_dict(daily_row)
        daily["approval_revision"] = run.get("approval_revision", 0)
        for persona_row in personas:
            persona = dict(persona_row)
            with db() as conn:
                context_row = conn.execute(
                    "SELECT * FROM persona_contexts WHERE persona_id=?", (persona["id"],)
                ).fetchone()
                persona_context = persona_context_dict(context_row) if context_row else {}
                editorial_context = approved_persona_editorial_context(
                    conn, persona["id"], persona["slug"], run["id"]
                )
                stable_history = editorial_stable_claim_history(
                    conn, run["context_date"], persona["id"]
                )
                existing_hashes = {
                    row["topic_input_hash"] for row in conn.execute(
                        "SELECT topic_input_hash FROM persona_editorial_evaluations WHERE run_id=? AND persona_id=?",
                        (run["id"], persona["id"]),
                    ).fetchall()
                }
                today_count = conn.execute(
                    """SELECT COUNT(*) FROM persona_editorial_evaluations
                       WHERE persona_id=? AND status='WRITE' AND candidate_id IS NOT NULL
                       AND EXISTS (SELECT 1 FROM daily_context_runs r WHERE r.id=persona_editorial_evaluations.run_id AND r.context_date=?)""",
                    (persona["id"], run["context_date"]),
                ).fetchone()[0]
            topics = public_topics + build_persona_private_topics(editorial_context)
            if not topics:
                continue
            persona["_editorial_context"] = editorial_context
            inputs = []
            for topic in topics:
                input_payload = editorial_topic_input_payload(
                    topic, daily, persona, persona_context, topics=topics,
                    claim_history=stable_history, editorial_context=editorial_context,
                )
                encoded = json.dumps(
                    input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                inputs.append((
                    topic,
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    input_payload,
                ))
            pending = [item for item in inputs if item[1] not in existing_hashes]
            if pending:
                try:
                    decisions = await evaluate_persona_editorial(
                        persona, persona_context, daily, topics, stable_history, today_count
                    )
                except RuntimeError:
                    continue
                decisions = apply_editorial_marginal_threshold(decisions, today_count)
                decisions = apply_editorial_claim_history(persona["id"], decisions, history)
                write_persona_editorial_evaluations(run["id"], persona["id"], pending, decisions)
        resolve_persona_editorial_collisions(run["id"])
        await generate_pending_persona_editorial_candidates(run["id"], run["context_date"])
        processed.append(run["id"])
    return processed


async def refresh_daily_post_draft(context_date: str, run_id: int, cards: dict | None = None, synthesis: dict | None = None):
    # Compatibility entry point for older callers. The approved run is the sole input authority.
    return await run_persona_editorial_pipeline(run_id)


async def execute_daily_context_run(run_id: int):
    run = get_daily_context_run(run_id)
    paths = daily_context_paths(run["context_date"])
    manifest = {
        "context_date": run["context_date"],
        "accounts_path": str(paths["accounts"]),
        "source_db": str(paths["source_db"]),
        "output": str(paths["output"]),
        "stages": {},
    }
    try:
        if not paths["accounts"].exists():
            raise RuntimeError("母池账号配置不存在")
        paths["root"].mkdir(parents=True, exist_ok=True)
        sources = market_sources_module()
        key = twitter241_api_key()
        collect_result = await asyncio.to_thread(
            sources.collect,
            paths["accounts"],
            paths["source_db"],
            paths["output"],
            key=key,
            hours=int(os.getenv("XOPS_DAILY_CONTEXT_HOURS", "30")),
            workers=int(os.getenv("XOPS_DAILY_CONTEXT_WORKERS", "8")),
            resume_hours=int(os.getenv("XOPS_DAILY_CONTEXT_RESUME_HOURS", "20")),
        )
        snapshot_output = Path(collect_result.get("snapshot_dir", paths["output"]))
        manifest["output"] = str(snapshot_output)
        manifest["stages"]["collect"] = collect_result
        update_daily_context_run(run_id, raw_manifest=json.dumps(manifest, ensure_ascii=False))

        validation_result = await asyncio.to_thread(
            sources.cross_validate,
            paths["source_db"],
            snapshot_output,
            hours=int(os.getenv("XOPS_DAILY_CONTEXT_HOURS", "30")),
        )
        manifest["stages"]["cross_validate"] = validation_result
        facts = read_card_file(snapshot_output / "fact_cards.json", "cards")
        opinions = read_card_file(snapshot_output / "opinion_cards.json", "opinions")
        attention_topics = read_card_file(snapshot_output / "attention_topics.json", "topics")
        if not attention_topics:
            attention_topics = read_card_file(snapshot_output / "attention_topics.json", "hot")
        attention_topics = attention_topics[:20]
        discussion_path = snapshot_output / "discussion_topics.json"
        discussion_topics = read_card_file(discussion_path, "hot")
        hot_discussion_topics = discussion_topics[:20]
        if not discussion_topics and not discussion_path.exists():
            discussion_topics = attention_topics
        discussion_topics = discussion_topics[:20]
        opportunity_questions = build_opportunity_questions(discussion_topics)
        editorial_questions = build_editorial_questions(hot_discussion_topics)
        research_questions = build_research_questions(discussion_topics)
        niche_topics = [
            {
                key: item[key]
                for key in ("title", "key", "unique_authors", "post_count")
                if key in item
            }
            for item in read_card_file(snapshot_output / "attention_topics.json", "niche")
            if isinstance(item, dict)
        ]
        coverage = {
            "collect": collect_result if isinstance(collect_result, dict) else {},
            "cross_validate": validation_result if isinstance(validation_result, dict) else {},
            "fact_cards": len(facts),
            "opinion_cards": len(opinions),
            "attention_topics": len(attention_topics),
            "discussion_topics": len(discussion_topics),
            "opportunity_questions": len(opportunity_questions),
            "editorial_questions": len(editorial_questions),
            "research_questions": len(research_questions),
            "niche_topics": len(niche_topics),
        }
        manifest["count"] = coverage["collect"].get("posts_seen", 0)
        covered_accounts = coverage["collect"].get("accounts_fetched", 0) + coverage["collect"].get(
            "accounts_skipped", 0
        )
        if not covered_accounts:
            raise RuntimeError("母池账号抓取全部失败")
        if not coverage["collect"].get("posts_seen") and not coverage["cross_validate"].get("source_posts"):
            raise RuntimeError("母池没有可用帖子")
        if not facts and not opinions:
            raise RuntimeError("交叉验证未产出可用事实或观点卡")
        full_cards = {
            "coverage": coverage,
            "topic_selection_policy": topic_selection_policy(),
            "discussion_topics": discussion_topics,
            "opportunity_questions": opportunity_questions,
            "editorial_questions": editorial_questions,
            "research_questions": research_questions,
            "attention_topics": attention_topics,
            "niche_topics": niche_topics,
            "fact_cards": facts,
            "opinion_cards": opinions,
        }
        cards = controlled_cards(
            facts,
            opinions,
            coverage,
            attention_topics,
            niche_topics,
            discussion_topics,
            opportunity_questions,
            editorial_questions,
            research_questions,
            topic_selection_policy(),
            recent_topic_claims(),
        )
        update_daily_context_run(
            run_id,
            raw_manifest=json.dumps(manifest, ensure_ascii=False),
            raw_cards=json.dumps(full_cards, ensure_ascii=False),
        )
        synthesis = await synthesize_daily_cards(run["context_date"], cards)
        selected_topics = synthesis.get("selected_topics", [])
        full_cards["question_candidates"] = {
            "opportunity": opportunity_questions,
            "editorial": editorial_questions,
            "research": research_questions,
        }
        full_cards["selected_topics"] = selected_topics
        full_cards["rejected_topics"] = synthesis.get("rejected_topics", [])
        full_cards["opportunity_questions"] = [
            item for item in selected_topics if item.get("content_type") == "opportunity"
        ]
        full_cards["editorial_questions"] = [
            item for item in selected_topics if item.get("content_type") == "editorial"
        ]
        full_cards["research_questions"] = [
            item for item in selected_topics if item.get("content_type") == "research"
        ]
        coverage["selected_topics"] = len(selected_topics)
        coverage["rejected_topics"] = len(full_cards["rejected_topics"])
        result = update_daily_context_run(
            run_id,
            status="needs_review",
            raw_cards=json.dumps(full_cards, ensure_ascii=False),
            synthesis=json.dumps(synthesis, ensure_ascii=False),
            error="",
            completed_at=int(time.time()),
        )
        return result
    except Exception as error:
        manifest["failed_stage"] = next(reversed(manifest["stages"]), "setup")
        return update_daily_context_run(
            run_id,
            status="failed",
            raw_manifest=json.dumps(manifest, ensure_ascii=False),
            error=str(error)[:1000],
            completed_at=int(time.time()),
        )


def queue_daily_context(context_date: str, trigger: str):
    run, created = create_daily_context_run(context_date, trigger)
    if not created:
        return run, False

    task = asyncio.create_task(execute_daily_context_run(run["id"]))
    DAILY_CONTEXT_TASKS.add(task)
    task.add_done_callback(DAILY_CONTEXT_TASKS.discard)
    return run, True


async def run_due_daily_context():
    if not daily_context_scheduler_enabled():
        return
    now = datetime.now(TZ)
    hour, minute = daily_context_schedule()
    if (now.hour, now.minute) < (hour, minute):
        return
    queue_daily_context(now.date().isoformat(), "schedule")


async def daily_context_scheduler():
    while True:
        try:
            await run_due_daily_context()
        except Exception:
            pass
        await asyncio.sleep(30)


async def run_due_daily_post():
    return await run_persona_editorial_pipeline()


async def persona_editorial_scheduler():
    while True:
        try:
            await run_persona_editorial_pipeline()
        except Exception:
            pass
        await asyncio.sleep(30)


def recover_interrupted_daily_context_run():
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """UPDATE daily_context_runs
               SET status='queued', error='服务重启前的运行待补跑', updated_at=?
               WHERE context_date=? AND status='running'""",
            (now, shanghai_today()),
        )
        conn.execute(
            """UPDATE daily_context_runs
               SET status='failed', error='服务重启前的历史运行已中断', completed_at=?, updated_at=?
               WHERE context_date<>? AND status='running'""",
            (now, now, shanghai_today()),
        )


@asynccontextmanager
async def lifespan(_app):
    init_db()
    recover_interrupted_daily_context_run()
    context_task = asyncio.create_task(daily_context_scheduler())
    editorial_task = asyncio.create_task(persona_editorial_scheduler())
    yield
    context_task.cancel()
    editorial_task.cancel()
    for task in list(DAILY_CONTEXT_TASKS):
        task.cancel()


app = FastAPI(lifespan=lifespan)
if CHARACTERS_DIR.exists():
    app.mount("/assets/characters", StaticFiles(directory=CHARACTERS_DIR), name="characters")


@app.get("/health")
def health():
    hour, minute = daily_context_schedule()
    return {
        "ok": True,
        "daily_context_enabled": daily_context_scheduler_enabled(),
        "daily_context_run_time": f"{hour:02d}:{minute:02d}",
        "timezone": str(TZ),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(INDEX_HTML.replace("__BASE_URL__", os.environ["XOPS_BASE_URL"].rstrip("/")))


@app.get("/personas", response_class=HTMLResponse)
def persona_center():
    html = read_text(APP_DIR / "persona_center.html")
    return HTMLResponse(
        html.replace("__BASE_URL__", os.environ["XOPS_BASE_URL"].rstrip("/")),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/market", response_class=HTMLResponse)
def market_context_center():
    html = read_text(APP_DIR / "market_context.html")
    return HTMLResponse(
        html.replace("__BASE_URL__", os.environ["XOPS_BASE_URL"].rstrip("/")),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/context/projects")
def list_project_contexts():
    with db() as conn:
        rows = conn.execute("SELECT * FROM project_contexts ORDER BY name COLLATE NOCASE").fetchall()
    return [project_context_dict(row) for row in rows]


@app.get("/api/context/projects/{slug}")
def get_project_context(slug: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM project_contexts WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "Project context not found")
    return project_context_dict(row)


@app.put("/api/context/projects/{slug}")
def put_project_context(slug: str, request: ProjectContextIn):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """INSERT INTO project_contexts(
                slug,name,aliases,audience_baseline,native_context,market_structure,
                recurring_debates,current_state,sources,updated_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name,aliases=excluded.aliases,audience_baseline=excluded.audience_baseline,
                native_context=excluded.native_context,market_structure=excluded.market_structure,
                recurring_debates=excluded.recurring_debates,current_state=excluded.current_state,
                sources=excluded.sources,updated_at=excluded.updated_at,expires_at=excluded.expires_at""",
            (
                slug,
                request.name,
                json.dumps(request.aliases, ensure_ascii=False),
                request.audience_baseline,
                request.native_context,
                request.market_structure,
                request.recurring_debates,
                request.current_state,
                json.dumps(request.sources, ensure_ascii=False),
                now,
                request.expires_at,
            ),
        )
        row = conn.execute("SELECT * FROM project_contexts WHERE slug=?", (slug,)).fetchone()
    return project_context_dict(row)


@app.get("/api/context/daily/latest")
def get_latest_daily_context():
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_market_contexts ORDER BY context_date DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise HTTPException(404, "Daily market context not found")
    return daily_context_dict(row)


@app.get("/api/context/daily/{context_date}")
def get_daily_context(context_date: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Daily market context not found")
    return daily_context_dict(row)


def save_daily_context_row(conn, context_date: str, request: DailyMarketContextIn, now: int):
    conn.execute(
        """INSERT INTO daily_market_contexts(
            context_date,market_state,event_clusters,debates,evidence,unknowns,raw_feed,sources,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(context_date) DO UPDATE SET
            market_state=excluded.market_state,event_clusters=excluded.event_clusters,
            debates=excluded.debates,evidence=excluded.evidence,unknowns=excluded.unknowns,
            raw_feed=excluded.raw_feed,sources=excluded.sources,updated_at=excluded.updated_at""",
        (
            context_date,
            request.market_state,
            request.event_clusters,
            request.debates,
            request.evidence,
            request.unknowns,
            request.raw_feed,
            json.dumps(request.sources, ensure_ascii=False),
            now,
        ),
    )
    return conn.execute(
        "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
    ).fetchone()


def save_daily_context(context_date: str, request: DailyMarketContextIn):
    with db() as conn:
        row = save_daily_context_row(conn, context_date, request, int(time.time()))
    return daily_context_dict(row)


@app.put("/api/context/daily/{context_date}")
def put_daily_context(context_date: str, request: DailyMarketContextIn):
    return save_daily_context(context_date, request)


def synthesis_text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


@app.post("/api/context/daily/{context_date}/synthesize")
async def synthesize_daily_context(context_date: str, request: DailySynthesisIn):
    prompt = (
        "你是 Crypto 市场研究编辑。把以下 X 母池原始信息整理成当天市场状态，输出 JSON 对象。\n"
        "字段必须是 market_state,event_clusters,debates,evidence,unknowns,sources。\n"
        "market_state 写已发生的市场状态；event_clusters 按事件聚类；debates 写市场解释和不同判断；"
        "evidence 只保留可由输入确认的事实和链接线索；unknowns 写尚不能确认的关键缺口；"
        "sources 是来源对象数组。严格区分确认事实、市场解读、分歧和未知。不得把作者的交易、持仓或生活经历复制为通用事实。"
        "没有来源时明确写未知，不要补造。\n\n原始信息：\n"
        f"{request.raw_feed}"
    )
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                os.getenv("XOPS_LLM_BASE_URL", "https://api.deepseek.com") + "/chat/completions",
                headers={"Authorization": f"Bearer {llm_api_key()}"},
                json={
                    "model": os.getenv("XOPS_LLM_MODEL", "deepseek-chat"),
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                },
            )
            response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(502, "每日市场状态生成失败") from error
    context = DailyMarketContextIn(
        market_state=synthesis_text(result.get("market_state")),
        event_clusters=synthesis_text(result.get("event_clusters")),
        debates=synthesis_text(result.get("debates")),
        evidence=synthesis_text(result.get("evidence")),
        unknowns=synthesis_text(result.get("unknowns")),
        raw_feed=request.raw_feed,
        sources=result.get("sources") if isinstance(result.get("sources"), list) else [],
    )
    return {
        "context_date": context_date,
        "date": context_date,
        **context.model_dump(),
    }


@app.get("/api/context/daily-runs")
def list_daily_context_runs(status: str | None = None, limit: int = 30):
    limit = max(1, min(limit, 100))
    with db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM daily_context_runs WHERE status=? ORDER BY context_date DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM daily_context_runs ORDER BY context_date DESC LIMIT ?", (limit,)
            ).fetchall()
    return [daily_context_run_dict(row) for row in rows]


def get_daily_context_run_for_date(context_date: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_context_runs WHERE context_date=?", (context_date,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Daily context run not found")
    return daily_context_run_dict(row)


@app.get("/api/context/daily-runs/{context_date}/source-posts")
def get_daily_context_source_posts(context_date: str, limit: int = 50, offset: int = 0):
    run = get_daily_context_run_for_date(context_date)
    if run["status"] in {"queued", "running"}:
        raise HTTPException(404, "Mother-pool source posts are not available until the run completes")
    path = daily_context_paths(context_date)["output"] / "latest.json"
    if not path.exists():
        raise HTTPException(404, "Mother-pool source posts artifact not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise HTTPException(500, "Mother-pool source posts artifact is invalid") from error
    posts = payload.get("posts") if isinstance(payload, dict) else None
    if not isinstance(posts, list):
        raise HTTPException(500, "Mother-pool source posts artifact is invalid")
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    fields = ("post_id", "author_id", "handle", "text", "created_at", "url", "is_reply", "source_lists")
    return {
        "context_date": context_date,
        "generated_at": payload.get("generated_at"),
        "since": payload.get("since"),
        "total": len(posts),
        "limit": limit,
        "offset": offset,
        "coverage": {
            key: payload.get(key, 0)
            for key in (
                "account_universe",
                "accounts_covered",
                "accounts_fetched",
                "accounts_skipped",
                "accounts_failed",
            )
        },
        "posts": [{key: post.get(key) for key in fields} for post in posts[offset : offset + limit] if isinstance(post, dict)],
    }


@app.get("/api/context/daily-runs/{context_date}")
def get_daily_context_run_detail(context_date: str):
    return get_daily_context_run_for_date(context_date)


@app.post("/api/context/daily-runs/{context_date}/run")
async def start_daily_context_run(context_date: str, force: bool = False):
    if context_date != shanghai_today():
        raise HTTPException(422, "Manual mother-pool runs are limited to today's Shanghai date")
    existing = None
    try:
        existing = get_daily_context_run_for_date(context_date)
    except HTTPException as error:
        if error.status_code != 404:
            raise
    if existing and force and existing["status"] == "failed":
        return await retry_daily_context_run(context_date)
    run, started = queue_daily_context(context_date, "manual")
    return {**run, "started": started}


@app.post("/api/context/daily-runs/{context_date}/retry")
async def retry_daily_context_run(context_date: str):
    if context_date != shanghai_today():
        raise HTTPException(422, "Manual mother-pool retries are limited to today's Shanghai date")
    run = get_daily_context_run_for_date(context_date)
    if run["status"] != "failed":
        raise HTTPException(409, "Only failed daily context runs can be retried")
    run_id = run["id"]
    now = int(time.time())
    update_daily_context_run(
        run_id,
        status="running",
        trigger="retry",
        error="",
        started_at=now,
        completed_at=None,
        approved_at=None,
    )
    task = asyncio.create_task(execute_daily_context_run(run_id))
    DAILY_CONTEXT_TASKS.add(task)
    task.add_done_callback(DAILY_CONTEXT_TASKS.discard)
    return get_daily_context_run(run_id)


@app.put("/api/context/daily-runs/{context_date}/review")
def review_daily_context_run(context_date: str, request: DailyMarketContextIn):
    run = get_daily_context_run_for_date(context_date)
    if run["status"] not in {"needs_review", "approved"}:
        raise HTTPException(409, "Only completed daily context runs can be reviewed")
    synthesis = json.dumps(
        {
            "market_state": request.market_state,
            "event_clusters": request.event_clusters,
            "debates": request.debates,
            "evidence": request.evidence,
            "unknowns": request.unknowns,
            "sources": request.sources,
            "opportunity_questions": run["synthesis"].get("opportunity_questions", []),
            "editorial_questions": run["synthesis"].get("editorial_questions", []),
            "research_questions": run["synthesis"].get("research_questions", []),
        },
        ensure_ascii=False,
    )
    now = int(time.time())
    with db() as conn:
        if run["status"] == "approved":
            conn.execute(
                """UPDATE post_candidates SET status='superseded',updated_at=?
                   WHERE id IN (SELECT candidate_id FROM persona_editorial_evaluations WHERE run_id=?)""",
                (now, run["id"]),
            )
            conn.execute(
                """UPDATE topic_claim_history SET status='superseded',last_seen_at=?
                   WHERE source IN (
                     SELECT 'persona_editorial:' || id FROM persona_editorial_evaluations WHERE run_id=?
                   )""",
                (now, run["id"]),
            )
            conn.execute(
                """UPDATE persona_editorial_evaluations
                   SET status='HOLD',reason_code='context_revised',
                       rationale='正式 Context 已撤回并重新审核。',updated_at=?
                   WHERE run_id=? AND status='WRITE'""",
                (now, run["id"]),
            )
            conn.execute(
                """UPDATE daily_context_runs
                   SET synthesis=?,status='needs_review',approved_at=NULL,updated_at=? WHERE id=?""",
                (synthesis, now, run["id"]),
            )
        else:
            conn.execute(
                "UPDATE daily_context_runs SET synthesis=?,updated_at=? WHERE id=?",
                (synthesis, now, run["id"]),
            )
        row = conn.execute("SELECT * FROM daily_context_runs WHERE id=?", (run["id"],)).fetchone()
    return daily_context_run_dict(row)


@app.post("/api/context/daily-runs/{context_date}/approve")
def approve_daily_context_run(context_date: str):
    now = int(time.time())
    with db() as conn:
        current_row = conn.execute(
            "SELECT * FROM daily_context_runs WHERE context_date=?", (context_date,)
        ).fetchone()
        if not current_row:
            raise HTTPException(404, "Daily context run not found")
        current = daily_context_run_dict(current_row)
        if current["status"] == "approved":
            return current
        if current["status"] != "needs_review":
            raise HTTPException(409, "Only reviewed daily context runs can be approved")
        draft = current["draft_context"]
        request = DailyMarketContextIn(
            market_state=draft["market_state"],
            event_clusters=draft["event_clusters"],
            debates=draft["debates"],
            evidence=draft["evidence"],
            unknowns=draft["unknowns"],
            raw_feed="",
            sources=draft["sources"],
        )
        save_daily_context_row(conn, context_date, request, now)
        conn.execute(
            """UPDATE daily_context_runs
               SET status='approved',approved_at=?,approval_revision=approval_revision+1,updated_at=?
               WHERE id=?""",
            (now, now, current["id"]),
        )
        row = conn.execute(
            "SELECT * FROM daily_context_runs WHERE id=?", (current["id"],)
        ).fetchone()
    return daily_context_run_dict(row)


@app.get("/api/personas/{persona_id}/context")
def get_persona_context(persona_id: int):
    with db() as conn:
        persona = conn.execute("SELECT id FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not persona:
            raise HTTPException(404, "Persona not found")
        row = conn.execute("SELECT * FROM persona_contexts WHERE persona_id=?", (persona_id,)).fetchone()
    if row:
        return persona_context_dict(row)
    return {
        "persona_id": persona_id,
        "audience_baseline": "",
        "prior_views": "",
        "watchlist": "",
        "unresolved": "",
        "forbidden_claims": "",
        "updated_at": None,
    }


@app.put("/api/personas/{persona_id}/context")
def put_persona_context(persona_id: int, request: PersonaContextIn):
    now = int(time.time())
    with db() as conn:
        persona = conn.execute("SELECT id FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not persona:
            raise HTTPException(404, "Persona not found")
        conn.execute(
            """INSERT INTO persona_contexts(
                persona_id,audience_baseline,prior_views,watchlist,unresolved,forbidden_claims,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(persona_id) DO UPDATE SET
                audience_baseline=excluded.audience_baseline,prior_views=excluded.prior_views,
                watchlist=excluded.watchlist,unresolved=excluded.unresolved,
                forbidden_claims=excluded.forbidden_claims,updated_at=excluded.updated_at""",
            (
                persona_id,
                request.audience_baseline,
                request.prior_views,
                request.watchlist,
                request.unresolved,
                request.forbidden_claims,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM persona_contexts WHERE persona_id=?", (persona_id,)).fetchone()
    return persona_context_dict(row)


@app.get("/api/personas/{persona_id}/editorial-context")
def get_persona_editorial_context(persona_id: int):
    with db() as conn:
        persona = conn.execute("SELECT id FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not persona:
            raise HTTPException(404, "Persona not found")
        row = conn.execute(
            "SELECT * FROM persona_editorial_contexts WHERE persona_id=?", (persona_id,)
        ).fetchone()
        result = persona_editorial_context_dict(row)
        result["expressed_source_ids"] = sorted(
            expressed_editorial_source_ids(conn, persona_id)
        )
    return result


@app.put("/api/personas/{persona_id}/editorial-context")
def put_persona_editorial_context(persona_id: int, request: PersonaEditorialContextIn):
    now = int(time.time())
    with db() as conn:
        persona = conn.execute(
            "SELECT id,slug FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Persona not found")
        raw = validate_persona_editorial_context_input(
            request.model_dump(), {item["id"] for item in persona_assets(persona["slug"])}
        )
        draft_json = json.dumps(raw, ensure_ascii=False)
        conn.execute(
            """INSERT INTO persona_editorial_contexts(
                persona_id,draft_json,approved_json,status,approval_revision,created_at,updated_at
            ) VALUES(?,?,'{}','needs_review',0,?,?)
            ON CONFLICT(persona_id) DO UPDATE SET
                draft_json=excluded.draft_json,status=CASE
                    WHEN persona_editorial_contexts.approved_json=excluded.draft_json
                         AND persona_editorial_contexts.approval_revision>0 THEN 'approved'
                    ELSE 'needs_review' END,
                updated_at=excluded.updated_at""",
            (persona_id, draft_json, now, now),
        )
        row = conn.execute(
            "SELECT * FROM persona_editorial_contexts WHERE persona_id=?", (persona_id,)
        ).fetchone()
    return persona_editorial_context_dict(row)


@app.post("/api/personas/{persona_id}/editorial-context/approve")
def approve_persona_editorial_context(persona_id: int):
    now = int(time.time())
    with db() as conn:
        persona = conn.execute(
            "SELECT id,slug FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Persona not found")
        row = conn.execute(
            "SELECT * FROM persona_editorial_contexts WHERE persona_id=?", (persona_id,)
        ).fetchone()
        if not row:
            raise HTTPException(409, "Save an editorial context draft before approving")
        draft = validate_persona_editorial_context_input(
            json_value(row["draft_json"], empty_persona_editorial_context()),
            {item["id"] for item in persona_assets(persona["slug"])},
        )
        draft_json = json.dumps(draft, ensure_ascii=False)
        if row["approval_revision"] and json_value(row["approved_json"], {}) == draft:
            conn.execute(
                "UPDATE persona_editorial_contexts SET status='approved',updated_at=? WHERE persona_id=?",
                (now, persona_id),
            )
        else:
            active = conn.execute(
                """SELECT e.id FROM persona_editorial_evaluations e
                   JOIN daily_context_runs r ON r.id=e.run_id
                   WHERE e.persona_id=? AND r.context_date=? AND e.status='WRITE'""",
                (persona_id, shanghai_today()),
            ).fetchall()
            for evaluation in active:
                supersede_persona_editorial_evaluation(
                    conn, evaluation["id"], "editorial_context_revised",
                    "该人设的正式 Editorial Context 已批准新版本。",
                )
            conn.execute(
                """UPDATE persona_editorial_contexts
                   SET draft_json=?,approved_json=?,status='approved',
                       approval_revision=approval_revision+1,approved_at=?,updated_at=?
                   WHERE persona_id=?""",
                (draft_json, draft_json, now, now, persona_id),
            )
        saved = conn.execute(
            "SELECT * FROM persona_editorial_contexts WHERE persona_id=?", (persona_id,)
        ).fetchone()
    return persona_editorial_context_dict(saved)


def assemble_context_pack(conn, persona_id: int, request: ContextPackIn):
    persona = conn.execute("SELECT id FROM personas WHERE id=?", (persona_id,)).fetchone()
    if not persona:
        raise HTTPException(404, "Persona not found")
    context_date = request.context_date
    if context_date:
        daily = conn.execute(
            "SELECT * FROM daily_market_contexts WHERE context_date=?", (context_date,)
        ).fetchone()
    else:
        daily = conn.execute(
            "SELECT * FROM daily_market_contexts ORDER BY context_date DESC LIMIT 1"
        ).fetchone()
    if not daily:
        raise HTTPException(422, "Daily market context is required")

    project_slugs = list(dict.fromkeys(request.project_slugs))
    projects = []
    for slug in project_slugs:
        project = conn.execute("SELECT * FROM project_contexts WHERE slug=?", (slug,)).fetchone()
        if not project:
            raise HTTPException(422, f"Project context not found: {slug}")
        projects.append(project_context_dict(project))
    persona_context = conn.execute(
        "SELECT * FROM persona_contexts WHERE persona_id=?", (persona_id,)
    ).fetchone()
    account_continuity = persona_context_dict(persona_context) if persona_context else {
        "persona_id": persona_id,
        "audience_baseline": "",
        "prior_views": "",
        "watchlist": "",
        "unresolved": "",
        "forbidden_claims": "",
        "updated_at": None,
    }
    daily_row = daily_context_dict(daily)
    daily_data = {
        key: daily_row[key]
        for key in (
            "context_date",
            "market_state",
            "event_clusters",
            "debates",
            "evidence",
            "unknowns",
            "sources",
            "updated_at",
        )
    }
    approved_run = conn.execute(
        "SELECT raw_cards,synthesis FROM daily_context_runs WHERE context_date=? AND status='approved'",
        (daily_data["context_date"],),
    ).fetchone()
    approved_cards = json_value(approved_run["raw_cards"], {}) if approved_run else {}
    approved_synthesis = json_value(approved_run["synthesis"], {}) if approved_run else {}
    attention_topics = approved_cards.get("attention_topics", []) if isinstance(approved_cards, dict) else []
    attention_topics = [item for item in attention_topics if isinstance(item, dict)][:10]
    all_discussion_topics = (
        approved_cards.get("discussion_topics", attention_topics)
        if isinstance(approved_cards, dict)
        else attention_topics
    )
    all_discussion_topics = [item for item in all_discussion_topics if isinstance(item, dict)]
    opportunity_questions = (
        approved_cards.get("opportunity_questions") if isinstance(approved_cards, dict) else None
    )
    if not isinstance(opportunity_questions, list):
        opportunity_questions = approved_synthesis.get("opportunity_questions")
    if not isinstance(opportunity_questions, list):
        opportunity_questions = build_opportunity_questions(all_discussion_topics)
    opportunity_questions = [item for item in opportunity_questions if isinstance(item, dict)]
    editorial_questions = approved_cards.get("editorial_questions") if isinstance(approved_cards, dict) else None
    if not isinstance(editorial_questions, list):
        editorial_questions = approved_synthesis.get("editorial_questions")
    if not isinstance(editorial_questions, list):
        editorial_questions = build_editorial_questions(all_discussion_topics)
    editorial_questions = [item for item in editorial_questions if isinstance(item, dict)]
    research_questions = approved_cards.get("research_questions") if isinstance(approved_cards, dict) else None
    if not isinstance(research_questions, list):
        research_questions = approved_synthesis.get("research_questions")
    if not isinstance(research_questions, list):
        research_questions = build_research_questions(all_discussion_topics)
    research_questions = [item for item in research_questions if isinstance(item, dict)]
    selected_question = selected_opportunity_question(request.topic, opportunity_questions)
    selected_editorial = selected_editorial_question(request.topic, editorial_questions)
    selected_research = selected_research_question(request.topic, research_questions)
    selected_question = selected_question or selected_editorial or selected_research
    source_keys = set(selected_question.get("source_topic_keys", [])) if selected_question else set()
    discussion_topics = [
        item for item in all_discussion_topics if not source_keys or item.get("key") in source_keys
    ][:10]
    if selected_question and str(selected_question.get("id", "")).startswith("research:"):
        attention_topics = []
    content = {
        "topic": request.topic,
        "topic_attention": topic_attention(
            str(selected_question.get("source_topic_title") if selected_question else request.topic), discussion_topics
        ),
        "discussion_topics": discussion_topics,
        "attention_topics": attention_topics,
        "opportunity_questions": [selected_question] if selected_question and selected_question.get("id", "").startswith("opportunity:") else [],
        "selected_opportunity_question": selected_question if selected_question and selected_question.get("id", "").startswith("opportunity:") else None,
        "editorial_questions": [selected_question] if selected_question and selected_question.get("id", "").startswith("editorial:") else [],
        "selected_editorial_question": selected_question if selected_question and selected_question.get("id", "").startswith("editorial:") else None,
        "research_questions": [selected_question] if selected_question and selected_question.get("id", "").startswith("research:") else [],
        "selected_research_question": selected_question if selected_question and selected_question.get("id", "").startswith("research:") else None,
        "daily_market": daily_data,
        "project_dossiers": projects,
        "account_continuity": account_continuity,
        "operator_notes": request.operator_notes,
        "evidence": {"daily": daily_data["evidence"], "sources": daily_data["sources"]},
        "unknowns": daily_data["unknowns"],
        "staleness": {
            "daily_updated_at": daily_data["updated_at"],
            "projects": [
                {"slug": project["slug"], "updated_at": project["updated_at"], "expires_at": project["expires_at"], "stale": project["stale"]}
                for project in projects
            ],
        },
    }
    return daily_data["context_date"], project_slugs, content


@app.post("/api/personas/{persona_id}/context-packs")
def create_context_pack(persona_id: int, request: ContextPackIn):
    now = int(time.time())
    with db() as conn:
        context_date, project_slugs, content = assemble_context_pack(conn, persona_id, request)
        cursor = conn.execute(
            """INSERT INTO context_packs(
                persona_id,topic,project_slugs,context_date,operator_notes,content,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                persona_id,
                request.topic,
                json.dumps(project_slugs, ensure_ascii=False),
                context_date,
                request.operator_notes,
                json.dumps(content, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM context_packs WHERE id=?", (cursor.lastrowid,)).fetchone()
    return context_pack_dict(row)


@app.get("/api/context-packs/{pack_id}")
def get_context_pack(pack_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM context_packs WHERE id=?", (pack_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Context pack not found")
    return context_pack_dict(row)


@app.put("/api/context-packs/{pack_id}")
def update_context_pack(pack_id: int, request: dict):
    with db() as conn:
        row = conn.execute("SELECT * FROM context_packs WHERE id=?", (pack_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Context pack not found")
        content = json_value(row["content"], {})
        if set(request) == {"operator_notes"}:
            content["operator_notes"] = str(request["operator_notes"] or "")
        else:
            required = {"topic", "daily_market", "project_dossiers", "account_continuity"}
            if not required.issubset(request):
                raise HTTPException(422, "Context pack content is incomplete")
            content = request
        operator_notes = str(content.get("operator_notes", ""))
        conn.execute(
            "UPDATE context_packs SET operator_notes=?,content=?,updated_at=? WHERE id=?",
            (operator_notes, json.dumps(content, ensure_ascii=False), int(time.time()), pack_id),
        )
        row = conn.execute("SELECT * FROM context_packs WHERE id=?", (pack_id,)).fetchone()
    return context_pack_dict(row)


def persona_assets(slug):
    folder = CHARACTERS_DIR / slug
    if not folder.exists():
        return []
    collection = ASSET_COLLECTIONS.get(slug)
    roots = [folder / collection["folder"]] if collection else [folder]
    result = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            if path.name == "avatar-generated-v1.png":
                continue
            if path.stem.upper().startswith("PREVIEW"):
                continue
            relative = path.relative_to(CHARACTERS_DIR).as_posix()
            result.append(
                {
                    "id": f"{slug}:{path.name}",
                    "name": path.stem.replace("-", " "),
                    "group": path.parent.name,
                    "url": f"/assets/characters/{relative}",
                }
            )
    return result


def persona_asset_collection(slug):
    collection = ASSET_COLLECTIONS.get(slug)
    if not collection:
        return None
    assets = persona_assets(slug)
    return {
        **collection,
        "count": len(assets),
        "ready": len(assets) == collection["expected_count"],
    }


@app.get("/api/personas")
def list_personas():
    with db() as conn:
        rows = conn.execute(
            "SELECT id,slug,name,role,avatar,status,current_version,updated_at FROM personas ORDER BY id"
        ).fetchall()
    return [
        {
            **dict(row),
            **PERSONA_PUBLIC_PROFILE.get(
                row["slug"], {"display_name": row["name"], "handle": ""}
            ),
            "avatar_url": f"/assets/characters/{row['avatar']}" if row["avatar"] else None,
        }
        for row in rows
    ]


@app.get("/api/personas/{persona_id}/post-candidates")
def list_persona_post_candidates(persona_id: int):
    with db() as conn:
        persona = conn.execute("SELECT id FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not persona:
            raise HTTPException(404, "Persona not found")
        rows = conn.execute(
            """SELECT c.* FROM post_candidates c
               WHERE c.persona_id=?
                 AND NOT (
                   c.source LIKE 'persona_editorial:%' AND EXISTS (
                       SELECT 1 FROM persona_editorial_evaluations e
                       WHERE ('persona_editorial:' || e.id)=c.source AND e.status<>'WRITE'
                   )
                 )
               ORDER BY c.context_date DESC, c.id DESC""",
            (persona_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/personas/{persona_id}")
def get_persona(persona_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Persona not found")
        versions = conn.execute(
            "SELECT version,created_at FROM persona_versions WHERE persona_id=? ORDER BY version DESC",
            (persona_id,),
        ).fetchall()
    return {
        **dict(row),
        **PERSONA_PUBLIC_PROFILE.get(
            row["slug"], {"display_name": row["name"], "handle": ""}
        ),
        "draft": json.loads(row["draft"]),
        "avatar_url": f"/assets/characters/{row['avatar']}" if row["avatar"] else None,
        "assets": persona_assets(row["slug"]),
        "asset_collection": persona_asset_collection(row["slug"]),
        "versions": [dict(version) for version in versions],
    }


@app.put("/api/personas/{persona_id}")
def save_persona(persona_id: int, draft: PersonaDraftIn):
    identity = draft.data.get("identity", {})
    name = str(identity.get("name", "")).strip()
    role = str(identity.get("role", "")).strip()
    if not name or not role:
        raise HTTPException(400, "name and role are required")
    now = int(time.time())
    with db() as conn:
        cursor = conn.execute(
            """UPDATE personas SET name=?,role=?,draft=?,status='draft',updated_at=? WHERE id=?""",
            (name, role, json.dumps(draft.data, ensure_ascii=False), now, persona_id),
        )
        if not cursor.rowcount:
            raise HTTPException(404, "Persona not found")
    return {"id": persona_id, "status": "draft", "updated_at": now}


@app.post("/api/personas/{persona_id}/publish")
def publish_persona(persona_id: int):
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT draft,current_version FROM personas WHERE id=?", (persona_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Persona not found")
        version = row["current_version"] + 1
        conn.execute(
            "INSERT INTO persona_versions(persona_id,version,content,created_at) VALUES(?,?,?,?)",
            (persona_id, version, row["draft"], now),
        )
        conn.execute(
            "UPDATE personas SET status='published',current_version=?,updated_at=? WHERE id=?",
            (version, now, persona_id),
        )
    return {"id": persona_id, "status": "published", "version": version}


@app.post("/api/personas/{persona_id}/prompt-preview")
def preview_persona_prompt(persona_id: int, draft: PersonaDraftIn):
    if not draft.data.get("identity", {}).get("name"):
        raise HTTPException(400, "name is required")
    with db() as conn:
        row = conn.execute("SELECT slug FROM personas WHERE id=?", (persona_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Persona not found")
    sections = (
        ("身份与灵魂", draft.data.get("identity", {})),
        ("文风与口风", draft.data.get("voice", {})),
        ("内容策略", draft.data.get("content", {})),
        ("视觉设定", draft.data.get("visual", {})),
        ("正反例", draft.data.get("examples", {})),
    )
    parts = ["你正在为一个原创 AI 虚拟角色创作内容。严格保持角色一致，不冒充真人或专业人士。"]
    for title, values in sections:
        lines = [f"{key}: {value}" for key, value in values.items() if value not in (None, "")]
        parts.append(f"\n## {title}\n" + "\n".join(lines))
    collection = persona_asset_collection(row["slug"])
    assets = persona_assets(row["slug"])
    if collection:
        asset_lines = "\n".join(f"- {asset['id']}: {asset['name']}" for asset in assets)
        parts.append(
            f"\n## 已连接素材\n素材包: {collection['name']}\n"
            f"素材状态: {collection['count']}/{collection['expected_count']}\n"
            f"使用边界: {collection['usage']}\n可选素材:\n{asset_lines}"
        )
    parts.append("\n## 本次任务\n根据具体内容槽位生成候选稿；未确认的信息不补成事实，也不用等待后续的空话代替当前结论。")
    return {"prompt": "\n".join(parts)}


@app.post("/api/personas/{persona_id}/generate-post")
async def generate_persona_post(persona_id: int, request: PostGenerationIn):
    with db() as conn:
        row = conn.execute("SELECT slug,draft FROM personas WHERE id=?", (persona_id,)).fetchone()
        pack_row = (
            conn.execute("SELECT * FROM context_packs WHERE id=?", (request.context_pack_id,)).fetchone()
            if request.context_pack_id is not None
            else None
        )
    if not row:
        raise HTTPException(404, "Persona not found")
    facts = (request.facts or "").strip()
    if request.context_pack_id is not None:
        if not pack_row:
            raise HTTPException(404, "Context pack not found")
        if pack_row["persona_id"] != persona_id:
            raise HTTPException(422, "Context pack does not belong to persona")
    if not facts and not pack_row:
        raise HTTPException(422, "facts or context_pack_id is required")

    draft = json.loads(row["draft"])
    profile = PERSONA_PUBLIC_PROFILE.get(row["slug"], {})
    persona = {
        "账号名": profile.get("display_name", draft.get("identity", {}).get("name", "")),
        "身份与认知边界": draft.get("identity", {}),
        "口风": draft.get("voice", {}),
        "内容策略": draft.get("content", {}),
        "正反例": draft.get("examples", {}),
    }
    form_hint = secrets.choice(
        (
            "从具体事件切入，说明它为什么值得现在讨论",
            "像回应一个圈内争议，但把自己的判断依据讲清楚",
            "从一个数据或产品变化切入，把直接后果说完整",
            "像个人研究记录，直接给出当前能成立的结论和现实后果",
            "默认读者懂基础概念，直接讨论这次变化",
            "围绕一个明确判断自然展开，不写成报告目录",
        )
    )
    length_hint = secrets.choice(("160～260 字", "220～360 字", "按完整表达所需长度"))
    paragraph_hint = secrets.choice(("一段", "一到两段", "按自然停顿分段", "段落数量自由"))
    if pack_row:
        context_pack = context_pack_dict(pack_row)
        context_input = (
            "选题：\n"
            f"{context_pack['content'].get('topic', '')}\n\n"
            "选题热度匹配（hot 是当天默认的具体讨论议题；custom_or_niche 是人工指定的非默认热点）：\n"
            f"{json.dumps(context_pack['content'].get('topic_attention', {}), ensure_ascii=False)}\n\n"
            "读者机会题（确定性生成，优先围绕已选题写，不能自行另造）：\n"
            f"{json.dumps(context_pack['content'].get('selected_opportunity_question'), ensure_ascii=False)}\n\n"
            "观点 / 乐子题（确定性生成，只能围绕已选题写，不能自行另造）：\n"
            f"{json.dumps(context_pack['content'].get('selected_editorial_question'), ensure_ascii=False)}\n\n"
            "行业研究题（确定性生成，只能围绕已选题写，不能自行另造）：\n"
            f"{json.dumps(context_pack['content'].get('selected_research_question'), ensure_ascii=False)}\n\n"
            "当天可写讨论议题（按讨论热度排序）：\n"
            f"{json.dumps(context_pack['content'].get('discussion_topics', []), ensure_ascii=False)}\n\n"
            "当天父级热度地图（只用于理解市场注意力，不能单独反推具体选题）：\n"
            f"{json.dumps(context_pack['content'].get('attention_topics', []), ensure_ascii=False)}\n\n"
            "每日市场状态（这是今天为什么值得讨论，不是全部事实）：\n"
            f"{json.dumps(context_pack['content'].get('daily_market', {}), ensure_ascii=False)}\n\n"
            "项目长期语境（不把官方文档当成完整市场语境）：\n"
            f"{json.dumps(context_pack['content'].get('project_dossiers', []), ensure_ascii=False)}\n\n"
            "账号连续性（只用于保持此前判断一致，不能虚构持仓或经历）：\n"
            f"{json.dumps(context_pack['content'].get('account_continuity', {}), ensure_ascii=False)}\n\n"
            "可核验线索：\n"
            f"{json.dumps(context_pack['content'].get('evidence', {}), ensure_ascii=False)}\n\n"
            "未知与过期提示：\n"
            f"{json.dumps({'unknowns': context_pack['content'].get('unknowns', ''), 'staleness': context_pack['content'].get('staleness', {})}, ensure_ascii=False)}\n\n"
            "运营补充：\n"
            f"{context_pack['content'].get('operator_notes', '')}"
        )
    else:
        context_input = "未提供 Context Pack；只能依据补充事实生成，不得假设任何市场背景。"
    prompt = (
        "你在为原创虚拟角色写一条中文 X Post。只输出可直接发布的正文，不要解释、标题或引号。\n"
        "严格使用输入事实，不补造项目数据、收益、亲历或结论；未确认的信息不影响主判断时直接删除，不把未知项写成正文凑长度。\n"
        "当 Context Pack 写明受众默认知识时，不要重复解释这些基础概念；项目官方资料只是一层机制信息，不等于完整市场语境。\n"
        "不得把来源作者的交易、持仓、项目体验或生活经历移植为该账号经历；未知项不能被补成结论。\n"
        "补充事实中的 approved_persona_context 是唯一私人素材授权：只有 source_kind=life 且 first_person_allowed=true 才能写具体第一人称亲历；thought、feedback、expression_debt 和图片都不能证明亲历。\n"
        "没有上述亲历授权时，若观点必须用第一人称，只能用“我认为、我觉得、我的判断、我的理解、我倾向、我更关心、在我看来”引出判断，不得再写任何第一人称动作、账户、持仓或经历。\n"
        "输入中的观点只提炼核心判断，不得提及、猜测或影射原作者，也不得把观点改写成已确认事实；必须用该人设重新表达，不能整段照搬。\n"
        "若 Context Pack 有已选的读者机会题，就回答它；若有已选的观点 / 乐子题，就围绕它下判断或讲出新视角，不强塞操作步骤、收益测算或观察清单；若有已选的行业研究题，就围绕它做客观分析，先给一个能成立的核心结论，再用最必要的事实解释，不写成百科、资料堆砌或赚钱建议。三者都没有时再按人工选题写。对应 discussion_topics 是事实和判断主轴；attention_topics 只用于理解父级市场注意力，不能单独反推具体选题。不得从冷门官方材料反向挑题；custom_or_niche 仅表示人工自定义题可以写，不是当天默认热点。\n"
        "正文必须让没有看过前序对话的圈内读者看懂：交代具体触发，把因果链讲完整，给出该账号的判断及其边界；不要只抛结论或堆资料。\n"
        "结尾必须交付当下成立的判断、具体后果或有用解释，不能为稳妥硬加观察清单。禁止用“继续观察”“等后续材料”“再看正式文本”“尚未形成交易条件”“我会关注”等无信息占位收尾；素材不足时改选一个能下结论的角度。\n"
        "观点 / 乐子题可以有趣、有代入感或带一点调侃，但不能写成鸡汤、万能人生道理，不能评价未给出的公众人物或机构，也不能推断任何人的动机、人品、持仓或私下经历。\n"
        "不要使用标题、编号、项目符号、固定开头、固定结尾、固定句数或固定段数。不要为了完整而套模板。\n"
        "可以按人设偶尔使用项目参与号召，但不能默认使用、不能整段复用，也不能号召追涨、合约、杠杆或借钱投入。\n"
        f"本次仅作形式扰动：{form_hint}；长度参考 {length_hint}；{paragraph_hint}。不要把这句提示写进正文。\n\n"
        f"人设：\n{json.dumps(persona, ensure_ascii=False)}\n\n"
        f"Context Pack：\n{context_input}\n\n"
        f"补充事实、事件和观点（可为空）：\n{facts}"
    )
    messages = [{"role": "user", "content": prompt}]
    post = ""
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            for _ in range(2):
                response = await client.post(
                    os.getenv("XOPS_LLM_BASE_URL", "https://api.deepseek.com") + "/chat/completions",
                    headers={"Authorization": f"Bearer {llm_api_key()}"},
                    json={
                        "model": os.getenv("XOPS_LLM_MODEL", "deepseek-chat"),
                        "messages": messages,
                        "temperature": 0.95,
                    },
                )
                response.raise_for_status()
                post = response.json()["choices"][0]["message"]["content"].strip()
                if not any(phrase in post for phrase in EMPTY_WAITING_PHRASES):
                    break
                messages.extend(
                    [
                        {"role": "assistant", "content": post},
                        {
                            "role": "user",
                            "content": "这版用等待后续代替了当前结论。重新写：保留事实边界，直接给出现在能成立的判断和现实后果，不写观察清单或等待句。",
                        },
                    ]
                )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(502, "Post 生成失败") from error
    if any(phrase in post for phrase in EMPTY_WAITING_PHRASES):
        raise HTTPException(502, "Post 缺少当前结论")
    return {"post": post, "context_pack_id": request.context_pack_id}


def queued_post_rows(conn):
    return conn.execute(
        """SELECT c.*,p.slug persona_slug,p.name persona_name,p.avatar
           FROM post_candidates c JOIN personas p ON p.id=c.persona_id
           WHERE c.status='needs_review' AND (
               c.source LIKE 'initial_batch:%' OR (
                   c.source LIKE 'persona_editorial:%' AND EXISTS (
                       SELECT 1 FROM persona_editorial_evaluations e
                       JOIN daily_context_runs r ON r.id=e.run_id
                       WHERE ('persona_editorial:' || e.id)=c.source
                         AND e.status='WRITE' AND r.status='approved'
                   )
               )
           )
           ORDER BY c.persona_id,c.created_at,c.id"""
    ).fetchall()


@app.post("/api/post-candidates/{candidate_id}/published")
def mark_post_candidate_published(candidate_id: int):
    with db() as conn:
        candidate = conn.execute(
            "SELECT id,persona_id,status FROM post_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not candidate:
            raise HTTPException(404, "Post candidate not found")
        if candidate["status"] == "published":
            return {"id": candidate_id, "status": "published"}
        if candidate["status"] != "needs_review":
            raise HTTPException(409, "Post candidate is not publishable")
        head = next(
            (row for row in queued_post_rows(conn) if row["persona_id"] == candidate["persona_id"]),
            None,
        )
        if not head or head["id"] != candidate_id:
            raise HTTPException(409, "Post candidate is not the current queue head")
        conn.execute(
            "UPDATE post_candidates SET status='published',updated_at=? WHERE id=?",
            (int(time.time()), candidate_id),
        )
    return {"id": candidate_id, "status": "published"}


@app.get("/api/daily-post")
def get_daily_post():
    posts = get_daily_posts()
    if not posts:
        raise HTTPException(404, "Daily Post draft not found")
    return min(posts, key=lambda item: (item["created_at"], item["id"]))


def daily_post_asset_url(slug: str, _context_date: str, asset_id: str = ""):
    if not asset_id:
        return None
    return next(
        (asset["url"] for asset in persona_assets(slug) if asset["id"] == asset_id),
        None,
    )


@app.get("/api/daily-posts")
def get_daily_posts():
    with db() as conn:
        queued = queued_post_rows(conn)
    heads = {}
    remaining = {}
    for row in queued:
        persona_id = row["persona_id"]
        remaining[persona_id] = remaining.get(persona_id, 0) + 1
        heads.setdefault(persona_id, row)
    return [
        {
            **dict(row),
            "position": 1,
            "remaining": remaining[row["persona_id"]],
            "image_url": daily_post_asset_url(
                row["persona_slug"], row["context_date"], row["asset_id"]
            ),
            "image_note": (
                "已批准素材候选；发布前确认图片与正文匹配。"
                if row["asset_id"] else "本条未选择图片素材。"
            ),
        }
        for row in heads.values()
    ]


INDEX_HTML = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>每日 Post 草稿队列</title>
<style>body{font:15px/1.7 system-ui;max-width:1080px;margin:36px auto;padding:0 18px;color:#18181b;background:#f7f8fa}header{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:22px}h1{margin:0;font-size:26px}header p{margin:3px 0 0;color:#71717a}nav{display:flex;gap:16px}a{color:#2563eb;text-decoration:none}.queue{display:grid;gap:18px}.card{display:grid;grid-template-columns:180px 1fr;background:#fff;border:1px solid #e2e4e8;border-radius:14px;overflow:hidden}.image{width:100%;height:100%;min-height:220px;object-fit:cover;background:#eceef1}.content{padding:20px;white-space:pre-wrap}.meta{color:#71717a;font-size:13px;margin-bottom:8px}.title{font-weight:750;margin-bottom:10px}.note{color:#8a641b;font-size:12px;margin-top:14px}.done{margin-top:14px;border:0;border-radius:9px;padding:9px 14px;background:#18181b;color:#fff;cursor:pointer}.done:disabled{opacity:.55;cursor:wait}.queued{padding:28px;color:#71717a}.empty-image{display:grid;place-items:center;background:#eceef1;color:#8b9098}@media(max-width:680px){header{display:block}nav{margin-top:12px}.card{grid-template-columns:1fr}.image{height:260px}}</style>
<header><div><h1>Post 草稿队列</h1><p>每个人设只显示当前一条；标记已发后自动显示下一条。</p></div><nav><a href="__BASE_URL__/personas">人设</a><a href="__BASE_URL__/market">每日研究</a></nav></header>
<main id="result" class="queue"><div class="queued">正在读取队列…</div></main>
<script>
const base='__BASE_URL__',result=document.querySelector('#result');
async function load(){
  try{
    const response=await fetch(base+'/api/daily-posts'),items=await response.json();
    result.innerHTML='';
    if(!items.length){result.textContent='队列已清空。';return}
    items.forEach(x=>{
      const card=document.createElement('article');card.className='card';
      if(x.image_url){const img=document.createElement('img');img.className='image';img.src=base+x.image_url;img.alt=x.persona_name+' 素材候选';card.append(img)}
      else{const empty=document.createElement('div');empty.className='empty-image';empty.textContent='暂无素材';card.append(empty)}
      const content=document.createElement('div');content.className='content';
      const meta=document.createElement('div');meta.className='meta';meta.textContent=`${x.persona_name} · 队列还剩 ${x.remaining} 条 · ${x.context_date}`;content.append(meta);
      const title=document.createElement('div');title.className='title';title.textContent=x.title;content.append(title);
      const body=document.createElement('div');body.textContent=x.body;content.append(body);
      const note=document.createElement('div');note.className='note';note.textContent=x.image_note;content.append(note);
      const done=document.createElement('button');done.className='done';done.textContent='已发，下一条';
      done.onclick=async()=>{done.disabled=true;done.textContent='处理中…';try{const marked=await fetch(base+`/api/post-candidates/${x.id}/published`,{method:'POST'});if(!marked.ok){const error=await marked.json();throw new Error(error.detail||'更新失败')}await load()}catch(error){done.disabled=false;done.textContent='已发，下一条';alert(error.message)}};
      content.append(done);card.append(content);result.append(card);
    });
  }catch(error){result.textContent=error.message}
}
load();
</script>"""
