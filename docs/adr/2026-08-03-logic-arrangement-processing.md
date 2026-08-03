# ADR: Logic arrangement processing order

**Status:** accepted
**Date:** 2026-08-03

## Decision

The forward product path is the accepted Guitar Pro score. Audio-onset recognition and the pmetal experiment are frozen as historical evidence and are not inputs to the product pipeline.

Processing order:

1. Parse Guitar Pro and preserve its bar/rhythm/tempo identity.
2. Resolve the target instrument and map Shreddage/Hydra articulations, keyswitches, pitch-bends and reserved velocity zones.
3. Build one compact whole-arrangement view across all rendered tracks.
4. Export a non-mutating measure-level context and ask Hermes for a draft expression plan: measure energy, accents, per-track role/shift, and an optional Solo-humanization decision.
5. Explain the draft in chat and require explicit approval. There is no model call or automatic fallback in the application.
6. On approved apply, rebuild from Guitar Pro. Optional Solo timing is produced inside the existing Guitar renderer **before** keyswitch/pitch-bend emission; the post-mapping expression pass changes velocity only, with instrument-safe clamps. Keyswitches, control events and reserved Hydra velocity zones are immutable.
7. Export separate Logic MIDI tracks and Type-1 `_ALL.mid` from the same processed track objects.
8. Generate rewrite proposals only when explicitly requested. A rewrite proposal is a separate review artifact and never silently mutates the baseline score or MIDI.

## Why articulation mapping comes first

In Shreddage/Hydra, velocity is not only loudness: high values may select Rake or Pinch. Enriching velocity first can accidentally change articulation, and a later mapper can overwrite musical dynamics. Mapping first and then applying a target-aware velocity pass lets the renderer preserve service events and clamp ordinary sustain below reserved zones.

## Creative planning workflow

The creative planner is **not** a coder model. The initial `qwen3-coder-next` experiment is rejected and must not be deployed.

The accepted near-term workflow is conversational Hermes/MCP with human approval:

1. the UI exports the normal target-library MIDI plus a compact measure-level arrangement context without enriching either one;
2. Hermes reads it through MCP using the active creative/reasoning model;
3. Hermes saves a draft expression/rewrite plan and explains the musical decisions in chat;
4. only explicit user approval allows the application to apply the draft to MIDI.

A later optional unattended UI button may use a configurable creative API provider such as OpenAI. Provider credentials stay outside project artifacts. A coder-model endpoint is not a fallback for creative planning; deterministic processing remains only a technical/offline mode.

## Safety contract

- baseline mode remains byte-compatible with the existing converter;
- context preparation and approved expression apply are separate opt-in stages;
- LLM output is untrusted JSON, schema-validated and range-clamped;
- Hermes/MCP stores a draft first; applying it is a separate explicit action;
- no coder model is used as a creative fallback;
- solo humanization is deterministic for a given seed and runs before guitar keyswitch/bend emission, never as a post-mapping timing pass;
- rewrite is proposal-only and off by default;
- the frozen Spring Melody baseline is recorded in `docs/evidence/spring-melody-logic-baseline.json`.
