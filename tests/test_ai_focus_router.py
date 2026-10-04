import json
from datetime import datetime, timezone
from unittest.mock import patch

from market_sources.ai_focus_router import classify_event, deduplicate_events, event_key, normalize_url
from scripts.run_ai_radar_demo import parse_payload, resolve_accounts


def post(text, **extra):
    return {"post_id": extra.pop("post_id", "one"), "handle": extra.pop("handle", "radar"), "text": text,
            "upstream_text": extra.pop("upstream_text", ""), "created_at": extra.pop("created_at", "2026-09-01T00:00:00+00:00"),
            "is_reply": False, "source_tier": "S3", "external_urls": [], "upstream_external_urls": [],
            "upstream_post_id": "", **extra}


def test_specialist_priority_beats_frontier_lab_name():
    assert classify_event(post("OpenAI signs an enterprise contract for agent deployment"))["focus"] == "business_enterprise"
    assert classify_event(post("OpenAI faces a copyright lawsuit over a new model"))["focus"] == "policy_safety_legal"


def test_all_output_focuses_have_unambiguous_examples():
    cases = {
        "frontier_models": "A new frontier model launches with a longer context window and API pricing.",
        "ai_coding": "The new IDE and CLI coding agent can edit a codebase.",
        "agents_automation": "A browser agent adds MCP skills for workflow automation.",
        "creative_multimodal": "A new video generation model ships a ComfyUI workflow.",
        "research_open_source": "A new arXiv paper releases open weights and a GitHub benchmark.",
        "robotics_embodied": "A humanoid robot VLA dataset is released.",
        "chips_compute": "A new GPU uses HBM for data center inference.",
        "business_enterprise": "The company raised a Series B after signing an enterprise contract.",
        "policy_safety_legal": "A court issued a copyright ruling under the new AI law.",
        "ai_for_science": "A protein model improves antibody drug discovery.",
    }
    assert {expected: classify_event(post(text))["focus"] for expected, text in cases.items()} == {key: key for key in cases}


def test_research_asset_beats_agent_and_coding_tags():
    routed = classify_event(post("A paper releases a GitHub benchmark for coding agents."))
    assert routed["focus"] == "research_open_source"
    assert {"ai_coding", "agents_automation"}.issubset(set(routed["secondary_tags"]))


def test_substrings_do_not_create_false_policy_science_or_business_routes():
    assert classify_event(post("OpenClaw now calls Claude Code through its CLI."))["focus"] == "ai_coding"
    assert classify_event(post('Anthropic is internally testing a new Claude feature.', handle="testingcatalog"))["focus"] == "frontier_models"
    assert classify_event(post("TimesFM is a time-series foundation model on Hugging Face.", handle="HuggingPapers"))["focus"] == "research_open_source"
    assert classify_event(post("Gemini Enterprise will act as an expert in a new Rooms feature."))["focus"] == "business_enterprise"
    assert classify_event(post("Codex is now inside the ChatGPT desktop client."))["focus"] == "ai_coding"


def test_general_tech_sources_need_an_ai_signal():
    unrelated = post("The FTC and state attorneys general plan to sue Amazon over retail ad prices.", handle="Techmeme")
    assert classify_event(unrelated)["focus"] is None
    ai_policy = post("The EU designated ChatGPT under the AI Act and DSA.", handle="Techmeme")
    assert classify_event(ai_policy)["focus"] == "policy_safety_legal"


def test_ai_company_product_lead_move_routes_to_business():
    item = post("Linear's Head of Product is leaving to join OpenAI and lead Codex product work.")
    assert classify_event(item)["focus"] == "business_enterprise"


def test_dominant_vertical_signal_wins():
    assert classify_event(post("GPU CUDA compute made modern AI agents and robots possible."))["focus"] == "chips_compute"
    assert classify_event(post("A 3DGS world generation model is useful for robotics and film."))["focus"] == "creative_multimodal"
    assert classify_event(post("A paper on agent skills includes one embodied interaction benchmark."))["focus"] == "research_open_source"


def test_single_multimodal_mention_does_not_hijack_agent_workflow():
    text = "ChatGPT Work has a browser agent, MCP skills, multi-agent automation, and one image generation skill."
    assert classify_event(post(text))["focus"] == "agents_automation"


