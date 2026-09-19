# Agent workflow: reusable power-metal MIDI enrichment

This guide is the clean-context entry point for an agent running on Linux. It
describes how to analyze and enrich a new song without copying decisions from
Spring Melody into unrelated material. The target repertoire is power metal:
ballads, straight eighth-note passages, gallops, melodic leads and dense rhythm
guitars.

Read `CODEX.md`, `AGENTS.md` and `LESSONS.md` first. The stable Guitar Pro
converter remains the source of truth. Experimental note edits and expression
belong in a separate candidate and must never overwrite the accepted baseline.

## 1. Choose the correct layer

| Request | Work layer | Safety mode |
|---|---|---|
| Preserve notes; vary pick velocity or existing CC1/PB | `solo_expression_humanize.py` | Notes, timing, durations, service notes and authored PB anchors immutable |
| Add left-hand string motion to long Lead notes | `--finger-vibrato` | Long monophonic notes only; authored bends excluded |
| Add/remove notes, fix drums, rewrite a rhythm figure | Agent-authored full candidate | Explicit musical edit; report every changed track and bar |
| Import the result into Logic | `midi_patch_export.py` | Conductor plus only semantically changed tracks |

Do not put a song-specific bar number, note or section into reusable code. A
requested local edit belongs in the agent's candidate-building script or plan,
which should be kept with the listening artifacts outside Git.

## 2. Analyze before editing

For every input, establish these facts from the file rather than from the task
description:

1. MIDI type, PPQ, tempo/time-signature map and exact track names.
2. Which track is Lead/Solo, Rhythm, drums, bass and harmonic support.
3. Note density and rests by bar; long monophonic Lead spans; polyphonic spans.
4. Existing non-zero pitch-wheel curves, CC1 envelopes and articulation service
   notes. An authored bend is not an empty place for automatic vibrato.
5. For Hydra, the current articulation state around every proposed edit. A
   Harmonics or Mute keyswitch must be followed by the correct Sustain/Mute
   state; do not assume Kontakt resets between playback runs.
6. Whether the target instrument actually sounds the requested articulation.
   A structurally valid harmonic outside the loaded sample range can be silent.
7. Baseline hashes and note/event fingerprints before producing variants.

If a rendered stem is available, use it as listening evidence for loudness,
attack and articulation. Do not derive a rigid tempo map from a drifting Suno
stem and do not treat audio transcription as note authority.

## 3. Lead/Solo expression variants

`solo_expression_humanize.py` works on an already target-mapped MIDI. It does
not rebuild Guitar Pro and does not alter other tracks.

Example, using Hydra's verified ±7-semitone pitch-bend range:

```bash
python solo_expression_humanize.py baseline_ALL.mid lead-natural.mid \
  --track-name "Lead Guitar" \
  --finger-vibrato --finger-vibrato-style natural \
  --pitch-bend-range 7 --seed 23 \
  --changed-tracks-only \
  --manifest lead-natural.json
```

The finger-vibrato pass is deliberately unlike a regular LFO:

- it starts after a randomized delay;
- depth and cycle length vary deterministically with the seed;
- the sharp excursion is larger than the tiny flat-side relaxation;
- it skips short notes, chords/overlaps and spans with authored PB motion;
- it returns PB to zero before note-off;
- it preserves every existing PB anchor.

Styles are listening alternatives, not genre labels:

| Style | Use |
|---|---|
| `subtle` | Ballad sustain behind vocals; sparse and shallow |
| `natural` | Default melodic power-metal Lead |
| `expressive` | Exposed held notes and final sustains; audition carefully |

`--pitch-bend` has a different purpose: it densifies movement *between existing
anchors*. It does not invent finger vibrato. `--modulation` varies existing CC1
envelopes; in Hydra CC1 is the amount of the instrument's internal vibrato, not
the pitch waveform. Combine dimensions only as an explicit A/B experiment.

