from pathlib import Path

HOTWORD_DEMO = Path(__file__).resolve().parents[1] / "web" / "hotword_demo.html"


def test_hotword_demo_records_through_live_8443_bridge():
    html = HOTWORD_DEMO.read_text(encoding="utf-8")

    assert "开始双路实时转写" in html
    assert "wss://${location.hostname}:8443/${endpoint}" in html
    assert "'asr-plain':'asr-dynamic'" in html
    assert "hotwordMode=off" not in html
    assert "call.started" in html
    assert "audio.frame" in html
    assert "call.ended" in html
    assert "scene_signal.add" in html
    assert "8443 双路实时转写中" in html
    assert "暂不启用后置纠错" in html
    assert "普通 ASR 原始转写" in html
    assert "动态热词 ASR 原始转写" in html
    assert "未纠错" in html
    assert "hotword_demo_transcript.js?v=20260810-2" in html
    assert "diff-ordinary" in html
    assert "diff-dynamic" in html
    assert "真实基站定位" in html
    assert "加载真实基站地址" in html
    assert "167真实地址库" in html
    assert "可直接朗读" in html
    assert "当前加载的热词明细" in html
    assert 'id="hotwordSearch"' in html
    assert 'id="hotwordList"' in html
    assert "handshakeTokenCount" in html
    assert "eventId:'latest'" not in html
    assert "模拟基站定位消息" not in html
    assert 'id="audioFile"' not in html
    assert 'id="compareBtn"' not in html


def test_hotword_demo_keeps_both_live_routes_isolated_and_rebinds_location():
    html = HOTWORD_DEMO.read_text(encoding="utf-8")

    # Each route is an independent bridge stream. A shared sequence produces
    # a false gap on every other audio frame at the gateway.
    assert "seq:0,generation:0" in html
    assert "live.seq+=1" in html
    assert "state.seq" not in html

    # A stale socket must never clear a newer connection, and stop/restart
    # must wait until both old sockets have actually been closed.
    assert "live.ws!==ws||live.generation!==generation" in html
    assert "await Promise.all(LIVE_CHANNELS.map" in html
    assert "await closeAllLiveSockets(0)" in html

    # The real address scope is call-scoped, so every recording run must ask
    # the real location endpoint for a fresh dynamic callId.
    assert "prepareDynamicCallId" in html
    assert "state.addressReady" in html
    assert "state.addressCallReady" in html
