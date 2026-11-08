"""
code_demo/asr_tts/remote_tts_client.py — 远程 CosyVoice TTS 客户端

通过 HTTP API 调用远程 CosyVoice 服务合成语音。
接口与 TTSEngine 对齐：start() / stop() / synthesize()。
可在 TTSEngineRouter 中无缝切换。
"""

import base64
import time
from pathlib import Path
from typing import Optional


class RemoteTTSEngine:
    """
    远程 CosyVoice TTS 客户端。

    通过 HTTP API 调用远程部署的 CosyVoice 服务。
    接口与 TTSEngine 对齐，可在 TTSEngineRouter 中无缝切换。
    """

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 60):
        self.base_url = base_url.rstrip("/")  # 远程 TTS 服务地址
        self.api_key = api_key  # 远程 TTS 服务访问密钥
        self.timeout = timeout  # 远程 TTS 请求超时时间
        self._running = False  # 远程 TTS 客户端是否已启动
        self._sample_rate = 22050  # 远程音频输出采样率

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """验证远程服务连通性。"""
        try:
            import requests
            resp = requests.get(
                f"{self.base_url}/api/tts/health",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("sample_rate"):
                self._sample_rate = data["sample_rate"]
            self._running = data.get("model_loaded", True)
            print(f"[RemoteTTS] 连接成功: {self.base_url} "
                  f"(设备: {data.get('device', 'unknown')}, "
                  f"采样率: {self._sample_rate})")
            return True
        except Exception as e:
            print(f"[RemoteTTS] 连接失败: {self.base_url} — {e}")
            return False

    def stop(self):
        """停止远程客户端（仅标记状态）。"""
        self._running = False

    def synthesize(
        self,
        text: str,
        prompt_text: Optional[str] = None,
        prompt_wav: Optional[str] = None,
        speed: float = 1.0,
        spk_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """
        调用远程服务合成语音，返回 WAV 字节。

        Args:
            text: 要合成的文本
            prompt_text: 参考音频对应的文本
            prompt_wav: 参考音频文件路径
            speed: 语速倍率
            spk_id: 预注册说话人 ID（无需传参考音频）
        """
        if not self._running:
            return None

        import requests

        payload = {"text": text, "speed": speed}
        if spk_id:
            payload["spk_id"] = spk_id
        if prompt_text:
            payload["prompt_text"] = prompt_text
        if prompt_wav and Path(prompt_wav).is_file():
            with open(prompt_wav, "rb") as f:
                payload["prompt_audio"] = base64.b64encode(f.read()).decode()

        try:
            t0 = time.time()
            resp = requests.post(
                f"{self.base_url}/api/tts/synthesize",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()

            elapsed = time.time() - t0
            print(f"[RemoteTTS] 合成完成 ({elapsed:.1f}s, "
                  f"{len(resp.content) / 1024:.0f}KB)")
            return resp.content

        except requests.exceptions.Timeout:
            print(f"[RemoteTTS] 请求超时 ({self.timeout}s)")
            return None
        except requests.exceptions.ConnectionError:
            print(f"[RemoteTTS] 连接失败: {self.base_url}")
            return None
        except Exception as e:
            print(f"[RemoteTTS] 合成失败: {e}")
            return None

    def check_health(self) -> dict:
        """检查远程服务健康状态。"""
        try:
            import requests
            resp = requests.get(
                f"{self.base_url}/api/tts/health",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
