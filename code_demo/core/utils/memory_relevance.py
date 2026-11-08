"""记忆相关性评分 — 4 信号加权模型。

移植自 llm_wiki/src/lib/graph-relevance.ts，适配 code_demo 的记忆系统。

4 信号模型：
  Signal 1: 直接链接 ×3.0 — 记忆之间是否有显式关联
  Signal 2: 来源重叠 ×4.0 — 是否来自同一对话/项目/工具
  Signal 3: 图距离 ×1.5 — Adamic-Adar 共同邻居评分
  Signal 4: 类型亲和 ×1.0 — 记忆类型之间的天然关联度

用法：
    from code_demo.core.utils.memory_relevance import (
        MemoryNode, MemoryGraph,
        build_memory_graph, calculate_relevance, get_related_memories,
    )
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path


# ── 类型定义 ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryNode:
    """记忆图谱中的一个节点。"""
    id: str                              # 唯一标识（memory 文件名去 .md）
    title: str                           # 显示标题
    type: str                            # user / feedback / project / reference / skill_learned / mistake
    path: str                            # 文件路径
    sources: frozenset[str] = field(default_factory=frozenset)  # 来源标识（对话ID、项目名等）
    out_links: frozenset[str] = field(default_factory=frozenset)  # 出链（引用的其他记忆）
    in_links: frozenset[str] = field(default_factory=frozenset)   # 入链（被其他记忆引用）
    # ── 置信度分级字段（Task 104）──
    confidence: str = "medium"           # "high" | "medium" | "low"
    verified: bool = False               # 是否被后续对话确认
    source_type: str = ""                # "user_explicit" | "ai_inference" | "task_result" | "cognitive_personality"


@dataclass
class MemoryGraph:
    """记忆关系图谱。"""
    nodes: dict[str, MemoryNode]  # 按 ID 索引的记忆节点
    data_version: int = 0  # 记忆图数据版本号


# ── 权重常量（移植自 graph-relevance.ts WEIGHTS）─────────────────────────────

WEIGHTS = {
    "direct_link": 3.0,
    "source_overlap": 4.0,
    "common_neighbor": 1.5,
    "type_affinity": 1.0,
}

# 类型亲和矩阵（移植自 graph-relevance.ts TYPE_AFFINITY）
# code_demo 的记忆类型：user / feedback / project / reference / skill_learned / mistake
TYPE_AFFINITY: dict[str, dict[str, float]] = {
    "user":          {"feedback": 1.2, "project": 1.0, "reference": 0.8, "skill_learned": 1.0, "mistake": 1.0, "user": 0.8},
    "feedback":      {"user": 1.2,     "project": 1.0, "reference": 1.0, "skill_learned": 1.2, "mistake": 1.2, "feedback": 0.8},
    "project":       {"user": 1.0,     "feedback": 1.0, "reference": 1.0, "skill_learned": 1.0, "mistake": 1.0, "project": 0.5},
    "reference":     {"user": 0.8,     "feedback": 1.0, "project": 1.0, "skill_learned": 0.8, "mistake": 0.8, "reference": 0.5},
    "skill_learned": {"user": 1.0,     "feedback": 1.2, "project": 1.0, "reference": 0.8, "mistake": 1.0, "skill_learned": 0.8},
    "mistake":       {"user": 1.0,     "feedback": 1.2, "project": 1.0, "reference": 0.8, "skill_learned": 1.0, "mistake": 0.5},
}

# ── frontmatter 解析 ──────────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---\n([\s\S]*?)\n---")
_TITLE_RE = re.compile(r"^title:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
_TYPE_RE = re.compile(r"^type:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
_CONFIDENCE_RE = re.compile(r"^confidence:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
_VERIFIED_RE = re.compile(r"^verified:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
_SOURCE_TYPE_RE = re.compile(r"^source_type:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SOURCES_BLOCK_RE = re.compile(r"^sources:\s*\n((?:\s+-\s+.+\n?)*)", re.MULTILINE)
_SOURCES_INLINE_RE = re.compile(r"^sources:\s*\[([^\]]*)\]", re.MULTILINE)
_SOURCE_ITEM_RE = re.compile(r"^\s+-\s+[\"']?(.+?)[\"']?\s*$")


def _parse_frontmatter(content: str) -> dict:
    """从 Markdown 文件内容解析 frontmatter 元数据。"""
    fm_match = _FM_RE.match(content)
    fm = fm_match.group(1) if fm_match else ""

    title_m = _TITLE_RE.search(fm)
    type_m = _TYPE_RE.search(fm)

    # 解析 sources 数组
    sources: list[str] = []
    block_m = _SOURCES_BLOCK_RE.search(fm)
    if block_m:
        for line in block_m.group(1).split("\n"):
            item_m = _SOURCE_ITEM_RE.match(line)
            if item_m:
                sources.append(item_m.group(1))
    else:
        inline_m = _SOURCES_INLINE_RE.search(fm)
        if inline_m:
            for item in inline_m.group(1).split(","):
                trimmed = item.strip().strip("\"'")
                if trimmed:
                    sources.append(trimmed)

    title = title_m.group(1).strip() if title_m else ""
    if not title:
        heading_m = _HEADING_RE.search(content)
        title = heading_m.group(1).strip() if heading_m else ""

    return {
        "title": title,
        "type": type_m.group(1).strip().lower() if type_m else "reference",
        "sources": sources,
        "confidence": _parse_confidence_level(fm),
        "verified": _parse_verified(fm),
        "source_type": _parse_source_type(fm),
    }


def _parse_confidence_level(fm: str) -> str:
    """从 frontmatter 解析置信度等级字段。"""
    m = _CONFIDENCE_RE.search(fm)
    if not m:
        return "medium"
    raw = m.group(1).strip().lower()
    # 兼容旧格式：如果存储的是浮点数（如 0.8），转为等级
    try:
        val = float(raw)
        if val > 0.7:
            return "high"
        elif val >= 0.4:
            return "medium"
        else:
            return "low"
    except ValueError:
        pass
    # 已经是等级字符串
    if raw in ("high", "medium", "low"):
        return raw
    return "medium"


def _parse_verified(fm: str) -> bool:
    """从 frontmatter 解析 verified 字段。"""
    m = _VERIFIED_RE.search(fm)
    if not m:
        return False
    return m.group(1).strip().lower() in ("true", "1", "yes")


def _parse_source_type(fm: str) -> str:
    """从 frontmatter 解析 source_type 字段。"""
    m = _SOURCE_TYPE_RE.search(fm)
    if not m:
        return ""
    return m.group(1).strip()


# ── Wikilink 提取 ─────────────────────────────────────────────────────────────

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")


def _extract_links(content: str) -> list[str]:
    """提取 Markdown 中的 [[wikilink]] 引用。"""
    return [m.strip() for m in _WIKILINK_RE.findall(content)]


# ── 链接解析 ──────────────────────────────────────────────────────────────────

def _resolve_target(raw: str, node_ids: set[str]) -> str | None:
    """尝试将原始链接文本解析为已知的节点 ID。"""
    if raw in node_ids:
        return raw
    normalized = raw.lower().replace(" ", "-")
    for nid in node_ids:
        nid_lower = nid.lower()
        if nid_lower == normalized:
            return nid
        if nid_lower == raw.lower():
            return nid
        if nid_lower.replace(" ", "-") == normalized:
            return nid
    return None


# ── 图谱构建 ──────────────────────────────────────────────────────────────────

def build_memory_graph(memory_dir: str | Path, data_version: int = 0) -> MemoryGraph:
    """扫描 memory 目录，构建记忆关系图谱。

    移植自 graph-relevance.ts buildRetrievalGraph()。

    Args:
        memory_dir: memory 目录路径（包含 .md 文件）
        data_version: 数据版本号，用于缓存失效

    Returns:
        MemoryGraph 包含所有记忆节点和它们之间的链接关系
    """
    memory_path = Path(memory_dir)
    if not memory_path.exists():
        return MemoryGraph(nodes={}, data_version=data_version)

    # 收集所有 .md 文件
    md_files = sorted(memory_path.glob("*.md"))
    if not md_files:
        return MemoryGraph(nodes={}, data_version=data_version)

    # 第一遍：读取所有文件，提取元数据和原始链接
    raw_nodes: list[dict] = []
    for md_file in md_files:
        if md_file.name == "MEMORY.md":
            continue  # 跳过索引文件
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        node_id = md_file.stem
        fm = _parse_frontmatter(content)
        raw_nodes.append({
            "id": node_id,
            "title": fm["title"] or md_file.stem.replace("_", " "),
            "type": fm["type"],
            "path": str(md_file),
            "sources": fm["sources"],
            "raw_links": _extract_links(content),
            "confidence": fm["confidence"],
            "verified": fm["verified"],
            "source_type": fm["source_type"],
        })

    node_ids = {n["id"] for n in raw_nodes}

    # 第二遍：解析链接，构建出链/入链映射
    out_links: dict[str, set[str]] = {nid: set() for nid in node_ids}
    in_links: dict[str, set[str]] = {nid: set() for nid in node_ids}

    for raw in raw_nodes:
        for link_target in raw["raw_links"]:
            resolved = _resolve_target(link_target, node_ids)
            if resolved is None or resolved == raw["id"]:
                continue
            out_links[raw["id"]].add(resolved)
            in_links[resolved].add(raw["id"])

    # 构建节点映射
    nodes: dict[str, MemoryNode] = {}
    for raw in raw_nodes:
        nodes[raw["id"]] = MemoryNode(
            id=raw["id"],
            title=raw["title"],
            type=raw["type"],
            path=raw["path"],
            sources=frozenset(raw["sources"]),
            out_links=frozenset(out_links.get(raw["id"], set())),
            in_links=frozenset(in_links.get(raw["id"], set())),
            confidence=raw["confidence"],
            verified=raw["verified"],
            source_type=raw["source_type"],
        )

    return MemoryGraph(nodes=nodes, data_version=data_version)


# ── 核心评分函数 ──────────────────────────────────────────────────────────────

def _get_neighbors(node: MemoryNode) -> frozenset[str]:
    """获取节点的所有邻居（出链 + 入链）。"""
    return node.out_links | node.in_links


def _get_degree(node: MemoryNode) -> int:
    """获取节点的度数（出链 + 入链数量）。"""
    return len(node.out_links) + len(node.in_links)


def calculate_relevance(
    node_a: MemoryNode,
    node_b: MemoryNode,
    graph: MemoryGraph,
) -> float:
    """计算两个记忆节点之间的相关性评分。

    移植自 graph-relevance.ts calculateRelevance()。

    4 信号加权：
      1. 直接链接 (×3.0) — A→B 或 B→A 的显式引用
      2. 来源重叠 (×4.0) — 共享的 sources 数量
      3. 共同邻居 (×1.5) — Adamic-Adar 指数
      4. 类型亲和 (×1.0) — 类型间的天然关联度

    Returns:
        相关性评分（越高越相关），同一节点返回 0
    """
    if node_a.id == node_b.id:
        return 0.0

    # Signal 1: 直接链接 (weight 3.0)
    forward = 1 if node_b.id in node_a.out_links else 0
    backward = 1 if node_a.id in node_b.out_links else 0
    direct_link_score = (forward + backward) * WEIGHTS["direct_link"]

    # Signal 2: 来源重叠 (weight 4.0)
    shared_sources = len(node_a.sources & node_b.sources)
    source_overlap_score = shared_sources * WEIGHTS["source_overlap"]

    # Signal 3: 共同邻居 — Adamic-Adar (weight 1.5)
    neighbors_a = _get_neighbors(node_a)
    neighbors_b = _get_neighbors(node_b)
    adamic_adar = 0.0
    for neighbor_id in neighbors_a:
        if neighbor_id in neighbors_b:
            neighbor = graph.nodes.get(neighbor_id)
            if neighbor:
                degree = _get_degree(neighbor)
                adamic_adar += 1.0 / math.log(max(degree, 2))
    common_neighbor_score = adamic_adar * WEIGHTS["common_neighbor"]

    # Signal 4: 类型亲和 (weight 1.0)
    affinity_map = TYPE_AFFINITY.get(node_a.type, {})
    type_affinity_score = affinity_map.get(node_b.type, 0.5) * WEIGHTS["type_affinity"]

    return direct_link_score + source_overlap_score + common_neighbor_score + type_affinity_score


def get_related_memories(
    memory_id: str,
    graph: MemoryGraph,
    limit: int = 5,
) -> list[tuple[MemoryNode, float]]:
    """获取与指定记忆最相关的其他记忆。

    移植自 graph-relevance.ts getRelatedNodes()。

    Args:
        memory_id: 查询的记忆 ID
        graph: 记忆关系图谱
        limit: 返回的最大结果数

    Returns:
        [(node, relevance_score), ...] 按评分降序排列
    """
    source_node = graph.nodes.get(memory_id)
    if not source_node:
        return []

    scored: list[tuple[MemoryNode, float]] = []
    for nid, node in graph.nodes.items():
        if nid == memory_id:
            continue
        relevance = calculate_relevance(source_node, node, graph)
        if relevance > 0:
            scored.append((node, relevance))

    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def retrieve_relevant_memories(
    query: str,
    graph: MemoryGraph,
    context_sources: set[str] | None = None,
    context_memory_ids: set[str] | None = None,
    top_k: int = 5,
) -> list[tuple[MemoryNode, float]]:
    """基于查询上下文检索相关记忆。

    结合 4 信号模型与上下文信息，用于 Phase 2 的记忆检索。

    Args:
        query: 查询文本（用于简单关键词匹配作为额外信号）
        graph: 记忆关系图谱
        context_sources: 当前上下文的来源标识集合
        context_memory_ids: 当前上下文已引用的记忆 ID 集合
        top_k: 返回的最大结果数

    Returns:
        [(node, score), ...] 按评分降序排列
    """
    scored: list[tuple[MemoryNode, float]] = []
    query_lower = query.lower()

    for nid, node in graph.nodes.items():
        score = 0.0

        # 基础信号：标题关键词匹配
        if query_lower and query_lower in node.title.lower():
            score += 2.0

        # 来源重叠信号
        if context_sources and node.sources:
            shared = len(node.sources & context_sources)
            score += shared * WEIGHTS["source_overlap"]

        # 直接引用信号
        if context_memory_ids and nid in context_memory_ids:
            score += WEIGHTS["direct_link"]

        # 图距离信号：与上下文记忆的 Adamic-Adar
        if context_memory_ids:
            for ctx_id in context_memory_ids:
                ctx_node = graph.nodes.get(ctx_id)
                if ctx_node:
                    score += calculate_relevance(ctx_node, node, graph) * 0.3

        if score > 0:
            scored.append((node, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]
