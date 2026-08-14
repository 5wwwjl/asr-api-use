#!/usr/bin/env python3
"""Generate one deployable hotword table from the 12/72 ASR taxonomy assets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ASSET_ROOT = Path(
    "/home/twai/xhw/119_robot_analysis/reports/asr_hotword_assets"
)
DEFAULT_OUTPUT_DIR = BASE_DIR / "hotwords_taxonomy_12x72"
EXPECTED_LEVEL1_COUNT = 12
EXPECTED_BUCKET_COUNT = 72


@dataclass(frozen=True)
class TaxonomyBucket:
    level1_id: str
    level1_name: str
    bucket_id: str
    bucket_name: str
    applicable: bool
    source_term_count: int
    source_file: Path
    coverage_status: str
    note: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_terms(path: Path) -> list[str]:
    terms: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        term = raw_line.strip()
        if not term or term.startswith("#"):
            continue
        if any(char.isspace() for char in term):
            raise ValueError(f"hotword contains whitespace: {term!r} ({path})")
        if len(term) > 32:
            raise ValueError(f"hotword exceeds 32 characters: {term!r} ({path})")
        terms.append(term)
    return list(dict.fromkeys(terms))


def load_taxonomy(asset_root: Path) -> list[TaxonomyBucket]:
    asset_root = asset_root.resolve()
    summary_path = asset_root / "category_hotword_summary.csv"
    with summary_path.open(encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.DictReader(handle))

    if len(raw_rows) != EXPECTED_BUCKET_COUNT:
        raise ValueError(
            f"expected {EXPECTED_BUCKET_COUNT} taxonomy buckets, got {len(raw_rows)}"
        )
    level1_ids = {row["level1_id"] for row in raw_rows}
    if len(level1_ids) != EXPECTED_LEVEL1_COUNT:
        raise ValueError(
            f"expected {EXPECTED_LEVEL1_COUNT} level-1 categories, got {len(level1_ids)}"
        )
    bucket_ids = [row["bucket_id"] for row in raw_rows]
    if len(bucket_ids) != len(set(bucket_ids)):
        raise ValueError("taxonomy contains duplicate bucket_id values")

    buckets: list[TaxonomyBucket] = []
    for row in raw_rows:
        source_file = (asset_root / row["relative_file"]).resolve()
        if not source_file.is_relative_to(asset_root):
            raise ValueError(f"taxonomy source escapes asset root: {source_file}")
        if not source_file.is_file():
            raise FileNotFoundError(source_file)
        terms = _read_terms(source_file)
        declared_count = int(row["term_count"])
        if declared_count != len(terms):
            raise ValueError(
                f"term count mismatch for {row['bucket_id']}: "
                f"declared={declared_count}, actual={len(terms)}"
            )
        applicable = row["hotword_applicable"].strip().lower() == "yes"
        if not applicable and terms:
            raise ValueError(
                f"non-lexical bucket unexpectedly contains hotwords: {row['bucket_id']}"
            )
        buckets.append(
            TaxonomyBucket(
                level1_id=row["level1_id"],
                level1_name=row["level1_name"],
                bucket_id=row["bucket_id"],
                bucket_name=row["bucket_name"],
                applicable=applicable,
                source_term_count=declared_count,
                source_file=source_file,
                coverage_status=row["coverage_status"],
                note=row["note"],
            )
        )
    return buckets


def build_table(
    buckets: list[TaxonomyBucket],
) -> tuple[list[str], list[dict[str, object]]]:
    terms: list[str] = []
    seen: set[str] = set()
    index_rows: list[dict[str, object]] = []
    for bucket in buckets:
        bucket_terms = _read_terms(bucket.source_file)
        contributed: list[str] = []
        for term in bucket_terms:
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
            contributed.append(term)
        index_rows.append(
            {
                "level1_id": bucket.level1_id,
                "level1_name": bucket.level1_name,
                "bucket_id": bucket.bucket_id,
                "bucket_name": bucket.bucket_name,
                "hotword_applicable": "yes" if bucket.applicable else "no",
                "source_term_count": len(bucket_terms),
                "contributed_unique_count": len(contributed),
                "overlap_count": len(bucket_terms) - len(contributed),
                "coverage_status": bucket.coverage_status,
                "note": bucket.note,
            }
        )
    return terms, index_rows


def render_table(
    buckets: list[TaxonomyBucket],
    terms: list[str],
    index_rows: list[dict[str, object]],
    *,
    asset_root: Path,
) -> str:
    lines = [
        "# 消防接警ASR 12个一级分类/72个二级分类热词表",
        "# 由 generate_taxonomy_hotwords.py 生成，请勿手工编辑。",
        f"# 分类资产来源: {asset_root.resolve()}",
        f"# 一级分类: {len({bucket.level1_id for bucket in buckets})}",
        f"# 二级分类: {len(buckets)}",
        f"# 全局去重热词: {len(terms)}",
        "# 说明: 数字、否定、方言、噪声、多人等非静态词汇桶保留索引但不造词。",
        "",
    ]
    seen: set[str] = set()
    current_level1 = ""
    for bucket, index_row in zip(buckets, index_rows, strict=True):
        if bucket.level1_id != current_level1:
            current_level1 = bucket.level1_id
            lines.extend(
                ["", f"# 一级分类 {bucket.level1_id} {bucket.level1_name}"]
            )
        lines.append(
            f"# 二级分类 {bucket.bucket_id} {bucket.bucket_name}; "
            f"适用={index_row['hotword_applicable']}; "
            f"分类词数={index_row['source_term_count']}; "
            f"本表新增={index_row['contributed_unique_count']}"
        )
        for term in _read_terms(bucket.source_file):
            if term in seen:
                continue
            seen.add(term)
            lines.append(term)
    lines.append("")
    return "\n".join(lines)


def render_bucket_table(bucket: TaxonomyBucket) -> str:
    terms = _read_terms(bucket.source_file)
    lines = [
        f"# {bucket.bucket_id} {bucket.bucket_name}",
        f"# 一级分类: {bucket.level1_id} {bucket.level1_name}",
        f"# 适用: {'yes' if bucket.applicable else 'no'}",
        f"# 热词数: {len(terms)}",
        "# 由 generate_taxonomy_hotwords.py 生成，请勿手工编辑。",
        "",
        *terms,
        "",
    ]
    return "\n".join(lines)


def generate(asset_root: Path, output_dir: Path) -> dict[str, object]:
    asset_root = asset_root.resolve()
    output_dir = output_dir.resolve()
    buckets = load_taxonomy(asset_root)
    terms, index_rows = build_table(buckets)
    rendered = render_table(
        buckets, terms, index_rows, asset_root=asset_root
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    table_path = output_dir / "full.txt"
    table_path.write_text(rendered, encoding="utf-8")

    index_path = output_dir / "category_index.csv"
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(index_rows[0]))
        writer.writeheader()
        writer.writerows(index_rows)

    level2_dir = output_dir / "by_level2"
    level2_dir.mkdir(parents=True, exist_ok=True)
    level2_table_rows: list[dict[str, object]] = []
    for bucket in buckets:
        rendered_bucket = render_bucket_table(bucket)
        relative_path = Path("by_level2") / f"{bucket.bucket_id}.txt"
        table_path = output_dir / relative_path
        table_path.write_text(rendered_bucket, encoding="utf-8")
        level2_table_rows.append(
            {
                "level1_id": bucket.level1_id,
                "level1_name": bucket.level1_name,
                "bucket_id": bucket.bucket_id,
                "bucket_name": bucket.bucket_name,
                "hotword_applicable": "yes" if bucket.applicable else "no",
                "term_count": bucket.source_term_count,
                "relative_file": relative_path.as_posix(),
                "sha256": _sha256_bytes(rendered_bucket.encode("utf-8")),
                "coverage_status": bucket.coverage_status,
                "note": bucket.note,
            }
        )
    level2_index_path = output_dir / "level2_table_index.csv"
    with level2_index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(level2_table_rows[0]))
        writer.writeheader()
        writer.writerows(level2_table_rows)

    summary_source = asset_root / "category_hotword_summary.csv"
    manifest = {
        "generator": str(Path(__file__).resolve()),
        "asset_root": str(asset_root),
        "source_category_summary_sha256": _sha256_bytes(summary_source.read_bytes()),
        "level1_count": len({bucket.level1_id for bucket in buckets}),
        "bucket_count": len(buckets),
        "applicable_bucket_count": sum(bucket.applicable for bucket in buckets),
        "nonempty_bucket_count": sum(bucket.source_term_count > 0 for bucket in buckets),
        "level2_table_count": len(level2_table_rows),
        "level2_table_index_sha256": _sha256_bytes(level2_index_path.read_bytes()),
        "unique_hotword_count": len(terms),
        "full_txt_sha256": _sha256_bytes(rendered.encode("utf-8")),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    manifest = generate(args.asset_root, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
