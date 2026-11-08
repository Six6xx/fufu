"""
code_demo/core/agent.py — Agent 运行时引擎

把所有核心模块粘合成一个持久化运行的代理。
"""

import json
import time
import threading
import os
from pathlib import Path
from typing import Any

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

from code_demo.core.config import (
    WORKDIR, DEFAULT_WORKDIR, AI_CONFIG_DIR, INBOX_DIR, TEAM_DIR, VALID_MSG_TYPES, MODEL, MODEL_CHAT,
    CONTEXT_LIMIT, KEEP_RECENT_MESSAGES, ENABLE_REASONING, REASONING_EFFORT,
)
from code_demo.core.event import EventBUS, MessageBUS
from code_demo.core.event.bus import BUS  # 全局 MessageBUS
from code_demo.core.event.hooks import HookManager
from code_demo.core.protocol import RequestStore, REQUEST_STORE
from code_demo.core.teammate import (
    scan_unclaimed_tasks,
    claim_task,
    is_claimable_task,
)
from code_demo.core.tools import (
    run_bash, run_read, run_write, run_edit,
    CapabilityPermissionGate, permission_gate,
)
from code_demo.core.mcp_manager import MCPManager
from code_demo.core.tools.native.runner import (
    NATIVE_TOOL_SCHEMAS as NATIVE_TOOL_DEFS,
    NATIVE_TOOL_EXECUTORS as NATIVE_EXECUTORS,
)
from code_demo.core.utils.provider_manager import ProviderManager
from code_demo.core.utils.voice_provider_manager import VoiceProviderManager
from code_demo.core.utils import SKILL_REGISTRY
from code_demo.core.utils.session_manager import SessionManager
from code_demo.core.utils.memory import MemoryManager
from code_demo.core.utils.scheduler import CronScheduler
from code_demo.core.prompt import SystemPromptBuilder
from code_demo.core.tool_dispatcher import ToolDispatcher, PROXY_TOOL_DEFS
from code_demo.core.tools.desktop_tool import DESKTOP_TOOL_DEFS, make_sync_handler
from code_demo.core.voice_manager import VoiceIOManager
from code_demo.core.session_controller import SessionController
from code_demo.core.inference_engine import AsyncInferenceEngine, StreamCallbacks

# ── 上下文限制（从 config.py 导入，此处保留本地常量供向后兼容）──
MAX_ITERATIONS = 30  # agent_loop 最大迭代次数
MAX_RECOVERY_ATTEMPTS = 3  # API 调用最大重试次数


