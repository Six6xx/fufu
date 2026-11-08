---
name: browser-harness
description: 合并自 browser-harness + browser-sandbox
source: manual
locked: false
created_at: 2026-07-09
updated_at: 2026-07-10
usage_count: 3
---

---
name: browser-harness
description: 浏览器控制与联网搜索能力。轻量获取用 curl，重度交互用 CDP 浏览器控制。
source: manual
locked: false
---

# browser-harness

通过 CDP（Chrome DevTools Protocol）直接控制浏览器。对于特定任务的编辑，请使用 `agent-workspace/agent_helpers.py`。如遇安装、设置或连接问题，请参阅 https://github.com/browser-use/browser-harness/blob/main/install.md。

领域技能默认关闭。设置 `BH_DOMAIN_SKILLS=1` 以启用；详见底部章节。

**如果 `BH_DOMAIN_SKILLS=1` 且任务针对特定网站，请在自行设计方法前，先读取 `$BH_AGENT_WORKSPACE/domain-skills/<site>/` 目录下的所有文件。**

## 轻量替代：curl / requests（优先使用）

对于不需要 JavaScript 渲染、登录态、复杂交互的简单网页获取，**优先使用 curl 或 python requests**，无需 CDP 浏览器：

```bash
# 获取网页内容
curl -sL --max-time 15 "https://example.com"

# 搜索
curl -sL --max-time 15 "https://html.duckduckgo.com/html/?q=关键词"

# 携带 User-Agent 避免被拦截
curl -sL --max-time 15 -A "Mozilla/5.0 (compatible; bot)" "https://example.com"
```

```bash
# python requests
python3 -c "
import requests
resp = requests.get('https://example.com', timeout=15)
print(resp.text[:5000])
"
```

**仅当 curl 无法满足**（需要 JavaScript 渲染、登录态、复杂交互）时，才升级到下方 CDP 浏览器控制。

### 升级到 CDP 浏览器前的安全约束
- 拉起本地 GUI 浏览器（`open http(s)://`、`open -a`、启动 chrome 等）属 high 风险，必须先请求并等待用户确认
- 禁止下载二进制文件、提交表单/注册、大量并发请求
- 所有网络请求必须设置超时（10-15 秒），失败不重试超过 2 次

## CDP 浏览器使用方法

BASH

```bash
browser-harness <<'PY'
print(page_info())
PY
```

- 以 `browser-harness` 方式调用。多行命令请使用 heredoc。
- 辅助函数已预导入。`run.py` 在 `exec` 前会先调用 `ensure_daemon()`。
- 首次导航应使用 `new_tab(url)`，而非 `goto_url(url)`。
- 默认本地流程会连接到正在运行的 Chrome/Chromium CDP 端点。无需浏览器 ID 或本地配置文件选择。

## 本地 Chrome

若守护进程无法连接，请运行诊断命令：

BASH

```bash
browser-harness --doctor
```

若未启用 Chrome 远程调试，harness 将打开：

TEXT

```tex
chrome://inspect/#remote-debugging
```

请用户勾选"为此浏览器实例允许远程调试"，并在 Chrome 弹出权限提示时点击"允许"。然后重试相同的 `browser-harness` 命令。

## 远程浏览器

对于无头服务器、并行子代理或隔离工作，请使用 Browser Use 云服务。只需认证一次：

BASH

```bash
browser-harness auth login
```

或安全地导入密钥：

BASH

```bash
browser-harness auth login --api-key-stdin
```

选择一个简短的自定义名称；下方 `r7k2` 仅为占位符：

BASH

```bash
browser-harness <<'PY'
start_remote_daemon("r7k2")
PY

BU_NAME=r7k2 browser-harness <<'PY'
new_tab("https://example.com")
print(page_info())
PY
```

任务完成后，若云浏览器仍在运行，请直接询问："是否现在关闭此浏览器？" 若回答是，则运行 `stop_remote_daemon(name)`。远程守护进程在停止或超时前将持续计费。

不要启动远程守护进程后仍继续使用默认守护进程。请为 `BU_NAME` 使用相同名称。

云配置文件 Cookie 同步参考：https://github.com/browser-use/browser-harness/blob/main/interaction-skills/profile-sync.md。

## 页面工作流

