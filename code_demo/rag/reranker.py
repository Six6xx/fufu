"""RAG 重排序器 — RRF 融合 + 多路结果合并。

基于 Reciprocal Rank Fusion（RRF）算法，将多路检索结果（向量检索、BM25、FTS5 等）
融合为统一的排序列表。

RRF 公式：score(d) = Σ  weight_i / (k + rank_i(d))
  其中 k=60（标准参数），rank_i(d) 为文档 d 在第 i 路的排名（从 1 开始）。

用法：
    from code_demo.rag.reranker import Reranker, RankedResult

    reranker = Reranker()
    results = reranker.rerank(
        {"vector": vector_results, "bm25": bm25_results},
        top_k=5,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


# ── 结果协议（duck-typing，兼容 SearchResult 和任意对象）─────────────────────

@runtime_checkable
class _ResultLike(Protocol):
    """检索结果的最小接口。"""
    id: str  # 检索结果唯一标识
    content: str  # 检索结果正文
    score: float  # 检索结果原始得分
    metadata: dict  # 检索结果附加元数据


# ── 重排序结果 ────────────────────────────────────────────────────────────────

@dataclass
class RankedResult:
    """重排序后的结果。"""
    id: str  # 重排结果对应的文档 ID
    content: str  # 重排结果正文
    score: float          # RRF 融合分数
    sources: list[str]    # 来源列表，如 ["vector", "bm25"]
    metadata: dict = field(default_factory=dict)  # 重排结果附加元数据


# ── 辅助：从任意对象提取字段 ──────────────────────────────────────────────────

def _get_id(obj: Any) -> str:
    """兼容 id / doc_id 两种字段名。"""
    if hasattr(obj, "id"):
        return str(obj.id)
    if hasattr(obj, "doc_id"):
        return str(obj.doc_id)
    raise AttributeError(f"结果对象缺少 id/doc_id 字段：{type(obj)}")


def _get_content(obj: Any) -> str:
    """兼容 content / snippet / title 多种字段名。"""
    for attr in ("content", "snippet", "title"):
        val = getattr(obj, attr, None)
        if val:
            return str(val)
    return ""


def _get_metadata(obj: Any) -> dict:
    return getattr(obj, "metadata", {}) or {}


# ── n-gram Jaccard 相似度 ─────────────────────────────────────────────────────

def _ngram_set(text: str, n: int = 3) -> set[str]:
    """将文本拆成 n-gram 集合（字符级）。"""
    text = text.strip()
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(a: str, b: str, n: int = 3) -> float:
    """计算两段文本的 n-gram Jaccard 相似度。"""
    set_a = _ngram_set(a, n)
    set_b = _ngram_set(b, n)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# ── RRF 重排序器 ──────────────────────────────────────────────────────────────

class Reranker:
    """RRF 重排序器。

    将多路检索结果（向量 / BM25 / FTS5 等）通过 Reciprocal Rank Fusion 融合排序。

    RRF 公式：score(d) = Σ  weight_i / (k + rank_i(d))
    其中 k=60（标准参数），rank_i(d) 为文档 d 在第 i 路的排名（从 1 开始）。

    示例::

        reranker = Reranker(k=60)
        ranked = reranker.rerank(
            {"vector": vector_hits, "bm25": bm25_hits},
            top_k=5,
            source_weights={"vector": 1.2, "bm25": 1.0},
        )
        deduped = reranker.deduplicate(ranked)
    """

    DEFAULT_K = 60  # RRF 常数

    def __init__(self, k: int = DEFAULT_K):
        """
        Args:
            k: RRF 常数（越大越平滑不同来源的权重差异，推荐 60）。
        """
        if k <= 0:
            raise ValueError(f"k 必须为正整数，当前值：{k}")
        self.k = k  # 倒数排名融合的常数参数

    # ── 公开方法 ──────────────────────────────────────────────────────────────

    def rerank(
        self,
        candidates: dict[str, list],
        top_k: int = 5,
        source_weights: Optional[dict[str, float]] = None,
    ) -> list[RankedResult]:
        """多路融合重排序。

        Args:
            candidates: 按来源分组的候选结果字典。
                key: 来源名（如 "vector"、"bm25"、"fts5"）。
                value: 结果列表，每个元素需有 id/doc_id、content/snippet/title、
                       score、metadata 属性（与 SearchResult 兼容）。
            top_k: 最终返回数量。
            source_weights: 来源权重（默认等权 1.0），
                如 {"vector": 1.2, "bm25": 1.0}。

        Returns:
            按 RRF 分数降序排列的 RankedResult 列表（长度 ≤ top_k）。
        """
        if not candidates:
            return []

        weights = source_weights or {}

        # rrf_scores[doc_id] → 累计 RRF 分
        rrf_scores: dict[str, float] = {}
        # doc_sources[doc_id] → 出现的来源集合
        doc_sources: dict[str, set[str]] = {}
        # doc_info[doc_id] → (content, metadata) 首次遇到时缓存
        doc_info: dict[str, tuple[str, dict]] = {}

        for source_name, result_list in candidates.items():
            if not result_list:
                continue

            weight = weights.get(source_name, 1.0)

            for rank, result in enumerate(result_list, start=1):
                doc_id = _get_id(result)
                contribution = self._rrf_score(rank, weight)

                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + contribution

                if doc_id not in doc_sources:
                    doc_sources[doc_id] = set()
                doc_sources[doc_id].add(source_name)

                if doc_id not in doc_info:
                    doc_info[doc_id] = (_get_content(result), _get_metadata(result))

        # 按 RRF 分降序，取 top_k
        sorted_ids = sorted(rrf_scores, key=lambda d: -rrf_scores[d])[:top_k]

        return [
            RankedResult(
                id=doc_id,
                content=doc_info[doc_id][0],
                score=rrf_scores[doc_id],
                sources=sorted(doc_sources[doc_id]),
                metadata=doc_info[doc_id][1],
            )
            for doc_id in sorted_ids
        ]

    def _rrf_score(self, rank: int, weight: float = 1.0) -> float:
        """计算单个排名的 RRF 贡献分。

        Args:
            rank: 文档在该路结果中的排名（从 1 开始）。
            weight: 来源权重（默认 1.0）。

        Returns:
            RRF 贡献值 = weight / (k + rank)
        """
        return weight / (self.k + rank)

    def deduplicate(
        self,
        results: list[RankedResult],
        similarity_threshold: float = 0.9,
        ngram_size: int = 3,
    ) -> list[RankedResult]:
        """基于内容相似度去重。

        遍历已排序结果，若某条结果与前面任意已保留结果的 n-gram Jaccard
        相似度超过阈值，则丢弃该结果（保留分数更高的那条）。

        Args:
            results: 已按 RRF 分降序排列的结果列表。
            similarity_threshold: Jaccard 相似度阈值（0~1），超过则视为重复。
            ngram_size: n-gram 大小（字符级），默认 3。

        Returns:
            去重后的结果列表（保持原有顺序）。
        """
        if not results:
            return []

        kept: list[RankedResult] = []

        for candidate in results:
            is_duplicate = False
            for existing in kept:
                sim = _jaccard(candidate.content, existing.content, n=ngram_size)
                if sim >= similarity_threshold:
                    is_duplicate = True
                    break
            if not is_duplicate:
                kept.append(candidate)

        return kept
