# WebSocket 服务器

> 源码：`core/server.py`

## 一、整体架构

```
create_app(enable_asr, enable_tts) → FastAPI app
    │
    ├── GET /            → 返回 index.html (静态页面)
    ├── GET /static/*    → 返回 app.js / style.css
    ├── GET /health      → 返回 Agent 状态 JSON (含 default_workspace, workspace, model, provider_id, provider_name, provider_models, show_agent_team)
    ├── GET /api/browse  → 目录浏览 (仅目录，用于工作空间选择器)
    ├── GET /api/providers → 返回模型供应商列表 (API Key 脱敏)
    ├── POST /api/providers/* → 模型供应商 CRUD + 发现 + 设置活跃
    ├── GET /api/voice-providers → 返回语音供应商列表 (TTS/ASR + 活跃状态 + 回退开关)
    ├── POST /api/voice-providers/* → 语音供应商 CRUD + 测试连接 + 设置回退
    ├── GET /api/companion/show-agent-team → 返回 AgentTeam 链路可见性
    ├── POST /api/companion/show-agent-team → 切换 AgentTeam 链路可见性
    └── WS /ws           → 双向实时通信 (核心)
```

服务器采用 **单例 Agent + 单例 CompanionAgent** 模式：全局共享一个 Agent 和一个 CompanionAgent 实例，多个 WebSocket 连接复用。`_run_agent()` 内部调用 `companion.process()` 而非直接调用 `agent.agent_loop()`。

### 生命周期管理 (lifespan)

采用 FastAPI 推荐的 `asynccontextmanager` 模式管理应用生命周期（替代已废弃的 `@app.on_event`）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    agent = _get_agent()
    if enable_tts:
        agent._init_tts_engine()
    if enable_asr:
        # 检查 DASHSCOPE_API_KEY
    yield
    # ── shutdown ──
    if _agent:
        _agent.stop()
        _agent = None
    _companion = None

app = FastAPI(lifespan=lifespan)
```

启动时预加载 Agent 单例和可选的 TTS/ASR 引擎，关闭时清理资源。

## 二、WebSocket 消息协议

### 客户端 → 服务器

| type | 字段 | 说明 |
|------|------|------|
| `text` | `content`, `thinking`, `attachments`, `context_limit` | 发送文本消息，触发 LLM 推理。`attachments` 为文件附件列表 `[{id, name, type}]`，`context_limit` 为前端压缩预设计算的上下文阈值 |
| `audio` | `content` (base64 webm) | 发送音频快照给 ASR |
| `toggle_tts` | `enabled` | 开关 TTS 语音合成 |
| `toggle_asr` | `enabled` | 开关 ASR 语音识别 |
| `clear` | — | 清空当前会话对话历史 |
| `session_new` | — | 新建会话 |
| `session_load` | `session_id` | 加载指定会话 |
| `session_rename` | `session_id`, `name` | 重命名会话 |
| `session_delete` | `session_id` | 删除会话 |
| `session_list` | — | 获取会话列表 |
| `workspace_set` | `path` | 设置当前会话的工作空间路径 |
| `workspace_get` | — | 获取当前工作空间路径 |
| `model_set` | `provider_id`, `model` | 切换当前模型（重建 OpenAI client，持久化到会话） |
| `model_get` | — | 获取当前模型信息 |

### 服务器 → 客户端

| type | 说明 |
|------|------|
| `stream_content` | LLM 生成的文本片段（逐 token） |
| `stream_reasoning` | LLM 的思考过程片段 |
| `stream_end` | LLM 生成完毕 |
| `stream_audio` | TTS 合成的音频（base64） |
| `tts_done` | 所有 TTS 音频发送完毕 |
| `asr_partial` | ASR 中间识别结果（频繁变化） |
| `asr_final` | ASR 最终句子（已确认） |
| `asr_auto_send` | 静默 2 秒后自动发送的文本 |
| `session_loaded` | 会话加载完成（含 `workspace`, `model`, `provider_id`, `provider_name`, `models` 字段） |
| `session_created` | 新会话创建完成（含 `workspace`, `model`, `provider_id`, `provider_name`, `models` 字段） |
| `workspace_updated` | 工作空间切换结果（含 `workspace` 字段） |
| `model_updated` | 模型切换结果（含 `model`, `provider_id`, `provider_name`, `models` 字段） |
| `error` | 错误消息 |
| `status` | 状态更新 |
| `cognitive_thought` | 认知思维气泡（mode="idle"磨碎念/"act"自主执行进度，含 text/mode/tts 字段） |

## 三、文本对话流程

```
客户端发送 { type: "text", content: "你好", attachments: [...], context_limit: 500000 }
    │
    ▼
