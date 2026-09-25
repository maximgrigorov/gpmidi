from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import re
import sys
import types
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import guitarpro
from guitarpro.models import NoteType, SlideType

logger = logging.getLogger("gpmidi.import")


def _find_apollotab_root() -> Path:
    """Locate the installed ApolloTab package.

    The hardcoded POSIX site-packages path reported "ApolloTab не установлен" on
    Windows and for user-site installs even when the package was importable.
    """
    try:
        spec = importlib.util.find_spec("ApolloTab")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        for location in (spec.submodule_search_locations or []):
            return Path(location)
        if spec.origin:
            return Path(spec.origin).parent
    for entry in sys.path:
        candidate = Path(entry) / "ApolloTab"
        if candidate.is_dir():
            return candidate
    return (
        Path(sys.prefix) / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages" / "ApolloTab"
    )


APOLLOTAB_ROOT = _find_apollotab_root()

APOLLOTAB_DYNAMIC_TO_VELOCITY = {
    "PPP": 15,
    "PP": 31,
    "P": 47,
    "MP": 63,
    "MF": 79,
    "F": 95,
    "FF": 111,
    "FFF": 127,
}

# Тики на четверть в адаптере (та же сетка, что TICKS_PER_BEAT конвертера).
TICKS_PER_QUARTER = 960

# GPIF <Automation type="Tempo"><Value> хранит пару «значение единица»:
# "120 2" = 120 четвертей/мин, "80 3" = 80 четвертей-с-точкой/мин = 120 BPM.
# Единица -> сколько четвертей длится одна доля темпа.
GPIF_TEMPO_UNIT_QUARTERS = {
    1: 0.5,   # восьмая
    2: 1.0,   # четверть
    3: 1.5,   # четверть с точкой
    4: 2.0,   # половинная
    5: 3.0,   # половинная с точкой
}


@dataclass
class GPDenominator:
    value: int


@dataclass
class GPTimeSignature:
    numerator: int
    denominator: GPDenominator


@dataclass
class GPMeasureHeader:
    timeSignature: GPTimeSignature


@dataclass
class GPTuplet:
    enters: int = 1
    times: int = 1


@dataclass
class GPDuration:
    value: int
    isDotted: bool = False
    tuplet: GPTuplet = field(default_factory=GPTuplet)
    time: int = 0


@dataclass
class GPTempoValue:
    value: float


@dataclass
class GPTempoAutomation:
    bar: int
    position: float
    value: float


@dataclass
class GPMixTableChange:
    tempo: GPTempoValue | None = None


@dataclass
class GPBeatEffect:
    mixTableChange: GPMixTableChange | None = None
    hairpin: str | None = None          # "Crescendo" | "Decrescendo" (GPIF <Hairpin>)
    grace: str | None = None            # "OnBeat" | "BeforeBeat" (GPIF <GraceNotes>)


@dataclass
class GPBeatStatus:
    name: str


@dataclass
class GPBendPoint:
    position: int
    value: int


@dataclass
class GPBend:
    points: list[GPBendPoint]


class NaturalHarmonic:
    pass


class ArtificialHarmonic:
    pass


class TappedHarmonic:
    pass


class PinchHarmonic:
    pass


class SemiHarmonic:
    pass


@dataclass
class GPNoteEffect:
    harmonic: Any = None
    tremoloPicking: Any = None
    trill: Any = None
    palmMute: bool = False
    staccato: bool = False
    bend: GPBend | None = None
    vibrato: bool = False
    vibratoType: str | None = None      # "Slight" | "Wide" (сайдкар из GPIF)
    slides: list[Any] = field(default_factory=list)
    hammer: bool = False
    tapping: bool = False               # GPIF Property "Tapped" (sidecar-извлечение)
    accent: int = 0                     # 0=нет, 1=Normal, 2=Heavy (GPIF <Accent> биты)
    letRing: bool = False


@dataclass
class GPNote:
    string: int
    value: int
    velocity: int
    effect: GPNoteEffect
    type: Any
    realValue: int
    harmonicFret: float = 0.0           # GPIF HarmonicFret (относительный лад узла)
    playedOffset: int = 0               # GPIF playback Offset, 480 PPQ
    playedDuration: float = 1.0         # GPIF playback Duration, множитель


