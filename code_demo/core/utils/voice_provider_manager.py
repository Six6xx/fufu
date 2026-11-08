"""
code_demo/core/utils/voice_provider_manager.py — 语音服务供应商管理器 (TTS/ASR)

管理 TTS 和 ASR 远程服务的 CRUD 操作和持久化。
与 ProviderManager（LLM）平行设计，但字段和逻辑独立。

每个 TTS 供应商包含：名称、URL、API Key（可选）、类型、配置（参考文本等）。
每个 ASR 供应商包含：名称、URL、API Key、类型、配置（预留）。

存储结构:
  ai_configuration/voice_providers.json
    {
      "tts_providers": [...],
      "asr_providers": [...],
      "active_tts_provider_id": "...",
      "active_asr_provider_id": "...",
      "tts_fallback_to_local": true
    }
"""

import json
import os
import uuid
from pathlib import Path

from ..config import VOICE_PROVIDERS_FILE


def _mask_api_key(key: str) -> str:
    """脱敏 API Key：保留前 3 位和后 4 位，中间用 **** 替代。"""
    if not key or len(key) < 10:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def _empty_data() -> dict:
    """返回空的配置文件结构。"""
    return {
        "tts_providers": [],
        "asr_providers": [],
        "active_tts_provider_id": None,
        "active_asr_provider_id": None,
        "tts_fallback_to_local": True,
    }


