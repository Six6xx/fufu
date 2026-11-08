"""混合检索器 — CJK 双字分词 + BM25 + Embedding 融合。

移植自 llm_wiki/src/lib/search.ts + llm_wiki/src/lib/embedding.ts，
适配 code_demo 的 RAG 子系统。

功能：
  1. CJK 双字分词（中文字符 bigram + 单字索引）
  2. 停用词过滤（CJK + 英文）
  3. 倒排索引 + BM25 评分
  4. Embedding 搜索（chunk 级过采样 → 页级 max-pool + 加权尾部求和）
  5. 混合检索（BM25 + Embedding 融合排序）

用法：
    from code_demo.rag.hybrid_retriever import (
        tokenize_query, HybridRetriever, SearchResult,
    )

    retriever = HybridRetriever()
    retriever.add_documents(docs)
    results = retriever.search("查询文本", top_k=10)
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

import numpy as np


# ── 停用词（移植自 llm_wiki/src/lib/search.ts STOP_WORDS）────────────────────

STOP_WORDS: set[str] = {
    # CJK
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "not", "it", "this", "that", "as", "but",
    "and", "the", "of", "to", "in", "a", "is", "that", "for",
    "it", "on", "with", "as", "was", "this", "but", "from",
}


# ── CJK 分词（移植自 llm_wiki/src/lib/search.ts tokenizeQuery()）─────────────

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def tokenize_query(query: str) -> list[str]:
    """将查询文本分词 — CJK 双字分词 + 英文单词。

    移植自 llm_wiki/src/lib/search.ts tokenizeQuery()。

    中文字符生成 bigram + 单字 token，非中文字符按空格/标点分割。
    结果经过停用词过滤和小写化。

    Args:
        query: 查询文本

    Returns:
        token 列表（已去停用词）
    """
    tokens: list[str] = []
    i = 0
    chars = list(query)

    while i < len(chars):
        ch = chars[i]

        if _CJK_RE.match(ch):
            # 单字 token
            tokens.append(ch)

            # 收集连续 CJK 字符
            cjk_run = [ch]
            j = i + 1
            while j < len(chars) and _CJK_RE.match(chars[j]):
                cjk_run.append(chars[j])
                j += 1

            # 生成 bigram
            for k in range(len(cjk_run) - 1):
                tokens.append(cjk_run[k] + cjk_run[k + 1])

            i = j
        else:
            # 非 CJK：按空格/标点分割
            if ch.strip() and ch.isalnum():
                word = ch.lower()
                j = i + 1
                while j < len(chars) and chars[j].isalnum() and not _CJK_RE.match(chars[j]):
                    word += chars[j].lower()
                    j += 1
                tokens.append(word)
                i = j
            else:
                i += 1

    # 去停用词
    return [t for t in tokens if t not in STOP_WORDS and len(t.strip()) > 0]


# ── 搜索结果 ──────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """检索结果。"""
    doc_id: str  # 命中的文档 ID
    title: str  # 命中文档标题
    score: float              # 融合分数
    bm25_score: float = 0.0   # BM25 分数
    embed_score: float = 0.0  # Embedding 分数
    snippet: str = ""         # 摘要片段
    metadata: dict = field(default_factory=dict)  # 命中文档的附加元数据


# ── 倒排索引 + BM25 ──────────────────────────────────────────────────────────

@dataclass
class _DocEntry:
    doc_id: str  # 文档唯一标识
    title: str  # 文档标题
    tokens: list[str]  # 文档分词结果
    content: str  # 文档正文内容
    metadata: dict  # 文档附加元数据


class BM25Index:
    """BM25 倒排索引。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1  # BM25 词频饱和参数
        self.b = b  # BM25 文档长度归一化参数
        self._docs: list[_DocEntry] = []  # BM25 索引中的文档条目
        self._idf: dict[str, float] = {}  # 各词项的逆文档频率
        self._avg_dl: float = 0.0  # 索引文档的平均长度
        self._inverted: dict[str, list[tuple[int, int]]] = defaultdict(list)  # token → [(doc_idx, tf)]

    def add_document(
        self,
        doc_id: str,
        title: str,
        content: str,
        tokens: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        """添加文档到索引。"""
        if tokens is None:
            tokens = tokenize_query(f"{title} {content}")

        doc_idx = len(self._docs)
        self._docs.append(_DocEntry(
            doc_id=doc_id,
            title=title,
            tokens=tokens,
            content=content,
            metadata=metadata or {},
        ))

        # 词频
        tf_map: dict[str, int] = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1

        for token, tf in tf_map.items():
            self._inverted[token].append((doc_idx, tf))

    def build(self) -> None:
        """构建 IDF 和平均文档长度（在所有文档添加后调用）。"""
        n = len(self._docs)
        if n == 0:
            return

        # 平均文档长度
        total_tokens = sum(len(d.tokens) for d in self._docs)
        self._avg_dl = total_tokens / n

        # IDF
        for token, postings in self._inverted.items():
            df = len(postings)
            self._idf[token] = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query_tokens: list[str], top_k: int = 20) -> list[tuple[str, float]]:
        """BM25 搜索。

        Returns:
            [(doc_id, score), ...] 按分数降序
        """
        if not self._docs or not query_tokens:
            return []

        scores = np.zeros(len(self._docs), dtype=np.float64)

        for token in query_tokens:
            idf = self._idf.get(token, 0.0)
            if idf <= 0:
                continue

            for doc_idx, tf in self._inverted.get(token, []):
                dl = len(self._docs[doc_idx].tokens)
                # BM25 TF-IDF
                tf_norm = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
                )
                scores[doc_idx] += idf * tf_norm

        # Top-K
        top_indices = np.argsort(-scores)[:top_k]
        return [
            (self._docs[idx].doc_id, float(scores[idx]))
            for idx in top_indices
            if scores[idx] > 0
        ]

    def get_document(self, doc_id: str) -> _DocEntry | None:
        """获取文档条目。"""
        for doc in self._docs:
            if doc.doc_id == doc_id:
                return doc
        return None


