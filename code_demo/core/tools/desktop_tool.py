"""
core/tools/desktop_tool.py — 桌面交互工具定义与分派

注册桌面控制和屏幕观察两个工具，路由到 PerceptionManager。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..desktop.perception_manager import PerceptionManager

logger = logging.getLogger(__name__)

# ── 工具 Schema 定义 ──────────────────────────────────────────

DESKTOP_TOOL_DEFS = [
    {
        "name": "desktop_control",
        "description": "控制桌面：鼠标点击、键盘输入、滚动、拖拽等操作。用于需要操作本地GUI应用的场景。所有操作需要用户确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "hotkey", "scroll", "drag", "move"],
                    "description": "要执行的操作类型",
                },
                "x": {"type": "integer", "description": "鼠标X坐标"},
                "y": {"type": "integer", "description": "鼠标Y坐标"},
                "text": {"type": "string", "description": "要输入的文本（action=type时）"},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "组合键列表（action=hotkey时）",
                },
                "clicks": {"type": "integer", "description": "滚动量（action=scroll时）"},
                "x2": {"type": "integer", "description": "拖拽终点X（action=drag时）"},
                "y2": {"type": "integer", "description": "拖拽终点Y（action=drag时）"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "screen_observe",
        "description": "观察屏幕：截屏分析UI元素、开始/停止持续观看、回答关于当前画面的问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "screenshot",
                        "analyze",
                        "locate",
                        "start_watching",
                        "stop_watching",
                        "ask",
                    ],
                    "description": "观察操作类型",
                },
                "task": {
                    "type": "string",
                    "description": "分析任务描述（action=analyze时）",
                },
                "element": {
                    "type": "string",
                    "description": "要定位的元素描述（action=locate时）",
                },
                "question": {
                    "type": "string",
                    "description": "关于画面的问题（action=ask时）",
                },
            },
            "required": ["action"],
        },
    },
]


# ── 异步分派函数 ──────────────────────────────────────────────


async def dispatch_desktop_tool(
    tool_name: str, tool_input: dict, perception: "PerceptionManager"
) -> str:
    """分派桌面工具调用到 PerceptionManager。

    Args:
        tool_name: "desktop_control" 或 "screen_observe"
        tool_input: 工具参数
        perception: PerceptionManager 实例

    Returns:
        JSON 格式的执行结果字符串
    """
    try:
        if tool_name == "desktop_control":
            return await _handle_desktop_control(tool_input, perception)
        elif tool_name == "screen_observe":
            return await _handle_screen_observe(tool_input, perception)
        else:
            return json.dumps({"error": f"未知桌面工具: {tool_name}"}, ensure_ascii=False)
    except Exception as e:
        logger.error("[DesktopTool] 执行异常: tool=%s, error=%s", tool_name, e, exc_info=True)
        return json.dumps(
            {"success": False, "error": f"桌面工具执行异常: {e}"},
            ensure_ascii=False,
        )


async def _handle_desktop_control(
    tool_input: dict, perception: "PerceptionManager"
) -> str:
    """处理 desktop_control 工具调用。"""
    action = tool_input.get("action", "")
    if not action:
        return json.dumps({"success": False, "error": "缺少 action 参数"}, ensure_ascii=False)

    # 构建传给 take_action 的 params（排除 action 字段本身）
    params = {k: v for k, v in tool_input.items() if k != "action"}

    # 对 drag 操作，映射 x/y/x2/y2 → x1/y1/x2/y2
    if action == "drag":
        params["x1"] = params.pop("x", 0)
        params["y1"] = params.pop("y", 0)

    result = await perception.take_action(action, params)
    return json.dumps(result, ensure_ascii=False)


async def _handle_screen_observe(
    tool_input: dict, perception: "PerceptionManager"
) -> str:
    """处理 screen_observe 工具调用。"""
    action = tool_input.get("action", "")
    if not action:
        return json.dumps({"success": False, "error": "缺少 action 参数"}, ensure_ascii=False)

    # ── 自动启动感知（用户不需要显式说 start_watching）──
    auto_start_actions = {"screenshot", "analyze", "locate", "ask"}
    if action in auto_start_actions and not perception._watching:
        try:
            await perception.start_watching()
            logger.info("[DesktopTool] 感知未启动，已自动启动")
        except Exception as e:
            logger.warning("[DesktopTool] 自动启动感知失败: %s", e)

    if action == "screenshot":
        # 真正执行截屏 + VLM 分析
        try:
            import asyncio as _asyncio

            screenshot_bytes = await _asyncio.get_event_loop().run_in_executor(
                None, perception.controller.screenshot
            )
            if not screenshot_bytes:
                return json.dumps(
                    {"success": False, "error": "截屏失败，可能需要屏幕录制权限"},
                    ensure_ascii=False,
                )
            # 调用 VLM 分析截图内容
            description = await perception.vision.answer_question(
                screenshot_bytes, "", "描述当前屏幕上的内容"
            )
            return json.dumps(
                {"success": True, "description": description, "message": "已截取屏幕并分析"},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error("[DesktopTool] screenshot 执行异常: %s", e)
            return json.dumps(
                {"success": False, "error": f"截屏分析失败: {e}"},
                ensure_ascii=False,
            )

    elif action == "analyze":
        task = tool_input.get("task", "描述当前屏幕内容")
        answer = await perception.answer_about_screen(task)
        return json.dumps({"success": True, "analysis": answer}, ensure_ascii=False)

    elif action == "locate":
        element = tool_input.get("element", "")
        if not element:
            return json.dumps(
                {"success": False, "error": "locate 需要 element 参数"},
                ensure_ascii=False,
            )
        coords = await perception.analyze_and_locate(element)
        if coords:
            return json.dumps(
                {"success": True, "x": coords[0], "y": coords[1]},
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": False, "message": f"未找到元素: {element}"},
            ensure_ascii=False,
        )

    elif action == "start_watching":
        await perception.start_watching()
        return json.dumps(
            {"success": True, "message": "感知流水线已启动"},
            ensure_ascii=False,
        )

    elif action == "stop_watching":
        await perception.stop_watching()
        return json.dumps(
            {"success": True, "message": "感知流水线已停止"},
            ensure_ascii=False,
        )

    elif action == "ask":
        question = tool_input.get("question", "")
        if not question:
            return json.dumps(
                {"success": False, "error": "ask 需要 question 参数"},
                ensure_ascii=False,
            )
        answer = await perception.answer_about_screen(question)
        return json.dumps({"success": True, "answer": answer}, ensure_ascii=False)

    else:
        return json.dumps(
            {"success": False, "error": f"未知观察操作: {action}"},
            ensure_ascii=False,
        )


# ── 同步适配器（供 ToolDispatcher 注册使用）──────────────────


def make_sync_handler(tool_name: str, perception: "PerceptionManager"):
    """为指定桌面工具创建同步 handler（供 ToolDispatcher.register 使用）。

    运行环境：inference_engine 通过 run_in_executor 调用 dispatcher，
    工具函数在线程池线程中运行（该线程无 running loop），可安全用 asyncio.run()。
    """

    def handler(params: dict) -> str:
        coro = dispatch_desktop_tool(tool_name, params, perception)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 极少数情况：确实在有 running loop 的线程中调用
            # 创建独立线程来运行协程，避免阻塞当前事件循环
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            # 正常路径：线程池中无 running loop，直接 asyncio.run()
            return asyncio.run(coro)

    return handler
