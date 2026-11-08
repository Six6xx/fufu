"""
core/protocol/input_event.py — 多模态输入事件抽象层

为 neuro-sama 方向的多模态并发输入提供统一接口。
所有输入源（文本、音频、视频帧、情绪信号、中断信号）
均统一为 InputEvent，由 AsyncInferenceEngine 消费。
"""
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Modality(Enum):
    """输入模态类型"""
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO_FRAME = "video_frame"
    EMOTION_SIGNAL = "emotion"
    INTERRUPT = "interrupt"


@dataclass
class InputEvent:
    """输入事件基类。所有输入源统一为此接口。"""
    modality: Modality  # 输入内容的模态类型
    timestamp: float = field(default_factory=time.time)  # 输入事件发生时间
    source: str = "user"  # "user", "system", "sensor", "timer"
    metadata: dict = field(default_factory=dict)  # 输入事件附加元数据


@dataclass
class TextInput(InputEvent):
    """文本输入事件。"""
    modality: Modality = field(default=Modality.TEXT, init=False)  # 固定标记为文本输入
    content: str = ""  # 用户输入的文本
    attachments: list[dict] | None = None  # 与文本关联的附件
    original_content: str | None = None  # 未注入文件内容的原始文本


@dataclass
class AudioInput(InputEvent):
    """音频输入事件（原始音频流）。"""
    modality: Modality = field(default=Modality.AUDIO, init=False)  # 固定标记为音频输入
    audio_bytes: bytes = b""  # 输入音频的二进制数据
    sample_rate: int = 16000  # 输入音频采样率
    channels: int = 1  # 输入音频声道数
    format: str = "pcm_s16le"  # 音频编码格式


@dataclass
class ImageInput(InputEvent):
    """图像输入事件（单帧）。"""
    modality: Modality = field(default=Modality.IMAGE, init=False)  # 固定标记为图像输入
    image_bytes: bytes = b""  # 输入图像的二进制数据
    width: int = 0  # 图像宽度
    height: int = 0  # 图像高度
    format: str = "jpeg"  # jpeg, png, webp


@dataclass
class VideoFrameInput(InputEvent):
    """视频帧输入事件。"""
    modality: Modality = field(default=Modality.VIDEO_FRAME, init=False)  # 固定标记为视频帧输入
    frame_bytes: bytes = b""  # 视频帧图像数据
    width: int = 0  # 视频帧宽度
    height: int = 0  # 视频帧高度
    frame_index: int = 0  # 视频帧序号
    fps: float = 30.0  # 视频帧率


@dataclass
class EmotionSignal(InputEvent):
    """情绪信号输入事件（来自情感状态机或外部传感器）。"""
    modality: Modality = field(default=Modality.EMOTION_SIGNAL, init=False)  # 固定标记为情绪信号
    emotion: str = ""  # "happy", "sad", "angry", "neutral", etc.
    intensity: float = 0.5  # 0.0 ~ 1.0
    source: str = "fsm"  # "fsm" (情感状态机), "sensor" (外部)


@dataclass
class InterruptSignal(InputEvent):
    """中断信号事件。"""
    modality: Modality = field(default=Modality.INTERRUPT, init=False)  # 固定标记为打断信号
    reason: str = ""  # "user_speaking", "priority_event", "timeout"
    cancel_current: bool = True  # 是否取消当前进行中的推理
