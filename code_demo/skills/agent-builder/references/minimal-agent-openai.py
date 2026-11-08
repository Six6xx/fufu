import os
from openai import OpenAI
from typing import List, Dict, Any

# zh: 初始化 OpenAI 客户端
# en: Initialize the OpenAI client.
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# zh: 加载工具定义（此处仅为示例，实际应从 tool-templates-openai.py 导入）
# en: Load tool definitions (for example only; in practice, import from tool-templates-openai.py).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "在当前目录中运行shell命令",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
        }
    }
]

def execute_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    # zh: 工具执行逻辑（简化版）
    # en: Tool execution logic (simplified).
    if tool_name == "bash":
        return os.popen(tool_args["command"]).read()
    return "Unknown tool."

def run_agent_loop(user_input: str, messages: List[Dict[str, Any]]) -> str:
    """zh: 运行一个极简的 OpenAI 工具调用代理循环。
       en: Run a minimal OpenAI tool-calling agent loop."""
    messages.append({"role": "user", "content": user_input})

    while True:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto"
        )
        message = response.choices[0].message
        messages.append(message)

        if message.tool_calls:
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = eval(tool_call.function.arguments)  # TODO: Use json.loads
                tool_result = execute_tool(tool_name, tool_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": tool_result
                })
        else:
            return message.content or ""

# zh: 使用示例
# en: Usage example.
if __name__ == "__main__":
    conversation = [{"role": "system", "content": "You are a helpful assistant with bash access."}]
    result = run_agent_loop("List files in the current directory.", conversation)
    print(result)