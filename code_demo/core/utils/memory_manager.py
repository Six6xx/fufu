"""三层记忆统一管理器 — Phase 2 核心模块

融合 Markdown 持久化 + SQLite FTS5 全文检索 + 4 信号相关性评分 + 时间衰减，
为 Agent 提供统一的记忆读写接口。

架构：
  ThreeLayerMemory
  ├── 短期记忆：直接引用 Agent.history（外部管理，不在此类中）
  ├── 压缩层：复用 context_budget.py 的 ContextCompressor
  └── 长期记忆：
      ├── Markdown 后端 → 复用 memory.py 的 MemoryManager
      ├── SQLite FTS5  → 内嵌类 _FTS5Backend
      └── 相关性评分   → 复用 memory_relevance.py 的 4 信号模型
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .context_budget import ContextCompressor
from .dream import ConsolidationReport, DreamConsolidator
from .memory import MemoryManager
from .memory_relevance import build_memory_graph, retrieve_relevant_memories


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class RecallResult:
    """单条检索结果。"""

    name: str  # 召回记忆的名称
    content: str  # 召回记忆的正文
    score: float  # 记忆相关性评分
    source: str  # "fts5" | "markdown" | "profile"
    mem_type: str  # "user" | "feedback" | "project" | "reference" | "skill_learned" | "mistake"


# ── 中文分词辅助 ──────────────────────────────────────────────────────────────

def _segment_text(text: str) -> str:
    """使用 jieba 分词（如果可用），否则返回原文。

    FTS5 索引和查询都应使用相同分词策略，以保证匹配一致性。
    """
    try:
        import jieba  # type: ignore[import-untyped]

        words = list(jieba.cut(text))
        return " ".join(w for w in words if w.strip())
    except ImportError:
        return text


def _prepare_fts_query(query: str) -> str:
    """将查询文本转为合法的 FTS5 MATCH 表达式。

    - 若 jieba 可用：先分词，再用 OR 连接各词项
    - 否则：将 CJK 拆为单字，Latin 保留整词，OR 连接
    """
    try:
        import jieba  # type: ignore[import-untyped]

        words = list(jieba.cut(query))
        terms = [w.strip() for w in words if w.strip() and re.match(r"\w", w)]
    except ImportError:
        # 将 CJK 逐字拆分，Latin 保留整词
        terms = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", query)

    if not terms:
        return ""

    # OR 拓宽召回；BM25 排序会优先匹配更多词项的文档
    return " OR ".join(f'"{t}"' for t in terms[:10])


# ── FTS5 后端 ─────────────────────────────────────────────────────────────────

class _FTS5Backend:
    """SQLite FTS5 全文检索后端。

    表结构：
      - memories  (FTS5 虚拟表): name, description, content, mem_type, created_at, last_accessed
      - memory_meta (普通表):    name PK, mem_type, description, created_at, last_accessed, access_count

    中文支持：插入前对 description/content 做分词，查询前对 query 做分词。
    若 jieba 不可用，fallback 到 unicode61 逐字分词。
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path  # 记忆数据库文件路径
        self._conn: sqlite3.Connection | None = None  # 延迟创建的 SQLite 数据库连接
        self._init_db()

    # ── 初始化 ──────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """初始化数据库连接和表结构。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")

        self._conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(
                name,
                description,
                content,
                mem_type UNINDEXED,
                created_at UNINDEXED,
                last_accessed UNINDEXED,
                tokenize='unicode61'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_meta (
                name TEXT PRIMARY KEY,
                mem_type TEXT,
                description TEXT,
                created_at REAL,
                last_accessed REAL,
                access_count INTEGER DEFAULT 0
            )
            """
        )
        self._conn.commit()

    # ── 写入 ────────────────────────────────────────────────────────────────

    def upsert(self, name: str, content: str, mem_type: str, description: str) -> None:
        """插入或更新记忆到 FTS5 索引。

        对 content / description 先分词再写入 FTS5，
        原始 description 保存在 memory_meta 中。
        """
        now = time.time()

        # 分词后存入 FTS5（提升中文检索精度）
        content_seg = _segment_text(content)
        desc_seg = _segment_text(description)

        row = self._conn.execute(
            "SELECT access_count FROM memory_meta WHERE name = ?", (name,)
        ).fetchone()

        if row:
            access_count = row[0] + 1
            self._conn.execute("DELETE FROM memories WHERE name = ?", (name,))
            self._conn.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?)",
                (name, desc_seg, content_seg, mem_type, now, now),
            )
            self._conn.execute(
                "UPDATE memory_meta SET mem_type=?, description=?, last_accessed=?, access_count=? WHERE name=?",
                (mem_type, description, now, access_count, name),
            )
        else:
            self._conn.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?)",
                (name, desc_seg, content_seg, mem_type, now, now),
            )
            self._conn.execute(
                "INSERT INTO memory_meta VALUES (?, ?, ?, ?, ?, ?)",
                (name, mem_type, description, now, now, 0),
            )
        self._conn.commit()

    # ── 检索 ────────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> list[RecallResult]:
        """FTS5 全文检索。

        Returns:
            RecallResult 列表（content 留空，由调用方从 MemoryManager 填充），
            按 BM25 rank 升序（最相关在前）。
        """
        fts_query = _prepare_fts_query(query)
        if not fts_query:
            return []

        try:
            rows = self._conn.execute(
                """
                SELECT name, mem_type, rank
                FROM memories
                WHERE memories MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, top_k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        results: list[RecallResult] = []
        for name, mem_type, rank in rows:
            # BM25 rank 为负数，取反转正
            score = max(-rank, 0.1)
            results.append(
                RecallResult(
                    name=name, content="", score=score,
                    source="fts5", mem_type=mem_type,
                )
            )
        return results

    # ── 删除 ────────────────────────────────────────────────────────────────

    def delete(self, name: str) -> None:
        """删除记忆。"""
        self._conn.execute("DELETE FROM memories WHERE name = ?", (name,))
        self._conn.execute("DELETE FROM memory_meta WHERE name = ?", (name,))
        self._conn.commit()

    # ── 元数据 ──────────────────────────────────────────────────────────────

    def get_last_accessed(self, name: str) -> float | None:
        """获取记忆的最后访问时间戳。"""
        row = self._conn.execute(
            "SELECT last_accessed FROM memory_meta WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def count(self) -> int:
        """返回已索引的记忆条数。"""
        row = self._conn.execute("SELECT COUNT(*) FROM memory_meta").fetchone()
        return row[0] if row else 0

    # ── 同步 ────────────────────────────────────────────────────────────────

    def sync_from_files(self, memory_manager: MemoryManager) -> int:
        """从 Markdown 文件同步到 FTS5（冷启动 / 修复用）。

        对 MemoryManager 中的每条记忆执行 upsert，幂等操作。

        Returns:
            同步的记忆条数
        """
        count = 0
        for name, mem in memory_manager.memories.items():
            self.upsert(
                name=name,
                content=mem.get("content", ""),
                mem_type=mem.get("type", "project"),
                description=mem.get("description", ""),
            )
            count += 1
        return count

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None


# ── 三层记忆统一接口 ─────────────────────────────────────────────────────────

class ThreeLayerMemory:
    """三层记忆统一接口。

    融合 Markdown 持久化 + SQLite FTS5 + 4 信号相关性评分 + 时间衰减，
    为 Agent 提供统一的记忆读写入口。

    三层架构：
      - 短期记忆：Agent.history（外部管理，不在此类中）
      - 压缩层：ContextCompressor
      - 长期记忆：Markdown + SQLite FTS5 双后端 + 4 信号评分
    """

    HALF_LIFE_DAYS = 30  # 30 天半衰期

    def __init__(self, config_dir: Path, memory_dir: Optional[Path] = None):
        """
        Args:
            config_dir: ai_configuration 目录路径
            memory_dir: .memory 目录路径（默认 config_dir / '.memory'）
        """
        self._config_dir = config_dir  # 记忆系统配置目录
        self._memory_dir = memory_dir or (config_dir / ".memory")  # Markdown 记忆文件目录

        # Layer 2 — 压缩层
        self._compressor = ContextCompressor()  # 压缩和裁剪上下文的组件

        # Layer 3 — 长期记忆：双后端
        self._mm = MemoryManager(memory_dir=self._memory_dir)  # 基础 Markdown 记忆管理器
        self._mm.load_all()

        db_path = self._memory_dir / "memory.db"
        self._fts5 = _FTS5Backend(db_path)  # SQLite FTS5 全文检索后端

        # 会话整合器
        self._dream = DreamConsolidator(memory_dir=self._memory_dir)  # 记忆整合组件

        # 记忆图谱（惰性构建）
        self._graph = None  # 记忆之间的关联图

        # FTS5 同步标记
        self._synced = False  # 记忆索引是否已与文件同步

    # ── 核心检索 ────────────────────────────────────────────────────────────

    def recall(
        self,
        query: str,
        top_k: int = 3,
        context_sources: list[str] | None = None,
        context_memory_ids: list[str] | None = None,
    ) -> str:
        """统一检索接口 — 核心方法。

        流程：
          1. FTS5 全文检索 top_k * 2 候选
          2. Markdown keyword 检索 top_k * 2 候选
          3. 去重合并
          4. 用 memory_relevance 的 4 信号加权评分
          5. 应用 30 天半衰期时间衰减
          6. 返回 top_k 最终结果，格式化为 prompt 注入文本

        Args:
            query: 检索查询文本
            top_k: 返回的最大结果数
            context_sources: 当前上下文的来源标识列表
            context_memory_ids: 当前上下文已引用的记忆 ID 列表

        Returns:
            格式化的记忆片段文本，可直接注入 system prompt
        """
        self.ensure_synced()

        expand = top_k * 2

        # Step 1: FTS5 全文检索
        fts5_results = self._fts5.search(query, top_k=expand)
        # 填充 content（FTS5 只返回 name/score，不存原文）
        for r in fts5_results:
            mem = self._mm.memories.get(r.name)
            if mem:
                r.content = mem.get("content", "")

        # Step 2: Markdown 关键词检索
        md_results = self._keyword_search(query, top_k=expand)

        # Step 3: 去重合并（保留较高分）
        seen: dict[str, RecallResult] = {}
        for r in fts5_results + md_results:
            if r.name not in seen or r.score > seen[r.name].score:
                seen[r.name] = r
        candidates = list(seen.values())

        if not candidates:
            return ""

        # Step 4: 4 信号相关性评分
        self._rebuild_graph()
        if self._graph and self._graph.nodes:
            relevance_map = self._build_relevance_map(query, context_sources, context_memory_ids)
            for r in candidates:
                rel_score = relevance_map.get(r.name, 0.0)
                # 加权融合：FTS5/keyword 60% + relevance 40%
                r.score = r.score * 0.6 + rel_score * 0.4

        # Step 5: 时间衰减
        candidates = self._apply_time_decay(candidates)

        # Step 6: 排序取 top_k 并格式化
        candidates.sort(key=lambda x: -x.score)
        return self._format_recall_results(candidates[:top_k])

    # ── 会话整合 ────────────────────────────────────────────────────────────

    def consolidate_session(self, messages: list) -> ConsolidationReport:
        """会话结束整合 — 调用 DreamConsolidator。

        整合完成后自动同步新/变更记忆到 FTS5。

        Args:
            messages: 对话历史（OpenAI 格式 messages 列表）

        Returns:
            ConsolidationReport 执行报告
        """
        report = self._dream.consolidate_session(messages, self._mm)
        # 同步变更到 FTS5
        self._fts5.sync_from_files(self._mm)
        self._graph = None  # 失效图谱缓存
        return report

    # ── 写入 ────────────────────────────────────────────────────────────────

    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        """保存到双后端（Markdown + SQLite FTS5）。

        Args:
            name: 记忆名称
            description: 简短描述
            mem_type: 记忆类型 (user/feedback/project/reference/skill_learned/mistake)
            content: 记忆内容

        Returns:
            状态消息
        """
        result = self._mm.save_memory(name, description, mem_type, content)
        if not result.startswith("Error"):
            self._fts5.upsert(name, content, mem_type, description)
            self._graph = None  # 失效图谱缓存
        return result

    def delete_memory(self, name: str) -> str:
        """删除记忆（两个后端同步）。

        Args:
            name: 记忆名称

        Returns:
            状态消息
        """
        result = self._mm.delete_memory(name)
        if not result.startswith("Error"):
            self._fts5.delete(name)
            self._graph = None  # 失效图谱缓存
        return result

    # ── 兼容旧接口 ──────────────────────────────────────────────────────────

    def get_memory_prompt(self) -> str:
        """构建完整记忆 prompt（兼容旧接口）。

        输出格式与 MemoryManager.load_memory_prompt() 一致，
        方便渐进迁移。

        Returns:
            格式化的完整记忆文本
        """
        return self._mm.load_memory_prompt()

    # ── 时间衰减 ────────────────────────────────────────────────────────────

    def _apply_time_decay(self, results: list[RecallResult]) -> list[RecallResult]:
        """30 天半衰期时间衰减。

        公式：score *= 2^(-days_since_last_access / HALF_LIFE_DAYS)
        无访问记录的新记忆不衰减。
        """
        now = time.time()
        for r in results:
            last_accessed = self._fts5.get_last_accessed(r.name)
            if last_accessed is not None:
                days_since = (now - last_accessed) / 86400
                decay = math.pow(2, -days_since / self.HALF_LIFE_DAYS)
                r.score *= decay
        return results

    # ── 同步 ────────────────────────────────────────────────────────────────

    def ensure_synced(self) -> None:
        """确保 FTS5 索引与 Markdown 文件同步。

        首次调用时执行全量同步（冷启动），后续由 save/delete 保持一致性。
        """
        if not self._synced:
            self._fts5.sync_from_files(self._mm)
            self._synced = True

    # ── 关键词检索（Markdown 后端）───────────────────────────────────────────

    def _keyword_search(self, query: str, top_k: int = 5) -> list[RecallResult]:
        """基于 MemoryManager 的关键词检索。

        在 name / description / content 中做关键词匹配评分。
        """
        query_lower = query.lower()
        query_terms = set(query_lower.split())

        scored: list[RecallResult] = []
        for name, mem in self._mm.memories.items():
            score = 0.0

            # 名称精确匹配
            if query_lower in name.lower():
                score += 5.0

            # 描述关键词匹配
            desc_lower = mem.get("description", "").lower()
            for term in query_terms:
                if term in desc_lower:
                    score += 2.0

            # 内容关键词匹配
            content_lower = mem.get("content", "").lower()
            for term in query_terms:
                if term in content_lower:
                    score += 1.0

            if score > 0:
                scored.append(
                    RecallResult(
                        name=name,
                        content=mem.get("content", ""),
                        score=score,
                        source="markdown",
                        mem_type=mem.get("type", "project"),
                    )
                )

        scored.sort(key=lambda x: -x.score)
        return scored[:top_k]

    # ── 图谱与相关性 ────────────────────────────────────────────────────────

    def _rebuild_graph(self) -> None:
        """重建记忆图谱（惰性）。"""
        if self._graph is None:
            self._graph = build_memory_graph(self._memory_dir)

    def _build_relevance_map(
        self,
        query: str,
        context_sources: list[str] | None,
        context_memory_ids: list[str] | None,
    ) -> dict[str, float]:
        """构建 记忆名称 → 相关性评分 的映射。

        使用 memory_relevance 的 4 信号模型。
        需要在 frontmatter name 与文件名 stem 之间双向映射。
        """
        # 构建 stem → name 映射
        stem_to_name: dict[str, str] = {}
        name_to_stem: dict[str, str] = {}
        for name, mem in self._mm.memories.items():
            file_name = mem.get("file", "")
            if file_name:
                stem = Path(file_name).stem
                stem_to_name[stem] = name
                name_to_stem[name] = stem

        # 转换 context_memory_ids（可能是 frontmatter name）为 stem
        ctx_ids: set[str] | None = None
        if context_memory_ids:
            ctx_ids = {name_to_stem.get(nid, nid) for nid in context_memory_ids}

        ctx_sources: set[str] | None = set(context_sources) if context_sources else None

        # 从图谱检索相关记忆
        relevant = retrieve_relevant_memories(
            query, self._graph,
            context_sources=ctx_sources,
            context_memory_ids=ctx_ids,
            top_k=len(self._graph.nodes),
        )

        # 将 stem 映射回 name
        relevance_map: dict[str, float] = {}
        for node, score in relevant:
            name = stem_to_name.get(node.id, node.id)
            relevance_map[name] = score
        return relevance_map

    # ── 格式化 ──────────────────────────────────────────────────────────────

    def _format_recall_results(self, results: list[RecallResult]) -> str:
        """将检索结果格式化为可注入 prompt 的文本。

        格式与 MemoryManager.load_memory_prompt() 风格一致，
        但增加了相关度信息，适合动态注入。
        """
        if not results:
            return ""

        sections = ["# Relevant Memories", ""]
        for r in results:
            # 从 MemoryManager 获取 description
            mem = self._mm.memories.get(r.name, {})
            desc = mem.get("description", "")

            header = f"### {r.name}"
            if desc:
                header += f": {desc}"
            sections.append(header)

            # 压缩长内容
            content = self._compressor.compress(r.content, max_tokens=2000)
            if content.strip():
                sections.append(content.strip())
            sections.append("")

        return "\n".join(sections)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """关闭底层资源（SQLite 连接等）。"""
        self._fts5.close()

    def __enter__(self) -> "ThreeLayerMemory":
        return self

    def __exit__(self, *args) -> None:
        self.close()
