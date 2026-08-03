# ADR: Logic arrangement processing order

**Status:** accepted  
**Date:** 2026-08-03

## Decision

The forward product path is the accepted Guitar Pro score. Audio-onset recognition and the pmetal experiment are frozen as historical evidence and are not inputs to the product pipeline.

Processing order:

1. Parse Guitar Pro and preserve its bar/rhythm/tempo identity.
2. Resolve the target instrument and map Shreddage/Hydra articulations, keyswitches, pitch-bends and reserved velocity zones.
3. Build one compact whole-arrangement view across all rendered tracks.
4. Ask the LLM for a validated expression plan: measure energy, accents, per-track role/shift, and optional solo-humanization ranges.
5. Apply musical velocity and solo microtiming **after** articulation mapping, with instrument-safe clamps. Keyswitches, control events and reserved Hydra velocity zones are immutable.
6. Export separate Logic MIDI tracks and Type-1 `_ALL.mid` from the same processed track objects.
7. Generate rewrite proposals only when explicitly requested. A rewrite proposal is a separate review artifact and never silently mutates the baseline score or MIDI.

## Why articulation mapping comes first

In Shreddage/Hydra, velocity is not only loudness: high values may select Rake or Pinch. Enriching velocity first can accidentally change articulation, and a later mapper can overwrite musical dynamics. Mapping first and then applying a target-aware velocity pass lets the renderer preserve service events and clamp ordinary sustain below reserved zones.

## Safety contract

- baseline mode remains byte-compatible with the existing converter;
- expression processing is opt-in per job;
- LLM output is untrusted JSON, schema-validated and range-clamped;
- failure or malformed output falls back to a deterministic conservative plan;
- solo humanization is deterministic for a given seed;
- rewrite is proposal-only and off by default;
- the frozen Spring Melody baseline is recorded in `docs/evidence/spring-melody-logic-baseline.json`.
