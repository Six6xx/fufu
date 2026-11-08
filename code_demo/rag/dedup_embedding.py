"""Embedding 去重 + MinHash/LSH 文本去重。

移植自 llm_wiki/src/lib/dedup_embedding.ts + graphify/_minhash.py + graphify/dedup.py，
适配 code_demo 的 RAG 子系统。

功能：
  1. 余弦相似度计算
  2. MinHash 签名生成（Mersenne-prime 置换哈希族，兼容 datasketch）
  3. Band-LSH 候选对生成（最优 b/r 参数搜索，无需 scipy）
  4. Union-Find 聚类（路径压缩 + 按秩合并）
  5. Jaro-Winkler 验证（移植自 graphify/dedup.py）
  6. 实体去重完整流程（normalize → entropy gate → MinHash/LSH → Jaro-Winkler → merge）
  7. Embedding 去重完整流程（cosine similarity → candidate pairs → union-find clusters）

用法：
    from code_demo.rag.dedup_embedding import (
        cosine_similarity, embed_pages, candidate_pairs, cluster_by_pairs,
        MinHash, MinHashLSH, deduplicate_entities, deduplicate_by_embedding,
    )
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

import numpy as np


# ── Union-Find（移植自 graphify/dedup.py _UF + llm_wiki dedup_embedding.ts clusterByPairs）──

class UnionFind:
    """并查集 — 路径压缩 + 按秩合并。

    移植自 graphify/dedup.py _UF 和 llm_wiki dedup_embedding.ts clusterByPairs()。
    """

    def __init__(self, n: int):
        self.parent = list(range(n))  # 并查集节点的父节点索引
        self.rank = [0] * n  # 并查集树的秩，用于控制树高

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # 路径压缩
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ── MinHash（移植自 graphify/_minhash.py MinHash）────────────────────────────────

_MERSENNE_PRIME = (1 << 61) - 1
_MAX_HASH = (1 << 32) - 1


class MinHash:
    """MinHash 签名生成器 — Mersenne-prime 置换哈希族。

    移植自 graphify/_minhash.py MinHash。
    与 datasketch 兼容（相同的哈希族和参数化方式）。
    """

    def __init__(self, num_perm: int = 128, seed: int = 42):
        self.num_perm = num_perm  # MinHash 签名使用的置换数
        self._rng = np.random.RandomState(seed)  # 生成可复现哈希参数的随机数生成器
        self._a = self._rng.randint(1, _MERSENNE_PRIME, size=num_perm, dtype=np.uint64)  # 线性哈希族的乘数参数
        self._b = self._rng.randint(0, _MERSENNE_PRIME, size=num_perm, dtype=np.uint64)  # 线性哈希族的偏移参数
        self._hash_values = np.full(num_perm, _MAX_HASH, dtype=np.uint32)  # 当前文本的最小哈希签名

    def update(self, shingle: str) -> None:
        """用单个 shingle 更新 MinHash 签名。"""
        h = np.uint64(int(hashlib.sha1(shingle.encode("utf-8")).hexdigest()[:8], 16))
        values = np.bitwise_and(
            np.uint64(_MERSENNE_PRIME),
            (self._a * h + self._b) % np.uint64(_MERSENNE_PRIME),
        ).astype(np.uint32)
        np.minimum(self._hash_values, values, out=self._hash_values)

    def merge(self, other: MinHash) -> MinHash:
        """合并两个 MinHash 签名（取逐元素最小值）。"""
        merged = MinHash.__new__(MinHash)
        merged.num_perm = self.num_perm
        merged._a = self._a
        merged._b = self._b
        merged._hash_values = np.minimum(self._hash_values, other._hash_values)
        return merged

    def jaccard(self, other: MinHash) -> float:
        """估算两个签名的 Jaccard 相似度。"""
        return float(np.mean(self._hash_values == other._hash_values))

    @property
    def hash_values(self) -> np.ndarray:
        return self._hash_values.copy()


# ── Band-LSH（移植自 graphify/_minhash.py MinHashLSH）───────────────────────────

class MinHashLSH:
    """Band-hashing LSH — 候选对生成。

    移植自 graphify/_minhash.py MinHashLSH。
    使用数值积分替代 scipy.integrate.quad 计算最优 b/r 参数。
    """

    def __init__(self, threshold: float = 0.5, num_perm: int = 128):
        self.threshold = threshold  # 判定文本相似的 Jaccard 相似度阈值
        self.num_perm = num_perm  # MinHash 签名的置换数
        self.b, self.r = self._optimal_params(num_perm, threshold)
        self._bands: dict[tuple[int, int], list[str]] = {}  # LSH 分桶到候选文本 ID 的索引
        self._signatures: dict[str, np.ndarray] = {}  # 文本 ID 到 MinHash 签名的映射

    def insert(self, key: str, minhash: MinHash) -> None:
        """将 MinHash 签名插入 LSH 索引。"""
        self._signatures[key] = minhash.hash_values
        hv = minhash.hash_values
        for band_idx in range(self.b):
            start = band_idx * self.r
            end = start + self.r
            if end > len(hv):
                break
            bucket = hash(tuple(hv[start:end].tolist()))
            band_key = (band_idx, bucket)
            if band_key not in self._bands:
                self._bands[band_key] = []
            self._bands[band_key].append(key)

    def query(self, key: str) -> set[str]:
        """查找与指定签名相似的所有候选。"""
        candidates: set[str] = set()
        hv = self._signatures.get(key)
        if hv is None:
            return candidates
        for band_idx in range(self.b):
            start = band_idx * self.r
            end = start + self.r
            if end > len(hv):
                break
            bucket = hash(tuple(hv[start:end].tolist()))
            band_key = (band_idx, bucket)
            for candidate in self._bands.get(band_key, []):
                if candidate != key:
                    candidates.add(candidate)
        return candidates

    @staticmethod
    def _optimal_params(num_perm: int, threshold: float) -> tuple[int, int]:
        """搜索最优 b (bands) 和 r (rows per band) 参数。

        使用数值积分（梯形法则）替代 scipy.integrate.quad。
        目标：最小化 |P(s*) - 0.5|，其中 s* = threshold。
        """
        best_b, best_r = 1, num_perm
        best_err = float("inf")
        for b in range(1, num_perm + 1):
            if num_perm % b != 0:
                continue
            r = num_perm // b
            # P(s) = 1 - (1 - s^r)^b
            # 用梯形法则计算 ∫₀¹ |P(s) - step(s, threshold)| ds
            n_steps = 200
            ds = 1.0 / n_steps
            err = 0.0
            for i in range(n_steps + 1):
                s = i * ds
                p = 1.0 - (1.0 - s ** r) ** b
                target = 1.0 if s >= threshold else 0.0
                err += abs(p - target) * ds
            if err < best_err:
                best_err = err
                best_b, best_r = b, r
        return best_b, best_r


# ── 实体去重工具（移植自 graphify/dedup.py）───────────────────────────────────

@dataclass
class Entity:
    """去重前的一个实体提及。"""
    text: str  # 实体提及的原始文本
    label: str = ""  # 实体类别标签
    source: str = ""  # 实体提及的来源
    metadata: dict = field(default_factory=dict)  # 实体的补充属性


def _norm(text: str) -> str:
    """规范化文本：小写 + NFKC + 去除多余空格。

    移植自 graphify/dedup.py _norm()。
    """
    text = unicodedata.normalize("NFKC", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _entropy(text: str) -> float:
    """计算文本的字符级信息熵。

    移植自 graphify/dedup.py _entropy()。
    """
    if not text:
        return 0.0
    freq: dict[str, int] = {}
    for ch in text:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(text)
    return -sum(
        (c / length) * math.log2(c / length)
        for c in freq.values()
        if c > 0
    )


def _shingles(text: str, k: int = 3) -> list[str]:
    """生成 character k-shingles。

    移植自 graphify/dedup.py _shingles()。
    """
    text = _norm(text)
    if len(text) < k:
        return [text] if text else []
    return [text[i : i + k] for i in range(len(text) - k + 1)]


def _make_minhash(text: str, num_perm: int = 128) -> MinHash:
    """从文本生成 MinHash 签名。

    移植自 graphify/dedup.py _make_minhash()。
    """
    mh = MinHash(num_perm=num_perm)
    for shingle in _shingles(text, k=3):
        mh.update(shingle)
    return mh


def _jaro_similarity(s1: str, s2: str) -> float:
    """Jaro 相似度。"""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    max_dist = max(len(s1), len(s2)) // 2 - 1
    if max_dist < 0:
        max_dist = 0

    s1_matches = [False] * len(s1)
    s2_matches = [False] * len(s2)
    matches = 0
    transpositions = 0

    for i in range(len(s1)):
        start = max(0, i - max_dist)
        end = min(i + max_dist + 1, len(s2))
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len(s1)):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    m = float(matches)
    return (m / len(s1) + m / len(s2) + (m - transpositions / 2) / m) / 3


def _jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Jaro-Winkler 相似度（前缀奖励 ≤ 4 字符）。

    移植自 graphify/dedup.py 使用的 jellyfish.jaro_winkler 行为。
    """
    jaro = _jaro_similarity(s1, s2)
    prefix = 0
    for i in range(min(4, len(s1), len(s2))):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * prefix_weight * (1 - jaro)


