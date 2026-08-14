import sys
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

import pytest

from hotword_manager import (  # noqa: E402
    HotwordManager,
    HotwordStage,
    InvalidSceneSignal,
)


SCENE_HOTWORD_DIR = Path(
    "/home/twai/wjl/DynamicHotwordLoading/hotwords"
)


def write_hotwords(root: Path):
    (root / "scenes").mkdir(parents=True)
    (root / "address.txt").write_text("科兴科学园 38\n科苑北路\n", encoding="utf-8")
    (root / "inquiry_fire_base.txt").write_text("着火 20\n冒烟\n", encoding="utf-8")
    (root / "scenes" / "highrise.txt").write_text("高层建筑\n超高层\n", encoding="utf-8")
    (root / "scenes" / "crowded_place.txt").write_text("商场\n学校\n", encoding="utf-8")
    (root / "scenes" / "chemical.txt").write_text("液化气\n危化品\n", encoding="utf-8")
    (root / "scenes" / "elevator.txt").write_text("电梯困人\n卡住\n", encoding="utf-8")


def test_addressbot_starts_with_address_hotwords(tmp_path):
    write_hotwords(tmp_path)

    manager = HotwordManager(project="addressbot", hotword_dir=tmp_path, mode="dynamic")

    assert manager.stage == HotwordStage.ADDRESS
    assert manager.current_hotwords() == "科兴科学园 科苑北路"


def test_firebot_starts_with_inquiry_base_hotwords(tmp_path):
    write_hotwords(tmp_path)

    manager = HotwordManager(project="firebot", hotword_dir=tmp_path, mode="dynamic")

    assert manager.stage == HotwordStage.INQUIRY_BASE
    assert manager.current_hotwords() == "着火 冒烟"


def test_off_mode_has_no_hotwords(tmp_path):
    write_hotwords(tmp_path)
    manager = HotwordManager(project="firebot", hotword_dir=tmp_path, mode="off")

    assert manager.current_hotwords() == ""
    assert manager.hotword_count == 0


def test_stage_event_switches_from_address_to_inquiry_base(tmp_path):
    write_hotwords(tmp_path)
    manager = HotwordManager(project="addressbot", hotword_dir=tmp_path, mode="dynamic")

    changed = manager.apply_event({
        "eventType": "stage.changed",
        "stage": "inquiry_base",
    })

    assert changed is True
    assert manager.stage == HotwordStage.INQUIRY_BASE
    assert manager.current_hotwords() == "着火 冒烟"


def test_hotword_switch_event_accepts_scene_name(tmp_path):
    write_hotwords(tmp_path)
    manager = HotwordManager(project="firebot", hotword_dir=tmp_path, mode="dynamic")

    changed = manager.apply_event({
        "eventType": "asr.hotwords.switch",
        "scene": "highrise",
    })

    assert changed is True
    assert manager.stage == HotwordStage.HIGHRISE
    assert manager.current_hotwords() == "高层建筑 超高层"


def test_scene_detection_switches_next_hotwords(tmp_path):
    write_hotwords(tmp_path)
    manager = HotwordManager(project="firebot", hotword_dir=tmp_path, mode="dynamic")

    changed = manager.update_from_recognized_text("二十多楼的高层建筑正在冒烟")

    assert changed is True
    assert manager.stage == HotwordStage.HIGHRISE
    assert manager.current_hotwords() == "高层建筑 超高层"


def test_unknown_stage_keeps_current_hotwords(tmp_path):
    write_hotwords(tmp_path)
    manager = HotwordManager(project="firebot", hotword_dir=tmp_path, mode="dynamic")

    changed = manager.apply_event({
        "eventType": "asr.hotwords.switch",
        "stage": "not-a-stage",
    })

    assert changed is False
    assert manager.stage == HotwordStage.INQUIRY_BASE
    assert manager.current_hotwords() == "着火 冒烟"


def test_full_mode_uses_same_full_table_for_every_stage(tmp_path):
    write_hotwords(tmp_path)
    full_file = tmp_path / "full.txt"
    full_file.write_text("科兴科学园\n灾害类型\n", encoding="utf-8")
    manager = HotwordManager(
        project="addressbot",
        hotword_dir=tmp_path,
        full_hotword_file=full_file,
        mode="full",
    )

    assert manager.current_hotwords() == "科兴科学园 灾害类型"

    changed = manager.apply_event({"eventType": "stage.changed", "stage": "inquiry_base"})

    assert changed is True
    assert manager.stage == HotwordStage.INQUIRY_BASE
    assert manager.current_hotwords() == "科兴科学园 灾害类型"


def test_full_mode_falls_back_to_dynamic_table_when_full_table_is_empty(tmp_path):
    write_hotwords(tmp_path)
    full_file = tmp_path / "full.txt"
    full_file.write_text("# no terms\n", encoding="utf-8")
    manager = HotwordManager(
        project="firebot",
        hotword_dir=tmp_path,
        full_hotword_file=full_file,
        mode="full",
    )

    assert manager.current_hotwords() == "着火 冒烟"


