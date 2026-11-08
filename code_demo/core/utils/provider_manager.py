"""
code_demo/core/utils/provider_manager.py — 模型供应商管理器

管理模型供应商的 CRUD 操作和持久化。
每个供应商包含：名称、API Base URL、API Key、可用模型列表。
支持从供应商自动发现可用模型（通过 OpenAI /models 端点）。

存储结构:
  ai_configuration/model_providers.json
    {"providers": [...], "active_provider_id": "..."}
"""

import json
import os
import re
import uuid
from pathlib import Path

from ..config import MODEL_PROVIDERS_FILE


# 模型类型推断关键词
_VLM_KEYWORDS = re.compile(
    r"(vl|vision|image|video|multimodal|img|pic|photo|picture)",
    re.IGNORECASE,
)


def _infer_model_type(model_name: str) -> str:
    """从模型名称推断类型：vlm 或 llm。"""
    return "vlm" if _VLM_KEYWORDS.search(model_name) else "llm"


def _mask_api_key(key: str) -> str:
    """脱敏 API Key：保留前 3 位和后 4 位，中间用 **** 替代。"""
    if not key or len(key) < 10:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


class ProviderManager:
    """
    模型供应商管理器。

    提供供应商的创建、读取、更新、删除操作。
    支持从供应商 API 自动发现可用模型。
    所有写入操作使用原子模式（先写 .tmp 再 rename），防止中断导致损坏。
    """

    def __init__(self):
        self._file: Path = MODEL_PROVIDERS_FILE  # 模型供应商配置文件路径
        self._data: dict = self._load()  # 已加载的模型供应商配置

    # ── 持久化 ──

    def _load(self) -> dict:
        """加载供应商配置文件。"""
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return {"providers": [], "active_provider_id": None}

    def _save(self):
        """原子写入供应商配置文件。"""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._file.with_suffix(self._file.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(self._file))
        except Exception as e:
            print(f"[ProviderManager] 写入失败: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _find(self, provider_id: str) -> dict | None:
        """在列表中查找供应商。"""
        for p in self._data.get("providers", []):
            if p.get("id") == provider_id:
                return p
        return None

    # ── CRUD ──

    def list_providers(self, mask_keys: bool = True) -> list[dict]:
        """返回所有供应商列表。mask_keys=True 时对 api_key 脱敏。"""
        providers = self._data.get("providers", [])
        if not mask_keys:
            return providers
        result = []
        for p in providers:
            item = dict(p)
            item["api_key"] = _mask_api_key(item.get("api_key", ""))
            result.append(item)
        return result

    def get_provider(self, provider_id: str) -> dict | None:
        """获取单个供应商（不脱敏，内部使用）。"""
        return self._find(provider_id)

    def add_provider(self, name: str, url: str, api_key: str,
                     models: list[dict] | None = None) -> str:
        """添加供应商，返回新供应商 ID。"""
        provider_id = str(uuid.uuid4())[:8]
        provider = {
            "id": provider_id,
            "name": name,
            "url": url.rstrip("/"),
            "api_key": api_key,
            "models": models or [],
        }
        self._data.setdefault("providers", []).append(provider)
        # 如果是第一个供应商，自动设为活跃
        if len(self._data["providers"]) == 1:
            self._data["active_provider_id"] = provider_id
        self._save()
        print(f"[ProviderManager] 添加供应商: {name} ({provider_id})")
        return provider_id

    def update_provider(self, provider_id: str, updates: dict) -> bool:
        """更新供应商字段。api_key 为空字符串时保留原值。"""
        provider = self._find(provider_id)
        if not provider:
            return False
        allowed = {"name", "url", "api_key", "models"}
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "api_key" and value == "":
                continue  # 空密钥 = 保留原值
            if key == "url":
                value = value.rstrip("/")
            provider[key] = value
        self._save()
        print(f"[ProviderManager] 更新供应商: {provider.get('name')} ({provider_id})")
        return True

    def delete_provider(self, provider_id: str) -> bool:
        """删除供应商。如果删除的是活跃供应商，active 重置。"""
        providers = self._data.get("providers", [])
        for i, p in enumerate(providers):
            if p.get("id") == provider_id:
                removed = providers.pop(i)
                if self._data.get("active_provider_id") == provider_id:
                    self._data["active_provider_id"] = (
                        providers[0]["id"] if providers else None
                    )
                self._save()
                print(f"[ProviderManager] 删除供应商: {removed.get('name')} ({provider_id})")
                return True
        return False

    # ── 活跃供应商 ──

    def set_active(self, provider_id: str) -> bool:
        """设置活跃供应商。"""
        if not self._find(provider_id):
            return False
        self._data["active_provider_id"] = provider_id
        self._save()
        return True

    def get_active(self) -> dict | None:
        """获取活跃供应商（不脱敏）。"""
        active_id = self._data.get("active_provider_id")
        if not active_id:
            return None
        return self._find(active_id)

    def get_active_id(self) -> str | None:
        """获取活跃供应商 ID。"""
        return self._data.get("active_provider_id")

    # ── 模型发现 ──

    def discover_models(self, provider_id: str) -> list[dict]:
        """
        通过 OpenAI /models 端点发现供应商可用模型。

        返回: [{"name": "...", "type": "llm"|"vlm"}, ...]
        失败时抛出异常。
        """
        provider = self._find(provider_id)
        if not provider:
            raise ValueError(f"供应商 {provider_id} 不存在")

        from openai import OpenAI

        client = OpenAI(
            api_key=provider["api_key"],
            base_url=provider["url"],
        )

        models = []
        seen = set()
        try:
            response = client.models.list()
            for m in response.data:
                name = m.id
                if name in seen:
                    continue
                seen.add(name)
                models.append({
                    "name": name,
                    "type": _infer_model_type(name),
                })
        except Exception as e:
            raise RuntimeError(f"模型发现失败: {e}") from e

        # 按名称排序
        models.sort(key=lambda x: x["name"].lower())
        print(f"[ProviderManager] 发现 {len(models)} 个模型: {provider.get('name')}")
        return models
