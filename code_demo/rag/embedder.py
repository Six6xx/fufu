"""Embedding 服务 — dashscope text-embedding-v3 封装。

功能：
  1. 单条文本向量化（embed_text）
  2. 批量文本向量化（embed_batch，按 25 条分批）
  3. API 不可用时 graceful fallback（返回零向量，不阻断主流程）
  4. 环境变量：DASHSCOPE_API_KEY

用法：
    from code_demo.rag.embedder import Embedder, EmbeddingResult

    embedder = Embedder()
    vec = embedder.embed_text("示例文本")
    vecs = embedder.embed_batch(["文本一", "文本二"])
"""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import Optional


@dataclass
class EmbeddingResult:
    """单次 embedding 结果。"""
    text: str  # 生成向量时对应的原始文本
    vector: list[float]  # 文本对应的 Embedding 向量
    token_count: int  # 文本使用的 token 数


class Embedder:
    """文本向量化服务。

    使用 dashscope text-embedding-v3（1024维），支持单条和批量调用。
    API 不可用时返回零向量并打印警告，不阻断主流程。
    """

    DEFAULT_MODEL = "text-embedding-v3"
    DIMENSION = 1024
    MAX_TOKENS = 8192
    _BATCH_LIMIT = 25  # dashscope 单批上限

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        """
        Args:
            api_key: dashscope API key（默认从 DASHSCOPE_API_KEY 环境变量读取）
            model: 模型名称，默认 text-embedding-v3
        """
        self.model = model  # Embedding 服务使用的模型名称
        self._api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")  # 访问 Embedding 服务的 API Key
        self._available: Optional[bool] = None  # 延迟检测

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def embed_text(self, text: str) -> list[float]:
        """单条文本向量化。

        Args:
            text: 输入文本（内部会粗略截断到 MAX_TOKENS 字符）
        Returns:
            1024 维 float 向量；API 不可用时返回零向量
        """
        text = self._truncate(text)
        results = self._call_api([text])
        if results is None:
            return self._zero_vector()
        return results[0]

    def embed_batch(
        self,
        texts: list[str],
        batch_size: int = 25,
    ) -> list[list[float]]:
        """批量文本向量化。

        Args:
            texts: 文本列表
            batch_size: 每批大小（dashscope 限制 25）
        Returns:
            向量列表，与输入等长；API 不可用时对应位置返回零向量
        """
        if not texts:
            return []

        batch_size = min(batch_size, self._BATCH_LIMIT)
        vectors: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = [self._truncate(t) for t in texts[i : i + batch_size]]
            result = self._call_api(batch)
            if result is None:
                vectors.extend(self._zero_vector() for _ in batch)
            else:
                vectors.extend(result)

        return vectors

    def dimension(self) -> int:
        """返回向量维度（1024）。"""
        return self.DIMENSION

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _call_api(self, texts: list[str]) -> Optional[list[list[float]]]:
        """调用 dashscope TextEmbedding API。

        Returns:
            向量列表；失败时返回 None（并打印警告）
        """
        if not self._api_key:
            if self._available is not False:
                warnings.warn(
                    "[Embedder] DASHSCOPE_API_KEY 未设置，将返回零向量。",
                    stacklevel=3,
                )
                self._available = False
            return None

        try:
            import dashscope  # type: ignore

            dashscope.api_key = self._api_key
            resp = dashscope.TextEmbedding.call(
                model=self.model,
                input=texts,
                dimension=self.DIMENSION,
            )

            if resp.status_code != 200:
                warnings.warn(
                    f"[Embedder] API 返回错误 {resp.status_code}: {resp.message}",
                    stacklevel=3,
                )
                return None

            # 按 index 排序，确保顺序与输入一致
            embeddings_raw = sorted(
                resp.output.get("embeddings", []),
                key=lambda x: x.get("text_index", 0),
            )
            vectors = [item["embedding"] for item in embeddings_raw]

            if len(vectors) != len(texts):
                warnings.warn(
                    f"[Embedder] 返回向量数 ({len(vectors)}) 与输入数 ({len(texts)}) 不一致",
                    stacklevel=3,
                )
                # 补齐缺失位置
                while len(vectors) < len(texts):
                    vectors.append(self._zero_vector())

            self._available = True
            return vectors

        except ImportError:
            if self._available is not False:
                warnings.warn(
                    "[Embedder] dashscope 未安装（pip install dashscope），将返回零向量。",
                    stacklevel=3,
                )
                self._available = False
            return None

        except Exception as exc:  # noqa: BLE001
            warnings.warn(
                f"[Embedder] API 调用异常: {exc}",
                stacklevel=3,
            )
            return None

    def _truncate(self, text: str) -> str:
        """粗略截断到 MAX_TOKENS 字符（避免超限）。"""
        if len(text) > self.MAX_TOKENS * 4:
            return text[: self.MAX_TOKENS * 4]
        return text

    def _zero_vector(self) -> list[float]:
        """返回全零向量（fallback 用）。"""
        return [0.0] * self.DIMENSION
