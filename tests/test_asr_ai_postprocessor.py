import sys
import urllib.request
from pathlib import Path

ASR_DIR = Path(__file__).resolve().parents[1]
if str(ASR_DIR) not in sys.path:
    sys.path.insert(0, str(ASR_DIR))

from asr_ai_postprocessor import (
    AsrAddressDbAlignLlmHighlightPostprocessor,
    AsrAddressDbAlignPostprocessor,
    AsrAddressDbAlignRuleHighlightPostprocessor,
    AsrAddressHotwordPostprocessor,
    AsrCallPostprocessor,
    AsrRuleKeywordHighlighter,
    TranscriptTurn,
    call_asr_llm_api,
    create_asr_call_postprocessor,
)


def test_empty_transcript_returns_none():
    processor = AsrCallPostprocessor(enabled=True, llm_callable=lambda prompt: ({}, 1.0))

    assert processor.build_event(call_id="c1", callfrom="8001", callto="119", turns=[]) is None


def test_build_event_normalizes_llm_result():
    seen = {}

    def fake_llm(prompt):
        seen["prompt"] = prompt
        return {
            "correctedText": "报警人：我这里发生了火灾。接警员：好，我马上派车过来。",
            "turns": [
                {
                    "segmentId": "caller-0001",
                    "speaker": "caller",
                    "originalText": "我这里发生了活该",
                    "correctedText": "我这里发生了火灾",
                    "keywords": ["火灾"],
                }
            ],
        }, 123.4

    processor = AsrCallPostprocessor(enabled=True, llm_callable=fake_llm)
    event = processor.build_event(
        call_id="call-1",
        callfrom="8001",
        callto="119",
        turns=[
            TranscriptTurn(
                segment_id="caller-0001",
                speaker="caller",
                direction="inbound",
                text="我这里发生了活该",
                start_time_ms=100,
                end_time_ms=1200,
            ),
            TranscriptTurn(
                segment_id="agent-0002",
                speaker="agent",
                direction="outbound",
                text="好，我马上派出过来",
                start_time_ms=1300,
                end_time_ms=2200,
            ),
        ],
    )

    assert "我这里发生了活该" in seen["prompt"]
    assert "天维尔有限公司" in seen["prompt"]
    assert "科兴软件园" in seen["prompt"]
    assert "一77栋" in seen["prompt"]
    assert "一期七栋" in seen["prompt"]
    assert "每个 turn" in seen["prompt"]
    assert "不要输出整通全局 keywords" in seen["prompt"]
    assert "不要输出 entities" in seen["prompt"]
    assert event["event"] == "call.corrected"
    assert event["callId"] == "call-1"
    assert event["callfrom"] == "8001"
    assert event["callto"] == "119"
    assert event["originalText"] == "报警人：我这里发生了活该\n接警员：好，我马上派出过来"
    assert event["correctedText"] == "报警人：我这里发生了火灾。接警员：好，我马上派车过来。"
    assert "keywords" not in event
    assert "entities" not in event
    assert event["turns"][0]["correctedText"] == "我这里发生了火灾"
    assert event["turns"][0]["keywords"] == ["火灾"]
    assert event["llmElapsedMs"] == 123.4


def test_disabled_processor_returns_none():
    called = {"llm": False}

    def fake_llm(prompt):
        called["llm"] = True
        return {}, 1.0

    processor = AsrCallPostprocessor(enabled=False, llm_callable=fake_llm)
    event = processor.build_event(
        call_id="call-1",
        callfrom="8001",
        callto="119",
        turns=[TranscriptTurn(segment_id="s1", speaker="caller", direction="inbound", text="着火了")],
    )

    assert event is None
    assert called["llm"] is False


