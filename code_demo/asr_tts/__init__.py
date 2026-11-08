"""
code_demo/asr_tts/__init__.py — 语音识别与合成模块

模块边界：
  asr — 实时语音识别引擎（基于 dashscope Recognition）
  tts — 文本转语音引擎（基于本地 CosyVoice3）
"""

from code_demo.asr_tts.asr import ASREngine, ASRCallback
from code_demo.asr_tts.tts import TTSEngine

__all__ = ["ASREngine", "ASRCallback", "TTSEngine"]
