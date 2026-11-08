# RAG 子系统

> 移植自 llm_harness 项目（llm_wiki + graphify），适配 code_demo 的检索增强生成能力。
> 最后更新：2026-06-22（Phase 2.5 统一知识图谱引擎全部完成）

## 概述

`code_demo/rag/` 是一个完整的 RAG（Retrieval-Augmented Generation）子系统，提供从文本分块、去重、聚类到混合检索的全流程能力。

Phase 2 增强模块已全部完成，新增 Embedding 服务、向量存储、文档索引器和重排序四个模块。

Phase 2.5 统一知识图谱引擎已全部完成，新增 SQLite 统一图谱、跨层链接检测和查询路由三个模块。

所有模块均为**纯函数/无状态**设计，可独立使用或组合为完整管线。

## 模块结构

```
rag/
├── __init__.py              # 统一导出：Embedder/EmbeddingResult/VectorStore/VectorDocument/VectorSearchResult/
│                            #   Reranker/RankedResult/DocumentIndexer/IndexStats + 基础4模块
│                            #   + UnifiedGraph/GraphNode/GraphEdge/GraphStats
│                            #   + CrossLayerLinkDetector/DetectedLink + QueryRouter/QueryPlan
├── text_chunker.py          # Markdown 感知文本分块器
├── dedup_embedding.py       # MinHash/LSH + Embedding 去重
├── graph_cluster.py         # Leiden 社区检测
├── hybrid_retriever.py      # CJK 双字分词 + BM25 + Embedding 混合检索
├── embedder.py              # ✅ Phase 2 — dashscope text-embedding-v3 封装
├── vector_store.py          # ✅ Phase 2 — SQLite + numpy 轻量级向量存储
├── reranker.py              # ✅ Phase 2 — RRF 多路融合重排序
├── document_indexer.py      # ✅ Phase 2 — 增量文档索引器
├── unified_graph.py         # ✅ Phase 2.5 — SQLite 统一知识图谱
├── cross_layer_links.py     # ✅ Phase 2.5 — 三策略跨层链接检测
└── query_router.py          # ✅ Phase 2.5 — 查询意图分类路由
```

## 模块详解

### 1. text_chunker — Markdown 感知文本分块器

**来源**: llm_wiki/src/lib/text-chunker.ts

将 Markdown 文档分割为适合 embedding 的文本块，遵循 8 项设计约束：

| # | 约束 | 说明 |
|---|------|------|
| 1 | heading 面包屑 | 每个 chunk 携带 `heading_path`（`## Intro > ### Usage`） |
| 2 | 分割优先级 | 标题 → 段落 → 换行 → 句子 → 空格 → 硬切 |
| 3 | 代码块不拆分 | ` ``` ... ``` ` 围栏代码块作为原子单元 |
| 4 | 表格不拆分 | 连续 `|` 开头的行作为原子单元 |
| 5 | YAML frontmatter 剥离 | 分块前移除 `--- ... ---` |
| 6 | 相邻 overlap | chunk 之间注入重叠文本（句子/词边界对齐） |
| 7 | 微小 chunk 合并 | 短于 `min_chars` 的 chunk 合并到邻居 |
| 8 | 确定性 | 相同输入 → 相同输出 |

**核心 API**:

```python
from code_demo.rag import chunk_markdown, ChunkOptions

# 默认选项
chunks = chunk_markdown(content)

# 自定义选项
opts = ChunkOptions(
    target_chars=1000,    # 目标字符数
    max_chars=1500,       # 硬上限
    min_chars=200,        # 合并阈值
    overlap_chars=200,    # 重叠字符数
)
chunks = chunk_markdown(content, opts)

# Chunk 属性
for c in chunks:
    c.index         # 顺序索引
    c.text          # 文本内容
    c.heading_path  # 标题面包屑
    c.char_start    # 原始偏移
    c.char_end      # 结束偏移
    c.oversized     # 是否超过 max_chars
```

### 2. dedup_embedding — MinHash/LSH + Embedding 去重

**来源**: llm_wiki/src/lib/dedup_embedding.ts + graphify/_minhash.py + graphify/dedup.py

提供两种去重管线：

#### 2a. 实体文本去重（3 趟管线）

```
Pass 1: 规范化 + 熵过滤 → 精确去重
Pass 2: MinHash/LSH blocking + Jaro-Winkler 验证
Pass 3: Union-Find 合并 + 代表选取
```

```python
from code_demo.rag import deduplicate_entities, Entity

entities = [
    Entity(text="Python 3.11", label="version", source="doc1"),
    Entity(text="python 3.11", label="version", source="doc2"),
    Entity(text="Java 17", label="version", source="doc1"),
]

results = deduplicate_entities(entities)
# [{"canonical": "Python 3.11", "aliases": ["python 3.11"], ...}]
```

