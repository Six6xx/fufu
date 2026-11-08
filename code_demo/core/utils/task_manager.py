"""
code_demo/core/utils/task_manager.py — 持久化任务看板

磁盘上的任务图（CRUD），支持依赖关系和工作树绑定。
来源：19_mcp-plugin.py L1513–L1656 (TaskManager)
"""

import json
import time
from pathlib import Path


class TaskManager:
    """
    持久化的 TaskRecord 存储。
    理解为"磁盘上的工作图"，而非"当前正在运行的工作进程"。
    """

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir  # 任务看板数据目录
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1  # 下一个待分配的任务 ID

    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, task: dict):
        path = self._path(task["id"])
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        """创建一个新任务并持久化。返回任务 JSON。"""
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "blockedBy": [],
            "blocks": [],
            "owner": "",
            "worktree": "",
            "worktree_state": "unbound",
            "last_worktree": "",
            "closeout": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        """获取任务详情。"""
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def get_dict(self, task_id: int) -> dict:
        """获取任务详情（字典形式，内部使用）。"""
        return self._load(task_id)

    def exists(self, task_id: int) -> bool:
        """检查任务是否存在。"""
        return self._path(task_id).exists()

    def update(self, task_id: int, status: str = None, owner: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        """更新任务状态、所有者或依赖关系。"""
        task = self._load(task_id)
        if owner is not None:
            task["owner"] = owner
        if status:
            if status not in ("pending", "in_progress", "completed", "deleted"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            # 当一个任务完成时，将其从所有其他任务的 blockedBy 列表中移除
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # 双向更新：同时更新被阻塞任务的 blockedBy 列表
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        """将工作树路径与任务绑定。"""
        task = self._load(task_id)
        task["worktree"] = worktree
        task["worktree_state"] = "active"
        if owner:
            task["owner"] = owner
        if task["status"] == "pending":
            task["status"] = "in_progress"
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def unbind_worktree(self, task_id: int) -> str:
        """解绑工作树。"""
        task = self._load(task_id)
        task["worktree"] = ""
        task["worktree_state"] = "unbound"
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def record_closeout(self, task_id: int, action: str,
                        reason: str = "", keep_binding: bool = False) -> str:
        """记录任务的关闭信息。"""
        task = self._load(task_id)
        task["closeout"] = {
            "action": action,
            "reason": reason,
            "at": time.time(),
        }
        task["worktree_state"] = action
        if not keep_binding:
            task["worktree"] = ""
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int):
        """从所有其他任务的 blockedBy 列表中移除 completed_id。"""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text(encoding="utf-8"))
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        """列出所有任务。"""
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text(encoding="utf-8")))
        if not tasks:
            return "No tasks"
        lines = []
        for t in tasks:
            marker = {
                "pending": "[ ]", "in_progress": "[>]",
                "completed": "[x]", "deleted": "[-]",
            }.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)
