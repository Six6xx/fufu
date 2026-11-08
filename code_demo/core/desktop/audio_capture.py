"""
跨平台系统音频捕获模块

使用 soundcard 库统一捕获系统正在播放的音频（loopback 录制），
分段切割后送入 ASR 转写，支持 macOS / Windows / Linux。

平台方案：
  - macOS: soundcard loopback（需 BlackHole 等虚拟声卡配合）
  - Windows: soundcard WASAPI loopback（原生支持）
  - Linux: soundcard PulseAudio monitor
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

# ---------- 尝试导入 soundcard ----------
try:
    import soundcard as sc

    _SOUNDCARD_AVAILABLE = True
except ImportError:
    _SOUNDCARD_AVAILABLE = False
    logger.warning(
        "[AudioCapture] soundcard 库未安装，系统音频捕获不可用。"
        " 请执行 `pip install soundcard` 安装。"
    )

# ---------- 尝试导入 numpy ----------
try:
    import numpy as np

    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    logger.warning(
        "[AudioCapture] numpy 未安装，音频处理功能受限。"
        " 请执行 `pip install numpy` 安装。"
    )


# ============================================================
# 数据结构
# ============================================================


@dataclass
class AudioSegment:
    """一段录制的音频及其转写结果"""

    audio_data: bytes  # PCM/WAV 音频数据
    start_time: float  # 起始时间戳 (time.time())
    end_time: float  # 结束时间戳
    transcript: str = ""  # ASR 转写结果（异步填充）
    sample_rate: int = 16000  # 音频采样率


# ASR 回调类型: async def callback(audio_bytes, sample_rate) -> str
ASRCallback = Callable[[bytes, int], Awaitable[str]]

# 静音检测阈值（16-bit PCM 振幅归一化后的 RMS）
_SILENCE_RMS_THRESHOLD = 0.005
# 连续静音段数阈值 —— 超过后降低录制频率
_SILENCE_STREAK_LIMIT = 3
# 静音时的录制间隔倍率
_SILENCE_SLOWDOWN_FACTOR = 3.0


# ============================================================
# 工具函数
# ============================================================


def _pcm_to_wav(pcm_data: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """将原始 PCM 16-bit 数据打包为 WAV 格式 bytes"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _compute_rms(pcm_data: bytes) -> float:
    """计算 PCM 16-bit mono 音频的 RMS 振幅（归一化到 0~1）"""
    if _NUMPY_AVAILABLE:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        if len(samples) == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(samples**2)) / 32768.0)
        return rms
    else:
        # 纯 Python fallback（较慢但可用）
        n_samples = len(pcm_data) // 2
        if n_samples == 0:
            return 0.0
        total = 0.0
        for i in range(0, len(pcm_data), 2):
            sample = struct.unpack_from("<h", pcm_data, i)[0]
            total += sample * sample
        rms = (total / n_samples) ** 0.5 / 32768.0
        return rms


# ============================================================
# 主类
# ============================================================


