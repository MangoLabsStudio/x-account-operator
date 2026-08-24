# 每日 Post 草稿

Railway 上运行的人设内容服务。每天定时刷新市场研究，并按队列为 10 个人设生成 Post 草稿；其中 3 条带素材候选，7 条为文字草稿。

## Production access

Open `https://x-account-operator-api-production.up.railway.app/`.

不连接 X 账号，不保存 X OAuth，不排期，也不自动发布。

## Context workflow

Open `/market` before generating posts:

1. The daily scheduler runs at 08:15 Asia/Shanghai and gathers the configured X mother pool.
2. It cross-validates the collected cards and writes an **automatic draft** only.
3. On `/market`, load that draft, edit it if needed, then click “确认保存”. The page first saves the review back to the run and only then promotes it to the formal daily market context.
4. A failed run can be retried from the same page. “立即抓取” is for an on-demand run.
5. The original manual path remains available: paste a feed, synthesize an unsaved preview, then save the daily market map yourself.
6. Maintain stable project dossiers separately from daily events.
7. Store each persona's prior views, watchlist, unresolved questions, and claim limits.
8. 系统按 `XOPS_DAILY_POST_PERSONAS` 的顺序生成当天 Post 草稿队列。

Automatic drafts and formal daily context are intentionally separate. A generated post should only use a formally saved market map, never an unreviewed scheduler draft.

## Topic selection

`configs/topic_selection_policy.json` is the permanent selection authority.

The daily flow is: mother-pool heat → facts and opinions → candidate questions → common-knowledge and history screening → selected topics → live research → persona writing.

- Heat only decides what to research. It does not make a topic publishable.
- A refreshed number is not a new topic unless it changes the conclusion, scale, mechanism, participation condition, or invalidation condition.
- `topic_claim_history` stores covered core claims across all personas. Semantic duplicates without a material delta are rejected.
- Automatic post generation reads only `selected_topics`; raw deterministic questions are retained under `question_candidates` for audit and can never trigger writing directly.
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

结果入口是 `/`，当天队列 API 是 `/api/daily-posts`。数据保存在 Railway `/data` 持久卷。