@dataclass
class GPBeat:
    start: int
    duration: GPDuration
    notes: list[GPNote]
    effect: GPBeatEffect
    status: GPBeatStatus


@dataclass
class GPVoice:
    beats: list[GPBeat]


@dataclass
class GPMeasure:
    start: int
    header: GPMeasureHeader
    voices: list[GPVoice]


@dataclass
class GPString:
    number: int
    value: int


@dataclass
class GPChannel:
    instrument: int


@dataclass
class GPTrack:
    name: str
    strings: list[GPString]
    channel: GPChannel
    measures: list[GPMeasure]
    isPercussion: bool = False


@dataclass
class GPSong:
    title: str
    artist: str
    album: str
    tempo: float
    tracks: list[GPTrack]
    measureHeaders: list[GPMeasureHeader]


def is_gp7_gp8_archive(path: str | Path) -> bool:
    path = Path(path)
    if path.suffix.lower() != ".gp":
        return False
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            return "Content/score.gpif" in names
    except Exception:
        return False


def parse_song(path: str | Path):
    path = Path(path)
    if is_gp7_gp8_archive(path):
        sync_points = _count_gpif_sync_points(path)
        if sync_points:
            warnings.warn(
                f"Файл содержит {sync_points} audio sync-point(ов): в Guitar Pro "
                "воспроизведение идёт по темп-карте аудиодорожки, а не по нотному "
                "темпу. MIDI-экспорт использует НОТНЫЙ темп (Tempo-автоматизации), "
                "поэтому тайминг может отличаться от того, что слышно в GP.",
                stacklevel=2,
            )
        multivoice_bars = _count_gpif_multivoice_bars(path)
        if multivoice_bars:
            warnings.warn(
                f"Файл содержит {multivoice_bars} такт(ов) с несколькими голосами "
                "(Voice 2+). ApolloTab склеивает голоса последовательно, поэтому "
                "ноты второго голоса экспортируются с НЕВЕРНЫМ таймингом (сдвиг). "
                "См. B4: полноценная поддержка мультиголоса пока не реализована.",
                stacklevel=2,
            )

        played_offsets = _count_gpif_significant_played_offsets(path)
        if played_offsets:
            warnings.warn(
                f"Файл содержит {played_offsets} нот(ы) со значительным сдвигом "
                "в слое «как сыграно» (<Offset> на ноте, > 1/32). По умолчанию "
                "эти сдвиги сохраняются только на соло-дорожках; на остальных "
                "эталон — нотная запись. Опция preserve_gp_played_offsets "
                "включает их на всех Guitar/Bass, --no-preserve-gp-played-offsets "
                "выключает везде.",
                stacklevel=2,
            )

        # B14: колонки <Bars> в MasterBar идут ПО НОТОНОСЦАМ (staves), а
        # ApolloTab считает «одна колонка = один трек». Любой трек с двумя
        # нотоносцами (piano, вокал) сдвигает все последующие треки на чужие
        # колонки, а хвостовые колонки теряются. Разворачиваем мульти-стафф
        # треки в отдельные треки "<имя> (Staff N)" ДО парсинга.
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("Content/score.gpif"))
        expanded = _expand_multistaff_tracks(root)
        parse_path = path
        if expanded:
            warnings.warn(
                f"Файл содержит {expanded} трек(ов) с несколькими нотоносцами: "
                "каждый нотоносец экспортируется отдельным треком "
                "\"<имя> (Staff N)\" (иначе ApolloTab смещает все последующие "
                "треки на чужие партии — B14).",
                stacklevel=2,
            )
            parse_path = _write_patched_gp(path, ET.tostring(root, encoding="utf-8"))

        try:
            tempo_automations = _extract_gpif_tempo_automations_root(root)
            drum_tables = _extract_gpif_percussion_articulations_root(root)
            note_extras = _extract_gpif_note_extras_root(root)
            raw = _parse_gp7_gp8_with_apollotab(parse_path)
        finally:
            if parse_path != path:
                try:
                    Path(parse_path).unlink()
                except OSError:
                    pass
        return adapt_apollotab_song(
            raw,
            tempo_automations=tempo_automations,
            drum_tables=drum_tables,
            note_extras=note_extras,
        )
    return guitarpro.parse(str(path))


