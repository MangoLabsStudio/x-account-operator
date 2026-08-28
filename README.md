# X Account Operator

面向 20 个人设的中文内容生产与审核服务。系统每天从 Crypto 与 AI 的 X 信源池增量抓取公开帖子，整理市场 Context，再经过选题、角度、人设 Thesis、事实落地、内容结构和成稿审核，最终只向前端输出可人工审核的完整推文。

系统不登录 X、不自动点赞、转发、关注或发布。所有候选稿停在人工审核队列。

## 当前流程

```mermaid
flowchart LR
    A[Crypto / AI X 信源池] --> B[每日 Context]
    B --> C[热点池与发现池]
    C --> D[Grok 补充实时语境]
    D --> E[Gemini 展开独立角度]
    E --> F[Persona Thesis Resolver]
    F --> G[Reality Grounding]
    G --> H[按题材选择内容结构]
    H --> I[Gemini 成稿与审核]
    I --> J[needs_review 队列]
    J --> K[人工审核与发布]
```

完整设计见 [中文架构文档](docs/architecture.md)；人设调度与 Editorial Context 契约见 [灵活人设内容调度器](docs/flexible-persona-content-scheduler.md)。

## 核心规则

- 热点和发现只决定研究对象，不直接决定文章结论。
- Topic 先展开为互不替代的 Angle，再由每个人设解析成唯一、明确的 Thesis。
- Thesis 决定“说什么”；内容结构只决定“怎么说”，不能改写主张。
- Grok 用于补充最新背景和争议，不会把搜索结果自动升级为正式事实。
- Gemini 只能把已批准证据写成事实；推断必须保留为推断。
- 同一事件允许不同人设表达不同 Thesis，换口吻的同一 Thesis 会被去重。
- 每个人设每天目标为 3 条待审稿；目标不是硬凑数，无法通过门槛时允许少于 3 条。
- 批量重新生成只把旧稿标记为 `superseded`，不会物理删除历史正文。

## 页面与接口

生产入口：<https://x-account-operator-api-production.up.railway.app/>

| 入口 | 用途 |
| --- | --- |
| `/` | 今日完整推文队列 |
| `/market` | 每日 Context、来源、选题及拒绝原因 |
| `/personas` | 人设、语气、内容边界和素材管理 |
| `GET /api/daily-posts` | 获取当天 `needs_review` 推文 |
| `POST /api/daily-posts/generate` | 对当天已批准 Context 启动生成 |
| `POST /api/daily-posts/regenerate` | 保留旧稿并重新生成 |
| `POST /api/post-candidates/{id}/rewrite` | 按人工反馈重写单条候选 |
| `POST /api/post-candidates/{id}/published` | 将当前队首标记为已发布 |
| `GET /api/thesis-metrics` | 查看 Thesis 流水线指标 |
| `GET /health` | 服务、并发池和契约版本状态 |

配置 `XOPS_OPERATOR_TOKEN` 后，所有非 GET 的 `/api` 请求必须携带 `X-Ops-Token`。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
XOPS_DATA_DIR="$PWD/data" .venv/bin/uvicorn app:app --reload
```

打开 <http://127.0.0.1:8000/>。

## 关键配置

| 文件 | 作用 |
| --- | --- |
| `configs/content_source_accounts.json` | Crypto X 总信源池 |
| `configs/ai_content_source_accounts.json` | AI X 信源池 |
| `configs/topic_selection_policy.json` | 热点、发现、增量价值和拒绝标准 |
| `configs/editorial_content_structures.json` | 各题材的语义槽、论证形状和 CTA 策略 |
| `configs/persona_supplement_topics.json` | 可审计的常青补位题源 |

常用环境变量：

```text
XOPS_DATA_DIR=/data
XOPS_TIMEZONE=Asia/Shanghai

XOPS_DAILY_CONTEXT_ENABLED=true
XOPS_DAILY_CONTEXT_RUN_TIME=08:15
XOPS_DAILY_CONTEXT_HOURS=30
XOPS_DAILY_CONTEXT_WORKERS=8
XOPS_DAILY_CONTEXT_RESUME_HOURS=20
XOPS_MOTHER_POOL_ACCOUNTS=/app/configs/content_source_accounts.json
XOPS_AI_SOURCE_ACCOUNTS=/app/configs/ai_content_source_accounts.json
XOPS_AI_SOURCE_ENABLED=true

XOPS_DAILY_POST_ENABLED=true
XOPS_DAILY_POST_TARGET_PER_PERSONA=3
XOPS_DAILY_SUPPLEMENT_COOLDOWN_DAYS=7
XOPS_EDITORIAL_RESEARCH_CONCURRENCY=4
XOPS_EDITORIAL_EVALUATION_CONCURRENCY=5
XOPS_EDITORIAL_GENERATION_CONCURRENCY=5

TWITTER241_RAPIDAPI_KEY=...
XOPS_GROK_API_KEY=...
XOPS_GROK_BASE_URL=https://www.micuapi.ai/v1
XOPS_GROK_MODEL=grok-4.6
XOPS_GEMINI_API_KEY_1=...
XOPS_GEMINI_API_KEY_2=...
XOPS_GEMINI_API_KEY_3=...
XOPS_GEMINI_API_KEY_4=...
XOPS_GEMINI_API_KEY_5=...
XOPS_GEMINI_BASE_URL=https://www.micuapi.ai/v1
XOPS_GEMINI_MODEL=gemini-3.1-pro-preview-low
XOPS_OPERATOR_TOKEN=...
```

凭据只通过运行环境或 macOS Keychain 注入，不进入 SQLite、生成产物或 Git。

## 数据与状态

生产数据保存在 Railway `/data` 持久卷的 SQLite 中。主要状态：

- `daily_context_runs`：`queued → running → needs_review → approved`，失败为 `failed`。
- `persona_editorial_evaluations`：每个人设对每个题的 `WRITE / HOLD / IGNORE`、Thesis 和生成断点。
- `post_candidates`：正式待审稿为 `needs_review`，已确认发布为 `published`，被新一轮替代的旧稿为 `superseded`。
- `topic_claim_history`：保存已覆盖的核心主张，用于同人设与跨人设去重。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```

## 目录

```text
app.py                  FastAPI、SQLite 与内容生产主流程
market_sources/         Crypto / AI 信源抓取和交叉验证
configs/                选题、结构、人设与信源硬配置
docs/                   架构和调度契约
scripts/                数据导入、预览和一次性维护脚本
tests/                  单元与流程测试
assets/                 已授权的人设素材
```

## 部署

Railway 使用 `start.sh` 启动：

```text
uvicorn app:app --host 0.0.0.0 --port $PORT
```

`railway.json` 已配置 `/health` 健康检查和 `/data` 持久卷。部署后至少检查：

```bash
curl -fsS https://x-account-operator-api-production.up.railway.app/health
curl -fsS https://x-account-operator-api-production.up.railway.app/api/daily-posts
```
