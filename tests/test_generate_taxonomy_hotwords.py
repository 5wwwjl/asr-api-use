import csv
import sys
from pathlib import Path


ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from generate_taxonomy_hotwords import generate  # noqa: E402


def _write_fake_assets(root: Path) -> None:
    rows = []
    for level_index in range(12):
        for bucket_index in range(6):
            bucket_number = level_index * 6 + bucket_index + 1
            bucket_id = f"ASR-T{level_index + 1:02d}-{bucket_index + 1:02d}"
            relative_file = f"funasr/by_bucket/{bucket_id}.txt"
            path = root / relative_file
            path.parent.mkdir(parents=True, exist_ok=True)
            applicable = bucket_number <= 44
            terms = [f"分类词{bucket_number}", "公共热词"] if applicable else []
            path.write_text("\n".join(terms) + ("\n" if terms else ""), encoding="utf-8")
            rows.append(
                {
                    "level1_id": f"ASR-T{level_index + 1:02d}",
                    "level1_name": f"一级{level_index + 1}",
                    "bucket_id": bucket_id,
                    "bucket_name": f"二级{bucket_number}",
                    "hotword_applicable": "yes" if applicable else "no",
                    "term_count": len(terms),
                    "relative_file": relative_file,
                    "coverage_status": "AVAILABLE" if applicable else "TEXT_UNVERIFIABLE",
                    "note": "",
                }
            )
    with (root / "category_hotword_summary.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_generate_builds_one_deduplicated_table_from_12_by_72_assets(tmp_path):
    asset_root = tmp_path / "assets"
    output_dir = tmp_path / "output"
    _write_fake_assets(asset_root)

    manifest = generate(asset_root, output_dir)

    assert manifest["level1_count"] == 12
    assert manifest["bucket_count"] == 72
    assert manifest["applicable_bucket_count"] == 44
    assert manifest["unique_hotword_count"] == 45
    full_text = (output_dir / "full.txt").read_text(encoding="utf-8")
    assert full_text.count("公共热词\n") == 1
    assert "# 二级分类 ASR-T12-06 二级72; 适用=no" in full_text
    index_rows = list(
        csv.DictReader((output_dir / "category_index.csv").open(encoding="utf-8-sig"))
    )
    assert len(index_rows) == 72
    assert index_rows[0]["contributed_unique_count"] == "2"
    assert index_rows[1]["contributed_unique_count"] == "1"
    level2_rows = list(
        csv.DictReader(
            (output_dir / "level2_table_index.csv").open(encoding="utf-8-sig")
        )
    )
    assert len(level2_rows) == 72
    assert len(list((output_dir / "by_level2").glob("*.txt"))) == 72
    first_table = (output_dir / level2_rows[0]["relative_file"]).read_text(
        encoding="utf-8"
    )
    second_table = (output_dir / level2_rows[1]["relative_file"]).read_text(
        encoding="utf-8"
    )
    assert "分类词1\n" in first_table
    assert "公共热词\n" in first_table
    assert "公共热词\n" in second_table
    assert manifest["level2_table_count"] == 72
