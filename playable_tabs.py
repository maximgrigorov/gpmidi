from __future__ import annotations

import argparse
import copy
import logging
import math
import shutil
import time
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import guitarpro
from guitarpro.models import NoteType
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

from gp_import import _write_patched_gp, is_gp7_gp8_archive, parse_song
from gp_to_shreddage import (
    TICKS_PER_BEAT,
    iter_voice_beats_with_canonical_ticks,
    resolve_track_type,
    safe_filename,
)
from third_party.gtrsnipe_core.core.config import MapperConfig
from third_party.gtrsnipe_core.core.types import MusicalEvent, Song, Track
from third_party.gtrsnipe_core.formats.tab.generator.ascii import AsciiTabGenerator
from third_party.gtrsnipe_core.guitar.mapper import GuitarMapper

LOGGER = logging.getLogger(__name__)

GUITAR_BASE = dict(tuning="STANDARD", num_strings=6, max_fret=22, capo=0, unplayable_fret_span=4)
PRESET_CONFIGS: dict[str, dict[str, Any]] = {
    "solo": dict(
        **GUITAR_BASE,
        sweet_spot_low=5,
        sweet_spot_high=15,
        sweet_spot_bonus=1.0,
        movement_penalty=5.0,
        string_switch_penalty=8.0,
    ),
    "rhythm": dict(
        **GUITAR_BASE,
        sweet_spot_low=0,
        sweet_spot_high=7,
        sweet_spot_bonus=1.0,
        prefer_open=True,
    ),
    "bass": dict(
        tuning="BASS_STANDARD",
        num_strings=4,
        max_fret=20,
        capo=0,
        unplayable_fret_span=5,
        sweet_spot_low=0,
        sweet_spot_high=12,
        sweet_spot_bonus=1.0,
        prefer_open=True,
    ),
}

# name -> (type, min, max, default, UI label). "preset" is handled as enum.
TUNABLE_PARAMS: dict[str, tuple[Any, Any, Any, Any, str]] = {
    "preset": (str, None, None, "auto", "Пресет"),
    "sweet_spot_low": (int, 0, 22, 0, "Рабочая зона: от лада"),
    "sweet_spot_high": (int, 0, 22, 12, "Рабочая зона: до лада"),
    "sweet_spot_bonus": (float, 0.0, 10.0, 1.0, "Притяжение к рабочей зоне"),
    "movement_penalty": (float, 0.0, 50.0, 3.0, "Штраф движения по грифу"),
    "string_switch_penalty": (float, 0.0, 50.0, 5.0, "Штраф смены струны"),
    "unplayable_fret_span": (int, 3, 6, 4, "Максимальная растяжка"),
    "prefer_open": (bool, None, None, False, "Предпочитать открытые струны"),
}

OPEN_PITCHES = {
    "GUITAR": [64, 59, 55, 50, 45, 40],  # high -> low
    "BASS": [43, 38, 33, 28],             # high -> low
}


def has_standard_tuning(track, track_type: str) -> bool:
    """Строй трека совпадает со строем пресетов маппера (OPEN_PITCHES).

    Маппер раскладывает высоты по ФИКСИРОВАННОМУ стандартному грифу
    (MapperConfig tuning="STANDARD"/"BASS_STANDARD"). Для drop-D и любого
    другого строя обратная запись позиций меняла бы высоты — сравниваем
    отсортированные питчи, чтобы не зависеть от нумерации струн парсеров.
    """
    expected = OPEN_PITCHES.get(track_type)
    if not expected:
        return False
    actual = sorted(int(string.value) for string in getattr(track, "strings", []) or [])
    return actual == sorted(expected)


@dataclass
class ScoreNote:
    start_tick: int
    dur_ticks: int
    pitch: int
    velocity: int
    source_notes: list[Any] = field(default_factory=list, repr=False)
    source_indices: list[int] = field(default_factory=list, repr=False)


@dataclass
class MappedNote:
    start_tick: int
    dur_ticks: int
    pitch: int
    velocity: int
    string: int
    fret: int
    technique: str | None
    source_indices: list[int]


@dataclass
class TrackGeneration:
    track_name: str
    track_type: str
    basename: str
    preset: str
    params: dict[str, Any]
    files: list[str]
    status: dict[str, str]
    mapped: list[MappedNote]
    note_positions: list[tuple[int, int] | None]
    version: int = 1
    generated_at: str = field(default_factory=lambda: datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"))

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "params": self.params,
            "files": self.files,
            "status": self.status,
            "version": self.version,
            "generated_at": self.generated_at,
        }


