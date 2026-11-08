"""
code-demo/core/tools/mcp/loader.py — PluginLoader
从 .claude-plugin/ 目录加载插件。
来源：19_mcp-plugin.py L395–L427
"""

import json
from pathlib import Path
from code_demo.core.config import WORKDIR


class PluginLoader:
    """从 .claude-plugin/ 目录加载插件。"""

    def __init__(self, search_dirs: list = None):
        self.search_dirs = search_dirs or [WORKDIR]  # 搜索 MCP 插件配置的目录列表
        self.plugins = {}  # 已加载的 MCP 插件客户端映射

    def scan(self) -> list:
        """扫描目录以查找 .claude-plugin/plugin.json 清单文件。"""
        found = []
        for search_dir in self.search_dirs:
            plugin_dir = Path(search_dir) / ".claude-plugin"
            manifest_path = plugin_dir / "plugin.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    name = manifest.get("name", plugin_dir.parent.name)
                    self.plugins[name] = manifest
                    found.append(name)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[Plugin] {manifest_path} 加载失败：{e}")
        return found

    def get_mcp_servers(self) -> dict:
        """从已加载的插件中提取 MCP 服务器配置。返回 {server_name: {command, args, env}}。"""
        servers = {}
        for plugin_name, manifest in self.plugins.items():
            for server_name, config in manifest.get("mcpServers", {}).items():
                servers[f"{plugin_name}__{server_name}"] = config
        return servers

    def load_from_config(self) -> dict:
        """从 mcp_config.json 加载服务器配置（替代 plugin 扫描）。"""
        from code_demo.core.tools.mcp.config_loader import MCPConfigLoader
        config_loader = MCPConfigLoader()
        return config_loader.load()