def _expand_multistaff_tracks(root: ET.Element) -> int:
    """Развернуть мульти-стафф треки в отдельные Track-узлы (по одному на
    нотоносец), с уникальными id и обновлённым списком MasterTrack/Tracks.
    Возвращает число развёрнутых (исходных мульти-стафф) треков. Мутирует root.
    """
    import copy as _copy

    tracks_el = root.find("Tracks")
    if tracks_el is None:
        return 0
    originals = list(tracks_el)
    if not any(len(tr.findall("Staves/Staff")) > 1 for tr in originals):
        return 0

    numeric_ids = []
    for tr in originals:
        try:
            numeric_ids.append(int(tr.get("id", "")))
        except ValueError:
            pass
    next_id = (max(numeric_ids) + 1) if numeric_ids else 1000

    expanded_count = 0
    new_tracks: list[ET.Element] = []
    id_order: list[str] = []
    for tr in originals:
        staffs = tr.findall("Staves/Staff")
        if len(staffs) <= 1:
            new_tracks.append(tr)
            id_order.append(tr.get("id", ""))
            continue
        expanded_count += 1
        base_name = tr.findtext("Name") or "Track"
        for si, staff in enumerate(staffs):
            clone = _copy.deepcopy(tr)
            staves_el = clone.find("Staves")
            for st in list(staves_el):
                staves_el.remove(st)
            staves_el.append(_copy.deepcopy(staff))
            name_el = clone.find("Name")
            if name_el is None:
                name_el = ET.SubElement(clone, "Name")
            name_el.text = f"{base_name} (Staff {si + 1})"
            if si == 0:
                new_id = tr.get("id", "")
            else:
                new_id = str(next_id)
                next_id += 1
                clone.set("id", new_id)
            new_tracks.append(clone)
            id_order.append(new_id)

    for tr in originals:
        tracks_el.remove(tr)
    for tr in new_tracks:
        tracks_el.append(tr)

    mt_tracks = root.find("MasterTrack/Tracks")
    if mt_tracks is not None:
        mt_tracks.text = " ".join(id_order)
    return expanded_count


def _write_patched_gp(src: Path, gpif_bytes: bytes) -> Path:
    """Скопировать .gp во временный файл, заменив Content/score.gpif."""
    import tempfile

    fd, tmp_name = tempfile.mkstemp(suffix=".gp")
    os.close(fd)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            if name == "Content/score.gpif":
                zout.writestr(name, gpif_bytes)
            else:
                zout.writestr(name, zin.read(name))
    return Path(tmp_name)


def _extract_gpif_percussion_articulations_root(root: ET.Element) -> dict[int, list[int]]:
    """track_index -> плоский список OutputMidiNumber (GM-нота) по Articulation.

    ApolloTab не заполняет track.percussion_articulations для GP8: в GPIF
    <Articulations> вложены в <InstrumentSet>/<Elements>/<Element>, а парсер
    ищет их прямым ребёнком <Track>. note.percussion_articulation — это индекс
    в ПЛОСКИЙ список Articulation по документному порядку; строим его сами.
    """
    tables: dict[int, list[int]] = {}
    for ti, track in enumerate(root.findall("Tracks/Track")):
        flat: list[int] = []
        for elem in track.findall(".//InstrumentSet/Elements/Element"):
            for art in elem.findall("Articulations/Articulation"):
                try:
                    flat.append(int(art.findtext("OutputMidiNumber") or -1))
                except ValueError:
                    flat.append(-1)
        if flat:
            tables[ti] = flat
    return tables


