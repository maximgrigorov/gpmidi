from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pytest
import requests

import arrangement_openai as provider


def context():
    return {
        "schema_version": 1,
        "measure_count": 4,
        "tracks": [
            {"name": "Guitar Solo", "type": "GUITAR", "measures": []},
            {"name": "Drums", "type": "DRUMS", "measures": []},
        ],
    }


def raw_plan():
    return {
        "schema_version": 1,
        "summary": "draft",
        "global_velocity_shift": 0,
        "measure_energy": [
            {"measure": measure, "velocity_shift": 0, "accents": []}
            for measure in range(1, 5)
        ],
        "tracks": [
            {"track": "Guitar Solo", "role": "support", "velocity_shift": 8, "variance": 5,
             "velocity_processing": True, "minimum_velocity": 1},
            {"track": "Drums", "role": "pulse", "velocity_shift": 2, "variance": 1,
             "velocity_processing": True, "minimum_velocity": 28},
        ],
        "solo_humanization": {"enabled": True},
    }


def response(plan=None):
    reply = MagicMock()
    reply.raise_for_status.return_value = None
    reply.json.return_value = {
        "id": "resp_123",
        "model": "gpt-5.6-sol-2026-07-01",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(plan or raw_plan())}]}],
        "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 100},
                  "output_tokens": 200, "total_tokens": 1200},
    }
    return reply


def config(**overrides):
    result = {"token": "unit-test-token", "model": "gpt-5.6-sol", "timeout_seconds": 180,
              "max_output_tokens": 5000, "max_estimated_usd": 0.50}
    result.update(overrides)
    return result


def test_defaults_and_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_TOKEN", raising=False)
    assert provider.get_openai_config()["model"] == "gpt-5.6-sol"
    assert provider.is_openai_configured() is False
    monkeypatch.setenv("OPENAI_TOKEN", "x")
    assert provider.is_openai_configured() is True


def test_musical_prompt_requests_complete_production_plan_without_automatic_apply():
    instructions = " ".join(provider.INSTRUCTIONS.split())
    assert "production-quality whole-song" in instructions
    assert "every measure from 1 through measure_count" in instructions
    assert "exactly one tracks entry for every input track" in instructions
    assert "Prefer global_velocity_shift=0" in instructions
    assert "normally stay within -4..+4" in instructions
    assert "separate explicit approval step" in instructions
    assert "Solo/Lead guitar must keep velocity_processing=false" in instructions


def test_cost_uses_cached_discount_and_clamps():
    assert provider.calculate_cost_estimate(1000, 100, 200) == pytest.approx(0.01055)
    assert provider.calculate_cost_estimate(10, 99, 0) == pytest.approx(0.000005)


def test_responses_contract_compact_schema_usage_and_solo_guard(monkeypatch):
    post = MagicMock(return_value=response())
    monkeypatch.setattr(provider.requests, "post", post)
    result = provider.create_openai_draft(context(), config())
    payload = post.call_args.kwargs["json"]
    assert payload["store"] is False
    assert payload["max_output_tokens"] == 5000
    assert "response_format" not in payload
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert payload["input"] == json.dumps(context(), ensure_ascii=False, separators=(",", ":"))
    assert "Guitar Solo" in payload["input"]
    solo = next(item for item in result["plan"]["tracks"] if item["track"] == "Guitar Solo")
    assert solo["velocity_processing"] is False
    assert solo["velocity_shift"] == solo["variance"] == 0
    assert result["plan"]["solo_humanization"] == {"enabled": False}
    assert result["usage"] == {
        "input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200,
        "total_tokens": 1200, "estimated_cost_usd": 0.01055,
        "pricing_note": "estimate, not authoritative billing",
    }
    assert result["model"] == "gpt-5.6-sol-2026-07-01"
    assert result["response_id"] == "resp_123"
    estimated_input_bytes = len((payload["instructions"] + payload["input"]).encode("utf-8"))
    assert result["preflight_estimated_cost_usd"] == pytest.approx(
        provider.calculate_cost_estimate(estimated_input_bytes, 0, 5000)
    )


def test_custom_instructions_are_normalized_sent_and_fingerprinted(monkeypatch):
    post = MagicMock(return_value=response())
    monkeypatch.setattr(provider.requests, "post", post)

    result = provider.create_openai_draft(
        context(), config(), instructions="  Keep the accepted groove.\r\nHumanize attacks only.  "
    )

    assert post.call_args.kwargs["json"]["instructions"] == (
        "Keep the accepted groove.\nHumanize attacks only."
    )
    assert result["instructions"] == "Keep the accepted groove.\nHumanize attacks only."
    assert result["instructions_sha256"] == hashlib.sha256(
        result["instructions"].encode("utf-8")
    ).hexdigest()


def test_empty_instructions_use_versioned_default_and_oversized_fail_before_network(monkeypatch):
    post = MagicMock(return_value=response())
    monkeypatch.setattr(provider.requests, "post", post)
    provider.create_openai_draft(context(), config(), instructions=" \n ")
    assert post.call_args.kwargs["json"]["instructions"] == provider.INSTRUCTIONS

    post.reset_mock()
    with pytest.raises(provider.OpenAIDraftError, match="too long"):
        provider.create_openai_draft(
            context(), config(), instructions="x" * (provider.MAX_INSTRUCTIONS_CHARS + 1)
        )
    post.assert_not_called()