def test_scene_dynamic_starts_with_baseline_and_classification_assist():
    manager = HotwordManager(
        mode="scene_dynamic", scene_hotword_dir=SCENE_HOTWORD_DIR
    )

    assert manager.library_ids == (
        "baseline",
        "classification_assist.call_type",
    )
    assert manager.hotword_count == 413
    assert manager.hotword_version == 1
    assert "报警" in manager.current_hotwords().split()


def test_scene_dynamic_call_type_replaces_classification_assist():
    manager = HotwordManager(
        mode="scene_dynamic", scene_hotword_dir=SCENE_HOTWORD_DIR
    )

    changed = manager.apply_event(
        {
            "eventType": "scene_signal.add",
            "signals": {"call_type": ["fire_fighting"]},
        }
    )

    assert changed is True
    assert manager.hotword_version == 2
    assert "classification_assist.call_type" not in manager.library_ids
    assert "call_type.fire_fighting" in manager.library_ids
    assert "煤气罐" in manager.current_hotwords().split()


def test_scene_dynamic_accumulates_usage_and_structure_libraries():
    manager = HotwordManager(
        mode="scene_dynamic", scene_hotword_dir=SCENE_HOTWORD_DIR
    )

    manager.apply_event(
        {
            "type": "scene_signal.add",
            "signals": {"building_usage": ["crowded_place"]},
        }
    )
    manager.apply_event(
        {
            "eventType": "scene_signal.add",
            "signals": {
                "building_structure": ["highrise_multistory"]
            },
        }
    )

    assert "building_usage.crowded_place" in manager.library_ids
    assert "building_structure.highrise_multistory" in manager.library_ids
    assert manager.hotword_version == 3


def test_scene_dynamic_repeated_signal_is_idempotent():
    manager = HotwordManager(
        mode="scene_dynamic", scene_hotword_dir=SCENE_HOTWORD_DIR
    )
    event = {
        "eventType": "scene_signal.add",
        "signals": {"call_type": ["social_assistance"]},
    }

    assert manager.apply_event(event) is True
    version = manager.hotword_version
    assert manager.apply_event(event) is False
    assert manager.hotword_version == version


def test_scene_dynamic_invalid_signal_keeps_current_snapshot():
    manager = HotwordManager(
        mode="scene_dynamic", scene_hotword_dir=SCENE_HOTWORD_DIR
    )
    original = manager.current_hotwords()

    with pytest.raises(InvalidSceneSignal, match="unknown"):
        manager.apply_event(
            {
                "eventType": "scene_signal.add",
                "signals": {"building_structure": ["unknown"]},
            }
        )

    assert manager.current_hotwords() == original
    assert manager.hotword_version == 1


def test_scene_dynamic_uses_stable_global_limit():
    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir=SCENE_HOTWORD_DIR,
        max_hotwords=10,
        warning_threshold=8,
    )

    assert manager.hotword_count == 10
    assert manager.warning_threshold_reached is True
    assert manager.truncated is True


def test_scene_dynamic_default_limits_address_words_to_1000():
    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir=SCENE_HOTWORD_DIR,
        max_hotwords=0,
    )
    address_words = {f"演示地址{i:04d}": 99 for i in range(2000)}

    manager.set_address_hotwords(
        scope_id="scope-large",
        inventory_version="v1",
        hotwords=address_words,
    )
    manager.apply_event({
        "eventType": "scene_signal.add",
        "signals": {
            "call_type": ["fire_fighting"],
            "building_structure": ["highrise_multistory"],
        },
    })

    selected = set(manager.current_hotwords().split())
    assert "着火" in selected
    assert "仓库冒烟" in selected
    assert "高层建筑" in selected
    assert len(selected & address_words.keys()) == 1000
    assert manager.hotword_count > 1000
    assert manager.truncated is False


def test_scene_dynamic_address_limit_does_not_limit_scene_words():
    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir=SCENE_HOTWORD_DIR,
        max_hotwords=0,
        max_address_hotwords=1,
    )

    manager.set_address_hotwords(
        scope_id="scope-small",
        inventory_version="v1",
        hotwords={"低权重地址": 10, "高权重地址": 20},
    )
    manager.apply_event({
        "eventType": "scene_signal.add",
        "signals": {"call_type": ["fire_fighting"]},
    })

    selected = set(manager.current_hotwords().split())
    assert "高权重地址" in selected
    assert "低权重地址" not in selected
    assert "着火" in selected
    assert "仓库冒烟" in selected


def test_scene_dynamic_catalog_failure_falls_back_to_full_file(tmp_path):
    full_file = tmp_path / "full.txt"
    full_file.write_text("回退热词\n", encoding="utf-8")

    manager = HotwordManager(
        mode="scene_dynamic",
        scene_hotword_dir=tmp_path / "missing",
        full_hotword_file=full_file,
    )

    assert manager.library_ids == ()
    assert manager.current_hotwords() == "回退热词"
