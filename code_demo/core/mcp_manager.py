"""
core/mcp_manager.py — MCP 连接管理器

将 MCP 服务器连接、断开、工具过滤等逻辑从 Agent 中提取为独立模块。
"""
import shutil
from code_demo.core.tools import MCPToolRouter, PluginLoader, MCPClient, MCPHttpClient, MCPConfigLoader


class MCPManager:
    """MCP 服务器连接管理，从 Agent 中提取的独立模块。"""

    def __init__(self):
        self.router = MCPToolRouter()  # 管理已连接 MCP 客户端及其工具的路由器
        self.errors: dict[str, str] = {}  # {server_name: error_message}
        self.disabled_tools: dict[str, set[str]] = {}  # {server_name: {tool_name, ...}}
        self._init()

    def _init(self):
        """初始化 MCP：config.json 优先，回退到 plugin 扫描。"""
        router = self.router
        self.errors.clear()

        # 优先：从 mcp_config.json 加载
        config_loader = MCPConfigLoader()
        servers = config_loader.load()
        source = "mcp_config.json" if servers else None

        # 回退：plugin 扫描
        if not servers:
            loader = PluginLoader()
            found = loader.scan()
            if found:
                print(f"[Agent] 发现 {len(found)} 个插件: {', '.join(found)}")
                servers = loader.get_mcp_servers()
                source = "plugin"

        if not servers:
            return

        # 创建客户端
        connected = 0
        for server_name, cfg in servers.items():
            ok, err = self._connect_single(server_name, cfg)
            if ok:
                connected += 1
            elif err:
                self.errors[server_name] = err

        if connected:
            print(f"[Agent] MCP 就绪: {connected} 个服务器 ({source})")

    def _connect_single(self, server_name: str, cfg: dict) -> tuple[bool, str | None]:
        """连接单个 MCP 服务器。返回 (success, error_msg)。"""
        server_type = cfg.get("type", "stdio")
        timeout = cfg.get("timeout", 30)

        try:
            if server_type in ("streamableHttp", "sse"):
                client = MCPHttpClient(
                    server_name=server_name,
                    url=cfg.get("url", ""),
                    headers=cfg.get("headers"),
                    timeout=timeout,
                )
            else:  # stdio（含 uvx, npx 等）
                command = cfg.get("command", "")
                if not command:
                    return False, "缺少 command 字段"
                client = MCPClient(
                    server_name=server_name,
                    command=command,
                    args=cfg.get("args", []),
                    env=cfg.get("env"),
                    timeout=timeout,
                )

            if client.connect():
                client.list_tools()
                self.router.register_client(client)
                tool_count = len(client._tools)
                print(f"[MCP] ✓ {server_name} ({server_type}): {tool_count} tools")
                return True, None
            else:
                # 尝试获取更详细的错误信息
                err = self._get_detailed_error(server_name, server_type, client, cfg)
                print(f"[MCP] ✗ {server_name} ({server_type}): {err}")
                return False, err

        except Exception as e:
            err = str(e)
            print(f"[MCP] ✗ {server_name} ({server_type}): {err}")
            return False, err

    def _get_detailed_error(self, server_name: str, server_type: str, client, cfg: dict) -> str:
        """获取更详细的连接错误信息。"""
        if server_type in ("streamableHttp", "sse"):
            url = cfg.get("url", "未知")
            return f"HTTP 连接失败: {url}"
        else:
            command = cfg.get("command", "")
            args = cfg.get("args", [])
            cmd_str = f"{command} {' '.join(args)}".strip()

            # 尝试从 stderr 获取更多信息
            if hasattr(client, 'process') and client.process and client.process.stderr:
                try:
                    stderr_output = client.process.stderr.read(500)
                    if stderr_output:
                        # 取第一行错误
                        first_line = stderr_output.split('\n')[0].strip()
                        if first_line:
                            return f"{cmd_str}: {first_line[:100]}"
                except Exception:
                    pass

            # 检查命令是否存在
            if not shutil.which(command):
                return f"命令未找到: {command}"

            return f"初始化失败: {cmd_str}"

    def connect_server(self, name: str, cfg: dict) -> tuple[bool, str | None]:
        """运行时连接单个 MCP 服务器。返回 (success, error_msg)。"""
        # 先断开已有连接
        if name in self.router.clients:
            self.disconnect_server(name)

        ok, err = self._connect_single(name, cfg)
        if ok:
            self.errors.pop(name, None)
            self.disabled_tools.pop(name, None)  # 新连接默认全部启用
        else:
            self.errors[name] = err or "未知错误"
        return ok, err

    def disconnect_server(self, name: str):
        """运行时断开单个 MCP 服务器。"""
        client = self.router.clients.pop(name, None)
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        self.errors.pop(name, None)
        self.disabled_tools.pop(name, None)  # 清理禁用记录
        print(f"[MCP] 已断开: {name}")

    def toggle_tool(self, server_name: str, tool_name: str, enable: bool):
        """启用/禁用单个 MCP 工具。"""
        if server_name not in self.disabled_tools:
            self.disabled_tools[server_name] = set()
        if enable:
            self.disabled_tools[server_name].discard(tool_name)
        else:
            self.disabled_tools[server_name].add(tool_name)
        # 清理空集合
        if not self.disabled_tools[server_name]:
            del self.disabled_tools[server_name]

    def get_server_tools(self, server_name: str) -> list:
        """获取某服务器所有工具及启用状态。"""
        client = self.router.clients.get(server_name)
        if not client:
            return []
        disabled = self.disabled_tools.get(server_name, set())
        result = []
        for tool in client._tools:
            result.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "enabled": tool["name"] not in disabled,
            })
        return result

    def get_tool_defs(self) -> list[dict]:
        """返回过滤后的 MCP 工具定义列表（排除 disabled_tools 中的工具）。"""
        tools = []
        for tool in self.router.get_all_tools():
            parts = tool["name"].split("__", 2)
            if len(parts) == 3:
                server, actual = parts[1], parts[2]
                if actual in self.disabled_tools.get(server, set()):
                    continue
            tools.append(tool)
        return tools

    def disconnect_all(self):
        """断开所有 MCP 连接（用于 Agent.stop()）。"""
        for client in self.router.clients.values():
            try:
                client.disconnect()
            except Exception:
                pass
