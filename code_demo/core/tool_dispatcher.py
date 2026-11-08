"""
core/tool_dispatcher.py — 工具分发器（registry pattern）

将 agent.py 中的 execute_tool if-elif God Method 重构为基于注册表的分发器。
"""

import json
import threading
from typing import Callable, Any
from pathlib import Path

from code_demo.core.config import AI_CONFIG_DIR, COMPANION_PROFILE_FILE, INBOX_DIR, TEAM_DIR
from code_demo.core.event.hooks import HookManager
from code_demo.core.tools import CapabilityPermissionGate, MCPToolRouter
from code_demo.core.tools.native.runner import NATIVE_TOOL_EXECUTORS as NATIVE_EXECUTORS
from code_demo.core.teammate import scan_unclaimed_tasks, claim_task


# ── 代理工具定义（非原生工具，由 dispatcher 内部处理）──
PROXY_TOOL_DEFS = [
    {
        "name": "send_message",
        "description": "给队友发送消息",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "接收者名称"},
                "content": {"type": "string", "description": "消息内容"},
                "msg_type": {"type": "string", "description": "消息类型", "default": "message"},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "read_inbox",
        "description": "读取并清空收件箱",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_tasks",
        "description": "列出所有可认领的任务",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "claim_task",
        "description": "认领一个任务",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_skills",
        "description": "列出所有可用技能",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "load_skill",
        "description": "加载指定技能",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名称"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "complete_companion_setup",
        "description": "保存用户确认的陪伴资料并移除首次设置 Skill。仅在用户明确确认全部资料后调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "user_name": {"type": "string", "description": "用户希望使用的姓名或称呼"},
                "companion_name": {"type": "string", "description": "陪伴者自己的名称"},
                "companion_call_name": {"type": "string", "description": "用户希望怎样称呼陪伴者"},
                "interests": {"type": "string", "description": "用户喜欢做的事、兴趣或话题"},
                "personality": {"type": "string", "description": "用户希望陪伴者呈现的人格和交流风格"},
                "user_confirmed": {"type": "boolean", "description": "用户已复述确认以上资料时必须为 true"},
            },
            "required": ["user_name", "companion_name", "companion_call_name", "interests", "personality", "user_confirmed"],
        },
    },
    {
        "name": "suggest_skill_merges",
        "description": "检测当前可合并的相似技能对，返回合并建议列表。用于在发现重复或高度相似的技能时，向用户提出合并建议。",
        "parameters": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "相似度阈值（0-1），超过此值视为可合并，默认 0.6"
                }
            },
            "required": []
        },
    },
    {
        "name": "merge_skills",
        "description": "合并两个相似技能为一个。合并前应先向用户说明理由并获得确认。合并后原技能会被删除，产生一个融合后的新技能。",
        "parameters": {
            "type": "object",
            "properties": {
                "name_a": {
                    "type": "string",
                    "description": "要合并的第一个技能名称"
                },
                "name_b": {
                    "type": "string",
                    "description": "要合并的第二个技能名称"
                },
                "merged_body": {
                    "type": "string",
                    "description": "合并后的技能正文内容（应综合两个技能的精华，去除重复）"
                }
            },
            "required": ["name_a", "name_b", "merged_body"]
        },
    },
    {
        "name": "list_teammates",
        "description": "列出所有队友",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "cron_create",
        "description": "使用 cron 表达式安排一个周期性或单次任务",
        "parameters": {
            "type": "object",
            "properties": {
                "cron": {"type": "string", "description": "5 字段 cron 表达式：'分钟 小时 日期 月份 星期'"},
                "prompt": {"type": "string", "description": "任务触发时要注入的提示词"},
                "recurring": {"type": "boolean", "description": "true=重复执行，false=触发一次后删除。默认为 true。"},
                "durable": {"type": "boolean", "description": "true=持久化到磁盘，false=仅限当前会话。默认为 false。"},
            },
            "required": ["cron", "prompt"],
        },
    },
    {
        "name": "cron_delete",
        "description": "按任务 ID 删除一个定时任务",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "要删除的任务 ID"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "cron_list",
        "description": "列出所有已安排的定时任务",
        "parameters": {"type": "object", "properties": {}},
    },
]


