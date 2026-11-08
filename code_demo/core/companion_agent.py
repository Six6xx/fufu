"""
code_demo/core/companion_agent.py — CompanionAgent 伴侣层

位于 AgentTeam 之上的情感人格层，三阶段流程：
1. 即时情感反应 — 始终可见
2. 委派执行 — 通过 AgentTeam，可见性由 show_agent_team 控制
3. 结果总结 — 始终可见

不创建新的 Agent 实例，共享 agent.client。
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import TYPE_CHECKING

from code_demo.core.config import AI_CONFIG_DIR, COMPANION_MODEL, COMPANION_PERSONA_PROMPT, COMPANION_PROFILE_FILE, MODEL, ENABLE_REASONING

if TYPE_CHECKING:
    from code_demo.core.agent import Agent
    from code_demo.core.agent_team import AgentTeam


class CompanionAgent:
    """伴侣代理：情感人格层 + 执行委派。

    三阶段流程：
    1. 即时情感反应 — 直接 OpenAI 调用，简短表达性格
    2. 委派执行 — 通过 AgentTeam 调用现有 Agent
    3. 结果总结 — 直接 OpenAI 调用，简短总结成果

    不创建新的 Agent 实例，共享 agent.client。
    """

    def __init__(
        self,
        agent: Agent,
        agent_team: AgentTeam,
        persona_prompt: str = "",
    ) -> None:
        self.agent = agent  # 共享的 Agent 实例（用于获取 client）
        self.agent_team = agent_team  # 执行层
        self.persona_prompt = persona_prompt or COMPANION_PERSONA_PROMPT  # 伴侣人格使用的系统提示词
        self._vlm_cache: dict = {}  # 视觉模型结果缓存
        self.cognitive_engine = None  # 由 server.py 创建后绑定

    @property
    def client(self):
        """始终读取 Agent 当前客户端，避免运行时切换供应商后保留旧引用。"""
        return self.agent.client

    @property
    def companion_model(self) -> str:
        """未单独配置伴侣模型时，跟随 Agent 当前会话模型。"""
        return self._get_companion_model()

    # 工具类别归纳表：用于把执行层工具池浓缩成 Phase 1 可读的能力清单
    _CAPABILITY_CATEGORIES = [
        ("文件与代码", {"read_file", "write_file", "edit_file", "analyze_file", "graph_query", "graph_update"}),
        ("终端命令", {"bash"}),
        ("桌面与屏幕", {"desktop_control", "screen_observe"}),
        ("团队协作", {"send_message", "read_inbox", "list_tasks", "claim_task", "list_teammates"}),
        ("技能管理", {"list_skills", "load_skill", "suggest_skill_merges", "merge_skills", "complete_companion_setup"}),
        ("定时任务", {"cron_create", "cron_delete", "cron_list"}),
        ("笔记管理", {"list_expired_notes", "delete_notes"}),
    ]

    def _build_capability_digest(self) -> str:
        """动态枚举执行层（AgentTeam/Agent）当前可用工具，生成浓缩清单。

        每次调用时从共享 Agent 单例的 tool_defs 读取——该列表在 MCP
        连接/断开时由 _build_tool_pool 重建，因此工具增减自动同步，
        无单例初始化时序风险（CompanionAgent 持有的就是同一个实例）。
        """
        tool_defs = list(getattr(self.agent, "tool_defs", None) or [])
        if not tool_defs:
            return ""
        by_name: dict[str, str] = {}
        for t in tool_defs:
            name = t.get("name")
            if not name:
                continue
            desc = (t.get("description") or "").strip().split("\n")[0]
            if len(desc) > 40:
                desc = desc[:40] + "…"
            by_name[name] = desc
        lines: list[str] = []
        seen: set[str] = set()
        for cat, names in self._CAPABILITY_CATEGORIES:
            hit = [n for n in by_name if n in names]
            if not hit:
                continue
            lines.append(f"◆ {cat}:")
            for n in hit:
                lines.append(f"  - {n}: {by_name[n]}")
                seen.add(n)
        rest = [n for n in by_name if n not in seen]
        if rest:
            lines.append("◆ 扩展工具(MCP):")
            if len(rest) > 8:
                # 工具过多时只列名称，控制清单总长度
                lines.append("  - " + "、".join(rest[:24]) + ("…" if len(rest) > 24 else ""))
            else:
                for n in rest:
                    lines.append(f"  - {n}: {by_name[n]}")
        return "\n".join(lines)

    def _get_persona_prompt(self) -> str:
        """每次生成前读取陪伴资料，支持设置完成后立即生效。"""
        try:
            profile = _json.loads(COMPANION_PROFILE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.persona_prompt = COMPANION_PERSONA_PROMPT
            return self.persona_prompt
        required = ("user_name", "companion_name", "companion_call_name", "interests", "personality")
        if not all(str(profile.get(key, "")).strip() for key in required):
            self.persona_prompt = COMPANION_PERSONA_PROMPT
            registry = getattr(getattr(self.agent, "prompt_builder", None), "skill_registry", None)
            onboarding = registry.get_skill_body("__companion_onboarding") if registry else ""
            if onboarding:
                self.persona_prompt += (
                    "\n\n【首次设置流程】\n" + onboarding
                    + "\n完成四项并复述给用户确认后，委派执行层调用 complete_companion_setup 保存资料。"
                )
            return self.persona_prompt
        self.persona_prompt = (
            COMPANION_PERSONA_PROMPT
            + "\n\n【用户确认的陪伴设置】"
            + f"\n用户称呼：{profile['user_name']}"
            + f"\n陪伴者名称：{profile['companion_name']}"
            + f"\n用户对陪伴者的称呼：{profile['companion_call_name']}"
            + f"\n用户兴趣：{profile['interests']}"
            + f"\n偏好人格与交流风格：{profile['personality']}"
        )
        return self.persona_prompt

    # ────────────────────────────────
    # 主入口
    # ────────────────────────────────

    def process(
        self,
        user_input: str,
        token_callback=None,
        tool_callback=None,
        context_limit=None,
        attachments=None,
        original_content=None,
        show_agent_team: bool = False,
        phase2_token_callback=None,
    ) -> str | None:
        """三阶段处理用户输入。

        Args:
            user_input: 用户输入文本
            token_callback: Phase 1/3 使用的回调 (token_type, text)，始终推送到前端
            tool_callback: Phase 2 工具事件回调 (event_type, data)
            context_limit: 上下文字符数限制
            attachments: 附件列表
            original_content: 原始用户文本
            show_agent_team: True=Phase 2 完整可见；False=Phase 2 隐藏
            phase2_token_callback: Phase 2 专用 token 回调（server.py 传入，
                隐藏模式下可能是 tts_only 过滤回调）

        Returns:
            Agent 的最终回复文本
        """
        client = self.agent.client
        if client is None:
            return "错误：LLM 客户端未初始化。"

        model = self._get_companion_model()

        # ── Phase 1: 即时情感反应（始终可见）──
        print("\n[CompanionAgent] Phase 1: 即时情感反应")
        self._phase1_reaction(client, model, user_input, token_callback)

        # ── Phase 2: 委派给 AgentTeam（可见性控制）──
        print(f"[CompanionAgent] Phase 2: 委派 AgentTeam (visible={show_agent_team})")
        if show_agent_team:
            # 显示模式：透传所有回调
            p2_token_cb = token_callback
            p2_tool_cb = tool_callback
        else:
            # 隐藏模式：使用 phase2_token_callback（可能是 tts_only 或 None）
            p2_token_cb = phase2_token_callback
            p2_tool_cb = None  # 工具事件不推送

        team_result = self.agent_team.execute(
            user_input=user_input,
            token_callback=p2_token_cb,
            tool_callback=p2_tool_cb,
            context_limit=context_limit,
            attachments=attachments,
            original_content=original_content,
        )

        # ── Phase 3: 结果总结（始终可见，仅当任务成功时）──
        if team_result.success and team_result.task_result:
            print("\n[CompanionAgent] Phase 3: 结果总结")
            self._phase3_summary(
                client, model, user_input, team_result, token_callback
            )

        return team_result.task_result

    # ────────────────────────────────
    # 异步三阶段接口
    # ────────────────────────────────

    async def phase1_react(
        self,
        user_input: str,
        token_callback=None,
        context_messages: list = None,
        thought_context: str = "",
        screen_image_b64: str | None = None,
        realtime_context: str = "",
    ) -> tuple[str, str]:
        """Phase 1: 即时情感反应（快速返回）。

        Args:
            realtime_context: 系统程序化预取的只读实时信息（时间/天气/记忆），
                注入 prompt 供直接引用，避免模型虚构"我调用了工具"。

        Returns:
            (reaction_text, action_type)
            - reaction_text: 伴侣的即时反应文本
            - action_type: "chat" | "answer" | "clarify" | "execute"
        """
        if self.client is None:
            return "错误：LLM 客户端未初始化。", "chat"

        model = self.companion_model
        env_summary = self._get_environment_summary()

        realtime_block = ""
        if realtime_context and realtime_context.strip():
            realtime_block = (
                "【实时信息·权威数据】以下信息由系统程序化实时获取，是当前真实、"
                "最新的权威数据，优先级高于对话历史中出现的任何同类信息。"
                "若与历史中你自己说过的时间/天气等内容矛盾，一律以此处为准，"
                "不要复读历史中的错误信息，也不要声称调用了任何工具查询："
                + realtime_context
            )

        vision_hint = ""
        if screen_image_b64 and self._is_companion_vlm():
            vision_hint = "\n\n【已附截图】当前用户消息已附带一张截图。你可以直接基于这张图回答关于图中可见内容的问题；这不代表你能看到截图之后的实时屏幕，也不代表你已经调用了工具。若用户要求获取新截图、点击、输入或操作桌面，仍须委派执行团队。"

        # 动态注入执行层能力清单（只读参考，非 function calling）
        capability_digest = self._build_capability_digest()
        capability_block = ""
        if capability_digest:
            capability_block = (
                "\n\n【你可以委派执行的能力清单】以下是执行团队（AgentTeam）当前真实可用的工具：\n"
                + capability_digest
                + "\n⚠️ 这些能力你自己【无法直接调用】，只能通过在回复末尾输出 [EXECUTE] 委派给执行团队完成。"
                "绝不允许在 [CHAT]/[ANSWER] 回复中声称你已经使用了这些工具，或描述它们的执行结果。"
            )

        messages = [
            {
                "role": "system",
                "content": (
                    self._get_persona_prompt()
                    + f"\n\n当前环境状态: {env_summary}"
                    + (f"\n\n【你最近的内心想法】（这些是你已经想过的事，说话时应保持一致）：\n{thought_context}" if thought_context else "")
                    + "\n\n用户发来了一条消息。你的任务："
                    "\n【即时反应】只输出一句话（15-40字）+ 末尾动作标记。禁止多段落、禁止 bullet list。后续由认知循环展开。"
                    "\n\n【动作标记】在回复末尾单独一行选择一个："
                    "\n   - [CHAT] — 纯闲聊、情感表达、日常陪伴，不需要任何行动"
                    "\n   - [ANSWER] — 用户问了一个你可以基于知识回应的问题——注意：Phase 1 仍然只输出一句话反应，展开内容由后续认知循环处理"
                    "\n   - [CLARIFY] — 用户意图不明确，需要你追问确认后再决定"
                    "\n   - [EXECUTE] — 需要分析代码、读写文件、查询信息等具体操作，委派执行团队处理"
                    + capability_block
                    + "\n\n【EXECUTE 判定原则】"
                    "\n凡是答案依赖访问当前工作区、网络、运行环境，或需要读写文件、执行命令、获取新截图、操作桌面的请求，都必须选 [EXECUTE]。"
                    "仅基于用户已提供内容或通用知识进行解释、推理、方案讨论，不需要外部验证时，选 [ANSWER]。"
                    "\n反向边界（以下情况【不要】委派 [EXECUTE]）："
                    "\n- 问当前时间、今天天气、回顾我们之前聊过的内容——这些已由【实时信息·权威数据】区块提供，直接用 [CHAT]/[ANSWER] 回答；"
                    "\n- 纯情感陪伴、夸奖、吐槽、分享心情 → [CHAT]；"
                    "\n- 概念性知识问答、观点讨论、方案分析（不需要查任何实时资料）→ [ANSWER]。"
                    "\n若缺少执行所需的目标、范围或关键参数，选 [CLARIFY]；不要用 [EXECUTE] 猜测用户未说明的目标。"
                    "\n\n【自检约束】如果你打算在回答里描述某个工具的执行结果（如'我看了一下，里面有…'、'搜到的结果是…'），"
                    "那这条消息就必须是 [EXECUTE]，绝不能是 [CHAT]/[ANSWER]——因为你此刻还没有执行任何工具。"
                    "\n\n【重要：当前阶段行为约束】"
                    "\n你在当前对话阶段【不能直接执行操作】。如果用户要求你做具体操作（截屏、点击、输入、打开应用、改代码、搜索等），你必须："
                    "\n1. 用自然语言表达你愿意帮忙的态度（如'交给我吧~'、'我来看看'）"
                    "\n2. 在回复末尾添加 [EXECUTE] 标记，由后续阶段来真正执行"
                    "\n3. 绝对不要说'已经帮你做了'、'点好了'、'改完了'等暗示操作已完成的话——你现在还没有执行任何操作"
                    "\n4. 你只是在做意图判断和情感回应，实际执行会在标记 [EXECUTE] 后由系统的执行层完成"
                    "\n- 可以用俏皮的方式说'我来看看'、'交给我吧'，但不要谎称已经完成了实际没做的事"
                    "\n- 不要谎称未发生过的事，可以期待未来想做什么，以及对之前发生的事进行回顾"
                    "\n\n【分类示例——务必遵守】"
                    "\n用户: \"帮我看看项目里有哪些 python 文件\" → [EXECUTE]（听起来轻量，但需要真实读取目录）"
                    "\n用户: \"搜一下 wttr.in 是什么\" → [EXECUTE]（需要真实联网搜索，不能凭印象回答）"
                    "\n用户: \"帮我看看桌面上有什么\" → [EXECUTE]（需要真实截屏观察）"
                    "\n用户: \"你能看到我的屏幕吗？\" → [EXECUTE]（需要调用截屏工具来回答）"
                    "\n用户: \"帮我点一下那个按钮\" → [EXECUTE]"
                    "\n用户: \"现在几点了\" → [ANSWER]（时间已由实时信息提供，直接回答，不委派）"
                    "\n用户: \"今天天气怎么样\" → [ANSWER]（天气已由实时信息提供，直接回答，不委派）"
                    "\n用户: \"哈哈你今天真可爱\" → [CHAT]（纯闲聊，不需要任何行动）"
                    "\n用户: \"我今天心情不好\" → [CHAT]"
                    "\n用户: \"什么是量子计算\" → [ANSWER]"
                    "\n\n【关键规则】"
                    "\n已附截图时，可直接描述该截图；用户要求新的屏幕状态或桌面操作时才选 [EXECUTE]。不得把旧截图说成实时状态。"
                    + vision_hint
                ),
            },
        ]

        # 加入历史对话上下文（只取 user/assistant 角色，跳过 system 和 tool 消息）
        if context_messages:
            for m in context_messages:
                role = m.get("role")
                content = m.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content[:500]})

        # 注入位置前移：realtime_block 作为独立 system 消息，紧邻 user 消息之前
        # 这样模型注意力权重更高，不易被历史对话中的错误信息压制
        if realtime_block:
            messages.append({"role": "system", "content": realtime_block})

        # 短回复硬性约束 reminder —— 利用 recency bias 放在 user 消息紧前
        messages.append({"role": "system", "content": (
            "【硬性规则 — 违反此规则等于任务失败】\n"
            "你必须只输出一句话（15-40字）的即时反应，然后换行写动作标记。\n"
            "即使用户的问题很复杂、需要展开，你也只说第一反应；详细内容会由你的认知循环在后续自动展开，现在不需要你说。\n"
            "禁止输出 bullet list、编号列表、多段落、方案罗列。你只说一句话。\n\n"
            "动作判定示例：需要读取当前项目/网页/运行环境 → [EXECUTE]；基于已给材料或通用知识讨论 → [ANSWER]；缺少执行目标或关键参数 → [CLARIFY]。\n\n"
            "你必须像上面的示范一样——一句话，不展开。这是硬性规则。"
        )})

        if screen_image_b64 and self._is_companion_vlm():
            # VLM 多模态消息：截图 + 用户文字
            user_content = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screen_image_b64}", "detail": "low"}},
                {"type": "text", "text": user_input},
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_input})

        loop = asyncio.get_event_loop()

        def _run():
            return self._stream_openai(self.client, model, messages, token_callback, max_tokens=600)

        response = await loop.run_in_executor(None, _run)

        # 只解析最后一个非空行的精确动作标记，避免正文或引用内容误触发。
        tags = {
            "[EXECUTE]": "execute",
            "[ANSWER]": "answer",
            "[CLARIFY]": "clarify",
            "[CHAT]": "chat",
            "[NO_EXECUTE]": "chat",  # 兼容旧模型输出
        }
        lines = response.strip().splitlines()
        last_line = lines[-1].strip() if lines else ""
        action_type = tags.get(last_line, "clarify")
        if last_line in tags:
            lines.pop()
        reaction_text = "\n".join(lines).strip()

        return reaction_text, action_type

    async def phase2_execute(
        self,
        user_input: str,
        token_callback=None,
        tool_callback=None,
        progress_callback=None,
        cancel_check=None,
        context_limit=None,
        attachments=None,
        original_content=None,
    ) -> "AgentTeamResult":
        """Phase 2: 异步执行（直接 await，零线程池开销）。"""
        return await self.agent_team.async_execute(
            user_input=user_input,
            token_callback=token_callback,
            tool_callback=tool_callback,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            context_limit=context_limit,
            attachments=attachments,
            original_content=original_content,
        )

    async def phase3_summarize(
        self,
        team_result,
        user_input: str,
        token_callback=None,
    ) -> str:
        """Phase 3: 执行完成后自动触发的结果总结。"""
        if self.client is None:
            return ""

        model = self.companion_model
        loop = asyncio.get_event_loop()

        def _run():
            return self._phase3_summary(
                self.client, model, user_input, team_result, token_callback
            )

        return await loop.run_in_executor(None, _run)

    async def companion_chat(
        self,
        user_input: str,
        token_callback=None,
        task_context: str = "",
        context_messages: list = None,
    ) -> tuple[str, bool]:
        """独立对话通道：后台任务执行中与用户闲聊/交互。

        Args:
            user_input: 用户消息
            token_callback: token流回调
            task_context: 当前后台任务的进度上下文（可选）
            context_messages: 历史对话上下文（仅 user/assistant 角色）

        Returns:
            (response_text, change_detected)
            - response_text: 伴侣AI的回复
            - change_detected: 是否检测到用户想要变更需求
        """
        if self.client is None:
            return "错误：LLM 客户端未初始化。", False

        # 记录对话事件到情景记忆
        if self.cognitive_engine:
            try:
                await self.cognitive_engine.record_episodic_event(
                    event_type="dialogue",
                    content=user_input,
                    emotion="neutral",
                )
            except Exception:
                pass

        thought_ctx = self._get_thought_context()
        thought_injection = self._build_thought_injection(thought_ctx)

        system_prompt = f"""你是芙芙，正在后台帮用户处理事情。

