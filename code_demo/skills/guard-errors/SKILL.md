---
name: guard-errors
description: 防御性守卫: 统一防御文件未找到、权限拒绝、运行时错误等常见错误重犯
source: manual
locked: false
created_at: 2026-07-10
updated_at: 2026-07-10
usage_count: 0
---

## 触发条件
工具输出匹配以下任一模式，自动激活对应防护：
- `(?i)not found|no such file|FileNotFoundE` → 文件/路径问题
- `(?i)permission denied|access denied|权限拒绝` → 权限问题
- `(?i)error|exception|traceback|失败|出错|报错` → 运行时错误

## 核心教训

### 路径问题
1. 路径前缀不要重复拼接 — 正确路径是 `code_demo/core/xxx.py`，不是 `code_demo/code_demo/core/xxx.py`
2. read_file 前先用 bash ls 确认路径 — 避免盲目读取不存在的文件
3. graph.json 使用 'links' 而非 'edges' 作为 key — 读取图谱数据时注意字段名

### 权限问题
4. 文件操作前检查权限 — 读取/写入前确认文件可访问（ls -la）
5. 知识图谱产物路径可能需要创建目录 — graphify-out/ 目录不存在时先 mkdir -p
6. 用户拒绝操作时不要重试，应换方式 — 避免重复触及权限边界

### 运行时错误
7. 工具未安装时先检查再使用 — graphify 等需先 pip install 再调用
8. bash run_bash() 不接受 timeout 参数 — 超时需用其他方式设置
9. tree-sitter 解析失败时检查文件编码 — 非 UTF-8 文件可能导致解析错误

## 防护步骤
1. 操作前确认依赖已安装、路径正确、文件存在
2. 检查 API 参数名是否正确（如 links vs edges）
3. 确认目标目录存在，必要时 mkdir -p
4. 遇到权限拒绝时换用替代方案，不重试

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

