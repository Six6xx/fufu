"""RAG 子系统 — 检索增强生成模块。

移植自 llm_harness 项目（llm_wiki + graphify），适配 code_demo。

模块：
  - text_chunker: Markdown 感知的文本分块器
  - dedup_embedding: Embedding 去重 + MinHash/LSH 文本去重
  - graph_cluster: Leiden 社区检测 + 超大社区拆分
  - hybrid_retriever: CJK 双字分词 + BM25 + Embedding 混合检索
  - reranker: RRF 融合重排序 + 多路结果合并 + n-gram 去重
  - embedder: dashscope text-embedding-v3 封装（单条 + 批量向量化）
  - vector_store: SQLite + numpy 轻量级向量存储与检索
  - unified_graph: SQLite 统一知识图谱存储（记忆/技能/代码结构）
"""
from code_demo.rag.text_chunker import (
    Chunk,
    ChunkOptions,
    chunk_markdown,
    strip_frontmatter,
)
from code_demo.rag.dedup_embedding import (
    MinHash,
    MinHashLSH,
    UnionFind,
    Entity,
    cosine_similarity,
    embed_pages,
    candidate_pairs,
    cluster_by_pairs,
    deduplicate_entities,
    deduplicate_by_embedding,
)
from code_demo.rag.graph_cluster import (
    GraphCluster,
    ClusterResult,
    GraphNode,
    GraphEdge,
    build_graph,
    cluster,
    cohesion_score,
    score_all,
)
from code_demo.rag.hybrid_retriever import (
    Document,
    SearchResult,
    BM25Index,
    EmbeddingIndex,
    HybridRetriever,
    tokenize_query,
    STOP_WORDS,
)
from code_demo.rag.embedder import Embedder, EmbeddingResult
from code_demo.rag.vector_store import (
    VectorStore,
    Document as VectorDocument,
    SearchResult as VectorSearchResult,
)
from code_demo.rag.reranker import (
    RankedResult,
    Reranker,
)
from code_demo.rag.document_indexer import DocumentIndexer, IndexStats
from code_demo.rag.unified_graph import UnifiedGraph, GraphNode, GraphEdge, GraphStats
from code_demo.rag.query_router import QueryRouter, QueryPlan
from code_demo.rag.cross_layer_links import CrossLayerLinkDetector, DetectedLink

__all__ = [
    # text_chunker
    "Chunk", "ChunkOptions", "chunk_markdown", "strip_frontmatter",
    # dedup_embedding
    "MinHash", "MinHashLSH", "UnionFind", "Entity",
    "cosine_similarity", "embed_pages", "candidate_pairs",
    "cluster_by_pairs", "deduplicate_entities", "deduplicate_by_embedding",
    # graph_cluster
    "GraphCluster", "ClusterResult", "GraphNode", "GraphEdge",
    "build_graph", "cluster", "cohesion_score", "score_all",
    # hybrid_retriever
    "Document", "SearchResult", "BM25Index", "EmbeddingIndex",
    "HybridRetriever", "tokenize_query", "STOP_WORDS",
    # reranker
    "RankedResult", "Reranker",
    # document_indexer
    "DocumentIndexer", "IndexStats",
    # unified_graph
    "UnifiedGraph", "GraphNode", "GraphEdge", "GraphStats",
    # query_router
    "QueryRouter", "QueryPlan",
    # cross_layer_links
    "CrossLayerLinkDetector", "DetectedLink",
    # embedder
    "Embedder", "EmbeddingResult",
    # vector_store
    "VectorStore", "VectorDocument", "VectorSearchResult",
]
