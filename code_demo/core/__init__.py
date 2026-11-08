"""
code-demo/core — AI 编码代理核心模块

模块边界：
  config    — 全局常量
  event     — EventBus / MessageBus / HookManager
  tools     — MCP 客户端/加载器/路由器/权限管理
  protocol  — RequestStore 协议存储
  teammate  — 任务扫描与认领 / TeammateManager
  utils     — safe_path / persist / SkillRegistry / security / scheduler
              / memory / dream / worktree / notification / background / task_manager
"""

from code_demo.core import config
from code_demo.core import event
from code_demo.core import tools
from code_demo.core import protocol
from code_demo.core import teammate
from code_demo.core import utils


def __getattr__(name):
    """Load the main Agent lazily so config/CLI help do not import LLM SDKs."""
    if name == "Agent":
        from code_demo.core.agent import Agent
        return Agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "config",
    "event",
    "tools",
    "protocol",
    "teammate",
    "utils",
    "Agent",
]
