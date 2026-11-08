"""统一知识图谱存储 — SQLite 轻量级图数据库

将记忆、技能、会话、代码结构通过节点和边关联，
提供 BFS 路径查找、邻居查询、关键词搜索等能力。

Schema:
    nodes(id TEXT PK, label TEXT, type TEXT, layer TEXT, source TEXT,
          properties_json TEXT, created_at REAL, updated_at REAL)
    edges(source_id TEXT, target_id TEXT, relation TEXT, confidence TEXT,
          weight REAL, layer TEXT, created_at REAL,
          PRIMARY KEY(source_id, target_id, relation))
"""

import sqlite3
import json
import re
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """图谱节点"""
    id: str                       # 唯一标识
    label: str                    # 显示名称
    node_type: str                # memory / skill / session / document / code_entity
    layer: str                    # knowledge / structure / cross
    source: str                   # 来源文件路径或标识
    properties: dict = field(default_factory=dict)  # 图节点结构化属性
    created_at: float = 0.0  # 图节点创建时间
    updated_at: float = 0.0  # 图节点更新时间


@dataclass
class GraphEdge:
    """图谱边"""
    source_id: str  # 起始节点 ID
    target_id: str  # 目标节点 ID
    relation: str                 # feedback_to / guards / implements / related_to / depends_on
    confidence: str               # HIGH / MEDIUM / LOW
    weight: float = 1.0  # 边的连接权重
    layer: str = "cross"          # knowledge / structure / cross
    created_at: float = 0.0  # 边的创建时间


@dataclass
class GraphStats:
    """图谱统计"""
    total_nodes: int = 0  # 图中节点总数
    total_edges: int = 0  # 图中关系总数
    nodes_by_type: dict = field(default_factory=dict)  # 按类别统计的节点数量
    nodes_by_layer: dict = field(default_factory=dict)  # 按图层统计的节点数量
    edges_by_relation: dict = field(default_factory=dict)  # 按关系类型统计的边数量


# ---------------------------------------------------------------------------
# 建表 DDL
# ---------------------------------------------------------------------------

_CREATE_NODES_TABLE = """
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    label           TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    layer           TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT '',
    properties_json TEXT    NOT NULL DEFAULT '{}',
    created_at      REAL    NOT NULL DEFAULT 0,
    updated_at      REAL    NOT NULL DEFAULT 0
)
"""

_CREATE_EDGES_TABLE = """
CREATE TABLE IF NOT EXISTS edges (
    source_id   TEXT    NOT NULL,
    target_id   TEXT    NOT NULL,
    relation    TEXT    NOT NULL,
    confidence  TEXT    NOT NULL DEFAULT 'MEDIUM',
    weight      REAL    NOT NULL DEFAULT 1.0,
    layer       TEXT    NOT NULL DEFAULT 'cross',
    created_at  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, target_id, relation)
)
"""

_INDEX_EDGES_SOURCE = """
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)
"""

_INDEX_EDGES_TARGET = """
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)
"""

_INDEX_NODES_TYPE = """
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)
"""

_INDEX_NODES_LAYER = """
CREATE INDEX IF NOT EXISTS idx_nodes_layer ON nodes(layer)
"""


# ---------------------------------------------------------------------------
# UnifiedGraph
# ---------------------------------------------------------------------------

