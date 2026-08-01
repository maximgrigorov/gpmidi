# Basic Pitch bass runtime

Pinned CPU/TFLite baseline for Phase 3. This runtime transcribes an isolated bass stem only; it does not mutate GP or existing MIDI assets.

## Build

```bash
docker build --pull=false -t gpmidi/basic-pitch:0.4.0-phase3 .
```

The build verifies the bundled TFLite model against SHA-256 `3db297d54af8e01c6e5618245c956b1d71b6a2b978cb2dedb527173186552676`.

## Isolated invocation

```bash
docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --memory 2g --cpus 2 --pids-limit 256 \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  -v "$WORKDIR:/work" \
  gpmidi/basic-pitch:0.4.0-phase3 \
  --input /work/input.wav \
  --output /work/output.mid
```

AILab must publish and invoke the image by immutable registry digest, not by this local development tag.

## Verified synthetic smoke — 2026-08-01

A two-second 82.406889 Hz E2 sine was generated at 22,050 Hz and passed through the isolated invocation above.

- Local image ID: `sha256:f5fac828aca074fe7441910b11463e0aad73d44906539a52ef2f81575adb9d8e`
- Image size: 212,681,400 bytes
- Image user: `65532:65532`
- Input WAV SHA-256: `24de3dadba2dc5875713aa34a40d5e313535846797099ce45968453c2e180666`
- Output MIDI SHA-256: `21e271a2c08646b910be38af63950ad60ffed3a97ffc3e79f95374b362dfc4d8`
- Output: valid Type-1 MIDI, PPQ 220, 573 bytes
- Detected event: MIDI note 40 (E2), velocity 86
- Runtime summary: `note_events=1`, allowed MIDI range 28–67

This smoke proves installation, model loading, digest verification, audio decode, inference and MIDI serialization. It does not establish musical quality; that requires representative Spring Melody excerpts evaluated against the corresponding Suno bass MIDI.
