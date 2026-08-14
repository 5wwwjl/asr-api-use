import json
import subprocess
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "web" / "hotword_demo_transcript.js"


def run_transcript_scenario(messages):
    script = f"""
require({json.dumps(str(MODULE.resolve()))});
const state = HotwordTranscript.createState();
let result = '';
for (const message of {json.dumps(messages, ensure_ascii=False)}) {{
  const next = HotwordTranscript.applyMessage(state, message);
  if (next !== null) result = next;
}}
process.stdout.write(result);
"""
    return subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_post_correction_is_ignored_and_raw_streaming_remains():
    messages = [
        {"mode": "streaming", "segmentId": "caller-0001", "text": "商住楼冒烟"},
        {
            "event": "call.corrected",
            "segmentId": "caller-0001",
            "segmentIds": ["caller-0001"],
            "correctedText": "商住楼的合用前室冒烟了",
        },
    ]

    assert run_transcript_scenario(messages) == "商住楼冒烟"


def test_raw_streaming_can_continue_after_ignored_correction():
    messages = [
        {"mode": "streaming", "segmentId": "caller-0001", "text": "商住楼冒烟"},
        {
            "eventType": "call.corrected",
            "segmentId": "caller-0001",
            "correctedText": "商住楼的合用前室冒烟了",
        },
        {
            "mode": "streaming",
            "segmentId": "caller-0001",
            "text": "商住楼冒烟烟正在竖向蔓延",
        },
    ]

    assert run_transcript_scenario(messages) == "商住楼冒烟烟正在竖向蔓延"


def test_distinct_segments_are_kept_in_order():
    messages = [
        {"mode": "streaming", "segmentId": "caller-0001", "text": "第一句话"},
        {"mode": "streaming", "segmentId": "caller-0002", "text": "第二句话"},
    ]

    assert run_transcript_scenario(messages) == "第一句话，第二句话"
