from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
FUNASR_SERVER = ASR_DIR / "funasr_server_xhw.py"


def test_funasr_websocket_accepts_unlimited_hotword_handshake():
    source = FUNASR_SERVER.read_text(encoding="utf-8")

    assert "max_size=None" in source


def test_funasr_inference_does_not_block_websocket_event_loop():
    source = FUNASR_SERVER.read_text(encoding="utf-8")

    assert "inference_lock = asyncio.Lock()" in source
    assert "await asyncio.to_thread(model.generate, **generate_kwargs)" in source


def test_funasr_drops_obsolete_partial_work_instead_of_building_a_queue():
    source = FUNASR_SERVER.read_text(encoding="utf-8")

    assert "partial_result_count > 0" in source
    assert "deferred_partial_count < MAX_DEFERRED_PARTIALS" in source
    assert "and inference_lock.locked()" in source
    assert "deferred_partial_count" in source


def test_funasr_finish_message_flushes_and_marks_the_final_result():
    source = FUNASR_SERVER.read_text(encoding="utf-8")

    assert "was_speaking and not is_speaking" in source
    assert '"mode": "streaming" if is_speaking else "2pass-offline"' in source
    assert '"is_final": not is_speaking' in source
