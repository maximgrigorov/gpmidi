from __future__ import annotations

from pathlib import Path

import mido
from transcription_spike.real_input_evaluation import evaluate_pitched_midi_artifacts


def write_midi(path: Path, notes: list[tuple[int, int, int]]) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=1000)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=1_000_000, time=0))
    current_tick = 0
    for onset_tick, pitch, duration_ticks in notes:
        track.append(mido.Message("note_on", note=pitch, velocity=100, time=onset_tick - current_tick))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=duration_ticks))
        current_tick = onset_tick + duration_ticks
    midi.save(path)


def test_driver_reproduces_shift_window_exact_and_onset_only_metrics(tmp_path: Path) -> None:
    reference = tmp_path / "reference.mid"
    prediction = tmp_path / "prediction.mid"
    write_midi(reference, [(10_000, 40, 100), (11_000, 41, 100), (30_000, 50, 100)])
    write_midi(prediction, [(0, 40, 100), (1_020, 52, 100)])

    report = evaluate_pitched_midi_artifacts(
        reference_midi=reference,
        prediction_midi=prediction,
        instrument="bass",
        prediction_offset_seconds=10.0,
        window_start_seconds=10.0,
        window_end_seconds=12.0,
        tolerances_seconds=(0.05,),
    )

    assert report["reference"]["events"] == 2
    assert report["prediction"]["events"] == 2
    assert report["metrics"]["exact_pitch_50ms"]["true_positives"] == 1
    assert report["metrics"]["onset_only_50ms"]["true_positives"] == 2
    assert report["onset_pairs_50ms"]["interval_histogram"] == {"0": 1, "11": 1}
