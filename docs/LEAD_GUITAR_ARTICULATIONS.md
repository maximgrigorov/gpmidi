# Lead guitar: slides, finger vibrato and the articulation manifest

This page covers the opt-in tools for an expressive Solo/Lead guitar on
Shreddage 3.5 Hydra. Nothing here changes a default export. Without the new
flags, `gp_to_shreddage.py` and `solo_expression_humanize.py` produce the same
bytes as before.

Read [`AGENTS.md`](../AGENTS.md) and [`LESSONS.md`](../LESSONS.md) first. The
general agent workflow is in
[`AGENT_POWER_METAL_ENRICHMENT.md`](AGENT_POWER_METAL_ENRICHMENT.md).

## 1. CC1 vibrato and finger vibrato are different things

| | CC1 vibrato | Finger vibrato |
|---|---|---|
| MIDI | Control Change 1 (Mod Wheel) | Pitch Bend |
| What Hydra does with it | Turns the **Vibrato Amount** knob: the instrument oscillates the pitch itself | Moves the pitch directly; the instrument adds nothing |
| Where it comes from | GP vibrato (`Slight` = 32, `Wide` = 80) and `--auto-sustain-vibrato` in the converter | `solo_expression_humanize.py --finger-vibrato`, a post-pass on the mapped MIDI |
| Shape | Envelope: delay, rise, hold, fall (`VIBRATO_MODE = "envelope"`) | Irregular micro-bends: mostly sharp, small flat return, rate and depth vary per cycle |
| Scope | Per note, but the knob is per channel | Per channel: any other sounding note bends too |

Never put both on the same note. Two vibratos on top of each other sound like a
synthetic wobble. The manifest-gated post-pass skips every note with GP or
automatic CC1 vibrato.

## 2. Slides to the real target (`--slide-to-target`)

By default the converter bends every slide a fixed `SLIDE_BEND_ST = 2`
semitones toward the next note, on any polyphony. With `--slide-to-target`:

| GP effect | Result |
|---|---|
| `legatoSlideTo` / `shiftSlideTo` | Pitch Bend steps from 0 to the **actual** interval of the next note on the same string, one event per fret crossed, ending one tick before the target. PB resets to 0 on the target attack, before its `note_on`. |
| `intoFromBelow` / `intoFromAbove` | Scoop on the **attack**: starts 2 semitones below/above and glides to 0. The old code bent the end of the note instead. |
| `outDownwards` / `outUpwards` | 2-semitone fall/rise at the end of the note, in the direction of the **type**, not toward the next note. |

Glide length is `glide_ms_per_semitone x interval`, clamped to
`[glide_min_ms, glide_max_ms]` and to `max_note_fraction` of the source note.
The values live in `slide_to_target` in
`config/articulation_maps/shreddage_hydra_3.5.yaml`. They are starting values,
not measurements, so tune them by ear. Darkwall has no section and uses the
module defaults.

### Safe fallbacks

PB is channel-wide, so the converter would rather leave a slide flat than bend
the wrong note. These cases emit no slide PB. The reason is in the stats and the
manifest:

| Reason | Meaning |
|---|---|
| `no_target` | No later note on the same string |
| `target_after_rest` | The target comes after a rest longer than `max_rest_frac16` (a quarter) |
| `polyphony` | Another note sounds during the glide (a held note under a double-stop slide, overlapping voices) |
| `authored_bend` | The source carries a GP bend: the authored curve wins |
| `target_prebend` | The target starts pre-bent: the glide and the pre-bend would contradict each other |
| `harmonic`, `tapping` | Harmonic pitch is remapped (octave folding), and tapping has its own Hydra logic |
| `same_pitch` | A slide to the same pitch moves nothing |
| `approached_by_slide` | A slide-in on a note that a slide-to already reaches |

Two cases still emit PB:

- **Parallel slides:** a double-stop where every note slides by the same
  interval in the same window gets one shared curve. This is the
  power-chord slide.
