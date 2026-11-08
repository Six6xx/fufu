"""
code-demo/core/utils — 工具函数与注册表

包含：
- safe_path / persist — 路径安全与输出持久化
- SkillRegistry — 技能注册表
- security — Bash 命令安全验证
- notification — 优先级通知队列
- task_manager — 持久化任务看板
- background — 后台任务管理
- scheduler — Cron 定时调度
- memory — 跨会话记忆管理
- dream — 记忆整合器
- worktree — Git 工作树管理
- memory_relevance — 4 信号记忆相关性评分
- context_budget — 上下文预算分配器
- agent_template — Agent 定义解析
"""

from code_demo.core.utils.safe_path import safe_path
from code_demo.core.utils.persist import persist_large_output
from code_demo.core.utils.skill_registry import SkillRegistry, SKILL_REGISTRY
from code_demo.core.utils.security import BashSecurityValidator, bash_validator
from code_demo.core.utils.notification import NotificationQueue
from code_demo.core.utils.task_manager import TaskManager
from code_demo.core.utils.background import BackgroundManager
from code_demo.core.utils.scheduler import CronLock, CronScheduler, cron_matches
from code_demo.core.utils.memory import MemoryManager
from code_demo.core.utils.dream import DreamConsolidator
from code_demo.core.utils.worktree import WorktreeManager
from code_demo.core.utils.session_manager import SessionManager
from code_demo.core.utils.provider_manager import ProviderManager
from code_demo.core.utils.agent_template import AgentTemplate, PlanItem, CompactState
from code_demo.core.utils.memory_relevance import (
    MemoryNode, MemoryGraph, build_memory_graph, calculate_relevance,
    get_related_memories, retrieve_relevant_memories,
)
from code_demo.core.utils.context_budget import (
    ContextBudget, BudgetAllocation, compute_context_budget, allocate_page_tokens,
    ContextCompressor,
)
from code_demo.core.utils.memory_manager import (
    ThreeLayerMemory, RecallResult,
)
from code_demo.core.utils.user_profile import UserProfile, ProfileFact
from code_demo.core.utils.skill_conflict import (
    SkillConflictDetector,
    SkillMerger,
    SkillLifecycleManager,
    ConflictResult,
    MergeResult,
)

__all__ = [
    "safe_path",
    "persist_large_output",
    "SkillRegistry",
    "SKILL_REGISTRY",
    "BashSecurityValidator",
    "bash_validator",
    "NotificationQueue",
    "TaskManager",
    "BackgroundManager",
    "CronLock",
    "CronScheduler",
    "cron_matches",
    "MemoryManager",
    "DreamConsolidator",
    "WorktreeManager",
    "SessionManager",
    "ProviderManager",
    "AgentTemplate",
    "PlanItem",
    "CompactState",
    "MemoryNode",
    "MemoryGraph",
    "build_memory_graph",
    "calculate_relevance",
    "get_related_memories",
    "retrieve_relevant_memories",
    "ContextBudget",
    "BudgetAllocation",
    "compute_context_budget",
    "allocate_page_tokens",
    "ContextCompressor",
    "ThreeLayerMemory",
    "RecallResult",
    "UserProfile",
    "ProfileFact",
    "SkillConflictDetector",
    "SkillMerger",
    "SkillLifecycleManager",
    "ConflictResult",
    "MergeResult",
]