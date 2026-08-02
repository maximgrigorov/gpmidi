from __future__ import annotations

import json
from pathlib import Path

import mido
from transcription_spike.drum_real_input_evaluation import evaluate_drum_artifacts


def test_evaluator_uses_midi_tempo_normalizes_classes_and_separates_offset_sensitivity(tmp_path: Path) -> None:
    reference_path = tmp_path / "reference.mid"
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    tempo = mido.MidiTrack()
    tempo.append(mido.MetaMessage("set_tempo", tempo=1_000_000, time=0))
    drums = mido.MidiTrack()
    drums.append(mido.Message("note_on", channel=9, note=81, velocity=90, time=0))
    drums.append(mido.Message("note_on", channel=9, note=36, velocity=100, time=480))
    midi.tracks.extend([tempo, drums])
    midi.save(reference_path)

    prediction_path = tmp_path / "prediction.json"
    prediction_path.write_text(
        json.dumps(
            {
                "adapter": "fixture",
                "events": [
                    {"instrument": "KD", "midi_note": 36, "time_seconds": 1.145, "velocity": 100}
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_drum_artifacts(
        reference_midi=reference_path,
        prediction_json=prediction_path,
        tolerances_seconds=(0.05,),
        offset_range_seconds=(-0.2, 0.2),
        offset_step_seconds=0.005,
    )

    assert report["reference"]["events_total"] == 2
    assert report["reference"]["events_evaluated"] == 1
    assert report["reference"]["excluded_note_counts"] == {"81": 1}
    assert report["prediction"]["class_counts"] == {"kick": 1}
    assert report["metrics"]["onset_class_50ms"]["f1"] == 0.0
    assert report["offset_sensitivity"]["best_offset_seconds"] == -0.145
    assert report["offset_sensitivity"]["best_50ms"]["f1"] == 1.0
    assert report["offset_sensitivity"]["note"] == "Raw source-seconds metrics remain authoritative."
