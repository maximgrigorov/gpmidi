# AGENTS.md

> **Прочти [LESSONS.md](LESSONS.md) перед тем, как трогать тайминг, динамику,
> артикуляции, бенды или вибрато.** Там собрано то, что выяснено ИЗМЕРЕНИЕМ и
> стоило времени. Почти каждая правдоподобная гипотеза в этой области
> оказывается неверной — в том числе гипотезы, записанные в этом файле.
>
> **Для клонирования, локального запуска, архитектуры, credential contracts,
> exact-SHA deployment, live-проверки и rollback используй
> [docs/CODEX_PROJECT_GUIDE.md](docs/CODEX_PROJECT_GUIDE.md).**

## Project overview
- This directory contains a Guitar Pro to MIDI conversion project centered on `gp_to_shreddage.py`.
- The script converts `.gp5` / `.gp` files into separate Type 0 MIDI files per
  track plus one combined Type 1 `<base>_ALL.mid`.
- Conversion logic is already validated by the user and must be treated as stable.
- The primary purpose is preparing Logic Pro friendly MIDI while preserving / translating articulations from the source Guitar Pro arrangement.
- Track classification uses instrument/percussion metadata plus track names:
  - `GUITAR` -> Shreddage Hydra mapping
  - `BASS` -> Shreddage Darkwall mapping
  - `DRUMS` -> Shreddage Drums mapping
  - `OTHER` -> universal Category A effects only, otherwise notes copied as-is
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

## Humanization (drums) — opt-in, approved 2026-07-15
- `--humanize` synthesizes drum velocity + micro-timing; `--ghost-notes` also
  ADDS snare ghost notes. Both are OFF by default: without flags the export is
  byte-for-byte identical to before (verified against a baseline run).
- Profiles are versioned configs, same philosophy as articulation maps:
  `config/humanize_profiles/*.yaml`, loaded via `humanize.py`. New genre or
  instrument = new YAML, not code changes. The profile used is logged per track.
- **Why this exists (measured, do not re-litigate from intuition):**
  - Guitar Pro stores only 8 dynamic levels (PPP..FFF). For percussion ApolloTab
    reports `beat.dynamics = None` and `note.velocity = 95` for *every* note —
    the drum track arrives as a flat line. The 20 MF beats present in the GPIF
    never survive. So velocity is SYNTHESIZED, not transferred: there is
    nothing to preserve.
  - Notation cannot express micro-timing at all, so it is synthesized too
    (typical live-drummer figures, 10-30 ms).
  - Commercial "live groove" MIDI packs are not a usable source: measured on
    "95 BPM - Nordic Knights", true jitter is std 2.46 ms with 47% of notes
    exactly on the grid. Their advertised "11-88% off-grid" is 16% triplet
    sextuplets (a subdivision, not feel) plus sub-3ms dither. Do not add such a
    dataset to the repo — it is paid content and contributes nothing anyway.
  - `pct_off_grid16` is a misleading metric: it counts triplets as liveness and
    happily reports 54% on a fully quantized file. Use `jitter_ms` (deviation
    from the combined 16th+triplet grid, in milliseconds) instead.
- **Ordering inside `humanize_drums` is load-bearing**: ghost notes are inserted
  BEFORE the velocity/timing passes. If added after, they sit exactly on the grid
  (100+ mechanical notes in an otherwise humanized track) and the fill-detection
  count is computed from already-shifted ticks, so bars near a barline are
  misclassified. Both were real bugs; keep the order.
- Ghost notes are drums-only by design. On a distorted guitar a quiet extra note
  is mud, not life — the guitar equivalent is pick accent and palm-mute depth,
  which is a different mechanism. Do not generalize `ghost_notes` to other tracks.
- Tick-based values in profiles must be expressed as fractions of a 16th
  (`min_gap_frac16`, `max_shift_frac16`): the converter runs at tpb=960 and the
  standalone script at 480, so absolute ticks silently mean different things.
- Profiles are per instrument and NOT interchangeable — `drums_metal`,
  `guitar_metal`, `bass_metal`. Copying one onto another instrument is a bug:
  - GUITAR: velocity is an ARTICULATION SWITCH, not loudness. On Hydra's sustain
    120-126 = Rake, 127 = Pinch. The cap comes from `articulation_maps`
    (`sustain.vel_max`), never from the humanize profile. The drum numbers
    (accent +6, std 6) on F=95 peak at exactly 119 against a 119 cap — one unit
    of headroom, safe by luck, not by design.
  - GUITAR timing cannot be a post-pass: the keyswitch is emitted `KS_LEAD_MS=15`
    before the note, so a note shifted back would land before its own KS. The
    shift is computed in `build_instrument_midi` BEFORE KS/PB emission, and ties
    plus `LEGATO_OVERLAP_MS` overlap derive from the same shifted `start_tick` —
    hence per-BEAT, not per-note.
  - Strum direction is by 16th-index parity, not metrical class. Parity gives
    all-downstrokes on 8ths (metal downpicking) and true down-up alternation on
    16ths; the metrical-class version produced three upstrokes in a row.
  - BASS (Darkwall) has NO velocity zones at all (no Rake/Pinch articulations),
    and strum is disabled: the part has 564 single notes and exactly one chord.

