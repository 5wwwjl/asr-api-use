from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
SERVER = ASR_DIR / "funasr_server_xhw.py"
START_SCRIPT = ASR_DIR / "start_accuracy_raw_models.sh"


def test_server_can_disable_vad_for_raw_accuracy_evaluation():
    source = SERVER.read_text(encoding="utf-8")

    assert 'parser.add_argument("--disable_vad"' in source
    assert 'if not args.disable_vad:' in source
    assert '"vad_model": args.vad_model' in source
    assert '"merge_vad": True' in source


def test_raw_accuracy_models_are_isolated_from_online_ports():
    source = START_SCRIPT.read_text(encoding="utf-8")

    assert "funasr-accuracy-raw-a" in source
    assert "funasr-accuracy-raw-c" in source
    assert "10101" in source
    assert "10102" in source
    assert "--disable_vad" in source
    assert 'A_PORT="${ASR_RAW_A_PORT:-10101}"' in source
    assert 'C_PORT="${ASR_RAW_C_PORT:-10102}"' in source
    assert 'PRODUCTION_CONTAINER=' not in source