class VoiceProviderManager:
    """
    语音服务供应商管理器 (TTS/ASR)。

    提供 TTS 和 ASR 供应商的 CRUD 操作。
    所有写入操作使用原子模式（先写 .tmp 再 rename），防止中断导致损坏。
    """

    def __init__(self):
        self._file: Path = VOICE_PROVIDERS_FILE  # 语音供应商配置文件路径
        self._data: dict = self._load()  # 已加载的语音供应商配置

    # ── 持久化 ──

    def _load(self) -> dict:
        """加载配置文件。"""
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            # 确保所有字段存在
            defaults = _empty_data()
            for key, default in defaults.items():
                if key not in data:
                    data[key] = default
            return data
        except (json.JSONDecodeError, FileNotFoundError):
            return _empty_data()

    def _save(self):
        """原子写入配置文件。"""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._file.with_suffix(self._file.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(self._file))
        except Exception as e:
            print(f"[VoiceProviderManager] 写入失败: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _find(self, category: str, provider_id: str) -> dict | None:
        """在指定类别中查找供应商。category: 'tts_providers' | 'asr_providers'"""
        for p in self._data.get(category, []):
            if p.get("id") == provider_id:
                return p
        return None

    def _mask_list(self, providers: list[dict]) -> list[dict]:
        """对供应商列表中的 api_key 脱敏。"""
        result = []
        for p in providers:
            item = dict(p)
            item["api_key"] = _mask_api_key(item.get("api_key", ""))
            result.append(item)
        return result

    # ══════════════════════════════════════════════
    #  TTS 供应商
    # ══════════════════════════════════════════════

    def list_tts_providers(self, mask_keys: bool = True) -> list[dict]:
        """返回所有 TTS 供应商。"""
        providers = self._data.get("tts_providers", [])
        if not mask_keys:
            return providers
        return self._mask_list(providers)

    def get_tts_provider(self, provider_id: str) -> dict | None:
        """获取单个 TTS 供应商（不脱敏）。"""
        return self._find("tts_providers", provider_id)

    def add_tts_provider(self, name: str, url: str, api_key: str = "",
                         config: dict | None = None) -> str:
        """添加 TTS 供应商，返回 ID。"""
        provider_id = "tts_" + str(uuid.uuid4())[:8]
        provider = {
            "id": provider_id,
            "name": name,
            "url": url.rstrip("/"),
            "api_key": api_key,
            "provider_type": "remote",
            "config": config or {},
        }
        self._data.setdefault("tts_providers", []).append(provider)
        # 第一个供应商自动设为活跃
        if len(self._data["tts_providers"]) == 1:
            self._data["active_tts_provider_id"] = provider_id
        self._save()
        print(f"[VoiceProviderManager] 添加 TTS 供应商: {name} ({provider_id})")
        return provider_id

    def update_tts_provider(self, provider_id: str, updates: dict) -> bool:
        """更新 TTS 供应商字段。api_key 为空字符串时保留原值。"""
        provider = self._find("tts_providers", provider_id)
        if not provider:
            return False
        allowed = {"name", "url", "api_key", "config"}
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "api_key" and value == "":
                continue
            if key == "url":
                value = value.rstrip("/")
            provider[key] = value
        self._save()
        print(f"[VoiceProviderManager] 更新 TTS 供应商: {provider.get('name')} ({provider_id})")
        return True

    def delete_tts_provider(self, provider_id: str) -> bool:
        """删除 TTS 供应商。"""
        providers = self._data.get("tts_providers", [])
        for i, p in enumerate(providers):
            if p.get("id") == provider_id:
                removed = providers.pop(i)
                if self._data.get("active_tts_provider_id") == provider_id:
                    self._data["active_tts_provider_id"] = (
                        providers[0]["id"] if providers else None
                    )
                self._save()
                print(f"[VoiceProviderManager] 删除 TTS 供应商: {removed.get('name')} ({provider_id})")
                return True
        return False

    def set_active_tts(self, provider_id: str | None) -> bool:
        """设置活跃 TTS 供应商。provider_id=None 时取消激活（仅使用本地）。"""
        if provider_id is not None and not self._find("tts_providers", provider_id):
            return False
        self._data["active_tts_provider_id"] = provider_id
        self._save()
        return True

    def get_active_tts(self) -> dict | None:
        """获取活跃 TTS 供应商（不脱敏）。"""
        active_id = self._data.get("active_tts_provider_id")
        if not active_id:
            return None
        return self._find("tts_providers", active_id)

    def get_active_tts_id(self) -> str | None:
        """获取活跃 TTS 供应商 ID。"""
        return self._data.get("active_tts_provider_id")

    # ══════════════════════════════════════════════
    #  ASR 供应商（预留）
    # ══════════════════════════════════════════════

    def list_asr_providers(self, mask_keys: bool = True) -> list[dict]:
        """返回所有 ASR 供应商。"""
        providers = self._data.get("asr_providers", [])
        if not mask_keys:
            return providers
        return self._mask_list(providers)

    def get_asr_provider(self, provider_id: str) -> dict | None:
        """获取单个 ASR 供应商（不脱敏）。"""
        return self._find("asr_providers", provider_id)

    def add_asr_provider(self, name: str, url: str, api_key: str = "",
                         config: dict | None = None) -> str:
        """添加 ASR 供应商，返回 ID。"""
        provider_id = "asr_" + str(uuid.uuid4())[:8]
        provider = {
            "id": provider_id,
            "name": name,
            "url": url.rstrip("/"),
            "api_key": api_key,
            "provider_type": "remote",
            "config": config or {},
        }
        self._data.setdefault("asr_providers", []).append(provider)
        if len(self._data["asr_providers"]) == 1:
            self._data["active_asr_provider_id"] = provider_id
        self._save()
        print(f"[VoiceProviderManager] 添加 ASR 供应商: {name} ({provider_id})")
        return provider_id

    def delete_asr_provider(self, provider_id: str) -> bool:
        """删除 ASR 供应商。"""
        providers = self._data.get("asr_providers", [])
        for i, p in enumerate(providers):
            if p.get("id") == provider_id:
                removed = providers.pop(i)
                if self._data.get("active_asr_provider_id") == provider_id:
                    self._data["active_asr_provider_id"] = (
                        providers[0]["id"] if providers else None
                    )
                self._save()
                print(f"[VoiceProviderManager] 删除 ASR 供应商: {removed.get('name')} ({provider_id})")
                return True
        return False

    def set_active_asr(self, provider_id: str | None) -> bool:
        """设置活跃 ASR 供应商。"""
        if provider_id is not None and not self._find("asr_providers", provider_id):
            return False
        self._data["active_asr_provider_id"] = provider_id
        self._save()
        return True

    def get_active_asr_id(self) -> str | None:
        """获取活跃 ASR 供应商 ID。"""
        return self._data.get("active_asr_provider_id")

    # ══════════════════════════════════════════════
    #  回退设置
    # ══════════════════════════════════════════════

    def get_tts_fallback(self) -> bool:
        """获取 TTS 回退到本地的开关状态。"""
        return self._data.get("tts_fallback_to_local", True)

    def set_tts_fallback(self, enabled: bool):
        """设置 TTS 回退到本地的开关。"""
        self._data["tts_fallback_to_local"] = enabled
        self._save()

    # ══════════════════════════════════════════════
    #  健康检查
    # ══════════════════════════════════════════════

    def check_tts_health(self, provider_id: str) -> dict:
        """检查 TTS 供应商的健康状态。"""
        provider = self._find("tts_providers", provider_id)
        if not provider:
            return {"status": "error", "error": "供应商不存在"}
        try:
            import requests
            url = provider["url"].rstrip("/") + "/api/tts/health"
            headers = {}
            if provider.get("api_key"):
                headers["Authorization"] = f"Bearer {provider['api_key']}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}
