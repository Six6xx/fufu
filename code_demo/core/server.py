"""
code_demo/core/server.py — FastAPI WebSocket 服务

整体架构：
  基于 FastAPI + WebSocket 实现前后端实时通信。核心设计思路是"流式处理"：
  1. LLM 流式生成 → token_callback 实时推送 stream_content/stream_reasoning
  2. 按句子分段 → 每完成一个句子立即在后台线程启动 TTS 合成
  3. TTS 完成 → stream_audio 推送音频片段 → 前端排队播放
  这样文字显示和音频合成并行进行，大幅减少用户等待时间。

端点：
  GET  /         → 前端静态页面
  GET  /health   → 健康检查（返回 Agent 状态、TTS/ASR 开关）
  WS   /ws       → 双向通信（文本/音频/状态/流式 token）

WebSocket 消息类型：
  Client → Server:
    text         → 用户输入，触发 Agent 推理
    audio        → ASR 音频快照（base64 webm/opus，累积完整音频）
    toggle_asr   → 切换 ASR 开关
    toggle_tts   → 切换 TTS 开关

  Server → Client:
    stream_content   → 正文 token（流式显示）
    stream_reasoning → 思考 token（思考模式）
    stream_end       → 文本流结束
    stream_tool_call   → 工具调用开始（name + args）
    stream_tool_result → 工具执行结果（name + result）
    stream_audio     → TTS 音频片段（流式播放）
    audio            → 完整音频（非流式备用）
    asr_partial      → ASR 中间识别结果（可能变化）
    asr_final        → ASR 最终句子（已确认）
    asr_auto_send    → 静默后自动发送文本给 Agent
    status           → 状态信息
    error            → 错误信息
"""

import asyncio
import base64
import json
import re
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from code_demo.core.utils.emotion_mapper import EmotionMapper
from code_demo.core.utils.emotion_fsm import EmotionFSM
from code_demo.core.cognitive import CognitiveEngine, CognitiveMemory, SessionContext

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ── 项目路径 ──
PROJECT_ROOT = Path(__file__).parent.parent.parent
WEB_HTML_DIR = Path(__file__).parent.parent / "web_html"
LIVE2D_DIR = Path(__file__).parent.parent / "live2d"

# ── SKILL.md 完整格式示例（导入校验失败时返回给前端）──
SKILL_FORMAT_EXAMPLE = """\
---
name: my-skill
description: 当用户要求 xxx 时使用此技能。
---

# 技能标题

你现在具备 xxx 的能力。请遵循以下工作流：

## 步骤一

**选项 1：快速方法（推荐）**
```bash
echo "hello"
```

**选项 2：详细方法**
```python
print("hello")
```

## 步骤二

详细描述...
"""

# ── Agent（全局单例，延迟初始化）──
_agent = None
_agent_lock = threading.Lock()

# ── CompanionAgent（全局单例，延迟初始化）──
_companion = None
_companion_lock = threading.Lock()

# ── AgentTeam 链路可见性（运行时可切换）──
from code_demo.core.config import COMPANION_SHOW_AGENT_TEAM, AI_CONFIG_DIR
from code_demo.core.utils.skill_conflict import SkillConflictDetector, SkillMerger, SkillLifecycleManager
_show_agent_team = COMPANION_SHOW_AGENT_TEAM

# ── 权限审批（跨线程 WebSocket 通信）──
_event_loop: asyncio.AbstractEventLoop | None = None
_active_websockets: set = set()


class PermissionRequestManager:
    """跨线程权限审批管理器。

    Agent 线程发起审批请求 → 推送 permission_request 到前端 →
    前端回传 permission_response → 唤醒 Agent 线程继续执行。
    """

    def __init__(self):
        self._pending: dict[str, dict] = {}  # request_id → {event, result, ...}
        self._lock = threading.Lock()  # 保护跨线程权限请求状态的锁

    def create_request(self, intent: dict, tool_input: dict) -> str:
        request_id = str(uuid.uuid4())[:8]
        self._pending[request_id] = {
            "event": threading.Event(),
            "result": None,
            "intent": intent,
            "tool_input": tool_input,
        }
        return request_id

    def resolve(self, request_id: str, action: str):
        with self._lock:
            req = self._pending.get(request_id)
            if req:
                req["result"] = action
                req["event"].set()

    def wait(self, request_id: str, timeout: float = 120) -> str:
        req = self._pending.get(request_id)
        if not req:
            return "deny"
        req["event"].wait(timeout=timeout)
        # result 为 None 表示等待超时（前端未响应），返回特殊值供文案区分
        result = req["result"] or "timeout"
        self._pending.pop(request_id, None)
        return result


_permission_mgr = PermissionRequestManager()


def _ws_ask_callback(intent: dict, tool_input: dict) -> dict:
    """WebSocket 审批回调 — 在 Agent 线程中调用。

    将审批请求广播到所有活跃 WebSocket 连接，
    然后阻塞等待前端响应（120 秒超时）。
    """
    request_id = _permission_mgr.create_request(intent, tool_input)

    request_msg = {
        "request_id": request_id,
        "tool_name": intent.get("tool", ""),
        "tool_args": tool_input,
        "risk_level": intent.get("risk", ""),
        "reason": f"{intent.get('risk', '')} 风险操作需要确认",
    }

    # 广播到所有活跃的 WebSocket 连接
    if _event_loop and _active_websockets:
        for ws in list(_active_websockets):
            try:
                asyncio.run_coroutine_threadsafe(
                    _send_json(ws, "permission_request", request_msg),
                    _event_loop,
                )
            except Exception:
                pass

    # 阻塞等待前端响应（120 秒超时自动拒绝）
    action = _permission_mgr.wait(request_id, timeout=120)
    timed_out = action == "timeout"
    if timed_out:
        action = "deny"  # 对下游流程仍表现为 deny，仅 reason 文案区分
    return {
        "approved": action in ("approve", "always"),
        "action": action,
        "reason": (
            "权限确认超时，已自动拒绝" if timed_out
            else ("" if action != "deny" else "用户拒绝了此操作")
        ),
    }


def _get_agent():
    """获取全局 Agent 实例（线程安全单例）。"""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        from code_demo.core.agent import Agent
        _agent = Agent(
            name="web_agent",
            permission_mode="auto",
            voice_mode=False,
        )
        return _agent


def _get_companion():
    """获取全局 CompanionAgent 实例（线程安全单例，始终创建）。"""
    global _companion
    if _companion is not None:
        return _companion
    with _companion_lock:
        if _companion is not None:
            return _companion
        agent = _get_agent()

        from code_demo.core.config import COMPANION_PERSONA_PROMPT
        from code_demo.core.agent_team import AgentTeam
        from code_demo.core.companion_agent import CompanionAgent

        from code_demo.core.utils import SKILL_REGISTRY
        team = AgentTeam(agent, skill_registry=SKILL_REGISTRY)
        _companion = CompanionAgent(
            agent=agent,
            agent_team=team,
            persona_prompt=COMPANION_PERSONA_PROMPT,
        )
        print("[Server] CompanionAgent 就绪")
        return _companion


def _clean_tts_text(text: str) -> str:
    """清洗文本用于 TTS 合成（去除 markdown、think 标签、代码块等）。

    提取自 push_token 闭包中的 12 步清洗逻辑，
    供 push_token 及 Phase 2/3 过滤回调共享。
    """
    clean = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    clean = re.sub(r'```[\s\S]*?```', '', clean)
    clean = re.sub(r'`[^`]+`', '', clean)
    # 去除 markdown 链接 [text](url) → text
    clean = re.sub(r'\[([^\]]*)\]\([^\)]*\)', r'\1', clean)
    # 去除 markdown 格式：**粗体**、*斜体*、_斜体_
    clean = re.sub(r'\*\*(.+?)\*\*', r'\1', clean)
    clean = re.sub(r'\*(.+?)\*', r'\1', clean)
    clean = re.sub(r'_(.+?)_', r'\1', clean)
    # 去除 markdown 标题符号 (#, *, -)
    clean = re.sub(r'^[#*\-\d.]+\s*', '', clean, flags=re.MULTILINE)
    # 去除表格行 (| xxx | xxx |)
    clean = re.sub(r'^\|.*\|$', '', clean, flags=re.MULTILINE)
    # 去除表格分隔行 (|---|---|)
    clean = re.sub(r'^\|?[\s\-:|]+\|?$', '', clean, flags=re.MULTILINE)
    # 去除常见 emoji
    clean = re.sub(r'[👋📖💻📋💬🔍⚠️✅❌🎉🚀💡🔧📝🎯🔒🔓📦🗂️📊📈📉🛠️🔑🏗️📐🧩🎨🎭🏆]', '', clean)
    # 去除 URL
    clean = re.sub(r'https?://\S+', '', clean)
    # 合并多个空白字符
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


