# AI 垂直资讯 Radar Demo

这是独立的只读 Demo：外部 X 聚合账号只作发现输入，输出是我们自己的十个垂直账号。它记录事件、发现帖和上游原帖，生成待审中文草稿，但不发布、不互动，也不修改现有 production、crypto 或 Douyin 数据。

```bash
.venv/bin/python scripts/run_ai_radar_demo.py
```

每次执行都从首屏开始按 cursor 继续翻页，直到越过配置的时间窗；不使用 `collect_big_source_posts` 的 20 小时断点跳过，也不以单页 40 条作为总量上限。首次运行解析账号 ID，之后缓存在独立 `data/ai_radar_demo/accounts.json`，避免重复调用账号查询。抓取和改写凭据只在运行时从 Keychain 或环境变量读取，不会写进输出。只抓取、不改写可运行：

```bash
.venv/bin/python scripts/run_ai_radar_demo.py --no-rewrite
```

输出在 `data/ai_radar_demo/output/`：

- `latest.json`、`latest.md`、`latest.html`：事件、原帖和分类结果；
- `drafts.json`、`drafts.md`、`drafts.html`：十个自有账号的待审草稿；
- `by_focus/`：按领域拆分的事件；
- `by_account/`：按自有账号拆分的草稿。

SQLite 使用四类持久记录：原始帖子、规范化事件、事件对应的全部发现帖/上游原帖，以及草稿版本。同一事件只保留一份有效待审稿；输入或改写规则变化时，旧稿标记为 `superseded`，不会覆盖溯源记录。

输入保留原创、引用和转推，过滤回复。引用/转推会解析 payload 的上游 Tweet、作者和 expanded URL。事件键依次为上游外链 URL、上游 Tweet ID、主帖外链 URL、规范化文本签名。多个聚合号的同一事件会合并，但保持 `discovery_only`，发布前必须回溯官方公告、论文、Repo、监管文件或可靠媒体原文。

路由只允许一个 focus，按事件新增资产/动作排序：政策法律、商业企业、AI for Science、机器人、芯片算力、创意多模态、研究开源、AI 编程、Agent 自动化、前沿模型。无法明确判定的事件保持低置信未分类，不会被强行输出。每个 focus 在配置中只对应一个自有账号，次级标签不会复制生成第二份稿件。

草稿生成前有第一方来源硬过滤：quote/retweet 检查上游原帖，普通帖检查本帖。团队成员、创始人、官方账号或项目维护者在发布自家研究、产品、功能、公司进展、故障修复、增长或项目预告时，事件继续保留用于内部溯源，但草稿标记为 `excluded_first_party_self_publication`，不会进入待审网页。明确的第一方口吻自动识别；没有第一人称但已确认团队归属的作者，通过 `first_party_affiliations` 关联作者与自家项目。第三方转述和个人使用现成工具的普通体验不因这条规则被过滤。

改写复用本地抖音的最小改写规则：把原稿当骨架，保留语序、段落、叙事顺序、人称、情绪和篇幅；中文只做局部清理，英文只做忠实中文化；只删除链接、话题标签、互动号召和不影响正文的身份前情。正文不出现原帖作者姓名、昵称、handle、履历或来源归因，不新增或删失事实、数字、日期、因果、观点、解释和结论。每条成稿正文下固定附一行 `原帖：<原始 X 链接>`，链接标签不展示作者名；quote/retweet 优先使用上游原帖，普通帖使用本帖。所有草稿固定为 `needs_verification` + `needs_review`；通过过滤且未生成过当前版本草稿的新增事件全部进入待审，不设每轮或每账号数量上限。

当前建议每两小时运行一次。系统只生成内部候选，不登录、转发或发布到任何 X 账号。

Railway 独立服务为 `ai-radar-demo`，使用单独的 `/data` 持久卷，网页与两小时调度由 `ai_radar_app.py` 同一进程提供。部署包的 Railpack 启动配置在 `deploy/ai-radar/railpack.json`，公开待审页为：

https://ai-radar-demo-production.up.railway.app/
