import json
import subprocess
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "web" / "accuracy_ae_transcript.js"


def run_scenario(channel, messages):
    create = "createBaselineState" if channel == "baseline" else "createEnhancedState"
    apply = "applyBaseline" if channel == "baseline" else "applyEnhanced"
    script = f"""
require({json.dumps(str(MODULE.resolve()))});
const state = AccuracyAETranscript.{create}();
let result = {{}};
for (const message of {json.dumps(messages, ensure_ascii=False)}) {{
  result = AccuracyAETranscript.{apply}(state, message) || result;
}}
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_baseline_replaces_partial_and_keeps_offline_final():
    result = run_scenario("baseline", [
        {"mode": "2pass-online", "text": "科兴"},
        {"mode": "2pass-online", "text": "科兴科学园"},
        {"mode": "2pass-offline", "text": "科兴科学园发生火灾"},
    ])

    assert result["text"] == "科兴科学园发生火灾"
    assert result["items"] == [{
        "id": "a-final-1",
        "text": "科兴科学园发生火灾",
        "final": True,
    }]


def test_enhanced_replaces_final_text_with_turn_correction():
    result = run_scenario("enhanced", [
        {
            "eventType": "speech.final",
            "payload": {"segmentId": "caller-0001", "text": "我在科信科学园"},
        },
        {
            "eventType": "call.corrected",
            "segmentId": "caller-0001",
            "correctionProvider": "db_align",
            "turns": [{
                "segmentId": "caller-0001",
                "originalText": "我在科信科学园",
                "correctedText": "我在科兴科学园",
            }],
        },
    ])

    assert result["text"] == "我在科兴科学园"
    assert result["correctionCount"] == 1
    assert result["items"][0]["raw"] == "我在科信科学园"
    assert result["items"][0]["corrected"] == "我在科兴科学园"
    assert result["items"][0]["correctionProvider"] == "db_align"


def test_enhanced_streaming_text_is_visible_before_correction():
    result = run_scenario("enhanced", [
        {"mode": "streaming", "segmentId": "caller-0001", "text": "仓库冒"},
        {"mode": "streaming", "segmentId": "caller-0001", "text": "仓库冒烟"},
    ])

    assert result["text"] == "仓库冒烟"
    assert result["items"][0]["final"] is False
    assert result["correctionCount"] == 0


def test_late_complete_final_supersedes_early_short_correction():
    result = run_scenario("enhanced", [
        {
            "eventType": "speech.final",
            "payload": {"segmentId": "caller-0001", "text": "火焰已经窜至"},
        },
        {
            "eventType": "call.corrected",
            "segmentId": "caller-0001",
            "correctedText": "火焰已经窜至",
            "correctionProvider": "db_align+rule_highlight",
        },
        {
            "eventType": "speech.final",
            "payload": {"segmentId": "caller-0001", "text": "火焰已经窜出窗口"},
        },
    ])

    assert result["text"] == "火焰已经窜出窗口"
    assert result["items"][0]["corrected"] == ""
    assert result["correctionCount"] == 0
