import sqlite3
from datetime import datetime, timezone

from market_sources.ai_draft_pipeline import (
    BLOCKED_FIRST_PARTY_SELF_PUBLICATION,
    ELIGIBLE,
    apply_first_party_affiliation,
    build_draft_prompt,
    draft_eligibility,
    draft_violations,
    init_db,
    normalize_draft_markup,
    original_post_url,
    output_account_for_event,
    parse_draft_response,
    persist_draft,
    persist_event,
    refresh_draft_content_rules,
    refresh_draft_eligibility,
    remove_source_author_identifiers,
)
from scripts.run_ai_radar_demo import fetch_account, load_drafts, pending_drafts, render_drafts, runtime_paths


def event():
    return {
        "event_id": "event-1", "event_key": "x:99", "focus": "ai_coding",
        "confidence": "high", "canonical_source_url": "https://x.com/official/status/99",
        "verification_status": "needs_verification", "route_reason": "matched:ai_coding",
        "first_seen_at": "2026-09-01T00:00:00+00:00", "last_seen_at": "2026-09-01T01:00:00+00:00",
        "source_post_ids": ["one", "two"], "post_id": "one", "text": "Aggregator says a coding agent launched",
        "post_type": "quote", "handle": "MaxForAI", "author_name": "Max",
        "discovery_url": "https://x.com/MaxForAI/status/one", "upstream_text": "Official: a coding agent launched.",
        "upstream_handle": "official", "upstream_author_name": "Official Author",
        "upstream_url": "https://x.com/official/status/99",
    }


def posts():
    return [
        {"post_id": "one", "handle": "MaxForAI", "url": "https://x.com/MaxForAI/status/one", "text": "First discovery", "created_at": "2026-09-01T00:00:00+00:00", "post_type": "quote", "upstream_post_id": "99", "upstream_handle": "official", "upstream_url": "https://x.com/official/status/99", "upstream_text": "Official: a coding agent launched.", "external_urls": [], "upstream_external_urls": ["https://example.com/release"]},
        {"post_id": "two", "handle": "aigclink", "url": "https://x.com/aigclink/status/two", "text": "Second discovery", "created_at": "2026-09-01T01:00:00+00:00", "post_type": "retweet", "upstream_post_id": "99", "upstream_handle": "official", "upstream_url": "https://x.com/official/status/99", "upstream_text": "Official: a coding agent launched.", "external_urls": [], "upstream_external_urls": ["https://example.com/release"]},
    ]


def config():
    return {
        "focuses": [{"id": "ai_coding", "title": "AI 编程"}],
        "output_accounts": [{
            "slug": "coding-cn", "name": "Dev", "profile_handle": "devcodes",
            "profile_handle_is_internal_only": True,
            "bio": "I build with AI coding tools in real repos. The demo is the easy part.",
            "focus": "ai_coding",
        }],
    }


def test_event_is_idempotent_and_keeps_every_discovery():
    db = sqlite3.connect(":memory:")
    init_db(db)
    persist_event(db, event(), posts())
    persist_event(db, event(), posts())
    assert db.execute("SELECT COUNT(*) FROM ai_radar_events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM ai_radar_event_discoveries").fetchone()[0] == 2
    stored = db.execute("SELECT upstream_text, upstream_url FROM ai_radar_event_discoveries WHERE discovery_post_id='one'").fetchone()
    assert stored == ("Official: a coding agent launched.", "https://x.com/official/status/99")


def test_single_focus_has_one_output_account_and_review_only_draft():
    db = sqlite3.connect(":memory:")
    init_db(db)
    persist_event(db, event(), posts())
    assert output_account_for_event(event(), config())["slug"] == "coding-cn"
    assert persist_draft(db, event(), config(), "官方称，新的编程智能体已推出。", model="test") is True
    assert persist_draft(db, event(), config(), "官方称，新的编程智能体已推出。", model="test") is False
    stored = db.execute("SELECT output_account_id,verification_status,review_status FROM ai_radar_drafts").fetchone()
    assert stored == ("coding-cn", "needs_verification", "needs_review")


