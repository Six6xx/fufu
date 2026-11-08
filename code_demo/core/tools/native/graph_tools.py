"""
tools/native/graph_tools.py — 知识图谱原生工具

基于 graphify 的零 LLM 成本图谱工具：
- graph_query: 查询项目知识图谱（概况/核心节点/影响分析/建议问题）
- graph_update: 增量更新知识图谱（AST 提取，无 LLM 成本）
- analyze_file: 深度分析单个文件的结构和图谱关系

依赖：graphify（已安装为 graphifyy 包）
"""

from __future__ import annotations

import json
from pathlib import Path


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _find_graph_json(workdir: str | Path | None = None) -> Path | None:
    """查找 graphify-out/graph.json。

    搜索顺序：workdir/graphify-out → code_demo/graphify-out → 项目根/graphify-out
    """
    candidates = []
    if workdir:
        candidates.append(Path(workdir) / "graphify-out" / "graph.json")
    # code_demo 目录下的 graphify-out
    candidates.append(Path(__file__).parent.parent.parent / "graphify-out" / "graph.json")
    # 项目根目录下的 graphify-out
    candidates.append(Path(__file__).parent.parent.parent.parent / "graphify-out" / "graph.json")

    for p in candidates:
        if p.exists():
            return p
    return None


# ── 工具实现 ──────────────────────────────────────────────────────────────────

def run_graph_query(
    query: str = "",
    mode: str = "summary",
    workdir=None,
) -> str:
    """查询项目知识图谱。

    Args:
        query: 查询关键词（affected 模式必填）
        mode: 查询模式
          - summary: 图谱概况（节点数、边数、核心节点）
          - god_nodes: 连接最多的核心抽象（top 10）
          - surprises: 跨社区的非直觉关联
          - affected: 查找与 query 相关的节点及影响范围
          - questions: 图谱建议的探索问题
        workdir: 工作目录
    """
    graph_path = _find_graph_json(workdir)
    if not graph_path:
        return "未找到知识图谱（graphify-out/graph.json）。可先用 graph_update 构建。"

    try:
        from graphify.serve import _load_graph
        from graphify.analyze import god_nodes, surprising_connections, suggest_questions
        from graphify.affected import affected_nodes, resolve_seed, format_affected
        from graphify.cluster import cluster
    except ImportError:
        return "graphify 未安装。请执行: pip install graphifyy"

    try:
        G = _load_graph(str(graph_path))
    except Exception as e:
        return f"图谱加载失败: {e}"

    if mode == "summary":
        gods = god_nodes(G, top_n=5)
        god_lines = "\n".join(
            f"  - {g['label']} (degree={g['degree']}, {g['id']})" for g in gods
        )
        return (
            f"知识图谱概况:\n"
            f"  节点数: {G.number_of_nodes()}\n"
            f"  边数: {G.number_of_edges()}\n"
            f"核心节点 (god nodes):\n{god_lines}\n"
            f"图谱路径: {graph_path}"
        )

    elif mode == "god_nodes":
        gods = god_nodes(G, top_n=10)
        lines = [f"核心节点 (top 10):"]
        for i, g in enumerate(gods, 1):
            lines.append(f"  {i}. {g['label']} (degree={g['degree']}) — {g['id']}")
        return "\n".join(lines)

    elif mode == "surprises":
        communities = cluster(G)
        surprises = surprising_connections(G, communities, top_n=5)
        if not surprises:
            return "未发现跨社区的非直觉关联。"
        lines = ["非直觉关联 (跨社区边):"]
        for s in surprises:
            lines.append(f"  - {s.get('source', '?')} ↔ {s.get('target', '?')}: {s.get('why', '')}")
        return "\n".join(lines)

    elif mode == "affected":
        if not query:
            return "affected 模式需要提供 query 参数（要查询的实体名称）。"
        seed = resolve_seed(G, query)
        if not seed:
            # 尝试模糊匹配
            matches = [n for n in G.nodes() if query.lower() in n.lower()][:5]
            if matches:
                return (
                    f"未找到精确匹配 '{query}'，可能的节点:\n"
                    + "\n".join(f"  - {m}" for m in matches)
                )
            return f"图谱中未找到与 '{query}' 相关的节点。"
        text = format_affected(G, seed)
        return text[:3000]  # 截断防止过长

    elif mode == "questions":
        communities = cluster(G)
        labels = {}
        questions = suggest_questions(G, communities, labels, top_n=5)
        if not questions:
            return "图谱暂无建议问题。"
        lines = ["图谱建议的探索问题:"]
        for q in questions:
            lines.append(f"  - [{q.get('type', '?')}] {q.get('question', '')}")
            lines.append(f"    原因: {q.get('why', '')}")
        return "\n".join(lines)

    else:
        return f"未知模式 '{mode}'。可选: summary, god_nodes, surprises, affected, questions"


