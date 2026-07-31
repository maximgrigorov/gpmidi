# Reference-time real-input findings

Date: 2026-08-01

A real 96-measure GP8 + six aligned stem MIDI + full mix/six WAV stems was exercised against the deployed Asset API and reference-time service. Raw music assets remain outside Git.

## Findings

### 1. GP8 is not accepted or parsed end-to-end

Modern `.gp` files are ZIP/GPIF containers beginning with `PK\x03\x04`. Asset API currently recognizes only `BCFZ` for the `.gp` suffix. Commit `6eae7f4` adds the missing ZIP signature with a failing-first unit test.

That commit is necessary but not sufficient: `reference_time.gp_grid.extract_gp_grid()` passes bytes directly to PyGuitarPro, which rejects GP8 ZIP/GPIF as an unsupported version. Before deploying the signature fix as complete GP8 support, reference-time must either:

- reuse the repository's established GP8/ApolloTab path in `gp_import.py`; or
- add a focused GPIF grid extractor for measure headers, time signatures, markers/repeats and empty-measure status.

The next real run must consume the original GP8 asset, not a compatibility proxy.

### 2. Stem entry times are misclassified as tempo-map conflicts

All six real stem MIDI files had exactly identical:

- duration;
- 363 tempo events;
- time signature;
- 96 measure boundaries (maximum difference 0.0 seconds).

They naturally had different first note-on times because instruments enter at different places. Consensus classified three of those differences as `preroll_downbeat_mismatch`, marked the entire analysis conflicted, and applied the conflict penalty to all 96 mappings.

For stem MIDI, first musical event is not timeline origin. If duration, conductor/tempo events, time signatures and measure boundaries agree, different instrument entry times must not create a global tempo conflict. Preserve first-note timing as useful part-level evidence, but separate it from whole-file pre-roll.

### 3. Audio path executes, but currently corroborates rather than warps

All seven five-minute WAV files decoded completely. Downbeat proximity was attached to 76/96 source measures and increased confidence for those measures. However audio does not alter source measure seconds or create a monotonic audio↔notation warp; source MIDI remains the timing authority.

The report should expose richer audio diagnostics:

- nearest candidate distance in milliseconds;
- number and roles of stems supporting the candidate;
- contradictory/missing evidence, not only positive proximity;
- whether audio changed mapping topology or only confidence.

### 4. Real input did not challenge mapping topology

The real bundle contained 96 source measures and 96 GP measures with matching order, so every result was one-to-one identity. Add a controlled real-derived fixture with a removed/duplicated measure or repeat expansion before claiming one-to-many/many-to-one acceptance.

### 5. Combined converter MIDI is target-specific, not GM-safe

The web converter successfully produced nine per-track MIDI files and `_ALL.mid` from the GP8 source. Per-track files intentionally use the same channel for separate sampler instances. `_ALL.mid` retains those channel assignments and conflicting program changes, so it must not be advertised as standalone GM playback. A separate GM-safe audition merge is required for phone/browser previews.

## Acceptance order

1. Complete original-GP8 support in reference-time.
2. Fix stem-aware pre-roll consensus.
3. Re-run the same private real-input acceptance without proxy data.
4. Add a controlled topology mismatch.
5. Only then build the minimal upload/analyze/report UI and any restoration/compiler stage.