class SystemAudioCapture:
    """跨平台系统音频捕获，分段录制并支持 ASR 转写"""

    def __init__(
        self,
        segment_duration: float = 5.0,
        sample_rate: int = 16000,
        max_segments: int = 12,
    ):
        """
        Args:
            segment_duration: 每段录制时长（秒）
            sample_rate: 目标采样率（Hz），默认 16kHz 适配 ASR
            max_segments: 滚动缓存最大段数（默认 12 段 = 60 秒 @ 5s/段）
        """
        self._segment_duration = segment_duration  # 每段系统音频的采集时长
        self._sample_rate = sample_rate  # 系统音频采样率
        self._running = False  # 系统音频采集是否运行
        self._segments: deque[AudioSegment] = deque(maxlen=max_segments)  # 近期采集到的音频片段缓冲区
        self._asr_callback: Optional[ASRCallback] = None  # 将音频片段送去转写的回调
        self._capture_task: Optional[asyncio.Task] = None  # 当前音频采集异步任务
        self._silence_streak = 0

    # ------ 公开接口 ------

    async def start(self) -> None:
        """启动音频捕获循环"""
        if self._running:
            logger.warning("[AudioCapture] 已在运行中，忽略重复启动")
            return

        if not _SOUNDCARD_AVAILABLE:
            logger.error(
                "[AudioCapture] soundcard 库不可用，无法启动系统音频捕获"
            )
            return

        self._running = True
        self._silence_streak = 0
        self._capture_task = asyncio.create_task(self._capture_loop())
        logger.info("[AudioCapture] 系统音频捕获已启动")

    async def stop(self) -> None:
        """停止捕获"""
        if not self._running:
            return

        self._running = False
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        self._capture_task = None
        logger.info("[AudioCapture] 系统音频捕获已停止")

    def set_asr_callback(self, callback: ASRCallback) -> None:
        """设置 ASR 转写回调: async def callback(audio_bytes, sample_rate) -> str"""
        self._asr_callback = callback

    def get_recent_transcripts(self, seconds: int = 30) -> str:
        """获取最近 N 秒的转写文本拼接"""
        now = time.time()
        cutoff = now - seconds
        parts: list[str] = []
        for seg in self._segments:
            if seg.end_time >= cutoff and seg.transcript:
                parts.append(seg.transcript)
        return " ".join(parts)

    def get_recent_segments(self, n: int = 6) -> list[AudioSegment]:
        """获取最近 N 个音频段"""
        segments = list(self._segments)
        return segments[-n:] if len(segments) > n else segments

    @property
    def is_running(self) -> bool:
        return self._running

    # ------ 内部实现 ------

    def _get_loopback_device(self):
        """
        获取系统音频 loopback 录制设备（Microphone 对象）。

        使用 soundcard.all_microphones(include_loopback=True) 获取包含
        loopback 虚拟麦克风在内的所有麦克风设备，优先选择 isloopback=True 的设备。

        macOS 原生不支持 loopback，需安装 BlackHole 等虚拟声卡。
        """
        if not _SOUNDCARD_AVAILABLE:
            return None

        try:
            all_mics = sc.all_microphones(include_loopback=True)
            if not all_mics:
                logger.error("[AudioCapture] 未找到任何麦克风设备")
                return None

            # 优先选择 isloopback == True 的设备
            loopback_mics = [m for m in all_mics if getattr(m, 'isloopback', False)]

            if loopback_mics:
                mic = loopback_mics[0]
                logger.info(
                    f"[AudioCapture] 使用 loopback 设备: {mic.name}"
                )
                return mic

            # 没有找到 loopback 设备
            import sys
            if sys.platform == "darwin":
                logger.warning(
                    "[AudioCapture] macOS 未检测到 loopback 设备。"
                    " macOS 原生不支持系统音频回录，需要安装虚拟声卡"
                    "（如 BlackHole: https://github.com/ExistentialAudio/BlackHole）"
                    " 并在「音频 MIDI 设置」中配置多输出设备。"
                )
            else:
                logger.warning(
                    "[AudioCapture] 未检测到 loopback 设备，系统音频捕获不可用。"
                )
            return None

        except Exception as e:
            logger.error(f"[AudioCapture] 获取 loopback 设备失败: {e}")
            return None

    def _record_segment_sync(self, mic, num_frames: int) -> Optional[bytes]:
        """
        同步录制一段系统音频（阻塞调用，需在线程中执行）

        Args:
            mic: soundcard Microphone 对象（loopback 虚拟麦克风）
            num_frames: 需要录制的帧数

        Returns:
            PCM 16-bit mono bytes，或 None（失败时）
        """
        try:
            # soundcard Microphone loopback 录制
            # record() 返回 numpy array, shape=(num_frames, channels)
            data = mic.record(
                numframes=num_frames,
                samplerate=self._sample_rate,
                channels=1,  # mono
            )

            if _NUMPY_AVAILABLE:
                # data 是 float32 [-1, 1] 的 numpy array
                # 转为 16-bit PCM
                pcm_int16 = (data * 32767).astype(np.int16)
                return pcm_int16.tobytes()
            else:
                # fallback: 手动转换（假设 data 是 list of list）
                pcm_bytes = bytearray()
                for frame in data:
                    sample = frame[0] if hasattr(frame, "__getitem__") else frame
                    clamped = max(-1.0, min(1.0, float(sample)))
                    int_val = int(clamped * 32767)
                    pcm_bytes.extend(struct.pack("<h", int_val))
                return bytes(pcm_bytes)

        except Exception as e:
            logger.error(f"[AudioCapture] 录制失败: {e}")
            return None

    async def _capture_loop(self) -> None:
        """后台捕获协程主循环"""
        mic = self._get_loopback_device()
        if mic is None:
            logger.error(
                "[AudioCapture] 无法获取 loopback 设备，捕获终止。"
                " 请确认已安装可用的 loopback 音频设备。"
            )
            self._running = False
            return

        num_frames = int(self._sample_rate * self._segment_duration)
        loop = asyncio.get_running_loop()

        logger.info(
            f"[AudioCapture] 开始捕获循环 "
            f"(segment={self._segment_duration}s, rate={self._sample_rate}Hz)"
        )

        while self._running:
            start_time = time.time()

            try:
                # 使用 run_in_executor 包装同步阻塞的录制操作
                pcm_data = await loop.run_in_executor(
                    None, self._record_segment_sync, mic, num_frames
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AudioCapture] 捕获异常: {e}")
                await asyncio.sleep(1.0)
                continue

            end_time = time.time()

            if pcm_data is None:
                await asyncio.sleep(0.5)
                continue

            # 静音检测
            rms = _compute_rms(pcm_data)
            is_silent = rms < _SILENCE_RMS_THRESHOLD

            if is_silent:
                self._silence_streak += 1
            else:
                self._silence_streak = 0

            # 创建 AudioSegment（WAV 格式便于 ASR 处理）
            wav_data = _pcm_to_wav(pcm_data, self._sample_rate, channels=1)
            segment = AudioSegment(
                audio_data=wav_data,
                start_time=start_time,
                end_time=end_time,
                sample_rate=self._sample_rate,
            )
            self._segments.append(segment)

            # 如果有 ASR 回调且非静音，异步执行转写
            if self._asr_callback and not is_silent:
                asyncio.create_task(self._do_asr(segment))

            # 静音降频：连续多段静音时，增加等待时间
            if self._silence_streak >= _SILENCE_STREAK_LIMIT:
                sleep_time = self._segment_duration * (_SILENCE_SLOWDOWN_FACTOR - 1)
                logger.debug(
                    f"[AudioCapture] 连续 {self._silence_streak} 段静音，"
                    f"降频等待 {sleep_time:.1f}s"
                )
                try:
                    await asyncio.sleep(sleep_time)
                except asyncio.CancelledError:
                    break

        logger.info("[AudioCapture] 捕获循环已退出")

    async def _do_asr(self, segment: AudioSegment) -> None:
        """对单个音频段执行 ASR 转写"""
        if not self._asr_callback:
            return
        try:
            transcript = await self._asr_callback(
                segment.audio_data, segment.sample_rate
            )
            segment.transcript = transcript
            if transcript:
                logger.debug(
                    f"[AudioCapture] ASR 转写: {transcript[:80]}..."
                    if len(transcript) > 80
                    else f"[AudioCapture] ASR 转写: {transcript}"
                )
        except Exception as e:
            logger.error(f"[AudioCapture] ASR 转写失败: {e}")