def run_graph_update(path: str = ".", workdir=None) -> str:
    """增量更新项目知识图谱（AST 提取，无 LLM 成本）。

    扫描 path 下的代码文件，提取函数/类/导入关系，合并到已有的 graph.json 中。
    如果 graph.json 不存在，则从零构建。

    Args:
        path: 要分析的目录路径（相对于 workdir）
        workdir: 工作目录
    """
    try:
        from graphify.extract import collect_files, extract
        from graphify.build import build_from_json, build_merge
        from graphify.cluster import cluster
        from graphify.export import to_json
    except ImportError:
        return "graphify 未安装。请执行: pip install graphifyy"

    # 解析路径
    if workdir and not Path(path).is_absolute():
        target = Path(workdir) / path
    else:
        target = Path(path)

    if not target.exists():
        return f"路径不存在: {target}"

    graph_path = _find_graph_json(workdir)
    output_dir = target / "graphify-out" if target.is_dir() else target.parent / "graphify-out"
    output_path = output_dir / "graph.json"

    try:
        # 1. 收集文件
        if target.is_file():
            files = [target]
        else:
            files = collect_files(target)

        if not files:
            return f"未找到可分析的代码文件: {target}"

        # 2. AST 提取
        result = extract(files)
        node_count = len(result.get("nodes", []))
        edge_count = len(result.get("edges", []))

        # 3. 构建或增量合并
        if graph_path and graph_path.exists():
            G = build_merge([result], str(graph_path))
            action = "增量更新"
        else:
            G = build_from_json(result)
            action = "从零构建"
            output_dir.mkdir(parents=True, exist_ok=True)

        # 4. 社区检测
        communities = cluster(G)

        # 5. 保存
        to_json(G, communities, str(output_path))

        return (
            f"图谱{action}完成:\n"
            f"  扫描文件: {len(files)}\n"
            f"  提取节点: {node_count}\n"
            f"  提取边: {edge_count}\n"
            f"  图谱总计: {G.number_of_nodes()} 节点, {G.number_of_edges()} 边\n"
            f"  保存到: {output_path}"
        )
    except Exception as e:
        return f"图谱更新失败: {e}"


