"""上下文预算分配器 — 动态分配 LLM 上下文窗口。

移植自 llm_wiki/src/lib/context-budget.ts，适配 code_demo 的 RAG 子系统。

设计原则：
  1. 5% 给索引（标题+摘要列表）
  2. 50% 给页面内容
  3. 15% 保留给回复生成
  4. 每个页面最多占 30% 的页面预算（防止单页垄断）
  5. 每个页面最少 5000 tokens（保证有用内容）

用法：
    from code_demo.core.utils.context_budget import (
        compute_context_budget, ContextBudget, BudgetAllocation,
    )

    budget = compute_context_budget(max_context=128000)
    # budget.index_tokens, budget.page_tokens, budget.response_tokens
    # budget.per_page_cap, budget.per_page_floor
"""
from __future__ import annotations

from dataclasses import dataclass


# ── 默认常量（移植自 context-budget.ts）──────────────────────────────────────

DEFAULT_MAX_CTX = 204800      # 默认最大上下文 token 数
RESPONSE_RESERVE_FRAC = 0.15  # 保留给回复生成的比例
INDEX_BUDGET_FRAC = 0.05      # 分配给索引（标题列表）的比例
PAGE_BUDGET_FRAC = 0.50       # 分配给页面内容的比例
PER_PAGE_FRAC = 0.30          # 每个页面最多占页面预算的比例
PER_PAGE_FLOOR = 5000         # 每个页面最少分配的 token 数


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class BudgetAllocation:
    """单个条目的预算分配。"""
    item_id: str  # 分配预算的条目 ID
    title: str  # 条目标题
    allocated_tokens: int  # 分配给条目的 token 数
    priority: float = 0.0  # 排序/相关性优先级


@dataclass
class ContextBudget:
    """上下文预算配置。

    移植自 context-budget.ts ContextBudget interface。
    """
    max_context: int            # 最大上下文 token 数
    index_tokens: int           # 索引预算（标题列表）
    page_tokens: int            # 页面内容总预算
    response_tokens: int        # 回复保留预算
    per_page_cap: int           # 每个页面的 token 上限
    per_page_floor: int         # 每个页面的 token 下限
    available_for_pages: int    # 实际可用于页面内容的 token 数


# ── 核心函数 ──────────────────────────────────────────────────────────────────

def compute_context_budget(
    max_context: int = DEFAULT_MAX_CTX,
    num_pages: int = 0,
    response_reserve_frac: float = RESPONSE_RESERVE_FRAC,
    index_budget_frac: float = INDEX_BUDGET_FRAC,
    page_budget_frac: float = PAGE_BUDGET_FRAC,
    per_page_frac: float = PER_PAGE_FRAC,
    per_page_floor: int = PER_PAGE_FLOOR,
) -> ContextBudget:
    """计算上下文预算分配。

    移植自 context-budget.ts computeContextBudget()。

    Args:
        max_context: 最大上下文 token 数（如 128000 或 204800）
        num_pages: 预计检索的页面数（用于计算 per_page_cap）
        response_reserve_frac: 保留给回复生成的比例（默认 15%）
        index_budget_frac: 分配给索引的比例（默认 5%）
        page_budget_frac: 分配给页面内容的比例（默认 50%）
        per_page_frac: 每个页面最多占页面预算的比例（默认 30%）
        per_page_floor: 每个页面最少分配的 token 数（默认 5000）

    Returns:
        ContextBudget 包含各部分的 token 预算
    """
    response_tokens = int(max_context * response_reserve_frac)
    index_tokens = int(max_context * index_budget_frac)
    page_tokens = int(max_context * page_budget_frac)

    # 每个页面上限 = 页面总预算 × 30%，但不低于 floor
    per_page_cap = max(
        int(page_tokens * per_page_frac),
        per_page_floor,
    )

    # 可用预算 = 页面总预算（减去索引和回复后的剩余）
    available_for_pages = page_tokens

    return ContextBudget(
        max_context=max_context,
        index_tokens=index_tokens,
        page_tokens=page_tokens,
        response_tokens=response_tokens,
        per_page_cap=per_page_cap,
        per_page_floor=per_page_floor,
        available_for_pages=available_for_pages,
    )


# ── 页面分配 ──────────────────────────────────────────────────────────────────

