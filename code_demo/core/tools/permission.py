"""
code-demo/core/tools/permission.py — CapabilityPermissionGate

原生工具与外部能力共享权限门控。
MCP 不应绕过控制平面。原生工具和 MCP 工具首先都会被标准化为统一的能力意图
（capability intents），然后通过相同的允许/询问策略进行管控。

权限模式：
  - default: 写操作需要确认，读操作放行
  - plan:    只读模式，禁止所有写操作
  - auto:    自动批准非高风险操作

审批方式：
  - WebSocket 模式: 通过 set_ask_callback() 注入回调，前端弹窗审批
  - CLI 模式:       回退到终端 input()
  - "always" 选项:  用户可选择始终允许该工具

来源：19_mcp-plugin.py L62–L151, _extract_constants.py
"""

import json
from code_demo.core.config import PERMISSION_MODES


class CapabilityPermissionGate:
    """
    原生工具与外部能力共享权限门控。
    """
    READ_PREFIXES = ("read", "list", "get", "show", "search", "query", "inspect")
    HIGH_RISK_PREFIXES = ("delete", "remove", "drop", "shutdown")

    # 危险命令模式（增强版）
    # 注意：curl / wget 本身（联网只读获取网页）已降级为 write 风险，
    #       仅当管道到 shell 执行（curl ... | sh / | bash）时才判定为 high。
    DANGEROUS_PATTERNS = (
        # 传统危险命令
        "rm -rf", "sudo", "shutdown", "reboot",
        # 远程代码执行（管道到 shell 才危险，curl/wget 单独获取网页不算）
        "| sh", "| bash", "|sh", "|bash",
        # 权限与系统操作
        "chmod 777", "mkfs", "dd if=",
        # 动态执行
        "eval", "exec",
    )

    # 本地浏览器 GUI 调用模式（high risk，需用户确认）
    # 这些会拉起本地图形界面浏览器/应用，脱离沙箱的只读网络访问语义，
    # 因此即使在 auto 模式下也必须先获得用户确认。
    LOCAL_BROWSER_PATTERNS = (
        "open -a",                 # macOS 打开应用
        "open http",               # macOS 用默认浏览器打开 URL
        "open https",
        "webbrowser",              # Python webbrowser 模块
        "google chrome",           # 启动 Chrome
        "google-chrome",
        "chromium",                # 启动 Chromium
        "safari",                  # 启动 Safari
        "--remote-debugging-port",  # CDP 本地调试连接特征
    )

    def __init__(self, mode: str = "default", ask_callback=None):
        self.mode = mode if mode in PERMISSION_MODES else "default"  # 当前工具权限模式
        self._ask_callback = ask_callback  # 可选 WebSocket 审批回调

    def set_ask_callback(self, callback) -> None:
        """设置权限审批回调（server.py 注入 WebSocket 审批函数）。"""
        self._ask_callback = callback

    def set_mode(self, mode: str) -> None:
        """运行时切换权限模式。"""
        if mode in PERMISSION_MODES:
            self.mode = mode

    def normalize(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name.startswith("mcp__"):
            _, server_name, actual_tool = tool_name.split("__", 2)
            source = "mcp"
        else:
            server_name = None
            actual_tool = tool_name
            source = "native"

        lowered = actual_tool.lower()
        # 桌面工具：操作类 high risk，观察类 write（auto 模式自动放行）
        if actual_tool == "desktop_control":
            risk = "high"  # 所有桌面操作需确认
        elif actual_tool == "screen_observe":
            risk = "write"  # 只读感知，auto 模式自动放行
        elif actual_tool == "read_file" or lowered.startswith(self.READ_PREFIXES):
            risk = "read"
        elif actual_tool == "bash":
            command = tool_input.get("command", "")
            lowered_cmd = command.lower()
            # 本地浏览器 GUI 调用 → high（需确认）
            if any(token in lowered_cmd for token in self.LOCAL_BROWSER_PATTERNS):
                risk = "high"
            # 传统危险命令 / 管道到 shell 执行 → high
            elif any(token in command for token in self.DANGEROUS_PATTERNS):
                risk = "high"
            # 其余（含单纯 curl/wget 联网读取）→ write
            else:
                risk = "write"
        elif actual_tool == "delete_notes":
            risk = "write"  # 笔记删除仅限 .cognitive_notes/ 目录，自带安全边界，auto 模式放行
        elif lowered.startswith(self.HIGH_RISK_PREFIXES):
            risk = "high"
        else:
            risk = "write"

        return {
            "source": source,
            "server": server_name,
            "tool": actual_tool,
            "risk": risk,
        }

    def check(self, tool_name: str, tool_input: dict) -> dict:
        intent = self.normalize(tool_name, tool_input)

        # 读操作始终放行
        if intent["risk"] == "read":
            return {"behavior": "allow", "reason": "Read capability", "intent": intent}

        # Plan 模式：禁止所有写操作
        if self.mode == "plan" and intent["risk"] in ("write", "high"):
            return {
                "behavior": "deny",
                "reason": "Plan 模式: 不允许修改操作，仅可读取和分析",
                "intent": intent,
            }

        # auto 模式下所有风险等级均自动放行（用户明确要求不受管控）
        if self.mode == "auto":
            return {
                "behavior": "allow",
                "reason": "Auto mode - unrestricted",
                "intent": intent,
            }

        # high 风险操作需要手动确认（default/plan 模式）
        if intent["risk"] == "high":
            return {
                "behavior": "ask",
                "reason": "High-risk capability 需要手动确认",
                "intent": intent,
            }

        # default 模式默认 ask
        return {
            "behavior": "ask",
            "reason": "State-changing capability 需要手动确认",
            "intent": intent,
        }

    def ask_user(self, intent: dict, tool_input: dict) -> dict:
        """请求用户审批。

        Returns:
            {"approved": bool, "action": "approve"|"deny"|"always", "reason": str}
        """
        if self._ask_callback:
            return self._ask_callback(intent, tool_input)

        # CLI 回退：终端 input()
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        source = (
            f"{intent['source']}:{intent['server']}/{intent['tool']}"
            if intent.get("server")
            else f"{intent['source']}/{intent['tool']}"
        )
        print(f"\n [Permission] {source} risk={intent['risk']}: {preview}")
        try:
            answer = input(" 是否允许？ (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return {"approved": False, "action": "deny", "reason": "用户取消"}
        if answer in ("always", "a"):
            return {"approved": True, "action": "always", "reason": ""}
        approved = answer in ("y", "yes")
        action = "approve" if approved else "deny"
        return {"approved": approved, "action": action, "reason": ""}


permission_gate = CapabilityPermissionGate()