class UnifiedGraph:
    """统一知识图谱

    SQLite 存储，nodes + edges 两表设计。
    WAL 模式支持并发读。
    """

    def __init__(self, db_path: Path):
        """
        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)  # 统一图谱 SQLite 数据库路径
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))  # 当前图谱数据库连接
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        """创建表和索引"""
        cur = self._conn.cursor()
        cur.execute(_CREATE_NODES_TABLE)
        cur.execute(_CREATE_EDGES_TABLE)
        cur.execute(_INDEX_EDGES_SOURCE)
        cur.execute(_INDEX_EDGES_TARGET)
        cur.execute(_INDEX_NODES_TYPE)
        cur.execute(_INDEX_NODES_LAYER)
        self._conn.commit()

    @staticmethod
    def _row_to_node(row: tuple) -> GraphNode:
        return GraphNode(
            id=row[0],
            label=row[1],
            node_type=row[2],
            layer=row[3],
            source=row[4],
            properties=json.loads(row[5]),
            created_at=row[6],
            updated_at=row[7],
        )

    @staticmethod
    def _row_to_edge(row: tuple) -> GraphEdge:
        return GraphEdge(
            source_id=row[0],
            target_id=row[1],
            relation=row[2],
            confidence=row[3],
            weight=row[4],
            layer=row[5],
            created_at=row[6],
        )

    @staticmethod
    def _parse_frontmatter(text: str) -> Optional[dict]:
        """解析以 --- 分隔的前置元数据"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result: dict = {"content": body.strip()}
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def import_memories(self, memory_dir: Path) -> int:
        """从 .memory/ 目录导入记忆节点

        读取所有 .md 文件（除 MEMORY.md 索引文件），
        解析 frontmatter 提取 name/type/description，
        创建 type=memory, layer=knowledge 的节点。

        Returns:
            导入的节点数量
        """
        if not memory_dir.exists():
            return 0

        count = 0
        now = time.time()
        for md_file in sorted(memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = self._parse_frontmatter(text)
            if not parsed:
                continue

            name = parsed.get("name", md_file.stem)
            node = GraphNode(
                id=f"memory:{name}",
                label=name,
                node_type="memory",
                layer="knowledge",
                source=str(md_file),
                properties={
                    "description": parsed.get("description", ""),
                    "memory_type": parsed.get("type", "project"),
                },
                created_at=now,
                updated_at=now,
            )
            self.add_node(node)
            count += 1

        return count

    def import_skills(self, skills_dir: Path) -> int:
        """从 skills/ 目录导入技能节点

        扫描每个子目录的 SKILL.md，
        创建 type=skill, layer=knowledge 的节点。

        Returns:
            导入的节点数量
        """
        if not skills_dir.exists():
            return 0

        count = 0
        now = time.time()
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = self._parse_frontmatter(text)
            if not parsed:
                continue

            name = parsed.get("name", skill_dir.name)
            node = GraphNode(
                id=f"skill:{name}",
                label=name,
                node_type="skill",
                layer="knowledge",
                source=str(skill_md),
                properties={
                    "description": parsed.get("description", ""),
                    "source": parsed.get("source", "manual"),
                },
                created_at=now,
                updated_at=now,
            )
            self.add_node(node)
            count += 1

        return count

    def import_structure(self, graph_json_path: Path) -> int:
        """从 graphify-out/graph.json 导入代码结构节点

        读取 Graphify 产出的 graph.json，
        将每个代码实体创建为 type=code_entity, layer=structure 的节点，
        将代码依赖关系创建为 edges。

        Returns:
            导入的节点数量
        """
        if not graph_json_path.exists():
            return 0

        try:
            data = json.loads(graph_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0

        now = time.time()
        node_count = 0

        # 导入节点
        for n in data.get("nodes", []):
            node_id = n.get("id", "")
            if not node_id:
                continue
            label = n.get("label", node_id)
            file_type = n.get("file_type", "code")
            source_file = n.get("source_file", "")
            props = {
                k: v for k, v in n.items()
                if k not in ("id", "label", "file_type", "source_file")
            }
            node = GraphNode(
                id=f"struct:{node_id}",
                label=label,
                node_type="code_entity",
                layer="structure",
                source=source_file,
                properties={"file_type": file_type, **props},
                created_at=now,
                updated_at=now,
            )
            self.add_node(node)
            node_count += 1

        # 导入边
        for e in data.get("links", []):
            src = e.get("source", "")
            tgt = e.get("target", "")
            relation = e.get("relation", "related_to")
            if not src or not tgt:
                continue

            # 映射 confidence_score 到 confidence 字符串
            conf_score = e.get("confidence_score", 0.5)
            if isinstance(conf_score, (int, float)):
                if conf_score >= 0.8:
                    confidence = "HIGH"
                elif conf_score >= 0.5:
                    confidence = "MEDIUM"
                else:
                    confidence = "LOW"
            else:
                confidence = "MEDIUM"

            edge = GraphEdge(
                source_id=f"struct:{src}",
                target_id=f"struct:{tgt}",
                relation=relation,
                confidence=confidence,
                weight=e.get("weight", 1.0),
                layer="structure",
                created_at=now,
            )
            self.add_edge(edge)

        return node_count

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_node(self, node: GraphNode) -> None:
        """添加或更新节点（upsert）"""
        now = time.time()
        if node.created_at == 0.0:
            node.created_at = now
        node.updated_at = now

        self._conn.execute(
            """INSERT OR REPLACE INTO nodes
               (id, label, type, layer, source, properties_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node.id,
                node.label,
                node.node_type,
                node.layer,
                node.source,
                json.dumps(node.properties, ensure_ascii=False),
                node.created_at,
                node.updated_at,
            ),
        )
        self._conn.commit()

    def add_edge(self, edge: GraphEdge) -> None:
        """添加或更新边（upsert）"""
        now = time.time()
        if edge.created_at == 0.0:
            edge.created_at = now

        self._conn.execute(
            """INSERT OR REPLACE INTO edges
               (source_id, target_id, relation, confidence, weight, layer, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.source_id,
                edge.target_id,
                edge.relation,
                edge.confidence,
                edge.weight,
                edge.layer,
                edge.created_at,
            ),
        )
        self._conn.commit()

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        """获取单个节点"""
        cur = self._conn.execute(
            "SELECT id, label, type, layer, source, properties_json, "
            "created_at, updated_at FROM nodes WHERE id = ?",
            (node_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def get_neighbors(self, node_id: str, relation: Optional[str] = None,
                      direction: str = "both") -> list[tuple[GraphNode, GraphEdge]]:
        """获取邻居节点

        Args:
            node_id: 中心节点 ID
            relation: 过滤关系类型（可选）
            direction: "out" / "in" / "both"
        Returns:
            (邻居节点, 连接边) 对列表
        """
        results: list[tuple[GraphNode, GraphEdge]] = []
        rel_clause = "AND relation = ?" if relation else ""
        rel_params: list = [relation] if relation else []

        # 出边：node_id 是 source
        if direction in ("out", "both"):
            rows = self._conn.execute(
                f"""SELECT e.source_id, e.target_id, e.relation, e.confidence,
                          e.weight, e.layer, e.created_at,
                          n.id, n.label, n.type, n.layer, n.source,
                          n.properties_json, n.created_at, n.updated_at
                   FROM edges e
                   JOIN nodes n ON n.id = e.target_id
                   WHERE e.source_id = ? {rel_clause}""",
                [node_id] + rel_params,
            ).fetchall()
            for row in rows:
                edge = self._row_to_edge(row[:7])
                neighbor = self._row_to_node(row[7:])
                results.append((neighbor, edge))

        # 入边：node_id 是 target
        if direction in ("in", "both"):
            rows = self._conn.execute(
                f"""SELECT e.source_id, e.target_id, e.relation, e.confidence,
                          e.weight, e.layer, e.created_at,
                          n.id, n.label, n.type, n.layer, n.source,
                          n.properties_json, n.created_at, n.updated_at
                   FROM edges e
                   JOIN nodes n ON n.id = e.source_id
                   WHERE e.target_id = ? {rel_clause}""",
                [node_id] + rel_params,
            ).fetchall()
            for row in rows:
                edge = self._row_to_edge(row[:7])
                neighbor = self._row_to_node(row[7:])
                results.append((neighbor, edge))

        return results

    def find_path(self, source_id: str, target_id: str,
                  max_depth: int = 3) -> Optional[list[str]]:
        """BFS 寻找两节点间最短路径

        Returns:
            节点 ID 路径列表，未找到返回 None
        """
        if source_id == target_id:
            return [source_id]

        # 预构建邻接表
        all_edges = self._conn.execute(
            "SELECT source_id, target_id FROM edges"
        ).fetchall()
        adj: dict[str, list[str]] = {}
        for src, tgt in all_edges:
            adj.setdefault(src, []).append(tgt)
            adj.setdefault(tgt, []).append(src)

        # BFS
        visited: set[str] = {source_id}
        queue: deque[tuple[str, list[str]]] = deque()
        queue.append((source_id, [source_id]))

        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            for neighbor in adj.get(current, []):
                if neighbor in visited:
                    continue
                new_path = path + [neighbor]
                if neighbor == target_id:
                    return new_path
                visited.add(neighbor)
                queue.append((neighbor, new_path))

        return None

    def search_nodes(self, query: str, node_type: Optional[str] = None,
                     layer: Optional[str] = None, limit: int = 10) -> list[GraphNode]:
        """关键词搜索节点（label + properties 模糊匹配）"""
        conditions: list[str] = []
        params: list = []

        # label LIKE
        conditions.append("(label LIKE ? OR properties_json LIKE ?)")
        like_val = f"%{query}%"
        params.extend([like_val, like_val])

        if node_type:
            conditions.append("type = ?")
            params.append(node_type)

        if layer:
            conditions.append("layer = ?")
            params.append(layer)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = self._conn.execute(
            f"""SELECT id, label, type, layer, source, properties_json,
                       created_at, updated_at
                FROM nodes WHERE {where}
                ORDER BY updated_at DESC LIMIT ?""",
            params,
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def delete_node(self, node_id: str) -> bool:
        """删除节点及其相关边"""
        # 先删边
        self._conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                           (node_id, node_id))
        cur = self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def delete_edge(self, source_id: str, target_id: str, relation: str) -> bool:
        """删除边"""
        cur = self._conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 统计与导出
    # ------------------------------------------------------------------

    def stats(self) -> GraphStats:
        """图谱统计信息"""
        total_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

        nodes_by_type: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT type, COUNT(*) FROM nodes GROUP BY type"
        ).fetchall():
            nodes_by_type[row[0]] = row[1]

        nodes_by_layer: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT layer, COUNT(*) FROM nodes GROUP BY layer"
        ).fetchall():
            nodes_by_layer[row[0]] = row[1]

        edges_by_relation: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT relation, COUNT(*) FROM edges GROUP BY relation"
        ).fetchall():
            edges_by_relation[row[0]] = row[1]

        return GraphStats(
            total_nodes=total_nodes,
            total_edges=total_edges,
            nodes_by_type=nodes_by_type,
            nodes_by_layer=nodes_by_layer,
            edges_by_relation=edges_by_relation,
        )

    def export_graph_json(self) -> dict:
        """导出为 JSON 格式（兼容 Graphify 格式）"""
        nodes = []
        for row in self._conn.execute(
            "SELECT id, label, type, layer, source, properties_json, "
            "created_at, updated_at FROM nodes"
        ).fetchall():
            node = self._row_to_node(row)
            n_dict: dict = {
                "id": node.id,
                "label": node.label,
                "file_type": node.node_type,
                "source_file": node.source,
                **node.properties,
            }
            nodes.append(n_dict)

        links = []
        for row in self._conn.execute(
            "SELECT source_id, target_id, relation, confidence, "
            "weight, layer, created_at FROM edges"
        ).fetchall():
            edge = self._row_to_edge(row)
            links.append({
                "source": edge.source_id,
                "target": edge.target_id,
                "relation": edge.relation,
                "confidence": edge.confidence,
                "weight": edge.weight,
            })

        return {
            "directed": False,
            "multigraph": False,
            "graph": {},
            "nodes": nodes,
            "links": links,
        }

    # ------------------------------------------------------------------
    # 全量同步
    # ------------------------------------------------------------------

    def sync_from_files(self, memory_dir: Path, skills_dir: Path,
                        graph_json_path: Optional[Path] = None) -> dict:
        """全量同步：从文件系统重建图谱

        Returns:
            {"memories": n, "skills": n, "structures": n}
        """
        result: dict = {}

        # 清空现有数据
        self._conn.execute("DELETE FROM edges")
        self._conn.execute("DELETE FROM nodes")
        self._conn.commit()

        result["memories"] = self.import_memories(memory_dir)
        result["skills"] = self.import_skills(skills_dir)
        result["structures"] = (
            self.import_structure(graph_json_path) if graph_json_path else 0
        )
        return result

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
