"""
code_demo/core/tools/mcp/config_loader.py — MCP 配置加载器

从 mcp_config.json 加载 MCP 服务器配置。
支持 stdio（含 uvx/npx）和 streamableHttp/sse 两种传输类型。
支持 disabled: true 禁用特定服务器。

配置格式:
{
    "mcpServers": {
        "server_name": {
            "type": "stdio" | "streamableHttp" | "sse",
            "command": "uvx",           // stdio 类型
            "args": ["mcp-server-fetch"],
            "env": {},                  // 可选环境变量
            "url": "http://...",        // HTTP 类型
            "headers": {},              // 可选 HTTP 头
            "timeout": 60,              // 可选超时（秒）
            "disabled": true,           // 可选，禁用此服务器
            "autoApprove": []           // 预留字段
        }
    }
}
"""

import json
from pathlib import Path
from code_demo.core.config import MCP_CONFIG_FILE


class MCPConfigLoader:
    """从 mcp_config.json 加载 MCP 服务器配置。"""

    def __init__(self, config_path: Path = None):
        self.config_path = config_path or MCP_CONFIG_FILE  # MCP 服务配置文件路径
        self._raw: dict = {}  # 从配置文件载入的原始配置数据

    def load(self) -> dict:
        """
        加载配置文件并返回未禁用的服务器配置。
        返回: {server_name: {type, command, args, env, url, headers, timeout, ...}}
        如果文件不存在或解析失败，返回空字典。
        """
        if not self.config_path.exists():
            return {}

        try:
            self._raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[MCP Config] {self.config_path} 加载失败: {e}")
            return {}

        servers = {}
        for name, cfg in self._raw.get("mcpServers", {}).items():
            if cfg.get("disabled", False):
                print(f"[MCP Config] 跳过已禁用服务器: {name}")
                continue
            servers[name] = cfg

        return servers

    def list_all(self) -> dict:
        """
        返回所有服务器配置（含 disabled 状态），用于管理/展示。
        返回: {server_name: {**cfg, "_disabled": bool}}
        """
        if not self._raw:
            self.load()

        result = {}
        for name, cfg in self._raw.get("mcpServers", {}).items():
            result[name] = {**cfg, "_disabled": cfg.get("disabled", False)}
        return result

    def save(self, servers: dict):
        """将服务器配置写回 mcp_config.json（覆盖写入）。"""
        data = {"mcpServers": servers}
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._raw = data

    def add_server(self, name: str, cfg: dict):
        """添加单个服务器到配置并持久化。如已存在则覆盖。"""
        if not self._raw:
            self.load()  # 确保 _raw 已加载
        servers = self._raw.get("mcpServers", {})
        servers[name] = cfg
        self.save(servers)

    def delete_server(self, name: str) -> bool:
        """从配置中删除指定服务器并持久化。"""
        if not self._raw:
            self.load()
        servers = self._raw.get("mcpServers", {})
        if name not in servers:
            return False
        del servers[name]
        self.save(servers)
        return True
