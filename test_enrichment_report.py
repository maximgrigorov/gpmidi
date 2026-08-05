from __future__ import annotations

from mido import Message, MidiTrack

from enrichment_report import (
    analyze_track_changes,
    build_enrichment_report,
    render_enrichment_html,
)


def _note_track(*notes: tuple[int, int, int, int]) -> MidiTrack:
    """Build a track from (pitch, velocity, start, end) absolute note rows."""
    events = []
    for pitch, velocity, start, end in notes:
        events.append((start, 1, Message("note_on", note=pitch, velocity=velocity, time=0)))
        events.append((end, 0, Message("note_off", note=pitch, velocity=0, time=0)))
    events.sort(key=lambda row: (row[0], row[1]))
    track = MidiTrack()
    previous = 0
    for tick, _order, message in events:
        track.append(message.copy(time=tick - previous))
        previous = tick
    return track


def _spans() -> dict[int, tuple[int, int]]:
    return {1: (0, 3840), 2: (3840, 7680)}


def test_drum_report_proves_velocity_only_enrichment_added_no_hits():
    baseline = _note_track((36, 95, 0, 120), (38, 95, 960, 1080))
    enriched = _note_track((36, 101, 0, 120), (38, 90, 960, 1080))

    facts = analyze_track_changes(
        baseline,
        enriched,
        track_name="Drums",
        track_type="DRUMS",
        spans=_spans(),
        tempo_bpm=120,
        baseline_options={"humanize": True, "ghost_notes": True},
    )

    assert facts["totals"] == {
        "baseline_notes": 2,
        "enriched_notes": 2,
        "paired_notes": 2,
        "velocity_changed": 2,
        "onset_changed": 0,
        "duration_changed": 0,
        "added_notes": 0,
        "removed_notes": 0,
        "controller_events_changed": 0,
        "service_events_changed": 0,
    }
    assert facts["new_drum_hits"] == 0
    assert facts["baseline_processing"] == ["Humanize", "Ghost notes"]
    assert facts["affected_measures"] == [1]
    assert facts["measures"][0]["velocity_transitions"] == [
        {"before": 95, "after": 90, "count": 1},
        {"before": 95, "after": 101, "count": 1},
    ]
    assert facts["recommendation"]["code"] == "compare"
    assert "новых ударов" in facts["recommendation"]["reason"].casefold()


def test_report_attributes_cross_bar_timing_change_to_source_and_destination():
    baseline = _note_track((64, 95, 3800, 3920))
    enriched = _note_track((64, 95, 3860, 3980))

    facts = analyze_track_changes(
        baseline,
        enriched,
        track_name="Lead Guitar",
        track_type="GUITAR",
        spans=_spans(),
        tempo_bpm=120,
        baseline_options={},
    )

    assert facts["affected_measures"] == [1, 2]
    assert facts["measures"][0]["measure"] == 1
    assert facts["measures"][0]["onset_changed"] == 1
    assert facts["measures"][0]["destinations"] == [2]
    assert facts["totals"]["duration_changed"] == 0
    assert facts["max_abs_onset_shift_ticks"] == 60
    assert facts["max_abs_onset_shift_ms"] == 31.25
    assert facts["recommendation"]["code"] == "baseline"


def test_drum_report_names_real_added_hits_by_measure_and_kit_note():
    baseline = _note_track((36, 95, 0, 120))
    enriched = _note_track((36, 95, 0, 120), (42, 55, 4000, 4080))

    facts = analyze_track_changes(
        baseline,
        enriched,
        track_name="Drums",
        track_type="DRUMS",
        spans=_spans(),
        tempo_bpm=120,
        baseline_options={},
    )

    assert facts["new_drum_hits"] == 1
    assert facts["affected_measures"] == [2]
    assert facts["measures"][0]["added_notes"] == [
        {"pitch": 42, "label": "Closed Hi-Hat / HH 14in", "count": 1}
    ]
    assert facts["recommendation"]["code"] == "compare"


def test_controller_change_is_attributed_to_its_actual_measure():
    baseline = MidiTrack([
        Message("control_change", control=11, value=50, time=4000),
    ])
    enriched = MidiTrack([
        Message("control_change", control=11, value=70, time=4000),
    ])

    facts = analyze_track_changes(
        baseline,
        enriched,
        track_name="Strings",
        track_type="OTHER",
        spans=_spans(),
        tempo_bpm=120,
        baseline_options={"humanize": True, "ghost_notes": True},
    )

    assert facts["affected_measures"] == [2]
    assert facts["baseline_processing"] == []
    assert facts["measures"][0]["controller_events_changed"] == 1
    assert facts["totals"]["controller_events_changed"] == 1


def test_self_contained_light_html_escapes_names_and_contains_track_measure_facts():
    track = analyze_track_changes(
        _note_track((36, 95, 0, 120)),
        _note_track((36, 99, 0, 120)),
        track_name="Drums <script>alert(1)</script>",
        track_type="DRUMS",
        spans=_spans(),
        tempo_bpm=120,
        baseline_options={"humanize": True},
    )
    report = build_enrichment_report(
        source_name="song.gp5",
        source_sha256="abc123",
        plan_summary="Мягко поднять пульс.",
        model="gpt-test",
        tracks=[track],
    )

    html = render_enrichment_html(report)

    assert "<!doctype html>" in html.casefold()
    assert "background: #fbfaf8" in html
    assert "Drums &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "Фактические изменения Baseline → Enriched" in html
    assert "Такт 1" in html
    assert "Новых ударов в Enriched" in html
    assert "gpt-test" in html
    assert "@media (max-width: 720px)" in html
    assert "https://" not in html