def test_url_normalization_and_upstream_priority():
    assert normalize_url("https://www.example.com/a/?utm_source=x&ref=feed#part") == "https://example.com/a"
    assert event_key(post("short", upstream_external_urls=["https://example.com/release?utm_campaign=x"])) == "url:https://example.com/release"


def test_url_only_upstream_uses_discovery_text_for_fallback_key():
    first = post("First distinct event", upstream_text="https://t.co/a")
    second = post("Second distinct event", upstream_text="https://t.co/b")
    assert event_key(first) != event_key(second)


def test_duplicate_reposts_make_one_single_route_discovery_event():
    first = post("A new coding agent launches", post_id="1", handle="MaxForAI", upstream_post_id="99")
    second = post("Translated quote", post_id="2", handle="aigclink", upstream_post_id="99")
    events = deduplicate_events([first, second])
    assert len(events) == 1
    assert events[0]["focus"] == "ai_coding"
    assert events[0]["discovery_sources"] == ["MaxForAI", "aigclink"]
    assert events[0]["discovery_only"] is True


def test_low_confidence_event_is_not_forced_into_a_focus():
    assert classify_event(post("Interesting thoughts from a conference today.")) == {"focus": None, "confidence": "low", "secondary_tags": []}


def test_quote_payload_keeps_upstream_identity_and_url():
    upstream = {
        "__typename": "Tweet",
        "rest_id": "99",
        "core": {"user_results": {"result": {"rest_id": "20", "core": {"screen_name": "official", "name": "Official Author"}}}},
        "legacy": {
            "created_at": "Mon Sep 01 00:00:00 +0000 2026",
            "full_text": "Official model release",
            "entities": {"urls": [{"expanded_url": "https://example.com/release?utm_source=x"}]},
        },
    }
    tweet = {
        "__typename": "Tweet",
        "rest_id": "1",
        "core": {"user_results": {"result": {"rest_id": "10", "core": {"screen_name": "MaxForAI", "name": "Max"}}}},
        "legacy": {
            "created_at": "Mon Sep 01 01:00:00 +0000 2026",
            "full_text": "Worth watching",
            "entities": {"urls": []},
        },
        "quoted_status_result": {"result": upstream},
    }
    rows = parse_payload({"items": [tweet]}, {"user_id": "10", "handle": "MaxForAI"}, datetime(2026, 8, 31, tzinfo=timezone.utc))
    assert len(rows) == 1
    assert rows[0]["post_type"] == "quote"
    assert rows[0]["author_name"] == "Max"
    assert rows[0]["upstream_handle"] == "official"
    assert rows[0]["upstream_author_name"] == "Official Author"
    assert rows[0]["upstream_url"] == "https://x.com/official/status/99"
    assert rows[0]["upstream_external_urls"] == ["https://example.com/release?utm_source=x"]


def test_resolve_accounts_reuses_cache_without_lookup(tmp_path):
    cache = tmp_path / "accounts.json"
    cache.write_text(json.dumps([{"handle": "MaxForAI", "user_id": "10"}]), encoding="utf-8")
    config = {"source_accounts": [{"handle": "MaxForAI", "user_id": None}]}
    with patch("scripts.run_ai_radar_demo.lookup_user_id") as lookup:
        accounts, failures = resolve_accounts(config, "unused", cache)
    lookup.assert_not_called()
    assert accounts == [{"handle": "MaxForAI", "user_id": "10"}]
    assert failures == []


def test_resolve_accounts_keeps_partial_success(tmp_path):
    config = {"source_accounts": [{"handle": "good", "user_id": None}, {"handle": "bad", "user_id": None}]}
    with patch("scripts.run_ai_radar_demo.lookup_user_id", side_effect=lambda _key, handle: "10" if handle == "good" else (_ for _ in ()).throw(LookupError("missing"))):
        accounts, failures = resolve_accounts(config, "runtime-key", tmp_path / "accounts.json")
    assert accounts == [{"handle": "good", "user_id": "10"}]
    assert failures == [{"handle": "bad", "error": "missing"}]
