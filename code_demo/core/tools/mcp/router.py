"""
code-demo/core/tools/mcp/router.py — MCPToolRouter
将工具调用路由到正确的 MCP 服务器。
来源：19_mcp-plugin.py L432–L470
"""


class MCPToolRouter:
    """将工具调用路由到正确的 MCP 服务器。前缀：mcp__{server}__{tool}"""

    def __init__(self):
        self.clients = {}  # 按服务名称索引的 MCP 客户端集合

    def register_client(self, client):
        """注册 MCP 客户端（stdio 或 HTTP）。使用鸭子类型检测。"""
        if hasattr(client, 'server_name') and hasattr(client, 'call_tool'):
            self.clients[client.server_name] = client

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp__")

    def call(self, tool_name: str, arguments: dict) -> str:
        """将 MCP 工具调用路由到正确的服务器。"""
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return f"Error: Invalid MCP tool name: {tool_name}"
        _, server_name, actual_tool = parts
        client = self.clients.get(server_name)
        if not client:
            return f"Error: MCP server not found: {server_name}"
        return client.call_tool(actual_tool, arguments)

    def get_all_tools(self) -> list:
        """收集所有已连接 MCP 服务器的工具。"""
        tools = []
        for client in self.clients.values():
            tools.extend(client.get_agent_tools())
        return tools
