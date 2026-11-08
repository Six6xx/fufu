"""
core/cognitive/autonomous_executor.py — 认知引擎自主行动执行器

接收认知循环产生的 act 意图，静默调用 AgentTeam 执行，
结果持久化到 EpisodicMemory。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from ..agent_team import AgentTeam
    from ..companion_agent import CompanionAgent
    from .memory_graph import CognitiveMemory


@dataclass
class ExecutionResult:
    success: bool  # 自主行动是否成功
    summary: str          # 执行结果摘要
    full_result: str      # 完整结果
    intent: str           # 原始意图
    importance_score: float = 0.0   # 重要性评分
    importance_reason: str = ""     # 重要原因
    shareable: bool = False         # 是否值得分享给用户
    note_path: str = ""             # 笔记文件路径


class AutonomousExecutor:
    """认知引擎自主行动执行器：静默调用 AgentTeam 完成自主探索。"""

    # 安全约束 prompt 模板
    SAFETY_PROMPT = """【自主行动安全边界】
你正在执行自主只读探索，不是响应用户请求。遵守以下规则：
- 只允许读取工作目录: {workspace_root}
- 本次权限范围为 autonomous_readonly：只能阅读、查询和分析；禁止修改、创建或删除普通文件，禁止发送消息、操作桌面或执行其他有副作用的工具。
- 即使意图中出现“添加”“修复”“整理”等修改要求，也只能调查并给出建议，不得实际写入。用户明确提出的修改请求应由普通用户任务执行流程处理。
- 禁止执行危险 shell 命令
- 禁止访问用户个人目录（如 ~/Desktop, ~/Documents 等）
- 认知笔记系统已废弃，自主行动结果自动记录到情景记忆；不得清理或修改残留文件
- 完成后输出简洁摘要（100字以内）

【联网搜索能力】
需要联网搜索或获取网页内容时，使用 load_skill("browser-sandbox") 加载详细指引。
基本方法：通过 bash curl 获取网页（必须设置 --max-time 15 超时）。
- 搜索：curl -sL --max-time 15 "https://html.duckduckgo.com/html/?q=关键词"
- 获取页面：curl -sL --max-time 15 -A "Mozilla/5.0" "URL"
⚠️ 禁止下载二进制文件，禁止提交表单/注册，每次任务网络请求不超过 6 次。
⚠️ curl/requests 读网页是沙箱友好的，可直接用；但禁止拉起本地 GUI 浏览器
   （open http(s)://、open -a、启动 chrome/safari 等），那属需用户确认的 high 风险操作。

你的意图：{intent}
"""

    # 重要性评估 prompt
    IMPORTANCE_PROMPT = """基于以下自主行动的结果，评估其是否值得长期记住。

意图: {intent}
结果摘要: {result}

输出 JSON（严格遵守格式）:
"important": true/false, "score": 75.50, "reason": "一句话解释为什么重要或不重要"

评分标准：
- 100.00%: 影响核心认知/与用户关系的关键发现
- 70.00-99.99%: 有价值的知识点或模式
- 40.00-69.99%: 有趣但非必要
- 0.00-39.99%: 琐碎信息，不值得记住

