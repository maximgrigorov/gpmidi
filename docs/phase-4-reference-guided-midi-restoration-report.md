# Phase 4 — Reference-guided MIDI restoration

**Spring Melody drum PoC · 2 August 2026**

## Executive conclusion

Phase 4 is technically viable **as a review-first restoration assistant**, not as an automatic replacement for the trusted Guitar Pro → MIDI converter.

The experiment resolved the largest Phase 3 uncertainty: the apparent `−145 ms` ADTOF timing correction is not a hidden delay inside the neural network. An independent audio-versus-MIDI measurement, using no ADTOF predictions, found the same physical offset in every third of the song: the rendered drum WAV is approximately **140 ms later** than the reference MIDI. Therefore an event correction of **−140 ms** is justified for this specific asset pair.

ADTOF threshold calibration improved holdout precision from **63.9% to 75.0%**, while recall fell from **87.6% to 73.9%** and micro-F1 changed only from **73.9% to 74.4%**. On the final untouched section, micro-F1 fell from **60.4% to 54.9%**, driven mainly by hi-hat recall collapsing to 10%. The calibrated profile is therefore an experimental profile for this artifact, **not a global default**.

The implemented restoration layer:

1. reads source-time events;
2. maps each event into the destination Guitar Pro tick grid;
3. creates an immutable candidate patch;
4. requires explicit measure-level approval;
5. writes only a separate Type-1 MIDI overlay;
6. never modifies the source Guitar Pro revision.

The final E2E audition artifacts preserve the original 363-event tempo map, are deterministically reproducible, contain no negative MIDI delta times, and last approximately 300.2 seconds.

**Gate decision:** GO for supervised Phase 4 iteration; NO-GO for model-only automatic replacement.

---

## 1. Why Phase 4 exists

Phase 3 answered “can a drum transcription model detect useful attacks?” but did not yet answer “can those attacks become a safe, editable and musically meaningful MIDI part on the Guitar Pro score grid?”

The difficult part is not merely writing MIDI notes. Three coordinate systems must agree:

- **audio time** — seconds in the rendered WAV;
- **source musical time** — the performance represented by the reference assets;
- **destination score time** — measures and ticks in the Guitar Pro revision.

A transcription event that is correct in seconds can still land in the wrong measure after repeats, gaps or structural differences. A correct drum class can still lose articulation if five model classes are treated as a complete drum vocabulary. And an apparently good score can be misleading if thresholds were tuned on the same fragment used for evaluation.

Phase 4 therefore deliberately treats model output as **candidate evidence**, not authoritative score content.

## 2. Terminology in plain language

### Onset

The instant at which a sound starts. For a drum hit this is the attack transient, not the full ringing duration.

### Latency / timestamp offset

A constant displacement between two timelines. A prediction at `10.00 s` that corresponds to a reference hit at `9.86 s` has a `+140 ms` displacement relative to the reference and needs a `−140 ms` correction.

### Spectral flux

A simple audio transient measure: how quickly the spectrum changes between adjacent frames. Drum attacks usually create sharp positive peaks. It provides an independent clock check because it uses the WAV itself rather than ADTOF predictions.

### Threshold

The minimum model confidence required before an activation becomes an event. Raising it usually removes false hits but may also miss quiet real hits.

### Precision

Of all predicted hits, the fraction that were correct: `TP / (TP + FP)`. High precision means less MIDI cleanup.

### Recall

Of all real reference hits, the fraction recovered: `TP / (TP + FN)`. High recall means fewer missing notes.

### F1

The harmonic mean of precision and recall. It is useful when both matter, but it can hide different failure modes; therefore precision and recall are reported separately.

### Calibration, holdout and untouched split

- **Calibration (0–100 s):** thresholds may be selected here.
- **Holdout (100–200 s):** used once to see whether the selection transfers.
- **Final untouched (200–301.9 s):** not used for selection; this is the strongest generalization check.

This separation prevents **data leakage**: silently tuning settings using the same data later presented as proof.

### Tick and PPQ

