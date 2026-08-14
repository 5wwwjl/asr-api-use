"""Turn-level audio recording coordination across the two ASR call streams."""

from __future__ import annotations

from dataclasses import dataclass, field


def pair_key(callfrom: str, callto: str, call_id: str = "") -> str:
    a = (callfrom or "?").strip() or "?"
    b = (callto or "?").strip() or "?"
    if a in {"?", "micro"} and b in {"?", "micro"}:
        return call_id or "unknown"
    return "::".join(sorted([a, b]))


def speaker_key(speaker: str) -> str:
    normalized = (speaker or "").strip().lower()
    return "agent" if normalized in {"agent", "system"} else "caller"


def peer_speaker_key(speaker: str) -> str:
    return "caller" if speaker_key(speaker) == "agent" else "agent"


@dataclass(frozen=True)
class RecordingChunk:
    call_id: str
    callfrom: str
    callto: str
    speaker: str
    direction: str
    segment_id: str | None
    pcm: bytes
    start_time_ms: int = 0
    end_time_ms: int = 0


@dataclass(frozen=True)
class CompletedTurnRecording:
    call_id: str
    callfrom: str
    callto: str
    speaker: str
    direction: str
    segment_ids: tuple[str, ...]
    pcm: bytes
    start_time_ms: int
    end_time_ms: int

    @property
    def segment_id(self) -> str | None:
        return self.segment_ids[0] if self.segment_ids else None


@dataclass
class _PendingRecording:
    call_id: str = ""
    callfrom: str = ""
    callto: str = ""
    speaker: str = ""
    direction: str = ""
    parts: list[bytes] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    start_time_ms: int | None = None
    end_time_ms: int = 0
    vad_ended: bool = False
    peer_effective_speech_seen: bool = False

    def has_audio(self) -> bool:
        return any(self.parts)


class TurnRecordingCoordinator:
    """Accumulates VAD chunks and emits one WAV payload per conversational turn."""

    def __init__(self):
        self._pairs: dict[str, dict[str, _PendingRecording]] = {}

    def mark_speech_started(self, *, call_id: str, callfrom: str, callto: str, speaker: str) -> list[CompletedTurnRecording]:
        state = self._state(call_id, callfrom, callto, speaker)
        state.vad_ended = False
        return []

    def append_vad_segment(self, chunk: RecordingChunk) -> list[CompletedTurnRecording]:
        if not chunk.pcm:
            return []
        state = self._state(chunk.call_id, chunk.callfrom, chunk.callto, chunk.speaker)
        state.call_id = chunk.call_id or state.call_id
        state.callfrom = chunk.callfrom or state.callfrom
        state.callto = chunk.callto or state.callto
        state.speaker = chunk.speaker or state.speaker
        state.direction = chunk.direction or state.direction
        if state.start_time_ms is None:
            state.start_time_ms = max(0, int(chunk.start_time_ms or 0))
        state.end_time_ms = max(state.end_time_ms, int(chunk.end_time_ms or 0))
        if chunk.segment_id and chunk.segment_id not in state.segment_ids:
            state.segment_ids.append(chunk.segment_id)
        state.parts.append(chunk.pcm)
        state.vad_ended = True
        return self._complete_if_ready(chunk.callfrom, chunk.callto, chunk.call_id, chunk.speaker)

    def mark_effective_speech(self, *, call_id: str, callfrom: str, callto: str, speaker: str) -> list[CompletedTurnRecording]:
        key = pair_key(callfrom, callto, call_id)
        side = peer_speaker_key(speaker)
        state = self._pairs.get(key, {}).get(side)
        if not state:
            return []
        state.peer_effective_speech_seen = True
        return self._complete_if_ready(callfrom, callto, call_id, side)

    def finalize_call(self, call_id: str) -> list[CompletedTurnRecording]:
        completed: list[CompletedTurnRecording] = []
        for key, sides in list(self._pairs.items()):
            for side, state in list(sides.items()):
                if state.call_id == call_id and state.has_audio():
                    completed.append(self._complete_state(state))
                    del sides[side]
            if not sides:
                del self._pairs[key]
        return completed

    def _state(self, call_id: str, callfrom: str, callto: str, speaker: str) -> _PendingRecording:
        key = pair_key(callfrom, callto, call_id)
        side = speaker_key(speaker)
        pair = self._pairs.setdefault(key, {})
        if side not in pair:
            pair[side] = _PendingRecording(
                call_id=call_id or "",
                callfrom=callfrom or "",
                callto=callto or "",
                speaker=speaker or side,
                direction="",
            )
        return pair[side]

    def _complete_if_ready(self, callfrom: str, callto: str, call_id: str, speaker: str) -> list[CompletedTurnRecording]:
        key = pair_key(callfrom, callto, call_id)
        side = speaker_key(speaker)
        state = self._pairs.get(key, {}).get(side)
        if not state or not state.has_audio():
            return []
        if not state.vad_ended:
            return []
        completed = self._complete_state(state)
        del self._pairs[key][side]
        if not self._pairs[key]:
            del self._pairs[key]
        return [completed]

    def _complete_state(self, state: _PendingRecording) -> CompletedTurnRecording:
        return CompletedTurnRecording(
            call_id=state.call_id,
            callfrom=state.callfrom,
            callto=state.callto,
            speaker=state.speaker,
            direction=state.direction,
            segment_ids=tuple(state.segment_ids),
            pcm=b"".join(state.parts),
            start_time_ms=state.start_time_ms if state.start_time_ms is not None else 0,
            end_time_ms=state.end_time_ms,
        )
