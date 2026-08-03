"""Bounded OpenAI draft provider for target-mapped arrangement context."""
from __future__ import annotations

import json
import os
from typing import Any

import requests

from arrangement_processing import validate_plan

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_OUTPUT_TOKENS = 5000
DEFAULT_MAX_ESTIMATED_USD = 0.50

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "summary": {"type": "string", "maxLength": 240},
        "global_velocity_shift": {"type": "integer", "minimum": -12, "maximum": 12},
        "measure_energy": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "measure": {"type": "integer", "minimum": 1},
                    "velocity_shift": {"type": "integer", "minimum": -12, "maximum": 12},
                    "accents": {"type": "array", "items": {"type": "number", "minimum": 1, "maximum": 16}, "maxItems": 8},
                },
                "required": ["measure", "velocity_shift", "accents"],
            },
        },
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "track": {"type": "string"},
                    "role": {"type": "string", "maxLength": 32},
                    "velocity_shift": {"type": "integer", "minimum": -16, "maximum": 16},
                    "variance": {"type": "integer", "minimum": 0, "maximum": 12},
                    "velocity_processing": {"type": "boolean"},
                    "minimum_velocity": {"type": "integer", "minimum": 1, "maximum": 127},
                },
                "required": ["track", "role", "velocity_shift", "variance", "velocity_processing", "minimum_velocity"],
            },
        },
        "solo_humanization": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
    "required": ["schema_version", "summary", "global_velocity_shift", "measure_energy", "tracks", "solo_humanization"],
}

INSTRUCTIONS = """Create a production-quality whole-song musical-expression plan for MIDI that
has already been mapped to its target sample libraries. Return only the requested JSON schema.

Treat the input as performance evidence, not as a request for a sparse sketch. Infer the song's
energy contour from per-measure note density, onset patterns, existing mean velocities, track
entrances/exits, and interactions between tracks. Produce exactly one measure_energy entry for
every measure from 1 through measure_count, in ascending order, and exactly one tracks entry for
every input track. Shape coherent phrases and section transitions: avoid arbitrary measure-to-
measure oscillation, but do not flatten the arrangement into a few anchor measures. Accents must
match observed onset beats and should mark musically plausible pulse, syncopation, or phrase
peaks rather than every onset.

Preserve the mapped baseline's balance unless the evidence clearly calls for a correction.
Prefer global_velocity_shift=0. Track shifts should normally stay within -4..+4; use a larger
shift only when persistent input statistics justify it. Do not strongly attenuate a melodic or
vocal track merely to label it as support. Use low variance (usually 0..3), keep drums audible
with a defensible minimum_velocity, and avoid pushing ordinary guitar notes into the reserved
120..127 articulation zone.

Hard constraints: never add, remove, or repitch notes; never rewrite rhythm; never add drum hits;
never request post-mapping timing changes. Solo/Lead guitar must keep velocity_processing=false,
velocity_shift=0, and variance=0; solo_humanization.enabled must be false. The plan must be
musically complete and ready for evaluation, while actual MIDI application remains a separate
explicit approval step."""


class OpenAIDraftError(RuntimeError):
    """Safe provider error whose message contains no remote body or credential."""


