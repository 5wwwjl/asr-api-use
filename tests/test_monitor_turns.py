import json
import subprocess
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
MONITOR_TURNS_MODULE = ASR_DIR / "web" / "monitor_turns.js"


def run_monitor_turns_script(script: str) -> dict:
    script = script.replace(
        "require('./asr_api_use/web/monitor_turns.js')",
        f"require({json.dumps(str(MONITOR_TURNS_MODULE))})",
    )
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ASR_DIR,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def test_fixed_call_id_splits_turns_when_speaker_changes():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState({turnSwitchStableMs: 0, requireVadForSwitch: false});
state.handleEvent({event:'call.started', callId:'fixed-call', callfrom:'1001', callto:'1002'});
state.handleEvent({event:'speech.final', callId:'fixed-call', callfrom:'1001', callto:'1002', speaker:'agent', text:'请问地址在哪', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'speech.final', callId:'fixed-call', callfrom:'1001', callto:'1002', speaker:'agent', text:'请问地址在哪里？', startTimeMs:0, endTimeMs:1500});
state.handleEvent({event:'speech.final', callId:'fixed-call', callfrom:'1001', callto:'1002', speaker:'caller', text:'在高层住宅18层', startTimeMs:2000, endTimeMs:3500});
state.handleEvent({event:'speech.final', callId:'fixed-call', callfrom:'1001', callto:'1002', speaker:'agent', text:'有没有人员被困？', startTimeMs:4000, endTimeMs:5000});
state.handleEvent({event:'speech.final', callId:'fixed-call', callfrom:'1001', callto:'1002', speaker:'caller', text:'有三个人被困', startTimeMs:5500, endTimeMs:6500});

const conv = state.conversations()[0];
assert.equal(conv.turns.length, 4);
assert.deepEqual(conv.turns.map(t => t.type), ['Q', 'A', 'Q', 'A']);
assert.deepEqual(conv.turns.map(t => t.text), [
  '请问地址在哪里？',
  '在高层住宅18层',
  '有没有人员被困？',
  '有三个人被困',
]);
assert.deepEqual(conv.turns.map(t => t.locked), [true, true, true, false]);
assert.equal(state.totalTurns(), 4);

console.log(JSON.stringify({turns: conv.turns, totalTurns: state.totalTurns()}));
"""
    result = run_monitor_turns_script(script)

    assert [t["text"] for t in result["turns"]] == [
        "请问地址在哪里？",
        "在高层住宅18层",
        "有没有人员被困？",
        "有三个人被困",
    ]



def test_new_call_after_ended_same_phone_pair_starts_fresh_conversation():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState({turnSwitchStableMs: 0, requireVadForSwitch: false});
state.handleEvent({event:'call.started', callId:'call-old', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'call-old', segmentId:'agent-0001', callfrom:'8001', callto:'8002', speaker:'agent', text:'上一通问题', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'call.ended', callId:'call-old', callfrom:'8001', callto:'8002'});

state.handleEvent({event:'call.started', callId:'call-new', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'call-new', segmentId:'agent-0001', callfrom:'8001', callto:'8002', speaker:'agent', text:'这一通问题', startTimeMs:0, endTimeMs:900});

const conversations = state.conversations();
assert.equal(conversations.length, 2);
assert.deepEqual(conversations.map(c => c.turns.map(t => t.text)), [['上一通问题'], ['这一通问题']]);
assert.equal(conversations[0].status, 'ended');
assert.equal(conversations[1].status, 'active');
console.log(JSON.stringify({conversations}));
"""
    result = run_monitor_turns_script(script)

    assert [[turn["text"] for turn in c["turns"]] for c in result["conversations"]] == [
        ["上一通问题"],
        ["这一通问题"],
    ]


def test_hold_events_mark_phone_pair_conversation_as_holding_then_active():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState({turnSwitchStableMs: 0, requireVadForSwitch: false});
state.handleEvent({event:'call.started', callId:'caller-call', callfrom:'8015', callto:'8014'});
state.handleEvent({event:'call.started', callId:'agent-call', callfrom:'8015', callto:'8014'});