def run_analyze_file(path: str, workdir=None) -> str:
    """深度分析单个文件的结构和图谱关系。

    结合 AST 提取 + 图谱上下文，返回：
    - 文件中定义的函数/类
    - 导入依赖
    - 在图谱中的关联节点

    Args:
        path: 文件路径（相对于 workdir）
        workdir: 工作目录
    """
    try:
        from graphify.extract import extract
    except ImportError:
        return "graphify 未安装。请执行: pip install graphifyy"

    # 解析路径
    if workdir and not Path(path).is_absolute():
        file_path = Path(workdir) / path
    else:
        file_path = Path(path)

    if not file_path.exists():
        return f"文件不存在: {file_path}"

    if not file_path.is_file():
        return f"不是文件: {file_path}"

    try:
        # 1. AST 提取
        result = extract([file_path])
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])

        if not nodes:
            return f"无法从 {file_path.name} 提取结构信息（可能是不支持的文件类型）。"

        # 2. 分类节点
        functions = [n for n in nodes if n.get("type") in ("function", "method")]
        classes = [n for n in nodes if n.get("type") == "class"]
        imports = [e for e in edges if e.get("relation") in ("imports", "imports_from")]
        calls = [e for e in edges if e.get("relation") == "calls"]

        lines = [f"文件分析: {file_path.name} ({file_path})"]
        lines.append(f"  提取节点: {len(nodes)}, 边: {len(edges)}")

        if classes:
            lines.append(f"\n类 ({len(classes)}):")
            for c in classes:
                lines.append(f"  - {c.get('label', c.get('id', '?'))}")

        if functions:
            lines.append(f"\n函数/方法 ({len(functions)}):")
            for f in functions:
                lines.append(f"  - {f.get('label', f.get('id', '?'))}")

        if imports:
            lines.append(f"\n导入 ({len(imports)}):")
            for imp in imports[:10]:
                lines.append(f"  - {imp.get('source', '?')} → {imp.get('target', '?')}")
            if len(imports) > 10:
                lines.append(f"  ... 还有 {len(imports) - 10} 个")

        if calls:
            lines.append(f"\n调用关系 ({len(calls)}):")
            for call in calls[:10]:
                lines.append(f"  - {call.get('source', '?')} → {call.get('target', '?')}")
            if len(calls) > 10:
                lines.append(f"  ... 还有 {len(calls) - 10} 个")

        # 3. 图谱上下文（如果可用）
        graph_path = _find_graph_json(workdir)
        if graph_path:
            try:
                from graphify.serve import _load_graph
                from graphify.affected import affected_nodes, resolve_seed
                G = _load_graph(str(graph_path))
                # 尝试在图谱中查找该文件相关的节点
                file_stem = file_path.stem
                seed = resolve_seed(G, file_stem)
                if seed:
                    hits = affected_nodes(G, seed, depth=1)
                    if hits:
                        lines.append(f"\n图谱关联 ({len(hits)} 个邻居):")
                        for h in hits[:10]:
                            lines.append(
                                f"  - {h.node_id} [depth={h.depth}, via {h.via_relation}]"
                            )
            except Exception:
                pass  # 图谱查询失败不影响基本分析结果

        return "\n".join(lines)

    except Exception as e:
        return f"文件分析失败: {e}"


# ── 工具定义 schema ──────────────────────────────────────────────────────────

GRAPH_TOOL_SCHEMAS = [
    {
        "name": "graph_query",
        "description": "查询项目知识图谱（代码结构、依赖关系、核心抽象、影响分析）",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询关键词（affected 模式必填，其他模式可省略）",
                },
                "mode": {
                    "type": "string",
                    "description": "查询模式: summary(概况) | god_nodes(核心节点) | surprises(非直觉关联) | affected(影响分析) | questions(建议问题)",
                    "enum": ["summary", "god_nodes", "surprises", "affected", "questions"],
                    "default": "summary",
                },
            },
            "required": [],
        },
    },
    {
        "name": "graph_update",
        "description": "增量更新项目知识图谱（AST 提取代码结构，无 LLM 成本）。修改代码后调用此工具保持图谱同步。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要分析的目录或文件路径（默认当前目录）",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
    {
        "name": "analyze_file",
        "description": "深度分析单个文件的结构（函数/类/导入/调用关系）及其在知识图谱中的位置",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要分析的文件路径",
                },
            },
            "required": ["path"],
        },
    },
]

# 工具名 → 执行函数映射
GRAPH_TOOL_EXECUTORS = {
    "graph_query": run_graph_query,
    "graph_update": run_graph_update,
    "analyze_file": run_analyze_file,
}
