# ADTOF-pytorch research runtime

Pinned CPU comparator for Phase 3 drum transcription.

- Upstream: <https://github.com/xavriley/ADTOF-pytorch>
- Commit: `85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9`
- Source archive SHA-256: `28602a3bd89836240d519396b566966c52b6439e2f3cda61d8a674433b350b56`
- Bundled checkpoint SHA-256: `1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320`
- Output: source-second JSON events for MIDI notes `35, 38, 47, 42, 49`.
- Velocity: fixed at `100`; the model does not estimate velocity.

The runtime verifies the checkpoint before inference and runs as UID/GID 65532.
Inference needs no network once the image is built.

## Compliance boundary

This runtime is **research-only**. ADTOF-pytorch has no LICENSE file and its
bundled checkpoint is a numeric conversion of the upstream ADTOF checkpoint.
Upstream ADTOF declares CC BY-NC-SA 4.0 while its package metadata also mentions
GPLv3. Do not ship or use this runtime commercially without explicit legal and
author clarification.

## Invocation

```bash
python /app/run.py \
  --input /work/drums.wav \
  --output /work/adtof-events.json \
  --checkpoint /usr/local/lib/python3.11/site-packages/adtof_pytorch/data/adtof_frame_rnn_pytorch_weights.pth
```
