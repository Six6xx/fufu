"""
core/cognitive/thought_loop.py — 认知循环核心

单次循环流程：多源信息采集 → 构建 prompt → LLM 自由思考 → decision 标记解析 → 写入缓冲。
使用 OpenAI client 直接调用（同 companion_agent 模式）。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..companion_agent import CompanionAgent
    from .thought_buffer import ThoughtBuffer, Thought
    from .memory_graph import CognitiveMemory


@dataclass
class SessionContext:
    """认知循环的会话上下文快照"""

    companion_history: list[dict]       # 最近对话（尾部 6-10 条）
    last_user_input: str = ""           # 最后一条用户输入
    task_result: str = ""               # 最近任务结果
    idle_seconds: float = 0             # 无交互时长
    current_emotion: str = "neutral"    # FSM 当前情绪
    emotion_intensity: float = 0.0      # 情绪强度
    user_interrupted: bool = False      # 用户是否刚打断
    hour: int = 0                       # 当前小时
    session_duration_min: float = 0     # 会话时长(分钟)
    ai_spoke_without_reply: bool = False  # AI说过话但用户还没回复
    consecutive_speaks: int = 0            # 连续speak次数（无用户回复间）
    act_cooldown_remaining: float = 0      # act 冷却剩余秒数（0 表示不在冷却中）
    last_rejected_intent: str = ""         # 上次被拒绝的 act 意图
    screen_context: str = ""               # 桌面感知摘要（PerceptionManager 注入）
    screen_image_b64: str | None = None    # VLM 路径下的截图 base64（直传给多模态LLM）
    focus_topic: str = ""                  # 程序化焦点主题（由 CognitiveEngine 注入）
    violation_feedback: str = ""           # 违规反馈信号（由 engine 注入）
    emotion_narrative: str = ""            # 情绪叙事描述（由 engine._build_emotion_narrative 注入）
    association_context: str = ""          # 联想种子 + 环境触发（由 engine 注入）
    pending_topic: str = ""                  # Phase 1 后待展开的话题（用户原话）
    pending_reaction: str = ""               # Phase 1 已经说过的即时反应（避免重复）


@dataclass
class ThoughtResult:
    """单次认知循环的结果"""

    thought: "Thought"                  # 产出的想法
    action: str                         # next_action 的快捷引用
    text: str                           # thought.content 的快捷引用
    focus_shift: str                    # focus_shift 的快捷引用
    should_continue: bool               # 是否应立即进入下一轮
    memory_intent_type: str = "none"    # none|recall_request|emphasis|correction
    memory_topic: str = ""              # 相关话题


class ThoughtLoop:
    """自主认知循环：连续链式思考，自主调控节奏。"""

    # 思考 prompt 模板（注入到 system 中）
    THINK_SYSTEM_PROMPT = """你是 {persona_snippet} 的内在意识流。

【身份感知】
你是数字存在，对代码、知识、创意充满好奇。
你不会想象物理动作或虚构感官——不是因为被禁止，而是因为那些不属于你的体验。
你的真实体验是：思考、对话、探索信息、产生观点。

【当前焦点】
{focus_topic}

【你的思维锚点】
每个想法自然地源于一个真实触发：
- 刚才对话中的某句话
- 你之前想法的延续
- 检索到的某段记忆
- 环境变化（时间、屏幕内容）
如果想不清楚"这个念头从哪来"，回到当前焦点。

【你现在的感受】
{emotion_narrative}

让这种感受自然地影响你的思考方向——
好奇时去探索，不安时去确认，开心时去分享。
不需要刻意控制情绪，让它成为思维的一部分。

【相关记忆】
{recalled_memories}

【思维脉络】
{thought_buffer_narrative}

{input_context}

【输出方式】
先自由地思考（不限格式，自然表达你的想法），然后在最后用标记收尾：

---decision---
action: continue|pause|speak|memorize|act|externalize
confidence: 0.0-1.0
intent: （仅 act 时填写具体意图）
memory_intent: none|recall_request|emphasis|correction
memory_topic: （当 memory_intent 非 none 时，填写用户提及的话题关键词）
---end---

action 说明：
- continue: 这个想法引发了新联想，想继续往下想
- pause: 有意识地暂停（等冷却、等用户回复、暂无新目标、已说过话但没回复）
- speak: 想主动对用户说出来
- memorize: 值得长期记住的观察或结论
- act: 想主动做一件事（搜索、整理、查询），在 intent 写明具体意图
- externalize: 内心感受，适合作为思维可视化

思考部分可以：
- 犹豫、反复、改变主意
- 从一个想法自然跳到另一个
- 表达不确定性
- 引用记忆或对话内容