def _extract_gpif_note_extras_root(root: ET.Element) -> dict[tuple[int, int, int, int], dict]:
    """Извлечь note-свойства, которые ApolloTab не парсит.

    Помимо Tapped и Vibrato сохраняем playback-слой GP8: Offset (480 PPQ)
    и Duration (множитель нотной длительности). По умолчанию экспортёр его
    игнорирует; данные нужны opt-in преобразованиям.

    Адрес ноты: (track_idx, measure_idx, beat_pos, note_pos), где track_idx —
    индекс КОЛОНКИ Bars (после разворачивания мульти-стафф треков совпадает
    с индексом трека), beat_pos — позиция бита в такте при ПОСЛЕДОВАТЕЛЬНОЙ
    склейке используемых голосов (так же, как ApolloTab отдаёт measure.beats),
    note_pos — индекс ноты в бите в порядке GPIF <Notes>.
    """
    tapped_ids = set()
    vibrato_by_id: dict[str, str] = {}
    played_by_id: dict[str, dict[str, int | float]] = {}
    for note in root.findall("Notes/Note"):
        note_id = note.get("id")
        if note_id is None:
            continue
        for prop in note.findall("Properties/Property"):
            if prop.get("name") in ("Tapped", "LeftHandTapped") and prop.find("Enable") is not None:
                tapped_ids.add(note_id)
        # <Vibrato>Slight|Wide</Vibrato> — ДОЧЕРНИЙ тег ноты, не Property.
        # ApolloTab отдаёт vibrato булевым, то есть Slight и Wide схлопываются
        # в "да/нет", и обе играются одинаково глубоко. В партитуре пользователя
        # все 53 ноты помечены Slight, а игрались на глубине 80/127.
        vib = note.find("Vibrato")
        vib_text = (vib.text or "").strip() if vib is not None else ""
        if vib_text:
            vibrato_by_id[note_id] = vib_text

        offset_text = note.findtext("Offset")
        duration_text = note.findtext("Duration")
        if offset_text is not None or duration_text is not None:
            try:
                played_offset = int(offset_text or 0)
            except ValueError:
                played_offset = 0
            try:
                played_duration = float(duration_text or 1.0)
            except ValueError:
                played_duration = 1.0
            played_by_id[note_id] = {
                "played_offset": played_offset,
                "played_duration": played_duration,
            }
    if not tapped_ids and not vibrato_by_id and not played_by_id:
        return {}

    notes_of_beat = {
        b.get("id"): (b.findtext("Notes") or "").split()
        for b in root.findall("Beats/Beat")
    }
    beats_of_voice = {
        v.get("id"): (v.findtext("Beats") or "").split()
        for v in root.findall("Voices/Voice")
    }
    voices_of_bar = {
        bar.get("id"): [v for v in (bar.findtext("Voices") or "").split() if v != "-1"]
        for bar in root.findall("Bars/Bar")
    }

    extras: dict[tuple[int, int, int, int], dict] = {}
    for mi, mbar in enumerate(root.findall("MasterBars/MasterBar")):
        bar_ids = (mbar.findtext("Bars") or "").split()
        for ti, bar_id in enumerate(bar_ids):
            beat_pos = 0
            for vid in voices_of_bar.get(bar_id, []):
                for bid in beats_of_voice.get(vid, []):
                    for npos, nid in enumerate(notes_of_beat.get(bid, [])):
                        address = (ti, mi, beat_pos, npos)
                        if nid in tapped_ids:
                            extras.setdefault(address, {})["tapping"] = True
                        if nid in vibrato_by_id:
                            extras.setdefault(address, {})["vibrato_type"] = vibrato_by_id[nid]
                        if nid in played_by_id:
                            extras.setdefault(address, {}).update(played_by_id[nid])
                    beat_pos += 1
    return extras


def _count_gpif_multivoice_bars(path: Path) -> int:
    """Число тактов (Bar) с более чем одним реально используемым голосом.

    В GPIF <Bar><Voices>0 1 -1 -1</Voices></Bar>: каждый не -1 = используемый
    голос. ApolloTab отдаёт только плоский measure.beats (голоса склеены
    последовательно), поэтому второй голос ломает тайминг. Детект нужен, чтобы
    ЯВНО предупредить пользователя о тихом баге B4.
    """
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("Content/score.gpif"))
    count = 0
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Bar":
            continue
        for child in elem:
            if child.tag.split("}")[-1] != "Voices":
                continue
            used = [tok for tok in (child.text or "").split() if tok != "-1"]
            if len(used) > 1:
                count += 1
    return count


def _count_gpif_sync_points(path: Path) -> int:
    """Число audio sync-point'ов в score.gpif (Automation Type=SyncPoint).

    Sync-point'ы появляются, когда к партитуре привязана аудиодорожка: они
    перебивают нотный темп ПРИ ВОСПРОИЗВЕДЕНИИ В GP. На экспорт не влияют
    (мы берём Tempo-автоматизации), но об этом стоит предупредить пользователя.
    """
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("Content/score.gpif"))
    count = 0
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Automation":
            continue
        for child in elem:
            if child.tag.split("}")[-1] == "Type" and (child.text or "").strip() == "SyncPoint":
                count += 1
    return count