def allocate_page_tokens(
    budget: ContextBudget,
    pages: list[dict],
    priority_key: str = "priority",
    char_to_token_ratio: float = 4.0,
) -> list[BudgetAllocation]:
    """将页面预算分配给各页面。

    按优先级降序分配，每个页面不超过 per_page_cap。

    Args:
        budget: 上下文预算配置
        pages: 页面列表，每项需包含 id、title 和 priority_key 指定的优先级字段
        priority_key: 优先级字段名
        char_to_token_ratio: 字符到 token 的换算比率（中文约 1.5，英文约 4.0）

    Returns:
        BudgetAllocation 列表，按优先级降序，总 token 不超过 page_tokens
    """
    if not pages:
        return []

    # 按优先级排序
    sorted_pages = sorted(pages, key=lambda p: p.get(priority_key, 0), reverse=True)

    remaining = budget.available_for_pages
    allocations: list[BudgetAllocation] = []

    for page in sorted_pages:
        if remaining <= 0:
            break

        page_id = page.get("id", "")
        title = page.get("title", "")
        priority = page.get(priority_key, 0.0)

        # 估算页面 token 数
        content = page.get("content", "")
        estimated_tokens = max(
            int(len(content) / char_to_token_ratio),
            budget.per_page_floor,
        )

        # 不超过 per_page_cap 和剩余预算
        allocated = min(estimated_tokens, budget.per_page_cap, remaining)

        allocations.append(BudgetAllocation(
            item_id=page_id,
            title=title,
            allocated_tokens=allocated,
            priority=priority,
        ))
        remaining -= allocated

    return allocations


def truncate_page_content(
    content: str,
    max_tokens: int,
    char_to_token_ratio: float = 4.0,
) -> str:
    """将页面内容截断到指定 token 预算。

    尝试在句子/段落边界截断，避免断句。

    Args:
        content: 页面内容
        max_tokens: 最大 token 数
        char_to_token_ratio: 字符到 token 的换算比率

    Returns:
        截断后的内容
    """
    max_chars = int(max_tokens * char_to_token_ratio)

    if len(content) <= max_chars:
        return content

    truncated = content[:max_chars]

    # 尝试在最后一个段落/句子边界截断
    last_para = truncated.rfind("\n\n")
    if last_para > max_chars * 0.7:
        return truncated[:last_para]

    last_sentence = max(
        truncated.rfind("。"),
        truncated.rfind("."),
        truncated.rfind("\n"),
        truncated.rfind("！"),
        truncated.rfind("？"),
    )
    if last_sentence > max_chars * 0.7:
        return truncated[:last_sentence + 1]

    return truncated


class ContextCompressor:
    """上下文压缩器 — 将长文本压缩到 token 预算内。

    封装 truncate_page_content 与 compute_context_budget，
    供三层记忆管理器使用。
    """

    def __init__(self, max_context: int = DEFAULT_MAX_CTX):
        self.max_context = max_context  # 模型允许使用的最大上下文长度
        self.budget = compute_context_budget(max_context)  # 按上下文长度计算出的分区预算

    def compress(self, content: str, max_tokens: int = None,
                 char_to_token_ratio: float = 4.0) -> str:
        """将内容压缩到指定 token 预算内。

        Args:
            content: 待压缩的文本内容
            max_tokens: 最大 token 数（默认使用 per_page_cap）
            char_to_token_ratio: 字符到 token 的换算比率

        Returns:
            截断后的内容，尽量在句子/段落边界截断
        """
        if max_tokens is None:
            max_tokens = self.budget.per_page_cap
        return truncate_page_content(content, max_tokens, char_to_token_ratio)

    def compress_entries(self, entries: list[str], total_budget: int = None,
                         char_to_token_ratio: float = 4.0) -> list[str]:
        """将多个文本条目压缩到总预算内，均分 token。

        Args:
            entries: 待压缩的文本列表
            total_budget: 总 token 预算（默认使用 page_tokens）
            char_to_token_ratio: 字符到 token 的换算比率

        Returns:
            截断后的文本列表
        """
        if total_budget is None:
            total_budget = self.budget.page_tokens

        per_entry = max(
            total_budget // max(len(entries), 1),
            self.budget.per_page_floor,
        )
        return [
            self.compress(e, per_entry, char_to_token_ratio)
            for e in entries
        ]


def build_context_index(
    pages: list[dict],
    max_tokens: int,
    char_to_token_ratio: float = 4.0,
) -> str:
    """构建索引文本（标题 + 摘要列表），用于上下文预算的 index_tokens 部分。

    Args:
        pages: 页面列表，每项需包含 title 和可选的 summary
        max_tokens: 索引文本的最大 token 数
        char_to_token_ratio: 字符到 token 的换算比率

    Returns:
        格式化的索引文本
    """
    max_chars = int(max_tokens * char_to_token_ratio)
    lines: list[str] = []
    current_chars = 0

    for page in pages:
        title = page.get("title", "")
        summary = page.get("summary", "")[:100]
        line = f"- {title}"
        if summary:
            line += f": {summary}"

        if current_chars + len(line) > max_chars:
            lines.append(f"  ... 还有 {len(pages) - len(lines)} 个结果")
            break

        lines.append(line)
        current_chars += len(line)

    return "\n".join(lines)
