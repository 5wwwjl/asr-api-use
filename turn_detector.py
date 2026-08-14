"""
TurnDetector — 轮次级活体检测 + 自动提交状态机。

在 VADEngine 之上增加轮次语义：
  IDLE → ARMED → LISTENING → WAITING_END → FINISHED → IDLE

用法:
    td = TurnDetector(vad_config, turn_config)
    td.start_turn()

    for pcm_chunk in mic_stream:
        result = td.feed(pcm_chunk, current_asr_text=latest_text)
        if result.event == TurnEvent.AUTO_SUBMIT:
            submit(result.answer_text, result.audio_segment)
        elif result.event == TurnEvent.NO_SPEECH:
            handle_timeout()
"""

from __future__ import annotations

import enum
import time
from collections import deque
from dataclasses import dataclass, field

from vad_engine import VADEngine, VADConfig
from text_filter import is_noise_text, has_valid_content


# ── 事件类型 ────────────────────────────────────────────────

class TurnEvent(enum.Enum):
    NONE = 0             # 无事件，继续
    LISTENING = 1        # 进入 LISTENING 状态（VAD 检测到人声）
    AUTO_SUBMIT = 2      # 自动提交（ASR 稳定 + 有有效文本）
    NO_SPEECH = 3        # 无人声超时（ARMED 状态超时）
    MAX_DURATION = 4     # 超过最大轮次时长，强制结束
    MANUAL_FINISH = 5    # 手动结束
    TTS_FINISHED = 6     # TTS 播放完毕，进入 ARMED


# ── TurnResult ──────────────────────────────────────────────

@dataclass
class TurnResult:
    """TurnDetector.feed() 返回的结果"""
    event: TurnEvent = TurnEvent.NONE
    state: str = "IDLE"                     # 当前状态名
    answer_text: str = ""                   # auto_submit 时的最终文本
    audio_segment: bytes | None = None      # auto_submit 时的完整 PCM
    silence_ms: int = 0                     # 当前静音时长
    elapsed_ms: int = 0                     # 本轮已用时长
    asr_text_latest: str = ""               # 当前最新 ASR 文本（供 UI 显示）


# ── TurnDetector 配置 ───────────────────────────────────────

@dataclass
class TurnConfig:
    """轮次控制参数"""
    no_speech_timeout_ms: int = 8000     # ARMED 状态超时，无人声则返回 NO_SPEECH
    max_turn_duration_ms: int = 30000    # 单轮最大时长，超时强制结束
    silence_confirm_ms: int = 1500       # VAD 静音多久算停顿（透传给 VADConfig）
    asr_stable_ms: int = 1000            # WAITING_END 中 ASR 文本稳定多久自动提交
    cooldown_ms: int = 800               # FINISHED 后冷却，避免残留文本污染下一轮
    pre_speech_ms: int = 300             # 说话开始前缓存音频毫秒数（透传给 VADConfig）
    speech_confirm_frames: int = 3       # 连续几帧有声确认说话开始
    min_speech_ms: int = 300             # 最短有效语音
    vad_aggressiveness: int = 2          # VAD 灵敏度

    def to_vad_config(self) -> VADConfig:
        return VADConfig(
            silence_confirm_ms=self.silence_confirm_ms,
            pre_speech_ms=self.pre_speech_ms,
            speech_confirm_frames=self.speech_confirm_frames,
            min_speech_ms=self.min_speech_ms,
            vad_aggressiveness=self.vad_aggressiveness,
        )


# ── TurnDetector ────────────────────────────────────────────