def auto_preset(track_name: str, track_type: str) -> str:
    if track_type == "BASS":
        return "bass"
    return "solo" if "solo" in (track_name or "").lower() else "rhythm"


def validate_params(raw: dict[str, Any], *, allow_missing: bool = True) -> dict[str, Any]:
    unknown = set(raw) - set(TUNABLE_PARAMS)
    if unknown:
        raise ValueError(f"неизвестные параметры: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for name, value in raw.items():
        typ, minimum, maximum, _default, _label = TUNABLE_PARAMS[name]
        if name == "preset":
            value = str(value or "auto").lower()
            if value not in {"auto", "solo", "rhythm", "bass"}:
                raise ValueError("preset должен быть auto, solo, rhythm или bass")
        elif typ is bool:
            value = value is True or str(value).lower() in {"1", "true", "on", "yes"}
        else:
            try:
                value = typ(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"неверное значение {name}") from exc
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} должен быть конечным числом")
            if minimum is not None and value < minimum or maximum is not None and value > maximum:
                raise ValueError(f"{name} вне диапазона {minimum}..{maximum}")
        result[name] = value
    if not allow_missing:
        missing = set(TUNABLE_PARAMS) - set(result)
        if missing:
            raise ValueError(f"не заданы параметры: {', '.join(sorted(missing))}")
    low = result.get("sweet_spot_low")
    high = result.get("sweet_spot_high")
    if low is not None and high is not None and high <= low:
        raise ValueError("sweet_spot_high должен быть больше sweet_spot_low")
    return result


def resolve_params(preset: str, overrides: dict[str, Any], *, track_name: str = "", track_type: str = "GUITAR") -> dict[str, Any]:
    requested = (preset or "auto").lower()
    actual = auto_preset(track_name, track_type) if requested == "auto" else requested
    if actual not in PRESET_CONFIGS:
        raise ValueError(f"неизвестный пресет: {requested}")
    if track_type == "BASS":
        actual = "bass"
    elif actual == "bass":
        raise ValueError("басовый пресет нельзя применить к гитаре")
    checked = validate_params(overrides)
    checked.pop("preset", None)
    params = dict(PRESET_CONFIGS[actual])
    params.update(checked)
    if params["sweet_spot_high"] <= params["sweet_spot_low"]:
        raise ValueError("sweet_spot_high должен быть больше sweet_spot_low")
    max_fret = int(params["max_fret"])
    if params["sweet_spot_high"] > max_fret:
        raise ValueError(f"sweet_spot_high выше максимального лада {max_fret}")
    config_values = asdict(MapperConfig(**params))
    config_values["preset"] = actual
    return config_values


def build_score_events(track) -> list[ScoreNote]:
    """Build clean fretted-pitch attacks. Harmonics deliberately use fretted pitch.

    Tie destinations extend the preceding attack on the same string and are kept
    in ``source_notes`` so re-fingering can update every tie segment consistently.
    """
    string_pitch = {string.number: string.value for string in track.strings}
    events: list[ScoreNote] = []
    last_note_on_string: dict[int, int] = {}
    source_index = 0
    for _measure, _voice, _vi, _bi, beat, _measure_tick, start_tick, dur in iter_voice_beats_with_canonical_ticks(track):
        if beat is None:
            continue
        for note in beat.notes:
            this_index = source_index
            source_index += 1
            if note.type == NoteType.tie:
                previous = last_note_on_string.get(note.string)
                if previous is not None:
                    event = events[previous]
                    event.dur_ticks = max(event.dur_ticks, start_tick + dur - event.start_tick)
                    event.source_notes.append(note)
                    event.source_indices.append(this_index)
                continue
            pitch = int(string_pitch.get(note.string, 0) + note.value)
            event = ScoreNote(
                int(start_tick), int(dur), pitch, max(1, min(127, int(note.velocity))),
                source_notes=[note], source_indices=[this_index],
            )
            events.append(event)
            last_note_on_string[note.string] = len(events) - 1
    return events


def _absolute_events_to_track(events: Iterable[tuple[int, int, Any]]) -> MidiTrack:
    track = MidiTrack()
    previous = 0
    for tick, order, message in sorted(events, key=lambda item: (item[0], item[1])):
        tick = max(0, int(tick))
        track.append(message.copy(time=tick - previous))
        previous = tick
    track.append(MetaMessage("end_of_track", time=0))
    return track


