# 人设内容候选

Railway 上运行的人设内容服务。它将母帖池整理成可审核的公共市场 Context，并将每个人设已批准的 Editorial Context 合入编辑输入；常驻编辑流水线只为 `WRITE` 结果生成待审核候选稿。

## Production access

Open `https://x-account-operator-api-production.up.railway.app/`.

不连接 X 账号，不保存 X OAuth，不排期，也不自动发布。

## 编辑 Context 与流水线

公共市场 Context 与每个人设的 Editorial Context 共同决定候选：

1. 母池抓取、交叉验证和人工审核形成正式公共市场 Context；未审核的自动草稿不能进入写作。
2. 每个人设都维护一份 Editorial Context 草稿与一份已批准版。草稿可随时修改，但只有已批准版会进入输入 fingerprint。
3. 选中的公共题先按来源合并回母题。Grok 对整批母题做一次 X/Web 实时研究，Gemini 再展开 0–N 个互不替代的机会、行业评价、项目评价、认知、交易哲学或社区角度；这些是可选镜头，不是配额。
4. 角度必须有具体对象、具体冲突、非显而易见的增量和读者价值。常识、同义改写、无结论内容直接淘汰；公共题不足时由可追溯的方法论卡补足每日待审稿下限。
5. 合格公共角度与该人设状态为 `ready` 的私有题合流。Resolver 只能返回 `WRITE(ThesisContract)`、`HOLD(reason)` 或 `IGNORE(reason)`；Topic 和 Angle 都不能直接充当 Thesis。
6. `ThesisContract` 冻结唯一主张、对象、范围、人设 lens、已批准证据、读者收益、信息增量和可证伪条件。确定性硬校验通过后，系统才按主张语义做同人设历史碰撞与跨人设碰撞；同一事件可保留不同 Thesis，换口吻的同一 Thesis 只保留更匹配的人设。
7. Structure 只从题材配置选择必需语义槽、允许的推理形状和 CTA 策略，不得修改 Thesis。每个批准的 Thesis 再由 Grok 补齐非权威背景，Gemini 只能把批准事实写成事实。成稿先做 Thesis Adherence 分类，再做现有主编审核；内容质量问题只允许定向重写一次，仍不通过则 `HOLD`。提供方失败走独立重试，不消耗内容修复次数。
8. 同日只要正式输入 fingerprint 发生实质变化（包括公共 Context 更新或人设 Editorial Context 重批），就可增量评估；每日待审稿未满时可改用下一张补位卡。人设 Context 的新批准版会先撤销该人当天旧候选。

系统以每个人设每天 3 条为目标（`XOPS_DAILY_POST_TARGET_PER_PERSONA`，默认 `3`），但数量不能降低 Thesis 门槛：热点、已批准历史角度和私有成熟表达优先；不足时才让可追溯的方法论、财富观与交易哲学卡走同一个 Resolver。没有合格 Thesis 时允许少于目标，不生成占位稿。自动流程不发布、不排期，也不操作 X 账号。候选稿只使用正式批准的公共 Context 与该人设正式批准的 Editorial Context。Grok 搜索只作背景；正式事实只能来自批准输入。队列只接收带 `persona_editorial_grok_gemini:<evaluation-id>` 来源和审计记录的候选，历史候选以 `legacy_candidate` 标记，新候选以 `thesis_contract_v1_candidate` 标记。

### 每人 Editorial Context

每个人设有如下字段，均以 JSON 数组保存：

- `life_context`：可核验的生活事实、角度和证据。只有此类条目明确标注 `first_person_allowed: true` 时，才可支持具体第一人称亲历。
- `thought_threads`：尚在形成或已经成熟的个人思考；
- `expression_debt`：成熟但尚未表达的主张，不是欠稿数或发布配额；
- `real_feedback`：真实读者或同行反馈；
- `available_asset_ids`：该人设可使用的已选图片素材。

`thought_threads`、`expression_debt`、`real_feedback` 和图片素材都不能证明个人亲历。私有题须处于 `ready` 且有可表达的核心主张，才可与公共热点一起进入评估。

