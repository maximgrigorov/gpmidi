# Phase 3 Basic Pitch real-input findings

Date: 2026-08-01

## Scope and identity

This spike evaluates an isolated Suno bass stem against the corresponding original Suno bass MIDI. The rewritten Guitar Pro file is not used as timing or note ground truth.

- Source timeline window: `[195.000, 215.000)` seconds
- Window selection: densest 20-second interval in the Suno bass MIDI, 58 reference onsets
- Source bass WAV SHA-256: `bd803bb39b8e197869e2086d54ad93b69eb301259f7b1861f09505a53d5e2b04`
- Source Suno bass MIDI SHA-256: `82250c89783273ce847500a7f9a582e6df74e9e2ea8be3af296f480d2f1b8ed9`
- Extract WAV SHA-256: `d3e5cec64a13fc8c35050f84b9e67f30124d8bb4010295ff7524fd9ccc0cbb1f`
- GP grid used: **no**

The source WAV digest matches the Phase 2 Project API `stem.bass` evidence. The MIDI digest matches the bass source evidence in the persisted Phase 2 report.

## Runtime provenance

- Adapter: Basic Pitch `0.4.0`
- Serialization: TFLite
- Model SHA-256: `3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676`
- Local immutable image ID: `sha256:f5fac828aca074fe7441910b11463e0aad73d44906539a52ef2f81575adb9d8e`
- Runtime controls: `--network none`, read-only rootfs, all capabilities dropped, no-new-privileges, 2 GiB memory, 2 CPUs, 256 PIDs
- Runtime duration through bounded runner: 27.551 seconds
- Output MIDI: 1,526 bytes, 78 predicted events
- Output MIDI SHA-256: `e6687e5855c00d0b81fa9c0754bc33d53d46aed919ccf3cd6daff449e29ce246`

The manual isolated invocation and bounded-runner invocation produced byte-identical MIDI.

## Metrics

Predicted excerpt onsets were shifted by exactly `+195.000 s` before evaluation on the source timeline.

At 50 ms tolerance:

- Exact-pitch: TP 14, FP 64, FN 44; precision 0.1795, recall 0.2414, F1 **0.2059**
- Onset-only: TP 20, FP 58, FN 38; precision 0.2564, recall 0.3448, F1 **0.2941**
- Exact-pitch matched onset error: p50 16.7 ms, p95 49.6 ms

At 100 ms tolerance:

- Exact-pitch F1: **0.2353**
- Onset-only F1: **0.3529**

For the 24 onset-matched pairs at 100 ms, pitch interval counts included:

- exact pitch: 11
- octave down (`-12`): 8
- other intervals: 5

## Decision

**Technical adapter gate: pass.** Installation, pinned model verification, network-isolated inference, timeout/output limits, provenance manifest and MIDI serialization all work.

**Musical quality gate: fail for default Basic Pitch settings.** The baseline over-predicts events and has substantial octave errors. No octave correction, quantization or GP-derived repair was applied, because this phase must measure the model rather than conceal its errors.

Basic Pitch remains a reproducible baseline and should not be promoted to automatic restoration. Next comparison should use at least one drum-specific candidate and, if bass work continues, a parameter sweep or a second bass-capable model evaluated on the same fixed source-seconds window.

## Evidence artifacts

Local evidence root:

`/home/hermes/shared/spring-melody-phase3/`

- Bounded manifest SHA-256: `aae81674bdeea9cb3eddeeeca873e0951c7ffc027e4432f78a5988a60453844a`
- Evaluation report SHA-256: `de465d784f432a4d6bcae1c500626520504e3717d704eb2ecf18b0d1f9a90076`
