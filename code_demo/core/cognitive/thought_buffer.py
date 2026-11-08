"""
core/cognitive/thought_buffer.py — 分层思维缓冲

近期全量保留 + 远期 LLM 摘要压缩，为认知循环提供思维上下文。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Thought:
    """单次认知循环产物"""

    seq: int                          # 序号（自增）
    content: str                      # 想法文本
    next_action: str                  # "continue" | "pause" | "externalize" | "memorize" | "speak" | "act"
    focus_shift: str                  # "deepen" | "diverge" | "idle"
    confidence: float = 0.5           # 置信度 0-1
    memo_tag: str = ""                # 若 memorize，写入哪个簇
    intent: str = ""                  # act 动作的执行意图
    timestamp: float = field(default_factory=time.time)  # 思考记录的创建时间


class ThoughtBuffer:
    """分层思维缓冲：近期全量 + 远期 LLM 摘要压缩。"""

    def __init__(self, full_window: int = 7):
        self.recent: list[Thought] = []  # 近期未压缩的思考记录
        self.summary: str = ""  # 已压缩思考内容的摘要
        self.total_thoughts: int = 0  # 累计写入缓冲区的思考条数
        self._full_window = full_window  # 触发窗口压缩前保留的思考条数
        self._pending_overflow: list[Thought] = []  # 待压缩的溢出想法

    def append(self, thought: Thought):
        """追加新想法，溢出时移入待压缩队列"""
        self.recent.append(thought)
        self.total_thoughts += 1
        if len(self.recent) > self._full_window:
            overflow = self.recent.pop(0)
            self._pending_overflow.append(overflow)

    def get_context_for_next_thought(self) -> str:
        """构建叙事式思维上下文"""
        if not self.recent:
            return ""

        parts = []
        if self.summary:
            parts.append(f"【更早的思绪】{self.summary}")

        # 近期想法用因果连接展示
        parts.append("【近期思维脉络】")
        for i, t in enumerate(self.recent):
            if i == 0:
                prefix = "最初"
            elif i == 1:
                prefix = "接着"
            elif i == len(self.recent) - 1:
                prefix = "最近"
            else:
                prefix = "然后"
            action_hint = f"（→ {t.next_action}）" if t.next_action not in ("continue", "") else ""
            parts.append(f"  {prefix}：{t.content}{action_hint}")

        return "\n".join(parts)

    def has_pending_compression(self) -> bool:
        """是否有待压缩的溢出想法"""
        return len(self._pending_overflow) >= 2  # 积攒 2 条再压缩

    def get_pending_for_compression(self) -> list[Thought]:
        """取出待压缩的想法并清空队列"""
        pending = self._pending_overflow[:]
        self._pending_overflow.clear()
        return pending

    def update_summary(self, new_summary: str):
        """更新压缩摘要"""
        self.summary = new_summary

    def clear(self):
        """清空缓冲区"""
        self.recent.clear()
        self.summary = ""
        self._pending_overflow.clear()
        self.total_thoughts = 0
