#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


PIPELINE_REVISION = 3
MAX_EDITORIAL_ATTEMPTS = 3

SOURCE_SCREENSHOTS = {
    "hegong-afterwork": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/07-zhangshifu-ai.png",
    "zhaojie-process": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/02-zhaojie-process.png",
    "linxue-model": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/03-linxue-model.png",
    "xiaocheng-product": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/04-xiaocheng-product.png",
    "ada-builds": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/05-ada-builds.png",
    "susu-multimodal": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/06-susu-multimodal.png",
    "zhangshifu-ai": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/01-hegong-afterwork.png",
    "lianglaoban-ai": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/08-lianglaoban-ai.png",
    "mojie-eval": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/09-mojie-eval.png",
    "wenwen-ai-industry": "generated/ai_preview/20260826-gemini-critic-v2/source_screenshots/10-wenwen-ai-industry.png",
}


TOPICS = [
    {
        "slug": "hegong-afterwork",
        "angle_family": "project_evaluation",
        "structure_id": "project_product_evaluation",
        "title": "OpenAI 把管理员操作放进 ChatGPT 和 Codex 后，工程重点是权限与人工升级",
        "content_revision": 6,
        "grok_suffix": "admin",
        "core_claim": "AI 自动处理请求能进入真实流程，靠的是沿用既有角色和权限，并把例外升级给人，而不是自动化比例本身。",
        "angle": "从工程落地解释权限继承、预设条件和人工升级这三个生产约束，不写组织管理教程。",
        "source_url": "https://openai.com/index/introducing-admin-plugin/",
        "facts": [
            "OpenAI 于 2026-08-25 为 ChatGPT Work 和 Codex 发布 Admin plugin，管理员可在对话中查询使用与额度、管理成员和权限，并处理额度与支出请求。",
            "该插件中的操作遵守现有角色和权限；符合预设条件的申请可自动处理，例外情况可升级给人工。OpenAI 称其 IT 团队已用类似工作流处理约 45% 的支持工单量。",
        ],
    },
    {
        "slug": "zhaojie-process",
        "angle_family": "project_evaluation",
        "structure_id": "project_product_evaluation",
        "title": "Reveal AI 让律师一句话发起案件材料整理，但审批权仍留在人手里",
        "content_revision": 2,
        "core_claim": "买法律 Agent 先别只问它会不会写，要看结果能否回到原始文档、关键输出由谁审核批准。",
        "angle": "把 Reveal 是谁和即将提供什么说清楚，结论落在溯源与审批，不写咨询报告。",
        "source_url": "https://www.revealdata.com/news/reveal-launches-powerful-agentic-ai-suite-automating-ediscovery-from-preservation-to-case-development",
        "facts": [
            "Reveal 是覆盖电子取证和争议解决流程的 AI 软件公司，于 2026-08-24 公布 Reveal AI。",
            "其即将推出的 agentic case building 可从一句自然语言请求出发，整理时间线、提取关键事实、起草证词材料，并把结果指向原始文档；律师全程指导和批准。",
        ],
    },
    {
        "slug": "linxue-model",
        "angle_family": "project_evaluation",
        "structure_id": "project_product_evaluation",
        "title": "Claude 把 chat 和 Cowork 的记忆合成一份",
        "core_claim": "记忆一旦跨入口，错误偏好也会跟着跨入口；这次更新有用的组合不是只共享记忆，而是用户还能查看、纠正和删除它。",
        "angle": "一句解释 Cowork，随后只讲共享与可纠正必须同时存在这一个普通用户判断，不讨论行业壁垒。",
        "source_url": "https://claude.com/blog/claudes-memory-works-everywhere-and-you-decide-whats-in-it",
        "facts": [
            "Anthropic 于 2026-08-25 宣布 Claude chat 与 Cowork 共用同一份记忆；Cowork 是 Claude 在云端继续处理文档、预算、物流等任务的工作区。",
            "用户可以按主题查看、编辑或删除记忆，也可以暂停或重置；Claude 会随着对话更新记忆。",
        ],
    },
    {
        "slug": "xiaocheng-product",
        "angle_family": "project_evaluation",
        "structure_id": "project_product_evaluation",
        "title": "Google Ads 先让 AI Max 的预算建议进 A/B 测试，再允许放大投放",
        "writing_title": "Google Ads 先把预算调整放进 A/B 测试，再决定要不要应用",
        "core_claim": "AI 产品真正减少的不是点击次数，而是让用户原来不敢做的高风险决策先变成可控试验。",
        "angle": "从产品设计看，为什么测试、预测和一键应用连在一起，比再加一个自动优化按钮更重要。",
        "source_url": "https://business.google.com/us/accelerate/announcements/make-ai-max-work-for-your-business-with-new-testing-and-planning-tools/",
        "facts": [
            "Google Ads 于 2026-08-26 公布新的 AI Max 测试和规划工具；跨多个 Search campaign 的预算与 ROI 目标实验将从 2026 年 9 月起逐步推出。",
            "AI Max 实验可保留品牌与地域控制；Performance Planner 可预估调整出价或预算对现有 campaign 的影响，并允许用户应用调整。",
        ],
    },
    {
        "slug": "ada-builds",
        "angle_family": "project_evaluation",
        "structure_id": "open_source_discovery",
        "title": "ai-memory 想让 Claude Code 换到 Codex 后还能接着干",
        "core_claim": "模型可以换，项目记忆不该跟着被锁在某个 Agent 里；把决策和失败方案沉淀成独立资产，比无限塞聊天记录更实用。",
        "angle": "用 GitHub 当前 Star 做 Hook，再解释它解决什么、怎样交接，以及独立开发者为什么会在意。",
        "source_url": "https://github.com/akitaonrails/ai-memory",
        "facts": [
            "ai-memory 是一个为 Claude Code、Codex 等编码 Agent 提供跨会话、跨工具长期项目记忆和交接的 MIT 开源项目。",
            "项目把经过筛选的 Agent 生命周期观察整理成可版本管理的 Markdown 项目 wiki；v1.32.1 于 2026-08-25 发布。",
        ],
    },
    {
        "slug": "susu-multimodal",
        "angle_family": "project_evaluation",
        "structure_id": "project_product_evaluation",
        "title": "Gemini Omni 进了 Google Ads，一份 brief 要直接长成多规格广告视频",
        "content_revision": 6,
        "core_claim": "对广告视频工具来说，把同一品牌要求带进分镜、修改和多比例导出，比只展示一条样片更接近真实交付需求。",
        "angle": "从设计交付链路讲品牌规范、分镜、局部修改和多比例导出怎样接在一起；配图由独立素材字段承载。",
        "source_url": "https://business.google.com/us/accelerate/announcements/omni-in-google-ads/",
        "facts": [
            "Google 于 2026-08-26 宣布 Gemini Omni 进入 Google Ads 的 Asset Studio，并面向全球免费推出多模态视频制作能力。",
            "用户可导入品牌规范和 URL，以提示词或已有静态素材生成分镜与动态场景，并修改场景、配音、节奏和比例，导出 16:9 与 9:16 等素材用于广告活动。",
        ],
    },
    {
        "slug": "zhangshifu-ai",
        "angle_family": "market_cognition",
        "structure_id": "practical_explainer",
        "title": "Kiro 把需求、设计和检查点一起交给 GPT-5.6：复杂任务别只塞一句 Prompt",
        "content_revision": 9,
        "grok_suffix": "kiro",
        "core_claim": "普通人理解 AI 执行复杂任务，先学会写清楚什么叫完成，比继续背提示词技巧更重要。",
        "angle": "借 Kiro 的结构化上下文讲一个普通人能复现的 AI 素养原则，不展开工程框架。",
        "source_url": "https://openai.com/index/gpt-5-6-in-kiro/",
        "facts": [
            "OpenAI 与 AWS 于 2026-08-24 宣布 GPT-5.6 Sol、Terra 和 Luna 可在 Kiro 中使用。",
            "Kiro 会把需求、技术设计、可执行任务和检查点作为结构化上下文交给模型；官方测试称 GPT-5.6 Terra 在 Kiro 的 Terminal-Bench 2.1 任务中成本约降低 82%，该结果属于官方测试口径。",
        ],
    },
    {
        "slug": "lianglaoban-ai",
        "angle_family": "industry_evaluation",
        "structure_id": "industry_structure",
        "title": "OpenAI 第一颗自研推理芯片 Jalapeño 开始交成绩单",
        "content_revision": 10,
        "core_claim": "Jalapeño 的首批能效与延迟数字足以进入推理成本核算，但还不能直接推出总成本、毛利或采购优势。",
        "angle": "先交代 Jalapeño 是什么，用一个能效数据做 Hook，再用条件判断把芯片动作翻译成成本账，不预测同行必然跟进。",
        "source_url": "https://openai.com/index/jalapeno-first-results/",
        "facts": [
            "OpenAI 于 2026-08-25 公布首颗自研推理芯片 Jalapeño 的首批测试结果。",
            "在 GPT-OSS 120B、DeepSeek R1 和 Kimi K2.5 三个公开模型上，OpenAI 称其峰值单位功耗工作量高 1.5 至 1.9 倍，端到端延迟低 1.7 至 3.6 倍。",
            "OpenAI 计划在 2026 年底前开始部署 Jalapeño，同时表示仍会继续使用 NVIDIA 和其他合作伙伴的加速器。",
        ],
    },
    {
        "slug": "mojie-eval",
        "angle_family": "project_evaluation",
        "structure_id": "project_product_evaluation",
        "title": "Claude Mythos 5 的安全产品思路是限制交付物，而不是把模型能力全部放出来",
        "core_claim": "高风险模型对外开放到什么程度，可以先由产品接口约束；限制交付物和保留人工审批，是产品边界，不是模型整体安全的证明。",
        "angle": "只谈限制交付物与人工复核这一个产品设计，不扩展基金、分类器猜测或团队说教。",
        "source_url": "https://claude.com/blog/bringing-claude-mythos-5-to-more-defenders",
        "facts": [
            "Anthropic 于 2026-08-21 宣布 Claude Mythos 5 可用于 Claude Security 扫描，并将通过合作伙伴的安全产品提供防御性结果。",
            "Claude Security 会返回漏洞发现、CWE 分类、置信度、严重性和建议修复；补丁必须由人审核批准后才能实施。",
        ],
    },
    {
        "slug": "wenwen-ai-industry",
        "angle_family": "industry_evaluation",
        "structure_id": "industry_structure",
        "title": "路透社所属的 Thomson Reuters 集团开始自己训练专业大模型",
        "content_revision": 3,
        "core_claim": "Thomson 提供了一条可能路径：自建专业模型不等于先喂入全部自有内容，专家评估和进入具体产品也可能是关键变量。",
        "angle": "保留 4000 万美元、专业数据和 CoCounsel 三个关键信号，只分析 Thomson 这一个案例提供的可能路径，不补行业史。",
        "source_url": "https://www.thomsonreuters.com/en/press-releases/2026/august/thomson-reuters-leverages-its-world-class-data-assets-to-launch-its-own-frontier-model",
        "facts": [
            "路透社所属的 Thomson Reuters 集团于 2026-08-24 发布首个自研大语言模型 Thomson，面向法律、税务等专业工作。",
            "公司称 Thomson 从开放基础模型出发，人才和算力投入 4000 万美元；中后期训练使用 Westlaw、Practical Law、Checkpoint 和 Reuters 的内容，并由数百名专业人士参与训练目标和评估。",
            "公告称 Thomson 将在 CoCounsel Legal 的后续版本中首先使用；目前训练使用的 Thomson Reuters 内容不到 10%。",
        ],
    },
]

