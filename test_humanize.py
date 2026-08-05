from copy import deepcopy
from types import SimpleNamespace

from humanize import humanize_drums, pitched_velocity, profile_for_track_type


class _AlwaysEarlyRng:
    def gauss(self, _mean, _stddev):
        return -2.0

    def random(self):
        return 0.5


class _ZeroRng:
    def gauss(self, _mean, _stddev):
        return 0.0


def _plain_note(**effect_overrides):
    effect = SimpleNamespace(
        harmonic=None, tapping=False, tremoloPicking=False,
        trill=False, palmMute=False, staccato=False,
    )
    for key, value in effect_overrides.items():
        setattr(effect, key, value)
    return SimpleNamespace(effect=effect)


def test_note_articulation_returns_profile_key_name():
    """Профили держат velocity-офсеты по ИМЕНАМ артикуляций ("palm_mute": -5).

    note_articulation возвращал номер keyswitch-ноты (12, 13, ...), поэтому
    (v["articulation"] or {}).get(articulation, 0) резолвился в 0 всегда —
    palm mute не приглушался ни на одном humanized-экспорте.
    """
    from gp_to_shreddage import note_articulation

    art_name, is_pinch = note_articulation(_plain_note(palmMute=True), "GUITAR")
    assert art_name == "palm_mute"
    assert is_pinch is False

    art_name, is_pinch = note_articulation(_plain_note(), "GUITAR")
    assert art_name == "sustain"
    assert is_pinch is False


def test_note_articulation_still_reports_pinch():
    from gp_to_shreddage import note_articulation

    class PinchHarmonic:  # _canonical_articulation dispatches on the type name
        pass

    art_name, is_pinch = note_articulation(_plain_note(harmonic=PinchHarmonic()), "GUITAR")
    assert art_name == "pinch_harmonics"
    assert is_pinch is True


def test_pitched_velocity_applies_articulation_offsets_from_profile():
    prof = profile_for_track_type("GUITAR")
    accent_beat = float(prof["velocity"]["accent"]["beat"])
    palm_mute_offset = float(prof["velocity"]["articulation"]["palm_mute"])
    sustain_offset = float(prof["velocity"]["articulation"]["sustain"])
    assert palm_mute_offset < 0 < sustain_offset  # the profile numbers this test relies on

    muted = pitched_velocity(95, 0, 960, "palm_mute", prof, _ZeroRng(), vel_cap=127)
    sustained = pitched_velocity(95, 0, 960, "sustain", prof, _ZeroRng(), vel_cap=119)
    assert muted == int(round(95 + accent_beat + palm_mute_offset))
    assert sustained == int(round(95 + accent_beat + sustain_offset))


def test_drum_humanize_does_not_move_first_attack_one_bar_early():
    tpb = 960
    first_used_bar = 4
    original_tick = first_used_bar * 4 * tpb
    notes = [{"tick": original_tick, "note": 49, "vel": 79, "dur": 240}]
    profile = deepcopy(profile_for_track_type("DRUMS"))

    stats = humanize_drums(
        notes,
        bpm=75,
        tpb=tpb,
        prof=profile,
        rng=_AlwaysEarlyRng(),
        ghost_override=False,
    )

    assert stats["pulled_from_silence"] == 1
    assert notes[0]["tick"] == original_tick