class TurnDetector:
    """轮次级 VAD + 自动提交状态机。

    核心设计：
    - VADEngine.feed() 持续调用，维持 pre_speech_buffer
    - TurnDetector 只在 ARMED/LISTENING 状态下响应 VAD 事件
    - IDLE/FINISHED/WAITING_END 下忽略 VAD 的 speech_started
    """

    def __init__(self, turn_config: TurnConfig | None = None):
        self.turn_config = turn_config or TurnConfig()
        self._vad = VADEngine(self.turn_config.to_vad_config())

        # 状态
        self.state = "IDLE"
        self._state_entered_at: float = 0.0       # 进入当前状态的时间戳
        self._turn_started_at: float = 0.0         # 本轮 ARMED 的时间戳
        self._last_text_change_at: float = 0.0     # ASR 文本最后变化时间
        self._last_asr_text: str = ""              # 上一帧的 ASR 文本
        self._answer_buffer: str = ""              # 累积的有效回答文本
        self._audio_segment: bytes | None = None   # VAD 返回的语音段
        self._event_queue: deque[TurnResult] = deque()  # 待消费事件

    # ── 公开 API ────────────────────────────────────────────

    def start_turn(self) -> TurnResult:
        """开始新一轮：IDLE → ARMED。"""
        self._vad.reset()
        self._reset_turn_state()
        self._transition_to("ARMED")
        self._turn_started_at = self._state_entered_at
        return TurnResult(event=TurnEvent.NONE, state=self.state)

    def tts_finished(self) -> TurnResult:
        """TTS 播放完毕通知，效果同 start_turn。"""
        return self.start_turn()

    def feed(self, pcm: bytes, current_asr_text: str = "") -> TurnResult:
        """喂入 PCM + 当前 ASR 文本，返回轮次事件。"""
        # 1. 始终喂 VAD（维持 pre_speech_buffer）
        vad_result = self._vad.feed(pcm)

        # 2. 追踪 ASR 文本变化
        asr_changed = current_asr_text != self._last_asr_text
        if asr_changed:
            self._last_asr_text = current_asr_text
            self._last_text_change_at = time.monotonic()
            # 累积有效内容到 answer_buffer（过滤噪声）
            if has_valid_content(current_asr_text):
                self._answer_buffer = current_asr_text

        # 3. 根据状态处理 VAD 事件
        elapsed = self._elapsed_ms()
        silence_ms = self._vad.silence_since_ms()

        if self.state == "IDLE":
            pass  # 等 start_turn()

        elif self.state == "ARMED":
            result = self._handle_armed(vad_result, elapsed)
            if result: return result

        elif self.state == "LISTENING":
            result = self._handle_listening(vad_result, elapsed)
            if result: return result

        elif self.state == "WAITING_END":
            result = self._handle_waiting_end(current_asr_text, asr_changed, elapsed)
            if result: return result

        elif self.state == "FINISHED":
            if elapsed >= self.turn_config.cooldown_ms:
                self._transition_to("IDLE")

        # 4. 消费事件队列
        if self._event_queue:
            return self._event_queue.popleft()

        return TurnResult(
            event=TurnEvent.NONE,
            state=self.state,
            answer_text=self._answer_buffer,
            silence_ms=silence_ms,
            elapsed_ms=elapsed,
            asr_text_latest=current_asr_text,
        )

    def reset_turn(self) -> TurnResult:
        """强制重置回 IDLE（取消当前轮）。"""
        self._vad.reset()
        self._reset_turn_state()
        self._transition_to("IDLE")
        return TurnResult(event=TurnEvent.NONE, state="IDLE")

    def finish_turn(self, manual: bool = True) -> TurnResult:
        """手动结束当前轮。"""
        event = TurnEvent.MANUAL_FINISH if manual else TurnEvent.AUTO_SUBMIT
        result = TurnResult(
            event=event,
            state="FINISHED",
            answer_text=self._answer_buffer,
            audio_segment=self._audio_segment,
            elapsed_ms=self._elapsed_ms(),
        )
        self._transition_to("FINISHED")
        return result

    # ── 状态处理 ────────────────────────────────────────────

    def _handle_armed(self, vr: VADEngine.FrameResult, elapsed: int) -> TurnResult | None:
        if vr.speech_started:
            self._transition_to("LISTENING")
            # VAD 已处于 speaking 状态，pre_speech_buffer 已补进 audio，不干预
            self._event_queue.append(TurnResult(
                event=TurnEvent.LISTENING, state="LISTENING",
                elapsed_ms=elapsed,
            ))
            return None

        if elapsed >= self.turn_config.no_speech_timeout_ms:
            self._transition_to("FINISHED")
            return TurnResult(
                event=TurnEvent.NO_SPEECH, state="FINISHED",
                elapsed_ms=elapsed,
            )

        return None

    def _handle_listening(self, vr: VADEngine.FrameResult, elapsed: int) -> TurnResult | None:
        if vr.speech_ended:
            self._audio_segment = vr.audio_segment
            self._transition_to("WAITING_END")
            return None  # 进入 WAITING_END，等待 ASR 稳定

        if elapsed >= self.turn_config.max_turn_duration_ms:
            self._transition_to("FINISHED")
            return TurnResult(
                event=TurnEvent.MAX_DURATION, state="FINISHED",
                answer_text=self._answer_buffer,
                audio_segment=self._audio_segment,
                elapsed_ms=elapsed,
            )

        return None

    def _handle_waiting_end(self, current_text: str, asr_changed: bool, elapsed: int) -> TurnResult | None:
        stable_ms = int((time.monotonic() - self._last_text_change_at) * 1000)

        # ASR 文本已稳定 + 有有效内容 → 自动提交
        if (stable_ms >= self.turn_config.asr_stable_ms
                and has_valid_content(self._answer_buffer)
                and not is_noise_text(self._answer_buffer)):
            self._transition_to("FINISHED")
            return TurnResult(
                event=TurnEvent.AUTO_SUBMIT, state="FINISHED",
                answer_text=self._answer_buffer,
                audio_segment=self._audio_segment,
                elapsed_ms=elapsed,
            )

        # 超时强制结束
        if elapsed >= self.turn_config.max_turn_duration_ms:
            self._transition_to("FINISHED")
            return TurnResult(
                event=TurnEvent.MAX_DURATION, state="FINISHED",
                answer_text=self._answer_buffer,
                audio_segment=self._audio_segment,
                elapsed_ms=elapsed,
            )

        return None

    # ── 内部工具 ────────────────────────────────────────────

    def _transition_to(self, new_state: str) -> None:
        self.state = new_state
        self._state_entered_at = time.monotonic()

    def _elapsed_ms(self) -> int:
        if self._turn_started_at == 0.0:
            return 0
        return int((time.monotonic() - self._turn_started_at) * 1000)

    def _reset_turn_state(self) -> None:
        self._answer_buffer = ""
        self._audio_segment = None
        self._last_asr_text = ""
        self._last_text_change_at = 0.0
        self._turn_started_at = 0.0
        self._event_queue.clear()
