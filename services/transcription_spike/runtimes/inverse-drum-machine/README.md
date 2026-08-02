# Inverse Drum Machine drum-transcription spike

This runtime evaluates the transcription head of the official Inverse Drum
Machine (IDM) model without adopting it as a product dependency.

## Pinned provenance

- Upstream: <https://github.com/bernardo-torres/inverse-drum-machine>
- Commit: `456656868538205ef756912c7cf5b0fd936de8af`
- Code and repository-bundled checkpoint: Apache-2.0
- Source tarball SHA-256:
  `d209125b1053dfa1dea19deba0e3bc908761326c5a7253f77b525e34e7cf7827`
- Checkpoint SHA-256:
  `5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c`

The image verifies both source and checkpoint digests while building. Inference
runs as UID/GID `65532:65532` and emits source-second drum events as JSON.
The adapter maps the nine IDM classes to General MIDI notes but does not
quantize, align or promote the output into restored MIDI.

## Container invocation

```bash
python /app/run.py \
  --input /work/drums.wav \
  --output /work/events.json \
  --checkpoint /opt/idm/pretrained/idm-44-train-kits/checkpoints/val-epoch=518-global_step=0.ckpt
```

A successful contract test is not evidence of model quality. Acceptance still
requires real AILab inference on a pinned audio fixture and source-second event
evaluation against its matching reference MIDI.
