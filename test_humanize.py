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


class _SigmaRng:
    """gauss() возвращает ровно mean + std: сдвиг предсказуем и ненулевой."""

    def gauss(self, mean, stddev):
        return mean + stddev

    def random(self):
        return 0.5


def _synthetic_profile(**timing_classes):
    return {
        "instrument": "Drums", "profile": "synthetic", "config_version": 0,
        "_profile_name": "synthetic",
        "velocity": {"reference_level": 95, "floor": 30, "ceil": 127, "classes": {}},
        "timing": {
            "classes": timing_classes,
            "tempo_ref_bpm": 90.0,
            "tempo_exponent": 0.5,
            "max_shift_frac16": 5.0,
        },
    }


def test_drum_humanize_scales_timing_by_the_tempo_at_each_note():
    """Раньше один bpm (последний в треке) масштабировал ВСЕ ноты: финал на
    180 BPM вдвое урезал задуманный ms-джиттер куплета на 90 BPM."""
    tpb = 960
    prof = _synthetic_profile(kick={"bias_ms": 0.0, "std_ms": 10.0})
    notes = [
        {"tick": 0, "note": 36, "vel": 95, "dur": 240},      # куплет, 90 BPM
        {"tick": 3840, "note": 36, "vel": 95, "dur": 240},   # финал, 180 BPM
    ]

    humanize_drums(
        notes, bpm=180, tpb=tpb, prof=prof, rng=_SigmaRng(), ghost_override=False,
        tempo_map=[(0, 90.0), (3840, 180.0)],
    )

    # 90 BPM: tempo_k=1, 10 мс = 14.4 тика; 180 BPM: tempo_k=0.707,
    # 7.07 мс = 20.4 тика. Со старым глобальным bpm=180 обе ноты получили бы 20.
    assert notes[0]["tick"] == 14
    assert notes[1]["tick"] == 3840 + 20


def test_drum_humanize_empty_bar_guard_uses_real_bar_boundaries():
    """3/4: такт = 2880 тиков. Сетка tpb*4 считала такты по 3840 и не видела,
    что нота уехала в реально пустой такт."""
    tpb = 960
    bar = 3 * tpb                       # 3/4
    prof = _synthetic_profile(crash={"bias_ms": -2.0, "std_ms": 0.0})
    bar_starts = [0, bar, 2 * bar, 3 * bar]
    notes = [
        {"tick": 0, "note": 49, "vel": 95, "dur": 240},        # такт 0
        {"tick": 2 * bar, "note": 49, "vel": 95, "dur": 240},  # такт 2; такт 1 пуст
    ]

    stats = humanize_drums(
        notes, bpm=90, tpb=tpb, prof=prof, rng=_AlwaysEarlyRng(), ghost_override=False,
        bar_starts=bar_starts,
    )

    assert stats["pulled_from_silence"] == 1
    assert notes[1]["tick"] == 2 * bar  # прижата к началу СВОЕГО такта, не 3840


def test_ghost_notes_use_real_bar_grid_in_three_four():
    """Снейр на 5-й шестнадцатой такта 1 в 3/4 (тик 3840): гост prob_before
    обязан встать на 4-ю шестнадцатую ЭТОГО такта (2880+720=3600). Сетка
    tpb*4 считала тот же тик началом такта 1 и кандидата не находила вовсе."""
    from humanize import _add_ghost_notes, _BarGrid

    tpb = 960
    bar = 3 * tpb
    grid = _BarGrid(tpb, [0, bar, 2 * bar, 3 * bar])
    notes = [{"tick": bar + 4 * 240, "note": 38, "vel": 95, "dur": 240}]

    class _EagerRng:
        def shuffle(self, _seq):
            pass

        def random(self):
            return 0.5  # prob_before=1.0 проходит, prob_after=0.0 — нет

        def randint(self, low, _high):
            return low

    ghost_config = {
        "enabled": True, "note": 38, "velocity": {"min": 30, "max": 30},
        "min_gap_frac16": 1.0, "prob_before": 1.0, "prob_after": 0.0,
        "max_per_bar": 2, "skip_bar_if_snare_hits_gte": 0,
    }

    added = _add_ghost_notes(notes, tpb, ghost_config, _EagerRng(), grid)

    assert added == 1
    ghost = notes[-1]
    assert ghost["_ghost"] is True
    assert ghost["tick"] == bar + 3 * 240
    assert grid.index(ghost["tick"]) == 1  # внутри своего такта


def test_ghost_note_candidate_beyond_short_bar_is_rejected():
    """В 3/4 такт кончается на 12-й шестнадцатой; кандидат q>=12 отбрасывается."""
    from humanize import _add_ghost_notes, _BarGrid

    tpb = 960
    bar = 3 * tpb
    grid = _BarGrid(tpb, [0, bar, 2 * bar])
    # снейр на последней (11-й) шестнадцатой: q=10 (чётная) и q=12 (за чертой)
    notes = [{"tick": 11 * 240, "note": 38, "vel": 95, "dur": 240}]

    class _EagerRng:
        def shuffle(self, _seq):
            pass

        def random(self):
            return 0.0

        def randint(self, low, _high):
            return low

    ghost_config = {
        "enabled": True, "note": 38, "velocity": {"min": 30, "max": 30},
        "min_gap_frac16": 1.0, "prob_before": 1.0, "prob_after": 1.0,
        "max_per_bar": 2, "skip_bar_if_snare_hits_gte": 0,
    }

    added = _add_ghost_notes(notes, tpb, ghost_config, _EagerRng(), grid)

    assert added == 0


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
