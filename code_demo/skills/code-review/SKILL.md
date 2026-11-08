---
name: code-review
description: 执行全面的代码审查，涵盖安全性、性能和可维护性分析。当用户要求审查代码、检查 bug 或审计代码库时使用。
source: manual
locked: false
created_at: 2026-06-20
updated_at: 2026-07-06
usage_count: 2
---

# 代码审查技能（Code Review Skill）

你现在具备执行全面代码审查的专业能力。使用 `read_file` 阅读代码，使用 `bash` 运行检查命令，使用 `edit_file` 直接修复问题。

## 审查清单

### 1. 安全性（关键）

检查以下内容：
- [ ] **注入漏洞**：SQL 注入、命令注入、XSS、模板注入
- [ ] **身份验证问题**：硬编码凭据、弱认证机制
- [ ] **授权缺陷**：缺少访问控制、不安全的直接对象引用（IDOR）
- [ ] **数据泄露**：敏感信息出现在日志或错误消息中
- [ ] **加密问题**：弱算法、密钥管理不当
- [ ] **依赖项漏洞**：已知漏洞

```bash
# 快速安全扫描
npm audit                    # Node.js
pip-audit                    # Python
cargo audit                  # Rust
grep -r "password\|secret\|api_key" --include="*.py" --include="*.js"
```

### 2. 正确性

检查以下内容：
- [ ] **逻辑错误**：差一错误、空值处理、边界情况
- [ ] **竞态条件**：无同步机制的并发访问
- [ ] **资源泄漏**：未关闭的文件、连接或内存
- [ ] **错误处理**：吞没异常、缺失错误路径
- [ ] **类型安全**：隐式转换、过度使用 any 类型

### 3. 性能

检查以下内容：
- [ ] **N+1 查询**：循环中的数据库调用
- [ ] **内存问题**：大内存分配、未释放的引用
- [ ] **阻塞操作**：异步代码中的同步 I/O
- [ ] **低效算法**：本可用 O(n) 却用了 O(n²)
- [ ] **缺少缓存**：重复执行高开销计算

### 4. 可维护性

检查以下内容：
- [ ] **命名规范**：清晰、一致、具有描述性
- [ ] **复杂度过高**：函数超过 50 行，嵌套深度超过 3 层
- [ ] **代码重复**：复制粘贴的代码块
- [ ] **死代码**：未使用的导入、不可达的分支
- [ ] **注释质量**：过时、冗余或在需要处缺失

### 5. 测试

检查以下内容：
- [ ] **覆盖率**：关键路径是否被测试
- [ ] **边界情况**：null、空值、边界值
- [ ] **模拟隔离**：外部依赖是否被正确模拟
- [ ] **断言质量**：是否有意义、具体明确

## 审查输出格式

```markdown
## 代码审查：[文件/组件名称]

### 摘要
[1–2 句概述]

### 关键问题
1. **[问题]**（第 X 行）：[描述]
   - 影响：[可能导致什么后果]
   - 修复建议：[解决方案]

### 改进建议
1. **[建议]**（第 X 行）：[描述]

### 优点
- [做得好的地方]

### 结论
[ ] 可合并
[ ] 需少量修改
[ ] 需重大修订
```

## 常见需警惕的模式

### Python
```python
# 错误：SQL 注入
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
# 正确：
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

# 错误：命令注入
os.system(f"ls {user_input}")
# 正确：
subprocess.run(["ls", user_input], check=True)

# 错误：可变默认参数
def append(item, lst=[]):  # 缺陷：共享可变默认值
# 正确：
def append(item, lst=None):
    lst = lst or []
```

### JavaScript/TypeScript
```javascript
// 错误：原型污染
Object.assign(target, userInput)
// 正确：
Object.assign(target, sanitize(userInput))

// 错误：使用 eval
eval(userCode)
// 正确：切勿对用户输入使用 eval

// 错误：回调地狱
getData(x => process(x, y => save(y, z => done(z))))
// 正确：
const data = await getData();
const processed = await process(data);
await save(processed);
```

## 审查命令

```bash
# 查看近期变更
git diff HEAD~5 --stat
git log --oneline -10

# 发现潜在问题
grep -rn "TODO\|FIXME\|HACK\|XXX" .
grep -rn "password\|secret\|token" . --include="*.py"

# 检查复杂度（Python）
pip install radon && radon cc . -a

# 检查依赖项
npm outdated  # Node
pip list --outdated  # Python
```

## 审查工作流

1. **理解上下文**：使用 `bash("git log --oneline -10")` 查看近期变更
2. **运行代码**：使用 `bash` 构建、测试并运行
3. **自上而下阅读**：使用 `read_file` 从主入口开始逐文件审查
4. **检查测试**：变更是否被测试？测试是否通过？
5. **安全扫描**：使用 `bash` 运行自动化工具
6. **人工审查**：使用上述清单逐项检查
7. **直接修复**：对于明确的问题，使用 `edit_file` 直接修复
8. **撰写反馈**：具体明确、提供修复建议、保持友善
