from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import statistics
import zipfile
from dataclasses import dataclass
from pathlib import Path

import mido

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class PackageResult:
    archive_path: Path
    all_tracks_path: Path
    report_html_path: Path
    report_json_path: Path


def _safe_stem(filename: str) -> str:
    value = _SAFE.sub("_", Path(filename).stem).strip("._")
    return value or "audio"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _midi_summary(path: Path) -> tuple[list[dict[str, int | str]], int]:
    midi = mido.MidiFile(path)
    if midi.type != 1:
        raise ValueError("SheetSage2 combined MIDI must be Type 1")
    tracks: list[dict[str, int | str]] = []
    note_total = 0
    for index, track in enumerate(midi.tracks):
        notes = sum(message.type == "note_on" and message.velocity > 0 for message in track)
        if notes:
            tracks.append({"index": index, "name": track.name or f"Track {index}", "notes": notes})
            note_total += notes
    if not tracks:
        raise ValueError("SheetSage2 MIDI has no note-bearing tracks")
    return tracks, note_total


def _lab_rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    return [
        line.split("\t")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _unique_labels(path: Path) -> list[str]:
    labels: list[str] = []
    for row in _lab_rows(path):
        if len(row) >= 3 and row[2] not in labels:
            labels.append(row[2])
    return labels


def _musical_summary(model_output: Path) -> dict:
    beat_times: list[float] = []
    for row in _lab_rows(model_output / "beat.lab"):
        try:
            beat_times.append(float(row[0]))
        except (IndexError, ValueError):
            continue
    intervals = [end - start for start, end in zip(beat_times, beat_times[1:]) if end > start]
    estimated_bpm = round(60.0 / statistics.median(intervals), 2) if intervals else None
    return {
        "estimated_bpm": estimated_bpm,
        "beat_count": len(beat_times),
        "downbeat_count": len(_lab_rows(model_output / "downbeat.lab")),
        "chords": _unique_labels(model_output / "chord.lab"),
        "keys": _unique_labels(model_output / "key.lab"),
        "sections": _unique_labels(model_output / "structure.lab"),
    }


def _report_html(report: dict) -> str:
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            int(track["index"]), html.escape(str(track["name"])), int(track["notes"])
        )
        for track in report["tracks"]
    )
    summary = report["musical_summary"]
    bpm = "не определён" if summary["estimated_bpm"] is None else str(summary["estimated_bpm"])
    chords = ", ".join(summary["chords"]) or "не определены"
    keys = ", ".join(summary["keys"]) or "не определены"
    sections = ", ".join(summary["sections"]) or "не определены"
    return f"""<!doctype html>
<html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>SheetSage2 — {html.escape(report['source_filename'])}</title>
<style>body{{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 18px;color:#172033}}h1{{margin-bottom:4px}}.muted{{color:#667085}}table{{border-collapse:collapse;width:100%;margin:24px 0}}th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}code{{overflow-wrap:anywhere}}.ok{{background:#eaf8ef;padding:12px;border-radius:8px}}</style></head>
<body><h1>SheetSage2: отчёт транскрипции</h1><p class=\"muted\">{html.escape(report['source_filename'])}</p>
<p class=\"ok\">Собран единый Type‑1 MIDI: <strong>{html.escape(report['all_tracks_midi'])}</strong></p>
<ul><li>Дорожек с нотами: {report['track_count']}</li><li>Всего нот: {report['note_count']}</li><li>Время обработки: {report['elapsed_seconds']:.2f} с</li></ul>
<h2>Музыкальная разметка</h2><ul><li>Оценочный BPM: {html.escape(bpm)}</li><li>Beats / downbeats: {summary['beat_count']} / {summary['downbeat_count']}</li><li>Аккорды: {html.escape(chords)}</li><li>Тональности: {html.escape(keys)}</li><li>Секции: {html.escape(sections)}</li></ul>
<table><thead><tr><th>#</th><th>Дорожка</th><th>Нот</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Воспроизводимость</h2><p>SheetSage2 revision: <code>{html.escape(report['model_revision'])}</code></p><p>SHA‑256 WAV: <code>{report['input_sha256']}</code></p>
<p class=\"muted\">Результат является автоматической транскрипцией и предназначен для ручной проверки по тактам.</p></body></html>"""


def package_transcription(
    *,
    model_output: Path,
    destination: Path,
    original_filename: str,
    model_revision: str,
    input_sha256: str,
    elapsed_seconds: float,
) -> PackageResult:
    model_output = Path(model_output)
    source_midi = model_output / "transcription.mid"
    if not source_midi.is_file():
        raise ValueError("SheetSage2 did not produce transcription.mid")
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    raw = destination / "raw"
    raw.mkdir(parents=True)
    stem = _safe_stem(original_filename)
    all_tracks = destination / f"{stem}_ALL_TRACKS.mid"
    shutil.copyfile(source_midi, all_tracks)
    tracks, note_count = _midi_summary(all_tracks)

    for item in sorted(model_output.iterdir()):
        if item.is_file():
            shutil.copyfile(item, raw / item.name)

    report = {
        "schema_version": "gpmidi-sheetsage2-report-v1",
        "source_filename": Path(original_filename).name,
        "input_sha256": input_sha256,
        "model_revision": model_revision,
        "elapsed_seconds": float(elapsed_seconds),
        "all_tracks_midi": all_tracks.name,
        "track_count": len(tracks),
        "note_count": note_count,
        "tracks": tracks,
        "musical_summary": _musical_summary(model_output),
    }
    report_json = destination / "report.json"
    report_html = destination / "report.html"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report_html.write_text(_report_html(report), encoding="utf-8")

    artifact_paths = [path for path in destination.rglob("*") if path.is_file()]
    manifest = {
        "schema_version": "gpmidi-sheetsage2-package-v1",
        "files": [
            {
                "path": path.relative_to(destination).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in sorted(artifact_paths)
        ],
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    archive = destination / f"{stem}_SheetSage2.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file() and path != archive:
                output.write(path, path.relative_to(destination).as_posix())
    return PackageResult(archive, all_tracks, report_html, report_json)