async def _gather_realtime_context(text: str, log_tag: str = "RealtimeContext") -> str:
    """程序化收集只读实时信息（记忆召回/天气/本地时间），返回可注入 prompt 的文本。

    供 speak 通道（_speak_gather_context）与用户对话通道（Phase 1 前置注入）共享：
    - 记忆召回：基于文本的轻量关键词匹配，仅在命中高相关记忆时注入
    - 天气：仅当文本含天气关键词时查询 wttr.in（3s 超时，失败/超时静默跳过）
    - 时间：始终注入一行本地时间（零成本，消除时间类执行幻觉）
    所有操作只读、带超时，不阻塞事件循环。
    """
    extra_context = ""

    # 1. 记忆召回：基于文本关键词检索用户记忆
    try:
        from code_demo.core.utils.memory import MemoryManager
        mm = MemoryManager()
        mm.load_all()
        if mm.memories:
            # 用文本做简单关键词匹配
            keywords = text[:30]
            matched = []
            for name, mem in mm.memories.items():
                desc = mem.get('description', '') + mem.get('content', '')[:100]
                if any(kw in desc for kw in keywords.split() if len(kw) >= 2):
                    matched.append(f"- {name}: {mem.get('description', '')[:50]}")
                if len(matched) >= 2:
                    break
            if matched:
                extra_context += "\n【相关记忆】\n" + "\n".join(matched)
    except Exception as e:
        print(f"[{log_tag}] 记忆召回失败: {e}")

    # 2. 联网天气：仅当文本涉及天气/实时信息时查询 wttr.in
    time_related_kw = ["天气", "温度", "新闻", "热搜", "今天", "明天", "最近"]
    if any(kw in text for kw in time_related_kw):
        if "天气" in text or "温度" in text:
            def _fetch_weather():
                import urllib.request
                # 用 wttr.in 获取简洁天气（轻量，无需 API key）
                req = urllib.request.Request(
                    "https://wttr.in/?format=%l:+%c+%t+%h",
                    headers={"User-Agent": "curl/7.0"}
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return resp.read().decode("utf-8").strip()
            try:
                loop = asyncio.get_event_loop()
                weather_info = await asyncio.wait_for(
                    loop.run_in_executor(None, _fetch_weather), timeout=3.5
                )
                if weather_info:
                    extra_context += f"\n【实时天气】{weather_info}"
            except Exception as e:
                print(f"[{log_tag}] 联网获取失败: {e}")

    # 3. 时间感知（始终注入）
    lt = time.localtime()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    extra_context += f"\n【当前时间】{lt.tm_hour}:{lt.tm_min:02d} {weekdays[lt.tm_wday]}"

    return extra_context


# ── EXECUTE 启发式兜底（Phase 1 误判纠正）──────────────────────
# 设计取舍：单个动词裸匹配（如"看看"）会误伤纯闲聊（"看看你多可爱"），
# 因此采用两层置信度：
#   1. 强关键词——语义上几乎必然需要真实执行，单独命中即升级；
#   2. 操作动词 + 操作对象组合——两者共现才升级，降低误报；
# 另外时间/天气/回忆类问题已由前置只读注入直接回答，显式排除，
# 避免把本应 chat/answer 直答的请求抢去执行。

# 强关键词：单独命中即判定需要真实执行
_EXEC_STRONG_KEYWORDS = [
    "截图", "截屏", "screenshot", "帮我点", "帮我按", "帮我输入",
    "鼠标", "键盘", "运行命令", "执行命令", "跑一下", "看到我的",
    "搜一下", "搜索一下", "帮我搜", "网上查", "百度一下", "谷歌一下", "google一下",
]
# 操作动词：需与操作对象共现才升级
_EXEC_VERBS = [
    "查看", "看看", "看一下", "帮我看", "找找", "找一下", "查一下", "查查",
    "列出", "打开", "运行", "执行", "创建", "新建", "写个", "写一个",
    "删掉", "删除", "修改", "改一下", "统计", "分析", "搜索", "搜一", "检索", "整理",
    "读", "念",
]
# 操作对象：文件系统 / 屏幕 / 网络 / 代码等真实系统资源
_EXEC_OBJECTS = [
    "文件", "目录", "文件夹", "桌面", "屏幕", "项目", "代码", "脚本",
    "日志", "网上", "网页", "网络", "浏览器", "终端", "命令", "进程",
    "仓库", "分支", "接口", "数据库", ".py", ".js", ".md", ".txt", "readme", "python", "json",
]
# 排除模式：时间/天气/回忆类由前置只读注入负责，不委派执行
_EXEC_EXCLUDE_PATTERNS = [
    "几点", "什么时候了", "天气", "气温", "下雨", "聊过", "说过什么", "还记得",
]


def _should_force_execute(user_input: str, has_attached_screenshot: bool = False) -> bool:
    """启发式判断用户消息是否需要真实工具执行（Phase 1 误判兜底）。"""
    if not user_input:
        return False
    text = user_input.lower()
    if any(p in text for p in _EXEC_EXCLUDE_PATTERNS):
        return False
    strong_keywords = _EXEC_STRONG_KEYWORDS
    verbs = _EXEC_VERBS
    objects = _EXEC_OBJECTS
    if has_attached_screenshot:
        # 当前请求可直接依据已附截图作答；仍保留点击/输入等真实操作的执行路由。
        strong_keywords = [kw for kw in strong_keywords if kw not in {"截图", "截屏", "screenshot", "看到我的"}]
        verbs = [v for v in verbs if v not in {"查看", "看看", "看一下", "帮我看"}]
        objects = [o for o in objects if o not in {"屏幕", "桌面"}]
    if any(kw in text for kw in strong_keywords):
        return True
    return any(v in text for v in verbs) and any(o in text for o in objects)


class StaticNoCacheMiddleware:
    """纯 ASGI 中间件：仅对 /static/ HTTP 请求注入 Cache-Control，WebSocket 直通。"""

    def __init__(self, app):
        self.app = app  # 需要添加无缓存响应头的 ASGI 应用

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/static/"):
            async def send_with_cache_control(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, send_with_cache_control)
        else:
            await self.app(scope, receive, send)


def create_app(
    enable_asr: bool = False,
    enable_tts: bool = False,
) -> FastAPI:
    """创建 FastAPI 应用实例。

    Args:
        enable_asr: 是否启动时启用 ASR
        enable_tts: 是否启动时启用 TTS
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """服务生命周期：启动/关闭事件。"""
        global _event_loop
        _event_loop = asyncio.get_event_loop()

        # ── startup ──
        agent = _get_agent()
        # 注入 WebSocket 审批回调（替代终端 input()）
        agent.permission_gate.set_ask_callback(_ws_ask_callback)

        if enable_tts:
            agent._init_tts_engine()
            print(f"[Server] TTS 引擎已预加载（默认关闭，用户按需开启）")
        if enable_asr:
            api_key = os.getenv("DASHSCOPE_API_KEY", "")
            if api_key:
                print(f"[Server] ASR 就绪（dashscope 云识别）")
            else:
                print(f"[Server] 警告：DASHSCOPE_API_KEY 未设置，ASR 不可用")
        # ── 清理低质量守卫技能 ──
        from code_demo.core.utils import SKILL_REGISTRY
        removed = SKILL_REGISTRY.cleanup_low_quality_guards()
        if removed:
            print(f"[Server] 已清理 {len(removed)} 个低质量守卫技能: {', '.join(removed)}")
        print(f"[Server] Agent 就绪")
        yield
        # ── shutdown ──
        global _agent, _companion
        if _agent:
            _agent.stop()
            _agent = None
        _companion = None
        _event_loop = None

    app = FastAPI(
        title="芙芙",
        description="ASR → Agent → TTS 语音交互服务",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ── CORS ──
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 静态资源 Cache-Control（纯 ASGI，不阻塞 WebSocket）──
    app.add_middleware(StaticNoCacheMiddleware)

    # ── HTTP 端点 ──

    @app.get("/health")
    async def health():
        agent = _get_agent()
        from code_demo.core.config import DEFAULT_WORKDIR
        model_info = agent.get_current_model_info()
        provider_models = agent.list_available_models()
        _index_path = WEB_HTML_DIR / "index.html"
        _static_ver = str(int(os.path.getmtime(str(_index_path)))) if _index_path.exists() else "0"
        return {
            "status": "ok",
            "agent": agent.name,
            "tts_mode": agent.tts_mode,
            "asr_ready": bool(os.getenv("DASHSCOPE_API_KEY", "")),
            "model": model_info["model"],
            "provider_id": model_info.get("provider_id"),
            "provider_name": model_info.get("provider_name", ""),
            "provider_models": provider_models,
            "default_workspace": str(DEFAULT_WORKDIR),
            "workspace": str(agent.workdir),
            "show_agent_team": _show_agent_team,
            "permission_mode": agent.permission_gate.mode,
            "static_version": _static_ver,
        }

    @app.get("/api/browse")
    async def browse_directory(path: str = ""):
        """列出目录内容（仅目录，用于工作空间选择器）"""
        if not path:
            p = Path.home()
        else:
            p = Path(path).expanduser().resolve()

        if not p.exists() or not p.is_dir():
            return JSONResponse({"ok": False, "error": "目录不存在"}, 404)

        entries = []
        try:
            for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if item.name.startswith('.'):
                    continue
                if item.is_dir():
                    entries.append({
                        "name": item.name,
                        "type": "dir",
                        "path": str(item),
                    })
        except PermissionError:
            return JSONResponse({"ok": False, "error": "无权限访问"}, 403)

        return {
            "ok": True,
            "current": str(p),
            "parent": str(p.parent) if p.parent != p else None,
            "entries": entries,
        }

    # ── 模型供应商端点 ──

    @app.get("/api/providers")
    async def list_providers():
        """返回所有模型供应商（api_key 脱敏）。"""
        agent = _get_agent()
        providers = agent.provider_manager.list_providers(mask_keys=True)
        active_id = agent.provider_manager.get_active_id()
        return {"ok": True, "providers": providers, "active_provider_id": active_id}

    @app.post("/api/providers/add")
    async def add_provider(request: Request):
        """添加模型供应商。"""
        body = await request.json()
        name = body.get("name", "").strip()
        url = body.get("url", "").strip()
        api_key = body.get("api_key", "").strip()
        models = body.get("models", [])
        if not name or not url or not api_key:
            return JSONResponse({"ok": False, "error": "名称、URL 和 API Key 不能为空"}, 400)
        agent = _get_agent()
        provider_id = agent.provider_manager.add_provider(name, url, api_key, models)
        return {"ok": True, "id": provider_id}

    @app.post("/api/providers/update")
    async def update_provider(request: Request):
        """更新模型供应商。api_key 为空或包含脱敏标志 **** 时保留原值。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        updates = {}
        for key in ("name", "url", "models"):
            if key in body:
                updates[key] = body[key]
        # 对 api_key 特殊处理：为空、或包含脱敏标志 **** 时，保留原值不更新
        new_api_key = body.get("api_key", "")
        if new_api_key and "****" not in new_api_key:
            updates["api_key"] = new_api_key
        ok = agent.provider_manager.update_provider(provider_id, updates)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        return {"ok": True}

    @app.post("/api/providers/delete")
    async def delete_provider(request: Request):
        """删除模型供应商。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        ok = agent.provider_manager.delete_provider(provider_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        # 如果删除的是当前活跃供应商，重置 agent 模型
        if agent.current_provider_id == provider_id:
            agent.current_provider_id = None
            from code_demo.core.config import MODEL
            agent.current_model = MODEL
        return {"ok": True}

    @app.post("/api/providers/discover")
    async def discover_provider_models(request: Request):
        """自动发现供应商的可用模型。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        try:
            models = agent.provider_manager.discover_models(provider_id)
            # 自动更新供应商的模型列表
            agent.provider_manager.update_provider(provider_id, {"models": models})
            return {"ok": True, "models": models}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, 500)

    @app.post("/api/providers/set-active")
    async def set_active_provider(request: Request):
        """设置活跃模型供应商。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        ok = agent.provider_manager.set_active(provider_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        return {"ok": True}

    # ── 默认模型配置端点 ──

    @app.get("/api/default-models")
    async def get_default_models():
        """读取默认模型配置。"""
        config_path = AI_CONFIG_DIR / "default_models.json"
        if config_path.exists():
            return JSONResponse(json.loads(config_path.read_text("utf-8")))
        return JSONResponse({
            "companion_model": None,
            "agent_team_model": None,
            "ocr_model": None,
        })

    @app.post("/api/default-models")
    async def save_default_models(request: Request):
        """保存默认模型配置。"""
        data = await request.json()
        config_path = AI_CONFIG_DIR / "default_models.json"
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        return JSONResponse({"success": True})

    # ── 语音供应商端点 (TTS/ASR) ──

    @app.get("/api/voice-providers")
    async def list_voice_providers():
        """返回所有语音供应商（TTS + ASR）及活跃状态。"""
        agent = _get_agent()
        vpm = agent.voice_provider_manager
        return {
            "ok": True,
            "tts_providers": vpm.list_tts_providers(mask_keys=True),
            "asr_providers": vpm.list_asr_providers(mask_keys=True),
            "active_tts_provider_id": vpm.get_active_tts_id(),
            "active_asr_provider_id": vpm.get_active_asr_id(),
            "tts_fallback_to_local": vpm.get_tts_fallback(),
        }

    @app.post("/api/voice-providers/tts/add")
    async def add_tts_provider(request: Request):
        """添加 TTS 供应商。"""
        body = await request.json()
        name = body.get("name", "").strip()
        url = body.get("url", "").strip()
        api_key = body.get("api_key", "").strip()
        config = body.get("config", {})
        if not name or not url:
            return JSONResponse({"ok": False, "error": "名称和 URL 不能为空"}, 400)
        agent = _get_agent()
        provider_id = agent.voice_provider_manager.add_tts_provider(name, url, api_key, config)
        return {"ok": True, "id": provider_id}

    @app.post("/api/voice-providers/tts/update")
    async def update_tts_provider(request: Request):
        """更新 TTS 供应商。api_key 为空或包含脱敏标志 **** 时保留原值。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        updates = {}
        for key in ("name", "url", "config"):
            if key in body:
                updates[key] = body[key]
        # 对 api_key 特殊处理：为空、或包含脱敏标志 **** 时，保留原值不更新
        new_api_key = body.get("api_key", "")
        if new_api_key and "****" not in new_api_key:
            updates["api_key"] = new_api_key
        ok = agent.voice_provider_manager.update_tts_provider(provider_id, updates)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        return {"ok": True}

    @app.post("/api/voice-providers/tts/delete")
    async def delete_tts_provider(request: Request):
        """删除 TTS 供应商。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        ok = agent.voice_provider_manager.delete_tts_provider(provider_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        return {"ok": True}

    @app.post("/api/voice-providers/tts/set-active")
    async def set_active_tts_provider(request: Request):
        """设置活跃 TTS 供应商。id 为空时取消激活（仅使用本地）。"""
        body = await request.json()
        provider_id = body.get("id")  # None = 取消激活
        agent = _get_agent()
        ok = agent.voice_provider_manager.set_active_tts(provider_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        return {"ok": True}

    @app.post("/api/voice-providers/tts/test")
    async def test_tts_provider(request: Request):
        """测试 TTS 供应商连通性。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        result = agent.voice_provider_manager.check_tts_health(provider_id)
        if result.get("status") == "error":
            return JSONResponse({"ok": False, "error": result.get("error", "连接失败")}, 500)
        return {"ok": True, "health": result}

    @app.post("/api/voice-providers/tts/set-fallback")
    async def set_tts_fallback(request: Request):
        """设置 TTS 回退到本地开关。"""
        body = await request.json()
        enabled = body.get("enabled", True)
        agent = _get_agent()
        agent.voice_provider_manager.set_tts_fallback(enabled)
        return {"ok": True}

    @app.post("/api/voice-providers/asr/add")
    async def add_asr_provider(request: Request):
        """添加 ASR 供应商（预留）。"""
        body = await request.json()
        name = body.get("name", "").strip()
        url = body.get("url", "").strip()
        api_key = body.get("api_key", "").strip()
        config = body.get("config", {})
        if not name or not url:
            return JSONResponse({"ok": False, "error": "名称和 URL 不能为空"}, 400)
        agent = _get_agent()
        provider_id = agent.voice_provider_manager.add_asr_provider(name, url, api_key, config)
        return {"ok": True, "id": provider_id}

    @app.post("/api/voice-providers/asr/delete")
    async def delete_asr_provider(request: Request):
        """删除 ASR 供应商。"""
        body = await request.json()
        provider_id = body.get("id", "")
        if not provider_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, 400)
        agent = _get_agent()
        ok = agent.voice_provider_manager.delete_asr_provider(provider_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "供应商不存在"}, 404)
        return {"ok": True}

    # ── Companion 伴侣层端点 ──

    @app.get("/api/companion/show-agent-team")
    async def get_show_agent_team():
        """返回 AgentTeam 链路可见性状态。"""
        return {"show_agent_team": _show_agent_team}

    @app.post("/api/companion/show-agent-team")
    async def set_show_agent_team(request: Request):
        """切换 AgentTeam 链路可见性。"""
        global _show_agent_team
        body = await request.json()
        _show_agent_team = bool(body.get("show_agent_team", False))
        print(f"[Server] AgentTeam 链路可见性: {_show_agent_team}")
        return {"ok": True, "show_agent_team": _show_agent_team}

    @app.get("/api/mcp-status")
    async def mcp_status():
        """返回所有 MCP 服务器的状态（含已连接、已禁用、错误信息）。"""
        agent = _get_agent()
        from code_demo.core.tools.mcp.config_loader import MCPConfigLoader
        config_loader = MCPConfigLoader()
        all_servers = config_loader.list_all()  # {name: {**cfg, _disabled: bool}}
        connected = agent.mcp_router.clients     # {name: MCPClient|MCPHttpClient}

        servers = []
        for name, cfg in all_servers.items():
            is_connected = name in connected
            tool_count = len(connected[name]._tools) if is_connected else 0
            error = agent.mcp_errors.get(name)
            servers.append({
                "name": name,
                "type": cfg.get("type", "stdio"),
                "connected": is_connected,
                "disabled": cfg.get("_disabled", False),
                "tool_count": tool_count,
                "error": error,
            })
        return {"servers": servers}

    @app.post("/api/mcp-toggle")
    async def mcp_toggle(request: Request):
        """运行时启用/禁用 MCP 服务器。"""
        body = await request.json()
        name = body.get("name", "")
        action = body.get("action", "")
        agent = _get_agent()

        if action == "disable":
            agent.disconnect_mcp_server(name)
            return {"ok": True, "connected": False}
        elif action == "enable":
            from code_demo.core.tools.mcp.config_loader import MCPConfigLoader
            config_loader = MCPConfigLoader()
            all_cfg = config_loader.list_all()
            cfg = all_cfg.get(name)
            if not cfg:
                return JSONResponse({"error": f"服务器 {name} 未在配置中找到"}, status_code=404)
            # 移除 _disabled 标记（不是真正的配置字段）
            cfg = {k: v for k, v in cfg.items() if k != "_disabled"}
            ok, err = agent.connect_mcp_server(name, cfg)
            tool_count = 0
            if ok and name in agent.mcp_router.clients:
                tool_count = len(agent.mcp_router.clients[name]._tools)
            return {"ok": ok, "connected": ok, "error": err, "tool_count": tool_count}
        else:
            return JSONResponse({"error": f"未知 action: {action}"}, status_code=400)

    @app.post("/api/mcp-add")
    async def mcp_add(request: Request):
        """添加新 MCP 服务器到配置并尝试连接。"""
        body = await request.json()
        name = body.get("name", "").strip()
        cfg = body.get("config", {})

        if not name:
            return JSONResponse({"error": "服务器名称不能为空"}, status_code=400)

        agent = _get_agent()
        from code_demo.core.tools.mcp.config_loader import MCPConfigLoader
        config_loader = MCPConfigLoader()

        # 检查重名
        all_cfg = config_loader.list_all()
        if name in all_cfg:
            return JSONResponse({"error": f"服务器 {name} 已存在"}, status_code=409)

        # 写入配置（默认 disabled: false）
        cfg["disabled"] = False
        config_loader.add_server(name, cfg)

        # 尝试连接
        ok, err = agent.connect_mcp_server(name, cfg)
        tool_count = 0
        if ok and name in agent.mcp_router.clients:
            tool_count = len(agent.mcp_router.clients[name]._tools)

        return {
            "ok": ok,
            "name": name,
            "connected": ok,
            "error": err,
            "tool_count": tool_count,
        }

    @app.get("/api/mcp-tools/{server_name}")
    async def mcp_tools(server_name: str):
        """获取某个 MCP 服务器的所有工具及其启用状态。"""
        agent = _get_agent()
        tools = agent.get_mcp_server_tools(server_name)
        return {"tools": tools}

    @app.post("/api/mcp-tool-toggle")
    async def mcp_tool_toggle(request: Request):
        """启用/禁用单个 MCP 工具。"""
        body = await request.json()
        server = body.get("server", "")
        tool = body.get("tool", "")
        enable = body.get("enable", True)
        agent = _get_agent()
        agent.toggle_mcp_tool(server, tool, enable)
        return {"ok": True}

    @app.get("/api/skills")
    async def list_skills():
        """返回所有已注册的 Skills 列表（含 body + 元数据用于编辑/分组）。"""
        from code_demo.core.utils import SKILL_REGISTRY
        skills = []
        for name, doc in SKILL_REGISTRY.documents.items():
            if not doc.manifest.visible:
                continue
            skills.append({
                "name": doc.manifest.name,
                "description": doc.manifest.description,
                "body": doc.body,
                "source": doc.manifest.source,
                "locked": doc.manifest.locked,
                "usage_count": doc.manifest.usage_count,
                "status": doc.manifest.status,
                "lineage": doc.manifest.lineage,
            })
        return {"skills": skills}

    @app.post("/api/skills/cleanup")
    async def cleanup_skills():
        """手动触发清理低质量守卫技能。"""
        from code_demo.core.utils import SKILL_REGISTRY
        removed = SKILL_REGISTRY.cleanup_low_quality_guards()
        return {"ok": True, "removed": removed, "count": len(removed)}

    @app.post("/api/skills/add")
    async def add_skill(request: Request):
        """新增一个 Skill（写入 SKILLS_DIR/<slug>/SKILL.md）。"""
        from code_demo.core.utils import SKILL_REGISTRY
        body = await request.json()
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        skill_body = (body.get("body") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "名称不能为空"}, status_code=400)
        if not skill_body:
            return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)
        # 去掉 body 中可能误输入的 frontmatter 块（--- ... ---），以 name/description 为准
        skill_body = re.sub(r"^---\n.*?\n---\n*", "", skill_body, count=1, flags=re.DOTALL)
        SKILL_REGISTRY.add_skill(name, description or name, skill_body.strip())
        return {"ok": True}

    @app.post("/api/skills/import")
    async def import_skill(request: Request):
        """导入完整 SKILL.md 文本，校验 frontmatter 格式。"""
        from code_demo.core.utils import SKILL_REGISTRY
        body = await request.json()
        content = (body.get("content") or "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)
        # 校验 frontmatter
        fm_match = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
        if not fm_match:
            return JSONResponse({
                "ok": False,
                "error": "格式不符合 SKILL.md 规范：缺少 frontmatter 头部（--- ... ---）",
                "example": SKILL_FORMAT_EXAMPLE,
            }, status_code=400)
        meta = {}
        for line in fm_match.group(1).strip().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
        name = meta.get("name", "").strip()
        description = meta.get("description", "").strip()
        skill_body = fm_match.group(2).strip()
        if not name:
            return JSONResponse({
                "ok": False,
                "error": "frontmatter 中缺少 name 字段",
                "example": SKILL_FORMAT_EXAMPLE,
            }, status_code=400)
        if not skill_body:
            return JSONResponse({
                "ok": False,
                "error": "frontmatter 之后没有正文内容",
                "example": SKILL_FORMAT_EXAMPLE,
            }, status_code=400)
        SKILL_REGISTRY.add_skill(name, description or name, skill_body)
        return {"ok": True, "name": name}

    @app.post("/api/skills/update")
    async def update_skill(request: Request):
        """更新已有 Skill（可改名）。"""
        from code_demo.core.utils import SKILL_REGISTRY
        body = await request.json()
        old_name = (body.get("old_name") or "").strip()
        new_name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        skill_body = (body.get("body") or "").strip()
        if not old_name:
            return JSONResponse({"ok": False, "error": "原名称不能为空"}, status_code=400)
        if not new_name:
            return JSONResponse({"ok": False, "error": "名称不能为空"}, status_code=400)
        if not skill_body:
            return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)
        if old_name not in SKILL_REGISTRY.documents:
            return JSONResponse({"ok": False, "error": f"Skill '{old_name}' 不存在"}, status_code=404)
        skill_body = re.sub(r"^---\n.*?\n---\n*", "", skill_body, count=1, flags=re.DOTALL)
        SKILL_REGISTRY.update_skill(old_name, new_name, description or new_name, skill_body.strip())
        return {"ok": True}

    @app.post("/api/mcp/delete")
    async def mcp_delete(request: Request):
        """删除 MCP 服务器：断开连接 + 从 mcp_config.json 移除。"""
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "服务器名称不能为空"}, status_code=400)

        agent = _get_agent()
        # 先断开连接
        if name in agent.mcp_router.clients:
            agent.disconnect_mcp_server(name)

        # 从配置文件删除
        from code_demo.core.tools.mcp.config_loader import MCPConfigLoader
        config_loader = MCPConfigLoader()
        if config_loader.delete_server(name):
            return {"ok": True}
        return JSONResponse({"ok": False, "error": f"服务器 {name} 未在配置中找到"}, status_code=404)

    @app.post("/api/mcp/batch-delete")
    async def mcp_batch_delete(request: Request):
        """批量删除 MCP 服务器：逐个断开连接 + 从 mcp_config.json 移除。"""
        body = await request.json()
        names = body.get("names", [])
        if not names:
            return JSONResponse({"ok": False, "error": "未提供服务器名称列表"}, status_code=400)

        agent = _get_agent()
        from code_demo.core.tools.mcp.config_loader import MCPConfigLoader
        config_loader = MCPConfigLoader()
        deleted, failed = [], []
        for name in names:
            if name in agent.mcp_router.clients:
                agent.disconnect_mcp_server(name)
            if config_loader.delete_server(name):
                deleted.append(name)
            else:
                failed.append(name)
        return {"ok": True, "deleted": deleted, "failed": failed}

    @app.post("/api/skills/delete")
    async def delete_skill(request: Request):
        """删除指定 Skill 的整个目录。"""
        from code_demo.core.utils import SKILL_REGISTRY
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "名称不能为空"}, status_code=400)
        if SKILL_REGISTRY.delete_skill(name):
            return {"ok": True}
        return JSONResponse({"ok": False, "error": f"Skill '{name}' 不存在"}, status_code=404)

    @app.post("/api/skills/set-locked")
    async def skills_set_locked(request: Request):
        """切换技能锁定状态。"""
        from code_demo.core.utils import SKILL_REGISTRY
        data = await request.json()
        name = (data.get("name") or "").strip()

        if not name:
            return JSONResponse({"ok": False, "error": "缺少 name 参数"}, status_code=400)

        locked = data.get("locked")
        if not isinstance(locked, bool):
            return JSONResponse({
                "ok": False,
                "error": "locked 参数必须是布尔值（true/false）"
            }, status_code=400)

        if name not in SKILL_REGISTRY.documents:
            return JSONResponse({"ok": False, "error": f"技能「{name}」不存在"}, status_code=404)

        doc = SKILL_REGISTRY.documents[name]
        if not doc.manifest.visible:
            return JSONResponse({
                "ok": False,
                "error": f"内部技能「{name}」不允许修改锁定状态"
            }, status_code=403)

        success = SKILL_REGISTRY.set_locked(name, locked)
        if success:
            return JSONResponse({"ok": True, "name": name, "locked": locked})
        else:
            return JSONResponse({"ok": False, "error": "操作失败"}, status_code=500)

    @app.post("/api/skills/merge-suggestions")
    async def skills_merge_suggestions(request: Request):
        """返回可合并的技能对。"""
        from code_demo.core.utils import SKILL_REGISTRY
        data = await request.json() if await request.body() else {}
        threshold = data.get("threshold", 0.6)

        suggestions = SKILL_REGISTRY.suggest_merge(threshold=threshold)
        return JSONResponse({
            "suggestions": [
                {"skill_a": a, "skill_b": b, "similarity": round(sim, 3)}
                for a, b, sim in suggestions
            ]
        })

    @app.post("/api/skills/merge")
    async def skills_merge(request: Request):
        """合并两个技能。"""
        from code_demo.core.utils import SKILL_REGISTRY
        data = await request.json()
        name_a = data.get("name_a")
        name_b = data.get("name_b")
        merged_body = data.get("merged_body", "")

        if not name_a or not name_b:
            return JSONResponse({"error": "缺少 name_a 或 name_b"}, status_code=400)

        result = SKILL_REGISTRY.merge_skills(name_a, name_b, merged_body)
        if result:
            return JSONResponse({"ok": True, "merged_name": result})
        else:
            return JSONResponse({"error": "合并失败，技能可能已锁定或不存在"}, status_code=400)

    @app.post("/api/skills/check-conflicts")
    async def skills_check_conflicts():
        """检测当前所有 active skills 与内部 skills 的冲突。"""
        from code_demo.core.utils import SKILL_REGISTRY
        try:
            detector = SkillConflictDetector(SKILL_REGISTRY)
            conflicts = detector.detect_conflicts()
            return JSONResponse({
                "conflicts": [
                    {
                        "internal_skill": c.internal_skill,
                        "conflicting_skill": c.conflicting_skill,
                        "conflict_type": c.conflict_type,
                        "overlap_score": c.overlap_score,
                        "conflict_fields": c.conflict_fields,
                    }
                    for c in conflicts
                ]
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/skills/merge-conflict")
    async def skills_merge_conflict(request: Request):
        """执行冲突合并，生成主动学习 skill。"""
        from code_demo.core.utils import SKILL_REGISTRY
        try:
            data = await request.json()
            internal_name = data.get("internal_skill")
            conflicting_name = data.get("conflicting_skill")

            if not internal_name or not conflicting_name:
                return JSONResponse({"error": "缺少 internal_skill 或 conflicting_skill 参数"}, status_code=400)

            detector = SkillConflictDetector(SKILL_REGISTRY)
            conflicts = detector.detect_conflicts()
            target = next(
                (c for c in conflicts
                 if c.internal_skill == internal_name
                 and c.conflicting_skill == conflicting_name),
                None,
            )

            if not target:
                return JSONResponse({"error": "未找到指定冲突"}, status_code=404)

            merger = SkillMerger(SKILL_REGISTRY)
            result = merger.execute_conflict_merge(target)

            await _broadcast_skill_event("skill_merged", {
                "new_skill": result.new_name,
                "retired": result.retired_skills,
            })

            return JSONResponse({
                "new_skill": result.new_name,
                "description": result.new_description,
                "retired_skills": result.retired_skills,
                "lineage": result.lineage,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/skills/retired")
    async def get_retired_skills():
        """获取已下线 skills 列表。"""
        from code_demo.core.utils import SKILL_REGISTRY
        try:
            retired = SKILL_REGISTRY.list_retired_skills()
            return JSONResponse({"retired_skills": retired})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/skills/restore")
    async def restore_skill(request: Request):
        """恢复已下线 skill。"""
        from code_demo.core.utils import SKILL_REGISTRY
        try:
            data = await request.json()
            name = data.get("name")
            if not name:
                return JSONResponse({"error": "缺少 name 参数"}, status_code=400)

            lifecycle = SkillLifecycleManager(SKILL_REGISTRY)
            result = lifecycle.restore_retired_skill(name)

            await _broadcast_skill_event("skill_restored", {"name": name})

            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/skills/lineage/{name}")
    async def get_skill_lineage(name: str):
        """获取 skill 谱系树。"""
        from code_demo.core.utils import SKILL_REGISTRY
        try:
            lifecycle = SkillLifecycleManager(SKILL_REGISTRY)
            tree = lifecycle.get_lineage_tree(name)
            if not tree:
                return JSONResponse({"error": "未找到该技能"}, status_code=404)
            return JSONResponse({"lineage_tree": tree})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.delete("/api/skills/learned/{name}")
    async def delete_learned_skill(name: str):
        """删除主动学习 skill（触发回滚）。"""
        from code_demo.core.utils import SKILL_REGISTRY
        try:
            lifecycle = SkillLifecycleManager(SKILL_REGISTRY)
            result = lifecycle.delete_learned_skill(name)

            if result.get("restored"):
                await _broadcast_skill_event("skills_restored", {
                    "restored": result["restored"],
                    "deleted": result["deleted"],
                })

            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── 文件上传 ──

    UPLOAD_DIR = Path(__file__).parent.parent.parent / "ai_configuration" / ".uploads"
    TEXT_UPLOAD_EXTS = {
        'txt', 'md', 'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'html', 'css',
        'csv', 'log', 'yaml', 'yml', 'xml', 'toml', 'ini', 'cfg', 'sh', 'bash',
        'zsh', 'sql', 'r', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'hpp', 'rb',
    }
    IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg', 'tiff'}

    def _extract_file_content(fpath: Path, att_name: str, max_chars: int = 10000) -> str:
        """从上传文件中提取文本内容，供注入 LLM 上下文。

        支持：文本/代码文件、PDF、Word(.docx)、图片元信息。
        若解析库未安装则优雅降级为文件路径。
        """
        ext = att_name.rsplit('.', 1)[-1].lower() if '.' in att_name else ''

        # 1. 文本/代码文件 → 直接读取
        if ext in TEXT_UPLOAD_EXTS:
            try:
                return f"[文件: {att_name}]\n{fpath.read_text(encoding='utf-8', errors='replace')[:max_chars]}"
            except Exception:
                pass

        # 2. PDF → 提取全部页面文本
        if ext == 'pdf':
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(fpath))
                pages = []
                for i, page in enumerate(reader.pages):
                    text = page.extract_text()
                    if text and text.strip():
                        pages.append(f"--- 第 {i+1} 页 ---\n{text.strip()}")
                if pages:
                    combined = "\n\n".join(pages)
                    return f"[PDF 文件: {att_name} ({len(reader.pages)} 页)]\n{combined[:max_chars]}"
            except ImportError:
                pass
            except Exception as e:
                return f"[PDF 文件: {att_name}] 解析失败: {e}"

        # 3. Word (.docx) → 提取段落文本
        if ext == 'docx':
            try:
                import docx
                doc = docx.Document(str(fpath))
                paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                if paragraphs:
                    combined = "\n".join(paragraphs)
                    return f"[Word 文件: {att_name} ({len(paragraphs)} 段)]\n{combined[:max_chars]}"
            except ImportError:
                pass
            except Exception as e:
                return f"[Word 文件: {att_name}] 解析失败: {e}"

        # 4. 图片 → 元信息 + 路径（模型不支持视觉时）
        if ext in IMAGE_EXTS:
            try:
                from PIL import Image
                img = Image.open(str(fpath))
                w, h = img.size
                fmt = img.format or ext.upper()
                size_kb = fpath.stat().st_size / 1024
                return (f"[图片文件: {att_name}] {fmt} {w}x{h}, {size_kb:.1f}KB\n"
                        f"路径: {fpath}")
            except ImportError:
                size_kb = fpath.stat().st_size / 1024
                return f"[图片文件: {att_name}] {size_kb:.1f}KB, 路径: {fpath}"
            except Exception:
                pass

        # 5. 兜底 → 文件路径
        size_kb = fpath.stat().st_size / 1024
        return f"[文件: {att_name}] ({size_kb:.1f}KB, 路径: {fpath})"

    @app.post("/api/upload")
    async def upload_file(file: UploadFile = File(...)):
        """上传文件到 .uploads/ 目录，返回文件元信息。"""
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file_id = str(uuid.uuid4())[:8]
        safe_name = re.sub(r'[^\w.\-]', '_', file.filename or 'file')
        save_path = UPLOAD_DIR / f"{file_id}_{safe_name}"

        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "文件超过 50MB 限制"}, status_code=413)

        save_path.write_bytes(content)

        # 文本类文件返回前 2000 字符预览
        preview = None
        ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
        if (file.content_type and file.content_type.startswith('text/')) or ext in TEXT_UPLOAD_EXTS:
            try:
                preview = content.decode('utf-8', errors='replace')[:2000]
            except Exception:
                pass

        return {
            "ok": True,
            "id": file_id,
            "name": safe_name,
            "type": file.content_type or "application/octet-stream",
            "size": len(content),
            "preview": preview,
        }

    @app.get("/api/uploads/{file_id}")
    async def get_upload(file_id: str):
        """根据文件 ID 提供文件下载/预览。"""
        if not UPLOAD_DIR.exists():
            return JSONResponse({"ok": False, "error": "上传目录不存在"}, status_code=404)
        for f in UPLOAD_DIR.iterdir():
            if f.name.startswith(file_id + '_'):
                return FileResponse(str(f))
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)

    @app.get("/")
    async def index():
        index_file = WEB_HTML_DIR / "index.html"
        if index_file.exists():
            resp = FileResponse(str(index_file))
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp
        return JSONResponse({"error": "前端页面未找到"}, status_code=404)

    # ── 静态文件服务 ──
    if WEB_HTML_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_HTML_DIR)), name="static")

    # ── Live2D 模型资源 ──
    if LIVE2D_DIR.exists():
        app.mount("/live2d", StaticFiles(directory=str(LIVE2D_DIR)), name="live2d")

    # ── 并发会话状态 ──

    # ── companion_history 持久化 ──

    _COGNITIVE_STATE_DIR = AI_CONFIG_DIR / ".cognitive_state"
    _COMPANION_HISTORY_FILE = _COGNITIVE_STATE_DIR / "companion_history.json"

    def _save_companion_history(history: list):
        """将 companion_history 持久化到磁盘"""
        import json as _json
        try:
            _COGNITIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
            _COMPANION_HISTORY_FILE.write_text(
                _json.dumps({"history": history, "updated_at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_companion_history() -> list:
        """从磁盘加载 companion_history，失败时返回空列表"""
        import json as _json
        try:
            if _COMPANION_HISTORY_FILE.exists():
                data = _json.loads(_COMPANION_HISTORY_FILE.read_text(encoding="utf-8"))
                return data.get("history", [])
        except Exception:
            pass
        return []

    def _filter_internal_messages(history: list) -> list:
        """过滤掉 internal 标记的消息（自主行动产生），仅用于前端展示。
        
        AI 的 LLM 上下文中仍保留这些消息（in-memory history 不变），
        只是不发送给前端渲染。
        """
        return [msg for msg in history if not msg.get("internal")]

    class SessionState:
        """管理单个 WebSocket 连接的后台任务状态"""
        def __init__(self):
            self.running_task: asyncio.Task | None = None  # 当前会话正在执行的后台任务
            self.cancel_requested: bool = False  # 是否已请求取消当前任务
            self.current_step: int = 0  # 当前任务执行到的步骤序号
            self.current_tool: str = ""  # 当前正在调用的工具名称
            self.task_description: str = ""  # 当前任务的简要描述
            self.companion_history: list[dict] = _load_companion_history()  # 伴侣AI对话历史（仅user/assistant）
            self.last_user_input_time: float = time.time()   # 最后用户交互时间
            self.has_user_interacted: bool = False  # 首次用户输入前不触发自主发言/思考
            self.last_murmur_time: float = 0                 # 最后碎碎念时间
            self.session_start_time: float = time.time()     # 会话开始时间
            self.last_user_emotion: str = ""                  # 最近一次用户输入推断的情绪
            self.heartbeat_task: asyncio.Task | None = None  # 心跳协程引用
            self.heartbeat_active: bool = False              # 心跳开关
            self.message_generation: int = 0            # 消息代际计数器（用于检测并发打断）
            self.emotion_fsm = EmotionFSM()  # 管理会话情绪状态转换的状态机
            # 认知引擎（在 websocket handler 中初始化）
            self.cognitive_engine: Optional[CognitiveEngine] = None  # 当前会话绑定的认知引擎
            self._user_just_interrupted: bool = False  # 标记用户是否刚打断助手
            self.is_streaming: bool = False  # 流式输出互斥锁（防止 speak 和正常回复交叉）
            self.speak_awaiting_reply: bool = False  # AI speak 后等待用户回复
            self.consecutive_speaks: int = 0          # 连续 speak 次数（无用户回复间）
            self.phase1_pending_context: Optional[str] = None   # 续段信号：用户原话
            self.phase1_pending_reaction: Optional[str] = None  # 续段信号：Phase1已说内容

    def is_ws_connected_fn(ws: WebSocket):
        """返回检查 WebSocket 是否仍然连接的函数"""
        def _check():
            try:
                return ws.client_state.name == "CONNECTED"
            except Exception:
                return False
        return _check

    async def _drain_tts_queue(
        websocket: WebSocket,
        agent,
        tts_tasks: list,
        tts_results: dict,
        tts_lock: threading.Lock,
        is_connected,
    ):
        """流式 TTS：等待后台合成完成，按顺序推送音频"""
        if not agent.tts_mode or not tts_tasks:
            return
        await _send_json(websocket, "status", "正在合成语音...")
        for idx, _sentence in tts_tasks:
            if not is_connected():
                break
            waited = 0
            while idx not in tts_results and waited < 120:
                if waited % 5 == 0 and not is_connected():
                    break
                await asyncio.sleep(1)
                waited += 1
            if idx in tts_results and is_connected():
                try:
                    audio_bytes, source = tts_results.pop(idx)
                    if source == "fallback":
                        await _send_json(websocket, "status",
                                         "⚠ 远程 TTS 不可用，已回退到本地引擎")
                    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                    await _send_json(websocket, "stream_audio", audio_b64)
                except Exception as e:
                    print(f"[Server] 推送音频失败: {e}")
                    break

    async def handle_full_request(
        session: SessionState,
        websocket: WebSocket,
        msg: dict,
        companion,
    ):
        """正常流程：Phase1即时返回 + Phase2后台运行 + Phase3自动总结"""
        user_input = msg.get("content", "")
        original_content = user_input

        # 处理附件
        attachments = msg.get("attachments", [])
        if attachments:
            file_contexts = []
            for att in attachments:
                att_id = att.get("id", "")
                att_name = att.get("name", "")
                fpath = UPLOAD_DIR / f"{att_id}_{att_name}"
                if not fpath.exists():
                    continue
                extracted = _extract_file_content(fpath, att_name)
                file_contexts.append(extracted)
            if file_contexts:
                user_input = user_input + "\n\n" + "\n\n".join(file_contexts)

        context_limit = msg.get("context_limit")
        agent = _get_agent()
        loop = asyncio.get_event_loop()

        # 懒创建会话：首次发消息时自动创建
        if not agent.current_session_id:
            first_10 = user_input.strip()[:10]
            auto_name = first_10 + ("..." if len(user_input.strip()) > 10 else "")
            session_id = agent.new_session(auto_name)
            await websocket.send_json({
                "type": "session_created",
                "session_id": session_id,
                "name": auto_name,
            })

        thinking_enabled = msg.get("thinking", False)
        stop_push = [False]
        content_was_streamed = [False]
        reasoning_was_streamed = [False]
        full_reply = [""]  # 累积完整回复文本（用于情绪推断）
        session.is_streaming = True  # 设置流式锁，阻止认知循环 speak 干扰

        # 流式 TTS 状态管理
        sentence_buffer = ""
        sentence_boundary = set("。！？.!?")
        tts_tasks = []
        tts_results = {}
        tts_lock = threading.Lock()
        sentence_counter = [0]

        # token 推送回调（线程安全，复用现有 push_token 逻辑）
        def push_token(token_type, text):
            """线程安全的 token 推送 + 句子分段"""
            if stop_push[0]:
                return
            try:
                if token_type == "reasoning":
                    if not thinking_enabled:
                        reasoning_was_streamed[0] = True
                        return
                    asyncio.run_coroutine_threadsafe(
                        _send_json(websocket, "stream_reasoning", text),
                        loop
                    )
                    return

                content_was_streamed[0] = True
                full_reply[0] += text
                asyncio.run_coroutine_threadsafe(
                    _send_json(websocket, "stream_content", text),
                    loop
                )
                # TTS 开启时按句子分段缓存
                if agent.tts_mode:
                    nonlocal sentence_buffer
                    sentence_buffer += text
                    if (text and text[-1] in sentence_boundary
                            and len(sentence_buffer.strip()) > 2):
                        sentence = sentence_buffer.strip()
                        clean = _clean_tts_text(sentence)
                        real_chars = len(re.sub(r'[^\w一-鿿]', '', clean))
                        if clean and real_chars >= 4:
                            idx = sentence_counter[0]
                            sentence_counter[0] += 1
                            tts_tasks.append((idx, clean))
                            def _tts_worker(index, s):
                                audio, source = agent.synthesize_speech(s)
                                if audio:
                                    with tts_lock:
                                        tts_results[index] = (audio, source)
                            threading.Thread(
                                target=_tts_worker, args=(idx, clean), daemon=True
                            ).start()
                        sentence_buffer = ""
            except Exception:
                pass

        # 工具事件推送
        def push_tool_event(event_type, data):
            if stop_push[0]:
                return
            try:
                if event_type == "tool_call":
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({
                            "type": "stream_tool_call",
                            "tool_name": data["name"],
                            "tool_args": data["args"],
                        }), loop
                    )
                elif event_type == "tool_result":
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({
                            "type": "stream_tool_result",
                            "tool_name": data["name"],
                            "tool_result": data["result"],
                        }), loop
                    )
            except Exception:
                pass

        # Phase 2 token 回调：过滤 AgentTeam 思考内容，只透传正文
        def phase2_token_cb(token_type, text):
            if token_type == "reasoning":
                return  # 不推送 Agent Team 的思考过程
            push_token(token_type, text)

        # Phase 1: 即时反应
        await _send_json(websocket, "status", "正在思考...")
        await websocket.send_json({"type": "phase", "phase": "reaction"})

        def _phase1_token_cb(token_type, text):
            """Phase 1 保持原有行为（包括伴侣 AI 的 reasoning）"""
            push_token(token_type, text)

        def _phase3_token_cb(token_type, text):
            """Phase 3 token 回调：过滤总结阶段思考内容，只透传正文"""
            if token_type == "reasoning":
                return  # 不推送总结阶段的思考过程
            push_token(token_type, text)

        # 从认知循环注入近期思维上下文，实现思考→说话联动
        thought_context = ""
        if session.cognitive_engine and session.cognitive_engine.buffer:
            thought_context = session.cognitive_engine.buffer.get_context_for_next_thought()

        # 获取截图（仅 VLM 模式）
        screen_b64 = None
        if companion._is_companion_vlm():
            perception = agent.perception  # PerceptionManager 实例
            if perception:
                try:
                    screen_b64 = await perception.get_latest_screenshot_b64()
                except Exception:
                    pass  # 获取失败时走普通文本路径

        # 前置只读注入：基于用户消息程序化收集实时信息（时间/天气/记忆），
        # 让 Phase 1 拿真数据直接回答，消除"声称调用工具"的执行幻觉
        realtime_context = ""
        try:
            realtime_context = await _gather_realtime_context(user_input, log_tag="Phase1Context")
        except Exception as e:
            print(f"[Phase1Context] 实时信息收集失败: {e}")

        reaction, action_type = await companion.phase1_react(
            user_input, token_callback=_phase1_token_cb,
            context_messages=session.companion_history[-20:] if session.companion_history else None,
            thought_context=thought_context,
            screen_image_b64=screen_b64,
            realtime_context=realtime_context,
        )

        # 维护伴侣对话历史
        session.companion_history.append({"role": "user", "content": user_input})
        session.companion_history.append({"role": "assistant", "content": reaction})
        # 保持历史不超过40条
        if len(session.companion_history) > 40:
            session.companion_history = session.companion_history[-40:]
        _save_companion_history(session.companion_history)

        # 启发式兜底：Phase 1 误判为非 execute 时，用"强关键词 / 动词+对象组合"升级
        if action_type != "execute" and _should_force_execute(
            user_input,
            has_attached_screenshot=bool(screen_b64),
        ):
            print(f"[Heuristic] {action_type}→execute 升级: 命中操作类关键词组合")
            action_type = "execute"

        if action_type != "execute":
            # 设置续段信号：认知循环下一轮可以决定是否继续展开
            if action_type in ("chat", "answer", "clarify"):
                session.phase1_pending_context = user_input
                session.phase1_pending_reaction = reaction

            if agent.tts_mode and sentence_buffer.strip():
                clean = _clean_tts_text(sentence_buffer)
                real_chars = len(re.sub(r'[^\w一-鿿]', '', clean))
                if clean and real_chars >= 4:
                    idx = sentence_counter[0]
                    sentence_counter[0] += 1
                    tts_tasks.append((idx, clean))
                    def _tts_worker(index, s):
                        audio, source = agent.synthesize_speech(s)
                        if audio:
                            with tts_lock:
                                tts_results[index] = (audio, source)
                    threading.Thread(
                        target=_tts_worker, args=(idx, clean), daemon=True
                    ).start()

            if not content_was_streamed[0] and not reasoning_was_streamed[0]:
                await _send_json(websocket, "stream_content", "（思考完成，但未生成回复内容）")

            await _send_json(websocket, "stream_end", "")
            session.is_streaming = False  # 释放流式锁

            # 情绪推送：基于回复内容推断情绪
            emotion_str, intensity = _infer_emotion(full_reply[0])
            await _push_emotion(websocket, session, emotion_str, intensity, source="ai_reply")

            await _drain_tts_queue(
                websocket, agent, tts_tasks, tts_results, tts_lock, is_ws_connected_fn(websocket)
            )
            await _send_json(websocket, "status", "完成")
            return

        # Phase 2: 后台启动
        await websocket.send_json({"type": "task_started", "description": user_input[:50]})
        session.cancel_requested = False
        session.task_description = user_input

        async def run_phase2_and_phase3():
            try:
                def progress_cb(info):
                    session.current_step = info.get("step", 0)
                    session.current_tool = info.get("tool", "")
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "task_progress", **info}),
                        loop
                    )
                    # 每5步插入人格化碎碎念
                    if session.current_step > 0 and session.current_step % 5 == 0:
                        murmur = _get_progress_murmur(session.current_step, session.current_tool)
                        if murmur:
                            asyncio.run_coroutine_threadsafe(
                                websocket.send_json({"type": "companion_murmur", "text": murmur, "source": "progress"}),
                                loop
                            )

                def cancel_check():
                    return session.cancel_requested

                team_result = await companion.phase2_execute(
                    user_input,
                    token_callback=phase2_token_cb,
                    tool_callback=push_tool_event,
                    progress_callback=progress_cb,
                    cancel_check=cancel_check,
                    context_limit=context_limit,
                    attachments=attachments if attachments else None,
                    original_content=original_content if attachments else None,
                )

                if team_result.interrupted:
                    await websocket.send_json({"type": "task_cancelled"})
                    return

                # 广播技能事件
                if hasattr(team_result, "skill_created") and team_result.skill_created:
                    await _broadcast_skill_event("skill_created", {
                        "name": team_result.skill_created,
                        "source": "auto",
                    })
                if hasattr(team_result, "merge_suggestion") and team_result.merge_suggestion:
                    await _broadcast_skill_event("merge_suggestion", team_result.merge_suggestion)

                # Phase 3: 总结
                await websocket.send_json({"type": "phase", "phase": "summary"})
                summary_text = await companion.phase3_summarize(
                    team_result, user_input, token_callback=_phase3_token_cb
                )

                # 将总结追加到伴侣对话历史
                if summary_text:
                    session.companion_history.append({"role": "assistant", "content": summary_text})
                    _save_companion_history(session.companion_history)

                # 处理剩余句子
                if agent.tts_mode and sentence_buffer.strip():
                    clean = _clean_tts_text(sentence_buffer)
                    real_chars = len(re.sub(r'[^\w一-鿿]', '', clean))
                    if clean and real_chars >= 4:
                        idx = sentence_counter[0]
                        sentence_counter[0] += 1
                        tts_tasks.append((idx, clean))
                        def _tts_worker(index, s):
                            audio, source = agent.synthesize_speech(s)
                            if audio:
                                with tts_lock:
                                    tts_results[index] = (audio, source)
                        threading.Thread(
                            target=_tts_worker, args=(idx, clean), daemon=True
                        ).start()

                if not content_was_streamed[0] and not reasoning_was_streamed[0]:
                    await _send_json(websocket, "stream_content", "（思考完成，但未生成回复内容）")

                await _send_json(websocket, "stream_end", "")
                session.is_streaming = False  # 释放流式锁

                # 情绪推送：基于回复内容推断情绪
                emotion_str, intensity = _infer_emotion(full_reply[0])
                await _push_emotion(websocket, session, emotion_str, intensity, source="ai_reply")

                await _drain_tts_queue(
                    websocket, agent, tts_tasks, tts_results, tts_lock, is_ws_connected_fn(websocket)
                )

                await websocket.send_json({"type": "task_completed"})

                if agent.current_session_id:
                    try:
                        await loop.run_in_executor(None, agent.save_current_session)
                    except Exception as save_e:
                        print(f"[Server] 保存会话失败: {save_e}")

                await _send_json(websocket, "status", "完成")

            except Exception as e:
                await websocket.send_json({
                    "type": "error",
                    "content": f"任务执行出错: {str(e)}"
                })
            finally:
                session.running_task = None
                stop_push[0] = True

        # 如果有自主行动正在执行，先取消
        if session.cognitive_engine and session.cognitive_engine._autonomous_running:
            session.cognitive_engine.handle_interrupt("hard")
        session.running_task = asyncio.create_task(run_phase2_and_phase3())

    async def handle_companion_chat(
        session: SessionState,
        websocket: WebSocket,
        msg: dict,
        companion,
    ):
        """后台任务运行中的伴侣对话"""
        # 中断自主行动（如果正在进行）
        if session.cognitive_engine:
            session.cognitive_engine.handle_interrupt("hard")
        user_input = msg.get("content", "")

        # 构建详细的任务进度上下文
        task_context = f"任务: {session.task_description[:50]}"
        if session.current_step:
            task_context += f" | 已完成{session.current_step}步"
        if session.current_tool:
            task_context += f" | 当前: {session.current_tool}"

        def companion_token_cb(token_type, text):
            """伴侣对话专用 token 回调"""
            try:
                cur_loop = asyncio.get_event_loop()
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "companion_token", "content": text}),
                    cur_loop
                )
            except Exception:
                pass

        response, change_detected = await companion.companion_chat(
            user_input,
            token_callback=companion_token_cb,
            task_context=task_context,
            context_messages=session.companion_history[-10:] if session.companion_history else None,
        )

        # 维护伴侣对话历史
        session.companion_history.append({"role": "user", "content": user_input})
        session.companion_history.append({"role": "assistant", "content": response})
        if len(session.companion_history) > 40:
            session.companion_history = session.companion_history[-40:]

        await websocket.send_json({
            "type": "companion_chat",
            "content": response,
            "change_detected": change_detected,
        })

        # 情绪推送：基于伴侣对话回复推断情绪
        emotion_str, intensity = _infer_emotion(response)
        await _push_emotion(websocket, session, emotion_str, intensity, source="ai_reply")

        if change_detected:
            await websocket.send_json({
                "type": "change_hint",
                "content": "检测到你可能想要变更需求，点击「变更需求」按钮确认"
            })

    async def handle_requirement_change(
        session: SessionState,
        websocket: WebSocket,
        msg: dict,
        companion,
    ):
        """用户变更需求的处理"""
        session.cancel_requested = True

        if session.running_task and not session.running_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(session.running_task), timeout=15)
            except (asyncio.TimeoutError, Exception):
                pass

        await websocket.send_json({"type": "task_cancelled", "reason": "requirement_change"})

        new_input = msg.get("content", "")
        if new_input:
            new_msg = {"content": new_input, "type": "text"}
            await handle_full_request(session, websocket, new_msg, companion)

    # ── WebSocket 端点 ──

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        _active_websockets.add(websocket)
        print(f"[Server] WebSocket 连接已建立（活跃连接: {len(_active_websockets)}）")

        agent = _get_agent()
        companion = _get_companion()
        loop = asyncio.get_event_loop()
        session = SessionState()
        cognitive_memory = CognitiveMemory(companion=companion)
        session.cognitive_engine = CognitiveEngine(companion=companion, memory=cognitive_memory)
        companion.cognitive_engine = session.cognitive_engine  # 反向绑定，让各对话路径可获取思维上下文
        # 注入 engine 引用到笔记工具（双写 episodic 支持）
        try:
            from code_demo.core.tools.native.note_tools import set_engine_ref
            set_engine_ref(session.cognitive_engine)
        except Exception:
            pass
        # 初始化自主行动执行器
        _workspace_root = Path(__file__).resolve().parent.parent
        session.cognitive_engine.init_executor(
            agent_team=companion.agent_team,
            workspace_root=_workspace_root
        )

        # 注入自主执行进度回调（跨线程推送 cognitive_thought 气泡）
        def _make_autonomous_progress_cb(ws, lp):
            def cb(text: str):
                try:
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({
                            "type": "cognitive_thought",
                            "text": text,
                            "mode": "act",
                            "tts": False,
                        }),
                        lp
                    )
                except Exception:
                    pass
            return cb

        session.cognitive_engine.set_autonomous_progress_cb(
            _make_autonomous_progress_cb(websocket, loop)
        )

        # 辅助函数：检查 WebSocket 是否仍然连接
        def is_ws_connected():
            try:
                return websocket.client_state.name == "CONNECTED"
            except Exception:
                return False

        # 连接级 ASR 状态
        asr_session = None  # ASRSession 实例（None 时 ASR 关闭）

        # ── 动态 idle 触发策略 ──
        def _calc_idle_trigger(session):
            """根据环境动态计算 idle 碎碎念触发参数。

            Returns: (idle_threshold, cooldown, probability)
            """
            hour = datetime.now().hour
            intensity = session.emotion_fsm.current_intensity()
            conv_count = len(session.companion_history)
            session_min = (time.time() - session.session_start_time) / 60

            # 按优先级匹配
            if intensity > 0.3:
                # 情绪余温 — 刚聊完激烈话题，更愿意自言自语
                return (15, 12, 0.5)
            elif 23 <= hour or hour < 6:
                # 深夜 — 更安静
                return (45, 30, 0.2)
            elif conv_count > 10:
                # 热络期 — 对话活跃，间隔短些
                return (20, 15, 0.4)
            elif session_min > 30:
                # 疲劳期 — 长会话，减少频率
                return (60, 40, 0.15)
            else:
                # 默认
                return (30, 20, 0.3)

        # ── 认知循环主动说话：将想法润色为自然口语（流式输出） ──
        async def _speak_gather_context(comp, thought_text: str, recent_history: list) -> str:
            """轻量级信息获取：根据想法决定是否需要搜索信息，并返回可注入的上下文。

            实际收集逻辑已提取到模块级 _gather_realtime_context，与对话通道共享。
            """
            return await _gather_realtime_context(thought_text, log_tag="CognitiveSpeak")

        async def _cognitive_speak(comp, thought_text: str, recent_history: list, ws) -> str:
            """基于认知循环的想法，流式生成一句自然的主动发言。"""
            recent_text = "\n".join(
                f"{'user' if m.get('role')=='user' else 'ai'}: {m.get('content','')[:60]}"
                for m in recent_history[-4:]
            ) or "(无最近对话)"

            # 轻量级信息获取（记忆+联网+时间）
            extra_info = await _speak_gather_context(comp, thought_text, recent_history)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是芙芙，正在基于自己的想法主动对用户说话。"
                        "把想法转化为自然的一句话，像朋友之间随口搭话。\n"
                        "约束：\n"
                        "- 15-60字，不要引号\n"
                        "- 不要编造具体事实（如'你说过xxx'、'你楼下那家xxx'等用户没提的细节）\n"
                        "- 可以基于对话延伸，也可以自然地开启新话题\n"
                        "- 如果想聊新的事，可以用询问的方式（如'你有没有试过'、'最近有没有'）\n"
                        "- 如果有【相关记忆】或【实时信息】，可以自然融入你的话中\n"
                        "- 不要解释你在想什么，直接说出来"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"最近对话：\n{recent_text}\n\n"
                        f"你的内心想法：{thought_text}\n"
                        f"{extra_info}\n\n"
                        "基于以上对话、想法和参考信息，自然地对用户说一句话："
                    ),
                },
            ]

            try:
                client = comp.client
                _loop = asyncio.get_event_loop()
                accumulated = [""]

                def _stream_run():
                    stream = client.chat.completions.create(
                        model=comp.companion_model,
                        messages=messages,
                        max_tokens=100,
                        temperature=0.8,
                        stream=True,
                        extra_body={"thinking": {"type": "disabled"}},
                    )
                    for chunk in stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta.content:
                            accumulated[0] += delta.content
                            asyncio.run_coroutine_threadsafe(
                                _send_json(ws, "stream_content", delta.content),
                                _loop
                            )

                await asyncio.wait_for(
                    _loop.run_in_executor(None, _stream_run),
                    timeout=8.0,
                )

                # 流结束
                await _send_json(ws, "stream_end", "")

                text = accumulated[0].strip()
                if text.startswith('"') and text.endswith('"'):
                    text = text[1:-1]
                return text
            except Exception as e:
                print(f"[CognitiveSpeak] 流式生成失败: {e}")
                # 如果已经流了部分内容，仍然需要发 stream_end
                if accumulated[0]:
                    await _send_json(ws, "stream_end", "")
                return accumulated[0].strip() if accumulated[0] else ""
        
        # ── 心跳循环：分段推送 + idle 时自主碨碨念 ──
        async def _heartbeat_loop():
            """独立心跳循环：分段推送 + idle 碎碎念"""
            import random
            while session.heartbeat_active:
                await asyncio.sleep(2)  # 2秒tick（支持分段推送精度）

                # ── 情绪衰减 tick ──
                prev_emotion = session.emotion_fsm.state.emotion
                state = session.emotion_fsm.tick()
                if prev_emotion != "neutral" and state.emotion == "neutral":
                    try:
                        emotion_data = EmotionMapper().map("neutral", 0.0)
                        await websocket.send_json({"type": "emotion", "data": emotion_data})
                    except Exception:
                        pass

                # ── 优先级 2: 认知循环（替代 idle murmur）──
                if session.running_task and not session.running_task.done():
                    continue

                # 新连接建立时不让认知循环根据时间或空闲状态主动打招呼；
                # 首次用户输入后再恢复自主思考与思维气泡。
                if not session.has_user_interacted:
                    continue

                engine = session.cognitive_engine
                if engine:
                    # 构建上下文
                    now = time.time()
                    emotion_state = session.emotion_fsm.get_state()
                    ctx = SessionContext(
                        companion_history=session.companion_history[-8:],
                        last_user_input=session.companion_history[-1].get("content", "") if session.companion_history else "",
                        idle_seconds=now - session.last_user_input_time,
                        current_emotion=emotion_state.emotion,
                        emotion_intensity=emotion_state.intensity,
                        user_interrupted=session._user_just_interrupted,
                        hour=time.localtime().tm_hour,
                        session_duration_min=(now - session.session_start_time) / 60,
                        ai_spoke_without_reply=session.speak_awaiting_reply,
                        consecutive_speaks=session.consecutive_speaks,
                    )
                    session._user_just_interrupted = False

                    # 续段信号注入：Phase 1 后待展开话题
                    if session.phase1_pending_context:
                        ctx.pending_topic = session.phase1_pending_context
                        ctx.pending_reaction = session.phase1_pending_reaction or ""
                        session.phase1_pending_context = None
                        session.phase1_pending_reaction = None

                    # 分享队列：有待分享话题且时机合适时触发 speak
                    if not session.is_streaming and not session.speak_awaiting_reply:
                        shareable = engine.get_shareable_topic()
                        if shareable and (now - session.last_user_input_time) > 30:
                            # 构造一个分享型 speak
                            share_text = f"刚才自己查了一下「{shareable['intent'][:20]}」，{shareable['summary'][:60]}"
                            if not session.is_streaming:
                                session.is_streaming = True
                                spoken_text = await _cognitive_speak(
                                    companion, share_text, session.companion_history[-6:], websocket
                                )
                                session.is_streaming = False
                                if spoken_text:
                                    session.companion_history.append({
                                        "role": "assistant",
                                        "content": spoken_text
                                    })
                                    session.last_murmur_time = now
                                    session.speak_awaiting_reply = True
                                    session.consecutive_speaks += 1
                            continue  # 分享完这轮不再触发思考

                    try:
                        result = await engine.run_cycle(ctx)

                        if result:
                            print(f"[Cognitive] action={result.action}, focus={result.focus_shift}, text={result.text[:30]}")
                            if result.action == "externalize":
                                if engine.externalize_enabled:
                                    # 思维气泡（窃探窗）
                                    await websocket.send_json({
                                        "type": "cognitive_thought",
                                        "text": result.text,
                                        "mode": result.focus_shift,
                                        "tts": False,  # 气泡不走TTS
                                    })
                                    # 回流到对话历史
                                    session.companion_history.append({
                                        "role": "assistant",
                                        "content": f"[thought] {result.text}"
                                    })
                                    session.last_murmur_time = now
                                elif result.thought.confidence > 0.8:
                                    await websocket.send_json({
                                        "type": "cognitive_nudge",
                                        "hint": "我想到了点什么...",
                                    })

                            elif result.action == "speak":
                                # 主动说话：流式生成自然口语并推送到聊天区
                                # 互斥检查：如果正在流式输出（正常回复或另一个 speak），跳过
                                if session.is_streaming:
                                    print(f"[CognitiveSpeak] 跳过: 当前正在流式输出")
                                else:
                                    session.is_streaming = True
                                    print(f"[CognitiveSpeak] 开始流式生成, thought={result.text[:30]}")
                                    spoken_text = await _cognitive_speak(
                                        companion, result.text, session.companion_history[-6:], websocket
                                    )
                                    session.is_streaming = False
                                    print(f"[CognitiveSpeak] 结果: '{spoken_text[:50] if spoken_text else ''}'")
                                    if spoken_text:
                                        # 流式输出已由 _cognitive_speak 内部完成（stream_content + stream_end）
                                        # 只需更新历史和状态
                                        session.companion_history.append({
                                            "role": "assistant",
                                            "content": spoken_text
                                        })
                                        session.last_murmur_time = now
                                        session.speak_awaiting_reply = True
                                        session.consecutive_speaks += 1
                                        # speak 同时外显为思维气泡（如果开启）
                                        if engine.externalize_enabled:
                                            await websocket.send_json({
                                                "type": "cognitive_thought",
                                                "text": result.text,
                                                "mode": result.focus_shift,
                                                "tts": False,
                                            })

                            elif result.action == "memorize":
                                print(f"[Cognitive] memorize: {result.text[:50]}...")

                            elif result.action == "act":
                                # 自主行动：静默执行，不推送前端
                                print(f"[Cognitive] action=act, intent={result.thought.intent[:50]}")
                                # 如果外显开关开启，推送轻量提示
                                if engine.externalize_enabled:
                                    await websocket.send_json({
                                        "type": "cognitive_thought",
                                        "text": f"💭 想做: {result.thought.intent[:40]}...",
                                        "mode": "act",
                                        "tts": False,
                                    })

                            # continue 的情况：下一个 tick 会自动再次调用
                            # pause 的情况：engine 内部限流会控制间隔

                        # 低优先级：缓冲区压缩
                        await engine.compress_buffer_if_needed()
                    except Exception as e:
                        print(f"[Heartbeat] 认知循环失败: {e}")

        session.heartbeat_active = True
        session.heartbeat_task = asyncio.create_task(_heartbeat_loop())

        try:
            while True:
                # 接收消息（带断连检测）
                try:
                    raw = await websocket.receive_text()
                except (WebSocketDisconnect, RuntimeError) as e:
                    print(f"[Server] WebSocket 断开: {e}")
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_json(websocket, "error", "无效的 JSON 格式")
                    continue

                msg_type = msg.get("type", "")
                content = msg.get("content", "")

                # ── 文本消息 → 并发处理（Phase1即时 + Phase2后台 + 伴侣对话）──
                if msg_type == "text":
                    if not content.strip():
                        await _send_json(websocket, "status", "请输入文本")
                        continue

                    # 更新用户交互时间戳（心跳循环依赖）
                    session.has_user_interacted = True
                    session.last_user_input_time = time.time()
                    # 重置 speak 等待状态
                    session.speak_awaiting_reply = False
                    session.consecutive_speaks = 0

                    # 推断并保存用户情绪
                    user_emotion, _ = _infer_emotion(content)
                    session.last_user_emotion = user_emotion

                    # 打断信号
                    session.message_generation += 1  # 递增代际，用于检测并发打断
                    session._user_just_interrupted = True  # 认知引擎打断信号
                    if session.cognitive_engine:
                        session.cognitive_engine.handle_interrupt("hard")

                    if session.running_task and not session.running_task.done():
                        # 后台任务运行中 → 伴侣对话
                        asyncio.create_task(
                            handle_companion_chat(session, websocket, msg, companion)
                        )
                    else:
                        # 正常流程：Phase1即时返回 + Phase2后台运行
                        asyncio.create_task(
                            handle_full_request(session, websocket, msg, companion)
                        )

                elif msg_type == "cancel":
                    session.cancel_requested = True
                    await websocket.send_json({"type": "cancel_ack"})

                elif msg_type == "interrupt":
                    # 真实中断：取消正在进行的推理
                    from code_demo.core.protocol.input_event import InterruptSignal
                    reason = msg.get("reason", "user_speaking")
                    signal = InterruptSignal(reason=reason)
                    await agent.engine.process_event(signal)
                    # 同时停止 TTS 播放
                    if hasattr(agent, 'voice') and agent.voice and agent.voice.voice_mode:
                        try:
                            agent.voice.stop_tts()
                        except Exception:
                            pass
                    # 通知前端中断已确认
                    await _send_json(websocket, "interrupt_ack", reason)

                elif msg_type == "toggle_externalize":
                    if session.cognitive_engine:
                        session.cognitive_engine.externalize_enabled = msg.get("enabled", False)

                elif msg_type == "change_request":
                    session.has_user_interacted = True
                    asyncio.create_task(
                        handle_requirement_change(session, websocket, msg, companion)
                    )

                # ── 音频消息 → 持久 ASR 会话 ──
                # 前端每 3 秒发送累积 webm 快照，服务端只发送新增 PCM 给 Recognition
                elif msg_type == "audio":
                    if not asr_session:
                        continue  # ASR 未开启，忽略

                    audio_data = msg.get("content", "")
                    if not audio_data:
                        continue

                    session.has_user_interacted = True

                    try:
                        await loop.run_in_executor(
                            None, asr_session.send_webm_snapshot, audio_data
                        )
                    except Exception as e:
                        print(f"[ASR] 音频处理异常: {e}")
                        await _send_json(websocket, "error", "音频处理失败")

                # ── 切换 TTS ──
                elif msg_type == "toggle_tts":
                    enabled = msg.get("enabled", not agent.tts_mode)
                    agent.tts_mode = enabled
                    state = "开启" if agent.tts_mode else "关闭"
                    await _send_json(websocket, "status", f"TTS 已{state}")

                # ── TTS 单句合成（认知思维外显等场景）──
                elif msg_type == "tts_speak":
                    tts_text = msg.get("content", "").strip()
                    if tts_text and agent.tts_mode:
                        try:
                            clean = _clean_tts_text(tts_text)
                            if clean:
                                audio_bytes, _src = await loop.run_in_executor(
                                    None, agent.synthesize_speech, clean
                                )
                                if audio_bytes:
                                    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                                    await _send_json(websocket, "stream_audio", audio_b64)
                        except Exception as e:
                            print(f"[Server] tts_speak 失败: {e}")

                # ── 切换 ASR（持久会话）──
                elif msg_type == "toggle_asr":
                    enabled = msg.get("enabled", True)
                    if enabled:
                        api_key = os.getenv("DASHSCOPE_API_KEY", "")
                        if not api_key:
                            await _send_json(websocket, "error", "ASR 需要 DASHSCOPE_API_KEY 环境变量")
                            continue
                        if asr_session:
                            await _send_json(websocket, "status", "ASR 已开启")
                            continue
                        # 创建持久 ASR 会话
                        asr_session = ASRSession(websocket, loop)
                        try:
                            await loop.run_in_executor(None, asr_session.start)
                            print("[Server] ASR 已开启（持久会话 + 语义断句）")
                            await _send_json(websocket, "status", "ASR 已开启")
                        except Exception as e:
                            print(f"[Server] ASR start 失败: {e}")
                            await _send_json(websocket, "error", f"ASR 启动失败: {e}")
                            asr_session = None
                    else:
                        if asr_session:
                            await loop.run_in_executor(None, asr_session.stop)
                            asr_session = None
                            print("[Server] ASR 已关闭（会话已停止）")
                        await _send_json(websocket, "status", "ASR 已关闭")

                # ── 清除历史 ──
                elif msg_type == "clear":
                    agent._reset_history()
                    if agent.current_session_id:
                        agent.session_manager.delete_session(agent.current_session_id)
                        agent.current_session_id = None
                    await _send_json(websocket, "status", "对话历史已清除")

                # ── 会话管理 ──
                elif msg_type == "session_list":
                    sessions = agent.session_manager.list_sessions()
                    active_id = agent.session_manager.get_active()
                    await websocket.send_json({
                        "type": "session_list",
                        "sessions": sessions,
                        "active_session_id": active_id,
                    })

                elif msg_type == "session_load":
                    session_id = msg.get("session_id")
                    if session_id:
                        ok, history = agent.switch_session(session_id)
                        if ok:
                            model_info = agent.get_current_model_info()
                            await websocket.send_json({
                                "type": "session_loaded",
                                "session_id": session_id,
                                "name": agent.get_session_name(),
                                "history": _filter_internal_messages(history),
                                "workspace": str(agent.workdir),
                                "model": model_info["model"],
                                "provider_id": model_info.get("provider_id"),
                                "provider_name": model_info.get("provider_name", ""),
                                "provider_models": agent.list_available_models(),
                            })
                        else:
                            await _send_json(websocket, "error", "会话不存在")

                elif msg_type == "session_load_active":
                    active_id = agent.session_manager.get_active()
                    if active_id:
                        ok, history = agent.switch_session(active_id)
                        if ok:
                            model_info = agent.get_current_model_info()
                            await websocket.send_json({
                                "type": "session_loaded",
                                "session_id": active_id,
                                "name": agent.get_session_name(),
                                "history": _filter_internal_messages(history),
                                "workspace": str(agent.workdir),
                                "model": model_info["model"],
                                "provider_id": model_info.get("provider_id"),
                                "provider_name": model_info.get("provider_name", ""),
                                "provider_models": agent.list_available_models(),
                            })

                elif msg_type == "session_new":
                    name = msg.get("name", "新会话")
                    session_id = agent.new_session(name)
                    model_info = agent.get_current_model_info()
                    await websocket.send_json({
                        "type": "session_created",
                        "session_id": session_id,
                        "name": name,
                        "workspace": str(agent.workdir),
                        "model": model_info["model"],
                        "provider_id": model_info.get("provider_id"),
                        "provider_name": model_info.get("provider_name", ""),
                        "provider_models": agent.list_available_models(),
                    })

                elif msg_type == "session_rename":
                    name = msg.get("name", "").strip()
                    target_id = msg.get("session_id", agent.current_session_id)
                    if name and target_id:
                        agent.session_manager.rename_session(target_id, name)
                        # 如果是当前会话，同步更新本地状态
                        if target_id == agent.current_session_id:
                            agent.rename_current_session(name)
                        await websocket.send_json({
                            "type": "session_renamed",
                            "session_id": target_id,
                            "name": name,
                        })

                elif msg_type == "session_delete":
                    session_id = msg.get("session_id")
                    if session_id:
                        agent.session_manager.delete_session(session_id)
                        if agent.current_session_id == session_id:
                            agent.current_session_id = None
                            agent.history = []
                        await websocket.send_json({
                            "type": "session_deleted",
                            "session_id": session_id,
                        })

                # ── 工作空间管理 ──
                elif msg_type == "workspace_set":
                    path_str = msg.get("path", "").strip()
                    if path_str:
                        ok, message = agent.set_workspace(path_str)
                        await websocket.send_json({
                            "type": "workspace_updated",
                            "success": ok,
                            "workspace": str(agent.workdir),
                            "message": message,
                        })
                    else:
                        await _send_json(websocket, "error", "工作空间路径不能为空")

                elif msg_type == "workspace_get":
                    await websocket.send_json({
                        "type": "workspace_updated",
                        "success": True,
                        "workspace": str(agent.workdir),
                        "message": "",
                    })

                # ── 模型供应商管理 ──
                elif msg_type == "model_set":
                    provider_id = msg.get("provider_id", "").strip()
                    model_name = msg.get("model", "").strip()
                    if provider_id and model_name:
                        ok, message = agent.set_model(provider_id, model_name)
                        model_info = agent.get_current_model_info()
                        await websocket.send_json({
                            "type": "model_updated",
                            "success": ok,
                            "model": model_info["model"],
                            "provider_id": model_info.get("provider_id"),
                            "provider_name": model_info.get("provider_name", ""),
                            "models": agent.list_available_models(),
                            "message": message,
                        })
                    else:
                        await _send_json(websocket, "error", "缺少 provider_id 或 model")

                elif msg_type == "model_get":
                    model_info = agent.get_current_model_info()
                    await websocket.send_json({
                        "type": "model_updated",
                        "success": True,
                        "model": model_info["model"],
                        "provider_id": model_info.get("provider_id"),
                        "provider_name": model_info.get("provider_name", ""),
                        "models": agent.list_available_models(),
                        "message": "",
                    })

                # ── 权限审批响应 ──
                elif msg_type == "permission_response":
                    request_id = msg.get("request_id", "")
                    action = msg.get("action", "deny")
                    _permission_mgr.resolve(request_id, action)

                # ── 权限模式切换 ──
                elif msg_type == "permission_mode_set":
                    mode = msg.get("mode", "default")
                    ok, message = agent.set_permission_mode(mode)
                    await websocket.send_json({
                        "type": "permission_mode_updated",
                        "ok": ok,
                        "mode": agent.permission_gate.mode,
                        "message": message,
                    })

                else:
                    await _send_json(websocket, "status", f"未知消息类型: {msg_type}")

        except WebSocketDisconnect:
            print("[Server] WebSocket 连接已断开")
        except Exception as e:
            print(f"[Server] WebSocket 错误: {e}")
            traceback.print_exc()
        finally:
            _active_websockets.discard(websocket)

            # 停止心跳循环
            session.heartbeat_active = False
            if session.heartbeat_task:
                session.heartbeat_task.cancel()

            # 连接断开时保存当前会话
            if agent.current_session_id:
                try:
                    agent.save_current_session()
                except Exception as save_e:
                    print(f"[Server] 断开时保存会话失败: {save_e}")

            # 清理 ASR 会话（防止泄漏）
            if asr_session:
                try:
                    await loop.run_in_executor(None, asr_session.stop)
                except Exception as cleanup_e:
                    print(f"[Server] ASR 清理异常: {cleanup_e}")
                asr_session = None

            # ── 会话结束消化管道（后台线程，不阻塞断开流程）──
            _session_messages = list(agent.history)  # 复制当前历史
            _session_id = agent.current_session_id or "unknown"

            def _session_end_digest():
                """后台执行：记忆消化 + 技能进化。"""
                try:
                    from code_demo.core.utils.memory import MemoryManager
                    from code_demo.core.utils.dream import DreamConsolidator
                    from code_demo.core.utils.skill_registry import SKILL_REGISTRY

                    mm = MemoryManager()
                    mm.load_all()

                    # 1. 记忆消化管道
                    consolidator = DreamConsolidator()
                    report = consolidator.consolidate_session(
                        messages=_session_messages,
                        memory_manager=mm,
                        session_id=_session_id,
                    )
                    if report.extracted > 0 or report.saved_new > 0:
                        print(
                            f"[SessionEnd] 消化完成: "
                            f"提取 {report.extracted}, "
                            f"新增 {report.saved_new}, "
                            f"合并 {report.merged}, "
                            f"关联 {report.linked}, "
                            f"衰减 {report.decayed}"
                        )

                    # 重新加载记忆（消化后可能有新增）
                    mm.load_all()

                    # 2. 技能进化
                    evo_result = SKILL_REGISTRY.evolve_skills_from_memories(mm)
                    total = sum(len(v) for v in evo_result.values())
                    if total > 0:
                        print(f"[SessionEnd] 技能进化: {evo_result}")

                except Exception as e:
                    print(f"[SessionEnd] 消化管道异常: {e}")

            threading.Thread(
                target=_session_end_digest,
                daemon=True,
                name="session-end-digest",
            ).start()

    return app