- **Interval too wide:** if the interval is at least the PB range (Hydra 7,
  Darkwall 2), the result is `fixed_depth_fallback`. It is the old partial
  2-semitone glide (1 on Darkwall), but now in the true direction and never at
  the 8191 ceiling.

GP8 imports sometimes report the wrong string for a note (LESSONS.md p.18). On
Through the Night, 3 of 6 slide-to notes have no same-string target within a
quarter, and all three are skipped as `target_after_rest`. The legacy code bent
them toward a note several bars later.

### The native Hydra Legato Slide

Hydra has a non-latching "Legato Slide To Next" FX keyswitch
(`fx_keyswitches.legato_slide_to_next`, C0 = 24, up to 12 semitones). It turns
a legato transition, meaning overlapping notes, into a sampled slide.
**It is not verified against the user's preset.** The open questions are the
note number, hold versus press, and whether Legato is enabled. So the map ships
`slide_to_target.native.verified: false`, and the converter uses Pitch Bend.

To switch to the native slide, first verify it in Kontakt (TACT tab,
keyswitches): a held C0 must make the next overlapping note slide. Then set
`verified: true`. The converter then uses it only for `legatoSlideTo` (a shift
slide re-picks the target by definition) on monophonic slides of 1-12
semitones. It does three things:

- holds the keyswitch from `KS_LEAD_MS` before the target attack to
  `KS_LEAD_MS` after it;
- extends the source note `LEGATO_OVERLAP_MS` into the target, the same as a
  hammer-on;
- sends no PB.

## 3. The articulation manifest (`--articulation-manifest`)

The converter writes `<base>_articulations.json` next to the MIDI files. It
covers Guitar/Bass tracks only. Ticks are the MIDI ticks (960 PPQ), so a note
is identified by its attack tick and pitch. Each note record holds:

| Field | Content |
|---|---|
| `source` | `gp` (every note currently comes from the Guitar Pro file) |
| `bar`, `voice`, `string`, `fret`, `pitch` | Position. `string`/`fret` as the parser reports them |
| `on_tick`, `off_tick`, `grid_tick`, `velocity` | Final MIDI timing and velocity; `grid_tick` is the notated attack |
| `articulation`, `keyswitch` | Resolved Hydra articulation and the keyswitch note of its beat |
| `harmonic`, `pinch_harmonic`, `tapping`, `bend` | Effects that conflict with automatic pitch motion |
| `slides`, `slide`, `slide_target` | GP slide types; resolution (`pitch_bend_to_target`, `native_keyswitch`, `fixed_depth_fallback`, `slide_in`, `slide_out`, `skipped` + reason, or `legacy_fixed_depth` without the flag); whether a resolved slide reaches this note |
| `legato_source`, `vibrato`, `auto_cc1_vibrato` | Hammer/pull source; GP vibrato type; CC1 from `--auto-sustain-vibrato` |
| `polyphony` | `mono`, `double_stop`, `chord` or `overlap`, from final note ends |
| `finger_vibrato` | `eligible` and `skip_reasons` |

The track entry adds `pitch_bend_range`, the config version, `slide_mode` and a
summary (`finger_vibrato_eligible` and counts per skip reason). The file is a
generated listening artifact: never commit it.

## 4. Finger vibrato for lead guitar

`solo_expression_humanize.py --finger-vibrato` keeps its irregular,
deterministic micro-bends and its safety rules: monophonic long notes only, no
existing PB, PB back to 0 before note-off, every authored anchor preserved.
New options:

| Option | Effect |
|---|---|
| `--articulation-manifest FILE` | Only notes the converter marked eligible. A note missing from the manifest is skipped; another PPQ or a missing track is an error |
| `--finger-vibrato-placement phrase` | Phrase ends first. A long note followed by a rest of 150 ms or more, with nothing after it, or at least 2x the median length of the next three notes (an arrival before a faster run) gets probability 0.95. Long notes inside a run of equally long notes get 0.35x the style probability |
| `--skip-cc1-vibrato-notes` | Skip notes with CC1 vibrato; implied by both options above |

