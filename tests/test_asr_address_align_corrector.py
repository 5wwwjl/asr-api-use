import sys
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_address_align_corrector import AsrAddressAlignCorrector, compact_pinyin  # noqa: E402
from asr_address_db import AddressTerm  # noqa: E402


def _corrector():
    return AsrAddressAlignCorrector([
        AddressTerm(term="科苑北路", table="loi_road", column="cn_name"),
        AddressTerm(term="科兴科学园D1栋", table="poi_1", column="building_name"),
        AddressTerm(term="南山科兴科学园", table="aoi_3", column="aoi_name"),
        AddressTerm(term="怡阁小区", table="aoi_3", column="aoi_name"),
        AddressTerm(term="广东省深圳市南山区科兴科学园c栋g层", table="poi_3", column="poi_name"),
    ])


def test_alignment_corrects_science_park_alias_without_adding_characters():
    result = _corrector().correct("我在科兴科雪园门口")

    assert result.corrected == "我在科兴科学园门口"
    assert result.replacements[0]["original"] == "科兴科雪园"
    assert result.replacements[0]["corrected"] == "科兴科学园"


def test_alignment_does_not_add_road_suffix():
    result = _corrector().correct("我在科苑北科兴科雪园门口")

    assert "科苑北路" not in result.corrected


def test_alignment_preserves_building_marker():
    result = _corrector().correct("位置是科信科学园D1栋这边")

    assert result.corrected == "位置是科兴科学园D1栋这边"


def test_alignment_does_not_correct_generic_address_phrase():
    result = _corrector().correct("我在一个小区门口")

    assert result.corrected == "我在一个小区门口"
    assert result.replacements == []


def test_alignment_does_not_consume_particle_after_exact_address():
    corrector = AsrAddressAlignCorrector([
        AddressTerm(term="科兴科学园", table="aoi_3", column="aoi_name"),
        AddressTerm(term="科兴科学园C", table="poi_3", column="poi_name"),
    ])

    result = corrector.correct("你好我在科兴科学园的肯德基")

    assert result.corrected == "你好我在科兴科学园的肯德基"
    assert result.replacements == []


def test_alignment_corrects_address_typo_without_consuming_particle():
    corrector = AsrAddressAlignCorrector([
        AddressTerm(term="科兴科学园", table="aoi_3", column="aoi_name"),
        AddressTerm(term="科兴科学园C", table="poi_3", column="poi_name"),
    ])

    result = corrector.correct("你好我在科兴科雪园的肯德基")

    assert result.corrected == "你好我在科兴科学园的肯德基"
    assert result.replacements[0]["original"] == "科兴科雪园"
    assert result.replacements[0]["corrected"] == "科兴科学园"


def test_alignment_preserves_explicit_alphanumeric_building_marker():
    corrector = AsrAddressAlignCorrector([
        AddressTerm(term="科兴科学园C1栋", table="poi_1", column="building_name"),
    ])

    result = corrector.correct("我在科兴科学园C1栋")

    assert result.corrected == "我在科兴科学园C1栋"
    assert result.replacements == []


def test_contextual_building_homophone_corrects_startup_to_seven_building():
    corrector = AsrAddressAlignCorrector([])

    result = corrector.correct("我在软件园一期启动")

    assert result.corrected == "我在软件园一期七栋"
    assert result.replacements == [{
        "span": [2, 9],
        "original": "软件园一期启动",
        "corrected": "软件园一期七栋",
        "score": 1.0,
        "source": "context_rule",
        "kind": "software_park_phase_seven_building",
        "method": "context_rule",
    }]


def test_contextual_building_homophone_normalizes_park_character_and_7dong():
    corrector = AsrAddressAlignCorrector([])

    result = corrector.correct("我在软件里一期7动")

    assert result.corrected == "我在软件园一期七栋"


def test_contextual_building_homophone_does_not_rewrite_real_startup_phrase():
    corrector = AsrAddressAlignCorrector([])

    result = corrector.correct("设备正在启动")

    assert result.corrected == "设备正在启动"
    assert result.replacements == []


def test_contextual_building_homophone_corrects_standalone_location_answer():
    corrector = AsrAddressAlignCorrector([])

    result = corrector.correct("在启动")

    assert result.corrected == "在七栋"


def test_contextual_building_homophone_corrects_explicit_numeric_dong():
    corrector = AsrAddressAlignCorrector([])

    result = corrector.correct("7动7动")

    assert result.corrected == "七栋七栋"


def test_numeric_pinyin_selects_canonical_seven_building_instead_of_one():
    corrector = AsrAddressAlignCorrector([
        AddressTerm(term="深圳软件园一期1栋", table="poi_3", column="poi_name"),
        AddressTerm(term="深圳软件园一期7栋", table="poi_3", column="poi_name"),
    ])

    result = corrector.correct("我这里是深圳软件园一期七栋")

    assert result.corrected == "我这里是深圳软件园一期7栋"
    assert result.replacements[0]["original"] == "深圳软件园一期七栋"
    assert result.replacements[0]["corrected"] == "深圳软件园一期7栋"


def test_numeric_pinyin_reads_contiguous_digits_as_a_number():
    assert compact_pinyin("7栋") == compact_pinyin("七栋")
    assert compact_pinyin("17栋") == compact_pinyin("十七栋")
