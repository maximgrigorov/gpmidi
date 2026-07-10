# AGENTS.md

## Project overview
- This directory contains a Guitar Pro to MIDI conversion project centered on `gp_to_shreddage.py`.
- The script converts `.gp5` / `.gp` files into separate Type 0 MIDI files per track.
- Conversion logic is already validated by the user and must be treated as stable.
- The primary purpose is preparing Logic Pro friendly MIDI while preserving / translating articulations from the source Guitar Pro arrangement.
- Track classification is based on track names:
  - `GUITAR` -> Shreddage Hydra mapping
  - `BASS` -> Shreddage Darkwall mapping
  - `OTHER` -> notes copied as-is without articulation mapping
- Output is written into a sibling folder named `<source>_midi/`.

## Current code of record
- `gp_to_shreddage.py` is the source of truth for conversion behavior.
- Articulation/keyswitch maps are NOT hardcoded: they live in versioned
  configs `config/articulation_maps/*.yaml` (Hydra 3.5, Darkwall 3.5,
  Shreddage Drums), loaded via `articulation_config.py`. New instrument or
  library version = new YAML file, not code changes. The exporter logs which
  config version was used per track.
- Track types: GUITAR (Hydra), BASS (Darkwall), DRUMS (Shreddage Drums,
  detected by percussion flag via `resolve_track_type`), OTHER (Category A
  universal effects only: staccato gate, accent velocity, hairpin -> CC11).
- Do not silently alter articulation mapping, keyswitch behavior, bend/vibrato/slide handling, timing normalization, or track-type detection without explicit user approval.
- UI / container / packaging work is allowed as long as conversion semantics remain unchanged.

## Working rules for future agents
1. Work directly in this directory unless the user says otherwise.
2. Preserve the existing converter logic; wrap it, import it, or orchestrate it, but do not change its musical behavior unless explicitly asked.
3. Prefer additive changes:
   - web UI
   - API wrapper
   - session storage
   - download helpers
   - containerization
   - documentation
4. Before claiming the app works, run a real conversion with an actual `.gp` or `.gp5` file and verify downloadable MIDI outputs exist.
5. If changing anything around file handling or downloads, verify both:
   - upload -> parse -> render summary
   - per-track MIDI download
6. Keep the UI simple and light-themed unless the user asks for another style.
7. If you need metadata for the UI, derive it from the existing parser/converter flow instead of reimplementing musical logic differently.

## Useful implementation notes
- The converter already exposes reusable functions such as:
  - `detect_track_type`
  - `build_instrument_midi`
  - `build_other_midi`
  - `safe_filename`
- A web wrapper can import the script as a module, provided dependencies are installed.
- Session artifacts should be isolated per upload/session to avoid filename collisions.
- Good UX for this project:
  - drag-and-drop upload
  - visible track list
  - indication of recognized track type
  - what was adjusted / mapped
  - one-click download per MIDI track
  - optional download-all archive

## Definition of done for UI tasks
- User can drag/drop a Guitar Pro file.
- User sees parsed track list and conversion summary.
- User can download individual MIDI tracks.
- App runs in a container on a published port.
- The converter script remains musically unchanged.
