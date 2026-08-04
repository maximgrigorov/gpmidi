from copy import deepcopy

from humanize import humanize_drums, profile_for_track_type


class _AlwaysEarlyRng:
    def gauss(self, _mean, _stddev):
        return -2.0

    def random(self):
        return 0.5


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
