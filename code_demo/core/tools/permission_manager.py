"""
code_demo/core/tools/permission_manager.py — 工具权限决策管道

管理工具调用的权限决策：拒绝规则 → 模式检查 → 允许规则 → 询问用户。
来源：19_mcp-plugin.py L2381–L2490 (PermissionManager)
"""

import json
from fnmatch import fnmatch

from ..config import MODES, READ_ONLY_TOOLS, WRITE_TOOLS
from ..utils.security import bash_validator


# 默认规则：首个匹配项生效
DEFAULT_RULES = [
    # 始终拒绝的危险模式
    {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
    {"tool": "bash", "content": "sudo *", "behavior": "deny"},
    # 允许阅读任何文件
    {"tool": "read_file", "path": "*", "behavior": "allow"},
]


class PermissionManager:
    """
    管理工具调用的权限决策。

    决策管道：拒绝规则 → 模式检查 → 允许规则 → 询问用户
    """

    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"未知模式：{mode}. 请在 {MODES} 中选择。")
        self.mode = mode  # 当前权限策略模式
        self.rules = rules or list(DEFAULT_RULES)  # 工具调用允许/拒绝规则
        self.consecutive_denials = 0  # 连续被拒绝的工具调用次数
        self.max_consecutive_denials = 3  # 触发连续拒绝提醒的次数阈值

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        返回权限决策：{"behavior": "allow"|"deny"|"ask", "reason": str}
        """
        # 步骤 0：Bash 安全性验证
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny",
                            "reason": f"Bash validator flagged: {desc}"}

        # 步骤 1：拒绝规则（始终优先检查）
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny",
                        "reason": f"Blocked by deny rule: {rule}"}

        # 步骤 2：基于模式的决策
        if self.mode == "plan":
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny",
                        "reason": "Plan mode: write operations are blocked"}
            return {"behavior": "allow",
                    "reason": "Plan mode: read-only allowed"}

        if self.mode == "auto":
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {"behavior": "allow",
                        "reason": "Auto mode: read-only tool auto-approved"}
            # 继续检查允许规则

        # 步骤 3：允许规则
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {"behavior": "allow",
                        "reason": f"Matched allow rule: {rule}"}

        # 步骤 4：询问用户
        return {"behavior": "ask",
                "reason": f"No rule matched for {tool_name}, asking user"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """交互式审批提示。若获批准则返回 True。"""
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n[Permission] {tool_name}: {preview}")
        try:
            answer = input("Allow this tool call? [y/N/always]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer == "always":
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"  [{self.consecutive_denials} 连续拒绝——考虑切换到 plan mode]")
        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        """检查规则是否匹配该工具调用。"""
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False
        return True