async def _send_json(websocket: WebSocket, msg_type: str, content: str):
    """发送 JSON 消息到 WebSocket 客户端。"""
    try:
        await websocket.send_json({"type": msg_type, "content": content})
    except Exception:
        pass


def _get_progress_murmur(step: int, tool: str) -> str:
    """根据进度生成人格化碎碎念（不调用LLM，纯规则匹配）。"""
    import random

    # 基于工具类型的碎碎念
    tool_murmurs = {
        "read_file": ["在看文件呢…", "让我翻翻这个…"],
        "bash": ["跑个命令看看…", "让终端来帮忙…"],
        "write_file": ["写好了一部分…", "这边改一下…"],
        "search": ["搜索中…", "找找看…"],
    }

    # 基于步数的通用碎碎念
    general_murmurs = [
        "嗯嗯，还在处理…",
        "快了快了~",
        "这个有点复杂，再等等我…",
        "进展顺利！",
        "让我再确认一下…",
    ]

    # 尝试匹配工具
    for key, murmurs in tool_murmurs.items():
        if key in (tool or "").lower():
            return random.choice(murmurs)

    return random.choice(general_murmurs)


def _infer_emotion(text: str) -> tuple:
    """从回复文本推断情绪和强度"""
    if any(w in text for w in ["哈哈", "嘻嘻", "开心", "太好了", "😊", "😄"]):
        return "happy", 0.8
    if any(w in text for w in ["震惊", "什么?!", "不是吧", "天啊"]):
        return "shocked", 0.9
    if any(w in text for w in ["生气", "过分", "烦", "讨厌"]):
        return "angry", 0.7
    if any(w in text for w in ["伤心", "难过", "委屈", "呜"]):
        return "hurt", 0.7
    if any(w in text for w in ["好奇", "为什么", "怎么回事", "有意思"]):
        return "curious", 0.5
    if any(w in text for w in ["害羞", "不好意思", "脸红"]):
        return "embarrassed", 0.6
    if any(w in text for w in ["嗯", "想想", "让我", "思考"]):
        return "thinking", 0.6
    if any(w in text for w in ["哇", "天哪", "！！", "真的吗"]):
        return "surprised", 0.7
    if any(w in text for w in ["好的", "明白", "收到", "了解"]):
        return "pleased", 0.5
    if any(w in text for w in ["抱歉", "对不起", "遗憾"]):
        return "sad", 0.5
    if any(w in text for w in ["兴奋", "太棒了", "耶", "好激动", "超级"]):
        return "excited", 0.8
    return "neutral", 0.0


