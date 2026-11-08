"""
code_demo/core/agent_team.py — AgentTeam 执行层

包装现有 Agent，作为"工作团队"执行实际任务。
不创建新的 Agent 实例或 LLM 客户端，直接委托给 agent.agent_loop()。

自进化能力（Phase 3 §4.6）：
  - 执行日志收集：包装 tool_callback 收集每次工具调用的结构化数据
  - 自检分析：执行完成后分析工具调用模式（重复序列/冗余/重试）
  - 自动技能创建：复杂任务（>AUTO_SKILL_MIN_STEPS 步）完成后尝试创建技能
  - 合并建议：检测可合并的技能对
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from code_demo.core.config import AUTO_SKILL_MIN_STEPS, SELF_CHECK_INTERVAL

if TYPE_CHECKING:
    from code_demo.core.agent import Agent
    from code_demo.core.utils.skill_registry import SkillRegistry


class TaskInterruptedError(Exception):
    """用户中断任务"""
    pass


@dataclass
class AgentTeamResult:
    """AgentTeam 执行结果。"""

    task_result: str  # Agent 返回的最终文本
    success: bool = True  # 是否成功完成
    tool_calls_made: int = 0  # 工具调用次数
    error: str | None = None  # 错误信息（仅失败时）
    execution_log: list[dict] = field(default_factory=list)  # 结构化执行日志
    skill_created: str | None = None  # 自动创建的技能名（如有）
    merge_suggestion: dict | None = None  # 合并建议（如有）
    interim_checks: list[dict] = field(default_factory=list)  # 中间自检记录
    interrupted: bool = False  # 是否被用户中断


class AgentTeam:
    """包装现有 Agent 的执行层。

    不创建新的 Agent 实例或 LLM 客户端。
    直接调用 agent.agent_loop()，复用全部现有能力：
    工具调用、MCP、记忆、技能、TTS 分句等。

    自进化：执行日志收集 → 自检分析 → 自动技能创建 → 合并建议。
    """

    def __init__(
        self,
        agent: Agent,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.agent = agent  # 执行任务时复用的核心 Agent
        self.skill_registry = skill_registry  # 执行后技能生成与合并所用的技能注册表

    # ── 执行 ────────────────────────────────────────────────────────────

    def execute(
        self,
        user_input: str,
        token_callback=None,
        tool_callback=None,
        progress_callback=None,    # 新增：进度推送回调
        cancel_check=None,         # 新增：中断检查函数（返回 bool）
        context_limit=None,
        attachments=None,
        original_content=None,
    ) -> AgentTeamResult:
        """执行任务，委托给内部 Agent。

        参数签名与 Agent.agent_loop() 基本一致，透传所有参数。
        新增 progress_callback / cancel_check 用于进度推送与可中断执行。

        增强：
        - 包装 tool_callback 收集执行日志
        - 执行完成后运行自检分析
        - 复杂任务自动创建技能
        - 检测可合并的技能对

        Returns:
            AgentTeamResult 包含任务结果、执行日志、自进化元数据。
        """
        # 包装 tool_callback 以收集执行日志，并植入中间自检钩子
        interim_checks: list[dict] = []
        wrapped_callback, execution_log = self._collect_execution_log(
            tool_callback,
            interim_checks,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

        # ── 预分析：检测分析类任务，注入图谱提示 ──
        pre_hint = self._pre_analysis(user_input)
        if pre_hint:
            user_input = f"{pre_hint}\n\n{user_input}"

        try:
            try:
                result = self.agent.agent_loop(
                    user_input=user_input,
                    token_callback=token_callback,
                    tool_callback=wrapped_callback,
                    context_limit=context_limit,
                    attachments=attachments,
                    original_content=original_content,
                )
            except TaskInterruptedError:
                return AgentTeamResult(
                    task_result="任务已被用户中断",
                    success=False,
                    error="任务已被用户中断",
                    execution_log=execution_log,
                    interim_checks=interim_checks,
                    interrupted=True,
                )

            if result is None or result.startswith("错误"):
                return AgentTeamResult(
                    task_result=result or "(空响应)",
                    success=False,
                    error=result,
                    execution_log=execution_log,
                    interim_checks=interim_checks,
                )

            # 统计工具调用次数
            tool_count = len(execution_log)

            # ── 自检分析 ──
            self_check = self._self_check(execution_log)
            if self_check:
                print(f"[AgentTeam] 自检: {self_check['type']} — {self_check['detail']}")

            # ── 自动技能创建 ──
            skill_created = None
            if (
                self.skill_registry
                and tool_count > AUTO_SKILL_MIN_STEPS
                and self_check
                and self_check.get("type") == "skill_candidate"
            ):
                skill_created = self.skill_registry.auto_create(
                    task_description=user_input or "",
                    execution_log=execution_log,
                    result=result[:200] if result else "",
                )
                if skill_created:
                    print(f"[AgentTeam] 自动创建技能: {skill_created}")

            # ── 合并建议 ──
            merge_suggestion = None
            if self.skill_registry:
                suggestions = self.skill_registry.suggest_merge()
                if suggestions:
                    top = suggestions[0]
                    merge_suggestion = {
                        "skill_a": top[0],
                        "skill_b": top[1],
                        "similarity": top[2],
                    }

            return AgentTeamResult(
                task_result=result,
                success=True,
                tool_calls_made=tool_count,
                execution_log=execution_log,
                skill_created=skill_created,
                merge_suggestion=merge_suggestion,
                interim_checks=interim_checks,
            )

        except Exception as e:
            print(f"[AgentTeam] 执行异常: {e}")
            return AgentTeamResult(
                task_result=f"执行失败: {e}",
                success=False,
                error=str(e),
                execution_log=execution_log,
                interim_checks=interim_checks,
            )

    # ── 异步执行 ────────────────────────────────────────────────────────────

    async def async_execute(
        self,
        user_input: str,
        token_callback=None,
        tool_callback=None,
        progress_callback=None,
        cancel_check=None,
        context_limit=None,
        attachments=None,
        original_content=None,
    ) -> AgentTeamResult:
        """异步执行任务（WebSocket 场景，避免 run_in_executor 开销）。

        逻辑与 execute() 一致，但内部调用 agent.async_agent_loop()，
        实现全链路 async 直连。
        """
        from code_demo.core.inference_engine import StreamCallbacks

        # 包装 tool_callback 以收集执行日志，并植入中间自检钩子
        interim_checks: list[dict] = []
        wrapped_callback, execution_log = self._collect_execution_log(
            tool_callback,
            interim_checks,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

        # ── 预分析：检测分析类任务，注入图谱提示 ──
        pre_hint = self._pre_analysis(user_input)
        if pre_hint:
            user_input = f"{pre_hint}\n\n{user_input}"

        try:
            try:
                callbacks = StreamCallbacks(
                    token_callback=token_callback,
                    tool_callback=wrapped_callback,
                )
                result = await self.agent.async_agent_loop(
                    user_input=user_input,
                    callbacks=callbacks,
                    context_limit=context_limit,
                    attachments=attachments,
                    original_content=original_content,
                )
            except TaskInterruptedError:
                return AgentTeamResult(
                    task_result="任务已被用户中断",
                    success=False,
                    error="任务已被用户中断",
                    execution_log=execution_log,
                    interim_checks=interim_checks,
                    interrupted=True,
                )

            if result is None or result.startswith("错误"):
                return AgentTeamResult(
                    task_result=result or "(空响应)",
                    success=False,
                    error=result,
                    execution_log=execution_log,
                    interim_checks=interim_checks,
                )

            # 统计工具调用次数
            tool_count = len(execution_log)

            # ── 自检分析 ──
            self_check = self._self_check(execution_log)
            if self_check:
                print(f"[AgentTeam] 自检: {self_check['type']} — {self_check['detail']}")

            # ── 自动技能创建 ──
            skill_created = None
            if (
                self.skill_registry
                and tool_count > AUTO_SKILL_MIN_STEPS
                and self_check
                and self_check.get("type") == "skill_candidate"
            ):
                skill_created = self.skill_registry.auto_create(
                    task_description=user_input or "",
                    execution_log=execution_log,
                    result=result[:200] if result else "",
                )
                if skill_created:
                    print(f"[AgentTeam] 自动创建技能: {skill_created}")

            # ── 合并建议 ──
            merge_suggestion = None
            if self.skill_registry:
                suggestions = self.skill_registry.suggest_merge()
                if suggestions:
                    top = suggestions[0]
                    merge_suggestion = {
                        "skill_a": top[0],
                        "skill_b": top[1],
                        "similarity": top[2],
                    }

            return AgentTeamResult(
                task_result=result,
                success=True,
                tool_calls_made=tool_count,
                execution_log=execution_log,
                skill_created=skill_created,
                merge_suggestion=merge_suggestion,
                interim_checks=interim_checks,
            )

        except Exception as e:
            print(f"[AgentTeam] 执行异常: {e}")
            return AgentTeamResult(
                task_result=f"执行失败: {e}",
                success=False,
                error=str(e),
                execution_log=execution_log,
                interim_checks=interim_checks,
            )

    # ── 预分析 ────────────────────────────────────────────────────────────

    def _pre_analysis(self, user_input: str) -> str | None:
        """快速预分析：判断任务类型，提供初始上下文。

        检测用户输入中是否包含分析类关键词，如果项目有知识图谱则注入提示。
        返回一段文本（会被追加到 user_input 前面），或 None。
        """
        if not user_input:
            return None

        from code_demo.core.tools.native.graph_tools import _find_graph_json

        # 检查图谱是否存在
        graph_exists = _find_graph_json() is not None

        # 关键词匹配判断是否需要分析
        analysis_keywords = [
            "分析", "结构", "架构", "依赖", "调用", "关系",
            "影响", "谁调用了", "在哪里", "怎么工作的", "代码结构",
            "模块", "组件", "函数", "类", "图谱",
        ]
        needs_analysis = any(kw in user_input for kw in analysis_keywords)

        if needs_analysis and graph_exists:
            return (
                "[系统提示: 项目有知识图谱可用（graphify-out/graph.json）。"
                "建议先用 graph_query 了解相关结构，再用 read_file 查看具体代码。]"
            )
        elif needs_analysis and not graph_exists:
            return (
                "[系统提示: 项目尚未构建知识图谱。"
                "建议先用 graph_update 构建图谱，再进行深入分析。]"
            )
        return None

    # ── 执行日志收集 ────────────────────────────────────────────────────

    def _collect_execution_log(
        self,
        original_callback,
        interim_checks: list[dict] | None = None,
        progress_callback=None,
        cancel_check=None,
    ):
        """包装 tool_callback，收集工具调用事件为结构化日志。

        每 SELF_CHECK_INTERVAL 步触发一次中间自检（_interim_check），
        结果追加到 interim_checks 列表（若提供）。
        同时支持 progress_callback 进度推送与 cancel_check 中断检查。

        Returns:
            (wrapped_callback, execution_log) — log 是可变列表，
            callback 执行过程中会持续追加条目。
        """
        log: list[dict] = []
        pending_calls: dict[str, dict] = {}  # tool_name → args
        step_state = {"count": 0, "last_check_at": 0}  # 用 dict 避免闭包赋值问题

        def wrapper(event_type: str, data: dict):
            name = data.get("name", "") if isinstance(data, dict) else ""
            if event_type == "tool_call":
                # 中断检查（工具执行前）
                if cancel_check and cancel_check():
                    raise TaskInterruptedError("用户中断")
                # 进度推送：开始执行
                if progress_callback:
                    progress_callback({
                        "step": step_state["count"] + 1,
                        "tool": name,
                        "status": "executing",
                    })
                pending_calls[name] = data.get("args", {})
            elif event_type == "tool_result":
                result_value = data.get("result", "")
                log.append({
                    "tool": name,
                    "args": pending_calls.pop(name, {}),
                    "result": str(result_value)[:500],
                })
                # 步数计数
                step_state["count"] += 1
                # 进度推送：执行完成
                if progress_callback:
                    progress_callback({
                        "step": step_state["count"],
                        "tool": name,
                        "status": "done",
                        "preview": str(result_value)[:100] if result_value else "",
                    })
                # 每 SELF_CHECK_INTERVAL 步触发中间自检
                steps_since_check = step_state["count"] - step_state["last_check_at"]
                if steps_since_check >= SELF_CHECK_INTERVAL:
                    step_state["last_check_at"] = step_state["count"]
                    recent_log = log[step_state["count"] - steps_since_check:]
                    try:
                        interim_result = self._interim_check(recent_log, step_state["count"])
                        if interim_result is not None:
                            if interim_checks is not None:
                                interim_checks.append(interim_result)
                            print(
                                f"[AgentTeam] 中间自检 @step{step_state['count']}: "
                                f"{interim_result['type']} — {interim_result['detail']}"
                            )
                    except Exception as exc:  # noqa: BLE001
                        # 自检失败不影响主任务
                        print(f"[AgentTeam] 中间自检异常（已忽略）: {exc}")
            # 透传给原始 callback
            if original_callback:
                original_callback(event_type, data)

        return wrapper, log

    # ── 中间自检 ──────────────────────────────────────────────────────

    def _interim_check(self, recent_log: list[dict], step_count: int) -> dict | None:
        """长任务中间自检（非阻塞，轻量）。

        仅分析最近 N 步的工具调用模式，不调用 LLM，快速完成。
        检查：
        - 是否有重复模式（可能卡住）
        - 是否连续调用相同工具（冗余）
        - 是否已达到可提取技能的特征

        Args:
            recent_log: 最近一个检查窗口的工具调用日志
            step_count: 当前总步数（用于日志标注）

        Returns:
            自检结果字典 {"type", "detail", "step"} 或 None
        """
        if not recent_log:
            return None

        tools = [step["tool"] for step in recent_log]

        # 检测连续相同工具（冗余 / 可能卡住）
        for i in range(1, len(tools)):
            if tools[i] == tools[i - 1]:
                count = 1
                for j in range(i, len(tools)):
                    if tools[j] == tools[i]:
                        count += 1
                    else:
                        break
                if count >= 3:
                    return {
                        "type": "redundant",
                        "detail": f"工具 '{tools[i]}' 连续调用 {count} 次",
                        "step": step_count,
                    }

        # 检测重复子序列（长度 3 的模式出现 ≥ 2 次 → 技能候选）
        pattern_len = 3
        if len(tools) >= pattern_len * 2:
            patterns: dict[str, int] = {}
            for i in range(len(tools) - pattern_len + 1):
                pattern = "→".join(tools[i: i + pattern_len])
                patterns[pattern] = patterns.get(pattern, 0) + 1
            for pattern, cnt in patterns.items():
                if cnt >= 2:
                    return {
                        "type": "skill_candidate",
                        "detail": f"工具序列 [{pattern}] 出现 {cnt} 次",
                        "step": step_count,
                    }

        # 检测失败重试（同一工具相邻调用且参数相同）
        for i in range(1, len(recent_log)):
            if (
                recent_log[i]["tool"] == recent_log[i - 1]["tool"]
                and recent_log[i].get("args") == recent_log[i - 1].get("args")
            ):
                return {
                    "type": "retry",
                    "detail": f"工具 '{recent_log[i]['tool']}' 使用相同参数重试",
                    "step": step_count,
                }

        return None

    # ── 自检分析 ────────────────────────────────────────────────────────

    def _self_check(self, execution_log: list[dict]) -> dict | None:
        """分析工具调用序列模式。

        检测三种模式：
        1. 重复工具序列 — 同一子序列出现 ≥ 2 次（技能候选）
        2. 连续相同工具 — 相邻调用同一工具（可能冗余）
        3. 失败重试 — 相邻两次相同工具 + 相似参数

        Args:
            execution_log: 结构化执行日志

        Returns:
            {"type": str, "detail": str} 或 None
        """
        if len(execution_log) < SELF_CHECK_INTERVAL:
            return None

        tools = [step["tool"] for step in execution_log]

        # 检测 1：连续相同工具（冗余步骤）
        for i in range(1, len(tools)):
            if tools[i] == tools[i - 1]:
                # 检查是否连续 3+ 次相同工具
                count = 1
                for j in range(i, len(tools)):
                    if tools[j] == tools[i]:
                        count += 1
                    else:
                        break
                if count >= 3:
                    return {
                        "type": "redundant",
                        "detail": f"工具 '{tools[i]}' 连续调用 {count} 次",
                    }

        # 检测 2：重复子序列（长度 3 的模式出现 ≥ 2 次）
        pattern_len = 3
        patterns: dict[str, int] = {}
        for i in range(len(tools) - pattern_len + 1):
            pattern = "→".join(tools[i : i + pattern_len])
            patterns[pattern] = patterns.get(pattern, 0) + 1

        for pattern, count in patterns.items():
            if count >= 2:
                return {
                    "type": "skill_candidate",
                    "detail": f"工具序列 [{pattern}] 出现 {count} 次",
                }

        # 检测 3：失败重试（同一工具相邻调用且参数相似）
        for i in range(1, len(execution_log)):
            if (
                execution_log[i]["tool"] == execution_log[i - 1]["tool"]
                and execution_log[i].get("args") == execution_log[i - 1].get("args")
            ):
                return {
                    "type": "retry",
                    "detail": (
                        f"工具 '{execution_log[i]['tool']}' "
                        f"使用相同参数重试"
                    ),
                }

        return None
