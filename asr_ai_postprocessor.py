"""AI postprocessing for completed ASR calls."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import http.client
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
for import_root in (BASE_DIR, PROJECT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))
CORRECTION_DIR = (
    BASE_DIR / "correction"
    if (BASE_DIR / "correction").is_dir()
    else PROJECT_ROOT / "correction"
)
FENCE_PATTERN = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
ASR_TEXT_REPLACEMENTS = (
    ("一77栋", "一期七栋"),
)

LOG = logging.getLogger("asr-ai-postprocessor")


@dataclass(frozen=True)
class TranscriptTurn:
    segment_id: str
    speaker: str
    direction: str
    text: str
    start_time_ms: int = 0
    end_time_ms: int = 0


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _speaker_label(speaker: str, direction: str = "") -> str:
    normalized = (speaker or "").strip().lower()
    if normalized == "caller" or direction == "inbound":
        return "报警人"
    if normalized == "agent" or direction == "outbound":
        return "接警员"
    return speaker or "未知"


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _apply_known_text_replacements(text: str) -> str:
    result = str(text or "")
    for source, target in ASR_TEXT_REPLACEMENTS:
        result = result.replace(source, target)
    return result


def _apply_proxy_env() -> None:
    proxy = (os.getenv("ASR_LLM_PROXY", "").strip() or os.getenv("LLM_PROXY", "").strip())
    if not proxy:
        return
    os.environ["LLM_PROXY"] = proxy
    os.environ["http_proxy"] = proxy
    os.environ["https_proxy"] = proxy
    os.environ["HTTP_PROXY"] = proxy
    os.environ["HTTPS_PROXY"] = proxy


def _open_llm_request(req: urllib.request.Request, *, timeout: int):
    proxy = (os.getenv("ASR_LLM_PROXY", "").strip() or os.getenv("LLM_PROXY", "").strip())
    if not proxy:
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({
        "http": proxy,
        "https": proxy,
    }))
    with _without_no_proxy():
        return opener.open(req, timeout=timeout)


@contextmanager
def _without_no_proxy():
    saved = {key: os.environ.get(key) for key in ("no_proxy", "NO_PROXY")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_local_env() -> None:
    for env_path in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _strip_markdown_fence(text: str) -> str:
    return re.sub(FENCE_PATTERN, "", text).strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_markdown_fence(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    in_string = False
    escaped = False
    depth = 0
    start = -1
    for idx, ch in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = cleaned[start:idx + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
                start = -1
    raise ValueError(f"无法解析 ASR 修正 JSON: {text[:200]}")


def _get_llm_thinking_type() -> str:
    value = os.getenv("ASR_LLM_THINKING_TYPE", os.getenv("LLM_THINKING_TYPE", "disabled")).strip().lower()
    return value if value in {"disabled", "enabled"} else "disabled"


def call_asr_llm_api(prompt: str) -> tuple[dict, float]:
    _load_local_env()
    _apply_proxy_env()
    base_url = (os.getenv("ASR_LLM_BASE_URL", "").strip() or os.getenv("LLM_BASE_URL", "").strip()).rstrip("/")
    api_key = os.getenv("ASR_LLM_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("ASR_LLM_MODEL", "").strip() or os.getenv("LLM_MODEL", "").strip()
    timeout = int(os.getenv("ASR_LLM_TIMEOUT_SECONDS", os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    retries = int(os.getenv("ASR_LLM_RETRIES", "1"))
    max_tokens = int(os.getenv("ASR_LLM_MAX_TOKENS", os.getenv("LLM_MAX_TOKENS", "512")))
    if not base_url or not api_key or not model:
        raise ValueError("请配置 ASR_LLM_BASE_URL / ASR_LLM_API_KEY / ASR_LLM_MODEL，或通用 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是 119 消防报警电话的 ASR 修正助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    thinking_type = _get_llm_thinking_type()
    if thinking_type == "enabled":
        payload["thinking"] = {"type": thinking_type}
    req = urllib.request.Request(
        url=f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    start = time.perf_counter()
    last_error = None
    for attempt in range(retries + 1):
        try:
            with _open_llm_request(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"ASR LLM HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, http.client.RemoteDisconnected, TimeoutError) as e:
            last_error = e
            if attempt >= retries:
                raise
            time.sleep(0.2 * (attempt + 1))
    else:
        raise RuntimeError(f"ASR LLM request failed: {last_error}")
    elapsed_ms = (time.perf_counter() - start) * 1000
    data = json.loads(body)
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, dict):
        return content, elapsed_ms
    return _extract_json_object(str(content)), elapsed_ms


class AsrCallPostprocessor:
    """Correct and extract one ended callId transcript with an LLM."""

    def __init__(self, *, enabled: bool = False, llm_callable: Callable[[str], tuple[dict, float]] | None = None):
        self.enabled = enabled
        self._llm_call = llm_callable or call_asr_llm_api

    def build_event(self, *, call_id: str, callfrom: str, callto: str, turns: list[TranscriptTurn]) -> dict | None:
        if not self.enabled:
            return None
        cleaned_turns = [turn for turn in turns if (turn.text or "").strip()]
        if not cleaned_turns:
            return None

        original_text = self._join_turns(cleaned_turns)
        prompt = self._build_prompt(cleaned_turns, original_text)
        _apply_proxy_env()
        payload, elapsed_ms = self._llm_call(prompt)
        if not isinstance(payload, dict):
            raise RuntimeError(f"AI correction returned non-object payload: {payload!r}")

        corrected_text = _apply_known_text_replacements(
            str(payload.get("correctedText") or original_text).strip() or original_text
        )
        return {
            "event": "call.corrected",
            "callId": call_id,
            "callfrom": callfrom,
            "callto": callto,
            "originalText": original_text,
            "correctedText": corrected_text,
            "turns": self._normalize_turn_results(payload.get("turns"), cleaned_turns),
            "llmElapsedMs": round(float(elapsed_ms), 1),
        }

    def build_turn_event(self, *, call_id: str, callfrom: str, callto: str, turn: TranscriptTurn) -> dict | None:
        if not self.enabled or not (turn.text or "").strip():
            return None
        original_text = f"{_speaker_label(turn.speaker, turn.direction)}：{turn.text.strip()}"
        prompt = self._build_turn_prompt(turn)
        _apply_proxy_env()
        payload, elapsed_ms = self._llm_call(prompt)
        if not isinstance(payload, dict):
            raise RuntimeError(f"AI correction returned non-object payload: {payload!r}")

        corrected_text = _apply_known_text_replacements(
            str(payload.get("correctedText") or turn.text).strip() or turn.text
        )
        if isinstance(payload.get("turns"), list):
            turns = self._normalize_turn_results(payload.get("turns"), [turn])
        else:
            turns = [{
                "segmentId": turn.segment_id,
                "speaker": turn.speaker,
                "direction": turn.direction,
                "originalText": turn.text,
                "correctedText": corrected_text,
                "keywords": _clean_list(payload.get("keywords")),
            }]
        event = {
            "event": "call.corrected",
            "callId": call_id,
            "callfrom": callfrom,
            "callto": callto,
            "originalText": original_text,
            "correctedText": corrected_text,
            "turns": turns,
            "llmElapsedMs": round(float(elapsed_ms), 1),
        }
        event["correctionScope"] = "turn"
        event["segmentId"] = turn.segment_id
        event["speaker"] = turn.speaker
        event["direction"] = turn.direction
        event["startTimeMs"] = turn.start_time_ms
        event["endTimeMs"] = turn.end_time_ms
        event["durationMs"] = max(0, turn.end_time_ms - turn.start_time_ms)
        return event

    def _join_turns(self, turns: list[TranscriptTurn]) -> str:
        lines = []
        for turn in turns:
            label = _speaker_label(turn.speaker, turn.direction)
            lines.append(f"{label}：{turn.text.strip()}")
        return "\n".join(lines)

    def _normalize_turn_results(self, value: Any, source_turns: list[TranscriptTurn]) -> list[dict]:
        if isinstance(value, list):
            normalized = []
            for idx, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                fallback = source_turns[idx] if idx < len(source_turns) else None
                normalized.append({
                    "segmentId": str(item.get("segmentId") or (fallback.segment_id if fallback else "")),
                    "speaker": str(item.get("speaker") or (fallback.speaker if fallback else "")),
                    "direction": str(item.get("direction") or (fallback.direction if fallback else "")),
                    "originalText": str(item.get("originalText") or (fallback.text if fallback else "")),
                    "correctedText": _apply_known_text_replacements(str(item.get("correctedText") or item.get("originalText") or (fallback.text if fallback else ""))),
                    "keywords": _clean_list(item.get("keywords")),
                })
            if normalized:
                return normalized

        return [
            {
                "segmentId": turn.segment_id,
                "speaker": turn.speaker,
                "direction": turn.direction,
                "originalText": turn.text,
                "correctedText": _apply_known_text_replacements(turn.text),
                "keywords": [],
            }
            for turn in source_turns
        ]

    def _build_prompt(self, turns: list[TranscriptTurn], original_text: str) -> str:
        turn_rows = []
        for turn in turns:
            turn_rows.append(
                f"- segmentId={turn.segment_id} speaker={turn.speaker} "
                f"direction={turn.direction} text={turn.text}"
            )
        turn_text = "\n".join(turn_rows)
        return f"""你是 119 消防报警电话的 ASR 后处理助手。请对整通电话转写做一次后处理。

