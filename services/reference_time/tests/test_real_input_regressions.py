"""Regression tests for real-input GP8 and stem consensus findings."""

from __future__ import annotations

import io
import zipfile

import pytest
from reference_time.audio_evidence import apply_audio_evidence_to_measures
from reference_time.consensus import build_consensus
from reference_time.gp_grid import extract_gp_grid
from reference_time.midi_tempo import extract_tempo_evidence
from reference_time.models import AudioEvidence, SourceMeasure


def _gp8_bytes(score_gpif: str | None, *, declared_size: int | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        if score_gpif is not None:
            info = zipfile.ZipInfo("Content/score.gpif")
            archive.writestr(info, score_gpif.encode("utf-8"))
        archive.writestr("Content/other.bin", b"x")
    payload = buf.getvalue()
    if declared_size is None:
        return payload

    # Patch the score member's uncompressed size in local + central headers.
    marker = b"Content/score.gpif"
    data = bytearray(payload)
    local_name = data.find(marker)
    central_name = data.find(marker, local_name + len(marker))
    assert local_name > 0 and central_name > local_name
    # Local header: uncompressed size is 8 bytes before filename.
    # Central header: uncompressed size is 22 bytes before filename.
    data[local_name - 8:local_name - 4] = declared_size.to_bytes(4, "little")
    data[central_name - 22:central_name - 18] = declared_size.to_bytes(4, "little")
    return bytes(data)


def _minimal_gpif() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<GPIF>
  <MasterTrack><Automations>
    <Automation><Type>Tempo</Type><Bar>0</Bar><Position>0</Position><Value>75 2</Value></Automation>
    <Automation><Type>Tempo</Type><Bar>0</Bar><Position>0.5</Position><Value>120 2</Value></Automation>
    <Automation><Type>SyncPoint</Type><Bar>1</Bar><Value><BarIndex>1</BarIndex></Value></Automation>
  </Automations></MasterTrack>
  <MasterBars>
    <MasterBar><Time>4/4</Time><Section><Letter>A</Letter><Text>Intro</Text></Section><Bars>0</Bars></MasterBar>
    <MasterBar><Time>3/4</Time><Repeat start="true" end="true" count="2"/><AlternateEndings>1 3</AlternateEndings><Bars>1</Bars></MasterBar>
  </MasterBars>
  <Bars>
    <Bar id="0"><Voices>0 -1 -1 -1</Voices></Bar>
    <Bar id="1"><Voices>-1 -1 -1 -1</Voices></Bar>
  </Bars>
  <Voices><Voice id="0"><Beats>0</Beats></Voice></Voices>
  <Beats><Beat id="0"><Notes>0</Notes></Beat></Beats>
  <Notes><Note id="0"/></Notes>
</GPIF>"""


def test_gp8_zip_gpif_grid_preserves_structure_and_provenance():
    measures = extract_gp_grid(
        _gp8_bytes(_minimal_gpif()),
        "a" * 64,
        original_filename="song.gp",
    )

    assert len(measures) == 2
    assert (measures[0].numerator, measures[0].denominator) == (4, 4)
    assert measures[0].marker_text == "A — Intro"
    assert measures[0].tempo_bpm == 75.0
    assert measures[0].is_empty is False
    assert (measures[1].numerator, measures[1].denominator) == (3, 4)
    assert measures[1].tempo_bpm == 120.0
    assert measures[1].has_repeat_open is True
    assert measures[1].has_repeat_close is True
    assert measures[1].repeat_close_count == 2
    assert measures[1].alternate_ending_numbers == [1, 3]
    assert measures[1].is_empty is True
    assert any(
        warning.context == {"audio_sync_point_count": 1, "applied": False}
        for warning in measures[0].warnings
    )


def test_gp8_zip_without_score_gpif_fails_closed():
    with pytest.raises(ValueError, match="Content/score.gpif"):
        extract_gp_grid(_gp8_bytes(None), "a" * 64, original_filename="song.gp")


def test_gp8_zip_rejects_declared_oversized_gpif_before_extracting():
    payload = _gp8_bytes(_minimal_gpif(), declared_size=100 * 1024 * 1024)
    with pytest.raises(ValueError, match="size limit"):
        extract_gp_grid(payload, "a" * 64, original_filename="song.gp")


def _stem_midi(first_note_tick: int, note: int) -> bytes:
    import mido

    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    conductor.append(mido.MetaMessage("end_of_track", time=16 * 4 * 480))
    part = mido.MidiTrack()
    part.append(mido.Message("note_on", note=note, velocity=80, time=first_note_tick))
    part.append(mido.Message("note_off", note=note, velocity=0, time=480))
    part.append(mido.MetaMessage("end_of_track", time=16 * 4 * 480 - first_note_tick - 480))
    mid.tracks.extend([conductor, part])
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def test_stem_entry_times_are_diagnostic_not_timeline_conflicts():
    entries = [0, 21, 4 * 480, 26 * 480, 30 * 480]
    sources = [
        extract_tempo_evidence(_stem_midi(tick, 60 + i), f"stem-{i}", f"{i + 1:064x}")
        for i, tick in enumerate(entries)
    ]

    consensus = build_consensus(sources)

    assert consensus.decision.value == "agreed", consensus.conflict_regions
    assert not consensus.conflict_regions
    comparisons = list(consensus.agreement_metrics.values())
    assert any(item["part_entry_comparison"]["aligned"] is False for item in comparisons)
    assert all(item["timeline_origin_comparison"]["aligned"] is True for item in comparisons)


def test_audio_diagnostics_are_measurable_per_measure_and_asset():
    measures = [
        SourceMeasure(
            index=index,
            tick_start=index * 1920,
            tick_end=(index + 1) * 1920,
            seconds_start=index * 2.0,
            seconds_end=(index + 1) * 2.0,
            numerator=4,
            denominator=4,
            tempo_bpm=120.0,
            confidence=1.0,
        )
        for index in range(2)
    ]
    evidence = [
        AudioEvidence(
            asset_link_id="mix",
            sha256="1" * 64,
            role="mix",
            onset_times=[0.1, 1.9, 2.0],
            downbeat_candidates=[0.2, 2.0],
        ),
        AudioEvidence(
            asset_link_id="drums",
            sha256="2" * 64,
            role="drums",
            onset_times=[0.0, 0.5, 1.0],
            downbeat_candidates=[0.0],
        ),
    ]

    enriched = apply_audio_evidence_to_measures(measures, evidence)

    first = {item.asset_link_id: item for item in enriched[0].audio_diagnostics}
    assert first["mix"].onset_count == 2
    assert first["mix"].downbeat_candidate_count == 1
    assert first["mix"].nearest_downbeat_distance_seconds == pytest.approx(0.2)
    assert first["mix"].corroboration == pytest.approx(0.6)
    assert first["drums"].onset_count == 3
    assert first["drums"].corroboration == pytest.approx(1.0)
    assert enriched[0].audio_downbeat_evidence == pytest.approx(1.0)

    second = {item.asset_link_id: item for item in enriched[1].audio_diagnostics}
    assert second["mix"].onset_count == 1
    assert second["mix"].corroboration == pytest.approx(1.0)
    assert second["drums"].onset_count == 0
    assert second["drums"].corroboration is None
