# 人设内容候选

Railway 上运行的人设内容服务。它将母帖池整理成可审核的公共市场 Context，并将每个人设已批准的 Editorial Context 合入编辑输入；常驻编辑流水线只为 `WRITE` 结果生成待审核候选稿。

## Production access

Open `https://x-account-operator-api-production.up.railway.app/`.

不连接 X 账号，不保存 X OAuth，不排期，也不自动发布。

## 编辑 Context 与流水线

公共市场 Context 与每个人设的 Editorial Context 共同决定候选：

1. 母池抓取、交叉验证和人工审核形成正式公共市场 Context；未审核的自动草稿不能进入写作。
2. 每个人设都维护一份 Editorial Context 草稿与一份已批准版。草稿可随时修改，但只有已批准版会进入输入 fingerprint。
3. 公共热点与该人设状态为 `ready` 的私有题合流，再由该人设独立给出 `WRITE`、`HOLD` 或 `IGNORE`。
4. 系统仲裁跨人设的相同核心主张：同一事件可有不同结论；只有换了口吻的同一结论只保留最自然的人设。
5. 只有 `WRITE` 生成候选稿，随后由人工审核。`HOLD` 仅保留在内部状态，绝不被写成“继续观察”等正文。
6. 同日只要正式输入 fingerprint 发生实质变化（包括公共 Context 更新或人设 Editorial Context 重批），就可增量评估；无变化不补稿。人设 Context 的新批准版会先撤销该人当天旧候选。

没有固定 10 条、3–5 条或轮转栏目配额；当天可以是 0 条。自动流程不发布、不排期，也不操作 X 账号。候选稿只使用正式批准的公共 Context 与该人设正式批准的 Editorial Context。

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

The daily flow is: mother-pool heat → facts and opinions → public candidate questions → formal public Context + approved persona private topics → persona evaluation → claim arbitration → candidate writing → human review.

- Heat only decides what to research. It does not make a topic publishable.
- A refreshed number is not a new topic unless it changes the conclusion, scale, mechanism, participation condition, or invalidation condition.
- `topic_claim_history` stores covered core claims. 私有原始 Context 只进入所属人设的模型输入；归一化核心主张仍参与全团队去重，避免换个人设重复同一结论。
- 原始问题、未批准的公共 Context 与未批准的人设 Context 都只保留作审计，不能直接触发写作。
- 每个人设先判断自己是否会注意到、是否有知识边界内的独特主张、与已有内容相比是否有新增价值；不能满足则 `HOLD` 或 `IGNORE`。
- 选题并非按人设顺序分配，`WRITE` 也不是发布指令。
- `/market` shows both selected topics and rejected topics with reasons.

## Daily mother-pool scheduler

The scheduler runs inside the service process and never stores credentials in SQLite or generated artifacts. On macOS it can read the existing Keychain entries. On Linux/systemd, set both `TWITTER241_RAPIDAPI_KEY` and `XOPS_LLM_API_KEY` in `/etc/x-account-operator.env`; the installer leaves the scheduler disabled until both are present.

```text
XOPS_DAILY_CONTEXT_ENABLED=true
XOPS_DAILY_CONTEXT_RUN_TIME=08:15
XOPS_DAILY_CONTEXT_HOURS=30
XOPS_DAILY_CONTEXT_WORKERS=8
XOPS_DAILY_CONTEXT_RESUME_HOURS=20
XOPS_MOTHER_POOL_ACCOUNTS=/path/to/content_source_accounts.json
XOPS_DAILY_POST_ENABLED=true
XOPS_DAILY_POST_PERSONAS=acheng,ridehail-driver-zhao,college-student-linjia,atuo,axu,nanqiao,qiliang,aye,xiaoman,maili
```

- `XOPS_DAILY_CONTEXT_ENABLED=false` pauses only the automatic run; manual runs in `/market` remain available.
- `XOPS_DAILY_CONTEXT_RUN_TIME` is interpreted in `XOPS_TIMEZONE` (default: `Asia/Shanghai`).
- `XOPS_MOTHER_POOL_ACCOUNTS` is the account-list JSON used by the collector. If omitted, the service uses the verified default source configuration.
- `XOPS_DAILY_POST_PERSONAS` defines the personas that are evaluated, not a promised draft count. The pipeline may legitimately return no `WRITE` results.

结果入口是 `/`，当天真实候选稿 API 是 `/api/daily-posts`。人设评估状态保存在 SQLite，供流水线幂等、恢复和审计使用。数据保存在 Railway `/data` 持久卷。
