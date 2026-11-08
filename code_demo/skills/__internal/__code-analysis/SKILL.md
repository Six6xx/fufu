---
name: __code-analysis
description: "代码分析指导——强制调用规则、决策树、工具选择矩阵、操作顺序和编辑策略"
source: manual
locked: true
visible: false
layer: structure
status: active
---

# 代码分析指导（含图谱强制规则）

## ⚠️ 强制调用规则

以下场景 **MUST** 调用 graphify 工具，不是建议，是规则：

| 场景 | 必须执行 | 绝对禁止 |
|------|---------|---------|
| 涉及多文件/多模块分析 | `graph_query(mode="summary")` 先了解全貌 | 禁止凭记忆直接回答 |
| 需要了解调用链/依赖关系 | `graph_query(query="实体名", mode="affected")` | 禁止猜测依赖关系 |
| 分析单个文件结构 | `analyze_file(path="文件路径")` | 禁止仅 read_file 后就下结论 |
| 查找核心抽象/枢纽节点 | `graph_query(mode="god_nodes")` | 禁止凭直觉判断枢纽 |
| 新增文件/修改签名/删除代码后 | `graph_update()` 同步图谱 | 禁止修改后跳过更新 |

## 决策树

用户提问时，按以下决策树 MUST 执行对应工具调用序列：

```
问题类型判断
├─ 涉及"架构/结构/模块/全貌" → MUST: graph_query(mode="summary")
├─ 涉及"调用/依赖/影响/关联" → MUST: graph_query(query="目标", mode="affected")
├─ 涉及具体文件 → MUST: analyze_file(path="路径") → 再按需 read_file
├─ 涉及"核心/枢纽/关键" → MUST: graph_query(mode="god_nodes")
├─ 修改代码请求 → 执行修改 → MUST: graph_update()
└─ 简单单点查询(某函数在哪) → MUST: graph_query(query="函数名")
```

## 工具选择矩阵（必须使用，非推荐）

| 目的 | 必须使用 | 说明 |
|------|---------|------|
| 找函数位置 | `graph_query` | 比搜索更快更准 |
| 理解文件作用 | `analyze_file` | 结构化概览 |
| 读取具体内容 | `read_file` | 按需指定行范围 |
| 搜索字符串 | `read_file` + 关键词 | 先定位再读取 |
| 了解调用关系 | `graph_query(mode="affected")` | 下游依赖分析 |
| 项目架构全貌 | `graph_query(mode="summary")` | 模块和社区结构 |
| 核心枢纽 | `graph_query(mode="god_nodes")` | 高连接节点 |
| 非直觉关联 | `graph_query(mode="surprises")` | 跨模块依赖 |

## 图谱查询模式

| 模式 | 用途 | 示例 |
|------|------|------|
| `summary` | 项目概况（节点数、核心抽象） | `graph_query(mode="summary")` |
| `god_nodes` | 连接最多的核心节点 | `graph_query(mode="god_nodes")` |
| `surprises` | 跨社区的非直觉关联 | `graph_query(mode="surprises")` |
| `affected` | 修改某个实体的影响范围 | `graph_query(query="Agent", mode="affected")` |
| `questions` | 图谱建议的探索方向 | `graph_query(mode="questions")` |

## graph_update 触发时机

### MUST 触发
- 创建新文件后 — 新节点需要加入图谱
- 修改函数签名后 — 接口变更影响依赖关系
- 删除文件后 — 节点失效需从图谱移除
- 重构移动代码后 — 路径变更需同步

### 不需要触发
- 修改函数内部实现（签名不变）
- 修改注释或文档字符串
- 调整代码格式（缩进/空行）
- 修改非代码文件（配置/资源）

## 禁止行为

- 禁止在不调用 graphify 的情况下直接回答复杂架构/依赖问题 — 硬性规则
- 禁止在每次小修改后都 graph_update — 仅结构性变更时触发
- 禁止用 graph_query 查询非代码问题 — 它是代码图谱，不是通用搜索引擎
- 禁止忽略图谱中的依赖警告 — 循环依赖或孤立节点必须关注
- 禁止在图谱为空时跳过 graph_update — 先构建再查询

## 操作顺序：五步法

**定位 → 读取 → 理解 → 修改 → 验证**

### 1. 定位 — MUST 先用 graphify
- 图谱中没有 → `graph_update` 先构建
- 非代码问题 → 跳过图谱，直接 `read_file`

### 2. 读取 — read_file / analyze_file
- 优先 `analyze_file` 了解全貌，再 `read_file` 看细节

### 3. 理解 — 上下文和依赖关系
- 通过 `graph_query(mode="affected")` 了解影响范围

### 4. 修改 — 最小化变更
- 只改必须改的部分，不重写无关代码
- 修改前用注释保留原始逻辑（`# 原始: ...`）

### 5. 验证 — 语法检查/测试
- 修改后立即验证，失败则回退

## 自动触发规则

| 触发关键词 | 必须自动执行 |
|-----------|------------|
| 架构/结构/模块/依赖/调用/影响/关联 | `graph_query(mode="summary" 或 mode="affected")` |
| 具体文件路径 | `analyze_file(path="路径")` |
| 修改代码请求 | 修改完成后自动 `graph_update()` |

## 编辑策略

- 缩进方式与原文件一致，命名风格不混合
- 使用精确替换而非重写整个函数
- 复杂修改分步进行，每步验证
- 不删除现有注释，按需更新
- 保留原始逻辑的注释备份（`# 原始: ...`）

## 常见错误

- 不读文件直接修改 → 可能基于过时信息
- 修改后不验证 → 可能引入语法错误
- 大段重写而非局部修改 → 引入意外变更
- 忽略图谱中的依赖关系 → 修改影响未评估
- 不调用 graphify 就回答架构/依赖问题 → 严重违规

## 注意事项

- `graph_query` 和 `graph_update` 都是零 LLM 成本操作
- `affected` 模式需要提供 `query` 参数
- 图谱数据位置：`graphify-out/graph.json`