def _env_int(name: str, default: int, lower: int) -> int:
    try:
        return max(lower, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lower: float) -> float:
    try:
        return max(lower, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def get_openai_config() -> dict[str, Any]:
    return {
        "token": os.environ.get("OPENAI_TOKEN", "").strip() or None,
        "model": os.environ.get("OPENAI_ARRANGEMENT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "timeout_seconds": _env_int("OPENAI_ARRANGEMENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 1),
        "max_output_tokens": _env_int("OPENAI_ARRANGEMENT_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, 256),
        "max_estimated_usd": _env_float("OPENAI_ARRANGEMENT_MAX_ESTIMATED_USD", DEFAULT_MAX_ESTIMATED_USD, 0.01),
    }


def is_openai_configured() -> bool:
    return bool(get_openai_config()["token"])


def calculate_cost_estimate(input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    total_input = max(0, int(input_tokens))
    cached = min(total_input, max(0, int(cached_input_tokens)))
    uncached = total_input - cached
    return (uncached * 5.0 + cached * 0.5 + max(0, int(output_tokens)) * 30.0) / 1_000_000


def validate_spend_guard(estimate: float, max_estimated_usd: float) -> None:
    if estimate > max_estimated_usd:
        raise OpenAIDraftError(
            f"OpenAI draft blocked by spend guard ({estimate:.4f} USD > {max_estimated_usd:.4f} USD)"
        )


def _output_text(result: dict[str, Any]) -> str:
    if isinstance(result.get("output_text"), str) and result["output_text"].strip():
        return result["output_text"]
    for item in result.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                return str(content.get("text", ""))
    return ""


def _enforce_solo_policy(plan: dict[str, Any], context: dict[str, Any]) -> None:
    solo_names = {
        str(track.get("name", ""))
        for track in context.get("tracks", [])
        if str(track.get("type", "")) == "GUITAR"
        and any(word in str(track.get("name", "")).casefold() for word in ("solo", "lead"))
    }
    for track in plan.get("tracks", []):
        if track.get("track") in solo_names:
            track["velocity_processing"] = False
            track["velocity_shift"] = 0
            track["variance"] = 0
    plan["solo_humanization"] = {"enabled": False}


def create_openai_draft(context: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or get_openai_config())
    token = str(config.get("token") or "").strip()
    if not token:
        raise OpenAIDraftError("OpenAI draft is not configured")

    compact_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    # A character-ratio estimate under-counted the real Spring Melody request
    # (18,868 estimated vs 22,991 billed input tokens). UTF-8 byte count is a
    # deliberately conservative tokenizer upper bound, making the guard
    # fail-closed at the cost of rejecting some requests that would be cheaper.
    estimated_input = len(compact_context.encode("utf-8"))
    max_output = int(config.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
    preflight_cost = calculate_cost_estimate(estimated_input, 0, max_output)
    validate_spend_guard(preflight_cost, float(config.get("max_estimated_usd", DEFAULT_MAX_ESTIMATED_USD)))

    payload = {
        "model": config.get("model", DEFAULT_MODEL),
        "instructions": INSTRUCTIONS,
        "input": compact_context,
        "max_output_tokens": max_output,
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "gpmidi_expression_plan", "strict": True, "schema": PLAN_SCHEMA}},
    }
    try:
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )
        response.raise_for_status()
        result = response.json()
        raw_plan = json.loads(_output_text(result))
        if not isinstance(raw_plan, dict):
            raise ValueError("plan is not an object")
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
        # Keep request metadata out of any upstream traceback/logging contract.
        raise OpenAIDraftError("OpenAI draft request or response failed") from None

    track_names = [str(track.get("name", "")) for track in context.get("tracks", [])]
    try:
        plan = validate_plan(
            raw_plan,
            track_names=track_names,
            measure_count=max(1, int(context.get("measure_count", 1))),
            allow_solo_humanization=False,
        )
    except (TypeError, ValueError) as exc:
        raise OpenAIDraftError("OpenAI draft plan failed validation") from exc
    _enforce_solo_policy(plan, context)

    raw_usage = result.get("usage") or {}
    input_tokens = int(raw_usage.get("input_tokens", 0) or 0)
    details = raw_usage.get("input_tokens_details") or {}
    cached_tokens = int(details.get("cached_tokens", raw_usage.get("cached_input_tokens", 0)) or 0)
    output_tokens = int(raw_usage.get("output_tokens", 0) or 0)
    total_tokens = int(raw_usage.get("total_tokens", input_tokens + output_tokens) or input_tokens + output_tokens)
    estimated_cost = calculate_cost_estimate(input_tokens, cached_tokens, output_tokens)
    return {
        "status": "draft_ready",
        "plan": plan,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "pricing_note": "estimate, not authoritative billing",
        },
        "provider": "openai",
        "model": str(result.get("model") or config.get("model", DEFAULT_MODEL)),
        "response_id": str(result.get("id") or ""),
        "preflight_estimated_cost_usd": round(preflight_cost, 6),
    }
