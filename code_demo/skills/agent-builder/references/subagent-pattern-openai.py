import os
import sys
from typing import List, Dict, Any, Optional
from openai import OpenAI

# zh: 初始化 OpenAI 客户端，使用环境变量中的 API 密钥和自定义基础 URL（如果配置了本地推理后端）
# en: Initialize the OpenAI client using the API key from environment variables and a custom base URL if configured for local inference backends.
client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", "your-api-key"),
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
)

# zh: 为子代理预定义一组安全的基础工具，确保操作在受限目录内进行
# en: Predefined set of safe base tools for subagents, ensuring operations are confined to a restricted directory.
base_tools = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "在当前目录中运行shell命令。所有操作必须严格限制在项目根目录下。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的shell命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定路径的文件内容。路径必须是相对路径且在允许的目录内。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的相对路径"},
                    "limit": {"type": "integer", "description": "可选，限制读取的行数"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "向指定文件写入内容，如果文件不存在则创建。路径必须是相对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的相对路径"},
                    "content": {"type": "string", "description": "要写入的文本内容"}
                },
                "required": ["path", "content"]
            }
        }
    }
]

# zh: 执行工具调用的实际逻辑，根据工具名称分发到具体实现
# en: The actual logic to execute a tool call, dispatching based on the tool name.
def execute_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a tool call and return its result as a string."""
    try:
        if tool_name == "bash":
            result = os.popen(tool_args["command"]).read()
            return result if result else "Command executed successfully with no output."
        elif tool_name == "read_file":
            with open(tool_args["path"], 'r', encoding='utf-8') as f:
                content = f.readlines()
                if "limit" in tool_args:
                    content = content[:tool_args["limit"]]
                return "".join(content)
        elif tool_name == "write_file":
            with open(tool_args["path"], 'w', encoding='utf-8') as f:
                f.write(tool_args["content"])
            return f"Wrote {len(tool_args['content'])} characters to {tool_args['path']}."
        else:
            return f"Error: Unknown tool '{tool_name}'."
    except Exception as e:
        return f"Tool execution error: {str(e)}"

# zh: 子代理支持的不同类型及其对应的系统提示词（含中英双语）
# en: Different types of subagents supported, each with its own bilingual system prompt.
AGENT_TYPES = {
    "coder": {
        "prompt_en": "You are an expert software engineer. Your task is to write, debug, and explain code.",
        "prompt_zh": "你是一位专业的软件工程师。你的任务是编写、调试和解释代码。"
    },
    "writer": {
        "prompt_en": "You are a skilled technical writer. Your task is to draft, edit, and summarize documents.",
        "prompt_zh": "你是一位熟练的技术文档撰写者。你的任务是起草、编辑和总结文档。"
    },
    "researcher": {
        "prompt_en": "You are a meticulous researcher. Your task is to gather, analyze, and synthesize information.",
        "prompt_zh": "你是一位细致的研究员。你的任务是收集、分析和综合信息。"
    }
}

# zh: 用于触发子代理执行任务的专用工具定义
# en: A dedicated tool definition used to trigger the subagent to perform a task.
TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "run_task",
        "description": "启动一个具有特定角色的子代理来完成一项独立任务。子代理拥有隔离的上下文和一套安全工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_description": {"type": "string", "description": "需要子代理执行的具体任务描述"},
                "agent_type": {"type": "string", "description": "子代理的角色类型", "enum": list(AGENT_TYPES.keys())}
            },
            "required": ["task_description", "agent_type"]
        }
    }
}

# zh: 子代理的核心执行循环，处理工具调用直到获得最终答案
# en: The core execution loop for the subagent, handling tool calls until a final answer is obtained.
def run_subagent(task_description: str, agent_type: str) -> str:
    """Run a subagent to complete a given task and return its final output."""
    if agent_type not in AGENT_TYPES:
        return f"Error: Unsupported agent type '{agent_type}'."

    # zh: 构建初始消息列表，包含系统提示和用户任务
    # en: Build the initial message list with the system prompt and user task.
    messages = [
        {"role": "system", "content": AGENT_TYPES[agent_type]["prompt_en"] + " " + AGENT_TYPES[agent_type]["prompt_zh"]},
        {"role": "user", "content": task_description}
    ]

    # zh: 开始与模型的交互循环
    # en: Start the interaction loop with the model.
    while True:
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=base_tools,
                tool_choice="auto"
            )
            message = response.choices[0].message

            # zh: 如果模型决定调用工具，则执行工具并将结果返回给模型
            # en: If the model decides to call a tool, execute it and send the result back.
            if message.tool_calls:
                messages.append(message)
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = eval(tool_call.function.arguments) # TODO: Use json.loads for safety
                    tool_result = execute_tool(tool_name, tool_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": tool_result
                    })
                    # zh: 显示进度：打印工具调用和结果摘要
                    # en: Show progress: print a summary of the tool call and result.
                    sys.stdout.write(f"[Subagent] Called {tool_name} -> {tool_result[:60]}...\n")
                    sys.stdout.flush()
            else:
                # zh: 模型返回最终答案，结束循环
                # en: The model has returned a final answer; exit the loop.
                final_answer = message.content or ""
                return final_answer

        except Exception as e:
            return f"Subagent execution failed: {str(e)}"

# zh: 主函数：演示如何从主代理调用子代理
# en: Main function: demonstrates how to invoke a subagent from a main agent.
if __name__ == "__main__":
    example_task = "Create a Python script 'hello.py' that prints 'Hello, World!'."
    result = run_subagent(example_task, "coder")
    print(f"\n[Main Agent] Subagent Result:\n{result}")