"""
ASR 文本后处理纠错引擎。

算法：分词 + 拼音 Hash 匹配 + 滑动窗口编辑距离 fallback + 上下文消歧打分。
支持全量加载和动态按需加载两种模式。
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import jieba
except ImportError:
    jieba = None
try:
    from pypinyin import lazy_pinyin, Style
except ImportError:
    lazy_pinyin = None
    Style = None


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CorrectionResult:
    original: str
    corrected: str
    replacements: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0
    pools_loaded: list[str] = field(default_factory=list)


@dataclass
class HotwordEntry:
    id: str
    word: str
    pinyin: str
    pinyin_compact: str
    weight: float
    context_keywords: list[str]
    ngram: list[str]
    min_len: int
    max_len: int
    source_key: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compact(text: str) -> str:
    if lazy_pinyin is None:
        return text  # fallback: return raw text
    return ''.join(lazy_pinyin(text, style=Style.NORMAL))


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance for pinyin strings."""
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(
                prev[j + 1] + 1,   # deletion
                curr[j] + 1,       # insertion
                prev[j] + cost     # substitution
            ))
        prev = curr
    return prev[-1]


def _clean_noise(text: str, noise_words: set[str]) -> str:
    """移除文本中嵌入的口语填充词。"""
    # Sort by length descending to match longer phrases first
    for nw in sorted(noise_words, key=len, reverse=True):
        # Only replace when surrounded by punctuation or spaces (not within words)
        text = re.sub(r'(?<![a-zA-Z0-9一-鿿])' + re.escape(nw) +
                      r'(?![a-zA-Z0-9一-鿿])', '', text)
    # Clean up: collapse multiple punctuation/spaces
    text = re.sub(r'[,，。\s]+', '', text)
    return text.strip()


# ---------------------------------------------------------------------------
# AsrCorrector
# ---------------------------------------------------------------------------

