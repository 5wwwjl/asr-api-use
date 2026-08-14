"""
VAD 活体检测引擎 — 独立模块，零外部依赖。

吃 PCM 16kHz 16bit 单声道字节流，吐出语音段边界信息。
专为 119 接警场景设计，集成 webrtcvad 做帧级判定 + 滑动窗口追踪最近静音时长。
"""

from collections import deque
from dataclasses import dataclass
import math

import webrtcvad


@dataclass
class VADConfig:
    """VAD 配置，所有参数可调"""

    sample_rate: int = 16000          # 采样率 (webrtcvad 仅支持 8000/16000/32000)
    frame_ms: int = 30                # 每帧毫秒数 (10/20/30)
    vad_aggressiveness: int = 2       # 0=最宽松, 1=中等, 2=最严格, 3=非常严格
    speech_confirm_frames: int = 3    # 连续 N 帧有声才确认"开始说话"
    silence_confirm_ms: int = 1500    # 连续静音多少毫秒算停顿 (条件2)
    min_speech_ms: int = 300          # 最短有效语音，短于此的视为噪音
    pre_speech_ms: int = 300          # 说话开始前缓存的音频毫秒数，避免开头丢字
    energy_silence_db: float = -55.0  # 低于该能量强制视为静音，修正电话噪声误判


