# Phase 3 drum real-input findings

Date: 2026-08-02

## Scope and decision question

This report compares two independently runnable drum-transcription adapters on
the same complete Spring Melody isolated drum stem and the corresponding
original Suno drum MIDI. The goal is to separate technical runnability from
musical usefulness and licensing suitability before any Phase 4 restoration
work.

The rewritten Guitar Pro measure grid is not used. Reference and predictions
are compared in source seconds with the MIDI tempo automation applied.

## Fixed input and reference

- drum WAV SHA-256: `debb6cf9bd3236d4a6ca1f89503cc3f7b7ea2577128d2596dd80f7bba105d186`;
- drum WAV size: 58,243,644 bytes;
- original Suno drum MIDI SHA-256:
  `034075af9b4752e80a61ba1f2d8bbedf97e3a0c8a571988aa6282b58edf3dfb8`;
- reference MIDI size: 10,769 bytes;
- reference attacks: 945 total, of which 942 are covered by the shared
  five-class taxonomy;
- excluded reference events: three MIDI note 81 open-triangle attacks, because
  neither adapter exposes that class.

The WAV was uploaded to a hash-addressed Gitea Generic Package and read back
before use. The cluster downloaded the same object and each PipelineRun checked
its SHA-256 before inference.

## Shared taxonomy and evaluator

Both adapters are normalized by General MIDI note, not by their incompatible
internal labels:

- kick: 35, 36;
- snare: 37, 38, 39, 40;
- tom: 41, 43, 45, 47, 48, 50;
- hi-hat: 42, 44, 46;
- cymbal: 49, 51, 52, 53, 55, 57, 59.

The existing one-to-one event matcher reports micro and per-class onset/class
precision, recall and F1 at 25, 50 and 100 ms. Raw source-time metrics are the
primary result. A separate offset sweep from -200 to +200 ms in 5 ms steps is
reported only as latency sensitivity; it is not silently applied to the raw
result.

Machine-readable reports:

- `docs/evidence/phase-3-adtof-spring-evaluation.json`, SHA-256
  `45d20bd00a23d9d4b9e3d023bbbd0adba171b5d9fee61b4f12e5bb8118ccf80e`;
- `docs/evidence/phase-3-idm-spring-evaluation.json`, SHA-256
  `ffb3c8d10b4fc1a6f130d9ffc10df01759eb7cbb97b156ab7c5ec2518a4384eb`.

## ADTOF-pytorch

### Reproducibility evidence

- gpmidi source commit:
  `32897f38c71b0f422a29c686e5cd59e8fd2eeb9c`;
- upstream commit: `85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9`;
- source archive SHA-256:
  `28602a3bd89836240d519396b566966c52b6439e2f3cda61d8a674433b350b56`;
- checkpoint SHA-256:
  `1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320`;
- immutable image digest:
  `sha256:e24301c26f585edbe9fe3d13784639d309ef3734a10eb5735576d7b34a32788e`;
- successful real-input PipelineRun: `phase3-adtof-spring-eval-6p5zp`;
- readback PipelineRun: `phase3-adtof-evidence-readback-rfg2f`;
- events: 1,291;
- events JSON SHA-256:
  `e3a5b4f9d60865be8aa09fdc72e31e3d748d2184b263877c9a360dc5ba88e591`;
- readback size: 151,297 bytes, with the same SHA-256.

Prediction counts are 415 kick, 175 snare, 54 tom, 539 hi-hat and 108
cymbal events.

### Raw source-time metrics

- F1@25 ms: `0.010748`;
- F1@50 ms: `0.017017` (TP=19, precision `0.014717`, recall `0.020170`);
- F1@100 ms: `0.213166`.

The sharp increase at 100 ms indicated a systematic delay. A diagnostic offset
sweep found its best F1@50 ms at -145 ms:

- micro precision `0.532920`;
- micro recall `0.730361`;
- micro F1 `0.616211`;
- 688 true positives;
- matched onset error p50 `11.7 ms`, p95 `19.4 ms` after the diagnostic shift.

