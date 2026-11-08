"""
code_demo/core/utils/scheduler.py — Cron 定时调度器

包含 CronLock（PID 锁）和 CronScheduler（调度记录 + 后台检查 + 持久化）。
来源：19_mcp-plugin.py L1040–L1347 (CronLock + CronScheduler)
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue, Empty

from ..config import (
    CRON_LOCK_FILE, SCHEDULED_TASKS_FILE,
    AUTO_EXPIRY_DAYS, JITTER_MINUTES, JITTER_OFFSET_MAX,
)


class CronLock:
    """
    基于 PID 文件的锁，用于防止多个会话同时触发同一个定时任务。
    """

    def __init__(self, lock_path: Path = None):
        self._lock_path = lock_path or CRON_LOCK_FILE  # 定时任务进程锁文件路径

    def acquire(self) -> bool:
        """
        尝试获取定时任务锁。成功时返回 True。
        如果锁文件已存在，检查其中记录的 PID 是否仍在运行。
        若该进程已终止，则锁视为过期，可以接管。
        """
        if self._lock_path.exists():
            try:
                stored_pid = int(self._lock_path.read_text(encoding="utf-8").strip())
                os.kill(stored_pid, 0)  # PID 存活探测
                return False  # 进程仍在运行
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass  # 锁已过期
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()))
        return True

    def release(self):
        """如果锁文件属于当前进程，则将其移除。"""
        try:
            if self._lock_path.exists():
                stored_pid = int(self._lock_path.read_text(encoding="utf-8").strip())
                if stored_pid == os.getpid():
                    self._lock_path.unlink()
        except (ValueError, OSError):
            pass


def cron_matches(expr: str, dt: datetime) -> bool:
    """
    检查一个五字段的 cron 表达式是否匹配给定的日期时间。
    字段：分钟 小时 日期 月份 星期
    支持：*（任意）、*/N（每 N 个）、N（精确值）、N-M（范围）、N,M（列表）
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
    # Python weekday: 0=Monday; cron: 0=Sunday. Convert.
    cron_dow = (dt.weekday() + 1) % 7
    values[4] = cron_dow
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    for field, value, (lo, hi) in zip(fields, values, ranges):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """将单个 cron 字段与一个值进行匹配。"""
    if field == "*":
        return True

    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)

        if part == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            if int(part) == value:
                return True

    return False


class CronScheduler:
    """
    管理定时任务并进行后台检查。
    调度记录、分钟级检查器、可选持久化机制和通知队列。
    """

    def __init__(self):
        self.tasks = []  # 任务字典列表
        self.queue = Queue()  # 通知队列
        self._stop_event = threading.Event()  # 通知调度后台线程停止的事件
        self._thread = None  # 定时任务检查后台线程
        self._last_check_minute = -1  # 避免同一分钟内重复触发

    def start(self):
        """加载持久化任务并启动后台检查线程。"""
        self._load_durable()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        count = len(self.tasks)
        if count:
            print(f"[Cron] 已加载 {count} 个定时任务")

    def stop(self):
        """停止后台线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def create(self, cron_expr: str, prompt: str,
               recurring: bool = True, durable: bool = False) -> str:
        """创建一个新的定时任务。返回任务 ID。"""
        task_id = str(uuid.uuid4())[:8]
        now = time.time()

        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": now,
        }

        # 周期性任务的抖动处理
        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron_expr)

        self.tasks.append(task)
        if durable:
            self._save_durable()

        mode = "recurring" if recurring else "one-shot"
        store = "durable" if durable else "session-only"
        return f"创建任务 {task_id} ({mode}, {store}): cron={cron_expr}"

    def delete(self, task_id: str) -> str:
        """按任务 ID 删除一个定时任务。"""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            return f"Deleted task {task_id}"
        return f"任务 {task_id} 不存在"

    def list_tasks(self) -> str:
        """列出所有定时任务。"""
        if not self.tasks:
            return "No scheduled tasks"
        lines = []
        for t in self.tasks:
            mode = "recurring" if t["recurring"] else "one-shot"
            store = "durable" if t["durable"] else "session"
            lines.append(f"  {t['id']} {t['cron']} [{mode}/{store}]")
        return "\n".join(lines)

    def drain_notifications(self) -> list:
        """清空队列中所有待处理的通知。"""
        notifications = []
        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break
        return notifications

    def _load_durable(self):
        """从 SCHEDULED_TASKS_FILE 加载持久化任务。"""
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text(encoding="utf-8"))
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            print(f"[Cron] 加载任务时出错：{e}")

    def _save_durable(self):
        """将持久化任务保存到磁盘。"""
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(
            json.dumps(durable, indent=2, ensure_ascii=False) + "\n"
        )

    def _check_loop(self):
        """后台线程：每秒检查是否有任务到期。"""
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute

            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)

            self._stop_event.wait(timeout=1)

    def _check_tasks(self, now: datetime):
        """将所有任务与当前时间比对，触发匹配的任务。"""
        expired = []
        fired_oneshots = []

        for task in self.tasks:
            # 自动过期：超过 AUTO_EXPIRY_DAYS 天的周期性任务
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue

            # 为匹配检查应用抖动偏移
            check_time = now
            jitter = task.get("jitter_offset", 0)
            if jitter:
                check_time = now - timedelta(minutes=jitter)

            if cron_matches(task["cron"], check_time):
                notification = f"[Scheduled task {task['id']}]: {task['prompt']}"
                self.queue.put(notification)
                task["last_fired"] = time.time()
                print(f"[Cron] Fired: {task['id']}")

                if not task["recurring"]:
                    fired_oneshots.append(task["id"])

        # 清理已过期和单次任务
        if expired or fired_oneshots:
            remove_ids = set(expired) | set(fired_oneshots)
            self.tasks = [t for t in self.tasks if t["id"] not in remove_ids]
            for tid in expired:
                print(f"[Cron] 自动过期：{tid}（已超过 {AUTO_EXPIRY_DAYS} 天）")
            for tid in fired_oneshots:
                print(f"[Cron] 单次任务已完成并已移除：{tid}")
            self._save_durable()

    def detect_missed_tasks(self) -> list:
        """
        启动时检查每个持久化任务的 last_fired 时间。
        如果某个任务在会话关闭期间本应触发，则将其标记为"已错过"。
        """
        now = datetime.now()
        missed = []
        for task in self.tasks:
            last_fired = task.get("last_fired")
            if last_fired is None:
                continue
            last_dt = datetime.fromtimestamp(last_fired)
            check = last_dt + timedelta(minutes=1)
            cap = min(now, last_dt + timedelta(hours=24))
            while check <= cap:
                if cron_matches(task["cron"], check):
                    missed.append({
                        "id": task["id"],
                        "cron": task["cron"],
                        "prompt": task["prompt"],
                        "missed_at": check.isoformat(),
                    })
                    break
                check += timedelta(minutes=1)
        return missed

    def _compute_jitter(self, cron_expr: str) -> int:
        """如果 cron 目标时间为 :00 或 :30，返回一个小偏移量（1–4 分钟）。"""
        fields = cron_expr.strip().split()
        if len(fields) < 1:
            return 0
        minute_field = fields[0]
        try:
            minute_val = int(minute_field)
            if minute_val in JITTER_MINUTES:
                return (hash(cron_expr) % JITTER_OFFSET_MAX) + 1
        except ValueError:
            pass
        return 0
