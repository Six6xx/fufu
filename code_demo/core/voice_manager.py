"""
core/voice_manager.py — 语音 IO 管理器（ASR + TTS）
"""
import os
import asyncio
import tempfile
import subprocess
import platform
from pathlib import Path


# ── ASR / TTS 引擎（延迟导入）──
_ASREngine = None
_TTSEngine = None


def _get_asr_engine():
    global _ASREngine
    if _ASREngine is None:
        from code_demo.asr_tts.asr import ASREngine
        _ASREngine = ASREngine
    return _ASREngine


def _get_tts_engine():
    global _TTSEngine
    if _TTSEngine is None:
        from code_demo.asr_tts.tts import TTSEngine
        _TTSEngine = TTSEngine
    return _TTSEngine


class VoiceIOManager:
    """语音输入输出管理器，封装 ASR 和 TTS 功能。"""

    def __init__(self, voice_provider_manager=None):
        self._voice_mode: bool = False  # CLI 是否处于语音输入模式
        self._asr_engine = None  # 当前 ASR 识别引擎
        self._tts_mode: bool = False  # 是否启用文本转语音输出
        self._tts_router = None  # 负责选择本地或远程 TTS 的路由器
        self._voice_provider_manager = voice_provider_manager  # 管理远程 ASR/TTS 供应商的组件

    # ── ASR 相关 ──

    def init_asr_engine(self) -> bool:
        """延迟初始化 ASR 引擎。返回是否成功。"""
        if self._asr_engine is not None:
            return True
        try:
            engine_cls = _get_asr_engine()
            self._asr_engine = engine_cls()
            if not self._asr_engine.start():
                print("[VoiceIO] ASR 引擎启动失败")
                self._asr_engine = None
                return False
            print("[VoiceIO] ASR 引擎就绪")
            return True
        except ImportError as e:
            print(f"[VoiceIO] ASR 依赖未安装: {e}")
            print("[VoiceIO] 请安装: pip install pyaudio dashscope")
            return False
        except Exception as e:
            print(f"[VoiceIO] ASR 初始化失败: {e}")
            return False

    def toggle_voice_mode(self):
        """切换语音/文本模式。"""
        self._voice_mode = not self._voice_mode
        if self._voice_mode:
            if self.init_asr_engine():
                print("[VoiceIO] 已切换到语音模式")
            else:
                self._voice_mode = False
                print("[VoiceIO] 语音模式启动失败，保持文本模式")
        else:
            if self._asr_engine:
                self._asr_engine.stop()
                self._asr_engine = None
            print("[VoiceIO] 已切换到文本模式")

    def get_voice_input(self) -> str:
        """通过 ASR 获取语音输入。返回识别文本，失败返回空字符串。"""
        if self._asr_engine is None:
            if not self.init_asr_engine():
                return ""
        try:
            return self._asr_engine.listen()
        except Exception as e:
            print(f"[VoiceIO] 语音识别出错: {e}")
            return ""

    # ── TTS 相关 ──

    def init_tts_router(self) -> bool:
        """延迟初始化 TTS 路由器（管理远程 + 本地引擎）。返回是否成功。"""
        if self._tts_router is not None:
            return True
        try:
            from code_demo.asr_tts.tts_router import TTSEngineRouter
            from code_demo.core.config import TTS_MODEL_PATH, TTS_PROMPT_WAV, TTS_PROMPT_TEXT

            self._tts_router = TTSEngineRouter(self._voice_provider_manager)

            # 初始化本地引擎作为回退（如果模型路径存在）
            if TTS_MODEL_PATH and Path(TTS_MODEL_PATH).is_dir():
                self._tts_router.init_local_engine(
                    TTS_MODEL_PATH,
                    TTS_PROMPT_TEXT if TTS_PROMPT_TEXT else None,
                    TTS_PROMPT_WAV if TTS_PROMPT_WAV else None,
                )

            # 至少需要一个引擎可用（远程或本地）
            if self._tts_router.has_remote_provider or self._tts_router.has_local_engine:
                print("[VoiceIO] TTS 路由器就绪"
                      f"（远程: {'有' if self._tts_router.has_remote_provider else '无'}, "
                      f"本地: {'有' if self._tts_router.has_local_engine else '无'}）")
                return True

            print("[VoiceIO] TTS 无可用引擎")
            self._tts_router = None
            return False
        except ImportError as e:
            print(f"[VoiceIO] TTS 依赖未安装: {e}")
            return False
        except Exception as e:
            print(f"[VoiceIO] TTS 路由器初始化失败: {e}")
            return False

    def toggle_tts_mode(self):
        """切换 TTS 开/关。"""
        self._tts_mode = not self._tts_mode
        if self._tts_mode:
            if self.init_tts_router():
                print("[VoiceIO] TTS 已开启（Agent 回复将自动合成语音）")
            else:
                self._tts_mode = False
                print("[VoiceIO] TTS 启动失败，保持关闭")
        else:
            if self._tts_router:
                self._tts_router.stop()
                self._tts_router = None
            print("[VoiceIO] TTS 已关闭")

    def synthesize_speech(self, text: str, speed: float = 1.0) -> tuple:
        """使用 TTS 路由器合成语音。

        Args:
            text: 要合成的文本
            speed: 语速倍率，1.0 为正常，>1 加速，<1 减速

        Returns:
            (wav_bytes, source) — wav_bytes 为 bytes 或 None，
            source 为 "remote"/"local"/"fallback"/"none"
        """
        if self._tts_router is None:
            if not self.init_tts_router():
                return None, "none"
        return self._tts_router.synthesize(text, speed=speed)

    @property
    def tts_mode(self) -> bool:
        return self._tts_mode

    @tts_mode.setter
    def tts_mode(self, value: bool):
        """允许外部设置 TTS 模式。"""
        if value and not self._tts_mode:
            if self._tts_router is None:
                self.init_tts_router()
            self._tts_mode = self._tts_router is not None
        elif not value and self._tts_mode:
            self._tts_mode = False

    @property
    def voice_mode(self) -> bool:
        return self._voice_mode

    @voice_mode.setter
    def voice_mode(self, value: bool):
        self._voice_mode = value

    def play_tts_response(self, text: str):
        """合成语音并通过系统播放器播放（仅 CLI 模式）。"""
        if self._tts_router is None:
            return
        wav_bytes, source = self._tts_router.synthesize(text)
        if wav_bytes is None:
            return
        # 保存到临时文件并播放
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                tmp_path = f.name
            # 尝试用系统播放器播放
            system = platform.system()
            if system == "Darwin":
                subprocess.run(["afplay", tmp_path], check=False)
            elif system == "Linux":
                subprocess.run(["aplay", tmp_path], check=False, capture_output=True)
            elif system == "Windows":
                import winsound
                winsound.PlaySound(tmp_path, winsound.SND_FILENAME)
            else:
                print(f"[TTS] 音频已保存: {tmp_path}")
                return
            # 播放后清理
            os.unlink(tmp_path)
        except Exception as e:
            print(f"[TTS] 播放失败: {e}")

    # ── ASR 异步转写 ──

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000,
                         format: str = "pcm_s16le") -> str | None:
        """异步语音转文本（包装同步 ASR 引擎）。"""
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, self._sync_transcribe, audio_bytes, sample_rate, format
        )
        return text if text and text.strip() else None

    def _sync_transcribe(self, audio_bytes: bytes, sample_rate: int, format: str) -> str:
        """同步 ASR 转写（供 run_in_executor 调用）。"""
        if self._asr_engine is None:
            if not self.init_asr_engine():
                return ""
        engine = self._asr_engine
        if not engine:
            return ""
        # 尝试使用 recognize_bytes 接口，不存在则降级
        if hasattr(engine, 'recognize_bytes'):
            return engine.recognize_bytes(audio_bytes, sample_rate=sample_rate) or ""
        # 降级：如果 ASR 引擎不支持 bytes 输入，返回空
        print("[VoiceIO] ASR 引擎不支持 recognize_bytes 接口")
        return ""

    # ── 生命周期 ──

    def stop(self):
        """释放所有语音资源。"""
        if self._asr_engine:
            self._asr_engine.stop()
            self._asr_engine = None
        if self._tts_router:
            self._tts_router.stop()
            self._tts_router = None