素材必须属于当前人设。若 Editorial Context 只批准一张素材，它可作为候选稿默认素材；若批准多张，只有私有条目的 `asset_ids` 明确指定时才会使用。

### Editorial Context API

- `GET /api/personas/{persona_id}/editorial-context`：读取 `status`、`approval_revision`、`draft`、`approved` 与派生的 `expressed_source_ids`；
- `PUT /api/personas/{persona_id}/editorial-context`：保存上述五个字段的草稿，不会进入引擎；
- `POST /api/personas/{persona_id}/editorial-context/approve`：批准当前草稿。不同内容会生成新批准版本，撤销该人当天旧候选。

## 选题与人设判断

`configs/topic_selection_policy.json` is the permanent selection authority.

The daily flow is: mother-pool heat → facts and opinions → approved mother topics → Grok topic context → Gemini angle expansion and quality gate → approved persona private topics → persona/topic relevance → Persona Thesis resolution → semantic thesis dedup → structure selection → Grok draft context → Gemini draft → thesis adherence validation → existing editorial review → human review. Content structures live in `configs/editorial_content_structures.json`; they decide required semantic slots, allowed reasoning shapes, and CTA policy, never the claim itself.

- Heat only decides what to research. It does not make a topic publishable.
- A refreshed number is not a new topic unless it changes the conclusion, scale, mechanism, participation condition, or invalidation condition.
- `topic_claim_history` stores covered core claims. 私有原始 Context 只进入所属人设的模型输入；归一化核心主张仍参与全团队去重，避免换个人设重复同一结论。
- 原始问题、未批准的公共 Context 与未批准的人设 Context 都只保留作审计，不能直接触发写作。
- 每个人设先判断自己是否会注意到、是否有知识边界内的独特主张、与已有内容相比是否有新增价值；不能满足则 `HOLD` 或 `IGNORE`。
- 选题并非按人设顺序分配，`WRITE` 也不是发布指令。
- `/market` shows both selected topics and rejected topics with reasons.
- `POST /api/context/daily-runs/{date}/retry-angle-expansion` immediately releases a failed angle stage after API credit, rate-limit, or provider recovery; otherwise three short retries are followed by a 30-minute cooldown retry.

## Daily mother-pool scheduler

The scheduler runs inside the service process and never stores credentials in SQLite or generated artifacts. On Linux/systemd, set `TWITTER241_RAPIDAPI_KEY`, `XOPS_LLM_API_KEY`, `XOPS_GROK_API_KEY` and `XOPS_GEMINI_API_KEY` in `/etc/x-account-operator.env`; formal candidate generation stays retryable rather than falling back when either dedicated editor provider is absent.

```text
XOPS_DAILY_CONTEXT_ENABLED=true
XOPS_DAILY_CONTEXT_RUN_TIME=08:15
XOPS_DAILY_CONTEXT_HOURS=30
XOPS_DAILY_CONTEXT_WORKERS=8
XOPS_DAILY_CONTEXT_RESUME_HOURS=20
XOPS_MOTHER_POOL_ACCOUNTS=/path/to/content_source_accounts.json
XOPS_DAILY_POST_ENABLED=true
XOPS_DAILY_POST_PERSONAS=acheng,ridehail-driver-zhao,college-student-linjia,atuo,axu,nanqiao,qiliang,aye,xiaoman,maili,hegong-afterwork,zhaojie-process,linxue-model,xiaocheng-product,ada-builds,susu-multimodal,zhangshifu-ai,lianglaoban-ai,mojie-eval,wenwen-ai-industry
XOPS_DAILY_POST_TARGET_PER_PERSONA=3
XOPS_DAILY_SUPPLEMENT_COOLDOWN_DAYS=7
XOPS_GROK_API_KEY=...
XOPS_GROK_BASE_URL=https://www.micuapi.ai/v1
XOPS_GROK_MODEL=grok-4.6
XOPS_EDITORIAL_RESEARCH_CONCURRENCY=4
XOPS_EDITORIAL_EVALUATION_CONCURRENCY=5
XOPS_GEMINI_API_KEY=...
XOPS_GEMINI_BASE_URL=https://www.micuapi.ai/v1
XOPS_GEMINI_MODEL=gemini-3.1-pro-preview-low
XOPS_EDITORIAL_GENERATION_CONCURRENCY=5
XOPS_OPERATOR_TOKEN=...  # optional: protect all /api write requests
```