A tick is an integer position on a MIDI musical grid. PPQ (“pulses per quarter note”) is the grid resolution. The overlay uses **960 PPQ**, so one quarter note occupies 960 ticks.

### Tempo map

A sequence of tempo changes. Spring Melody has 363 tempo events around 75 BPM. A MIDI file with correct notes but a fake constant 120 BPM is not a valid standalone audition file. The final artifacts copy and rescale the original tempo map.

### Type-1 MIDI

A Standard MIDI File with multiple tracks: here one metadata/tempo track and one drum track.

### Articulation

A distinct playing technique or drum sound, represented here by a MIDI pitch/class. The reference MIDI contains 13 pitches; ADTOF exposes only five broad classes. Five-class detection therefore cannot reconstruct the full expressive drum vocabulary by itself.

### Candidate patch

A reviewable JSON proposal. It records source time, destination measure/tick, pitch, velocity, confidence and source identities. It is not an applied edit.

### Overlay

A separate MIDI file containing approved candidates. It can be auditioned or imported without mutating the Guitar Pro source.

### Mapping confidence

Confidence that a source-time region corresponds to a destination score measure. Model confidence answers “is this probably a drum hit?”; mapping confidence answers “is this probably the correct destination region?” Combined candidate confidence is their product.

## 3. Safe gate and rollback baseline

The trusted baseline remains the existing Guitar Pro → MIDI path on `origin/main` at:

```text
aac15ba3afb3cf7eeb3c702ed03d5fbe39c31035
```

The experimental work is isolated on `feat/transcription-spike`. Before this report, the published feature head was:

```text
cb41901ea92c742bd0f79cdb57e0e354b4c475a7
```

Safety invariants enforced in code:

- Guitar Pro input is never modified;
- `default_action = review_required`;
- candidates begin with `status = proposed`;
- no decision produces an empty overlay;
- source gaps and low-confidence mappings cannot become candidates;
- output is a separate Type-1 MIDI file;
- velocity randomization/humanization is not applied;
- the legacy converter remains the default and rollback.

For the downloadable audition files, all mapped measures were mechanically approved only to exercise the complete export path. This is **not** a claim that a human approved every musical decision.

## 4. Independent latency experiment

### Question

Was the Phase 3 `−145 ms` optimum caused by ADTOF internals, or by the relationship between the supplied WAV and MIDI?

### Model-side audit

ADTOF operates on a 100 fps grid: one frame every 10 ms. Its centered STFT and convolution path do not explain a stable 145 ms shift, and peak picking does not intentionally add 145 ms.

### Independent measurement

The WAV was transformed into positive spectral flux. The algorithm then sampled that audio-only transient score around reference MIDI event times while sweeping candidate lags. **ADTOF predictions were not used.**

Results:

- `0–100 s`: best lag `+140 ms`, 176 reference events;
- `100–200 s`: best lag `+140 ms`, 410 reference events;
- `200–301.9 s`: best lag `+140 ms`, 357 reference events.

The same result appears in all three independent windows. The top lag is also consistently stronger than the next 10 ms bin. This stability supports an asset-level WAV↔MIDI offset rather than model latency.

### Sign convention

The audio transient occurs about 140 ms **after** the MIDI timestamp. To place model events on the MIDI timeline, subtract 140 ms:

```text
corrected_event_time = predicted_audio_time − 0.140 s
```

The earlier ADTOF sweep preferred `−145 ms`; a 5 ms discrepancy is below the model’s 10 ms frame spacing and is not evidence for a separate model delay.

### Limitation

The correction is justified for this exact WAV/MIDI asset pair. It must not become a universal ADTOF constant without repeating the independent clock check on other assets.

## 5. Threshold calibration without leakage

Threshold order:

```text
kick, snare, tom, hi-hat, cymbal
```

Baseline profile:

```text
0.22, 0.24, 0.32, 0.22, 0.30
```

Selected on the calibration window only:

```text
0.44, 0.36, 0.12, 0.54, 0.50
```

Selection objectives reflected cleanup priorities:

- kick/snare: balanced F1;
- hi-hat/cymbal: precision-weighted F0.5 to reduce floods of false hits;
- tom: recall-weighted F2 because the baseline missed most toms.

