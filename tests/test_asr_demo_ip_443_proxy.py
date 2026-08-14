from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
NGINX_CONFIG = ASR_DIR / "docker" / "asr-demo-ip-443.conf"
START_SCRIPT = ASR_DIR / "start_asr_demo_ip_443.sh"


def test_ip_443_proxy_exposes_only_demo_and_realtime_asr_routes():
    config = NGINX_CONFIG.read_text(encoding="utf-8")

    assert "listen 443 ssl;" in config
    assert "server_name 192.168.173.167;" in config
    assert "location = /accuracy_ae_demo.html" in config
    assert "location = /accuracy_ae_transcript.js" in config
    assert "location = /asr-accuracy-a" in config
    assert "location = /asr" in config
    assert "location /" in config
    assert "return 404;" in config


def test_ip_443_proxy_uses_ip_san_certificate_and_persistent_container():
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert "asr-ip-192.168.173.167.pem" in script
    assert "asr-ip-192.168.173.167.key" in script
    assert "--restart unless-stopped" in script
    assert "--network host" in script
