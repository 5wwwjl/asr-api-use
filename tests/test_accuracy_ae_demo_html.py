from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "web" / "accuracy_ae_demo.html"


def test_accuracy_ae_demo_uses_one_microphone_for_both_realtime_paths():
    html = HTML.read_text(encoding="utf-8")

    assert "getUserMedia" in html
    assert "/asr-accuracy-a" in html
    assert "wsUrl('/asr'" in html
    assert "pcm.buffer.slice" in html
    assert "sendE('audio.frame'" in html


def test_accuracy_ae_demo_labels_baseline_and_correction_scope():
    html = HTML.read_text(encoding="utf-8")

    assert "Paraformer-large 原始识别" in html
    assert "不注入热词" in html
    assert "FunASR 分类热词增强" in html
    assert "原识别：" in html
    assert "740 条" in html
    assert "12×72分类静态热词" in html
    assert "GPU Paraformer-large" in html
    assert "实时演示的 A 使用 CPU" not in html
    assert "CPU 运行同权重模型" not in html


def test_accuracy_ae_demo_has_accessible_live_controls():
    html = HTML.read_text(encoding="utf-8")

    assert 'aria-pressed="false"' in html
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html
    assert "prefers-reduced-motion" in html
