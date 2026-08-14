"""Lightweight PCM diagnostics and bounded gain before ASR/VAD."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
import sys


@dataclass(frozen=True)
class AudioProcessResult:
    pcm: bytes
    raw_db: float
    processed_db: float
    gain_db: float
    audio_level: int
    diagnostics: list[str]


@dataclass
class _StreamState:
    last_seq: int | None = None
    last_end_time_ms: int | None = None


class AudioPreprocessor:
    """Apply conservative per-stream gain and report frame continuity issues."""

    def __init__(
        self,
        *,
        target_db: float = -24.0,
        gain_below_db: float = -30.0,
        gain_above_db: float = -52.0,
        max_gain_db: float = 12.0,
        peak_limit_db: float = -3.0,
        expected_frame_ms: int = 100,
        max_gap_ms: int = 180,
    ):
        self.target_db = target_db
        self.gain_below_db = gain_below_db
        self.gain_above_db = gain_above_db
        self.max_gain_db = max_gain_db
        self.peak_limit_db = peak_limit_db
        self.expected_frame_ms = expected_frame_ms
        self.max_gap_ms = max_gap_ms
        self._streams: dict[tuple[str, str], _StreamState] = {}

    def process(
        self,
        call_id: str,
        speaker: str,
        pcm: bytes,
        *,
        seq: int | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> AudioProcessResult:
        diagnostics = self._diagnostics(call_id, speaker, seq, start_time_ms, end_time_ms)
        samples = self._samples(pcm)
        raw_db, peak = self._measure(samples)
        gain = self._gain(raw_db, peak)

        if gain <= 1.0:
            return AudioProcessResult(
                pcm=pcm,
                raw_db=raw_db,
                processed_db=raw_db,
                gain_db=0.0,
                audio_level=self._level(raw_db),
                diagnostics=diagnostics,
            )

        for idx, sample in enumerate(samples):
            samples[idx] = max(-32768, min(32767, round(sample * gain)))
        if sys.byteorder != "little":
            samples.byteswap()
        processed_pcm = samples.tobytes()
        processed_db, _ = self._measure(self._samples(processed_pcm))
        return AudioProcessResult(
            pcm=processed_pcm,
            raw_db=raw_db,
            processed_db=processed_db,
            gain_db=round(20 * math.log10(gain), 1),
            audio_level=self._level(processed_db),
            diagnostics=diagnostics,
        )

    def _diagnostics(
        self,
        call_id: str,
        speaker: str,
        seq: int | None,
        start_time_ms: int | None,
        end_time_ms: int | None,
    ) -> list[str]:
        key = (call_id or "?", speaker or "unknown")
        state = self._streams.setdefault(key, _StreamState())
        diagnostics: list[str] = []

        if seq is not None and state.last_seq is not None and seq != state.last_seq + 1:
            diagnostics.append(f"seq gap: expected {state.last_seq + 1}, got {seq}")
        if (
            start_time_ms is not None
            and state.last_end_time_ms is not None
            and start_time_ms - state.last_end_time_ms > self.max_gap_ms
        ):
            gap = start_time_ms - state.last_end_time_ms
            diagnostics.append(f"timestamp gap: {gap}ms")

        if seq is not None:
            state.last_seq = seq
        if end_time_ms is not None:
            state.last_end_time_ms = end_time_ms
        return diagnostics

    def _gain(self, raw_db: float, peak: int) -> float:
        if peak <= 0:
            return 1.0
        if raw_db < self.gain_above_db or raw_db >= self.gain_below_db:
            return 1.0

        target_gain = 10 ** ((self.target_db - raw_db) / 20)
        peak_limit = 32767 * (10 ** (self.peak_limit_db / 20))
        peak_gain = peak_limit / peak
        max_gain = 10 ** (self.max_gain_db / 20)
        return min(target_gain, peak_gain, max_gain)

    def _samples(self, pcm: bytes) -> array:
        samples = array("h")
        if len(pcm) < 2:
            return samples
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if sys.byteorder != "little":
            samples.byteswap()
        return samples

    def _measure(self, samples: array) -> tuple[float, int]:
        if not samples:
            return -60.0, 0
        total = sum(sample * sample for sample in samples)
        peak = max(abs(sample) for sample in samples)
        if total <= 0 or peak <= 0:
            return -60.0, peak
        rms = math.sqrt(total / len(samples))
        return max(-60.0, min(0.0, 20 * math.log10(rms / 32768.0))), peak

    def _level(self, db: float) -> int:
        return max(0, min(100, int(round((db + 60.0) * 100.0 / 60.0))))
