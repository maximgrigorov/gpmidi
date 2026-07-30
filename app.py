from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

import guitarpro
from mido import MidiFile

from gp_import import parse_song
from gp_to_shreddage import (
    build_combined_midi,
    PITCH_BEND_RANGE_ST,
    TICKS_PER_BEAT,
    build_drum_midi,
    build_instrument_midi,
    build_other_midi,
    detect_track_type,
    iter_voice_beats_with_canonical_ticks,
    resolve_track_type,
    safe_filename,
)
from articulation_config import config_for_track_type
from playable_tabs import (
    TUNABLE_PARAMS,
    TrackGeneration,
    build_score_events,
    generate_track,
    map_with_gtrsnipe,
    refinger_gp,
    validate_params,
)
from tab_print import print_pdf

APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = APP_ROOT / "data"
SESSIONS_ROOT = DATA_ROOT / "sessions"
ALLOWED_EXTENSIONS = {".gp", ".gp3", ".gp4", ".gp5", ".gpx"}
MAX_CONTENT_LENGTH = 128 * 1024 * 1024
SESSION_HISTORY_LIMIT = 8
DEFAULT_TEMPO_US = 500000
CUPS_SERVER = os.environ.get("CUPS_SERVER", "192.168.20.64")
CUPS_PRINTER = os.environ.get("CUPS_PRINTER", "")
MIDI_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

TRACK_LABELS = {
    "GUITAR": "Гитара / Hydra",
    "BASS": "Бас / Darkwall",
    "DRUMS": "Барабаны / Shreddage Drums",
    "OTHER": "Без маппинга",
}

FIX_DESCRIPTIONS = {
    "GUITAR": [
        "Shreddage Hydra keyswitch-мэппинг",
        "коррекция артикуляций",
        "pitch bend / slide перенос",
        "vibrato -> CC1",
    ],
    "BASS": [
        "Shreddage Darkwall keyswitch-мэппинг",
        "коррекция артикуляций",
        "pitch bend / slide перенос",
        "vibrato -> CC1",
    ],
    "DRUMS": [
        "перевод GM-нот в раскладку Shreddage Drums",
        "remap: High Tom/Ride 2/Surdo, предупреждение о Side Stick",
    ],
    "OTHER": ["ноты скопированы как есть без артикуляционного маппинга"],
}


def _keyswitch_display_maps() -> dict[str, dict[int, str]]:
    """KS-нота -> имя артикуляции, из версионируемых YAML-конфигов
    (config/articulation_maps/), а не из захардкоженного словаря."""
    maps: dict[str, dict[int, str]] = {"OTHER": {}, "DRUMS": {}}
    for track_type in ("GUITAR", "BASS"):
        cfg = config_for_track_type(track_type)
        ks_map: dict[int, str] = {}
        for name, spec in (cfg.get("keyswitches") or {}).items():
            note = int(spec["note"])
            if note not in ks_map:  # vel-зоны (rake/pinch) не перетирают sustain
                ks_map[note] = name
        maps[track_type] = ks_map
    return maps


ARTICULATION_MAPS = _keyswitch_display_maps()
HYDRA_PITCH_BEND_RANGE = int(round(float(
    (config_for_track_type("GUITAR") or {}).get(
        "pitch_bend_range", PITCH_BEND_RANGE_ST
    )
)))

ARTICULATION_LABELS = {
    "sustain": "sustain",
    "palm_mute": "mute",
    "mute": "mute",
    "staccato": "staccato",
    "staccato_long": "staccato",
    "staccato_short": "staccato (short)",
    "sustain_short": "sustain (short)",
    "buzz_trill": "buzz trill",
    "tremolo": "tremolo",
    "tapping": "tapping",
    "harmonics": "harmonics",
    "choke": "choke",
    "fx": "fx",
    "power_chord_sustain": "pc sustain",
    "power_chord_mute": "pc mute",
    "power_chord_staccato": "pc staccato",
    "hit": "hit",
    "other": "other",
}