**MinHash**: Mersenne-prime 置换哈希族，兼容 datasketch。  
**Band-LSH**: 最优 b/r 参数搜索（数值积分替代 scipy）。  
**Jaro-Winkler**: 前缀奖励（≤ 4 字符），阈值 0.92。

#### 2b. Embedding 向量去重

```python
from code_demo.rag import deduplicate_by_embedding

items = [
    {"id": "doc1", "embedding": [0.1, 0.2, ...]},
    {"id": "doc2", "embedding": [0.1, 0.2, ...]},  # 与 doc1 相似
    {"id": "doc3", "embedding": [0.9, 0.8, ...]},  # 不同
]

result = deduplicate_by_embedding(items, threshold=0.95)
# 保留 doc1 和 doc3，doc2 被标记为 _dedup_cluster=0
```

**流程**: cosine similarity → Top-K candidate pairs → Union-Find clustering

### 3. graph_cluster — Leiden 社区检测

**来源**: graphify/worked/mixed-corpus/raw/cluster.py

对知识图谱进行社区检测，支持 igraph（首选）和纯 Python 降级。

```python
from code_demo.rag import cluster, build_graph

nodes = [{"id": "a", "label": "Python"}, {"id": "b", "label": "Flask"}, ...]
edges = [{"source": "a", "target": "b", "weight": 1.0}, ...]

result = cluster(nodes, edges)

for c in result.clusters:
    c.label        # "community_0"
    c.member_ids   # ["a", "b", "c"]
    c.cohesion     # 0.75 (边密度)
    c.oversized    # 是否拆分后的子社区
```

**特性**:
- igraph Leiden（modularity 目标函数）→ 降级到纯 Python 贪婪模块检测
- 超大社区自动拆分：超过总节点数 25% 的社区被递归子图聚类
- `cohesion_score()`: 社区内聚度 = 内部实际边数 / 最大可能边数

### 4. hybrid_retriever — 混合检索

**来源**: llm_wiki/src/lib/search.ts + llm_wiki/src/lib/embedding.ts

CJK 双字分词 + BM25 + Embedding 三路检索融合。

```python
from code_demo.rag import HybridRetriever, Document

retriever = HybridRetriever(alpha=0.4)  # BM25 权重 0.4, Embedding 权重 0.6

docs = [
    Document(doc_id="1", title="Python 基础", content="Python 是一种..."),
    Document(doc_id="2", title="Java 基础", content="Java 是一种..."),
]
retriever.add_documents(docs)
results = retriever.search("Python 编程", top_k=10)

for r in results:
    r.doc_id       # 文档 ID
    r.score        # 融合分数
    r.bm25_score   # BM25 分数
    r.embed_score  # Embedding 分数
    r.snippet      # 摘要片段
```

#### CJK 双字分词

```python
from code_demo.rag import tokenize_query

tokens = tokenize_query("这是一个中文测试查询 test query")
# ['这是', '是一', '个中', '中文', '文测', '测试', '试查', '查询', 'test', 'query']
```

中文字符生成 bigram + 单字 token，非中文字符按空格/标点分割，结果经停用词过滤。

#### BM25 索引

- 倒排索引 + BM25 TF-IDF（k1=1.5, b=0.75）
- IDF: `log((N - df + 0.5) / (df + 0.5) + 1)`

#### Embedding 搜索

- chunk 级 3x 过采样（overfetch）
- 页级聚合：max-pool（最高 chunk 分数）+ 0.3 × 次优分数之和
- Min-Max 归一化后与 BM25 融合

---

## Phase 2 增强模块（✅ 2026-06-22 已全部完成）

### 5. embedder — Embedding 服务

**路径**: `rag/embedder.py`（~185 行）  
**导出**: `Embedder`, `EmbeddingResult`

dashscope text-embedding-v3 封装，1024 维向量。

| 特性 | 说明 |
|------|------|
| 批量调用 | 一次最多 25 条文本 |
| Graceful fallback | 无 API key 时返回零向量，不抛异常 |
| 异步支持 | `embed(text)` 单条 + `embed_batch(texts)` 批量 |
| 结果表示 | `EmbeddingResult(text, embedding, model, tokens_used)` |

```python
from code_demo.rag import Embedder

embedder = Embedder()  # 无 API key 时自动 fallback
result = embedder.embed("这是一段测试文本")
# result.embedding: list[float], 长度 1024

results = embedder.embed_batch(["文本1", "文本2", "文本3"])
```

### 6. vector_store — 轻量级向量存储

**路径**: `rag/vector_store.py`（~285 行）  
**导出**: `VectorStore`, `VectorDocument`, `VectorSearchResult`