## Vibrato and bend (changed 2026-07-15 with explicit user approval)
- `VIBRATO_MODE = "envelope"`. CC1 on Shreddage is the **Vibrato Amount knob**
  (depth), not the waveform — the instrument oscillates the pitch itself. The old
  `"sine"` mode sent a 5.5 Hz sine on CC1 and thus spun the depth knob 5.5×/sec
  (206 reversals on Solo Guitar, up to 15 within a single note). Flip the constant
  back to `"sine"` to revert.
- `pitch_bend_range: 7` in `shreddage_hydra_3.5.yaml`. **This MUST equal the
  PITCH BEND RANGE knob in the Kontakt preset** — Kontakt need not honour the RPN
  that `emit_pitch_bend_range_rpn` sends. The score contains bends of 2/4/6
  semitones; at the old range of 2 anything deeper pinned at 8191 (14 events),
  rendering as rise + PLATEAU + jump — the "staircase" the user hand-fixed in
  Logic. Darkwall's range is still the unverified default of 2.
- Still broken, not yet approved to fix: `SLIDE_BEND_ST = 2.0` bends a fixed
  2 semitones toward the next note instead of gliding to its pitch, and the whole
  `fx_keyswitches` block (incl. `legato_slide_to_next`, the proper tool for
  slides up to 12 semitones) is dead config, referenced nowhere in the code.

## Smoke verification
- `verify_midi.py` runs automatically after each track (`--no-verify` disables).
  It checks invariants on the produced artifact, not musicality. Every check is a
  fossilised real bug — see the module docstring. ERROR fails the summary; WARN
  and INFO are advisory.
- A check that never fires is worthless: validate changes against the
  pre-fix artifacts, which still contain BEND_CEILING and CC1_PROPELLER.
  Two earlier versions of the CC1 check silently passed the very file they were
  written for, because the reversal count was divided by the wrong denominator.
  Count reversals WITHIN a single note.
- `PB_LEAK` must not fire on pre-bends: a pre-bend legitimately attacks with
  PB != 0, its curve starting on the note's own tick. Only a stale value (last PB
  point more than a 16th behind) counts as a leak.

## Combined export
- Alongside the per-track files, both the CLI and the web app emit one Type 1
  `<base>_ALL.mid` with every track — drag one file into Logic instead of
  thirteen. It is assembled by `build_combined_midi` from **the same MidiTrack
  objects** that go into the per-track files, so the two cannot diverge.
- Channel stays 0 on all tracks, matching the per-track files the user has
  already validated. Logic splits a Type 1 file by TRACK, not by channel, and the
  targets are Kontakt instruments — GM channel 10 is irrelevant to them.
- The web app filters empty tracks out of the combined file
  (`is_empty_export_track`); the CLI does not, mirroring its per-file behaviour.

## Accepted forward baseline — 2026-08-04
- The user reviewed `_ALL.mid` from `Spring_Melody_Per_Track_Experiment_Clear_Solo.zip`
  and gave it an unambiguous GO. This is the authority for forward expression
  and Solo-humanization work. Per-track filesystem timestamps are not acceptance
  evidence; compare the Type-1 `_ALL.mid` and hashes instead.
- Provenance, hashes and structural fingerprints are frozen in
  `docs/evidence/spring-melody-expression-baseline.json`.
- The Replay Fixed Clear Solo package is an acceptable fallback, not the baseline.
- Solo timing authority is the clear Solo with accepted GP played offsets. Future
  humanization must not grid-snap or alter pitches, note count, onset ticks or
  durations. Velocity, pitch-bend shaping and CC1 modulation are independent,
  opt-in A/B dimensions.
- `solo_expression_humanize.py` is the safe post-mapped experiment path. It
  operates on an accepted MIDI, requires at least one explicit dimension flag,
  excludes Hydra service notes from velocity processing, preserves every
  existing pitch-bend anchor, and fails closed on note/service invariants.
- `docs/evidence/spring-melody-solo-humanization-v1.json` is listening-review
  evidence only. It does not supersede the accepted baseline until the user gives
  an explicit musical GO.

## Fret-hand cost (guitar) — opt-in, approved 2026-09-24
- `--fret-hand-cost` (CLI, only together with `--humanize`; the parser rejects it
  alone) delays the attack of a guitar beat when the fretting hand moves:
  `beat_hand_position` (median fretted fret, the same value `fret_noise` uses).
  Without the flag every export is byte-for-byte identical: 330 MIDI files,
  4 inputs x 6 flag combinations, compared against `origin/main`.
- Numbers live in `config/humanize_profiles/guitar_metal.yaml` (`fret_hand_cost`,
  config_version 2); the position-change threshold is reused from
  `shreddage_hydra_3.5.yaml` (`fret_noise_on_hand_shift.min_fret_shift`), never
  duplicated. Travel time is in ms (physical, tempo-independent); only the cap is
  `*_frac16`. The user approved ms over frac16 on 2026-09-24.