def _count_gpif_significant_played_offsets(path: Path) -> int:
    """Число нот с |<Offset>| > 60 (в тиках 480 ppq, т.е. больше 1/32).

    GP8 хранит на нотах слой «как сыграно» (<Offset> — сдвиг старта,
    <Duration> — множитель длительности); плеер GP и его MIDI-экспорт играют
    по нему. Наш экспорт СОЗНАТЕЛЬНО идёт по нотной записи (выбор
    пользователя), микро-хуманизация (|offset| <= 60) не в счёт.
    """
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("Content/score.gpif"))
    count = 0
    for note in root.findall("Notes/Note"):
        try:
            off = int(note.findtext("Offset") or 0)
        except ValueError:
            continue
        if abs(off) > 60:
            count += 1
    return count


def _extract_gpif_tempo_automations_root(root: ET.Element) -> list[GPTempoAutomation]:
    automations: list[GPTempoAutomation] = []

    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Automation":
            continue
        data = {child.tag.split("}")[-1]: (child.text or "").strip() for child in elem}
        if data.get("Type") != "Tempo":
            continue

        value_match = re.match(r"\s*(-?\d+(?:\.\d+)?)(?:\s+(\d+))?", data.get("Value", ""))
        if not value_match:
            continue

        # Второй токен <Value> — единица темпа. Раньше он отбрасывался, и темп,
        # записанный от восьмых/половинных/четверти с точкой, экспортировался
        # с прямо неверным BPM ("80 3" читалось как 80 вместо 120).
        bpm = float(value_match.group(1))
        unit_token = value_match.group(2)
        if unit_token is not None:
            unit = GPIF_TEMPO_UNIT_QUARTERS.get(int(unit_token))
            if unit is None:
                logger.warning(
                    "неизвестная единица темпа %r в GPIF Automation (Value=%r); "
                    "читаю как четверти", unit_token, data.get("Value", ""),
                )
            else:
                bpm *= unit

        automations.append(
            GPTempoAutomation(
                bar=int(data.get("Bar", "0") or 0),
                # GPIF stores Position as a decimal beat offset; int() raised
                # ValueError and aborted the whole parse on an off-beat tempo mark.
                position=float(data.get("Position", "0") or 0),
                value=bpm,
            )
        )

    automations.sort(key=lambda item: (item.bar, item.position))
    return automations


def _parse_gp7_gp8_with_apollotab(path: Path):
    if not APOLLOTAB_ROOT.exists():
        raise RuntimeError(
            "ApolloTab не установлен. Добавьте его в requirements и пересоберите контейнер."
        )

    pkg = sys.modules.get("ApolloTab")
    if pkg is None:
        pkg = types.ModuleType("ApolloTab")
        pkg.__path__ = [str(APOLLOTAB_ROOT)]
        sys.modules["ApolloTab"] = pkg

    note_mod = importlib.import_module("ApolloTab.models.note")
    bend_init = note_mod.BendData.__init__
    if not getattr(note_mod.BendData.__init__, "_hermes_patched", False):
        def patched(self, *args, **kwargs):
            kwargs.pop("bend_style", None)
            return bend_init(self, *args, **kwargs)
        patched._hermes_patched = True  # type: ignore[attr-defined]
        note_mod.BendData.__init__ = patched

    parser = importlib.import_module("ApolloTab.parser")
    return parser.parse_score(str(path))


