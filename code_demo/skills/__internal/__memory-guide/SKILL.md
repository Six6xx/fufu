---
name: __memory-guide
description: "记忆管理专项指导——类型判断、recall使用、保存时机和UserProfile提取"
source: manual
locked: true
visible: false
layer: memory
status: active
---

# 记忆管理指导

本项目的 `MemoryManager` 将记忆存储为 `ai_configuration/.memory/` 目录下的独立 Markdown 文件，每个文件使用 YAML frontmatter，并通过 `MEMORY.md` 索引文件汇总（上限 200 行）。

## 记忆格式

```markdown
---
name: 简短标识名
description: 一行摘要，用于判断相关性
type: user | feedback | project | reference
---

记忆正文内容...
```

## 四种记忆类型

### user — 用户偏好/习惯/个人信息
记录用户角色的持久性信息，帮助未来对话调整协作方式。

**何时保存**：
- 用户表明偏好（如"我喜欢用制表符"、"始终使用 pytest"）
- 了解到用户的技术背景或职责（如"我是后端工程师"、"我负责运维"）

**示例**：
```
name: user-prefs
description: 用户偏好——代码风格与工具链选择
type: user
---
- 偏好制表符缩进
- 测试框架使用 pytest
- 输出语言：中文
```

### feedback — 行为反馈/纠正
用户对你工作方式的纠正或确认，指导未来行为改进。

**何时保存**：
- 用户纠正你（如"不要这样做"、"那样错了"）
- 用户确认某种做法有效（如"对，就是这样"）

**示例**：
```
name: no-mock-db
description: 集成测试必须使用真实数据库，禁止 mock
type: feedback
---
用户明确要求集成测试不使用 mock 数据库。
原因：上次 mock 测试通过但生产迁移失败。
```

### project — 项目事实/架构决策
无法从代码直接推断的项目级信息：决策、截止日期、业务规则。

**何时保存**：
- 了解到项目决策的背景原因（如"这个中间件因合规要求必须保留"）
- 获知时间节点或里程碑（如"下周三前冻结合并"）

**示例**：
```
name: merge-freeze
description: 2026-03-05 起冻结非关键合并
type: project
---
移动端 release 分支切割后，冻结所有非关键 PR 工作。
```

### reference — 外部资源链接/文档引用
指向外部系统或资源的指针。

**何时保存**：
- 获知工单看板、仪表盘、文档链接等外部资源位置

**示例**：
```
name: bug-tracker
description: Bug 跟踪在 Linear 的 INGEST 项目中
type: reference
---
管道相关 Bug 统一在 Linear 项目 "INGEST" 中跟踪。
```

## recall() 使用

- 收到查询时自动调用 `recall(query)`，将结果自然融入回答
- **不要直接展示原始 recall 结果**——它是参考，不是回复内容
- 融合方式：基于 recall 信息给出更精准的回答，但保持自然语气
- 如果 recall 返回的信息与当前问题不相关，忽略它

## 何时保存

- 用户表达**明确偏好**时 → type: user
- 用户**纠正你的行为**时 → type: feedback
- 发现**项目架构决策**且无法从代码推导时 → type: project
- 获知**外部资源位置**时 → type: reference
- 保存时机：识别到信息的当下，不要等到对话结束

## 何时不保存

- 可从代码直接推导的信息（函数签名、文件结构、目录布局）
- 仅本次对话有用的临时内容（当前调试方案）
- 调试中的试错方案（修复已在代码中，提交信息有上下文）
- Git 历史可查的信息（最近变更、提交记录）
- 临时任务状态（当前分支、打开的 PR、待办事项）
- 敏感信息或凭证（API 密钥、密码、Token）

## UserProfile 隐式提取

- 从对话中正则匹配用户习惯和偏好
- 零 LLM 成本，后台静默运行
- 提取维度：编程语言偏好、工具链选择、输出语言、代码风格
- 提取结果写入 UserProfile，供后续对话使用
- 不需要用户明确声明，从行为中推断

## 记忆索引

- 所有记忆文件通过 `MEMORY.md` 索引文件汇总（上限 200 行）
- 索引保持精简：名称 + 一行摘要
- 记忆文件存放在 `ai_configuration/.memory/` 目录下