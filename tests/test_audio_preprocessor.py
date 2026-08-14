import sys
from array import array
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from audio_preprocessor import AudioPreprocessor  # noqa: E402


def pcm_with_sample(sample: int, samples: int = 1600) -> bytes:
    return int(sample).to_bytes(2, "little", signed=True) * samples


def max_abs_sample(pcm: bytes) -> int:
    values = array("h")
    values.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return max(abs(v) for v in values) if values else 0


def test_quiet_speech_is_amplified_with_safe_bound():
    pre = AudioPreprocessor(target_db=-24.0, max_gain_db=12.0)
    quiet = pcm_with_sample(300)

    result = pre.process("call-1", "caller", quiet, seq=1, start_time_ms=0, end_time_ms=100)

    assert result.pcm != quiet
    assert result.gain_db > 0
    assert result.gain_db <= 12.0
    assert max_abs_sample(result.pcm) > max_abs_sample(quiet)
    assert max_abs_sample(result.pcm) < 32767


def test_silence_and_normal_volume_are_not_changed():
    pre = AudioPreprocessor()
    silence = pcm_with_sample(0)
    normal = pcm_with_sample(6000)

    silence_result = pre.process("call-1", "caller", silence, seq=1, start_time_ms=0, end_time_ms=100)
    normal_result = pre.process("call-1", "caller", normal, seq=2, start_time_ms=100, end_time_ms=200)

    assert silence_result.pcm == silence
    assert silence_result.gain_db == 0
    assert normal_result.pcm == normal
    assert normal_result.gain_db == 0


def test_diagnostics_report_seq_and_timestamp_gaps_per_stream():
    pre = AudioPreprocessor(expected_frame_ms=100, max_gap_ms=160)
    pcm = pcm_with_sample(800)

    first = pre.process("call-1", "caller", pcm, seq=1, start_time_ms=0, end_time_ms=100)
    other_speaker = pre.process("call-1", "agent", pcm, seq=9, start_time_ms=0, end_time_ms=100)
    second = pre.process("call-1", "caller", pcm, seq=3, start_time_ms=400, end_time_ms=500)

    assert first.diagnostics == []
    assert other_speaker.diagnostics == []
    assert any("seq gap" in item for item in second.diagnostics)
    assert any("timestamp gap" in item for item in second.diagnostics)
