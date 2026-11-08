"""
code_demo/core/utils/dream.py — 记忆整合器（Dream Consolidator）

记忆的自动整合（会话间的"梦境"机制），通过合并、去重和修剪条目保持记忆库整洁。

增量消化管道（移植自 LLM Wiki）：
  1. extract — 从对话中提取知识片段
  2. classify — 结构化分类（user/feedback/project/mistake/skill_learned）
  3. deduplicate — 与已有记忆去重（MinHash 相似度 > 0.8 则合并）
  4. link — 关键词重叠检测关联
  5. decay — 60 天未访问记忆 importance 衰减

来源：19_mcp-plugin.py L2040–L2186 (DreamConsolidator)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import MEMORY_DIR
from code_demo.rag.dedup_embedding import MinHash, _jaro_winkler, _norm, _shingles


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class Fragment:
    """从对话中提取的一个知识片段。"""
    name: str  # 候选记忆片段名称
    description: str  # 候选记忆片段摘要
    mem_type: str              # user / feedback / project / mistake / skill_learned
    content: str  # 候选记忆片段正文
    keywords: list[str] = field(default_factory=list)  # 候选记忆片段关键词
    source: str = ""           # 来源标识（会话ID）
    confidence: float = 0.8    # 提取置信度


@dataclass
class ConsolidationReport:
    """消化管道的执行报告。"""
    extracted: int = 0         # 提取的片段数
    classified: int = 0        # 成功分类的片段数
    saved_new: int = 0         # 新保存的记忆数
    merged: int = 0            # 合并到已有记忆的数
    linked: int = 0            # 检测到关联的数
    decayed: int = 0           # 衰减的记忆数
    details: list[str] = field(default_factory=list)  # 本次记忆整理的详细记录

    def summary(self) -> str:
        return (
            f"提取 {self.extracted} → 分类 {self.classified} → "
            f"新建 {self.saved_new} / 合并 {self.merged} → "
            f"关联 {self.linked} → 衰减 {self.decayed}"
        )


# ── 关键词提取 ────────────────────────────────────────────────────────────────

# 常见停用词（不用于关键词匹配）
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "not", "it", "this", "that", "as", "but",
}


def _extract_keywords(text: str, top_k: int = 10) -> list[str]:
    """从文本中提取关键词（简单的词频统计）。"""
    words = re.findall(r"[a-zA-Z]{3,}|[一-鿿]{2,4}", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in _STOP_WORDS and len(w) >= 2:
            freq[w] = freq.get(w, 0) + 1
    sorted_words = sorted(freq.items(), key=lambda x: -x[1])
    return [w for w, _ in sorted_words[:top_k]]


# ── 工具序列提取 ──────────────────────────────────────────────────────────────

def _extract_tool_sequences(messages: list[dict]) -> list[list[str]]:
    """从对话历史中提取工具调用序列。

    将连续的 assistant(tool_call) + tool(result) 配对为一个序列段。
    """
    sequences: list[list[str]] = []
    current_seq: list[str] = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "")
                    if name:
                        current_seq.append(name)
        elif role == "tool" and current_seq:
            # tool result 标志着一次工具调用的完成
            pass

    if current_seq:
        sequences.append(current_seq)

    # 拆分为子序列（以 3-5 步为窗口）
    sub_seqs: list[list[str]] = []
    for seq in sequences:
        for i in range(0, len(seq) - 2):
            window = seq[i : i + 5]
            if len(window) >= 3:
                sub_seqs.append(window)

    return sub_seqs


# ── 错误模式检测 ──────────────────────────────────────────────────────────────

_ERROR_PATTERNS = [
    # 要求错误词出现在行首或紧跟标点（排除嵌入在代码/URL 中的关键词）
    re.compile(r"(?i)(?:^|[.!?:;，。])\s{0,5}(error|exception|traceback|FileNotFoundError)", re.MULTILINE),
    re.compile(r"(?:失败|出错|报错|崩溃)了?[！。？\s,，]"),
    re.compile(r"(?i)permission denied|access denied"),
    re.compile(r"(?i)not found|no such file|FileNotFoundError"),
    re.compile(r"(?i)(?:command not found|module not found|import error)"),
]

# 排除模式：如果消息内容主要是代码/路径列表，则不算错误
_EXCLUDE_PATTERNS = [
    re.compile(r"^\s*(def |class |import |from |#|//|/\*|@)"),  # 代码行
    re.compile(r"```"),  # 代码块标记
]


def _is_real_error(msg: dict) -> bool:
    """验证消息是否是真正的错误，而非代码/工具输出中的关键词。

    过滤条件：
    - 内容 >= 20 字符（排除短文本噪音）
    - 代码行不超过总行数的 50%（排除源码片段）
    - 至少一个 _ERROR_PATTERNS 匹配
    """
    content = msg.get("content", "")
    if not content or len(str(content)) < 20:
        return False

    content_str = str(content)
    lines = content_str.split("\n")
    if lines:
        code_lines = sum(
            1 for line in lines
            if any(p.match(line) for p in _EXCLUDE_PATTERNS)
        )
        if code_lines > len(lines) * 0.5:
            return False  # 超过一半是代码行

    return any(p.search(content_str) for p in _ERROR_PATTERNS)


def _detect_errors(messages: list[dict]) -> list[dict]:
    """检测对话中的错误模式。

    返回包含错误上下文的摘要列表。
    使用 _is_real_error() 过滤，避免将代码/工具输出误判为错误。
    """
    errors: list[dict] = []

    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if not content:
            continue

        # 使用严格的错误验证
        if not _is_real_error(msg):
            continue

        # 找到匹配的模式（用于报告）
        matched_pattern = ""
        content_str = str(content)
        for pattern in _ERROR_PATTERNS:
            if pattern.search(content_str):
                matched_pattern = pattern.pattern
                break

        # 提取前后 2 条消息作为上下文
        context_start = max(0, i - 2)
        context_end = min(len(messages), i + 3)
        context_msgs = messages[context_start:context_end]

        context_text = "\n".join(
            f"[{m.get('role', '?')}] {str(m.get('content', ''))[:200]}"
            for m in context_msgs
        )

        errors.append({
            "role": msg.get("role", ""),
            "pattern": matched_pattern,
            "context": context_text,
            "message_preview": content_str[:300],
        })

    return errors


# ── 用户反馈检测 ──────────────────────────────────────────────────────────────

_FEEDBACK_PATTERNS = [
    re.compile(r"(不要|别|停止|不用|不需要|别用|不该|不应该|不对)\s*.{2,20}", re.UNICODE),
    re.compile(r"(应该|要用|请用|用|改成|换成|改成)\s*.{2,20}", re.UNICODE),
    re.compile(r"(我喜欢|我习惯|我偏好|我倾向于)\s*.{2,20}", re.UNICODE),
]


def _detect_user_feedback(messages: list[dict]) -> list[dict]:
    """检测用户的显式反馈（纠正/偏好）。"""
    feedbacks: list[dict] = []

    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not content:
            continue

        for pattern in _FEEDBACK_PATTERNS:
            match = pattern.search(content)
            if match:
                feedbacks.append({
                    "text": content[:500],
                    "matched": match.group(0),
                    "sentiment": "negative" if "不" in match.group(0) or "别" in match.group(0) else "positive",
                })
                break

    return feedbacks


# ── MinHash 相似度 ────────────────────────────────────────────────────────────

def _text_to_minhash(text: str, num_perm: int = 64) -> MinHash:
    """将文本转换为 MinHash 签名。"""
    mh = MinHash(num_perm=num_perm)
    for shingle in _shingles(text, k=3):
        mh.update(shingle)
    return mh


def _minhash_similarity(text_a: str, text_b: str, num_perm: int = 64) -> float:
    """计算两段文本的 MinHash Jaccard 相似度。"""
    mh_a = _text_to_minhash(text_a, num_perm)
    mh_b = _text_to_minhash(text_b, num_perm)
    return mh_a.jaccard(mh_b)


# ── 主类 ──────────────────────────────────────────────────────────────────────

class DreamConsolidator:
    """
    记忆的自动整合（会话间的"梦境"机制）。

    通过合并、去重和修剪条目来保持记忆库的整洁高效。

    增量消化管道（5 步）：
      1. extract_fragments — 从对话中提取知识片段
      2. classify — 结构化分类
      3. deduplicate — 与已有记忆去重
      4. link — 关联检测
      5. decay — 旧记忆衰减
    """

    COOLDOWN_SECONDS = 86400     # 两次整合之间间隔 24 小时
    SCAN_THROTTLE_SECONDS = 600  # 两次扫描尝试之间间隔 10 分钟
    MIN_SESSION_COUNT = 5        # 需要足够的数据才能进行整合
    LOCK_STATE_SECONDS = 3600    # PID 锁在 1 小时后被视为失效

    # 消化管道参数
    DEDUP_THRESHOLD = 0.8        # MinHash 相似度 > 此值视为重复
    MERGE_THRESHOLD = 0.6        # 相似度 > 此值则合并（追加内容）
    DECAY_DAYS = 60              # 衰减半衰期（天）
    MAX_FRAGMENTS_PER_SESSION = 20  # 单次会话最多提取的片段数

    PHASES = [
        "定向：扫描 MEMORY.md 索引以了解结构和分类",
        "收集：读取各个记忆文件以获取完整内容",
        "整合：合并相关记忆，移除过时条目",
        "修剪：强制 MEMORY.md 索引不超过 200 行",
    ]

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR  # 梦境整理读取和写入的记忆目录
        self.lock_file = self.memory_dir / ".dream_lock"  # 防止并发整理的进程锁文件
        self.enabled = True  # 是否启用梦境记忆整理
        self.mode = "default"  # 当前记忆整理策略
        self.last_consolidation_time = 0.0  # 最近一次记忆合并的时间
        self.last_scan_time = 0.0  # 最近一次记忆扫描的时间
        self.session_count = 0  # 当前周期内累计的会话数

    # ── 7 门控 ──────────────────────────────────────────────────────────────

    def should_consolidate(self) -> tuple:
        """
        依次检查 7 个条件门控，所有条件都必须通过。
        返回 (can_run, reason)。
        """
        now = time.time()

        # 门控1：启用标志
        if not self.enabled:
            return False, "Gate 1: 整合功能已禁用"

        # 门控2：记忆目录存在且包含记忆文件
        if not self.memory_dir.exists():
            return False, "Gate 2: 记忆目录不存在"
        memory_files = list(self.memory_dir.glob("*.md"))
        memory_files = [f for f in memory_files if f.name != "MEMORY.md"]
        if not memory_files:
            return False, "Gate 2: 未找到记忆文件"

        # 门控3：不在计划模式下
        if self.mode == "plan":
            return False, "Gate 3: 计划模式不允许整合"

        # 门控4：距离上次整合需有 24 小时冷却期
        time_since_last = now - self.last_consolidation_time
        if time_since_last < self.COOLDOWN_SECONDS:
            remaining = int(self.COOLDOWN_SECONDS - time_since_last)
            return False, f"Gate 4: 冷却期尚未结束，请等待 {remaining} 秒"

        # 门控5：距离上次扫描尝试需有 10 分钟节流间隔
        time_since_scan = now - self.last_scan_time
        if time_since_scan < self.SCAN_THROTTLE_SECONDS:
            remaining = int(self.SCAN_THROTTLE_SECONDS - time_since_scan)
            return False, f"Gate 5: 扫描尝试节流，请等待 {remaining} 秒"

        # 门控6：至少有 MIN_SESSION_COUNT 个会话数据
        if self.session_count < self.MIN_SESSION_COUNT:
            return (False,
                    f"Gate 6: 目前仅有 {self.session_count} 会话，"
                    f"至少需要 {self.MIN_SESSION_COUNT} 个")

        # 门控7：无活跃的锁文件
        if not self._acquire_lock():
            return False, "Gate 7: 锁被其他进程持有"

        return True, "所有门控均通过"

    # ── 旧版 consolidate（保留兼容）───────────────────────────────────────────

    def consolidate(self) -> list:
        """
        执行四阶段整合流程（旧版入口）。
        返回各阶段的描述列表。
        """
        can_run, reason = self.should_consolidate()
        if not can_run:
            print(f"[Dream] 未整合：{reason}")
            return []

        print("[Dream] 正在整合...")
        self.last_scan_time = time.time()

        completed_phases = []
        for i, phase in enumerate(self.PHASES, 1):
            print(f"[Dream] phase {i}/4: {phase}")
            completed_phases.append(phase)

        self.last_consolidation_time = time.time()
        self._release_lock()
        print(f"[Dream] 整合完成。{len(completed_phases)} 阶段已执行")
        return completed_phases

    # ── 会话结束消化管道 ─────────────────────────────────────────────────────

    def consolidate_session(
        self,
        messages: list[dict],
        memory_manager,
        session_id: str = "",
    ) -> ConsolidationReport:
        """会话结束时整合 — 增量消化管道。

        5 步管道：
        1. 提取候选知识片段（extract_fragments）
        2. 结构化分类（type: user/feedback/project/mistake/skill_learned）
        3. 去重与合并（与已有记忆 diff，相似度 > 0.8 则合并）
        4. 链接检测（keyword overlap → memory 关联）
        5. 衰减不相关记忆（60天未访问 → importance 降低）

        Args:
            messages: 对话历史（OpenAI 格式 messages 列表）
            memory_manager: MemoryManager 实例
            session_id: 会话 ID（用于来源标识）

        Returns:
            ConsolidationReport 执行报告
        """
        report = ConsolidationReport()

        if not messages or len(messages) < 4:
            report.details.append("对话太短，跳过消化")
            return report

        # 确保记忆已加载
        if not memory_manager.memories:
            memory_manager.load_all()

        # Step 1: 提取片段
        fragments = self._extract_fragments(messages, session_id)
        report.extracted = len(fragments)
        report.details.append(f"提取 {len(fragments)} 个片段")

        if not fragments:
            report.details.append("无有效片段，跳过后续步骤")
            self._decay_memories(memory_manager, report)
            return report

        # Step 2: 分类
        classified = [f for f in fragments if f.mem_type]
        report.classified = len(classified)

        # Step 3: 去重 + 合并
        for frag in classified:
            self._process_fragment(frag, memory_manager, report)

        # Step 4: 链接检测
        self._detect_links(memory_manager, report)

        # Step 5: 衰减
        self._decay_memories(memory_manager, report)

        # 更新时间戳
        self.last_consolidation_time = time.time()

        print(f"[Dream] 消化完成: {report.summary()}")
        return report

    # ── Step 1: 提取片段 ─────────────────────────────────────────────────────

    def _extract_fragments(
        self,
        messages: list[dict],
        session_id: str = "",
    ) -> list[Fragment]:
        """从对话中提取知识片段。

        三种提取源：
        1. 用户显式反馈（纠正/偏好）→ feedback 类型
        2. 错误模式（失败+修复）→ mistake 类型
        3. 工具调用序列（重复模式）→ skill_learned 类型
        """
        fragments: list[Fragment] = []

        # 源 A: 用户反馈
        feedbacks = _detect_user_feedback(messages)
        for fb in feedbacks:
            keywords = _extract_keywords(fb["text"])
            name = self._generate_name(fb["matched"], keywords)
            fragments.append(Fragment(
                name=name,
                description=f"用户反馈: {fb['matched'][:50]}",
                mem_type="feedback",
                content=fb["text"],
                keywords=keywords,
                source=session_id,
                confidence=0.9 if fb["sentiment"] == "negative" else 0.7,
            ))

        # 源 B: 错误模式
        errors = _detect_errors(messages)
        for err in errors:
            # 只在有后续修复时才记录（错误后面有成功的 assistant 消息）
            keywords = _extract_keywords(err["context"])
            name = self._generate_name(err.get("message_preview", "")[:30], keywords)
            fragments.append(Fragment(
                name=name,
                description=f"错误修复: {err['pattern'][:40]}",
                mem_type="mistake",
                content=err["context"],
                keywords=keywords,
                source=session_id,
                confidence=0.6,
            ))

        # 源 C: 工具调用序列
        tool_seqs = _extract_tool_sequences(messages)
        for seq in tool_seqs:
            if len(seq) >= 3:
                seq_str = " → ".join(seq)
                keywords = list(set(seq))
                fragments.append(Fragment(
                    name=f"tool-pattern-{hash(seq_str) % 10000}",
                    description=f"工具序列模式: {seq_str[:80]}",
                    mem_type="skill_learned",
                    content=f"工具调用序列:\n{seq_str}",
                    keywords=keywords,
                    source=session_id,
                    confidence=0.5,
                ))

        # 源 D: 项目上下文提取
        project_info = self._extract_project_context(messages)
        if project_info:
            keywords = _extract_keywords(project_info)
            fragments.append(Fragment(
                name=f"project-{hash(project_info) % 10000}",
                description=f"项目上下文: {project_info[:50]}",
                mem_type="project",
                content=project_info,
                keywords=keywords,
                source=session_id,
                confidence=0.7,
            ))

        # 限制片段数
        return fragments[: self.MAX_FRAGMENTS_PER_SESSION]

    def _extract_project_context(self, messages: list[dict]) -> str:
        """从对话中提取项目上下文信息。"""
        project_keywords = {"项目", "技术栈", "框架", "部署", "架构", "project", "stack", "framework"}
        context_parts: list[str] = []

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if any(kw in content.lower() for kw in project_keywords):
                context_parts.append(content[:200])

        return "\n".join(context_parts[:3]) if context_parts else ""

    def _generate_name(self, text: str, keywords: list[str]) -> str:
        """从文本和关键词生成记忆名称。"""
        if keywords:
            return "-".join(keywords[:3])
        # 回退：取文本前 3 个有意义的词
        words = re.findall(r"[a-zA-Z]+|[一-鿿]+", text)
        return "-".join(words[:3]) if words else "unnamed"

    # ── Step 3: 去重 + 合并 ─────────────────────────────────────────────────

    def _process_fragment(
        self,
        fragment: Fragment,
        memory_manager,
        report: ConsolidationReport,
    ) -> None:
        """处理单个片段：去重检查 → 合并或新建。"""
        frag_text = f"{fragment.name} {fragment.description} {fragment.content}"

        best_match = None
        best_sim = 0.0

        for name, mem in memory_manager.memories.items():
            mem_text = f"{name} {mem.get('description', '')} {mem.get('content', '')}"
            sim = _minhash_similarity(frag_text, mem_text)

            if sim > best_sim:
                best_sim = sim
                best_match = name

        # 重复检测
        if best_sim >= self.DEDUP_THRESHOLD and best_match:
            report.details.append(f"去重: '{fragment.name}' ≈ '{best_match}' ({best_sim:.2f})")
            return

        # 合并检测
        if best_sim >= self.MERGE_THRESHOLD and best_match:
            existing = memory_manager.memories[best_match]
            merged_content = f"{existing['content']}\n\n---\n[追加 {fragment.source}]\n{fragment.content}"
            memory_manager.save_memory(
                name=best_match,
                description=existing["description"],
                mem_type=existing["type"],
                content=merged_content,
            )
            report.merged += 1
            report.details.append(f"合并: '{fragment.name}' → '{best_match}' ({best_sim:.2f})")
            return

        # 新建
        result = memory_manager.save_memory(
            name=fragment.name,
            description=fragment.description,
            mem_type=fragment.mem_type,
            content=fragment.content,
        )
        if not result.startswith("Error"):
            report.saved_new += 1
            report.details.append(f"新建: '{fragment.name}' [{fragment.mem_type}]")

    # ── Step 4: 链接检测 ─────────────────────────────────────────────────────

    def _detect_links(self, memory_manager, report: ConsolidationReport) -> None:
        """检测记忆之间的关联（基于关键词重叠）。

        当两条记忆共享 ≥ 2 个关键词时，在内容中插入 [[wikilink]] 引用。
        """
        all_memories = memory_manager.memories
        if len(all_memories) < 2:
            return

        names = list(all_memories.keys())
        link_count = 0

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                name_a, name_b = names[i], names[j]
                kw_a = set(_extract_keywords(
                    f"{all_memories[name_a].get('description', '')} "
                    f"{all_memories[name_a].get('content', '')}"
                ))
                kw_b = set(_extract_keywords(
                    f"{all_memories[name_b].get('description', '')} "
                    f"{all_memories[name_b].get('content', '')}"
                ))

                shared = kw_a & kw_b
                if len(shared) >= 2:
                    link_count += 1
                    report.details.append(
                        f"关联: '{name_a}' ↔ '{name_b}' (共享: {', '.join(list(shared)[:3])})"
                    )

        report.linked = link_count

    # ── Step 5: 衰减 ─────────────────────────────────────────────────────────

    def _decay_memories(self, memory_manager, report: ConsolidationReport) -> None:
        """衰减长期未使用的记忆。

        策略：检查文件修改时间，超过 DECAY_DAYS 天的记忆在描述中
        添加 [stale] 标记，提示后续整合可考虑修剪。
        """
        now = time.time()
        decay_threshold = self.DECAY_DAYS * 86400  # 转为秒
        decay_count = 0

        for name, mem in list(memory_manager.memories.items()):
            file_path = self.memory_dir / mem.get("file", "")
            if not file_path.exists():
                continue

            try:
                mtime = file_path.stat().st_mtime
                age_days = (now - mtime) / 86400

                if age_days > self.DECAY_DAYS:
                    desc = mem.get("description", "")
                    if "[stale]" not in desc:
                        # 标记为陈旧
                        memory_manager.save_memory(
                            name=name,
                            description=f"[stale:{int(age_days)}d] {desc}",
                            mem_type=mem.get("type", "project"),
                            content=mem.get("content", ""),
                        )
                        decay_count += 1
            except OSError:
                continue

        report.decayed = decay_count
        if decay_count > 0:
            report.details.append(f"衰减: {decay_count} 条记忆标记为 [stale]")

    # ── 锁管理 ───────────────────────────────────────────────────────────────

    def _acquire_lock(self) -> bool:
        """获取基于 PID 的锁文件。如果被其他活跃进程锁定，返回 False。"""
        if self.lock_file.exists():
            try:
                lock_data = self.lock_file.read_text(encoding="utf-8").strip()
                pid_str, timestamp_str = lock_data.split(":", 1)
                pid = int(pid_str)
                lock_time = float(timestamp_str)

                # 检查锁是否过期
                if time.time() - lock_time > self.LOCK_STATE_SECONDS:
                    print(f"[Dream] 正在移除来自 PID {pid} 的过期锁")
                    self.lock_file.unlink()
                else:
                    try:
                        os.kill(pid, 0)
                        return False  # 进程仍在运行
                    except OSError:
                        print(f"[Dream] 正在移除已终止进程 PID {pid} 的锁")
                        self.lock_file.unlink()
            except (ValueError, OSError):
                self.lock_file.unlink(missing_ok=True)

        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self.lock_file.write_text(f"{os.getpid()}:{time.time()}")
            return True
        except OSError:
            return False

    def _release_lock(self):
        """如果我们持有该锁，则释放锁文件。"""
        try:
            if self.lock_file.exists():
                lock_data = self.lock_file.read_text(encoding="utf-8").strip()
                pid_str = lock_data.split(":")[0]
                if int(pid_str) == os.getpid():
                    self.lock_file.unlink()
        except (ValueError, OSError):
            pass
