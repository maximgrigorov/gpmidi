from __future__ import annotations

from mido import Message, MetaMessage, MidiFile, MidiTrack

from verify_midi import smoke_check


def _save(path, messages):
    midi = MidiFile(type=0, ticks_per_beat=960)
    track = MidiTrack()
    track.append(MetaMessage("track_name", name="Bass", time=0))
    track.extend(messages)
    track.append(MetaMessage("end_of_track", time=0))
    midi.tracks.append(track)
    midi.save(path)


def _codes(path):
    return [code for severity, code, _message in smoke_check(path, "BASS") if severity == "ERROR"]


def test_initial_keyswitch_at_tick_zero_is_valid_when_ordered_before_first_note(tmp_path):
    path = tmp_path / "initial-before.mid"
    _save(path, [
        Message("note_on", note=0, velocity=100, time=0),
        Message("note_on", note=43, velocity=95, time=0),
        Message("note_off", note=0, velocity=0, time=6),
        Message("note_off", note=43, velocity=0, time=954),
    ])

    assert "KS_LEAD" not in _codes(path)


def test_same_tick_articulation_change_after_music_is_still_rejected(tmp_path):
    path = tmp_path / "transition-after.mid"
    _save(path, [
        Message("note_on", note=0, velocity=100, time=0),
        Message("note_off", note=0, velocity=0, time=6),
        Message("note_on", note=43, velocity=95, time=954),
        Message("note_off", note=43, velocity=0, time=960),
        Message("note_on", note=45, velocity=95, time=0),
        Message("note_on", note=1, velocity=100, time=0),
        Message("note_off", note=1, velocity=0, time=6),
        Message("note_off", note=45, velocity=0, time=954),
    ])

    assert "KS_LEAD" in _codes(path)


# --- VEL_ZONE: зоны Hydra существуют только на sustain-KS -------------------
# Пре-фикс артефактов в репозитории нет, поэтому обе стороны проверяются на
# синтетике: проверка обязана МОЛЧАТЬ на легальном velocity 120+ вне sustain и
# обязана СРАБОТАТЬ на sustain (проверка, которая никогда не срабатывает, не
# проверена — LESSONS.md п.11).

SUSTAIN_KS = 12     # Hydra C-1
PALM_MUTE_KS = 13   # Hydra C#-1


def _guitar_codes(path):
    return [code for severity, code, _message in smoke_check(path, "GUITAR")
            if severity == "ERROR"]


def _guitar_file(path, ks_note, velocity):
    """KS-импульс, затем нота с данной velocity (лид 15 мс как у экспортёра)."""
    _save(path, [
        Message("note_on", note=ks_note, velocity=100, time=0),
        Message("note_off", note=ks_note, velocity=0, time=6),
        Message("note_on", note=64, velocity=velocity, time=18),
        Message("note_off", note=64, velocity=0, time=960),
    ])


def test_vel_zone_fires_on_sustain_attack_in_rake_zone(tmp_path):
    path = tmp_path / "sustain-123.mid"
    _guitar_file(path, SUSTAIN_KS, 123)

    assert "VEL_ZONE" in _guitar_codes(path)


def test_vel_zone_is_silent_on_palm_mute_attack_in_the_same_range(tmp_path):
    """palm-muted FF (111) с акцентом (+12) = 123 — легально: vel_cap=127 вне
    sustain. Раньше это давало ложный ERROR на корректном артефакте."""
    path = tmp_path / "palm-mute-123.mid"
    _guitar_file(path, PALM_MUTE_KS, 123)

    assert "VEL_ZONE" not in _guitar_codes(path)


def test_vel_zone_is_silent_on_intentional_pinch_at_127(tmp_path):
    """Pinch кодируется именно так: sustain-KS + velocity 127. Зона — rake
    (120-126), поэтому 127 не флажится."""
    path = tmp_path / "pinch-127.mid"
    _guitar_file(path, SUSTAIN_KS, 127)

    assert "VEL_ZONE" not in _guitar_codes(path)


def test_vel_zone_tracks_articulation_changes_through_the_file(tmp_path):
    """После возврата с palm mute на sustain зона снова активна."""
    path = tmp_path / "sequence.mid"
    _save(path, [
        # sustain -> нота 95 (чисто)
        Message("note_on", note=SUSTAIN_KS, velocity=100, time=0),
        Message("note_off", note=SUSTAIN_KS, velocity=0, time=6),
        Message("note_on", note=64, velocity=95, time=18),
        Message("note_off", note=64, velocity=0, time=960),
        # palm mute -> нота 123 (легально)
        Message("note_on", note=PALM_MUTE_KS, velocity=100, time=0),
        Message("note_off", note=PALM_MUTE_KS, velocity=0, time=6),
        Message("note_on", note=64, velocity=123, time=18),
        Message("note_off", note=64, velocity=0, time=960),
        # обратно sustain -> нота 123 (ошибка: rake-зона)
        Message("note_on", note=SUSTAIN_KS, velocity=100, time=0),
        Message("note_off", note=SUSTAIN_KS, velocity=0, time=6),
        Message("note_on", note=64, velocity=123, time=18),
        Message("note_off", note=64, velocity=0, time=960),
    ])

    found = smoke_check(path, "GUITAR")
    vel_zone = [message for severity, code, message in found if code == "VEL_ZONE"]
    assert len(vel_zone) == 1
    assert vel_zone[0].startswith("1 нот")