macOS 本地可把多把 Gemini key 放进 Keychain service `codex.xops.gemini.pool`，account 使用
`slot-1` 到 `slot-5`，并设置 `XOPS_GEMINI_KEYCHAIN_ACCOUNTS=slot-1,slot-2,slot-3,slot-4,slot-5`。
同一服务进程内，每把 key 同时只处理一个请求；没有 Keychain pool 时继续使用单一 `XOPS_GEMINI_API_KEY`。
如果运行环境同时注入了单 key，必须显式设置 `XOPS_GEMINI_KEYCHAIN_ACCOUNTS` 才会启用本地 Keychain pool。

Railway 使用 `XOPS_GEMINI_API_KEY_1` 到 `XOPS_GEMINI_API_KEY_5` 组成同样的池，并设置
`XOPS_EDITORIAL_GENERATION_CONCURRENCY=5`。只要任一编号槽位存在，就优先使用编号池；全部缺失时才回退
`XOPS_GEMINI_API_KEY`。`/health` 只返回 `gemini_pool_configured_slots` 数量，不返回任何密钥信息。
`XOPS_EDITORIAL_EVALUATION_CONCURRENCY=5` 会让人设编辑判断最多同时处理 5 个人设，避免逐个串行等待。

- `XOPS_DAILY_CONTEXT_ENABLED=false` pauses only the automatic run; manual runs in `/market` remain available.
- `XOPS_DAILY_CONTEXT_RUN_TIME` is interpreted in `XOPS_TIMEZONE` (default: `Asia/Shanghai`).
- `XOPS_MOTHER_POOL_ACCOUNTS` is the account-list JSON used by the collector. If omitted, the service uses the verified default source configuration.
- `XOPS_DAILY_POST_PERSONAS` defines the personas that enter the daily queue. `XOPS_DAILY_POST_TARGET_PER_PERSONA` defaults to `3`; hot topics still win first, and source-backed methodology cards plus approved evergreen judgments fill only the remaining slots. `XOPS_DAILY_SUPPLEMENT_COOLDOWN_DAYS` defaults to `7`, so the same persona will not recycle the same successful supplement claim too soon.
- `POST /api/daily-posts/generate` is the daily trigger for an external scheduler. It starts a single background generation run for today's approved Context and returns immediately; poll `GET /api/daily-posts` for the resulting `needs_review` drafts. It never collects, approves, publishes, or schedules X posts.
- The built-in daily scheduler automatically approves a completed machine-generated Context for draft generation, then tries the configured personas up to `XOPS_DAILY_POST_TARGET_PER_PERSONA`. A failed Thesis gate is never bypassed to fill quantity. Drafts still stop at `needs_review`; publishing remains manual.
- 人设按 `content.topic_domain` 取公共题：现有公共题缺省为 `crypto`，不会被 10 个 `ai` 人设硬写；AI 人设仍可先消费自己已批准的长期观点，待独立 AI 热点源接入后再消费 AI 公共题。
- If `XOPS_OPERATOR_TOKEN` is configured, every non-GET `/api` request must send it as `X-Ops-Token`. The three built-in operator pages prompt once after a 401 and keep it only in that browser tab's session storage. `/health` exposes `operator_auth_enabled`.
- In `/market`, a reviewer may explicitly confirm only a fact card's representative source reference, with a different first-party verification URL and a short verification note. X/Twitter links, the same source-post URL, and empty evidence are rejected. `two_source_candidate` and `corroborated_candidate` never become verified facts automatically, and a promoted fact is usable only by a selected topic that cites that exact reviewed reference.

结果入口是 `/`，当天真实推文 API 是 `/api/daily-posts`。接口在本轮 Thesis 与生成任务结算后展示已通过的完整推文；实际数量可以少于目标，但不会展示事实池、观点池、选题或生成中的半成品。人设评估和 Thesis 状态只保存在 SQLite 内部，供幂等、恢复和审计使用。数据保存在 Railway `/data` 持久卷。
