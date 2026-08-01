"""Guitar Pro destination-grid extraction.

Legacy GP3/4/5 files are parsed with PyGuitarPro. Modern GP7/8 ``.gp``
archives are read directly from their bounded ``Content/score.gpif`` member;
no converter-only ApolloTab dependency or GP5 proxy is involved.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, cast

import guitarpro

from .models import GPMeasure, Warning, WarningCode

GP_PPQ = 960
MAX_GPIF_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_GPIF_COMPRESSION_RATIO = 200


def _measure_ticks(numerator: int, denominator: int) -> int:
    if numerator <= 0 or denominator <= 0:
        raise ValueError(f"Invalid GP time signature {numerator}/{denominator}")
    return GP_PPQ * 4 * numerator // denominator


def _has_notes_in_measure(song: guitarpro.Song, measure_idx: int) -> bool:
    for track in song.tracks:
        if measure_idx < len(track.measures):
            for voice in track.measures[measure_idx].voices:
                for beat in voice.beats:
                    if beat.notes:
                        return True
    return False


def _warning_empty(index: int) -> Warning:
    return Warning(
        code=WarningCode.GP_EMPTY_MEASURE,
        message=f"Measure {index + 1} has no notes in any track",
    )


def _warning_repeat(index: int, opened: bool, closed: bool, count: int) -> Warning:
    return Warning(
        code=WarningCode.GP_REPEAT_DETECTED,
        message=f"Measure {index + 1} has repeat markers",
        context={
            "repeat_open": opened,
            "repeat_close": closed,
            "repeat_count": count,
        },
    )


def _parse_ints(text: str | None) -> list[int]:
    return [int(value) for value in re.findall(r"-?\d+", text or "")]


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def _gpif_member(archive: zipfile.ZipFile) -> bytes:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("GP archive exceeds member count limit")
    unsafe = [
        info.filename for info in infos
        if info.filename.startswith(("/", "\\")) or ".." in info.filename.split("/")
    ]
    if unsafe:
        raise ValueError("GP archive contains unsafe member paths")
    try:
        info = archive.getinfo("Content/score.gpif")
    except KeyError as exc:
        raise ValueError("GP archive is missing Content/score.gpif") from exc
    if info.file_size > MAX_GPIF_BYTES:
        raise ValueError(
            f"Content/score.gpif exceeds size limit ({info.file_size} > {MAX_GPIF_BYTES})"
        )
    if info.file_size and info.compress_size == 0:
        raise ValueError("Content/score.gpif has an invalid compressed size")
    if info.compress_size and info.file_size / info.compress_size > MAX_GPIF_COMPRESSION_RATIO:
        raise ValueError("Content/score.gpif exceeds compression ratio limit")
    data = archive.read(info)
    if len(data) > MAX_GPIF_BYTES:
        raise ValueError("Content/score.gpif exceeds size limit")
    return data


def _parse_gpif_root(gp_bytes: bytes) -> ET.Element:
    try:
        with zipfile.ZipFile(io.BytesIO(gp_bytes)) as archive:
            data = _gpif_member(archive)
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"Failed to read GP archive: {exc}") from exc
    upper_prefix = data[:4096].upper()
    if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
        raise ValueError("GPIF document type/entity declarations are not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse Content/score.gpif: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != "GPIF":
        raise ValueError(f"Unexpected GPIF root element: {root.tag}")
    return root


def _gpif_tempo_by_measure(root: ET.Element, count: int) -> list[float | None]:
    events: list[tuple[int, float, float]] = []
    for automation in root.findall("MasterTrack/Automations/Automation"):
        if (automation.findtext("Type") or "").strip().lower() != "tempo":
            continue
        try:
            bar = int(automation.findtext("Bar") or 0)
            position = float(automation.findtext("Position") or 0)
            value_element = automation.find("Value")
            if value_element is None:
                continue
            value_text = " ".join(value_element.itertext())
            bpm = float(value_text.split()[0])
        except (AttributeError, TypeError, ValueError, IndexError):
            continue
        if bpm > 0:
            events.append((bar, position, bpm))
    events.sort()
    result: list[float | None] = []
    current: float | None = None
    event_index = 0
    for measure_index in range(count):
        while event_index < len(events):
            bar, position, bpm = events[event_index]
            if not (bar < measure_index or (bar == measure_index and position <= 0)):
                break
            current = bpm
            event_index += 1
        result.append(current)
    return result


def _gpif_nonempty_measure_indices(root: ET.Element, master_bars: list[ET.Element]) -> set[int]:
    bars = {bar.get("id"): bar for bar in root.findall("Bars/Bar")}
    voices = {voice.get("id"): voice for voice in root.findall("Voices/Voice")}
    beats = {beat.get("id"): beat for beat in root.findall("Beats/Beat")}
    nonempty: set[int] = set()
    for measure_index, master_bar in enumerate(master_bars):
        for bar_id in (master_bar.findtext("Bars") or "").split():
            bar = bars.get(bar_id)
            if bar is None:
                continue
            for voice_id in (bar.findtext("Voices") or "").split():
                if voice_id == "-1":
                    continue
                voice = voices.get(voice_id)
                if voice is None:
                    continue
                for beat_id in (voice.findtext("Beats") or "").split():
                    if beat_id == "-1":
                        continue
                    beat = beats.get(beat_id)
                    if beat is not None and any(
                        note_id != "-1" for note_id in (beat.findtext("Notes") or "").split()
                    ):
                        nonempty.add(measure_index)
                        break
                if measure_index in nonempty:
                    break
            if measure_index in nonempty:
                break
    return nonempty


def _gpif_section(master_bar: ET.Element) -> tuple[str | None, str | None]:
    section = master_bar.find("Section")
    if section is None:
        return None, None
    letter = (section.findtext("Letter") or "").strip()
    text = (section.findtext("Text") or "").strip()
    marker = " — ".join(value for value in (letter, text) if value) or None
    return marker, text or None


def _gpif_repeat(master_bar: ET.Element) -> tuple[bool, bool, int]:
    repeat = master_bar.find("Repeat")
    if repeat is None:
        return False, False, 0
    opened = _parse_bool(repeat.get("start")) or _parse_bool(repeat.findtext("Start"))
    closed = _parse_bool(repeat.get("end")) or _parse_bool(repeat.findtext("End"))
    count_text = repeat.get("count") or repeat.findtext("Count")
    try:
        count = max(0, int(count_text or 0)) if closed else 0
    except ValueError:
        count = 0
    return opened, closed, count


def _extract_gpif_grid(root: ET.Element, gp_revision_sha256: str) -> list[GPMeasure]:
    master_bars = list(root.findall("MasterBars/MasterBar"))
    if not master_bars:
        raise ValueError("Content/score.gpif contains no MasterBars")
    tempos = _gpif_tempo_by_measure(root, len(master_bars))
    nonempty = _gpif_nonempty_measure_indices(root, master_bars)
    sync_count = sum(
        1 for automation in root.findall("MasterTrack/Automations/Automation")
        if (automation.findtext("Type") or "").strip().lower() == "syncpoint"
    )

    measures: list[GPMeasure] = []
    current_tick = 0
    for index, master_bar in enumerate(master_bars):
        time_text = (master_bar.findtext("Time") or "4/4").strip()
        match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", time_text)
        if not match:
            raise ValueError(f"Invalid GPIF time signature at measure {index + 1}: {time_text}")
        numerator, denominator = map(int, match.groups())
        ticks = _measure_ticks(numerator, denominator)
        marker, section = _gpif_section(master_bar)
        repeat_open, repeat_close, repeat_count = _gpif_repeat(master_bar)
        alternate_numbers = _parse_ints(master_bar.findtext("AlternateEndings"))
        is_empty = index not in nonempty
        warnings: list[Warning] = []
        if is_empty:
            warnings.append(_warning_empty(index))
        if repeat_open or repeat_close:
            warnings.append(_warning_repeat(index, repeat_open, repeat_close, repeat_count))
        if index == 0 and sync_count:
            warnings.append(Warning(
                code=WarningCode.GP_AUDIO_SYNC_POINTS,
                message=(
                    f"GPIF contains {sync_count} audio sync point(s); the notation grid "
                    "uses score tempo automation and does not apply audio warp"
                ),
                context={"audio_sync_point_count": sync_count, "applied": False},
            ))
        measures.append(GPMeasure(
            gp_revision_sha256=gp_revision_sha256,
            measure_index=index,
            measure_number=index + 1,
            tick_start=current_tick,
            tick_end=current_tick + ticks,
            numerator=numerator,
            denominator=denominator,
            marker_text=marker,
            section_text=section,
            has_repeat_open=repeat_open,
            has_repeat_close=repeat_close,
            repeat_close_count=repeat_count,
            has_alternate_ending=bool(alternate_numbers),
            alternate_ending_numbers=alternate_numbers,
            is_empty=is_empty,
            tempo_bpm=tempos[index],
            warnings=warnings,
        ))
        current_tick += ticks
    return measures


def _extract_legacy_grid(gp_bytes: bytes, gp_revision_sha256: str) -> list[GPMeasure]:
    try:
        song = guitarpro.parse(io.BytesIO(gp_bytes))
    except Exception as exc:
        raise ValueError(f"Failed to parse Guitar Pro file: {exc}") from exc

    measures: list[GPMeasure] = []
    current_tick = 0
    for index, header in enumerate(song.measureHeaders):
        ts = header.timeSignature
        numerator = int(ts.numerator)
        raw_denominator = cast(Any, ts.denominator)
        denominator = int(getattr(raw_denominator, "value", raw_denominator))
        ticks = _measure_ticks(numerator, denominator)
        marker = header.marker.title if header.marker and header.marker.title else None
        repeat_open = bool(getattr(header, "isRepeatOpen", False))
        repeat_count = max(0, getattr(header, "repeatClose", 0))
        repeat_close = repeat_count > 0
        alternate = getattr(header, "repeatAlternative", 0) or 0
        alternate_numbers = [bit + 1 for bit in range(8) if alternate & (1 << bit)]
        tempo = getattr(header, "tempo", None)
        if tempo is not None and hasattr(tempo, "value"):
            tempo = float(getattr(tempo, "value"))
        elif isinstance(tempo, (int, float)):
            tempo = float(tempo)
        else:
            tempo = None
        is_empty = not _has_notes_in_measure(song, index)
        warnings = [_warning_empty(index)] if is_empty else []
        if repeat_open or repeat_close:
            warnings.append(_warning_repeat(index, repeat_open, repeat_close, repeat_count))
        measures.append(GPMeasure(
            gp_revision_sha256=gp_revision_sha256,
            measure_index=index,
            measure_number=index + 1,
            tick_start=current_tick,
            tick_end=current_tick + ticks,
            numerator=numerator,
            denominator=denominator,
            marker_text=marker,
            has_repeat_open=repeat_open,
            has_repeat_close=repeat_close,
            repeat_close_count=repeat_count,
            has_alternate_ending=bool(alternate_numbers),
            alternate_ending_numbers=alternate_numbers,
            is_empty=is_empty,
            tempo_bpm=tempo,
            warnings=warnings,
        ))
        current_tick += ticks
    return measures


def extract_gp_grid(
    gp_bytes: bytes,
    gp_revision_sha256: str,
    original_filename: str = "",
) -> list[GPMeasure]:
    """Extract a deterministic destination grid from GP3/4/5 or GP7/8 bytes."""
    stream = io.BytesIO(gp_bytes)
    if zipfile.is_zipfile(stream):
        return _extract_gpif_grid(_parse_gpif_root(gp_bytes), gp_revision_sha256)
    return _extract_legacy_grid(gp_bytes, gp_revision_sha256)