def write_score_midi(events: list[ScoreNote], out_path: str | Path, song, *, track_name: str = "Score") -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    midi_events: list[tuple[int, int, Any]] = [
        (0, 0, MetaMessage("track_name", name=track_name, time=0)),
        (0, 1, MetaMessage("set_tempo", tempo=bpm2tempo(float(getattr(song, "tempo", 120) or 120)), time=0)),
    ]
    measure_tick = 0
    last_signature = None
    for header in getattr(song, "measureHeaders", []) or []:
        ts = header.timeSignature
        signature = (int(ts.numerator), int(ts.denominator.value))
        if signature != last_signature:
            midi_events.append((measure_tick, 2, MetaMessage("time_signature", numerator=signature[0], denominator=signature[1], time=0)))
            last_signature = signature
        measure_tick += int(round(signature[0] * TICKS_PER_BEAT * 4 / signature[1]))
    if last_signature is None:
        midi_events.append((0, 2, MetaMessage("time_signature", numerator=4, denominator=4, time=0)))

    # Tempo changes are attached to beats by gp_import/builders.
    seen_tempos = {0: float(getattr(song, "tempo", 120) or 120)}
    for track in getattr(song, "tracks", []) or []:
        for _m, _v, _vi, _bi, beat, _mt, start_tick, _dur in iter_voice_beats_with_canonical_ticks(track):
            change = getattr(getattr(beat, "effect", None), "mixTableChange", None)
            tempo = getattr(change, "tempo", None)
            if tempo is not None and getattr(tempo, "value", None):
                seen_tempos[int(start_tick)] = float(tempo.value)
        if len(seen_tempos) > 1:
            break
    for tick, bpm in seen_tempos.items():
        if tick:
            midi_events.append((tick, 1, MetaMessage("set_tempo", tempo=bpm2tempo(bpm), time=0)))

    for event in events:
        midi_events.append((event.start_tick, 10, Message("note_on", note=event.pitch, velocity=event.velocity, time=0)))
        midi_events.append((event.start_tick + event.dur_ticks, 5, Message("note_off", note=event.pitch, velocity=0, time=0)))
    midi = MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
    midi.tracks.append(_absolute_events_to_track(midi_events))
    midi.save(out_path)
    return out_path


def _mapper_config(params: dict[str, Any]) -> MapperConfig:
    usable = {key: value for key, value in params.items() if key != "preset"}
    return MapperConfig(**usable)


def map_with_gtrsnipe(
    events: list[ScoreNote], track_type: str, title: str, params: dict[str, Any],
    *, tempo: float = 120.0, time_signature: str = "4/4",
) -> tuple[list[MappedNote], str]:
    config = _mapper_config(params)
    musical: list[MusicalEvent] = []
    for index, event in enumerate(events):
        item = MusicalEvent(
            time=event.start_tick / TICKS_PER_BEAT,
            pitch=event.pitch,
            duration=event.dur_ticks / TICKS_PER_BEAT,
            velocity=event.velocity,
        )
        item.source_index = index  # type: ignore[attr-defined]  # VENDOR adapter metadata
        musical.append(item)
    started = time.monotonic()
    mapped_raw = GuitarMapper(config).map_events_to_fretboard(musical, no_articulations=False)
    mapped: list[MappedNote] = []
    for item in mapped_raw:
        source = events[item.source_index]  # type: ignore[attr-defined]
        mapped.append(MappedNote(
            source.start_tick, source.dur_ticks, source.pitch, source.velocity,
            int(item.string), int(item.fret), item.technique, list(source.source_indices),
        ))
    mapped_song = Song(
        tracks=[Track(events=mapped_raw, instrument_name="Bass" if track_type == "BASS" else "Guitar")],
        tempo=float(tempo),
        time_signature=time_signature,
        title=title,
    )
    score = AsciiTabGenerator._create_score_from_song(mapped_song)
    command = f"gpmidi playable-tabs preset={params['preset']} {datetime.now().date().isoformat()}"
    text = AsciiTabGenerator._format_score(score, command, 100, config.quantization_resolution, config)
    LOGGER.info("gtrsnipe mapped %s in %.2fs", title, time.monotonic() - started)
    return mapped, text


