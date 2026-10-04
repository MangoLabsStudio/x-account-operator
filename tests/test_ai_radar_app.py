import json
import os

os.environ["AI_RADAR_SCHEDULER_ENABLED"] = "false"

from fastapi.testclient import TestClient

import ai_radar_app


def test_ai_radar_viewer_serves_page_and_sanitized_drafts(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_radar_app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "drafts.html").write_text("<h1>待审草稿</h1>", encoding="utf-8")
    (tmp_path / "drafts.json").write_text(json.dumps({
        "generated_at": "2026-09-01T00:00:00+00:00",
        "draft_count": 1,
        "drafts": [{
            "event_id": "internal-id",
            "output_account_name": "模型前线",
            "focus": "frontier_models",
            "draft_zh": "正文",
            "original_post_url": "https://x.com/source/status/1",
            "verification_status": "needs_verification",
            "review_status": "needs_review",
            "created_at": "2026-09-01T00:00:00+00:00",
            "event": {"handle": "private-source"},
        }],
    }), encoding="utf-8")
    with TestClient(ai_radar_app.app) as client:
        assert client.get("/").status_code == 200
        payload = client.get("/api/drafts").json()
        assert payload["draft_count"] == 1
        assert payload["drafts"][0]["draft_zh"] == "正文"
        assert payload["drafts"][0]["persona"]["name"] == "Milo"
        assert payload["drafts"][0]["persona"]["profile_handle"] == "miloonmodels"
        assert payload["drafts"][0]["persona"]["profile_handle_is_internal_only"] is True
        assert payload["personas"][0]["draft_count"] == 1
        assert "event" not in payload["drafts"][0]
        assert "event_id" not in payload["drafts"][0]
        assert client.get("/health").json()["draft_count"] == 1


def test_persona_profiles_match_the_ten_ai_focuses():
    profiles = {item["focus"]: item["name"] for item in ai_radar_app.persona_profiles()}
    assert profiles == {
        "frontier_models": "Milo", "ai_coding": "Dev", "agents_automation": "Nate",
        "creative_multimodal": "Maya", "research_open_source": "Theo",
        "robotics_embodied": "Rory", "chips_compute": "Sam",
        "business_enterprise": "Ben", "policy_safety_legal": "Lex",
        "ai_for_science": "Iris",
    }
