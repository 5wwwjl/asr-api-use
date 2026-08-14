"""REST reader and hotword extractor for a location address scope."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Mapping
from uuid import uuid4

import aiohttp

from asr_address_scope import AddressScopeBinding


LOG = logging.getLogger("asr-address-scope-client")
SOURCE_TYPES = ("BUILDING", "AOI", "POI")


class AddressScopeQueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AddressHotwordResult:
    hotwords: Mapping[str, int]
    items: tuple[Mapping[str, object], ...]
    filtered_hotwords: tuple[Mapping[str, object], ...]
    item_count: int
    poi_count: int
    aoi_count: int
    building_count: int
    alias_count: int
    query_ms: float


class AddressScopeClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
        max_attempts: int | None = None,
        source: str | None = None,
    ) -> None:
        self._source = (source or os.getenv("ASR_ADDRESS_SCOPE_SOURCE", "rest")).strip().lower()
        self._base_url = (base_url or os.getenv(
            "ASR_ADDRESS_SCOPE_BASE_URL", "http://192.168.173.167:18080"
        )).rstrip("/")
        self._timeout_seconds = timeout_seconds or float(
            os.getenv("ASR_ADDRESS_SCOPE_TIMEOUT_SECONDS", "3")
        )
        self._page_size = page_size or int(
            os.getenv("ASR_ADDRESS_SCOPE_PAGE_SIZE", "1000")
        )
        self._max_pages = max_pages or int(
            os.getenv("ASR_ADDRESS_SCOPE_MAX_PAGES", "100")
        )
        self._max_attempts = max_attempts or int(
            os.getenv("ASR_ADDRESS_SCOPE_MAX_ATTEMPTS", "2")
        )
        if not self._base_url or not 1 <= self._page_size <= 5000:
            raise ValueError("invalid address-scope REST configuration")
        if self._source not in {"database", "rest"}:
            raise ValueError("ASR_ADDRESS_SCOPE_SOURCE must be database or rest")
        self._db_host = os.getenv("ASR_ADDRESS_SCOPE_DB_HOST", "").strip()
        self._db_port = int(os.getenv("ASR_ADDRESS_SCOPE_DB_PORT", "15432"))
        self._db_name = os.getenv("ASR_ADDRESS_SCOPE_DB_NAME", "dispatch_assist").strip()
        self._db_user = os.getenv("ASR_ADDRESS_SCOPE_DB_USER", "").strip()
        self._db_password = os.getenv("ASR_ADDRESS_SCOPE_DB_PASSWORD", "")
        self._db_connect_timeout = int(os.getenv("ASR_ADDRESS_SCOPE_DB_CONNECT_TIMEOUT", "5"))
        if self._source == "database" and not all((self._db_host, self._db_name, self._db_user, self._db_password)):
            raise ValueError("address-scope database source requires host, database, user and password")

    async def fetch_hotwords(self, binding: AddressScopeBinding) -> AddressHotwordResult:
        started = perf_counter()
        if self._source == "database":
            items = await asyncio.to_thread(self._fetch_items_from_database, binding)
        else:
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                items = await self._fetch_items(session, binding)
        hotwords, counts, filtered_hotwords = self._extract(items)
        return AddressHotwordResult(
            hotwords=hotwords,
            items=tuple(items),
            filtered_hotwords=tuple(filtered_hotwords),
            item_count=len(items),
            poi_count=counts["POI"],
            aoi_count=counts["AOI"],
            building_count=counts["BUILDING"],
            alias_count=counts["alias"],
            query_ms=round((perf_counter() - started) * 1000, 3),
        )

    async def _fetch_items(self, session: aiohttp.ClientSession, binding: AddressScopeBinding) -> list[dict]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        items: dict[str, dict] = {}
        url = f"{self._base_url}{binding.items_path}"
        for _ in range(self._max_pages):
            payload = {
                "requestId": str(uuid4()),
                "sourceTypes": list(SOURCE_TYPES),
                "pageSize": self._page_size,
                "cursor": cursor,
            }
            response = await self._post(session, url, payload)
            data = self._validate_page(response, binding)
            for item in data["items"]:
                inventory_id = item.get("inventoryId")
                if isinstance(inventory_id, str) and inventory_id:
                    items.setdefault(inventory_id, item)
            if not data["hasMore"]:
                return list(items.values())
            cursor = data.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise AddressScopeQueryError("invalid or repeated address-scope cursor")
            seen_cursors.add(cursor)
        raise AddressScopeQueryError("address-scope maximum page count exceeded")

    def _fetch_items_from_database(self, binding: AddressScopeBinding) -> list[dict]:
        """Read a materialized logical scope by ID; do not run coordinate or GIS queries."""
        try:
            import psycopg2
            import psycopg2.extras

            connection = psycopg2.connect(
                host=self._db_host,
                port=self._db_port,
                dbname=self._db_name,
                user=self._db_user,
                password=self._db_password,
                connect_timeout=self._db_connect_timeout,
            )
            try:
                connection.set_session(readonly=True, autocommit=False)
                with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT inventory_id::text AS "inventoryId",
                               source_type AS "sourceType",
                               source_id AS "sourceId",
                               standard_name AS "standardName",
                               short_name AS "shortName",
                               full_address AS "fullAddress",
                               aliases,
                               aoi_name AS "aoiName",
                               road_name AS "roadName",
                               hit_level AS "hitLevel",
                               longitude,
                               latitude
                        FROM dispatch_assist.logical_address_scope_item
                        WHERE scope_id = %s::uuid
                          AND source_type IN ('BUILDING', 'AOI', 'POI')
                        ORDER BY inventory_id
                        """,
                        (binding.scope_id,),
                    )
                    return [dict(row) for row in cursor.fetchall()]
            finally:
                connection.close()
        except Exception as exc:
            raise AddressScopeQueryError("address-scope database query failed") from exc

    async def _post(self, session, url: str, payload: dict) -> Mapping[str, object]:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        raise AddressScopeQueryError(f"address-scope HTTP {response.status}")
                    decoded = await response.json()
                    if not isinstance(decoded, Mapping):
                        raise AddressScopeQueryError("address-scope response must be an object")
                    return decoded
            except AddressScopeQueryError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self._max_attempts:
                    await asyncio.sleep(0.05 * (2**attempt))
        raise AddressScopeQueryError("address-scope request failed") from last_error

    @staticmethod
    def _validate_page(payload: Mapping[str, object], binding: AddressScopeBinding) -> Mapping[str, object]:
        if payload.get("success") is not True or payload.get("code") != "OK":
            raise AddressScopeQueryError("address-scope response was not OK")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AddressScopeQueryError("address-scope data must be an object")
        ref = data.get("addressScopeRef")
        if not isinstance(ref, Mapping) or (
            ref.get("scopeId") != binding.scope_id
            or ref.get("locationResolutionId") != binding.location_resolution_id
            or ref.get("locationResolutionVersion") != binding.location_resolution_version
            or ref.get("inventoryVersion") != binding.inventory_version
        ):
            raise AddressScopeQueryError("address-scope response version mismatch")
        items = data.get("items")
        if not isinstance(items, list) or type(data.get("hasMore")) is not bool:
            raise AddressScopeQueryError("invalid address-scope page")
        for item in items:
            if not isinstance(item, dict) or item.get("sourceType") not in SOURCE_TYPES:
                raise AddressScopeQueryError("invalid address-scope item")
        return data

    @staticmethod
    def _extract(items: list[dict]) -> tuple[Mapping[str, int], dict[str, int], list[dict[str, object]]]:
        words: dict[str, int] = {}
        sources: dict[str, str] = {}
        seen = {"POI": set(), "AOI": set(), "BUILDING": set(), "alias": set()}

        def add(value: object, weight: int, bucket: str, source_field: str) -> None:
            if not isinstance(value, str):
                return
            text = " ".join(value.split())
            if not text or len(text) > 32 or text.isdecimal() or any(ord(c) < 32 for c in text):
                return
            if weight > words.get(text, 0):
                words[text] = weight
                sources[text] = source_field
            seen[bucket].add(text)

        for item in items:
            source_type = item["sourceType"]
            add(item.get("standardName"), 20, source_type, "standardName")
            add(item.get("aoiName"), 20, "AOI", "aoiName")
            add(item.get("shortName"), 15, "alias", "shortName")
            aliases = item.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, list):
                for alias in aliases:
                    add(alias, 15, "alias", "aliases")
        ordered_words = dict(sorted(words.items(), key=lambda item: (-item[1], item[0])))
        return (
            ordered_words,
            {key: len(value) for key, value in seen.items()},
            [
                {"word": word, "weight": weight, "sourceField": sources[word]}
                for word, weight in ordered_words.items()
            ],
        )