server.py 接收：
    ├── 保存 original_content = content（原始用户文本，用于 UI 恢复显示）
    ├── 处理 attachments → 读取文本文件内容注入 content，二进制文件传路径
    ├── 提取 context_limit → 传给 agent_loop 覆盖全局 CONTEXT_LIMIT
    ├── 创建 tts_only_token_callback（隐藏模式下仅做 TTS 分句）
    └── 启动线程池任务 loop.run_in_executor(None, _run_agent, ...)
    │
    ▼
_run_agent() 调用 CompanionAgent.process():
    ├── Phase 1: 即时情感反应 → token_callback=push_token（始终可见）
    ├── Phase 2: 委派 AgentTeam → agent.agent_loop()
    │   ├── show_agent_team=True: token_callback=push_token, tool_callback=push_tool_event
    │   └── show_agent_team=False: token_callback=tts_only_token_callback, tool_callback=None
    └── Phase 3: 结果总结 → token_callback=push_token（始终可见）
    │
    ▼
客户端逐字收到 stream_content（Phase 1 + Phase 2[可选] + Phase 3）
```

**关键设计**：`token_callback` 通过 `run_coroutine_threadsafe` 从线程池线程安全地调度到 asyncio 事件循环。

**前置只读注入与委派兜底**（2026-07-29 ~ 07-30）：`handle_full_request` 在调用 `phase1_react` 前经模块级 `_gather_realtime_context(text, log_tag)` 收集实时信息（本地时间每轮必注入；天气按"天气/温度"关键词触发 wttr.in，`run_in_executor` + 3.5s 超时；记忆按关键词轻量召回最多 2 条各截 50 字），异常兜底不中断主流程；该函数由原 speak 通道闭包内的收集逻辑提取而来，speak 通道一行委托复用。Phase 1 判定结果由 `_should_force_execute()` 三层启发式后置兜底（排除词优先放行 → 强关键词升级 → 操作动词+对象名词共现升级 EXECUTE），确保需真实工具执行的请求委派 agent team。

**认知循环续段机制**（2026-08 ~）：会话级状态新增 `phase1_pending_context` / `phase1_pending_reaction` 续段信号字段。Phase 1 完成后，若为 [ANSWER] 类型，server.py 将用户原话存入 `phase1_pending_context`、Phase 1 已说内容存入 `phase1_pending_reaction`；下一轮认知循环 `_build_input_context` 检测到续段信号时，将其注入 `SessionContext.pending_topic` / `pending_reaction` 并清除信号，引导 ThoughtLoop 在续段场景中决策是否 speak 展开。

**自主执行进度前端外显**（2026-08 ~）：`_make_autonomous_progress_cb(ws, loop)` 工厂函数在 WebSocket 连接建立后注入 `cognitive_engine.set_autonomous_progress_cb()`。自主执行期间 AutonomousExecutor 通过 `tool_callback` 拦截工具调用，按类别生成简短中文描述（如"📖 正在读取文件...""⚡ 正在执行命令..."），通过 WebSocket 推送 `cognitive_thought` 消息（mode="act"），前端渲染为琥网色气泡。同类工具 3 秒内去重（节流防刷屏）。

## 四、流式 TTS 设计

TTS 不是等 LLM 全部生成完再合成，而是**边出文字边合成**：

```
LLM 流式输出 token
    │
    ▼
sentence_buffer 累积 token
    "你" → "你好" → "你好，" → "你好，我是芙芙。"
    │
    ▼ 检测到句子边界 (。！？.!?\n)
    │
    ▼
启动 TTS worker 线程 (并行)
    Thread(target=_tts_worker, args=(sentence_index, sentence_text))
    │
    ▼
同时 LLM 继续生成下一句
    │
    ▼
_tts_worker 通过 TTSEngineRouter 合成
    audio, source = agent.synthesize_speech(sentence)
    tts_results[index] = (audio_bytes, source)  (加锁写入)
    │
    ▼
