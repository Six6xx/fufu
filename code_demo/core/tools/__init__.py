"""
code_demo/core/tools/ — 代理工具层

子包：
- native/ — 原生工具执行器 (bash/read/write/edit) + 权限门控
- mcp/    — MCP 协议客户端、插件加载器、路由器
- permission_manager.py — 高级权限决策管道
"""

from code_demo.core.tools.mcp import MCPClient, MCPHttpClient, PluginLoader, MCPToolRouter, MCPConfigLoader
from code_demo.core.tools.native import run_bash, run_read, run_write, run_edit
from code_demo.core.tools.permission import CapabilityPermissionGate, permission_gate
from code_demo.core.tools.permission_manager import PermissionManager

__all__ = [
    "MCPClient", "MCPHttpClient", "PluginLoader", "MCPToolRouter", "MCPConfigLoader",
    "run_bash", "run_read", "run_write", "run_edit",
    "CapabilityPermissionGate", "permission_gate",
    "PermissionManager",
]