注意：score 为 0.00-100.00 的浮点数，保留两位小数。"""

    # 自主行动执行超时（秒）。
    # 必须大于权限等待超时（约 120s），否则本地浏览器等 high-risk 操作
    # 弹窗确认还没等到用户响应，任务就先被超时终止，形成超时竞争。
    EXECUTION_TIMEOUT = 150.0

    # 工具名到进度描述的映射
    _TOOL_DESC_MAP = {
        "read_file": "📖 正在读取文件...",
        "read": "📖 正在读取文件...",
        "write_file": "✍️ 正在写代码...",
        "write": "✍️ 正在写代码...",
        "create_file": "✍️ 正在写代码...",
        "bash": "⚡ 正在执行命令...",
        "shell": "⚡ 正在执行命令...",
        "run_command": "⚡ 正在执行命令...",
        "search": "🔍 正在搜索...",
        "grep": "🔍 正在搜索...",
        "find": "🔍 正在搜索...",
        "graph_query": "🧠 正在分析代码结构...",
    }

    # 同类工具调用节流间隔（秒）
    _THROTTLE_INTERVAL = 3.0

    def __init__(self, agent_team: "AgentTeam", companion: "CompanionAgent", workspace_root: Path,
                 progress_callback: Optional[Callable[[str], None]] = None):
        self.agent_team = agent_team  # 执行自主任务时使用的 Agent 团队
        self.companion = companion  # 用于 LLM 调用
        self.workspace_root = workspace_root  # 自主执行允许操作的工作区根目录
        self._cancelled = False  # 当前执行是否已收到取消请求
        self._progress_cb = progress_callback  # 向调用方报告执行进度的回调
        self._last_progress_time: float = 0  # 上次发送进度通知的时间
        self._last_progress_category: str = ""  # 上次发送进度所属的类别

    async def execute(self, intent: str, thought_context: str = "") -> ExecutionResult:
        """静默执行意图"""
        self._cancelled = False

        # 1. 构建安全约束 prompt
        safe_input = self.SAFETY_PROMPT.format(
            workspace_root=self.workspace_root,
            intent=intent,
        )
        if thought_context:
            safe_input += f"\n\n背景上下文：{thought_context}"

        # 2. 自主行动使用线程局部只读范围，不切换共享权限门的全局模式。
        dispatcher = getattr(self.agent_team.agent, "dispatcher", None)

        # 推送开始通知
        if self._progress_cb:
            try:
                self._progress_cb(f"🚀 开始：{intent[:30]}")
            except Exception:
                pass

        # 3. 调用 AgentTeam（同步，需 run_in_executor）
        loop = asyncio.get_event_loop()

        # 记录执行前的历史长度，执行后标记新增消息为 internal（不显示在前端）
        session_history = getattr(
            getattr(self.agent_team.agent, 'session', None), 'history', None
        )
        history_len_before = len(session_history) if session_history is not None else -1

        # 工具调用进度回调（带节流）
        def _tool_cb(tool_name, tool_args):
            if not self._progress_cb:
                return
            now = time.time()
            # 确定工具类别描述
            desc = self._TOOL_DESC_MAP.get(tool_name, "🔧 正在处理...")
            # 节流：同类工具间隔 < 3秒跳过
            category = desc  # 用描述作为类别标识
            if (category == self._last_progress_category
                    and now - self._last_progress_time < self._THROTTLE_INTERVAL):
                return
            self._last_progress_time = now
            self._last_progress_category = category
            try:
                self._progress_cb(desc)
            except Exception:
                pass

        def _run():
            old_scope = dispatcher.set_execution_scope("autonomous_readonly") if dispatcher else None
            try:
                return self.agent_team.execute(
                    user_input=safe_input,
                    token_callback=None,  # 静默，不推送
                    tool_callback=_tool_cb if self._progress_cb else None,
                    cancel_check=lambda: self._cancelled,
                )
            finally:
                if dispatcher and old_scope is not None:
                    dispatcher.restore_execution_scope(old_scope)

        try:
            team_result = await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=self.EXECUTION_TIMEOUT,  # 大于权限等待超时(120s)，避免超时竞争
            )
        except asyncio.TimeoutError:
            return ExecutionResult(
                success=False,
                summary="执行超时",
                full_result="",
                intent=intent,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                summary=f"执行失败: {str(e)[:100]}",
                full_result="",
                intent=intent,
            )
        finally:
            # 标记自主行动期间新增的 history 消息为 internal（前端不显示）
            if session_history is not None and history_len_before >= 0:
                for msg in session_history[history_len_before:]:
                    msg["internal"] = True

        if not team_result.success:
            return ExecutionResult(
                success=False,
                summary=team_result.error or "执行失败",
                full_result=team_result.task_result or "",
                intent=intent,
            )

        summary = (team_result.task_result or "")[:200]
        full = team_result.task_result or ""

        # 3. 评估重要性
        score, reason = await self.evaluate_importance(intent, summary)
        shareable = score >= 40.0

        # 4. 保存到 EpisodicMemory
        await self.save_note(
            intent=intent,
            summary=summary,
            full=full,
            importance=score,
            shareable=shareable,
        )

        return ExecutionResult(
            success=True,
            summary=summary,
            full_result=full,
            intent=intent,
            importance_score=score,
            importance_reason=reason,
            shareable=shareable,
        )

    def cancel(self):
        """用户中断时取消"""
        self._cancelled = True

    async def evaluate_importance(self, intent: str, result: str) -> tuple[float, str]:
        """LLM 评估重要性，返回 (score, reason)"""
        prompt_text = self.IMPORTANCE_PROMPT.format(
            intent=intent,
            result=result[:500],
        )

        messages = [
            {"role": "system", "content": "你是一个记忆重要性评估器。只输出 JSON，不要其他内容。"},
            {"role": "user", "content": prompt_text},
        ]

        try:
            loop = asyncio.get_event_loop()
            client = self.companion.client
            model = self.companion.companion_model

            def _call():
                return client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=150,
                    temperature=0.3,
                    extra_body={"thinking": {"type": "disabled"}},
                )

            response = await asyncio.wait_for(
                loop.run_in_executor(None, _call),
                timeout=10.0,
            )

            if not response.choices:
                return (0.0, "评估失败")

            raw = response.choices[0].message.content.strip()
            # 提取 JSON
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not json_match:
                return (0.0, "解析失败")

            data = json.loads(json_match.group())
            score = float(data.get("score", 0))
            score = round(min(100.0, max(0.0, score)), 2)
            reason = data.get("reason", "")
            return (score, reason)

        except Exception as e:
            print(f"[AutonomousExecutor] 重要性评估失败: {e}")
            return (0.0, f"评估异常: {str(e)[:50]}")

    async def save_note(self, intent: str, summary: str, full: str, importance: int, shareable: bool) -> None:
        """自主行动结果写入 EpisodicMemory，不再创建笔记文件"""
        try:
            if hasattr(self.companion, 'cognitive_engine'):
                engine = self.companion.cognitive_engine
                if engine and hasattr(engine, 'episodic') and engine.episodic:
                    if full and full != summary:
                        content = summary + "\n" + full[:300]
                    else:
                        content = summary
                    import json as _json
                    action_topic = f"自主行动/{intent[:20]}"
                    structured_path = _json.dumps(
                        {"topic": action_topic, "query": content[:100]},
                        ensure_ascii=False
                    )
                    engine.episodic.record_event(
                        event_type="action",
                        content=content,
                        category_path=structured_path,
                        emotion="neutral",
                    )
                    return
            # fallback: 如果 episodic 不可用，仅打印日志不创建文件
            print(f"[AutonomousExecutor] episodic 不可用，跳过笔记保存: {intent[:30]}")
        except Exception as e:
            print(f"[AutonomousExecutor] 保存到 episodic 失败: {e}")