### Holdout result (100–200 s)

- precision: `0.6388 → 0.7500` (**+11.1 percentage points**);
- recall: `0.8756 → 0.7390` (**−13.7 points**);
- F1: `0.7387 → 0.7445` (**+0.6 points**).

Notable classes:

- hi-hat precision: `0.464 → 0.659`, but recall `0.745 → 0.403`;
- cymbal precision: `0.520 → 0.727`, but recall `0.813 → 0.500`;
- tom recall: `0.500 → 0.556`, while precision fell `0.474 → 0.357`;
- snare remained excellent: F1 `0.970 → 0.976`.

### Final untouched result (200–301.9 s)

- precision: `0.5287 → 0.5823` (**+5.4 points**);
- recall: `0.7034 → 0.5198` (**−18.4 points**);
- F1: `0.6036 → 0.5493` (**−5.4 points**).

The largest failure is hi-hat:

- precision `0.420 → 0.320`;
- recall `0.544 → 0.100`;
- F1 `0.474 → 0.152`.

Tom and cymbal improve in some respects, but the full profile does not generalize. The useful lesson is not “calibration failed”; it is that **a single global threshold per class is too coarse for this song’s changing sections**, especially for hi-hat.

### Decision

The selected thresholds are retained as an evidence-backed experimental profile used to create one audition artifact. They are **not promoted to production defaults**. Next calibration should use several songs/sections or section-aware thresholds, and must preserve a fresh untouched test set.

## 6. What Phase 4 actually implements

For every source event inside an accepted structural mapping:

1. find the corresponding source interval and GP tick interval;
2. compute the linear scale from seconds to destination ticks;
3. preserve the event’s relative position inside that interval;
4. preserve MIDI pitch/articulation and velocity when available;
5. combine event confidence with mapping confidence;
6. emit an immutable candidate with a deterministic ID.

Conceptually:

```text
target_tick = gp_start
            + round((event_seconds − source_start)
            × (gp_end − gp_start)
            / (source_end − source_start))
```

Mappings marked as gaps, unsupported mapping types, missing pitches and mappings below confidence `0.5` are rejected rather than guessed.

### Velocity policy

Two explicit strategies exist:

- `preserve`: retain source velocity;
- `confidence`: deterministic `30 + 97 × confidence`, clamped to MIDI 1–127.

The second strategy makes an ADTOF audition less flat, but confidence is not the same as musical dynamics. It remains experimental. No random timing or velocity humanization is introduced.

### Tempo-map correction discovered during verification

The first generated overlays had a constant 120 BPM metadata event. Their ticks were structurally correct for DAW import into an existing tempo project, but standalone playback was too fast. Verification caught this presentation-layer defect.

The compiler now accepts a reference tempo MIDI, copies tempo/time-signature/key-signature metadata, and rescales metadata ticks from 480 PPQ to 960 PPQ. The final artifacts contain all **363** source tempo events and play for approximately **300.2 s**, close to the source performance length.

## 7. E2E artifacts

### ADTOF calibrated restoration — experimental model candidate

- mapped notes: **872**;
- mapped measures containing events: **78**;
- pitch vocabulary: **5** (`35, 38, 42, 47, 49`);
- class counts: kick 402, snare 168, hi-hat 158, tom 92, cymbal 52;
- velocity range: **44–118** using deterministic confidence mapping;
- mapping-confidence median: approximately **0.783**;
- duration: **300.193 s**;
- tempo events: **363**;
- SHA-256: `1767310b872f40b7e63c54c17045b6cde32e83cc7a9e5d2c69d53a271bcab64b`.

Interpretation: this is the interesting ML result. It demonstrates that model attacks can be corrected, mapped onto the score grid and exported safely. It is not yet a realistic final drum performance because five classes cannot preserve the source’s full articulation vocabulary and the threshold profile fails to generalize in the last section.

### Source-MIDI transfer — control / upper-bound path

