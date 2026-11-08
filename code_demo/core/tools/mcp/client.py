"""
code-demo/core/tools/mcp/client.py — MCPClient
基于 stdio 的极简 MCP 客户端。
来源：19_mcp-plugin.py L276–L374
"""

import json
import os
import subprocess
import time
from queue import Empty, Queue
import threading


# 默认接收超时（秒）
RECV_TIMEOUT = 30.0
# 工具调用超时（秒）— 网页抓取等操作可能较慢
CALL_TIMEOUT = 120.0


class MCPClient:
    """基于 stdio 的极简 MCP 客户端。"""

    def __init__(self, server_name: str, command: str, args: list = None, env: dict = None, timeout: float = RECV_TIMEOUT):
        self.server_name = server_name  # MCP 服务名称
        self.command = command  # 启动 MCP 服务的可执行命令
        self.args = args or []  # 启动 MCP 服务时传入的参数
        self.env = {**os.environ, **(env or {})}  # MCP 子进程环境变量
        self.timeout = timeout  # 等待 MCP 响应的默认超时时间
        self.process = None  # 当前 MCP 子进程
        self._request_id = 0  # JSON-RPC 请求序号
        self._tools = []  # 该 MCP 服务公布的工具定义
        self._stdout_lines = Queue()  # 后台读取到的 MCP 标准输出队列
        self._stdout_reader = None  # 读取 MCP 子进程输出的后台线程

    def connect(self):
        """启动 MCP 服务器进程。"""
        try:
            self._stdout_lines = Queue()
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
            )
            self._stdout_reader = threading.Thread(
                target=self._read_stdout, args=(self.process,), daemon=True,
                name=f"mcp-stdout-{self.server_name}",
            )
            self._stdout_reader.start()
            self._send({
                "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "teaching-agent", "version": "1.0"}
                }
            })
            response = self._recv()
            if response and "result" in response:
                # JSON-RPC 通知：不带 id，服务器不回复
                self._send_notification({"method": "notifications/initialized"})
                time.sleep(0.5)  # 等待服务器完成初始化
                return True
        except FileNotFoundError:
            print(f"[MCP] Server command not found: {self.command}")
        except Exception as e:
            print(f"[MCP] Connection failed: {e}")
        return False

    def _read_stdout(self, process):
        """在后台读取 stdout，避免依赖 Windows 不支持的 select(pipe)。"""
        if process is None or process.stdout is None:
            self._stdout_lines.put(None)
            return
        try:
            for line in iter(process.stdout.readline, ""):
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def list_tools(self) -> list:
        """从服务器获取可用工具列表"""
        self._send({"method": "tools/list"})
        response = self._recv()
        if response and "result" in response:
            self._tools = response["result"].get("tools", [])
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """在服务器上执行一个工具。"""
        # 检查进程是否存活
        if not self.process or self.process.poll() is not None:
            return f"MCP Error: {self.server_name} 进程已退出"

        self._send({"method": "tools/call", "params": {
            "name": tool_name,
            "arguments": arguments,
        }})
        response = self._recv(timeout=CALL_TIMEOUT)
        if response and "result" in response:
            content = response["result"].get("content", [])
            return "\n".join(c.get("text", str(c)) for c in content)
        if response and "error" in response:
            return f"MCP Error: {response['error'].get('message', 'unknown')}"
        return f"MCP Error: {tool_name} 响应超时（{int(CALL_TIMEOUT)}s）或进程已退出"

    def get_agent_tools(self) -> list:
        """将 MCP 工具转换为代理工具格式。前缀：mcp__{server_name}__{tool_name}"""
        agent_tools = []
        for tool in self._tools:
            prefixed_name = f"mcp__{self.server_name}__{tool['name']}"
            # MCP 协议用 inputSchema，需转为 OpenAI parameters 格式
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
        """关闭服务器进程"""
        if self.process:
            try:
                self._send({"method": "shutdown"})
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None

    def _recv(self, timeout: float = None) -> dict | None:
        if timeout is None:
            timeout = self.timeout
        if not self.process or self.process.poll() is not None:
            return None
        try:
            # Queue.get(timeout=...) 对 Windows 和 POSIX 子进程管道都可用。
            line = self._stdout_lines.get(timeout=timeout)
            if line:
                return json.loads(line)
        except Empty:
            print(f"[MCP] 接收超时（{timeout}秒）")
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return None

    def _send(self, message: dict):
        """发送 JSON-RPC 请求（带 id，期望响应）。"""
        if not self.process or self.process.poll() is not None:
            return
        self._request_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._request_id, **message}
        line = json.dumps(envelope, ensure_ascii=False) + "\n"
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _send_notification(self, message: dict):
        """发送 JSON-RPC 通知（不带 id，不期望响应）。"""
        if not self.process or self.process.poll() is not None:
            return
        envelope = {"jsonrpc": "2.0", **message}
        line = json.dumps(envelope, ensure_ascii=False) + "\n"
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