任务：
1. 纠正明显的 ASR 识别错误，只改消防报警场景中**有十足把握**的错词。**不确定就保留原文，禁止猜测**。
2. 不要补充原文没有的信息。地址、楼栋、被困人数、伤情、灾情必须有明确文本依据才输出。
3. 每个 turn 都要单独提取 keywords，关键词偏向地址、单位、楼栋、楼层、房号、被困、伤情、火灾、烟、危险品、派车等。
4. 不要输出整通全局 keywords，不要输出 entities，不做结构化字段抽取。
5. 输出每个 turn 的 correctedText 和 keywords，segmentId 必须保留。
6. **禁止行为**：看到不认识的词/地名/单位名，**不要去猜成热词列表中相似的词**。只有当发音明显相近且有上下文支撑时才改。

高频消防/地点热词参考：
- 消防灾情：火灾、着火、冒烟、浓烟、明火、阴燃、爆炸、燃气泄漏、煤气泄漏、电动车、电瓶车、配电房、变压器、消防通道、消火栓、喷淋、楼梯间、疏散通道、被困、受伤。
- 地点/单位：天维尔有限公司、科兴软件园、软件园、科技园、工业园、产业园、写字楼、办公楼、宿舍楼、地下室、地下车库、商铺、厂房、仓库。
- 地址常见纠错：`一77栋` 通常应按小区/园区地址修正为 `一期七栋`；类似“期/栋/座/单元/楼层/房号”的数字混淆，只在上下文确认为地址时修正。
- 使用规则：这些词只是 ASR 近音纠错**候选参考**。只有当原文发音相近且上下文支持时才改。**没有把握就保留原文，禁止把不认识的字词强行映射到热词**。上面列出的地点/单位只是常见词参考，不是纠错目标列表。

