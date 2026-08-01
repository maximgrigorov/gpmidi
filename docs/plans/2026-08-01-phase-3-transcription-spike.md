# Phase 3 Transcription Spike Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Measure whether isolated Spring Melody drum and bass WAV stems can produce musically useful MIDI events before building any restoration workflow.

**Architecture:** Add a separate `services/transcription_spike` package and optional model-specific runtime images. Keep model dependencies out of `reference-time` and the stable converter. Evaluate predicted events against the corresponding original Suno MIDI stem in source seconds; do not use the manually rewritten GP as timing ground truth and do not infer correctness from equal measure counts. Phase 3 produces immutable evidence only and never mutates GP/MIDI assets.

**Tech Stack:** Python 3.11, Pydantic 2, mido, pytest; Basic Pitch (Apache-2.0) as the first isolated bass adapter; Omnizart (MIT) as the first drum candidate; ADTOF only as an explicitly non-commercial research comparator because its repository is CC BY-NC-SA 4.0.

---

## Non-negotiable correction to Phase 2 interpretation

The current Spring Melody GP was manually rewritten and consolidated from individual tracks. Its 96-measure grid no longer represents the original Suno MIDI timeline. Therefore:

- a `96 source measures -> 96 GP measures` identity result is not evidence of musical alignment;
- Phase 3 model evaluation uses Suno stem MIDI against the matching WAV stem in source seconds;
- GP is not mutated in Phase 3;
- any later source-to-GP transfer requires content-derived anchors or explicit human anchors and separate acceptance evidence.

## Gates

- **Pre-flight:** exact branch/SHA, clean worktree, model/code/weight licenses recorded.
- **Revision:** every production behavior follows RED -> GREEN -> full relevant suite.
- **Escalation:** request Fable 5 review after the evaluation contract and first real adapter form a concrete diff.
- **Abort:** do not build restoration if representative-section precision/recall and listening evidence are not useful; do not hide poor classes behind a macro average.

### Task 1: Deterministic event and evaluation contracts

**Objective:** Represent reference/predicted events in source seconds and compute deterministic per-class and aggregate onset metrics without any GP-grid assumption.

**Files:**
- Create: `services/transcription_spike/transcription_spike/models.py`
- Create: `services/transcription_spike/transcription_spike/evaluation.py`
- Create: `services/transcription_spike/transcription_spike/__init__.py`
- Create: `services/transcription_spike/tests/test_evaluation.py`
- Create: `services/transcription_spike/requirements.txt`
- Create: `services/transcription_spike/requirements-test.txt`

**TDD sequence:**
1. Failing tests for exact match, tolerance boundary, duplicate predictions, missed references, wrong pitch/class, empty sets, stable ordering, and finite metrics.
2. Run `python -m pytest -q services/transcription_spike/tests/test_evaluation.py` and verify failures are due to missing contracts.
3. Implement immutable events and deterministic maximum-cardinality matching within `(instrument, pitch/class)` groups.
4. Re-run focused tests, then `python -m pytest -q services/transcription_spike/tests`.
5. Commit `feat(transcription): add deterministic event evaluation contract`.

### Task 2: MIDI-to-source-seconds reference extraction

**Objective:** Parse type-0/type-1 Suno MIDI tempo maps into normalized reference events without quantizing to GP measures.

**Files:**
- Create: `services/transcription_spike/transcription_spike/midi_reference.py`
- Create: `services/transcription_spike/tests/test_midi_reference.py`

**TDD sequence:**
1. Generate tiny MIDI fixtures with tempo changes, independent tracks, percussion classes, and bass notes.
2. Verify RED for absolute tick/second conversion and note pairing.
3. Implement extraction using merged absolute-tick tempo events and explicit instrument profile.
4. Verify focused and package suites.
5. Commit `feat(transcription): extract reference events in source seconds`.

### Task 3: Versioned model adapter contract

**Objective:** Execute an external model in a bounded workspace and normalize its MIDI/JSON output while recording provenance.

**Files:**
- Create: `services/transcription_spike/transcription_spike/adapters.py`
- Create: `services/transcription_spike/tests/test_adapters.py`

**Required behavior:** argv lists only (no shell), timeout, output-size cap, isolated directory, deterministic output discovery, SHA-256 of audio/model/output, command/version/runtime/duration recording, sanitized errors, no network during inference.

**TDD sequence:** failing fake-executable tests -> minimal implementation -> focused tests -> package tests -> commit.

### Task 4: Basic Pitch bass runtime

**Objective:** Run a pinned Basic Pitch model against an isolated bass stem and emit normalized notes plus raw MIDI.

**Files:**
- Create: `services/transcription_spike/runtimes/basic-pitch/Dockerfile`
- Create: `services/transcription_spike/runtimes/basic-pitch/run.py`
- Create: `services/transcription_spike/tests/test_basic_pitch_contract.py`
- Create: `services/transcription_spike/THIRD_PARTY_MODELS.md`

**Acceptance:** pinned package/model digest, Apache-2.0 notice, CPU baseline first, bounded representative excerpts before full five-minute inference, machine-readable manifest, output parsed back and evaluated.

### Task 5: Drum candidates

**Objective:** Compare at least two genuinely runnable drum paths on the same excerpts and class mapping.

**Candidates:**
- Omnizart drum checkpoint — MIT code; verify checkpoint provenance/license before retention.
- ADTLib — BSD code; verify bundled model license and modern runtime compatibility.
- ADTOF — research comparator only, clearly marked non-commercial due CC BY-NC-SA 4.0.

Do not add a candidate merely because a repository exists. A candidate counts only after pinned installation, inference, parsed output, license evidence, resource measurements, and reproducible failure/success logs.

### Task 6: Representative real-input evidence

**Objective:** Run bass and drums on short, musically diverse Spring Melody excerpts before full-song processing.

**Evidence per candidate:**
- exact audio/MIDI digests and excerpt bounds;
- package/model/container digests;
- wall time, peak RAM, peak VRAM, CPU/GPU mode;
- per-class TP/FP/FN, precision, recall, F1;
- onset-error p50/p95;
- raw and normalized outputs;
- listening MIDI for Kontakt/Logic without overwriting validated converter outputs.

Report micro and per-class metrics; never use GP measure identity as the oracle. Request Fable 5 review on the implementation and evidence interpretation before Phase 4.

### Task 7: Go/no-go decision

Proceed to restoration only if the useful instrument classes are individually credible on representative sections and manual listening confirms the metrics. If only some classes work, scope Phase 4 to those classes. Preserve all restoration as opt-in/default-off.