def adapt_apollotab_song(
    song: Any,
    tempo_automations: list[GPTempoAutomation] | None = None,
    drum_tables: dict[int, list[int]] | None = None,
    note_extras: dict[tuple[int, int, int, int], dict] | None = None,
) -> GPSong:
    tempo_automations = tempo_automations or []
    drum_tables = drum_tables or {}
    note_extras = note_extras or {}
    # Базовым темпом первая автоматизация становится только если она реально
    # стоит в начале партитуры. Иначе (автоматизация в такте N) вступление
    # играло её темпом, а в самой точке смены set_tempo не эмитился — билдеры
    # дедуплицируют равные значения.
    first_automation = tempo_automations[0] if tempo_automations else None
    if first_automation is not None and first_automation.bar == 0 and first_automation.position <= 0:
        base_tempo = first_automation.value
    else:
        base_tempo = float(getattr(song, "tempo", 120) or 120)
    tracks = [
        adapt_apollotab_track(
            track,
            tempo_automations=tempo_automations,
            drum_table=drum_tables.get(ti),
            track_note_extras={
                addr[1:]: flags for addr, flags in note_extras.items() if addr[0] == ti
            },
        )
        for ti, track in enumerate(getattr(song, "tracks", []))
    ]
    measure_headers: list[GPMeasureHeader] = []
    max_measures = max((len(track.measures) for track in tracks), default=0)
    for idx in range(max_measures):
        if tracks and idx < len(tracks[0].measures):
            measure_headers.append(tracks[0].measures[idx].header)
        else:
            measure_headers.append(GPMeasureHeader(timeSignature=GPTimeSignature(4, GPDenominator(4))))
    return GPSong(
        title=getattr(song, "title", "") or "",
        artist=getattr(song, "artist", "") or "",
        album=getattr(song, "album", "") or "",
        tempo=base_tempo,
        tracks=tracks,
        measureHeaders=measure_headers,
    )


def adapt_apollotab_track(
    track: Any,
    tempo_automations: list[GPTempoAutomation] | None = None,
    drum_table: list[int] | None = None,
    track_note_extras: dict[tuple[int, int, int], dict] | None = None,
) -> GPTrack:
    tempo_automations = tempo_automations or []
    track_note_extras = track_note_extras or {}
    strings = [GPString(number=i, value=int(v)) for i, v in enumerate(getattr(track, "strings", []) or [])]
    is_percussion = bool(getattr(track, "is_percussion", False))
    measures: list[GPMeasure] = []
    current_tick = 960
    for idx, measure in enumerate(getattr(track, "measures", []) or []):
        measure_tempo_automations = [a for a in tempo_automations if a.bar == idx]
        measure_extras = {
            addr[1:]: flags for addr, flags in track_note_extras.items() if addr[0] == idx
        }
        adapted_measure = adapt_apollotab_measure(
            measure, current_tick,
            tempo_automations=measure_tempo_automations,
            drum_table=drum_table if is_percussion else None,
            measure_note_extras=measure_extras,
        )
        measures.append(adapted_measure)
        current_tick += sum(beat.duration.time for beat in adapted_measure.voices[0].beats)
    return GPTrack(
        name=getattr(track, "name", "") or "",
        strings=strings,
        channel=GPChannel(instrument=int(getattr(track, "instrument", 0) or 0)),
        measures=measures,
        isPercussion=is_percussion,
    )


def adapt_apollotab_measure(
    measure: Any,
    start_tick: int,
    tempo_automations: list[GPTempoAutomation] | None = None,
    drum_table: list[int] | None = None,
    measure_note_extras: dict[tuple[int, int], dict] | None = None,
) -> GPMeasure:
    tempo_automations = sorted(tempo_automations or [], key=lambda item: item.position)
    measure_note_extras = measure_note_extras or {}
    numerator, denominator = getattr(measure, "time_signature", (4, 4)) or (4, 4)
    header = GPMeasureHeader(timeSignature=GPTimeSignature(int(numerator), GPDenominator(int(denominator))))
    beats: list[GPBeat] = []
    beat_start = start_tick
    tempo_idx = 0
    for beat_pos, beat in enumerate(getattr(measure, "beats", []) or []):
        beat_extras = {
            addr[1]: flags for addr, flags in measure_note_extras.items() if addr[0] == beat_pos
        }
        adapted_beat = adapt_apollotab_beat(beat, beat_start, drum_table=drum_table, beat_note_extras=beat_extras)
        rel_start = beat_start - start_tick
        # Position хранится в четвертях от начала такта, rel_start — в тиках
        # (960 на четверть). Прямое сравнение схлопывало любую ненулевую
        # позицию к началу такта (Position=2 удовлетворял "2 <= 960" уже на
        # второй доле).
        while (tempo_idx < len(tempo_automations)
               and tempo_automations[tempo_idx].position * TICKS_PER_QUARTER <= rel_start):
            adapted_beat.effect.mixTableChange = GPMixTableChange(
                tempo=GPTempoValue(value=tempo_automations[tempo_idx].value)
            )
            tempo_idx += 1
        beats.append(adapted_beat)
        beat_start += adapted_beat.duration.time

    # B6: ApolloTab добавляет по одному пустому такту-паузе на каждый
    # неиспользуемый голос (-1). В 4/4 с одним голосом это 3 фантомные
    # четвертные паузы в хвосте (сумма длительностей такта > канонической).
    # Пауз-хвост без нот не влияет на ноты, но ломает инвариант длины такта;
    # обрезаем ИЗБЫТОК за пределами канонической длины (реальные ноты не
    # трогаем — они никогда не выходят за такт).
    canonical = int(round(int(numerator) * 960 * 4.0 / int(denominator)))
    while (
        len(beats) > 1
        and not beats[-1].notes
        and sum(b.duration.time for b in beats) - beats[-1].duration.time >= canonical
    ):
        beats.pop()

    return GPMeasure(start=start_tick, header=header, voices=[GPVoice(beats=beats)])


