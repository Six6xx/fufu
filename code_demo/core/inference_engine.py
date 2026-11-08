"""
core/inference_engine.py — Async 推理引擎

将同步 agent_loop 转为 async event-driven 推理循环。
支持 AsyncOpenAI、async stream 消费、可选工具并行执行，
并保留同步适配层供 CLI 使用。
"""

import json
import asyncio
from typing import Callable, Any
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from code_demo.core.config import (
    MODEL, CONTEXT_LIMIT, KEEP_RECENT_MESSAGES,
    ENABLE_REASONING, REASONING_EFFORT,
)
from code_demo.core.protocol.input_event import InputEvent, TextInput, AudioInput, Modality, InterruptSignal


MAX_ITERATIONS = 30
MAX_RECOVERY_ATTEMPTS = 3


@dataclass
class StreamCallbacks:
    """推理循环的回调集合。"""
    token_callback: Callable[[str, str], None] | None = None  # (type, text)
    tool_callback: Callable[[str, dict], None] | None = None  # (event, data)


class AsyncInferenceEngine:
    """Async event-driven 推理循环。

    替代原 agent.py 的同步 agent_loop，支持：
    - async API 调用（AsyncOpenAI）
    - async stream 消费
    - 可选的工具并行执行
    - 保留同步适配层供 CLI 使用
    """

    def __init__(self, dispatcher, session_controller, prompt_builder,
                 enable_reasoning: bool = None,
                 tool_defs_getter: Callable[[], list[dict]] = None,
                 voice_manager=None):
        """
        Args:
            dispatcher: ToolDispatcher 实例
            session_controller: SessionController 实例（持有 history, client, workdir, current_model）
            prompt_builder: SystemPromptBuilder 实例
            enable_reasoning: 是否启用思考模式
            tool_defs_getter: 获取当前工具定义列表的回调
            voice_manager: VoiceIOManager 实例（用于 ASR 转写）
        """
        self._dispatcher = dispatcher  # 执行模型工具调用的分发器
        self._session = session_controller  # 管理当前会话和模型客户端的控制器
        self._prompt_builder = prompt_builder  # 生成推理提示词的构建器
        self._enable_reasoning = ENABLE_REASONING if enable_reasoning is None else enable_reasoning  # 是否向模型请求推理内容
        self._tool_defs_getter = tool_defs_getter or (lambda: [])  # 动态获取当前工具定义的回调
        self._voice_manager = voice_manager  # 处理语音输入输出的管理器
        self._async_client: AsyncOpenAI | None = None  # 当前模型供应商的异步客户端
        self._current_run_task: asyncio.Task | None = None  # 当前正在执行的推理任务

    def _ensure_async_client(self):
        """从同步 client 的配置创建/更新 AsyncOpenAI 实例。"""
        sync_client = self._session.client
        if sync_client is None:
            self._async_client = None
            return
        # 复用同步 client 的 api_key 和 base_url
        self._async_client = AsyncOpenAI(
            api_key=sync_client.api_key,
            base_url=str(sync_client.base_url),
        )

    async def run(self, user_input: str = None, callbacks: StreamCallbacks = None,
                  context_limit: int = None, attachments: list = None,
                  original_content: str = None) -> str | None:
        """Async 推理循环主入口。

        完整复制原 agent_loop 的逻辑，但使用 async/await。

        Args:
            user_input: 用户输入文本
            callbacks: 流式回调集合
            context_limit: 上下文字符数限制
            attachments: 附件列表
            original_content: 原始用户文本（未注入文件内容）
        """
        if callbacks is None:
            callbacks = StreamCallbacks()

        self._ensure_async_client()
        if self._async_client is None:
            return "错误：LLM 客户端未初始化。请配置 DASHSCOPE_API_KEY。"

        history = self._session.history

        # 初始化历史（首次调用时）
        if not history:
            system_prompt = self._prompt_builder.build()
            history.append({"role": "system", "content": system_prompt})

        # 添加用户输入
        if user_input:
            msg = {"role": "user", "content": user_input}
            if attachments:
                msg["attachments"] = attachments
            if original_content:
                msg["original_content"] = original_content
            history.append(msg)

        self._current_run_task = asyncio.current_task()

        try:
            max_output_recovery_count = 0
            effective_context_limit = context_limit or CONTEXT_LIMIT

            for iteration in range(MAX_ITERATIONS):
                # 上下文大小检查
                context_size = len(str(history))
                if context_size > effective_context_limit:
                    print(f"[Engine] 上下文过大 ({context_size} 字符，限制 {effective_context_limit})，进行压缩...")
                    self._compact_history(history)

                # ── async API 调用（带重试）──
                stream = None
                for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):
                    try:
                        api_kwargs = {
                            "model": self._session.current_model or MODEL,
                            "messages": self._clean_messages(history),
                            "tools": self._tools_to_openai_format(),
                            "tool_choice": "auto",
                            "stream": True,
                        }
                        if self._enable_reasoning:
                            api_kwargs["reasoning_effort"] = REASONING_EFFORT

                        stream = await self._async_client.chat.completions.create(**api_kwargs)
                        break
                    except Exception as e:
                        if attempt < MAX_RECOVERY_ATTEMPTS:
                            delay = min(2 ** attempt, 30)
                            print(f"\n[Recovery] API 调用失败: {e}，{delay}s 后重试...")
                            await asyncio.sleep(delay)
                            continue
                        print(f"\n[Error] API 调用失败: {e}")
                        return f"API 调用失败: {str(e)[:200]}"

                if stream is None:
                    return "错误：API 响应为空"

                # ── async stream 消费 ──
                accumulated_content = ""
                accumulated_reasoning = ""
                accumulated_tool_calls: dict[int, dict] = {}
                finish_reason = None

                print()  # 换行，准备输出
                async for chunk in stream:
                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta
                    finish_reason = chunk.choices[0].finish_reason or finish_reason

                    # 累积思考内容（DeepSeek reasoning_content）
                    reasoning = getattr(delta, 'reasoning_content', None)
                    if reasoning:
                        accumulated_reasoning += reasoning
                        if callbacks.token_callback:
                            callbacks.token_callback("reasoning", reasoning)

                    # 累积正文
                    if delta.content:
                        accumulated_content += delta.content
                        print(delta.content, end="", flush=True)
                        if callbacks.token_callback:
                            callbacks.token_callback("content", delta.content)

                    # 累积 tool_calls（流式模式下分多次到达）
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": tc.id or "",
                                    "function": {"name": "", "arguments": ""},
                                }
                            entry = accumulated_tool_calls[idx]
                            if tc.id:
                                entry["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    entry["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    entry["function"]["arguments"] += tc.function.arguments

                if accumulated_content:
                    print()  # 流式输出结束后换行

                # ── max_tokens 恢复 ──
                if finish_reason == "length":
                    max_output_recovery_count += 1
                    if max_output_recovery_count <= 3:
                        print("[Recovery] 输出被截断，请求续接...")
                        history.append({"role": "assistant", "content": accumulated_content})
                        history.append({"role": "user", "content": "输出已达上限。请直接从停止处继续。"})
                        continue
                    else:
                        print("[Error] max_tokens 恢复次数已耗尽，终止生成")
                        if accumulated_content:
                            history.append({
                                "role": "assistant",
                                "content": accumulated_content + "\n\n[输出被截断，已达最大恢复次数]",
                            })
                        break
                else:
                    max_output_recovery_count = 0

                # ── 构造 assistant 消息 ──
                assistant_msg = {"role": "assistant"}
                tool_calls_list = []

                for idx in sorted(accumulated_tool_calls.keys()):
                    tc = accumulated_tool_calls[idx]
                    if tc["function"]["name"]:
                        tool_calls_list.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        })

                if accumulated_content:
                    assistant_msg["content"] = accumulated_content
                if tool_calls_list:
                    assistant_msg["tool_calls"] = tool_calls_list

                # 仅当有实质内容时才写入历史
                if accumulated_content or tool_calls_list:
                    history.append(assistant_msg)

                # ── 无工具调用 → 返回累积文本 ──
                if not tool_calls_list:
                    return accumulated_content or "(空响应)"

                # ── 执行工具调用 ──
                results = []
                for tc in tool_calls_list:
                    func_name = tc["function"]["name"]
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        results.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"Error: {func_name} 参数 JSON 无效",
                        })
                        continue

                    # 通知：工具调用开始
                    if callbacks.tool_callback:
                        callbacks.tool_callback("tool_call", {"name": func_name, "args": func_args})

                    # 工具执行（同步 dispatcher，用 run_in_executor 避免阻塞 event loop）
                    loop = asyncio.get_running_loop()
                    output = await loop.run_in_executor(
                        None, self._dispatcher.dispatch, func_name, func_args
                    )
                    print(f"  > {func_name}: {str(output)[:150]}")

                    # 通知：工具执行完成
                    if callbacks.tool_callback:
                        callbacks.tool_callback("tool_result", {
                            "name": func_name,
                            "result": str(output)[:2000],
                        })

                    results.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(output),
                    })

                history.extend(results)

            # 达到最大迭代次数
            return "(已达到最大推理轮次)"
        except asyncio.CancelledError:
            print("[Engine] 推理被中断取消")
            # 将中断标记记录到 history 中保持上下文完整性
            history = self._session.history
            if history and history[-1].get("role") == "assistant":
                history[-1]["content"] = history[-1].get("content", "") + " [interrupted]"
            return "[interrupted]"
        finally:
            self._current_run_task = None

    async def process_event(self, event: InputEvent, callbacks: StreamCallbacks = None) -> str | None:
        """基于 InputEvent 的统一入口。

        根据事件模态分发到对应处理器。
        当前仅实现 TEXT 模态，其他模态为预留接口。
        """
        match event.modality:
            case Modality.TEXT:
                text_event: TextInput = event
                return await self.run(
                    user_input=text_event.content,
                    callbacks=callbacks,
                    attachments=text_event.attachments,
                    original_content=text_event.original_content,
                )
            case Modality.INTERRUPT:
                interrupt: InterruptSignal = event
                if interrupt.cancel_current and self._current_run_task:
                    self._current_run_task.cancel()
                    print(f"[Engine] 取消推理任务: {interrupt.reason}")
                else:
                    print(f"[Engine] 收到中断信号但无活跃任务: {interrupt.reason}")
                return None
            case Modality.AUDIO:
                audio_event: AudioInput = event
                if not self._voice_manager:
                    print("[Engine] 无 VoiceIOManager，忽略音频输入")
                    return None
                text = await self._voice_manager.transcribe(
                    audio_bytes=audio_event.audio_bytes,
                    sample_rate=audio_event.sample_rate,
                    format=audio_event.format,
                )
                if not text:
                    return None  # 静音/噪声
                print(f"[Engine] ASR 识别结果: {text[:50]}")
                return await self.run(user_input=text, callbacks=callbacks)
            case _:
                # 其他模态暂未实现
                print(f"[Engine] 未支持的输入模态: {event.modality.value}")
                return None

    def run_sync(self, user_input: str = None, token_callback=None, tool_callback=None,
                 context_limit=None, attachments=None, original_content=None) -> str | None:
        """同步适配层，供 CLI REPL 使用。"""
        callbacks = StreamCallbacks(
            token_callback=token_callback,
            tool_callback=tool_callback,
        )
        # 在已有 event loop 中运行时需特殊处理
        try:
            loop = asyncio.get_running_loop()
            # 已有 running loop → 在新线程中运行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self.run(user_input, callbacks, context_limit, attachments, original_content)
                )
                return future.result()
        except RuntimeError:
            # 没有 running loop → 直接用 asyncio.run
            return asyncio.run(
                self.run(user_input, callbacks, context_limit, attachments, original_content)
            )

    # ── 辅助方法（从 agent.py 迁移）──

    def _clean_messages(self, messages: list) -> list:
        """清理消息列表，确保符合 API 格式要求。

        internal 标记的消息（自主行动产生，含自主安全边界 prompt）仅作历史记录，
        不进入后续 LLM 上下文，避免自主行动规则泄漏到用户请求路径。
        """
        cleaned = []
        for msg in messages:
            if msg.get("internal"):
                continue
            clean: dict = {"role": msg["role"]}
            content = msg.get("content", "")
            if isinstance(content, str):
                clean["content"] = content
            elif content is None:
                clean["content"] = ""
            else:
                try:
                    clean["content"] = json.dumps(content, ensure_ascii=False)
                except (TypeError, ValueError):
                    clean["content"] = str(content)

            # 保留 tool_calls（assistant 消息）
            if "tool_calls" in msg:
                clean["tool_calls"] = msg["tool_calls"]

            # 保留 tool_call_id（tool 消息）
            if "tool_call_id" in msg:
                clean["tool_call_id"] = msg["tool_call_id"]

            cleaned.append(clean)
        return cleaned

    def _compact_history(self, history: list):
        """压缩对话历史：保留系统消息 + 最新 N 条消息。

        截断点必须落在安全的消息边界上（user 消息或无 tool_calls 的
        assistant 消息），避免切断 assistant(tool_calls) → tool 序列。

        注意：in-place 修改 history 列表以保持外部引用完整。
        """
        system_msgs = [m for m in history if m.get("role") == "system"]
        non_system = [m for m in history if m.get("role") != "system"]

        if len(non_system) <= KEEP_RECENT_MESSAGES:
            return  # 不需要压缩

        # 初始截断点
        cut = len(non_system) - KEEP_RECENT_MESSAGES

        # 向前回溯，找到安全的截断点
        while cut > 0:
            msg = non_system[cut]
            role = msg.get("role")
            if role == "user":
                break
            if role == "assistant" and not msg.get("tool_calls"):
                break
            cut -= 1

        keep = non_system[cut:]
        # in-place 修改以保持引用完整
        history.clear()
        history.extend(system_msgs + keep)
        print(f"[Engine] 上下文已压缩至 {len(history)} 条消息")

    def _tools_to_openai_format(self) -> list[dict]:
        """将内部工具定义转换为 OpenAI Chat Completions 格式。"""
        openai_tools = []
        for t in self._tool_defs_getter():
            props = t.get("parameters", {}).get("properties", {})
            required = t.get("parameters", {}).get("required", [])
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            })
        return openai_tools
