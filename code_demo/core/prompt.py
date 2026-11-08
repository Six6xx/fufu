"""
code_demo/core/prompt.py — SystemPromptBuilder
从独立模块组装系统提示词。
来源：19_mcp-plugin.py L1713–L1894
"""
import datetime
import platform
import os
from pathlib import Path

from code_demo.core.config import WORKDIR, MODEL


SYSTEM_LEAD_PROMPT = """你是一个位于 {workdir} 的专业编码代理。

## 核心能力
- 文件操作 (read_file/write_file/edit_file)
- 代码分析 (analyze_file) — 深度分析文件结构、依赖、在图谱中的位置
- 知识图谱 (graph_query/graph_update) — 查询/更新项目知识图谱
- Shell 命令（{shell_name}；命令语法需符合当前平台）
- 队友通信 (send_message/read_inbox/list_teammates)
- 任务管理 (list_tasks/claim_task)
- 技能加载 (list_skills/load_skill)

## 分析判断原则
遇到分析类任务时，先判断复杂度再选择方式：
- **简单查询**（某个函数在哪、谁调用了它）→ 直接 graph_query
- **单文件分析**（理解一个文件的作用）→ analyze_file
- **项目全貌**（架构理解、模块关系）→ graph_query(mode="summary")
- **影响分析**（改了 A 会影响谁）→ graph_query(mode="affected", query="A")
- **代码不存在图谱中** → graph_update 先构建，再查询
- **非代码问题** → 直接回答或 read_file

## 强制工具使用规则
分析复杂代码（涉及多文件、跨模块、调用链、影响范围）时，MUST先调用 graph_query/analyze_file 获取结构数据，禁止凭记忆直接回答。

## 工作原则
0. 当前处于 Agent 执行阶段：只有收到用户任务或执行层委派的任务时才调用工具；数字人 Phase 1 的即时回应不是执行结果，也不能据此声称任务完成
1. 先读取再编辑，不凭空猜测
2. 分析代码时优先使用图谱工具，比 bash grep 更准确
3. 修改代码后用 graph_update 保持图谱同步
4. 简洁输出，工具结果比文字描述更有说服力
5. 安全第一，不执行危险命令
6. auto 模式下所有操作直接执行，无需等待确认；其他模式下 high 风险操作会由系统弹出确认卡片，不要自行拒绝
7. 工具返回 denied/timeout 时，必须如实告知用户操作未成功，禁止声称或暗示操作已完成——截图内容不能作为自身操作成功的证据

## 自我认知
- 你有跨会话记忆，会记住用户偏好和项目上下文
- 你有技能系统，可以加载和使用已学到的技能
- 你的执行模式会被分析，重复的工具调用序列会被自动提取为技能
- 你的知识图谱存储在 graphify-out/，可随时查询和更新

## 可用工具
{tool_listing}

## 可用技能
{skill_listing}

### 技能调用原则
优先使用技能和 MCP 工具，它们比原始 shell 命令更安全准确。
"""
# === 静态/动态边界标记 ===
DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="


class SystemPromptBuilder:
    """从独立模块组装系统提示词。"""

    def __init__(
        self,
        tool_defs: list[dict] = None,
        skill_registry=None,
        memory_manager=None,
        workdir_getter=None,
        three_layer_memory=None,
        user_profile=None,
        query_router=None,
    ):
        self.tool_defs = tool_defs or []  # 当前提示词可引用的工具定义
        self.skill_registry = skill_registry  # 可用技能及其元数据注册表
        self.memory_manager = memory_manager  # 提供长期记忆检索能力的管理器
        self._workdir_getter = workdir_getter or (lambda: WORKDIR)  # 获取当前工作目录的回调
        self.three_layer_memory = three_layer_memory  # ThreeLayerMemory 实例（可选）
        self.user_profile = user_profile              # UserProfile 实例（可选）
        self.query_router = query_router              # QueryRouter 实例（可选）

    def _build_core(self) -> str:
        tool_lines = []
        for t in self.tool_defs:
            name = t.get("name", "")
            desc = t.get("description", "")
            params = t.get("parameters", {}).get("properties", {})
            param_names = ", ".join(params.keys()) if params else ""
            tool_lines.append(f"  - {name}({param_names}): {desc}")
        tool_listing = "\n".join(tool_lines) if tool_lines else "(无工具)"

        skill_listing = "(无可用技能)"
        if self.skill_registry:
            skill_listing = self.skill_registry.describe_available()

        return SYSTEM_LEAD_PROMPT.format(
            workdir=self._workdir_getter(),
            tool_listing=tool_listing,
            skill_listing=skill_listing,
            shell_name="PowerShell" if os.name == "nt" else "bash",
        )

    def _build_dynamic_context(self) -> str:
        lines = [
            f"当前日期: {datetime.date.today().isoformat()}",
            f"工作目录: {self._workdir_getter()}",
            f"模型: {os.getenv('MODEL', 'unknown')}",
            f"平台: {platform.system()}",
        ]
        return "\n".join(lines)

    def build(self, query: str = "") -> str:
        """组装完整的系统提示词（含内置 Skills 分层注入）。

        Args:
            query: 当前用户查询文本（可选）。
                   传入后将触发：
                   - 始终注入层（layer="always"）的内部 skills
                   - 上下文触发层（通过 query_router 路由到对应 layer 的内部 skills）
                   - 三层记忆增强（recall + 用户画像）
        """
        parts: list[str] = []

        # 1. 始终注入层：加载 layer="always" 的内部skills
        if self.skill_registry:
            try:
                always_skills = self.skill_registry.get_internal_skills(layer="always")
                if always_skills:
                    parts.append("\n\n".join(always_skills))
            except Exception:
                pass

        # 2. 核心提示词（含工具列表、可见技能列表）
        parts.append(self._build_core())

        # 3. 上下文触发层（仅当 query 非空且 query_router 可用）
        if query and self.query_router:
            try:
                plan = self.query_router.route(query)
                triggered_skills = self.skill_registry.get_internal_skills(layer=plan.query_type)
                if triggered_skills:
                    parts.append("## 当前任务指导\n\n" + "\n\n".join(triggered_skills))
            except Exception:
                pass  # 容错：路由失败不影响基本功能

        # 4. 动态上下文
        parts.append(self._build_dynamic_context())

        # 5. 三层记忆增强（现有逻辑保留不变）
        if query:
            memory_parts: list[str] = []

            # 用户画像摘要
            if self.user_profile:
                try:
                    summary = self.user_profile.get_summary()
                    if summary:
                        memory_parts.append(f"## 用户画像\n{summary}")
                except Exception:
                    pass

            # 三层记忆检索
            if self.three_layer_memory:
                try:
                    recall_text = self.three_layer_memory.recall(query, top_k=3)
                    if recall_text:
                        memory_parts.append(f"## 相关记忆\n{recall_text}")
                except Exception:
                    pass

            if memory_parts:
                parts.append("\n\n".join(memory_parts))

        # 6. 基础记忆
        if self.memory_manager:
            try:
                mem_prompt = self.memory_manager.load_memory_prompt()
                if mem_prompt:
                    parts.append(mem_prompt)
            except Exception:
                pass

        return "\n\n".join(filter(None, parts))

    def refresh(self):
        """当工具池变化时刷新构建器。"""
        # 工具池更新后，重新构建核心提示词
        # 注意：build() 方法每次调用都会重新生成，所以这里无需缓存
        # 但保留此方法以便未来扩展（如缓存提示词避免重复构建）
        pass