def adapt_apollotab_beat(
    beat: Any,
    start_tick: int,
    drum_table: list[int] | None = None,
    beat_note_extras: dict[int, dict] | None = None,
) -> GPBeat:
    beat_note_extras = beat_note_extras or {}
    duration = adapt_apollotab_duration(beat)
    notes = [
        adapt_apollotab_note(note, beat, drum_table=drum_table, extras=beat_note_extras.get(npos))
        for npos, note in enumerate(getattr(beat, "notes", []) or [])
        if not getattr(note, "is_rest", False)
    ]
    status = GPBeatStatus(name="rest" if getattr(beat, "is_rest", False) else "normal")
    hairpin = getattr(beat, "crescendo", None)
    grace = getattr(beat, "grace_type", None)
    return GPBeat(
        start=start_tick,
        duration=duration,
        notes=notes,
        effect=GPBeatEffect(
            mixTableChange=None,
            hairpin=hairpin if hairpin in ("Crescendo", "Decrescendo") else None,
            grace=grace if grace in ("OnBeat", "BeforeBeat") else None,
        ),
        status=status,
    )


def adapt_apollotab_duration(beat: Any) -> GPDuration:
    raw_value = int(getattr(getattr(beat, "duration", None), "value", getattr(beat, "duration", 4)))
    duration = GPDuration(
        value=raw_value,
        isDotted=bool(getattr(beat, "is_dotted", False)),
        tuplet=GPTuplet(
            enters=int(getattr(beat, "tuplet_numerator", -1) or -1) if int(getattr(beat, "tuplet_numerator", -1) or -1) > 0 else 1,
            times=int(getattr(beat, "tuplet_denominator", -1) or -1) if int(getattr(beat, "tuplet_denominator", -1) or -1) > 0 else 1,
        ),
    )
    base = 960 * 4.0 / raw_value
    if duration.isDotted:
        base *= 1.5
    if duration.tuplet.enters > 1 and duration.tuplet.times > 0:
        base = base * duration.tuplet.times / duration.tuplet.enters
    duration.time = int(round(base))
    return duration


def adapt_apollotab_note(
    note: Any,
    beat: Any,
    drum_table: list[int] | None = None,
    extras: dict | None = None,
) -> GPNote:
    extras = extras or {}
    string_index = int(getattr(note, "string", 0) or 0)
    fret = int(getattr(note, "fret", 0) or 0)
    dynamics = str(getattr(beat, "dynamics", "") or "").upper()
    velocity = APOLLOTAB_DYNAMIC_TO_VELOCITY.get(dynamics, int(getattr(note, "velocity", 95) or 95))
    real_value = int(getattr(note, "midi_pitch", 0) or 0)

    # Перкуссия (GP8): string/fret = -1, вместо них — индекс артикуляции в
    # плоскую таблицу Articulation трека; переводим в GM-ноту и кладём её в
    # value при string=0 (нулевая настройка) — общая формула string+fret
    # тогда даёт GM-ноту без спецветок в билдерах. B10.
    if bool(getattr(note, "is_percussion", False)) or string_index < 0:
        art_idx = int(getattr(note, "percussion_articulation", -1))
        gm_note = -1
        if drum_table and 0 <= art_idx < len(drum_table):
            gm_note = drum_table[art_idx]
        if gm_note < 0:
            gm_note = max(0, real_value)
        string_index, fret, real_value = 0, gm_note, gm_note

    effect = GPNoteEffect(
        harmonic=adapt_harmonic(note),
        tremoloPicking=int(getattr(beat, "tremolo_picking", 0) or 0) or None,
        trill=bool(getattr(note, "trill_value", 0) or 0) or None,
        palmMute=bool(getattr(note, "is_palm_mute", False)),
        staccato=bool(getattr(note, "is_staccato", False)),
        bend=adapt_bend(getattr(note, "bend", None)),
        vibrato=bool(getattr(note, "vibrato", None)),
        vibratoType=extras.get("vibrato_type"),
        slides=adapt_slides(note),
        hammer=bool(getattr(note, "is_hammer_pull_origin", False)),
        tapping=bool(extras.get("tapping", False)),
        accent=int(getattr(note, "accentuated_type", 0) or 0),
        letRing=bool(getattr(note, "is_let_ring", False)),
    )
    note_type = NoteType.tie if bool(getattr(note, "is_tie_destination", False)) else NoteType.normal
    return GPNote(
        string=string_index,
        value=fret,
        velocity=velocity,
        effect=effect,
        type=note_type,
        realValue=real_value,
        harmonicFret=float(getattr(note, "harmonic_value", 0.0) or 0.0),
        playedOffset=int(extras.get("played_offset", 0) or 0),
        playedDuration=float(extras.get("played_duration", 1.0) or 1.0),
    )