def test_multi_source_event_prompt_and_strict_json_response():
    prompt = build_draft_prompt(event(), config()["output_accounts"][0])
    assert "Aggregator says a coding agent launched" in prompt
    assert "人称逐句继承" in prompt
    assert "第三人称" not in prompt
    assert "正文不得出现原帖作者的姓名、昵称、handle" in prompt
    assert "Official Author、official" in prompt
    assert parse_draft_response('{"draft_zh":"官方称，新的编程智能体已推出。"}') == "官方称，新的编程智能体已推出。"
    assert parse_draft_response('```json\n{"draft_zh":"x","extra":true}\n```') == "x"
    assert draft_violations("OpenAI 表示将推出新功能。") == []
    assert draft_violations("我已经实测。", source_text="我已经实测了。") == []
    assert draft_violations("我已经实测。", source_text="模型已经发布。") == ["added_first_person"]
    assert draft_violations("我觉得已经可以用了。", source_text="模型已经发布。") == ["added_first_person"]
    assert draft_violations("模型形成递归式自我改进。", source_text="recursive self-improvement") == []
    assert draft_violations("我们已经实测：https://t.co/a #AI", source_text="我们已经实测") == ["source_markup"]
    assert draft_violations("开源研究站关注到论文 arxiv.org/abs/123", "开源研究站") == ["source_markup", "self_reference"]
    assert draft_violations("据信源账号 @foo 表示已经发布。", source_text="已经发布。") == ["source_markup", "attribution_tone"]
    assert normalize_draft_markup("@moshhamedani 构建了项目，联系 foo@example.com") == "构建了项目，联系 foo@example.com"
    assert normalize_draft_markup("正文 https://example.com/a #AI") == "正文"
    assert draft_violations("Gavin Baker 认为数据中心有价值。", source_author_identifiers=["Gavin Baker"]) == ["source_author_identity"]
    assert draft_violations("数据中心有价值。", source_author_identifiers=["Gavin Baker"]) == []
    assert remove_source_author_identifiers("Gavin Baker 认为数据中心有价值。", ["Gavin Baker"]) == "认为数据中心有价值。"


def test_pending_drafts_includes_every_new_event_without_a_cap():
    db = sqlite3.connect(":memory:")
    init_db(db)
    one = event()
    two = event() | {"event_id": "event-2", "event_key": "x:100", "post_id": "three", "source_post_ids": ["three"]}
    persist_event(db, one, posts())
    persist_event(db, two, [])
    tasks = pending_drafts(db, [one, two], config())
    assert {item[0]["event_id"] for item in tasks} == {"event-1", "event-2"}
    assert persist_draft(db, one, config(), "新的编程智能体已经推出。", model="test") is True
    assert [item[0]["event_id"] for item in pending_drafts(db, [one, two], config())] == ["event-2"]
    db.execute("UPDATE ai_radar_drafts SET review_status='superseded' WHERE event_id='event-1'")
    assert [item[0]["event_id"] for item in pending_drafts(db, [one, two], config())] == ["event-2"]


def test_source_identity_draft_is_queued_once_for_rewrite():
    db = sqlite3.connect(":memory:")
    init_db(db)
    item = event()
    persist_event(db, item, posts())
    assert persist_draft(db, item, config(), "Official Author 发布了新功能。", model="test") is True
    assert refresh_draft_content_rules(db) == 1
    assert db.execute("SELECT review_status FROM ai_radar_drafts").fetchone()[0] == "needs_rewrite"
    assert [task[0]["event_id"] for task in pending_drafts(db, [item], config())] == ["event-1"]