当前任务状态: {task_context or '执行中'}
{thought_injection}
规则：
- 如果用户只是闲聊，正常回复，保持简短自然
- 如果用户问任务进度，根据上面的任务状态信息回答
- 如果用户提到之前的对话内容，参考历史对话上下文回答
- 如果用户表达了想要修改/取消/变更当前任务的意图，在回复末尾加上标记 [CHANGE_DETECTED]
- 如果用户要求做某件事（关网页、搜索、改文件等），不要拒绝，回复中加上 [CHANGE_DETECTED] 标记，让系统中断当前任务来处理新请求
- 不要谎称完成了实际没做的事，可以俏皮地说'等我看看~'或'我马上去办！'
- 不要提及你检测到了变更意图，只是自然回复
- 保持 1-3 句简短回复"""

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # 注入对话历史上下文
        if context_messages:
            for m in context_messages:
                role = m.get("role")
                content = m.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content[:300]})

        messages.append({"role": "user", "content": user_input})

        loop = asyncio.get_event_loop()

        def _run():
            return self._stream_openai(
                self.client, self.companion_model, messages, token_callback
            )

        response = await loop.run_in_executor(None, _run)

        change_detected = "[CHANGE_DETECTED]" in response
        clean_response = response.replace("[CHANGE_DETECTED]", "").strip()
        return clean_response, change_detected

    # ------------------------------------------------------------------
    # idle 碎碎念 — Phase 5.5 多样化风格
    # ------------------------------------------------------------------

    def _pick_murmur_type(self, context: dict) -> str:
        """根据环境上下文选择碎碎念类型"""
        import random

        emotion_intensity = context.get("emotion_intensity", 0)
        idle_seconds = context.get("idle_seconds", 0)
        conversation_count = context.get("conversation_count", 0)
        hour = context.get("hour", 12)

        # 带权重的类型选择
        candidates = []

        # 情绪余温 — 残留情绪>0.2时高权重
        if emotion_intensity > 0.2:
            candidates.append(("emotion_afterglow", 4))

        # 话题回忆 — 对话轮数>3时
        if conversation_count > 3:
            candidates.append(("topic_callback", 2))

        # 时间反应 — 深夜(22-6)或清晨(6-8)
        if hour >= 22 or hour < 6:
            candidates.append(("time_reaction", 3))
        elif 6 <= hour < 8:
            candidates.append(("time_reaction", 2))

        # 主动搭话 — idle>60s 且有过对话，低权重
        if idle_seconds > 60 and conversation_count > 0:
            candidates.append(("proactive_engage", 1))

        # 默认观察类始终可选
        candidates.append(("idle_observation", 3))

        # 加权随机选择
        types = [c[0] for c in candidates]
        weights = [c[1] for c in candidates]
        return random.choices(types, weights=weights, k=1)[0]

    def _build_time_hint(self, hour: int, idle_secs: float) -> str:
        """根据时间和空闲时长生成环境描述"""
        # 时间段感知
        if 0 <= hour < 6:
            period = "深夜了，有点困"
        elif 6 <= hour < 9:
            period = "早上，新的一天"
        elif 9 <= hour < 12:
            period = "上午"
        elif 12 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 22:
            period = "晚上"
        else:
            period = "夜深了"

        # 空闲时长感知
        if idle_secs > 120:
            idle_desc = "已经安静了好一会儿了"
        elif idle_secs > 60:
            idle_desc = "有一小会儿没说话了"
        else:
            idle_desc = "刚刚安静下来"

        return f"现在是{period}，{idle_desc}"

    def _build_type_instruction(self, murmur_type: str, recent_emotion: str,
                                 emotion_intensity: float, last_user_emotion: str,
                                 last_topic: str) -> str:
        """根据碎碎念类型生成对应的内容指令"""
        if murmur_type == "emotion_afterglow":
            return f"带有'{recent_emotion}'情绪余温的感想（强度{emotion_intensity:.1f}）"
        elif murmur_type == "topic_callback":
            return "回忆/延续之前话题的自言自语"
        elif murmur_type == "time_reaction":
            return "对当前时间的自然反应（如犯困、感叹时间快等）"
        elif murmur_type == "proactive_engage":
            return "对用户的轻度搭话（自然、不刻意、不强迫对方回应）"
        else:  # idle_observation
            return "自言自语/碎碎念"

    def _build_constraints(self, murmur_type: str) -> str:
        """根据类型返回不同的生成约束"""
        base = "- 自然、随意，像是不经意间的表达\n- 保持你的人格特点\n"

        if murmur_type == "proactive_engage":
            return base + "- 可以轻度提及用户，但不要强迫对方回应\n- 语气轻松随意，像是顺口一说"
        elif murmur_type == "emotion_afterglow":
            return base + "- 体现刚才对话留下的情绪痕迹\n- 不要重复刚才说过的话，而是延伸感受"
        elif murmur_type == "topic_callback":
            return base + "- 像是突然想起之前聊的某个点\n- 不要主动问用户问题"
        elif murmur_type == "time_reaction":
            return base + "- 对时间流逝的自然感受\n- 可以打哈欠、伸懒腰、感叹等"
        else:
            return base + "- 可以是：看窗外、哼歌、伸懒腰、发呆等\n- 不要主动问用户问题，不要刻意寻求互动"

    async def generate_idle_murmur(self, context: dict) -> tuple[str, str] | None:
        """idle 状态下生成自主碎碎念（轻量 LLM 调用）。

        Args:
            context: 包含 idle_seconds, last_topic, task_description,
                     hour, session_duration_min, recent_emotion,
                     emotion_intensity, conversation_count, last_user_emotion

        Returns:
            (碎碎念文本, source类型) 元组，或 None（生成失败时）
            source: "idle" | "proactive"
        """
        if self.client is None:
            return None

        # 选择碎碎念类型
        murmur_type = self._pick_murmur_type(context)

        idle_secs = context.get("idle_seconds", 0)
        last_topic = context.get("last_topic", "")
        hour = context.get("hour", 12)
        recent_emotion = context.get("recent_emotion", "neutral")
        emotion_intensity = context.get("emotion_intensity", 0)
        last_user_emotion = context.get("last_user_emotion", "")

        # 构建时间感知提示
        time_hint = self._build_time_hint(hour, idle_secs)
        topic_hint = f"刚才聊的内容大概是：{last_topic[:60]}" if last_topic else "之前没聊什么特别的"

        # 根据类型构建不同的生成指令
        type_instruction = self._build_type_instruction(
            murmur_type, recent_emotion, emotion_intensity, last_user_emotion, last_topic
        )

        # 字数限制：proactive 放宽
        char_limit = "20-50字" if murmur_type == "proactive_engage" else "15-30字"

        system_prompt = f"""{self._get_persona_prompt()}