def test_build_turn_event_marks_turn_scope():
    def fake_llm(prompt):
        assert "我这里发生了活该" in prompt
        return {
            "correctedText": "报警人：我这里发生了火灾",
            "turns": [
                {
                    "segmentId": "caller-0001",
                    "speaker": "caller",
                    "direction": "inbound",
                    "originalText": "我这里发生了活该",
                    "correctedText": "我这里发生了火灾",
                    "keywords": ["火灾"],
                }
            ],
        }, 45.6

    processor = AsrCallPostprocessor(enabled=True, llm_callable=fake_llm)
    event = processor.build_turn_event(
        call_id="call-turn",
        callfrom="8001",
        callto="119",
        turn=TranscriptTurn(
            segment_id="caller-0001",
            speaker="caller",
            direction="inbound",
            text="我这里发生了活该",
            start_time_ms=100,
            end_time_ms=900,
        ),
    )

    assert event["event"] == "call.corrected"
    assert event["correctionScope"] == "turn"
    assert event["segmentId"] == "caller-0001"
    assert event["speaker"] == "caller"
    assert event["direction"] == "inbound"
    assert event["durationMs"] == 800
    assert event["turns"][0]["correctedText"] == "我这里发生了火灾"
    assert event["turns"][0]["keywords"] == ["火灾"]