# ── Embedding 搜索（移植自 llm_wiki/src/lib/embedding.ts）────────────────────

def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """余弦相似度。"""
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


@dataclass
class _ChunkEntry:
    doc_id: str  # 文本块所属文档 ID
    chunk_idx: int  # 文本块在文档中的序号
    text: str  # 文本块正文
    embedding: np.ndarray  # 文本块的 Embedding 向量


class EmbeddingIndex:
    """Embedding 向量索引 — chunk 级过采样 + 页级聚合。

    移植自 llm_wiki/src/lib/embedding.ts searchByEmbedding()。
    """

    CHUNK_OVERFETCH = 3    # chunk 级过采样倍数
    PAGE_WEIGHT = 0.3      # 加权尾部求和的次优权重

    def __init__(self):
        self._chunks: list[_ChunkEntry] = []  # 已建立向量索引的文本块
        self._doc_chunks: dict[str, list[_ChunkEntry]] = defaultdict(list)  # 文档 ID 到文本块的索引

    def add_chunk(self, doc_id: str, chunk_idx: int, text: str, embedding: list[float]) -> None:
        """添加一个 chunk 的 embedding。"""
        entry = _ChunkEntry(
            doc_id=doc_id,
            chunk_idx=chunk_idx,
            text=text,
            embedding=np.array(embedding, dtype=np.float64),
        )
        self._chunks.append(entry)
        self._doc_chunks[doc_id].append(entry)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[str, float, str]]:
        """Embedding 搜索 — chunk 级过采样 → 页级 max-pool + 加权尾部求和。

        移植自 llm_wiki/src/lib/embedding.ts searchByEmbedding()。

        Args:
            query_embedding: 查询向量
            top_k: 返回的最大结果数

        Returns:
            [(doc_id, score, best_chunk_text), ...] 按分数降序
        """
        if not self._chunks:
            return []

        q = np.array(query_embedding, dtype=np.float64)

        # Step 1: chunk 级过采样
        chunk_scores: list[tuple[int, float]] = []
        for i, chunk in enumerate(self._chunks):
            sim = _cosine_sim(q, chunk.embedding)
            if sim > 0:
                chunk_scores.append((i, sim))

        chunk_scores.sort(key=lambda x: -x[1])
        overfetch_k = top_k * self.CHUNK_OVERFETCH
        top_chunks = chunk_scores[:overfetch_k]

        # Step 2: 页级聚合（max-pool + 加权尾部求和）
        page_scores: dict[str, list[float]] = defaultdict(list)
        page_best_chunk: dict[str, str] = {}

        for chunk_idx, score in top_chunks:
            chunk = self._chunks[chunk_idx]
            page_scores[chunk.doc_id].append(score)
            if chunk.doc_id not in page_best_chunk or score > page_scores[chunk.doc_id][0]:
                page_best_chunk[chunk.doc_id] = chunk.text

        # Step 3: 页级分数计算
        results: list[tuple[str, float, str]] = []
        for doc_id, scores in page_scores.items():
            scores_sorted = sorted(scores, reverse=True)
            max_score = scores_sorted[0]

            # 加权尾部求和：主分数 + 0.3 × 次分数之和
            tail_sum = sum(scores_sorted[1:]) * self.PAGE_WEIGHT if len(scores_sorted) > 1 else 0
            final_score = max_score + tail_sum

            results.append((doc_id, final_score, page_best_chunk.get(doc_id, "")))

        results.sort(key=lambda x: -x[1])
        return results[:top_k]


# ── 混合检索器 ────────────────────────────────────────────────────────────────

@dataclass
class Document:
    """待索引的文档。"""
    doc_id: str  # 文档唯一标识
    title: str  # 文档标题
    content: str  # 文档全文内容
    metadata: dict = field(default_factory=dict)  # 文档附加元数据
    embedding: list[float] | None = None  # 文档向量表示
    chunks: list[dict] | None = None  # [{"text": "...", "embedding": [...]}]


