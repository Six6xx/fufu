"""
code_demo.core.protocol — 协议工作流存储
来源：19_mcp-plugin.py L558–L610
"""

from code_demo.core.protocol.store import RequestStore, REQUEST_STORE
from code_demo.core.protocol.input_event import (
    Modality, InputEvent, TextInput, AudioInput,
    ImageInput, VideoFrameInput, EmotionSignal, InterruptSignal,
)

__all__ = [
    "RequestStore",
    "REQUEST_STORE",
    "Modality",
    "InputEvent",
    "TextInput",
    "AudioInput",
    "ImageInput",
    "VideoFrameInput",
    "EmotionSignal",
    "InterruptSignal",
]