state.handleEvent({event:'call.hold.started', callId:'caller-call', callfrom:'8015', callto:'8014', message:'通话保持中，转写暂停'});
let conv = state.conversations()[0];
assert.equal(conv.status, 'holding');
assert.equal(conv.holdMessage, '通话保持中，转写暂停');
assert.deepEqual(Array.from(conv.activeCallIds).sort(), ['agent-call', 'caller-call']);

state.handleEvent({event:'call.hold.ended', callId:'caller-call', callfrom:'8015', callto:'8014', message:'通话已恢复，转写继续'});
conv = state.conversations()[0];
assert.equal(conv.status, 'active');
assert.equal(conv.holdMessage, '通话已恢复，转写继续');

console.log(JSON.stringify({status: conv.status, holdMessage: conv.holdMessage}));
"""
    result = run_monitor_turns_script(script)

    assert result == {"status": "active", "holdMessage": "通话已恢复，转写继续"}


def test_model_state_aggregates_two_stream_switch_progress():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState();
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'asr.model.state', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:null, availableProviders:['funasr','xfyun']});
state.handleEvent({event:'asr.model.state', callId:'agent-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:null, availableProviders:['funasr','xfyun']});

const conv = state.conversations()[0];
let summary = state.modelSummary(conv);
assert.equal(summary.currentProvider, 'funasr');
assert.equal(summary.canUseXfyun, true);

state.handleEvent({event:'asr.model.switch.pending', requestId:'req-1', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:'xfyun'});
state.handleEvent({event:'asr.model.switch.pending', requestId:'req-1', callId:'agent-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:'xfyun'});
summary = state.modelSummary(conv);
assert.equal(summary.status, 'pending');
assert.equal(summary.completed, 0);
assert.equal(summary.total, 2);

state.handleEvent({event:'asr.model.changed', requestId:'req-1', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'xfyun', pendingProvider:null});
summary = state.modelSummary(conv);
assert.equal(summary.status, 'pending');
assert.equal(summary.completed, 1);

state.handleEvent({event:'asr.model.changed', requestId:'req-1', callId:'agent-stream', callfrom:'8001', callto:'8002', currentProvider:'xfyun', pendingProvider:null});
summary = state.modelSummary(conv);
assert.equal(summary.status, 'active');
assert.equal(summary.currentProvider, 'xfyun');

console.log(JSON.stringify(summary));
"""
    result = run_monitor_turns_script(script)

    assert result["currentProvider"] == "xfyun"
    assert result["status"] == "active"