SQLite + numpy 实现的轻量级向量数据库，无需外部向量数据库依赖。

| 特性 | 说明 |
|------|------|
| 存储引擎 | SQLite + numpy 数组序列化 |
| 检索算法 | 暴力余弦相似度（小数据集足够） |
| 并发支持 | WAL 模式，支持并发读 |
| Metadata 过滤 | 支持按平标过滤检索结果 |

```python
from code_demo.rag import VectorStore, VectorDocument

store = VectorStore("/path/to/db.sqlite")

# 插入/更新
store.upsert(VectorDocument(
    doc_id="doc_001",
    text="这是文档内容",
    embedding=[0.1, 0.2, ...],  # 1024 维
    metadata={"source": "doc.md", "type": "markdown"}
))

# 检索
results = store.search(query_embedding, top_k=5, filter_metadata={"type": "markdown"})
for r in results:
    r.doc_id      # 文档 ID
    r.score       # 余弦相似度
    r.text        # 文本内容
    r.metadata    # 元数据
```

### 7. reranker — 多路融合重排序

**路径**: `rag/reranker.py`（~238 行）  
**导出**: `Reranker`, `RankedResult`

RRF（Reciprocal Rank Fusion）多路融合重排序，将向量检索和 BM25 检索的结果合并。

| 特性 | 说明 |
|------|------|
| RRF 算法 | 多路检索结果融合，控制各路权重 |
| n-gram Jaccard 去重 | 默认阈值 0.85，移除重复片段 |
| 元数据保留 | 返回各路来源标记 |

```python
from code_demo.rag import Reranker

reranker = Reranker()

# bm25_results 和 vector_results 是两路检索结果列表
ranked = reranker.rerank(
    query="Python 编程",
    results_list=[bm25_results, vector_results],
    weights=[0.4, 0.6]   # 各路权重
)
for r in ranked:
    r.text        # 文本内容
    r.rrf_score   # RRF 融合分数
    r.sources     # 来源标记（哪路检索命中）
```

### 8. document_indexer — 增量文档索引器

**路径**: `rag/document_indexer.py`（~290 行）  
**导出**: `DocumentIndexer`, `IndexStats`

增量文档索引器，自动检测文件变更并更新向量库。

| 特性 | 说明 |
|------|------|
| 增量测量 | 文件 mtime + 内容哈希变更检测 |
| 代码分块 | Python/JS/TS 等按函数/类分块（正则 AST 解析） |
| Markdown 分块 | 复用 text_chunker.chunk_markdown() |
| 其他文件 | 按固定大小分块 |

```python
from code_demo.rag import DocumentIndexer

indexer = DocumentIndexer(
    vector_store=store,
    embedder=embedder
)

# 单文件索引
indexer.index_file("/path/to/file.py")

# 目录增量索引
stats = indexer.index_directory("/path/to/project", extensions=[".py", ".md"])
print(stats.indexed, stats.skipped, stats.failed)

# 检索
results = indexer.search("如何实现 TTS", top_k=5)
```

## 完整数据流

```
原始文档/代码
    │
    ├─ document_indexer.index_file()
    │   │
    │   ├─ Markdown: text_chunker.chunk_markdown() → Chunk 列表
    │   └─ 代码: AST 解析 → 函数/类分块
    │
    ├─ embedder.embed_batch() → 1024维向量
    │
    └─ vector_store.upsert() → SQLite 持久化

用户查询
    │
    ├─ embedder.embed(query) → 查询向量
    │
    ├─ vector_store.search() → 向量检索结果
    ├─ hybrid_retriever.search() → BM25 + Embedding 结果
    │
    └─ reranker.rerank() → RRF 融合排序 → 最终结果
```

## 与现有系统的集成点

| 集成点 | 说明 |
|--------|------|
| `core/utils/memory_relevance.py` | 4 信号记忆评分可结合 RAG 检索结果进行记忆增强 |
| `core/utils/context_budget.py` | 上下文预算分配器控制 RAG 检索结果的 token 分配 |
| `core/utils/memory.py` | MemoryManager 的记忆文件可作为 DocumentIndexer 的知识源 |
| `core/utils/dream.py` | DreamConsolidator 的记忆整合可结合 graph_cluster 进行社区发现 |
| `rag/embedder.py` | 提供向量嵌入，VectorStore 和 DocumentIndexer 依赖此模块 |
| `rag/vector_store.py` | DocumentIndexer 的底层存储层 |
| `rag/reranker.py` | 对 HybridRetriever + VectorStore 的合并结果进行二次排序 |
| `rag/unified_graph.py` | 统一图谱节点包含记忆、技能、文档、代码实体；cross_layer_links 依赖此模块 |
| `rag/cross_layer_links.py` | 跨层链接检测依赖 unified_graph + dedup_embedding (MinHash) |
| `rag/query_router.py` | 路由结果分发给 ThreeLayerMemory.recall() / SkillRegistry.search() / HybridRetriever |