class VADEngine:
    """
    VAD 活体检测引擎。

    用法:
        vad = VADEngine(config)
        for pcm_chunk in mic_stream:
            result = vad.feed(pcm_chunk)
            if result.speech_started:
                ...  # 说话开始
            if result.speech_ended:
                ...  # 一句话结束，可以送 ASR
    """

    @dataclass
    class FrameResult:
        """feed() 返回的结果"""
        is_speaking: bool = False           # 当前是否在说话状态
        speech_started: bool = False         # 本帧刚检测到说话开始
        speech_ended: bool = False           # 本帧刚检测到说话结束
        silence_duration_ms: int = 0         # 当前连续静音时长
        audio_segment: bytes | None = None   # speech_ended=True 时返回完整语音段 PCM

    def __init__(self, config: VADConfig | None = None):
        self.config = config or VADConfig()
        self._vad = webrtcvad.Vad(self.config.vad_aggressiveness)

        self._frame_bytes = (self.config.sample_rate * 2 * self.config.frame_ms) // 1000  # 16bit = 2bytes

        # 状态
        self._is_speaking = False
        self._speech_buffer: bytearray = bytearray()  # 当前语音段的 PCM 数据
        self._speech_frames: int = 0                   # 当前段的有声帧计数
        self._silence_frames: int = 0                  # 连续静音帧计数
        self._confirm_frames: int = 0                  # 连续有声帧计数 (确认说话开始)
        self._pending_pcm: bytearray = bytearray()     # 跨 feed 保存不足一帧的 PCM 尾部

        # pre-speech buffer — 缓存说话开始前 300ms，避免 confirm_frames 导致开头丢字
        self._pre_speech_frames = int(self.config.pre_speech_ms / self.config.frame_ms)
        self._pre_speech_buffer: deque[bytes] = deque(maxlen=self._pre_speech_frames)

        # 滑动窗口 — 追踪最近 silence_confirm_ms 内的有声帧
        self._window_frames = int(self.config.silence_confirm_ms / self.config.frame_ms)
        self._recent_speech: deque[bool] = deque(maxlen=self._window_frames)

    # ── 公开 API ────────────────────────────────────────────

    def feed(self, pcm: bytes) -> FrameResult:
        """喂入 PCM 数据块；不足一帧的尾部会保留到下一次 feed。"""
        combined = bytes(self._pending_pcm) + pcm
        self._pending_pcm.clear()
        result = self.FrameResult(is_speaking=self._is_speaking)

        offset = 0
        while offset + self._frame_bytes <= len(combined):
            frame = combined[offset:offset + self._frame_bytes]
            offset += self._frame_bytes
            self._pre_speech_buffer.append(frame)  # 始终维护 300ms 滑动窗口
            is_speech = self._is_speech_frame(frame)
            self._recent_speech.append(is_speech)

            if self._is_speaking:
                frame_result = self._handle_speaking_frame(is_speech, frame)
            else:
                frame_result = self._handle_silent_frame(is_speech, frame)

            result.speech_started = result.speech_started or frame_result.speech_started
            if frame_result.speech_ended:
                result.speech_ended = True
                result.audio_segment = frame_result.audio_segment
                result.silence_duration_ms = frame_result.silence_duration_ms

        if offset < len(combined):
            self._pending_pcm.extend(combined[offset:])
        if not result.speech_ended:
            result.silence_duration_ms = self._silence_frames * self.config.frame_ms
        result.is_speaking = self._is_speaking
        return result

    def flush(self) -> bytes | None:
        """清空语音缓冲区，返回未输出的残留语音(如说话中直接挂断)。"""
        if self._is_speaking and self._pending_pcm:
            self._speech_buffer.extend(self._pending_pcm)
        self._pending_pcm.clear()
        if len(self._speech_buffer) >= self._min_speech_bytes():
            buf = bytes(self._speech_buffer)
            self._speech_buffer.clear()
            self._speech_frames = 0
            self._is_speaking = False
            return buf
        self._speech_buffer.clear()
        self._is_speaking = False
        return None

    def reset(self) -> None:
        """重置所有状态，用于新对话开始"""
        self._is_speaking = False
        self._speech_buffer.clear()
        self._speech_frames = 0
        self._silence_frames = 0
        self._confirm_frames = 0
        self._pending_pcm.clear()
        self._pre_speech_buffer.clear()
        self._recent_speech.clear()

    # ── 说话中帧处理 ────────────────────────────────────────

    def _handle_speaking_frame(self, is_speech: bool, frame: bytes) -> FrameResult:
        result = self.FrameResult(is_speaking=True, speech_started=False, speech_ended=False)

        if is_speech:
            self._silence_frames = 0
            self._speech_frames += 1
            self._confirm_frames = 0
            self._speech_buffer.extend(frame)

            # 更新说话结束检测窗口
            self._recent_speech.append(True)

        else:
            self._silence_frames += 1
            self._speech_buffer.extend(frame)  # 包含短暂停顿，ASR 上下文更完整

            # 检查是否停顿足够久
            if self._silence_frames * self.config.frame_ms >= self.config.silence_confirm_ms:
                ended_silence_ms = self._silence_frames * self.config.frame_ms
                if self._speech_frames * self.config.frame_ms >= self.config.min_speech_ms:
                    result.speech_ended = True
                    result.silence_duration_ms = ended_silence_ms
                    result.audio_segment = bytes(self._speech_buffer)

                self._is_speaking = False
                self._speech_buffer.clear()
                self._speech_frames = 0
                self._silence_frames = 0

        return result

    # ── 静默中帧处理 ────────────────────────────────────────

    def _handle_silent_frame(self, is_speech: bool, frame: bytes) -> FrameResult:
        result = self.FrameResult(is_speaking=False, speech_started=False, speech_ended=False)

        if is_speech:
            self._confirm_frames += 1
            self._silence_frames = 0

            if self._confirm_frames >= self.config.speech_confirm_frames:
                # 确认说话开始
                confirmed_speech_frames = self._confirm_frames
                self._is_speaking = True
                result.speech_started = True
                # 把 pre_speech_buffer 里缓存的前 300ms 补到语音段开头
                for f in self._pre_speech_buffer:
                    self._speech_buffer.extend(f)
                # pre-speech 中大部分是静音，只记录真正通过 VAD 的确认帧。
                self._speech_frames = confirmed_speech_frames
                self._confirm_frames = 0
        else:
            self._confirm_frames = 0
            self._silence_frames += 1

        return result

    # ── 工具方法 ────────────────────────────────────────────

    def _is_speech_frame(self, frame: bytes) -> bool:
        """调用 webrtcvad 判断单帧是否为语音，并用能量门压掉近静音误判。"""
        if self._frame_db(frame) < self.config.energy_silence_db:
            return False
        try:
            return self._vad.is_speech(frame, self.config.sample_rate)
        except Exception:
            return False

    def _frame_db(self, frame: bytes) -> float:
        if len(frame) < 2:
            return -60.0
        sample_count = len(frame) // 2
        total = 0
        for i in range(0, sample_count * 2, 2):
            sample = int.from_bytes(frame[i:i + 2], byteorder="little", signed=True)
            total += sample * sample
        if total <= 0:
            return -60.0
        rms = math.sqrt(total / sample_count)
        return max(-60.0, min(0.0, 20 * math.log10(rms / 32768.0)))

    def _min_speech_bytes(self) -> int:
        return (self.config.sample_rate * 2 * self.config.min_speech_ms) // 1000

    # ── 高层查询 (供 gateway 判断自动提交条件) ────────────────

    def silence_since_ms(self) -> int:
        """返回最近一次人声距现在的静音时长 (毫秒)，用于条件2判断"""
        count = 0
        for had_speech in reversed(self._recent_speech):
            if had_speech:
                break
            count += 1
        return count * self.config.frame_ms

