# 人设内容候选

Railway 上运行的人设内容服务。它先把母帖池整理成可审核的市场 Context；只有 Context 被人工正式批准后，常驻编辑流水线才会让每个人设独立判断此刻是否值得表达，并只为 `WRITE` 结果生成待审核候选稿。

## Production access

Open `https://x-account-operator-api-production.up.railway.app/`.

不连接 X 账号，不保存 X OAuth，不排期，也不自动发布。

## 编辑流水线

在 `/market` 完成 Context 审核后，流程如下：

1. The daily scheduler runs at 08:15 Asia/Shanghai and gathers the configured X mother pool.
2. It cross-validates the collected cards and writes an **automatic draft** only.
3. On `/market`, load that draft, edit it if needed, then click “确认保存”. The page first saves the review back to the run and only then promotes it to the formal daily market context.
4. 每个人设基于正式 Context、自己的人设卡、既有主张与未解问题，独立给出 `WRITE`、`HOLD` 或 `IGNORE`。
5. 系统仲裁跨人设的相同核心主张：同一事件可有不同结论；只有换了口吻的同一结论只保留最自然的人设。
6. 只有 `WRITE` 生成候选稿，随后由人工审核。`HOLD` 仅保留在内部状态，绝不被写成“继续观察”等正文。
7. 同日有实质市场变化时，新的正式 Context 输入 fingerprint 会触发一次增量评估；没有变化不会为了刷新而补稿。
8. A failed run can be retried from the same page. “立即抓取” is for an on-demand run. The original manual path remains available: paste a feed, synthesize an unsaved preview, then save the daily market map yourself.

没有固定 10 条、3–5 条或轮转栏目配额；当天可以是 0 条。自动流程不发布、不排期，也不操作 X 账号。候选稿只使用正式保存的市场 Context，绝不使用未审核的调度草稿。

## 选题与人设判断

`configs/topic_selection_policy.json` is the permanent selection authority.

The daily flow is: mother-pool heat → facts and opinions → candidate questions → common-knowledge and history screening → formal Context → persona evaluation → claim arbitration → candidate writing → human review.

- Heat only decides what to research. It does not make a topic publishable.
- A refreshed number is not a new topic unless it changes the conclusion, scale, mechanism, participation condition, or invalidation condition.
- `topic_claim_history` stores covered core claims across all personas. Semantic duplicates without a material delta are rejected.
- 原始问题和未批准的选题只保留作审计，不能直接触发写作。
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
