"""Deterministic, artifact-derived Baseline -> Enriched MIDI report."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from html import escape
from typing import Any, Iterable

from articulation_config import config_for_track_type

_OPTION_LABELS = {
    "humanize": "Humanize",
    "ghost_notes": "Ghost notes",
    "auto_sustain_vibrato": "Auto sustain vibrato",
    "fret_noise_on_hand_shift": "Fret noise on hand shift",
    "fret_hand_cost": "Fret-hand shift delay",
    "keep_gp_played_overlaps": "Keep GP played overlaps",
    "humanize_timing_over_gp_offsets": "Humanize timing over GP offsets",
    "expand_gp_hidden_32nds": "Expand hidden GP 32nds",
    "preserve_gp_played_offsets": "Preserve GP played offsets",
}
_APPLICABLE_OPTIONS = {
    "DRUMS": {"humanize", "ghost_notes"},
    "GUITAR": {
        "humanize", "auto_sustain_vibrato", "fret_noise_on_hand_shift",
        "fret_hand_cost", "expand_gp_hidden_32nds", "preserve_gp_played_offsets",
        "keep_gp_played_overlaps", "humanize_timing_over_gp_offsets",
    },
    "BASS": {
        "humanize", "fret_noise_on_hand_shift", "expand_gp_hidden_32nds",
        "preserve_gp_played_offsets", "keep_gp_played_overlaps",
        "humanize_timing_over_gp_offsets",
    },
    "OTHER": {"expand_gp_hidden_32nds"},
}


def _absolute_messages(track: Iterable[Any]) -> list[tuple[int, Any]]:
    tick = 0
    rows = []
    for message in track:
        tick += int(getattr(message, "time", 0) or 0)
        rows.append((tick, message))
    return rows


def _extract_notes(track: Iterable[Any], service_notes: set[int]) -> list[dict[str, int]]:
    active: dict[int, deque[dict[str, int]]] = defaultdict(deque)
    notes: list[dict[str, int]] = []
    for tick, message in _absolute_messages(track):
        kind = getattr(message, "type", None)
        if kind not in {"note_on", "note_off"}:
            continue
        pitch = int(getattr(message, "note", -1))
        if pitch in service_notes:
            continue
        velocity = int(getattr(message, "velocity", 0) or 0)
        if kind == "note_on" and velocity > 0:
            active[pitch].append({"pitch": pitch, "velocity": velocity, "start": tick, "end": tick})
        elif active[pitch]:
            note = active[pitch].popleft()
            note["end"] = tick
            notes.append(note)
    for queue in active.values():
        notes.extend(queue)
    return sorted(notes, key=lambda row: (row["start"], row["pitch"], row["end"]))


def _service_notes(track_type: str) -> set[int]:
    if track_type not in {"GUITAR", "BASS"}:
        return set()
    config = config_for_track_type(track_type) or {}
    notes: set[int] = set()
    for section in ("keyswitches", "fx_keyswitches"):
        for value in (config.get(section) or {}).values():
            if isinstance(value, int):
                notes.add(value)
            elif isinstance(value, dict) and value.get("note") is not None:
                notes.add(int(value["note"]))
    return notes


def _measure_for_tick(tick: int, spans: dict[int, tuple[int, int]]) -> int:
    for measure, (start, end) in spans.items():
        if start <= tick < end:
            return measure
    if not spans:
        return 1
    return min(spans) if tick < spans[min(spans)][0] else max(spans)


def _pair_notes(
    baseline: list[dict[str, int]], enriched: list[dict[str, int]],
) -> tuple[list[tuple[dict[str, int], dict[str, int]]], list[dict[str, int]], list[dict[str, int]]]:
    before_by_pitch: dict[int, list[dict[str, int]]] = defaultdict(list)
    after_by_pitch: dict[int, list[dict[str, int]]] = defaultdict(list)
    for note in baseline:
        before_by_pitch[note["pitch"]].append(note)
    for note in enriched:
        after_by_pitch[note["pitch"]].append(note)
    pairs = []
    removed = []
    added = []
    for pitch in sorted(set(before_by_pitch) | set(after_by_pitch)):
        before = sorted(before_by_pitch[pitch], key=lambda row: (row["start"], row["end"]))
        after = sorted(after_by_pitch[pitch], key=lambda row: (row["start"], row["end"]))
        shared = min(len(before), len(after))
        pairs.extend(zip(before[:shared], after[:shared], strict=True))
        removed.extend(before[shared:])
        added.extend(after[shared:])
    return pairs, removed, added


def _control_signature(track: Iterable[Any]) -> dict[tuple[str, int], list[tuple[int, int]]]:
    result: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    for tick, message in _absolute_messages(track):
        kind = getattr(message, "type", None)
        if kind == "control_change":
            result[(kind, int(message.control))].append((tick, int(message.value)))
        elif kind == "pitchwheel":
            result[(kind, -1)].append((tick, int(message.pitch)))
    return result


def _service_signature(track: Iterable[Any], service_notes: set[int]) -> dict[int, list[tuple[int, int]]]:
    result: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for tick, message in _absolute_messages(track):
        if (
            getattr(message, "type", None) == "note_on"
            and int(getattr(message, "velocity", 0) or 0) > 0
            and int(getattr(message, "note", -1)) in service_notes
        ):
            result[int(message.note)].append((tick, int(message.velocity)))
    return result


def _sequence_difference_ticks(before: dict[Any, list[Any]], after: dict[Any, list[Any]]) -> list[int]:
    """Return one attribution tick for every changed, added, or removed event."""
    changed_ticks = []
    for key in set(before) | set(after):
        left = before.get(key, [])
        right = after.get(key, [])
        shared = min(len(left), len(right))
        for index in range(shared):
            if left[index] != right[index]:
                changed_ticks.append(int(left[index][0]))
        changed_ticks.extend(int(item[0]) for item in left[shared:])
        changed_ticks.extend(int(item[0]) for item in right[shared:])
    return changed_ticks


def _drum_label(pitch: int) -> str:
    config = config_for_track_type("DRUMS") or {}
    return str((config.get("kit_layout") or {}).get(pitch) or f"MIDI note {pitch}")


def _note_counter_rows(counter: Counter[int], track_type: str) -> list[dict[str, Any]]:
    return [
        {
            "pitch": pitch,
            "label": _drum_label(pitch) if track_type == "DRUMS" else f"MIDI note {pitch}",
            "count": count,
        }
        for pitch, count in sorted(counter.items())
    ]


def _recommendation(track_name: str, track_type: str, totals: dict[str, int], max_velocity: int) -> dict[str, str]:
    changed = sum(totals[key] for key in (
        "velocity_changed", "onset_changed", "duration_changed", "added_notes",
        "removed_notes", "controller_events_changed", "service_events_changed",
    ))
    if changed == 0:
        return {
            "code": "baseline",
            "label": "Baseline",
            "reason": "Фактических отличий нет: Enriched не даёт дополнительного результата.",
        }
    solo = track_type == "GUITAR" and any(word in track_name.casefold() for word in ("solo", "lead"))
    if solo and any(totals[key] for key in (
        "onset_changed", "duration_changed", "controller_events_changed", "service_events_changed",
    )):
        return {
            "code": "baseline",
            "label": "Baseline предпочтительнее",
            "reason": "В Solo/Lead изменены тайминг, длительности или управляющие события; это наиболее слышимая зона риска.",
        }
    if totals["added_notes"] or totals["removed_notes"]:
        return {
            "code": "compare",
            "label": "Обязательно сравнить",
            "reason": "Изменилось количество нот/ударов; перед выбором нужна слуховая проверка.",
        }
    if any(totals[key] for key in (
        "onset_changed", "duration_changed", "controller_events_changed", "service_events_changed",
    )):
        return {
            "code": "compare",
            "label": "Обязательно сравнить",
            "reason": "Изменены тайминг, длительности или управляющие события — это может заметно повлиять на исполнение.",
        }
    ratio = totals["velocity_changed"] / max(1, totals["paired_notes"])
    if ratio <= 0.35 and max_velocity <= 8:
        return {
            "code": "enriched",
            "label": "Enriched можно использовать",
            "reason": "Только умеренные velocity-изменения на ограниченной части нот; структура и тайминг сохранены.",
        }
    drum_note = " Новых ударов не добавлено." if track_type == "DRUMS" else ""
    return {
        "code": "compare",
        "label": "Сравнить версии",
        "reason": "Velocity изменена широко или заметно; структура сохранена, но баланс может отличаться." + drum_note,
    }


def analyze_track_changes(
    baseline: Iterable[Any],
    enriched: Iterable[Any],
    *,
    track_name: str,
    track_type: str,
    spans: dict[int, tuple[int, int]],
    tempo_bpm: float,
    baseline_options: dict[str, Any] | None = None,
    service_notes: set[int] | None = None,
) -> dict[str, Any]:
    """Return factual per-measure deltas between two rendered MIDI tracks."""
    baseline = list(baseline)
    enriched = list(enriched)
    service_notes = set(service_notes) if service_notes is not None else _service_notes(track_type)
    before_notes = _extract_notes(baseline, service_notes)
    after_notes = _extract_notes(enriched, service_notes)
    pairs, removed, added = _pair_notes(before_notes, after_notes)
    rows: dict[int, dict[str, Any]] = {}

    def measure_row(measure: int) -> dict[str, Any]:
        if measure not in rows:
            rows[measure] = {
                "measure": measure,
                "velocity_changed": 0,
                "velocity_deltas": [],
                "velocity_transitions_counter": Counter(),
                "onset_changed": 0,
                "duration_changed": 0,
                "controller_events_changed": 0,
                "service_events_changed": 0,
                "max_abs_onset_shift_ticks": 0,
                "destinations": set(),
                "arrivals_from": set(),
                "added_counter": Counter(),
                "removed_counter": Counter(),
            }
        return rows[measure]

    velocity_changed = 0
    onset_changed = 0
    duration_changed = 0
    max_abs_onset = 0
    max_abs_velocity = 0
    for before, after in pairs:
        source_measure = _measure_for_tick(before["start"], spans)
        destination_measure = _measure_for_tick(after["start"], spans)
        velocity_delta = after["velocity"] - before["velocity"]
        onset_delta = after["start"] - before["start"]
        before_duration = before["end"] - before["start"]
        after_duration = after["end"] - after["start"]
        if not (velocity_delta or onset_delta or before_duration != after_duration):
            continue
        row = measure_row(source_measure)
        if velocity_delta:
            velocity_changed += 1
            max_abs_velocity = max(max_abs_velocity, abs(velocity_delta))
            row["velocity_changed"] += 1
            row["velocity_deltas"].append(velocity_delta)
            row["velocity_transitions_counter"][(before["velocity"], after["velocity"])] += 1
        if onset_delta:
            onset_changed += 1
            max_abs_onset = max(max_abs_onset, abs(onset_delta))
            row["onset_changed"] += 1
            row["max_abs_onset_shift_ticks"] = max(row["max_abs_onset_shift_ticks"], abs(onset_delta))
            if destination_measure != source_measure:
                row["destinations"].add(destination_measure)
                measure_row(destination_measure)["arrivals_from"].add(source_measure)
        if before_duration != after_duration:
            duration_changed += 1
            row["duration_changed"] += 1

    for note in removed:
        measure_row(_measure_for_tick(note["start"], spans))["removed_counter"][note["pitch"]] += 1
    for note in added:
        measure_row(_measure_for_tick(note["start"], spans))["added_counter"][note["pitch"]] += 1

    controller_change_ticks = _sequence_difference_ticks(
        _control_signature(baseline), _control_signature(enriched)
    )
    service_change_ticks = _sequence_difference_ticks(
        _service_signature(baseline, service_notes),
        _service_signature(enriched, service_notes),
    )
    for tick in controller_change_ticks:
        measure_row(_measure_for_tick(tick, spans))["controller_events_changed"] += 1
    for tick in service_change_ticks:
        measure_row(_measure_for_tick(tick, spans))["service_events_changed"] += 1
    controller_changed = len(controller_change_ticks)
    service_changed = len(service_change_ticks)

    measures = []
    for measure in sorted(rows):
        row = rows[measure]
        deltas = row.pop("velocity_deltas")
        transitions = row.pop("velocity_transitions_counter")
        added_counter = row.pop("added_counter")
        removed_counter = row.pop("removed_counter")
        row["velocity_delta_mean"] = round(sum(deltas) / len(deltas), 2) if deltas else 0.0
        row["velocity_delta_min"] = min(deltas) if deltas else 0
        row["velocity_delta_max"] = max(deltas) if deltas else 0
        row["velocity_transitions"] = [
            {"before": before, "after": after, "count": count}
            for (before, after), count in sorted(transitions.items())
        ]
        row["destinations"] = sorted(row["destinations"])
        row["arrivals_from"] = sorted(row["arrivals_from"])
        row["added_notes"] = _note_counter_rows(added_counter, track_type)
        row["removed_notes"] = _note_counter_rows(removed_counter, track_type)
        measures.append(row)

    totals = {
        "baseline_notes": len(before_notes),
        "enriched_notes": len(after_notes),
        "paired_notes": len(pairs),
        "velocity_changed": velocity_changed,
        "onset_changed": onset_changed,
        "duration_changed": duration_changed,
        "added_notes": len(added),
        "removed_notes": len(removed),
        "controller_events_changed": controller_changed,
        "service_events_changed": service_changed,
    }
    ms_per_tick = 60_000.0 / max(float(tempo_bpm or 120), 1.0) / 960.0
    processing = [
        label for key, label in _OPTION_LABELS.items()
        if key in _APPLICABLE_OPTIONS.get(track_type, set())
        and bool((baseline_options or {}).get(key))
    ]
    return {
        "track": str(track_name),
        "type": str(track_type),
        "baseline_processing": processing,
        "totals": totals,
        "new_drum_hits": len(added) if track_type == "DRUMS" else None,
        "affected_measures": sorted(rows),
        "measures": measures,
        "max_abs_velocity_delta": max_abs_velocity,
        "max_abs_onset_shift_ticks": max_abs_onset,
        "max_abs_onset_shift_ms": round(max_abs_onset * ms_per_tick, 2),
        "recommendation": _recommendation(str(track_name), str(track_type), totals, max_abs_velocity),
    }


def build_enrichment_report(
    *, source_name: str, source_sha256: str, plan_summary: str,
    model: str, tracks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "comparison": "baseline_to_enriched_actual_midi",
        "source": {"name": str(source_name), "sha256": str(source_sha256)},
        "plan": {"model": str(model or "—"), "summary": str(plan_summary or "")},
        "summary": {
            "tracks": len(tracks),
            "tracks_changed": sum(1 for track in tracks if track["affected_measures"]),
            "affected_measures": len({
                measure for track in tracks for measure in track["affected_measures"]
            }),
            "velocity_changed": sum(track["totals"]["velocity_changed"] for track in tracks),
            "onset_changed": sum(track["totals"]["onset_changed"] for track in tracks),
            "added_notes": sum(track["totals"]["added_notes"] for track in tracks),
            "removed_notes": sum(track["totals"]["removed_notes"] for track in tracks),
            "new_drum_hits": sum(track["new_drum_hits"] or 0 for track in tracks),
        },
        "tracks": tracks,
    }


def _metric(label: str, value: Any, note: str = "") -> str:
    return (
        '<div class="metric"><span>' + escape(str(label)) + '</span>'
        '<strong>' + escape(str(value)) + '</strong>'
        + (f'<small>{escape(note)}</small>' if note else "") + "</div>"
    )


def render_enrichment_html(report: dict[str, Any]) -> str:
    """Render a self-contained, light, mobile-friendly Russian HTML report."""
    source = report["source"]
    summary = report["summary"]
    track_sections = []
    for track in report["tracks"]:
        totals = track["totals"]
        processing = ", ".join(track["baseline_processing"]) or "без дополнительных опций"
        measure_cards = []
        for row in track["measures"]:
            details = []
            if row["velocity_changed"]:
                details.append(
                    f'Velocity: {row["velocity_changed"]} нот, Δ {row["velocity_delta_min"]:+d}…{row["velocity_delta_max"]:+d}, среднее {row["velocity_delta_mean"]:+.2f}'
                )
                transitions = ", ".join(
                    f'{item["before"]}→{item["after"]} ×{item["count"]}'
                    for item in row["velocity_transitions"][:8]
                )
                if transitions:
                    details.append("Переходы: " + transitions)
            if row["onset_changed"]:
                details.append(
                    f'Сдвинуты атаки: {row["onset_changed"]}, максимум {row["max_abs_onset_shift_ticks"]} тиков'
                )
            if row["duration_changed"]:
                details.append(f'Изменены длительности: {row["duration_changed"]}')
            if row["controller_events_changed"]:
                details.append(f'Изменены CC/Pitch Bend события: {row["controller_events_changed"]}')
            if row["service_events_changed"]:
                details.append(f'Изменены keyswitch-события: {row["service_events_changed"]}')
            if row["destinations"]:
                details.append("Перешли через границу в такт(ы): " + ", ".join(map(str, row["destinations"])))
            if row["arrivals_from"]:
                details.append("Пришли через границу из такта(ов): " + ", ".join(map(str, row["arrivals_from"])))
            for kind, title in (("added_notes", "Добавлены"), ("removed_notes", "Удалены")):
                if row[kind]:
                    values = ", ".join(
                        f'{item["label"]} (MIDI {item["pitch"]}) ×{item["count"]}' for item in row[kind]
                    )
                    details.append(f"{title}: {values}")
            if not details:
                details.append("Такт отмечен из-за связанного переноса через тактовую черту.")
            measure_cards.append(
                f'<article class="measure"><h4>Такт {row["measure"]}</h4><ul>'
                + "".join(f"<li>{escape(value)}</li>" for value in details)
                + "</ul></article>"
            )
        if not measure_cards:
            measure_cards.append('<p class="empty">Baseline и Enriched фактически совпадают.</p>')
        drum_metric = ""
        if track["type"] == "DRUMS":
            drum_metric = _metric("Новых ударов в Enriched", track["new_drum_hits"], "относительно Baseline")
        recommendation = track["recommendation"]
        track_sections.append(f'''
        <section class="track-card">
          <div class="track-head">
            <div><span class="eyebrow">{escape(track["type"])}</span><h2>{escape(track["track"])}</h2></div>
            <span class="decision {escape(recommendation["code"])}">{escape(recommendation["label"])}</span>
          </div>
          <p class="reason">{escape(recommendation["reason"])}</p>
          <div class="baseline-note"><strong>Baseline уже включал:</strong> {escape(processing)}.</div>
          <div class="metrics compact">
            {_metric("Velocity", totals["velocity_changed"], "изменённых нот")}
            {_metric("Атаки", totals["onset_changed"], "сдвинутых")}
            {_metric("Длительности", totals["duration_changed"], "изменённых")}
            {_metric("Добавлено / удалено", f'{totals["added_notes"]} / {totals["removed_notes"]}', "музыкальных нот")}
            {drum_metric}
          </div>
          <details open><summary>Изменённые такты: {escape(", ".join(map(str, track["affected_measures"])) or "нет")}</summary>
            <div class="measure-grid">{"".join(measure_cards)}</div>
          </details>
        </section>''')

    plan = report["plan"]
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Фактический отчёт Baseline → Enriched</title>
<style>
*,*::before,*::after{{box-sizing:border-box}}html{{background: #fbfaf8;color:#31302e}}
body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5;background: #fbfaf8}}
main{{max-width:1120px;margin:0 auto;padding:52px 28px 80px}}h1,h2,h3,h4,p{{margin-top:0}}
h1{{font-size:clamp(30px,5vw,52px);line-height:1.04;letter-spacing:-.035em;margin-bottom:14px}}
h2{{font-size:24px;letter-spacing:-.02em;margin-bottom:0}}.lead{{font-size:18px;color:#615d59;max-width:780px}}
.eyebrow{{font-size:12px;font-weight:700;letter-spacing:.09em;color:#6e6862}}.source{{font-family:ui-monospace,monospace;font-size:12px;color:#6e6862;overflow-wrap:anywhere}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:28px 0}}
.metrics.compact{{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:18px 0}}
.metric{{background:#fff;border:1px solid rgba(49,48,46,.11);border-radius:14px;padding:16px;box-shadow:0 4px 18px rgba(49,48,46,.035)}}
.metric span,.metric small{{display:block;color:#6e6862;font-size:12px}}.metric strong{{display:block;font-size:26px;margin:4px 0;color:#31302e}}
.context,.track-card{{background:#fff;border:1px solid rgba(49,48,46,.11);border-radius:16px;padding:24px;margin:18px 0;box-shadow:0 4px 18px rgba(49,48,46,.035)}}
.context{{background:#f5f2ed}}.track-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}}
.decision{{display:inline-flex;border-radius:999px;padding:6px 11px;font-size:12px;font-weight:700;white-space:nowrap}}
.decision.baseline{{background:#f6eee9;color:#84533d}}.decision.enriched{{background:#eaf5ef;color:#2f6b4f}}.decision.compare{{background:#f7f0df;color:#7b6225}}
.reason{{color:#4f4b47;margin:14px 0}}.baseline-note{{background:#f6f5f4;border-radius:10px;padding:11px 13px;color:#56514d}}
details{{border-top:1px solid rgba(49,48,46,.1);padding-top:15px}}summary{{cursor:pointer;font-weight:650}}
.measure-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:14px}}.measure{{background:#fbfaf8;border:1px solid rgba(49,48,46,.09);border-radius:12px;padding:14px}}
.measure h4{{margin-bottom:7px}}.measure ul{{margin:0;padding-left:19px;color:#56514d}}.measure li+li{{margin-top:4px}}.empty{{color:#6e6862;margin:12px 0 0}}
.footer{{margin-top:30px;color:#77716b;font-size:12px}}
@media (max-width: 720px){{main{{padding:30px 16px 56px}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.measure-grid{{grid-template-columns:1fr}}.track-head{{display:block}}.decision{{margin-top:10px;white-space:normal}}}}
@media print{{html,body{{background:#fff}}main{{max-width:none;padding:12mm}}.track-card,.context,.metric{{box-shadow:none;break-inside:avoid}}}}
</style>
</head>
<body><main>
<header><span class="eyebrow">GPMIDI · ARTIFACT DIFF</span><h1>Фактические изменения Baseline → Enriched</h1>
<p class="lead">Отчёт построен по реально выпущенным MIDI, а не по намерениям модели. Он показывает, какие дорожки и такты изменились и насколько рискованно выбирать Enriched без прослушивания.</p>
<p class="source">Источник: {escape(source["name"])} · SHA-256 {escape(source["sha256"])}</p></header>
<div class="metrics">
{_metric("Дорожек изменено", f'{summary["tracks_changed"]} / {summary["tracks"]}')}
{_metric("Затронуто тактов", summary["affected_measures"])}
{_metric("Velocity", summary["velocity_changed"], "изменённых нот")}
{_metric("Новых ударов", summary["new_drum_hits"], "в Enriched")}
</div>
<section class="context"><span class="eyebrow">ПЛАН МОДЕЛИ</span><h3>{escape(plan["model"])}</h3><p>{escape(plan["summary"] or "Резюме в плане отсутствует.")}</p><p><strong>Важно:</strong> выбранные Humanize/Ghost notes уже входят в Baseline. Ниже показана только фактическая разница между скачиваемыми версиями.</p></section>
{"".join(track_sections)}
<p class="footer">Рекомендация основана на структурном риске. Она не заменяет слуховое сравнение и не оценивает качество тембра Kontakt/Logic.</p>
</main></body></html>'''
