"""
code-demo/core/teammate/functions.py — 队友任务认领函数
来源：19_mcp-plugin.py L627–L678（已清洗，剔除 cron/compact/identity）
"""

import json
import time
import threading
from code_demo.core.config import TASKS_DIR, CLAIM_EVENTS_PATH

_claim_lock = threading.Lock()


def _append_claim_event(payload: dict):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    with CLAIM_EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _task_allows_role(task: dict, role: str | None) -> bool:
    required_role = task.get("claim_role") or task.get("required_role") or ""
    if not required_role:
        return True
    return bool(role) and role == required_role


def is_claimable_task(task: dict, role: str | None = None) -> bool:
    return (
        task.get("status") == "pending"
        and not task.get("owner")
        and not task.get("blockedBy")
        and _task_allows_role(task, role)
    )


def scan_unclaimed_tasks(role: str | None = None) -> list:
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text(encoding="utf-8"))
        if is_claimable_task(task, role):
            unclaimed.append(task)
    return unclaimed


def claim_task(
    task_id: int,
    owner: str,
    role: str | None = None,
    source: str = "manual"
) -> str:
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"任务 {task_id} 不存在"
        task = json.loads(path.read_text(encoding="utf-8"))
        if not is_claimable_task(task, role):
            return f"错误：任务 {task_id} 无法被角色为 {role or '(any)'} 的队友认领"
        task["owner"] = owner
        task["status"] = "inprogress"
        task["claimed_at"] = time.time()
        task["claim_source"] = source
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))
    _append_claim_event({
        "event": "task.claimed",
        "task_id": task_id,
        "owner": owner,
        "role": role,
        "source": source,
        "ts": time.time(),
    })
    return f"已通过 {source} 为 {owner} 认领任务 #{task_id}"