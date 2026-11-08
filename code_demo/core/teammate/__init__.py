"""
code-demo/core/teammate — 队友管理与协作

包含：
- functions — 任务扫描与认领
- manager — 队友注册表与工作循环
"""

from code_demo.core.teammate.functions import (
    _append_claim_event,
    _task_allows_role,
    is_claimable_task,
    scan_unclaimed_tasks,
    claim_task,
)
from code_demo.core.teammate.manager import TeammateManager

__all__ = [
    "_append_claim_event",
    "_task_allows_role",
    "is_claimable_task",
    "scan_unclaimed_tasks",
    "claim_task",
    "TeammateManager",
]