def _wrap_tuttut_ascii(
    text: str,
    max_width: int = 100,
    empty_measure_width: int = 16,
    total_measures: int | None = None,
) -> str:
    """Reflow tuttut output, expanding its zero-width silent measures."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    parsed: list[tuple[str, list[str]]] = []
    for line in lines:
        if " " not in line:
            raise ValueError("unexpected tuttut ASCII line without string label")
        label, body = line.split(" ", 1)
        if not body.startswith("||") or not body.endswith("|"):
            raise ValueError("tuttut ASCII has no expected outer bar lines")
        parsed.append((label, body[2:-1].split("|")))

    measure_counts = {len(measures) for _label, measures in parsed}
    if len(measure_counts) != 1:
        raise ValueError("tuttut ASCII strings have different measure counts")
    measure_count = measure_counts.pop()
    if total_measures is not None:
        if total_measures < measure_count:
            raise ValueError("requested measure count is shorter than tuttut output")
        for _label, measures in parsed:
            measures.extend([""] * (total_measures - measure_count))
        measure_count = total_measures
    widths: list[int] = []
    for index in range(measure_count):
        measure_widths = {len(measures[index]) for _label, measures in parsed}
        if len(measure_widths) != 1:
            raise ValueError(f"tuttut measure {index + 1} is not column-aligned")
        width = measure_widths.pop()
        widths.append(width or empty_measure_width)
        if width == 0:
            for _label, measures in parsed:
                measures[index] = "-" * empty_measure_width

    systems: list[str] = []
    start = 0
    label_width = max(len(label) for label, _measures in parsed)
    while start < measure_count:
        end = start
        content_width = 0
        while end < measure_count:
            candidate_width = content_width + widths[end]
            measure_total = end - start + 1
            line_width = label_width + 1 + candidate_width + measure_total + 2
            if line_width > max_width:
                break
            content_width = candidate_width
            end += 1
        if end == start:
            raise ValueError(f"tuttut measure {start + 1} exceeds printable width {max_width}")
        heading = f"Такты {start + 1}–{end}"
        staff = "\n".join(
            f"{label} ||{'|'.join(measures[start:end])}|"
            for label, measures in parsed
        )
        systems.append(f"{heading}\n{staff}")
        start = end
    return "\n\n".join(systems) + "\n"


def run_tuttut(
    score_path: str | Path,
    out_path: str | Path,
    track_type: str,
    *,
    total_measures: int | None = None,
) -> Path:
    """Run tuttut 0.0.6 and reflow its page-wide ASCII at measure boundaries."""
    import pretty_midi
    from tuttut.logic.tab import Tab
    from tuttut.logic.theory import Tuning

    score_path, out_path = Path(score_path), Path(out_path)
    tuning = Tuning(["G2", "D2", "A1", "E1"]) if track_type == "BASS" else Tuning()
    temp_name = out_path.stem + "-raw-tuttut"
    tab = Tab(
        temp_name,
        tuning,
        pretty_midi.PrettyMIDI(str(score_path)),
        weights={"b": 1, "height": 1, "length": 1, "n_changed_strings": 1},
        output_dir=str(out_path.parent),
    )
    tab.to_ascii()
    generated = (out_path.parent / temp_name).with_suffix(".txt")
    try:
        raw_text = generated.read_text(encoding="utf-8")
        out_path.write_text(
            _wrap_tuttut_ascii(raw_text, total_measures=total_measures),
            encoding="utf-8",
        )
    finally:
        # _wrap_tuttut_ascii has several ValueError paths; the raw page-wide file
        # must never survive into the job output the user downloads.
        generated.unlink(missing_ok=True)
    return out_path


def _count_source_notes(track) -> int:
    return sum(len(beat.notes) for *_prefix, beat, _mt, _bt, _dur in iter_voice_beats_with_canonical_ticks(track))


def generate_track(
    song,
    track,
    out_dir: str | Path,
    *,
    preset: str = "auto",
    overrides: dict[str, Any] | None = None,
    version: int = 1,
    generate_score: bool = True,
) -> TrackGeneration:
    from tab_print import ascii_to_pdf

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    track_type = resolve_track_type(track)
    if track_type not in {"GUITAR", "BASS"}:
        raise ValueError("playable tabs поддерживают только GUITAR/BASS")
    actual = auto_preset(track.name, track_type) if preset == "auto" else preset
    params = resolve_params(actual, overrides or {}, track_name=track.name, track_type=track_type)
    basename = safe_filename(track.name) or "Track"
    score_events = build_score_events(track)
    score_path = out_dir / f"{basename}.score.mid"
    if generate_score or not score_path.exists():
        write_score_midi(score_events, score_path, song, track_name=track.name)

    files = [score_path.name]
    status: dict[str, str] = {}
    if not has_standard_tuning(track, track_type):
        # Маппер всегда считает стандартный строй: таб играбелен в стандартном
        # строе, а перефингеровка исходного файла будет пропущена (refinger_gp).
        status["tuning"] = (
            "нестандартный строй: таб построен для стандартного строя, "
            "перефингеровка GP-файла для этого трека пропускается"
        )
        LOGGER.warning("track %r has a non-standard tuning; tabs assume standard, "
                       "re-fingering will be skipped", track.name)
    mapped: list[MappedNote] = []
    try:
        headers = getattr(song, "measureHeaders", []) or []
        if headers:
            first_ts = headers[0].timeSignature
            signature = f"{first_ts.numerator}/{first_ts.denominator.value}"
        else:
            signature = "4/4"
        mapped, text = map_with_gtrsnipe(
            score_events, track_type, track.name, params,
            tempo=float(getattr(song, "tempo", 120) or 120),
            time_signature=signature,
        )
        text_path = out_dir / f"{basename}.playable.gtrsnipe.txt"
        text_path.write_text(text, encoding="utf-8")
        files.append(text_path.name)
        pdf = ascii_to_pdf(text_path, track.name, preset=params["preset"], version=version)
        files.append(pdf.path.name)
        status["gtrsnipe"] = "ok" if not pdf.warning else f"ok; {pdf.warning}"
    except Exception as exc:
        LOGGER.exception("gtrsnipe failed for %s", track.name)
        status["gtrsnipe"] = f"failed: {str(exc)[:180]}"

    try:
        tuttut_path = out_dir / f"{basename}.playable.tuttut.txt"
        run_tuttut(score_path, tuttut_path, track_type, total_measures=len(track.measures))
        files.append(tuttut_path.name)
        pdf = ascii_to_pdf(tuttut_path, track.name, preset=params["preset"], version=version)
        files.append(pdf.path.name)
        status["tuttut"] = "ok" if not pdf.warning else f"ok; {pdf.warning}"
    except Exception as exc:
        LOGGER.warning("tuttut failed for %s: %s", track.name, exc)
        status["tuttut"] = f"failed: {str(exc)[:180]}"

    positions: list[tuple[int, int] | None] = [None] * _count_source_notes(track)
    for item in mapped:
        for source_index in item.source_indices:
            if source_index < len(positions):
                positions[source_index] = (item.string, item.fret)
    missing = sum(position is None for position in positions)
    mapper_failed = status.get("gtrsnipe", "").startswith("failed")
    report_path = out_dir / f"{basename}.unplayable_report.txt"
    if missing:
        if mapper_failed:
            lines = [
                f"{track.name}: маппер не выполнился, играбельность НЕ ПРОВЕРЕНА "
                f"({missing} нот без позиции). Статус: {status.get('gtrsnipe', '')}"
            ]
        else:
            lines = [f"{track.name}: {missing} нот/сегментов не получили играбельную позицию."]
        lines.extend(f"note_index={index}: mapper returned no position" for index, value in enumerate(positions) if value is None)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        files.append(report_path.name)
        status["unplayable"] = str(missing)
    else:
        report_path.unlink(missing_ok=True)
        status["unplayable"] = "0"

    return TrackGeneration(
        track.name, track_type, basename, params["preset"], params,
        files, status, mapped, positions, version=version,
    )


def fret_for_pitch(pitch: int, string: int, opens: list[int], max_fret: int) -> int:
    if not 0 <= string < len(opens):
        raise ValueError("string index out of range")
    fret = int(pitch) - int(opens[string])
    if not 0 <= fret <= max_fret:
        raise ValueError(f"pitch {pitch} is outside string {string} range")
    return fret


def iter_gpif_note_references(root: ET.Element, track_index: int) -> Iterable[tuple[ET.Element, int, ET.Element]]:
    notes = {item.get("id"): item for item in root.findall("Notes/Note")}
    beats = {item.get("id"): item for item in root.findall("Beats/Beat")}
    beats_of_voice = {item.get("id"): (item.findtext("Beats") or "").split() for item in root.findall("Voices/Voice")}
    voices_of_bar = {
        item.get("id"): [value for value in (item.findtext("Voices") or "").split() if value != "-1"]
        for item in root.findall("Bars/Bar")
    }
    for master_bar in root.findall("MasterBars/MasterBar"):
        bars = (master_bar.findtext("Bars") or "").split()
        if track_index >= len(bars):
            continue
        for voice_id in voices_of_bar.get(bars[track_index], []):
            for beat_id in beats_of_voice.get(voice_id, []):
                beat = beats.get(beat_id)
                if beat is None:
                    continue
                for token_index, note_id in enumerate((beat.findtext("Notes") or "").split()):
                    note = notes.get(note_id)
                    if note is not None:
                        yield beat, token_index, note


def iter_gpif_note_elements(root: ET.Element, track_index: int) -> Iterable[ET.Element]:
    for _beat, _token_index, note in iter_gpif_note_references(root, track_index):
        yield note


def _set_gpif_property(note: ET.Element, name: str, child: str, value: int) -> None:
    properties = note.find("Properties")
    if properties is None:
        properties = ET.SubElement(note, "Properties")
    prop = next((item for item in properties.findall("Property") if item.get("name") == name), None)
    if prop is None:
        prop = ET.SubElement(properties, "Property", {"name": name})
    target = prop.find(child)
    if target is None:
        target = ET.SubElement(prop, child)
    target.text = str(value)


def _next_numeric_id(container: ET.Element, child_tag: str) -> int:
    values = [int(item.get("id")) for item in container.findall(child_tag) if (item.get("id") or "").isdigit()]
    return max(values, default=0) + 1


def _materialize_gpif_track_occurrences(root: ET.Element, track_index: int) -> None:
    """Copy-on-write Bar→Voice→Beat chains for every target-track occurrence.

    GPIF deduplicates all these definitions, not only notes. Materialising the
    chain prevents rewriting one beat occurrence from changing earlier repeats
    or another track that references the same definition.
    """
    bars_container, voices_container, beats_container = root.find("Bars"), root.find("Voices"), root.find("Beats")
    if bars_container is None or voices_container is None or beats_container is None:
        raise ValueError("GPIF Bars/Voices/Beats container missing")
    bars = {item.get("id"): item for item in bars_container.findall("Bar")}
    voices = {item.get("id"): item for item in voices_container.findall("Voice")}
    beats = {item.get("id"): item for item in beats_container.findall("Beat")}
    next_bar = _next_numeric_id(bars_container, "Bar")
    next_voice = _next_numeric_id(voices_container, "Voice")
    next_beat = _next_numeric_id(beats_container, "Beat")

    for master_bar in root.findall("MasterBars/MasterBar"):
        bars_element = master_bar.find("Bars")
        bar_ids = (bars_element.text or "").split()
        if track_index >= len(bar_ids) or bar_ids[track_index] not in bars:
            continue
        bar = copy.deepcopy(bars[bar_ids[track_index]])
        bar.set("id", str(next_bar))
        next_bar += 1
        bars_container.append(bar)
        bar_ids[track_index] = bar.get("id")
        bars_element.text = " ".join(bar_ids)

        voices_element = bar.find("Voices")
        voice_ids = (voices_element.text or "").split()
        for voice_index, voice_id in enumerate(voice_ids):
            if voice_id == "-1" or voice_id not in voices:
                continue
            voice = copy.deepcopy(voices[voice_id])
            voice.set("id", str(next_voice))
            next_voice += 1
            voices_container.append(voice)
            voice_ids[voice_index] = voice.get("id")
            beats_element = voice.find("Beats")
            beat_ids = (beats_element.text or "").split()
            for beat_index, beat_id in enumerate(beat_ids):
                if beat_id not in beats:
                    continue
                beat = copy.deepcopy(beats[beat_id])
                beat.set("id", str(next_beat))
                next_beat += 1
                beats_container.append(beat)
                beat_ids[beat_index] = beat.get("id")
            beats_element.text = " ".join(beat_ids)
        voices_element.text = " ".join(voice_ids)


def patch_gpif_track_positions(
    root: ET.Element,
    track_index: int,
    positions: list[tuple[int, int] | None],
    *,
    num_strings: int,
) -> None:
    """Patch occurrence fingerings, cloning GPIF Note definitions when reused.

    GPIF deduplicates identical note definitions: a single ``Notes/Note`` id may
    be referenced hundreds of times. Different optimized positions therefore
    require per-occurrence clones and rewritten ``Beat/Notes`` references.
    """
    _materialize_gpif_track_occurrences(root, track_index)
    references = list(iter_gpif_note_references(root, track_index))
    if len(references) != len(positions):
        raise ValueError(f"GPIF occurrence count {len(references)} != parsed count {len(positions)}")
    container = root.find("Notes")
    if container is None:
        raise ValueError("GPIF Notes container missing")
    numeric_ids = [int(note.get("id")) for note in container.findall("Note") if (note.get("id") or "").isdigit()]
    next_id = max(numeric_ids, default=0) + 1
    for (beat, token_index, note), position in zip(references, positions):
        # Always clone: Note ids can be shared not only across repeated beats but
        # even across tracks. Leaving the original definition untouched prevents
        # one track's first assigned position from corrupting another track.
        target = copy.deepcopy(note)
        target.set("id", str(next_id))
        next_id += 1
        container.append(target)
        notes_element = beat.find("Notes")
        if notes_element is None:
            raise ValueError("GPIF Beat/Notes missing")
        tokens = (notes_element.text or "").split()
        tokens[token_index] = target.get("id") or ""
        notes_element.text = " ".join(tokens)
        if position is not None:
            gpif_string = num_strings - 1 - position[0]
            _set_gpif_property(target, "String", "String", gpif_string)
            _set_gpif_property(target, "Fret", "Fret", position[1])


def _track_note_signature(track) -> list[tuple[int, int, int]]:
    result = []
    string_pitch = {string.number: string.value for string in track.strings}
    for _m, _v, _vi, _bi, beat, _mt, start, dur in iter_voice_beats_with_canonical_ticks(track):
        for note in beat.notes:
            result.append((int(start), int(dur), int(string_pitch.get(note.string, 0) + note.value)))
    return result


def _verify_refinger(original_song, patched_path: Path, generations: dict[str, TrackGeneration]) -> None:
    patched_song = parse_song(patched_path)
    # `note_positions` holds gtrsnipe indices (0=highest). Re-parsing a .gp keeps
    # that convention (the ApolloTab adapter builds 0-based strings), but a .gp5
    # comes back through raw pyguitarpro, where 1=highest — which is exactly what
    # the write path emits. Compare in the parser's own base, or every .gp5
    # re-fingering fails this invariant on its own correct output.
    string_offset = 0 if is_gp7_gp8_archive(patched_path) else 1
    original_by_name = {track.name: track for track in original_song.tracks}
    patched_by_name = {track.name: track for track in patched_song.tracks}
    for name, original in original_by_name.items():
        patched = patched_by_name.get(name)
        if patched is None or _track_note_signature(original) != _track_note_signature(patched):
            raise ValueError(f"pitch/rhythm invariant failed for {name}")
    for name, generation in generations.items():
        patched = patched_by_name.get(name)
        if patched is None:
            raise ValueError(f"patched track missing: {name}")
        actual = [(note.string - string_offset, note.value) for *_prefix, beat, _mt, _bt, _dur in iter_voice_beats_with_canonical_ticks(patched) for note in beat.notes]
        for index, expected in enumerate(generation.note_positions):
            if expected is not None and index < len(actual) and actual[index] != expected:
                raise ValueError(f"fingering invariant failed for {name} note {index}: {actual[index]} != {expected}")


def refinger_gp(
    src_path: str | Path,
    generations: dict[str, TrackGeneration],
    out_path: str | Path,
    *,
    original_song=None,
) -> Path | None:
    """Записать перефингерованную копию. Возвращает out_path или None,
    если ни один трек не был перефингерован (все пропущены).

    Треки с нестандартным строем пропускаются с предупреждением: маппер
    считает стандартный гриф, и обратная запись его позиций в drop-D (и
    любой другой строй) меняла бы высоты. Раньше такой трек доходил до
    _verify_refinger, валил инвариант и перефингеровка отменялась для
    ВСЕГО файла. Верифицируются только реально применённые треки; полный
    pitch/rhythm-инвариант по-прежнему проверяет все треки файла.
    """
    src_path, out_path = Path(src_path), Path(out_path)
    original_song = original_song or parse_song(src_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    original_by_name = {track.name: track for track in original_song.tracks}

    def _tuning_allows(name: str, generation: TrackGeneration) -> bool:
        track = original_by_name.get(name)
        if track is None:
            warnings.warn(f"re-fingering skipped for {name}: track not found in source")
            return False
        if not has_standard_tuning(track, generation.track_type):
            warnings.warn(
                f"re-fingering skipped for {name}: non-standard tuning "
                f"(mapper positions assume the standard {generation.track_type} tuning)"
            )
            return False
        return True

    applied: dict[str, TrackGeneration] = {}
    if is_gp7_gp8_archive(src_path):
        with zipfile.ZipFile(src_path) as archive:
            root = ET.fromstring(archive.read("Content/score.gpif"))
        tracks = root.findall("Tracks/Track")
        for name, generation in generations.items():
            if not _tuning_allows(name, generation):
                continue
            matches = [(index, track) for index, track in enumerate(tracks) if (track.findtext("Name") or "") == name]
            if len(matches) != 1:
                warnings.warn(f"re-fingering skipped for {name}: track name is not unique")
                continue
            track_index, track_element = matches[0]
            if len(track_element.findall("Staves/Staff")) > 1:
                warnings.warn(f"re-fingering skipped for {name}: multi-staff track")
                continue
            num_strings = len(OPEN_PITCHES[generation.track_type])
            # String conventions (empirically verified on the GP8 sample):
            # gtrsnipe/ApolloTab: 0=highest; GPIF: 0=lowest. GPIF also
            # deduplicates Note definitions, so the helper clones as needed.
            patch_gpif_track_positions(
                root, track_index, generation.note_positions, num_strings=num_strings,
            )
            applied[name] = generation
        if not applied:
            return None
        temporary = _write_patched_gp(src_path, ET.tostring(root, encoding="utf-8"))
        shutil.move(temporary, out_path)
    elif src_path.suffix.lower() == ".gp5":
        song = guitarpro.parse(str(src_path))
        tracks_by_name = {track.name: track for track in song.tracks}
        for name, generation in generations.items():
            if not _tuning_allows(name, generation):
                continue
            track = tracks_by_name.get(name)
            if track is None:
                continue
            notes = [note for measure in track.measures for voice in measure.voices for beat in voice.beats for note in beat.notes]
            if len(notes) != len(generation.note_positions):
                raise ValueError(f"note count mismatch for {name}")
            for note, position in zip(notes, generation.note_positions):
                if position is not None:
                    note.string = position[0] + 1  # pyguitarpro: 1=highest
                    note.value = position[1]
            applied[name] = generation
        if not applied:
            return None
        guitarpro.write(song, str(out_path))
    else:
        raise ValueError("re-fingering поддерживает только .gp и .gp5")
    try:
        _verify_refinger(original_song, out_path, applied)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise
    return out_path


def generate_song(
    src_path: str | Path,
    out_dir: str | Path,
    *,
    preset: str = "auto",
    track_filters: list[str] | None = None,
) -> tuple[list[TrackGeneration], Path | None]:
    src_path, out_dir = Path(src_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    song = parse_song(src_path)
    selected: list[TrackGeneration] = []
    for track in song.tracks:
        track_type = resolve_track_type(track)
        if track_type not in {"GUITAR", "BASS"}:
            continue
        if track_filters and not any(token.lower() in track.name.lower() for token in track_filters):
            continue
        selected.append(generate_track(song, track, out_dir, preset=preset))
    refingered = None
    if selected and src_path.suffix.lower() in {".gp", ".gp5"}:
        candidate = out_dir / f"{safe_filename(src_path.stem)}_refingered{src_path.suffix.lower()}"
        # None = все треки пропущены (например, нестандартный строй)
        refingered = refinger_gp(src_path, {item.track_name: item for item in selected}, candidate, original_song=song)
    return selected, refingered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate playable guitar/bass tabs without changing normal MIDI export")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--outdir", type=Path)
    parser.add_argument("--preset", choices=["auto", "solo", "rhythm", "bass"], default="auto")
    parser.add_argument("--tracks", nargs="*")
    args = parser.parse_args(argv)
    outdir = args.outdir or args.input.with_name(args.input.stem + "_playable")
    generations, refingered = generate_song(args.input, outdir, preset=args.preset, track_filters=args.tracks)
    for result in generations:
        print(f"{result.track_name}: {result.preset}; {result.status}; {len(result.files)} files")
    if refingered:
        print(f"refingered: {refingered}")
    return 0 if generations else 1


if __name__ == "__main__":
    raise SystemExit(main())
