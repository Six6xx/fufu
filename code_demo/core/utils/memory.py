"""
code_demo/core/utils/memory.py — 跨会话记忆管理

加载、构建和保存持久化记忆，每条记忆对应一个 Markdown 文件。
来源：19_mcp-plugin.py L1915–L2037 (MemoryManager)
"""

import re
import time
from pathlib import Path

from ..config import MEMORY_DIR, MEMORY_INDEX, MEMORY_TYPES, MAX_INDEX_LINES


class MemoryManager:
    """
    跨会话加载、构建和保存持久化记忆。

    每条记忆对应一个 Markdown 文件，外加一个紧凑的索引文件。
    """

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR  # 跨会话记忆文件目录
        self.memories = {}  # name -> {description, type, content}

    def load_all(self):
        """加载 MEMORY.md 索引及所有独立的记忆文件。"""
        self.memories = {}
        if not self.memory_dir.exists():
            return

        # 扫描除 MEMORY.md 外的所有 .md 文件
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }

        count = len(self.memories)
        if count > 0:
            print(f"[Memory] 已加载 {count} 条记忆 (from {self.memory_dir})")

    def load_memory_prompt(self) -> str:
        """构建一个记忆片段，用于注入系统提示词中。"""
        if not self.memories:
            return ""

        sections = []
        sections.append("# Memories (persistent across sessions)")
        sections.append("")

        # 按类型分组以提高可读性
        for mem_type in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue
            sections.append(f"## [{mem_type}]")
            for name, mem in typed.items():
                sections.append(f"### {name}: {mem['description']}")
                if mem["content"].strip():
                    sections.append(mem["content"].strip())
                sections.append("")

        return "\n".join(sections)

    def save_memory(self, name: str, description: str,
                    mem_type: str, content: str) -> str:
        """将记忆保存到磁盘并更新索引。返回状态消息。"""
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"

        # 对名称进行清理以用于文件名
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: name cannot be empty"

        # 写入带 frontmatter 的记忆文件
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"---\n"
            f"{content}\n"
        )
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            file_path.write_text(frontmatter, encoding="utf-8")
        except Exception as e:
            return f"Error: 写入 {file_path} 失败: {e}"

        # 更新内存块
        self.memories[name] = {
            "description": description,
            "type": mem_type,
            "content": content,
            "file": file_name,
        }

        # 重建 MEMORY.md 索引
        self._rebuild_index()
        return f"保存记忆 '{name}' [{mem_type}] 至 {file_path}"

    def delete_memory(self, name: str) -> str:
        """删除一条记忆。"""
        if name not in self.memories:
            return f"Error: 记忆 '{name}' 不存在"
        mem = self.memories[name]
        file_path = self.memory_dir / mem["file"]
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError as e:
            return f"Error: 删除文件失败: {e}"
        del self.memories[name]
        self._rebuild_index()
        return f"已删除记忆 '{name}'"

    def _rebuild_index(self):
        """根据当前内存重建 MEMORY.md，最多保留 MAX_INDEX_LINES 行。"""
        lines = ["# Memory Index", ""]
        for name, mem in self.memories.items():
            lines.append(f"- {name}: {mem['description']} [{mem['type']}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"... (已截断至 {MAX_INDEX_LINES} 行)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _parse_frontmatter(self, text: str) -> dict | None:
        """解析以 --- 分隔的前置元数据和正文内容。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result = {"content": body.strip()}
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result
