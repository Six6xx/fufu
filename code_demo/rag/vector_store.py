"""轻量级向量存储 — SQLite + numpy 余弦相似度。

功能：
  1. 文档存储（SQLite，向量以 BLOB 存储）
  2. 向量检索（暴力余弦相似度，适合 <10K 文档）
  3. 自动向量化（传入 Embedder 时，add_documents 自动计算缺失的 vector）
  4. 元数据过滤（按 doc_type / source_file 等字段过滤）
  5. WAL 模式（并发读不阻塞写）

表结构：
    documents(id TEXT PK, content TEXT, metadata_json TEXT, vector BLOB, created_at REAL)

用法：
    from pathlib import Path
    from code_demo.rag.vector_store import VectorStore, Document

    store = VectorStore(Path("data/vectors.db"), embedder=embedder)
    store.add_documents([Document(id="1", content="...", metadata={})])
    results = store.search("查询文本", top_k=5)
"""
from __future__ import annotations

import json
import sqlite3
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


# ── 数据类 ────────────────────────────────────────────────────────────────────

@dataclass
class Document:
    """待存储的文档。"""
    id: str  # 文档块的唯一标识
    content: str  # 文档全文内容
    metadata: dict = field(default_factory=dict)  # source_file, chunk_index, doc_type 等
    vector: Optional[list[float]] = None           # 外部传入或由 store 自动计算


@dataclass
class SearchResult:
    """检索结果。"""
    id: str  # 命中文档块的唯一标识
    content: str  # 命中文档块的文本内容
    score: float   # 余弦相似度 0~1
    metadata: dict = field(default_factory=dict)  # 命中文档的附加元数据


# ── VectorStore ───────────────────────────────────────────────────────────────

class VectorStore:
    """SQLite 向量存储。

    检索方式：暴力余弦相似度扫描（适合 <10K 文档规模）。
    大规模时可替换为 sqlite-vec 扩展或 ChromaDB。
    """

    _CREATE_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS documents (
            id           TEXT PRIMARY KEY,
            content      TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            vector       BLOB,
            created_at   REAL NOT NULL
        )
    """

    def __init__(self, db_path: Path, embedder=None) -> None:
        """
        Args:
            db_path: SQLite 数据库路径（父目录不存在时自动创建）
            embedder: Embedder 实例（可选，用于自动向量化）
        """
        self.db_path = Path(db_path)  # 向量存储 SQLite 数据库路径
        self.embedder = embedder  # 生成文本向量的组件
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()  # 当前 SQLite 数据库连接

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: list[Document]) -> int:
        """批量添加文档。

        如果 document.vector 为 None 且 embedder 已设置，自动批量计算向量。

        Args:
            documents: 文档列表
        Returns:
            成功添加/更新的数量（INSERT OR REPLACE）
        """
        if not documents:
            return 0

        # 自动向量化：收集缺失向量的文档，批量调用 embedder
        docs_need_embed = [d for d in documents if d.vector is None and self.embedder is not None]
        if docs_need_embed:
            texts = [d.content for d in docs_need_embed]
            try:
                vectors = self.embedder.embed_batch(texts)
                for doc, vec in zip(docs_need_embed, vectors):
                    doc.vector = vec
            except Exception as exc:  # noqa: BLE001
                warnings.warn(
                    f"[VectorStore] 自动向量化失败: {exc}，受影响文档将以 NULL 向量存储",
                    stacklevel=2,
                )

        now = time.time()
        rows = [
            (
                doc.id,
                doc.content,
                json.dumps(doc.metadata, ensure_ascii=False),
                self._vec_to_blob(doc.vector) if doc.vector is not None else None,
                now,
            )
            for doc in documents
        ]

        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO documents "
                "(id, content, metadata_json, vector, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )

        return len(rows)

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_metadata: Optional[dict] = None,
    ) -> list[SearchResult]:
        """向量检索。

        流程：query → embedder.embed_text → 余弦相似度扫描 → top_k

        Args:
            query: 查询文本
            top_k: 返回数量
            filter_metadata: 元数据过滤（如 {"doc_type": "code"}），键值须全部匹配
        Returns:
            按相似度降序排列的结果列表
        """
        if self.embedder is None:
            warnings.warn(
                "[VectorStore] search() 需要 embedder，但未设置，返回空结果。",
                stacklevel=2,
            )
            return []

        try:
            query_vec = np.array(
                self.embedder.embed_text(query), dtype=np.float32
            )
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"[VectorStore] 查询向量化失败: {exc}", stacklevel=2)
            return []

        # 加载所有有向量的文档
        cursor = self._conn.execute(
            "SELECT id, content, metadata_json, vector FROM documents WHERE vector IS NOT NULL"
        )
        rows = cursor.fetchall()

        if not rows:
            return []

        candidates: list[tuple[str, str, dict, float]] = []

        for row_id, content, meta_json, blob in rows:
            try:
                metadata = json.loads(meta_json)
            except Exception:  # noqa: BLE001
                metadata = {}

            # 元数据过滤
            if filter_metadata and not self._match_metadata(metadata, filter_metadata):
                continue

            try:
                doc_vec = self._blob_to_vec(blob)
            except Exception:  # noqa: BLE001
                continue

            score = self._cosine_similarity(query_vec, doc_vec)
            candidates.append((row_id, content, metadata, score))

        # 按相似度降序，取 top_k
        candidates.sort(key=lambda x: -x[3])

        return [
            SearchResult(id=row_id, content=content, score=score, metadata=metadata)
            for row_id, content, metadata, score in candidates[:top_k]
        ]

    def delete(self, doc_ids: list[str]) -> int:
        """批量删除文档。

        Args:
            doc_ids: 要删除的文档 ID 列表
        Returns:
            实际删除的数量
        """
        if not doc_ids:
            return 0

        placeholders = ",".join("?" * len(doc_ids))
        with self._conn:
            cursor = self._conn.execute(
                f"DELETE FROM documents WHERE id IN ({placeholders})", doc_ids
            )
        return cursor.rowcount

    def count(self) -> int:
        """返回文档总数。"""
        cursor = self._conn.execute("SELECT COUNT(*) FROM documents")
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def clear(self) -> None:
        """清空所有文档。"""
        with self._conn:
            self._conn.execute("DELETE FROM documents")

    def close(self) -> None:
        """关闭数据库连接。"""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """建立 SQLite 连接并初始化表结构。"""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # WAL 模式：并发读不阻塞写
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(self._CREATE_TABLE_SQL)
        conn.commit()
        return conn

    @staticmethod
    def _cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """计算余弦相似度。

        Args:
            vec_a: 向量 A（numpy array）
            vec_b: 向量 B（numpy array）
        Returns:
            余弦相似度，范围 [0, 1]；零向量返回 0.0
        """
        norm_a = float(np.linalg.norm(vec_a))
        norm_b = float(np.linalg.norm(vec_b))
        if norm_a < 1e-10 or norm_b < 1e-10:
            return 0.0
        return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))

    @staticmethod
    def _vec_to_blob(vector: list[float]) -> bytes:
        """将向量转换为 BLOB（float32 bytes）。"""
        return np.array(vector, dtype=np.float32).tobytes()

    @staticmethod
    def _blob_to_vec(blob: bytes) -> np.ndarray:
        """将 BLOB 还原为 float32 numpy 向量。"""
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def _match_metadata(metadata: dict, filter_dict: dict) -> bool:
        """判断元数据是否满足过滤条件（所有键值须全部匹配）。"""
        for key, value in filter_dict.items():
            if metadata.get(key) != value:
                return False
        return True