LLM 生成完毕后，按顺序发送音频
    for idx in range(sentence_count):
        while idx not in tts_results: await sleep(0.1)
        audio_bytes, source = tts_results[idx]
        if source == "fallback":
            await _send_json(ws, "status", "⚠ 远程 TTS 不可用，已回退到本地引擎")
        audio_b64 = base64.b64encode(audio_bytes)
        await _send_json(ws, "stream_audio", audio_b64)
```

### TTS 引擎路由 (TTSEngineRouter)

`agent.synthesize_speech()` 内部通过 `TTSEngineRouter` 选择引擎：

```
TTSEngineRouter.synthesize(text)
    │
    ├── 有活跃远程 TTS 供应商 → RemoteTTSEngine (HTTP POST → 远程 CosyVoice)
    │   ├── 成功 → return (wav, "remote")
    │   └── 失败 + fallback=True → 回退本地 TTSEngine → return (wav, "fallback")
    │
    └── 无远程供应商 → 本地 TTSEngine (CosyVoice3 模型) → return (wav, "local")
```

`synthesize_speech()` 返回 `(bytes|None, source)` 元组，`source` 为 `"remote"` / `"local"` / `"fallback"` / `"none"`。回退时通过 WebSocket 发送 `status` 警告消息通知前端。

### 句子边界检测

```python
SENTENCE_BOUNDARY = set("。！？.!?")  # 仅句末标点，不含 \n

# 在 token_callback 中：
sentence_buffer += text
if text[-1] in SENTENCE_BOUNDARY and len(sentence_buffer.strip()) > 2:
    sentence = sentence_buffer.strip()
    clean = clean_markdown(sentence)  # 12 步清洗（见下方）
    real_chars = len(re.sub(r'[^\w一-鿿]', '', clean))
    if clean and real_chars >= 4:     # 至少 4 个有效字符
        idx = sentence_count
        sentence_count += 1
        threading.Thread(target=_tts_worker, args=(idx, clean)).start()
    sentence_buffer = ""
```

**清洗流程**：去除 `<think>` 标签 → 代码块 → 行内代码 → `[text](url)` 链接 → 粗体/斜体标记 → 标题符号 → 表格行 → emoji → URL → 合并空白

**设计决策**：`\n` 不作为句子边界，避免 LLM 输出中的 markdown 表格行、列表项等碎片触发 TTS。

## 五、ASR 持久会话 (ASRSession)

### 核心思路

每 1 秒前端发送一次完整 WebM 音频快照，服务端：
1. 将完整 WebM 转换为 PCM
2. 只发送**新增部分**给 dashscope Recognition
3. Recognition 持续识别，通过回调推送结果

### 数据流

```
前端 MediaRecorder.start(1000)   ← 每 1 秒触发 ondataavailable
    │
    ▼ 累积所有 chunks → new Blob → base64
WebSocket: { type: "audio", content: "<完整 webm base64>" }
    │
    ▼
ASRSession.send_webm_snapshot()
    │ ffmpeg webm → PCM (完整转换)
    │ 跳过 pcm_offset 之前的数据
    │ 只发送新增 PCM 帧 (100ms chunks)
    ▼
dashscope Recognition 持续识别 (后台线程)
    │
    ├── on_event (中间结果) → asr_partial → 前端显示 (可能变化)
    │
    └── on_event (句子完成) → asr_final → 前端锁定
                                │
                                └── 2 秒无新句子 → asr_auto_send → 自动发送给 Agent
```

### ASRSession 类结构

```python
class ASRSession:
    def __init__(self, websocket, loop):
        self.pcm_offset = 0           # 已发送的 PCM 字节数
        self._pending_text = ""       # 已完成的句子
        self._auto_send_task = None   # 2秒自动发送定时器
        
        self._recognition = Recognition(
            model="paraformer-realtime-v2",
            semantic_punctuation_enabled=True,   # 语义断句
            disfluency_removal_enabled=True,      # 去口头禅
            language_hints=["zh", "en"],          # 中英文
            audio_event_detection_enabled=True,   # 音频事件检测
        )
    
    def send_webm_snapshot(self, webm_b64):
        """WebM → PCM，只发新增部分"""
        pcm = _webm_to_pcm_sync(webm_b64)
        new_data = pcm[self.pcm_offset:]
        for i in range(0, len(new_data), 3200):  # 100ms chunks
            self._recognition.send_audio_frame(new_data[i:i+3200])
        self.pcm_offset = len(pcm)
