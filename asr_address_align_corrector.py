"""Database-backed ASR address correction using pinyin alignment."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from asr_address_db import AddressTerm, address_term_weight

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover
    lazy_pinyin = None
    Style = None


CHINESE_OR_ALNUM_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
ADMIN_DISTRICT_RE = re.compile(r"[\u4e00-\u9fff]{2,}区")
ALNUM_BUILDING_SUFFIX_RE = re.compile(r"[A-Za-z][0-9]*(?:栋|座|幢|单元|号楼|楼|层|室)$", re.I)
NUM_BUILDING_SUFFIX_RE = re.compile(r"\d+(?:栋|座|幢|号楼|楼|层|室)$")
TRAILING_LOCATION_SUFFIX_RE = re.compile(r"(?:首层|大堂|门口|附近|旁边|入口)$")
DIGIT_RE = re.compile(r"\d+")
ARABIC_NUMBER_RE = re.compile(r"\d+")
ALNUM_MARKER_RE = re.compile(r"[A-Za-z][0-9]*(?:栋|座|幢|单元|号楼|楼|层|室)", re.I)
GENERIC_ADDRESS_RE = re.compile(
    r"(?:一|两|几|多|某|这|那|哪|一个|两个|几个|这个|那个|哪个)"
    r"(?:小区|地方|位置|路口|门口)"
)
SOFTWARE_PARK_PHASE_SEVEN_BUILDING_RE = re.compile(
    r"(?P<prefix>(?:深圳)?软件[园里](?:第)?(?:[一二三四五六七八九十]+|\d+)期)"
    r"(?P<suffix>启动|[7七]动)"
)
EXPLICIT_SEVEN_BUILDING_HOMOPHONE_RE = re.compile(r"[7七]动")
STANDALONE_SEVEN_BUILDING_HOMOPHONE_RE = re.compile(r"^(?:在)?启动$")
BAD_FRAGMENT_START = set("是在和与及到的了")
TRAILING_GRAMMATICAL_PARTICLES = set("的了呢啊呀吧吗嘛")
BUILDING_SUFFIXES = ("大厦", "小区", "花园", "公馆", "公寓", "科学园", "科技园")


@dataclass(frozen=True)
class AlignCandidate:
    term: str
    source: str
    weight: float
    kind: str
    pinyin: str


@dataclass(frozen=True)
class CandidateIndex:
    candidates: list[AlignCandidate]
    by_term: dict[str, AlignCandidate]
    by_length_prefix: dict[int, dict[str, list[AlignCandidate]]]


@dataclass(frozen=True)
class AlignCorrectionResult:
    original: str
    corrected: str
    replacements: list[dict]
    elapsed_ms: float


_CHINESE_DIGITS = "零一二三四五六七八九"
_CHINESE_SMALL_UNITS = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))


def arabic_integer_to_chinese(raw: str) -> str:
    """Convert an Arabic integer to its ordinary Chinese numeric reading."""
    if not raw:
        return raw
    if len(raw) > 1 and raw.startswith("0"):
        return "".join(_CHINESE_DIGITS[int(char)] for char in raw)

    value = int(raw)
    if value == 0:
        return _CHINESE_DIGITS[0]
    if value > 9999:
        return "".join(_CHINESE_DIGITS[int(char)] for char in raw)

    parts: list[str] = []
    zero_pending = False
    remainder = value
    for unit_value, unit_name in _CHINESE_SMALL_UNITS:
        digit, remainder = divmod(remainder, unit_value)
        if digit:
            if zero_pending:
                parts.append(_CHINESE_DIGITS[0])
                zero_pending = False
            parts.append(_CHINESE_DIGITS[digit])
            parts.append(unit_name)
        elif parts and remainder:
            zero_pending = True

    result = "".join(parts)
    if result.startswith("一十"):
        result = result[1:]
    return result


def normalize_arabic_number_readings(text: str) -> str:
    return ARABIC_NUMBER_RE.sub(
        lambda match: arabic_integer_to_chinese(match.group(0)),
        str(text or ""),
    )


def compact_pinyin(text: str) -> str:
    phonetic_text = normalize_arabic_number_readings(text)
    if lazy_pinyin is None:
        return phonetic_text
    return "".join(lazy_pinyin(phonetic_text, style=Style.NORMAL))


def normalized_edit_similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            ))
        previous = current
    distance = previous[-1]
    return 1.0 - distance / max(len(a), len(b))


def strip_known_suffixes(term: str) -> str:
    text = term.strip()
    changed = True
    while changed:
        changed = False
        for pattern in (ALNUM_BUILDING_SUFFIX_RE, NUM_BUILDING_SUFFIX_RE, TRAILING_LOCATION_SUFFIX_RE):
            new_text = pattern.sub("", text).strip()
            if new_text != text:
                text = new_text
                changed = True
    return text


def derive_alias_terms(term: AddressTerm) -> list[tuple[str, str]]:
    value = term.term.strip()
    aliases: list[tuple[str, str]] = [(value, "db_exact")]

    stripped = strip_known_suffixes(value)
    if stripped and stripped != value and len(stripped) >= 3:
        aliases.append((stripped, "derived_strip_suffix"))

    for district in ADMIN_DISTRICT_RE.findall(value):
        if 3 <= len(district) <= 6:
            aliases.append((district, "derived_admin"))

    if value.startswith("南山") and len(value) > 4:
        aliases.append((value[2:], "derived_drop_nanshan_prefix"))

    for suffix in BUILDING_SUFFIXES:
        idx = value.find(suffix)
        if idx >= 2:
            start = max(0, idx - 4)
            alias = value[start:idx + len(suffix)]
            alias = re.sub(r"^[省市区县街道镇村路号\dA-Za-z]+", "", alias)
            if 3 <= len(alias) <= 10:
                aliases.append((alias, f"derived_compound_{suffix}"))

    seen: set[str] = set()
    output: list[tuple[str, str]] = []
    for alias, kind in aliases:
        alias = alias.strip("()（）[]【】,，;；、 ")
        if len(alias) < 3 or alias in seen:
            continue
        seen.add(alias)
        output.append((alias, kind))
    return output


def build_candidates(terms: list[AddressTerm]) -> list[AlignCandidate]:
    best: dict[str, AlignCandidate] = {}
    for term in terms:
        base_weight = address_term_weight(term)
        source = f"{term.table}.{term.column}"
        for alias, kind in derive_alias_terms(term):
            weight = base_weight
            if kind.startswith("derived"):
                weight = max(0.7, base_weight - 0.2)
            candidate = AlignCandidate(
                term=alias,
                source=source,
                weight=weight,
                kind=kind,
                pinyin=compact_pinyin(alias),
            )
            current = best.get(alias)
            if current is None or candidate.weight > current.weight:
                best[alias] = candidate
    return list(best.values())


def build_candidate_index(candidates: list[AlignCandidate]) -> CandidateIndex:
    by_term = {candidate.term: candidate for candidate in candidates}
    by_length_prefix: dict[int, dict[str, list[AlignCandidate]]] = defaultdict(lambda: defaultdict(list))
    for candidate in candidates:
        by_length_prefix[len(candidate.term)][candidate.pinyin[:2]].append(candidate)
    return CandidateIndex(
        candidates=candidates,
        by_term=by_term,
        by_length_prefix={length: dict(prefixes) for length, prefixes in by_length_prefix.items()},
    )


def iter_fragments(text: str, *, min_len: int = 3, max_len: int = 14):
    for match in CHINESE_OR_ALNUM_RE.finditer(text):
        run = match.group(0)
        start0 = match.start()
        upper = min(max_len, len(run))
        for size in range(upper, min_len - 1, -1):
            for offset in range(0, len(run) - size + 1):
                yield start0 + offset, start0 + offset + size, run[offset:offset + size]


def preserves_structural_tokens(fragment: str, candidate: str) -> bool:
    for digits in DIGIT_RE.findall(fragment):
        if digits not in candidate:
            return False
    candidate_lower = candidate.lower()
    for marker in ALNUM_MARKER_RE.findall(fragment):
        if marker.lower() not in candidate_lower:
            return False
    return True


def score_candidate(fragment: str, fragment_pinyin: str, candidate: AlignCandidate) -> float:
    if candidate.term == fragment:
        return 0.0
    if len(candidate.term) != len(fragment):
        return 0.0
    if fragment[0] in BAD_FRAGMENT_START:
        return 0.0
    if GENERIC_ADDRESS_RE.search(fragment):
        return 0.0
    if not preserves_structural_tokens(fragment, candidate.term):
        return 0.0

    py_score = normalized_edit_similarity(fragment_pinyin, candidate.pinyin)
    char_score = SequenceMatcher(None, fragment, candidate.term).ratio()
    char_overlap = len(set(fragment) & set(candidate.term)) / max(len(set(fragment)), 1)
    weight_score = min(1.0, candidate.weight / 3.0)

    if py_score < 0.86:
        suffix_like_address = any(suffix in candidate.term for suffix in BUILDING_SUFFIXES)
        if not (suffix_like_address and py_score >= 0.82 and char_score >= 0.74):
            return 0.0
    if char_overlap < 0.45:
        return 0.0
    if len(fragment) <= 3 and py_score < 0.92:
        return 0.0

    return py_score * 0.48 + char_score * 0.24 + char_overlap * 0.16 + weight_score * 0.12


def correct_contextual_building_homophones(text: str) -> tuple[str, list[dict]]:
    replacements: list[dict] = []

    def replace(match: re.Match) -> str:
        original = match.group(0)
        corrected = match.group("prefix").replace("软件里", "软件园") + "七栋"
        replacements.append({
            "span": [match.start(), match.end()],
            "original": original,
            "corrected": corrected,
            "score": 1.0,
            "source": "context_rule",
            "kind": "software_park_phase_seven_building",
            "method": "context_rule",
        })
        return corrected

    corrected_text = SOFTWARE_PARK_PHASE_SEVEN_BUILDING_RE.sub(replace, text)

    def replace_explicit_numeric(match: re.Match) -> str:
        replacements.append({
            "span": [match.start(), match.end()],
            "original": match.group(0),
            "corrected": "七栋",
            "score": 1.0,
            "source": "context_rule",
            "kind": "explicit_seven_building_homophone",
            "method": "context_rule",
        })
        return "七栋"

    corrected_text = EXPLICIT_SEVEN_BUILDING_HOMOPHONE_RE.sub(
        replace_explicit_numeric,
        corrected_text,
    )

    standalone = STANDALONE_SEVEN_BUILDING_HOMOPHONE_RE.fullmatch(corrected_text)
    if standalone:
        original = standalone.group(0)
        corrected = "在七栋" if original.startswith("在") else "七栋"
        replacements.append({
            "span": [0, len(original)],
            "original": original,
            "corrected": corrected,
            "score": 1.0,
            "source": "context_rule",
            "kind": "standalone_seven_building_homophone",
            "method": "context_rule",
        })
        corrected_text = corrected

    return corrected_text, replacements


def correct_with_alignment(text: str, index: CandidateIndex) -> tuple[str, list[dict]]:
    proposed: list[dict] = []
    for start, end, fragment in iter_fragments(text):
        # Do not let fuzzy address matching consume a trailing grammatical
        # particle, e.g. "科兴科学园的" -> "科兴科学园C". Shorter fragments
        # remain eligible, so an actual typo inside the address can still be
        # corrected while the particle is preserved.
        if fragment[-1] in TRAILING_GRAMMATICAL_PARTICLES:
            continue
        if fragment in index.by_term:
            continue
        fragment_pinyin = compact_pinyin(fragment)
        best: tuple[AlignCandidate, float] | None = None
        for candidate in index.by_length_prefix.get(len(fragment), {}).get(fragment_pinyin[:2], []):
            score = score_candidate(fragment, fragment_pinyin, candidate)
            if score <= 0:
                continue
            if best is None or score > best[1]:
                best = (candidate, score)
        if best is None or best[1] < 0.78:
            continue
        candidate, score = best
        proposed.append({
            "span": [start, end],
            "original": fragment,
            "corrected": candidate.term,
            "score": round(score, 3),
            "source": candidate.source,
            "kind": candidate.kind,
            "method": "pinyin_align",
        })

    proposed.sort(key=lambda item: (item["span"][0], -(item["span"][1] - item["span"][0]), -item["score"]))
    selected: list[dict] = []
    used: set[int] = set()
    for item in proposed:
        span = set(range(item["span"][0], item["span"][1]))
        if span & used:
            continue
        selected.append(item)
        used.update(span)

    if not selected:
        return text, []
    parts: list[str] = []
    last = 0
    for item in sorted(selected, key=lambda row: row["span"][0]):
        start, end = item["span"]
        parts.append(text[last:start])
        parts.append(item["corrected"])
        last = end
    parts.append(text[last:])
    return "".join(parts), selected


class AsrAddressAlignCorrector:
    def __init__(self, terms: list[AddressTerm]):
        self.terms = terms
        self.candidates = build_candidates(terms)
        self.index = build_candidate_index(self.candidates)

    def correct(self, text: str) -> AlignCorrectionResult:
        started = time.perf_counter()
        normalized, contextual_replacements = correct_contextual_building_homophones(text)
        corrected, alignment_replacements = correct_with_alignment(normalized, self.index)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return AlignCorrectionResult(
            original=text,
            corrected=corrected,
            replacements=contextual_replacements + alignment_replacements,
            elapsed_ms=elapsed_ms,
        )
