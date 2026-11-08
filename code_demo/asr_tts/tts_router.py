"""
code_demo/asr_tts/tts_router.py — TTS 引擎路由器

管理远程和本地 TTS 引擎，根据配置自动选择引擎并在远程失败时回退。

策略：
1. 如果有活跃的远程 TTS 供应商 → 优先使用远程
2. 远程失败 + fallback 开启 → 回退到本地引擎（带警告）
3. 没有远程供应商 → 直接使用本地引擎
"""

from typing import Optional


class TTSEngineRouter:
    """
    TTS 引擎路由器。

    根据 VoiceProviderManager 的配置，选择使用远程或本地 TTS 引擎。
    远程失败时自动回退到本地引擎。

    synthesize() 返回 (wav_bytes, source):
        source: "remote"  — 远程合成成功
                "local"   — 本地引擎合成（无远程配置）
                "fallback" — 远程失败后回退到本地
                "none"    — 合成失败（无可用引擎）
    """

    def __init__(self, voice_provider_manager, local_engine=None):
        """
        Args:
            voice_provider_manager: VoiceProviderManager 实例
            local_engine: 本地 TTSEngine 实例（可选，可延迟初始化）
        """
        self._vpm = voice_provider_manager  # 提供远程 TTS 配置的供应商管理器
        self._local_engine = local_engine  # 可选的本地 TTS 引擎
        self._remote_client = None  # 延迟创建

    def synthesize(
        self,
        text: str,
        prompt_text: Optional[str] = None,
        prompt_wav: Optional[str] = None,
        speed: float = 1.0,
    ) -> tuple:
        """
        合成语音。

        Returns:
            (wav_bytes, source) — wav_bytes 为 bytes 或 None，
            source 为 "remote"/"local"/"fallback"/"none"
        """
        active = self._vpm.get_active_tts()

        # ── 尝试远程合成 ──
        if active and active.get("provider_type") == "remote":
            try:
                wav = self._synthesize_remote(text, prompt_text, prompt_wav, speed, active)
                if wav:
                    return wav, "remote"
            except Exception as e:
                print(f"[TTS Router] 远程合成失败: {e}")

            # 远程失败，检查是否可以回退
            if not self._vpm.get_tts_fallback():
                print("[TTS Router] 回退已禁用，无法使用本地引擎")
                return None, "none"
            print("[TTS Router] 尝试回退到本地引擎...")

        # ── 本地引擎 ──
        if self._local_engine is not None:
            try:
                wav = self._local_engine.synthesize(
                    text, prompt_text=prompt_text, prompt_wav=prompt_wav, speed=speed
                )
                if wav:
                    source = "fallback" if active else "local"
                    return wav, source
            except Exception as e:
                print(f"[TTS Router] 本地合成失败: {e}")

        return None, "none"

    def _synthesize_remote(
        self,
        text: str,
        prompt_text: Optional[str],
        prompt_wav: Optional[str],
        speed: float,
        provider: dict,
    ) -> Optional[bytes]:
        """调用远程 CosyVoice 服务合成语音。"""
        from .remote_tts_client import RemoteTTSEngine

        # 延迟创建远程客户端（或 URL 变更时重建）
        if (self._remote_client is None
                or self._remote_client.base_url != provider["url"].rstrip("/")):
            self._remote_client = RemoteTTSEngine(
                base_url=provider["url"],
                api_key=provider.get("api_key", ""),
            )
            if not self._remote_client.start():
                return None

        # 从 provider config 读取 spk_id
        config = provider.get("config", {})
        spk_id = config.get("spk_id")

        return self._remote_client.synthesize(
            text, prompt_text=prompt_text, prompt_wav=prompt_wav,
            speed=speed, spk_id=spk_id
        )

    def init_local_engine(self, model_dir: str, prompt_text: Optional[str] = None,
                          prompt_wav: Optional[str] = None) -> bool:
        """延迟初始化本地 TTS 引擎。"""
        if self._local_engine is not None:
            return True
        try:
            from .tts import TTSEngine
            engine = TTSEngine(
                model_dir=model_dir,
                prompt_text=prompt_text if prompt_text else None,
                prompt_wav=prompt_wav if prompt_wav else None,
            )
            if not engine.start():
                print("[TTS Router] 本地引擎启动失败")
                return False
            self._local_engine = engine
            print("[TTS Router] 本地引擎就绪")
            return True
        except ImportError as e:
            print(f"[TTS Router] 本地 TTS 依赖未安装: {e}")
            return False
        except Exception as e:
            print(f"[TTS Router] 本地引擎初始化失败: {e}")
            return False

    @property
    def has_local_engine(self) -> bool:
        return self._local_engine is not None

    @property
    def has_remote_provider(self) -> bool:
        return self._vpm.get_active_tts() is not None

    def stop(self):
        """释放所有引擎资源。"""
        if self._local_engine:
            self._local_engine.stop()
            self._local_engine = None
        if self._remote_client:
            self._remote_client.stop()
            self._remote_client = None