MANUAL_REWRITE = {
    "hegong-afterwork": (
        "只用 verified_facts，不虚构实测、公司项目或节省工时。先交代 OpenAI 为 ChatGPT Work 和 Codex 发布 Admin plugin，"
        "随后只讲一个工程判断：真正值得看的是操作沿用既有角色和权限、符合条件才自动处理、例外升级给人工。"
        "第一句不得补‘盲目自动化会导致安全漏洞、返工’等泛化事故，也不要写安全进入业务、严密继承、底层边界等无来源效果。"
        "不要把符合预设条件夸成完全符合，也不要写安全升级。观点请明确写成：这份公告真正值得看的不是 45%，"
        "而是沿用既有角色和权限、符合预设条件才自动处理、例外升级给人工这三个控制点。"
        "45% 只能写成 OpenAI 对其 IT 团队类似工作流的官方口径，不能外推成插件效果、企业普遍效率或自动化越高越好。"
    ),
    "zhaojie-process": (
        "严格保留‘即将推出’时态：写成官方称该流程可从自然语言请求出发，结果会指向原始文档，律师指导并批准。"
        "不得写已经跑通、每个节点签字、每一句都能溯源、工业级工具，也不要制造一键效率与溯源的假二选一。"
        "结尾只留人话判断：买法律 Agent，先看结果能否回到原文、关键输出由谁审核批准。"
    ),
    "linxue-model": (
        "只写已核变化：chat 与 Cowork 现在共用记忆，用户可查看、编辑、删除、暂停或重置。"
        "不得虚构过去总要重新交代、现在自动记得或已经解决任务断档。给一个具体冲突："
        "记忆跨入口后，错误偏好也可能跨入口；所以共享与用户可查看、纠正、删除必须同时存在。"
        "这是产品使用判断，不上升到行业标准，也不用‘不是X而是Y’模板句。"
    ),
    "xiaocheng-product": (
        "删除‘Google Ads 是大多数企业投放搜索广告的核心阵地’这一无依据开场。"
        "第一句用动作 Hook：一条预算建议，先进入 A/B 测试，再决定要不要应用。"
        "第二句写 Google Ads 于 8 月 26 日公布新的测试和规划工具。正文不要出现 AI Max，也不要补产品历史或类别。"
        "提到 Performance Planner 时，只能用已核事实解释为‘可预估调整出价或预算对现有 campaign 的影响，并允许用户应用调整’。"
        "事实段之后明确写‘我的判断是’，再表达：自动化不等于把确认按钮藏掉；先让调整可测试、影响可预测，再交给用户应用，也是 AI 产品降低决策阻力的一种设计。"
        "这是产品观点，不得写成官方已经证明的因果，也不扩大成行业必然。"
        "禁止补写过去如何操作、手动配置、资金风险等历史背景。禁止使用我、我们、看到、过去很危险、极大消除等亲历或无依据因果，也不要建议读者实际投放。"
    ),
    "ada-builds": (
        "删除‘我们’及任何第一人称经历；保留当天 GitHub Star Hook，但删许可证和版本号。"
        "不得断言现有 Agent 换工具就一定失忆、用户通常要重讲架构；只能说这是 ai-memory 试图解决的问题。"
        "只写它把经过筛选的 Agent 生命周期观察整理成可版本管理的 Markdown wiki，并据此给 Claude Code、Codex 交接。"
        "结尾落在作者判断：项目记忆应该属于 repo，不属于某个模型。"
    ),
    "susu-multimodal": (
        "开头用麦冬的明确判断：‘我更关心 AI 视频工具允许改什么，而不是第一眼的样片有多炫。’这只是观点，不是亲历。"
        "紧接着定位：Google Ads 的 Asset Studio 是承载这次多模态视频制作能力的产品入口，Gemini Omni 进入后新增下列能力。"
        "只写已核能力：导入品牌规范和 URL，以提示词或静态素材生成分镜与动态场景，"
        "可修改场景、配音、节奏和比例，并导出 16:9、9:16 等素材。"
        "论证只按‘已核入口 → 可修改的对象 → 可导出的比例 → 一句创作判断’推进，不写修改前后对比。"
        "不得补写以前要手动重做、不用反复重做、修改自动同步、同步调整、稳稳切出、一次性交付、保持品牌一致、性能保证、行业竞争已经转向、"
        "告别跑图碰运气、进入广告投放池，也不和垂类影视模型横评。"
        "不得补‘修改时品牌调性会失控、第一版最难、同一个地方完成’等经验断言。"
        "明确标成创作判断：这次值得讲的是官方同时给出了品牌规范、生成、局部修改和多比例导出这些环节，"
        "因此比只展示单条样片更接近真实交付需求；不要断言成品已经可交付。正文不贴来源 URL，来源由页面单独展示。"
    ),
    "zhangshifu-ai": (
        "这是一条普通人的 AI 素养解释，不是编程教程。第一句必须写：‘先写清需求、任务和检查点，再把复杂任务交给 AI。’"
        "第二句必须原样定位 Kiro：‘Kiro 会把需求、技术设计、可执行任务和检查点作为结构化上下文交给模型。’"
        "不得把 Kiro 称为开发平台、工具、产品或其他 verified_facts 没提供的类别。"
        "随后交代 OpenAI 与 AWS 宣布 GPT-5.6 可用于 Kiro。"
        "禁止使用‘很多人以为、大家以为、写了上千字仍跑偏、推倒重来、模型盲目猜测、逻辑藏在’等稻草人或无来源使用史。"
        "可以用装修验收清单作一个比喻，但比喻和正文都不得出现‘我、我们、本人’，不得虚构用户实测、团队流程或节省工时。"
        "官方约 82% 成本若出现，只能写成 GPT-5.6 Terra 在 Kiro 的 Terminal-Bench 2.1 官方测试口径；"
        "不得写成结构化上下文因此导致了这项结果，也可以完全不写。"
        "结尾只给一个能复现的原则：即使不使用 Kiro，也可以借鉴这个拆法——交给 AI 复杂任务前，"
        "先写清什么叫完成、分几步、在哪里检查。禁止写‘普通人不用 Kiro、完全通用’等全称判断。"
    ),
    "lianglaoban-ai": (
        "保留官方测试数据、2026 年底前开始部署以及继续使用 NVIDIA 等合作伙伴。"
        "第一句直接使用‘峰值单位功耗工作量高 1.5 至 1.9 倍、端到端延迟低 1.7 至 3.6 倍’这两个已核数字做 Hook。"
        "第二句只交代它们来自 OpenAI 首颗自研推理芯片 Jalapeño 的首批官方测试；"
        "禁止写‘逼着所有人重新算账、漂亮首秀’等群体情绪或营销评价。"
        "指标必须原词保留为‘峰值单位功耗工作量’和‘端到端延迟’；禁止改写成单次推理电耗、电费账单、单字生成时间或吞吐。"
        "第一段只允许列出事实包中的指标；第二段只写计划部署时间和继续使用合作伙伴加速器；第三段只写规定的结论。"
        "禁止写 Jalapeño 的研发目的、"
        "‘旨在优化能效’或‘测试直接决定能否进入核算’，也不得自行列晶圆良率、折旧、运营摊销、"
        "采购价、服务器利用率等事实包没有的项目。明确区分芯片测试指标和整笔商业账：可以说这些指标值得被放进成本核算，但不得声称它们已经降低能耗、"
        "减少理论资源占用、改善硬件周转或证明资本效率。事实包没有芯片采购价、利用率、"
        "部署规模或实际售价，所以不能推出 API 履约成本下降、毛利扩大或采购议价优势。"
        "禁止补‘直接关联硬件效率与响应速度’、部署后的条件场景、理论耗时、成本传导、付款方、开发者或订阅用户。"
        "结论只允许一层：这两项官方测试指标值得纳入成本测算，但仅凭它们不能推出 API 履约成本、毛利或采购议价优势；"
        "它们是技术成绩，不是利润成绩单。禁止使用我、我们、本人，不要写电费账、资本效率或任何经营类比。"
    ),
    "mojie-eval": (
        "压成两段，只讲接口交付物和证据边界。删除高网络能力、未全面开放等全部背景。"
        "第一句用结论 Hook：这份公告能证明的只有两件事——Claude Security 限定了返回的交付内容，补丁仍要人批准。"
        "第二句定位：Anthropic 宣布 Claude Mythos 5 可用于 Claude Security 扫描，并通过合作伙伴安全产品提供防御性结果。"
        "写‘官方公布的交付内容包括’，补丁需人工审核批准。白盒的明确判断是：这份公告最多证明产品限制了交付物并保留人工审批，"
        "证据不能外推成模型整体安全。禁止把 Mythos 5 称为高风险模型，禁止补写官方安全意图、开放能力、严格限制或任何 Grok 背景。"
        "禁止出现‘官方声称安全、保障防御安全、安全可控’等 verified_facts 没有的句子。"
        "禁止使用我、我们、观察到、亲测等第一人称经历。"
        "避免‘但这不代表’等模板转折。"
    ),
    "wenwen-ai-industry": (
        "保留 4000 万美元、专业数据、专家参与、CoCounsel 后续版本和不到 10% 内容。"
        "把‘目前训练使用的集团内容不到 10%’作为 Hook，但不要改写成已证明的全行业反常识。"
        "禁止第一人称，禁止补行业过去如何、普遍认为、自建往往、数据商贬值、竞争已经转移或专业责任等未核背景。"
        "只把 Thomson 写成一个案例：从开放基础模型出发，中后期加入专业内容和专家评估，并先进入 CoCounsel Legal 后续版本。"
        "结尾只能说它提供了一条可能路径：自建专业模型不等于先喂入全部自有内容，专家评估和落进具体产品也可能是关键变量。"
    ),
}

