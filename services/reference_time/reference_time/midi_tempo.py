"""MIDI tempo-map extraction.

Parses Type 0 and Type 1 MIDI files using mido, extracts tempo events,
time signatures, and builds measure boundaries with piecewise tick-to-second
conversion. Avoids floating-point drift by keeping integer ticks as primary
and computing seconds only at serialization.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

import mido

from .models import (
    SourceMeasure,
    SourceTempoEvidence,
    TempoEvent,
    TimeSignatureEvent,
    Warning,
    WarningCode,
)

DEFAULT_TEMPO_BPM = 120.0
DEFAULT_TEMPO_US = 500_000
DEFAULT_NUMERATOR = 4
DEFAULT_DENOMINATOR = 4

SUSPICIOUSLY_SHORT_SECONDS = 5.0
SUSPICIOUSLY_LONG_SECONDS = 3600.0


def _ticks_to_seconds_piecewise(
    target_tick: int,
    tempo_events: list[tuple[int, int]],
    ppq: int,
) -> Fraction:
    """Convert tick to seconds using piecewise tempo regions.

    Uses Fraction arithmetic to avoid cumulative floating-point drift.
    tempo_events: list of (tick, microseconds_per_beat), sorted by tick.
    """
    if not tempo_events:
        tempo_events = [(0, DEFAULT_TEMPO_US)]

    result = Fraction(0)
    prev_tick = 0
    prev_tempo_us = tempo_events[0][1] if tempo_events[0][0] == 0 else DEFAULT_TEMPO_US

    for evt_tick, evt_tempo_us in tempo_events:
        if evt_tick > target_tick:
            break
        if evt_tick > prev_tick:
            delta_ticks = evt_tick - prev_tick
            result += Fraction(delta_ticks * prev_tempo_us, ppq * 1_000_000)
        prev_tick = evt_tick
        prev_tempo_us = evt_tempo_us

    if target_tick > prev_tick:
        delta_ticks = target_tick - prev_tick
        result += Fraction(delta_ticks * prev_tempo_us, ppq * 1_000_000)

    return result


def _build_tempo_table(
    midi_file: mido.MidiFile,
) -> list[tuple[int, int]]:
    """Extract sorted (absolute_tick, tempo_us) from all tracks."""
    events: list[tuple[int, int]] = []

    for track in midi_file.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "set_tempo":
                events.append((abs_tick, msg.tempo))

    events.sort(key=lambda x: x[0])
    return events


def _build_time_sig_table(
    midi_file: mido.MidiFile,
) -> list[tuple[int, int, int]]:
    """Extract sorted (absolute_tick, numerator, denominator)."""
    events: list[tuple[int, int, int]] = []

    for track in midi_file.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "time_signature":
                events.append((abs_tick, msg.numerator, msg.denominator))

    events.sort(key=lambda x: x[0])
    return events


def _find_first_musical_event(midi_file: mido.MidiFile) -> Optional[int]:
    """Find the tick of the first note_on with velocity > 0."""
    first_tick: Optional[int] = None

    for track in midi_file.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                if first_tick is None or abs_tick < first_tick:
                    first_tick = abs_tick
                break

    return first_tick


def _find_last_event_tick(midi_file: mido.MidiFile) -> int:
    """Find the tick of the last event across all tracks."""
    last_tick = 0
    for track in midi_file.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
        if abs_tick > last_tick:
            last_tick = abs_tick
    return last_tick


def extract_tempo_evidence(
    midi_bytes: bytes,
    asset_link_id: str,
    sha256: str,
) -> SourceTempoEvidence:
    """Extract tempo/timing evidence from MIDI bytes.

    Raises ValueError for SMPTE or fundamentally malformed files.
    Returns warnings for recoverable issues.
    """
    warnings: list[Warning] = []
    validation_errors: list[str] = []

    try:
        midi_file = mido.MidiFile(file=__import__("io").BytesIO(midi_bytes))
    except Exception as e:
        raise ValueError(f"Failed to parse MIDI: {e}") from e

    ppq = midi_file.ticks_per_beat
    if ppq < 0 or (ppq & 0x8000):
        raise ValueError(
            f"SMPTE time division ({ppq}) is not supported"
        )

    if ppq == 0:
        raise ValueError("Zero ticks_per_beat is invalid")

    midi_type = midi_file.type

    tempo_table = _build_tempo_table(midi_file)
    ts_table = _build_time_sig_table(midi_file)

    if not tempo_table:
        tempo_table = [(0, DEFAULT_TEMPO_US)]
        warnings.append(Warning(
            code=WarningCode.DEFAULT_TEMPO,
            message=f"No tempo events; using MIDI default {DEFAULT_TEMPO_BPM} BPM",
        ))

    if not ts_table:
        ts_table = [(0, DEFAULT_NUMERATOR, DEFAULT_DENOMINATOR)]
        warnings.append(Warning(
            code=WarningCode.DEFAULT_TIME_SIG,
            message=f"No time signature; using default {DEFAULT_NUMERATOR}/{DEFAULT_DENOMINATOR}",
        ))

    for i in range(1, len(tempo_table)):
        if tempo_table[i][0] < tempo_table[i - 1][0]:
            validation_errors.append(
                f"Non-monotonic tempo at tick {tempo_table[i][0]}"
            )

    tempo_events = []
    for tick, tempo_us in tempo_table:
        secs = float(_ticks_to_seconds_piecewise(tick, tempo_table, ppq))
        bpm = 60_000_000.0 / tempo_us
        tempo_events.append(TempoEvent(tick=tick, seconds=secs, bpm=bpm))

    time_signatures = []
    for tick, num, den in ts_table:
        secs = float(_ticks_to_seconds_piecewise(tick, tempo_table, ppq))
        time_signatures.append(TimeSignatureEvent(
            tick=tick, seconds=secs, numerator=num, denominator=den,
        ))

    first_tick = _find_first_musical_event(midi_file)
    first_seconds = None
    if first_tick is not None:
        first_seconds = float(_ticks_to_seconds_piecewise(first_tick, tempo_table, ppq))
        if first_tick > 0:
            warnings.append(Warning(
                code=WarningCode.PRE_ROLL_DETECTED,
                message=f"First musical event at tick {first_tick} ({first_seconds:.3f}s)",
                context={"tick": first_tick, "seconds": first_seconds},
            ))

    last_tick = _find_last_event_tick(midi_file)
    duration_seconds = float(_ticks_to_seconds_piecewise(last_tick, tempo_table, ppq))

    if duration_seconds < SUSPICIOUSLY_SHORT_SECONDS:
        warnings.append(Warning(
            code=WarningCode.SHORT_DURATION,
            message=f"Duration {duration_seconds:.1f}s is suspiciously short",
        ))
    elif duration_seconds > SUSPICIOUSLY_LONG_SECONDS:
        warnings.append(Warning(
            code=WarningCode.LONG_DURATION,
            message=f"Duration {duration_seconds:.1f}s is suspiciously long",
        ))

    has_musical = False
    for track in midi_file.tracks:
        for msg in track:
            if msg.type == "note_on" and msg.velocity > 0:
                has_musical = True
                break
        if has_musical:
            break
    if not has_musical:
        warnings.append(Warning(
            code=WarningCode.EMPTY_TRACK,
            message="No note_on events found in any track",
        ))

    return SourceTempoEvidence(
        asset_link_id=asset_link_id,
        sha256=sha256,
        parser_name="mido",
        parser_version=mido.__version__ if hasattr(mido, "__version__") else "unknown",
        midi_ppq=ppq,
        time_signatures=time_signatures,
        tempo_events=tempo_events,
        first_event_tick=first_tick,
        first_event_seconds=first_seconds,
        duration_ticks=last_tick,
        duration_seconds=duration_seconds,
        source_type=midi_type,
        warnings=warnings,
        validation_errors=validation_errors,
    )


def build_source_measures(
    evidence: SourceTempoEvidence,
) -> list[SourceMeasure]:
    """Build measure boundaries from tempo/time-signature evidence.

    Handles time-signature changes on measure boundaries. For mid-measure
    changes, marks the affected measure as ambiguous.
    """
    ppq = evidence.midi_ppq
    duration_ticks = evidence.duration_ticks

    tempo_table = [(e.tick, int(60_000_000.0 / e.bpm)) for e in evidence.tempo_events]

    ts_events = evidence.time_signatures
    if not ts_events:
        ts_events = [TimeSignatureEvent(tick=0, seconds=0.0, numerator=4, denominator=4)]

    measures: list[SourceMeasure] = []
    current_tick = 0
    ts_idx = 0
    measure_idx = 0

    while current_tick < duration_ticks:
        while (
            ts_idx + 1 < len(ts_events)
            and ts_events[ts_idx + 1].tick <= current_tick
        ):
            ts_idx += 1

        ts = ts_events[ts_idx]
        num = ts.numerator
        den = ts.denominator

        ticks_per_beat = ppq * 4 // den
        measure_ticks = ticks_per_beat * num
        end_tick = current_tick + measure_ticks

        measure_warnings: list[Warning] = []

        next_ts_idx = ts_idx + 1
        if next_ts_idx < len(ts_events):
            next_ts_tick = ts_events[next_ts_idx].tick
            if current_tick < next_ts_tick < end_tick:
                measure_warnings.append(Warning(
                    code=WarningCode.MID_MEASURE_TS_CHANGE,
                    message=(
                        f"Time signature changes mid-measure at tick {next_ts_tick} "
                        f"(measure starts at {current_tick}, would end at {end_tick})"
                    ),
                    context={
                        "change_tick": next_ts_tick,
                        "measure_start": current_tick,
                        "measure_end": end_tick,
                    },
                ))
                end_tick = next_ts_tick

        if end_tick > duration_ticks:
            end_tick = duration_ticks

        secs_start = float(
            _ticks_to_seconds_piecewise(current_tick, tempo_table, ppq)
        )
        secs_end = float(
            _ticks_to_seconds_piecewise(end_tick, tempo_table, ppq)
        )

        current_tempo_us = tempo_table[0][1]
        for t_tick, t_us in tempo_table:
            if t_tick <= current_tick:
                current_tempo_us = t_us
            else:
                break
        current_bpm = 60_000_000.0 / current_tempo_us

        confidence = 1.0
        if measure_warnings:
            confidence = 0.5

        measures.append(SourceMeasure(
            index=measure_idx,
            tick_start=current_tick,
            tick_end=end_tick,
            seconds_start=secs_start,
            seconds_end=secs_end,
            numerator=num,
            denominator=den,
            tempo_bpm=current_bpm,
            confidence=confidence,
            warnings=measure_warnings,
        ))

        current_tick = end_tick
        measure_idx += 1

    return measures
