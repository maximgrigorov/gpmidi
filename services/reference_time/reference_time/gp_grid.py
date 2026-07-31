"""Guitar Pro grid extraction.

Extracts the destination GP measure grid from a parsed Guitar Pro file.
Uses the existing guitarpro library (PyGuitarPro / ApolloTab).
GP positions remain musical ticks/beats in the even destination grid.
GP tempo is destination-only evidence, NOT WAV timing truth.
"""

from __future__ import annotations

from typing import Optional

import guitarpro

from .models import GPMeasure, Warning, WarningCode

GP_PPQ = 960


def _measure_ticks(numerator: int, denominator: int) -> int:
    """Calculate measure length in GP ticks."""
    ticks_per_beat = GP_PPQ * 4 // denominator
    return ticks_per_beat * numerator


def _has_notes_in_measure(song: guitarpro.Song, measure_idx: int) -> bool:
    """Check if any track has notes in this measure."""
    for track in song.tracks:
        if measure_idx < len(track.measures):
            measure = track.measures[measure_idx]
            for voice in measure.voices:
                for beat in voice.beats:
                    if beat.notes:
                        return True
    return False


def extract_gp_grid(
    gp_bytes: bytes,
    gp_revision_sha256: str,
    original_filename: str = "",
) -> list[GPMeasure]:
    """Extract measure grid from Guitar Pro file bytes.

    Returns list of GPMeasure sorted by measure_index.
    Measure headers come from the song's measureHeaders which are
    shared across tracks, avoiding multiplying measure count.
    """
    ext = original_filename.lower()
    try:
        import io
        stream = io.BytesIO(gp_bytes)
        song = guitarpro.parse(stream)
    except Exception as e:
        raise ValueError(f"Failed to parse Guitar Pro file: {e}") from e

    measures: list[GPMeasure] = []
    current_tick = 0

    for idx, header in enumerate(song.measureHeaders):
        warnings: list[Warning] = []

        ts = header.timeSignature
        num = ts.numerator
        den_value = ts.denominator.value if hasattr(ts.denominator, "value") else ts.denominator

        m_ticks = _measure_ticks(num, den_value)

        marker_text: Optional[str] = None
        if header.marker and header.marker.title:
            marker_text = header.marker.title

        has_repeat_open = getattr(header, "isRepeatOpen", False)
        has_repeat_close = header.repeatClose > 0 if hasattr(header, "repeatClose") else False
        repeat_close_count = max(0, getattr(header, "repeatClose", 0))

        has_alt = False
        alt_numbers: list[int] = []
        if hasattr(header, "repeatAlternative") and header.repeatAlternative:
            has_alt = True
            alt_val = header.repeatAlternative
            for bit in range(8):
                if alt_val & (1 << bit):
                    alt_numbers.append(bit + 1)

        tempo_bpm: Optional[float] = None
        if hasattr(header, "tempo") and header.tempo:
            tempo_val = header.tempo
            if hasattr(tempo_val, "value"):
                tempo_bpm = float(tempo_val.value)
            elif isinstance(tempo_val, (int, float)):
                tempo_bpm = float(tempo_val)

        is_empty = not _has_notes_in_measure(song, idx)
        if is_empty:
            warnings.append(Warning(
                code=WarningCode.GP_EMPTY_MEASURE,
                message=f"Measure {idx + 1} has no notes in any track",
            ))

        if has_repeat_open or has_repeat_close:
            warnings.append(Warning(
                code=WarningCode.GP_REPEAT_DETECTED,
                message=f"Measure {idx + 1} has repeat markers",
                context={
                    "repeat_open": has_repeat_open,
                    "repeat_close": has_repeat_close,
                    "repeat_count": repeat_close_count,
                },
            ))

        measures.append(GPMeasure(
            gp_revision_sha256=gp_revision_sha256,
            measure_index=idx,
            measure_number=idx + 1,
            tick_start=current_tick,
            tick_end=current_tick + m_ticks,
            numerator=num,
            denominator=den_value,
            marker_text=marker_text,
            has_repeat_open=has_repeat_open,
            has_repeat_close=has_repeat_close,
            repeat_close_count=repeat_close_count,
            has_alternate_ending=has_alt,
            alternate_ending_numbers=alt_numbers,
            is_empty=is_empty,
            tempo_bpm=tempo_bpm,
            warnings=warnings,
        ))

        current_tick += m_ticks

    return measures
