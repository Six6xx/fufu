"""协议工作流函数 —— 关停、计划审批、任务认领、权限门控。

注意：shutdown/gate 模块尚未从 19_mcp-plugin.py 中提取，此包为预留占位。
"""
from code_demo.core.tools.permission import CapabilityPermissionGate, permission_gate

__all__ = ["CapabilityPermissionGate", "permission_gate"]