# SheetSage2 on-demand service

Private AILab proof of concept for converting an uploaded WAV, FLAC or MP3 file into SheetSage2 lead-sheet artifacts without reserving the GPU while idle.

## Architecture

- `gpmidi-web` streams the upload to the internal FastAPI control plane.
- The control plane persists input/state on `sheetsage2-data` and launches one bounded Kubernetes Job.
- The Job requests one NVIDIA GPU, reads verified model files from the read-only `sheetsage2-models` mount, and runs with Hugging Face/Transformers offline.
- Success is published only after the canonical Type-1 MIDI, ZIP, JSON report and HTML report have been parsed/assembled.
- Job visibility and downloads are scoped by a per-Flask-session owner token. Only its SHA-256 digest is persisted by the service.

## Model provenance and license boundary

The deployment pins exact revisions of:

- `m-a-p/SheetSage2` (`488abe28ef4db3dbb056da19cb49d80f4b14bc61`)
- `m-a-p/MERT-v2-FullSong` (`d8ba1c745e733b3908ce6ad16ebeb17ac7600a42`)

The model sync step verifies the published SHA-256 value of each `model.safetensors` before atomically publishing the cache. Both upstream model cards declare **CC-BY-NC-4.0**. This integration is therefore for non-commercial/private use unless separate rights are obtained. The upstream code/model notices retained in each pinned snapshot remain authoritative.

## Local checks

```bash
python -m pytest -q
python -m ruff check .
```

These checks use a fake transcriber and do not prove CUDA inference. Production-like image build, cache synchronization and bounded real inference run only through the AILab Tekton exact-SHA path.
