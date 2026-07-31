"""Audio evidence extraction — bounded Phase 2 scope.

Validates timing from mix/drums stem when present.
Uses soundfile for metadata and scipy for lightweight onset detection.
Does NOT perform transcription, labeling, or GPU processing.

Resource bounds:
- Reads at most MAX_READ_SECONDS of audio (default 600s)
- Downsamples to ANALYSIS_SR for processing
- Uses temporary files only inside bounded pod storage
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import numpy as np

from .models import Warning, WarningCode

MAX_READ_SECONDS = 600
ANALYSIS_SR = 22050
ONSET_HOP_LENGTH = 512
ONSET_FRAME_LENGTH = 2048


class AudioEvidence:
    """Lightweight audio evidence for phase/offset validation."""

    def __init__(
        self,
        duration_seconds: float,
        sample_rate: int,
        channels: int,
        onset_times: list[float],
        downbeat_candidates: list[float],
        warnings: list[Warning],
    ):
        self.duration_seconds = duration_seconds
        self.sample_rate = sample_rate
        self.channels = channels
        self.onset_times = onset_times
        self.downbeat_candidates = downbeat_candidates
        self.warnings = warnings


def _compute_onset_envelope(
    samples: "np.ndarray",
    sr: int,
    hop_length: int = ONSET_HOP_LENGTH,
) -> "np.ndarray":
    """Compute a spectral flux onset envelope."""
    import numpy as np

    frame_length = ONSET_FRAME_LENGTH
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
            diff = spectrum - prev_spectrum
            envelope[i] = np.sum(np.maximum(0, diff))

        prev_spectrum = spectrum

    return envelope


def _pick_peaks(
    envelope: "np.ndarray",
    sr: int,
    hop_length: int,
    threshold_ratio: float = 0.3,
) -> list[float]:
    """Simple peak-picking on onset envelope, returns times in seconds."""
    import numpy as np

    if len(envelope) < 3:
        return []

    threshold = threshold_ratio * np.max(envelope)
    peaks: list[float] = []

    for i in range(1, len(envelope) - 1):
        if (
            envelope[i] > threshold
            and envelope[i] > envelope[i - 1]
            and envelope[i] >= envelope[i + 1]
        ):
            peaks.append(i * hop_length / sr)

    return peaks


def _estimate_downbeats(
    onset_times: list[float],
    estimated_bpm: Optional[float] = None,
    max_candidates: int = 200,
) -> list[float]:
    """Estimate downbeat candidates from onset times.

    Uses inter-onset intervals to find periodicity suggesting bar boundaries.
    This is a rough estimate for phase validation, not transcription.
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

    if estimated_bpm and estimated_bpm > 0:
        beat_period = 60.0 / estimated_bpm
    else:
        beat_period = median_ioi

    bar_period = beat_period * 4

    candidates: list[float] = []
    if onset_times:
        candidates.append(onset_times[0])
        last = onset_times[0]
        for t in onset_times[1:]:
            if t - last >= bar_period * 0.8:
                candidates.append(t)
                last = t
                if len(candidates) >= max_candidates:
                    break

    return candidates


def extract_audio_evidence(
    audio_bytes: bytes,
    max_seconds: float = MAX_READ_SECONDS,
) -> AudioEvidence:
    """Extract audio evidence from WAV/FLAC bytes.

    Returns AudioEvidence with onset times and downbeat candidates.
    On failure, returns degraded evidence with warnings.
    """
    warnings: list[Warning] = []

    try:
        import soundfile as sf
        import numpy as np
    except ImportError as e:
        return AudioEvidence(
            duration_seconds=0.0,
            sample_rate=0,
            channels=0,
            onset_times=[],
            downbeat_candidates=[],
            warnings=[Warning(
                code=WarningCode.AUDIO_DECODE_FAILED,
                message=f"Audio dependencies not available: {e}",
            )],
        )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False, dir=os.environ.get("TMPDIR", "/tmp")
        ) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        info = sf.info(tmp_path)
        duration = info.duration
        sr = info.samplerate
        channels = info.channels

        max_frames = int(max_seconds * sr)
        data, file_sr = sf.read(tmp_path, frames=max_frames, dtype="float32")

        if data.ndim > 1:
            mono = np.mean(data, axis=1)
        else:
            mono = data

        if file_sr != ANALYSIS_SR:
            ratio = ANALYSIS_SR / file_sr
            new_length = int(len(mono) * ratio)
            indices = np.linspace(0, len(mono) - 1, new_length)
            mono = np.interp(indices, np.arange(len(mono)), mono)
            effective_sr = ANALYSIS_SR
        else:
            effective_sr = file_sr

        envelope = _compute_onset_envelope(mono, effective_sr)
        onset_times = _pick_peaks(envelope, effective_sr, ONSET_HOP_LENGTH)
        downbeat_candidates = _estimate_downbeats(onset_times)

        return AudioEvidence(
            duration_seconds=duration,
            sample_rate=sr,
            channels=channels,
            onset_times=onset_times,
            downbeat_candidates=downbeat_candidates,
            warnings=warnings,
        )

    except Exception as e:
        warnings.append(Warning(
            code=WarningCode.AUDIO_DECODE_FAILED,
            message=f"Audio processing failed: {e}",
        ))
        return AudioEvidence(
            duration_seconds=0.0,
            sample_rate=0,
            channels=0,
            onset_times=[],
            downbeat_candidates=[],
            warnings=warnings,
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
