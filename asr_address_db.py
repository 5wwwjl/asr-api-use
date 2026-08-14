"""Load ASR address hotwords from PostgreSQL tables."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Iterable

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SPLIT_RE = re.compile(r"[|、,，;；/]+")

DEFAULT_ADDRESS_FIELD_SPEC = ",".join([
    "aoi_2.aoi_name",
    "aoi_2.alias_name",
    "aoi_3.aoi_name",
    "aoi_3_entrance_exit.name",
    "loi_road.cn_name",
    "poi_1.building_name",
    "poi_1.short_name",
    "poi_1.aoi_name",
    "poi_1_entrance_exit.name",
    "poi_3.poi_name",
])


def _load_local_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass(frozen=True)
class AddressDbField:
    table: str
    column: str


@dataclass(frozen=True)
class AddressTerm:
    term: str
    table: str
    column: str


ADDRESS_FIELD_WEIGHTS = {
    ("loi_road", "cn_name"): 3.0,
    ("aoi_3", "aoi_name"): 2.8,
    ("poi_1", "building_name"): 2.7,
    ("aoi_2", "aoi_name"): 2.4,
    ("aoi_2", "alias_name"): 2.4,
    ("poi_1", "short_name"): 2.3,
    ("poi_1", "aoi_name"): 2.2,
    ("aoi_3_entrance_exit", "name"): 2.1,
    ("poi_1_entrance_exit", "name"): 2.0,
    ("poi_3", "poi_name"): 0.8,
}


def address_term_weight(term: AddressTerm) -> float:
    return ADDRESS_FIELD_WEIGHTS.get((term.table, term.column), 1.5)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _validate_identifier(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER_RE.match(text):
        raise ValueError(f"invalid address DB {label}: {value!r}")
    return text


def parse_address_field_spec(value: str) -> list[AddressDbField]:
    fields: list[AddressDbField] = []
    for raw_item in str(value or "").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "." not in item:
            raise ValueError(f"address DB field must be table.column: {item!r}")
        table, column = item.split(".", 1)
        fields.append(AddressDbField(
            table=_validate_identifier(table, "table"),
            column=_validate_identifier(column, "column"),
        ))
    return fields


def _expand_term(value: object, *, min_length: int) -> list[str]:
    text = str(value or "").strip()
    if not text or text == "[]":
        return []
    if _SPLIT_RE.search(text):
        candidates = [part.strip() for part in _SPLIT_RE.split(text)]
    else:
        candidates = [text]

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = candidate.strip()
        if not term or term == "[]" or len(term) < min_length:
            continue
        if term not in seen:
            result.append(term)
            seen.add(term)
    return result


class AddressDatabaseSource:
    def __init__(
        self,
        *,
        enabled: bool = False,
        host: str = "",
        port: int = 5432,
        database: str = "",
        user: str = "",
        password: str = "",
        schema: str = "ai",
        fields: Iterable[AddressDbField] | None = None,
        connect_timeout: int = 5,
        min_term_length: int = 3,
    ):
        self.enabled = enabled
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.schema = _validate_identifier(schema, "schema")
        self.fields = list(fields or parse_address_field_spec(DEFAULT_ADDRESS_FIELD_SPEC))
        self.connect_timeout = connect_timeout
        self.min_term_length = min_term_length

    def _connect(self):
        import psycopg2

        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=self.connect_timeout,
        )

    def load_terms(self) -> list[AddressTerm]:
        if not self.enabled:
            return []

        loaded: list[AddressTerm] = []
        seen_terms: set[str] = set()
        with self._connect() as conn:
            with conn.cursor() as cur:
                for field in self.fields:
                    loaded.extend(self._load_field_terms(cur, field, seen_terms))
        return loaded

    def _load_field_terms(self, cur, field: AddressDbField, seen_terms: set[str]) -> list[AddressTerm]:
        table = _validate_identifier(field.table, "table")
        column = _validate_identifier(field.column, "column")
        sql = (
            f'SELECT DISTINCT trim({column}) '
            f'FROM "{self.schema}"."{table}" '
            f"WHERE {column} IS NOT NULL "
            f"AND trim({column}) <> '' "
            f"AND is_deleted = false "
            f"ORDER BY 1"
        )
        cur.execute(sql, None)
        terms: list[AddressTerm] = []
        for (raw_value,) in cur.fetchall():
            for term in _expand_term(raw_value, min_length=self.min_term_length):
                if term in seen_terms:
                    continue
                seen_terms.add(term)
                terms.append(AddressTerm(term=term, table=table, column=column))
        return terms


def create_address_database_source() -> AddressDatabaseSource:
    _load_local_env()
    fields = parse_address_field_spec(os.getenv("ASR_ADDRESS_DB_FIELDS", DEFAULT_ADDRESS_FIELD_SPEC))
    return AddressDatabaseSource(
        enabled=_env_bool("ASR_ADDRESS_DB_ENABLED", False),
        host=os.getenv("ASR_DB_HOST", "").strip(),
        port=int(os.getenv("ASR_DB_PORT", "5432")),
        database=os.getenv("ASR_DB_NAME", "").strip(),
        user=os.getenv("ASR_DB_USER", "").strip(),
        password=os.getenv("ASR_DB_PASS", ""),
        schema=os.getenv("ASR_ADDRESS_DB_SCHEMA", os.getenv("ASR_DB_SCHEMA", "ai")).strip() or "ai",
        fields=fields,
        connect_timeout=int(os.getenv("ASR_DB_CONNECT_TIMEOUT", "5")),
        min_term_length=int(os.getenv("ASR_ADDRESS_DB_MIN_TERM_LENGTH", "3")),
    )


def _main() -> int:
    source = create_address_database_source()
    terms = source.load_terms()
    payload = {
        "enabled": source.enabled,
        "schema": source.schema,
        "fieldCount": len(source.fields),
        "termCount": len(terms),
        "sample": [asdict(term) for term in terms[:30]],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
