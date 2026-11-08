"""
core/desktop/vision.py — VisionInterpreter 多模态视觉理解

支持两种模式：
  A) 桌面操控 — 截图 UI 元素定位 & 操作步骤分析
  B) 视频理解 — 关键帧 + ASR 文本 → 场景理解 & 问答
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

from .vlm_config import VLMConfig, VLMConfigLoader

logger = logging.getLogger(__name__)

# 图片压缩目标分辨率（短边不超过 720）
_TARGET_SHORT_SIDE = 720


def _compress_image(image_bytes: bytes, max_short_side: int = _TARGET_SHORT_SIDE) -> bytes:
    """将图片压缩到目标分辨率，返回 PNG 格式 bytes。"""
    try:
        from PIL import Image
    except ImportError:
        # Pillow 未安装，原样返回
        logger.debug("Pillow 未安装，跳过图片压缩")
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    short_side = min(w, h)

    if short_side > max_short_side:
        scale = max_short_side / short_side
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _encode_base64(image_bytes: bytes) -> str:
    """将 bytes 编码为 base64 字符串。"""
    return base64.b64encode(image_bytes).decode("utf-8")


class VisionInterpreter:
    """多模态视觉理解：支持桌面 UI 定位和视频内容理解"""

    def __init__(self, vlm_config: VLMConfig | None = None):
        if vlm_config is None:
            vlm_config = VLMConfigLoader.load_vlm_config()
        self._config = vlm_config  # 当前视觉模型供应商配置
        self._client: Any = None  # 视觉模型 API 客户端

    @property
    def config(self) -> VLMConfig:
        return self._config

    async def _get_client(self) -> Any:
        """懒加载：根据 provider 创建对应的异步 API 客户端"""
        if self._client is not None:
            return self._client

        provider = self._config.provider.lower()

        if "anthropic" in provider:
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=self._config.api_key)
            except ImportError:
                raise ImportError(
                    "需要安装 anthropic 包: pip install anthropic"
                )
        else:
            # OpenAI / OpenAI 兼容接口（含百炼、本地部署等）
            try:
                from openai import AsyncOpenAI
                import httpx
                kwargs: dict[str, Any] = {"api_key": self._config.api_key}
                if self._config.base_url:
                    kwargs["base_url"] = self._config.base_url
                kwargs["timeout"] = httpx.Timeout(60.0, connect=10.0)
                kwargs["max_retries"] = 2
                self._client = AsyncOpenAI(**kwargs)
                logger.info(
                    "VLM 客户端初始化完成: base_url=%s, model=%s",
                    self._config.base_url, self._config.model,
                )
            except ImportError:
                raise ImportError(
                    "需要安装 openai 包: pip install openai"
                )

        return self._client

    # ──────────────────────────────────────────────
    # 消息构建：根据 provider 差异化构建 image content
    # ──────────────────────────────────────────────

    def _build_image_content_part(self, image_b64: str) -> dict[str, Any]:
        """根据 provider 构建图片 content part"""
        provider = self._config.provider.lower()

        if "anthropic" in provider:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_b64,
                },
            }
        else:
            # OpenAI / 兼容接口格式
            return {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{image_b64}",
                    "detail": "low",
                },
            }

    def _build_text_content_part(self, text: str) -> dict[str, Any]:
        """构建文本 content part"""
        return {"type": "text", "text": text}

    # ──────────────────────────────────────────────
    # API 调用统一入口
    # ──────────────────────────────────────────────

    async def _call_vlm(
        self,
        system_prompt: str,
        user_content: list[dict[str, Any]],
    ) -> str:
        """统一的 VLM 调用入口，返回模型文本响应"""
        client = await self._get_client()
        provider = self._config.provider.lower()

        try:
            if "anthropic" in provider:
                response = await client.messages.create(
                    model=self._config.model,
                    max_tokens=self._config.max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                )
                return response.content[0].text

            else:
                # OpenAI / 兼容接口
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ]
                response = await client.chat.completions.create(
                    model=self._config.model,
                    messages=messages,
                    max_tokens=self._config.max_tokens,
                    temperature=self._config.temperature,
                )
                return response.choices[0].message.content or ""

        except Exception as e:
            logger.error(f"VLM 调用失败: {type(e).__name__}: {e}")
            logger.error(f"  配置: base_url={self._config.base_url}, model={self._config.model}")
            raise

    # ──────────────────────────────────────────────
    # 模式 A: 桌面操控
    # ──────────────────────────────────────────────

    async def locate_element(
        self, screenshot: bytes, description: str
    ) -> tuple[int, int] | None:
        """
        在截图中定位指定 UI 元素，返回中心坐标 (x, y)。

        Args:
            screenshot: 截图的原始 bytes
            description: 待定位元素的自然语言描述

        Returns:
            (x, y) 坐标元组，未找到时返回 None
        """
        compressed = _compress_image(screenshot)
        image_b64 = _encode_base64(compressed)

        system_prompt = (
            "你是一个 UI 元素定位助手。用户会给你一张桌面截图和一个元素描述，"
            "请返回该元素的中心坐标。\n"
            "输出格式：仅输出 JSON，如 {\"x\": 123, \"y\": 456}。\n"
            "如果找不到该元素，输出 null。\n"
            "不要输出任何其他文字。"
        )

        user_content = [
            self._build_image_content_part(image_b64),
            self._build_text_content_part(f"请定位以下元素：{description}"),
        ]

        try:
            raw = await self._call_vlm(system_prompt, user_content)
            raw = raw.strip()

            if raw.lower() == "null" or not raw:
                return None

            # 尝试解析 JSON
            result = json.loads(raw)
            if result is None:
                return None
            return (int(result["x"]), int(result["y"]))

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("定位元素响应解析失败: %s, raw=%s", e, raw[:200])
            return None
        except Exception as e:
            logger.error("VLM 调用失败 (locate_element): %s", e)
            return None

    async def analyze_ui(self, screenshot: bytes, task: str) -> dict[str, Any]:
        """
        分析当前界面，给出完成任务的操作步骤。

        Args:
            screenshot: 截图的原始 bytes
            task: 用户想要完成的任务描述

        Returns:
            {"elements": [...], "steps": ["点击搜索框", "输入XX", ...]}
        """
        compressed = _compress_image(screenshot)
        image_b64 = _encode_base64(compressed)

        system_prompt = (
            "你是一个桌面操作规划助手。用户会给你一张桌面截图和一个任务描述，"
            "请分析界面并给出操作步骤。\n"
            "输出格式：仅输出 JSON，结构如下：\n"
            '{"elements": [{"name": "元素名", "x": 100, "y": 200}], '
            '"steps": ["步骤1", "步骤2"]}\n'
            "不要输出任何其他文字。"
        )

        user_content = [
            self._build_image_content_part(image_b64),
            self._build_text_content_part(f"任务：{task}\n请分析界面并给出操作步骤。"),
        ]

        try:
            raw = await self._call_vlm(system_prompt, user_content)
            raw = raw.strip()
            # 尝试提取 JSON 块
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            logger.error("UI 分析响应解析失败: %s", e)
            return {"elements": [], "steps": [], "error": str(e)}

    # ──────────────────────────────────────────────
    # 模式 B: 视频理解
    # ──────────────────────────────────────────────

    async def understand_scene(
        self,
        frames: list[bytes],
        audio_text: str,
        context: str = "",
    ) -> dict[str, str]:
        """
        理解当前视频场景。

        Args:
            frames: 最近几帧关键帧图片 (bytes list)
            audio_text: ASR 转写文本
            context: 对话上下文

        Returns:
            {"scene": "...", "action": "...", "mood": "...", "comment": "..."}
        """
        system_prompt = (
            "你是一个视频场景理解助手。用户会给你视频的关键帧图片和语音转写文本，"
            "请分析当前场景。\n"
            "输出格式：仅输出 JSON，结构如下：\n"
            '{"scene": "场景描述", "action": "正在进行的动作", '
            '"mood": "情绪氛围", "comment": "你想说的自然语言评论"}\n'
            "不要输出任何其他文字。"
        )

        # 构建多帧 content
        user_content: list[dict[str, Any]] = []

        for i, frame in enumerate(frames[:4]):  # 最多 4 帧
            compressed = _compress_image(frame)
            image_b64 = _encode_base64(compressed)
            user_content.append(self._build_image_content_part(image_b64))

        text_part = f"语音内容：{audio_text}" if audio_text else "（无语音）"
        if context:
            text_part += f"\n对话上下文：{context}"
        text_part += "\n请分析当前视频场景。"
        user_content.append(self._build_text_content_part(text_part))

        try:
            raw = await self._call_vlm(system_prompt, user_content)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            logger.error("场景理解响应解析失败: %s", e)
            return {
                "scene": "",
                "action": "",
                "mood": "",
                "comment": "",
                "error": str(e),
            }

    async def answer_question(
        self,
        screenshot: bytes,
        audio_text: str,
        question: str,
    ) -> str:
        """
        用户问关于当前画面的问题，立即回答。

        Args:
            screenshot: 当前画面截图
            audio_text: ASR 转写的用户语音
            question: 用户的具体问题

        Returns:
            自然语言回答文本
        """
        compressed = _compress_image(screenshot)
        image_b64 = _encode_base64(compressed)

        system_prompt = (
            "你是一个视觉问答助手。用户会给你一张画面截图和一个问题，请直接回答。\n"
            "回答要简洁自然，像朋友聊天一样。"
        )

        text = question
        if audio_text and audio_text != question:
            text = f"用户说：{audio_text}\n问题：{question}"

        user_content = [
            self._build_image_content_part(image_b64),
            self._build_text_content_part(text),
        ]

        try:
            return await self._call_vlm(system_prompt, user_content)
        except Exception as e:
            logger.error("VLM 调用失败 (answer_question): %s", e)
            return f"抱歉，视觉理解出现问题：{e}"