def test_spend_guard_blocks_before_network(monkeypatch):
    post = MagicMock()
    monkeypatch.setattr(provider.requests, "post", post)
    with pytest.raises(provider.OpenAIDraftError, match="spend guard"):
        provider.create_openai_draft(context(), config(max_estimated_usd=0.01, max_output_tokens=5000))
    post.assert_not_called()


def test_missing_token_fails_without_network(monkeypatch):
    post = MagicMock()
    monkeypatch.setattr(provider.requests, "post", post)
    with pytest.raises(provider.OpenAIDraftError, match="not configured"):
        provider.create_openai_draft(context(), config(token=None))
    post.assert_not_called()


@pytest.mark.parametrize("failure", [
    requests.Timeout("unit-test-token remote secret"),
    ValueError("unit-test-token invalid json"),
])
def test_remote_and_decode_errors_are_sanitized(monkeypatch, failure):
    if isinstance(failure, requests.RequestException):
        monkeypatch.setattr(provider.requests, "post", MagicMock(side_effect=failure))
    else:
        reply = response()
        reply.json.side_effect = failure
        monkeypatch.setattr(provider.requests, "post", MagicMock(return_value=reply))
    with pytest.raises(provider.OpenAIDraftError) as caught:
        provider.create_openai_draft(context(), config())
    assert "unit-test-token" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(("status", "expected_code", "expected_message"), [
    (400, "provider_rejected", "rejected the request (HTTP 400)"),
    (401, "authentication_failed", "authentication failed (HTTP 401)"),
    (429, "rate_limited", "rate limit (HTTP 429)"),
    (500, "provider_unavailable", "temporarily unavailable (HTTP 500)"),
])
def test_http_failures_expose_safe_diagnostic_category_without_response_body(
    monkeypatch, status, expected_code, expected_message
):
    reply = response()
    reply.status_code = status
    reply.headers = {"x-request-id": "req_safe_123"}
    reply.text = "unit-test-token upstream secret body"
    error = requests.HTTPError("unit-test-token upstream secret body", response=reply)
    reply.raise_for_status.side_effect = error
    monkeypatch.setattr(provider.requests, "post", MagicMock(return_value=reply))

    with pytest.raises(provider.OpenAIDraftError) as caught:
        provider.create_openai_draft(context(), config())

    assert caught.value.code == expected_code
    assert expected_message in str(caught.value)
    assert caught.value.provider_request_id == "req_safe_123"
    assert "unit-test-token" not in str(caught.value)
    assert "secret body" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_timeout_and_empty_output_have_distinct_safe_diagnostics(monkeypatch):
    monkeypatch.setattr(
        provider.requests, "post", MagicMock(side_effect=requests.Timeout("unit-test-token"))
    )
    with pytest.raises(provider.OpenAIDraftError) as timeout:
        provider.create_openai_draft(context(), config())
    assert timeout.value.code == "timeout"
    assert "timed out" in str(timeout.value)

    reply = response()
    reply.json.return_value["output"] = []
    monkeypatch.setattr(provider.requests, "post", MagicMock(return_value=reply))
    with pytest.raises(provider.OpenAIDraftError) as empty:
        provider.create_openai_draft(context(), config())
    assert empty.value.code == "empty_output"
    assert "no usable output" in str(empty.value)


def test_empty_output_does_not_expose_untrusted_response_id(monkeypatch):
    reply = response()
    reply.json.return_value["id"] = "unit-test-token secret body"
    reply.json.return_value["output"] = []
    monkeypatch.setattr(provider.requests, "post", MagicMock(return_value=reply))

    with pytest.raises(provider.OpenAIDraftError) as caught:
        provider.create_openai_draft(context(), config())

    assert caught.value.provider_request_id is None
    assert "unit-test-token" not in str(caught.value)


def test_empty_output_is_failure(monkeypatch):
    reply = response()
    reply.json.return_value["output"] = []
    monkeypatch.setattr(provider.requests, "post", MagicMock(return_value=reply))
    with pytest.raises(provider.OpenAIDraftError, match="no usable output") as caught:
        provider.create_openai_draft(context(), config())
    assert caught.value.code == "empty_output"


def test_incomplete_plan_missing_solo_track_fails_closed(monkeypatch):
    incomplete = raw_plan()
    incomplete["measure_energy"] = [
        {"measure": measure, "velocity_shift": 0, "accents": []}
        for measure in range(1, 5)
    ]
    incomplete["tracks"] = [
        item for item in incomplete["tracks"] if item["track"] != "Guitar Solo"
    ]
    monkeypatch.setattr(provider.requests, "post", MagicMock(return_value=response(incomplete)))

    with pytest.raises(provider.OpenAIDraftError, match="complete"):
        provider.create_openai_draft(context(), config())


def test_no_credential_literal_in_provider_source():
    source = open(provider.__file__, encoding="utf-8").read()
    assert "unit-test-token" not in source
    assert "OPENAI_TOKEN" in source