def persona_for(slug):
    return {
        "slug": slug,
        "name": app.PERSONA_PUBLIC_PROFILE[slug]["display_name"],
        "card": app.editorial_persona_card({"draft": app.PERSONA_OVERRIDES[slug]}),
        "continuity": {},
        "why_this_persona": next(item["angle"] for item in TOPICS if item["slug"] == slug),
    }


def verified_facts(item):
    facts = [
        {
            "id": f"official:{item['slug']}:{index}",
            "text": text,
            "source_refs": [item["source_url"]],
            "status": "official_primary",
        }
        for index, text in enumerate(item["facts"], start=1)
    ]
    return {"schema": "facts_used_ids", "facts": facts, "requires_fact_ids": True}


def topic_for(item):
    writing_title = item.get("writing_title", item["title"])
    topic = {
        "claim_key": f"ai-preview:{item['slug']}:{writing_title}",
        "subject": writing_title.split(" ")[0],
        "title": writing_title,
        "core_claim": item["core_claim"],
        "content_type": "editorial",
        "angle_family": item["angle_family"],
        "structure_id": item["structure_id"],
        "topic_domain": "ai",
        "material_delta": item["core_claim"],
        "audience_value": item["angle"],
        "why_now": "2026-08-26 AI 热点预览",
        "source_topic_keys": [item["source_url"]],
        "source_refs": [item["source_url"]],
    }
    return {**topic, "style_recipe": app.editorial_content_structure(topic)}


