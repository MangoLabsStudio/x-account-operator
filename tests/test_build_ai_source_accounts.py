import json
from pathlib import Path
from unittest.mock import patch

from scripts.build_ai_source_accounts import build, handle_from_url, user_id_from_payload


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_handle_from_url_accepts_x_and_rejects_non_x():
    assert handle_from_url("https://x.com/Example/status/12") == "Example"
    try:
        handle_from_url("https://example.com/user")
    except ValueError as error:
        assert "有效的 X" in str(error)
    else:
        raise AssertionError("expected invalid X URL")


def test_user_id_from_payload_finds_nested_user():
    assert user_id_from_payload({"data": {"result": {"__typename": "User", "rest_id": "42"}}}) == "42"


def test_build_reuses_output_then_crypto_and_looks_up_only_missing(tmp_path):
    source = tmp_path / "social_selected.json"
    crypto = tmp_path / "crypto.json"
    output = tmp_path / "ai.json"
    write_json(source, [
        {"x_url": "https://x.com/cached"},
        {"x_url": "https://x.com/crypto"},
        {"x_url": "https://x.com/lookup"},
        {"x_url": "https://x.com/duplicate_id"},
        {"x_url": "https://x.com/cached"},
    ])
    write_json(crypto, [{"user_id": "2", "handle": "crypto", "source_lists": ["crypto"]}])
    write_json(output, [{"user_id": "1", "handle": "cached", "source_lists": ["ai"]}])

    def lookup(_key, handle):
        return {"lookup": "3", "duplicate_id": "3"}[handle]

    with patch("scripts.build_ai_source_accounts.twitter241_api_key", return_value="runtime-key") as key, patch(
        "scripts.build_ai_source_accounts.lookup_user_id", side_effect=lookup
    ) as request:
        result = build(source, crypto, output, workers=2)

    assert key.call_count == 1
    assert sorted(call.args[1] for call in request.call_args_list) == ["duplicate_id", "lookup"]
    assert result["source_handles"] == 4
    assert result["accounts_written"] == 3
    assert result["reused_existing"] == 1
    assert result["reused_crypto"] == 1
    assert result["looked_up"] == 2
    assert result["duplicate_user_ids"] == 1
    assert json.loads(output.read_text()) == [
        {"user_id": "1", "handle": "cached", "source_lists": ["ai_influence_landscape_20260804"]},
        {"user_id": "2", "handle": "crypto", "source_lists": ["ai_influence_landscape_20260804"]},
        {"user_id": "3", "handle": "lookup", "source_lists": ["ai_influence_landscape_20260804"]},
    ]


def test_build_keeps_partial_output_and_reports_lookup_failures(tmp_path):
    source = tmp_path / "social_selected.json"
    crypto = tmp_path / "crypto.json"
    output = tmp_path / "ai.json"
    write_json(source, [{"x_url": "https://x.com/good"}, {"x_url": "https://x.com/bad"}, {"x_url": "bad-url"}])
    write_json(crypto, [])

    def lookup(_key, handle):
        if handle == "bad":
            raise RuntimeError("not found")
        return "9"

    with patch("scripts.build_ai_source_accounts.twitter241_api_key", return_value="runtime-key"), patch(
        "scripts.build_ai_source_accounts.lookup_user_id", side_effect=lookup
    ):
        result = build(source, crypto, output, workers=1)

    assert json.loads(output.read_text()) == [
        {"user_id": "9", "handle": "good", "source_lists": ["ai_influence_landscape_20260804"]}
    ]
    assert len(result["failures"]) == 2
    assert result["failures"][0]["index"] == 2
    assert result["failures"][1]["handle"] == "bad"


def test_build_does_not_read_keychain_when_everything_is_known(tmp_path):
    source = tmp_path / "social_selected.json"
    crypto = tmp_path / "crypto.json"
    output = tmp_path / "ai.json"
    write_json(source, [{"x_url": "https://x.com/known"}])
    write_json(crypto, [{"user_id": "1", "handle": "known", "source_lists": ["crypto"]}])

    with patch("scripts.build_ai_source_accounts.twitter241_api_key") as key:
        result = build(source, crypto, output)

    key.assert_not_called()
    assert result["accounts_written"] == 1
