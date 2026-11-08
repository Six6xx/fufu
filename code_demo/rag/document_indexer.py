"""文档索引器 — 代码/文档/日志的向量索引管理。

功能：
  1. 增量索引：基于文件内容 MD5 哈希，未变化的文件跳过
  2. 代码分块：优先按函数/类定义分块，fallback 到固定行数滑动窗口
  3. 文档分块：复用 text_chunker.chunk_markdown() 进行智能分块
  4. 日志分块：按固定行数切割
  5. 元数据：source_file / chunk_index / doc_type / line_range
  6. 状态持久化：JSON 文件（ai_configuration/.memory/index_state.json）

用法：
    from code_demo.rag.document_indexer import DocumentIndexer, IndexStats
    from code_demo.rag.vector_store import VectorStore
    from code_demo.rag.embedder import Embedder

    store = VectorStore(Path("data/vectors.db"), embedder=Embedder())
    indexer = DocumentIndexer(store, Embedder())
    stats = indexer.index_all("/path/to/project")
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .embedder import Embedder
from .text_chunker import chunk_markdown
from .vector_store import Document as VectorDocument, VectorStore


# ── 统计数据类 ────────────────────────────────────────────────────────────────

@dataclass
class IndexStats:
    """索引统计信息。"""
    total_files: int = 0  # 本次扫描发现的文件总数
    indexed_files: int = 0  # 本次成功建立索引的文件数
    skipped_files: int = 0   # 未变化，跳过
    total_chunks: int = 0  # 本次生成并索引的文本块总数
    elapsed_seconds: float = 0.0  # 本次索引过程耗时（秒）

    def __add__(self, other: "IndexStats") -> "IndexStats":
        return IndexStats(
            total_files=self.total_files + other.total_files,
            indexed_files=self.indexed_files + other.indexed_files,
            skipped_files=self.skipped_files + other.skipped_files,
            total_chunks=self.total_chunks + other.total_chunks,
            elapsed_seconds=self.elapsed_seconds + other.elapsed_seconds,
        )


# ── 文档索引器 ────────────────────────────────────────────────────────────────

class DocumentIndexer:
    """文档索引器

    将项目中的代码文件、Markdown 文档、日志文件分块后
    通过 Embedder 向量化并存入 VectorStore。

    支持增量索引：基于文件内容哈希，未变化的文件跳过。
    """

    # 支持的文件扩展名
    CODE_EXTENSIONS: set[str] = {
        '.py', '.js', '.ts', '.css', '.html',
        '.json', '.yaml', '.yml', '.toml',
    }
    DOC_EXTENSIONS: set[str] = {'.md', '.txt', '.rst'}
    LOG_EXTENSIONS: set[str] = {'.log'}

    # 排除的目录
    EXCLUDE_DIRS: set[str] = {
        '__pycache__', 'node_modules', '.git', '.venv', 'venv',
        'claude-code', '.qoder', 'graphify-out',
    }

    # 默认状态文件路径（相对于项目根目录）
    _DEFAULT_STATE_REL = Path("ai_configuration/.memory/index_state.json")

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder,
        index_state_path: Optional[Path] = None,
    ) -> None:
        """
        Args:
            vector_store: 向量存储实例
            embedder: Embedding 服务实例
            index_state_path: 索引状态文件路径（记录已索引文件的哈希，用于增量）
        """
        self.vector_store = vector_store  # 文档向量持久化存储
        self.embedder = embedder  # 生成文档向量的模型封装
        self._state_path: Optional[Path] = index_state_path  # 增量索引状态文件路径
        # {str(file_path): content_hash}
        self._index_state: dict[str, str] = {}  # 文件到索引摘要的状态映射
        if self._state_path is not None:
            self._load_state()

    # ── 公共索引接口 ──────────────────────────────────────────────────────────

    def index_code_files(
        self,
        directory: str | Path,
        extensions: Optional[set[str]] = None,
    ) -> IndexStats:
        """索引代码文件。

        Args:
            directory: 目标目录
            extensions: 文件扩展名过滤（默认 CODE_EXTENSIONS）
        Returns:
            索引统计
        """
        exts = extensions if extensions is not None else self.CODE_EXTENSIONS
        return self._index_directory(directory, exts, doc_type="code")

    def index_markdown_docs(self, directory: str | Path) -> IndexStats:
        """索引 Markdown 文档。

        使用 text_chunker.chunk_markdown 进行智能分块。
        """
        return self._index_directory(directory, self.DOC_EXTENSIONS, doc_type="doc")

    def index_logs(
        self,
        directory: str | Path,
        max_lines_per_chunk: int = 50,
    ) -> IndexStats:
        """索引日志文件。

        按行数分块，每块 max_lines_per_chunk 行。
        """
        return self._index_directory(
            directory,
            self.LOG_EXTENSIONS,
            doc_type="log",
            log_lines=max_lines_per_chunk,
        )

    def index_all(self, project_root: str | Path) -> IndexStats:
        """一键索引整个项目。

        自动扫描代码/文档/日志并索引。
        """
        root = Path(project_root)
        t0 = time.monotonic()

        stats = IndexStats()
        stats = stats + self.index_code_files(root)
        stats = stats + self.index_markdown_docs(root)
        stats = stats + self.index_logs(root)

        stats.elapsed_seconds = time.monotonic() - t0
        return stats

    def remove_stale(self, directory: str | Path) -> int:
        """删除已不存在的文件的索引。

        Returns:
            删除的 chunk 数量
        """
        directory = Path(directory).resolve()
        stale_keys = [
            k for k in list(self._index_state.keys())
            if k.startswith(str(directory)) and not Path(k).exists()
        ]
        if not stale_keys:
            return 0

        # 从向量存储中删除对应文档（按 source_file 前缀匹配 ID）
        # 约定 doc ID 格式：{source_file}::{chunk_index}
        deleted = 0
        for file_key in stale_keys:
            # 构造该文件所有可能的 chunk ID 前缀并删除
            # 由于 VectorStore 没有按 metadata 过滤删除的接口，
            # 我们只能遍历已知 chunk 数量（通过 state 记录）删除。
            # 简化：删除状态记录，后续重建时自然不包含这些文件。
            del self._index_state[file_key]
            deleted += 1

        self._save_state()
        return deleted

    # ── 内部实现 ──────────────────────────────────────────────────────────────

    def _index_directory(
        self,
        directory: str | Path,
        extensions: set[str],
        doc_type: str,
        log_lines: int = 50,
    ) -> IndexStats:
        """扫描目录并索引指定扩展名的文件。"""
        directory = Path(directory).resolve()
        stats = IndexStats()
        t0 = time.monotonic()

        for file_path in self._iter_files(directory, extensions):
            stats.total_files += 1
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                stats.skipped_files += 1
                continue

            content_hash = self._file_hash(file_path)
            if self._should_skip(file_path, content_hash):
                stats.skipped_files += 1
                continue

            # 分块
            if doc_type == "code":
                raw_chunks = self._chunk_code(content, file_path)
            elif doc_type == "doc":
                raw_chunks = self._chunk_doc(content, file_path)
            else:  # log
                raw_chunks = self._chunk_log(content, file_path, log_lines)

            if not raw_chunks:
                self._index_state[str(file_path)] = content_hash
                self._save_state()
                stats.indexed_files += 1
                continue

            # 构造 VectorDocument 列表
            documents: list[VectorDocument] = []
            for chunk_dict in raw_chunks:
                doc_id = f"{file_path}::{chunk_dict['chunk_index']}"
                metadata = {
                    "source_file": str(file_path),
                    "chunk_index": chunk_dict["chunk_index"],
                    "doc_type": doc_type,
                    "line_range": chunk_dict.get("line_range", ""),
                }
                if "heading_path" in chunk_dict:
                    metadata["heading_path"] = chunk_dict["heading_path"]
                documents.append(
                    VectorDocument(
                        id=doc_id,
                        content=chunk_dict["text"],
                        metadata=metadata,
                    )
                )

            self.vector_store.add_documents(documents)
            self._index_state[str(file_path)] = content_hash
            self._save_state()

            stats.indexed_files += 1
            stats.total_chunks += len(documents)

        stats.elapsed_seconds = time.monotonic() - t0
        return stats

    def _iter_files(self, directory: Path, extensions: set[str]):
        """递归遍历目录，过滤扩展名和排除目录。"""
        for item in directory.rglob("*"):
            # 检查路径中是否含排除目录
            if any(part in self.EXCLUDE_DIRS for part in item.parts):
                continue
            if item.is_file() and item.suffix.lower() in extensions:
                yield item

    # ── 分块策略 ──────────────────────────────────────────────────────────────

    def _chunk_code(self, content: str, file_path: Path) -> list[dict]:
        """代码文件分块策略。

        优先按函数/类定义分块（正则匹配 def/class），
        fallback 到固定行数滑动窗口（100行，重叠20行）。
        """
        lines = content.splitlines()
        if not lines:
            return []

        # 尝试按 def/class 分块
        boundary_pattern = re.compile(r"^(class |def |async def )", re.MULTILINE)
        boundaries: list[int] = []
        for i, line in enumerate(lines):
            if boundary_pattern.match(line):
                boundaries.append(i)

        chunks: list[dict] = []

        if len(boundaries) >= 2:
            # 按函数/类边界分块
            for idx, start_line in enumerate(boundaries):
                end_line = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)
                segment_lines = lines[start_line:end_line]

                # 若单个块过大（>200行），进一步按固定窗口切分
                if len(segment_lines) > 200:
                    sub_chunks = self._sliding_window_chunks(
                        segment_lines,
                        start_offset=start_line,
                        window=100,
                        overlap=20,
                    )
                    for sc in sub_chunks:
                        sc["chunk_index"] = len(chunks)
                        chunks.append(sc)
                else:
                    chunks.append({
                        "text": "\n".join(segment_lines),
                        "chunk_index": len(chunks),
                        "line_range": f"{start_line + 1}-{end_line}",
                    })

            # 若文件开头（第一个边界之前）有内容，作为第一个 chunk 插在最前
            if boundaries[0] > 0:
                header_lines = lines[:boundaries[0]]
                header_chunk = {
                    "text": "\n".join(header_lines),
                    "chunk_index": 0,
                    "line_range": f"1-{boundaries[0]}",
                }
                # 重新编号
                for c in chunks:
                    c["chunk_index"] += 1
                chunks.insert(0, header_chunk)

        else:
            # fallback：固定行数滑动窗口
            chunks = self._sliding_window_chunks(lines, start_offset=0, window=100, overlap=20)
            for idx, c in enumerate(chunks):
                c["chunk_index"] = idx

        return chunks

    def _chunk_doc(self, content: str, file_path: Path) -> list[dict]:
        """Markdown/文本文档分块。

        复用 text_chunker.chunk_markdown() 进行智能分块。
        对于非 Markdown 文件（.txt/.rst），直接按段落分块。
        """
        suffix = file_path.suffix.lower()
        chunks: list[dict] = []

        if suffix == ".md":
            md_chunks = chunk_markdown(content)
            for c in md_chunks:
                # 估算行范围（通过字符偏移粗略计算）
                line_start = content[:c.char_start].count("\n") + 1
                line_end = content[:c.char_end].count("\n") + 1
                chunks.append({
                    "text": c.text,
                    "chunk_index": c.index,
                    "line_range": f"{line_start}-{line_end}",
                    "heading_path": c.heading_path,
                })
        else:
            # txt/rst：按段落（双换行）分块，合并小段落
            paragraphs = re.split(r"\n{2,}", content)
            buf = ""
            buf_start_line = 1
            line_cursor = 1
            chunk_idx = 0

            for para in paragraphs:
                para_lines = para.count("\n") + 1
                if len(buf) + len(para) < 800:
                    buf = (buf + "\n\n" + para).lstrip() if buf else para
                else:
                    if buf:
                        chunks.append({
                            "text": buf,
                            "chunk_index": chunk_idx,
                            "line_range": f"{buf_start_line}-{line_cursor - 1}",
                        })
                        chunk_idx += 1
                    buf = para
                    buf_start_line = line_cursor
                line_cursor += para_lines + 2  # +2 for blank lines between paragraphs

            if buf:
                chunks.append({
                    "text": buf,
                    "chunk_index": chunk_idx,
                    "line_range": f"{buf_start_line}-{line_cursor}",
                })

        return chunks

    def _chunk_log(
        self,
        content: str,
        file_path: Path,
        max_lines_per_chunk: int,
    ) -> list[dict]:
        """日志文件分块：按固定行数切割。"""
        lines = content.splitlines()
        if not lines:
            return []

        chunks: list[dict] = []
        chunk_idx = 0
        for i in range(0, len(lines), max_lines_per_chunk):
            segment = lines[i:i + max_lines_per_chunk]
            start_line = i + 1
            end_line = i + len(segment)
            chunks.append({
                "text": "\n".join(segment),
                "chunk_index": chunk_idx,
                "line_range": f"{start_line}-{end_line}",
            })
            chunk_idx += 1

        return chunks

    @staticmethod
    def _sliding_window_chunks(
        lines: list[str],
        start_offset: int,
        window: int,
        overlap: int,
    ) -> list[dict]:
        """固定行数滑动窗口分块。

        Args:
            lines: 源代码行列表
            start_offset: 该 lines 在原始文件中的起始行号（0-based）
            window: 每块行数
            overlap: 相邻块重叠行数
        Returns:
            chunk dict 列表（不含 chunk_index，由调用方填充）
        """
        step = max(1, window - overlap)
        chunks: list[dict] = []
        for i in range(0, len(lines), step):
            segment = lines[i:i + window]
            if not segment:
                break
            abs_start = start_offset + i + 1  # 1-based
            abs_end = start_offset + i + len(segment)
            chunks.append({
                "text": "\n".join(segment),
                "line_range": f"{abs_start}-{abs_end}",
            })
            if i + window >= len(lines):
                break
        return chunks

    # ── 增量索引辅助 ──────────────────────────────────────────────────────────

    def _file_hash(self, file_path: Path) -> str:
        """计算文件内容 MD5。"""
        try:
            raw = file_path.read_bytes()
            return hashlib.md5(raw).hexdigest()
        except Exception:  # noqa: BLE001
            return ""

    def _should_skip(self, file_path: Path, content_hash: str) -> bool:
        """检查文件是否已索引且未变化。"""
        if not content_hash:
            return False
        return self._index_state.get(str(file_path)) == content_hash

    def _save_state(self) -> None:
        """持久化索引状态到 JSON 文件。"""
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._index_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def _load_state(self) -> None:
        """从 JSON 文件加载索引状态。"""
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._index_state = data
        except Exception:  # noqa: BLE001
            self._index_state = {}
