"""
tools/native/note_tools.py — 认知笔记管理工具

提供给 Agent 使用的笔记生命周期管理工具：
- list_expired_notes: 列出过期（>N天）且低重要性的笔记
- delete_notes: 删除指定笔记文件（仅限 .cognitive_notes/ 目录）

安全边界：只能操作 .cognitive_notes/ 下的文件，不会影响其他文件。

双写机制：笔记删除时同步清理 EpisodicMemory.Trash 中对应内容（如果 engine 可用）。
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# 模块级 engine 引用（由外部注入，用于 episodic 双写）
_engine_ref: Optional[Any] = None


def set_engine_ref(engine) -> None:
    """注入 CognitiveEngine 引用，使笔记工具能同步操作 EpisodicMemory。"""
    global _engine_ref
    _engine_ref = engine


def run_list_expired_notes(days: int = 3, workdir=None) -> str:
    """列出过期且低重要性的笔记。

    Args:
        days: 超过多少天算过期，默认 3 天
        workdir: 工作目录根路径
    """
    if isinstance(days, str):
        days = int(days)

    notes_dir = Path(workdir or ".") / ".cognitive_notes"
    if not notes_dir.exists() or not list(notes_dir.glob("*.md")):
        return "笔记目录为空，笔记系统已完全迁移至情景记忆（EpisodicMemory）。无需管理。"

    threshold = datetime.now() - timedelta(days=days)
    expired: list[dict] = []

    for f in sorted(notes_dir.glob("*.md")):
        try:
            # 从文件名解析日期：YYYY-MM-DD_HH-MM_slug.md
            date_str = f.name[:16]
            file_date = datetime.strptime(date_str, "%Y-%m-%d_%H-%M")
            if file_date >= threshold:
                continue

            # 读取重要性评分
            content = f.read_text(encoding="utf-8")
            score_match = re.search(r"重要性: ([\d.]+)%", content)
            score = float(score_match.group(1)) if score_match else 0.0

            # 读取意图
            intent_match = re.search(r"意图: (.+)", content)
            intent = intent_match.group(1).strip() if intent_match else f.stem

            # 读取摘要（取前100字）
            summary_match = re.search(r"## 摘要\n\n(.+?)(\n\n|$)", content, re.DOTALL)
            summary = summary_match.group(1).strip()[:100] if summary_match else ""

            expired.append({
                "filename": f.name,
                "date": file_date.strftime("%Y-%m-%d %H:%M"),
                "importance": score,
                "intent": intent,
                "summary": summary,
                "age_days": (datetime.now() - file_date).days,
            })
        except (ValueError, OSError):
            continue

    if not expired:
        return "没有过期笔记需要清理。"

    # 格式化输出
    lines = [f"找到 {len(expired)} 个过期笔记（>{days}天）：\n"]
    for item in expired:
        lines.append(
            f"- [{item['filename']}] "
            f"重要性={item['importance']:.1f}% | "
            f"{item['age_days']}天前 | "
            f"意图: {item['intent'][:30]}"
        )
        if item['summary']:
            lines.append(f"  摘要: {item['summary'][:60]}")
    lines.append("\n你可以使用 delete_notes 工具删除不需要的笔记。")
    # 提示情景记忆同步状态
    if _engine_ref and hasattr(_engine_ref, 'episodic') and _engine_ref.episodic:
        lines.append("（这些笔记内容已同步到情景记忆系统，删除笔记文件时会同步清理 Trash 层。）")
    lines.append("\n⚠️ 笔记系统已废弃，不再产生新笔记。以上均为历史遗留文件，可放心清理。")
    return "\n".join(lines)


def run_delete_notes(filenames: str, workdir=None) -> str:
    """删除指定的笔记文件。

    Args:
        filenames: 逗号分隔的文件名列表（仅文件名，不含路径）
        workdir: 工作目录根路径

    安全限制：只能删除 .cognitive_notes/ 目录下的 .md 文件。
    """
    notes_dir = Path(workdir or ".") / ".cognitive_notes"
    if not notes_dir.exists():
        return "笔记目录不存在"

    # 解析文件名列表
    if isinstance(filenames, list):
        names = filenames
    else:
        names = [n.strip().strip('"').strip("'") for n in filenames.split(",")]
    names = [n for n in names if n]

    if not names:
        return "未指定要删除的文件"

    deleted = []
    skipped = []

    for name in names:
        # 安全检查：只允许 .md 文件，不允许路径遍历
        if "/" in name or "\\" in name or ".." in name:
            skipped.append(f"{name} (路径不安全)")
            continue
        if not name.endswith(".md"):
            skipped.append(f"{name} (只能删除 .md 文件)")
            continue

        target = notes_dir / name
        if not target.exists():
            skipped.append(f"{name} (不存在)")
            continue

        # 确认文件确实在 notes_dir 下
        try:
            target.resolve().relative_to(notes_dir.resolve())
        except ValueError:
            skipped.append(f"{name} (路径越界)")
            continue

        target.unlink()
        deleted.append(name)

        # 双写：同步从 EpisodicMemory Trash 层清理对应内容
        try:
            if _engine_ref and hasattr(_engine_ref, 'episodic') and _engine_ref.episodic:
                episodic = _engine_ref.episodic
                # 从文件名提取日期作为 date_key 进行定向清理
                date_key = name[:10]  # "YYYY-MM-DD"
                if date_key in episodic.trash_store:
                    # 搜索该日期下与笔记文件名相关的事件并移除
                    for cat_path, events in list(episodic.trash_store[date_key].items()):
                        # 匹配：笔记文件名中的 slug 部分作为关键词
                        slug = name[17:-3] if len(name) > 20 else name  # 去除日期前缀和.md后缀
                        events[:] = [e for e in events if slug not in (e.detail or "")]
                        if not events:
                            del episodic.trash_store[date_key][cat_path]
                    # 清理空日期分区
                    if not episodic.trash_store[date_key]:
                        del episodic.trash_store[date_key]
        except Exception:
            pass  # episodic 清理失败不影响主流程

    result_parts = []
    if deleted:
        result_parts.append(f"已删除 {len(deleted)} 个笔记: {', '.join(deleted)}")
    if skipped:
        result_parts.append(f"跳过 {len(skipped)} 个: {', '.join(skipped)}")

    return "\n".join(result_parts) if result_parts else "没有文件被处理"


# ---- 工具 Schema 定义 ----

NOTE_TOOL_SCHEMAS = [
    {
        "name": "list_expired_notes",
        "description": "列出过期（默认>3天）且可能不再需要的认知笔记。显示每个笔记的重要性评分、意图和摘要，帮助你决定哪些要保留、哪些可以清理。",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "超过多少天算过期，默认3天",
                },
            },
            "required": [],
        },
    },
    {
        "name": "delete_notes",
        "description": "删除指定的认知笔记文件（仅限 .cognitive_notes/ 目录）。你需要先用 list_expired_notes 查看有哪些过期笔记，然后自行判断哪些不再有价值后再删除。",
        "parameters": {
            "type": "object",
            "properties": {
                "filenames": {
                    "type": "string",
                    "description": "逗号分隔的文件名列表，如 '2024-01-10_14-30_天气.md, 2024-01-11_09-00_新闻.md'",
                },
            },
            "required": ["filenames"],
        },
    },
]

# 工具名 → 执行函数
NOTE_TOOL_EXECUTORS = {
    "list_expired_notes": run_list_expired_notes,
    "delete_notes": run_delete_notes,
}
