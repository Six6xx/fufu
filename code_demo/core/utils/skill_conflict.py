"""
code-demo/core/utils/skill_conflict.py — 冲突检测与合并引擎

检测普通 skills 与内部 skills 的内容冲突，执行合并生成"主动学习 skill"，
将被合并的普通 skill 下线（移动到 __retired/）。

三种冲突检测策略：
  - keyword_overlap: 关键词重叠度（jieba 分词 + 集合交集/并集）
  - instruction_conflict: 指令矛盾检测（正则模式匹配）
  - tool_sequence_overlap: 工具调用序列覆盖度
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from code_demo.core.config import (
    CONFLICT_THRESHOLD_KEYWORD,
    CONFLICT_THRESHOLD_TOOL_SEQ,
    LEARNED_SKILLS_DIR,
    RETIRED_SKILLS_DIR,
    SKILLS_DIR,
)

if TYPE_CHECKING:
    from code_demo.core.utils.skill_registry import SkillRegistry

# ── jieba 可选依赖 ─────────────────────────────────────────────────────────

try:
    import jieba

    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass
class ConflictResult:
    """冲突检测结果。"""

    internal_skill: str  # 内部 skill 名称
    conflicting_skill: str  # 冲突的普通 skill 名称
    conflict_type: str  # keyword_overlap | instruction_conflict | tool_sequence_overlap
    overlap_score: float  # 冲突分数 0-1
    conflict_fields: list  # 具体冲突的内容片段描述


@dataclass
class MergeResult:
    """合并结果。"""

    new_name: str  # 新 skill 名称 (learned-xxx)
    new_body: str  # 合并后的正文
    new_description: str  # 描述
    lineage: list  # 谱系记录
    retired_skills: list  # 被下线的 skill 名称列表
    new_source: str = "merged"  # 合并后的 source 值


# ── 分词辅助 ────────────────────────────────────────────────────────────────


def _tokenize(text: str) -> set[str]:
    """对文本分词，返回词集合。

    jieba 可用时使用 jieba 分词，否则 fallback 到字符级 3-gram。
    """
    if _JIEBA_AVAILABLE:
        words = jieba.lcut(text)
        # 过滤空白和单字符
        return {w.strip().lower() for w in words if len(w.strip()) > 1}
    # fallback: 字符级 3-gram
    chars = text.replace(" ", "").replace("\n", "").lower()
    return {chars[i : i + 3] for i in range(len(chars) - 2) if len(chars) >= 3}


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    """字符级 n-gram 集合（用于 fallback 相似度）。"""
    chars = text.replace(" ", "").replace("\n", "").lower()
    return {chars[i : i + n] for i in range(len(chars) - n + 1) if len(chars) >= n}


# ── 指令矛盾正则 ───────────────────────────────────────────────────────────

_MANDATORY_PATTERNS = [
    re.compile(r"(必须|总是|优先|务必|一定要|千万|务必|强制|shall|must|always|priority)", re.I),
    re.compile(r"(不要|禁止|避免|切勿|不可|不能|严禁|never|must not|avoid|forbidden|do not)", re.I),
]

_TOOL_CALL_PATTERN = re.compile(r"`(\w+)\s*\(|(\w+)\s*\(|graph_query|read_file|analyze_file|write_file|edit_file|bash|search_code|grep|glob")


# ── SkillConflictDetector ───────────────────────────────────────────────────


class SkillConflictDetector:
    """检测普通 skills 与内部 skills 之间的冲突。"""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry  # 用于读取和更新技能定义的注册表

    # ── 公开接口 ────────────────────────────────────────────────────────────

    def detect_conflicts(self) -> list[ConflictResult]:
        """检测所有内部 skills 与普通 active skills 之间的冲突。

        Returns:
            ConflictResult 列表，按 overlap_score 降序
        """
        internal_skills = self._get_internal_skills()
        active_skills = self._get_active_visible_skills()

        if not internal_skills or not active_skills:
            return []

        results: list[ConflictResult] = []

        for int_name, int_body in internal_skills:
            for act_name, act_body in active_skills:
                # 检测三种冲突类型
                kw_score = self._keyword_overlap(int_body, act_body)
                instr_conflicts = self._instruction_conflict(int_body, act_body)
                tool_score = self._tool_sequence_overlap(int_body, act_body)

                # 关键词重叠
                if kw_score >= CONFLICT_THRESHOLD_KEYWORD:
                    results.append(
                        ConflictResult(
                            internal_skill=int_name,
                            conflicting_skill=act_name,
                            conflict_type="keyword_overlap",
                            overlap_score=kw_score,
                            conflict_fields=self._extract_keyword_conflicts(int_body, act_body),
                        )
                    )

                # 指令矛盾
                if instr_conflicts:
                    results.append(
                        ConflictResult(
                            internal_skill=int_name,
                            conflicting_skill=act_name,
                            conflict_type="instruction_conflict",
                            overlap_score=1.0,  # 矛盾存在即标记为满分
                            conflict_fields=instr_conflicts,
                        )
                    )

                # 工具序列覆盖
                if tool_score >= CONFLICT_THRESHOLD_TOOL_SEQ:
                    results.append(
                        ConflictResult(
                            internal_skill=int_name,
                            conflicting_skill=act_name,
                            conflict_type="tool_sequence_overlap",
                            overlap_score=tool_score,
                            conflict_fields=self._extract_tool_conflicts(int_body, act_body),
                        )
                    )

        # 按 overlap_score 降序
        results.sort(key=lambda r: -r.overlap_score)
        return results

    # ── 检测策略 ────────────────────────────────────────────────────────────

    def _keyword_overlap(self, text_a: str, text_b: str) -> float:
        """关键词重叠度（jieba 分词 + 集合交集/并集）。

        Returns:
            Jaccard 相似度 0-1
        """
        set_a = _tokenize(text_a)
        set_b = _tokenize(text_b)

        if not set_a or not set_b:
            return 0.0

        intersection = set_a & set_b
        union = set_a | set_b

        if not union:
            return 0.0

        return len(intersection) / len(union)

    def _instruction_conflict(self, text_a: str, text_b: str) -> list[str]:
        """指令矛盾检测。

        A 说"必须/总是/优先"的内容，B 说"不要/禁止/避免"（或反之），
        则视为矛盾。

        Returns:
            矛盾描述列表
        """
        conflicts: list[str] = []

        # 提取 A 中的强制性指令行
        mandatory_lines_a = self._extract_directive_lines(text_a, _MANDATORY_PATTERNS[0])
        # 提取 B 中的禁止性指令行
        forbidden_lines_b = self._extract_directive_lines(text_b, _MANDATORY_PATTERNS[1])

        for m_line in mandatory_lines_a:
            for f_line in forbidden_lines_b:
                if self._lines_share_topic(m_line, f_line):
                    conflicts.append(
                        f"矛盾: 内部skill要求「{m_line.strip()[:60]}」"
                        f"vs 普通skill禁止「{f_line.strip()[:60]}」"
                    )

        # 反向：A 禁止、B 强制
        forbidden_lines_a = self._extract_directive_lines(text_a, _MANDATORY_PATTERNS[1])
        mandatory_lines_b = self._extract_directive_lines(text_b, _MANDATORY_PATTERNS[0])

        for f_line in forbidden_lines_a:
            for m_line in mandatory_lines_b:
                if self._lines_share_topic(f_line, m_line):
                    conflicts.append(
                        f"矛盾: 内部skill禁止「{f_line.strip()[:60]}」"
                        f"vs 普通skill要求「{m_line.strip()[:60]}」"
                    )

        return conflicts

    def _tool_sequence_overlap(self, text_a: str, text_b: str) -> float:
        """工具调用序列覆盖度。

        提取两个 skill 中提到的工具名称序列，计算覆盖度。

        Returns:
            覆盖度 0-1
        """
        tools_a = self._extract_tool_names(text_a)
        tools_b = self._extract_tool_names(text_b)

        if not tools_a or not tools_b:
            return 0.0

        # 计算交集 / 较小集合的大小（覆盖度）
        intersection = tools_a & tools_b
        denominator = min(len(tools_a), len(tools_b))

        if denominator == 0:
            return 0.0

        return len(intersection) / denominator

    # ── 内部辅助 ────────────────────────────────────────────────────────────

    def _get_internal_skills(self) -> list[tuple[str, str]]:
        """获取所有 visible=False 的内部 skill (name, body)。"""
        result = []
        for name, doc in self.registry.documents.items():
            m = doc.manifest
            if not m.visible and m.status == "active":
                result.append((name, doc.body))
        return result

    def _get_active_visible_skills(self) -> list[tuple[str, str]]:
        """获取所有 visible=True, status=active 的普通 skill (name, body)。"""
        result = []
        for name, doc in self.registry.documents.items():
            m = doc.manifest
            if m.visible and m.status == "active":
                result.append((name, doc.body))
        return result

    @staticmethod
    def _extract_directive_lines(text: str, pattern: re.Pattern) -> list[str]:
        """从文本中提取匹配指令模式的行。"""
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if line and pattern.search(line):
                lines.append(line)
        return lines

    @staticmethod
    def _lines_share_topic(line_a: str, line_b: str, min_overlap: int = 2) -> bool:
        """判断两行是否涉及同一主题（共享足够关键词）。"""
        # 提取关键词（中文词 + 英文词）
        words_a = set(re.findall(r"[a-zA-Z_]\w{2,}|[一-鿿]{2,}", line_a.lower()))
        words_b = set(re.findall(r"[a-zA-Z_]\w{2,}|[一-鿿]{2,}", line_b.lower()))
        shared = words_a & words_b
        return len(shared) >= min_overlap

    @staticmethod
    def _extract_tool_names(text: str) -> set[str]:
        """从文本中提取工具名称。"""
        tools = set()

        # 匹配反引号包裹的函数调用: `tool_name(`
        for m in re.finditer(r"`(\w+)\s*\(", text):
            tools.add(m.group(1).lower())

        # 匹配普通函数调用: tool_name(
        for m in re.finditer(r"(?<!`)\b(\w+)\s*\(", text):
            name = m.group(1).lower()
            # 过滤常见非工具词
            if name not in {
                "if", "for", "while", "switch", "return", "else",
                "function", "class", "import", "from", "with",
                "当", "如果", "对于", "在",
            }:
                tools.add(name)

        # 匹配常见工具名称（直接出现在文本中）
        known_tools = {
            "graph_query", "read_file", "analyze_file", "write_file",
            "edit_file", "bash", "search_code", "grep", "glob",
            "search_codebase", "search_symbol", "web_search", "web_fetch",
            "minhash", "jaccard",
        }
        text_lower = text.lower()
        for tool in known_tools:
            if tool in text_lower:
                tools.add(tool)

        return tools

    def _extract_keyword_conflicts(self, text_a: str, text_b: str) -> list[str]:
        """提取关键词重叠的具体内容。"""
        tokens_a = _tokenize(text_a)
        tokens_b = _tokenize(text_b)
        overlap = tokens_a & tokens_b
        if not overlap:
            return ["(关键词重叠但无法提取具体片段)"]
        # 最多展示 5 个关键词
        sample = sorted(overlap)[:5]
        return [f"重叠关键词: {', '.join(sample)}"]

    def _extract_tool_conflicts(self, text_a: str, text_b: str) -> list[str]:
        """提取工具序列重叠的具体内容。"""
        tools_a = self._extract_tool_names(text_a)
        tools_b = self._extract_tool_names(text_b)
        overlap = tools_a & tools_b
        if not overlap:
            return ["(工具序列重叠但无法提取具体工具)"]
        sample = sorted(overlap)[:5]
        return [f"重叠工具: {', '.join(sample)}"]


# ── Section 分割辅助 ────────────────────────────────────────────────────────


def _split_sections(body: str) -> dict[str, str]:
    """按 ## 标题分割正文为 section 映射。

    Returns:
        {section_title: section_content} 无标题的部分归入 "__preamble__"
    """
    sections: dict[str, str] = {}
    current_title = "__preamble__"
    current_lines: list[str] = []

    for line in body.splitlines():
        if re.match(r"^##\s+", line):
            # 保存上一个 section
            if current_lines:
                sections[current_title] = "\n".join(current_lines).strip()
            current_title = re.sub(r"^##\s+", "", line).strip()
            current_lines = []
        else:
            current_lines.append(line)

    # 保存最后一个 section
    if current_lines:
        sections[current_title] = "\n".join(current_lines).strip()

    return sections


def _build_body_from_sections(sections: dict[str, str]) -> str:
    """从 section 映射重建正文。"""
    parts: list[str] = []

    # preamble 放最前面
    preamble = sections.pop("__preamble__", "")
    if preamble:
        parts.append(preamble)

    for title, content in sections.items():
        parts.append(f"\n## {title}\n\n{content}")

    return "\n".join(parts)


# ── SkillMerger ─────────────────────────────────────────────────────────────


class SkillMerger:
    """合并冲突 skill，生成"主动学习 skill"。"""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry  # 用于读取和更新技能定义的注册表

    # ── 公开接口 ────────────────────────────────────────────────────────────

    def merge(
        self,
        internal_skill_name: str,
        conflicting_skill_name: str,
        conflict_fields: list[str],
    ) -> MergeResult:
        """合并两个冲突 skill。

        策略:
        1. 内部 skill 的规则覆盖冲突部分（关键 skills 优先）
        2. 保留普通 skill 中不冲突的补充内容
        3. 生成新的 learned skill 正文
        4. 构建 lineage 记录

        Args:
            internal_skill_name: 内部 skill 名称
            conflicting_skill_name: 冲突的普通 skill 名称
            conflict_fields: 冲突字段描述列表

        Returns:
            MergeResult
        """
        int_doc = self.registry.documents.get(internal_skill_name)
        act_doc = self.registry.documents.get(conflicting_skill_name)

        int_body = int_doc.body if int_doc else ""
        act_body = act_doc.body if act_doc else ""

        int_sections = _split_sections(int_body)
        act_sections = _split_sections(act_body)

        # 合并策略：冲突 section 以内部 skill 为准，非冲突 section 合并保留
        merged_sections: dict[str, str] = {}
        conflict_titles = self._identify_conflict_section_titles(
            int_sections, act_sections, conflict_fields
        )

        # 1. 内部 skill 的所有 section 都保留
        for title, content in int_sections.items():
            merged_sections[title] = content

        # 2. 普通 skill 中不冲突的 section 合并保留
        for title, content in act_sections.items():
            if title == "__preamble__":
                # preamble 不覆盖
                continue
            if title in conflict_titles:
                # 冲突 section 已由内部 skill 覆盖，跳过
                continue
            if title in merged_sections:
                # 同名非冲突 section：追加普通 skill 的补充内容
                merged_sections[title] = (
                    merged_sections[title]
                    + "\n\n--- 补充内容（来自 " + conflicting_skill_name + "） ---\n\n"
                    + content
                )
            else:
                # 独有的 section 直接保留
                merged_sections[title] = content

        # 3. 普通 skill 的 preamble 如有独特内容，追加到末尾
        act_preamble = act_sections.get("__preamble__", "")
        int_preamble = int_sections.get("__preamble__", "")
        if act_preamble and act_preamble != int_preamble:
            if "__preamble__" in merged_sections:
                merged_sections["__preamble__"] += "\n\n" + act_preamble
            else:
                merged_sections["__preamble__"] = act_preamble

        new_body = _build_body_from_sections(dict(merged_sections))

        # 4. 生成名称和描述
        slug = re.sub(r"[^\w\-]+", "-", conflicting_skill_name).strip("-").lower()
        new_name = f"learned-{slug}"
        new_description = (
            f"主动学习skill（合并自 {internal_skill_name} + {conflicting_skill_name}）"
        )

        # 5. 构建 lineage
        today = date.today().isoformat()
        conflict_summary = "; ".join(conflict_fields[:3]) if conflict_fields else "内容重叠"
        lineage = [
            {
                "source": conflicting_skill_name,
                "type": "retired_skill",
                "merged_at": today,
                "conflict_field": conflict_summary,
            },
            {
                "source": internal_skill_name,
                "type": "internal_skill",
                "merged_at": today,
                "conflict_field": conflict_summary,
            },
        ]

        # 根据参与合并的双方 source 决定合并后的 source
        source_int = int_doc.manifest.source if int_doc else "auto"
        source_act = act_doc.manifest.source if act_doc else "auto"
        new_source = "manual" if (source_int == "manual" or source_act == "manual") else "merged"

        return MergeResult(
            new_name=new_name,
            new_body=new_body,
            new_description=new_description,
            lineage=lineage,
            retired_skills=[conflicting_skill_name],
            new_source=new_source,
        )

    def retire_skill(self, name: str) -> bool:
        """将 skill 移动到 __retired/ 并标记 status=retired。

        Args:
            name: skill 名称

        Returns:
            是否成功
        """
        doc = self.registry.documents.get(name)
        if not doc:
            return False

        src_path = doc.manifest.path
        src_dir = src_path.parent

        if not src_dir.exists():
            return False

        # 1. 读取原始内容
        try:
            content = src_path.read_text(encoding="utf-8")
        except OSError:
            return False

        # 2. 修改 frontmatter: status=retired, retired_at=当前日期
        today = date.today().isoformat()

        if re.search(r"^status:\s*\S+", content, re.MULTILINE):
            content = re.sub(
                r"^status:\s*\S+",
                f"status: retired",
                content,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            content = content.replace("---\n", "---\nstatus: retired\n", 1)

        if re.search(r"^retired_at:", content, re.MULTILINE):
            content = re.sub(
                r"^retired_at:.*$",
                f"retired_at: {today}",
                content,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            content = content.replace("---\n", f"---\nretired_at: {today}\n", 1)

        # 3. 物理移动: skills/{name}/ → skills/__retired/{name}/
        retired_dir = RETIRED_SKILLS_DIR / src_dir.name
        RETIRED_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        try:
            if retired_dir.exists():
                shutil.rmtree(retired_dir, ignore_errors=True)
            shutil.move(str(src_dir), str(retired_dir))

            # 写入修改后的内容
            retired_skill_path = retired_dir / src_path.name
            retired_skill_path.write_text(content, encoding="utf-8")
        except OSError:
            return False

        # 4. 从 registry 中移除
        if name in self.registry.documents:
            del self.registry.documents[name]

        return True

    def create_learned_skill(self, merge_result: MergeResult) -> str:
        """创建主动学习 skill 到 skills/__learned/。

        Args:
            merge_result: 合并结果

        Returns:
            创建的 skill 名称
        """
        LEARNED_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        # 1. 构建目录
        slug = re.sub(r"[^\w\-]+", "-", merge_result.new_name).strip("-").lower()
        skill_dir = LEARNED_SKILLS_DIR / slug
        skill_dir.mkdir(parents=True, exist_ok=True)

        # 2. 构建 frontmatter
        today = date.today().isoformat()
        lineage_yaml = self._lineage_to_yaml(merge_result.lineage)

        content = (
            f"---\n"
            f"name: {merge_result.new_name}\n"
            f"description: {merge_result.new_description}\n"
            f"source: {merge_result.new_source}\n"
            f"visible: true\n"
            f"layer: none\n"
            f"status: active\n"
            f"locked: false\n"
            f"created_at: {today}\n"
            f"updated_at: {today}\n"
            f"usage_count: 0\n"
            f"lineage:\n{lineage_yaml}\n"
            f"---\n\n"
            f"{merge_result.new_body}\n"
        )

        # 3. 写入 SKILL.md
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(content, encoding="utf-8")

        # 4. 刷新 registry
        self.registry.documents.clear()
        self.registry._load_all()

        return merge_result.new_name

    def execute_conflict_merge(self, conflict: ConflictResult) -> MergeResult:
        """完整执行一次冲突合并流程。

        1. merge() 生成新内容
        2. retire_skill() 下线冲突 skill
        3. create_learned_skill() 创建新 skill
        4. 返回 MergeResult

        Args:
            conflict: 冲突检测结果

        Returns:
            MergeResult
        """
        # 1. 合并
        result = self.merge(
            internal_skill_name=conflict.internal_skill,
            conflicting_skill_name=conflict.conflicting_skill,
            conflict_fields=conflict.conflict_fields,
        )

        # 2. 下线冲突 skill
        self.retire_skill(conflict.conflicting_skill)

        # 3. 创建新的 learned skill
        self.create_learned_skill(result)

        return result

    # ── 内部辅助 ────────────────────────────────────────────────────────────

    @staticmethod
    def _identify_conflict_section_titles(
        int_sections: dict[str, str],
        act_sections: dict[str, str],
        conflict_fields: list[str],
    ) -> set[str]:
        """识别哪些 section 属于冲突区域。

        如果 conflict_fields 中提到某个 section 标题关键词，
        或者两个 skill 有同名 section 且内容差异较大，
        则视为冲突 section。
        """
        conflict_titles: set[str] = set()

        # 从 conflict_fields 提取可能的 section 标题
        for field_desc in conflict_fields:
            for title in act_sections:
                if title != "__preamble__" and title.lower() in field_desc.lower():
                    conflict_titles.add(title)

        # 同名 section 如果关键词重叠度高，也视为冲突
        for title in int_sections:
            if title in act_sections and title != "__preamble__":
                set_a = _tokenize(int_sections[title])
                set_b = _tokenize(act_sections[title])
                if set_a and set_b:
                    jaccard = len(set_a & set_b) / len(set_a | set_b)
                    if jaccard >= 0.4:
                        conflict_titles.add(title)

        return conflict_titles

    @staticmethod
    def _lineage_to_yaml(lineage: list[dict]) -> str:
        """将 lineage 列表转为 YAML 多行格式。"""
        lines: list[str] = []
        for entry in lineage:
            lines.append(f"  - source: {entry.get('source', '')}")
            lines.append(f"    type: {entry.get('type', '')}")
            if "merged_at" in entry:
                lines.append(f"    merged_at: {entry['merged_at']}")
            if "conflict_field" in entry:
                lines.append(f"    conflict_field: {entry['conflict_field']}")
        return "\n".join(lines)


# ── SkillLifecycleManager ───────────────────────────────────────────────────


class SkillLifecycleManager:
    """主动学习Skill的完整生命周期管理"""

    def __init__(self, registry, merger: SkillMerger = None):
        self.registry = registry  # 用于读取和更新技能定义的注册表
        self.merger = merger or SkillMerger(registry)  # 负责合并技能内容的处理器

    def delete_learned_skill(self, name: str) -> dict:
        """删除主动学习skill并触发回滚。"""
        doc = self.registry.documents.get(name)
        if not doc:
            return {"deleted": name, "restored": [], "conflicts": []}

        restored: list[str] = []
        for entry in doc.manifest.lineage or []:
            if entry.get("type") == "retired_skill":
                source_name = entry.get("source")
                if source_name and self._restore_file(source_name):
                    restored.append(source_name)
            # internal_skill: 关键skill始终存在，无需操作

        detector = SkillConflictDetector(self.registry)
        conflicts = detector.detect_conflicts()
        restored_conflicts = [c for c in conflicts if c.conflicting_skill in restored]

        self.registry.delete_skill(name)

        return {
            "deleted": name,
            "restored": restored,
            "conflicts": restored_conflicts,
        }

    def merge_learned_skills(self, skill_a: str, skill_b: str) -> MergeResult:
        """两个主动学习skill进一步合并（高阶合并）。"""
        doc_a = self.registry.documents.get(skill_a)
        doc_b = self.registry.documents.get(skill_b)

        if not doc_a or not doc_b:
            return MergeResult(
                new_name="",
                new_body="",
                new_description="",
                lineage=[],
                retired_skills=[],
            )

        # usage_count 高的作为主体
        if doc_b.manifest.usage_count > doc_a.manifest.usage_count:
            main_doc, sub_doc = doc_b, doc_a
            main_name, sub_name = skill_b, skill_a
        else:
            main_doc, sub_doc = doc_a, doc_b
            main_name, sub_name = skill_a, skill_b

        merged_body = (
            main_doc.body
            + f"\n\n--- 补充内容（来自 {sub_name}） ---\n\n"
            + sub_doc.body
        )

        today = date.today().isoformat()
        merged_lineage: list[dict] = [
            {
                "source": main_name,
                "type": "merged",
                "merged_at": today,
                "conflict_field": f"高阶合并主体: {main_name}",
            },
            {
                "source": sub_name,
                "type": "merged",
                "merged_at": today,
                "conflict_field": f"高阶合并补充: {sub_name}",
            },
        ]
        for entry in doc_a.manifest.lineage or []:
            if entry not in merged_lineage:
                merged_lineage.append(entry)
        for entry in doc_b.manifest.lineage or []:
            if entry not in merged_lineage:
                merged_lineage.append(entry)

        slug = re.sub(r"[^\w\-]+", "-", f"{main_name}-{sub_name}").strip("-").lower()
        if len(slug) > 80:
            slug = re.sub(r"[^\w\-]+", "-", f"{main_name}-advanced").strip("-").lower()
        new_name = f"learned-{slug}"
        new_description = f"高阶主动学习skill（合并自 {main_name} + {sub_name}）"

        result = MergeResult(
            new_name=new_name,
            new_body=merged_body,
            new_description=new_description,
            lineage=merged_lineage,
            retired_skills=[skill_a, skill_b],
        )

        self.merger.create_learned_skill(result)

        # 将原两个 learned skill 移入 __retired/（它们的 retired_skills 已被吸收，不再恢复）
        for skill_name in [skill_a, skill_b]:
            self.merger.retire_skill(skill_name)

        return result

    def restore_retired_skill(self, name: str) -> dict:
        """手动恢复一个已下线的skill。"""
        restored_name = name if self._restore_file(name) else None

        detector = SkillConflictDetector(self.registry)
        conflicts = detector.detect_conflicts()
        restored_conflicts = [c for c in conflicts if c.conflicting_skill == name]

        return {
            "restored": restored_name,
            "conflicts": restored_conflicts,
        }

    def get_lineage_tree(self, name: str) -> dict:
        """获取skill的完整谱系路径图（递归）。"""
        return self._build_lineage_tree(name, set())

    def _build_lineage_tree(self, name: str, visited: set[str]) -> dict:
        if name in visited:
            return {"name": name, "type": "cycle", "status": "unknown", "children": []}
        visited.add(name)

        node = {"name": name, "type": "skill", "status": "unknown", "children": []}
        doc = self.registry.documents.get(name)
        retired_dir = RETIRED_SKILLS_DIR / name

        if doc:
            node["status"] = doc.manifest.status
            if doc.manifest.source == "merged" or doc.manifest.lineage:
                node["type"] = "merged"
            elif not doc.manifest.visible:
                node["type"] = "internal_skill"
            else:
                node["type"] = "active_skill"
        elif retired_dir.exists():
            node["status"] = "retired"
            lineage = self._read_lineage_from_retired(name)
            node["type"] = "merged" if lineage else "retired_skill"
        else:
            node["status"] = "missing"
            node["type"] = "unknown"
            return node

        lineage = doc.manifest.lineage if doc else self._read_lineage_from_retired(name)
        for entry in lineage or []:
            source_name = entry.get("source")
            source_type = entry.get("type", "retired_skill")
            if not source_name:
                continue

            if source_type == "internal_skill":
                child = {
                    "name": source_name,
                    "type": "internal_skill",
                    "status": "active",
                    "children": [],
                }
            else:
                child = self._build_lineage_tree(source_name, visited)

            node["children"].append(child)

        return node

    def _restore_file(self, name: str) -> bool:
        """从 __retired/ 恢复文件到 skills/。"""
        retired_dir = RETIRED_SKILLS_DIR / name
        if not retired_dir.exists():
            return False

        target_dir = SKILLS_DIR / name
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

        try:
            shutil.move(str(retired_dir), str(target_dir))
        except OSError:
            return False

        skill_path = target_dir / "SKILL.md"
        if not skill_path.exists():
            skill_path = target_dir / "SKILL_zh.md"
        if skill_path.exists():
            try:
                content = skill_path.read_text(encoding="utf-8")
                if re.search(r"^status:\s*\S+", content, re.MULTILINE):
                    content = re.sub(
                        r"^status:\s*\S+",
                        "status: active",
                        content,
                        count=1,
                        flags=re.MULTILINE,
                    )
                else:
                    content = content.replace("---\n", "---\nstatus: active\n", 1)
                content = re.sub(r"^retired_at:.*$\n?", "", content, flags=re.MULTILINE)
                skill_path.write_text(content, encoding="utf-8")
            except OSError:
                pass

        self.registry.documents.clear()
        self.registry._load_all()
        return True

    def _read_lineage_from_retired(self, name: str) -> list:
        """从 __retired/ 目录中读取已下线skill的lineage。"""
        retired_dir = RETIRED_SKILLS_DIR / name
        if not retired_dir.exists():
            return []

        path = retired_dir / "SKILL.md"
        if not path.exists():
            path = retired_dir / "SKILL_zh.md"
        if not path.exists():
            return []

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []

        meta, _ = self.registry._parse_frontmatter(text)
        return self.registry._parse_lineage(meta.get("lineage", ""))
