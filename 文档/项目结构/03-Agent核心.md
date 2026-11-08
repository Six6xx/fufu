# Agent 核心引擎

> 源码：`core/agent.py` + `core/agent_team.py` + `core/companion_agent.py` + `core/prompt.py` + `core/config.py` + `core/utils/provider_manager.py` + `core/utils/voice_provider_manager.py`

## 一、整体职责

`Agent` 是整个系统的中枢，负责：

1. **初始化**：连接 MCP 服务器、构建工具池、初始化 LLM 客户端、加载模型供应商
2. **工具调度**：接收工具调用请求 → 权限检查 → 分发执行
3. **LLM 推理循环**：流式调用 API → 实时输出 token → 解析工具调用 → 循环
4. **模型切换**：运行时切换模型供应商和模型，重建 OpenAI client
5. **语音模式**：ASR（语音识别）和 TTS（语音合成路由）的延迟初始化与切换

### 上层封装

Agent 被两层抽象包装，形成三阶段对话流程：

```
CompanionAgent（伴侣层 — companion_agent.py）
    │
    ├── Phase 1: 即时情感反应（环境感知 + 委派意识，直接 OpenAI 调用）
    ├── Phase 2: 委派 AgentTeam → agent.agent_loop()（完整工具调用循环）
    └── Phase 3: 结果总结（执行元数据注入，直接 OpenAI 调用）

AgentTeam（执行层 — agent_team.py）
    │
    ├── _pre_analysis() → 检测分析关键词，注入图谱提示
    └── execute() → agent.agent_loop()（委托 + 执行日志收集 + 自检 + 自动技能创建）
        返回 AgentTeamResult(task_result, success, tool_calls_made, execution_log, skill_created, merge_suggestion)
```

**关键设计**：CompanionAgent 不创建新 Agent 实例，共享 `agent.client`；Phase 1/3 使用独立消息列表，不污染 `agent.history`。Phase 1 注入环境感知摘要（图谱节点数/记忆条数/技能个数），让伴侣层知道当前可用资源；Phase 3 注入执行元数据（工具调用次数/技能创建/自检结果），让总结有据可依。

**Phase 1 委派判定与实时信息注入**（2026-07-29 ~ 07-30）：`_build_capability_digest()` 动态枚举 `agent.tool_defs` 按 7 类归纳注入 Phase 1 prompt（声明只能经 [EXECUTE] 委派、禁止声称已使用），配合【EXECUTE 判定原则】与【自检约束】降低"需真实工具执行却误判为聊天"的漏判；`phase1_react` 接收 `realtime_context` 参数（server.py 侧 `_gather_realtime_context()` 收集的时间/天气/记忆），以独立 system 消息紧邻最终 user 消息注入，高频只读需求由真数据直答、消除执行幻觉；server.py 侧另有 `_should_force_execute()` 三层启发式对 Phase 1 判定结果做兜底覆盖。

**Phase 1 短回复治理**（2026-08 ~）：Phase 1 采用"末尾 system reminder"约束机制（利用 recency bias 策略强化约束效力）——在 messages 数组中 user 消息紧前插入独立 system reminder，内含对抗性 few-shot 示范（复杂请求仍只给一句轻量反应）。`max_tokens` 恢复为 600（依靠 prompt 约束控制长度而非物理截断，避免句子被截断的不自然感）。ANSWER 类型定义明确为"Phase 1 仍只输出一句话反应，展开内容由后续认知循环处理"，确保即便是知识问答也不在 Phase 1 铺陈。

## 二、初始化流程

```
Agent.__init__()
    │
    ├── _init_mcp()           ← 扫描插件清单 → 启动 MCP 子进程 → 注册路由
    ├── _build_tool_pool()    ← 合并 原生工具 + 代理工具 + MCP工具
    ├── _init_llm()           ← 创建 OpenAI 客户端 (DashScope 兼容)
    ├── self.workdir = DEFAULT_WORKDIR  ← 当前工作空间（per-session 可变）
    ├── self.provider_manager = ProviderManager()  ← 模型供应商管理
    ├── self.voice_provider_manager = VoiceProviderManager()  ← 语音供应商管理 (TTS/ASR)
    ├── self.current_model = MODEL  ← 当前模型名（per-session 可变）
    ├── self.current_provider_id = None  ← 当前供应商 ID
    └── SystemPromptBuilder   ← 传入工具定义 + 技能注册表 + workdir_getter 回调
```

### 工具池构成

```
tool_defs = [
    NATIVE_TOOL_DEFS    ← bash, read_file, write_file, edit_file (4个基础工具)
                        + graph_query, graph_update, analyze_file (3个图谱工具)
                        = 共 7 个原生工具
    + PROXY_TOOL_DEFS   ← send_message, read_inbox, list_tasks,
                           claim_task, list_skills, load_skill, list_teammates (7个)
    + MCP 工具           ← mcp__{server}__{tool} (动态数量)
]
```