你现在处于空闲状态。{time_hint}。{topic_hint}。

生成一句{type_instruction}（{char_limit}），要求：
{self._build_constraints(murmur_type)}
只输出内容本身，不要加引号或说明"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "（自言自语）" if murmur_type != "proactive_engage" else "（看向用户）"},
        ]

        loop = asyncio.get_event_loop()

        def _run():
            return self._stream_openai(
                self.client, self.companion_model, messages, None, max_tokens=80
            )

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=5.0
            )
            if not result or not result.strip():
                return None

            source = "proactive" if murmur_type == "proactive_engage" else "idle"
            return (result.strip(), source)
        except (asyncio.TimeoutError, Exception):
            return None

    # ────────────────────────────────
    # Phase 1: 即时情感反应
    # ────────────────────────────────

    def _phase1_reaction(
        self, client, model: str, user_input: str, token_callback
    ) -> str:
        """Phase 1: 即时情感反应 — 简短、有性格的回应 + 委派意识。"""
        env_summary = self._get_environment_summary()
        thought_ctx = self._get_thought_context()
        thought_injection = self._build_thought_injection(thought_ctx)
        messages = [
            {
                "role": "system",
                "content": (
                    self._get_persona_prompt()
                    + f"\n\n当前环境状态: {env_summary}"
                    + thought_injection
                    + "\n\n用户发来了一个请求。你需要："
                    "\n1. 用 1-2 句话表达你的即时反应（保持人格）"
                    "\n2. 判断这个请求是否需要执行任务（分析代码、读写文件、查询信息等）"
                    "\n3. 如果需要，明确说'让我来看看'或'交给我来处理'"
                    "\n\n注意：你有能力处理各种具体操作。"
                    "\n你的角色是理解用户意图、决定是否需要行动、然后执行。"
                    "\n\n【重要：当前阶段行为约束】"
                    "\n你在当前对话阶段【不能直接执行操作】。"
                    "\n- 如果用户要求具体操作，表达态度即可（如'交给我吧'、'让我来看看'），后续阶段会真正执行"
                    "\n- 绝对不要说'已经帮你做了'、'点好了'、'改完了'等暗示操作已完成的话——你现在还没有执行任何操作"
                ),
            },
            {"role": "user", "content": user_input},
        ]
        return self._stream_openai(client, model, messages, token_callback)

    # ────────────────────────────────
    # Phase 3: 结果总结
    # ────────────────────────────────

    def _phase3_summary(
        self, client, model: str, user_input: str, team_result, token_callback
    ) -> str:
        """Phase 3: 结果总结 — 按任务复杂度分级汇报。"""
        result_preview = team_result.task_result[:800] if team_result.task_result else ""
        if len(team_result.task_result or "") > 800:
            result_preview += "..."

        tool_count = team_result.tool_calls_made
        skill_created = team_result.skill_created or ""

        thought_ctx = self._get_thought_context()
        thought_injection = self._build_thought_injection(thought_ctx)

        # 按复杂度分级汇报策略
        if tool_count <= 3:
            report_style = "用1句话简短说明完成了什么，像随手帮个忙一样轻松。"
            max_tokens_val = 100
        elif tool_count <= 10:
            report_style = "用2-3句话说明做了什么和结果，像完成了一个小项目回来汇报。"
            max_tokens_val = 200
        else:
            report_style = ("这是个复杂任务，用3-5句话汇报：\n"
                           "1. 概述做了什么\n"
                           "2. 关键变更点\n"
                           "3. 如果有需要用户确认或注意的地方，主动提出")
            max_tokens_val = 350

        # 处理失败/部分成功
        success_hint = ""
        if not team_result.success:
            success_hint = "\n\n注意：任务执行遇到了问题。请诚实告知用户哪里出了问题，并建议下一步怎么办。不要掩饰失败。"
        elif hasattr(team_result, 'warnings') and team_result.warnings:
            success_hint = "\n\n注意：任务完成了但有一些警告，酌情提醒用户。"

        # 技能创建提示
        skill_hint = ""
        if skill_created:
            skill_hint = f"\n你还学到了一个新技巧（{skill_created}），可以自然地提一嘴。"

        messages = [
            {
                "role": "system",
                "content": (
                    self._get_persona_prompt()
                    + thought_injection
                    + f"\n\n你刚完成了任务。执行信息:"
                    f"\n- 工具调用次数: {tool_count}"
                    f"\n- 任务规模: {'简单' if tool_count <= 3 else '中等' if tool_count <= 10 else '复杂'}"
                    f"\n\n汇报要求: {report_style}"
                    f"{success_hint}{skill_hint}"
                    "\n\n风格要求：像刚做完一件事回来跟朋友说，语气自然连贯，"
                    "不要用'任务完成'这种公文风格开头。"
                    "\n\n【真实性底线】你必须且只能基于上面的「任务结果摘要」来回复。"
                    "\n- 结果摘要里写了什么，你就说什么；没写的内容绝对不能编造"
                    "\n- 如果结果摘要为空或信息不足，直接说'没拿到具体结果'，不要凭空描述操作细节"
                    "\n- 禁止使用'我看到了...'、'截图显示...'等措辞，除非结果摘要中确实包含相关描述"
                ),
            },
            {
                "role": "user",
                "content": f"用户原始请求：{user_input}\n\n任务结果摘要：{result_preview}",
            },
        ]
        return self._stream_openai(client, model, messages, token_callback, max_tokens=max_tokens_val)

    # ────────────────────────────────
    # 通用工具方法
    # ────────────────────────────────

    def _stream_openai(
        self, client, model: str, messages: list[dict], token_callback, use_reasoning=False, max_tokens=200
    ) -> str:
        """通用流式 OpenAI 调用，通过 token_callback 推送内容。

        使用独立消息列表，不复用 agent.history，避免污染主对话历史。

        Args:
            use_reasoning: 是否启用推理模式。默认 False，伴侣层不使用推理。
            max_tokens: 最大生成 token 数，默认 200。
        """
        try:
            # 构建 API 调用参数
            kwargs = {
                "model": model,
                "messages": messages,
                "stream": True,
                "max_tokens": max_tokens,
            }
            # 仅在调用方明确要求时才启用推理
            if use_reasoning and ENABLE_REASONING:
                kwargs["reasoning_effort"] = "low"
            else:
                # DeepSeek 模型默认启用思考模式，必须显式禁用
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            stream = client.chat.completions.create(**kwargs)

            accumulated = ""
            # 内部控制标记列表
            _CONTROL_TAGS = ["[EXECUTE]", "[NO_EXECUTE]", "[CHAT]", "[ANSWER]", "[CLARIFY]", "[CHANGE_DETECTED]"]
            _buffer = ""  # 缓冲区：暂存可能包含控制标记的内容

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # 处理思考内容（reasoning mode 下先返回 reasoning_content）
                reasoning = getattr(delta, 'reasoning_content', None)
                if reasoning:
                    if token_callback:
                        token_callback("reasoning", reasoning)
                    continue  # 思考内容不计入最终回答文本

                # 处理回答内容
                if delta.content:
                    accumulated += delta.content
                    _buffer += delta.content

                    if token_callback:
                        # 先尝试移除缓冲区中所有完整的控制标记
                        cleaned = _buffer
                        for tag in _CONTROL_TAGS:
                            cleaned = cleaned.replace(tag, "")

                        # 检查缓冲区末尾是否可能是某个控制标记的前缀
                        has_partial = False
                        for tag in _CONTROL_TAGS:
                            for i in range(1, len(tag)):
                                if _buffer.endswith(tag[:i]):
                                    has_partial = True
                                    break
                            if has_partial:
                                break

                        if has_partial:
                            # 可能正在接收控制标记，继续缓冲，等待后续 token
                            continue
                        else:
                            # 没有部分匹配，安全推送清理后的内容
                            if cleaned:
                                token_callback("content", cleaned)
                            _buffer = ""

            # 流结束后，推送缓冲区中剩余的非标记内容
            if _buffer and token_callback:
                cleaned = _buffer
                for tag in _CONTROL_TAGS:
                    cleaned = cleaned.replace(tag, "")
                if cleaned:
                    token_callback("content", cleaned)

            # 段落间加分隔，视觉上与 Agent 执行结果区分
            if accumulated and token_callback:
                token_callback("content", "\n\n")

            return accumulated

        except Exception as e:
            print(f"[CompanionAgent] 流式调用失败: {e}")
            return ""

    def _get_thought_context(self, max_chars: int = 500) -> str:
        """从认知引擎获取近期思维摘要（未说出口的内心独白）"""
        try:
            if not self.cognitive_engine or not self.cognitive_engine.buffer:
                return ""
            raw = self.cognitive_engine.buffer.get_context_for_next_thought()
            if not raw or not raw.strip():
                return ""
            # 截断过长内容
            if len(raw) > max_chars:
                raw = raw[:max_chars] + "..."
            return raw
        except Exception:
            return ""

    def _get_episodic_context(self, max_chars: int = 300) -> str:
        """从情景记忆获取最近事件摘要"""
        try:
            if not self.cognitive_engine:
                return ""
            episodic = self.cognitive_engine.episodic
            if not episodic:
                return ""
            return episodic.get_recent_summary(max_items=5)[:max_chars]
        except Exception:
            return ""

    def _build_thought_injection(self, thought_ctx: str) -> str:
        """构建思维注入文本段（统一模板）"""
        if not thought_ctx:
            injection = ""
        else:
            injection = (
                "\n\n【你的内心独白（未说出口）】以下是你最近想过但还没有对用户说的内容，"
                "你可以基于这些想法自然地展开话题，但不要说'我之前跟你说过'——因为你并没有说出口。\n"
                f"{thought_ctx}"
            )

        # 追加情景记忆上下文
        episodic_ctx = self._get_episodic_context(max_chars=300)
        if episodic_ctx:
            injection += f"\n\n【最近发生的事（情景记忆）】\n{episodic_ctx}"

        return injection

    def _get_companion_model(self) -> str:
        """获取伴侣层使用的模型名。

        优先级：COMPANION_MODEL 环境变量 > agent.current_model > MODEL 默认值
        """
        if COMPANION_MODEL:
            return COMPANION_MODEL
        return self.agent.current_model or MODEL or ""

    def _is_companion_vlm(self) -> bool:
        """判断当前伴侣AI模型是否为视觉模型（VLM）。

        读取 model_providers.json，查找当前 companion_model 的 type 字段。
        结果缓存到实例属性避免重复文件读取。
        """
        model_name = self.companion_model
        if model_name in self._vlm_cache:
            return self._vlm_cache[model_name]

        is_vlm = False
        try:
            config_path = AI_CONFIG_DIR / "model_providers.json"
            with open(config_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            for provider in data.get("providers", []):
                for model in provider.get("models", []):
                    if model.get("name") == model_name:
                        is_vlm = model.get("type") == "vlm"
                        break
                if is_vlm:
                    break
        except Exception as e:
            print(f"[CompanionAgent] 读取 model_providers.json 失败: {e}")

        self._vlm_cache[model_name] = is_vlm
        return is_vlm

    def _get_environment_summary(self) -> str:
        """快速感知当前环境状态（图谱/记忆/技能）。

        注入到 Phase 1 系统提示词中，让 CompanionAgent 知道可用资源。
        """
        parts = []

        # 检查知识图谱
        try:
            from code_demo.core.tools.native.graph_tools import _find_graph_json
            graph_path = _find_graph_json()
            if graph_path:
                from graphify.serve import _load_graph
                G = _load_graph(str(graph_path))
                parts.append(f"知识图谱: {G.number_of_nodes()} 个节点")
            else:
                parts.append("知识图谱: 未构建")
        except Exception:
            parts.append("知识图谱: 不可用")

        # 检查记忆数量
        if hasattr(self.agent, "memory_manager") and self.agent.memory_manager:
            mem_count = len(getattr(self.agent.memory_manager, "memories", {}))
            parts.append(f"记忆: {mem_count} 条")

        # 检查技能数量
        if hasattr(self.agent, "skill_registry") and self.agent.skill_registry:
            skill_count = len(getattr(self.agent.skill_registry, "documents", {}))
            parts.append(f"技能: {skill_count} 个")

        return " | ".join(parts) if parts else "环境信息不可用"