class ToolDispatcher:
    """基于 registry 的工具分发器，替代原 agent.py 中的 if-elif God Method。"""

    def __init__(
        self,
        hook_manager: HookManager,
        permission_gate: CapabilityPermissionGate,
        mcp_router: MCPToolRouter,
        workdir_getter: Callable[[], Path],
        message_bus,
        scheduler,
        skill_registry,
        name: str,
        role: str,
    ):
        self._registry: dict[str, Callable[[dict], str]] = {}  # 工具名称到处理函数的注册表
        self._hook_manager = hook_manager  # 执行工具前后触发的 Hook 管理器
        self._permission_gate = permission_gate  # 原生及 MCP 工具统一权限门控
        self._mcp_router = mcp_router  # MCP 工具路由器
        self._workdir_getter = workdir_getter  # 获取当前工作目录的回调
        self._message_bus = message_bus  # 代理间消息总线
        self._scheduler = scheduler  # 定时任务调度器
        self._skill_registry = skill_registry  # 技能读取与管理注册表
        self._agent_name = name  # 当前执行 Agent 的名称
        self._agent_role = role  # 当前执行 Agent 的角色
        self._execution_scope = threading.local()

        # 注册内置工具
        self._register_builtins()

    def register(self, name: str, handler: Callable[[dict], str]):
        """注册一个工具处理器。"""
        self._registry[name] = handler

    def set_execution_scope(self, scope: str):
        """设置当前线程的执行范围，返回此前范围以便调用方恢复。"""
        previous = getattr(self._execution_scope, "value", "user")
        self._execution_scope.value = scope
        return previous

    def restore_execution_scope(self, scope: str):
        self._execution_scope.value = scope

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """分发工具调用。完整复制原 execute_tool 的 Hook + 权限 + 执行 + PostHook 逻辑。"""

        # ── PreToolUse Hook ──
        hook_context = {"tool_name": tool_name, "tool_input": tool_input}
        pre_result = self._hook_manager.run_hooks("PreToolUse", hook_context)

        if pre_result["blocked"]:
            return json.dumps({
                "status": "blocked",
                "reason": f"Hook 阻止了此操作: {pre_result.get('block_reason', '')}",
                "suggestion": "此操作被安全策略阻止。请尝试其他方式完成任务。",
            }, ensure_ascii=False)

        # Hook 可能修改了 tool_input
        tool_input = hook_context.get("tool_input", tool_input)

        # ── 权限检查 ──
        permission_override = pre_result.get("permission_override")
        check = self._permission_gate.check(tool_name, tool_input)
        if permission_override:
            check["behavior"] = permission_override

        execution_scope = getattr(self._execution_scope, "value", "user")
        if execution_scope == "autonomous_readonly":
            # Read-only autonomous work is enforced here, after hooks, so a hook
            # cannot accidentally upgrade the scope to a write-capable action.
            read_only_tools = {"read_file", "graph_query", "analyze_file", "screen_observe",
                               "list_skills", "load_skill", "list_tasks", "list_teammates"}
            actual_tool = check.get("intent", {}).get("tool", tool_name)
            if check.get("intent", {}).get("risk") != "read" and actual_tool not in read_only_tools:
                check["behavior"] = "deny"
                check["reason"] = "自主只读范围禁止此工具；仅允许明确列入白名单的只读能力"

        if check["behavior"] == "deny":
            mode_hint = (
                "切换到 auto 模式可自动批准非高风险操作"
                if self._permission_gate.mode == "default"
                else "请调整操作方式或联系用户"
            )
            return json.dumps({
                "status": "denied",
                "reason": check.get("reason", "权限被拒绝"),
                "risk_level": check.get("intent", {}).get("risk", "unknown"),
                "suggestion": f"当前权限模式: {self._permission_gate.mode}。{mode_hint}",
            }, ensure_ascii=False)

        if check["behavior"] == "ask":
            result = self._permission_gate.ask_user(check["intent"], tool_input)
            if not result.get("approved", False):
                deny_reason = result.get("reason") or "用户拒绝了此操作"
                return json.dumps({
                    "status": "denied",
                    "reason": deny_reason,
                    "user_action": result.get("action", "deny"),
                    "suggestion": f"操作未执行成功（{deny_reason}）。请如实告知用户操作失败，不得基于截图或推测声称操作已完成。",
                }, ensure_ascii=False)

        try:
            # ── 先查 registry（代理工具）──
            if tool_name in self._registry:
                tool_result = self._registry[tool_name](tool_input)

            # ── 然后查原生工具 ──
            elif tool_name in NATIVE_EXECUTORS:
                executor = NATIVE_EXECUTORS[tool_name]
                tool_result = executor(**tool_input, workdir=self._workdir_getter())

            # ── 最后查 MCP ──
            elif self._mcp_router.is_mcp_tool(tool_name):
                tool_result = self._mcp_router.call(tool_name, tool_input)

            else:
                tool_result = json.dumps({"error": f"未知工具: {tool_name}"})

            # ── PostToolUse Hook ──
            post_context = {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": str(tool_result)[:1000],
            }
            post_result = self._hook_manager.run_hooks("PostToolUse", post_context)
            if post_result["messages"]:
                hook_msg = "\n".join(post_result["messages"])
                tool_result = f"{tool_result}\n\n[Hook 提示]: {hook_msg}"

            return tool_result
        except Exception as e:
            return f"Error: {tool_name} 执行失败: {str(e)[:200]}"

    # ────────────────────────────────
    # 内置 Handler 注册
    # ────────────────────────────────

    def _register_builtins(self):
        """注册所有内置 handler。"""
        self.register("send_message", self._handle_send_message)
        self.register("read_inbox", self._handle_read_inbox)
        self.register("list_tasks", self._handle_list_tasks)
        self.register("claim_task", self._handle_claim_task)
        self.register("list_teammates", self._handle_list_teammates)
        self.register("list_skills", self._handle_list_skills)
        self.register("load_skill", self._handle_load_skill)
        self.register("complete_companion_setup", self._handle_complete_companion_setup)
        self.register("suggest_skill_merges", self._handle_suggest_skill_merges)
        self.register("merge_skills", self._handle_merge_skills)
        self.register("cron_create", self._handle_cron_create)
        self.register("cron_delete", self._handle_cron_delete)
        self.register("cron_list", self._handle_cron_list)

    # ────────────────────────────────
    # Handler 实现
    # ────────────────────────────────

    def _handle_send_message(self, params: dict) -> str:
        to = params.get("to", "")
        content = params.get("content", "")
        msg_type = params.get("msg_type", "message")
        return self._message_bus.send(self._agent_name, to, content, msg_type)

    def _handle_read_inbox(self, params: dict = None) -> str:
        msgs = self._message_bus.read_inbox(self._agent_name)
        if not msgs:
            return json.dumps({"messages": [], "note": "收件箱为空"})
        return json.dumps({"messages": msgs}, indent=2, ensure_ascii=False)

    def _handle_list_tasks(self, params: dict = None) -> str:
        tasks = scan_unclaimed_tasks(self._agent_role)
        if not tasks:
            return json.dumps({"tasks": [], "note": "无未认领任务"})
        summaries = [
            {
                "task_id": t.get("task_id"),
                "subject": t.get("subject", t.get("description", "")),
                "status": t.get("status"),
                "claim_role": t.get("claim_role", ""),
            }
            for t in tasks
        ]
        return json.dumps({"tasks": summaries}, indent=2, ensure_ascii=False)

    def _handle_claim_task(self, params: dict) -> str:
        task_id = int(params.get("task_id", -1))
        return claim_task(task_id, self._agent_name, self._agent_role, source="agent_auto")

    def _handle_list_teammates(self, params: dict = None) -> str:
        """列出 .team/ 目录下的队友。优先从 config.json 读取，回退到收件箱文件名。"""
        teammates = []
        team_dir = TEAM_DIR
        if team_dir.exists():
            # 优先：从 TeammateManager 的 config.json 读取
            cfg_file = team_dir / "config.json"
            if cfg_file.exists():
                try:
                    data = json.loads(cfg_file.read_text())
                    members = data.get("members", [])
                    teammates = [
                        {"name": m.get("name"), "role": m.get("role", "unknown"), "status": m.get("status", "unknown")}
                        for m in members
                    ]
                except Exception:
                    pass
            # 回退：从收件箱文件名推断
            if not teammates:
                inbox_dir = INBOX_DIR
                if inbox_dir.exists():
                    names = set()
                    for f in inbox_dir.glob("*.jsonl"):
                        names.add(f.stem)
                    teammates = [{"name": n, "role": "unknown"} for n in sorted(names)]
        return json.dumps({"teammates": teammates}, indent=2, ensure_ascii=False)

    def _handle_list_skills(self, params: dict = None) -> str:
        return self._skill_registry.describe_available()

    def _handle_load_skill(self, params: dict) -> str:
        skill_name = params.get("name", "")
        result = self._skill_registry.load_full_text(skill_name)
        self._skill_registry.record_usage(skill_name)
        return result

    def _handle_complete_companion_setup(self, params: dict) -> str:
        """保存已由用户确认的陪伴资料，并在成功后移除首次设置技能。"""
        fields = ("user_name", "companion_name", "companion_call_name", "interests", "personality")
        profile = {key: str(params.get(key, "")).strip() for key in fields}
        missing = [key for key, value in profile.items() if not value]
        if missing:
            return json.dumps({"ok": False, "error": "资料尚未齐全", "missing": missing}, ensure_ascii=False)
        if params.get("user_confirmed") is not True:
            return json.dumps({"ok": False, "error": "必须先向用户复述资料并取得明确确认"}, ensure_ascii=False)

        profile["updated_at"] = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
        COMPANION_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COMPANION_PROFILE_FILE.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        removed = self._skill_registry.delete_skill("__companion_onboarding")
        if not removed:
            return json.dumps({"ok": False, "saved": True, "error": "资料已保存，但未找到设置 Skill；请检查技能目录"}, ensure_ascii=False)
        return json.dumps({"ok": True, "saved": True, "onboarding_skill_removed": True}, ensure_ascii=False)

    def _handle_suggest_skill_merges(self, params: dict) -> str:
        threshold = params.get("threshold", 0.6)
        suggestions = self._skill_registry.suggest_merge(threshold=threshold)
        if suggestions:
            lines = []
            for a, b, sim in suggestions:
                lines.append(f"- {a} \u2194 {b} (相似度: {sim:.1%})")
            return f"发现 {len(suggestions)} 对可合并技能：\n" + "\n".join(lines)
        else:
            return "当前没有发现可合并的相似技能对。"

    def _handle_merge_skills(self, params: dict) -> str:
        name_a = params.get("name_a", "")
        name_b = params.get("name_b", "")
        merged_body = params.get("merged_body", "")
        if not name_a or not name_b:
            return "错误：缺少 name_a 或 name_b 参数"
        elif not merged_body:
            return "错误：merged_body 不能为空，请提供合并后的技能正文"
        else:
            result = self._skill_registry.merge_skills(name_a, name_b, merged_body)
            if result:
                return f"合并成功！新技能名称: {result}"
            else:
                return "合并失败：技能可能已锁定、不存在，或名称有误"

    def _handle_cron_create(self, params: dict) -> str:
        return self._scheduler.create(
            params["cron"],
            params["prompt"],
            params.get("recurring", True),
            params.get("durable", False),
        )

    def _handle_cron_delete(self, params: dict) -> str:
        return self._scheduler.delete(params["id"])

    def _handle_cron_list(self, params: dict = None) -> str:
        return self._scheduler.list_tasks()
