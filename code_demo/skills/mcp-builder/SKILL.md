---
name: mcp-builder
description: 构建 MCP（模型上下文协议）服务器，为 agent 赋予新能力。当用户希望创建 MCP 服务器、为 agent 添加工具或集成外部服务时使用。
source: manual
locked: false
created_at: 2026-06-20
updated_at: 2026-07-06
usage_count: 2
---

# MCP 服务器构建技能（MCP Server Building Skill）

你现在具备构建 MCP（Model Context Protocol，模型上下文协议）服务器的专业能力。MCP 使芙芙能通过标准化协议与外部服务交互。

## 什么是 MCP？

MCP 服务器为 Agent 提供以下能力：
- **工具（Tools）**：Agent 可调用的函数（类似 API 端点），注册后以 `mcp__{server}__{tool}` 格式暴露
- **资源（Resources）**：Agent 可读取的数据（如文件或数据库记录）
- **提示模板（Prompts）**：预构建的提示词模板

## 项目 MCP 架构

本项目通过以下组件集成 MCP：

```
MCPToolRouter (core/mcp/tool_router.py)
├── MCPConfigLoader — 从 mcp_config.json 加载配置
├── MCPStdioClient — stdio 传输（本地进程）
├── MCPHttpClient — HTTP 传输（streamableHttp / sse）
└── 动态工具注册 — mcp__{server_name}__{tool_name}
```

**配置文件**：`mcp_config.json`（项目根目录）

## 快速入门：注册 MCP 服务器

### 1. 在 mcp_config.json 中添加配置

```json
{
  "mcpServers": {
    "my-server": {
      "command": "python3",
      "args": ["/path/to/my_server.py"],
      "type": "stdio"
    },
    "remote-server": {
      "url": "http://localhost:3000/mcp",
      "type": "streamableHttp"
    }
  }
}
```

**支持的传输类型**：
| 类型 | 说明 | 配置字段 |
|------|------|----------|
| `stdio` | 本地进程通信 | `command` + `args` |
| `streamableHttp` | HTTP 流式通信 | `url` |
| `sse` | Server-Sent Events | `url` |

### 2. 构建 stdio MCP 服务器（Python）

```python
#!/usr/bin/env python3
"""my_server.py - 标准 MCP stdio 服务器"""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("my-server")

@server.tool()
async def hello(name: str) -> str:
    """向某人问好。

    参数：
        name: 要问候的姓名
    """
    return f"Hello, {name}!"

@server.tool()
async def add_numbers(a: int, b: int) -> str:
    """将两个数字相加。

    参数：
        a: 第一个数字
        b: 第二个数字
    """
    return str(a + b)

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

安装依赖：
```bash
pip install mcp
```

### 3. 构建 stdio MCP 服务器（TypeScript）

```bash
npm install @modelcontextprotocol/sdk
```

```typescript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const server = new Server({ name: "my-server", version: "1.0.0" });

server.setRequestHandler("tools/list", async () => ({
  tools: [{
    name: "hello",
    description: "向某人问好",
    inputSchema: {
      type: "object",
      properties: { name: { type: "string", description: "要问候的姓名" } },
      required: ["name"],
    },
  }],
}));

server.setRequestHandler("tools/call", async (request) => {
  if (request.params.name === "hello") {
    return { content: [{ type: "text", text: `Hello, ${request.params.arguments.name}!` }] };
  }
  throw new Error("未知工具");
});

const transport = new StdioServerTransport();
server.connect(transport);
```

## 高级模式

### 外部 API 集成

```python
import httpx
from mcp.server import Server

server = Server("weather-server")

@server.tool()
async def get_weather(city: str) -> str:
    """获取某城市的当前天气。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.weatherapi.com/v1/current.json",
            params={"key": "YOUR_API_KEY", "q": city}
        )
        data = resp.json()
        return f"{city}: {data['current']['temp_c']}°C, {data['current']['condition']['text']}"
```

### 数据库访问

```python
import sqlite3
from mcp.server import Server

server = Server("db-server")

@server.tool()
async def query_db(sql: str) -> str:
    """执行只读 SQL 查询。"""
    if not sql.strip().upper().startswith("SELECT"):
        return "错误：仅允许 SELECT 查询"
    conn = sqlite3.connect("data.db")
    rows = conn.execute(sql).fetchall()
    conn.close()
    return str(rows)
```

## Agent 侧工具调用方式

MCP 工具在 Agent 中以 `mcp__{server}__{tool}` 格式暴露，例如：

```
工具名: mcp__weather-server__get_weather
参数:   {"city": "北京"}
```

`MCPToolRouter` 负责：
1. 启动时从 `mcp_config.json` 加载所有服务器配置
2. 为每个服务器建立连接（stdio 进程 或 HTTP 客户端）
3. 枚举各服务器的工具列表，统一注册到 Agent 工具池
4. 转发 Agent 的工具调用到对应 MCP 服务器

## 测试

```bash
# 使用 MCP Inspector 测试
npx @anthropics/mcp-inspector python3 my_server.py

# 或直接发送测试消息
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 my_server.py
```

## 最佳实践

1. **清晰的工具描述**：Agent 依靠描述决定何时调用工具，描述越具体效果越好
2. **输入验证**：始终验证并清理输入，防止注入攻击
3. **错误处理**：返回有意义的错误信息，而非抛出未处理异常
4. **默认异步**：对 I/O 操作使用 async/await，避免阻塞事件循环
5. **安全性**：敏感操作（写入、删除）需明确标注，切勿暴露无鉴权接口
6. **幂等性**：工具应可安全重试，避免副作用叠加
7. **配置管理**：API Key 等敏感信息通过环境变量传入，不写入 `mcp_config.json`