图谱工具基于 graphify AST API，零 LLM 成本。Agent 系统提示词引导分析类任务优先使用图谱工具。

### 延迟初始化

ASR/TTS 引擎不在构造时加载，避免启动阻塞和显存浪费：

```python
# agent.py
ASREngine = None  # 模块级变量

def _get_asr_engine():
    global ASREngine
    if ASREngine is None:
        from code_demo.asr_tts.asr import ASREngine as _ASREngine
        ASREngine = _ASREngine
    return ASREngine
```

## 三、工具执行管道

```
execute_tool(tool_name, tool_input)
    │
    ├── 1. 权限检查 (CapabilityPermissionGate)
    │       ├── deny  → 返回 {"status": "denied"}
    │       ├── ask   → 弹出交互确认，拒绝则返回 denied
    │       └── allow → 继续
    │
    ├── 2. 路由分发
    │       ├── 代理工具 (send_message/read_inbox/...) → 内部 handler
    │       ├── 原生工具 (bash/read_file/...) → NATIVE_EXECUTORS[tool_name](**input, workdir=self.workdir)
    │       └── MCP 工具 (mcp__xxx__yyy) → mcp_router.call()
    │
    └── 3. 异常捕获 → 返回 "Error: ..."
```

### 代理工具 vs 原生工具

| 类型 | 特点 | 执行位置 |
|------|------|---------|
| **原生工具** | 直接操作文件系统和 Shell | `tools/native/runner.py` |
| **代理工具** | 操作 Agent 内部状态（消息、任务、技能） | `agent.py` 内部方法 |
| **MCP 工具** | 调用外部进程暴露的能力 | `tools/mcp/router.py` → `client.py` |

## 四、LLM 推理循环 (`agent_loop`)

这是 Agent 最核心的方法，采用**流式输出 + 工具调用循环**：

```
agent_loop(user_input, token_callback, context_limit=None, attachments=None, original_content=None)
    │
    ├── 首次调用：构建 system_prompt → 初始化 history
    │
    ├── 循环 (最多 MAX_ITERATIONS=30 轮):
    │   │
    │   ├── 上下文检查：> effective_context_limit → _compact_history()
    │   │   (effective_context_limit = context_limit 或 CONTEXT_LIMIT)
    │   │
    │   ├── 流式 API 调用 (带重试，最多 3 次)
    │   │   stream = client.chat.completions.create(
    │   │       model=self.current_model or MODEL, ...)  ← 使用当前模型
    │   │
    │   ├── 消费 stream chunks:
    │   │   ├── delta.content → token_callback("content", text)
    │   │   ├── delta.reasoning_content → token_callback("reasoning", text)
    │   │   └── delta.tool_calls → 累积到 tool_calls_buffer
    │   │
    │   ├── 检查是否有 tool_calls:
    │   │   ├── 无 → break (纯文本回复，循环结束)
    │   │   └── 有 → 逐个 execute_tool() → 追加结果到 history
    │   │
    │   └── max_tokens 恢复：若 finish_reason="length"，追加"请继续"消息重试
    │
    └── 返回最终文本

参数说明：
    user_input: 用户消息（可能已注入文件内容）
    token_callback: 流式 token 回调
    context_limit: 前端压缩预设传入的上下文限制（覆盖全局 CONTEXT_LIMIT）
    attachments: 附件列表 [{id, name, type}]，存入 history 用于 UI 恢复
    original_content: 原始用户文本（未注入文件内容），存入 history 用于 UI 显示
```

### 流式输出机制

```python
for chunk in stream:
    delta = chunk.choices[0].delta
    
    # 文本内容
    if delta.content:
        full_content += delta.content
        if token_callback:
            token_callback("content", delta.content)
    
    # 思考内容 (DeepSeek reasoning_content)
    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
        full_reasoning += delta.reasoning_content
        if token_callback:
            token_callback("reasoning", delta.reasoning_content)
```

### 上下文压缩 (`_compact_history`)

当 `len(str(history)) > CONTEXT_LIMIT` 时触发：

```python
def _compact_history(self):
    system = self.history[0]                    # 保留 system prompt
    recent = self.history[-KEEP_RECENT_MESSAGES:]  # 保留最近 N 条
    middle = self.history[1:-KEEP_RECENT_MESSAGES]
    
    summary = f"[已压缩 {len(middle)} 条历史消息]"
    self.history = [system, {"role": "system", "content": summary}] + recent
```

## 五、SystemPromptBuilder

