"""Runtime ASR hotword stage and scene-signal management."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


LOG = logging.getLogger("hotword_manager")
BASE_DIR = Path(__file__).resolve().parent


class HotwordStage(StrEnum):
    ADDRESS = "address"
    INQUIRY_BASE = "inquiry_base"
    HIGHRISE = "highrise"
    CROWDED_PLACE = "crowded_place"
    CHEMICAL = "chemical"
    ELEVATOR = "elevator"


class HotwordMode(StrEnum):
    OFF = "off"
    FULL = "full"
    DYNAMIC = "dynamic"
    SCENE_DYNAMIC = "scene_dynamic"


class HotwordCatalogError(ValueError):
    pass


class InvalidSceneSignal(ValueError):
    pass


REQUIRED_SCENE_LIBRARY_IDS = frozenset(
    {
        "baseline",
        "classification_assist.call_type",
        "call_type.fire_fighting",
        "call_type.social_assistance",
        "call_type.emergency_rescue",
        "building_usage.medical_eldercare",
        "building_usage.crowded_place",
        "building_usage.industrial_storage_site",
        "building_usage.residential_commercial_hotel",
        "building_usage.education_training",
        "building_structure.highrise_multistory",
        "building_structure.underground",
        "building_structure.open_air",
    }
)
SCENE_SIGNAL_DIMENSIONS = frozenset(
    {"call_type", "building_usage", "building_structure"}
)
CLASSIFICATION_ASSIST_LIBRARY_ID = "classification_assist.call_type"


STAGE_FILES = {
    HotwordStage.ADDRESS: "address.txt",
    HotwordStage.INQUIRY_BASE: "inquiry_fire_base.txt",
    HotwordStage.HIGHRISE: "scenes/highrise.txt",
    HotwordStage.CROWDED_PLACE: "scenes/crowded_place.txt",
    HotwordStage.CHEMICAL: "scenes/chemical.txt",
    HotwordStage.ELEVATOR: "scenes/elevator.txt",
}


SCENE_KEYWORDS = {
    HotwordStage.HIGHRISE: ("高层", "超高层", "二十楼", "三十楼", "楼顶"),
    HotwordStage.CROWDED_PLACE: (
        "商场",
        "学校",
        "医院",
        "影院",
        "ktv",
        "KTV",
        "地铁站",
        "人员密集",
    ),
    HotwordStage.CHEMICAL: (
        "液化气",
        "煤气",
        "酒精",
        "油漆",
        "危化品",
        "氧气瓶",
        "化学品",
    ),
    HotwordStage.ELEVATOR: ("电梯困人", "困在电梯", "电梯", "卡住"),
}


def normalize_project(project: str | None) -> str:
    raw = str(project or "").strip().lower().replace("-", "_")
    if raw in {"addressbot", "address_bot", "address"} or "addressbot" in raw:
        return "addressbot"
    return "firebot"


def parse_hotword_line(line: str) -> str:
    line = str(line or "").strip()
    if not line or line.startswith("#"):
        return ""
    parts = line.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        return parts[0].strip()
    return line


@dataclass(frozen=True)
class SceneHotwordSnapshot:
    library_ids: tuple[str, ...]
    hotwords: tuple[tuple[str, int], ...]
    warning_threshold_reached: bool = False
    truncated: bool = False

    @property
    def text(self) -> str:
        return " ".join(word for word, _ in self.hotwords)


class SceneHotwordCatalog:
    def __init__(
        self,
        libraries: Mapping[str, Mapping[str, int]],
        *,
        max_hotwords: int = 0,
        warning_threshold: int = 800,
    ) -> None:
        if max_hotwords < 0:
            raise ValueError("max hotwords must be zero (unlimited) or positive")
        if warning_threshold <= 0 or (
            max_hotwords > 0 and warning_threshold > max_hotwords
        ):
            raise ValueError(
                "warning threshold must be positive and not exceed max hotwords"
            )
        self._libraries = {
            library_id: MappingProxyType(dict(hotwords))
            for library_id, hotwords in libraries.items()
        }
        self.max_hotwords = max_hotwords
        self.warning_threshold = warning_threshold

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        max_hotwords: int = 0,
        warning_threshold: int = 800,
    ) -> "SceneHotwordCatalog":
        if not directory.is_dir():
            raise HotwordCatalogError(
                f"scene hotword directory does not exist: {directory}"
            )
        libraries: dict[str, dict[str, int]] = {}
        for path in sorted(directory.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HotwordCatalogError(
                    f"unable to read scene hotword catalog: {path}"
                ) from exc
            if not isinstance(payload, dict):
                raise HotwordCatalogError(
                    f"scene hotword file must contain an object: {path}"
                )
            library_id = payload.get("library_id")
            if not isinstance(library_id, str) or not library_id:
                raise HotwordCatalogError(f"missing library_id: {path}")
            if library_id in libraries:
                raise HotwordCatalogError(
                    f"duplicate library_id: {library_id}"
                )
            raw_hotwords = payload.get("hotwords")
            if not isinstance(raw_hotwords, list):
                raise HotwordCatalogError(
                    f"hotwords must be an array: {path}"
                )
            hotwords: dict[str, int] = {}
            for item in raw_hotwords:
                if not isinstance(item, dict):
                    raise HotwordCatalogError(
                        f"hotword entry must be an object: {path}"
                    )
                text = str(item.get("text") or "").strip()
                weight = item.get("weight")
                if not text:
                    raise HotwordCatalogError(f"blank hotword: {path}")
                if len(text) > 32:
                    raise HotwordCatalogError(
                        f"hotword exceeds 32 characters: {text}"
                    )
                if text in hotwords:
                    raise HotwordCatalogError(
                        f"duplicate hotword {text!r}: {path}"
                    )
                if type(weight) is not int or weight <= 0:
                    raise HotwordCatalogError(
                        f"invalid hotword weight for {text!r}: {path}"
                    )
                hotwords[text] = weight
            libraries[library_id] = hotwords

        missing = REQUIRED_SCENE_LIBRARY_IDS - libraries.keys()
        if missing:
            raise HotwordCatalogError(
                "missing required scene libraries: "
                + ", ".join(sorted(missing))
            )
        return cls(
            libraries,
            max_hotwords=max_hotwords,
            warning_threshold=warning_threshold,
        )

    def resolve(
        self,
        signals: Mapping[str, list[str]],
        *,
        extra_libraries: Mapping[str, Mapping[str, int]] | None = None,
    ) -> SceneHotwordSnapshot:
        library_ids = ["baseline"]
        if not signals.get("call_type"):
            library_ids.append(CLASSIFICATION_ASSIST_LIBRARY_ID)
        for dimension in ("call_type", "building_usage", "building_structure"):
            for value in signals.get(dimension, []):
                library_id = f"{dimension}.{value}"
                if library_id not in self._libraries:
                    raise InvalidSceneSignal(
                        f"unknown scene signal: {library_id}"
                    )
                if library_id not in library_ids:
                    library_ids.append(library_id)

        extra_libraries = extra_libraries or {}
        for library_id in extra_libraries:
            if library_id not in library_ids:
                library_ids.append(library_id)

        core_merged: dict[str, int] = {}
        for library_id in library_ids:
            if library_id in extra_libraries:
                continue
            source = self._libraries.get(library_id, {})
            for text, weight in source.items():
                core_merged[text] = max(weight, core_merged.get(text, 0))

        extra_merged: dict[str, int] = {}
        for source in extra_libraries.values():
            for text, weight in source.items():
                if text in core_merged:
                    core_merged[text] = max(weight, core_merged[text])
                else:
                    extra_merged[text] = max(weight, extra_merged.get(text, 0))

        # Preserve the baseline and selected scene vocabulary first. Address
        # candidates fill the remaining capacity and cannot crowd out the
        # business vocabulary when a scope contains tens of thousands of rows.
        ordered = sorted(
            core_merged.items(), key=lambda item: (-item[1], item[0])
        ) + sorted(
            extra_merged.items(), key=lambda item: (-item[1], item[0])
        )
        selected = ordered if self.max_hotwords == 0 else ordered[: self.max_hotwords]
        return SceneHotwordSnapshot(
            library_ids=tuple(library_ids),
            hotwords=tuple(selected),
            warning_threshold_reached=(
                len(ordered) >= self.warning_threshold
            ),
            truncated=(
                self.max_hotwords > 0 and len(ordered) > self.max_hotwords
            ),
        )


_SCENE_CATALOG_CACHE: dict[
    tuple[Path, int, int], SceneHotwordCatalog
] = {}


def _scene_catalog(
    directory: Path,
    *,
    max_hotwords: int,
    warning_threshold: int,
) -> SceneHotwordCatalog:
    key = (directory.resolve(), max_hotwords, warning_threshold)
    catalog = _SCENE_CATALOG_CACHE.get(key)
    if catalog is None:
        catalog = SceneHotwordCatalog.from_directory(
            directory,
            max_hotwords=max_hotwords,
            warning_threshold=warning_threshold,
        )
        _SCENE_CATALOG_CACHE[key] = catalog
    return catalog


class HotwordManager:
    """Per-call hotword stage and scene-signal state."""

    def __init__(
        self,
        *,
        project: str = "firebot",
        hotword_dir: str | os.PathLike | None = None,
        full_hotword_file: str | os.PathLike | None = None,
        scene_hotword_dir: str | os.PathLike | None = None,
        mode: str | HotwordMode | None = None,
        max_hotwords: int | None = None,
        max_address_hotwords: int | None = None,
        warning_threshold: int | None = None,
    ):
        self.project = normalize_project(project)
        self.hotword_dir = Path(
            hotword_dir or os.getenv("ASR_HOTWORD_DIR") or BASE_DIR / "hotwords"
        )
        self.full_hotword_file = Path(
            full_hotword_file
            or os.getenv("ASR_FULL_HOTWORD_FILE")
            or BASE_DIR / "hotwords_full" / "full.txt"
        )
        self.mode = self._normalize_mode(
            mode or os.getenv("ASR_PREPROCESS_HOTWORD_MODE") or "full"
        )
        self.scene_hotword_dir = Path(
            scene_hotword_dir
            or os.getenv("ASR_SCENE_HOTWORD_DIR")
            or "/home/twai/wjl/DynamicHotwordLoading/hotwords"
        )
        self.max_hotwords = int(
            max_hotwords
            if max_hotwords is not None
            else os.getenv("ASR_MAX_HOTWORDS", "0")
        )
        self.max_address_hotwords = int(
            max_address_hotwords
            if max_address_hotwords is not None
            else os.getenv("ASR_MAX_ADDRESS_HOTWORDS", "1000")
        )
        if self.max_address_hotwords < 0:
            raise ValueError(
                "max address hotwords must be zero (unlimited) or positive"
            )
        self.warning_threshold = int(
            warning_threshold
            if warning_threshold is not None
            else os.getenv("ASR_HOTWORD_WARNING_THRESHOLD", "800")
        )
        self.stage = (
            HotwordStage.ADDRESS
            if self.project == "addressbot"
            else HotwordStage.INQUIRY_BASE
        )
        self._cache: dict[Path, str] = {}
        self._full_fallback_logged = False
        self._scene_catalog: SceneHotwordCatalog | None = None
        self._selected_signals: dict[str, list[str]] = {}
        self._scene_snapshot: SceneHotwordSnapshot | None = None
        self._address_hotwords: dict[str, int] = {}
        self._address_scope_id: str | None = None
        self._address_inventory_version: str | None = None
        self.hotword_version = 1
        if self.mode == HotwordMode.SCENE_DYNAMIC:
            self._load_scene_catalog()

    def current_hotwords(self) -> str:
        if self.mode == HotwordMode.OFF:
            return ""
        if self.mode == HotwordMode.SCENE_DYNAMIC:
            if self._scene_snapshot is not None:
                return self._scene_snapshot.text
            return self._load_file(
                self.full_hotword_file, label="full-fallback"
            )
        if self.mode == HotwordMode.FULL:
            full_hotwords = self._load_file(
                self.full_hotword_file, label="full"
            )
            if full_hotwords:
                return full_hotwords
            if not self._full_fallback_logged:
                LOG.warning(
                    "Full ASR hotword table is empty; falling back to dynamic "
                    "stage=%s",
                    self.stage.value,
                )
                self._full_fallback_logged = True
        return self._load_stage(self.stage)

    def apply_event(self, event: dict) -> bool:
        event_type = str(
            event.get("eventType")
            or event.get("type")
            or event.get("event")
            or ""
        ).strip()
        if event_type == "scene_signal.add":
            if self.mode != HotwordMode.SCENE_DYNAMIC:
                return False
            return self.add_scene_signals(event.get("signals"))
        if event_type not in {"stage.changed", "asr.hotwords.switch"}:
            return False
        if self.mode == HotwordMode.SCENE_DYNAMIC:
            return False
        requested = event.get("stage") or event.get("scene") or event.get("phase")
        stage = self._stage_from_value(requested)
        if stage is None:
            return False
        return self.set_stage(stage)

    def update_from_recognized_text(self, text: str) -> bool:
        if self.mode == HotwordMode.SCENE_DYNAMIC:
            return False
        if self.stage == HotwordStage.ADDRESS:
            return False
        text = str(text or "")
        if not text:
            return False
        for stage, keywords in SCENE_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return self.set_stage(stage)
        return False

    def set_stage(self, stage: HotwordStage) -> bool:
        if self.stage == stage:
            return False
        old_stage = self.stage
        self.stage = stage
        LOG.info(
            "ASR hotword stage changed: %s -> %s", old_stage.value, stage.value
        )
        return True

    @property
    def library_ids(self) -> tuple[str, ...]:
        if self._scene_snapshot is None:
            return ()
        return self._scene_snapshot.library_ids

    @property
    def hotword_count(self) -> int:
        if self._scene_snapshot is not None:
            return len(self._scene_snapshot.hotwords)
        return len(self.current_hotwords().split())

    def current_hotword_items(self) -> tuple[tuple[str, int | None], ...]:
        """Return the configured entries that produced the current handshake."""
        if self._scene_snapshot is not None:
            return self._scene_snapshot.hotwords
        return tuple((word, None) for word in self.current_hotwords().split())

    @property
    def warning_threshold_reached(self) -> bool:
        return bool(
            self._scene_snapshot
            and self._scene_snapshot.warning_threshold_reached
        )

    @property
    def truncated(self) -> bool:
        return bool(self._scene_snapshot and self._scene_snapshot.truncated)

    def add_scene_signals(self, raw_signals: object) -> bool:
        if self._scene_catalog is None:
            raise InvalidSceneSignal("scene hotword catalog is unavailable")
        if not isinstance(raw_signals, Mapping) or not raw_signals:
            raise InvalidSceneSignal("signals must be a non-empty object")
        unknown_dimensions = set(raw_signals) - SCENE_SIGNAL_DIMENSIONS
        if unknown_dimensions:
            raise InvalidSceneSignal(
                "unknown scene signal dimension: "
                + ", ".join(sorted(unknown_dimensions))
            )

        candidate = {
            dimension: list(values)
            for dimension, values in self._selected_signals.items()
        }
        changed = False
        for dimension, raw_values in raw_signals.items():
            if (
                not isinstance(raw_values, list)
                or not raw_values
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in raw_values
                )
            ):
                raise InvalidSceneSignal(
                    f"{dimension} must be a non-empty string array"
                )
            values = list(
                dict.fromkeys(value.strip() for value in raw_values)
            )
            if dimension == "call_type":
                if len(values) != 1:
                    raise InvalidSceneSignal(
                        "call_type must contain exactly one value"
                    )
                if candidate.get(dimension) != values:
                    candidate[dimension] = values
                    changed = True
                continue
            selected = candidate.setdefault(dimension, [])
            for value in values:
                if value not in selected:
                    selected.append(value)
                    changed = True

        if not changed:
            return False
        next_snapshot = self._scene_catalog.resolve(
            candidate, extra_libraries=self._address_libraries()
        )
        self._selected_signals = candidate
        self._scene_snapshot = next_snapshot
        self.hotword_version += 1
        LOG.info(
            "ASR scene hotwords updated version=%d libraries=%s count=%d "
            "warning=%s truncated=%s effective_from=next_segment",
            self.hotword_version,
            ",".join(next_snapshot.library_ids),
            len(next_snapshot.hotwords),
            next_snapshot.warning_threshold_reached,
            next_snapshot.truncated,
        )
        return True

    @property
    def address_scope_id(self) -> str | None:
        return self._address_scope_id

    @property
    def address_inventory_version(self) -> str | None:
        return self._address_inventory_version

    @property
    def address_hotword_count(self) -> int:
        """Return selected address entries without splitting names on spaces."""
        return len(self._address_hotwords)

    def set_address_hotwords(
        self,
        *,
        scope_id: str,
        inventory_version: str,
        hotwords: Mapping[str, int],
    ) -> bool:
        """Replace the call-scoped address library for the next VAD segment."""
        if self.mode != HotwordMode.SCENE_DYNAMIC:
            return False
        if self._scene_catalog is None:
            raise InvalidSceneSignal("scene hotword catalog is unavailable")
        normalized: dict[str, int] = {}
        for text, weight in hotwords.items():
            word = str(text or "").strip()
            if not word or len(word) > 32 or type(weight) is not int or weight <= 0:
                continue
            normalized[word] = max(weight, normalized.get(word, 0))
        if not normalized:
            return False
        normalized = dict(sorted(
            normalized.items(), key=lambda item: (-item[1], item[0])
        ))
        if self.max_address_hotwords > 0:
            normalized = dict(
                list(normalized.items())[: self.max_address_hotwords]
            )
        if (
            self._address_scope_id == scope_id
            and self._address_inventory_version == inventory_version
            and self._address_hotwords == normalized
        ):
            return False
        self._address_scope_id = str(scope_id)
        self._address_inventory_version = str(inventory_version)
        self._address_hotwords = normalized
        self._scene_snapshot = self._scene_catalog.resolve(
            self._selected_signals, extra_libraries=self._address_libraries()
        )
        self.hotword_version += 1
        LOG.info(
            "ASR address hotwords updated version=%d scopeId=%s count=%d total=%d "
            "effective_from=next_segment",
            self.hotword_version,
            self._address_scope_id,
            len(normalized),
            len(self._scene_snapshot.hotwords),
        )
        return True

    def _load_scene_catalog(self) -> None:
        try:
            self._scene_catalog = _scene_catalog(
                self.scene_hotword_dir,
                max_hotwords=self.max_hotwords,
                warning_threshold=self.warning_threshold,
            )
            self._scene_snapshot = self._scene_catalog.resolve({})
        except (HotwordCatalogError, InvalidSceneSignal, ValueError, OSError):
            LOG.exception(
                "Unable to load scene hotword catalog path=%s; "
                "falling back to full hotwords",
                self.scene_hotword_dir,
            )
            self._scene_catalog = None
            self._scene_snapshot = None
            return
        LOG.info(
            "Loaded ASR scene hotword catalog path=%s libraries=%s count=%d",
            self.scene_hotword_dir,
            ",".join(self._scene_snapshot.library_ids),
            len(self._scene_snapshot.hotwords),
        )

    def _address_libraries(self) -> Mapping[str, Mapping[str, int]]:
        if not self._address_scope_id or not self._address_hotwords:
            return {}
        return {f"address.scope:{self._address_scope_id}": self._address_hotwords}

    def _load_stage(self, stage: HotwordStage) -> str:
        rel_path = STAGE_FILES[stage]
        path = self.hotword_dir / rel_path
        return self._load_file(path, label=stage.value)

    def _load_file(self, path: Path, *, label: str) -> str:
        if path in self._cache:
            return self._cache[path]

        words: list[str] = []
        if path.exists():
            with path.open(encoding="utf-8") as file_handle:
                for line in file_handle:
                    word = parse_hotword_line(line)
                    if word:
                        words.append(word)
        else:
            LOG.warning(
                "ASR hotword file missing for source=%s: %s", label, path
            )

        value = " ".join(dict.fromkeys(words))
        self._cache[path] = value
        if value:
            LOG.info(
                "Loaded %d ASR hotwords from source=%s path=%s",
                len(value.split()),
                label,
                path,
            )
        return value

    @staticmethod
    def _normalize_mode(value) -> HotwordMode:
        raw = str(value or "").strip().lower()
        try:
            return HotwordMode(raw)
        except ValueError:
            LOG.warning(
                "Unknown ASR_PREPROCESS_HOTWORD_MODE=%r; using full", value
            )
            return HotwordMode.FULL

    @staticmethod
    def _stage_from_value(value) -> HotwordStage | None:
        raw = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "address_phase": HotwordStage.ADDRESS,
            "address": HotwordStage.ADDRESS,
            "inquiry": HotwordStage.INQUIRY_BASE,
            "inquiry_base": HotwordStage.INQUIRY_BASE,
            "fire_base": HotwordStage.INQUIRY_BASE,
            "base": HotwordStage.INQUIRY_BASE,
            "highrise": HotwordStage.HIGHRISE,
            "high_rise": HotwordStage.HIGHRISE,
            "高层建筑": HotwordStage.HIGHRISE,
            "crowded_place": HotwordStage.CROWDED_PLACE,
            "crowded": HotwordStage.CROWDED_PLACE,
            "人员密集场所": HotwordStage.CROWDED_PLACE,
            "chemical": HotwordStage.CHEMICAL,
            "hazmat": HotwordStage.CHEMICAL,
            "危化品": HotwordStage.CHEMICAL,
            "elevator": HotwordStage.ELEVATOR,
            "电梯困人": HotwordStage.ELEVATOR,
        }
        return aliases.get(raw)
