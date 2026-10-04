import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from market_sources.collect_viral_priors import (
    china_relevant_finance_post, ingest_douyin, ingest_reddit, init_db, run_actor,
)


def test_ingest_reddit_keeps_post_content_and_metrics():
    with sqlite3.connect(":memory:") as db:
        init_db(db)
        count = ingest_reddit(db, [{
            "_type": "post", "id": "abc", "_subreddit": "technology",
            "title": "A strong take", "selftext": "Full body", "author": "person",
            "url": "https://example.com/news", "permalink": "/r/technology/comments/abc/post/",
            "createdAt": "2026-08-28T00:00:00Z",
            "score": 1200, "upvote_ratio": 0.95, "num_comments": 300,
            "num_crossposts": 4, "subreddit_subscribers": 100000,
            "stickied": False, "over_18": False,
        }])
        row = db.execute(
            "SELECT platform,title,body,community,metrics_json,url FROM viral_priors"
        ).fetchone()
        assert count == 1
        assert row[:4] == ("reddit", "A strong take", "Full body", "technology")
        assert json.loads(row[4])["comments"] == 300
        assert row[5] == "https://www.reddit.com/r/technology/comments/abc/post/"


def test_ingest_douyin_keeps_video_metrics():
    with sqlite3.connect(":memory:") as db:
        init_db(db)
        count = ingest_douyin(db, [{
            "aweme_id": "123", "desc": "情绪很强的口播", "author_nickname": "person",
            "play_count": 500000, "digg_count": 30000, "comment_count": 1200,
            "share_count": 800, "collect_count": 5000,
        }])
        row = db.execute(
            "SELECT platform,title,language,metrics_json FROM viral_priors"
        ).fetchone()
        assert count == 1
        assert row[:3] == ("douyin", "情绪很强的口播", "zh")
        assert json.loads(row[3])["plays"] == 500000


def test_ingest_douyin_supports_sian_output():
    with sqlite3.connect(":memory:") as db:
        init_db(db)
        count = ingest_douyin(db, [{
            "videoId": "456", "videoPageUrl": "https://www.douyin.com/video/456",
            "caption": "AI 新工具实测", "userName": "person", "postedAt": "2026-08-28",
            "playCount": 800000, "likeCount": 60000, "commentCount": 3200,
            "shareCount": 2100, "collectCount": 9000, "followerCount": 120000,
        }])
        row = db.execute(
            "SELECT external_id,url,title,author,metrics_json FROM viral_priors"
        ).fetchone()
        assert count == 1
        assert row[:4] == (
            "456", "https://www.douyin.com/video/456", "AI 新工具实测", "person"
        )
        metrics = json.loads(row[4])
        assert metrics["likes"] == 60000
        assert metrics["followers"] == 120000


def test_china_relevant_finance_post_requires_self_text_and_rejects_us_local_rules():
    base = {
        "_type": "post", "is_self": True,
        "title": "I stopped overtrading after tracking every loss",
        "selftext": "The useful part was not the win rate. It was discovering how often boredom made me enter bad trades." * 2,
    }
    assert china_relevant_finance_post(base)
    assert not china_relevant_finance_post({**base, "selftext": "My Roth IRA and 401(k) allocation " * 8})
    assert not china_relevant_finance_post({**base, "is_self": False})


def test_run_actor_times_out_without_resubmitting_paid_run():
    submitted = {
        "data": {"id": "run-1", "defaultDatasetId": "dataset-1", "status": "READY"}
    }
    with sqlite3.connect(":memory:") as db:
        init_db(db)
        with patch(
            "market_sources.collect_viral_priors.request", return_value=submitted
        ) as request_call, patch(
            "market_sources.collect_viral_priors.monotonic", side_effect=[0, 61]
        ), patch.dict(
            os.environ, {"XOPS_APIFY_POLL_TIMEOUT_SECONDS": "60"}
        ), pytest.raises(RuntimeError, match="稍后从同一 run 继续"):
            run_actor(db, "token", "actor", {}, 0.1)
        assert request_call.call_count == 1
        assert db.execute("SELECT run_id FROM apify_runs").fetchone()[0] == "run-1"


def test_run_actor_does_not_resubmit_terminal_failure_in_same_bucket():
    submitted = {
        "data": {"id": "run-1", "defaultDatasetId": "dataset-1", "status": "READY"}
    }
    failed = {
        "data": {
            "id": "run-1", "defaultDatasetId": "dataset-1", "status": "FAILED",
            "statusMessage": "actor failed",
        }
    }
    with sqlite3.connect(":memory:") as db:
        init_db(db)
        with patch(
            "market_sources.collect_viral_priors.request", side_effect=[submitted, failed]
        ), pytest.raises(RuntimeError, match="ended with FAILED"):
            run_actor(db, "token", "actor", {}, 0.1, cache_bucket="today")
        with patch("market_sources.collect_viral_priors.request") as request_call, pytest.raises(
            RuntimeError, match="下个采集窗口再提交"
        ):
            run_actor(db, "token", "actor", {}, 0.1, cache_bucket="today")
        request_call.assert_not_called()
