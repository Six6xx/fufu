"""
core/desktop/vlm_config.py — VLM (Vision Language Model) 独立供应商配置

从 model_providers.json 中扫描已配置供应商，筛选支持 vision 的模型，
并维护用户选择的 VLM 配置（持久化至 ai_configuration/vlm_config.json）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 已知支持 vision 能力的模型前缀/名称
_VISION_CAPABLE_PREFIXES: dict[str, list[str]] = {
    "openai": [
        "gpt-4o", "gpt-4-turbo", "gpt-4-vision",
        "gpt-4.1", "gpt-4.5",
    ],
    "anthropic": [
        "claude-sonnet", "claude-opus", "claude-haiku",
        "claude-3", "claude-4",
    ],
    "google": [
        "gemini-pro-vision", "gemini-1.5", "gemini-2",
    ],
    # 通用：带 vision / vl 标记的模型
    "_generic": ["vision", "vl", "4o", "qwen-vl", "glm-4v"],
}

# 默认配置文件路径
_DEFAULT_VLM_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "ai_configuration" / "vlm_config.json"
)
_DEFAULT_PROVIDERS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "ai_configuration" / "model_providers.json"
)


@dataclass
class VLMConfig:
    """VLM 供应商配置数据类"""

    provider: str        # 供应商 id 或名称，如 "openai", "百炼"
    model: str           # 模型名称，如 "gpt-4o", "qwen-vl-max"
    api_key: str         # 从对应 provider 配置中获取
    base_url: str = ""   # 自定义 endpoint（OpenAI 兼容接口）
    fallback_model: str = ""   # 备用模型
    max_tokens: int = 1024  # 视觉模型单次回复的 token 上限
    temperature: float = 0.3  # 视觉模型生成随机度

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "fallback_model": self.fallback_model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


class VLMConfigLoader:
    """从 model_providers.json 中加载 VLM 可用列表并管理用户选择"""

    @staticmethod
    def _is_vision_capable(model_name: str, provider_name: str = "") -> bool:
        """判断模型是否支持 vision 能力"""
        name_lower = model_name.lower()

        # 检查特定供应商的已知前缀
        for vendor, prefixes in _VISION_CAPABLE_PREFIXES.items():
            if vendor == "_generic":
                continue
            if vendor in provider_name.lower():
                for prefix in prefixes:
                    if name_lower.startswith(prefix.lower()):
                        return True

        # 通用判断：模型名包含 vision/vl 等标记
        for keyword in _VISION_CAPABLE_PREFIXES["_generic"]:
            if keyword.lower() in name_lower:
                return True

        return False

    @staticmethod
    def get_available_vlm_models(
        providers_path: str | Path | None = None,
    ) -> list[dict[str, str]]:
        """
        扫描已配置的供应商，返回支持 vision 的模型列表。

        Returns:
            [{"provider_id": "...", "provider_name": "...",
              "model": "...", "api_key": "...", "base_url": "..."}, ...]
        """
        path = Path(providers_path) if providers_path else _DEFAULT_PROVIDERS_PATH
        if not path.exists():
            logger.warning("model_providers.json 不存在: %s", path)
            return []

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("读取 model_providers.json 失败: %s", e)
            return []

        results: list[dict[str, str]] = []
        providers = data.get("providers", [])

        for provider in providers:
            provider_id = provider.get("id", "")
            provider_name = provider.get("name", "")
            api_key = provider.get("api_key", "")
            base_url = provider.get("url", "")
            models = provider.get("models", [])

            for model_entry in models:
                model_name = model_entry.get("name", "")
                model_type = model_entry.get("type", "")

                # 显式标记为 vlm 或判定为 vision capable
                if model_type == "vlm" or VLMConfigLoader._is_vision_capable(
                    model_name, provider_name
                ):
                    results.append({
                        "provider_id": provider_id,
                        "provider_name": provider_name,
                        "model": model_name,
                        "api_key": api_key,
                        "base_url": base_url,
                    })

        return results

    @staticmethod
    def load_vlm_config(config_path: str | Path | None = None) -> VLMConfig:
        """
        加载用户选择的 VLM 配置。

        优先读取 vlm_config.json；若不存在则从可用模型列表中选取第一个。
        """
        path = Path(config_path) if config_path else _DEFAULT_VLM_CONFIG_PATH

        # 尝试从持久化配置加载
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                provider_id = saved.get("vlm_provider", "")
                model = saved.get("vlm_model", "")
                fallback = saved.get("vlm_fallback", "")

                if provider_id and model:
                    # 从 providers 中查找对应的 api_key 和 base_url
                    available = VLMConfigLoader.get_available_vlm_models()
                    api_key = ""
                    base_url = ""

                    # 先在可用列表中精确匹配
                    for entry in available:
                        if (entry["provider_id"] == provider_id or
                                entry["provider_name"] == provider_id) and \
                                entry["model"] == model:
                            api_key = entry["api_key"]
                            base_url = entry["base_url"]
                            break

                    # 若未找到精确匹配，尝试仅匹配 provider
                    if not api_key:
                        providers_data = json.loads(
                            _DEFAULT_PROVIDERS_PATH.read_text(encoding="utf-8")
                        )
                        for p in providers_data.get("providers", []):
                            if p.get("id") == provider_id or p.get("name") == provider_id:
                                api_key = p.get("api_key", "")
                                base_url = p.get("url", "")
                                break

                    # 自动降级：当配置的 provider/model 找不到匹配时
                    if not api_key:
                        print(f"[VLMConfig] 警告: 找不到配置的 VLM (provider='{provider_id}', model='{model}')")
                        available_names = [m["provider_name"] + "/" + m["model"] for m in available]
                        print(f"[VLMConfig] 可用 VLM 模型: {available_names}")
                        # 自动降级到第一个可用模型
                        if available:
                            first = available[0]
                            print(f"[VLMConfig] 自动使用: {first['provider_name']}/{first['model']}")
                            return VLMConfig(
                                provider=first["provider_name"] or first["provider_id"],
                                model=first["model"],
                                api_key=first["api_key"],
                                base_url=first["base_url"],
                                fallback_model=fallback,
                            )
                        else:
                            print("[VLMConfig] 错误: 没有任何可用的 VLM 模型，请在 model_providers.json 中配置 type='vlm' 的模型")
                            logger.error("无可用 VLM 模型，无法降级")
                            return VLMConfig(provider="", model="", api_key="")

                    return VLMConfig(
                        provider=provider_id,
                        model=model,
                        api_key=api_key,
                        base_url=base_url,
                        fallback_model=fallback,
                    )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取 vlm_config.json 失败，将使用默认: %s", e)

        # 未保存配置 → 从可用列表取第一个
        available = VLMConfigLoader.get_available_vlm_models()
        if available:
            first = available[0]
            return VLMConfig(
                provider=first["provider_name"] or first["provider_id"],
                model=first["model"],
                api_key=first["api_key"],
                base_url=first["base_url"],
            )

        # 无可用模型 → 返回空配置
        logger.warning("无可用 VLM 模型，返回空配置")
        return VLMConfig(provider="", model="", api_key="")

    @staticmethod
    def save_vlm_choice(
        provider: str,
        model: str,
        fallback: str = "",
        config_path: str | Path | None = None,
    ) -> None:
        """保存用户的 VLM 选择到 vlm_config.json"""
        path = Path(config_path) if config_path else _DEFAULT_VLM_CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "vlm_provider": provider,
            "vlm_model": model,
            "vlm_fallback": fallback,
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("VLM 配置已保存: provider=%s, model=%s", provider, model)
