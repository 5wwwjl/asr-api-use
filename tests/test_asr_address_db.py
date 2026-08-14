import sys
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_address_db import (  # noqa: E402
    AddressDatabaseSource,
    AddressTerm,
    address_term_weight,
    create_address_database_source,
    parse_address_field_spec,
)


class FakeCursor:
    def __init__(self, rows_by_field):
        self.rows_by_field = rows_by_field
        self.executed = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        table = sql.split('FROM "ai"."', 1)[1].split('"', 1)[0]
        column = sql.split("SELECT DISTINCT trim(", 1)[1].split(")", 1)[0]
        self._rows = [(value,) for value in self.rows_by_field.get((table, column), [])]

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj


def test_parse_address_field_spec_uses_table_and_column_pairs():
    fields = parse_address_field_spec("poi_1.building_name, loi_road.cn_name")

    assert [(field.table, field.column) for field in fields] == [
        ("poi_1", "building_name"),
        ("loi_road", "cn_name"),
    ]


def test_database_source_loads_splits_filters_and_deduplicates_terms():
    cursor = FakeCursor({
        ("poi_1", "building_name"): [
            "科兴科学园",
            "万基产业园|2栋",
            "[]",
            "A座",
        ],
        ("loi_road", "cn_name"): [
            "科苑北路",
            "科兴科学园",
        ],
    })
    source = AddressDatabaseSource(
        enabled=True,
        host="db",
        database="ods7alm",
        user="tgms",
        password="secret",
        schema="ai",
        fields=parse_address_field_spec("poi_1.building_name,loi_road.cn_name"),
        min_term_length=3,
    )
    source._connect = lambda: FakeConnection(cursor)

    terms = source.load_terms()

    assert terms == [
        AddressTerm(term="科兴科学园", table="poi_1", column="building_name"),
        AddressTerm(term="万基产业园", table="poi_1", column="building_name"),
        AddressTerm(term="科苑北路", table="loi_road", column="cn_name"),
    ]
    first_sql, _ = cursor.executed[0]
    assert 'FROM "ai"."poi_1"' in first_sql
    assert "is_deleted = false" in first_sql


def test_address_term_weight_prioritizes_structured_address_fields():
    road = AddressTerm(term="科苑北路", table="loi_road", column="cn_name")
    aoi = AddressTerm(term="南山科兴科学园", table="aoi_3", column="aoi_name")
    building = AddressTerm(term="科兴科学园A栋", table="poi_1", column="building_name")
    poi = AddressTerm(term="某某店", table="poi_3", column="poi_name")

    assert address_term_weight(road) > address_term_weight(poi)
    assert address_term_weight(aoi) > address_term_weight(poi)
    assert address_term_weight(building) > address_term_weight(poi)


def test_factory_reads_address_database_environment(monkeypatch):
    monkeypatch.setenv("ASR_ADDRESS_DB_ENABLED", "true")
    monkeypatch.setenv("ASR_ADDRESS_DB_SCHEMA", "ai")
    monkeypatch.setenv("ASR_ADDRESS_DB_FIELDS", "aoi_3.aoi_name,poi_3.poi_name")
    monkeypatch.setenv("ASR_ADDRESS_DB_MIN_TERM_LENGTH", "4")
    monkeypatch.setenv("ASR_DB_HOST", "192.168.173.198")
    monkeypatch.setenv("ASR_DB_PORT", "54321")
    monkeypatch.setenv("ASR_DB_NAME", "ods7alm")
    monkeypatch.setenv("ASR_DB_USER", "tgms")
    monkeypatch.setenv("ASR_DB_PASS", "secret")

    source = create_address_database_source()

    assert source.enabled is True
    assert source.host == "192.168.173.198"
    assert source.port == 54321
    assert source.database == "ods7alm"
    assert source.schema == "ai"
    assert source.min_term_length == 4
    assert [(field.table, field.column) for field in source.fields] == [
        ("aoi_3", "aoi_name"),
        ("poi_3", "poi_name"),
    ]
