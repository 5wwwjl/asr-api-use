import sys
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from vad_engine import VADEngine, VADConfig


class AlwaysSpeechVad:
    def is_speech(self, frame, sample_rate):
        return True


def test_energy_gate_treats_low_volume_webrtc_speech_as_silence():
    vad = VADEngine(VADConfig(
        frame_ms=20,
        speech_confirm_frames=1,
        silence_confirm_ms=100,
        min_speech_ms=20,
        energy_silence_db=-50.0,
    ))
    vad._vad = AlwaysSpeechVad()
    loud = (12000).to_bytes(2, "little", signed=True) * 320
    silent = b"\0" * 640

    started = vad.feed(loud)
    assert started.speech_started
    assert started.is_speaking

    result = None
    for _ in range(5):
        result = vad.feed(silent)

    assert result is not None
    assert result.speech_ended
    assert result.is_speaking is False
    assert result.silence_duration_ms == 100


class SampleEnergyVad:
    def is_speech(self, frame, sample_rate):
        return any(frame)


def test_pcm_remainder_is_preserved_across_feed_calls():
    vad = VADEngine(VADConfig(
        frame_ms=20,
        speech_confirm_frames=1,
        silence_confirm_ms=100,
        min_speech_ms=20,
        pre_speech_ms=20,
        energy_silence_db=-55.0,
    ))
    vad._vad = SampleEnergyVad()
    pcm = (12000).to_bytes(2, "little", signed=True) * 640

    first = vad.feed(pcm[:960])
    second = vad.feed(pcm[960:])
    buffered = vad.flush()

    assert first.speech_started
    assert second.is_speaking
    assert buffered == pcm


def test_pre_speech_silence_does_not_count_toward_minimum_speech():
    vad = VADEngine(VADConfig(
        frame_ms=20,
        speech_confirm_frames=3,
        silence_confirm_ms=100,
        min_speech_ms=200,
        pre_speech_ms=300,
        energy_silence_db=-55.0,
    ))
    loud = (12000).to_bytes(2, "little", signed=True) * 320
    silent = b"\0" * 640
    decisions = iter([False] * 12 + [True] * 3 + [False] * 5)
    vad._is_speech_frame = lambda frame: next(decisions)

    result = None
    for frame in [silent] * 12 + [loud] * 3 + [silent] * 5:
        result = vad.feed(frame)

    assert result is not None
    assert result.is_speaking is False
    assert result.speech_ended is False
    assert result.audio_segment is None