def test_model_summary_preserves_last_provider_after_both_streams_end():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState();
for (const callId of ['caller-stream', 'agent-stream']) {
  state.handleEvent({event:'call.started', callId, callfrom:'8001', callto:'8002'});
  state.handleEvent({
    event:'asr.model.changed', callId, callfrom:'8001', callto:'8002',
    currentProvider:'xfyun', pendingProvider:null,
    availableProviders:['funasr','xfyun'], requestId:'req-xfyun'
  });
}
state.handleEvent({event:'call.ended', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.ended', callId:'agent-stream', callfrom:'8001', callto:'8002'});

const conv = state.conversations()[0];
const summary = state.modelSummary(conv);
assert.equal(conv.status, 'ended');
assert.equal(conv.activeCallIds.size, 0);
assert.equal(summary.currentProvider, 'xfyun');
assert.equal(summary.status, 'active');

console.log(JSON.stringify({
  conversationStatus: conv.status,
  activeCallCount: conv.activeCallIds.size,
  currentProvider: summary.currentProvider,
  modelStatus: summary.status,
}));
"""
    result = run_monitor_turns_script(script)

    assert result == {
        "conversationStatus": "ended",
        "activeCallCount": 0,
        "currentProvider": "xfyun",
        "modelStatus": "active",
    }


def test_turn_preserves_actual_providers_and_marks_cross_model_text_as_mixed():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState();
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({
  event:'asr.model.state', callId:'caller-stream', callfrom:'8001', callto:'8002',
  currentProvider:'funasr', pendingProvider:null, availableProviders:['funasr','xfyun']
});
state.handleEvent({
  event:'speech.final', callId:'caller-stream', segmentId:'caller-0001',
  callfrom:'8001', callto:'8002', speaker:'caller', text:'我在贵阳',
  provider:'funasr', startTimeMs:0, endTimeMs:1000, sendTimeMs:100
});
state.handleEvent({
  event:'asr.model.changed', callId:'caller-stream', callfrom:'8001', callto:'8002',
  currentProvider:'xfyun', pendingProvider:null, availableProviders:['funasr','xfyun']
});
state.handleEvent({
  event:'speech.final', callId:'caller-stream', segmentId:'caller-0002',
  callfrom:'8001', callto:'8002', speaker:'caller', text:'这里着火了',
  provider:'xfyun', startTimeMs:1000, endTimeMs:2000, sendTimeMs:200
});
state.handleEvent({
  event:'speech.final', finalSource:'offline', callId:'caller-stream',
  segmentId:'caller-0001', segmentIds:['caller-0001','caller-0002'],
  callfrom:'8001', callto:'8002', speaker:'caller', text:'我在贵阳这里着火了',
  provider:'mixed', providers:['funasr','xfyun'],
  startTimeMs:0, endTimeMs:2000, sendTimeMs:300
});

const turn = state.conversations()[0].turns[0];
assert.deepEqual(turn.providers, ['funasr','xfyun']);
assert.equal(turn.provider, 'mixed');
assert.equal(turn.text, '我在贵阳这里着火了');

console.log(JSON.stringify({
  provider: turn.provider,
  providers: turn.providers,
  text: turn.text,
}));
"""
    result = run_monitor_turns_script(script)

    assert result == {
        "provider": "mixed",
        "providers": ["funasr", "xfyun"],
        "text": "我在贵阳这里着火了",
    }


def test_turn_provider_uses_model_state_fallback_without_defaulting_unknown_to_funasr():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const withState = createMonitorState();
withState.handleEvent({event:'call.started', callId:'known-call', callfrom:'8001', callto:'8002'});
withState.handleEvent({
  event:'asr.model.state', callId:'known-call', callfrom:'8001', callto:'8002',
  currentProvider:'xfyun', pendingProvider:null, availableProviders:['funasr','xfyun']
});
withState.handleEvent({
  event:'speech.final', callId:'known-call', segmentId:'caller-0001',
  callfrom:'8001', callto:'8002', speaker:'caller', text:'旧事件没有provider',
  startTimeMs:0, endTimeMs:1000
});

const withoutState = createMonitorState();
withoutState.handleEvent({
  event:'speech.final', callId:'unknown-call', segmentId:'caller-0001',
  callfrom:'9001', callto:'9002', speaker:'caller', text:'来源未知',
  startTimeMs:0, endTimeMs:1000
});

const knownTurn = withState.conversations()[0].turns[0];
const unknownTurn = withoutState.conversations()[0].turns[0];
assert.equal(knownTurn.provider, 'xfyun');
assert.deepEqual(knownTurn.providers, ['xfyun']);
assert.equal(unknownTurn.provider, '');
assert.deepEqual(unknownTurn.providers, []);

console.log(JSON.stringify({
  knownProvider: knownTurn.provider,
  unknownProvider: unknownTurn.provider,
  unknownProviders: unknownTurn.providers,
}));
"""
    result = run_monitor_turns_script(script)

    assert result == {
        "knownProvider": "xfyun",
        "unknownProvider": "",
        "unknownProviders": [],
    }


def test_late_model_failure_after_call_ended_does_not_reactivate_stream():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState();
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'asr.model.state', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:null, availableProviders:['funasr','xfyun']});
state.handleEvent({event:'asr.model.switch.pending', requestId:'req-late', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:'xfyun'});
state.handleEvent({event:'asr.model.changed', requestId:'req-late', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'xfyun', pendingProvider:null});
state.handleEvent({event:'call.ended', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'asr.model.switch.failed', requestId:'req-late', callId:'caller-stream', callfrom:'8001', callto:'8002', currentProvider:'funasr', pendingProvider:null, errorCode:'XFYUN_CONNECTIONERROR'});

const conv = state.conversations()[0];
const summary = state.modelSummary(conv);
assert.equal(conv.status, 'ended');
assert.equal(conv.activeCallIds.size, 0);
assert.equal(conv.modelSwitchRequest, null);
assert.notEqual(summary.status, 'failed');

console.log(JSON.stringify({
  conversationStatus: conv.status,
  activeCallCount: conv.activeCallIds.size,
  switchRequest: conv.modelSwitchRequest,
  modelStatus: summary.status,
}));
"""
    result = run_monitor_turns_script(script)

    assert result == {
        "conversationStatus": "ended",
        "activeCallCount": 0,
        "switchRequest": None,
        "modelStatus": "active",
    }



def test_cumulative_two_stream_progress_is_debounced_and_delta_based():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState({turnSwitchStableMs: 1000, minSwitchChars: 3, requireVadForSwitch: false});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});