Per-class F1 at that best diagnostic offset:

- kick `0.8017`;
- snare `0.9179`;
- tom `0.2524`;
- hi-hat `0.4827`;
- cymbal `0.3382`.

The adapter/upstream port does not declare a fixed 145 ms output correction.
Therefore this corrected score is sensitivity evidence, not the primary metric
and not yet a production timestamp contract.

### License boundary

ADTOF-pytorch remains research-only. The port has no LICENSE file; its bundled
checkpoint is derived from ADTOF weights, while upstream ADTOF declares CC
BY-NC-SA 4.0 and package metadata also mentions GPLv3. It is not a commercial
or production candidate without author clarification and legal review.

## Inverse Drum Machine

### Reproducibility evidence

- gpmidi source commit:
  `9bfed03d1073743a1fb602b1ebce6164d4ffb1e3`;
- upstream commit: `456656868538205ef756912c7cf5b0fd936de8af`;
- checkpoint SHA-256:
  `5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c`;
- immutable image digest:
  `sha256:99c007476bb946fc728dc804b31b1e3a1520eda57bc7475b820085f194acaf72`;
- successful real-input PipelineRun: `phase3-idm-spring-eval-hbchv`;
- readback PipelineRun: `phase3-idm-evidence-readback-9wpbw`;
- events: 671;
- events JSON SHA-256:
  `a82c5273710c76390742c01d975a0f79dc465efc59559e3f74778d8339a5c820`;
- readback size: 84,948 bytes, with the same SHA-256.

Prediction counts are 154 kick, 199 snare, 124 tom, 161 hi-hat and 33
cymbal events.

### Raw source-time metrics

- F1@25 ms: `0.000000`;
- F1@50 ms: `0.013639` (TP=11, precision `0.016393`, recall `0.011677`);
- F1@100 ms: `0.065716`.

The same diagnostic sweep found its best F1@50 ms at -140 ms:

- micro precision `0.407463`;
- micro recall `0.289809`;
- micro F1 `0.338710`;
- 273 true positives.

Per-class F1 at that best diagnostic offset:

- kick `0.4332`;
- snare `0.7535`;
- tom `0.2391`;
- hi-hat `0.0492`;
- cymbal `0.0328`.

IDM is Apache-2.0 and its checkpoint is distributed in the same repository, so
its licensing position is substantially clearer than ADTOF's. Its tested
musical quality is nevertheless weak, especially for hi-hat and cymbal.

## Interpretation and decision

Both candidates are technically reproducible on AILab by immutable digest and
both process the real five-minute Spring Melody drum stem quickly on CPU. That
proves runtime feasibility, evidence preservation and structural output; it
does not prove timestamp correctness or restoration quality.

ADTOF is materially better than IDM on this song after a clearly disclosed
latency sensitivity check, particularly for kick and snare. However:

1. raw source-time scores for both are unacceptable until the systematic delay
   has an independently justified correction contract;
2. ADTOF over-predicts hi-hat/cymbal and misses many toms;
3. IDM substantially under-detects hi-hat/cymbal;
4. ADTOF is non-commercial/research-only under the current evidence;
5. IDM is permissive but not accurate enough for automatic restoration.

**Decision: no automatic drum restoration and no Phase 4 go.** ADTOF is the
best research comparator; IDM remains the only currently runnable permissive
candidate, but is a musical-quality no-go at the tested checkpoint/settings.

## Next minimum experiment

Before reconsidering the gate:

1. independently explain and test the model/audio latency instead of fitting an
   offset on the evaluation set;
2. evaluate at least one additional song/section with the same fixed correction;
3. test threshold calibration on a separate calibration excerpt, especially
   ADTOF hi-hat/cymbal precision and tom recall;
4. keep raw and corrected metrics side by side;
5. continue searching for a permissive modern checkpoint if commercial use is
   required.
