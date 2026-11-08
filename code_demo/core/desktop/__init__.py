"""
core.desktop - 桌面控制与实时画面捕获模块。
"""

from .controller import DesktopController
from .screen_stream import Frame, ScreenStream
from .vlm_config import VLMConfig, VLMConfigLoader
from .vision import VisionInterpreter
from .audio_capture import SystemAudioCapture
from .perception_manager import PerceptionManager

__all__ = [
    "DesktopController",
    "ScreenStream",
    "Frame",
    "VLMConfig",
    "VLMConfigLoader",
    "VisionInterpreter",
    "SystemAudioCapture",
    "PerceptionManager",
]
