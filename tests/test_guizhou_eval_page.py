import json
import math
import unicodedata
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
DATA_PATH = WEB_ROOT / "guizhou_eval" / "results.json"
PAGE_PATH = WEB_ROOT / "guizhou-eval.html"


def _metric_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if unicodedata.category(character)[0] not in {"P", "Z"}
    )


def _edit_distance(reference: str, hypothesis: str) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, reference_character in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_character in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_character != hypothesis_character),
                )
            )
        previous = current
    return previous[-1]


def _load_data() -> dict:
    if not DATA_PATH.is_file():
        pytest.skip("local Guizhou evaluation fixtures are not included in this checkout")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def test_guizhou_eval_contains_26_unique_samples_and_audio_files():
    data = _load_data()
    results = data["results"]

    assert data["summary"]["samples"] == 26
    assert len(results) == 26
    assert len({result["id"] for result in results}) == 26

    for result in results:
        audio_path = WEB_ROOT / result["audio"]
        assert audio_path.is_file(), result["audio"]
        assert audio_path.stat().st_size > 0, result["audio"]
        assert audio_path.suffix == ".mp3"


def test_guizhou_eval_summary_matches_recomputed_details():
    data = _load_data()
    summary = data["summary"]
    results = data["results"]

    edits = []
    reference_characters = []
    for result in results:
        reference = _metric_text(result["reference"])
        hypothesis = _metric_text(result["hypothesis"])
        edit_count = _edit_distance(reference, hypothesis)
        assert edit_count == result["edits"]
        assert len(reference) == result["referenceChars"]
        assert math.isclose(result["cer"], edit_count / len(reference))
        edits.append(edit_count)
        reference_characters.append(len(reference))

    assert sum(edits) == summary["totalEdits"] == 15
    assert sum(reference_characters) == summary["totalReferenceChars"] == 389
    assert math.isclose(summary["cer"], sum(edits) / sum(reference_characters))
    assert sum(edit_count == 0 for edit_count in edits) == summary["exactMatches"] == 17
    assert math.isclose(
        summary["exactMatchRate"], summary["exactMatches"] / summary["samples"]
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


def test_guizhou_eval_page_is_static_accessible_and_contains_no_credentials():
    html = PAGE_PATH.read_text(encoding="utf-8")

    assert 'lang="zh-CN"' in html
    assert 'aria-pressed="true"' in html
    assert 'aria-live="polite"' in html
    assert "guizhou_eval/results.json" in html
    assert "https://" not in html
    assert "http://" not in html
    assert "APISecret" not in html
    assert "APIKey" not in html
    assert "authorization=" not in html.lower()