def infer_instrument_preset(track_name: str, track_type: str) -> str:
    low = (track_name or "").lower()
    if "drum" in low or "perc" in low or "kick" in low or "snare" in low:
        return "drums"
    if track_type == "BASS" or "bass" in low or "contrabass" in low:
        return "bass"
    if track_type == "GUITAR":
        if any(token in low for token in ("acoustic", "clean", "nylon")):
            return "acoustic"
        if any(token in low for token in ("solo", "lead", "melody")):
            return "solo"
        if any(token in low for token in ("rhythm", "rhytm", "riff", "dist", "od", "overdrive")):
            return "rhythm"
        return "rhythm"
    if "violin" in low or "viola" in low or "cello" in low or "string" in low:
        return "strings"
    if "vocal" in low or "choir" in low or "voice" in low:
        return "voice"
    if "piano" in low or "keys" in low or "synth" in low or "organ" in low:
        return "keys"
    return "default"


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS



def ensure_session_id() -> str:
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
        session.modified = True
    return sid



def session_dir() -> Path:
    sid = ensure_session_id()
    path = SESSIONS_ROOT / sid
    path.mkdir(parents=True, exist_ok=True)
    return path



def uploads_root() -> Path:
    root = session_dir() / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root



def manifest_path() -> Path:
    return session_dir() / "manifest.json"



def load_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.exists():
        return {"jobs": [], "current_job_id": None}
    return json.loads(path.read_text(encoding="utf-8"))



def save_manifest(manifest: dict[str, Any]) -> None:
    manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")



def get_current_job(manifest: dict[str, Any] | None = None) -> dict[str, Any] | None:
    manifest = manifest or load_manifest()
    current_id = manifest.get("current_job_id")
    if not current_id:
        return None
    for job in manifest.get("jobs", []):
        if job["id"] == current_id:
            return job
    return None



def human_timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")



def unique_name(root: Path, raw_name: str) -> str:
    cleaned = secure_filename(raw_name) or "upload"
    candidate = cleaned
    stem = Path(cleaned).stem
    suffix = Path(cleaned).suffix
    idx = 2
    while (root / candidate).exists():
        candidate = f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate



