"""Audio evidence extraction — bounded Phase 2 scope.

Validates timing from a mix or stem when present. This is not transcription:
no labels, no models, no GPU.

Resource bounds:

* reads at most `max_seconds` of audio, never the whole asset;
* works from a file already streamed to bounded pod storage, so the decoder
  never sees an unbounded in-memory buffer;
* downsamples to a fixed analysis rate before any spectral work;
* truncates persisted onset/downbeat lists to documented maxima.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

from .models import AudioEvidence, AudioMeasureDiagnostic, Warning, WarningCode

logger = logging.getLogger(__name__)

DEFAULT_MAX_READ_SECONDS = 600.0
ANALYSIS_SR = 22050
ONSET_HOP_LENGTH = 512
ONSET_FRAME_LENGTH = 2048
DEFAULT_MAX_ONSETS = 512
DEFAULT_MAX_DOWNBEATS = 256


def _compute_onset_envelope(
    samples: np.ndarray,
    hop_length: int = ONSET_HOP_LENGTH,
) -> np.ndarray:
    """Spectral-flux onset envelope."""
    import numpy as np

    frame_length = ONSET_FRAME_LENGTH
    if len(samples) < frame_length:
        return np.array([0.0])
    n_frames = 1 + (len(samples) - frame_length) // hop_length
    if n_frames <= 1:
        return np.array([0.0])

    window = np.hanning(frame_length)
    envelope = np.zeros(n_frames)

    prev_spectrum = None
    for i in range(n_frames):
        start = i * hop_length
        frame = samples[start:start + frame_length] * window
        spectrum = np.abs(np.fft.rfft(frame))
        if prev_spectrum is not None:
            envelope[i] = float(np.sum(np.maximum(0.0, spectrum - prev_spectrum)))
        prev_spectrum = spectrum

    return envelope


def _pick_peaks(
    envelope: np.ndarray,
    sr: int,
    hop_length: int,
    threshold_ratio: float = 0.3,
) -> list[float]:
    """Peak-picking on the onset envelope; returns times in seconds."""
    import numpy as np

    if len(envelope) < 3:
        return []
    peak_max = float(np.max(envelope))
    if peak_max <= 0:
        return []
    threshold = threshold_ratio * peak_max

    peaks: list[float] = []
    for i in range(1, len(envelope) - 1):
        if (
            envelope[i] > threshold
            and envelope[i] > envelope[i - 1]
            and envelope[i] >= envelope[i + 1]
        ):
            peaks.append(round(i * hop_length / sr, 6))
    return peaks


def _estimate_downbeats(
    onset_times: list[float],
    estimated_bpm: float | None = None,
    max_candidates: int = DEFAULT_MAX_DOWNBEATS,
) -> list[float]:
    """Rough bar-boundary candidates from inter-onset intervals.

    Used only for phase/offset validation against the MIDI-derived grid.
    """
    if len(onset_times) < 4:
        return onset_times[:max_candidates]

    import numpy as np

    intervals = np.diff(onset_times)
    if len(intervals) == 0:
        return []

    median_ioi = float(np.median(intervals))
    if median_ioi <= 0:
        return onset_times[:max_candidates]

    beat_period = (
        60.0 / estimated_bpm if estimated_bpm and estimated_bpm > 0 else median_ioi
    )
    bar_period = beat_period * 4

    candidates: list[float] = [onset_times[0]]
    last = onset_times[0]
    for t in onset_times[1:]:
        if t - last >= bar_period * 0.8:
            candidates.append(t)
            last = t
            if len(candidates) >= max_candidates:
                break
    return candidates


def _degraded(
    asset_link_id: str, sha256: str, role: str, code: WarningCode, message: str
) -> AudioEvidence:
    return AudioEvidence(
        asset_link_id=asset_link_id,
        sha256=sha256,
        role=role,
        warnings=[Warning(code=code, message=message)],
    )


def extract_audio_evidence(
    path: str,
    asset_link_id: str,
    sha256: str,
    role: str = "",
    max_seconds: float = DEFAULT_MAX_READ_SECONDS,
    max_onsets: int = DEFAULT_MAX_ONSETS,
    max_downbeats: int = DEFAULT_MAX_DOWNBEATS,
    estimated_bpm: float | None = None,
) -> AudioEvidence:
    """Extract bounded audio evidence from an already-downloaded WAV/FLAC file.

    Never raises for audio problems: a decode failure degrades to evidence
    carrying `audio_decode_failed`, because absent audio must reduce confidence
    rather than fail the analysis.
    """
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as e:  # pragma: no cover - dependency is pinned in the image
        return _degraded(
            asset_link_id,
            sha256,
            role,
            WarningCode.AUDIO_DECODE_FAILED,
            f"Audio dependencies not available: {e}",
        )

    warnings: list[Warning] = []
    try:
        info = sf.info(path)
        duration = float(info.duration)
        sr = int(info.samplerate)
        channels = int(info.channels)

        max_frames = int(max_seconds * sr)
        truncated = duration > max_seconds
        if truncated:
            warnings.append(
                Warning(
                    code=WarningCode.AUDIO_TRUNCATED,
                    message=(
                        f"Only the first {max_seconds:.0f}s of {duration:.1f}s were "
                        f"analysed (bounded read)"
                    ),
                )
            )

        data, file_sr = sf.read(path, frames=max_frames, dtype="float32")
        mono = np.mean(data, axis=1) if data.ndim > 1 else data
        decoded_seconds = float(len(mono) / file_sr) if file_sr else 0.0

        if file_sr != ANALYSIS_SR and len(mono) > 1:
            new_length = max(1, int(len(mono) * ANALYSIS_SR / file_sr))
            indices = np.linspace(0, len(mono) - 1, new_length)
            mono = np.interp(indices, np.arange(len(mono)), mono)
            effective_sr = ANALYSIS_SR
        else:
            effective_sr = file_sr

        envelope = _compute_onset_envelope(mono)
        onset_times = _pick_peaks(envelope, effective_sr, ONSET_HOP_LENGTH)
        downbeats = _estimate_downbeats(
            onset_times, estimated_bpm=estimated_bpm, max_candidates=max_downbeats
        )

        return AudioEvidence(
            asset_link_id=asset_link_id,
            sha256=sha256,
            role=role,
            duration_seconds=duration,
            decoded_seconds=decoded_seconds,
            sample_rate=sr,
            channels=channels,
            onset_count=len(onset_times),
            onset_times=onset_times[:max_onsets],
            downbeat_candidates=downbeats[:max_downbeats],
            truncated=truncated,
            warnings=warnings,
        )

    except (OSError, ValueError, RuntimeError, MemoryError) as e:
        logger.warning("Audio decode failed for %s: %s", asset_link_id, e)
        return _degraded(
            asset_link_id,
            sha256,
            role,
            WarningCode.AUDIO_DECODE_FAILED,
            f"Audio processing failed: {type(e).__name__}",
        )


def apply_audio_evidence_to_measures(
    source_measures: list,
    audio_evidence: list[AudioEvidence],
    window_seconds: float = 0.5,
) -> list:
    """Attach downbeat corroboration to each source measure.

    Each measure gets the proximity, in [0,1], of the nearest audio downbeat
    candidate to its own start. A measure with no candidate within the window
    keeps `None`, which the aligner reads as "no audio evidence" rather than
    "audio contradicts".
    """
    enriched = []
    for sm in source_measures:
        best: float | None = None
        diagnostics: list[AudioMeasureDiagnostic] = []
        for ev in audio_evidence:
            onset_count = sum(
                sm.seconds_start <= time < sm.seconds_end for time in ev.onset_times
            )
            candidate_count = sum(
                sm.seconds_start <= time < sm.seconds_end
                for time in ev.downbeat_candidates
            )
            nearest = (
                min(abs(time - sm.seconds_start) for time in ev.downbeat_candidates)
                if ev.downbeat_candidates else None
            )
            corroboration = None
            if nearest is not None and nearest <= window_seconds:
                corroboration = max(0.0, 1.0 - nearest / window_seconds)
                best = corroboration if best is None else max(best, corroboration)
            diagnostics.append(AudioMeasureDiagnostic(
                asset_link_id=ev.asset_link_id,
                role=ev.role,
                onset_count=onset_count,
                downbeat_candidate_count=candidate_count,
                nearest_downbeat_distance_seconds=(
                    round(nearest, 6) if nearest is not None else None
                ),
                corroboration=corroboration,
            ))
        enriched.append(sm.model_copy(update={
            "audio_downbeat_evidence": best,
            "audio_diagnostics": diagnostics,
        }))
    return enriched
