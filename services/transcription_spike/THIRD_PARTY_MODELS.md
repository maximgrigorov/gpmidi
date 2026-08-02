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

## Drum candidates — feasibility checked, none accepted

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

### ADTOF

- Upstream: <https://github.com/MZehren/ADTOF>
- Repository license: CC BY-NC-SA 4.0.
- Current status: research-only comparator. It must not become a production or commercial dependency without a separate licensing decision.
