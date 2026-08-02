# Phase 4 audition artifacts

These are review artifacts, not automatic Guitar Pro edits.

- `spring-drums-adtof-calibrated-restoration.mid` — experimental five-class ADTOF candidate overlay, with the Spring asset-pair `-140 ms` correction and confidence-derived velocities.
- `spring-drums-adtof-calibrated-patch.json` — immutable candidate records used to build that overlay.
- `spring-drums-source-midi-restoration.mid` — source-MIDI transfer control, preserving the source pitch vocabulary and velocities.
- `spring-drums-source-midi-patch.json` — immutable candidate records for the control.

Both MIDI files are Type 1, use 960 PPQ, and copy/rescale the 363-event tempo map from the reference MIDI. The report and verification hashes are in:

- `docs/phase-4-reference-guided-midi-restoration-report.md`
- `docs/evidence/phase-4-e2e-verification.json`

Mechanical all-measure approval was used only to exercise the complete audition export. It is not a human musical approval.
