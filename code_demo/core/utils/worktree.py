"""
code_demo/core/utils/worktree.py — Git 工作树管理器

管理 Git 工作树的创建、遍历、运行命令和清理。
来源：19_mcp-plugin.py L2910–L3254 (WorktreeManager)
"""

import json
import re
import subprocess
import time
from pathlib import Path

from ..config import WORKDIR, AI_CONFIG_DIR
from ..utils.security import bash_validator
from ..utils.shell import run_shell_command


class WorktreeManager:
    """
    管理 Git 工作树 (worktrees) 的高级工具。

    在 `ai_configuration/.worktrees` 目录中存放所有工作树，
    并维护一个 `index.json` 文件来跟踪元数据。
    """

    def __init__(self, repo_root: Path, tasks=None, events=None):
        self.repo_root = repo_root  # Git 仓库根目录
        self.tasks = tasks  # 关联的任务管理器
        self.events = events  # 工作树操作事件总线
        self.dir = AI_CONFIG_DIR / ".worktrees"  # Git 工作树存放目录
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"  # 工作树元数据索引文件
        if not self.index_path.exists():
            self.index_path.write_text(
                json.dumps({"worktrees": []}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        self.git_available = self._check_git()  # 当前目录是否可调用 Git 工作树命令

    def _check_git(self) -> bool:
        """检查当前环境是否在一个有效的 Git 仓库中。"""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root, capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list) -> str:
        """安全地执行 Git 命令。"""
        if not self.git_available:
            raise RuntimeError("Not in a git repository")
        r = subprocess.run(
            ["git", *args], cwd=self.repo_root,
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            error_msg = (r.stdout + r.stderr).strip() or f"git {' '.join(args)} failed"
            print(f"[Worktree] git error: {error_msg[:200]}")
            return f"Error: {error_msg}"
        return (r.stdout + r.stderr).strip() or "(no output)"

    # --- 索引操作 ---

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, data: dict):
        self.index_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _find(self, name: str) -> dict | None:
        for wt in self._load_index().get("worktrees", []):
            if wt.get("name") == name:
                return wt
        return None

    def _update_entry(self, name: str, **changes) -> dict:
        idx = self._load_index()
        updated = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item.update(changes)
                updated = item
                break
        self._save_index(idx)
        if not updated:
            raise ValueError(f"未找到名为 '{name}' 的工作树")
        return updated

    def _validate_name(self, name: str):
        """验证工作树名称是否合法。"""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError(
                "工作树名称无效。请使用 1-40 个字符：字母、数字、点(.)、下划线(_)、连字符(-)"
            )

    # --- 核心操作 ---

    def create(self, name: str, task_id: int = None,
               base_ref: str = "HEAD") -> str:
        """创建一个新的工作树。"""
        self._validate_name(name)
        if self._find(name):
            return f"Worktree '{name}' 已存在"
        if task_id is not None and self.tasks and not self.tasks.exists(task_id):
            return f"任务 {task_id} 不存在"

        path = self.dir / name
        branch = f"wt/{name}"

        if self.events:
            self.events.emit("worktree.create.before", task_id=task_id, wt_name=name)
        try:
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])
            entry = {
                "name": name, "path": str(path), "branch": branch,
                "task_id": task_id, "status": "active",
                "created_at": time.time(),
            }
            idx = self._load_index()
            idx["worktrees"].append(entry)
            self._save_index(idx)

            if task_id is not None and self.tasks:
                self.tasks.bind_worktree(task_id, name)

            if self.events:
                self.events.emit("worktree.create.after", task_id=task_id, wt_name=name)
            return json.dumps(entry, indent=2, ensure_ascii=False)
        except Exception as e:
            if self.events:
                self.events.emit("worktree.create.failed", task_id=task_id,
                                 wt_name=name, error=str(e))
            raise

    def list_all(self) -> str:
        """列出所有已知的工作树。"""
        wts = self._load_index().get("worktrees", [])
        if not wts:
            return "索引中未找到 worktrees"
        lines = []
        for wt in wts:
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            lines.append(
                f"[{wt.get('status', '?')}] {wt['name']} -> "
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"
            )
        return "\n".join(lines)

    def status(self, name: str) -> str:
        """获取指定工作树的 Git 状态。"""
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: worktree path '{path}' 不存在"
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=path, capture_output=True, text=True, timeout=120,
        )
        return (r.stdout + r.stderr).strip() or "Clean worktree"

    def enter(self, name: str) -> str:
        """"进入"一个工作树（记录访问时间）。"""
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: worktree path '{path}' 不存在"
        updated = self._update_entry(name, last_entered_at=time.time())
        if self.events:
            self.events.emit("worktree.enter", task_id=wt.get("task_id"),
                             wt_name=name, path=str(path))
        return json.dumps(updated, indent=2, ensure_ascii=False)

    def run(self, name: str, command: str) -> str:
        """在指定工作树的目录中执行 shell 命令。"""
        if not bash_validator.is_safe(command):
            return bash_validator.describe_failures(command)
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: worktree path '{path}' 不存在"
        try:
            self._update_entry(
                name,
                last_entered_at=time.time(),
                last_command_at=time.time(),
                last_command_preview=command[:120],
            )
            if self.events:
                self.events.emit("worktree.run.before",
                                 task_id=wt.get("task_id"),
                                 wt_name=name, command=command[:120])
            r = run_shell_command(
                command, cwd=path,
                capture_output=True, text=True, timeout=600,
            )
            out = (r.stdout + r.stderr).strip()
            if self.events:
                self.events.emit("worktree.run.after",
                                 task_id=wt.get("task_id"), wt_name=name)
            return out[:500000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            if self.events:
                self.events.emit("worktree.run.timeout",
                                 task_id=wt.get("task_id"), wt_name=name)
            return "Error: Timeout (600s)"

    def remove(self, name: str, force: bool = False,
               complete_task: bool = False, reason: str = "") -> str:
        """移除一个工作树。"""
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        task_id = wt.get("task_id")

        if self.events:
            self.events.emit("worktree.remove.before", task_id=task_id, wt_name=name)
        try:
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(wt["path"])
            self._run_git(args)

            if complete_task and task_id is not None and self.tasks:
                self.tasks.update(task_id, status="completed")
                if self.events:
                    self.events.emit("task.completed", task_id=task_id, wt_name=name)
            if task_id is not None and self.tasks:
                self.tasks.record_closeout(task_id, "removed", reason,
                                           keep_binding=False)

            self._update_entry(
                name, status="removed", removed_at=time.time(),
                closeout={"action": "remove", "reason": reason, "at": time.time()},
            )
            if self.events:
                self.events.emit("worktree.remove.after",
                                 task_id=task_id, wt_name=name)
            return f"Removed worktree '{name}'"
        except Exception as e:
            if self.events:
                self.events.emit("worktree.remove.failed",
                                 task_id=task_id, wt_name=name, error=str(e))
            self._update_entry(
                name, status="removed", removed_at=time.time(),
                closeout={"action": "remove_failed",
                          "reason": str(e), "at": time.time()},
            )
            return (f"Warning: worktree '{name}' git 移除失败"
                    f"({str(e)[:100]})，已从索引中清理")

    def keep(self, name: str) -> str:
        """"保留"一个工作树，关联任务被记录为 "kept"。"""
        wt = self._find(name)
        if not wt:
            return f"Error: 未知 worktree '{name}'"
        if wt.get("task_id") is not None and self.tasks:
            self.tasks.record_closeout(wt["task_id"], "kept", "",
                                       keep_binding=True)
        self._update_entry(
            name, status="kept", kept_at=time.time(),
            closeout={"action": "keep", "reason": "", "at": time.time()},
        )
        if self.events:
            self.events.emit("worktree.keep",
                             task_id=wt.get("task_id"), wt_name=name)
        return json.dumps(self._find(name), indent=2, ensure_ascii=False)

    def closeout(self, name: str, action: str,
                 reason: str = "", force: bool = False,
                 complete_task: bool = False) -> str:
        """工作树的统一关闭出口。根据 action 决定是保留还是移除。"""
        if action == "keep":
            wt = self._find(name)
            if not wt:
                return f"Error: 未知 worktree '{name}'"
            if wt.get("task_id") is not None and self.tasks:
                self.tasks.record_closeout(
                    wt["task_id"], "kept", reason, keep_binding=True
                )
                if complete_task:
                    self.tasks.update(wt["task_id"], status="completed")
            self._update_entry(
                name, status="kept", kept_at=time.time(),
                closeout={"action": "keep", "reason": reason, "at": time.time()},
            )
            if self.events:
                self.events.emit("worktree.closeout.keep",
                                 task_id=wt.get("task_id"),
                                 wt_name=name, reason=reason)
            return json.dumps(self._find(name), indent=2, ensure_ascii=False)

        if action == "remove":
            if self.events:
                self.events.emit("worktree.closeout.remove",
                                 wt_name=name, reason=reason)
            return self.remove(name, force=force,
                               complete_task=complete_task, reason=reason)

        return f"Error: 未知 action '{action}'，可选 'keep' 或 'remove'"