- **先截图**：使用 `capture_screenshot()` 了解当前可见状态。
- **点击操作**：截图 → 读取像素坐标 → `click_at_xy(x, y)` → 再次截图。
- 导航后调用 `wait_for_load()`。
- 若当前标签页过期或为内部页，调用 `ensure_real_tab()`。
- 当坐标操作不适用时，使用 `js(...)` 进行 DOM 检查或数据提取。
- **登录墙**：停止并询问用户。例外：若 Chrome 已登录，可自动使用可用的 SSO；但仍需在涉及密码、MFA、授权确认或账户选择模糊时停止。
- 可通过 `cdp("Domain.method", ...)` 使用原始 CDP。

## 交互技能

若在浏览器机制上遇到困难，请查阅 https://github.com/browser-use/browser-harness/tree/main/interaction-skills。

- connection.md
- cookies.md
- cross-origin-iframes.md
- dialogs.md
- downloads.md
- drag-and-drop.md
- dropdowns.md
- iframes.md
- network-requests.md
- print-as-pdf.md
- profile-sync.md
- screenshots.md
- scrolling.md
- shadow-dom.md
- tabs.md
- uploads.md
- viewport.md

## 设计约束

- 默认使用坐标点击。CDP 鼠标事件可在合成器层级穿透 iframe/shadow DOM/跨域内容。
- 保持连接模型简单：使用默认守护进程、`BU_NAME`、`BU_CDP_URL`、`BU_CDP_WS` 或 `start_remote_daemon(...)`。
- 核心辅助函数保持简短。将特定任务的辅助函数添加到 `$BH_AGENT_WORKSPACE/agent_helpers.py`。

## 注意事项

- 本地 Chrome 控制需启用 `chrome://inspect/#remote-debugging`。
- Chrome 可能弹出"允许远程调试？"提示；需等待用户点击"允许"。
- 地址栏下拉建议不是真实的工作标签页。
- CDP 目标顺序与 Chrome 可见标签页顺序不一致。
- `BU_CDP_URL` 是 HTTP DevTools 端点；守护进程会将其解析为 WebSocket。
- 云浏览器运行时请主动询问是否保留；使用 `stop_remote_daemon(name)` 或 `PATCH /browsers/{id} {"action":"stop"}` 停止。

## 领域技能

仅当 `BH_DOMAIN_SKILLS=1` 时生效，否则忽略领域技能。

启用后，在自行设计方法前，请搜索 `$BH_AGENT_WORKSPACE/domain-skills/<host>/`。`goto_url(...)` 会为导航到的主机返回最多 10 个技能文件名。

> ⚠️ 用户反馈约束（来自记忆 code-graph-desktop）:
> [系统提示: 项目有知识图谱可用（graphify-out/graph.json）。建议先用 graph_query 了解相关结构，再用 read_file 查看具体代码。]

【自主行动安全边界】
你正在自主探索，不是响应用户请求。遵守以下规则：
- 只允许读取工作目录: /Users/tsk/Desktop/学习/大模型及相关学习/learn-claude-code/claude-code-t

> ⚠️ 用户反馈约束（来自记忆 no-code-modification）:
> 用户在不同会话中反复强调：分析研究代码时不要修改代码。
- "仔细深入研究下，不要改代码"
- "不要做任何的代码修改"
- "不要动代码"

合并自原 3 个重复 feedback（仔细深入-研究下-不要改代码, agent-team-伴侣, graph-系统提示-项目有知）。

> ⚠️ 用户反馈约束（来自记忆 code-有新文件-desktop）:
> 【自主行动安全边界】
你正在自主探索，不是响应用户请求。遵守以下规则：
- 只允许读取工作目录: /Users/tsk/Desktop/学习/大模型及相关学习/learn-claude-code/claude-code-test/code_demo
- 禁止修改/删除普通文件（笔记管理除外）
- 禁止执行危险 shell 命令
- 禁止访问用户个人目录（如 ~/Desktop, ~/Documen

> ⚠️ 用户反馈约束（来自记忆 code-curl-max）:
> 【自主行动安全边界】
你正在自主探索，不是响应用户请求。遵守以下规则：
- 只允许读取工作目录: /Users/tsk/Desktop/学习/大模型及相关学习/learn-claude-code/claude-code-test/code_demo
- 禁止修改/删除普通文件（笔记管理除外）
- 禁止执行危险 shell 命令
- 禁止访问用户个人目录（如 ~/Desktop, ~/Documen

