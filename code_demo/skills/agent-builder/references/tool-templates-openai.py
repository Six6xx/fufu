# zh: OpenAI 格式的工具模板集合。每个工具都是一个符合 OpenAI `tools` 参数规范的字典。
# en: A collection of tool templates in OpenAI format. Each tool is a dictionary conforming to the OpenAI `tools` parameter specification.

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "在当前目录中运行shell命令",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的shell命令"
                }
            },
            "required": ["command"]
        }
    }
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取文件内容，参数为路径和可选的行数限制",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径"
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多读取的行数"
                }
            },
            "required": ["path"]
        }
    }
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "对指定文件写入内容,没有则创建",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径"
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容"
                }
            },
            "required": ["path", "content"]
        }
    }
}

EDIT_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "编辑文件内容，参数为路径、要替换的旧文本和新文本",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径"
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的旧文本"
                },
                "new_text": {
                    "type": "string",
                    "description": "用于替换的新文本"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }
    }
}

LOAD_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "将指定名称的技能完整内容加载到当前上下文中。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "技能文件的路径"
                }
            },
            "required": ["path"]
        }
    }
}

COMPACT_TOOL = {
    "type": "function",
    "function": {
        "name": "compact",
        "description": "总结之前的对话，以便在更小的上下文中继续工作。",
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "总结时关注的重点领域"
                }
            },
            "required": ["focus"]
        }
    }
}

TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "task",
        "description": "启动一个具有全新上下文的子代理。它共享文件系统，但不共享对话历史。",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "给子代理的指令提示"
                },
                "description": {
                    "type": "string",
                    "description": "任务的简要描述"
                }
            },
            "required": ["prompt"]
        }
    }
}