#!/usr/bin/env python3
"""Build a bar-aligned reference-stem dynamics report for Spring Melody.

The script intentionally reconstructs audible relative balance, not hidden DAW
fader/plugin automation. Reference MIDI supplies the floating audio-time grid.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import math
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mido
import numpy as np
import soundfile as sf
from scipy.signal import butter, lfilter, sosfilt

ROLE_ORDER = ["Voice", "Guitar", "Synth pair", "Drums", "Bass", "Keyboard pair"]
SOURCE_ROLE = {
    "Vocals": "Voice",
    "Guitar": "Guitar",
    "Synth": "Synth pair",
    "Drums": "Drums",
    "Bass": "Bass",
    "Keyboard": "Keyboard pair",
}
COLORS = {
    "Voice": "#e45756",
    "Guitar": "#f2a541",
    "Synth pair": "#8f63d2",
    "Drums": "#4c78a8",
    "Bass": "#59a14f",
    "Keyboard pair": "#76b7b2",
}
CURRENT_TARGET = {
    "Voice": "Voice (temporary solo violin)",
    "Guitar": "the merged Guitar stem (rhythm + solo together)",
    "Synth pair": "Synth 1 and Synth 2 together as the Hydra pair",
    "Drums": "Drums",
    "Bass": "Bass",
    "Keyboard pair": "both Keyboard tracks together (treble + bass clef)",
}

# ITU-R BS.1770 K-weighting coefficients for 48 kHz.
K_SHELF_B = np.array([1.53512485958697, -2.69169618940638, 1.19839281085285])
K_SHELF_A = np.array([1.0, -1.69065929318241, 0.73248077421585])
K_HIGHPASS_B = np.array([1.0, -2.0, 1.0])
K_HIGHPASS_A = np.array([1.0, -1.99004745483398, 0.99007225036621])
EPS = 1e-15


@dataclass(frozen=True)
class Segment:
    role: str
    start_bar: int
    end_bar: int
    direction: str
    median_db: float
    peak_db: float
    mean_db: float


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def db10(power: np.ndarray | float) -> np.ndarray | float:
    return 10.0 * np.log10(np.maximum(power, EPS))


def db20(amplitude: np.ndarray | float) -> np.ndarray | float:
    return 20.0 * np.log10(np.maximum(amplitude, EPS))


def discover_inputs(project_root: Path) -> tuple[Path, dict[str, dict[str, Path]]]:
    masters = [p for p in project_root.glob("*.wav") if p.is_file()]
    if len(masters) != 1:
        raise RuntimeError(f"Expected exactly one top-level master WAV, got {masters}")
    stems: dict[str, dict[str, Path]] = {}
    for wav in project_root.rglob("*.wav"):
        if wav == masters[0]:
            continue
        match = re.search(r"\((Vocals|Guitar|Synth|Drums|Bass|Keyboard)\)\.wav$", wav.name)
        if not match:
            continue
        source_name = match.group(1)
        role = SOURCE_ROLE[source_name]
        midi = wav.with_suffix(".mid")
        if not midi.exists():
            raise RuntimeError(f"Missing matching MIDI for {wav}")
        stems[role] = {"wav": wav, "midi": midi, "source_role": source_name}
    missing = set(ROLE_ORDER) - set(stems)
    if missing:
        raise RuntimeError(f"Missing stem roles: {sorted(missing)}")
    return masters[0], stems


def tempo_signature(path: Path) -> dict[str, Any]:
    midi = mido.MidiFile(path)
    abs_tick = 0
    events: list[tuple[int, int]] = []
    max_tick = 0
    for msg in mido.merge_tracks(midi.tracks):
        abs_tick += msg.time
        max_tick = max(max_tick, abs_tick)
        if msg.type == "set_tempo":
            events.append((abs_tick, int(msg.tempo)))
    if not events or events[0][0] != 0:
        events.insert(0, (0, 500000))
    # Last tempo at the same tick wins.
    collapsed: list[tuple[int, int]] = []
    for tick, tempo in events:
        if collapsed and collapsed[-1][0] == tick:
            collapsed[-1] = (tick, tempo)
        else:
            collapsed.append((tick, tempo))
    return {
        "ticks_per_beat": midi.ticks_per_beat,
        "max_tick": max_tick,
        "tempo_events": collapsed,
    }


def time_at_tick(target: int, sig: dict[str, Any]) -> float:
    events: list[tuple[int, int]] = sig["tempo_events"]
    tpb = sig["ticks_per_beat"]
    elapsed = 0.0
    current_tick = events[0][0]
    current_tempo = events[0][1]
    for tick, tempo in events[1:]:
        if tick >= target:
            break
        elapsed += mido.tick2second(tick - current_tick, tpb, current_tempo)
        current_tick = tick
        current_tempo = tempo
    elapsed += mido.tick2second(target - current_tick, tpb, current_tempo)
    return float(elapsed)


def build_bar_grid(stems: dict[str, dict[str, Path]], wav_duration: float) -> tuple[list[float], dict[str, Any]]:
    signatures = {role: tempo_signature(paths["midi"]) for role, paths in stems.items()}
    canonical = signatures["Drums"]
    canonical_key = (canonical["ticks_per_beat"], canonical["tempo_events"])
    mismatches = []
    for role, sig in signatures.items():
        key = (sig["ticks_per_beat"], sig["tempo_events"])
        if key != canonical_key:
            mismatches.append(role)
    if mismatches:
        raise RuntimeError(f"Reference MIDI tempo maps differ: {mismatches}")
    tpb = canonical["ticks_per_beat"]
    bar_ticks = 4 * tpb
    full_bars = min(sig["max_tick"] // bar_ticks for sig in signatures.values())
    boundaries = [time_at_tick(i * bar_ticks, canonical) for i in range(full_bars + 1)]
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        raise RuntimeError("Non-monotonic bar grid")
    if boundaries[-1] > wav_duration:
        raise RuntimeError("MIDI bar grid exceeds WAV duration")
    tempos = [60_000_000.0 / tempo for _, tempo in canonical["tempo_events"]]
    meta = {
        "ticks_per_beat": tpb,
        "bar_ticks": bar_ticks,
        "full_bars": full_bars,
        "tempo_event_count": len(canonical["tempo_events"]),
        "tempo_bpm_min": min(tempos),
        "tempo_bpm_median": float(np.median(tempos)),
        "tempo_bpm_max": max(tempos),
        "last_full_bar_end_seconds": boundaries[-1],
        "wav_tail_seconds": wav_duration - boundaries[-1],
        "boundaries_seconds": boundaries,
    }
    return boundaries, meta


def k_weight(audio: np.ndarray, samplerate: int) -> np.ndarray:
    if samplerate != 48000:
        raise RuntimeError(f"K-weight coefficients are validated only for 48 kHz, got {samplerate}")
    stage1 = lfilter(K_SHELF_B, K_SHELF_A, audio, axis=0)
    return lfilter(K_HIGHPASS_B, K_HIGHPASS_A, stage1, axis=0)


def per_bar_power(audio: np.ndarray, samplerate: int, boundaries: list[float]) -> np.ndarray:
    out = []
    for start, end in zip(boundaries, boundaries[1:]):
        a = max(0, min(len(audio), int(round(start * samplerate))))
        b = max(a + 1, min(len(audio), int(round(end * samplerate))))
        block = audio[a:b]
        out.append(float(np.mean(np.square(block, dtype=np.float64))))
    return np.array(out)


def analyze_audio(path: Path, boundaries: list[float]) -> tuple[dict[str, Any], np.ndarray]:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] != 2:
        raise RuntimeError(f"Expected stereo: {path}")
    weighted = k_weight(audio, sr)
    k_power = per_bar_power(weighted, sr, boundaries)
    raw_power = per_bar_power(audio, sr, boundaries)
    sample_peak = []
    crest = []
    for start, end, rp in zip(boundaries, boundaries[1:], raw_power):
        a = max(0, min(len(audio), int(round(start * sr))))
        b = max(a + 1, min(len(audio), int(round(end * sr))))
        peak = float(np.max(np.abs(audio[a:b])))
        sample_peak.append(float(db20(peak)))
        crest.append(float(db20(peak / math.sqrt(max(rp, EPS)))))

    nyq = sr / 2.0
    bands = {
        "sub_20_120": (20.0, 120.0),
        "lowmid_120_2000": (120.0, 2000.0),
        "presence_2000_8000": (2000.0, 8000.0),
        "air_8000_20000": (8000.0, min(20000.0, nyq * 0.98)),
    }
    band_power: dict[str, list[float]] = {}
    for name, (lo, hi) in bands.items():
        sos = butter(3, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
        filtered = sosfilt(sos, audio, axis=0)
        band_power[name] = per_bar_power(filtered, sr, boundaries).tolist()
        del filtered

    metrics = {
        "samplerate": sr,
        "channels": audio.shape[1],
        "frames": len(audio),
        "duration_seconds": len(audio) / sr,
        "k_loudness_db": (-0.691 + db10(k_power)).tolist(),
        "rms_dbfs": db10(raw_power).tolist(),
        "sample_peak_dbfs": sample_peak,
        "crest_db": crest,
        "band_power": band_power,
    }
    return metrics, audio


def rolling_median_active(values: np.ndarray, active: np.ndarray) -> np.ndarray:
    result = np.full_like(values, np.nan, dtype=float)
    for idx in range(len(values)):
        if not active[idx]:
            continue
        lo, hi = max(0, idx - 1), min(len(values), idx + 2)
        candidates = values[lo:hi][active[lo:hi]]
        if len(candidates):
            result[idx] = float(np.median(candidates))
    return result


def close_single_bar_gaps(mask: np.ndarray, values: np.ndarray, active: np.ndarray) -> np.ndarray:
    result = mask.copy()
    for idx in range(1, len(mask) - 1):
        if result[idx] or not active[idx]:
            continue
        if result[idx - 1] and result[idx + 1] and np.sign(values[idx - 1]) == np.sign(values[idx + 1]):
            if np.sign(values[idx]) == np.sign(values[idx - 1]):
                result[idx] = True
    return result


def detect_segments(role: str, relative: np.ndarray, active: np.ndarray, threshold: float, single_threshold: float) -> list[Segment]:
    candidate = active & np.isfinite(relative) & (np.abs(relative) >= threshold)
    candidate = close_single_bar_gaps(candidate, relative, active)
    segments: list[Segment] = []
    idx = 0
    while idx < len(relative):
        if not candidate[idx]:
            idx += 1
            continue
        sign = 1 if relative[idx] > 0 else -1
        start = idx
        idx += 1
        while idx < len(relative) and candidate[idx] and (1 if relative[idx] > 0 else -1) == sign:
            idx += 1
        end = idx - 1
        vals = relative[start:end + 1]
        peak = float(np.max(np.abs(vals)))
        if end > start or peak >= single_threshold:
            segments.append(Segment(
                role=role,
                start_bar=start + 1,
                end_bar=end + 1,
                direction="raise" if sign > 0 else "lower",
                median_db=float(np.median(vals)),
                peak_db=float(vals[np.argmax(np.abs(vals))]),
                mean_db=float(np.mean(vals)),
            ))
    return segments


def segment_to_text(segment: Segment) -> str:
    target = CURRENT_TARGET[segment.role]
    amount = round(abs(segment.median_db) * 2) / 2
    bars = f"bar {segment.start_bar}" if segment.start_bar == segment.end_bar else f"bars {segment.start_bar}–{segment.end_bar}"
    verb = "bring up" if segment.direction == "raise" else "pull back"
    caveat = " Keep rhythm and solo guitar linked; do not automate them separately." if segment.role == "Guitar" else ""
    caution = " This is a strong structural contrast; audition before keeping the full amount." if abs(segment.median_db) >= 6.0 else ""
    return f"For {bars}, {verb} {target} by about {amount:.1f} dB relative to the other active instruments.{caveat}{caution}"


def group_prompts(segments: list[Segment]) -> list[dict[str, Any]]:
    # Conservative grouping: only segments with near-identical windows are combined.
    remaining = sorted(segments, key=lambda s: (s.start_bar, s.end_bar, ROLE_ORDER.index(s.role)))
    groups: list[list[Segment]] = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        keep = []
        seed_len = seed.end_bar - seed.start_bar + 1
        for seg in remaining:
            overlap = max(0, min(seed.end_bar, seg.end_bar) - max(seed.start_bar, seg.start_bar) + 1)
            shorter = min(seed_len, seg.end_bar - seg.start_bar + 1)
            near = abs(seed.start_bar - seg.start_bar) <= 2 and abs(seed.end_bar - seg.end_bar) <= 2
            if overlap >= max(1, math.ceil(shorter * 0.6)) and near:
                group.append(seg)
            else:
                keep.append(seg)
        remaining = keep
        groups.append(group)
    prompts = []
    for number, group in enumerate(groups, 1):
        start = min(s.start_bar for s in group)
        end = max(s.end_bar for s in group)
        sentences = [segment_to_text(s) for s in group]
        sentences.append("Keep the transition smooth and leave all smaller fluctuations unchanged for the later mix/master pass.")
        prompts.append({
            "number": number,
            "start_bar": start,
            "end_bar": end,
            "segments": [s.__dict__ for s in group],
            "text": " ".join(sentences),
        })
    return prompts


def png_data_uri(fig: plt.Figure, output_path: Path) -> str:
    fig.savefig(output_path, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def make_plots(output: Path, processed: dict[str, Any], threshold: float) -> dict[str, str]:
    plots = output / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    bars = np.arange(1, processed["bar_count"] + 1)
    uris: dict[str, str] = {}

    fig, ax = plt.subplots(figsize=(18, 6), facecolor="#0f172a")
    ax.set_facecolor("#111c34")
    for role in ROLE_ORDER:
        vals = np.array(processed["roles"][role]["within_stem_db"], dtype=float)
        active = np.array(processed["roles"][role]["active"], dtype=bool)
        vals[~active] = np.nan
        ax.plot(bars, vals, lw=1.8, color=COLORS[role], label=role)
    ax.axhline(0, color="#94a3b8", lw=.8)
    ax.set_xlim(.5, processed["bar_count"] + .5)
    ax.set(title="Внутренняя динамика каждого stem относительно его активной медианы", xlabel="Такт", ylabel="dB")
    ax.grid(alpha=.18); ax.legend(ncol=6, loc="upper center", bbox_to_anchor=(.5, 1.16))
    ax.tick_params(colors="#cbd5e1"); ax.xaxis.label.set_color("#cbd5e1"); ax.yaxis.label.set_color("#cbd5e1"); ax.title.set_color("#f8fafc")
    uris["within"] = png_data_uri(fig, plots / "01_within_stem_dynamics.png")

    fig, ax = plt.subplots(figsize=(18, 4.5), facecolor="#0f172a")
    ax.set_facecolor("#111c34")
    ax.plot(bars, processed["common_mode_db"], color="#f8fafc", lw=2)
    ax.fill_between(bars, 0, processed["common_mode_db"], color="#64748b", alpha=.35)
    ax.axhline(0, color="#94a3b8", lw=.8)
    ax.set_xlim(.5, processed["bar_count"] + .5)
    ax.set(title="Common-mode: общий подъём/спад ансамбля — не превращаем в шесть отдельных команд", xlabel="Такт", ylabel="dB")
    ax.grid(alpha=.18); ax.tick_params(colors="#cbd5e1"); ax.xaxis.label.set_color("#cbd5e1"); ax.yaxis.label.set_color("#cbd5e1"); ax.title.set_color("#f8fafc")
    uris["common"] = png_data_uri(fig, plots / "02_common_mode.png")

    fig, axes = plt.subplots(len(ROLE_ORDER), 1, figsize=(18, 13), sharex=True, facecolor="#0f172a")
    for ax, role in zip(axes, ROLE_ORDER):
        ax.set_facecolor("#111c34")
        vals = np.array(processed["roles"][role]["relative_db"], dtype=float)
        ax.plot(bars, vals, color=COLORS[role], lw=1.8)
        ax.axhline(threshold, color="#ef4444", ls="--", lw=.8); ax.axhline(-threshold, color="#ef4444", ls="--", lw=.8)
        ax.fill_between(bars, -threshold, threshold, color="#334155", alpha=.25)
        ax.set_xlim(.5, processed["bar_count"] + .5)
        ax.set_ylabel(role, rotation=0, ha="right", va="center", color="#f8fafc")
        ax.grid(alpha=.15); ax.tick_params(colors="#cbd5e1")
    axes[-1].set_xlabel("Такт", color="#cbd5e1")
    axes[0].set_title("Относительные отклонения после удаления common-mode", color="#f8fafc", pad=12)
    uris["relative"] = png_data_uri(fig, plots / "03_relative_deviations.png")

    matrix = np.array([processed["roles"][role]["relative_db"] for role in ROLE_ORDER], dtype=float)
    masked = np.ma.masked_invalid(matrix)
    vmax = max(3.0, float(np.nanpercentile(np.abs(matrix), 95)))
    fig, ax = plt.subplots(figsize=(18, 4.5), facecolor="#0f172a")
    ax.set_facecolor("#111c34")
    im = ax.imshow(masked, aspect="auto", cmap="RdBu_r", vmin=-vmax, vmax=vmax, extent=[.5, processed["bar_count"]+.5, len(ROLE_ORDER)-.5, -.5])
    ax.set_yticks(range(len(ROLE_ORDER)), ROLE_ORDER); ax.set_xlabel("Такт"); ax.set_title("Heatmap относительной роли: красный = вперёд, синий = назад")
    ax.tick_params(colors="#cbd5e1"); ax.xaxis.label.set_color("#cbd5e1"); ax.title.set_color("#f8fafc")
    cb=fig.colorbar(im, ax=ax, pad=.01); cb.set_label("dB", color="#cbd5e1"); cb.ax.tick_params(colors="#cbd5e1")
    uris["heatmap"] = png_data_uri(fig, plots / "04_relative_heatmap.png")
    return uris


def build_html(data: dict[str, Any], uris: dict[str, str]) -> str:
    prompts = data["prompts"]
    prompt_cards = "\n".join(
        f'''<article class="prompt-card"><header><b>Nova {p["number"]}</b><span>такты {p["start_bar"]}–{p["end_bar"]}</span></header><p class="prompt">{html.escape(p["text"])}</p><button onclick="copyPrompt(this)">Copy</button></article>'''
        for p in prompts
    ) or '<p class="ok">Нет устойчивых относительных отклонений выше порога.</p>'
    seg_rows = []
    for seg in data["segments"]:
        seg_rows.append(f"<tr><td>{html.escape(seg['role'])}</td><td>{seg['start_bar']}–{seg['end_bar']}</td><td>{'поднять' if seg['direction']=='raise' else 'опустить'}</td><td>{seg['median_db']:+.2f}</td><td>{seg['peak_db']:+.2f}</td></tr>")
    role_cards = "".join(
        f'<div><b>{r}</b><span>{html.escape(CURRENT_TARGET[r])}</span><small>K/RMS correlation {data["metric_validation"][r]["k_vs_rms_correlation"]:.3f}</small></div>' for r in ROLE_ORDER
    )
    meta = data["bar_grid"]
    inputs = "".join(f"<li><code>{html.escape(v['wav']['path'])}</code><small>SHA-256 {v['wav']['sha256']}</small></li>" for v in data["inputs"]["stems"].values())
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spring Melody — динамика reference stems</title>
<style>
:root{{--bg:#0b1220;--panel:#111c34;--panel2:#17233d;--text:#e7edf7;--muted:#9fb0c7;--line:#2b3b59;--accent:#f2a541;--ok:#52c788}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#0b1220,#11182a);color:var(--text);font:16px/1.55 Inter,Segoe UI,Arial,sans-serif}}main{{max-width:1800px;margin:auto;padding:38px 48px 80px}}h1{{font-size:42px;margin:.1em 0}}h2{{margin-top:44px;border-bottom:1px solid var(--line);padding-bottom:10px}}p.lead{{font-size:20px;color:#cbd8ea;max-width:1100px}}.verdict{{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:14px;margin:28px 0}}.verdict div,.mapping div{{background:var(--panel);border:1px solid var(--line);padding:18px;border-radius:12px}}.verdict b{{display:block;font-size:25px;color:var(--accent)}}.verdict span,.mapping span,small{{display:block;color:var(--muted)}}.notice{{border-left:4px solid var(--accent);background:#151f35;padding:18px 22px;border-radius:0 10px 10px 0;max-width:1300px}}.mapping{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}figure{{margin:22px 0;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}}figure img{{display:block;width:100%;height:auto}}figcaption{{color:var(--muted);margin-top:9px}}.prompts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.prompt-card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;position:relative}}.prompt-card header{{display:flex;justify-content:space-between;color:var(--accent)}}.prompt-card button{{background:var(--accent);border:0;border-radius:7px;padding:8px 18px;font-weight:700;cursor:pointer}}table{{width:100%;border-collapse:collapse;background:var(--panel)}}th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left}}code{{color:#b8d7ff;word-break:break-all}}details{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:14px 0}}.ok{{color:var(--ok)}}@media(max-width:1000px){{main{{padding:24px}}.verdict,.mapping,.prompts{{grid-template-columns:1fr}}}}
</style></head><body><main>
<p class="lead">REFERENCE MIX ANALYSIS / 2026-08-06</p><h1>Spring Melody: потактовая динамика инструментов</h1>
<p class="lead">Отчёт восстанавливает слышимую относительную иерархию из шести готовых stems. Он намеренно не копирует каждое колебание: Nova получает только устойчивые отклонения выше порога.</p>
<div class="verdict"><div><b>{meta['full_bars']}</b><span>полных тактов 4/4</span></div><div><b>{data['thresholds']['main_db']:.1f} dB</b><span>основной порог</span></div><div><b>{len(data['segments'])}</b><span>отобранных диапазонов</span></div><div><b>{len(prompts)}</b><span>сообщений Nova</span></div></div>
<div class="notice"><b>Guitar — один merged stem.</b> Rhythm и solo в reference неразделимы, поэтому любая рекомендация меняет их одновременно. Более точная внутренняя динамика остаётся для ручной правки на слух.</div>
<h2>Mapping на текущий Logic/Cryo проект</h2><div class="mapping">{role_cards}</div>
<h2>1. Что реально происходит внутри stems</h2><figure><img src="{uris['within']}" alt="Внутренняя динамика"><figcaption>Каждая линия центрирована по собственной активной медиане. Это ещё не инструкция для фейдера: здесь смешаны общая энергия секции и смена относительной роли.</figcaption></figure>
<h2>2. Общая динамика ансамбля</h2><figure><img src="{uris['common']}" alt="Common mode"><figcaption>Эта кривая удалена перед поиском Nova-команд. Она описывает общий подъём/спад песни, который не надо дублировать одинаковой automation на каждом stem.</figcaption></figure>
<h2>3. Относительная роль каждого инструмента</h2><figure><img src="{uris['relative']}" alt="Relative deviations"><figcaption>Пунктир — порог ±{data['thresholds']['main_db']:.1f} dB. Серые зоны оставлены без инструкций.</figcaption></figure>
<figure><img src="{uris['heatmap']}" alt="Heatmap"><figcaption>Heatmap помогает увидеть передачу фокуса между инструментами по тактам.</figcaption></figure>
<h2>4. Минимальный набор сообщений Nova</h2><div class="prompts">{prompt_cards}</div>
<h2>5. Отобранные диапазоны</h2><table><thead><tr><th>Инструмент</th><th>Такты</th><th>Действие</th><th>Медиана, dB</th><th>Пик, dB</th></tr></thead><tbody>{''.join(seg_rows)}</tbody></table>
<details><summary>Метод</summary><p>Границы тактов вычислены из общей reference MIDI tempo map: {meta['tempo_event_count']} tempo-события, {meta['tempo_bpm_min']:.2f}–{meta['tempo_bpm_max']:.2f} BPM. Для каждого полного такта измерена ungated K-weighted mean-square энергия, включая паузы, и независимо обычный RMS. Затем из каждой stem-кривой вычтена её активная медиана, а из каждого такта — robust common-mode активных stems. Median filter шириной три такта подавляет одиночные выбросы. Диапазон сохраняется при двух тактах выше порога; одиночный такт — только от {data['thresholds']['single_bar_db']:.1f} dB. Корреляция K-weighted и RMS кривых показана в mapping: это проверка, что диапазоны не возникли только из perceptual weighting.</p></details>
<details><summary>Ограничения</summary><p>Мы восстанавливаем слышимый результат, но не можем отделить исходный фейдер от исполнения, компрессии и тембра. Это не мешает перенести полезную макродинамику. Old Vocals задаёт динамическую роль текущей Voice/скрипки, но не её тембр. Synth и Keyboard переносятся только как совместные пары.</p></details>
<details><summary>Входы и provenance</summary><ul>{inputs}</ul><p>Master/stem consistency: correlation {data['master_validation']['correlation']:.4f}, residual after optimal scalar gain {data['master_validation']['residual_relative_db']:.2f} dB relative to master.</p></details>
<script>function copyPrompt(b){{const t=b.parentElement.querySelector('.prompt').innerText;const done=()=>{{b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1300)}};if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(t).then(done);else{{const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();done()}}}}</script>
</main></body></html>'''


def validate_master(master_path: Path, mix_sum: np.ndarray) -> dict[str, float]:
    master, sr = sf.read(master_path, dtype="float32", always_2d=True)
    if master.shape != mix_sum.shape:
        raise RuntimeError(f"Master/stem shape mismatch: {master.shape} vs {mix_sum.shape}")
    # Accumulate in chunks: avoid several additional 230+ MB float64 arrays.
    sums = {"x": 0.0, "y": 0.0, "xx": 0.0, "yy": 0.0, "xy": 0.0, "n": 0}
    chunk = 1_000_000
    for start in range(0, len(master), chunk):
        x = mix_sum[start:start + chunk].astype(np.float64, copy=False).reshape(-1)
        y = master[start:start + chunk].astype(np.float64, copy=False).reshape(-1)
        sums["x"] += float(np.sum(x)); sums["y"] += float(np.sum(y))
        sums["xx"] += float(np.dot(x, x)); sums["yy"] += float(np.dot(y, y)); sums["xy"] += float(np.dot(x, y))
        sums["n"] += len(x)
    gain = sums["xy"] / max(sums["xx"], EPS)
    residual_square_sum = sums["yy"] - 2.0 * gain * sums["xy"] + gain * gain * sums["xx"]
    residual_relative_db = float(db20(math.sqrt(max(residual_square_sum, EPS) / max(sums["yy"], EPS))))
    cov = sums["xy"] - sums["x"] * sums["y"] / sums["n"]
    var_x = sums["xx"] - sums["x"] ** 2 / sums["n"]
    var_y = sums["yy"] - sums["y"] ** 2 / sums["n"]
    corr = float(cov / math.sqrt(max(var_x * var_y, EPS)))
    return {"samplerate": sr, "optimal_sum_gain": gain, "correlation": corr, "residual_relative_db": residual_relative_db}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold-db", type=float, default=1.5)
    parser.add_argument("--single-bar-threshold-db", type=float, default=2.5)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    master, stems = discover_inputs(args.project_root)
    master_info = sf.info(master)
    boundaries, bar_meta = build_bar_grid(stems, master_info.duration)
    input_meta: dict[str, Any] = {
        "master": {"path": str(master), "sha256": sha256(master)},
        "stems": {},
    }
    raw: dict[str, dict[str, Any]] = {}
    mix_sum: np.ndarray | None = None
    for role in ROLE_ORDER:
        paths = stems[role]
        print(f"Analyzing {role}: {paths['wav'].name}", flush=True)
        metrics, audio = analyze_audio(paths["wav"], boundaries)
        raw[role] = metrics
        mix_sum = audio.copy() if mix_sum is None else mix_sum + audio
        input_meta["stems"][role] = {
            "source_role": paths["source_role"],
            "wav": {"path": str(paths["wav"]), "sha256": sha256(paths["wav"])},
            "midi": {"path": str(paths["midi"]), "sha256": sha256(paths["midi"])},
            "current_target": CURRENT_TARGET[role],
        }
    assert mix_sum is not None
    master_validation = validate_master(master, mix_sum)
    del mix_sum

    processed: dict[str, Any] = {"bar_count": bar_meta["full_bars"], "roles": {}}
    smoothed_by_role: dict[str, np.ndarray] = {}
    active_by_role: dict[str, np.ndarray] = {}
    for role in ROLE_ORDER:
        loud = np.array(raw[role]["k_loudness_db"], dtype=float)
        p90 = float(np.percentile(loud, 90))
        active_threshold = max(-58.0, p90 - 30.0)
        active = loud >= active_threshold
        if not np.any(active):
            raise RuntimeError(f"No active bars for {role}")
        median_active = float(np.median(loud[active]))
        within = loud - median_active
        smoothed = rolling_median_active(within, active)
        active_by_role[role] = active
        smoothed_by_role[role] = smoothed
        processed["roles"][role] = {
            "active_threshold_db": active_threshold,
            "active_median_db": median_active,
            "active": active.tolist(),
            "within_stem_db": within.tolist(),
            "smoothed_within_db": [None if not np.isfinite(v) else float(v) for v in smoothed],
        }

    common = []
    for idx in range(bar_meta["full_bars"]):
        vals = [smoothed_by_role[r][idx] for r in ROLE_ORDER if active_by_role[r][idx] and np.isfinite(smoothed_by_role[r][idx])]
        common.append(float(np.median(vals)) if vals else 0.0)
    common_arr = np.array(common)
    processed["common_mode_db"] = common

    # Independent unweighted RMS path, using the same activity mask and
    # normalization rules. It validates that K-weighting alone did not create
    # the selected role changes.
    rms_smoothed_by_role: dict[str, np.ndarray] = {}
    for role in ROLE_ORDER:
        active = active_by_role[role]
        rms = np.array(raw[role]["rms_dbfs"], dtype=float)
        rms_within = rms - float(np.median(rms[active]))
        rms_smoothed_by_role[role] = rolling_median_active(rms_within, active)
    rms_common = []
    for idx in range(bar_meta["full_bars"]):
        vals = [rms_smoothed_by_role[r][idx] for r in ROLE_ORDER if active_by_role[r][idx] and np.isfinite(rms_smoothed_by_role[r][idx])]
        rms_common.append(float(np.median(vals)) if vals else 0.0)
    rms_common_arr = np.array(rms_common)
    processed["rms_common_mode_db"] = rms_common

    metric_validation: dict[str, Any] = {}
    segments: list[Segment] = []
    for role in ROLE_ORDER:
        rel = smoothed_by_role[role] - common_arr
        rms_rel = rms_smoothed_by_role[role] - rms_common_arr
        rel[~active_by_role[role]] = np.nan
        rms_rel[~active_by_role[role]] = np.nan
        finite = np.isfinite(rel) & np.isfinite(rms_rel)
        correlation = float(np.corrcoef(rel[finite], rms_rel[finite])[0, 1]) if np.sum(finite) >= 2 else 1.0
        processed["roles"][role]["relative_db"] = [None if not np.isfinite(v) else float(v) for v in rel]
        processed["roles"][role]["rms_relative_db"] = [None if not np.isfinite(v) else float(v) for v in rms_rel]
        metric_validation[role] = {"k_vs_rms_correlation": correlation, "compared_bars": int(np.sum(finite))}
        role_segments = detect_segments(role, rel, active_by_role[role], args.threshold_db, args.single_bar_threshold_db)
        segments.extend(role_segments)
    segments.sort(key=lambda s: (s.start_bar, s.end_bar, ROLE_ORDER.index(s.role)))
    prompts = group_prompts(segments)

    result = {
        "schema_version": 1,
        "purpose": "audible relative reference-stem dynamics, not hidden DAW automation recovery",
        "inputs": input_meta,
        "bar_grid": bar_meta,
        "thresholds": {"main_db": args.threshold_db, "single_bar_db": args.single_bar_threshold_db, "smoothing_bars": 3},
        "raw_metrics": raw,
        "processed": processed,
        "metric_validation": metric_validation,
        "segments": [s.__dict__ for s in segments],
        "prompts": prompts,
        "master_validation": master_validation,
    }
    json_path = args.output / "reference_dynamics.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    csv_path = args.output / "reference_dynamics.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["bar", "start_s", "end_s", "role", "active", "k_loudness_db", "rms_dbfs", "within_stem_db", "smoothed_within_db", "common_mode_db", "relative_db"])
        for idx in range(bar_meta["full_bars"]):
            for role in ROLE_ORDER:
                p = processed["roles"][role]
                writer.writerow([
                    idx + 1, boundaries[idx], boundaries[idx + 1], role, p["active"][idx], raw[role]["k_loudness_db"][idx], raw[role]["rms_dbfs"][idx],
                    p["within_stem_db"][idx], p["smoothed_within_db"][idx], common[idx], p["relative_db"][idx],
                ])

    prompt_path = args.output / "reference_dynamics_prompts.txt"
    prompt_path.write_text("\n\n".join(f"NOVA {p['number']} — BARS {p['start_bar']}–{p['end_bar']}\n{p['text']}" for p in prompts) + "\n", encoding="utf-8")

    uris = make_plots(args.output, processed, args.threshold_db)
    html_path = args.output / "reference_dynamics.html"
    html_path.write_text(build_html(result, uris), encoding="utf-8")

    manifest = {
        "command": " ".join(map(str, sys.argv)),
        "python": sys.version,
        "platform": platform.platform(),
        "versions": {"numpy": np.__version__, "soundfile": sf.__version__, "matplotlib": matplotlib.__version__, "mido": getattr(mido, "__version__", "unknown")},
        "outputs": {},
    }
    for path in sorted(args.output.rglob("*")):
        if path.is_file() and path.name != "run_manifest.json":
            manifest["outputs"][str(path.relative_to(args.output))] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    (args.output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"bars": bar_meta["full_bars"], "segments": len(segments), "prompts": len(prompts), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