# ── 实体去重主流程（移植自 graphify/dedup.py deduplicate_entities()）───────────

def deduplicate_entities(
    entities: list[Entity],
    jw_threshold: float = 0.92,
    minhash_threshold: float = 0.5,
    num_perm: int = 128,
    min_entropy: float = 1.0,
) -> list[dict]:
    """实体去重 — 三趟管线。

    移植自 graphify/dedup.py deduplicate_entities()。

    Pass 1: 精确规范化去重
    Pass 2: MinHash/LSH blocking + Jaro-Winkler 验证
    Pass 3: Union-Find 合并 + 代表选取

    Args:
        entities: 原始实体列表
        jw_threshold: Jaro-Winkler 合并阈值
        minhash_threshold: MinHash LSH 候选阈值
        num_perm: MinHash 排列数
        min_entropy: 信息熵过滤阈值（低熵文本通常是噪音/数字）

    Returns:
        去重后的实体列表，每项包含 canonical、aliases、sources、mentions
    """
    if not entities:
        return []

    # Pass 1: 规范化 + 熵过滤
    norm_map: dict[str, list[Entity]] = {}
    for ent in entities:
        n = _norm(ent.text)
        if not n:
            continue
        if _entropy(n) < min_entropy and len(n) < 4:
            continue
        norm_map.setdefault(n, []).append(ent)

    # Pass 2: MinHash/LSH blocking + Jaro-Winkler 验证
    norms = list(norm_map.keys())
    n = len(norms)

    lsh = MinHashLSH(threshold=minhash_threshold, num_perm=num_perm)
    signatures: dict[str, MinHash] = {}
    for text in norms:
        mh = _make_minhash(text, num_perm)
        signatures[text] = mh
        lsh.insert(text, mh)

    uf = UnionFind(n)
    id_to_idx = {text: i for i, text in enumerate(norms)}

    for i, text in enumerate(norms):
        candidates = lsh.query(text)
        for cand in candidates:
            j = id_to_idx.get(cand)
            if j is None or j <= i:
                continue
            if uf.find(i) == uf.find(j):
                continue
            # 精确验证
            sim = _jaro_winkler(text, cand)
            if sim >= jw_threshold:
                uf.union(i, j)

    # Pass 3: 分组 + 代表选取
    groups: dict[int, list[str]] = {}
    for i, text in enumerate(norms):
        root = uf.find(i)
        groups.setdefault(root, []).append(text)

    result: list[dict] = []
    for members in groups.values():
        winner = _pick_winner(members)
        all_sources: set[str] = set()
        all_mentions = 0
        for member in members:
            for ent in norm_map[member]:
                if ent.source:
                    all_sources.add(ent.source)
                all_mentions += 1

        result.append({
            "canonical": winner,
            "aliases": [m for m in members if m != winner],
            "sources": sorted(all_sources),
            "mentions": all_mentions,
        })

    return result