```

### 噪音过滤策略

`_Callback.on_event()` 中实现多层过滤：

```
1. 句子长度 ≥ 5 字符
2. 有效字符数 ≥ 4（去除标点后）
3. 不在噪音词列表中 ("测试", "嗯", "啊", ...)
4. audio_event_detection 不标记为噪音
```

### 自动发送（2 秒静默）

```python
async def _auto_send_after_delay(self):
    await asyncio.sleep(2.0)         # 等待 2 秒
    text = self.pop_pending_text()   # 取走已完成的句子
    if text and len(text) >= 2:
        await _send_json(ws, "asr_auto_send", text)
```

**防抖设计**：每次新句子完成时会取消前一个定时器并重新启动。中途说话 → 新句子 → 定时器重置 → 直到真正停顿 2 秒才发送。

## 六、音频格式转换

```python
def _webm_to_pcm_sync(webm_b64: str) -> bytes | None:
    """WebM/Opus → 16kHz 16-bit mono PCM"""
    # 1. base64 解码 → 临时 .webm 文件
    # 2. ffmpeg -i input.webm -ar 16000 -ac 1 -f s16le -acodec pcm_s16le -
    # 3. 读取 stdout → 返回 PCM bytes
```

为什么需要转换：浏览器 MediaRecorder 输出 WebM/Opus 格式，但 dashscope Recognition 需要 16kHz PCM。

## 七、工作空间管理

### workspace_set / workspace_get

```python
elif msg_type == "workspace_set":
    path_str = msg.get("path", "")
    if path_str:
        ok, message = agent.set_workspace(path_str)
        await _send_json(ws, "workspace_updated", {
            "ok": ok,
            "message": message,
            "workspace": str(agent.workdir),
        })

elif msg_type == "workspace_get":
    await _send_json(ws, "workspace_updated", {
        "ok": True,
        "workspace": str(agent.workdir),
    })
```

### 会话响应中的 workspace 字段

`session_loaded`、`session_created`、`session_load_active` 响应均包含 `"workspace"` 字段，前端据此同步工作空间栏显示：

```python
await _send_json(ws, "session_loaded", {
    "session_id": session_id,
    "name": ...,
    "workspace": str(agent.workdir),
    ...
})
```

### /health 端点

```python
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "default_workspace": str(DEFAULT_WORKDIR),  # 默认工作空间
        "workspace": str(agent.workdir),             # 当前工作空间
        "model": agent.current_model,                # 当前模型名
        "provider_id": agent.current_provider_id,    # 当前供应商 ID
        "provider_name": active_provider.get("name", ""),  # 供应商名称
        "provider_models": active_provider.get("models", []),  # 模型列表
        "show_agent_team": _show_agent_team,         # AgentTeam 链路可见性
        ...
    }
```

前端在 WebSocket 连接建立前先调用 `/health` 获取工作空间和模型信息，用于初始化工作空间栏和 ModelSelector。

## 八、模型管理

### model_set / model_get

```python
elif msg_type == "model_set":
    provider_id = msg.get("provider_id", "")
    model_name = msg.get("model", "")
    ok, message = agent.set_model(provider_id, model_name)
    if ok:
        info = agent.get_current_model_info()
        await _send_json(ws, "model_updated", {
            "ok": True,
            "model": info["model"],
            "provider_id": info["provider_id"],
            "provider_name": info["provider_name"],
            "models": agent.list_available_models(),
        })

elif msg_type == "model_get":
    info = agent.get_current_model_info()
    await _send_json(ws, "model_updated", {
        "model": info["model"],
        "provider_id": info["provider_id"],
        "provider_name": info["provider_name"],
        "models": agent.list_available_models(),
    })
```

### 会话响应中的模型字段

`session_loaded`、`session_created` 响应均包含 `model`、`provider_id`、`provider_name`、`models` 字段，前端据此同步 ModelSelector 显示。

## 九、WebSocket 生命周期

```
客户端连接 /ws
    │
    ▼
创建连接级状态：
    tts_enabled = False
    asr_session = None
    stop_push = False
    │
    ▼
消息循环 async for message in websocket:
    │ 处理各种消息类型
    │
    ▼ (连接断开)
finally 清理：
    stop_push = True
    if asr_session: asr_session.stop()
    print("WebSocket disconnected")
```

`stop_push` 标志防止断开后仍尝试发送消息导致异常。
