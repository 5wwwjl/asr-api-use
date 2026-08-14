import math
import struct
import sys
import wave
from datetime import datetime
from io import BytesIO
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

import recording_store
from recording_store import OpenApiRecordingStore, RecordingStore, create_recording_store


def test_save_segment_writes_safe_playable_wav(tmp_path):
    store = RecordingStore(tmp_path)
    pcm = (1200).to_bytes(2, "little", signed=True) * 16000

    saved = store.save_segment(
        pcm,
        call_id="../../call:8017",
        speaker="agent/../../",
        start_time_ms=0,
        end_time_ms=1000,
        recorded_at=datetime(2026, 6, 12, 12, 30, 0),
    )

    assert saved.path.is_relative_to(tmp_path)
    assert saved.path == tmp_path / "2026-06-12" / "call_8017" / "agent-0001-0000000000-0000001000.wav"
    assert saved.audio_url == "/recordings/2026-06-12/call_8017/agent-0001-0000000000-0000001000.wav"
    assert saved.duration_ms == 1000
    with wave.open(str(saved.path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 16000
        assert wav.readframes(16000) == pcm


def test_save_segment_normalizes_quiet_speech_without_clipping(tmp_path):
    store = RecordingStore(tmp_path)
    pcm = (600).to_bytes(2, "little", signed=True) * 16000

    saved = store.save_segment(
        pcm,
        call_id="call-quiet",
        speaker="agent",
        end_time_ms=1000,
        recorded_at=datetime(2026, 6, 12, 12, 30, 0),
    )

    with wave.open(str(saved.path), "rb") as wav:
        values = struct.unpack("<16000h", wav.readframes(16000))

    rms = math.sqrt(sum(value * value for value in values) / len(values))
    rms_db = 20 * math.log10(rms / 32768)
    assert -24.0 <= rms_db <= -20.0
    assert max(abs(value) for value in values) < 32768


def test_openapi_store_uploads_wav_and_returns_record_id(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"success":true,"data":"rec-123","code":200}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(recording_store.request, "urlopen", fake_urlopen)

    store = OpenApiRecordingStore(
        "http://ids-dev.ks.telewave.tech/file-client/records/upload",
        download_url_template="http://ids-dev.ks.telewave.tech/file-client/records/preview-file/{record_id}",
        extra_headers={"Authorization": "Bearer test-token", "Host": "ids-dev.ks.telewave.tech"},
    )
    pcm = (1200).to_bytes(2, "little", signed=True) * 16000

    saved = store.save_segment(
        pcm,
        call_id="call-upload",
        speaker="caller",
        start_time_ms=0,
        end_time_ms=1000,
    )

    assert captured["url"] == "http://ids-dev.ks.telewave.tech/file-client/records/upload"
    assert captured["timeout"] == 10.0
    assert captured["headers"]["Content-type"].startswith("multipart/form-data; boundary=")
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["Host"] == "ids-dev.ks.telewave.tech"
    assert b'name="file"; filename="caller-0000000000-0000001000.wav"' in captured["body"]
    wav_start = captured["body"].index(b"RIFF")
    wav_end = captured["body"].rindex(b"\r\n--")
    with wave.open(BytesIO(captured["body"][wav_start:wav_end]), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 16000
    assert saved.path is None
    assert saved.record_id == "rec-123"
    assert saved.audio_url == "http://ids-dev.ks.telewave.tech/file-client/records/preview-file/rec-123"
    assert saved.duration_ms == 1000


def test_recording_store_factory_uses_openapi_when_configured(monkeypatch):
    monkeypatch.setenv("ASR_RECORDING_STORE", "openapi")
    monkeypatch.setenv("ASR_RECORDING_UPLOAD_URL", "http://192.168.169.252/file-client/records/upload")
    monkeypatch.setenv("ASR_RECORDING_UPLOAD_HOST_HEADER", "ids-dev.ks.telewave.tech")
    monkeypatch.setenv("ASR_RECORDING_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("ASR_RECORDING_UPLOAD_NO_PROXY", "true")
    monkeypatch.delenv("ASR_RECORDING_TENANT_ID", raising=False)

    store = create_recording_store()

    assert isinstance(store, OpenApiRecordingStore)
    assert store.upload_url == "http://192.168.169.252/file-client/records/upload"
    assert store.extra_headers == {
        "Host": "ids-dev.ks.telewave.tech",
        "Authorization": "Bearer test-token",
    }
    assert store.disable_proxy is True

def test_recording_store_factory_adds_tenant_upload_header(monkeypatch):
    monkeypatch.setenv("ASR_RECORDING_STORE", "openapi")
    monkeypatch.setenv("ASR_RECORDING_UPLOAD_URL", "http://192.168.169.252/file-client/records/tenant-upload")
    monkeypatch.setenv("ASR_RECORDING_UPLOAD_HOST_HEADER", "ids-dev.ks.telewave.tech")
    monkeypatch.setenv("ASR_RECORDING_TENANT_ID", "default")
    monkeypatch.delenv("ASR_RECORDING_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ASR_RECORDING_UPLOAD_AUTH_TOKEN", raising=False)

    store = create_recording_store()

    assert store.upload_url == "http://192.168.169.252/file-client/records/tenant-upload"
    assert store.extra_headers == {
        "Host": "ids-dev.ks.telewave.tech",
        "tenantId": "default",
    }

