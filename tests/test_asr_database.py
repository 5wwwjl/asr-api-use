import sys
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from datetime import datetime

from asr_database import AsrDatabaseReader, AsrDatabaseWriter, create_asr_database_reader, create_asr_database_writer


class CapturingWriter(AsrDatabaseWriter):
    def __init__(self):
        super().__init__(enabled=True, host="db", database="ods7alm", user="tgms", password="secret")
        self.inserted = []

    def _insert_record(self, record):
        self.inserted.append(record)
        return True


def speech(segment_id, text):
    return {
        "event": "speech.final",
        "callId": "call-1",
        "segmentId": segment_id,
        "callfrom": "8015",
        "callto": "119",
        "speaker": "caller",
        "text": text,
        "startTimeMs": 1000,
        "endTimeMs": 2000,
        "sendTimeMs": 1781510400000,
    }


def audio():
    return {
        "event": "audio.segment",
        "callId": "call-1",
        "segmentId": "caller-0001",
        "segmentIds": ["caller-0001", "caller-0002"],
        "callfrom": "8015",
        "callto": "119",
        "speaker": "caller",
        "audioUrl": "/recordings/2026-06-16/call-1/caller-0001.wav",
        "audioDurationMs": 2300,
        "startTimeMs": 1000,
        "endTimeMs": 3300,
        "sendTimeMs": 1781510401000,
    }


def test_audio_turn_inserts_one_record_with_merged_segment_text():
    writer = CapturingWriter()
    writer.remember_speech(speech("caller-0001", "我这里"))
    writer.remember_speech(speech("caller-0002", "发生火灾"))

    writer.save_audio_turn(audio())

    assert len(writer.inserted) == 1
    record = writer.inserted[0]
    assert record.call_id == "call-1"
    assert record.segment_id == "caller-0001"
    assert record.call_from == "8015"
    assert record.call_to == "119"
    assert record.speaker == "caller"
    assert record.speech_content == "我这里 发生火灾"
    assert record.speech_url == "/recordings/2026-06-16/call-1/caller-0001.wav"
    assert record.start_time == "00:00:01"
    assert record.end_time == "00:00:03"
    assert record.duration == 3
    assert record.create_by == "asr-bridge"
    assert record.asr_id.startswith("1781510401000")


def test_audio_turn_waits_until_all_segment_text_has_arrived():
    writer = CapturingWriter()
    writer.remember_speech(speech("caller-0001", "我这里"))

    writer.save_audio_turn(audio())

    assert writer.inserted == []

    writer.remember_speech(speech("caller-0002", "发生火灾"))

    assert len(writer.inserted) == 1
    assert writer.inserted[0].speech_content == "我这里 发生火灾"


def test_uuid_call_id_is_preserved_when_database_column_allows_36_chars():
    writer = CapturingWriter()
    event = speech("caller-0001", "有人被困")
    event["callId"] = "a726d4d0-cca6-4bdd-8dcf-70a93af12ada"
    writer.remember_speech(event)
    audio_event = audio()
    audio_event["callId"] = "a726d4d0-cca6-4bdd-8dcf-70a93af12ada"
    audio_event["segmentIds"] = ["caller-0001"]

    writer.save_audio_turn(audio_event)

    assert writer.inserted[0].call_id == "a726d4d0-cca6-4bdd-8dcf-70a93af12ada"
    assert len(writer.inserted[0].call_id) == 36


def test_factory_reads_asr_database_environment(monkeypatch):
    monkeypatch.setenv("ASR_DB_ENABLED", "true")
    monkeypatch.setenv("ASR_DB_HOST", "192.168.173.198")
    monkeypatch.setenv("ASR_DB_PORT", "54321")
    monkeypatch.setenv("ASR_DB_NAME", "ods7alm")
    monkeypatch.setenv("ASR_DB_USER", "tgms")
    monkeypatch.setenv("ASR_DB_PASS", "secret")
    monkeypatch.setenv("ASR_DB_SCHEMA", "ai")
    monkeypatch.setenv("ASR_DB_TABLE", "asr_speech_recognition")
    monkeypatch.setenv("ASR_DB_CREATE_BY", "asr-bridge")

    writer = create_asr_database_writer()

    assert writer.enabled is True
    assert writer.host == "192.168.173.198"
    assert writer.port == 54321
    assert writer.database == "ods7alm"
    assert writer.user == "tgms"
    assert writer.password == "secret"
    assert writer.schema == "ai"
    assert writer.table == "asr_speech_recognition"
    assert writer.create_by == "asr-bridge"
    assert writer.call_id_max_length == 36


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj


def test_reader_queries_records_by_call_id_and_maps_frontend_fields():
    rows = [(
        "asr-1",
        "call-1",
        "caller-0001",
        "8015",
        "8014",
        "caller",
        "这里着火了",
        "/recordings/call-1/caller-0001.wav",
        "00:00:01",
        "00:00:03",
        2,
        datetime(2026, 7, 6, 10, 0, 1),
        "asr-bridge",
    )]
    cursor = FakeCursor(rows)
    reader = AsrDatabaseReader(enabled=True, host="db", database="ods7alm", user="tgms")
    reader._config._connect = lambda: FakeConnection(cursor)

    records = reader.list_records("call-1", limit=50)

    assert records == [{
        "asrId": "asr-1",
        "callId": "call-1",
        "segmentId": "caller-0001",
        "callfrom": "8015",
        "callto": "8014",
        "speaker": "caller",
        "text": "这里着火了",
        "audioUrl": "/recordings/call-1/caller-0001.wav",
        "startTime": "00:00:01",
        "endTime": "00:00:03",
        "duration": 2,
        "sendTime": "2026-07-06 10:00:01",
        "createBy": "asr-bridge",
    }]
    sql, params = cursor.executed[0]
    assert "FROM ai.asr_speech_recognition" in sql
    assert "WHERE call_id = %s" in sql
    assert "ORDER BY send_time ASC, start_time ASC, segment_id ASC" in sql
    assert params == ("call-1", 50)


def test_reader_caps_limit_to_max_limit():
    cursor = FakeCursor([])
    reader = AsrDatabaseReader(enabled=True, max_limit=100)
    reader._config._connect = lambda: FakeConnection(cursor)

    reader.list_records("call-1", limit=5000)

    assert cursor.executed[0][1] == ("call-1", 100)


def test_reader_factory_reads_query_limits(monkeypatch):
    monkeypatch.setenv("ASR_DB_ENABLED", "true")
    monkeypatch.setenv("ASR_DB_HOST", "192.168.173.198")
    monkeypatch.setenv("ASR_DB_PORT", "54321")
    monkeypatch.setenv("ASR_DB_NAME", "ods7alm")
    monkeypatch.setenv("ASR_DB_USER", "tgms")
    monkeypatch.setenv("ASR_DB_PASS", "secret")
    monkeypatch.setenv("ASR_DB_SCHEMA", "ai")
    monkeypatch.setenv("ASR_DB_TABLE", "asr_speech_recognition")
    monkeypatch.setenv("ASR_DB_QUERY_DEFAULT_LIMIT", "200")
    monkeypatch.setenv("ASR_DB_QUERY_MAX_LIMIT", "800")

    reader = create_asr_database_reader()

    assert reader.enabled is True
    assert reader.host == "192.168.173.198"
    assert reader.port == 54321
    assert reader.database == "ods7alm"
    assert reader.user == "tgms"
    assert reader.password == "secret"
    assert reader.schema == "ai"
    assert reader.table == "asr_speech_recognition"
    assert reader.default_limit == 200
    assert reader.max_limit == 800
