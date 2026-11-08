"""
code_demo/core/utils/background.py — 后台任务管理器

线程化执行 + 通知队列，支持持久化任务记录。
来源：19_mcp-plugin.py L1378–L1509 (BackgroundManager)
"""

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from .shell import run_shell_command

from ..config import WORKDIR, STALL_THRESHOLD_S, TOOL_RESULTS_DIR


class BackgroundManager:
    """后台命令执行管理器，支持持久化记录和通知队列。"""

    def __init__(self):
        self.dir = TOOL_RESULTS_DIR / "background"  # 后台任务记录与输出目录
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tasks = {}  # task_id -> {status, result, command, started_at, ...}
        self._notification_queue = []  # 完成的任务结果
        self._lock = threading.Lock()  # 保护后台通知队列的线程锁

    def _record_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _output_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"

    def _persist_task(self, task_id: str):
        record = dict(self.tasks[task_id])
        self._record_path(task_id).write_text(
            json.dumps(record, indent=2, ensure_ascii=False)
        )

    def _preview(self, output: str, limit: int = 500) -> dict:
        """生成预览（紧凑），同时保留完整原始输出供下游使用。"""
        raw = output or "(no output)"
        compact = " ".join(raw.split())
        return {
            "preview": compact[:limit],
            "raw_output": raw,
            "is_truncated": len(compact) > limit,
        }

    @staticmethod
    def _safe_decode(b: bytes) -> str:
        """安全解码（兼容 UTF-8 / GBK / 二进制）。"""
        if b is None:
            return ""
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return b.decode("gbk")
            except UnicodeDecodeError:
                return b.decode("utf-8", errors="ignore")

    def run(self, command: str) -> str:
        """启动一个后台线程，并立即返回 task_id。"""
        task_id = str(uuid.uuid4())[:8]
        output_file = self._output_path(task_id)
        self.tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "result": None,
            "command": command,
            "started_at": time.time(),
            "finished_at": None,
            "result_preview": "",
            "output_file": str(output_file),
        }
        self._persist_task(task_id)
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return (
            f"后台任务 {task_id} started: {command[:80]} "
            f"(output_file={output_file})"
        )

    def _execute(self, task_id: str, command: str):
        """线程目标：运行子进程，捕获输出，并将结果推送到队列。"""
        try:
            r = run_shell_command(
                command, cwd=WORKDIR,
                capture_output=True, timeout=600,
            )
            output = (
                self._safe_decode(r.stdout) + self._safe_decode(r.stderr)
            ).strip()
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (600s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"

        final_output = output or "(no output)"
        preview = self._preview(final_output)
        output_path = self._output_path(task_id)
        output_path.write_text(final_output, encoding="utf-8")

        self.tasks[task_id]["finished_at"] = time.time()
        self.tasks[task_id]["result_preview"] = preview
        self.tasks[task_id]["status"] = status
        self._persist_task(task_id)

        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "preview": preview,
                "output_file": str(output_path),
            })

    def check(self, task_id: str = None) -> str:
        """检查单个任务的状态，或列出所有任务状态。"""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: 未知任务 {task_id}"
            visible = {
                "id": t["id"],
                "status": t["status"],
                "command": t["command"],
                "result_preview": t.get("result_preview", ""),
                "output_file": t.get("output_file", ""),
            }
            return json.dumps(visible, indent=2, ensure_ascii=False)
        lines = []
        for tid, t in self.tasks.items():
            preview = t.get("result_preview", {})
            preview_str = preview.get("preview", "") if isinstance(preview, dict) else str(preview)
            lines.append(
                f"{tid}: [{t['status']}] {t['command'][:60]}"
                f" -> {preview_str or '(running)'}"
            )
        return "\n".join(lines) if lines else "无后台任务"

    def drain_notifications(self) -> list:
        """返回并清空所有待处理的完成通知。"""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs

    def detect_stalled(self) -> list:
        """返回运行时间超过 STALL_THRESHOLD_S 的任务 ID（任务卡住）。"""
        now = time.time()
        stalled = []
        for task_id, info in self.tasks.items():
            if info["status"] != "running":
                continue
            elapsed = now - info.get("started_at", now)
            if elapsed > STALL_THRESHOLD_S:
                stalled.append(task_id)
        return stalled