state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂喂喂。', startTimeMs:0, endTimeMs:3200, sendTimeMs:0});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'你。', startTimeMs:0, endTimeMs:3400, sendTimeMs:200});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂喂喂，你现在。', startTimeMs:0, endTimeMs:5300, sendTimeMs:400});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'你好嗯这边有个。', startTimeMs:0, endTimeMs:8600, sendTimeMs:600});

let conv = state.conversations()[0];
assert.equal(conv.turns.length, 1);
assert.equal(conv.turns[0].text, '喂喂喂，你现在。');
assert.equal(conv.turns[0].locked, false);

state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'你好嗯，这边有个叫肖玉林的起火。', startTimeMs:0, endTimeMs:10800, sendTimeMs:2000});
conv = state.conversations()[0];
assert.equal(conv.turns.length, 2);
assert.deepEqual(conv.turns.map(t => t.type), ['A', 'Q']);
assert.deepEqual(conv.turns.map(t => t.text), ['喂喂喂，你现在。', '你好嗯，这边有个叫肖玉林的起火。']);
assert.deepEqual(conv.turns.map(t => t.locked), [true, false]);
assert.deepEqual(conv.turns.map(t => t.stabilityStatus), ['stable', 'waiting']);

state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂喂喂，你现在又有什么事情？他能分。', startTimeMs:0, endTimeMs:17500, sendTimeMs:2200});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂喂喂，你现在又有什么事情？他能分，但是分得好怪。', startTimeMs:0, endTimeMs:19200, sendTimeMs:3600});
conv = state.conversations()[0];
assert.equal(conv.turns.length, 3);
assert.deepEqual(conv.turns.map(t => t.type), ['A', 'Q', 'A']);
assert.equal(conv.turns[2].text, '又有什么事情？他能分，但是分得好怪。');
assert.equal(state.totalTurns(), 3);

console.log(JSON.stringify({turns: conv.turns, totalTurns: state.totalTurns()}));
"""
    result = run_monitor_turns_script(script)

    assert [t["text"] for t in result["turns"]] == [
        "喂喂喂，你现在。",
        "你好嗯，这边有个叫肖玉林的起火。",
        "又有什么事情？他能分，但是分得好怪。",
    ]



def test_vad_end_and_opposite_text_are_both_required_to_switch_turns():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState({requireVadForSwitch: true, minSwitchChars: 3});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});

state.handleEvent({event:'speech.vad', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', vadState:'speaking', audioLevel:62, volumeDb:-24.5, sendTimeMs:0});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂喂喂，你现在有什么事情？', startTimeMs:0, endTimeMs:3000, sendTimeMs:100});
state.handleEvent({event:'speech.vad', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', vadState:'speaking', sendTimeMs:200});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'你好，这边有个起火。', startTimeMs:0, endTimeMs:3600, sendTimeMs:300});

let conv = state.conversations()[0];
assert.equal(conv.turns.length, 1);
assert.equal(conv.turns[0].type, 'A');
assert.equal(conv.turns[0].text, '喂喂喂，你现在有什么事情？');
assert.equal(conv.turns[0].locked, false);
assert.equal(conv.turns[0].stabilityStatus, 'waiting');
assert.equal(conv.turns[0].audioLevel, 62);

state.handleEvent({event:'speech.vad', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', vadState:'ended', silenceDurationMs:1500, sendTimeMs:1800});
conv = state.conversations()[0];
assert.equal(conv.turns.length, 2);
assert.deepEqual(conv.turns.map(t => t.type), ['A', 'Q']);
assert.deepEqual(conv.turns.map(t => t.text), ['喂喂喂，你现在有什么事情？', '你好，这边有个起火。']);
assert.deepEqual(conv.turns.map(t => t.locked), [true, false]);

state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂喂喂，你现在有什么事情？他能分了吗？', startTimeMs:0, endTimeMs:5000, sendTimeMs:1900});
conv = state.conversations()[0];
assert.equal(conv.turns.length, 2);
assert.equal(conv.turns[1].type, 'Q');

state.handleEvent({event:'speech.vad', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', vadState:'ended', silenceDurationMs:1500, audioLevel:3, volumeDb:-58, sendTimeMs:3000});
conv = state.conversations()[0];
assert.equal(conv.turns.length, 3);
assert.deepEqual(conv.turns.map(t => t.type), ['A', 'Q', 'A']);
assert.equal(conv.turns[2].text, '他能分了吗？');
assert.equal(conv.turns[2].startTimeMs, 3000);
assert.equal(conv.turns[2].endTimeMs, 5000);
assert.deepEqual(conv.turns.map(t => t.stabilityStatus), ['stable', 'stable', 'waiting']);

console.log(JSON.stringify({turns: conv.turns, totalTurns: state.totalTurns()}));
"""
    result = run_monitor_turns_script(script)

    assert [t["text"] for t in result["turns"]] == [
        "喂喂喂，你现在有什么事情？",
        "你好，这边有个起火。",
        "他能分了吗？",
    ]
    assert result["turns"][2]["startTimeMs"] == 3000
    assert result["turns"][2]["endTimeMs"] == 5000


