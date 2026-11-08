# 事件与 Hook 系统

> 源码：`core/event/bus.py` + `core/event/hooks.py`

## 一、模块概览

```
event/
├── bus.py     → EventBUS (事件日志) + MessageBUS (代理间消息)
└── hooks.py   → HookManager (生命周期 Hook)
```

三个类各司其职：
- **EventBUS**：记录系统事件到 JSONL 文件（审计日志）
- **MessageBUS**：代理间异步消息通信（收件箱模式）
- **HookManager**：在工具调用等关键时刻执行用户自定义 Shell 命令

## 二、EventBUS — 事件日志

### 用途

记录工作树操作、任务变更等事件，供事后审计和调试。

### 存储格式

JSONL（每行一条 JSON），存储在 `ai_output/.worktrees/events.jsonl`：

```jsonl
{"event": "worktree.create.before", "timestamp": 1716955200.0, "task_id": 1, "worktree": "fix-bug"}
{"event": "worktree.create.after", "timestamp": 1716955201.5, "task_id": 1, "worktree": "fix-bug"}
{"event": "worktree.run.before", "timestamp": 1716955210.0, "command": "npm test"}
```

### API

```python
class EventBUS:
    def __init__(self, event_log_path: Path):
        # 创建父目录和空文件（如不存在）
    
    def emit(self, event: str, task_id=None, wt_name=None, error=None, **extra):
        # 追加一条事件记录
        # payload = {"event": event, "timestamp": time.time(), ...}
    
    def list_recent(self, limit: int = 20) -> str:
        # 返回最近 N 条事件的 JSON 字符串
        # 最多 200 条
```

### 使用场景

```python
# WorktreeManager 中
self.events.emit("worktree.create.before", task_id=task_id, wt_name=name)
# ... 创建 git worktree ...
self.events.emit("worktree.create.after", task_id=task_id, wt_name=name)
```

## 三、MessageBUS — 代理间消息

### 架构

每个代理有一个独立的 JSONL 收件箱文件：

```
ai_output/.team/inbox/
├── lead.jsonl       ← 负责人收件箱
├── alice.jsonl      ← 队友 alice 的收件箱
└── bob.jsonl        ← 队友 bob 的收件箱
```

### 消息格式

```json
{
    "type": "message",
    "from": "alice",
    "content": "任务 3 已完成，请审核",
    "timestamp": 1716955200.0
}
```

### 消息类型

| type | 说明 |
|------|------|
| `message` | 普通消息 |
| `broadcast` | 广播消息 |
| `shutdown_request` | 关机请求（lead → teammate） |
| `shutdown_response` | 关机响应（teammate → lead） |
| `plan_approval` | 计划审批请求 |
| `plan_approval_response` | 计划审批响应 |

### API

```python
class MessageBUS:
    def send(self, sender, to, content, msg_type="message", extra=None) -> str:
        # 追加一条消息到 {to}.jsonl
        # 验证 msg_type 合法性
    
    def read_inbox(self, name) -> list:
        # 原子读取并清空收件箱
        # 使用 rename 避免竞态条件
    
    def broadcast(self, sender, content, teammates) -> str:
        # 向所有队友（除自己）发送 broadcast 消息
```

### 原子读取机制

`read_inbox` 的关键设计是避免**竞态条件**（读取和清空之间有新消息到达）：

```
1. inbox_path.rename(temp_path)    ← 原子重命名（OS 级别）
2. 读取 temp_path 内容
3. 删除 temp_path
```

如果 rename 失败（例如 Windows 或并发），回退到"读取 → 清空"的非原子方式。

### 全局单例

```python
# bus.py
BUS = MessageBUS(INBOX_DIR)  # 模块级单例，所有模块共享
```

## 四、HookManager — 生命周期 Hook

### 概念

Hook 是在关键时刻（工具调用前/后、会话启动时）执行的**用户自定义 Shell 命令**。

```
.hooks.json 配置：
{
    "hooks": {
        "PreToolUse": [
            {"matcher": "bash", "command": "echo 'About to run bash'"}
        ],
        "PostToolUse": [
            {"matcher": "*", "command": "echo 'Tool done'"}
        ],
        "SessionStart": [
            {"command": "echo 'Session started'"}
        ]
    }
}
```

### 三种事件

| 事件 | 触发时机 | matcher 含义 |
|------|---------|-------------|
| `PreToolUse` | 工具执行前 | 匹配工具名称（`*` 匹配所有） |
| `PostToolUse` | 工具执行后 | 匹配工具名称 |
| `SessionStart` | Agent 会话启动时 | 不使用 |

### 退出码语义

Hook 命令的退出码决定行为：

| 退出码 | 含义 | 效果 |
|--------|------|------|
| `0` | 成功 | 静默继续，可选解析 stdout JSON |
| `1` | 阻止 | **阻止工具执行**，`result["blocked"] = True` |
| `2` | 注入 | 将 stderr 内容注入为额外消息 |

### 执行流程

```
run_hooks(event, context)
    │
    ├── 信任检查：TRUST_MARKER 是否存在？
    │   └── 否 → 返回空结果（不执行任何 Hook）
    │
    ├── 遍历 event 对应的 Hook 列表：
    │   │
    │   ├── matcher 过滤：context.tool_name 是否匹配？
    │   │
    │   ├── 构建环境变量：
    │   │   HOOK_EVENT = "PreToolUse"
    │   │   HOOK_TOOL_INPUT = '{"command": "ls"}'
    │   │   HOOK_TOOL_OUTPUT = "..." (仅 PostToolUse)
    │   │
    │   ├── subprocess.run(command, shell=True, timeout=60)
    │   │
    │   └── 根据退出码处理：
    │       ├── 0: 解析 stdout JSON（可选）
    │       │   ├── updatedInput → 修改 tool_input
    │       │   ├── additionalContext → 追加消息
    │       │   └── permissionDecision → 覆盖权限决策
    │       ├── 1: blocked = True
    │       └── 2: messages.append(stderr)
    │
    └── 返回 {"blocked": bool, "messages": [...]}
```

### 信任门控

Hook 仅在**受信任的工作区**中执行：

```python
def _check_workspace_trust(self) -> bool:
    if self._sdk_mode:
        return True    # SDK 模式隐式信任
    return TRUST_MARKER.exists()  # 需要 ai_output/.trusted 文件
```

这防止了在不受信任的仓库中执行恶意 Hook。

### 返回值结构

```python
{
    "blocked": False,           # 是否被阻止
    "block_reason": "",         # 阻止原因
    "messages": ["..."],        # 注入的消息
    "permission_override": "allow"  # 权限覆盖（可选）
}
```