def test_asr_llm_proxy_overrides_global_proxy_env(monkeypatch):
    seen = {}
    monkeypatch.setenv("ASR_LLM_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("LLM_PROXY", "http://bad-proxy:7890")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    def fake_llm(prompt):
        import os
        seen["LLM_PROXY"] = os.environ.get("LLM_PROXY")
        seen["https_proxy"] = os.environ.get("https_proxy")
        seen["HTTPS_PROXY"] = os.environ.get("HTTPS_PROXY")
        return {"correctedText": "报警人：着火了", "turns": []}, 1.0

    processor = AsrCallPostprocessor(enabled=True, llm_callable=fake_llm)
    event = processor.build_event(
        call_id="call-1",
        callfrom="8001",
        callto="119",
        turns=[TranscriptTurn(segment_id="s1", speaker="caller", direction="inbound", text="着火了")],
    )

    assert event["correctedText"] == "报警人：着火了"
    assert seen["LLM_PROXY"] == "http://127.0.0.1:7890"
    assert seen["https_proxy"] == "http://127.0.0.1:7890"
    assert seen["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_call_asr_llm_api_uses_explicit_asr_proxy_even_when_no_proxy_matches(monkeypatch):
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"correctedText\\":\\"ok\\",\\"keywords\\":[]}"}}]}'

    class FakeOpener:
        def open(self, req, timeout):
            import os
            seen["no_proxy_during_open"] = os.environ.get("no_proxy", "")
            seen["NO_PROXY_during_open"] = os.environ.get("NO_PROXY", "")
            seen["opened_by_proxy_opener"] = True
            seen["timeout"] = timeout
            return FakeResponse()

    def fake_build_opener(handler):
        seen["handler"] = handler
        return FakeOpener()

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("urlopen would obey no_proxy; explicit opener is required")

    monkeypatch.setenv("ASR_LLM_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("no_proxy", "api.deepseek.com")
    monkeypatch.setenv("NO_PROXY", "api.deepseek.com")
    monkeypatch.setenv("ASR_LLM_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("ASR_LLM_RETRIES", "0")
    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    payload, elapsed_ms = call_asr_llm_api("请纠正：着火了")

    assert payload["correctedText"] == "ok"
    assert seen["opened_by_proxy_opener"] is True
    assert "api.deepseek.com" not in seen["no_proxy_during_open"]
    assert "api.deepseek.com" not in seen["NO_PROXY_during_open"]
    assert seen["timeout"] == 7
    assert elapsed_ms >= 0


def test_known_address_replacement_applies_to_corrected_text():
    def fake_llm(prompt):
        return {
            "correctedText": "报警人：我在科兴软件园一77栋。",
            "turns": [
                {
                    "segmentId": "caller-0001",
                    "speaker": "caller",
                    "direction": "inbound",
                    "originalText": "我在科兴软件园一77栋。",
                    "correctedText": "我在科兴软件园一77栋。",
                    "keywords": ["科兴软件园"],
                }
            ],
        }, 1.0

    processor = AsrCallPostprocessor(enabled=True, llm_callable=fake_llm)
    event = processor.build_event(
        call_id="call-addr",
        callfrom="8001",
        callto="119",
        turns=[
            TranscriptTurn(
                segment_id="caller-0001",
                speaker="caller",
                direction="inbound",
                text="我在科兴软件园一77栋。",
            )
        ],
    )

    assert event["correctedText"] == "报警人：我在科兴软件园一期七栋。"
    assert event["turns"][0]["originalText"] == "我在科兴软件园一77栋。"
    assert event["turns"][0]["correctedText"] == "我在科兴软件园一期七栋。"


def test_factory_can_select_hotword_postprocessor(monkeypatch):
    monkeypatch.setenv("ASR_AI_CORRECTION_ENABLED", "true")
    monkeypatch.setenv("ASR_CORRECTION_PROVIDER", "hotword")
    monkeypatch.setenv("ASR_HOTWORD_CORRECTION_MODE", "dynamic")

    processor = create_asr_call_postprocessor()

    assert isinstance(processor, AsrAddressHotwordPostprocessor)
    assert processor.enabled is True


def test_factory_can_select_db_align_postprocessor(monkeypatch):
    monkeypatch.setenv("ASR_AI_CORRECTION_ENABLED", "true")
    monkeypatch.setenv("ASR_CORRECTION_PROVIDER", "db_align")

    class FakeCorrector:
        def correct(self, text):
            raise AssertionError("factory should not correct during construction")

    monkeypatch.setattr(
        AsrAddressDbAlignPostprocessor,
        "_create_corrector",
        lambda self: FakeCorrector(),
    )

    processor = create_asr_call_postprocessor()

    assert isinstance(processor, AsrAddressDbAlignPostprocessor)
    assert processor.enabled is True


def test_factory_defaults_to_db_align_postprocessor(monkeypatch):
    monkeypatch.setenv("ASR_AI_CORRECTION_ENABLED", "true")
    monkeypatch.delenv("ASR_CORRECTION_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_AI_CORRECTION_PROVIDER", raising=False)
    monkeypatch.setattr("asr_ai_postprocessor._load_local_env", lambda: None)

    class FakeCorrector:
        def correct(self, text):
            raise AssertionError("factory should not correct during construction")

    monkeypatch.setattr(
        AsrAddressDbAlignPostprocessor,
        "_create_corrector",
        lambda self: FakeCorrector(),
    )

    processor = create_asr_call_postprocessor()

    assert isinstance(processor, AsrAddressDbAlignPostprocessor)
    assert processor.enabled is True


def test_factory_can_select_llm_postprocessor(monkeypatch):
    monkeypatch.setenv("ASR_AI_CORRECTION_ENABLED", "true")
    monkeypatch.setenv("ASR_CORRECTION_PROVIDER", "llm")
    monkeypatch.delenv("ASR_AI_CORRECTION_PROVIDER", raising=False)

    processor = create_asr_call_postprocessor()

    assert isinstance(processor, AsrCallPostprocessor)
    assert processor.enabled is True


def test_hotword_postprocessor_keeps_call_corrected_contract():
    processor = AsrAddressHotwordPostprocessor(enabled=True, mode="dynamic")

    event = processor.build_turn_event(
        call_id="call-hotword",
        callfrom="8001",
        callto="119",
        turn=TranscriptTurn(
            segment_id="caller-0001",
            speaker="caller",
            direction="inbound",
            text="我在月海接到",
            start_time_ms=100,
            end_time_ms=900,
        ),
    )

    assert event["event"] == "call.corrected"
    assert event["callId"] == "call-hotword"
    assert event["correctionScope"] == "turn"
    assert event["segmentId"] == "caller-0001"
    assert event["originalText"] == "报警人：我在月海接到"
    assert event["correctedText"] == "我在粤海街道"
    assert event["turns"] == [
        {
            "segmentId": "caller-0001",
            "speaker": "caller",
            "direction": "inbound",
            "originalText": "我在月海接到",
            "correctedText": "我在粤海街道",
            "keywords": [],
        }
    ]
    assert event["llmElapsedMs"] >= 0
    assert event["correctionProvider"] == "hotword"


def test_db_align_postprocessor_keeps_call_corrected_contract():
    class Result:
        corrected = "我在科兴科学园"
        elapsed_ms = 12.3
        replacements = [{
            "original": "科兴科雪园",
            "corrected": "科兴科学园",
            "method": "pinyin_align",
        }]

    class FakeCorrector:
        def correct(self, text):
            assert text == "我在科兴科雪园"
            return Result()

    processor = AsrAddressDbAlignPostprocessor(enabled=True, corrector=FakeCorrector())

    event = processor.build_turn_event(
        call_id="call-db-align",
        callfrom="8001",
        callto="119",
        turn=TranscriptTurn(
            segment_id="caller-0001",
            speaker="caller",
            direction="inbound",
            text="我在科兴科雪园",
            start_time_ms=100,
            end_time_ms=900,
        ),
    )

    assert event["event"] == "call.corrected"
    assert event["correctionProvider"] == "db_align"
    assert event["correctionMode"] == "align"
    assert event["correctedText"] == "我在科兴科学园"
    assert event["turns"][0]["correctedText"] == "我在科兴科学园"
    assert event["replacements"] == Result.replacements


def test_db_align_llm_highlight_keeps_address_corrected_text_and_adds_keywords():
    class Result:
        corrected = "我在科兴科学园发生火灾"
        elapsed_ms = 12.3
        replacements = [{
            "original": "科信科学园",
            "corrected": "科兴科学园",
            "method": "pinyin_align",
        }]

    class FakeCorrector:
        def correct(self, text):
            assert text == "我在科信科学园发生火灾"
            return Result()

    seen = {}

    def fake_llm(prompt):
        seen["prompt"] = prompt
        return {
            "correctedText": "LLM 不得覆盖地址库纠错文本",
            "keywords": ["科兴科学园", "火灾", "火灾"],
        }, 45.6

    processor = AsrAddressDbAlignLlmHighlightPostprocessor(
        enabled=True,
        corrector=FakeCorrector(),
        llm_callable=fake_llm,
    )

    event = processor.build_turn_event(
        call_id="call-db-highlight",
        callfrom="8001",
        callto="119",
        turn=TranscriptTurn(
            segment_id="caller-0001",
            speaker="caller",
            direction="inbound",
            text="我在科信科学园发生火灾",
            start_time_ms=100,
            end_time_ms=900,
        ),
    )

    assert "只提取高亮关键词" in seen["prompt"]
    assert "科兴科学园发生火灾" in seen["prompt"]
    assert event["correctedText"] == "我在科兴科学园发生火灾"
    assert event["turns"][0]["correctedText"] == "我在科兴科学园发生火灾"
    assert event["turns"][0]["keywords"] == ["科兴科学园", "火灾"]
    assert event["correctionProvider"] == "db_align+llm_highlight"
    assert event["dbElapsedMs"] == 12.3
    assert event["llmHighlightElapsedMs"] == 45.6
    assert event["llmElapsedMs"] == 45.6


def test_db_align_llm_highlight_failure_keeps_address_correction():
    class Result:
        corrected = "我在科苑北路"
        elapsed_ms = 5.0
        replacements = []

    class FakeCorrector:
        def correct(self, text):
            return Result()

    processor = AsrAddressDbAlignLlmHighlightPostprocessor(
        enabled=True,
        corrector=FakeCorrector(),
        llm_callable=lambda prompt: (_ for _ in ()).throw(RuntimeError("LLM unavailable")),
    )

    event = processor.build_turn_event(
        call_id="call-db-highlight-failure",
        callfrom="8001",
        callto="119",
        turn=TranscriptTurn(segment_id="caller-0001", speaker="caller", direction="inbound", text="我在科园北路"),
    )

    assert event["correctedText"] == "我在科苑北路"
    assert event["turns"][0]["keywords"] == []
    assert event["llmHighlightElapsedMs"] is None
    assert event["llmHighlightFailed"] is True


def test_rule_highlighter_extracts_address_incident_and_structural_keywords():
    highlighter = AsrRuleKeywordHighlighter(address_terms=["科兴科学园", "科苑北路"])

    keywords, elapsed_ms, failed = highlighter.extract_keywords(
        original_text="我在科苑北路15号科兴科学园A3栋冒烟有人被困",
        corrected_text="我在科苑北路15号科兴科学园A3栋冒烟有人被困",
        speaker="caller",
        direction="inbound",
    )

    assert keywords == ["科苑北路15号", "科兴科学园A3栋", "冒烟", "有人被困"]
    assert elapsed_ms >= 0
    assert failed is False


def test_rule_highlighter_extracts_context_address_missing_from_database():
    highlighter = AsrRuleKeywordHighlighter(address_terms=[])

    keywords, elapsed_ms, failed = highlighter.extract_keywords(
        original_text="我在和小区有人被困",
        corrected_text="我在和小区有人被困",
        speaker="caller",
        direction="inbound",
    )

    assert keywords == ["和小区", "有人被困"]
    assert elapsed_ms >= 0
    assert failed is False


def test_rule_highlighter_extends_context_address_with_building_and_keeps_text():
    highlighter = AsrRuleKeywordHighlighter(address_terms=[])
    corrected_text = "地址在幸福花园A3栋发生火灾"

    keywords, _, _ = highlighter.extract_keywords(
        original_text=corrected_text,
        corrected_text=corrected_text,
        speaker="caller",
        direction="inbound",
    )

    assert corrected_text == "地址在幸福花园A3栋发生火灾"
    assert keywords == ["幸福花园A3栋", "火灾"]


def test_rule_highlighter_ignores_generic_unnamed_context_location():
    highlighter = AsrRuleKeywordHighlighter(address_terms=[])

    keywords, _, _ = highlighter.extract_keywords(
        original_text="我在这个小区",
        corrected_text="我在这个小区",
        speaker="caller",
        direction="inbound",
    )

    assert keywords == []


def test_db_align_rule_highlight_keeps_address_corrected_text_and_never_calls_llm():
    class Result:
        corrected = "我在科兴科学园发生火灾"
        elapsed_ms = 12.3
        replacements = []

    class FakeCorrector:
        candidates = [type("Candidate", (), {"term": "科兴科学园"})()]

        def correct(self, text):
            return Result()

    processor = AsrAddressDbAlignRuleHighlightPostprocessor(
        enabled=True,
        corrector=FakeCorrector(),
    )

    event = processor.build_turn_event(
        call_id="call-db-rule-highlight",
        callfrom="8001",
        callto="119",
        turn=TranscriptTurn(segment_id="caller-0001", speaker="caller", direction="inbound", text="我在科信科学园发生火灾"),
    )

    assert event["correctedText"] == "我在科兴科学园发生火灾"
    assert event["turns"][0]["correctedText"] == "我在科兴科学园发生火灾"
    assert event["turns"][0]["keywords"] == ["科兴科学园", "火灾"]
    assert event["correctionProvider"] == "db_align+rule_highlight"
    assert event["highlightProvider"] == "rule"
    assert event["llmElapsedMs"] is None
    assert event["llmHighlightElapsedMs"] is None
    assert event["ruleHighlightElapsedMs"] >= 0


def test_factory_can_select_rule_highlight_postprocessor(monkeypatch):
    monkeypatch.setenv("ASR_AI_CORRECTION_ENABLED", "true")
    monkeypatch.setenv("ASR_CORRECTION_PROVIDER", "db_align")
    monkeypatch.setenv("ASR_HIGHLIGHT_PROVIDER", "rule")
    monkeypatch.setattr(
        AsrAddressDbAlignPostprocessor,
        "_create_corrector",
        lambda self: type("Corrector", (), {"candidates": []})(),
    )

    processor = create_asr_call_postprocessor()

    assert isinstance(processor, AsrAddressDbAlignRuleHighlightPostprocessor)
