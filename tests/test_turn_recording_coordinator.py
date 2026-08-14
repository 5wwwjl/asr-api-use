import sys
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from turn_recording_coordinator import RecordingChunk, TurnRecordingCoordinator


def chunk(segment_id, pcm, speaker="caller", call_id="caller-call", start=0, end=1000):
    return RecordingChunk(
        call_id=call_id,
        callfrom="8001",
        callto="8002",
        speaker=speaker,
        direction="inbound" if speaker == "caller" else "outbound",
        segment_id=segment_id,
        pcm=pcm,
        start_time_ms=start,
        end_time_ms=end,
    )


def test_multiple_vad_segments_complete_as_separate_turns_when_each_vad_ends():
    coordinator = TurnRecordingCoordinator()

    first = coordinator.append_vad_segment(chunk("caller-0001", b"aaa", start=0, end=1000))
    second = coordinator.append_vad_segment(chunk("caller-0002", b"bbb", start=1200, end=2200))

    assert len(first) == 1
    assert first[0].call_id == "caller-call"
    assert first[0].speaker == "caller"
    assert first[0].segment_ids == ("caller-0001",)
    assert first[0].pcm == b"aaa"
    assert len(second) == 1
    assert second[0].segment_ids == ("caller-0002",)
    assert second[0].pcm == b"bbb"
    assert coordinator.mark_effective_speech(
        call_id="agent-call",
        callfrom="8001",
        callto="8002",
        speaker="agent",
    ) == []


def test_peer_speaking_does_not_complete_until_current_side_vad_ends():
    coordinator = TurnRecordingCoordinator()
    coordinator.mark_speech_started(
        call_id="caller-call",
        callfrom="8001",
        callto="8002",
        speaker="caller",
    )

    assert coordinator.mark_effective_speech(
        call_id="agent-call",
        callfrom="8001",
        callto="8002",
        speaker="agent",
    ) == []

    completed = coordinator.append_vad_segment(chunk("caller-0001", b"aaa", start=0, end=1000))

    assert len(completed) == 1
    assert completed[0].pcm == b"aaa"


def test_vad_end_completes_turn_even_when_peer_has_not_spoken():
    coordinator = TurnRecordingCoordinator()

    completed = coordinator.append_vad_segment(chunk(
        "agent-0001",
        b"agent-audio",
        speaker="agent",
        call_id="agent-call",
        start=0,
        end=1200,
    ))

    assert len(completed) == 1
    assert completed[0].call_id == "agent-call"
    assert completed[0].speaker == "agent"
    assert completed[0].segment_ids == ("agent-0001",)
    assert completed[0].pcm == b"agent-audio"


def test_finalize_call_flushes_unended_call_audio_only():
    coordinator = TurnRecordingCoordinator()
    assert len(coordinator.append_vad_segment(chunk("caller-0001", b"aaa", start=0, end=1000))) == 1
    coordinator.mark_speech_started(
        call_id="agent-call",
        callfrom="8001",
        callto="8002",
        speaker="agent",
    )

    completed = coordinator.finalize_call("caller-call")

    assert completed == []
    assert coordinator.finalize_call("agent-call") == []


def test_completed_turn_clears_state_before_next_pause():
    coordinator = TurnRecordingCoordinator()

    first = coordinator.append_vad_segment(chunk("caller-0001", b"aaa", start=0, end=1000))
    second = coordinator.append_vad_segment(chunk("caller-0002", b"bbb", start=1200, end=2200))

    assert len(first) == 1
    assert first[0].segment_ids == ("caller-0001",)
    assert len(second) == 1
    assert second[0].segment_ids == ("caller-0002",)
    assert coordinator.mark_effective_speech(
        call_id="agent-call",
        callfrom="8001",
        callto="8002",
        speaker="agent",
    ) == []