class Agent:
    """持久化运行的核心代理"""

    def __init__(
        self,
        name: str = "agent",
        role: str = "general",
        permission_mode: str = "default",
        voice_mode: bool = False,
        enable_reasoning: bool = None,
    ):
        self.name = name  # 当前 Agent 的名称标识
        self.role = role  # 当前 Agent 的职责角色
        self.running = False  # Agent 主循环是否正在运行

        # ── 思考模式 ──
        self.enable_reasoning = ENABLE_REASONING if enable_reasoning is None else enable_reasoning  # 是否启用模型推理内容

        # ── 总线 ──
        self.message_bus: MessageBUS = BUS  # 代理间消息总线
        self.request_store: RequestStore = REQUEST_STORE  # 持久化请求存储
        self.event_bus = EventBUS(AI_CONFIG_DIR / ".worktrees" / "events.jsonl")  # 运行事件日志总线

        # ── 权限 ──
        self.permission_gate = CapabilityPermissionGate(mode=permission_mode)  # 工具调用权限门控

        # ── Hook 管理器 ──
        self.hook_manager = HookManager()  # 工具生命周期 Hook 管理器

        # ── MCP 层 ──
        self.mcp = MCPManager()  # MCP 服务与工具管理器

        # ── 工具表 ──
        self.tool_defs: list[dict] = []  # 当前可供模型调用的工具定义
        self._build_tool_pool()

        # ── 模型供应商 ──
        self.provider_manager = ProviderManager()  # LLM 供应商及模型配置管理器
        self.voice_provider_manager = VoiceProviderManager()  # 语音服务供应商配置管理器

        # ── 语音 IO 管理器（依赖 voice_provider_manager）──
        self.voice = VoiceIOManager(voice_provider_manager=self.voice_provider_manager)  # ASR/TTS 语音 IO 管理器
        self.voice.voice_mode = voice_mode

        # ── 系统提示构建器 ──
        self.memory_manager = MemoryManager()  # 跨会话记忆管理器
        self.memory_manager.load_all()
        self.prompt_builder = SystemPromptBuilder(
            tool_defs=self.tool_defs,
            skill_registry=SKILL_REGISTRY,
            memory_manager=self.memory_manager,
            workdir_getter=lambda: self.workdir,
        )  # 系统提示词构建器

        # ── LLM 客户端 ──
        self.client: OpenAI | None = None  # 当前模型供应商的同步客户端
        self._init_llm()

        # ── 会话控制器 ──
        self.session = SessionController(
            session_manager=SessionManager(),
            provider_manager=self.provider_manager,
            prompt_builder=self.prompt_builder,
        )  # 会话、模型与工作区控制器
        self.session.client = self.client

        # ── 定时调度器 ──
        self.scheduler = CronScheduler()  # 定时任务调度器
        self.scheduler.start()

        # ── 桌面感知管理器（懒加载：仅创建实例，不自动启动） ──
        self._perception = self._init_perception()  # 桌面与环境感知组件

        # ── 工具分发器 ──
        self.dispatcher = ToolDispatcher(
            hook_manager=self.hook_manager,
            permission_gate=self.permission_gate,
            mcp_router=self.mcp.router,
            workdir_getter=lambda: self.workdir,
            message_bus=self.message_bus,
            scheduler=self.scheduler,
            skill_registry=SKILL_REGISTRY,
            name=self.name,
            role=self.role,
        )  # 统一工具分发器
        # 注册桌面工具到分发器
        self._register_desktop_tools()

        # ── 推理引擎 ──
        self.engine = AsyncInferenceEngine(
            dispatcher=self.dispatcher,
            session_controller=self.session,
            prompt_builder=self.prompt_builder,
            enable_reasoning=self.enable_reasoning,
            tool_defs_getter=lambda: self.tool_defs,
            voice_manager=self.voice,
        )  # 异步推理引擎

        # ── 收件箱轮询线程 ──
        self._poll_thread: threading.Thread | None = None  # 队友消息轮询后台线程
        self._poll_interval = 2.0  # 队友消息轮询间隔（秒）

    # ────────────────────────────────
    # 初始化
    # ────────────────────────────────

    def _build_tool_pool(self):
        """构建完整工具池：原生 + 代理 + 桌面 + MCP。"""
        self.tool_defs = list(NATIVE_TOOL_DEFS) + list(PROXY_TOOL_DEFS) + list(DESKTOP_TOOL_DEFS)
        self.tool_defs.extend(self.mcp.get_tool_defs())
        print(f"[Agent] 工具池就绪: {len(self.tool_defs)} 个工具")

    # ── MCP 兼容性代理属性/方法（保持 server.py 接口不变）──

    @property
    def perception(self):
        """桌面感知管理器实例（可能为 None）。"""
        return self._perception

    @property
    def mcp_router(self):
        """兼容属性：server.py 可能直接访问 agent.mcp_router"""
        return self.mcp.router

    @property
    def mcp_errors(self):
        return self.mcp.errors

    @property
    def mcp_disabled_tools(self):
        return self.mcp.disabled_tools

    def connect_mcp_server(self, name: str, cfg: dict) -> tuple[bool, str | None]:
        """兼容代理：连接 MCP 后重建工具池。"""
        ok, err = self.mcp.connect_server(name, cfg)
        if ok:
            self._build_tool_pool()
        return ok, err

    def disconnect_mcp_server(self, name: str):
        """兼容代理：断开 MCP 后重建工具池。"""
        self.mcp.disconnect_server(name)
        self._build_tool_pool()

    def toggle_mcp_tool(self, server_name: str, tool_name: str, enable: bool):
        self.mcp.toggle_tool(server_name, tool_name, enable)
        self._build_tool_pool()

    def get_mcp_server_tools(self, server_name: str) -> list:
        return self.mcp.get_server_tools(server_name)

    def _init_llm(self):
        """初始化 OpenAI 客户端。"""
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        base_url = os.getenv("DASHSCOPE_API_BASE_URL", "")
        if api_key and base_url:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
            print(f"[Agent] LLM 客户端就绪 (model={MODEL})")
        else:
            print("[Agent] 警告：未配置 API 密钥，LLM 功能不可用")

    def _init_perception(self):
        """懒加载初始化 PerceptionManager（仅创建实例，不启动感知流）。"""
        try:
            from code_demo.core.desktop.perception_manager import PerceptionManager
            perception = PerceptionManager()
            print("[Agent] PerceptionManager 已就绪（待命）")
            return perception
        except Exception as e:
            print(f"[Agent] PerceptionManager 初始化跳过: {e}")
            return None

    def _register_desktop_tools(self):
        """将桌面工具处理器注册到 ToolDispatcher。"""
        if not self._perception:
            return
        for tool_def in DESKTOP_TOOL_DEFS:
            tool_name = tool_def["name"]
            handler = make_sync_handler(tool_name, self._perception)
            self.dispatcher.register(tool_name, handler)
        print(f"[Agent] 桌面工具已注册: {[t['name'] for t in DESKTOP_TOOL_DEFS]}")

    def _tools_to_openai_format(self) -> list[dict]:
        """将内部工具定义转换为 OpenAI Chat Completions 格式。"""
        openai_tools = []
        for t in self.tool_defs:
            props = t.get("parameters", {}).get("properties", {})
            required = t.get("parameters", {}).get("required", [])
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            })
        return openai_tools

    # ────────────────────────────────
    # 工具执行
    # ────────────────────────────────

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """执行工具（委托给 ToolDispatcher）。"""
        return self.dispatcher.dispatch(tool_name, tool_input)

    def set_permission_mode(self, mode: str) -> tuple[bool, str]:
        """切换权限模式。

        Returns:
            (ok, message)
        """
        old_mode = self.permission_gate.mode
        self.permission_gate.set_mode(mode)
        new_mode = self.permission_gate.mode
        if new_mode == old_mode:
            return False, f"权限模式已是: {new_mode}"
        return True, f"权限模式已切换: {old_mode} → {new_mode}"

    # ────────────────────────────────
    # LLM 推理循环
    # ────────────────────────────────

    def agent_loop(self, user_input: str = None, token_callback=None, tool_callback=None,
                   context_limit=None, attachments=None, original_content=None) -> str | None:
        """LLM 推理循环（委托给 AsyncInferenceEngine 的同步适配层）。"""
        return self.engine.run_sync(
            user_input=user_input,
            token_callback=token_callback,
            tool_callback=tool_callback,
            context_limit=context_limit,
            attachments=attachments,
            original_content=original_content,
        )

    async def async_agent_loop(self, user_input=None, callbacks=None,
                               context_limit=None, attachments=None,
                               original_content=None) -> str | None:
        """Async 推理循环（WebSocket 场景直接 await，零线程池开销）。"""
        from code_demo.core.inference_engine import StreamCallbacks
        if callbacks is None:
            callbacks = StreamCallbacks()
        return await self.engine.run(
            user_input=user_input,
            callbacks=callbacks,
            context_limit=context_limit,
            attachments=attachments,
            original_content=original_content,
        )

    # ────────────────────────────────
    # 会话管理（委托给 SessionController + 兼容性代理）
    # ────────────────────────────────

    @property
    def history(self):
        return self.session.history

    @history.setter
    def history(self, value):
        self.session.history = value

    @property
    def current_session_id(self):
        return self.session.current_session_id

    @current_session_id.setter
    def current_session_id(self, value):
        self.session.current_session_id = value

    @property
    def current_model(self):
        return self.session.current_model

    @current_model.setter
    def current_model(self, value):
        self.session.current_model = value

    @property
    def current_provider_id(self):
        return self.session.current_provider_id

    @current_provider_id.setter
    def current_provider_id(self, value):
        self.session.current_provider_id = value

    @property
    def workdir(self):
        return self.session.workdir

    @workdir.setter
    def workdir(self, value):
        self.session.workdir = value

    @property
    def session_manager(self):
        return self.session._session_manager

    def save_current_session(self):
        self.session.save_current_session()

    def load_session(self, session_id: str) -> bool:
        result = self.session.load_session(session_id)
        if result:
            self.client = self.session.client
        return result

    def new_session(self, name: str = "新会话") -> str:
        session_id = self.session.new_session(name)
        self.client = self.session.client
        return session_id

    def switch_session(self, session_id: str) -> tuple[bool, list[dict]]:
        result = self.session.switch_session(session_id)
        self.client = self.session.client
        return result

    def get_workspace(self) -> str:
        return self.session.get_workspace()

    def set_workspace(self, path_str: str) -> tuple[bool, str]:
        return self.session.set_workspace(path_str)

    def get_current_model_info(self) -> dict:
        return self.session.get_current_model_info()

    def list_available_models(self) -> list[dict]:
        return self.session.list_available_models()

    def set_model(self, provider_id: str, model_name: str) -> tuple[bool, str]:
        result = self.session.set_model(provider_id, model_name)
        self.client = self.session.client
        return result

    def get_session_name(self) -> str:
        return self.session.get_session_name()

    def rename_current_session(self, name: str):
        self.session.rename_current_session(name)

    def _reset_history(self):
        """重置对话历史（用于 /clear 命令）。"""
        self.session.reset_history()

    # ────────────────────────────────
    # 消息处理
    # ────────────────────────────────

    def process_inbox(self):
        """处理收件箱中的所有消息。"""
        messages = self.message_bus.read_inbox(self.name)
        if not messages:
            return
        for msg in messages:
            msg_type = msg.get("type", "message")
            sender = msg.get("from", "unknown")
            content = msg.get("content", "")

            print(f"\n📬 [{self.name}] ← {sender} ({msg_type}): {content[:120]}")

            if msg_type == "shutdown_request":
                print(f"   ⚠ 收到关机请求，准备退出...")
                self.stop()

            elif msg_type == "plan_approval":
                request_id = msg.get("request_id", "")
                print(f"   📋 计划审批请求: {request_id}")

            elif msg_type == "broadcast":
                print(f"   📢 广播消息")

    def handle_command(self, line: str) -> str:
        """解析并执行单条命令行输入（JSON 格式的工具调用）。"""
        line = line.strip()
        if not line:
            return ""

        # 尝试 JSON 格式工具调用
        try:
            payload = json.loads(line)
            tool_name = payload.get("tool", payload.get("name", ""))
            tool_input = payload.get("input", payload.get("params", {}))
            if tool_name:
                result = self.execute_tool(tool_name, tool_input)
                return result
        except json.JSONDecodeError:
            pass

        # 纯文本 → 当成给同伴的消息
        return f"纯文本消息（未实现 LLM 解析）: {line}"

    # ── 语音兼容性代理（供 server.py 等外部模块直接调用）──

    def synthesize_speech(self, text: str, speed: float = 1.0) -> tuple:
        """兼容代理：语音合成。"""
        return self.voice.synthesize_speech(text, speed)

    @property
    def tts_mode(self) -> bool:
        return self.voice.tts_mode

    @tts_mode.setter
    def tts_mode(self, value: bool):
        self.voice.tts_mode = value

    # ────────────────────────────────
    # 生命周期
    # ────────────────────────────────

    def start(self):
        """启动代理：开始轮询 + 进入 REPL。"""
        self.running = True

        print(f"\n{'='*60}")
        print(f"  🤖 {self.name} (角色: {self.role}) 已启动")
        print(f"  工作目录: {self.workdir}")
        print(f"  工具: {len(self.tool_defs)} 个 | 技能: {len(SKILL_REGISTRY.documents)} 个")
        print(f"  LLM: {MODEL}" + (" (已连接)" if self.client else " (未配置)"))
        if self.voice.voice_mode:
            print(f"  模式: 语音 (输入 /voice 切换回文本)")
        print(f"{'='*60}")
        print(f"  直接输入自然语言进行对话 (LLM 驱动)")
        print(f"  特殊命令: /help  /inbox  /tasks  /skills  /tools  /clear  /voice  /tts  /quit")
        print(f"{'='*60}\n")

        # 启动收件箱轮询线程
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name=f"{self.name}-poll",
        )
        self._poll_thread.start()

        # 如果启动时已设语音模式，初始化 ASR
        if self.voice.voice_mode and not self.voice.init_asr_engine():
            self.voice.voice_mode = False

        # 主 REPL
        self._repl()

    def stop(self):
        """停止代理。"""
        self.running = False
        print(f"\n🛑 {self.name} 正在关闭...")
        # 停止定时调度器
        if self.scheduler:
            self.scheduler.stop()
        # 释放语音资源
        self.voice.stop()
        # 断开 MCP
        self.mcp.disconnect_all()

    def _poll_loop(self):
        """后台线程：周期性检查收件箱。"""
        while self.running:
            time.sleep(self._poll_interval)
            try:
                self.process_inbox()
            except Exception as e:
                print(f"[{self.name}] 收件箱轮询错误: {e}")

    def _repl(self):
        """主 REPL 循环：自然语言 → LLM 推理 → 工具调用 → 回复。"""
        mode_hint = " (Enter=录音)" if self.voice.voice_mode else ""
        print(f"💬 开始对话（输入 /quit 退出）{mode_hint}\n")

        while self.running:
            try:
                prompt = f"[{self.name}]" + (" 🎤 " if self.voice.voice_mode else " >> ")
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                # ── 语音模式：空行触发录音 ──
                if self.voice.voice_mode:
                    recognized = self.voice.get_voice_input()
                    if recognized:
                        print(f"[Agent] 识别结果: {recognized}")
                        if self.client is None:
                            print("错误：LLM 未配置。请设置 DASHSCOPE_API_KEY。")
                        else:
                            response = self.agent_loop(user_input=recognized)
                            if response and (response.startswith("API 调用失败") or response.startswith("错误")):
                                print(f"\n🤖 {response}\n")
                            elif not response or response == "(空响应)":
                                print()
                    else:
                        print("[Agent] 未检测到语音输入")
                continue

            # ── 特殊命令 ──
            if line == "/quit":
                break
            if line == "/help":
                self._print_help()
                continue
            if line == "/clear":
                self._reset_history()
                print("[Agent] 对话历史已清除")
                continue
            if line == "/inbox":
                print(self.dispatcher.dispatch("read_inbox", {}))
                continue
            if line == "/tasks":
                print(self.dispatcher.dispatch("list_tasks", {}))
                continue
            if line == "/skills":
                print(SKILL_REGISTRY.describe_available())
                continue
            if line == "/teammates":
                print(self.dispatcher.dispatch("list_teammates", {}))
                continue
            if line == "/tools":
                print(json.dumps(
                    [{"name": t["name"], "desc": t.get("description", "")[:60]} for t in self.tool_defs],
                    indent=2, ensure_ascii=False,
                ))
                continue
            if line == "/prompt":
                print(self.prompt_builder.build())
                continue
            if line == "/voice":
                self.voice.toggle_voice_mode()
                continue
            if line == "/tts":
                self.voice.toggle_tts_mode()
                continue

            # ── JSON 工具调用（保留原有直接执行方式） ──
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                    tool_name = payload.get("tool", payload.get("name", ""))
                    tool_input = payload.get("input", payload.get("params", {}))
                    if tool_name:
                        result = self.execute_tool(tool_name, tool_input)
                        print(result)
                        continue
                except json.JSONDecodeError:
                    pass

            # ── LLM 驱动对话（流式输出） ──
            if self.client is None:
                print("错误：LLM 未配置。请设置 DASHSCOPE_API_KEY 和 DASHSCOPE_API_BASE_URL。")
                print("可使用 JSON 格式进行工具调用: {\"tool\": \"bash\", \"input\": {...}}")
                continue

            response = self.agent_loop(user_input=line)
            # agent_loop 内部已流式打印文本，这里只处理错误/空响应
            if response and (response.startswith("API 调用失败") or response.startswith("错误")):
                print(f"\n🤖 {response}\n")
            elif not response or response == "(空响应)":
                print()

            # ── TTS 自动合成 ──
            if self.voice.tts_mode and response and not response.startswith("错误") and response != "(空响应)":
                self.voice.play_tts_response(response)

        self.stop()

    def _print_help(self):
        print("""
可用命令:
  /help        显示此帮助
  /clear       清除对话历史
  /voice       切换语音/文本模式 (ASR)
  /tts         开启/关闭 TTS (文本转语音)
  /inbox       读取收件箱
  /tasks       列出可认领任务
  /skills      列出可用技能
  /teammates   列出队友
  /tools       列出所有工具
  /prompt      显示系统提示词
  /quit        退出

JSON 工具调用 (绕过 LLM 直接执行):
  {"tool": "bash", "input": {"command": "echo hello"}}
  {"tool": "read_file", "input": {"path": "README.md"}}

自然语言输入将发送给 LLM 进行推理和工具调用。
语音模式下，直接按 Enter 开始录音，说完按 Enter 结束。
TTS 开启后，Agent 的文字回复会自动合成为语音并播放。
""")