def midi_note_name(note: int) -> str:
    octave = (note // 12) - 1
    return f"{MIDI_NOTE_NAMES[note % 12]}{octave}"



def build_preview_dir(job_dir: Path) -> Path:
    preview_dir = job_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir



def collect_timed_messages(midi_track) -> list[tuple[int, Any]]:
    abs_tick = 0
    messages: list[tuple[int, Any]] = []
    for msg in midi_track:
        abs_tick += int(getattr(msg, "time", 0) or 0)
        messages.append((abs_tick, msg))
    return messages



def tick_to_ms(tick: int, tempo_events: list[tuple[int, int]]) -> float:
    if tick <= 0:
        return 0.0

    total_ms = 0.0
    current_tick = 0
    current_tempo = tempo_events[0][1] if tempo_events else DEFAULT_TEMPO_US

    for event_tick, tempo in tempo_events[1:]:
        if tick <= event_tick:
            delta = tick - current_tick
            total_ms += delta * current_tempo / TICKS_PER_BEAT / 1000.0
            return total_ms
        delta = event_tick - current_tick
        total_ms += delta * current_tempo / TICKS_PER_BEAT / 1000.0
        current_tick = event_tick
        current_tempo = tempo

    delta = tick - current_tick
    total_ms += delta * current_tempo / TICKS_PER_BEAT / 1000.0
    return total_ms



def finalize_preview_note(note_data: dict[str, Any], track_type: str) -> dict[str, Any]:
    modifiers: list[str] = []
    if note_data["velocity"] == 1:
        modifiers.append("legato")
    if track_type == "GUITAR" and note_data["velocity"] == 127 and note_data["articulation_key"] == "sustain":
        modifiers.append("pinch")
    if note_data["cc1_peak"] > 0:
        modifiers.append("vibrato")
    if note_data["pitch_bend_seen"]:
        modifiers.append("bend/slide")

    articulation_label = ARTICULATION_LABELS.get(note_data["articulation_key"], note_data["articulation_key"])
    display = articulation_label if not modifiers else f"{articulation_label} + {', '.join(modifiers)}"

    return {
        "pitch": note_data["pitch"],
        "note_name": midi_note_name(note_data["pitch"]),
        "velocity": note_data["velocity"],
        "start_ms": round(note_data["start_ms"], 1),
        "end_ms": round(note_data["end_ms"], 1),
        "duration_ms": round(note_data["end_ms"] - note_data["start_ms"], 1),
        "bend_target_pitch": round(note_data.get("bend_target_pitch", note_data["pitch"]), 3),
        "bend_semitones": round(note_data.get("bend_semitones", 0.0), 3),
        "articulation": display,
        "articulation_key": note_data["articulation_key"],
        "modifiers": modifiers,
    }



def analyze_midi_preview(midi_track, track_type: str, track_name: str = "") -> dict[str, Any]:
    timed_messages = collect_timed_messages(midi_track)
    tempo_events: list[tuple[int, int]] = [(0, DEFAULT_TEMPO_US)]
    for abs_tick, msg in timed_messages:
        if getattr(msg, "type", None) == "set_tempo":
            tempo_events.append((abs_tick, int(msg.tempo)))
    tempo_events = sorted(dict((tick, tempo) for tick, tempo in tempo_events).items())
    if tempo_events[0][0] != 0:
        tempo_events.insert(0, (0, DEFAULT_TEMPO_US))

    ks_map = ARTICULATION_MAPS.get(track_type, {})
    if track_type == "OTHER":
        current_articulation = "other"
    elif track_type == "DRUMS":
        current_articulation = "hit"
    else:
        current_articulation = "sustain"
    current_cc1 = 0
    active_notes: dict[int, list[dict[str, Any]]] = {}
    notes: list[dict[str, Any]] = []

    for abs_tick, msg in timed_messages:
        msg_type = getattr(msg, "type", None)

        if msg_type == "note_on" and getattr(msg, "velocity", 0) > 0 and getattr(msg, "note", None) in ks_map:
            current_articulation = ks_map[msg.note]
            continue

        if msg_type == "control_change" and getattr(msg, "control", None) == 1:
            current_cc1 = int(msg.value)
            for pitch_stack in active_notes.values():
                for active in pitch_stack:
                    active["cc1_peak"] = max(active["cc1_peak"], current_cc1)
            continue

        if msg_type == "pitchwheel" and int(getattr(msg, "pitch", 0)) != 0:
            bend_semitones = (int(getattr(msg, "pitch", 0)) / 8192.0) * float(PITCH_BEND_RANGE_ST)
            for pitch_stack in active_notes.values():
                for active in pitch_stack:
                    active["pitch_bend_seen"] = True
                    active["last_bend_semitones"] = bend_semitones
                    if abs(bend_semitones) >= abs(active["max_abs_bend_semitones"]):
                        active["max_abs_bend_semitones"] = bend_semitones
            continue

        if msg_type == "note_on" and getattr(msg, "velocity", 0) > 0:
            pitch = int(msg.note)
            note_data = {
                "pitch": pitch,
                "velocity": int(msg.velocity),
                "start_tick": abs_tick,
                "start_ms": tick_to_ms(abs_tick, tempo_events),
                "cc1_peak": current_cc1,
                "pitch_bend_seen": False,
                "last_bend_semitones": 0.0,
                "max_abs_bend_semitones": 0.0,
                "articulation_key": current_articulation,
            }
            active_notes.setdefault(pitch, []).append(note_data)
            continue

        if msg_type in {"note_off", "note_on"} and (msg_type == "note_off" or getattr(msg, "velocity", 0) == 0):
            pitch = int(msg.note)
            stack = active_notes.get(pitch) or []
            if not stack:
                continue
            note_data = stack.pop(0)
            note_data["end_tick"] = abs_tick
            note_data["end_ms"] = tick_to_ms(abs_tick, tempo_events)
            if note_data["end_ms"] < note_data["start_ms"]:
                note_data["end_ms"] = note_data["start_ms"]
            bend_semitones = note_data["last_bend_semitones"] or note_data["max_abs_bend_semitones"]
            note_data["bend_semitones"] = bend_semitones
            note_data["bend_target_pitch"] = note_data["pitch"] + bend_semitones
            notes.append(finalize_preview_note(note_data, track_type))
            if not stack:
                active_notes.pop(pitch, None)

    if notes:
        min_pitch = min(note["pitch"] for note in notes)
        max_pitch = max(note["pitch"] for note in notes)
        duration_ms = max(note["end_ms"] for note in notes)
    else:
        min_pitch = 36
        max_pitch = 84
        duration_ms = 0.0

    articulation_counts: dict[str, int] = {}
    for note in notes:
        key = note["articulation_key"]
        articulation_counts[key] = articulation_counts.get(key, 0) + 1

    return {
        "track_name": track_name,
        "track_type": track_type,
        "instrument_preset": infer_instrument_preset(track_name, track_type),
        "note_count": len(notes),
        "duration_ms": round(duration_ms, 1),
        "pitch_min": min_pitch,
        "pitch_max": max_pitch,
        "articulation_counts": articulation_counts,
        "notes": notes,
    }



def save_preview(preview_dir: Path, file_name: str, preview_data: dict[str, Any]) -> str:
    preview_name = f"{file_name}.json"
    (preview_dir / preview_name).write_text(json.dumps(preview_data, ensure_ascii=False), encoding="utf-8")
    return preview_name



def is_empty_export_track(stats: dict[str, Any], preview_data: dict[str, Any]) -> bool:
    return (
        int(stats.get("notes", 0) or 0) == 0
        and int(stats.get("ks", 0) or 0) == 0
        and int(stats.get("cc1", 0) or 0) == 0
        and int(preview_data.get("note_count", 0) or 0) == 0
        and not preview_data.get("articulation_counts")
    )



def build_track_summary(song, out_dir: Path, job_dir: Path, job_id: str,
                        humanize: bool = False, ghost_notes: bool = False,
                        seed: int = 7, auto_sustain_vibrato: bool = False,
                        fret_noise_on_hand_shift: bool = False,
                        expand_gp_hidden_32nds: bool = False,
                        ) -> tuple[list[dict[str, Any]], list[Any]]:
    used: dict[str, int] = {}
    tracks: list[dict[str, Any]] = []
    midi_tracks: list[Any] = []
    preview_dir = build_preview_dir(job_dir)

    for idx, track in enumerate(song.tracks, start=1):
        track_type = resolve_track_type(track)
        if track_type == "DRUMS":
            midi_track, stats = build_drum_midi(
                song, track, humanize=humanize, humanize_seed=seed,
                ghost_notes=True if ghost_notes else None)
        elif track_type == "OTHER":
            midi_track, stats = build_other_midi(
                song, track,
                expand_gp_hidden_32nds=expand_gp_hidden_32nds,
            )
        else:
            midi_track, stats = build_instrument_midi(
                song, track, track_type, humanize=humanize, humanize_seed=seed,
                auto_sustain_vibrato=auto_sustain_vibrato,
                fret_noise_on_hand_shift=fret_noise_on_hand_shift,
                performance_seed=seed,
                expand_gp_hidden_32nds=expand_gp_hidden_32nds,
            )

        name = safe_filename(track.name) or f"Track_{idx}"
        file_name = name
        if file_name in used:
            used[file_name] += 1
            file_name = f"{name}_{used[file_name]}"
        else:
            used[file_name] = 1

        preview_data = analyze_midi_preview(midi_track, track_type, track.name or f"Track {idx}")
        if is_empty_export_track(stats, preview_data):
            continue

        midi = MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
        midi.tracks.append(midi_track)
        out_path = out_dir / f"{file_name}.mid"
        midi.save(out_path)
        midi_tracks.append(midi_track)

        preview_name = save_preview(preview_dir, file_name, preview_data)

        fixes = []
        if stats["ks"]:
            fixes.append(f"keyswitch-события: {stats['ks']}")
        if stats["cc1"]:
            fixes.append(f"CC1 vibrato automation: {stats['cc1']}")
        if stats.get("auto_vibrato_notes"):
            fixes.append(f"авто-вибрато на solo sustain: {stats['auto_vibrato_notes']}")
        if stats.get("fret_noise_events"):
            fixes.append(f"fret-noise при переносах руки: {stats['fret_noise_events']}")
        if stats.get("hidden_32nd_beats"):
            fixes.append(
                f"скрытые GP 32-е: {stats['hidden_32nd_beats']} beat / "
                f"{stats['hidden_32nd_notes']} нот")
        if stats.get("config"):
            fixes.append(f"конфиг артикуляций: {stats['config']}")
        if track_type != "OTHER":
            fixes.append("артикуляции адаптированы под целевую библиотеку")
        else:
            fixes.append("трек оставлен без маппинга, MIDI сохранён как есть")
        for warn in stats.get("warnings", []):
            fixes.append(f"внимание: {warn}")

        tracks.append(
            {
                "index": idx,
                "track_name": track.name or f"Track {idx}",
                "basename": file_name,
                "track_type": track_type,
                "track_type_label": TRACK_LABELS.get(track_type, track_type),
                "fix_categories": FIX_DESCRIPTIONS[track_type],
                "fixes": fixes,
                "stats": stats,
                "notes": stats["notes"],
                "download_name": out_path.name,
                "download_url": url_for("download_track", job_id=job_id, filename=out_path.name),
                "preview_url": url_for("preview_track", job_id=job_id, filename=preview_name),
                "preview_note_count": preview_data["note_count"],
                "preview_duration_ms": preview_data["duration_ms"],
                "preview_articulations": preview_data["articulation_counts"],
                "preview_instrument": preview_data["instrument_preset"],
            }
        )

    return tracks, midi_tracks



def summarize_song(song) -> dict[str, Any]:
    tempo = round(float(song.tempo), 3) if getattr(song, "tempo", None) else 120
    measure_count = len(song.measureHeaders)
    time_signatures = []
    seen = set()
    for header in song.measureHeaders:
        ts = header.timeSignature
        key = f"{ts.numerator}/{ts.denominator.value}"
        if key not in seen:
            seen.add(key)
            time_signatures.append(key)
    return {
        "title": getattr(song, "title", "") or "Без названия",
        "artist": getattr(song, "artist", "") or "—",
        "album": getattr(song, "album", "") or "—",
        "tempo": tempo,
        "tracks": len(song.tracks),
        "measures": measure_count,
        "time_signatures": time_signatures or ["4/4"],
    }



def make_zip(job_dir: Path) -> Path:
    zip_path = job_dir / "tracks.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for artifact in sorted(path for path in (job_dir / "output").iterdir() if path.is_file()):
            zf.write(artifact, arcname=artifact.name)
    return zip_path



def create_job(uploaded_file, humanize: bool = False,
               ghost_notes: bool = False, seed: int = 7,
               playable_tabs: bool = False,
               auto_sustain_vibrato: bool = False,
               fret_noise_on_hand_shift: bool = False,
               expand_gp_hidden_32nds: bool = False) -> str:
    root = uploads_root()
    job_id = uuid.uuid4().hex[:12]
    job_dir = root / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    original_name = uploaded_file.filename or "song.gp5"
    stored_name = unique_name(input_dir, original_name)
    source_path = input_dir / stored_name
    uploaded_file.save(source_path)

    song = parse_song(source_path)
    song_summary = summarize_song(song)
    tracks, midi_tracks = build_track_summary(
        song, output_dir, job_dir, job_id,
        humanize=humanize, ghost_notes=ghost_notes, seed=seed,
        auto_sustain_vibrato=auto_sustain_vibrato,
        fret_noise_on_hand_shift=fret_noise_on_hand_shift,
        expand_gp_hidden_32nds=expand_gp_hidden_32nds,
    )

    # Сборный Type 1 со всеми дорожками — из ТЕХ ЖЕ объектов, что и пофайловый
    # экспорт, поэтому разойтись они не могут. Кладём в output/ последним, чтобы
    # он попал и в zip.
    combined_name = None
    if midi_tracks:
        combined_name = f"{safe_filename(Path(original_name).stem) or 'song'}_ALL.mid"
        build_combined_midi(midi_tracks).save(output_dir / combined_name)

    playable_warnings: list[str] = []
    refingered_name = None
    if playable_tabs:
        track_summaries = {item["track_name"]: item for item in tracks}
        generations: dict[str, TrackGeneration] = {}
        for source_track in song.tracks:
            if resolve_track_type(source_track) not in {"GUITAR", "BASS"}:
                continue
            result = generate_track(song, source_track, output_dir, preset="auto")
            generations[source_track.name] = result
            summary = track_summaries.get(source_track.name)
            if summary is not None:
                summary["playable"] = result.manifest_dict()
        if generations and source_path.suffix.lower() in {".gp", ".gp5"}:
            candidate = output_dir / f"{safe_filename(Path(original_name).stem) or 'song'}_refingered{source_path.suffix.lower()}"
            try:
                refinger_gp(source_path, generations, candidate, original_song=song)
                refingered_name = candidate.name
            except Exception as exc:
                playable_warnings.append(f"re-fingering отменён: {str(exc)[:240]}")

    zip_path = make_zip(job_dir)

    job = {
        "id": job_id,
        "original_name": original_name,
        "stored_name": stored_name,
        "created_at": human_timestamp(),
        "song": song_summary,
        "tracks": tracks,
        "warnings": [t["track_name"] for t in tracks if t["track_type"] == "OTHER"],
        "playable_warnings": playable_warnings,
        "playable_tabs": playable_tabs,
        "refingered_name": refingered_name,
        "refingered_url": (url_for("download_track", job_id=job_id, filename=refingered_name)
                           if refingered_name else None),
        "zip_name": zip_path.name,
        "zip_url": url_for("download_zip", job_id=job_id),
        "combined_name": combined_name,
        "combined_url": (url_for("download_track", job_id=job_id, filename=combined_name)
                         if combined_name else None),
        "humanize": humanize,
        "ghost_notes": ghost_notes,
        "auto_sustain_vibrato": auto_sustain_vibrato,
        "fret_noise_on_hand_shift": fret_noise_on_hand_shift,
        "expand_gp_hidden_32nds": expand_gp_hidden_32nds,
    }

    manifest = load_manifest()
    manifest["jobs"] = [j for j in manifest.get("jobs", []) if j["id"] != job_id]
    manifest["jobs"].insert(0, job)
    manifest["jobs"] = manifest["jobs"][:SESSION_HISTORY_LIMIT]
    manifest["current_job_id"] = job_id
    save_manifest(manifest)
    return job_id


@app.get("/")
def index():
    manifest = load_manifest()
    current_job = get_current_job(manifest)
    jobs = manifest.get("jobs", [])
    return render_template(
        "index.html", current_job=current_job, jobs=jobs,
        max_size_mb=MAX_CONTENT_LENGTH // (1024 * 1024),
        printing_enabled=bool(CUPS_PRINTER), tunable_params=TUNABLE_PARAMS,
        hydra_pitch_bend_range=HYDRA_PITCH_BEND_RANGE,
    )


@app.post("/new-session")
def new_session():
    manifest = load_manifest()
    manifest["current_job_id"] = None
    save_manifest(manifest)
    flash("Новая сессия готова. Загрузите следующий Guitar Pro файл.", "success")
    return redirect(url_for("index"))


@app.post("/upload")
def upload():
    if "file" not in request.files:
        flash("Файл не получен.", "error")
        return redirect(url_for("index"))

    file = request.files["file"]
    if not file or not file.filename:
        flash("Выберите файл Guitar Pro.", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Поддерживаются только Guitar Pro файлы: .gp, .gp3, .gp4, .gp5, .gpx", "error")
        return redirect(url_for("index"))

    # Оживление — ОПЦИЯ, по умолчанию выключена: без галочки выхлоп прежний.
    humanize = request.form.get("humanize") == "on"
    ghost_notes = request.form.get("ghost_notes") == "on"
    auto_sustain_vibrato = request.form.get("auto_sustain_vibrato") == "on"
    fret_noise_on_hand_shift = request.form.get("fret_noise_on_hand_shift") == "on"
    expand_gp_hidden_32nds = request.form.get("expand_gp_hidden_32nds") == "on"
    playable_tabs = request.form.get("playable_tabs") == "on"
    try:
        seed = int(request.form.get("seed") or 7)
    except ValueError:
        seed = 7

    try:
        job_id = create_job(
            file, humanize=humanize, ghost_notes=ghost_notes, seed=seed,
            playable_tabs=playable_tabs,
            auto_sustain_vibrato=auto_sustain_vibrato,
            fret_noise_on_hand_shift=fret_noise_on_hand_shift,
            expand_gp_hidden_32nds=expand_gp_hidden_32nds,
        )
    except Exception as exc:  # pragma: no cover
        flash(f"Не удалось разобрать файл: {exc}", "error")
        return redirect(url_for("index"))

    msg = "Файл загружен и разобран. MIDI-дорожки готовы к скачиванию."
    if humanize:
        msg += " Оживление применено."
        if ghost_notes:
            msg += " Гост-ноты добавлены (партия изменена)."
    if auto_sustain_vibrato:
        msg += " Авто-вибрато длинных solo sustain включено."
    if fret_noise_on_hand_shift:
        msg += " Fret-noise при переносах руки включён."
    if expand_gp_hidden_32nds:
        msg += " Скрытые GP 32-е на тональных дорожках развёрнуты."
    flash(msg, "success")
    return redirect(url_for("job_details", job_id=job_id))


@app.get("/jobs/<job_id>")
def job_details(job_id: str):
    manifest = load_manifest()
    jobs = manifest.get("jobs", [])
    current_job = None
    for job in jobs:
        if job["id"] == job_id:
            current_job = job
            break
    if current_job is None:
        abort(404)
    manifest["current_job_id"] = job_id
    save_manifest(manifest)
    return render_template(
        "index.html", current_job=current_job, jobs=jobs,
        max_size_mb=MAX_CONTENT_LENGTH // (1024 * 1024),
        printing_enabled=bool(CUPS_PRINTER), tunable_params=TUNABLE_PARAMS,
        hydra_pitch_bend_range=HYDRA_PITCH_BEND_RANGE,
    )


def _job_by_id(manifest: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    return next((job for job in manifest.get("jobs", []) if job.get("id") == job_id), None)


def _artifact_names(job: dict[str, Any]) -> set[str]:
    names = {value for value in (job.get("combined_name"), job.get("refingered_name")) if value}
    for track in job.get("tracks", []):
        if track.get("download_name"):
            names.add(track["download_name"])
        names.update((track.get("playable") or {}).get("files", []))
    return names


@app.get("/download/<job_id>/<path:filename>")
def download_track(job_id: str, filename: str):
    manifest = load_manifest()
    job = _job_by_id(manifest, job_id)
    if job is None or filename not in _artifact_names(job) or Path(filename).name != filename:
        abort(404)
    path = uploads_root() / job_id / "output" / filename
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.get("/preview/<job_id>/<path:filename>")
def preview_track(job_id: str, filename: str):
    path = uploads_root() / job_id / "preview" / filename
    if not path.exists() or path.suffix.lower() != ".json":
        abort(404)
    return send_file(path, mimetype="application/json")


@app.get("/download/<job_id>/all.zip")
def download_zip(job_id: str):
    path = uploads_root() / job_id / "tracks.zip"
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{job_id}_tracks.zip")


def _mapping_generation(track, playable: dict[str, Any]) -> TrackGeneration:
    params = dict(playable["params"])
    events = build_score_events(track)
    mapped, _text = map_with_gtrsnipe(events, resolve_track_type(track), track.name, params)
    note_count = sum(len(beat.notes) for *_prefix, beat, _mt, _bt, _dur in iter_voice_beats_with_canonical_ticks(track))
    positions: list[tuple[int, int] | None] = [None] * note_count
    for item in mapped:
        for source_index in item.source_indices:
            positions[source_index] = (item.string, item.fret)
    return TrackGeneration(
        track.name, resolve_track_type(track), safe_filename(track.name), params["preset"], params,
        list(playable.get("files", [])), dict(playable.get("status", {})), mapped, positions,
        version=int(playable.get("version", 1)),
    )


@app.post("/jobs/<job_id>/playable/regen")
def regen_playable(job_id: str):
    manifest = load_manifest()
    job = _job_by_id(manifest, job_id)
    if job is None:
        abort(404)
    if not job.get("playable_tabs"):
        abort(400, description="playable tabs выключены для этого джоба")
    basename = request.form.get("track", "")
    summary = next((item for item in job.get("tracks", []) if item.get("basename") == basename and item.get("playable")), None)
    if summary is None:
        abort(400, description="трек отсутствует в манифесте playable tabs")
    incoming = {key: value for key, value in request.form.items() if key != "track"}
    if "prefer_open" in request.form:
        incoming["prefer_open"] = request.form.getlist("prefer_open")[-1]
    unknown = set(incoming) - set(TUNABLE_PARAMS)
    if unknown:
        abort(400, description=f"неизвестные параметры: {', '.join(sorted(unknown))}")
    old_playable = summary["playable"]
    merged = {key: value for key, value in old_playable.get("params", {}).items() if key in TUNABLE_PARAMS}
    merged.update(incoming)
    try:
        checked = validate_params(merged)
    except ValueError as exc:
        abort(400, description=str(exc))

    job_dir = uploads_root() / job_id
    source_path = job_dir / "input" / job["stored_name"]
    song = parse_song(source_path)
    source_tracks = {track.name: track for track in song.tracks}
    source_track = source_tracks.get(summary["track_name"])
    if source_track is None:
        abort(400, description="исходный трек не найден")
    preset = checked.pop("preset", old_playable.get("preset", "auto"))
    version = int(old_playable.get("version", 1)) + 1
    try:
        result = generate_track(
            song, source_track, job_dir / "output", preset=preset,
            overrides=checked, version=version, generate_score=False,
        )
        summary["playable"] = result.manifest_dict()
        generations: dict[str, TrackGeneration] = {source_track.name: result}
        for other in job.get("tracks", []):
            if other is summary or not other.get("playable"):
                continue
            track = source_tracks.get(other["track_name"])
            if track is not None:
                generations[track.name] = _mapping_generation(track, other["playable"])
        if job.get("refingered_name"):
            refinger_gp(
                source_path, generations, job_dir / "output" / job["refingered_name"],
                original_song=song,
            )
        make_zip(job_dir)
    except Exception as exc:
        abort(500, description=f"перегенерация не удалась: {exc}")
    save_manifest(manifest)
    flash(f"Таб {summary['track_name']} перегенерирован: v{version}.", "success")
    return redirect(url_for("job_details", job_id=job_id))


@app.post("/jobs/<job_id>/print")
def print_artifact(job_id: str):
    manifest = load_manifest()
    job = _job_by_id(manifest, job_id)
    if job is None:
        abort(404)
    artifact = request.form.get("artifact", "")
    if artifact not in _artifact_names(job) or Path(artifact).name != artifact or not artifact.lower().endswith(".pdf"):
        abort(400, description="PDF отсутствует в манифесте джоба")
    if not CUPS_PRINTER:
        abort(400, description="серверная печать выключена")
    result = print_pdf(
        uploads_root() / job_id / "output" / artifact,
        server=CUPS_SERVER, printer=CUPS_PRINTER,
    )
    if result.ok:
        flash(f"PDF отправлен на печать: {result.request_id or 'задание принято'}", "success")
    else:
        flash(f"Печать не удалась: {result.error}", "error")
    return redirect(url_for("job_details", job_id=job_id))


@app.post("/jobs/<job_id>/delete")
def delete_job(job_id: str):
    manifest = load_manifest()
    jobs = manifest.get("jobs", [])
    manifest["jobs"] = [job for job in jobs if job["id"] != job_id]
    if manifest.get("current_job_id") == job_id:
        manifest["current_job_id"] = manifest["jobs"][0]["id"] if manifest["jobs"] else None
    save_manifest(manifest)
    shutil.rmtree(uploads_root() / job_id, ignore_errors=True)
    flash("Сессия удалена.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
