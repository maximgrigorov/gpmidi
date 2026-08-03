from __future__ import annotations

from collections import Counter

import pytest
from mido import Message, MidiTrack

from arrangement_workflow import (
    assert_preservation,
    is_solo_track,
    musical_identity,
    require_apply_approval,
)


def _track(*messages):
    track = MidiTrack()
    track.extend(messages)
    return track


def test_apply_requires_explicit_approval_flag():
    with pytest.raises(ValueError, match="--approved"):
        require_apply_approval(False)
    require_apply_approval(True)


def test_solo_detection_is_narrow():
    assert is_solo_track("Guitar Solo", "GUITAR")
    assert is_solo_track("Lead Guitar", "GUITAR")
    assert not is_solo_track("Rhythm Guitar", "GUITAR")
    assert not is_solo_track("Lead Synth", "OTHER")


def test_identity_ignores_velocity_and_timing_but_not_added_hits():
    base = _track(
        Message("note_on", note=36, velocity=95, time=0),
        Message("note_off", note=36, velocity=0, time=60),
    )
    moved = _track(
        Message("note_on", note=36, velocity=70, time=12),
        Message("note_off", note=36, velocity=0, time=60),
    )
    added = _track(*moved, Message("note_on", note=49, velocity=80, time=100))

    assert musical_identity(base) == musical_identity(moved) == Counter({("on", 36): 1, ("off", 36): 1})
    assert musical_identity(added) != musical_identity(base)


def test_preservation_requires_exact_drum_rhythm_and_allows_solo_microtiming():
    base = _track(
        Message("note_on", note=36, velocity=95, time=0),
        Message("note_off", note=36, velocity=0, time=60),
    )
    velocity_only = _track(
        Message("note_on", note=36, velocity=80, time=0),
        Message("note_off", note=36, velocity=0, time=60),
    )
    moved = _track(
        Message("note_on", note=36, velocity=80, time=12),
        Message("note_off", note=36, velocity=0, time=60),
    )

    assert_preservation(base, velocity_only, track_type="DRUMS", allow_timing=False)
    with pytest.raises(RuntimeError, match="timing"):
        assert_preservation(base, moved, track_type="DRUMS", allow_timing=False)
    assert_preservation(base, moved, track_type="GUITAR", allow_timing=True)