def daily_for(item):
    return {
        "context_date": "2026-08-26",
        "market_state": "本轮只从近期 AI 产品、开源项目和公司动作中选择十个互不重复的题目。",
        "event_clusters": item["title"],
        "debates": item["core_claim"],
        "sources": [item["source_url"]],
    }


def grok_path_for(output_dir, item):
    suffix = f"-{item['grok_suffix']}" if item.get("grok_suffix") else ""
    return output_dir / f"grok-{item['slug']}{suffix}.json"


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def draft_failures(draft, writer_context, facts):
    failures = app.deterministic_editorial_style_failures(draft["text"], writer_context, facts)
    if len(draft["text"]) < 220:
        failures.append("正文少于 220 个字符，信息量不足")
    if len(draft["text"]) > 560:
        failures.append("正文超过 560 个字符，主线不够收敛")
    return failures


def is_llm_critic(critic):
    return (
        isinstance(critic, dict)
        and critic.get("mode") == "llm_critic"
        and bool(critic.get("model"))
    )


def obsolete_optional_cta_reject(critic, style, failures):
    if not isinstance(critic, dict):
        return False
    cta = str(style.get("cta", ""))
    reasons = critic.get("reasons", [])
    optional = any(word in cta for word in ("无 CTA", "不加 CTA", "可以无", "只有确实适合"))
    return (
        optional and not failures and critic.get("verdict") == "REJECT"
        and bool(reasons) and all("CTA" in str(reason) for reason in reasons)
    )


