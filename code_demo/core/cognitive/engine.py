"""
core/cognitive/engine.py — CognitiveEngine 总控

管理认知循环生命周期：限流、冷却、连续思考上限、缓冲区压缩。
由心跳循环调用 run_cycle()，引擎内部决定是否真正思考。
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..companion_agent import CompanionAgent
    from .memory_graph import CognitiveMemory
    from ..desktop.perception_manager import PerceptionManager

from .autonomous_executor import AutonomousExecutor, ExecutionResult
from .episodic_memory import EpisodicMemory
from .thought_loop import ThoughtLoop, SessionContext, ThoughtResult
from .thought_buffer import ThoughtBuffer, Thought
from ..config import AI_CONFIG_DIR, EPISODIC_DIR


class CognitiveEngine:
    """认知引擎总控：管理思维循环的生命周期。"""

    # 限制型思考参数（兜底保护）
    MAX_CONTINUOUS_ROUNDS = 5        # 连续思考最多 5 轮
    COOLDOWN_AFTER_BURST = 10.0      # 连续思考后冷却 10 秒
    IDLE_DECAY_THRESHOLDS = [        # (无交互秒数, 思考间隔秒数)
        (300, 30),    # 5 分钟后 → 30 秒一次
        (600, 60),    # 10 分钟后 → 60 秒一次
        (1200, 120),  # 20 分钟后 → 120 秒一次
        (3600, 300),  # 1 小时后 → 5 分钟一次
        (7200, 900),  # 2 小时后 → 15 分钟一次
    ]

    # 打断分级常量
    INTERRUPT_LEVELS = {
        "hard": 1.0,     # 强制停止（用户明确发消息）
        "soft": 0.6,     # 软打断（用户点击了 UI 元素）
        "hint": 0.3,     # 提示（用户可能要说话的前兆）
    }

    # 思维违规检测模式（后置反馈，不阻断）
    _VIOLATION_PATTERNS = [
        (r"(伸了个懒腰|看着窗外|闻到了?|摸了摸|走到|站起来|坐下|端起|放下杯子|打开窗|关上门)", "物理动作幻想"),
        (r"(他睡着了|他的呼吸|他皱眉|看着他的|他闭上眼)", "观察用户身体"),
        (r"(我记得我们一起|上次我们去|那天我们|还记得那次)", "编造共同经历"),
        (r"(窗外的阳光|月光洒在|雨声|风吹过|闻到.*香味)", "感官环境虚构"),
    ]

    # 情绪-思维耦合表：情绪 → (思维倾向描述, 行动倾向, 深度倾向)
    EMOTION_THINKING_BIAS = {
        "happy":     ("积极联想、分享欲强", "speak/act", "diverge"),
        "curious":   ("追问、深入、探索未知", "act/continue", "deepen"),
        "anxious":   ("反刍、确认、寻求安全感", "memorize/pause", "deepen"),
        "bored":     ("寻找新刺激、主动发起话题", "speak/act", "diverge"),
        "sad":       ("回顾、内省、寻求连接", "speak/externalize", "deepen"),
        "calm":      ("平稳观察、无急迫感", "continue/pause", "idle"),
        "neutral":   ("平稳观察、无急迫感", "continue/pause", "idle"),
        "excited":   ("快速联想、行动冲动强", "speak/act", "diverge"),
        "angry":     ("直接表达、想解决问题", "speak/act", "deepen"),
        "surprised": ("好奇、想了解更多", "continue/act", "deepen"),
    }

    def __init__(self, companion: "CompanionAgent", memory: "CognitiveMemory | None" = None):
        self.companion = companion  # 认知循环使用的伴侣对话对象
        self.memory = memory  # 长期认知记忆图谱
        self.buffer = ThoughtBuffer(full_window=7)  # 保存近期思考内容的缓冲区
        self._load_thought_summary()  # 恢复持久化的思维摘要
        self.loop = ThoughtLoop(companion=companion, buffer=self.buffer, memory=memory)  # 负责思考和动作决策的循环

        # 情景记忆
        self.episodic = EpisodicMemory(EPISODIC_DIR)  # 情景记忆存储与检索器
        self.episodic._llm_callback = self._episodic_llm_call
        self._last_decay_check: float = 0  # 上次执行记忆衰减检查的时间
        self._last_daily_merge_date: str = ""  # 上次执行每日记忆合并的日期

        # 桌面感知管理器（由外部通过 set_perception 注入）
        self._perception: "PerceptionManager | None" = None  # 采集屏幕等环境信息的组件

        # 状态跟踪
        self._continuous_count: int = 0       # 连续思考计数
        self._last_thought_time: float = 0    # 上次思考时间
        self._cooldown_until: float = 0       # 冷却截止时间
        self._externalize_enabled: bool = False  # 外显开关（用户控制）
        self._running: bool = False  # 认知循环是否正在运行
        # 自主行动
        self._autonomous_progress_cb: Optional[callable] = None  # 由 server.py 注入
        self._autonomous_executor: Optional[AutonomousExecutor] = None  # 执行认知循环自主行动的执行器
        self._autonomous_running: bool = False  # 自主行动任务是否正在运行
        self._autonomous_task: Optional[asyncio.Task] = None  # 当前自主行动的 asyncio 任务
        self._share_queue: list[dict] = []  # 待分享的行动结果
        self._last_act_time: float = 0     # 上次 act 时间（冷却用）
        self._act_cooldown_seconds: float = 5  # act 冷却时间（秒）
        self._user_interrupt_level: str = ""  # 当前打断级别
        self._consecutive_pauses: int = 0  # 连续暂停次数，用于调节自主思考
        self._pause_hibernating: bool = False  # 4次+ pause 后进入休眠
        self._last_cleanup_check: float = 0   # 上次笔记清理检查时间
        # cooldown 反馈 + 重复意图检测
        self._last_rejected_intent: str = ""        # 上次被冷却拒绝的 act intent
        self._cooldown_remaining: float = 0         # 冷却剩余秒数
        self._repeated_intent_count: int = 0        # 相同 intent 连续出现次数
        # 执行结果强信号（用于回流到下一轮 ThoughtLoop）
        self._completed_intents: list[dict] = []  # 最近已完成的 act 意图列表
        # 每条结构: {"intent": str, "summary": str, "time": float}
        # 焦点主题追踪
        self._focus_topic: str = ""           # 当前焦点主题文本
        self._focus_source: str = ""          # 焦点来源描述
        self._focus_rounds: int = 0           # 围绕该焦点的思考轮数
        self._last_user_input: str = ""       # 上次用户输入（用于检测焦点变化）
        # 思维违规后置反馈
        self._next_feedback: str = ""         # 下一轮注入的修正信号
        # 联想种子追踪
        self._last_hour: int = -1         # 上次记录的小时（用于整点触发）
        self._last_idle: float = 0        # 上次 idle 时长（用于检测用户回来）
        self._last_screen: str = ""       # 上次屏幕内容（用于检测变化）
        # 话题锁定机制（思维闭环）
        self._paused_topic: str = ""              # 被锁定的话题（归一化思维指纹）
        self._pause_reason: str = ""              # pause 原因（"thought_repetition"）
        self._topic_pause_count: int = 0          # 同话题累计 pause 次数
        self._topic_locked_until: float = 0.0     # 话题锁定解除时间戳
        self._last_diversification_seed: str = ""  # 上一轮注入的多样化种子（去重用）

    def init_executor(self, agent_team, workspace_root: Path):
        """延迟初始化自主行动执行器（需要 AgentTeam 实例）"""
        self._autonomous_executor = AutonomousExecutor(
            agent_team=agent_team,
            companion=self.companion,
            workspace_root=workspace_root,
            progress_callback=self._autonomous_progress_cb,
        )

    def set_perception(self, perception: "PerceptionManager") -> None:
        """注入桌面感知管理器引用（由 Agent 或 CompanionAgent 调用）。"""
        self._perception = perception

    def set_autonomous_progress_cb(self, cb) -> None:
        """注入自主执行进度回调（由 server.py 在 WebSocket 连接建立后调用）。"""
        self._autonomous_progress_cb = cb
        # 如果 executor 已经创建，同步更新
        if self._autonomous_executor:
            self._autonomous_executor._progress_cb = cb

    async def _episodic_llm_call(self, prompt: str) -> str:
        """EpisodicMemory 的 LLM 回调，用于生成骨架摘要和分类。"""
        try:
            client = self.companion.client
            if client is None:
                return ""
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=self.loop.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=200,
                    temperature=0.3,
                )
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"EpisodicMemory LLM 回调失败: {e}")
            return ""

    async def record_episodic_event(self, event_type: str, content: str,
                                    category_path: str = "", emotion: str = "neutral"):
        """外部调用接口：记录情景事件（结构化 category_path）"""
        try:
            if category_path:
                # 已有标签 → 直接作为 topic
                topic = category_path
            else:
                # 无标签 → LLM 分类
                topic = await self._classify_topic(content)
            structured_path = _json.dumps(
                {"topic": topic, "query": content[:100]}, ensure_ascii=False
            )
            self.episodic.record_event(event_type, content, structured_path, emotion)
        except Exception:
            pass

    async def _classify_topic(self, text: str) -> str:
        """用 LLM 对用户输入做话题分类，返回分类标签。"""
        if not text or not text.strip():
            return "日常/其他"

        prompt = (
            "请将以下用户输入分类到一个话题标签中，只返回标签本身，不要解释。\n"
            "可选分类：日常/闲聊、日常/问候、技术/编程、技术/工具、学术/数学、学术/科学、"
            "创作/写作、创作/设计、生活/天气、生活/饮食、娱乐/游戏、娱乐/音乐、工作/任务、工作/规划、其他\n\n"
            f"用户输入：{text[:200]}\n\n分类标签："
        )

        result = await self._episodic_llm_call(prompt)
        result = result.strip().strip('"').strip("'")

        # 如果 LLM 返回不在预设列表中，归入"其他"
        valid_prefixes = ("日常", "技术", "学术", "创作", "生活", "娱乐", "工作", "其他")
        if not result or not any(result.startswith(p) for p in valid_prefixes):
            return "日常/其他"
        return result

    @property
    def externalize_enabled(self) -> bool:
        return self._externalize_enabled

    @externalize_enabled.setter
    def externalize_enabled(self, value: bool):
        self._externalize_enabled = value

    # ────────────────────────────────────────────────────
    # 打断分级
    # ────────────────────────────────────────────────────

    def handle_interrupt(self, level: str = "hard"):
        """处理用户打断，分级响应。

        Args:
            level: "hard" | "soft" | "hint"
        """
        strength = self.INTERRUPT_LEVELS.get(level, 1.0)

        if strength >= 0.8:
            # 强打断：立即停止，清空连续计数
            self._continuous_count = 0
            self._consecutive_pauses = 0
            self._pause_hibernating = False
            self._user_interrupt_level = "hard"
            self._reset_topic_lock()  # 用户新消息，重置话题锁定
            # 中断自主行动
            if self._autonomous_running and self._autonomous_executor:
                self._autonomous_executor.cancel()
                self._autonomous_running = False
        elif strength >= 0.5:
            # 软打断：允许当前思考完成，但不继续
            self._user_interrupt_level = "soft"
        else:
            # 提示：仅标记，AI 可选择忽略
            self._user_interrupt_level = "hint"

    def _should_resist_interrupt(self) -> bool:
        """AI 是否应该抵抗当前打断（基于性格+当前思考状态）。

        只对 soft/hint 级别有效，hard 永远不抵抗。
        """
        if self._user_interrupt_level == "hard":
            return False

        # 条件：正在深入思考 + 连续计数 < 3（刚开始想）
        if self._continuous_count > 0 and self._continuous_count < 3:
            return True  # "等等，我还在想..."

        return False

    def _is_companion_vlm(self) -> bool:
        """委托给 CompanionAgent 判断当前模型是否为 VLM"""
        return self.companion._is_companion_vlm()

    # ────────────────────────────────────────────────────
    # 核心循环
    # ────────────────────────────────────────────────────

    async def run_cycle(self, ctx: SessionContext) -> Optional[ThoughtResult]:
        """执行单次认知循环（由心跳调用）。

        内置限流逻辑：
        1. 冷却期内不思考
        2. 根据 idle 时长动态调整思考间隔
        3. 连续 5 轮后强制冷却
        4. 打断分级：hard 跳过 / soft 可抵抗 / hint 可忽略

        Returns:
            ThoughtResult 或 None（不满足思考条件时）
        """
        now = time.time()

        # 清理过期的已完成意图（10分钟过期）
        self._completed_intents = [
            c for c in self._completed_intents
            if now - c.get("time", 0) < 600
        ]

        # 自主行动进行中时跳过普通思考
        if self._autonomous_running:
            return None

        # 检查打断
        if ctx.user_interrupted:
            self.handle_interrupt("hard")  # 用户发消息 = 强打断

        # 休眠状态：连续 pause 过多，等待用户唤醒或超时唤醒
        if self._pause_hibernating:
            # 根据 idle 时长动态计算休眠唤醒间隔
            idle_seconds = ctx.idle_seconds
            hibernate_interval = self._calc_hibernate_interval(idle_seconds)
            if now - self._last_thought_time < hibernate_interval:
                return None
            else:
                self._pause_hibernating = False  # 超时唤醒
                # 休眠唤醒时清理 Trash
                try:
                    self.episodic.cleanup_trash()
                    self.episodic.save()
                except Exception:
                    pass
                # 唤醒时检查是否需要清理笔记（每天24小时最多一次）
                if now - self._last_cleanup_check > 86400:
                    self._last_cleanup_check = now
                    self._pending_cleanup_hint = True  # 标记待向用户提示清理记忆

        # 如果是强打断且不抵抗，直接跳过
        if self._user_interrupt_level == "hard" and not self._should_resist_interrupt():
            self._user_interrupt_level = ""
            return None

        # 如果 AI 选择抵抗软打断，覆盖打断信号让 AI 继续
        if self._user_interrupt_level and self._should_resist_interrupt():
            ctx.user_interrupted = False

        # 检查冷却
        if now < self._cooldown_until:
            self._user_interrupt_level = ""
            return None

        # 根据 idle 时长计算最小间隔
        min_interval = self._calc_min_interval(ctx.idle_seconds)
        if now - self._last_thought_time < min_interval:
            self._user_interrupt_level = ""
            return None

        # 注入 cooldown 反馈到上下文
        if now - self._last_act_time < self._act_cooldown_seconds and self._last_rejected_intent:
            ctx.act_cooldown_remaining = self._act_cooldown_seconds - (now - self._last_act_time)
            ctx.last_rejected_intent = self._last_rejected_intent
        else:
            ctx.act_cooldown_remaining = 0
            ctx.last_rejected_intent = ""

        # 注入已完成意图强信号（仅冷却期内注入，避免旧行动持续抑制新行动）
        in_cooldown = now - self._last_act_time < self._act_cooldown_seconds
        if in_cooldown and self._completed_intents:
            # 过滤最近 2 分钟内的已完成意图
            recent_completed = [
                c for c in self._completed_intents
                if now - c["time"] < 120  # 2 分钟内有效
            ]
            if recent_completed:
                lines = [f"  ✓ [{c['intent'][:40]}] → {c['summary'][:60]}" for c in recent_completed]
                ctx.task_result = "以下意图刚执行完成（冷却期内）：\n" + "\n".join(lines)
            else:
                ctx.task_result = ""
        else:
            # 冷却期外：清空，不再用已完成的旧行动抑制新行动
            ctx.task_result = ""

        # 注入桌面感知（根据模型类型选择路径）
        if self._perception:
            if self._is_companion_vlm():
                # VLM 路径：截图 base64 直传给伴侣AI
                ctx.screen_image_b64 = await self._perception.get_latest_screenshot_b64()
                ctx.screen_context = ""
            elif self._perception.is_watching:
                # LLM 路径：保持原有文本摘要
                ctx.screen_context = self._perception.get_screen_summary()
                ctx.screen_image_b64 = None
            else:
                ctx.screen_context = ""
                ctx.screen_image_b64 = None
        else:
            ctx.screen_context = ""
            ctx.screen_image_b64 = None

        # 焦点提取（程序化）
        ctx.focus_topic = self._extract_focus_topic(ctx)

        # 情绪叙事（取代标签式注入，让情绪成为思维驱动力）
        ctx.emotion_narrative = self._build_emotion_narrative(ctx.current_emotion, ctx.emotion_intensity)

        # 注入违规反馈信号（上一轮检测到的违规，温和纠正）
        if self._next_feedback:
            ctx.violation_feedback = self._next_feedback
            self._next_feedback = ""  # 消费后清除
        else:
            ctx.violation_feedback = ""

        # 注入话题锁定反馈（通过 violation_feedback 追加，借用已有通道）
        if self._pause_reason == "thought_repetition" and self._paused_topic:
            topic_lock_msg = ""
            if self._topic_pause_count >= 4:
                topic_lock_msg = (
                    f"【系统】话题「{self._paused_topic[:15]}」已被锁定。"
                    "你必须转向其他方向，或选择 speak 开口。禁止继续想这个话题。"
                )
            elif self._topic_pause_count == 3:
                topic_lock_msg = (
                    f"【系统】你已围绕「{self._paused_topic[:15]}」思考 3 轮未果。"
                    "请做出决策：speak 向用户说出困惑，或 memorize 记录当前结论后放下。"
                )
            elif self._topic_pause_count >= 1:
                topic_lock_msg = (
                    f"【系统】你上次因重复思考「{self._paused_topic[:20]}」被 pause 了"
                    f"（累计 {self._topic_pause_count} 次）。"
                    f"请不要继续想这个话题，转向其他方向。"
                )

            # 锁定期内每轮注入一条多样化种子（候选层去重，连续两轮不同）
            if topic_lock_msg and self._topic_locked_until > time.time():
                seed = self._get_diversification_seed()
                if seed:
                    self._last_diversification_seed = seed
                    topic_lock_msg += f"\n【提示】试试想想别的：{seed}"

            if topic_lock_msg:
                if ctx.violation_feedback:
                    ctx.violation_feedback += "\n" + topic_lock_msg
                else:
                    ctx.violation_feedback = topic_lock_msg

        # 联想种子 + 环境触发
        association_seed = await self._get_association_seed(ctx.idle_seconds)
        env_trigger = self._get_environment_trigger(ctx)
        ctx.association_context = ""
        if env_trigger:
            ctx.association_context += env_trigger + "\n"
        if association_seed:
            ctx.association_context += association_seed

        # --- 情景记忆衰减检查 ---
        if now - self._last_decay_check > 300:  # 5分钟间隔
            self._last_decay_check = now
            try:
                await self.episodic.decay_hot_events()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"decay_hot_events 异常: {e}")

        # --- 每日跨日期合并检查 ---
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_daily_merge_date:
            self._last_daily_merge_date = today
            asyncio.ensure_future(self._daily_merge_check())

        # 话题锁定检查（附加分支）：如果当前焦点仍在被锁话题范围内且锁定未到期 → 强制清除焦点
        if (self._topic_locked_until > time.time()
            and self._focus_topic
            and self._is_similar_text(self._normalize_thought_text(self._focus_topic), self._paused_topic)):
            self._focus_topic = ""  # 清除焦点，触发自由联想

        # 执行思考
        result = await self.loop.run_cycle(ctx)
        if not result:
            self._user_interrupt_level = ""
            return None

        # 记录思维事件到情景记忆
        if result.text:
            try:
                topic = self._focus_topic or "日常互动/思考"
                structured_path = _json.dumps(
                    {"topic": topic, "query": result.text[:100]}, ensure_ascii=False
                )
                self.episodic.record_event(
                    event_type="thought",
                    content=result.text,
                    category_path=structured_path,
                    emotion=ctx.current_emotion,
                )
            except Exception:
                pass

        # 思维重复检测（覆盖 continue/pause 全部路径）：归一化指纹比较，命中直接视为 pause
        repetition_hit = False
        if result.thought and result.thought.content:
            if self._is_repetitive_thought(result.thought.content):
                repetition_hit = True
                topic_fingerprint = self._normalize_thought_text(result.thought.content)

                # 判断是否与上次锁定的同一话题（归一化后比较，阈值 0.7）
                if self._paused_topic and self._is_similar_text(
                        topic_fingerprint, self._paused_topic, threshold=0.7):
                    self._topic_pause_count += 1
                else:
                    self._paused_topic = topic_fingerprint
                    self._topic_pause_count = 1

                self._pause_reason = "thought_repetition"
                # 锁定时间随重复次数递增：5min → 10min → 15min
                lock_minutes = min(self._topic_pause_count * 5, 15)
                self._topic_locked_until = time.time() + lock_minutes * 60

                print(f"[CognitiveEngine] 检测到重复思维，强制 pause（话题累计 {self._topic_pause_count} 次）")
                result.thought.next_action = "pause"
                result.action = "pause"
                result.should_continue = False
                self._consecutive_pauses += 1

        # 话题锁定违规检测（锁"思维指纹"）：锁定期内本轮思维仍与被锁话题相似 → 升级注入等级
        if (not repetition_hit
                and result.thought and result.thought.content
                and self._paused_topic
                and self._topic_locked_until > time.time()):
            fp = self._normalize_thought_text(result.thought.content)
            if self._is_similar_text(fp, self._paused_topic, threshold=0.7):
                self._topic_pause_count += 1
                # 锁定时间按既有 5/10/15 分钟规则续期
                lock_minutes = min(self._topic_pause_count * 5, 15)
                self._topic_locked_until = time.time() + lock_minutes * 60
                print(f"[CognitiveEngine] 锁定期内思维仍触及被锁话题（累计 {self._topic_pause_count} 次），注入等级升级")

        # 更新状态
        self._last_thought_time = now

        # 后置违规检测（不阻断当前输出，仅设置下一轮修正信号）
        if result.text:
            self._check_thought_violations(result.text)

        if result.should_continue:
            self._continuous_count += 1
            # 连续思考达到上限 → 强制冷却
            if self._continuous_count >= self.MAX_CONTINUOUS_ROUNDS:
                self._cooldown_until = now + self.COOLDOWN_AFTER_BURST
                self._continuous_count = 0
                # 将 continue 覆盖为 pause（防止无限循环）
                result.thought.next_action = "pause"
                result.action = "pause"
                result.should_continue = False
        else:
            self._continuous_count = 0

        # pause 信号递增休眠
        if result.action == "pause":
            self._consecutive_pauses += 1
            if self._consecutive_pauses >= 4:
                self._pause_hibernating = True
        else:
            self._consecutive_pauses = 0

        # act 动作：启动自主行动
        if result.action == "act" and result.thought.intent:
            # 冷却检查
            if now - self._last_act_time < self._act_cooldown_seconds:
                remaining = self._act_cooldown_seconds - (now - self._last_act_time)
                intent_prefix = result.thought.intent[:20]

                # 重复意图检测：前20字匹配
                if self._last_rejected_intent[:20] == intent_prefix:
                    self._repeated_intent_count += 1
                else:
                    self._repeated_intent_count = 1

                self._last_rejected_intent = result.thought.intent
                self._cooldown_remaining = remaining

                print(f"[Cognitive] act 冷却中，跳过: {result.thought.intent[:30]} (剩余{remaining:.0f}s, 重复第{self._repeated_intent_count}次)")

                # 兜底：相同意图连续出现3次，强制休眠到 cooldown 结束
                if self._repeated_intent_count >= 3:
                    self._repeated_intent_count = 0
                    self._cooldown_until = now + remaining
                    print(f"[Cognitive] 重复意图3次，强制休眠 {remaining:.0f}s 等待冷却结束")
            elif self._autonomous_executor and not self._autonomous_running:
                self._last_act_time = now
                self._autonomous_running = True
                self._last_rejected_intent = ""  # 清除拒绝记录
                self._repeated_intent_count = 0
                thought_ctx = self.buffer.get_context_for_next_thought()
                self._autonomous_task = asyncio.create_task(
                    self._run_autonomous(result.thought.intent, thought_ctx)
                )
        # 定时清理提示：休眠唤醒后注入清理意图
        elif getattr(self, '_pending_cleanup_hint', False) and result.action != "act":
            self._pending_cleanup_hint = False  # 待处理的记忆清理提示标记
            if self._autonomous_executor and not self._autonomous_running:
                if now - self._last_act_time >= 600:
                    self._last_act_time = now
                    self._autonomous_running = True
                    thought_ctx = "定时维护任务：检查并清理过期笔记"
                    self._autonomous_task = asyncio.create_task(
                        self._run_autonomous(
                            "检查过期笔记，用 list_expired_notes 查看超过3天的笔记，自行判断哪些不再需要后用 delete_notes 清理",
                            thought_ctx,
                        )
                    )

        # speak 动作成功：重置话题锁定（AI已主动开口，打破循环）
        if result.action == "speak":
            self._reset_topic_lock()

        # 思考完成后清除打断状态
        self._user_interrupt_level = ""
        return result

    # ────────────────────────────────────────────────────
    # 情景记忆维护
    # ────────────────────────────────────────────────────

    async def _daily_merge_check(self):
        """每日首次活跃时检查跨日期骨架合并"""
        try:
            for cat_path in list(self.episodic.skeleton_events.keys()):
                await self.episodic.merge_cross_date_skeletons(cat_path)
            self.episodic.save()
        except Exception:
            pass

    # ────────────────────────────────────────────────────
    # 联想种子
    # ────────────────────────────────────────────────────

    async def _get_association_seed(self, idle_seconds: float = 0.0) -> str:
        """随机从记忆中抽取一条作为联想种子。

        触发率状态驱动：idle 且无焦点时 60%，其余情况 20%。
        返回空字符串表示本轮不触发联想。
        """
        import random

        no_focus = not self._focus_topic or self._focus_topic.strip().lower() == "idle"
        is_idle = idle_seconds >= self.IDLE_DECAY_THRESHOLDS[0][0]  # 进入 idle 衰减区间

        # idle 且无焦点 → 提升触发率到 0.6；否则保持 0.2
        trigger_prob = 0.6 if (is_idle and no_focus) else 0.2
        if random.random() > trigger_prob:
            return ""

        if not self.memory:
            return ""

        # 焦点深入时跳过（仅当焦点存在且连续 deepen 3轮+时不打扰）
        if self._focus_topic and self._focus_rounds > 3:
            return ""

        try:
            # 调用 memory_graph 的 random_recall
            random_memory = self.memory.random_recall(exclude_topic=self._focus_topic)
            if random_memory:
                return (
                    f"【联想种子】突然想起：{random_memory}\n"
                    f"（这是一个随机浮现的记忆片段。你可以跟着它联想、跟当前焦点关联、或忽略它继续当前思路）"
                )
        except Exception:
            pass

        return ""

    def _get_environment_trigger(self, ctx: "SessionContext") -> str:
        """检测环境变化，生成联想触发文本。

        触发条件：时间整点变化、用户从长时间离开回来、屏幕内容变化。
        返回空字符串表示无触发。
        """
        triggers = []

        # 时间整点触发
        if ctx.hour != self._last_hour and self._last_hour >= 0:
            triggers.append(f"现在{ctx.hour}点了")
        self._last_hour = ctx.hour

        # 长时间 idle 后用户回来
        if ctx.idle_seconds < 5 and self._last_idle > 300:
            triggers.append("用户刚回来")
        self._last_idle = ctx.idle_seconds

        # 屏幕内容变化
        if ctx.screen_context and ctx.screen_context != self._last_screen:
            triggers.append("屏幕内容发生了变化")
        if ctx.screen_context:
            self._last_screen = ctx.screen_context

        if triggers:
            return f"【环境变化】{'，'.join(triggers)}"
        return ""

    # ────────────────────────────────────────────────────
    # 情绪-思维耦合
    # ────────────────────────────────────────────────────

    def _build_emotion_narrative(self, emotion: str, intensity: float) -> str:
        """将情绪转化为思维驱动力的叙事描述。

        不再是 'emotion: happy 0.7' 这样的标签，
        而是能让 LLM 感受到情绪推力的自然语言。
        """
        bias = self.EMOTION_THINKING_BIAS.get(emotion, ("平稳", "continue/pause", "idle"))
        thinking_tendency = bias[0]
        action_tendency = bias[1]

        if intensity > 0.7:
            return (
                f"你现在的{emotion}感觉很强烈——{thinking_tendency}。"
                f"这种情绪让你自然倾向于{action_tendency}。"
            )
        elif intensity > 0.4:
            return f"你有些{emotion}——{thinking_tendency}。"
        else:
            return f"情绪平稳，略带{emotion}。"

    # ────────────────────────────────────────────────────
    # 思维违规后置检测
    # ────────────────────────────────────────────────────

    def _check_thought_violations(self, thought_text: str) -> None:
        """检查思维文本中的违规内容，设置下一轮修正信号。

        纯正则匹配，不做 LLM 调用，延迟 < 1ms。
        不丢弃当前想法，只在下一轮注入温和纠正。
        """
        import re

        for pattern, violation_type in self._VIOLATION_PATTERNS:
            if re.search(pattern, thought_text):
                self._next_feedback = (
                    f"【系统】上一个想法中出现了{violation_type}，"
                    f"这不是你的真实体验。回到当前焦点，想想你真正感兴趣的事。"
                )
                return  # 只报告第一个违规

    # ────────────────────────────────────────────────────
    # 焦点主题提取
    # ────────────────────────────────────────────────────

    def _extract_focus_topic(self, ctx: SessionContext) -> str:
        """程序化提取当前焦点主题，格式化后返回供 prompt 使用。

        逻辑：
        1. 用户有新输入 → 更新焦点为用户输入的核心内容
        2. 连续思考中上一轮有明确意图 → 继承为焦点
        3. idle 状态无新输入 → 保持上一个焦点，轮数+1
        """
        # 检测用户是否有新输入
        current_input = ctx.last_user_input.strip()

        if current_input and current_input != self._last_user_input:
            # 用户有新输入，更新焦点
            self._last_user_input = current_input
            # 提取核心内容（取前50字，去掉常见虚词）
            topic = self._extract_keywords(current_input)
            if topic:
                self._focus_topic = topic
                self._focus_source = "刚才的对话"
                self._focus_rounds = 0

        # 如果没有焦点，尝试从最近对话历史中提取
        if not self._focus_topic and ctx.companion_history:
            for msg in reversed(ctx.companion_history[-6:]):
                if msg.get("role") == "user":
                    content = msg.get("content", "").strip()
                    if content:
                        topic = self._extract_keywords(content)
                        if topic:
                            self._focus_topic = topic
                            self._focus_source = "之前的对话"
                            self._focus_rounds = 0
                        break

        # 递增焦点轮数
        self._focus_rounds += 1

        # 焦点衰减：超过 10 轮无更新，清除焦点
        if self._focus_rounds > 10 and not current_input:
            self._focus_topic = ""
            self._focus_rounds = 0

        # 格式化输出
        if self._focus_topic:
            return f"主题：{self._focus_topic} | 来源：{self._focus_source} | 已围绕此主题思考：{self._focus_rounds}轮"
        else:
            return "（暂无明确焦点，自由思考）"

    def _extract_keywords(self, text: str) -> str:
        """从文本中提取关键词/核心短语（纯规则，不依赖 LLM）"""
        import re

        # 去掉常见前缀虚词
        text = text[:80]  # 限制长度
        prefixes_to_strip = [
            "你好", "嗯", "啊", "哦", "好的", "那个", "就是",
            "我想问", "我想知道", "帮我看看", "可以",
        ]
        stripped = text
        for prefix in prefixes_to_strip:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].lstrip("，,。. ")
                break

        # 如果处理后为空，用原文
        if not stripped:
            stripped = text

        # 取核心部分（第一个标点前，或前30字）
        # 分割标点
        parts = re.split(r'[，。？！,.?!\n]', stripped)
        core = parts[0].strip() if parts else stripped

        # 限制长度
        if len(core) > 30:
            core = core[:30]

        return core if core else ""

    # ────────────────────────────────────────────────────
    # 自主行动
    # ────────────────────────────────────────────────────

    async def _run_autonomous(self, intent: str, thought_context: str):
        """执行自主行动并处理结果"""
        try:
            print(f"[Autonomous] 开始执行: {intent[:50]}")
            exec_result = await self._autonomous_executor.execute(intent, thought_context)

            if not exec_result.success:
                print(f"[Autonomous] 执行失败: {exec_result.summary[:50]}")
                return

            print(f"[Autonomous] 完成: {exec_result.summary[:50]}, 重要性={exec_result.importance_score:.2f}%")

            # 强信号：缓存已完成意图，下一轮 ThoughtLoop 可见
            self._completed_intents.append({
                "intent": intent,
                "summary": exec_result.summary[:150],
                "time": time.time(),
            })
            # 只保留最近 5 条
            if len(self._completed_intents) > 5:
                self._completed_intents = self._completed_intents[-5:]

            # 重要发现写入长期记忆
            if exec_result.importance_score >= 60.0 and self.memory:
                memory_content = (
                    f"{exec_result.summary}\n\n"
                    f"重要性: {exec_result.importance_score:.2f}%\n"
                    f"原因: {exec_result.importance_reason}"
                )
                await self.memory.memorize(
                    content=memory_content,
                    cluster_tag="",  # LLM 自动分配
                    source="autonomous_action",
                    confidence=exec_result.importance_score / 100.0,
                )
                print(f"[Autonomous] 已写入记忆: {intent[:30]}")

            # 可分享的结果加入分享队列
            if exec_result.shareable:
                self._share_queue.append({
                    "intent": intent,
                    "summary": exec_result.summary,
                    "score": exec_result.importance_score,
                    "time": time.time(),
                })
        except Exception as e:
            print(f"[Autonomous] 异常: {e}")
        finally:
            self._autonomous_running = False
            self._autonomous_task = None

    def get_shareable_topic(self) -> Optional[dict]:
        """获取一个待分享话题（由心跳循环在合适时机调用）。

        Returns:
            话题 dict 或 None
        """
        if not self._share_queue:
            return None
        # FIFO：取最早的
        return self._share_queue.pop(0)

    def _calc_min_interval(self, idle_seconds: float) -> float:
        """根据用户空闲时长和连续pause次数计算最小思考间隔"""
        # 基础间隔（根据idle时长）
        base_interval = 10.0
        for threshold, interval in self.IDLE_DECAY_THRESHOLDS:
            if idle_seconds >= threshold:
                base_interval = interval
                break

        # 连续 pause 递增因子
        if self._consecutive_pauses >= 3:
            return base_interval * 4  # 40s
        elif self._consecutive_pauses >= 2:
            return base_interval * 2  # 20s

        return base_interval

    def _calc_hibernate_interval(self, idle_seconds: float) -> float:
        """休眠后的唤醒间隔，随idle时长递增"""
        if idle_seconds > 7200:    # 2小时+
            return 1800  # 30分钟
        elif idle_seconds > 3600:  # 1小时+
            return 900   # 15分钟
        elif idle_seconds > 1200:  # 20分钟+
            return 600   # 10分钟
        else:
            return 300   # 5分钟（保持原有）

    def _normalize_thought_text(self, text: str) -> str:
        """归一化思维文本为指纹：剔除数字/时间表达差异后取前36字。

        例："已经 idle 39 分钟了" 与 "已经 idle 一个多小时了" 归一化后指纹一致，
        避免数字/时间词差异稀释字符相似度。
        """
        import re

        if not text:
            return ""
        t = text
        # 中文数字+时间单位表达（如"三十九分钟""一个多小时""几秒钟"）
        t = re.sub(
            r"[零一二两三四五六七八九十百千几半0-9]+\s*个?\s*多?\s*"
            r"(分钟|秒钟|秒|小时|钟头|天|周|月|年)",
            "", t)
        # 剩余阿拉伯数字（含小数）
        t = re.sub(r"\d+(\.\d+)?", "", t)
        # 压缩空白
        t = re.sub(r"\s+", "", t)
        return t[:36]

    def _is_repetitive_thought(self, text: str) -> bool:
        """检测当前思维是否与最近思维高度重复（归一化指纹比较，覆盖 continue/pause 路径）"""
        if not hasattr(self, '_recent_thought_texts'):
            self._recent_thought_texts = []  # 用于避免重复输出的近期思考内容

        # 归一化指纹（剔除数字/时间词差异）
        fingerprint = self._normalize_thought_text(text)

        # 检查与最近3条是否相似（比较时同样归一化）
        similar_count = sum(
            1 for prev in self._recent_thought_texts[-3:]
            if self._text_similarity(fingerprint, self._normalize_thought_text(prev)) > 0.7
        )

        # 维护队列（存原文，比较时按需归一化）
        self._recent_thought_texts.append(text)
        if len(self._recent_thought_texts) > 10:
            self._recent_thought_texts.pop(0)

        return similar_count >= 2

    def _text_similarity(self, a: str, b: str) -> float:
        """简单字符级相似度"""
        if not a or not b:
            return 0.0
        common = sum(1 for ca, cb in zip(a, b) if ca == cb)
        return common / max(len(a), len(b))

    def _is_similar_text(self, a: str, b: str, threshold: float = 0.6) -> bool:
        """简单字符相似度比较（用于话题锁定判断）"""
        if not a or not b:
            return False
        common = sum(1 for ca, cb in zip(a, b) if ca == cb)
        return common / max(len(a), len(b)) > threshold

    def _reset_topic_lock(self):
        """用户新输入或speak成功后重置话题锁定"""
        self._paused_topic = ""
        self._pause_reason = ""
        self._topic_pause_count = 0
        self._topic_locked_until = 0.0
        self._last_diversification_seed = ""

    def _get_diversification_seed(self) -> str:
        """获取话题多样化种子，引导AI转向新方向（排除上一轮已给过的种子）"""
        import random

        # 优先级1：从 EpisodicMemory 的 skeleton 骨架记忆中取一条非锁定话题
        # skeleton_events: dict[category_path -> EpisodicEvent]
        if hasattr(self, 'episodic') and self.episodic:
            try:
                candidates = []
                for cat_path, event in self.episodic.skeleton_events.items():
                    if self._is_similar_text(cat_path[:30], self._paused_topic):
                        continue  # 跳过与被锁话题相似的分类
                    seed_text = (event.summary or "").strip() or cat_path
                    if seed_text != self._last_diversification_seed:
                        candidates.append(seed_text)
                if candidates:
                    return random.choice(candidates[-5:])  # 最近的5个中随机取
            except Exception as e:
                print(f"[CognitiveEngine] 骨架层多样化种子获取失败: {e}")

        # 优先级2：预设兴趣方向（排除上一轮种子，保证连续两轮不重复）
        seeds = [
            "最近学到的新知识",
            "之前和用户讨论过的有趣话题",
            "观察一下当前环境",
            "回顾一下近期完成的事情",
        ]
        candidates = [s for s in seeds if s != self._last_diversification_seed] or seeds
        return random.choice(candidates)

    async def compress_buffer_if_needed(self):
        """如果缓冲区有待压缩内容，调用 LLM 压缩。

        由心跳循环在空闲时调用（低优先级）。
        """
        if not self.buffer.has_pending_compression():
            return

        pending = self.buffer.get_pending_for_compression()
        if not pending:
            return

        # 构建压缩 prompt
        thoughts_text = "\n".join(f"- {t.content}" for t in pending)
        existing_summary = self.buffer.summary or "(无)"

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个思维压缩器。将多个连续想法压缩成一段叙事摘要（80-120字）。\n\n"
                    "要求：\n"
                    "- 保留因果链（为什么从A想到了B）\n"
                    "- 保留关键结论和未解决的疑问\n"
                    "- 保留情绪走向（从犹豫到确定？从好奇到放弃？）\n"
                    "- 用第一人称叙事\n\n"
                    "示例输出：\n"
                    "\"用户提到对某个主题感兴趣，我想帮对方找些资料。但犹豫了一下——不确定方向对不对。最后还是决定先找再说。\""
                ),
            },
            {
                "role": "user",
                "content": (
                    f"已有摘要：{existing_summary}\n\n"
                    f"新增想法：\n{thoughts_text}\n\n"
                    "输出更新后的叙事摘要（80-120字，第一人称）："
                ),
            },
        ]

        try:
            client = self.companion.client
            loop = asyncio.get_event_loop()

            def _run():
                return client.chat.completions.create(
                    model=self.loop.model,
                    messages=messages,
                    max_tokens=200,
                    temperature=0.3,
                    extra_body={"thinking": {"type": "disabled"}},
                )

            response = await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=5.0,
            )
            if response.choices:
                new_summary = response.choices[0].message.content.strip()
                self.buffer.update_summary(new_summary)
                self._save_thought_summary()
        except Exception as e:
            print(f"[CognitiveEngine] Buffer compression failed: {e}")

    # ── ThoughtBuffer summary 持久化 ──

    _COGNITIVE_STATE_DIR = AI_CONFIG_DIR / ".cognitive_state"
    _THOUGHT_SUMMARY_FILE = _COGNITIVE_STATE_DIR / "thought_summary.txt"

    def _save_thought_summary(self):
        """将当前思维摘要持久化到磁盘"""
        try:
            self._COGNITIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
            summary = self.buffer.summary or ""
            self._THOUGHT_SUMMARY_FILE.write_text(summary, encoding="utf-8")
        except Exception:
            pass

    def _load_thought_summary(self):
        """从磁盘恢复思维摘要，失败时静默跳过"""
        try:
            if self._THOUGHT_SUMMARY_FILE.exists():
                summary = self._THOUGHT_SUMMARY_FILE.read_text(encoding="utf-8").strip()
                if summary:
                    self.buffer.summary = summary
        except Exception:
            pass

    def reset(self):
        """重置引擎状态（新会话时）"""
        self.buffer.clear()
        self._continuous_count = 0
        self._last_thought_time = 0
        self._cooldown_until = 0
        self._consecutive_pauses = 0
        self._pause_hibernating = False
        self._autonomous_running = False
        self._share_queue.clear()
        self._last_act_time = 0
        self._last_rejected_intent = ""
        self._cooldown_remaining = 0
        self._repeated_intent_count = 0
        if self._autonomous_task and not self._autonomous_task.done():
            self._autonomous_task.cancel()
        self._autonomous_task = None
        self.loop._thought_seq = 0
        # 焦点状态重置
        self._focus_topic = ""
        self._focus_source = ""
        self._focus_rounds = 0
        self._last_user_input = ""
        # 违规反馈重置
        self._next_feedback = ""
        # 联想种子重置
        self._last_hour = -1
        self._last_idle = 0
        self._last_screen = ""
        # 话题锁定重置
        self._reset_topic_lock()