Recommended first listening set:

```bash
for spec in subtle:17 natural:23 natural:31 expressive:29; do
  style=${spec%:*}; seed=${spec#*:}
  python solo_expression_humanize.py baseline_ALL.mid "lead-${style}-${seed}.mid" \
    --track-name "Lead Guitar" --finger-vibrato \
    --finger-vibrato-style "$style" --pitch-bend-range 7 --seed "$seed" \
    --changed-tracks-only --manifest "lead-${style}-${seed}.json"
done
```

Never solve a bad harmonic or a missing final bend by adding automatic vibrato.
First repair the articulation/note/range error, then enrich the surviving long
note.

## 4. Rhythm guitar and drums

These are arrangement edits, so they belong in an explicit candidate rather
than the expression-only Solo pass.

Rhythm guitar:

- do not blanket an entire underlay with Palm Mute;
- retain short rests before transitions and Solo entrances;
- for straight power-metal drive, down-picked eighths are a distinct option
  from sixteenth-note chug and gallop;
- use Mute only for the intended riff, then emit Sustain before an open chord or
  ringing underlay;
- preserve a deliberate exposed Lead gap unless the request explicitly asks to
  fill it;
- provide generic-guitar and Hydra variants when audition targets differ.

Drums:

- default to a balanced pass: keep authored kick/snare and fills, fill only
  conspicuous timekeeper gaps with hats/cymbals;
- avoid inserting regular hats through an existing fill;
- make a strict straight-rock version only as a comparison;
- report added hits separately from velocity/timing humanization;
- humanization and composition are separate changes and should be auditioned
  separately before combining.

Power-metal variant names should say what changed, for example:

- `drums-balanced-hats`
- `drums-straight-rock`
- `rhythm-downpicked-eighths`
- `rhythm-gallop`
- `lead-natural-finger-pitch`
- `lead-expressive-final-sustain`

## 5. Package only changed tracks

An agent may work most safely with a complete baseline and a complete candidate.
Afterward, export a Logic-friendly patch:

```bash
python midi_patch_export.py baseline_ALL.mid candidate_ALL.mid candidate_PATCH.mid \
  --manifest candidate_PATCH.json
```

The utility requires stable track count, order and names. It compares absolute
event semantics and writes the candidate conductor plus only changed tracks.
The manifest records source indices, event counts and whether note identity
changed.

For expression-only work, fail if notes or note timing changed:

```bash
python midi_patch_export.py baseline_ALL.mid candidate_ALL.mid expression_PATCH.mid \
  --require-note-identity --manifest expression_PATCH.json
```

Do not use `--require-note-identity` for an explicitly requested riff, passage
or drum-hit rewrite; the manifest must instead state that note identity changed.

## 6. Required artifact report

Each listening variant should have a JSON manifest and a short human report:

- baseline and candidate SHA-256;
- seed and every enabled dimension/style;
- exact changed track names and source indices;
- bars/sections touched by compositional edits;
- note events added/removed/repitched versus controller-only changes;
- articulation resets inserted or repaired;
- pitch-bend range assumed by the MIDI and required in Kontakt;
- structural checks run and their result;
- known listening questions, such as a potentially silent harmonic.

Generated GP/MIDI/audio files and per-song helper scripts are listening
artifacts, not repository fixtures. Keep only reusable code, tests and durable
measured lessons in Git.

## 7. Verification and handoff

Minimum local gate for these utilities:

```bash
python -m pytest -q test_solo_expression_humanize.py test_midi_patch_export.py
python -m ruff check solo_expression_humanize.py midi_patch_export.py \
  test_solo_expression_humanize.py test_midi_patch_export.py
git diff --check
```

Then reload every produced MIDI with `mido`, run the applicable
`verify_midi.py` checks and verify note-on/note-off balance. Structural checks
cannot judge musicality: the final gate is still an A/B render through the same
Logic/Kontakt instruments and user listening approval.