async def retry_provider(call, attempts=4):
    for attempt in range(attempts):
        try:
            return await call()
        except (app.httpx.TimeoutException, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            await asyncio.sleep((5, 10, 20)[attempt])
        except app.httpx.HTTPStatusError as error:
            status = error.response.status_code
            if attempt == attempts - 1 or (status != 429 and status < 500):
                raise
            delays = (10, 20, 45) if status == 429 else (5, 15, 30)
            await asyncio.sleep(delays[attempt])


async def generate_one(item, research_semaphore, generation_semaphore, output_dir):
    checkpoint_path = output_dir / f"{item['slug']}.json"
    checkpoint = {}
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    topic = topic_for(item)
    style = topic["style_recipe"]
    compatible = (
        checkpoint.get("title") == item["title"]
        and checkpoint.get("pipeline_revision") == PIPELINE_REVISION
        and checkpoint.get("style_revision") == style.get("revision")
        and checkpoint.get("style_id") == style.get("id")
        and (
            not item.get("content_revision")
            or checkpoint.get("content_revision") == item["content_revision"]
        )
    )
    if not compatible:
        checkpoint = {
            key: checkpoint[key]
            for key in ("grok", "verified_facts")
            if checkpoint.get("title") == item["title"] and key in checkpoint
        }
    checkpoint.update({
        "slug": item["slug"],
        "title": item["title"],
        "pipeline_revision": PIPELINE_REVISION,
        "style_revision": style.get("revision"),
        "style_id": style.get("id"),
        "content_revision": item.get("content_revision", 1),
    })
    result = checkpoint.get("result")
    if compatible and isinstance(result, dict) and is_llm_critic(result.get("critic")):
        print(f"resume {item['slug']} chars={len(result['draft']['text'])}", flush=True)
        return result

    facts = checkpoint.get("verified_facts") or verified_facts(item)
    grok = checkpoint.get("grok")
    if not isinstance(grok, dict):
        grok_path = grok_path_for(output_dir, item)
        if grok_path.exists():
            grok = json.loads(grok_path.read_text(encoding="utf-8"))
        else:
            async with research_semaphore:
                grok = await retry_provider(
                    lambda: app.enrich_persona_editorial_context(
                        topic, facts, daily_for(item)
                    )
                )
            write_json(grok_path, grok)
        grok = {
            **grok,
            "citations": list(dict.fromkeys([
                *grok.get("citations", []), item["source_url"],
            ])),
        }
        checkpoint["grok"] = grok
        write_json(checkpoint_path, checkpoint)
    if not isinstance(checkpoint.get("verified_facts"), dict):
        facts = await app.enrich_verified_facts_with_github_traction(topic, facts, grok)
        checkpoint["verified_facts"] = facts
        write_json(checkpoint_path, checkpoint)

    writer_context = {
        "source_kind": "market", "source_id": "", "source_item": None,
        "first_person_allowed": False, "available_assets": [],
    }
    persona = persona_for(item["slug"])
    async with generation_semaphore:
        draft = checkpoint.get("draft")
        failures = checkpoint.get("failures")
        if not isinstance(draft, dict) or not isinstance(failures, list):
            draft = await retry_provider(
                lambda: app.write_persona_editorial_gemini(
                    persona, topic, facts, grok, writer_context,
                    MANUAL_REWRITE.get(item["slug"], ""),
                )
            )
            failures = draft_failures(draft, writer_context, facts)
            checkpoint.update({"draft": draft, "failures": failures})
            write_json(checkpoint_path, checkpoint)

        attempt = 1
        critic = checkpoint.get("final_critic") or checkpoint.get("critic")
        if obsolete_optional_cta_reject(critic, style, failures):
            checkpoint.pop("critic", None)
            checkpoint.pop("final_critic", None)
            critic = None
            write_json(checkpoint_path, checkpoint)
        while True:
            if not is_llm_critic(critic):
                critic = await retry_provider(
                    lambda: app.critique_persona_editorial_draft(
                        persona, topic, facts, grok, writer_context, draft, failures
                    )
                )
                checkpoint.update({"critic": critic, "final_critic": critic})
                write_json(checkpoint_path, checkpoint)
            if critic["verdict"] == "PASS" and not failures:
                break
            if attempt >= MAX_EDITORIAL_ATTEMPTS:
                reasons = [*critic.get("reasons", []), *critic.get("unsupported_claims", []), *failures]
                raise RuntimeError(f"{item['slug']} critic rejected: {'；'.join(reasons)}")
            rewrite = "；".join([
                critic.get("rewrite_instruction", ""), *critic.get("reasons", []), *failures,
                "正文控制在 220 至 560 个中文字符，只讲一个主题。",
                "严格使用本条 style_recipe 的 Hook、Context、论证和收尾，不要退回统一说明文结构。",
                "删掉产品百科、参数清单、泛化风险提示、硬塞的行动号召和行业必然预测。",
                MANUAL_REWRITE.get(item["slug"], ""),
            ]).strip("；")
            draft = await retry_provider(
                lambda: app.write_persona_editorial_gemini(
                    persona, topic, facts, grok, writer_context, rewrite
                )
            )
            failures = draft_failures(draft, writer_context, facts)
            attempt += 1
            checkpoint.update({"draft": draft, "failures": failures})
            checkpoint.pop("critic", None)
            checkpoint.pop("final_critic", None)
            write_json(checkpoint_path, checkpoint)
            critic = None
        result = {
            **item,
            "topic": topic,
            "verified_facts": facts,
            "grok": grok,
            "draft": draft,
            "critic": critic,
            "attempts": attempt,
            "pipeline_revision": PIPELINE_REVISION,
        }
        checkpoint["result"] = result
        write_json(checkpoint_path, checkpoint)
        print(f"done {item['slug']} chars={len(draft['text'])} attempt={attempt}", flush=True)
        return result


def write_outputs(output_dir, results, errors):
    order = {item["slug"]: index for index, item in enumerate(TOPICS)}
    results.sort(key=lambda item: order[item["slug"]])
    for result in results:
        screenshot = ROOT / SOURCE_SCREENSHOTS.get(result["slug"], "")
        result["asset_path"] = str(screenshot.resolve()) if screenshot.is_file() else ""
    payload = {
        "generated_at": datetime.now(app.TZ).isoformat(),
        "status": "complete" if len(results) == len(TOPICS) and not errors else "partial",
        "posts": results,
        "errors": errors,
    }
    json_path = output_dir / "posts.json"
    write_json(json_path, payload)
    lines = ["# AI 人设预览稿（Grok Context → 人设文风结构 → Gemini Writer → Gemini Critic）", ""]
    for index, result in enumerate(results, start=1):
        lines.extend([
            f"## {index}. {app.PERSONA_PUBLIC_PROFILE[result['slug']]['display_name']}",
            "", f"题目：{result['title']}", "", result["draft"]["text"], "",
            f"来源：{result['source_url']}", "",
        ])
        if result.get("asset_path"):
            lines.extend([f"![信源截图]({result['asset_path']})", ""])
    if errors:
        lines.extend(["## 失败", "", *[f"- {error}" for error in errors], ""])
    markdown_path = output_dir / "posts.md"
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return payload, markdown_path, json_path


def bounded_concurrency(name, default):
    try:
        return min(5, max(1, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


async def main(output_dir):
    providers = ["GEMINI"]
    if not all(grok_path_for(output_dir, item).exists() for item in TOPICS):
        providers.append("GROK")
    for provider in providers:
        app.editorial_provider_config(provider)
    errors = []
    research_semaphore = asyncio.Semaphore(
        bounded_concurrency("XOPS_EDITORIAL_RESEARCH_CONCURRENCY", 4)
    )
    generation_semaphore = asyncio.Semaphore(
        bounded_concurrency("XOPS_EDITORIAL_GENERATION_CONCURRENCY", 5)
    )
    tasks = [
        asyncio.create_task(generate_one(
            item, research_semaphore, generation_semaphore, output_dir
        ))
        for item in TOPICS
    ]
    results = []
    for task in asyncio.as_completed(tasks):
        try:
            results.append(await task)
        except Exception as error:
            errors.append(str(error))
            print(f"failed {error}", flush=True)
        write_outputs(output_dir, results, errors)
    payload, markdown_path, json_path = write_outputs(output_dir, results, errors)
    print(json.dumps({
        "status": payload["status"], "count": len(results),
        "markdown": str(markdown_path), "json": str(json_path), "errors": errors,
    }, ensure_ascii=False), flush=True)
    if errors or len(results) != len(TOPICS):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else ROOT / "generated" / "ai_preview" / datetime.now(app.TZ).strftime("%Y%m%d-%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(output_dir))