负责组装系统提示词，分为**静态核心**和**动态上下文**两部分：

```
┌──────────────────────────────────┐
│ 静态核心 (_build_core)            │
│  ├── 角色定义 ("你是位于{workdir}的编码代理")
│  ├── 核心能力 (文件操作/代码分析/知识图谱/Shell/队友通信/任务管理/技能加载)
│  ├── 分析判断原则 (简单查询→graph_query / 单文件→analyze_file / 项目全貌→summary / 影响分析→affected)
│  ├── 工作原则 (先读再编辑/优先图谱工具/修改后更新图谱/简洁输出/安全第一)
│  ├── 自我认知 (跨会话记忆/技能系统/执行模式分析/图谱存储)
│  ├── 可用工具列表 (从 tool_defs 生成)
│  ├── 可用技能列表 (从 SkillRegistry 生成)
│  └── 技能调用原则 (优先使用技能和 MCP 工具)
├──── DYNAMIC_BOUNDARY ────────────┤
│ 动态上下文 (_build_dynamic_context)│
│  ├── 当前日期
│  ├── 工作目录 (self._workdir_getter())
│  ├── 模型名称
│  └── 平台信息
└──────────────────────────────────┘
```

`DYNAMIC_BOUNDARY` 标记用于未来优化：只重新生成动态部分而缓存静态核心。

**分析判断原则**（新增）：系统提示词中明确引导 Agent 根据任务复杂度选择工具：

| 任务类型 | 推荐方式 |
|---------|---------|
| 简单查询（某个函数在哪、谁调用了它） | 直接 `graph_query` |
| 单文件分析（理解一个文件的作用） | `analyze_file` |
| 项目全貌（架构理解、模块关系） | `graph_query(mode="summary")` |
| 影响分析（改了 A 会影响谁） | `graph_query(mode="affected", query="A")` |
| 代码不存在图谱中 | `graph_update` 先构建，再查询 |
| 非代码问题 | 直接回答或 `read_file` |

**自我认知**（新增）：告知 Agent 它拥有跨会话记忆、技能系统、执行模式分析、知识图谱存储四项能力，使其能主动利用这些资源。

**workdir_getter 回调**：构造函数接受 `workdir_getter` 参数（默认为 `lambda: WORKDIR`），Agent 传入 `lambda: self.workdir`，使得系统提示词中的工作目录随 `agent.set_workspace()` 动态变化，无需重建 prompt builder。

## 六、收件箱轮询

Agent 启动后开启一个后台线程，定期扫描消息收件箱：

```python
def _poll_loop(self):
    while self.running:
        inbox = self.message_bus.read_inbox(self.name)
        for msg in inbox:
            if msg["type"] == "shutdown_request":
                self.running = False
                return
            print(f"[收件箱] {msg['from']}: {msg['content'][:100]}")
        time.sleep(self._poll_interval)
```

## 七、REPL 命令行

`_repl()` 提供交互式命令行界面：

| 命令 | 功能 |
|------|------|
| `/help` | 显示帮助 |
| `/clear` | 清空对话历史 |
| `/inbox` | 查看收件箱 |
| `/tasks` | 列出任务 |
| `/skills` | 列出技能 |
| `/tools` | 列出工具 |
| `/prompt` | 显示系统提示词 |
| `/voice` | 切换语音输入模式 |
| `/tts` | 切换 TTS 语音合成 |
| `/quit` | 退出 |

## 八、config.py 关键常量

| 常量 | 值 | 用途 |
|------|-----|------|
| `DEFAULT_WORKDIR` | 计算值（`code_demo/` 或项目根） | 默认工作空间，Agent 初始化和新会话重置时使用 |
| `WORKDIR` | `= DEFAULT_WORKDIR` | 向后兼容别名 |
| `MODEL` | `os.getenv("MODEL")` | LLM 模型名 |
| `CONTEXT_LIMIT` | 120000 | 上下文字符数上限（可被前端 `context_limit` 参数覆盖） |
| `KEEP_RECENT_MESSAGES` | 30 | 压缩时保留的消息数 |
| `PERSIST_THRESHOLD` | 30000 | 大输出持久化阈值 |
| `MAX_OUTPUT_CHARS` | 500000 | 工具输出截断字符数 |
| `BASH_TIMEOUT` | 120 | Shell 命令超时(秒) |
| `HOOK_EVENTS` | `("PreToolUse", "PostToolUse", "SessionStart")` | Hook 事件类型 |
| `MODES` | `("default", "plan", "auto")` | 权限模式 |
| `MCP_CONFIG_FILE` | `_PROJECT_ROOT / "mcp_config.json"` | MCP 服务器配置文件路径（项目根目录） |
| `MODEL_PROVIDERS_FILE` | `AI_CONFIG_DIR / "model_providers.json"` | 模型供应商配置文件路径 |

