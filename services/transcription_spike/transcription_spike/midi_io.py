from __future__ import annotations

import math
from pathlib import Path

from .midi_reference import extract_midi_reference
from .models import EventKind, TranscriptionEvent


def read_pitched_midi_events(
    path: Path,
    *,
    instrument: str,
    timeline_offset_seconds: float = 0.0,
    window_start_seconds: float | None = None,
    window_end_seconds: float | None = None,
) -> list[TranscriptionEvent]:
    """Read pitched MIDI and place it on an absolute source-seconds timeline."""
    if not math.isfinite(timeline_offset_seconds) or timeline_offset_seconds < 0:
        raise ValueError("timeline_offset_seconds must be finite and non-negative")
    if window_start_seconds is not None and (
        not math.isfinite(window_start_seconds) or window_start_seconds < 0
    ):
        raise ValueError("window start must be finite and non-negative")
    if window_end_seconds is not None and (
        not math.isfinite(window_end_seconds) or window_end_seconds < 0
    ):
        raise ValueError("window end must be finite and non-negative")
    if (
        window_start_seconds is not None
        and window_end_seconds is not None
        and window_end_seconds < window_start_seconds
    ):
        raise ValueError("window end must not precede window start")

    reference = extract_midi_reference(
        Path(path).read_bytes(),
        instrument=instrument,
        kind=EventKind.PITCHED,
    )
    events: list[TranscriptionEvent] = []
    for event in reference.events:
        shifted = event.model_copy(
            update={
                "onset_seconds": event.onset_seconds + timeline_offset_seconds,
                "offset_seconds": (
                    event.offset_seconds + timeline_offset_seconds
                    if event.offset_seconds is not None
                    else None
                ),
            }
        )
        if window_start_seconds is not None and shifted.onset_seconds < window_start_seconds:
            continue
        if window_end_seconds is not None and shifted.onset_seconds >= window_end_seconds:
            continue
        events.append(shifted)
    return events
