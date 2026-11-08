"""
core/session_controller.py — 会话/模型/工作空间管理

从 agent.py 提取的独立模块，负责：
- 会话生命周期（创建、加载、保存、切换）
- 模型供应商切换
- 工作空间路径管理
"""

from pathlib import Path

from openai import OpenAI

from code_demo.core.config import DEFAULT_WORKDIR, MODEL
from code_demo.core.utils.session_manager import SessionManager
from code_demo.core.utils.provider_manager import ProviderManager
from code_demo.core.prompt import SystemPromptBuilder


class SessionController:
    """管理会话生命周期、模型切换、工作空间。"""

    def __init__(
        self,
        session_manager: SessionManager,
        provider_manager: ProviderManager,
        prompt_builder: SystemPromptBuilder,
    ):
        self._session_manager = session_manager  # 持久化会话管理器
        self._provider_manager = provider_manager  # LLM 供应商配置管理器
        self._prompt_builder = prompt_builder  # 系统提示词构建器

        # 共享状态（Agent 通过属性代理访问）
        self.current_session_id: str | None = None  # 当前会话 ID
        self.current_model: str = MODEL  # 当前选用的模型名称
        self.current_provider_id: str | None = None  # 当前模型供应商 ID
        self.workdir: Path = DEFAULT_WORKDIR  # 当前 Agent 工作目录
        self.history: list[dict] = []  # 当前会话的消息历史
        self.client: OpenAI | None = None  # 当前模型客户端

    # ────────────────────────────────
    # 会话管理
    # ────────────────────────────────

    def save_current_session(self):
        """保存当前会话到磁盘（仅在有 session_id 时）。"""
        if self.current_session_id:
            try:
                self._session_manager.save_session(
                    self.current_session_id, self.history
                )
            except Exception as e:
                print(f"[SessionController] 保存会话失败: {e}")

    def load_session(self, session_id: str) -> bool:
        """加载指定会话。成功返回 True，不存在返回 False。

        加载后刷新系统提示词，确保模型始终使用最新的记忆和动态上下文。
        """
        history = self._session_manager.load_session(session_id)
        if history is not None:
            self.current_session_id = session_id
            self.history = history
            self._session_manager.set_active(session_id)
            info = self._session_manager.get_session_info(session_id)
            # 恢复会话级工作空间
            if info and info.get("workspace"):
                ws = Path(info["workspace"])
                if ws.is_dir():
                    self.workdir = ws
                else:
                    self.workdir = DEFAULT_WORKDIR
            else:
                self.workdir = DEFAULT_WORKDIR
            # 恢复会话级模型供应商
            if info and info.get("provider_id"):
                pid = info["provider_id"]
                prov = self._provider_manager.get_provider(pid)
                if prov:
                    self.current_provider_id = pid
                    self.current_model = info.get("model", prov.get("models", [{}])[0].get("name", MODEL))
                    try:
                        self.client = OpenAI(api_key=prov["api_key"], base_url=prov["url"])
                    except Exception as e:
                        print(f"[SessionController] 恢复模型客户端失败: {e}")
                else:
                    self.current_provider_id = None
                    self.current_model = MODEL
            else:
                self.current_provider_id = None
                self.current_model = MODEL
            # 刷新系统提示词：磁盘中的旧提示词不含当前记忆/工具/运行时信息
            if self.history and self.history[0].get("role") == "system":
                self.history[0]["content"] = self._prompt_builder.build()
            return True
        return False

    def new_session(self, name: str = "新会话") -> str:
        """新建空会话，返回 session_id。"""
        session_id = self._session_manager.create_session(name)
        self.current_session_id = session_id
        self.history = []
        self.workdir = DEFAULT_WORKDIR
        # 重置模型供应商到当前活跃或默认
        active = self._provider_manager.get_active()
        if active:
            self.current_provider_id = active["id"]
            models = active.get("models", [])
            self.current_model = models[0]["name"] if models else MODEL
            try:
                self.client = OpenAI(api_key=active["api_key"], base_url=active["url"])
            except Exception:
                pass
        else:
            self.current_provider_id = None
            self.current_model = MODEL
        self._session_manager.set_active(session_id)
        # 持久化默认工作空间和模型到 session metadata
        self._session_manager._update_index_entry(
            session_id,
            workspace=str(self.workdir),
            provider_id=self.current_provider_id,
            model=self.current_model,
        )
        return session_id

    def switch_session(self, session_id: str) -> tuple[bool, list[dict]]:
        """切换会话：保存当前 → 加载目标。返回 (success, history)。"""
        self.save_current_session()
        if self.load_session(session_id):
            return True, self.history
        return False, []

    # ────────────────────────────────
    # 工作空间管理
    # ────────────────────────────────

    def get_workspace(self) -> str:
        """返回当前工作空间路径字符串。"""
        return str(self.workdir)

    def set_workspace(self, path_str: str) -> tuple[bool, str]:
        """验证并设置工作空间路径。返回 (success, message)。"""
        p = Path(path_str).expanduser().resolve()
        if not p.exists():
            return False, f"路径不存在: {p}"
        if not p.is_dir():
            return False, f"不是目录: {p}"
        self.workdir = p
        # 刷新系统提示词以反映新工作空间
        if self.history and self.history[0].get("role") == "system":
            self.history[0]["content"] = self._prompt_builder.build()
        # 持久化到 session metadata
        if self.current_session_id:
            self._session_manager._update_index_entry(
                self.current_session_id, workspace=str(p)
            )
        return True, f"工作目录已切换: {p}"

    # ────────────────────────────────
    # 模型供应商管理
    # ────────────────────────────────

    def get_current_model_info(self) -> dict:
        """返回当前模型信息：{model, provider_id, provider_name}。"""
        active = self._provider_manager.get_active()
        return {
            "model": self.current_model or MODEL or "unknown",
            "provider_id": self.current_provider_id or (active["id"] if active else None),
            "provider_name": active.get("name", "") if active else "",
        }

    def list_available_models(self) -> list[dict]:
        """返回当前活跃供应商的模型列表。"""
        active = self._provider_manager.get_active()
        if not active:
            return []
        return active.get("models", [])

    def set_model(self, provider_id: str, model_name: str) -> tuple[bool, str]:
        """切换到指定供应商的指定模型。重建 OpenAI client。返回 (success, message)。"""
        provider = self._provider_manager.get_provider(provider_id)
        if not provider:
            return False, f"供应商不存在: {provider_id}"

        api_key = provider.get("api_key", "")
        base_url = provider.get("url", "")
        if not api_key or not base_url:
            return False, "供应商 API Key 或 URL 未配置"

        try:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
            self.current_model = model_name
            self.current_provider_id = provider_id
            # 设为活跃供应商
            self._provider_manager.set_active(provider_id)
            # 持久化到 session metadata
            if self.current_session_id:
                self._session_manager._update_index_entry(
                    self.current_session_id,
                    provider_id=provider_id,
                    model=model_name,
                )
            print(f"[SessionController] 模型已切换: {provider.get('name')} / {model_name}")
            return True, f"模型已切换: {model_name}"
        except Exception as e:
            return False, f"切换模型失败: {e}"

    # ────────────────────────────────
    # 会话信息
    # ────────────────────────────────

    def get_session_name(self) -> str:
        """获取当前会话名称。"""
        if self.current_session_id:
            info = self._session_manager.get_session_info(self.current_session_id)
            if info:
                return info.get("name", "新会话")
        return "新会话"

    def rename_current_session(self, name: str):
        """重命名当前会话。"""
        if self.current_session_id:
            self._session_manager.rename_session(self.current_session_id, name)

    def reset_history(self):
        """重置对话历史。"""
        self.history = []