- Load-bearing rules, each measured (`docs/evidence/fret-hand-shift-input.json`):
  - only the ATTACK moves, later only; note ends stay, so a delayed note can never
    overlap the next attack (Hydra would play it legato);
  - hammer/pull and slide-to TARGETS are never delayed: they are not re-picked,
    and a delayed target would always break the legato overlap (LESSONS.md p.15);
  - silence before the attack is credited to the move (22% of shifts follow a rest);
  - the delay is computed before keyswitch emission; `fret_noise` stays on the
    undelayed tick (same events, same ticks — tested).
- It is a solo-line feature by measurement: hand shifts >= 4 frets on 25% of solo
  beats but 3.1% of rhythm beats (pnd Rhytm: 2 of 873).
- Acceptance is `hand_cost_check.differential` (one-sided Mann-Whitney, shift
  beats vs no-shift beats, legato targets excluded). It must FAIL without the
  feature — quantized and humanize-only — and does (`test_fret_hand_cost.py`;
  1 false pass in 300 seeded humanize-only runs, 300/300 passes with the feature).
- Use `onset_std_ms` (deviation from the notated attack) for guitar timing, not
  `jitter_ms`: the latter counts quantized 32nds as jitter (LESSONS.md p.3).

## Web UI options
- `humanize` / `ghost_notes` / `seed` are POST form fields on `/upload`, all
  optional, all off by default. The ghost checkbox is gated on humanize in JS —
  ghosts only exist inside humanization.
- `fret_hand_cost` is a per-track GUITAR effect (`track_N_fret_hand_cost`); its
  checkbox is disabled until that track's humanize is checked.
- The job page shows a badge for the mode used, and the manifest stores
  `humanize` / `ghost_notes` / `fret_hand_cost` per job, so an old session states
  what produced it.

## Still open (do not "fix" silently — ask first)
- **`--humanize` note ends follow the SHIFTED start** (LESSONS.md p.15, measured
  2026-09-24): 1909 guitar/bass notes lost (the old note_off kills a same-pitch
  successor) and 40-65% of hammer/pull overlaps broken. Fix on branch
  `fix/humanize-note-ends` (`clip_humanized_overlaps` + grid-decided legato),
  1909 -> 0, byte-identical without `--humanize`. It changes approved humanize
  output: merge only with the user's explicit GO.
- Plain export: a hammer onto the SAME pitch gets the 40 ms legato overlap and
  its target is cut to 40 ms (Spring Melody Solo bars 5 and 59, TtN Solo bar
  174). Fixing it changes the notation export — ask first.
- Under `--humanize` notes on DIFFERENT strings can still overlap by a few ms;
  Hydra plays legato on overlap. Unverified in the instrument.
- ~~Initial keyswitch is assumed, not set.~~ **DONE** in commit `4336ce8`: an
  explicit sustain KS is emitted at tick 0 in `build_instrument_midi`, and
  `verify_midi.py` ships the matching `KS_NO_INIT` check. The reason stays on
  record because it is load-bearing: Kontakt does not reset articulation between
  playbacks, so a track whose first section is plain sustain used to inherit
  whatever was selected last (Solo Guitar had notes from bar 8 but its first
  keyswitch at bar 58, so Hydra played the whole first solo on Harmonics).
- `SLIDE_BEND_ST = 2.0` bends a fixed 2 semitones toward the next note instead of
  gliding to its pitch; `fx_keyswitches.legato_slide_to_next` (the proper tool,
  up to 12 semitones) is dead config referenced nowhere.
- Darkwall `pitch_bend_range: 2` is still the unverified manual default.
- `<Note>` carries `RelativeVelocity` (191 notes) and `Accent` (18) as child
  elements, not Properties. Unverified whether ApolloTab surfaces them — if not,
  hand-written per-note dynamics are being dropped on the floor.
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

## Playable tabs (opt-in)
- `playable_tabs.py` builds clean score-MIDI directly from parsed GUITAR/BASS tracks, then runs both vendored gtrsnipe and installed tuttut. It must never consume the normal Shreddage MIDI because that contains KS/PB/CC/humanization.
- Upload and CLI paths also create ASCII/PDF tabs and a post-verified `_refingered.gp`/`.gp5`; original pitches and rhythm are hard invariants. Regeneration parameters are validated only through `TUNABLE_PARAMS`.
- gtrsnipe core is vendored under `third_party/gtrsnipe_core/` from the commit recorded in `VENDORED_FROM.txt`; its PolyForm Noncommercial license and Required Notices must stay in the repository.
- Server printing is enabled only when `CUPS_PRINTER` is non-empty. `CUPS_SERVER` defaults to the remote CUPS host; every print artifact must be whitelisted by the job manifest before invoking `lp`.
- GP8 string numbering was measured on the real sample: gtrsnipe/ApolloTab use 0=highest, while GPIF uses 0=lowest. Do not change the conversion formula without repeating the empirical and post-parse verification.
