from __future__ import annotations

import hashlib
import json
import os
import secrets
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
from mido import MidiFile
from werkzeug.utils import secure_filename

from arrangement_openai import (
    INSTRUCTIONS,
    MAX_INSTRUCTIONS_CHARS,
    create_openai_draft,
    get_openai_config,
    is_openai_configured,
    normalize_instructions,
)
from arrangement_processing import build_arrangement_context
from arrangement_workflow import apply as apply_arrangement_plan
from articulation_config import config_for_track_type
from gp_import import parse_song
from gp_to_shreddage import (
    PITCH_BEND_RANGE_ST,
    TICKS_PER_BEAT,
    build_combined_midi,
    build_drum_midi,
    build_instrument_midi,
    build_other_midi,
    iter_voice_beats_with_canonical_ticks,
    resolve_track_type,
    safe_filename,
)
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
# Overridable so the container can point converter session storage at a writable
# volume while the image root filesystem stays read-only.
DATA_ROOT = Path(os.environ.get("GPMIDI_DATA_ROOT", str(APP_ROOT / "data")))
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

TRACK_EFFECTS = {
    "DRUMS": ["humanize", "ghost_notes"],
    "GUITAR": [
        "humanize",
        "auto_sustain_vibrato",
        "fret_noise_on_hand_shift",
        "expand_gp_hidden_32nds",
        "preserve_gp_played_offsets",
    ],
    "BASS": [
        "humanize",
        "fret_noise_on_hand_shift",
        "expand_gp_hidden_32nds",
        "preserve_gp_played_offsets",
    ],
    "OTHER": ["expand_gp_hidden_32nds"],
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


def discover_tracks(song: Any) -> list[dict[str, Any]]:
    """Describe source tracks without rendering or mutating MIDI.

    Every detected track starts selected. This is the parse-first contract: the
    user can exclude Synth or narrow a run to a revised Solo before any
    converter, humanizer, or model is invoked.
    """
    discovered = []
    for index, track in enumerate(getattr(song, "tracks", []) or [], start=1):
        track_type = resolve_track_type(track)
        track_name = track.name or f"Track {index}"
        discovered.append({
            "index": index,
            "track_name": track_name,
            "track_type": track_type,
            "track_type_label": TRACK_LABELS.get(track_type, track_type),
            "instrument_preset": infer_instrument_preset(track_name, track_type),
            "available_effects": list(TRACK_EFFECTS.get(track_type, [])),
            "selected": True,
        })
    return discovered


def parse_track_options(
    form: Any,
    selected_track_indices: set[int],
    discovered_tracks: list[dict[str, Any]],
) -> dict[int, dict[str, bool]]:
    """Read only allow-listed effects for selected, server-known tracks."""
    options: dict[int, dict[str, bool]] = {}
    by_index = {int(track["index"]): track for track in discovered_tracks}
    for index in sorted(selected_track_indices):
        track = by_index.get(index)
        if track is None:
            continue
        effects = list(track.get("available_effects") or [])
        options[index] = {
            effect: form.get(f"track_{index}_{effect}") == "on"
            for effect in effects
        }
    return options


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



def _attach_midi_downloads(job: dict[str, Any]) -> dict[str, Any]:
    """Expose baseline and approved enriched MIDI as distinct UI downloads.

    Derive enriched links from the persisted apply manifest so jobs created by an
    older UI become usable immediately after deployment without re-running the
    model or enrichment step.
    """
    job["enriched_combined_name"] = None
    job["enriched_combined_url"] = None
    for track in job.get("tracks", []):
        track["enriched_download_name"] = None
        track["enriched_download_url"] = None

    arrangement = job.get("arrangement") or {}
    apply_manifest = arrangement.get("apply_manifest") or {}
    allowed = set(job.get("arrangement_artifacts", []))
    tracks_by_name = {
        str(track.get("track_name")): track for track in job.get("tracks", [])
    }
    for artifact in apply_manifest.get("artifacts", []):
        name = str(artifact.get("name") or "")
        if not name.endswith(".mid") or name not in allowed:
            continue
        download_url = url_for("download_track", job_id=job["id"], filename=name)
        if artifact.get("type") == "TYPE_1_ALL":
            job["enriched_combined_name"] = name
            job["enriched_combined_url"] = download_url
            continue
        track = tracks_by_name.get(str(artifact.get("track") or ""))
        if track is not None:
            track["enriched_download_name"] = name
            track["enriched_download_url"] = download_url
    return job


def get_current_job(manifest: dict[str, Any] | None = None) -> dict[str, Any] | None:
    manifest = manifest or load_manifest()
    current_id = manifest.get("current_job_id")
    if not current_id:
        return None
    for job in manifest.get("jobs", []):
        if job["id"] == current_id:
            return _attach_midi_downloads(job)
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
                        preserve_gp_played_offsets: bool = False,
                        selected_track_indices: set[int] | None = None,
                        track_options: dict[int, dict[str, bool]] | None = None,
                        ) -> tuple[list[dict[str, Any]], list[Any]]:
    used: dict[str, int] = {}
    tracks: list[dict[str, Any]] = []
    midi_tracks: list[Any] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_dir = build_preview_dir(job_dir)

    for idx, track in enumerate(song.tracks, start=1):
        track_type = resolve_track_type(track)
        name = safe_filename(track.name) or f"Track_{idx}"
        used[name] = used.get(name, 0) + 1
        file_name = name if used[name] == 1 else f"{name}_{used[name]}"
        if selected_track_indices is not None and idx not in selected_track_indices:
            continue

        effects = (track_options or {}).get(idx)
        if effects is None:
            effects = {
                "humanize": humanize,
                "ghost_notes": ghost_notes,
                "auto_sustain_vibrato": auto_sustain_vibrato,
                "fret_noise_on_hand_shift": fret_noise_on_hand_shift,
                "expand_gp_hidden_32nds": expand_gp_hidden_32nds,
                "preserve_gp_played_offsets": preserve_gp_played_offsets,
            }
        effective_effects: dict[str, bool] = dict(effects or {})

        def effect_enabled(key: str) -> bool:
            return bool(effective_effects.get(key, False))

        if track_type == "DRUMS":
            midi_track, stats = build_drum_midi(
                song, track, humanize=effect_enabled("humanize"), humanize_seed=seed,
                ghost_notes=True if effect_enabled("ghost_notes") else None)
        elif track_type == "OTHER":
            midi_track, stats = build_other_midi(
                song, track,
                expand_gp_hidden_32nds=effect_enabled("expand_gp_hidden_32nds"),
            )
        else:
            midi_track, stats = build_instrument_midi(
                song, track, track_type,
                humanize=effect_enabled("humanize"), humanize_seed=seed,
                auto_sustain_vibrato=effect_enabled("auto_sustain_vibrato"),
                fret_noise_on_hand_shift=effect_enabled("fret_noise_on_hand_shift"),
                performance_seed=seed,
                expand_gp_hidden_32nds=effect_enabled("expand_gp_hidden_32nds"),
                preserve_gp_played_offsets=effect_enabled("preserve_gp_played_offsets"),
            )

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
        if stats.get("played_offset_notes"):
            fixes.append(f"GP8-сдвиги атак: {stats['played_offset_notes']} нот")
        if stats.get("config"):
            fixes.append(f"конфиг артикуляций: {stats['config']}")
        if track_type != "OTHER":
            fixes.append("артикуляции адаптированы под целевую библиотеку")
        else:
            fixes.append("трек оставлен без маппинга, MIDI сохранён как есть")
        for warn in stats.get("warnings", []):
            fixes.append(f"внимание: {warn}")

        processing_report = [
            f"Маппинг: {TRACK_LABELS.get(track_type, track_type)}",
            f"Артикуляции: KS {int(stats.get('ks', 0) or 0)}, "
            f"CC1 {int(stats.get('cc1', 0) or 0)}",
        ]
        enabled_effects = [name for name, enabled in effective_effects.items() if enabled]
        if enabled_effects:
            processing_report.append("Эффекты: " + ", ".join(enabled_effects))
        else:
            processing_report.append("Эффекты: без дополнительного оживления")
        if stats.get("auto_vibrato_notes"):
            processing_report.append(f"Авто-вибрато: {stats['auto_vibrato_notes']} нот")
        if stats.get("fret_noise_events"):
            processing_report.append(f"Fret-noise: {stats['fret_noise_events']} событий")
        if stats.get("played_offset_notes"):
            processing_report.append(f"GP played offsets: {stats['played_offset_notes']} нот")

        tracks.append(
            {
                "index": idx,
                "track_name": track.name or f"Track {idx}",
                "basename": file_name,
                "track_type": track_type,
                "track_type_label": TRACK_LABELS.get(track_type, track_type),
                "fix_categories": FIX_DESCRIPTIONS[track_type],
                "fixes": fixes,
                "effects": effective_effects,
                "processing_report": processing_report,
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


def prepare_hermes_arrangement_context(
    song: Any,
    tracks: list[dict[str, Any]],
    midi_tracks: list[Any],
    output_dir: Path,
    original_name: str,
) -> tuple[dict[str, Any], list[str]]:
    """Export a draft context from mapped MIDI without changing any track."""
    service_by_type = {
        track_type: set(mapping) for track_type, mapping in ARTICULATION_MAPS.items()
    }
    rendered = [
        {
            "track_name": summary["track_name"],
            "track_type": summary["track_type"],
            "midi_track": midi_track,
        }
        for summary, midi_track in zip(tracks, midi_tracks)
    ]
    context = build_arrangement_context(song, rendered, service_notes=service_by_type)
    stem = safe_filename(Path(original_name).stem) or "song"
    context_name = f"{stem}_arrangement-context.json"
    payload = {
        "schema_version": 1,
        "stage": "awaiting_hermes_draft",
        "approved": False,
        "source_midi_state": "target_library_mapped",
        "apply_contract": "separate explicit approval required",
        "context": context,
    }
    (output_dir / context_name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "awaiting_hermes_draft",
        "source_midi_state": "target_library_mapped",
        "applied": False,
        "context_name": context_name,
    }, [context_name]



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



def create_parse_job(uploaded_file) -> str:
    """Persist and parse an upload, but do not render MIDI or call a model."""
    root = uploads_root()
    job_id = uuid.uuid4().hex[:12]
    job_dir = root / job_id
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    original_name = uploaded_file.filename or "song.gp5"
    stored_name = unique_name(input_dir, original_name)
    source_path = input_dir / stored_name
    uploaded_file.save(source_path)
    song = parse_song(source_path)
    job = {
        "id": job_id,
        "stage": "parsed",
        "original_name": original_name,
        "stored_name": stored_name,
        "created_at": human_timestamp(),
        "song": summarize_song(song),
        "detected_tracks": discover_tracks(song),
        "tracks": [],
        "warnings": [],
        "playable_warnings": [],
        "playable_tabs": False,
        "combined_name": None,
        "combined_url": None,
        "zip_name": None,
        "zip_url": None,
        "arrangement": None,
        "arrangement_artifacts": [],
    }
    manifest = load_manifest()
    manifest["jobs"] = [item for item in manifest.get("jobs", []) if item.get("id") != job_id]
    manifest["jobs"].insert(0, job)
    manifest["jobs"] = manifest["jobs"][:SESSION_HISTORY_LIMIT]
    manifest["current_job_id"] = job_id
    save_manifest(manifest)
    return job_id


def create_job(uploaded_file=None, humanize: bool = False,
               ghost_notes: bool = False, seed: int = 7,
               playable_tabs: bool = False,
               auto_sustain_vibrato: bool = False,
               fret_noise_on_hand_shift: bool = False,
               expand_gp_hidden_32nds: bool = False,
               preserve_gp_played_offsets: bool = False,
               prepare_arrangement_context: bool = False,
               openai_arrangement_draft: bool = False,
               arrangement_prompt: str | None = None,
               selected_track_indices: set[int] | None = None,
               track_options: dict[int, dict[str, bool]] | None = None,
               existing_job: dict[str, Any] | None = None) -> str:
    root = uploads_root()
    if existing_job is not None:
        job_id = str(existing_job["id"])
        job_dir = root / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        original_name = str(existing_job["original_name"])
        stored_name = str(existing_job["stored_name"])
        source_path = input_dir / stored_name
        if not source_path.is_file():
            raise FileNotFoundError("parsed Guitar Pro source is missing")
        shutil.rmtree(output_dir, ignore_errors=True)
        shutil.rmtree(job_dir / "preview", ignore_errors=True)
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        if uploaded_file is None:
            raise ValueError("uploaded file is required for a new job")
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
        preserve_gp_played_offsets=preserve_gp_played_offsets,
        selected_track_indices=selected_track_indices,
        track_options=track_options,
    )

    if track_options:
        humanize = any(options.get("humanize") for options in track_options.values())
        ghost_notes = any(options.get("ghost_notes") for options in track_options.values())
        auto_sustain_vibrato = any(
            options.get("auto_sustain_vibrato") for options in track_options.values())
        fret_noise_on_hand_shift = any(
            options.get("fret_noise_on_hand_shift") for options in track_options.values())
        expand_gp_hidden_32nds = any(
            options.get("expand_gp_hidden_32nds") for options in track_options.values())
        preserve_gp_played_offsets = any(
            options.get("preserve_gp_played_offsets") for options in track_options.values())

    prepare_arrangement_context = bool(prepare_arrangement_context or openai_arrangement_draft)
    arrangement = None
    arrangement_artifacts: list[str] = []
    if prepare_arrangement_context:
        arrangement, arrangement_artifacts = prepare_hermes_arrangement_context(
            song, tracks, midi_tracks, output_dir, original_name,
        )
        arrangement["context_url"] = url_for(
            "download_track", job_id=job_id, filename=arrangement["context_name"]
        )

    # OpenAI draft - only if configured and requested
    if openai_arrangement_draft and is_openai_configured():
        # Note: arrangement/context already prepared above if prepare_arrangement_context was set
        context_name = None
        if arrangement and arrangement.get("context_name"):
            context_name = arrangement["context_name"]
        else:
            # Need to prepare context if not already done
            arrangement, arrangement_artifacts = prepare_hermes_arrangement_context(
                song, tracks, midi_tracks, output_dir, original_name,
            )
            context_name = arrangement["context_name"]
        prompt_name: str | None = None
        prompt_sha256: str | None = None
        try:
            context_path = output_dir / context_name
            context = json.loads(context_path.read_text(encoding="utf-8"))
            resolved_prompt = normalize_instructions(arrangement_prompt)
            artifact_stem = safe_filename(Path(original_name).stem) or "song"
            prompt_name = f"{artifact_stem}_openai-prompt.txt"
            prompt_path = output_dir / prompt_name
            prompt_bytes = resolved_prompt.encode("utf-8")
            prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
            prompt_path.write_bytes(prompt_bytes)
            draft_result = create_openai_draft(
                context["context"], get_openai_config(), instructions=resolved_prompt
            )
            if draft_result.get("instructions_sha256") != prompt_sha256:
                raise RuntimeError("OpenAI prompt fingerprint mismatch")
            # Write plan payload with explicit schema for apply workflow
            plan_payload = {
                "schema_version": 1,
                "provider": "openai",
                "model": draft_result.get("model"),
                "response_id": draft_result.get("response_id"),
                "instructions_sha256": prompt_sha256,
                "approved": False,
                "plan": draft_result.get("plan"),
            }
            plan_name = f"{safe_filename(Path(original_name).stem) or 'song'}_openai-draft-plan.json"
            plan_path = output_dir / plan_name
            plan_path.write_text(
                json.dumps(plan_payload, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            # Write usage JSON with estimated_cost from actual usage
            usage_name = f"{safe_filename(Path(original_name).stem) or 'song'}_openai-usage.json"
            usage_path = output_dir / usage_name
            usage_payload = {
                "response_id": draft_result.get("response_id"),
                "model": draft_result.get("model"),
                "provider": draft_result.get("provider"),
                "instructions_sha256": prompt_sha256,
                "usage": draft_result.get("usage", {}),
                "estimated_cost_usd": (draft_result.get("usage") or {}).get("estimated_cost_usd"),
            }
            usage_path.write_text(
                json.dumps(usage_payload, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            # Update arrangement with draft status including actual usage and estimated_cost
            arrangement = {
                "status": draft_result.get("status", "draft_ready"),
                "context_name": context_name,
                "context_url": url_for("download_track", job_id=job_id, filename=context_name),
                "plan_name": plan_name,
                "plan_url": url_for("download_track", job_id=job_id, filename=plan_name),
                "usage_name": usage_name,
                "usage_url": url_for("download_track", job_id=job_id, filename=usage_name),
                "prompt_name": prompt_name,
                "prompt_url": url_for("download_track", job_id=job_id, filename=prompt_name),
                "prompt_sha256": prompt_sha256,
                "model": draft_result.get("model"),
                "summary": draft_result.get("plan", {}).get("summary", ""),
                "tokens": draft_result.get("usage", {}),
                "estimated_cost_usd": (draft_result.get("usage") or {}).get("estimated_cost_usd"),
                "usage_actual": draft_result.get("usage", {}),
            }
            arrangement_artifacts = [context_name, prompt_name, plan_name, usage_name]
        except Exception:
            # Catch error so baseline job still succeeds with arrangement draft_error status
            # Keep context artifact and baseline, use fixed sanitized user message
            arrangement = {
                "status": "draft_error",
                "error": "OpenAI draft request failed - please check your configuration",
                "context_name": context_name,
                "context_url": url_for("download_track", job_id=job_id, filename=context_name) if context_name else None,
            }
            if prompt_name and prompt_sha256 and (output_dir / prompt_name).is_file():
                arrangement["prompt_name"] = prompt_name
                arrangement["prompt_url"] = url_for(
                    "download_track", job_id=job_id, filename=prompt_name
                )
                arrangement["prompt_sha256"] = prompt_sha256
                arrangement_artifacts = [context_name, prompt_name]

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
        for source_index, source_track in enumerate(song.tracks, start=1):
            if selected_track_indices is not None and source_index not in selected_track_indices:
                continue
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
        "stage": "processed",
        "original_name": original_name,
        "stored_name": stored_name,
        "created_at": human_timestamp(),
        "song": song_summary,
        "detected_tracks": discover_tracks(song),
        "selected_track_indices": sorted(selected_track_indices) if selected_track_indices is not None else [
            int(track["index"]) for track in tracks
        ],
        "track_options": {str(index): options for index, options in (track_options or {}).items()},
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
        "preserve_gp_played_offsets": preserve_gp_played_offsets,
        "prepare_arrangement_context": prepare_arrangement_context,
        "openai_arrangement_draft": openai_arrangement_draft,
        "arrangement_apply_token": secrets.token_urlsafe(24) if openai_arrangement_draft else None,
        "seed": seed,
        "arrangement": arrangement,
        "arrangement_artifacts": arrangement_artifacts,
    }

    manifest = load_manifest()
    manifest["jobs"] = [j for j in manifest.get("jobs", []) if j["id"] != job_id]
    manifest["jobs"].insert(0, job)
    manifest["jobs"] = manifest["jobs"][:SESSION_HISTORY_LIMIT]
    manifest["current_job_id"] = job_id
    save_manifest(manifest)
    return job_id


@app.get("/healthz")
def healthz():
    """Liveness/readiness probe.

    Deliberately does not touch the Asset API: this reports whether the UI
    process itself is serving, so an upstream outage does not restart the pod.
    """
    from flask import jsonify
    return jsonify({
        "status": "ok",
        "sessions_root_writable": os.access(SESSIONS_ROOT, os.W_OK),
        "arrangement_workflow": "hermes_draft_then_explicit_apply",
        "direct_llm_enabled": is_openai_configured(),
    })


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
        openai_configured=is_openai_configured(),
        openai_arrangement_prompt=INSTRUCTIONS,
        openai_prompt_max_chars=MAX_INSTRUCTIONS_CHARS,
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

    try:
        job_id = create_parse_job(file)
    except Exception as exc:  # pragma: no cover
        flash(f"Не удалось разобрать файл: {exc}", "error")
        return redirect(url_for("index"))

    flash(
        "Файл разобран. Проверьте найденные дорожки, отключите ненужные и "
        "настройте эффекты перед обработкой.",
        "success",
    )
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
    current_job = _attach_midi_downloads(current_job)
    return render_template(
        "index.html", current_job=current_job, jobs=jobs,
        max_size_mb=MAX_CONTENT_LENGTH // (1024 * 1024),
        printing_enabled=bool(CUPS_PRINTER), tunable_params=TUNABLE_PARAMS,
        hydra_pitch_bend_range=HYDRA_PITCH_BEND_RANGE,
        openai_configured=is_openai_configured(),
        openai_arrangement_prompt=INSTRUCTIONS,
        openai_prompt_max_chars=MAX_INSTRUCTIONS_CHARS,
    )


def _job_by_id(manifest: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    return next((job for job in manifest.get("jobs", []) if job.get("id") == job_id), None)


@app.post("/jobs/<job_id>/process")
def process_job(job_id: str):
    manifest = load_manifest()
    job = _job_by_id(manifest, job_id)
    if job is None:
        abort(404)
    if job.get("stage") != "parsed":
        abort(409, description="job is already processed")

    discovered = list(job.get("detected_tracks") or [])
    known_indices = {int(track["index"]) for track in discovered}
    try:
        selected = {int(value) for value in request.form.getlist("track_indices")}
    except (TypeError, ValueError):
        abort(400, description="invalid track selection")
    if not selected:
        abort(400, description="select at least one track")
    if not selected.issubset(known_indices):
        abort(400, description="unknown track selected")

    options = parse_track_options(request.form, selected, discovered)
    try:
        seed = int(request.form.get("seed") or 7)
    except ValueError:
        seed = 7
    seed = max(0, min(9999, seed))
    prepare_context = request.form.get("prepare_arrangement_context") == "on"
    openai_draft = request.form.get("openai_arrangement_draft") == "on"

    try:
        create_job(
            None,
            seed=seed,
            playable_tabs=request.form.get("playable_tabs") == "on",
            prepare_arrangement_context=prepare_context,
            openai_arrangement_draft=openai_draft,
            arrangement_prompt=request.form.get("arrangement_prompt"),
            selected_track_indices=selected,
            track_options=options,
            existing_job=job,
        )
    except Exception as exc:  # pragma: no cover
        flash(f"Обработка не выполнена: {exc}", "error")
        return redirect(url_for("job_details", job_id=job_id))

    mode = "одна дорожка" if len(selected) == 1 else f"{len(selected)} дорожек"
    flash(
        f"Обработка завершена: {mode}. Не выбранные дорожки не рендерились и не "
        "передавались в expression workflow.",
        "success",
    )
    return redirect(url_for("job_details", job_id=job_id))


def _artifact_names(job: dict[str, Any]) -> set[str]:
    names = {value for value in (job.get("combined_name"), job.get("refingered_name")) if value}
    names.update(job.get("arrangement_artifacts", []))
    for track in job.get("tracks", []):
        if track.get("download_name"):
            names.add(track["download_name"])
        names.update((track.get("playable") or {}).get("files", []))
    return names


@app.post("/jobs/<job_id>/arrangement/apply")
def apply_arrangement(job_id: str):
    manifest = load_manifest()
    job = _job_by_id(manifest, job_id)
    if job is None:
        abort(404)
    arrangement = job.get("arrangement") or {}
    if arrangement.get("status") == "applied":
        flash("Enriched MIDI уже собран; повторное применение не выполнялось.", "warning")
        return redirect(url_for("job_details", job_id=job_id))
    if arrangement.get("status") != "draft_ready" or not arrangement.get("plan_name"):
        abort(400, description="OpenAI draft не готов к применению")
    expected_token = str(job.get("arrangement_apply_token") or "")
    submitted_token = str(request.form.get("apply_token") or "")
    if not expected_token or not secrets.compare_digest(expected_token, submitted_token):
        abort(400, description="Недействительное подтверждение применения")

    job_dir = uploads_root() / job_id
    source_path = job_dir / "input" / job["stored_name"]
    plan_name = str(arrangement["plan_name"])
    if Path(plan_name).name != plan_name:
        abort(400, description="Недействительное имя плана")
    plan_path = job_dir / "output" / plan_name
    if not source_path.is_file() or not plan_path.is_file():
        flash("Исходный GP или draft plan недоступен; baseline не изменён.", "error")
        return redirect(url_for("job_details", job_id=job_id))
    temp_dir = job_dir / "openai-apply"
    copied: list[str] = []
    try:
        apply_manifest = apply_arrangement_plan(
            source_path, plan_path, temp_dir,
            approved=True,
            seed=int(job.get("seed", 7)),
            render_options={
                "humanize": bool(job.get("humanize")),
                "ghost_notes": bool(job.get("ghost_notes")),
                "auto_sustain_vibrato": bool(job.get("auto_sustain_vibrato")),
                "fret_noise_on_hand_shift": bool(job.get("fret_noise_on_hand_shift")),
                "expand_gp_hidden_32nds": bool(job.get("expand_gp_hidden_32nds")),
                "preserve_gp_played_offsets": bool(job.get("preserve_gp_played_offsets")),
            },
            included_track_indices={
                int(track["index"]) for track in job.get("tracks", [])
                if track.get("index") is not None
            } or None,
            track_options={
                int(index): dict(options)
                for index, options in (job.get("track_options") or {}).items()
            } or None,
        )
        for artifact in sorted(temp_dir.iterdir()):
            if not artifact.is_file():
                continue
            name = artifact.name
            if name == "manifest.json":
                stem = safe_filename(Path(job["original_name"]).stem) or "song"
                name = f"{stem}_expression-manifest.json"
            destination = job_dir / "output" / name
            if destination.exists():
                raise RuntimeError("enriched artifact would overwrite an existing file")
            shutil.copy2(artifact, destination)
            copied.append(name)
    except Exception:
        for name in copied:
            (job_dir / "output" / name).unlink(missing_ok=True)
        flash("Enriched-сборка не выполнена; baseline сохранён без изменений.", "error")
        return redirect(url_for("job_details", job_id=job_id))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    job.setdefault("arrangement_artifacts", []).extend(copied)
    expression_by_track = {
        str(stats.get("track")): stats
        for stats in apply_manifest.get("track_stats", [])
    }
    for track in job.get("tracks", []):
        expression = expression_by_track.get(str(track.get("track_name")))
        if expression is None:
            continue
        track["expression"] = expression
    arrangement.update({
        "status": "applied", "applied": True,
        "apply_manifest": apply_manifest, "enriched_artifacts": copied,
    })
    job["arrangement_apply_token"] = None
    make_zip(job_dir)
    save_manifest(manifest)
    flash("Enriched MIDI собран отдельно; baseline не перезаписан.", "success")
    return redirect(url_for("job_details", job_id=job_id))


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


# --- Projects (AILab Asset Storage) ---

from ailab_client import AssetAPIError, get_client  # noqa: E402


@app.get("/projects")
def projects_list():
    try:
        client = get_client()
        projects = client.list_projects()
    except AssetAPIError as e:
        flash(f"Ошибка AILab: {e.message}", "error")
        projects = []
    except Exception as e:
        flash(f"AILab недоступен: {e}", "error")
        projects = []
    return render_template("projects.html", projects=projects)


@app.post("/projects")
def projects_create():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip() or None
    if not name:
        flash("Имя проекта обязательно.", "error")
        return redirect(url_for("projects_list"))
    try:
        client = get_client()
        result = client.create_project(name, description)
        flash(f"Проект «{name}» создан.", "success")
        return redirect(url_for("project_detail", project_id=result["id"]))
    except AssetAPIError as e:
        flash(f"Ошибка создания: {e.message}", "error")
    except Exception as e:
        flash(f"AILab недоступен: {e}", "error")
    return redirect(url_for("projects_list"))


@app.get("/projects/<project_id>")
def project_detail(project_id: str):
    try:
        client = get_client()
        data = client.get_project(project_id)
    except AssetAPIError as e:
        flash(f"Ошибка: {e.message}", "error")
        return redirect(url_for("projects_list"))
    except Exception as e:
        flash(f"AILab недоступен: {e}", "error")
        return redirect(url_for("projects_list"))

    analyses_data = {"analyses": [], "jobs": []}
    rt_error = None
    try:
        analyses_data = client.list_analyses(project_id)
    except AssetAPIError as e:
        rt_error = e.message
    except Exception as e:
        rt_error = f"Reference-time недоступен: {e}"

    all_assets = data["assets"]
    # Only a GP asset registered as a GP revision is an eligible analysis input;
    # the reference-time service rejects anything else with gp_revision_not_found.
    # `GET /v1/projects/{id}` reports the column name `asset_sha256`; only the
    # manifest endpoint renames it to `sha256`. Accept either.
    revision_by_sha = {}
    for r in data.get("gp_revisions", []):
        sha = r.get("asset_sha256") or r.get("sha256")
        if sha:
            revision_by_sha[sha] = r.get("revision")
    rt_gp = [
        dict(a, revision=revision_by_sha[a["asset_sha256"]])
        for a in all_assets
        if a.get("role") == "guitar-pro" and a.get("asset_sha256") in revision_by_sha
    ]
    rt_gp.sort(key=lambda a: -a["revision"])
    rt_midi = [a for a in all_assets if (a.get("role") or "").startswith("suno-midi")]
    rt_audio = [
        a for a in all_assets
        if a.get("role") == "mix" or (a.get("role") or "").startswith("stem.")
    ]
    rt_struct = [a for a in all_assets if a.get("role") == "structure"]

    return render_template("project_detail.html",
                           project=data["project"],
                           assets=all_assets,
                           gp_revisions=data["gp_revisions"],
                           analyses=analyses_data.get("analyses", []),
                           analysis_jobs=analyses_data.get("jobs", []),
                           rt_error=rt_error,
                           rt_gp_assets=rt_gp,
                           rt_midi_assets=rt_midi,
                           rt_audio_assets=rt_audio,
                           rt_struct_assets=rt_struct)


@app.post("/projects/<project_id>/upload")
def project_upload(project_id: str):
    role = request.form.get("role", "")
    if "file" not in request.files:
        flash("Файл не выбран.", "error")
        return redirect(url_for("project_detail", project_id=project_id))
    f = request.files["file"]
    if not f.filename:
        flash("Файл не выбран.", "error")
        return redirect(url_for("project_detail", project_id=project_id))
    try:
        client = get_client()
        ticket_data = client.create_upload_ticket(project_id, role, f.filename)
        ticket = ticket_data["ticket"]
        result = client.stream_proxy_upload(ticket, f.stream)
        sha_short = result.get("sha256", "")[:12]
        dedup = " (дедупликация)" if result.get("deduplicated") else ""
        flash(f"Загружено: {f.filename} → SHA {sha_short}{dedup}", "success")
    except AssetAPIError as e:
        flash(f"Ошибка загрузки: {e.message}", "error")
    except Exception as e:
        flash(f"AILab недоступен: {e}", "error")
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/projects/<project_id>/assets/<link_id>/delete")
def project_asset_delete(project_id: str, link_id: str):
    try:
        client = get_client()
        client.delete_asset_link(project_id, link_id)
        flash("Ссылка удалена. Физический файл сохранён.", "success")
    except AssetAPIError as e:
        flash(f"Ошибка: {e.message}", "error")
    except Exception as e:
        flash(f"AILab недоступен: {e}", "error")
    return redirect(url_for("project_detail", project_id=project_id))


@app.get("/projects/<project_id>/assets/<link_id>/download")
def project_asset_download(project_id: str, link_id: str):
    try:
        client = get_client()
        resp = client.download_asset(project_id, link_id)
        from flask import Response as FlaskResponse
        disposition = resp.headers.get("Content-Disposition", "")
        content_type = resp.headers.get("Content-Type", "application/octet-stream")

        def generate():
            for chunk in resp.iter_content(65536):
                yield chunk

        return FlaskResponse(
            generate(),
            content_type=content_type,
            headers={"Content-Disposition": disposition},
        )
    except AssetAPIError as e:
        abort(e.status_code, description=e.message)
    except Exception as e:
        abort(502, description=f"AILab недоступен: {e}")


# --- Reference-Time Analysis ---

@app.post("/projects/<project_id>/analyze")
def project_analyze(project_id: str):
    gp_link_id = request.form.get("gp_link_id", "")
    gp_sha = request.form.get("gp_sha", "")
    midi_ids = request.form.getlist("midi_link_ids")
    audio_ids = request.form.getlist("audio_link_ids")
    structure_id = request.form.get("structure_link_id") or None

    if not gp_link_id or not midi_ids:
        flash("Нужен GP файл и хотя бы один MIDI", "error")
        return redirect(url_for("project_detail", project_id=project_id))

    try:
        client = get_client()
        result = client.create_analysis(
            project_id, gp_link_id, gp_sha,
            midi_ids, audio_ids, structure_id,
        )
        if result.get("cache_hit"):
            flash(f"Анализ уже выполнен (cache hit): {result['analysis_id'][:8]}…", "success")
        else:
            flash(f"Анализ запущен: job {result['job_id'][:8]}…", "success")
    except AssetAPIError as e:
        flash(f"Ошибка анализа: {e.message}", "error")
    except Exception as e:
        flash(f"Reference-time недоступен: {e}", "error")

    return redirect(url_for("project_detail", project_id=project_id))


@app.get("/projects/<project_id>/analyses/<analysis_id>/report")
def project_analysis_report(project_id: str, analysis_id: str):
    try:
        client = get_client()
        html = client.get_analysis_report_html(project_id, analysis_id)
        return html
    except AssetAPIError as e:
        abort(e.status_code, description=e.message)
    except Exception as e:
        abort(502, description=f"Reference-time недоступен: {e}")


@app.get("/projects/<project_id>/analyses/<analysis_id>/report.json")
def project_analysis_report_json(project_id: str, analysis_id: str):
    try:
        client = get_client()
        data = client.get_analysis_report_json(project_id, analysis_id)
        from flask import jsonify
        return jsonify(data)
    except AssetAPIError as e:
        abort(e.status_code, description=e.message)
    except Exception as e:
        abort(502, description=f"Reference-time недоступен: {e}")


@app.get("/projects/<project_id>/analyses/<job_id>/status")
def project_analysis_status(project_id: str, job_id: str):
    """Single non-blocking job poll.

    One upstream request per call with the client's normal timeout: the browser
    drives the polling interval, so a long-running analysis never pins a Flask
    worker waiting for it.
    """
    from flask import jsonify
    try:
        client = get_client()
        job = client.get_analysis_job(job_id)
        if job.get("project_id") and job["project_id"] != project_id:
            return jsonify({"error": "job does not belong to this project"}), 404
        return jsonify(job)
    except AssetAPIError as e:
        return jsonify({"error": e.message, "code": e.code}), e.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
