import json
import math
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "web" / "guizhou_dialect" / "results.json"
PAGE_PATH = ROOT / "web" / "guizhou-dialect.html"

EXPECTED_IDS = {
    "Yc1v1a28106d20230811t1633191",
    "Yc1v1a28106d20230811t1722098",
    "Yc1v1a28106d20230811t1729292",
    "Yc1v1a28106d20230811t1735286",
    "Yc1v1a28106d20230811t1825135",
}


def _load_data() -> dict:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def test_guizhou_dialect_page_contains_five_machine_labeled_audio_files():
    data = _load_data()
    results = data["results"]
    first_audio = ROOT / results[0]["audio"].lstrip("/")
    if not first_audio.is_file():
        pytest.skip("local dialect evaluation audio is not included in this checkout")

    assert data["metadata"]["labelStatus"] == "machine_unverified"
    assert data["summary"]["samples"] == 5
    assert data["summary"]["successes"] == 5
    assert len(results) == 5
    assert {result["id"] for result in results} == EXPECTED_IDS

    for result in results:
        assert result["status"] == "success"
        assert result["hypothesis"].strip()
        assert result["attempts"] >= 1
        assert "reference" not in result
        assert "cer" not in result
        assert result["audio"].startswith("/audio_data/guizhou/playback/")
        assert result["sourceAudio"].startswith("/audio_data/guizhou/")
        for field in ("audio", "sourceAudio"):
            audio_path = ROOT / result[field].lstrip("/")
            assert audio_path.is_file(), result[field]
            assert audio_path.stat().st_size > 0


def test_guizhou_dialect_summary_matches_results():
    data = _load_data()
    summary = data["summary"]
    results = data["results"]

    assert math.isclose(
        summary["totalAudioSeconds"],
        sum(result["audioSeconds"] for result in results),
        abs_tol=0.001,
    )
    assert math.isclose(
        summary["averageAudioSeconds"],
        sum(result["audioSeconds"] for result in results) / len(results),
        abs_tol=0.001,
    )
    assert math.isclose(
        summary["averageElapsedSeconds"],
        sum(result["elapsedSeconds"] for result in results) / len(results),
        abs_tol=0.001,
    )


def test_guizhou_dialect_page_is_static_accessible_and_contains_no_credentials():
    html = PAGE_PATH.read_text(encoding="utf-8")

    assert 'lang="zh-CN"' in html
    assert "guizhou_dialect/results.json" in html
    assert "机器初标（未经人工校对）" in html
    assert "https://" not in html
    assert "http://" not in html
    assert "APISecret" not in html
    assert "APIKey" not in html
    assert "authorization=" not in html.lower()
