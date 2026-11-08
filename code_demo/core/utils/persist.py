"""
code-demo/core/utils/persist.py — 大输出持久化
来源：19_mcp-plugin.py L2666–L2678
"""

from code_demo.core.config import PERSIST_THRESHOLD, TOOL_RESULTS_DIR, PREVIEW_CHARS, WORKDIR


def persist_large_output(tool_use_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not stored_path.exists():
        stored_path.write_text(output)
    preview = output[:PREVIEW_CHARS]
    rel_path = stored_path.relative_to(WORKDIR)
    return (
        "<persisted-output>\n"
        f"所有输出已保存为: {rel_path}\n"
        "预览: \n"
        f"{preview}\n"
        "</persisted-output>"
    )