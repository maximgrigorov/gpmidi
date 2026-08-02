# Third-party transcription models

Phase 3 runtimes are isolated from the stable converter and `reference-time` service. A model is not accepted merely because its repository is public: code license, checkpoint provenance, exact artifact digest and reproducible inference must all be recorded.

## Basic Pitch — accepted bass baseline runtime

- Upstream: <https://github.com/spotify/basic-pitch>
- PyPI: `basic-pitch==0.4.0`
- Code/license: Apache License 2.0; upstream wheel includes `LICENSE` and `NOTICE`.
- PyPI wheel SHA-256: `738adb503aae7fdfc7d1e1511aa0ce35052315f260a19531ef4c356708425db0`.
- Runtime serialization: bundled ICASSP 2022 TFLite model.
- TFLite model SHA-256: `3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676`.
- Runtime: Python 3.10, `tflite-runtime==2.14.0`, `numpy==1.26.4`; all Python dependencies are hash-locked in `runtimes/basic-pitch/requirements.lock`.
- Scope: isolated bass stems only, MIDI 28–67 (E1–G4 in scientific pitch notation where MIDI 60 is C4).
- Baseline settings: onset 0.5, frame 0.3, minimum note 127.70 ms, melodia enabled, multiple pitch bends disabled.
- Network: the runtime needs no network during inference. Operational invocation must use container/Kubernetes network isolation; removing proxy variables in the generic runner is not equivalent to network isolation.

The model is a measurement baseline, not a restoration dependency. Its output is retained as evidence and must be evaluated against the corresponding Suno bass MIDI in source seconds before any Phase 4 decision.

## Drum candidates — real-input comparison complete, no product-quality pass

### Inverse Drum Machine (IDM)

- Upstream: <https://github.com/bernardo-torres/inverse-drum-machine>
- Inspected commit: `456656868538205ef756912c7cf5b0fd936de8af`.
- Repository license: Apache-2.0.
- The pretrained `idm-44-train-kits` checkpoint is bundled in the same licensed
  repository, not downloaded from a separately licensed model host.
- Checkpoint SHA-256:
  `5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c`.
- Scope: nine drum classes; the spike exports unaligned source-second events
  and General MIDI mappings as evidence JSON.
- Current status: pinned CPU inference was verified on the complete Spring
  Melody drum stem by immutable digest. It emitted 671 events. Against 942
  reference attacks in the shared taxonomy, raw source-time F1@50 ms is
  0.013639. A disclosed offset sensitivity sweep peaks at F1 0.338710 with a
  -140 ms shift, but this is not an accepted timestamp correction. IDM is a
  runtime pass and a musical-quality no-go at the tested checkpoint/settings.

### Omnizart

- Upstream: <https://github.com/Music-and-Culture-Technology-Lab/omnizart>
- Repository code license: MIT.
- Inspected version: `0.6.3`, commit `bcd8cb44d4da66ce87df10b6abee5c35a8cc2886`.
- Checkpoint distribution: upstream `checkpoints-20211001` release; the four
  drum checkpoint file digests are recorded in
  `docs/phase-3-drum-candidate-feasibility.md`.
- Current status: no-go for the current minimal path. Source/checkpoint
  distribution appears MIT-covered, but the upstream setup performs nested
  dependency installs while resolving metadata, so a reproducible hash-locked
  install was not achieved. No inference or image is claimed.

### ADTLib

- Upstream: <https://github.com/CarlSouthall/ADTLib>
- Inspected commit: `8e429f38736e7c8ef94e06314eaa1d1ac7dd30fe`.
- Repository `LICENSE.txt` is BSD-2-Clause, but package classifiers also say
  `Free for non-commercial use`; bundled checkpoint rights/provenance are not
  separately documented.
- Runtime imports Python-2.7-era TensorFlow 1 `tensorflow.contrib.rnn`.
- Current status: no-go as a permissive product candidate pending author
  clarification of licensing/model rights; modern runtime compatibility also
  fails the minimal-path gate.

### ADTOF / ADTOF-pytorch

- Upstream ADTOF: <https://github.com/MZehren/ADTOF>.
- PyTorch port: <https://github.com/xavriley/ADTOF-pytorch>, inspected commit
  `85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9`.
- ADTOF-pytorch has no LICENSE file. Its bundled 3,617,805-byte checkpoint is a
  numeric conversion of the upstream ADTOF weights.
- Checkpoint SHA-256:
  `1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320`.
- Upstream ADTOF declares CC BY-NC-SA 4.0 while `setup.py` also advertises GPLv3.
- Runtime scope: research-only five-class comparator; source-second events for
  kick, snare, tom, merged hi-hat and merged cymbal, with fixed velocity 100.
- Current status: exact-source CPU inference was reproduced on AILab and then
  run on the complete Spring Melody drum stem by immutable digest. It emitted
  1,291 events. Raw source-time F1@50 ms is 0.017017; a disclosed latency
  sensitivity sweep peaks at F1 0.616211 with a -145 ms shift. This makes ADTOF
  the stronger research comparator, but the shift lacks an independently
  justified adapter contract and the licensing boundary still prevents product
  use. Full metrics are in `docs/phase-3-drum-real-input-findings.md`.

Do not use either ADTOF implementation as a production or commercial dependency
without explicit licensing clarification from the authors and legal review.
