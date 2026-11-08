"""
code_demo/core/tools/mcp/http_client.py — Streamable HTTP MCP 客户端

基于 HTTP POST + SSE 的 MCP 客户端。
使用标准库 urllib.request，无额外依赖。

传输流程:
  POST {url}  Content-Type: application/json
    → {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}

  响应 A: Content-Type: application/json  → 直接解析
  响应 B: Content-Type: text/event-stream → 解析 SSE data: 行
"""

import json
import urllib.request
import urllib.error


class MCPHttpClient:
    """基于 Streamable HTTP 的 MCP 客户端。"""

    def __init__(self, server_name: str, url: str, headers: dict = None, timeout: int = 30):
        self.server_name = server_name  # MCP 服务名称
        self.url = url  # MCP HTTP 服务地址
        self.headers = headers or {}  # 发送请求时使用的 HTTP 请求头
        self.timeout = timeout  # 等待 HTTP 响应的超时时间
        self._request_id = 0  # JSON-RPC 请求序号
        self._tools = []  # 该 MCP 服务公布的工具定义

    def connect(self) -> bool:
        """发送 initialize + notifications/initialized，完成握手。"""
        try:
            # Step 1: initialize
            response = self._post({
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "teaching-agent", "version": "1.0"},
                },
            })
            if not response or "result" not in response:
                print(f"[MCP HTTP] {self.server_name}: initialize 失败")
                return False

            # Step 2: notifications/initialized（无响应）
            self._post({
                "method": "notifications/initialized",
                "params": {},
            }, expect_response=False)

            return True
        except Exception as e:
            print(f"[MCP HTTP] {self.server_name}: 连接失败 — {e}")
            return False

    def list_tools(self) -> list:
        """从服务器获取可用工具列表。"""
        response = self._post({"method": "tools/list", "params": {}})
        if response and "result" in response:
            self._tools = response["result"].get("tools", [])
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """在服务器上执行一个工具。"""
        response = self._post({
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }, timeout=120)
        if response and "result" in response:
            content = response["result"].get("content", [])
            return "\n".join(c.get("text", str(c)) for c in content)
        if response and "error" in response:
            return f"MCP Error: {response['error'].get('message', 'unknown')}"
        return f"MCP Error: {tool_name} 响应超时或网络错误"

    def get_agent_tools(self) -> list:
        """将 MCP 工具转换为代理工具格式。前缀：mcp__{server_name}__{tool_name}"""
        agent_tools = []
        for tool in self._tools:
            prefixed_name = f"mcp__{self.server_name}__{tool['name']}"
            params = tool.get("inputSchema", tool.get("function", {"type": "object", "properties": {}}))
            agent_tools.append({
                "name": prefixed_name,
                "description": tool.get("description", ""),
                "parameters": params,
                "_mcp_server": self.server_name,
                "_mcp_tool": tool["name"],
            })
        return agent_tools

    def disconnect(self):
        """HTTP 无状态，仅清理本地状态。"""
        self._tools = []

    # ── 内部方法 ──

    def _post(self, message: dict, expect_response: bool = True, timeout: int = None) -> dict | None:
        """
        发送 JSON-RPC 请求。
        expect_response=False 时用于通知类消息（不等待响应）。
        timeout 覆盖默认超时（用于工具调用等耗时操作）。
        """
        self._request_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._request_id, **message}

        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **self.headers,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                if not expect_response:
                    return None

                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8")

                if "text/event-stream" in content_type:
                    return self._parse_sse(raw)
                elif raw.strip():
                    return json.loads(raw)
        except urllib.error.HTTPError as e:
            print(f"[MCP HTTP] {self.server_name}: HTTP {e.code} — {e.reason}")
        except urllib.error.URLError as e:
            print(f"[MCP HTTP] {self.server_name}: 网络错误 — {e.reason}")
        except (json.JSONDecodeError, OSError) as e:
            print(f"[MCP HTTP] {self.server_name}: 解析错误 — {e}")

        return None

    def _parse_sse(self, raw: str) -> dict | None:
        """
        从 SSE 文本中提取最后一个 data: JSON 对象。
        SSE 格式: event: message\ndata: {...}\n\n
        """
        last_data = None
        for line in raw.splitlines():
            if line.startswith("data: "):
                last_data = line[6:].strip()

        if last_data:
            try:
                return json.loads(last_data)
            except json.JSONDecodeError:
                pass
        return None
