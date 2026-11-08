"""
code_demo.core.event — 事件总线与 Hook 管理器

包含：
- EventBUS — 工作树事件日志
- MessageBUS — 代理间消息通信
- HookManager — 生命周期 Hook
"""

from code_demo.core.event.bus import EventBUS, MessageBUS
from code_demo.core.event.hooks import HookManager

__all__ = ["EventBUS", "MessageBUS", "HookManager"]