def adapt_harmonic(note: Any) -> Any:
    harmonic_type = getattr(note, "harmonic_type", None)
    if not harmonic_type:
        return None
    name = str(harmonic_type)
    mapping = {
        "Natural": NaturalHarmonic,
        "Artificial": ArtificialHarmonic,
        "Tap": TappedHarmonic,
        "Pinch": PinchHarmonic,
        "Semi": SemiHarmonic,
    }
    cls = mapping.get(name)
    return cls() if cls else None


def adapt_bend(bend: Any) -> GPBend | None:
    if bend is None:
        return None
    bend_type = str(getattr(bend, "bend_type", "") or "")
    max_value = int(getattr(bend, "max_value", 0) or 0)
    final_value = int(getattr(bend, "value", 0) or 0)
    raw_points = list(getattr(bend, "points", []) or [])

    if raw_points and bend_type.startswith("BendType."):
        first_value = int(raw_points[0][1])

        if bend_type == "BendType.BEND":
            target = max_value or final_value or max(int(v) for _, v in raw_points)
            points = [GPBendPoint(position=0, value=first_value)]
            if target != first_value:
                points.append(GPBendPoint(position=3, value=target))
            points.append(GPBendPoint(position=12, value=final_value or target))
            return GPBend(points=points)

        if bend_type == "BendType.PREBEND":
            end_value = final_value or max_value or first_value
            return GPBend(points=[
                GPBendPoint(position=0, value=first_value),
                GPBendPoint(position=12, value=end_value),
            ])

        if bend_type == "BendType.BEND_RELEASE":
            peak = max_value or max(int(v) for _, v in raw_points)
            if first_value > 0:
                return GPBend(points=[
                    GPBendPoint(position=0, value=first_value),
                    GPBendPoint(position=3, value=first_value),
                    GPBendPoint(position=6, value=final_value),
                    GPBendPoint(position=12, value=final_value),
                ])
            return GPBend(points=[
                GPBendPoint(position=0, value=0),
                GPBendPoint(position=3, value=peak),
                GPBendPoint(position=6, value=peak),
                GPBendPoint(position=9, value=final_value),
                GPBendPoint(position=12, value=final_value),
            ])

    points = []
    for position, value in raw_points:
        points.append(GPBendPoint(position=int(position), value=int(value)))
    return GPBend(points=points) if points else None


def adapt_slides(note: Any) -> list[Any]:
    slides: list[Any] = []
    slide_in = getattr(note, "slide_in_type", None)
    slide_out = getattr(note, "slide_out_type", None)
    if slide_in is not None:
        text = str(slide_in)
        if "Below" in text:
            slides.append(SlideType.intoFromBelow)
        elif "Above" in text:
            slides.append(SlideType.intoFromAbove)
    if slide_out is not None:
        text = str(slide_out)
        if "Legato" in text:
            slides.append(SlideType.legatoSlideTo)
        elif "Shift" in text:
            slides.append(SlideType.shiftSlideTo)
        elif "OutUp" in text:
            slides.append(SlideType.outUpwards)
        elif "OutDown" in text:
            slides.append(SlideType.outDownwards)
    return slides