def test_cumulative_prefix_is_stripped_when_asr_formatting_changes():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');

const state = createMonitorState({requireVadForSwitch: true, minSwitchChars: 3});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});

state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂，你好，我是夏玉林，我是夏玉林。', startTimeMs:0, endTimeMs:4000, sendTimeMs:100});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'请继续讲。', startTimeMs:0, endTimeMs:4500, sendTimeMs:200});
state.handleEvent({event:'speech.vad', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', vadState:'ended', silenceDurationMs:1400, sendTimeMs:1800});

let conv = state.conversations()[0];
assert.equal(conv.turns.length, 2);
assert.equal(conv.turns[0].text, '喂，你好，我是夏玉林，我是夏玉林。');
assert.equal(conv.turns[0].locked, true);

state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'喂你好我是夏玉林我是夏玉林这什么东西啊？怎么又在跳啊？', startTimeMs:0, endTimeMs:8000, sendTimeMs:2200});
state.handleEvent({event:'speech.vad', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', vadState:'ended', silenceDurationMs:1400, sendTimeMs:3200});

conv = state.conversations()[0];
assert.equal(conv.turns.length, 3);
assert.deepEqual(conv.turns.map(t => t.type), ['A', 'Q', 'A']);
assert.equal(conv.turns[2].text, '这什么东西啊？怎么又在跳啊？');
assert.equal(conv.turns[2].startTimeMs, 4000);
assert.equal(conv.turns[2].endTimeMs, 8000);

console.log(JSON.stringify({turns: conv.turns, totalTurns: state.totalTurns()}));
"""
    result = run_monitor_turns_script(script)

    assert [t["text"] for t in result["turns"]] == [
        "喂，你好，我是夏玉林，我是夏玉林。",
        "请继续讲。",
        "这什么东西啊？怎么又在跳啊？",
    ]
    assert result["turns"][2]["startTimeMs"] == 4000
    assert result["turns"][2]["endTimeMs"] == 8000


def test_audio_segment_attaches_before_or_after_matching_turn():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'call-a', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'audio.segment', callId:'call-a', callfrom:'8001', callto:'8002', speaker:'agent', audioUrl:'/recordings/a.wav', audioDurationMs:1200, startTimeMs:0, endTimeMs:1200});
state.handleEvent({event:'speech.final', callId:'call-a', callfrom:'8001', callto:'8002', speaker:'agent', text:'第一句话。', startTimeMs:0, endTimeMs:1200});
state.handleEvent({event:'speech.final', callId:'call-a', callfrom:'8001', callto:'8002', speaker:'caller', text:'第二句话。', startTimeMs:1300, endTimeMs:2500});
state.handleEvent({event:'audio.segment', callId:'call-a', callfrom:'8001', callto:'8002', speaker:'caller', audioUrl:'/recordings/b.wav', audioDurationMs:1200, startTimeMs:1300, endTimeMs:2500});
const conv = state.conversations()[0];
assert.equal(conv.turns[0].audioUrl, '/recordings/a.wav');
assert.equal(conv.turns[0].audioDurationMs, 1200);
assert.equal(conv.turns[1].audioUrl, '/recordings/b.wav');
console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert [turn["audioUrl"] for turn in result["turns"]] == ["/recordings/a.wav", "/recordings/b.wav"]


def test_audio_segment_attaches_to_oldest_unmatched_turn_for_same_stream():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'第一问。', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'第一答。', startTimeMs:1100, endTimeMs:2000});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'第一问。第二问。', startTimeMs:0, endTimeMs:3000});
state.handleEvent({event:'audio.segment', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', audioUrl:'/recordings/q1.wav', audioDurationMs:1000, startTimeMs:0, endTimeMs:1000});
const conv = state.conversations()[0];
assert.equal(conv.turns[0].audioUrl, '/recordings/q1.wav');
assert.equal(conv.turns[2].audioUrl, null);
console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert result["turns"][0]["audioUrl"] == "/recordings/q1.wav"
    assert result["turns"][2]["audioUrl"] is None


def test_segment_id_binds_audio_to_exact_turn_even_when_audio_arrives_out_of_order():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'agent-stream', segmentId:'agent-0001', callfrom:'8001', callto:'8002', speaker:'agent', text:'第一问。', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'speech.final', callId:'caller-stream', segmentId:'caller-0001', callfrom:'8001', callto:'8002', speaker:'caller', text:'第一答。', startTimeMs:1100, endTimeMs:2000});
state.handleEvent({event:'speech.final', callId:'agent-stream', segmentId:'agent-0002', callfrom:'8001', callto:'8002', speaker:'agent', text:'第二问。', startTimeMs:2100, endTimeMs:3000});
state.handleEvent({event:'audio.segment', callId:'agent-stream', segmentId:'agent-0002', callfrom:'8001', callto:'8002', speaker:'agent', audioUrl:'/recordings/q2.wav', audioDurationMs:900, startTimeMs:2100, endTimeMs:3000});
state.handleEvent({event:'audio.segment', callId:'agent-stream', segmentId:'agent-0001', callfrom:'8001', callto:'8002', speaker:'agent', audioUrl:'/recordings/q1.wav', audioDurationMs:1000, startTimeMs:0, endTimeMs:1000});
const conv = state.conversations()[0];
assert.equal(conv.turns[0].audioUrl, '/recordings/q1.wav');
assert.equal(conv.turns[2].audioUrl, '/recordings/q2.wav');
console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert [result["turns"][0]["audioUrl"], result["turns"][2]["audioUrl"]] == [
        "/recordings/q1.wav",
        "/recordings/q2.wav",
    ]



def test_stable_final_splits_late_progressive_segments_by_segment_id():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'agent-stream', segmentId:'agent-0003', callfrom:'8001', callto:'8002', speaker:'agent', text:'哦那你那边还有什么其他情况吗', startTimeMs:0, endTimeMs:1200, sendTimeMs:100});
state.handleEvent({event:'speech.final', callId:'agent-stream', segmentId:'agent-0004', callfrom:'8001', callto:'8002', speaker:'agent', text:'嗯您注意观察到附近有什么危险设施或者是一些什么化学厂之类的东西吗', startTimeMs:2500, endTimeMs:5200, sendTimeMs:200});
let conv = state.conversations()[0];
assert.equal(conv.turns.length, 1);
assert.equal(conv.turns[0].text, '哦那你那边还有什么其他情况吗 嗯您注意观察到附近有什么危险设施或者是一些什么化学厂之类的东西吗');

state.handleEvent({event:'speech.final', finalSource:'turn-complete-streaming-fallback', callId:'agent-stream', segmentId:'agent-0003', segmentIds:['agent-0003'], callfrom:'8001', callto:'8002', speaker:'agent', text:'哦那你那边还有什么其他情况吗', startTimeMs:0, endTimeMs:1200, sendTimeMs:1000});
state.handleEvent({event:'speech.final', finalSource:'turn-complete-streaming-fallback', callId:'agent-stream', segmentId:'agent-0004', segmentIds:['agent-0004'], callfrom:'8001', callto:'8002', speaker:'agent', text:'嗯您注意观察到附近有什么危险设施或者是一些什么化学厂之类的东西吗', startTimeMs:2500, endTimeMs:5200, sendTimeMs:1100});
conv = state.conversations()[0];
assert.equal(conv.turns.length, 2);
assert.deepEqual(conv.turns.map(t => t.text), [
  '哦那你那边还有什么其他情况吗',
  '嗯您注意观察到附近有什么危险设施或者是一些什么化学厂之类的东西吗',
]);
assert.deepEqual(conv.turns.map(t => t.segmentId), ['agent-0003', 'agent-0004']);
assert.deepEqual(conv.turns.map(t => t.locked), [true, true]);
console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert [turn["text"] for turn in result["turns"]] == [
        "哦那你那边还有什么其他情况吗",
        "嗯您注意观察到附近有什么危险设施或者是一些什么化学厂之类的东西吗",
    ]


def test_stable_update_reorders_existing_turn_by_start_time():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});

state.handleEvent({event:'speech.final', callId:'caller-stream', segmentId:'caller-0002', callfrom:'8001', callto:'8002', speaker:'caller', text:'后来的报警描述', startTimeMs:13000, endTimeMs:16000, sendTimeMs:100});
state.handleEvent({event:'speech.final', callId:'agent-stream', segmentId:'agent-0001', callfrom:'8001', callto:'8002', speaker:'agent', text:'接警问询', startTimeMs:15000, endTimeMs:18000, sendTimeMs:200});
state.handleEvent({event:'speech.final', callId:'caller-stream', segmentId:'caller-0001', callfrom:'8001', callto:'8002', speaker:'caller', text:'更早的报警描述', startTimeMs:3000, endTimeMs:6000, sendTimeMs:300});

let conv = state.conversations()[0];
assert.deepEqual(conv.turns.map(t => t.segmentId), ['caller-0002', 'agent-0001', 'caller-0001']);

state.handleEvent({event:'speech.final', finalSource:'turn-complete-streaming-fallback', callId:'caller-stream', segmentId:'caller-0001', segmentIds:['caller-0001'], callfrom:'8001', callto:'8002', speaker:'caller', text:'更早的报警描述', startTimeMs:3000, endTimeMs:6000, sendTimeMs:1000});
conv = state.conversations()[0];
assert.deepEqual(conv.turns.map(t => t.segmentId), ['caller-0001', 'caller-0002', 'agent-0001']);
assert.deepEqual(conv.turns.map(t => t.startTimeMs), [3000, 13000, 15000]);

console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert [turn["segmentId"] for turn in result["turns"]] == [
        "caller-0001",
        "caller-0002",
        "agent-0001",
    ]

def test_default_switch_stability_is_shorter_than_one_second():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'第一句话。', sendTimeMs:0});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'第二句话。', sendTimeMs:600});
const conv = state.conversations()[0];
assert.equal(conv.turns.length, 2);
console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert len(result["turns"]) == 2


def test_call_corrected_is_stored_on_matching_conversation():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'call-ai', callfrom:'8001', callto:'119'});
state.handleEvent({event:'speech.final', callId:'call-ai', callfrom:'8001', callto:'119', speaker:'caller', text:'我这里发生了活该', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'call.corrected', callId:'call-ai', callfrom:'8001', callto:'119', originalText:'报警人：我这里发生了活该', correctedText:'报警人：我这里发生了火灾', turns:[{segmentId:'caller-0001', speaker:'caller', originalText:'我这里发生了活该', correctedText:'我这里发生了火灾', keywords:['火灾']}], llmElapsedMs:123.4});
const conv = state.conversations()[0];
assert.equal(conv.aiCorrections.length, 1);
assert.equal(conv.aiCorrections[0].callId, 'call-ai');
assert.equal(conv.aiCorrections[0].correctedText, '报警人：我这里发生了火灾');
assert.deepEqual(conv.aiCorrections[0].turns[0].keywords, ['火灾']);
assert.equal(Object.hasOwn(conv.aiCorrections[0], 'keywords'), false);
assert.equal(Object.hasOwn(conv.aiCorrections[0], 'entities'), false);
console.log(JSON.stringify({aiCorrections: conv.aiCorrections}));
"""
    result = run_monitor_turns_script(script)
    assert result["aiCorrections"][0]["correctedText"] == "报警人：我这里发生了火灾"


def test_call_ended_marks_that_call_turns_stable_even_if_other_stream_active():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'我这里发生了火灾', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'speech.final', callId:'agent-stream', callfrom:'8001', callto:'8002', speaker:'agent', text:'请问具体地址在哪里', startTimeMs:1100, endTimeMs:2000});
state.handleEvent({event:'call.ended', callId:'caller-stream', callfrom:'8001', callto:'8002'});
const conv = state.conversations()[0];
const callerTurn = conv.turns.find(t => t.callId === 'caller-stream');
assert.equal(callerTurn.locked, true);
assert.equal(callerTurn.stabilityStatus, 'stable');
assert.equal(conv.status, 'active');
console.log(JSON.stringify({turns: conv.turns, status: conv.status}));
"""
    result = run_monitor_turns_script(script)
    caller_turn = next(t for t in result["turns"] if t["callId"] == "caller-stream")
    assert caller_turn["stabilityStatus"] == "stable"

def test_call_ended_finalizes_active_turn_for_that_call_even_when_peer_stream_active():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:true});
state.handleEvent({event:'call.started', callId:'caller-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'call.started', callId:'agent-stream', callfrom:'8001', callto:'8002'});
state.handleEvent({event:'speech.final', callId:'caller-stream', callfrom:'8001', callto:'8002', speaker:'caller', text:'我这里发生了火灾', startTimeMs:0, endTimeMs:1000});
state.handleEvent({event:'call.ended', callId:'caller-stream', callfrom:'8001', callto:'8002'});
const conv = state.conversations()[0];
const callerTurn = conv.turns.find(t => t.callId === 'caller-stream');
assert.equal(callerTurn.locked, true);
assert.equal(callerTurn.stabilityStatus, 'stable');
assert.equal(callerTurn.vadState, 'ended');
assert.equal(conv.status, 'active');
console.log(JSON.stringify({turns: conv.turns, status: conv.status}));
"""
    result = run_monitor_turns_script(script)
    caller_turn = next(t for t in result["turns"] if t["callId"] == "caller-stream")
    assert caller_turn["locked"] is True
    assert caller_turn["stabilityStatus"] == "stable"
    assert caller_turn["vadState"] == "ended"
    assert result["status"] == "active"

