import sys
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from call_state import CallHoldState  # noqa: E402


def test_local_hold_marks_call_id_and_phone_pair_as_held():
    state = CallHoldState()

    change = state.apply_cti_event({
        "eventId": "evt-hold-1",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "eventTime": "2026-07-02 14:20:37 396",
        "ext": {"from": "8015", "to": "8014", "extId": "8014", "callerId": "8015"},
    })

    assert change is not None
    assert change.status == "holding"
    assert state.is_held(call_id="caller-call", callfrom="8015", callto="8014") is True
    assert state.is_held(call_id="agent-call", callfrom="8015", callto="8014") is True
    assert state.is_held(call_id="other-call", callfrom="8015", callto="8016") is False


def test_hold_cancel_releases_call_id_and_phone_pair():
    state = CallHoldState()
    state.apply_cti_event({
        "eventId": "evt-hold-1",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })

    change = state.apply_cti_event({
        "eventId": "evt-cancel-1",
        "eventType": "callHoldCancel",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })

    assert change is not None
    assert change.status == "active"
    assert state.is_held(call_id="caller-call", callfrom="8015", callto="8014") is False
    assert state.is_held(call_id="agent-call", callfrom="8015", callto="8014") is False


def test_duplicate_cti_event_is_ignored():
    state = CallHoldState()
    event = {
        "eventId": "evt-hold-1",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    }

    assert state.apply_cti_event(event) is not None
    assert state.apply_cti_event(event) is None


def test_clear_call_releases_pair_recorded_for_that_call():
    state = CallHoldState()
    state.apply_cti_event({
        "eventId": "evt-hold-1",
        "eventType": "localHoldCall",
        "callId": "caller-call",
        "ext": {"from": "8015", "to": "8014"},
    })

    state.clear_call("caller-call")

    assert state.is_held(call_id="caller-call", callfrom="8015", callto="8014") is False
    assert state.is_held(call_id="agent-call", callfrom="8015", callto="8014") is False
