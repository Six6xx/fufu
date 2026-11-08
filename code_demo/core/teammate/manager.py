"""
code_demo/core/teammate/manager.py — 队友管理器

持久化队友注册表及工作循环启动器，支持关机和计划审批。
来源：19_mcp-plugin.py L684–L1035 (TeammateManager)
"""

import json
import threading
import time
import uuid
from pathlib import Path

from ..config import (
    TEAM_DIR, MODEL, WORKDIR, VALID_MSG_TYPES,
)
from ..event.bus import BUS
from ..protocol.store import REQUEST_STORE
from .functions import scan_unclaimed_tasks, claim_task


# 空闲轮询参数
IDLE_TIMEOUT = 300   # 空闲超时（秒）
POLL_INTERVAL = 5    # 轮询间隔（秒）


def _ensure_identity_context(messages: list, name: str, role: str, team_name: str):
    """确保对话上下文中包含身份信息，防止长对话丢失身份与目标。"""
    identity = (
        f"[System Reminder] 你是 '{name}', role: {role}, "
        f"team: {team_name}, workdir: {WORKDIR}"
    )
    messages.append({"role": "user", "content": identity})


class TeammateManager:
    """持久化队友注册表及工作循环启动器。"""

    def __init__(self, team_dir: Path = None):
        self.dir = team_dir or TEAM_DIR  # 队友协作文件所在目录
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"  # 队友配置文件路径
        self.config = self._load_config()  # 已加载的队友配置
        self.threads = {}  # 正在运行的队友工作线程
        self._client = None  # 延迟初始化的 OpenAI client

    def _get_client(self):
        """延迟初始化 OpenAI 兼容客户端。"""
        if self._client is None:
            from openai import OpenAI
            import os
            self._client = OpenAI(
                api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url=os.getenv("DASHSCOPE_API_BASE_URL"),
            )
        return self._client

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """创建并启动一个队友线程。"""
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' 当前状态为 {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()

        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"已创建 '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        """
        协作团队成员的主循环：持续处理消息、执行工具、认领任务。
        """
        from ..tools.native.runner import run_bash, run_read, run_write, run_edit

        team_name = self.config["team_name"]
        sys_prompt = (
            f"你是 '{name}', role: {role}, 工作目录在 {WORKDIR}。"
            f"当你没有更多工作时，请使用 idle 工具。你将自动认领新任务。"
            f"使用 send_message 交流，完成你的工作。"
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        tools = self._teammate_tools()
        client = self._get_client()
        idle_requested = False

        while True:
            # ========================
            # 第一阶段：主动工作阶段（最多 50 轮）
            # ========================
            idle_requested = False
            for _ in range(50):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    content_str = json.dumps(msg["content"], indent=2,
                                             ensure_ascii=False)
                    messages.append({"role": "user", "content": content_str})

                if idle_requested:
                    break

                try:
                    response = client.chat.completions.create(
                        model=MODEL, messages=messages, tools=tools,
                    )
                except Exception:
                    self._set_status(name, "idle")
                    return

                content_assistant = response.choices[0].message
                messages.append({
                    "role": "assistant",
                    "content": content_assistant.content or "",
                })

                if content_assistant.content:
                    print(f"\033[32m[{name}] {content_assistant.content}\033[0m")

                if not content_assistant.tool_calls:
                    break

                results = []
                for tool_call in content_assistant.tool_calls:
                    if tool_call.function.name == "idle":
                        idle_requested = True
                        output = "进入空闲阶段。将轮询新任务。"
                    else:
                        args = json.loads(tool_call.function.arguments)
                        output = self._exec(name, tool_call.function.name, args)
                    print(f"[{name}] 调用 {tool_call.function.name}: "
                          f"{str(output)[:120]}...")
                    results.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(output),
                    })
                messages.extend(results)

                if idle_requested:
                    break

            # ========================
            # 第二阶段：空闲轮询阶段
            # ========================
            self._set_status(name, "idle")
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            resume = False
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                inbox = BUS.read_inbox(name)
                if inbox:
                    _ensure_identity_context(messages, name, role, team_name)
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({
                            "role": "user",
                            "content": json.dumps(msg, indent=2,
                                                  ensure_ascii=False),
                        })
                    resume = True
                    break

                unclaimed = scan_unclaimed_tasks(role)
                if unclaimed:
                    task = unclaimed[0]
                    claim_result = claim_task(
                        task["id"], name, role=role, source="auto"
                    )
                    if claim_result.startswith("Error:"):
                        continue
                    task_prompt = (
                        f"<auto-claimed>任务 # {task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    _ensure_identity_context(messages, name, role, team_name)
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({
                        "role": "assistant",
                        "content": f"{claim_result}. 正在处理中",
                    })
                    resume = True
                    break

            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """执行队友的工具调用。"""
        from ..tools.native.runner import run_bash, run_read, run_write, run_edit

        if tool_name == "bash":
            return run_bash(args["command"])
        if tool_name == "read_file":
            return run_read(args["path"])
        if tool_name == "write_file":
            return run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"],
                            args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2,
                              ensure_ascii=False)
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            approve = args["approve"]
            updated = REQUEST_STORE.update(
                req_id,
                status="approved" if approve else "rejected",
                resolved_by=sender,
                resolved_at=time.time(),
                response={"approve": approve, "reason": args.get("reason", "")},
            )
            if not updated:
                return f"Error: Unknown shutdown request {req_id}"
            BUS.send(sender, "lead", args.get("reason", ""),
                     "shutdown_response",
                     {"request_id": req_id, "approve": approve})
            return f"Shutdown {'approved' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            REQUEST_STORE.create({
                "request_id": req_id,
                "kind": "plan_approval",
                "from": sender,
                "to": "lead",
                "status": "pending",
                "plan": plan_text,
                "created_at": time.time(),
                "updated_at": time.time(),
            })
            BUS.send(sender, "lead", plan_text, "plan_approval",
                     {"request_id": req_id, "plan": plan_text})
            return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
        if tool_name == "claim_task":
            member = self._find_member(sender)
            return claim_task(
                args["task_id"], sender,
                role=member.get("role") if member else None,
                source="manual",
            )
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        """构建队友可用的工具列表。"""
        from ..tools.native.runner import NATIVE_TOOL_SCHEMAS

        base_tools = list(NATIVE_TOOL_SCHEMAS)

        return base_tools + [
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "给团队成员发送消息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string"},
                            "content": {"type": "string"},
                            "msg_type": {
                                "type": "string",
                                "enum": list(VALID_MSG_TYPES),
                            },
                        },
                        "required": ["to", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_inbox",
                    "description": "读取并清空你的收件箱。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "shutdown_response",
                    "description": "请响应关机请求。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string"},
                            "approve": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "plan_approval",
                    "description": "提交一份计划以供负责人审批。",
                    "parameters": {
                        "type": "object",
                        "properties": {"plan": {"type": "string"}},
                        "required": ["plan"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "idle",
                    "description": "表明你已无更多工作。将进入空闲轮询阶段。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "claim_task",
                    "description": "按任务 ID 从任务看板认领一个任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                    },
                },
            },
        ]

    def list_all(self) -> str:
        """列出所有队友。"""
        if not self.config["members"]:
            return "No teammates"
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """返回所有队友名称列表。"""
        return [m["name"] for m in self.config["members"]]
