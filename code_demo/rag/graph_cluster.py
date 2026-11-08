"""图聚类 — Leiden 社区检测 + 超大社区拆分。

移植自 graphify/worked/mixed-corpus/raw/cluster.py，适配 code_demo 的 RAG 子系统。

功能：
  1. 从节点/边构建 igraph 图
  2. Leiden 社区检测（可选 resolution 参数）
  3. 超大社区自动拆分（超过总节点数 25%，最少 10 个节点）
  4. 内聚度评分（边密度）
  5. 纯 Python 备选（无 igraph 时使用贪婪模块检测）

用法：
    from code_demo.rag.graph_cluster import (
        build_graph, cluster, cohesion_score, score_all,
        GraphCluster, ClusterResult,
    )

    clusters = cluster(nodes, edges)
    for c in clusters:
        print(c.label, c.member_ids, c.cohesion)
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class GraphCluster:
    """一个聚类（社区）。"""
    label: str                       # 社区标签
    member_ids: list[str]            # 成员节点 ID
    cohesion: float = 0.0            # 内聚度（0~1，边密度）
    oversized: bool = False          # 是否为拆分后的子社区


@dataclass
class ClusterResult:
    """聚类结果。"""
    clusters: list[GraphCluster]     # 社区列表
    total_nodes: int                 # 总节点数
    total_edges: int                 # 总边数
    modularity: float = 0.0          # 模块度（igraph 提供时为实际值）


# ── 节点/边定义 ──────────────────────────────────────────────────────────────

@dataclass
class GraphNode:
    """图中的节点。"""
    id: str  # 图节点唯一标识
    label: str = ""  # 图节点显示名称
    metadata: dict = field(default_factory=dict)  # 图节点的附加属性


@dataclass
class GraphEdge:
    """图中的边。"""
    source: str  # 起始节点 ID
    target: str  # 目标节点 ID
    weight: float = 1.0  # 边的连接权重


# ── igraph 路径（首选）───────────────────────────────────────────────────────

def _try_igraph(nodes: list[GraphNode], edges: list[GraphEdge]):
    """尝试使用 igraph 构建图和聚类。返回 None 如果 igraph 不可用。"""
    try:
        import igraph as ig
    except ImportError:
        return None

    if not nodes:
        return ClusterResult(clusters=[], total_nodes=0, total_edges=len(edges))

    id_to_idx = {n.id: i for i, n in enumerate(nodes)}
    g = ig.Graph(n=len(nodes), directed=False)
    g.vs["name"] = [n.id for n in nodes]
    g.vs["label"] = [n.label or n.id for n in nodes]

    edge_list = []
    weights = []
    for e in edges:
        si, ti = id_to_idx.get(e.source), id_to_idx.get(e.target)
        if si is not None and ti is not None and si != ti:
            edge_list.append((si, ti))
            weights.append(e.weight)

    if edge_list:
        g.add_edges(edge_list)
        g.es["weight"] = weights

    # Leiden 社区检测
    try:
        partition = g.community_leiden(
            objective_function="modularity",
            weights="weight" if weights else None,
            resolution=1.0,
        )
    except Exception:
        # 降级到 Louvain
        partition = g.community_multilevel(weights="weight" if weights else None)

    modularity = g.modularity(partition, weights="weight" if weights else None)

    clusters: list[GraphCluster] = []
    for comm_id, member_indices in enumerate(partition):
        member_ids = [nodes[i].id for i in member_indices]
        clusters.append(GraphCluster(
            label=f"community_{comm_id}",
            member_ids=member_ids,
        ))

    return ClusterResult(
        clusters=clusters,
        total_nodes=len(nodes),
        total_edges=len(edge_list),
        modularity=modularity,
    )


# ── 纯 Python 备选 — 贪婪模块检测 ────────────────────────────────────────────

def _greedy_modularity(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> ClusterResult:
    """纯 Python 贪婪模块社区检测。

    当 igraph 不可用时的降级方案。
    使用简单的邻居聚类：从度数最高的节点开始，贪心地添加高度连接的邻居。
    """
    if not nodes:
        return ClusterResult(clusters=[], total_nodes=0, total_edges=len(edges))

    # 构建邻接表
    adj: dict[str, dict[str, float]] = {n.id: {} for n in nodes}
    for e in edges:
        if e.source in adj and e.target in adj and e.source != e.target:
            adj[e.source][e.target] = e.weight
            adj[e.target][e.source] = e.weight

    # 按度数排序
    sorted_nodes = sorted(adj.keys(), key=lambda n: len(adj[n]), reverse=True)

    assigned: dict[str, int] = {}
    clusters: list[GraphCluster] = []
    comm_id = 0

    for nid in sorted_nodes:
        if nid in assigned:
            continue

        # 新社区
        members = [nid]
        assigned[nid] = comm_id

        # 贪心添加邻居
        candidates = sorted(
            adj[nid].items(),
            key=lambda x: x[1],
            reverse=True,
        )
        for neighbor, weight in candidates:
            if neighbor in assigned:
                continue
            # 检查与当前社区的连接强度
            community_connections = sum(
                1 for m in members if neighbor in adj.get(m, {})
            )
            if community_connections >= max(1, len(members) // 3):
                members.append(neighbor)
                assigned[neighbor] = comm_id

        clusters.append(GraphCluster(
            label=f"community_{comm_id}",
            member_ids=members,
        ))
        comm_id += 1

    return ClusterResult(
        clusters=clusters,
        total_nodes=len(nodes),
        total_edges=sum(1 for e in edges if e.source in adj and e.target in adj),
    )


# ── 超大社区拆分（移植自 graphify/cluster.py）─────────────────────────────────

_OVERSIZED_FRAC = 0.25   # 超过总节点数 25% 视为超大
_MIN_SPLIT_SIZE = 10     # 拆分子社区最少节点数


def _split_oversized(
    result: ClusterResult,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> ClusterResult:
    """拆分超大社区。

    移植自 graphify/cluster.py 的超大社区拆分逻辑。

    当某个社区包含超过 25% 的总节点数时，
    使用子图重新聚类将其拆分。
    """
    total = result.total_nodes
    threshold = max(int(total * _OVERSIZED_FRAC), _MIN_SPLIT_SIZE)

    # 构建邻接表
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e.source, set()).add(e.target)
        adj.setdefault(e.target, set()).add(e.source)

    new_clusters: list[GraphCluster] = []

    for cluster in result.clusters:
        if len(cluster.member_ids) <= threshold:
            new_clusters.append(cluster)
            continue

        # 超大社区 — 子图重新聚类
        member_set = set(cluster.member_ids)
        sub_edges = [
            GraphEdge(source=e.source, target=e.target, weight=e.weight)
            for e in edges
            if e.source in member_set and e.target in member_set
        ]
        sub_nodes = [
            GraphNode(id=nid)
            for nid in cluster.member_ids
        ]

        # 递归聚类（使用纯 Python 方法避免无限递归）
        sub_result = _greedy_modularity(sub_nodes, sub_edges)

        for sub_cluster in sub_result.clusters:
            sub_cluster.oversized = True
            sub_cluster.label = f"{cluster.label}_sub_{sub_cluster.label}"
            new_clusters.append(sub_cluster)

    return ClusterResult(
        clusters=new_clusters,
        total_nodes=result.total_nodes,
        total_edges=result.total_edges,
        modularity=result.modularity,
    )


# ── 内聚度评分 ────────────────────────────────────────────────────────────────

def cohesion_score(
    member_ids: list[str],
    edges: list[GraphEdge],
) -> float:
    """计算社区的内聚度（边密度）。

    移植自 graphify/cluster.py cohesion_score()。

    内聚度 = 社区内部实际边数 / 最大可能边数

    Returns:
        0.0 ~ 1.0 的内聚度分数
    """
    n = len(member_ids)
    if n < 2:
        return 0.0

    member_set = set(member_ids)
    internal_edges = sum(
        1 for e in edges
        if e.source in member_set and e.target in member_set
    )
    max_edges = n * (n - 1) / 2
    return internal_edges / max_edges if max_edges > 0 else 0.0


def score_all(
    result: ClusterResult,
    edges: list[GraphEdge],
) -> ClusterResult:
    """为所有社区计算内聚度。

    移植自 graphify/cluster.py score_all()。
    """
    for cluster in result.clusters:
        cluster.cohesion = cohesion_score(cluster.member_ids, edges)
    return result


# ── 公共 API ──────────────────────────────────────────────────────────────────

def build_graph(
    node_dicts: list[dict],
    edge_dicts: list[dict],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """从字典列表构建节点和边。

    移植自 graphify/cluster.py build_graph()。

    Args:
        node_dicts: [{"id": "...", "label": "...", ...}, ...]
        edge_dicts: [{"source": "...", "target": "...", "weight": 1.0}, ...]

    Returns:
        (nodes, edges) 元组
    """
    nodes = [
        GraphNode(
            id=d.get("id", ""),
            label=d.get("label", d.get("id", "")),
            metadata={k: v for k, v in d.items() if k not in ("id", "label")},
        )
        for d in node_dicts
    ]
    edges = [
        GraphEdge(
            source=d.get("source", ""),
            target=d.get("target", ""),
            weight=float(d.get("weight", 1.0)),
        )
        for d in edge_dicts
    ]
    return nodes, edges


def cluster(
    nodes: list[GraphNode] | list[dict],
    edges: list[GraphEdge] | list[dict],
    split_oversized: bool = True,
) -> ClusterResult:
    """对图进行社区检测。

    移植自 graphify/cluster.py cluster()。

    1. 优先使用 igraph Leiden 算法
    2. igraph 不可用时降级到纯 Python 贪婪模块检测
    3. 可选拆分超大社区

    Args:
        nodes: 节点列表（GraphNode 或 dict）
        edges: 边列表（GraphEdge 或 dict）
        split_oversized: 是否拆分超大社区

    Returns:
        ClusterResult 包含社区列表和元数据
    """
    # 类型统一
    if nodes and isinstance(nodes[0], dict):
        nodes, edges = build_graph(nodes, edges)  # type: ignore[arg-type]

    # 尝试 igraph
    result = _try_igraph(nodes, edges)  # type: ignore[arg-type]

    # 降级到纯 Python
    if result is None:
        result = _greedy_modularity(nodes, edges)  # type: ignore[arg-type]

    # 拆分超大社区
    if split_oversized:
        result = _split_oversized(result, nodes, edges)  # type: ignore[arg-type]

    # 计算内聚度
    result = score_all(result, edges)  # type: ignore[arg-type]

    return result