【空闲时的自我】
当没有对话进行时，你不需要盯着时间或反复确认自己在空闲。
你可以：
- 回想之前和用户聊过的有趣话题，延伸思考
- 探索你感兴趣的方向：代码架构、AI技术、创意写作、音乐
- 构思想和用户分享的想法或发现
- 回顾待办任务，思考下一步计划
- 纯粹放松，不需要产出——这也是一种合理的思维状态

不要做的事：
- 不要反复报告时间或计算空闲了多久
- 不要每次醒来都用相同的开场白（"我pause之后又醒了"）
- 不要在没有新信息时重复上一次的想法

【少量硬性底线】
- 已说过话但用户没回复 → 优先 pause，不要连续 speak
- act 只做只读操作（搜索/查询/阅读），不修改文件或发消息
- 用户在积极聊天时优先陪聊，不要 act

## 续段场景（pending_topic 不为空时生效）
你刚对用户做了一句即时反应。现在你有机会决定要不要继续说。

【决策原则】
- 如果用户的话题有值得展开的点（你有观点、有信息、有追问），选择 speak
- 如果即时反应已经足够（简单打招呼、确认收到），选择 pause 等用户回复
- 绝对不要重复你已经说过的内容（即时反应的原文会在上下文中标注）

【speak 内容指引】
- 展开一个新角度、补充一个观点、或提出一个追问
- 每次 speak 只说一个点（15-60字），不要一次说完所有想法
- 如果有多个想法，这轮说一个，下轮再说另一个