整通原文：
{original_text}

分段原文：
{turn_text}

只输出 JSON 对象，格式如下：
{{
  "correctedText": "纠正后的整通文本",
  "turns": [
    {{
      "segmentId": "原 segmentId",
      "speaker": "caller 或 agent",
      "direction": "inbound 或 outbound",
      "originalText": "原句",
      "correctedText": "纠正后的句子",
      "keywords": ["关键词"]
    }}
  ]
}}
"""

    def _build_turn_prompt(self, turn: TranscriptTurn) -> str:
        label = _speaker_label(turn.speaker, turn.direction)
        return (
            "纠正119报警ASR单句并提取关键词。只改确定错词，不确定保留；不要补信息；"
            "不抽实体。关键词限地址/单位/楼栋楼层房号/被困伤情/火灾烟爆炸燃气危险品/派车。"
            "参考：火灾、着火、冒烟、浓烟、被困、受伤、天维尔、科兴软件园、高新、"
            "粤海街道、西里街道、凯莉花园、国人通讯大厦、软件园、一期七栋。"
            "只输出JSON：{\"correctedText\":\"纠正后的句子\",\"keywords\":[\"关键词\"]}。"
            f" speaker={turn.speaker} label={label} text={turn.text}"
        )



class AsrAddressHotwordPostprocessor:
    """Build call.corrected events with the local address hotword corrector."""

    def __init__(self, *, enabled: bool = False, mode: str = "dynamic", provider_name: str = "hotword"):
        self.enabled = enabled
        self.mode = mode if mode in {"dynamic", "full", "align"} else "dynamic"
        self.provider_name = provider_name
        self._corrector = None
        if self.enabled:
            self._corrector = self._create_corrector()

    def _create_corrector(self):
        try:
            from asr_api_use.text_filter import NOISE_WORDS
        except ModuleNotFoundError:
            from text_filter import NOISE_WORDS
        from core.asr_corrector import AsrCorrector

        return AsrCorrector(
            mode=self.mode,
            correction_dir=str(CORRECTION_DIR),
            noise_words=NOISE_WORDS,
        )

    def _correct_text(self, text: str) -> tuple[str, float, list[dict[str, Any]]]:
        if not self._corrector:
            return text, 0.0, []
        if self.mode == "full":
            result = self._corrector.correct_full(text, text)
        else:
            result = self._corrector.correct_dynamic(text, text)
        return result.corrected, float(result.elapsed_ms or 0.0), result.replacements

    def build_event(self, *, call_id: str, callfrom: str, callto: str, turns: list[TranscriptTurn]) -> dict | None:
        if not self.enabled:
            return None
        cleaned_turns = [turn for turn in turns if (turn.text or "").strip()]
        if not cleaned_turns:
            return None

        original_text = self._join_turns(cleaned_turns)
        normalized_turns = []
        corrected_lines = []
        total_elapsed_ms = 0.0
        replacements = []
        for turn in cleaned_turns:
            corrected_text, elapsed_ms, turn_replacements = self._correct_text(turn.text.strip())
            total_elapsed_ms += elapsed_ms
            replacements.extend(turn_replacements)
            corrected_lines.append(f"{_speaker_label(turn.speaker, turn.direction)}：{corrected_text}")
            normalized_turns.append({
                "segmentId": turn.segment_id,
                "speaker": turn.speaker,
                "direction": turn.direction,
                "originalText": turn.text,
                "correctedText": corrected_text,
                "keywords": [],
            })

        return {
            "event": "call.corrected",
            "callId": call_id,
            "callfrom": callfrom,
            "callto": callto,
            "originalText": original_text,
            "correctedText": "\n".join(corrected_lines),
            "turns": normalized_turns,
            "llmElapsedMs": round(total_elapsed_ms, 1),
            "correctionProvider": self.provider_name,
            "correctionMode": self.mode,
            "replacements": replacements,
        }

    def build_turn_event(self, *, call_id: str, callfrom: str, callto: str, turn: TranscriptTurn) -> dict | None:
        if not self.enabled or not (turn.text or "").strip():
            return None
        corrected_text, elapsed_ms, replacements = self._correct_text(turn.text.strip())
        event = {
            "event": "call.corrected",
            "callId": call_id,
            "callfrom": callfrom,
            "callto": callto,
            "originalText": f"{_speaker_label(turn.speaker, turn.direction)}：{turn.text.strip()}",
            "correctedText": corrected_text,
            "turns": [{
                "segmentId": turn.segment_id,
                "speaker": turn.speaker,
                "direction": turn.direction,
                "originalText": turn.text,
                "correctedText": corrected_text,
                "keywords": [],
            }],
            "llmElapsedMs": round(elapsed_ms, 1),
            "correctionProvider": self.provider_name,
            "correctionMode": self.mode,
            "replacements": replacements,
            "correctionScope": "turn",
            "segmentId": turn.segment_id,
            "speaker": turn.speaker,
            "direction": turn.direction,
            "startTimeMs": turn.start_time_ms,
            "endTimeMs": turn.end_time_ms,
            "durationMs": max(0, turn.end_time_ms - turn.start_time_ms),
        }
        return event

    def _join_turns(self, turns: list[TranscriptTurn]) -> str:
        lines = []
        for turn in turns:
            label = _speaker_label(turn.speaker, turn.direction)
            lines.append(f"{label}：{turn.text.strip()}")
        return "\n".join(lines)


class AsrAddressDbAlignPostprocessor(AsrAddressHotwordPostprocessor):
    """Build call.corrected events with the database pinyin-alignment corrector."""

    def __init__(self, *, enabled: bool = False, corrector: Any | None = None):
        self._injected_corrector = corrector
        super().__init__(enabled=enabled, mode="align", provider_name="db_align")

    def _create_corrector(self):
        if self._injected_corrector is not None:
            return self._injected_corrector
        from asr_address_align_corrector import AsrAddressAlignCorrector
        from asr_address_db import create_address_database_source

        terms = create_address_database_source().load_terms()
        return AsrAddressAlignCorrector(terms)

    def _correct_text(self, text: str) -> tuple[str, float, list[dict[str, Any]]]:
        if not self._corrector:
            return text, 0.0, []
        result = self._corrector.correct(text)
        return result.corrected, float(result.elapsed_ms or 0.0), result.replacements


class AsrLlmKeywordHighlighter:
    """Extract display keywords without allowing the LLM to rewrite text."""

    def __init__(self, *, llm_callable: Callable[[str], tuple[dict, float]] | None = None):
        self._llm_call = llm_callable or call_asr_llm_api

    def extract_keywords(
        self,
        *,
        original_text: str,
        corrected_text: str,
        speaker: str,
        direction: str,
    ) -> tuple[list[str], float | None, bool]:
        prompt = self._build_prompt(
            original_text=original_text,
            corrected_text=corrected_text,
            speaker=speaker,
            direction=direction,
        )
        try:
            payload, elapsed_ms = self._llm_call(prompt)
            if not isinstance(payload, dict):
                raise RuntimeError(f"LLM keyword response is not an object: {payload!r}")
            return _clean_list(payload.get("keywords")), round(float(elapsed_ms), 1), False
        except Exception:
            LOG.exception("LLM 高亮提取失败，保留地址库纠错文本")
            return [], None, True

    def _build_prompt(
        self,
        *,
        original_text: str,
        corrected_text: str,
        speaker: str,
        direction: str,
    ) -> str:
        label = _speaker_label(speaker, direction)
        return (
            "你是119报警电话的高亮关键词提取器。只提取高亮关键词，绝对不得纠正、改写、"
            "补充或删除任何文本。关键词限地址、单位、楼栋楼层房号、被困伤情、火灾烟、"
            "爆炸、燃气、危险品、派车；只返回原文或纠正后文本中明确存在的词。"
            "只输出JSON：{\"keywords\":[\"关键词\"]}。"
            f" speaker={speaker} label={label} original_text={original_text} corrected_text={corrected_text}"
        )


class AsrRuleKeywordHighlighter:
    """Extract display keywords from corrected text without network calls."""

    _ADDRESS_TERM_MAX_LENGTH = 12
    _CONTEXT_ADDRESS_MAX_LENGTH = 16
    _LOCATION_INTRODUCERS = (
        "我现在在", "我住在", "地址是在", "地址在", "地点在", "位置在", "我在", "位于",
    )
    _LOCATION_SUFFIXES = (
        "科技园", "软件园", "工业园", "产业园", "科学园", "小区", "花园", "园区",
        "大厦", "广场", "社区", "街道", "大道", "路", "街", "道", "村",
    )
    _CONTEXT_BOUNDARY_TERMS = (
        "发生", "出现", "正在", "里面", "这边", "那里", "需要", "请",
    )
    _GENERIC_CONTEXT_ADDRESSES = {
        "这个小区", "那个小区", "一个小区", "该小区", "本小区",
        "这个社区", "那个社区", "一个社区", "该社区", "本社区",
    }
    _INCIDENT_TERMS = (
        "有人被困", "人员被困", "燃气泄漏", "煤气泄漏", "天然气泄漏",
        "地下车库", "消防通道", "电动车", "电瓶车", "危险品", "化学品",
        "配电房", "变压器", "消火栓", "火灾", "着火", "起火", "冒烟",
        "浓烟", "明火", "阴燃", "爆炸", "被困", "受伤", "燃气", "煤气",
        "喷淋", "派车",
    )
    _ADDRESS_EXTENSION_RE = re.compile(
        r"(?:\d+号)?(?:[A-Za-z]\d{0,2}(?:栋|座|单元)|[0-9一二三四五六七八九十]+(?:号楼|栋|座|单元|层|楼|室))?"
    )

    def __init__(self, *, address_terms: list[str] | tuple[str, ...] | None = None):
        terms = {
            str(term or "").strip()
            for term in (address_terms or [])
            if 3 <= len(str(term or "").strip()) <= self._ADDRESS_TERM_MAX_LENGTH
        }
        self._address_terms = tuple(sorted(terms, key=lambda term: (-len(term), term)))

    def extract_keywords(
        self,
        *,
        original_text: str,
        corrected_text: str,
        speaker: str,
        direction: str,
    ) -> tuple[list[str], float, bool]:
        del original_text, speaker, direction
        started = time.perf_counter()
        text = str(corrected_text or "")
        matches = self._address_matches(text)
        matches.extend(self._context_address_matches(text))
        matches.extend(self._term_matches(text, self._INCIDENT_TERMS))
        matches.extend(self._structural_matches(text))
        selected = self._select_non_overlapping(matches)
        keywords = _clean_list([text[start:end] for start, end in selected])
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return keywords, elapsed_ms, False

    def _address_matches(self, text: str) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        for term in self._address_terms:
            start = text.find(term)
            while start >= 0:
                end = start + len(term)
                suffix = self._ADDRESS_EXTENSION_RE.match(text[end:])
                if suffix:
                    end += len(suffix.group(0))
                matches.append((start, end))
                start = text.find(term, start + 1)
        return matches

    def _context_address_matches(self, text: str) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        boundaries = self._INCIDENT_TERMS + self._CONTEXT_BOUNDARY_TERMS
        for introducer in self._LOCATION_INTRODUCERS:
            intro_start = text.find(introducer)
            while intro_start >= 0:
                address_start = intro_start + len(introducer)
                tail = text[address_start:address_start + self._CONTEXT_ADDRESS_MAX_LENGTH + 8]
                punctuation = re.search(r"[，,。.!！?？；;：:\s]", tail)
                if punctuation:
                    tail = tail[:punctuation.start()]

                boundary_positions = [
                    pos
                    for term in boundaries
                    if (pos := tail.find(term)) >= 0
                ]
                if boundary_positions:
                    tail = tail[:min(boundary_positions)]

                suffix_spans: list[tuple[int, int]] = []
                for suffix in self._LOCATION_SUFFIXES:
                    suffix_start = tail.find(suffix)
                    while suffix_start >= 0:
                        # Require a name before the generic suffix (e.g. 和 + 小区).
                        if suffix_start > 0:
                            suffix_spans.append((suffix_start, suffix_start + len(suffix)))
                        suffix_start = tail.find(suffix, suffix_start + 1)

                if suffix_spans:
                    first_suffix_start = min(start for start, _ in suffix_spans)
                    address_end_in_tail = max(
                        end for start, end in suffix_spans if start == first_suffix_start
                    )
                    # Allow adjacent compound suffixes such as 科技园 + 小区.
                    while True:
                        adjacent_ends = [
                            end for start, end in suffix_spans if start == address_end_in_tail
                        ]
                        if not adjacent_ends:
                            break
                        address_end_in_tail = max(adjacent_ends)
                    extension = self._ADDRESS_EXTENSION_RE.match(tail[address_end_in_tail:])
                    if extension:
                        address_end_in_tail += len(extension.group(0))
                    candidate = tail[:address_end_in_tail]
                    if (
                        3 <= len(candidate) <= self._CONTEXT_ADDRESS_MAX_LENGTH
                        and candidate not in self._GENERIC_CONTEXT_ADDRESSES
                    ):
                        matches.append((address_start, address_start + address_end_in_tail))

                intro_start = text.find(introducer, intro_start + 1)
        return matches

    @staticmethod
    def _term_matches(text: str, terms: tuple[str, ...]) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        for term in terms:
            start = text.find(term)
            while start >= 0:
                matches.append((start, start + len(term)))
                start = text.find(term, start + 1)
        return matches

    @classmethod
    def _structural_matches(cls, text: str) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        for match in cls._ADDRESS_EXTENSION_RE.finditer(text):
            if match.group(0):
                matches.append((match.start(), match.end()))
        return matches

    @staticmethod
    def _select_non_overlapping(matches: list[tuple[int, int]]) -> list[tuple[int, int]]:
        selected: list[tuple[int, int]] = []
        used: set[int] = set()
        for start, end in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
            span = set(range(start, end))
            if span & used:
                continue
            selected.append((start, end))
            used.update(span)
        return selected


class AsrAddressDbAlignLlmHighlightPostprocessor(AsrAddressDbAlignPostprocessor):
    """Address DB correction followed by LLM keyword extraction only."""

    highlight_provider = "llm"

    def __init__(
        self,
        *,
        enabled: bool = False,
        corrector: Any | None = None,
        llm_callable: Callable[[str], tuple[dict, float]] | None = None,
    ):
        self._keyword_highlighter = AsrLlmKeywordHighlighter(llm_callable=llm_callable)
        super().__init__(enabled=enabled, corrector=corrector)

    def build_event(self, *, call_id: str, callfrom: str, callto: str, turns: list[TranscriptTurn]) -> dict | None:
        event = super().build_event(call_id=call_id, callfrom=callfrom, callto=callto, turns=turns)
        return self._add_highlights(event)

    def build_turn_event(self, *, call_id: str, callfrom: str, callto: str, turn: TranscriptTurn) -> dict | None:
        event = super().build_turn_event(call_id=call_id, callfrom=callfrom, callto=callto, turn=turn)
        return self._add_highlights(event)

    def _add_highlights(self, event: dict | None) -> dict | None:
        if not event:
            return event

        db_elapsed_ms = round(float(event.get("llmElapsedMs") or 0.0), 1)
        total_highlight_elapsed_ms = 0.0
        highlight_succeeded = False
        highlight_failed = False
        for turn in event.get("turns", []):
            keywords, elapsed_ms, failed = self._keyword_highlighter.extract_keywords(
                original_text=str(turn.get("originalText") or ""),
                corrected_text=str(turn.get("correctedText") or ""),
                speaker=str(turn.get("speaker") or ""),
                direction=str(turn.get("direction") or ""),
            )
            turn["keywords"] = keywords
            if elapsed_ms is not None:
                highlight_succeeded = True
                total_highlight_elapsed_ms += elapsed_ms
            highlight_failed = highlight_failed or failed

        event["dbElapsedMs"] = db_elapsed_ms
        event["highlightElapsedMs"] = round(total_highlight_elapsed_ms, 1) if highlight_succeeded else None
        event["highlightFailed"] = highlight_failed
        event["highlightProvider"] = self.highlight_provider
        event["correctionProvider"] = f"db_align+{self.highlight_provider}_highlight"
        event["correctionMode"] = "align+keyword_highlight"
        if self.highlight_provider == "llm":
            event["llmHighlightElapsedMs"] = event["highlightElapsedMs"]
            # Keep the existing field accurate for existing monitor clients.
            event["llmElapsedMs"] = event["llmHighlightElapsedMs"]
            event["llmHighlightFailed"] = highlight_failed
            event["ruleHighlightElapsedMs"] = None
        else:
            event["ruleHighlightElapsedMs"] = event["highlightElapsedMs"]
            event["llmElapsedMs"] = None
            event["llmHighlightElapsedMs"] = None
            event["llmHighlightFailed"] = False
        return event


class AsrAddressDbAlignRuleHighlightPostprocessor(AsrAddressDbAlignLlmHighlightPostprocessor):
    """Address DB correction followed by local rule-based keyword extraction."""

    highlight_provider = "rule"
    _ADDRESS_HIGHLIGHT_SOURCES = {
        "loi_road.cn_name",
        "aoi_2.aoi_name",
        "aoi_2.alias_name",
        "aoi_3.aoi_name",
        "aoi_3_entrance_exit.name",
        "poi_1.building_name",
        "poi_1.short_name",
        "poi_1.aoi_name",
        "poi_1_entrance_exit.name",
    }

    def __init__(self, *, enabled: bool = False, corrector: Any | None = None):
        super().__init__(enabled=enabled, corrector=corrector)
        candidates = getattr(self._corrector, "candidates", []) or []
        self._keyword_highlighter = AsrRuleKeywordHighlighter(
            address_terms=[
                str(getattr(candidate, "term", ""))
                for candidate in candidates
                if not getattr(candidate, "source", "")
                or getattr(candidate, "source", "") in self._ADDRESS_HIGHLIGHT_SOURCES
            ],
        )

def create_asr_call_postprocessor():
    _load_local_env()
    provider = (
        os.getenv("ASR_CORRECTION_PROVIDER", "")
        or os.getenv("ASR_AI_CORRECTION_PROVIDER", "")
        # Keep correction local and deterministic unless LLM is explicitly
        # requested through ASR_CORRECTION_PROVIDER.
        or "db_align"
    ).strip().lower()
    enabled = _env_bool("ASR_AI_CORRECTION_ENABLED")
    if provider in {"db_align", "address_db", "address_align"}:
        highlight_provider = os.getenv("ASR_HIGHLIGHT_PROVIDER", "llm").strip().lower()
        if highlight_provider == "rule":
            return AsrAddressDbAlignRuleHighlightPostprocessor(enabled=enabled)
        if _env_bool("ASR_LLM_HIGHLIGHT_ENABLED", True):
            return AsrAddressDbAlignLlmHighlightPostprocessor(enabled=enabled)
        return AsrAddressDbAlignPostprocessor(enabled=enabled)
    if provider in {"hotword", "address", "address_hotword"}:
        mode = os.getenv("ASR_HOTWORD_CORRECTION_MODE", "dynamic").strip().lower()
        return AsrAddressHotwordPostprocessor(enabled=enabled, mode=mode)
    return AsrCallPostprocessor(enabled=enabled)