def test_fetch_account_pages_until_all_new_posts_are_collected(monkeypatch):
    def tweet(post_id, created_at):
        return {
            "__typename": "Tweet", "rest_id": post_id,
            "core": {"user_results": {"result": {"rest_id": "42", "core": {"screen_name": "source"}}}},
            "legacy": {"id_str": post_id, "created_at": created_at, "full_text": post_id},
        }

    pages = {
        None: {"data": [tweet("new-1", "Tue Sep 01 01:00:00 +0000 2026")], "cursor": {"bottom": "page-2"}},
        "page-2": {"data": [tweet("new-2", "Tue Sep 01 00:30:00 +0000 2026")], "cursor": {"bottom": "page-3"}},
        "page-3": {"data": [tweet("old", "Mon Aug 31 20:00:00 +0000 2026")], "cursor": {"bottom": "page-4"}},
    }
    calls = []

    def fake_page(_key, _account, cursor=None, count=40):
        calls.append((cursor, count))
        return pages[cursor]

    monkeypatch.setattr("scripts.run_ai_radar_demo.fetch_page", fake_page)
    result = fetch_account(
        "runtime-key", {"handle": "source", "user_id": "42"},
        datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert calls == [(None, 40), ("page-2", 40), ("page-3", 40)]
    assert {post["post_id"] for post in result} == {"new-1", "new-2"}


def test_fetch_account_ignores_repeated_pin_and_nested_old_tweet(monkeypatch):
    def tweet(post_id, created_at):
        return {
            "__typename": "Tweet", "rest_id": post_id,
            "core": {"user_results": {"result": {"rest_id": "42", "core": {"screen_name": "source"}}}},
            "legacy": {"id_str": post_id, "created_at": created_at, "full_text": post_id},
        }

    recent = "Tue Sep 01 01:00:00 +0000 2026"
    old = "Mon Aug 31 20:00:00 +0000 2026"
    pages = {
        None: {"data": [tweet("pin", recent), tweet("new-1", recent)], "cursor": {"bottom": "page-2"}},
        "page-2": {"data": [tweet("pin", recent), tweet("new-2", recent), tweet("nested-old", old)], "cursor": {"bottom": "page-3"}},
        "page-3": {"data": [tweet("pin", recent), tweet("new-3", recent)], "cursor": {"bottom": "page-4"}},
        "page-4": {"data": [tweet("pin", recent), tweet("old", old)], "cursor": {"bottom": "page-5"}},
    }
    calls = []

    def fake_page(_key, _account, cursor=None, count=40):
        calls.append(cursor)
        return pages[cursor]

    monkeypatch.setattr("scripts.run_ai_radar_demo.fetch_page", fake_page)
    result = fetch_account(
        "runtime-key", {"handle": "source", "user_id": "42"},
        datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert calls == [None, "page-2", "page-3", "page-4"]
    assert {post["post_id"] for post in result} == {"pin", "new-1", "new-2", "new-3"}


def test_prompt_resolves_leading_short_link_to_subject_name():
    item = event()
    item["text"] = "https://t.co/example has opened its API."
    item["canonical_source_url"] = "https://dit.ai"
    prompt = build_draft_prompt(item, config()["output_accounts"][0])
    assert "原稿开头短链接对应的主体名：dit.ai" in prompt
    assert "只把它当主体名称，不要输出 URL" in prompt


def test_original_post_url_prefers_upstream_x_post():
    assert original_post_url(event()) == "https://x.com/official/status/99"
    item = event() | {"post_type": "original", "upstream_url": "", "discovery_url": "https://x.com/MaxForAI/status/one"}
    assert original_post_url(item) == "https://x.com/MaxForAI/status/one"


def test_first_party_self_publication_is_blocked_but_third_party_report_is_not():
    item = event() | {
        "upstream_text": "Today, we're sharing new research on Solaris, our first interface world model. We find it performs better.",
    }
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_research")
    item["upstream_text"] = "Runway released new research on an interface world model."
    assert draft_eligibility(item) == (ELIGIBLE, "")
    item["upstream_text"] = "We launched an API for developers today."
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_team_update")
    item["upstream_text"] = "Today Manus is independent again. We've shipped new features and we're shipping faster than ever."
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_team_update")
    item["upstream_text"] = "ChatGPT Work isn't working. Sorry, we're working on a fix."
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_team_update")
    item["upstream_text"] = "Samarth and I have joined OpenAI. We started Kairos with a dream."
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_team_update")
    item["upstream_text"] = "做个 Skill，帮大家分享场景。迭代一下这几天发。"
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "self_attributed_personal_project")
    item["upstream_text"] = "I built my last app with Claude Code."
    assert draft_eligibility(item) == (ELIGIBLE, "")
    item["upstream_text"] = "Here it is: our full interview with the Codex product lead."
    assert draft_eligibility(item) == (ELIGIBLE, "")
    item["upstream_text"] = "Join our community to discuss this paper."
    assert draft_eligibility(item) == (ELIGIBLE, "")
    item["upstream_text"] = "This is a Google paper. I wrote about a related idea earlier."
    assert draft_eligibility(item) == (ELIGIBLE, "")
    item["upstream_text"] = "Google 专门用自家的 AI 模型服务自家的业务。"
    assert draft_eligibility(item) == (ELIGIBLE, "")


def test_known_team_member_post_is_blocked_without_first_person():
    item = event() | {"upstream_handle": "finkd", "upstream_text": "Muse Code is out of beta today."}
    assert draft_eligibility(item) == (ELIGIBLE, "")
    apply_first_party_affiliation(item, {"finkd": ["Muse Code"]})
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "known_first_party_affiliation")
    apply_first_party_affiliation(item, {"finkd": ["*"]})
    assert draft_eligibility(item) == (BLOCKED_FIRST_PARTY_SELF_PUBLICATION, "known_first_party_affiliation")