The output manifest gains `stats.finger_vibrato_skipped`, which counts each
skipped note once by reason, and `finger_vibrato_context`.

Measured with seed 23 and the `natural` style:

| Solo | Uniform pass | With converter manifest |
|---|---|---|
| Spring Melody | 7 notes | 1 |
| Through the Night | 14 notes | 3 |
| pnd | 7 notes | 2 |

The removed notes already carried GP CC1 vibrato or were harmonics. On these
three solos, every eligible long note already counts as a phrase end. Most
long notes run straight into the next run with a 0-12 ms gap rather than
before a rest; they qualify as arrival notes. So `phrase` raises coverage
there (natural: 0.75 -> 0.95). It demotes
notes only in passages of equally long notes, such as a ballad melody line.

## 5. Recommended power-metal lead workflow

```bash
# 1. Base GP -> Hydra export with real slides and the manifest
python gp_to_shreddage.py song.gp --slide-to-target --articulation-manifest
```

```bash
# 2. Review the manifest: slide resolutions and why notes are excluded
python - <<'EOF'
import json
m = json.load(open("song_midi/song_articulations.json"))
for t in m["tracks"]:
    print(t["name"], t["slide_mode"], t["summary"])
    for n in t["notes"]:
        if n["slide"] and n["slide"]["resolution"] == "skipped":
            print("  bar", n["bar"], n["slides"], n["slide"]["reason"])
EOF
```

```bash
# 3. Optional post-pass: finger vibrato on eligible phrase-ending notes only
python solo_expression_humanize.py song_midi/song_ALL.mid lead-natural.mid \
  --track-name "Lead Guitar" --finger-vibrato --finger-vibrato-style natural \
  --finger-vibrato-placement phrase \
  --articulation-manifest song_midi/song_articulations.json \
  --pitch-bend-range 7 --seed 23 --changed-tracks-only --manifest lead-natural.json
```

```bash
# 4. Check the result, then render through Shreddage in Logic
python verify_midi.py lead-natural.mid --type GUITAR
```

For step 4, set Hydra's PITCH BEND RANGE knob to 7 before listening. Import the
conductor plus the changed Lead track next to the accepted baseline and compare
A/B by ear. Keep generated MIDI, JSON and audio out of Git.

## 6. What MIDI cannot fix (Hydra / Kontakt limits)

- **Pitch Bend is per channel.** The converter exports one channel per track,
  so a bend or slide on one note of a double-stop always bends the other note
  too. Slides and finger vibrato skip polyphony. Authored GP bends inside
  chords (unison/double-stop bends) are still emitted as written and marked
  `polyphony` in the manifest. A clean version needs a per-string channel
  (MPE-like) setup that Hydra does not offer.
- **PB range is a knob, not a message.** Kontakt does not have to honour the
  RPN the converter sends. Hydra's PITCH BEND RANGE knob must equal
  `pitch_bend_range` (7). Darkwall's 2 is still the unverified default.
- **A PB glide is not a sampled slide.** It bends one sample and has no
  fret-buzz or string-change noise. The real slide sample needs the native
  keyswitch, which is unverified (see above).
- **Legato depends on the instrument state.** Hammer/pull and the native slide
  rely on Hydra's overlap legato being enabled. The `legato_on`/`legato_off`
  note numbers in the map are suspect (comment in the YAML) and are not sent.
- **Articulation state persists.** Kontakt does not reset keyswitches between
  playback runs. The converter sends an initial Sustain keyswitch; a manual
  edit in Logic must keep one too.
- **Harmonics have their own sample range.** Pitches above `max_note` are
  silent and are folded down in octaves. Whether a folded harmonic sounds right
  is a listening question.
- **Velocity is an articulation switch on Sustain.** 120-126 is Rake, 127 is
  Pinch. No expression pass may push a normal note there.
- **Pinch harmonics and squeals.** A pinch is Sustain at velocity 127; Hydra
  has no separate pinch-vibrato behaviour, so the post-pass leaves pinches
  alone.
