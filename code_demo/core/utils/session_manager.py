"""
code_demo/core/utils/session_manager.py — 对话会话管理器

管理对话会话的 CRUD 操作和持久化。
每个会话保存完整的 agent.history 数组，支持懒创建、自动命名、重命名。

存储结构:
  .sessions/
    index.json         → {"sessions": [...], "active_session_id": "..."}
    <uuid>.json        → {id, name, created_at, updated_at, history}
"""

import json
import os
import time
import uuid
from pathlib import Path

from ..config import SESSIONS_DIR


class SessionManager:
    """
    对话会话管理器。

    提供会话的创建、加载、保存、重命名、删除等操作。
    使用 index.json 维护会话列表，每个会话对应一个 <uuid>.json 文件。
    所有写入操作使用原子模式（先写 .tmp 再 rename），防止中断导致损坏。
    """

    def __init__(self):
        self.dir = SESSIONS_DIR  # 会话数据目录
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"  # 会话索引文件路径
        self._ensure_index()

    # ── 索引操作 ──

    def _ensure_index(self):
        """确保 index.json 存在。"""
        if not self.index_path.exists():
            self._save_index({"sessions": [], "active_session_id": None})

    def _load_index(self) -> dict:
        """加载索引文件。"""
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return {"sessions": [], "active_session_id": None}

    def _save_index(self, data: dict):
        """原子写入索引文件。"""
        self._atomic_write(self.index_path, data)

    def _atomic_write(self, path: Path, data: dict):
        """原子写入 JSON 文件：先写 .tmp 再 rename。"""
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(path))
        except Exception as e:
            print(f"[SessionManager] 写入失败 {path}: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _find_in_index(self, session_id: str) -> dict | None:
        """在索引中查找会话。"""
        for s in self._load_index().get("sessions", []):
            if s.get("id") == session_id:
                return s
        return None

    def _update_index_entry(self, session_id: str, **changes) -> dict | None:
        """更新索引中的会话条目。"""
        idx = self._load_index()
        updated = None
        for item in idx.get("sessions", []):
            if item.get("id") == session_id:
                item.update(changes)
                updated = item
                break
        if updated:
            self._save_index(idx)
        return updated

    # ── 会话文件路径 ──

    def _session_path(self, session_id: str) -> Path:
        """获取会话文件路径。"""
        return self.dir / f"{session_id}.json"

    # ── 核心 CRUD ──

    def create_session(self, name: str = "新会话") -> str:
        """
        创建会话条目到 index，返回 session_id。
        不创建会话文件（懒创建，等有实际内容时才写入）。
        """
        session_id = str(uuid.uuid4())
        now = time.time()
        entry = {
            "id": session_id,
            "name": name,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }
        idx = self._load_index()
        idx["sessions"].append(entry)
        idx["active_session_id"] = session_id
        self._save_index(idx)
        return session_id

    def save_session(self, session_id: str, history: list[dict]):
        """
        保存完整历史到 <uuid>.json，同时更新 index 中的 updated_at 和 message_count。
        """
        path = self._session_path(session_id)
        info = self._find_in_index(session_id)
        now = time.time()

        data = {
            "id": session_id,
            "name": info["name"] if info else "新会话",
            "created_at": info["created_at"] if info else now,
            "updated_at": now,
            "history": history,
        }
        self._atomic_write(path, data)

        # 更新索引
        msg_count = len([m for m in history if m.get("role") in ("user", "assistant")])
        self._update_index_entry(
            session_id,
            updated_at=now,
            message_count=msg_count,
        )

    def load_session(self, session_id: str) -> list[dict] | None:
        """
        加载会话历史，返回 history 数组。
        如果会话文件不存在，返回 None。
        """
        path = self._session_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("history", [])
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"[SessionManager] 加载会话失败 {session_id}: {e}")
            return None

    def get_session_info(self, session_id: str) -> dict | None:
        """从 index 获取会话元数据。"""
        return self._find_in_index(session_id)

    def rename_session(self, session_id: str, name: str):
        """重命名会话，同时更新 index 和会话文件（如存在）。"""
        self._update_index_entry(session_id, name=name)

        path = self._session_path(session_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["name"] = name
                self._atomic_write(path, data)
            except (json.JSONDecodeError, FileNotFoundError):
                pass

    def delete_session(self, session_id: str):
        """删除会话文件和 index 条目。"""
        # 删除文件
        path = self._session_path(session_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as e:
                print(f"[SessionManager] 删除文件失败 {path}: {e}")

        # 从索引中移除
        idx = self._load_index()
        idx["sessions"] = [
            s for s in idx.get("sessions", []) if s.get("id") != session_id
        ]
        if idx.get("active_session_id") == session_id:
            idx["active_session_id"] = None
        self._save_index(idx)

    def list_sessions(self) -> list[dict]:
        """返回所有会话的元数据列表（按 updated_at 降序）。"""
        sessions = self._load_index().get("sessions", [])
        return sorted(sessions, key=lambda s: s.get("updated_at", 0), reverse=True)

    def set_active(self, session_id: str | None):
        """设置当前活跃会话 ID。"""
        idx = self._load_index()
        idx["active_session_id"] = session_id
        self._save_index(idx)

    def get_active(self) -> str | None:
        """获取当前活跃会话 ID。"""
        return self._load_index().get("active_session_id")