def test_first_party_research_never_enters_pending_queue():
    db = sqlite3.connect(":memory:")
    init_db(db)
    item = event() | {"upstream_text": "We present our new paper and benchmark results."}
    persist_event(db, item, posts())
    assert pending_drafts(db, [item], config()) == []


def test_blocked_research_draft_is_retained_but_removed_from_review_queue():
    db = sqlite3.connect(":memory:")
    init_db(db)
    item = event()
    persist_event(db, item, posts())
    assert persist_draft(db, item, config(), "新的编程智能体已经推出。", model="test") is True
    blocked = item | {
        "upstream_text": "New research: we trained a model on 80 production environments and report our findings.",
    }
    persist_event(db, blocked, posts())
    assert refresh_draft_eligibility(db) == 1
    assert db.execute("SELECT COUNT(*) FROM ai_radar_events").fetchone()[0] == 1
    assert db.execute("SELECT review_status FROM ai_radar_drafts").fetchone()[0] == "excluded_first_party_self_publication"
    assert load_drafts(db, config()) == []
    assert persist_draft(db, blocked, config(), "不应保存。", model="test") is False


def test_old_event_table_gets_eligibility_columns():
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE ai_radar_events(
             event_id TEXT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,focus TEXT,
             confidence TEXT NOT NULL,canonical_source_url TEXT NOT NULL,
             canonical_source_text TEXT NOT NULL,verification_status TEXT NOT NULL,
             first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
             route_reason TEXT NOT NULL,event_json TEXT NOT NULL
           )"""
    )
    init_db(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(ai_radar_events)")}
    assert {"draft_eligibility", "draft_block_reason"} <= columns


def test_runtime_paths_use_railway_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_RADAR_DATA_DIR", str(tmp_path))
    root, db_path, output_dir = runtime_paths({"db_path": "unused", "output_dir": "unused"})
    assert root == tmp_path
    assert db_path == tmp_path / "ai_radar_demo.sqlite3"
    assert output_dir == tmp_path / "output"


def test_rendered_draft_puts_anonymous_original_link_under_body(tmp_path):
    item = {
        "event_id": "event-1", "output_account_id": "coding-cn", "output_account_name": "Dev",
        "focus": "ai_coding", "draft_zh": "新的编程智能体已经推出。",
        "original_post_url": "https://x.com/official/status/99",
        "draft_with_source": "新的编程智能体已经推出。\n\n原帖：[查看原帖](<https://x.com/official/status/99>)",
        "verification_status": "needs_verification", "review_status": "needs_review",
        "model": "test", "created_at": "2026-09-01T00:00:00+00:00", "event": event(),
    }
    render_drafts([item], [], config(), tmp_path)
    html = (tmp_path / "drafts.html").read_text(encoding="utf-8")
    markdown = (tmp_path / "drafts.md").read_text(encoding="utf-8")
    account_markdown = (tmp_path / "by_account" / "coding-cn.md").read_text(encoding="utf-8")
    for output in (html, markdown, account_markdown):
        assert "https://x.com/official/status/99" in output
        assert "Official Author" not in output
        assert "发现帖 @" not in output
        assert "上游原帖 @" not in output
    assert "Dev" in html
    assert "@devcodes" in html
    assert "I build with AI coding tools in real repos." in html
    assert "data-filter='coding-cn'" in html
    assert "人物 handle 仅为本系统人物标识" in html