- source notes: **945**;
- mapped notes: **939**;
- safely rejected: **6** beyond accepted mapping coverage;
- mapped measures containing events: **80**;
- pitch vocabulary: **13**;
- velocity range: **51–51**, preserved from source;
- duration: **300.200 s**;
- tempo events: **363**;
- SHA-256: `fd6381752e2dbb4f98a83e0bd8467840fbab5276e74e31045dc93a84ad409730`.

Interpretation: this is not an ML win and must not be confused with ADTOF output. It is a control proving that the Phase 4 transfer layer can retain a much richer articulation vocabulary when the source events contain it. It also reveals that “realistic” dynamics cannot be recovered from this particular reference MIDI because every source velocity is 51.

### Downloads

- ADTOF MIDI: `http://192.168.40.254/shared/gpmidi-phase4/spring-drums-adtof-calibrated-restoration.mid`
- ADTOF candidate patch: `http://192.168.40.254/shared/gpmidi-phase4/spring-drums-adtof-calibrated-patch.json`
- Source-transfer MIDI: `http://192.168.40.254/shared/gpmidi-phase4/spring-drums-source-midi-restoration.mid`
- Source-transfer patch: `http://192.168.40.254/shared/gpmidi-phase4/spring-drums-source-midi-patch.json`

## 8. Verification performed

- transcription-spike test suite: **61 passed**;
- Ruff: **all checks passed**;
- Python bytecode compilation: passed;
- actual CLI E2E run for both artifacts: passed;
- second independent rebuild: byte-for-byte identical patches and MIDI files;
- MIDI type: 1;
- PPQ: 960;
- tempo events: 363 in each artifact;
- negative delta times: 0;
- downloadable files regenerated from the current code.

The initial test attempt accidentally used a virtual environment without Pydantic and failed during collection. This was an environment selection error, not a product regression. Rerunning in the project-capable environment produced 61/61 passing tests.

## 9. What we learned musically

1. **Timing is now the least mysterious part.** The 140 ms correction has an independent physical explanation and remains stable across the song.
2. **Detection quality is section-dependent.** A global hi-hat threshold that looks cleaner in one section can erase most hits in another.
3. **Five classes are a bottleneck.** The control has 13 pitches, while ADTOF emits five. Full articulation restoration requires either a richer model, post-classification, or transfer from a richer symbolic reference.
4. **Confidence is not expression.** Turning confidence into velocity creates audible variation but not necessarily intentional accents or groove.
5. **Reference-guided mapping is valuable even when ML is imperfect.** It isolates uncertain model events from the trusted score and makes review local, reversible and deterministic.

## 10. Go / no-go / rollback

### GO

- continue Phase 4 as a supervised candidate-generation workflow;
- use the −140 ms correction for this identified Spring Melody WAV/MIDI pair;
- retain class-specific thresholds as an experiment profile;
- review candidates measure by measure;
- pursue richer articulation and dynamics separately from onset timing.

### NO-GO

- do not auto-apply model output to Guitar Pro;
- do not promote this threshold profile globally;
- do not describe the five-class ADTOF artifact as a complete realistic drum part;
- do not treat confidence-derived velocity as recovered performance dynamics;
- do not remove the existing Guitar Pro → MIDI path.

### Rollback

Rollback is immediate: ignore/delete the candidate patch and overlay and continue using the trusted `origin/main` converter baseline. No source GP revision or production data is modified by Phase 4.

## 11. Recommended next experiment

The next highest-value slice is deliberately narrow:

1. choose 8–12 musically diverse measures, including the failing late hi-hat section;
2. have a human mark accepted/missing/extra hits and articulation corrections;
3. compare three sources on exactly those measures:
   - baseline Guitar Pro/MIDI;
   - five-class ADTOF candidates;
   - source-MIDI transfer control;
4. add a richer drum articulation classifier or deterministic contextual remapping only where five-class collapse is proven harmful;
5. evaluate edit time per measure, not only F1;
6. keep humanization opt-in and drums-only.

The practical success criterion should be: **does the candidate overlay reduce manual editing time while preserving score safety?** That is more aligned with the PoC goal than chasing a single aggregate F1 number.
