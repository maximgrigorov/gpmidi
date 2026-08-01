from __future__ import annotations

import io
from collections import defaultdict, deque
from dataclasses import dataclass

import mido

from .models import EventKind, TranscriptionEvent


@dataclass(frozen=True)
class MidiReference:
    midi_type: int
    ticks_per_beat: int
    events: tuple[TranscriptionEvent, ...]
    warnings: tuple[str, ...]


def extract_midi_reference(
    data: bytes,
    *,
    instrument: str,
    kind: EventKind,
    channels: set[int] | None = None,
    drum_map: dict[int, str] | None = None,
) -> MidiReference:
    """Extract complete note events on the MIDI file's own absolute timeline."""
    if kind is EventKind.PERCUSSIVE and drum_map is None:
        raise ValueError("percussive extraction requires an explicit drum_map")
    if kind is EventKind.PITCHED and drum_map is not None:
        raise ValueError("pitched extraction must not provide drum_map")

    midi = mido.MidiFile(file=io.BytesIO(data))
    tempo = 500_000
    absolute_seconds = 0.0
    active: dict[tuple[int, int], deque[tuple[float, int, str | None]]] = defaultdict(deque)
    events: list[TranscriptionEvent] = []
    warnings: set[str] = set()

    for message in mido.merge_tracks(midi.tracks):
        absolute_seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        if message.type == "set_tempo":
            tempo = message.tempo
            continue
        if not hasattr(message, "channel") or not hasattr(message, "note"):
            continue
        if channels is not None and message.channel not in channels:
            continue

        is_note_on = message.type == "note_on" and message.velocity > 0
        is_note_off = message.type == "note_off" or (
            message.type == "note_on" and message.velocity == 0
        )
        if not is_note_on and not is_note_off:
            continue

        event_class: str | None = None
        if kind is EventKind.PERCUSSIVE:
            assert drum_map is not None
            event_class = drum_map.get(message.note)
            if event_class is None:
                if is_note_on:
                    warnings.add(f"ignored_unmapped_drum_pitch:{message.note}")
                continue

        key = (message.channel, message.note)
        if is_note_on:
            active[key].append((absolute_seconds, message.velocity, event_class))
            continue
        if not active[key]:
            warnings.add(f"orphan_note_off:channel={message.channel},pitch={message.note}")
            continue

        onset_seconds, velocity, started_class = active[key].popleft()
        if kind is EventKind.PITCHED:
            events.append(
                TranscriptionEvent(
                    onset_seconds=onset_seconds,
                    offset_seconds=absolute_seconds,
                    kind=kind,
                    instrument=instrument,
                    midi_pitch=message.note,
                    velocity=velocity,
                )
            )
        else:
            events.append(
                TranscriptionEvent(
                    onset_seconds=onset_seconds,
                    offset_seconds=absolute_seconds,
                    kind=kind,
                    instrument=instrument,
                    midi_pitch=message.note,
                    event_class=started_class,
                    velocity=velocity,
                )
            )

    for (channel, pitch), starts in active.items():
        if starts:
            warnings.add(f"unclosed_note:channel={channel},pitch={pitch}")

    events.sort(
        key=lambda event: (
            event.onset_seconds,
            event.midi_pitch if event.midi_pitch is not None else -1,
            event.event_class or "",
        )
    )
    return MidiReference(
        midi_type=midi.type,
        ticks_per_beat=midi.ticks_per_beat,
        events=tuple(events),
        warnings=tuple(sorted(warnings)),
    )
