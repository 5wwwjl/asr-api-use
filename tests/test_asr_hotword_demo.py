import io
import sys
import wave
from pathlib import Path

import pytest


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_hotword_demo import (  # noqa: E402
    HotwordDemoService,
    RealLocationDemoClient,
    RealLocationDemoConfig,
    RealLocationDemoError,
    wav_bytes_to_pcm,
)
from hotword_manager import InvalidSceneSignal  # noqa: E402


SCENE_HOTWORD_DIR = Path("/home/twai/wjl/DynamicHotwordLoading/hotwords")


def address_audit():
    return {
        "eventId": "event-1",
        "scopeId": "scope-1",
        "inventoryVersion": "inventory-1",
        "addressScope": {
            "itemCount": 3,
            "poiCount": 1,
            "aoiCount": 1,
            "buildingCount": 1,
            "queryMs": 12.345,
        },
        "filteredHotwords": [
            {"word": "电谷源盛广场", "weight": 99},
            {"word": "源盛广场", "weight": 90},
            {"word": "电谷源盛广场", "weight": 80},
        ],
        "location": {
            "environment": "real-167",
            "longitude": 113.93924230,
            "latitude": 22.55250952,
            "radiusMeters": 2000,
            "resolutionMs": 60.587,
        },
    }


def test_demo_session_starts_with_preloaded_libraries():
    service = HotwordDemoService(scene_hotword_dir=SCENE_HOTWORD_DIR)

    result = service.start_session()

    assert result["stage"] == "preloaded"
    assert result["hotwordCount"] == 413
    assert len(result["hotwords"]) == 413
    assert result["handshakeTokenCount"] >= result["hotwordCount"]
    assert set(result["hotwords"][0]) == {"text", "weight"}
    assert [library["id"] for library in result["libraries"]] == [
        "baseline",
        "classification_assist.call_type",
    ]
    assert result["elapsedMs"] >= 0


def test_demo_address_and_scene_update_same_snapshot():
    service = HotwordDemoService(scene_hotword_dir=SCENE_HOTWORD_DIR)
    session_id = service.start_session()["sessionId"]

    address = service.apply_address_audit(session_id, address_audit())
    scene = service.apply_scene_signals(session_id, {
        "call_type": ["fire_fighting"],
        "building_structure": ["highrise_multistory"],
    })

    assert address["address"]["candidateCount"] == 3
    assert address["address"]["uniqueCandidateCount"] == 2
    assert address["address"]["selectedAddressCount"] == 2
    assert address["address"]["callId"] == ""
    assert address["address"]["environment"] == "real-167"
    assert address["address"]["longitude"] == 113.93924230
    assert address["address"]["radiusMeters"] == 2000
    assert address["address"]["resolutionMs"] == 60.587
    assert "电谷源盛广场" in service.hotwords(session_id).split()
    assert {"text": "电谷源盛广场", "weight": 99} in address["hotwords"]
    library_ids = [library["id"] for library in scene["libraries"]]
    assert "classification_assist.call_type" not in library_ids
    assert "call_type.fire_fighting" in library_ids
    assert "building_structure.highrise_multistory" in library_ids
    assert "address.scope:scope-1" in library_ids
    assert scene["effectiveFrom"] == "next_segment"


def test_demo_returns_examples_only_for_selected_real_address_words():
    service = HotwordDemoService(scene_hotword_dir=SCENE_HOTWORD_DIR)
    session_id = service.start_session()["sessionId"]
    audit = address_audit()
    audit["filteredHotwords"].extend([
        {"word": "东方科技大厦", "weight": 20},
        {"word": "万基产业园2栋", "weight": 20},
        {"word": "中国储能大厦", "weight": 20},
    ])

    result = service.apply_address_audit(session_id, audit)

    assert [item["hotword"] for item in result["address"]["examples"]] == [
        "东方科技大厦",
        "万基产业园2栋",
        "中国储能大厦",
    ]
    assert "电缆井冒烟" in result["address"]["examples"][0]["text"]


def test_demo_rejects_invalid_scene_signal():
    service = HotwordDemoService(scene_hotword_dir=SCENE_HOTWORD_DIR)
    session_id = service.start_session()["sessionId"]

    with pytest.raises(InvalidSceneSignal):
        service.apply_scene_signals(
            session_id,
            {"building_structure": ["invalid"]},
        )


def real_location_client():
    config = RealLocationDemoConfig(
        base_url="http://192.168.173.167:18082",
        longitude=113.93924230,
        latitude=22.55250952,
        radius_meters=2000,
        accuracy_meters=30,
        expected_inventory_version="ODS7ALM_AI_REAL_20260810_V1",
        timeout_seconds=10,
    )
    return RealLocationDemoClient(config=config, scope_client=object())


def test_real_location_demo_builds_fixed_coordinate_request():
    payload = real_location_client().build_request(
        call_id="demo-call-1",
        request_id="demo-request-1",
    )

    assert payload["sessionId"] == "demo-call-1"
    assert payload["alarmId"] == "demo-call-1"
    assert payload["radiusMeters"] == 2000
    assert payload["baseStationCoordinate"] == {
        "longitude": 113.93924230,
        "latitude": 22.55250952,
        "coordinateSystem": "WGS84",
        "accuracyMeters": 30,
        "capturedAt": payload["baseStationCoordinate"]["capturedAt"],
    }


def test_real_location_demo_parses_ready_real_inventory():
    binding = real_location_client().parse_response(
        {
            "success": True,
            "code": "OK",
            "data": {
                "sessionId": "demo-call-1",
                "addressScopeRef": {
                    "scopeId": "7fdf45a3-6f9a-4263-b621-4a86ed51dcee",
                    "locationResolutionId": "4cfb4827-6437-441b-af09-ed1154f4444e",
                    "locationResolutionVersion": 1,
                    "inventoryVersion": "ODS7ALM_AI_REAL_20260810_V1",
                    "scopeStatus": "READY",
                },
            },
        },
        call_id="demo-call-1",
        event_id="9b436e28-02f7-32d1-af3b-c02965bc0815",
    )

    assert binding.call_id == "demo-call-1"
    assert binding.scope_id == "7fdf45a3-6f9a-4263-b621-4a86ed51dcee"
    assert binding.inventory_version == "ODS7ALM_AI_REAL_20260810_V1"
    assert binding.items_path.endswith(f"/{binding.scope_id}/items")


def test_real_location_demo_rejects_non_real_inventory():
    with pytest.raises(RealLocationDemoError, match="INVENTORY_MISMATCH"):
        real_location_client().parse_response(
            {
                "success": True,
                "code": "OK",
                "data": {
                    "sessionId": "demo-call-1",
                    "addressScopeRef": {
                        "scopeId": "7fdf45a3-6f9a-4263-b621-4a86ed51dcee",
                        "locationResolutionId": "4cfb4827-6437-441b-af09-ed1154f4444e",
                        "locationResolutionVersion": 1,
                        "inventoryVersion": "REALISTIC_AI_SOURCE_V1",
                        "scopeStatus": "READY",
                    },
                },
            },
            call_id="demo-call-1",
            event_id="9b436e28-02f7-32d1-af3b-c02965bc0815",
        )


def test_wav_bytes_to_pcm_validates_demo_audio_format():
    pcm = b"\x01\x00" * 160
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm)

    assert wav_bytes_to_pcm(output.getvalue()) == pcm