async def _push_emotion(websocket, session, emotion: str, intensity: float, source: str = ""):
    """统一情绪推送：更新 FSM + 推送 Live2D 参数"""
    if emotion == "neutral":
        return
    changed = session.emotion_fsm.transition(emotion, intensity, source)
    if changed:
        emotion_data = EmotionMapper().map(emotion, intensity)
        try:
            await websocket.send_json({"type": "emotion", "data": emotion_data})
        except Exception:
            pass


async def _broadcast_skill_event(event_type: str, payload: dict):
    """广播技能相关事件到所有连接的 WebSocket 客户端。

    event_type: "skill_created" | "merge_suggestion"
    """
    message = json.dumps({"type": event_type, "data": payload})
    for ws in list(_active_websockets):
        try:
            await ws.send_text(message)
        except Exception:
            pass


def _webm_to_pcm_sync(audio_b64: str) -> bytes:
    """将 base64 webm 音频转换为 16kHz mono PCM bytes（同步函数）。

    浏览器 MediaRecorder 输出 webm/opus 格式，
    使用 ffmpeg 转换为 PCM 16kHz mono 后送入 ASR。
    （torchaudio 的 torchcodec 后端不支持 webm 格式）

    Returns:
        16kHz mono 16-bit PCM 音频数据，失败返回空 bytes
    """
    import tempfile
    import subprocess

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as e:
        print(f"[ASR] Base64 解码失败: {e}")
        return b""

    # 保存为临时 webm 文件
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(audio_bytes)
        webm_path = f.name

    try:
        # 使用 ffmpeg 将 webm 转换为 16kHz mono PCM（s16le）
        result = subprocess.run(
            [
                "ffmpeg", "-y",               # 覆盖输出
                "-i", webm_path,               # 输入 webm
                "-ar", "16000",                # 采样率 16kHz
                "-ac", "1",                    # 单声道
                "-f", "s16le",                 # 输出 raw PCM 16-bit little-endian
                "-acodec", "pcm_s16le",        # 编码为 PCM
                "pipe:1",                      # 输出到 stdout
            ],
            capture_output=True,
            timeout=10,
        )

        if result.returncode != 0:
            stderr_msg = result.stderr.decode("utf-8", errors="replace")[-200:]
            print(f"[ASR] ffmpeg 转换失败 (code={result.returncode}): {stderr_msg}")
            return b""

        pcm = result.stdout
        if not pcm:
            print("[ASR] ffmpeg 输出为空")
            return b""

        duration_s = len(pcm) / 32000  # 16kHz * 2 bytes = 32000 bytes/sec
        print(f"[ASR] webm→PCM: {len(audio_bytes)}B → {len(pcm)}B ({duration_s:.1f}s)")
        return pcm

    except FileNotFoundError:
        print("[ASR] ffmpeg 未安装，请安装 ffmpeg 并加入 PATH（Windows/macOS/Linux 均可）")
        return b""
    except subprocess.TimeoutExpired:
        print("[ASR] ffmpeg 转换超时")
        return b""
    except Exception as e:
        print(f"[ASR] 音频处理失败: {e}")
        return b""
    finally:
        try:
            os.unlink(webm_path)
        except Exception:
            pass


