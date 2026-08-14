import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from xfyun_dialect_iat_smoke import (  # noqa: E402
    DialectResultAccumulator,
    DialectServiceError,
    build_audio_message,
    build_auth_url,
)


def _response_result(
    *,
    sn: int,
    words: list[str],
    pgs: str = "apd",
    rg: list[int] | None = None,
    header_status: int = 1,
) -> dict:
    result = {
        "sn": sn,
        "ls": header_status == 2,
        "pgs": pgs,
        "rst": "pgs" if header_status != 2 else "rlt",
        "ws": [{"cw": [{"w": word}]} for word in words],
    }
    if rg is not None:
        result["rg"] = rg
    encoded = base64.b64encode(
        json.dumps(result, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return {
        "header": {"code": 0, "message": "success", "sid": "sid-1", "status": header_status},
        "payload": {
            "result": {
                "encoding": "utf8",
                "compress": "raw",
                "format": "json",
                "status": header_status,
                "seq": sn,
                "text": encoded,
            }
        },
    }


def test_build_auth_url_matches_fixed_hmac_sha256_vector():
    now = datetime(2026, 7, 21, 2, 20, 30, tzinfo=timezone.utc)

    url = build_auth_url(
        api_key="test-key",
        api_secret="test-secret",
        now=now,
    )

    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "iat.cn-huabei-1.xf-yun.com"
    assert parsed.path == "/v1"
    assert query["host"] == ["iat.cn-huabei-1.xf-yun.com"]
    assert query["date"] == ["Tue, 21 Jul 2026 02:20:30 GMT"]
    assert query["authorization"] == [
        "YXBpX2tleT0idGVzdC1rZXkiLCBhbGdvcml0aG09ImhtYWMtc2hhMjU2Iiwg"
        "aGVhZGVycz0iaG9zdCBkYXRlIHJlcXVlc3QtbGluZSIsIHNpZ25hdHVyZT0i"
        "WSs2NVZFaFpOV0VxNEhpVWZUSm11Mjc0T2xNcU1OeVQ1d3VLTDkwL1JlTT0i"
    ]
    assert "test-secret" not in url


def test_first_audio_message_contains_dialect_model_parameters():
    message = build_audio_message(
        app_id="test-app",
        sample_rate=16000,
        status=0,
        seq=0,
        pcm=b"\x01\x02\x03\x04",
    )

    assert message["header"] == {"status": 0, "app_id": "test-app"}
    assert message["parameter"]["iat"]["domain"] == "slm"
    assert message["parameter"]["iat"]["language"] == "zh_cn"
    assert message["parameter"]["iat"]["accent"] == "mulacc"
    assert message["parameter"]["iat"]["dwa"] == "wpgs"
    audio = message["payload"]["audio"]
    assert audio["sample_rate"] == 16000
    assert audio["encoding"] == "raw"
    assert audio["status"] == 0
    assert audio["seq"] == 0
    assert base64.b64decode(audio["audio"]) == b"\x01\x02\x03\x04"


@pytest.mark.parametrize("status", [1, 2])
def test_non_first_audio_message_omits_model_parameters(status: int):
    pcm = b"" if status == 2 else b"\x05\x06"

    message = build_audio_message(
        app_id="test-app",
        sample_rate=8000,
        status=status,
        seq=3,
        pcm=pcm,
    )

    assert "parameter" not in message
    assert message["header"]["status"] == status
    assert message["payload"]["audio"]["status"] == status
    assert message["payload"]["audio"]["sample_rate"] == 8000
    assert base64.b64decode(message["payload"]["audio"]["audio"]) == pcm


def test_result_accumulator_applies_wpgs_replacement_range():
    accumulator = DialectResultAccumulator()

    accumulator.consume(_response_result(sn=0, words=["科", "信"]))
    accumulator.consume(_response_result(sn=1, words=["科学园"]))
    accumulator.consume(
        _response_result(sn=2, words=["科兴科学园"], pgs="rpl", rg=[0, 1])
    )
    accumulator.consume(_response_result(sn=3, words=["到了", "。"], header_status=2))

    assert accumulator.final_received is True
    assert accumulator.sid == "sid-1"
    assert accumulator.final_text == "科兴科学园到了。"


def test_result_accumulator_raises_service_error():
    accumulator = DialectResultAccumulator()
    response = {
        "header": {
            "code": 10313,
            "message": "invalid appid",
            "sid": "sid-error",
            "status": 2,
        }
    }

    with pytest.raises(DialectServiceError) as exc_info:
        accumulator.consume(response)

    assert exc_info.value.code == "10313"
    assert exc_info.value.sid == "sid-error"