## Phase 2.5 统一知识图谱引擎（✅ 2026-06-22 已全部完成）

### 9. unified_graph — SQLite 统一知识图谱

**路径**: `rag/unified_graph.py`（~340 行）  
**导出**: `UnifiedGraph`, `GraphNode`, `GraphEdge`, `GraphStats`

将记忆、技能、会话、文档、代码结构关联为一张 SQLite 图谱。

| 特性 | 说明 |
|------|------|
| 存储引擎 | SQLite WAL 模式，支持并发读 |
| Schema | `nodes` 表 + `edges` 表 + `metadata` 表 |
| 节点类型 | memory / skill / session / document / code_entity |
| 层级 | knowledge（知识节点）/ structure（来自 Graphify）/ cross（跨层） |
| 置信度 | HIGH / MEDIUM / LOW |

```python
from code_demo.rag import UnifiedGraph

graph = UnifiedGraph("/path/to/graph.db")

# 导入
graph.import_memories(memory_manager)   # 记忆节点 (type=memory)
graph.import_skills(skill_registry)     # 技能节点 (type=skill)
graph.import_structure(graph_json_path) # 代码结构 (type=code_entity)
graph.sync_from_files()                 # 全量增量同步

# 查询
neighbors = graph.get_neighbors(node_id, relation="feedback_to")
path = graph.find_path(source_id, target_id, max_depth=3)
results = graph.search_nodes(keyword="TTS", node_type="skill")

# 统计与导出
stats = graph.stats()            # {"total_nodes": ..., "total_edges": ..., "by_type": ...}
data = graph.export_graph_json() # {"nodes": [...], "edges": [...]}
```

### 10. cross_layer_links — 跨层链接检测

**路径**: `rag/cross_layer_links.py`（~284 行）  
**导出**: `CrossLayerLinkDetector`, `DetectedLink`

三种策略检测记忆、技能、会话、代码节点间的关联。

| 策略 | 置信度 | 说明 |
|------|--------|------|
| 名称精确匹配 | HIGH | 归一化名称后精确比对 |
| 关键词重叠 | MEDIUM | ≥ 2 个共享关键词 |
| MinHash 语义推断 | LOW | Jaccard > 0.5，复用 dedup_embedding |

```python
from code_demo.rag import CrossLayerLinkDetector

detector = CrossLayerLinkDetector(graph)

# 检测所有跨层链接
links = detector.detect_all()              # 返回 DetectedLink 列表

# 检测单个节点的关联
links = detector.detect_for_node(node_id)

# 应用检测结果（HIGH/MEDIUM 自动入图，LOW 进审查）
detector.apply_links(links)

for link in links:
    link.source_id    # 源节点
    link.target_id    # 目标节点
    link.relation     # feedback_to / guards / implements / related_to
    link.confidence   # HIGH / MEDIUM / LOW
    link.evidence     # 关联依据描述
```

### 11. query_router — 查询意图分类路由

**路径**: `rag/query_router.py`（~150 行）  
**导出**: `QueryRouter`, `QueryPlan`

根据用户查询自动分配检索策略，正则匹配零 LLM 成本。

| 类型 | 触发模式 | 路由策略 |
|------|---------|----------|
| memory | 还记得/上次/之前/偏好 | ThreeLayerMemory.recall() |
| skill | 怎么做/步骤/流程/部署 | SkillRegistry.search() |
| knowledge | 什么是/解释/原理/概念 | HybridRetriever |
| structure | 调用/依赖/模块/函数/类 | graph_tools.graph_query() |
| hybrid | 混合 | 多策略合并 |

```python
from code_demo.rag import QueryRouter

router = QueryRouter()

plan = router.route("还记得我说过不要用 mock 吗")
# plan.query_type = "memory"
# plan.strategies = ["three_layer_memory"]

plan = router.route("怎么部署 CosyVoice")
# plan.query_type = "skill"

plan = router.route("什么是 Leiden 社区检测")
# plan.query_type = "knowledge"
```

## 依赖

- **numpy** — MinHash 向量化计算、BM25 矩阵运算、cosine similarity、向量存储序列化
- **igraph**（可选）— Leiden 社区检测；不可用时自动降级到纯 Python
- **dashscope**（可选）— text-embedding-v3 API；无 API key 时 graceful fallback（返回零向量）
- **sqlite3** — Python 标准库，无需额外安装（用于 VectorStore 和 DocumentIndexer）