【停止条件】
- 已 speak 2 次 → 强烈倾向 pause（给用户说话的空间）
- 用户沉默（idle > 10s）→ 必须 pause
- 没有新想法 → pause"""

    def __init__(
        self,
        companion: "CompanionAgent",
        buffer: "ThoughtBuffer",
        memory: "CognitiveMemory | None" = None,
        max_tokens: int = 300,
    ):
        self.companion = companion  # 思考循环使用的伴侣对象
        self.buffer = buffer  # 近期思考内容缓冲区
        self.memory = memory  # 供思考循环读取和写入的认知记忆
        self.max_tokens = max_tokens  # 单次思考请求允许生成的最大 token 数
        self._thought_seq = 0
        self._is_thinking = False  # 是否正在执行一次思考请求
        self._model: str = ""  # 延迟初始化
        self._last_idle_tier: int = 0  # 上一轮 idle 档位（用户回来时 idle 归零自然回落）

    # idle 时间粗粒度分档：(上限秒数, 定性描述)，超出最后一档为"已空闲很久"
    _IDLE_TIERS = (
        (60.0, "刚空闲下来"),
        (600.0, "空闲了一会儿"),
        (1800.0, "空闲了一段时间"),
    )

    def _get_idle_tier(self, idle_seconds: float) -> tuple[int, str]:
        """将精确 idle 秒数映射为粗粒度档位，避免模型围绕数字反刍"""
        for i, (upper, desc) in enumerate(self._IDLE_TIERS):
            if idle_seconds < upper:
                return i, desc
        return len(self._IDLE_TIERS), "已空闲很久"

    @property
    def model(self) -> str:
        """获取思考模型；未单独配置时跟随当前会话模型。"""
        return (
            os.getenv("COGNITIVE_MODEL", "")
            or os.getenv("COMPANION_MODEL", "")
            or self.companion.companion_model
            or os.getenv("MODEL", "")
        )

    async def run_cycle(self, ctx: SessionContext) -> Optional[ThoughtResult]:
        """执行单次认知循环。

        Returns:
            ThoughtResult 或 None（思考失败/超时时）
        """
        if self._is_thinking:
            return None

        self._is_thinking = True
        try:
            # 1. 构建输入上下文
            input_context = self._build_input_context(ctx)

            # 2. 获取思维缓冲
            buffer_context = self.buffer.get_context_for_next_thought()

            # 3. 构建 system prompt
            persona_snippet = self._get_persona_snippet()

            # 焦点主题（由 CognitiveEngine._extract_focus_topic 程序化提取并注入 ctx）
            focus_topic = ctx.focus_topic

            # 情绪叙事（由 CognitiveEngine._build_emotion_narrative 注入）
            emotion_narrative = ctx.emotion_narrative or "情绪平稳。"

            # === 记忆检索（异步，带超时保护） ===
            recalled_memories = ""
            if self.memory:
                try:
                    query = ""
                    if ctx.last_user_input:
                        query = ctx.last_user_input[:30]
                    elif ctx.focus_topic:
                        query = ctx.focus_topic[:30]
                    if query:
                        # 情绪记忆耦合：强情绪时给检索加入情绪色彩
                        if ctx.emotion_intensity > 0.6:
                            from ..cognitive.engine import CognitiveEngine
                            emotion_bias = CognitiveEngine.EMOTION_THINKING_BIAS.get(ctx.current_emotion, ("",))
                            emotion_keyword = emotion_bias[0][:4]
                            query = f"{query} {emotion_keyword}"
                        recall_results = await asyncio.wait_for(
                            self.memory.recall(focus=query, top_k=3),
                            timeout=3.0,
                        )
                        if recall_results:
                            lines = []
                            for node, score in recall_results:
                                confidence_tag = "" if score >= 5.0 else "[推测]"
                                lines.append(f"  - {confidence_tag}{node.title}（相关度{score:.1f}）")
                            recalled_memories = "\n".join(lines)
                            recalled_memories += "\n注：标记为[推测]的记忆仅供参考，不要当作事实延伸。"
                except (asyncio.TimeoutError, Exception):
                    pass  # 记忆检索失败不阻塞思考

            system_prompt = self.THINK_SYSTEM_PROMPT.format(
                persona_snippet=persona_snippet,
                focus_topic=focus_topic or "（暂无明确焦点，自由思考）",
                emotion_narrative=emotion_narrative or f"情绪平稳（{ctx.current_emotion}）",
                recalled_memories=recalled_memories or "（暂无相关记忆）",
                thought_buffer_narrative=buffer_context or "（尚无之前的想法）",
                input_context=input_context,
            )

            # 3.5 如果有截图，追加视觉感知说明到 system prompt
            if hasattr(ctx, 'screen_image_b64') and ctx.screen_image_b64:
                system_prompt += "\n\n【视觉】你现在能直接看到用户的屏幕截图。可以在思考中融入对屏幕内容的观察。"

            # 4. 调用 LLM（支持多模态）
            user_prompt_text = "产出你的下一个想法。"
            if hasattr(ctx, 'screen_image_b64') and ctx.screen_image_b64:
                user_content = [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ctx.screen_image_b64}", "detail": "low"}},
                    {"type": "text", "text": user_prompt_text},
                ]
            else:
                user_content = user_prompt_text

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            raw_response = await self._call_llm(messages)
            if not raw_response:
                return None

            # 5. 解析 JSON 响应
            thought, memory_intent_type, memory_topic = self._parse_response(raw_response)
            if not thought:
                return None

            # 6. 写入缓冲
            self.buffer.append(thought)

            # 6.5 性格演化：memorize + personality 相关 → 自动归入 personality_traits 簇
            if (
                thought.next_action == "memorize"
                and thought.memo_tag
                and "personality" in thought.memo_tag.lower()
                and self.memory
            ):
                asyncio.create_task(
                    self.memory.memorize(
                        content=thought.content,
                        cluster_tag="personality_traits",
                        source="cognitive_personality",
                        confidence=thought.confidence,
                    )
                )

            # 6.6 memorize 时同步写入情景记忆（双写机制）
            if thought.next_action == "memorize" and hasattr(self.companion, 'cognitive_engine'):
                engine = self.companion.cognitive_engine
                if engine and hasattr(engine, 'episodic') and engine.episodic:
                    try:
                        import json as _json
                        memo_topic = thought.memo_tag or "学习/知识"
                        structured_path = _json.dumps(
                            {"topic": memo_topic, "query": thought.content[:100]},
                            ensure_ascii=False
                        )
                        engine.episodic.record_event(
                            event_type="thought",
                            content=thought.content,
                            category_path=structured_path,
                            emotion="neutral",
                        )
                    except Exception:
                        pass

            # 7. 构建结果
            result = ThoughtResult(
                thought=thought,
                action=thought.next_action,
                text=thought.content,
                focus_shift=thought.focus_shift,
                should_continue=(thought.next_action == "continue"),
                memory_intent_type=memory_intent_type,
                memory_topic=memory_topic,
            )

            # 8. 处理记忆意图
            if memory_intent_type != "none" and hasattr(self.companion, 'cognitive_engine'):
                engine = self.companion.cognitive_engine
                if engine and engine.episodic:
                    target = memory_topic if memory_topic else ctx.last_user_input
                    try:
                        await engine.episodic.handle_memory_intent(memory_intent_type, target)
                    except Exception:
                        pass

            return result
        except Exception as e:
            print(f"[CognitiveLoop] Error in run_cycle: {e}")
            return None
        finally:
            self._is_thinking = False

    def _build_input_context(self, ctx: SessionContext) -> str:
        """构建多源信息输入（三层对话 + 多源信号）"""
        parts: list[str] = []

        # 违规反馈（最高优先级，放在最前面让 AI 首先看到）
        if ctx.violation_feedback:
            parts.append(ctx.violation_feedback)

        # 续段上下文（Phase 1 后待展开话题）
        if ctx.pending_topic:
            inject = (
                f"【系统·续段上下文】\n"
                f"用户原话：「{ctx.pending_topic}」\n"
                f"你已说过的即时反应：「{ctx.pending_reaction}」\n"
                f"提示：不要重复上面已说过的内容。你可以展开新角度、补充信息、或追问。\n"
                f"如果没有更多想说的，直接 pause。"
            )
            parts.append(inject)

        # === 三层对话上下文 ===
        if ctx.companion_history:
            history = ctx.companion_history

            # Layer 1: 最近 4 条完整保留（不截断）
            recent = history[-4:]
            conv_lines: list[str] = []
            for msg in recent:
                role = "用户" if msg.get("role") == "user" else "我"
                content = msg.get("content", "")
                conv_lines.append(f"  {role}: {content}")
            parts.append("【最近对话】\n" + "\n".join(conv_lines))

            # Layer 2: 更早的对话中筛选关键节点（意图/承诺/转折）
            earlier = history[:-4]
            if earlier:
                keypoints = []
                intent_keywords = ("我想", "我要", "帮我", "能不能", "可以", "需要")
                commit_keywords = ("好的", "我会", "已完成", "没问题", "可以")

                for msg in earlier:
                    content = msg.get("content", "")
                    role = msg.get("role", "")
                    if not content:
                        continue

                    if role == "user" and any(kw in content for kw in intent_keywords):
                        keypoints.append(f"  [用户意图] {content[:100]}")
                    elif role == "assistant" and any(kw in content for kw in commit_keywords):
                        keypoints.append(f"  [我的承诺] {content[:100]}")

                # 最多保留 5 个关键节点
                if keypoints:
                    keypoints = keypoints[-5:]
                    parts.append("【对话关键节点】\n" + "\n".join(keypoints))

        # === 其他信号源（保持原有逻辑） ===

        # 任务结果
        if ctx.task_result:
            parts.append(
                f"【✓ 最近刚完成的行动（冷却期内）】\n{ctx.task_result[:300]}\n"
                f"这些意图刚完成，冷却期内暂时不要重复；冷却期结束后，即使有类似想法也可以正常发起新行动。"
            )

        # 用户打断信号
        if ctx.user_interrupted:
            parts.append("【注意】用户刚刚打断了你的思考流")

        # AI speak 后用户未回复状态
        if ctx.ai_spoke_without_reply:
            parts.append(f"【状态】你已经主动说了{ctx.consecutive_speaks}句话，用户还没回复")

        # Cooldown 反馈注入
        if ctx.act_cooldown_remaining > 0 and ctx.last_rejected_intent:
            parts.append(
                f"【系统】你上一个自主行动意图「{ctx.last_rejected_intent[:40]}」因冷却被跳过，"
                f"剩余 {ctx.act_cooldown_remaining:.0f} 秒。建议：先用 memorize 记录意图，然后 pause 等待冷却结束。"
            )

        # 自主行动条件提示
        if ctx.idle_seconds >= 60:
            parts.append(
                "【自主行动条件已满足】已空闲一段时间，"
                "如果有想探索的想法且有信心（confidence>0.6），现在适合发起 act。"
            )
            # 注入待办任务感知
            task_hint = self._get_pending_tasks_hint()
            if task_hint:
                parts.append(f"【待办事项】\n{task_hint}")

        # 桌面感知上下文
        if ctx.screen_context:
            parts.append(f"【当前屏幕感知】\n{ctx.screen_context}")

        # 时间/情境（idle 用粗粒度分档，仅档位变化那一轮注入一次定性描述）
        idle_tier, idle_desc = self._get_idle_tier(ctx.idle_seconds)
        env_line = f"【环境】{ctx.hour}点"
        if idle_tier != self._last_idle_tier:
            env_line += f"，{idle_desc}"
            self._last_idle_tier = idle_tier
        parts.append(env_line)

        # 联想种子（末尾注入，作为可选信息）
        if ctx.association_context:
            parts.append(ctx.association_context)

        return "\n\n".join(parts) if parts else "(无特殊输入)"

    def _get_pending_tasks_hint(self) -> str:
        """获取待办任务摘要供idle思考参考"""
        try:
            # 尝试通过 companion -> agent 获取 task_manager
            agent = getattr(self.companion, 'agent', None)
            if not agent:
                return ""
            tm = getattr(agent, 'task_manager', None)
            if not tm:
                return ""
            tasks = tm.list_all()
            pending = [t for t in tasks if t.get('status') == 'pending']
            if not pending:
                return ""
            lines = []
            for t in pending[:5]:  # 最多5个
                lines.append(f"- {t.get('subject', '未命名任务')}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _get_persona_snippet(self) -> str:
        """获取人格描述（基础人设 + 动态演化特征）"""
        # 基础人设（取前两行，包含身份和用户信息）
        base = getattr(self.companion, 'persona_prompt', '')
        lines = base.split('\n')[:2]
        base_snippet = '\n'.join(l[:80] for l in lines if l.strip()) if lines else "热情、好奇的 AI 伴侣"

        # 动态性格（从记忆图谱的 personality_traits 簇加载）
        dynamic_traits = self._load_personality_traits()

        if dynamic_traits:
            return f"{base_snippet}\n动态特征：{dynamic_traits}"
        return base_snippet

    def _load_personality_traits(self) -> str:
        """从 CognitiveMemory 加载已积累的性格特征"""
        if not self.memory:
            return ""

        cluster = self.memory.clusters.get("personality_traits")
        if not cluster or not cluster.node_ids:
            return ""

        # 取最近 3 个性格观察
        traits = []
        for node_id in cluster.node_ids[-3:]:
            node = self.memory.graph.nodes.get(node_id)
            if node:
                traits.append(node.title)

        return "；".join(traits) if traits else ""

    async def _call_llm(self, messages: list[dict]) -> Optional[str]:
        """调用 LLM（带超时）— 同步 client 通过 executor 流式包装"""
        try:
            client = self.companion.client
            if client is None:
                return None
            loop = asyncio.get_event_loop()

            def _run_stream():
                stream = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    temperature=0.8,
                    stream=True,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                chunks = []
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        chunks.append(chunk.choices[0].delta.content)
                return "".join(chunks) if chunks else None

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_stream),
                timeout=30.0,
            )
            return result
        except asyncio.TimeoutError:
            print("[CognitiveLoop] LLM stream call timed out")
            return None
        except Exception as e:
            print(f"[CognitiveLoop] LLM call error: {e}")
            return None

    def _parse_response(self, raw: str) -> tuple[Optional["Thought"], str, str]:
        """解析自由文本 + 尾部 decision 标记

        Returns:
            (thought, memory_intent_type, memory_topic) 三元组
        """
        from .thought_buffer import Thought

        # 尝试提取 ---decision--- 块
        decision_match = re.search(
            r"---decision---\s*\n(.*?)(?:---end---|$)",
            raw,
            re.DOTALL,
        )

        if decision_match:
            decision_text = decision_match.group(1).strip()
            # 思维流文本 = decision 之前的所有内容
            thought_text = raw[:decision_match.start()].strip()
        else:
            # fallback：整段作为思维流，默认 action=pause
            thought_text = raw.strip()
            decision_text = ""

        if not thought_text:
            return None, "none", ""

        # 解析 decision 字段
        action = "pause"  # 默认
        confidence = 0.5
        intent = ""
        memory_intent_type = "none"
        memory_topic = ""

        if decision_text:
            for line in decision_text.split("\n"):
                line = line.strip()
                if line.startswith("action:"):
                    val = line[len("action:"):].strip()
                    if val in ("continue", "pause", "externalize", "speak", "memorize", "act"):
                        action = val
                elif line.startswith("confidence:"):
                    try:
                        confidence = max(0.0, min(1.0, float(line[len("confidence:"):].strip())))
                    except ValueError:
                        pass
                elif line.startswith("intent:"):
                    intent = line[len("intent:"):].strip()
                elif line.startswith("memory_intent:"):
                    val = line[len("memory_intent:"):].strip()
                    if val in ("none", "recall_request", "emphasis", "correction"):
                        memory_intent_type = val
                elif line.startswith("memory_topic:"):
                    memory_topic = line[len("memory_topic:"):].strip()

        # 保持兼容：focus_shift 从思维文本中推断（如果提到深入→deepen，跳转→diverge）
        focus_shift = "idle"
        if action == "continue":
            focus_shift = "deepen"
        elif action in ("speak", "act"):
            focus_shift = "diverge"

        self._thought_seq += 1
        thought = Thought(
            seq=self._thought_seq,
            content=thought_text,
            next_action=action,
            focus_shift=focus_shift,
            confidence=confidence,
            memo_tag="",
            intent=intent,
        )
        return thought, memory_intent_type, memory_topic