### Companion Agent 配置

| 常量 | 值 | 用途 |
|------|-----|------|
| `COMPANION_MODEL` | `os.getenv("COMPANION_MODEL", "")` | 伴侣层使用的模型名（空则复用 agent.current_model） |
| `COMPANION_SHOW_AGENT_TEAM` | `False` | AgentTeam 链路默认可见性（隐藏） |
| `COMPANION_PERSONA_PROMPT` | 芙芙人设提示词 | 伴侣层 Phase 1/3 的性格表达系统提示词 |

## 九、工作空间管理

Agent 支持 per-session 工作空间，通过 `self.workdir` 属性控制所有工具执行和系统提示词中的路径：

```python
# agent.py
class Agent:
    def __init__(self, ...):
        self.workdir: Path = DEFAULT_WORKDIR
    
    def get_workspace(self) -> str:
        """返回当前工作空间路径"""
        return str(self.workdir)
    
    def set_workspace(self, path_str: str) -> tuple[bool, str]:
        """设置工作空间，验证路径存在且为目录，刷新系统提示词，持久化到会话元数据"""
        p = Path(path_str).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            return False, "路径不存在或不是目录"
        self.workdir = p
        self._prompt_builder._refresh()  # 刷新系统提示词中的路径
        self.session_manager._update_index_entry(
            self._current_session_id, workspace=str(p))
        return True, f"工作空间已切换到 {p}"
```

**会话生命周期中的工作空间**：

| 事件 | workdir 变化 |
|------|-------------|
| `__init__()` | 设为 `DEFAULT_WORKDIR` |
| `load_session()` | 从会话索引恢复 `workspace` 字段 |
| `new_session()` | 重置为 `DEFAULT_WORKDIR`，持久化到新会话 |
| `set_workspace()` | 更新为指定路径，持久化到当前会话 |

**数据流**：
```
前端 DirPicker 选择目录
    │
    ▼ sendWS('workspace_set', '', { path: '/Users/...' })
server.py 接收
    │
    ▼ agent.set_workspace(path) → 验证 → self.workdir = p → 刷新 prompt → 持久化
    │
    ▼ send_json('workspace_updated', { workspace: str(agent.workdir) })
前端更新工作空间栏显示
```

**与工具执行的关系**：`execute_tool()` 对原生工具注入 `workdir=self.workdir`，runner.py 中的 4 个执行器将 `workdir` 传给 `safe_path()` 和 `subprocess.run(cwd=...)`，确保所有文件操作和 shell 命令在正确的工作空间内执行。

## 十、模型切换

Agent 支持运行时切换模型供应商和模型，通过 `ProviderManager` 管理供应商配置：

```python
# agent.py
class Agent:
    def __init__(self, ...):
        self.provider_manager = ProviderManager()
        self.current_model: str = MODEL          # 当前模型名
        self.current_provider_id: str | None = None  # 当前供应商 ID
    
    def get_current_model_info(self) -> dict:
        """返回 {model, provider_id, provider_name}"""
    
    def set_model(self, provider_id: str, model_name: str) -> tuple[bool, str]:
        """切换模型 — 重建 OpenAI client，更新 current_model"""
        # 1. 从 ProviderManager 获取供应商信息
        # 2. 重建 self.client = OpenAI(api_key=..., base_url=...)
        # 3. self.current_model = model_name
        # 4. 持久化到 session metadata
    
    def list_available_models(self) -> list[dict]:
        """返回当前供应商的模型列表"""
```

**会话生命周期中的模型**：

| 事件 | current_model 变化 |
|------|-------------------|
| `__init__()` | 设为 `MODEL`（.env 默认值），加载活跃供应商 |
| `load_session()` | 从会话索引恢复 `provider_id` 和 `model` 字段 |
| `new_session()` | 重置为活跃供应商的默认模型或 `MODEL` |
| `set_model()` | 更新为指定模型，重建 client，持久化到当前会话 |

**数据流**：
```
前端 ModelSelector 选择模型
    │
    ▼ sendWS('model_set', '', { provider_id: 'uuid', model: 'qwen-vl-max' })
server.py 接收
    │
    ▼ agent.set_model(provider_id, model_name)
    │   ├── 验证供应商存在
    │   ├── 重建 self.client = OpenAI(api_key, base_url)
    │   ├── self.current_model = model_name
    │   └── 持久化到 session metadata
    │
    ▼ send_json('model_updated', { model, provider_id, provider_name, models })
前端更新 ModelSelector 显示
```

**agent_loop 中的使用**：`model=self.current_model or MODEL`，确保有供应商配置时使用供应商模型，否则回退到 .env 默认值。
