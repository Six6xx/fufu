"""Markdown 感知的文本分块器 — 用于 Embedding 管道。

移植自 llm_wiki/src/lib/text-chunker.ts，适配 code_demo 的 RAG 子系统。

设计约束：
  1. 每个 chunk 携带 heading_path 面包屑（## Intro > ### Usage）
  2. 分割优先级：标题→段落→换行→句子→空格→硬切
  3. 不拆分围栏代码块（``` ... ```）
  4. 不拆分 Markdown 表格（连续 | 开头的行）
  5. YAML frontmatter 在分块前被剥离
  6. 相邻 chunk 之间有 overlap
  7. 微小 chunk (< min_chars) 合并到邻居
  8. 纯函数，确定性：相同输入 → 相同输出

用法：
    from code_demo.rag.text_chunker import chunk_markdown, ChunkOptions

    chunks = chunk_markdown(content)
    for chunk in chunks:
        print(chunk.text, chunk.heading_path)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ── 配置 ──────────────────────────────────────────────────────────────────────

@dataclass
class ChunkOptions:
    """分块器选项。"""
    target_chars: int = 1000      # 每个 chunk 的目标字符数
    max_chars: int = 1500         # 硬上限（不可分割的原子块超过此值仍会发出）
    min_chars: int = 200          # 短于此值的 chunk 会合并到邻居
    overlap_chars: int = 200      # 相邻 chunk 之间的重叠字符数


DEFAULT_OPTIONS = ChunkOptions()


# ── 输出结构 ──────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """分块后的一个文本片段。"""
    index: int              # 0-based 顺序索引
    text: str               # 可见内容（无 frontmatter，无标题前缀）
    heading_path: str       # 标题面包屑，如 "## Techniques > ### Flash Attention"
    char_start: int         # 原始输入中的字符偏移（frontmatter 剥离后）
    char_end: int           # 字符偏移（独占）
    oversized: bool         # 是否因为不可分割而超过 max_chars


# ── 公共 API ──────────────────────────────────────────────────────────────────

def chunk_markdown(
    content: str,
    options: ChunkOptions | None = None,
) -> list[Chunk]:
    """将 Markdown 文档分块为适合 embedding 的片段。

    移植自 text-chunker.ts chunkMarkdown()。

    Args:
        content: Markdown 文本内容
        options: 分块选项，默认使用 DEFAULT_OPTIONS

    Returns:
        Chunk 列表，按文档顺序排列
    """
    opts = options or DEFAULT_OPTIONS

    # 防御性配置修正
    if opts.max_chars < opts.target_chars:
        opts.max_chars = opts.target_chars
    if opts.overlap_chars >= opts.target_chars:
        opts.overlap_chars = opts.target_chars // 2

    body, body_offset = strip_frontmatter(content)
    if not body.strip():
        return []

    sections = _split_into_sections(body, body_offset)

    chunks: list[Chunk] = []
    running_index = 0
    for section in sections:
        section_chunks = _chunk_section(section, opts)
        for c in section_chunks:
            chunks.append(Chunk(
                index=running_index,
                text=c["text"],
                heading_path=c["heading_path"],
                char_start=c["char_start"],
                char_end=c["char_end"],
                oversized=c["oversized"],
            ))
            running_index += 1

    return chunks


# ── Frontmatter 处理 ──────────────────────────────────────────────────────────

def strip_frontmatter(content: str) -> tuple[str, int]:
    """剥离 YAML frontmatter，返回 (body, body_offset)。

    移植自 text-chunker.ts stripFrontmatter()。
    """
    if not (content.startswith("---\n") or content.startswith("---\r\n")):
        return content, 0

    rest = content[4:]  # 跳过第一个 ---\n 或 ---\r\n
    close_match = re.search(r"(^|\n)---\s*(\n|$)", rest)
    if not close_match:
        return content, 0

    after_match = re.match(r"(\n)?---\s*\n?", rest[close_match.start():])
    if not after_match:
        return content, 0

    body_offset = 4 + close_match.start() + len(after_match.group(0))
    return content[body_offset:], body_offset


# ── 段落分割 ──────────────────────────────────────────────────────────────────

@dataclass
class _Section:
    text: str  # 当前章节的正文文本
    body_start: int  # 正文在原文中的起始偏移
    heading_path: str  # 从文档根部到当前章节的标题路径


def _split_into_sections(body: str, body_offset: int) -> list[_Section]:
    """按 Markdown 标题将文档分割为段落。

    移植自 text-chunker.ts splitIntoSections()。
    """
    lines = body.split("\n")
    sections: list[_Section] = []
    headings: dict[int, str] = {}  # level → title

    current_lines: list[str] = []
    current_start = body_offset
    current_heading_path = ""
    in_fence = False
    fence_marker = ""
    char_cursor = body_offset

    def flush():
        text = "\n".join(current_lines)
        if text.strip():
            sections.append(_Section(
                text=text,
                body_start=current_start,
                heading_path=current_heading_path,
            ))

    for i, line in enumerate(lines):
        line_len = len(line) + (1 if i < len(lines) - 1 else 0)

        # 围栏代码块追踪
        fence_match = re.match(r"^(`{3,}|~{3,})", line)
        if fence_match:
            if not in_fence:
                in_fence = True
                fence_marker = fence_match.group(1)
            elif line.startswith(fence_marker) and line.strip() == fence_marker:
                in_fence = False
            current_lines.append(line)
            char_cursor += line_len
            continue

        # 标题检测（仅在围栏外）
        h_match = None if in_fence else re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if h_match:
            flush()
            level = len(h_match.group(1))
            title = h_match.group(2).strip()
            headings[level] = title
            # 清除更深层级
            for lvl in range(level + 1, 7):
                headings.pop(lvl, None)

            path_parts = []
            for lvl in range(1, 7):
                if lvl in headings:
                    path_parts.append(f"{'#' * lvl} {headings[lvl]}")

            current_lines = [line]
            current_start = char_cursor
            current_heading_path = " > ".join(path_parts)
            char_cursor += line_len
            continue

        current_lines.append(line)
        char_cursor += line_len

    flush()
    return sections


# ── 段落 → 分块 ──────────────────────────────────────────────────────────────

def _chunk_section(section: _Section, opts: ChunkOptions) -> list[dict]:
    """将单个段落分块。"""
    text, body_start, heading_path = section.text, section.body_start, section.heading_path

    if len(text) <= opts.target_chars:
        return [{
            "text": text,
            "heading_path": heading_path,
            "char_start": body_start,
            "char_end": body_start + len(text),
            "oversized": False,
        }]

    atoms = _tokenize_atoms(text)
    pieces = _split_atoms_to_pieces(atoms, opts)
    sized = _size_pieces(pieces, opts)
    merged = _merge_small(sized, opts)
    with_overlap = _apply_overlap(merged, opts)

    out = []
    for piece in with_overlap:
        out.append({
            "text": piece["text"],
            "heading_path": heading_path,
            "char_start": body_start + piece["offset"],
            "char_end": body_start + piece["offset"] + len(piece["text"]),
            "oversized": len(piece["text"]) > opts.max_chars,
        })
    return out


# ── 原子分割 ──────────────────────────────────────────────────────────────────

@dataclass
class _Atom:
    text: str  # 当前不可分割单元或段落的文本
    offset: int  # 单元在原文中的起始偏移
    indivisible: bool  # 代码块和表格不可分割
    kind: str          # code / table / paragraph / blank


def _tokenize_atoms(text: str) -> list[_Atom]:
    """将文本分割为原子单元（代码块/表格/段落）。

    移植自 text-chunker.ts tokenizeAtoms()。
    """
    atoms: list[_Atom] = []
    lines = text.split("\n")
    cursor = 0
    i = 0

    while i < len(lines):
        line = lines[i]

        # 围栏代码块
        fence_match = re.match(r"^(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            start = cursor
            body_lines = [line]
            j = i + 1
            cursor += len(line) + 1
            while j < len(lines):
                body_lines.append(lines[j])
                cursor += len(lines[j]) + 1
                if lines[j].startswith(marker) and lines[j].strip() == marker:
                    j += 1
                    break
                j += 1
            atoms.append(_Atom(
                text="\n".join(body_lines),
                offset=start,
                indivisible=True,
                kind="code",
            ))
            i = j
            continue

        # 表格：连续 | 开头的行（至少 2 行）
        if line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            if j - i >= 2:
                start = cursor
                body_lines = lines[i:j]
                content = "\n".join(body_lines)
                cursor += len(content) + (1 if j < len(lines) else 0)
                atoms.append(_Atom(
                    text=content,
                    offset=start,
                    indivisible=True,
                    kind="table",
                ))
                i = j
                continue

        # 空行
        if not line.strip():
            atoms.append(_Atom(text="", offset=cursor, indivisible=False, kind="blank"))
            cursor += len(line) + 1
            i += 1
            continue

        # 普通段落
        start = cursor
        body_lines = []
        while (
            i < len(lines)
            and lines[i].strip()
            and not re.match(r"^(`{3,}|~{3,})", lines[i])
        ):
            body_lines.append(lines[i])
            cursor += len(lines[i]) + 1
            i += 1
        atoms.append(_Atom(
            text="\n".join(body_lines),
            offset=start,
            indivisible=False,
            kind="paragraph",
        ))

    return [a for a in atoms if a.kind != "blank" or a.text]


# ── 递归分割 ──────────────────────────────────────────────────────────────────

def _split_atoms_to_pieces(atoms: list[_Atom], opts: ChunkOptions) -> list[dict]:
    """将原子单元转换为分块片段。"""
    pieces = []
    for atom in atoms:
        if atom.indivisible:
            pieces.append({"text": atom.text, "offset": atom.offset})
            continue
        if atom.kind == "blank":
            continue
        if len(atom.text) <= opts.target_chars:
            pieces.append({"text": atom.text, "offset": atom.offset})
            continue
        pieces.extend(_recursive_split(atom.text, atom.offset, opts.target_chars))
    return pieces


def _split_keeping_sep(text: str, sep: re.Pattern) -> list[str]:
    """按分隔符分割文本，保留分隔符在前一个片段中。"""
    out = []
    last = 0
    for m in sep.finditer(text):
        end = m.start() + len(m.group(0))
        out.append(text[last:end])
        last = end
    if last < len(text):
        out.append(text[last:])
    return [s for s in out if s]


def _recursive_split(text: str, base_offset: int, target_chars: int) -> list[dict]:
    """递归分割：先按段落，再按行，再按句子，最后硬切。

    移植自 text-chunker.ts recursiveSplit()。
    """
    # 先按双换行（段落）分割
    para_pieces = _split_keeping_sep(text, re.compile(r"(\n{2,})"))
    out = []
    cursor = base_offset

    for chunk in para_pieces:
        if not chunk:
            continue
        if len(chunk) <= target_chars:
            out.append({"text": chunk, "offset": cursor})
            cursor += len(chunk)
            continue

        # 段落太大，尝试更细的分割器
        splitters = [
            re.compile(r"(\n+)"),
            re.compile(r"([。！？!?；;]+\s*|(?:\.\s+))"),
            re.compile(r"(\s+)"),
        ]

        split_done = False
        for splitter in splitters:
            subs = _split_keeping_sep(chunk, splitter)
            if all(len(s) <= target_chars for s in subs) and len(subs) > 1:
                sub_cursor = cursor
                for s in subs:
                    if s:
                        out.append({"text": s, "offset": sub_cursor})
                        sub_cursor += len(s)
                cursor += len(chunk)
                split_done = True
                break

        if not split_done:
            # 硬切
            slice_cursor = cursor
            for i in range(0, len(chunk), target_chars):
                piece = chunk[i:i + target_chars]
                out.append({"text": piece, "offset": slice_cursor})
                slice_cursor += len(piece)
            cursor += len(chunk)

    return out


# ── 片段打包 ──────────────────────────────────────────────────────────────────

def _size_pieces(pieces: list[dict], opts: ChunkOptions) -> list[dict]:
    """贪心打包：累积片段直到超过 target_chars。"""
    out = []
    buf = ""
    buf_offset = None

    for p in pieces:
        if not p["text"]:
            continue
        # 单个片段超过 target_chars：flush 当前，单独发出
        if len(p["text"]) > opts.target_chars:
            if buf:
                out.append({"text": buf, "offset": buf_offset})
            out.append({"text": p["text"], "offset": p["offset"]})
            buf = ""
            buf_offset = None
            continue
        # 累积后会超过 target？
        if len(buf) + len(p["text"]) > opts.target_chars and buf:
            out.append({"text": buf, "offset": buf_offset})
            buf = p["text"]
            buf_offset = p["offset"]
            continue
        # 累积
        if not buf:
            buf_offset = p["offset"]
        buf += p["text"]

    if buf:
        out.append({"text": buf, "offset": buf_offset})
    return out


def _merge_small(pieces: list[dict], opts: ChunkOptions) -> list[dict]:
    """合并短于 min_chars 的 chunk 到邻居。"""
    if len(pieces) < 2:
        return pieces
    out = []
    for p in pieces:
        if out and len(out[-1]["text"]) < opts.min_chars and len(out[-1]["text"]) + len(p["text"]) <= opts.max_chars:
            out[-1]["text"] += p["text"]
        else:
            out.append({"text": p["text"], "offset": p["offset"]})
    return out


def _snap_overlap_head(tail: str) -> str:
    """将 overlap 对齐到句子/词边界。"""
    sent_match = re.search(r"[。！？!?.;；][\s]*", tail)
    if sent_match and sent_match.end() > 0 and sent_match.end() < len(tail):
        return tail[sent_match.end():]
    ws_match = re.search(r"\s", tail)
    if ws_match and ws_match.start() < len(tail) - 1:
        return tail[ws_match.start() + 1:]
    return tail


def _apply_overlap(pieces: list[dict], opts: ChunkOptions) -> list[dict]:
    """在相邻 chunk 之间注入重叠。"""
    if opts.overlap_chars <= 0 or len(pieces) < 2:
        return pieces
    out = [pieces[0]]
    for i in range(1, len(pieces)):
        prev = pieces[i - 1]
        curr = pieces[i]
        tail_src = prev["text"][-opts.overlap_chars:]
        snapped = _snap_overlap_head(tail_src)
        out.append({
            "text": snapped + curr["text"],
            "offset": curr["offset"] - len(snapped),
        })
    return out