class AsrCorrector:
    """ASR 文本后处理纠错引擎。

    启动时预加载全部热词到内存。全量模式使用合并索引，
    动态模式根据提问关键词从预加载的子集索引中组装。
    """

    def __init__(self, mode: str = "full",
                 correction_dir: str | None = None,
                 noise_words: set[str] | None = None):
        self.mode = mode
        base = correction_dir or os.path.join(
            os.path.dirname(__file__), '..', 'correction')
        self.correction_dir = os.path.abspath(base)
        self.noise_words = noise_words or set()

        # Master data: all entries keyed by id
        self._master_entries: dict[str, HotwordEntry] = {}
        # Per-subset pinyin indices: {subset_filename: {pinyin_compact: {entry_id}}}
        self._subset_indices: dict[str, dict[str, set[str]]] = {}
        # Global pinyin indices (always active): {pinyin_compact: {entry_id}}
        self._global_indices: dict[str, set[str]] = {}
        # Keyword → subset mapping
        self._keyword_map: list[dict] = []

        # Runtime index: the currently active pinyin_index + entries
        self.entries: dict[str, HotwordEntry] = {}
        self.pinyin_index: dict[str, set[str]] = {}

        # Legacy expert correction pairs are intentionally not loaded here.
        # Address correction now uses only the configured Xiangzhou hotword index.
        self._correction_pairs: list[dict] = []

        # jieba custom dict tracking
        self._jieba_words_loaded: set[str] = set()

        # Pre-load all data at startup
        self._preload_all()

        # Activate full or empty (dynamic will activate per-call)
        if mode == "full":
            self._activate_full()

    # ------------------------------------------------------------------
    # Preloading (once at startup)
    # ------------------------------------------------------------------

    def _preload_all(self):
        """启动时一次性预加载所有热词文件和映射表到内存。"""
        # Load keyword mapping
        km = {"global_files": [], "mappings": []}
        mapping_path = os.path.join(self.correction_dir, 'keyword_mapping.json')
        if os.path.exists(mapping_path):
            with open(mapping_path, encoding='utf-8') as f:
                km = json.load(f)
                self._keyword_map = km.get("mappings", [])

        # Load only files declared by keyword_mapping.json.
        # The current deployment uses the generated index from
        # hot_pool/xiangzhou_building_address_hotwords.json; legacy entries_hw*
        # files in correction/ must not be activated implicitly.
        configured_files: list[str] = []
        configured_files.extend(km.get("global_files", []))
        configured_files.extend(
            mapping.get("file", "") for mapping in self._keyword_map
            if mapping.get("file")
        )

        seen_files: set[str] = set()
        for configured in configured_files:
            gf_path = configured if os.path.isabs(configured) else os.path.join(
                os.path.dirname(self.correction_dir), configured)
            basename = os.path.basename(gf_path)
            if basename in seen_files:
                continue
            if os.path.exists(gf_path):
                self._preload_subset(basename, gf_path)
                seen_files.add(basename)

    def _preload_subset(self, name: str, filepath: str):
        """预加载一个子集文件：存 entries + 单独索引。"""
        with open(filepath, encoding='utf-8') as f:
            data = json.load(f)

        subset_index: dict[str, set[str]] = {}
        for e in data.get("entries", []):
            hw = HotwordEntry(
                id=e["id"], word=e["word"], pinyin=e["pinyin"],
                pinyin_compact=e["pinyin_compact"], weight=e.get("weight", 1.0),
                context_keywords=e.get("context_keywords", []),
                ngram=e.get("ngram", []), min_len=e.get("min_len", 2),
                max_len=e.get("max_len", 8), source_key=e.get("source_key", ""),
            )
            if hw.id not in self._master_entries:
                self._master_entries[hw.id] = hw
                if jieba is not None and hw.word not in self._jieba_words_loaded:
                    jieba.add_word(hw.word, freq=int(hw.weight * 5))
                    self._jieba_words_loaded.add(hw.word)

        for py, ids in data.get("pinyin_index", {}).items():
            if py not in subset_index:
                subset_index[py] = set()
            subset_index[py].update(ids)

        self._subset_indices[name] = subset_index

    def _load_correction_pairs(self):
        """加载专家标注的纠错对。"""
        pairs_path = os.path.join(self.correction_dir, 'correction_pairs.json')
        if os.path.exists(pairs_path):
            with open(pairs_path, encoding='utf-8') as f:
                data = json.load(f)
            self._correction_pairs = data.get('pairs', [])
            self._correction_pairs.sort(key=lambda x: len(x['error']), reverse=True)

    # ------------------------------------------------------------------
    # Index activation (no I/O)
    # ------------------------------------------------------------------

    def _activate_full(self):
        """激活全量索引（合并所有子集）。"""
        self.entries = dict(self._master_entries)
        merged: dict[str, set[str]] = {}
        for idx in self._subset_indices.values():
            for py, ids in idx.items():
                if py not in merged:
                    merged[py] = set()
                merged[py].update(ids)
        self.pinyin_index = merged

    def _activate_by_keywords(self, question_text: str):
        """根据提问关键词激活对应子集的合并索引。"""
        matched_files: set[str] = set()
        for mapping in self._keyword_map:
            if any(kw in question_text for kw in mapping.get("keywords", [])):
                fname = os.path.basename(mapping.get("file", ""))
                if fname and fname in self._subset_indices:
                    matched_files.add(fname)

        # Always include global files (e.g. entries_addr_china_address.json)
        for fname in self._subset_indices:
            if not fname.startswith("entries_hw"):
                matched_files.add(fname)

        if not matched_files:
            # Fallback: use all
            self._activate_full()
            return

        # Merge indices from matched subsets
        active_ids: set[str] = set()
        merged: dict[str, set[str]] = {}
        for fname in matched_files:
            for py, ids in self._subset_indices[fname].items():
                if py not in merged:
                    merged[py] = set()
                merged[py].update(ids)
                active_ids.update(ids)

        self.pinyin_index = merged
        self.entries = {eid: self._master_entries[eid]
                        for eid in active_ids if eid in self._master_entries}

    # ------------------------------------------------------------------
    # Correction
    # ------------------------------------------------------------------

    def _lookup_pinyin(self, text: str) -> list[HotwordEntry]:
        """通过拼音 hash 精确查找。"""
        py = _compact(text)
        ids = self.pinyin_index.get(py, set())
        return [self.entries[eid] for eid in ids if eid in self.entries]

    def _fuzzy_match(self, text: str, max_dist: int = 1,
                      require_char_overlap: bool = True) -> list[tuple[HotwordEntry, int]]:
        """通过拼音编辑距离模糊匹配。返回 [(entry, distance), ...]

        按拼音首字母前缀剪枝，大幅缩小候选空间。
        """
        py = _compact(text)
        text_chars = set(text)
        results = []
        target_len = len(py)
        # Prefix pruning: only check entries whose pinyin starts with same first 2 chars
        prefix = py[:2] if len(py) >= 2 else py[:1]
        for e in self.entries.values():
            epy = e.pinyin_compact
            # Quick length filter
            if abs(len(epy) - target_len) > max_dist * 2:
                continue
            # Prefix prune: first 2 pinyin chars must match (reduces search by ~90%)
            if len(prefix) >= 2 and len(epy) >= 2:
                if epy[:2] != prefix:
                    continue
            # Require at least 1 shared Chinese character
            if require_char_overlap:
                entry_chars = set(e.word)
                if not (text_chars & entry_chars):
                    continue
            dist = _edit_distance(py, epy)
            if dist <= max_dist:
                results.append((e, dist))
        results.sort(key=lambda x: x[1])
        return results[:10]  # top 10

    def _score_candidates(self, candidates: list[tuple[HotwordEntry, int]],
                          context_tokens: list[str], target_len: int = 0,
                          original_text: str = "") -> list[tuple[HotwordEntry, float]]:
        """对候选热词进行上下文打分。返回 [(entry, score), ...] 按分降序。"""
        scored = []
        for entry, dist in candidates:
            # Edit distance score (0-1, inverted: dist=0 → 1.0)
            dist_score = 1.0 - (dist * 0.5)

            # Character overlap: chars shared between original text and candidate
            char_overlap = 0.0
            if original_text:
                orig_chars = set(original_text)
                entry_chars = set(entry.word)
                overlap = len(orig_chars & entry_chars)
                char_overlap = min(1.0, overlap / max(len(orig_chars), 1))

            # Length penalty: prefer entries close to target word length
            len_penalty = 1.0
            if target_len > 0:
                len_diff = abs(len(entry.word) - target_len)
                len_penalty = max(0.5, 1.0 - len_diff * 0.2)

            # Context keyword hit score
            context_hits = 0
            for ck in entry.context_keywords:
                if any(ck in t for t in context_tokens):
                    context_hits += 1
            context_score = min(1.0, context_hits * 0.3)

            # Weight score (normalize to 0-1, max weight ~3.0)
            weight_score = min(1.0, entry.weight / 3.0)

            total = (dist_score * 0.30 + char_overlap * 0.25 +
                     len_penalty * 0.15 + context_score * 0.15 +
                     weight_score * 0.15)
            scored.append((entry, total))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    _SPEECH_BOUNDARY_TOKENS = {
        "我", "在", "我在", "这里", "这边", "那边", "有", "警情",
        "请问", "具体", "地址", "哪里", "哪儿",
    }
    _BOUNDARY_CHARS = set("，,。！？；;：:、 \t\n\r")
    _PROTECTED_ADDRESS_FRAGMENTS = {
        "一期", "二期", "三期", "四期", "五期", "六期",
        "中心", "校区", "小区", "公馆", "花园", "街道", "大道",
        "社区", "工业区", "产业园", "科技园",
    }
    _GENERIC_ADDRESS_PATTERNS = (
        r"(?:一|两|几|多|某|这|那|哪)个(?:小区|地方|位置|路口|门口)",
        r"(?:一个|两个|几个|这个|那个|哪个)(?:小区|地方|位置|路口|门口)",
        r"(?:有人|没人|没有人|一个人|两个人|几个人)",
    )
    _ADDRESS_QUESTION_PATTERNS = (
        "具体地址", "地址在哪里", "哪个小区", "哪个道路", "哪个街道",
        "什么位置", "在什么位置", "哪里", "哪儿",
    )
    _ADDRESS_INTENT_PREFIXES = (
        "我在", "位置是", "地址在", "大概地址", "现在在", "报警人说现在在",
        "这里是", "我这里是",
    )
    _ADDRESS_ENTITY_MARKERS = (
        "街道", "小区", "社区", "大厦", "园区", "科技园", "产业园",
        "科学园", "花园", "公馆", "公寓", "路", "街", "号", "栋", "楼",
        "门口", "附近", "交叉口", "停车场",
    )
    _COMPACT_ADDRESS_ENTITY_PATTERN = re.compile(
        r"[\u4e00-\u9fff]{2,}(?:路|街|小区|大厦|园区|科技园|科学园|花园|公馆|公寓|栋|楼|号)"
    )
    _ALNUM_BUILDING_PATTERN = re.compile(
        r"[A-Za-z][0-9]*(?:栋|座|幢|单元|号楼|楼|层|室)"
    )
    _TRAILING_BUILDING_MARKER_PATTERN = re.compile(r"[A-Za-z][0-9]*$")

    @classmethod
    def _is_boundary_token(cls, token: str) -> bool:
        if not token:
            return True
        if token in cls._SPEECH_BOUNDARY_TOKENS:
            return True
        return any(ch in cls._BOUNDARY_CHARS for ch in token)

    @classmethod
    def _window_crosses_boundary(cls, window: list[tuple[str, int, int]]) -> bool:
        return any(cls._is_boundary_token(t[0]) for t in window)

    @classmethod
    def _is_protected_fragment(cls, text: str) -> bool:
        stripped = text.strip("，,。！？；;：:、 \t\n\r()（）[]【】")
        return stripped in cls._PROTECTED_ADDRESS_FRAGMENTS

    @classmethod
    def _is_generic_address_phrase(cls, text: str) -> bool:
        stripped = text.strip("，,。！？；;：:、 \t\n\r()（）[]【】")
        return any(re.search(pattern, stripped) for pattern in cls._GENERIC_ADDRESS_PATTERNS)

    @classmethod
    def _drops_alphanumeric_building_marker(cls, original: str, candidate: str) -> bool:
        original_markers = set(cls._ALNUM_BUILDING_PATTERN.findall(original))
        if not original_markers:
            return False
        candidate_markers = set(cls._ALNUM_BUILDING_PATTERN.findall(candidate))
        return not original_markers <= candidate_markers

    @classmethod
    def _drops_trailing_building_marker(cls, original: str, candidate: str) -> bool:
        marker = cls._TRAILING_BUILDING_MARKER_PATTERN.search(original)
        if not marker:
            return False
        return marker.group(0) not in candidate

    @classmethod
    def _should_replace_candidate(cls, original: str, candidate: HotwordEntry) -> bool:
        if cls._is_generic_address_phrase(original):
            return False
        if candidate.source_key.startswith("db:") and len(candidate.word) > len(original):
            return False
        if cls._drops_alphanumeric_building_marker(original, candidate.word):
            return False
        if cls._drops_trailing_building_marker(original, candidate.word):
            return False
        if len(original) <= 4 and len(candidate.word) <= 4 and not (set(original) & set(candidate.word)):
            return False
        return True

    def _is_valid_hotword(self, text: str) -> bool:
        py = _compact(text)
        return any(
            eid in self.entries and self.entries[eid].word == text
            for eid in self.pinyin_index.get(py, set())
        )

    @classmethod
    def _address_likelihood_score(cls, text: str, question_text: str = "") -> float:
        """Score whether this turn is asking for or stating an address."""
        score = 0.0
        if any(pattern in question_text for pattern in cls._ADDRESS_QUESTION_PATTERNS):
            score += 0.45
        if any(prefix in text for prefix in cls._ADDRESS_INTENT_PREFIXES):
            score += 0.35
        if cls._COMPACT_ADDRESS_ENTITY_PATTERN.search(text):
            score += 0.35

        marker_hits = 0
        for marker in cls._ADDRESS_ENTITY_MARKERS:
            if marker in text:
                marker_hits += 1
        score += min(0.45, marker_hits * 0.12)
        return min(1.0, score)

    @classmethod
    def _should_run_address_correction(cls, text: str, question_text: str = "") -> bool:
        return cls._address_likelihood_score(text, question_text) >= 0.35

    @staticmethod
    def _iter_address_runs(text: str):
        start = None
        for i, ch in enumerate(text):
            is_addr_char = ("一" <= ch <= "鿿") or (ch.isascii() and ch.isalnum())
            if is_addr_char:
                if start is None:
                    start = i
            else:
                if start is not None:
                    yield start, i, text[start:i]
                    start = None
        if start is not None:
            yield start, len(text), text[start:]

    @staticmethod
    def _strip_speech_prefix(run: str, start: int) -> tuple[int, str]:
        for prefix in ("我在", "在"):
            if run.startswith(prefix) and len(run) > len(prefix) + 1:
                return start + len(prefix), run[len(prefix):]
        return start, run

    def _whole_phrase_replacements(self, text: str) -> list[dict]:
        """Find complete named-address spans without relying on jieba token cuts."""
        candidates: list[dict] = []
        for run_start, _run_end, run in self._iter_address_runs(text):
            run_start, run = self._strip_speech_prefix(run, run_start)
            if len(run) < 3:
                continue
            max_window = min(12, len(run))
            for size in range(max_window, 2, -1):
                for off in range(0, len(run) - size + 1):
                    fragment = run[off:off + size]
                    if any(c.isdigit() for c in fragment) and not self._ALNUM_BUILDING_PATTERN.search(fragment):
                        continue
                    if self._is_protected_fragment(fragment) or self._is_valid_hotword(fragment):
                        continue
                    matches = self._fuzzy_match(
                        fragment,
                        max_dist=2,
                        require_char_overlap=True,
                    )
                    matches = [
                        (entry, dist) for entry, dist in matches
                        if entry.word != fragment and len(entry.word) >= len(fragment) - 1
                    ]
                    if not matches:
                        continue
                    scored = self._score_candidates(
                        matches, [run], target_len=len(fragment), original_text=fragment
                    )
                    scored = [(entry, score + min(0.15, len(entry.word) * 0.01))
                              for entry, score in scored]
                    scored.sort(key=lambda x: (x[1], len(x[0].word)), reverse=True)
                    if not scored or scored[0][1] <= 0.72:
                        continue
                    best, best_score = scored[0]
                    if not self._should_replace_candidate(fragment, best):
                        continue
                    start = run_start + off
                    end = start + len(fragment)
                    candidates.append({
                        "span": (start, end),
                        "original": fragment,
                        "corrected": best.word,
                        "score": round(best_score, 3),
                        "entry_id": best.id,
                        "method": "whole_phrase",
                    })
        candidates.sort(key=lambda r: (r["span"][0], -(r["span"][1] - r["span"][0]), -r["score"]))
        selected: list[dict] = []
        used: set[int] = set()
        for cand in candidates:
            span = set(range(cand["span"][0], cand["span"][1]))
            if span & used:
                continue
            selected.append(cand)
            used.update(span)
        return selected

    @staticmethod
    def _has_address_context(text: str) -> bool:
        address_terms = (
            "地址", "位置", "哪里", "哪儿", "在哪", "具体在", "街道", "小区",
            "社区", "村", "镇", "路", "街", "号", "栋", "楼", "区",
        )
        return any(term in text for term in address_terms)

    def _should_apply_pair(self, pair: dict, text: str, idx: int,
                           question_text: str = "") -> bool:
        """Gate ambiguous expert pairs that are common non-address words."""
        err = pair.get("error", "")
        cor = pair.get("correct", "")
        if err == "接到" and cor == "街道":
            return self._has_address_context(question_text) or self._has_address_context(text)
        # Single-char non-digit pairs: only apply when adjacent to a digit (e.g. 5好→5号)
        if len(err) == 1 and len(cor) == 1 and not err.isdigit():
            if idx > 0 and text[idx - 1].isdigit():
                return True
            if idx + 1 < len(text) and text[idx + 1].isdigit():
                return True
            return False
        return True

    def _correct_text(self, asr_text: str, question_text: str = "") -> tuple[str, list[dict]]:
        """核心纠错逻辑。Phase 0: 专家纠错对查表; Phase 1: 拼音 Hash 匹配。"""
        replacements = []

        # Phase 0: Direct correction-pair lookup (expert-curated, high precision)
        corrected_text = asr_text
        replaced_spans_phase0: set[int] = set()
        for pair in self._correction_pairs:
            err = pair['error']
            cor = pair['correct']
            pos = 0
            while True:
                idx = corrected_text.find(err, pos)
                if idx == -1:
                    break
                # Check no overlap with existing replacements
                span_range = set(range(idx, idx + len(err)))
                if not (span_range & replaced_spans_phase0) and self._should_apply_pair(pair, corrected_text, idx, question_text):
                    replacements.append({
                        "span": (idx, idx + len(err)),
                        "original": err,
                        "corrected": cor,
                        "score": 0.95,
                        "entry_id": f"pair_{err}",
                        "method": "expert_pair",
                    })
                    replaced_spans_phase0.update(span_range)
                    # Apply replacement in-place for subsequent searches
                    corrected_text = (corrected_text[:idx] + cor +
                                      corrected_text[idx + len(err):])
                    pos = idx + len(cor)
                else:
                    pos = idx + 1

        # Phase 1: Pinyin hash matching via jieba tokenization
        if not self.pinyin_index or jieba is None or lazy_pinyin is None:
            if replacements:
                return corrected_text, replacements
            return asr_text, []

        if not self._should_run_address_correction(corrected_text, question_text):
            return corrected_text, replacements

        tokens = list(jieba.tokenize(corrected_text))
        print(f"[asr-tokens] {[(t[0], t[1], t[2]) for t in tokens]}")
        tokens.sort(key=lambda t: t[1])

        replaced_positions: set[int] = set()

        phase0_replacements = replacements[:]

        whole_phrase_replacements = self._whole_phrase_replacements(corrected_text)
        if whole_phrase_replacements:
            replacements.extend(whole_phrase_replacements)
            for r in whole_phrase_replacements:
                replaced_positions.update(range(r["span"][0], r["span"][1]))

        # Phase 0.5: Sliding-window combine adjacent 1-2 char tokens for homophone check
        # (catches cases like 借+到→街道 where jieba splits the word into single chars)
        for win_size in (3, 2):
            for i in range(len(tokens) - win_size + 1):
                window = tokens[i:i + win_size]
                if self._window_crosses_boundary(window):
                    continue
                # Only process if at least one token is short (1-2 chars)
                if all(len(t[0]) > 2 for t in window):
                    continue
                combined = ''.join(t[0] for t in window)
                if len(combined) < 2 or len(combined) > 8:
                    continue
                if self._is_protected_fragment(combined):
                    continue
                py = _compact(combined)
                ids = self.pinyin_index.get(py, set())
                hits = [self.entries[eid] for eid in ids if eid in self.entries]
                hits = [h for h in hits if h.word != combined]
                if not hits:
                    continue
                # Check no overlap with existing
                span = set(range(window[0][1], window[-1][2]))
                if span & replaced_positions:
                    continue
                # Score
                context = [t[0] for t in tokens[max(0,i-5):i+win_size+5]]
                scored = self._score_candidates(
                    [(h, 0) for h in hits], context,
                    target_len=len(combined), original_text=combined)
                scored = [(h, s + (0.15 if set(combined) & set(h.word) else 0.0))
                          for h, s in scored]
                scored.sort(key=lambda x: x[1], reverse=True)
                if scored and scored[0][1] > 0.30:
                    best, best_score = scored[0]
                    if not self._should_replace_candidate(combined, best):
                        continue
                    start, end = window[0][1], window[-1][2]
                    corrected_text = (corrected_text[:start] + best.word +
                                      corrected_text[end:])
                    replacements.append({
                        "span": (start, start + len(best.word)),
                        "original": combined,
                        "corrected": best.word,
                        "score": round(best_score, 3),
                        "entry_id": best.id,
                        "method": "window_combine",
                    })
                    replaced_positions.update(range(start, start + len(best.word)))

        window_replacements = [
            r for r in replacements
            if r['method'] in ('whole_phrase', 'window_combine')
        ]
        replacements = list(window_replacements)  # keep early results for text reconstruction

        # Phase 0.6: Fuzzy match adjacent address tokens against full hotword entries.
        # This catches near-homophone address phrases such as "长沙新湾" -> "长沙新苑".
        fuzzy_window_replacements: list[dict] = []
        if self._has_address_context(question_text) or self._has_address_context(corrected_text):
            for win_size in (4, 3, 2):
                if len(tokens) < win_size:
                    continue
                for i in range(len(tokens) - win_size + 1):
                    window = tokens[i:i + win_size]
                    if self._window_crosses_boundary(window):
                        continue
                    combined = ''.join(t[0] for t in window)
                    if len(combined) < 3 or len(combined) > 10:
                        continue
                    if self._is_protected_fragment(combined):
                        continue
                    if self._is_valid_hotword(combined):
                        continue
                    if any(c.isdigit() for c in combined):
                        continue

                    start, end = window[0][1], window[-1][2]
                    span = set(range(start, end))
                    if span & replaced_positions:
                        continue

                    candidates = self._fuzzy_match(
                        combined,
                        max_dist=2,
                        require_char_overlap=True,
                    )
                    candidates = [(entry, dist) for entry, dist in candidates
                                  if entry.word != combined and len(entry.word) >= len(combined) - 1]
                    if not candidates:
                        continue

                    context = [t[0] for t in tokens[max(0, i - 5):i + win_size + 5]]
                    scored = self._score_candidates(
                        candidates, context,
                        target_len=len(combined), original_text=combined)
                    scored = [(h, s + (0.2 if set(combined) & set(h.word) else 0.0))
                              for h, s in scored]
                    scored.sort(key=lambda x: x[1], reverse=True)
                    if not scored or scored[0][1] <= 0.60:
                        continue

                    best, best_score = scored[0]
                    if not self._should_replace_candidate(combined, best):
                        continue
                    fuzzy_window_replacements.append({
                        "span": (start, end),
                        "original": combined,
                        "corrected": best.word,
                        "score": round(best_score, 3),
                        "entry_id": best.id,
                        "method": "fuzzy_window",
                    })
                    replaced_positions.update(span)

        replacements.extend(fuzzy_window_replacements)

        for token_word, token_start, token_end in tokens:
            # Skip already-replaced spans
            if any(p in replaced_positions for p in range(token_start, token_end)):
                continue
            # Skip single-char tokens
            if len(token_word) <= 1:
                continue
            # Skip tokens with digits (avoid matching phone numbers, door numbers, etc.)
            if any(c.isdigit() for c in token_word):
                continue
            if self._is_protected_fragment(token_word):
                continue

            # Exact pinyin hash match
            hits = self._lookup_pinyin(token_word)
            if token_word in ('接到', '街道', '粤海'):
                print(f"[asr-trace] token='{token_word}' py={_compact(token_word)} hits={[(h.id, h.word) for h in hits]}")
            if not hits:
                continue

            # Filter: entry word must differ from token word (no self-match)
            hits = [h for h in hits if h.word != token_word]
            if not hits:
                continue

            # Score candidates (with overlap bonus, not hard filter)
            token_chars = set(token_word)
            context = [t[0] for t in tokens
                      if abs(t[1] - token_start) <= 10]
            scored = self._score_candidates(
                [(h, 0) for h in hits], context,
                target_len=len(token_word), original_text=token_word)
            # Apply character overlap as a score boost, not a hard gate
            scored = [(h, s + (0.15 if token_chars & set(h.word) else 0.0))
                      for h, s in scored]
            scored.sort(key=lambda x: x[1], reverse=True)
            if not scored or scored[0][1] <= 0.30:
                continue

            best_entry, best_score = scored[0]

            if not self._should_replace_candidate(token_word, best_entry):
                continue

            if self._is_protected_fragment(token_word):
                continue

            # Preserve address category words. "校区" and "小区" are homophones,
            # but changing a campus into a residential compound is not a safe correction.
            if token_word.endswith("校区") and best_entry.word.endswith("小区"):
                continue

            # Don't replace if original token is itself a valid hotword
            # (prevents "电器"→"电气" type false corrections on correct text)
            token_pinyin = _compact(token_word)
            if token_pinyin in self.pinyin_index:
                token_matches = self.pinyin_index[token_pinyin]
                token_is_valid = any(
                    eid in self.entries and self.entries[eid].word == token_word
                    for eid in token_matches
                )
                if token_is_valid:
                    continue

            # Apply replacement
            best_span = (token_start, token_end)
            replacements.append({
                "span": best_span,
                "original": token_word,
                "corrected": best_entry.word,
                "score": round(best_score, 3),
                "entry_id": best_entry.id,
                "method": "exact",
            })
            for p in range(token_start, token_end):
                replaced_positions.add(p)

        # Build corrected text
        if not replacements:
            return corrected_text, phase0_replacements + replacements

        replacements.sort(key=lambda r: r["span"][0])

        # Phase 0 replacements are already applied to corrected_text.
        # Build final result by applying Phase 1 replacements on top.
        result_parts = []
        last_end = 0
        for r in replacements:
            start, end = r["span"]
            if start > last_end:
                result_parts.append(corrected_text[last_end:start])
            result_parts.append(r["corrected"])
            last_end = end
        if last_end < len(corrected_text):
            result_parts.append(corrected_text[last_end:])

        # Deduplicate: replacements already includes window_replacements
        all_replacements = phase0_replacements + replacements
        seen = set()
        unique = []
        for r in all_replacements:
            key = (r['span'][0], r['span'][1], r['corrected'])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return ''.join(result_parts), unique

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def correct_full(self, asr_text: str, question_text: str = "") -> CorrectionResult:
        """全量模式纠错（使用合并的全部索引）。"""
        if self.mode != "full" or not self.pinyin_index:
            self._activate_full()
        t0 = time.perf_counter()
        corrected, replacements = self._correct_text(asr_text, question_text)
        elapsed = (time.perf_counter() - t0) * 1000
        return CorrectionResult(
            original=asr_text, corrected=corrected,
            replacements=replacements, elapsed_ms=elapsed,
            pools_loaded=[str(len(self._subset_indices)) + " subsets (preloaded)"],
        )

    def correct_dynamic(self, asr_text: str, question_text: str) -> CorrectionResult:
        """动态模式纠错（从预加载数据中激活对应子集索引，无磁盘 I/O）。"""
        t0 = time.perf_counter()
        self._activate_by_keywords(question_text)
        corrected, replacements = self._correct_text(asr_text, question_text)
        # Debug: log activation details
        jiedao_ids = self.pinyin_index.get('jiedao', set())
        jiedao_words = [self.entries[eid].word for eid in jiedao_ids if eid in self.entries]
        print(f"[asr-debug] question='{question_text[:60]}' entries={len(self.entries)} jiedao={jiedao_words} text='{asr_text[:40]}' corrected='{corrected[:40]}' reps={len(replacements)}")
        elapsed = (time.perf_counter() - t0) * 1000
        return CorrectionResult(
            original=asr_text, corrected=corrected,
            replacements=replacements, elapsed_ms=elapsed,
            pools_loaded=[str(len(self.entries)) + " entries activated"],
        )
