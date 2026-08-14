from pathlib import Path

MONITOR_HTML = Path(__file__).resolve().parents[1] / "web" / "monitor.html"


def test_monitor_html_cache_busts_turn_state_script_and_shows_build_id():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "monitor_turns.js?v=" in html
    assert "monitorBuild" in html
    assert "asr-model-switch-stability-20260722-v2" in html


def test_monitor_html_renders_turn_audio_player():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "function turnAudioHtml" in html
    assert "<audio controls preload=\"metadata\"" in html
    assert "audio/wav" in html


def test_monitor_html_uses_fusion_dashboard_layout():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert 'class="dashboard-shell"' in html
    assert 'class="command-bar"' in html
    assert 'id="callStage"' in html
    assert "transcript-bubble" in html
    assert "speaker-rail" in html


def test_monitor_html_keeps_live_diagnostics_and_custom_audio_controls():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "function toggleAudio" in html
    assert "function bindAudioPlayers" in html
    assert "audio-wave" in html
    assert "stabilityLabel" in html
    assert "vadLabel" in html
    assert "volume-meter" in html


def test_monitor_html_renders_ai_correction_panel():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "function aiCorrectionsHtml" in html
    assert "AI 修正 / RabbitMQ 推送" in html
    assert "call.corrected" in html
    assert "correctedText" in html
    assert "逐段修正 turns" in html
    assert "turn.keywords" in html
    assert "结构化实体 entities" not in html


def test_monitor_html_renders_rabbitmq_raw_panel():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "RabbitMQ 原始消息" in html
    assert "rabbitmq.message" in html
    assert "function rabbitmqPanelHtml" in html


def test_monitor_html_has_per_call_bidirectional_model_switch_controls():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "科大讯飞·贵州话" in html
    assert "function modelSwitchHtml" in html
    assert "function switchCallModel" in html
    assert "targetProvider" in html
    assert "effective:'immediate'" in html
    assert "连接期间缓存音频" in html
    assert "下一语音段切换" not in html
    assert "(!pending&&summary.currentProvider==='funasr')" in html
    assert "(!pending&&summary.currentProvider==='xfyun')" in html
    assert ">FunASR</button>" in html
    assert "当前模型 · FunASR" not in html
    assert "$cards.addEventListener('pointerdown'" in html
    assert "$cards.addEventListener('click'" in html
    assert "event.detail!==0" in html
    assert 'onclick="switchCallModel(this)"' not in html


def test_monitor_reconnect_does_not_force_end_active_calls():
    html = MONITOR_HTML.read_text(encoding="utf-8")
    onopen_block = html.split("ws.onopen=", 1)[1].split("ws.onmessage=", 1)[0]

    assert "clearAll()" not in onopen_block
    assert "asr.model.state" in onopen_block

def test_monitor_html_labels_every_turn_with_recognition_provider():
    html = MONITOR_HTML.read_text(encoding="utf-8")

    assert "function turnProviderBadgeHtml" in html
    assert "provider-tag" in html
    assert "实际识别模型" in html
    assert "FunASR + 科大讯飞（混合识别）" in html
    assert "模型未知" in html
    assert "turnProviderBadgeHtml(t)" in html