class HybridRetriever:
    """混合检索器 — BM25 + Embedding 融合排序。

    移植自 llm_wiki/src/lib/search.ts searchWiki() + llm_wiki/src/lib/embedding.ts。

    融合策略：
    - BM25 分数和 Embedding 分数分别归一化到 [0, 1]
    - 融合分数 = α × bm25_norm + (1 - α) × embed_norm
    - α 默认 0.4（Embedding 稍重）
    """

    def __init__(
        self,
        alpha: float = 0.4,
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        self.alpha = alpha  # 混合检索中向量检索的融合权重
        self.embed_fn = embed_fn  # 将查询文本转换为向量的回调
        self.bm25 = BM25Index()  # BM25 关键词检索索引
        self.embedding = EmbeddingIndex()  # 向量分块检索索引
        self._docs: dict[str, Document] = {}  # 已加入检索器的文档集合
        self._built = False  # 检索索引是否已构建

    def add_document(self, doc: Document) -> None:
        """添加文档到检索器。"""
        self._docs[doc.doc_id] = doc

        # BM25 索引
        tokens = tokenize_query(f"{doc.title} {doc.content}")
        self.bm25.add_document(
            doc_id=doc.doc_id,
            title=doc.title,
            content=doc.content,
            tokens=tokens,
            metadata=doc.metadata,
        )

        # Embedding 索引
        if doc.chunks:
            for i, chunk in enumerate(doc.chunks):
                emb = chunk.get("embedding", [])
                if emb:
                    self.embedding.add_chunk(
                        doc_id=doc.doc_id,
                        chunk_idx=i,
                        text=chunk.get("text", ""),
                        embedding=emb,
                    )
        elif doc.embedding:
            self.embedding.add_chunk(
                doc_id=doc.doc_id,
                chunk_idx=0,
                text=doc.content[:500],
                embedding=doc.embedding,
            )

        self._built = False

    def add_documents(self, docs: list[Document]) -> None:
        """批量添加文档。"""
        for doc in docs:
            self.add_document(doc)

    def build(self) -> None:
        """构建索引（在所有文档添加后调用）。"""
        self.bm25.build()
        self._built = True

    def search(
        self,
        query: str,
        top_k: int = 10,
        query_embedding: list[float] | None = None,
    ) -> list[SearchResult]:
        """混合检索。

        Args:
            query: 查询文本
            top_k: 返回的最大结果数
            query_embedding: 查询向量（如为 None 且有 embed_fn 则自动生成）

        Returns:
            SearchResult 列表，按融合分数降序
        """
        if not self._built:
            self.build()

        query_tokens = tokenize_query(query)

        # BM25 搜索
        bm25_results = self.bm25.search(query_tokens, top_k=top_k * 2)

        # Embedding 搜索
        embed_results: list[tuple[str, float, str]] = []
        if query_embedding is None and self.embed_fn is not None:
            query_embedding = self.embed_fn(query)
        if query_embedding is not None:
            embed_results = self.embedding.search(query_embedding, top_k=top_k * 2)

        # 分数归一化
        bm25_scores = self._normalize_scores(dict(bm25_results))
        embed_scores = self._normalize_scores({r[0]: r[1] for r in embed_results})
        embed_snippets = {r[0]: r[2] for r in embed_results}

        # 融合
        all_doc_ids = set(bm25_scores.keys()) | set(embed_scores.keys())
        fused: list[tuple[str, float, float, float]] = []

        for doc_id in all_doc_ids:
            bm25_s = bm25_scores.get(doc_id, 0.0)
            embed_s = embed_scores.get(doc_id, 0.0)
            fused_score = self.alpha * bm25_s + (1 - self.alpha) * embed_s
            fused.append((doc_id, fused_score, bm25_s, embed_s))

        fused.sort(key=lambda x: -x[1])

        # 构建结果
        results: list[SearchResult] = []
        for doc_id, score, bm25_s, embed_s in fused[:top_k]:
            doc = self._docs.get(doc_id)
            snippet = embed_snippets.get(doc_id, "")
            if not snippet and doc:
                snippet = doc.content[:200]

            results.append(SearchResult(
                doc_id=doc_id,
                title=doc.title if doc else "",
                score=score,
                bm25_score=bm25_s,
                embed_score=embed_s,
                snippet=snippet,
                metadata=doc.metadata if doc else {},
            ))

        return results

    @staticmethod
    def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
        """Min-Max 归一化分数到 [0, 1]。"""
        if not scores:
            return scores
        values = list(scores.values())
        min_v, max_v = min(values), max(values)
        if max_v - min_v < 1e-10:
            return {k: 1.0 for k in scores}
        return {k: (v - min_v) / (max_v - min_v) for k, v in scores.items()}
