"""
tools/native/runner.py — 原生工具执行器 (bash / read_file / write_file / edit_file)

负责：
- run_bash: 执行 shell 命令并返回结果
- run_read: 读取文件内容
- run_write: 写入文件
- run_edit: 替换文件中的字符串

所有函数返回 tool_result 字符串（与 19_mcp-plugin.py 逻辑一致）。
"""

import subprocess
import json
from pathlib import Path
from code_demo.core.utils.shell import run_shell_command
from code_demo.core.utils import safe_path
from code_demo.core.config import BASH_TIMEOUT, MAX_OUTPUT_CHARS
from code_demo.core.tools.native.graph_tools import (
    GRAPH_TOOL_SCHEMAS,
    GRAPH_TOOL_EXECUTORS,
)
from code_demo.core.tools.native.note_tools import (
    NOTE_TOOL_SCHEMAS,
    NOTE_TOOL_EXECUTORS,
)


def run_bash(command: str, workdir=None) -> str:
    """执行 shell 命令并返回纯文本结果。"""
    try:
        result = run_shell_command(
            command,
            capture_output=True,
            timeout=BASH_TIMEOUT,
            cwd=workdir,
        )
    except subprocess.TimeoutExpired:
        return "命令执行超时"
    except (FileNotFoundError, OSError) as e:
        return f"命令执行失败: {str(e)}"

    # 安全解码 stdout 和 stderr（兼容 UTF-8 / GBK / 二进制）
    def safe_decode(b: bytes) -> str:
        if b is None:
            return ""
        try:
            return b.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return b.decode('gbk')
            except UnicodeDecodeError:
                return b.decode('utf-8', errors='ignore')

    stdout = safe_decode(result.stdout)
    stderr = safe_decode(result.stderr)

    output = (stdout + stderr).strip()
    return output[:MAX_OUTPUT_CHARS] if output else "(no output)"


def run_read(path: str, limit: int = None, full_context = False, workdir=None) -> str:
    """读取文件内容，支持行数限制和 type coercion。"""
    try:
        # LLM 可能传字符串类型的参数，强制转换
        if limit is not None:
            limit = int(limit)
        if isinstance(full_context, str):
            full_context = full_context.lower() in ("true", "1", "yes")
        text = safe_path(path, workdir=workdir).read_text(encoding="utf-8")
        lines = text.splitlines()
        if full_context:
            return "\n".join(lines)
        else:
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)[:MAX_OUTPUT_CHARS]
    except Exception as e:
        return f"文件读取失败: {str(e)}"


def run_write(path: str, content: str, workdir=None) -> str:
    """将内容写入文件。"""
    try:
        fp = safe_path(path, workdir=workdir)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"文件写入失败: {str(e)}"


def run_edit(path: str, old_text: str, new_text: str, workdir=None) -> str:
    """在文件中查找并替换字符串。仅替换第一次出现。"""
    try:
        fp = safe_path(path, workdir=workdir)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"'{old_text}' not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"文件编辑失败: {str(e)}"


# ---- 原生工具池配置 ----
# 对应 19_mcp-plugin.py 中的 NATIVE_CHILD_TOOLS
# 此处定义工具定义 schema，由 tools/__init__.py 中的 build_tool_pool() 使用

NATIVE_TOOL_SCHEMAS = [
    {
        "name": "bash",
        "description": "执行 shell 命令并返回结果",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"}
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "limit": {"type": "integer", "description": "可选行数限制"},
                "full_context": {"type": "boolean", "description": "是否返回完整内容"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "写入文件内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "替换文件中的字符串（仅首次出现）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "old_text": {"type": "string", "description": "要替换的文本"},
                "new_text": {"type": "string", "description": "替换后的文本"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]

# 工具名 → 执行函数的映射
NATIVE_TOOL_EXECUTORS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    **GRAPH_TOOL_EXECUTORS,
    **NOTE_TOOL_EXECUTORS,
}

# 合并图谱工具 + 笔记管理工具 schema 到原生工具列表
NATIVE_TOOL_SCHEMAS = NATIVE_TOOL_SCHEMAS + GRAPH_TOOL_SCHEMAS + NOTE_TOOL_SCHEMAS