def _pick_winner(variants: list[str]) -> str:
    """从变体中选取最具代表性的文本。

    移植自 graphify/dedup.py _pick_winner()。
    优先级：长度适中 > 包含大写 > 字典序
    """
    if len(variants) == 1:
        return variants[0]

    def score(v: str) -> tuple:
        has_upper = any(c.isupper() for c in v)
        return (len(v), has_upper, v)

    return max(variants, key=score)


# ── Embedding 去重（移植自 llm_wiki/src/lib/dedup_embedding.ts）────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。

    移植自 dedup_embedding.ts cosineSimilarity()。
    """
    va, vb = np.array(a, dtype=np.float64), np.array(b, dtype=np.float64)
    norm_a, norm_b = np.linalg.norm(va), np.linalg.norm(vb)
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def page_to_embedding_text(page: dict) -> str:
    """将页面数据转换为适合 embedding 的文本（标题 + 内容摘要）。

    移植自 dedup_embedding.ts pageToEmbeddingText()。
    """
    title = page.get("title", "")
    content = page.get("content", "")[:2000]
    return f"{title}\n{content}".strip()


def embed_pages(
    pages: list[dict],
    embed_fn: Callable[[str], list[float]] | None = None,
) -> list[dict]:
    """为页面列表生成 embedding。

    移植自 dedup_embedding.ts embedPages()。

    Args:
        pages: 包含 title/content 字段的页面列表
        embed_fn: embedding 函数，接受文本返回向量。
                  如为 None，则页面需自带 embedding 字段

    Returns:
        带 embedding 字段的页面列表
    """
    result: list[dict] = []
    for page in pages:
        if embed_fn is not None:
            text = page_to_embedding_text(page)
            embedding = embed_fn(text)
        else:
            embedding = page.get("embedding", [])
        result.append({**page, "embedding": embedding})
    return result


def candidate_pairs(
    embeddings: list[list[float]],
    threshold: float = 0.95,
    top_k: int = 5,
) -> list[tuple[int, int, float]]:
    """生成余弦相似度超过阈值的候选对（Top-K 最近邻）。

    移植自 dedup_embedding.ts candidatePairs()。

    Args:
        embeddings: embedding 向量列表
        threshold: 余弦相似度阈值
        top_k: 每个向量的 Top-K 最近邻

    Returns:
        [(i, j, similarity), ...] 按相似度降序
    """
    n = len(embeddings)
    if n < 2:
        return []

    matrix = np.array(embeddings, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-10)
    normalized = matrix / norms

    # 余弦相似度矩阵
    sim_matrix = normalized @ normalized.T
    np.fill_diagonal(sim_matrix, 0.0)

    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        indices = np.argsort(-sim_matrix[i])[:top_k]
        for j in indices:
            if j <= i:
                continue
            sim = float(sim_matrix[i, j])
            if sim >= threshold:
                pairs.append((i, int(j), sim))

    pairs.sort(key=lambda x: -x[2])
    return pairs


def cluster_by_pairs(
    pairs: list[tuple[int, int, float]],
    n_items: int,
    min_cluster_size: int = 2,
) -> list[list[int]]:
    """将候选对通过 Union-Find 聚类。

    移植自 dedup_embedding.ts clusterByPairs()。

    Args:
        pairs: candidate_pairs() 返回的候选对
        n_items: 总条目数
        min_cluster_size: 最小聚类大小

    Returns:
        聚类索引列表
    """
    uf = UnionFind(n_items)
    for i, j, _ in pairs:
        uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for idx in range(n_items):
        root = uf.find(idx)
        groups.setdefault(root, []).append(idx)

    return [
        members
        for members in groups.values()
        if len(members) >= min_cluster_size
    ]


def deduplicate_by_embedding(
    items: list[dict],
    embedding_key: str = "embedding",
    threshold: float = 0.95,
    top_k: int = 5,
) -> list[dict]:
    """Embedding 去重完整流程。

    移植自 dedup_embedding.ts 的完整管线。

    Args:
        items: 包含 embedding 字段的条目列表
        embedding_key: embedding 字段名
        threshold: 余弦相似度阈值
        top_k: 每个向量的 Top-K 最近邻

    Returns:
        去重后的条目列表（每组保留第一个），附带 _dedup_cluster 和 _dedup_size 元数据
    """
    embeddings = [item.get(embedding_key, []) for item in items]
    if not embeddings or not any(embeddings):
        return items

    pairs = candidate_pairs(embeddings, threshold=threshold, top_k=top_k)
    clusters = cluster_by_pairs(pairs, len(items))

    keep_indices: set[int] = set(range(len(items)))
    cluster_map: dict[int, int] = {}

    for cluster in clusters:
        representative = min(cluster)
        cluster_map[representative] = len(cluster)
        for idx in cluster:
            if idx != representative:
                keep_indices.discard(idx)

    result: list[dict] = []
    for i, item in enumerate(items):
        if i in keep_indices:
            result.append({
                **item,
                "_dedup_cluster": i,
                "_dedup_size": cluster_map.get(i, 1),
            })

    return result