def test_audio_segment_ids_attach_to_turn_when_primary_segment_has_no_text():
    script = r"""
const assert = require('node:assert/strict');
const { createMonitorState } = require('./asr_api_use/web/monitor_turns.js');
const state = createMonitorState({turnSwitchStableMs:0, requireVadForSwitch:false});
state.handleEvent({event:'speech.final', callId:'caller-call', segmentId:'caller-0002', callfrom:'8001', callto:'8002', speaker:'caller', text:'我这里发生了火灾', startTimeMs:1200, endTimeMs:2200});
state.handleEvent({event:'audio.segment', callId:'caller-call', segmentId:'caller-0001', segmentIds:['caller-0001','caller-0002'], callfrom:'8001', callto:'8002', speaker:'caller', audioUrl:'/recordings/combined.wav', audioDurationMs:2200, startTimeMs:0, endTimeMs:2200});
const conv = state.conversations()[0];
assert.equal(conv.turns[0].audioUrl, '/recordings/combined.wav');
assert.deepEqual(conv.turns[0].audioSegments[0].segmentIds, ['caller-0001','caller-0002']);
console.log(JSON.stringify({turns:conv.turns}));
"""
    result = run_monitor_turns_script(script)
    assert result["turns"][0]["audioUrl"] == "/recordings/combined.wav"
