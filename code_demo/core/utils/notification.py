"""
code_demo/core/utils/notification.py — 优先级通知队列

支持相同键的折叠（folding），避免上下文被过时更新淹没。
来源：19_mcp-plugin.py L1349–L1375 (NotificationQueue)
"""

import threading


class NotificationQueue:
    """
    基于优先级的通知队列，支持相同键的折叠（folding）。

    折叠意味着新消息可以替换具有相同键的旧消息，
    从而避免上下文被过时的更新信息淹没。
    """

    PRIORITIES = {"immediate": 0, "high": 1, "medium": 2, "low": 3}

    def __init__(self):
        self._queue = []  # list of (priority, key, message)
        self._lock = threading.Lock()  # 保护通知队列并发访问的线程锁

    def push(self, message: str, priority: str = "medium", key: str = None):
        """向队列添加一条消息，若键与现有条目匹配则进行折叠。"""
        with self._lock:
            if key:
                # Fold: 用相同键的新消息替换现有消息
                self._queue = [(p, k, m) for p, k, m in self._queue if k != key]
            self._queue.append((self.PRIORITIES.get(priority, 2), key, message))
            self._queue.sort(key=lambda x: x[0])

    def drain(self) -> list:
        """按优先级顺序返回所有待处理消息，并清空队列。"""
        with self._lock:
            messages = [m for _, _, m in self._queue]
            self._queue.clear()
            return messages

    def peek(self) -> list:
        """按优先级顺序返回所有待处理消息，不清空队列。"""
        with self._lock:
            return [m for _, _, m in self._queue]

    def __len__(self):
        with self._lock:
            return len(self._queue)
