"""
code_demo/core/event/hooks.py — Hook 生命周期管理器

从 .hooks.json 配置文件加载并执行 Hook，支持阻断和消息注入。
来源：19_mcp-plugin.py L2195–L2308 (HookManager)
"""

import json
import os
import subprocess
from pathlib import Path
from ..utils.shell import run_shell_command

from ..config import WORKDIR, HOOK_EVENTS, HOOK_TIMEOUT, TRUST_MARKER


class HookManager:
    """
    从 .hooks.json 配置文件加载并执行 Hook。

    hook 管理器完成三项简单任务：
    - 加载 Hook 定义
    - 为指定事件运行匹配的命令
    - 为调用方聚合阻断（block）或消息（message）结果
    """

    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {event: [] for event in HOOK_EVENTS}  # 按事件类型登记的 Hook 列表
        self._sdk_mode = sdk_mode  # 是否以 SDK 模式跳过工作区信任检查
        config_path = config_path or WORKDIR / ".hooks.json"
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[Hooks] 已加载 {config_path}")
            except Exception as e:
                print(f"[Hooks] 配置错误: {e}")

    def _check_workspace_trust(self) -> bool:
        """
        检查当前工作区是否可信。

        教学版本使用一个简单的信任标记文件。
        在 SDK 模式下，信任被视为隐式成立。
        """
        if self._sdk_mode:
            return True
        return TRUST_MARKER.exists()

    def run_hooks(self, event: str, context: dict = None) -> dict:
        """
        执行某个事件的所有钩子。

        返回值：{"blocked": bool, "messages": list[str]}
        - blocked: 若任意 hook 返回退出码 1，则为 True
        - messages: 退出码为 2 的钩子输出的 stderr 内容（用于注入消息）
        """
        result = {"blocked": False, "messages": []}

        # 信任门控：拒绝在不受信任的工作区中运行 hook
        if not self._check_workspace_trust():
            return result

        hooks = self.hooks.get(event, [])

        for hook_def in hooks:
            # 检查匹配器（用于 PreToolUse/PostToolUse 的工具名称过滤器）
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            # 构建包含钩子上下文的环境
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False
                )[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(context["tool_output"])[:10000]

            try:
                r = run_shell_command(
                    command, cwd=WORKDIR, env=env,
                    capture_output=True, text=True, timeout=HOOK_TIMEOUT,
                )

                if r.returncode == 0:
                    # 静默继续
                    if r.stdout.strip():
                        print(f"    [hook:{event}] {r.stdout.strip()[:100]}")

                    # 可选的结构化标准输出解析
                    try:
                        hook_output = json.loads(r.stdout)
                        if "updatedInput" in hook_output and context:
                            context["tool_input"] = hook_output["updatedInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = hook_output["permissionDecision"]
                    except (json.JSONDecodeError, TypeError):
                        pass  # stdout 不是 JSON 格式——正常

                elif r.returncode == 1:
                    # 退出码为 1 的钩子被视作阻止
                    result["blocked"] = True
                    reason = r.stderr.strip() or "Blocked by hook"
                    result["block_reason"] = reason
                    print(f"    [hook:{event}] BLOCKED: {reason[:100]}")

                elif r.returncode == 2:
                    # 注入消息
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"    [hook:{event}] INJECT: {msg[:200]}")

            except subprocess.TimeoutExpired:
                print(f"    [hook:{event}] TIMEOUT ({HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"    [hook:{event}] ERROR: {e}")

        return result