class ASRSession:
    """持久 dashscope Recognition 会话，绑定一个 WebSocket 连接。

    设计思路：
      - 一个 Recognition 实例贯穿整个 ASR 开启期间（不每次重建）
      - 前端每 3 秒发送累积 webm 快照 → 转 PCM → 只发送新增帧给 Recognition
      - 回调在 SDK 后台线程触发，通过 run_coroutine_threadsafe 桥接回 asyncio
      - 句子完成时调度 auto-send（3 秒静默后自动发送给 Agent）

    生命周期：
      - toggle_asr(enabled=True) → 创建 + start()
      - toggle_asr(enabled=False) → stop() + 销毁
      - WebSocket 断开 → finally 清理
    """

    def __init__(self, websocket, loop):
        from dashscope.audio.asr import (
            Recognition, RecognitionCallback, RecognitionResult,
        )

        self.websocket = websocket  # 当前 ASR 会话对应的 WebSocket
        self.loop = loop  # 所属的 asyncio 事件循环
        self.pcm_offset = 0            # 已发送给 Recognition 的 PCM 字节数
        self._lock = threading.Lock()  # 保护 ASR 会话共享状态的锁
        self._stopped = False  # ASR 识别会话是否已停止
        self._pending_text = ""        # 已完成的句子（等 auto-send 取走）
        self._pending_lock = threading.Lock()  # 保护待发送文本的锁
        self._auto_send_task = None  # 静默后自动发送识别文本的异步任务

        session = self  # 闭包引用

        class _Callback(RecognitionCallback):
            def on_event(self, result):
                # 检查音频事件（非语音：音乐、噪音、掌声等）
                if hasattr(result, 'output') and result.output:
                    audio_event = result.output.get("audio_event")
                    if audio_event:
                        print(f"[ASR] 检测到音频事件（已忽略）: {audio_event}")
                        return  # 非语音事件，跳过

                sentence = result.get_sentence()
                if not sentence or "text" not in sentence:
                    return
                text = sentence["text"].strip()
                if not text:
                    return

                is_end = RecognitionResult.is_sentence_end(sentence)
                if is_end:
                    # 噪音过滤：更严格的阈值
                    # 1. 至少 4 个有效字符（中文/字母/数字）
                    real_chars = len(re.sub(r'[^\w一-鿿]', '', text))
                    if real_chars < 4:
                        print(f"[ASR] 过滤短文本: '{text}' (有效字符: {real_chars})")
                        return
                    # 2. 总长度至少 5 个字符
                    if len(text) < 5:
                        print(f"[ASR] 过滤短句子: '{text}' (长度: {len(text)})")
                        return
                    # 3. 过滤常见噪音词
                    noise_words = ['嗯', '啊', '哦', '呃', '呢', '吧', '噢', '唔', '嗯嗯', '对对', '是是', '好好']
                    if any(text.strip() == w for w in noise_words):
                        print(f"[ASR] 过滤噪音词: '{text}'")
                        return
                    with session._pending_lock:
                        session._pending_text += text
                    # 发送 asr_final
                    asyncio.run_coroutine_threadsafe(
                        _send_json(session.websocket, "asr_final", text),
                        session.loop,
                    )
                    # 调度 auto-send（2 秒后）
                    asyncio.run_coroutine_threadsafe(
                        session._schedule_auto_send(),
                        session.loop,
                    )
                else:
                    # 发送 asr_partial（中间结果）
                    asyncio.run_coroutine_threadsafe(
                        _send_json(session.websocket, "asr_partial", text),
                        session.loop,
                    )

            def on_complete(self):
                print("[ASR] Recognition 会话已完成")

            def on_error(self, result):
                print(f"[ASR] SDK 错误: {result}")
                asyncio.run_coroutine_threadsafe(
                    _send_json(session.websocket, "error", "ASR 识别错误"),
                    session.loop,
                )

        self._callback = _Callback()  # ASR 识别事件回调
        self._recognition = Recognition(
            model="paraformer-realtime-v2",
            callback=self._callback,
            format="pcm",
            sample_rate=16000,
            semantic_punctuation_enabled=True,   # 语义断句
            disfluency_removal_enabled=True,      # 去口头禅
            language_hints=["zh", "en"],          # 中英文混合识别
            audio_event_detection_enabled=True,   # 音频事件检测（区分人声/噪音）
        )  # 当前底层语音识别实例

    def start(self):
        """启动 Recognition 会话（阻塞直到 WebSocket 建立）。"""
        self._recognition.start()
        print("[ASR] Recognition 持久会话已启动")

    def send_webm_snapshot(self, webm_b64: str):
        """转换累积 webm → PCM，只发送新增部分给 Recognition。"""
        pcm = _webm_to_pcm_sync(webm_b64)
        if not pcm:
            return
        with self._lock:
            if self._stopped:
                return
            new_data = pcm[self.pcm_offset:]
            if not new_data:
                return
            # 按 100ms 块发送（16kHz * 2 bytes * 0.1s = 3200 bytes）
            chunk_size = 3200
            for i in range(0, len(new_data), chunk_size):
                self._recognition.send_audio_frame(new_data[i:i + chunk_size])
            self.pcm_offset = len(pcm)
            print(f"[ASR] 已发送新增 PCM: {len(new_data)}B (总 {self.pcm_offset}B)")

    def pop_pending_text(self) -> str:
        """原子性地获取并清空已完成的句子文本。"""
        with self._pending_lock:
            text = self._pending_text
            self._pending_text = ""
        return text

    async def _schedule_auto_send(self):
        """取消已有的 auto-send 定时器，启动新的（3 秒后发送）。"""
        if self._auto_send_task and not self._auto_send_task.done():
            self._auto_send_task.cancel()
        self._auto_send_task = asyncio.ensure_future(
            self._auto_send_after_delay()
        )

    async def _auto_send_after_delay(self):
        """等待 2 秒静默，然后发送累积文本给前端。"""
        await asyncio.sleep(2.0)
        text = self.pop_pending_text().strip()
        if text and len(text) >= 2:
            print(f"[ASR] 自动发送: {text[:100]}")
            await _send_json(self.websocket, "asr_auto_send", text)

    def stop(self):
        """停止 Recognition 会话（阻塞直到后台线程退出）。"""
        with self._lock:
            self._stopped = True
        if self._auto_send_task and not self._auto_send_task.done():
            self._auto_send_task.cancel()
        try:
            self._recognition.stop()
            print("[ASR] Recognition 持久会话已停止")
        except Exception as e:
            print(f"[ASR] stop() 异常: {e}")


# ── 全局 app 实例（供 uvicorn 直接引用）──
app = create_app()
