"""
code-demo/core/utils/skill_registry.py — SkillRegistry 技能注册表

扫描 SKILLS_DIR 下每个子目录的 SKILL.md 或 SKILL_zh.md，解析 YAML frontmatter。

权限模型（Phase 3 §4.0）：
  - source: "manual"（手动）| "auto"（AI 自动）
  - locked: false（AI 可读写）| true（AI 只读）
  - 用户始终拥有完整权限

自动创建与进化（Phase 3 §4.2）：
  - auto_create() — 从执行日志自动创建技能
  - suggest_merge() — 检测可合并的相似技能对
  - merge_skills() — 合并两个未锁定技能
  - record_usage() — 记录使用次数

来源：19_mcp-plugin.py L2526–L2540（教学精简版）
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from code_demo.core.config import SKILLS_DIR, LEARNED_SKILLS_DIR, RETIRED_SKILLS_DIR
from code_demo.rag.dedup_embedding import MinHash, _shingles


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class SkillManifest:
    """技能清单 — 从 SKILL.md frontmatter 解析。"""
    name: str  # 技能名称
    description: str  # 技能用途说明
    path: Path  # 技能定义文件路径
    source: str = "manual"         # "manual" | "auto" | "merged"
    locked: bool = False           # true = AI 只读
    created_at: str = ""           # ISO 日期 YYYY-MM-DD
    updated_at: str = ""           # ISO 日期 YYYY-MM-DD
    usage_count: int = 0           # 使用次数
    visible: bool = True          # 是否对AI/前端可见（False=内部skill）
    layer: str = "none"            # 注入层级: always|memory|structure|knowledge|skill|hybrid|none
    status: str = "active"        # 生命周期: active|retired
    lineage: list = field(default_factory=list)  # 合并谱系 [{source, type, merged_at, conflict_field}]


@dataclass
class SkillDocument:
    """技能文档 — 清单 + 正文。"""
    manifest: SkillManifest  # 技能元信息
    body: str  # 技能操作说明正文


# ── 质量过滤辅助 ──────────────────────────────────────────────────────────────

def _is_tool_output(text: str) -> bool:
    """判断文本是否主要是工具输出（路径列表、代码等），不适合创建守卫。"""
    lines = text.strip().split("\n")
    if not lines:
        return True
    path_like = sum(1 for l in lines if l.strip().startswith(("/", ".", "-")) and "/" in l)
    return path_like > len(lines) * 0.6


# ── MinHash 相似度（用于 suggest_merge）───────────────────────────────────────

def _skill_minhash(text: str, num_perm: int = 64) -> MinHash:
    """将技能文本转换为 MinHash 签名。"""
    mh = MinHash(num_perm=num_perm)
    for shingle in _shingles(text, k=3):
        mh.update(shingle)
    return mh


# ── 注册表 ────────────────────────────────────────────────────────────────────

class SkillRegistry:
    """技能注册表 — 扫描、管理、自动创建、融合进化。"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir  # 技能定义文件所在目录
        self.documents: dict[str, SkillDocument] = {}  # 按名称缓存的技能文档
        self._load_all()

    # ── 加载 ────────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        if not self.skills_dir.exists():
            return
        # 收集待扫描目录：主 skills/ + __learned/，跳过 __retired/
        scan_dirs: list[Path] = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if skill_dir.name == "__retired":
                continue  # 跳过已下线 skills
            if skill_dir.name == "__learned":
                continue  # 单独处理
            if skill_dir.name == "__internal":
                scan_dirs.append(skill_dir)  # __internal 也扫描
                continue
            scan_dirs.append(skill_dir)
        # 单独扫描 __learned/
        if LEARNED_SKILLS_DIR.exists():
            for skill_dir in sorted(LEARNED_SKILLS_DIR.iterdir()):
                if skill_dir.is_dir():
                    scan_dirs.append(skill_dir)

        for skill_dir in scan_dirs:
            path = skill_dir / "SKILL.md"
            if not path.exists():
                path = skill_dir / "SKILL_zh.md"
            if not path.exists():
                continue
            try:
                meta, body = self._parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = meta.get("name", skill_dir.name)
            description = meta.get("description", "No description")
            # 解析 lineage 字段（容错处理）
            lineage = self._parse_lineage(meta.get("lineage", ""))
            manifest = SkillManifest(
                name=name,
                description=description,
                path=path,
                source=meta.get("source", "manual"),
                locked=meta.get("locked", "false").lower() == "true",
                created_at=meta.get("created_at", ""),
                updated_at=meta.get("updated_at", ""),
                usage_count=int(meta.get("usage_count", "0") or 0),
                visible=meta.get("visible", "true").lower() == "true",
                layer=meta.get("layer", "none"),
                status=meta.get("status", "active"),
                lineage=lineage,
            )
            self.documents[name] = SkillDocument(manifest=manifest, body=body.strip())

    def _parse_lineage(self, raw: str | list) -> list[dict]:
        """解析 lineage 字段（容错处理多种格式）。

        frontmatter 中 lineage 可能为：
        - 多行 YAML 列表："- source: xxx\n  type: yyy\n"
        - 逗号分隔字符串
        - 已解析的 list
        - 空字符串
        """
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if not raw or not isinstance(raw, str):
            return []
        raw = raw.strip()
        if not raw:
            return []
        # 尝试解析 YAML 列表格式 "- source: xxx\n  type: yyy"
        items: list[dict] = []
        current: dict = {}
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("-"):
                # 新列表项
                if current:
                    items.append(current)
                    current = {}
                line = line[1:].strip()
                if ":" in line:
                    key, value = line.split(":", 1)
                    current[key.strip()] = value.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                current[key.strip()] = value.strip()
        if current:
            items.append(current)
        return items

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = {}
        current_key = None
        for line in match.group(1).strip().splitlines():
            if line.startswith(("  ", "\t")) and current_key:
                meta[current_key] += "\n" + line.strip()
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value in ("|", ">"):
                value = ""
            meta[key] = value
            current_key = key
        return meta, match.group(2)

    # ── 查询 ────────────────────────────────────────────────────────────────

    def describe_available(self) -> str:
        """返回可见且活跃的技能描述列表（供 AI 参考）。"""
        if not self.documents:
            return "(no skills available)"
        lines = []
        for name in sorted(self.documents):
            manifest = self.documents[name].manifest
            if not manifest.visible or manifest.status == "retired":
                continue
            desc = manifest.description.replace("\n", " ").strip()
            lines.append(f"- {manifest.name}: {desc}")
        if not lines:
            return "(no skills available)"
        return "\n".join(lines)

    def load_full_text(self, name: str) -> str:
        document = self.documents.get(name)
        if not document:
            known = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: Unknown skill '{name}'. Available skills: {known}"
        return (
            f'<skill name="{document.manifest.name}">\n'
            f"{document.body}\n"
            "</skill>"
        )

    def get_skill_body(self, name: str) -> str:
        doc = self.documents.get(name)
        return doc.body if doc else ""

    def list_skills(self) -> list[dict]:
        """返回可见技能的完整元数据（供 API 使用）。

        跳过 visible=False 的内部技能（前端不可见）。
        """
        result = []
        for name, doc in self.documents.items():
            m = doc.manifest
            if not m.visible:
                continue
            result.append({
                "name": m.name,
                "description": m.description,
                "source": m.source,
                "locked": m.locked,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
                "usage_count": m.usage_count,
                "status": m.status,
                "lineage": m.lineage,
            })
        return sorted(result, key=lambda x: x["name"])

    # ── 内部技能 & 下线技能 ──────────────────────────────────────────────

    def get_internal_skills(self, layer: str) -> list[str]:
        """获取指定 layer 的内部skill正文列表。

        Args:
            layer: 注入层级（always|memory|structure|knowledge|skill|hybrid|none）

        Returns:
            匹配的内部技能正文列表
        """
        results = []
        for doc in self.documents.values():
            m = doc.manifest
            if not m.visible and m.status == "active" and m.layer == layer:
                results.append(doc.body)
        return results

    def list_retired_skills(self) -> list[dict]:
        """返回已下线skills列表（从 __retired/ 目录读取）。

        Returns:
            [{name, description, status, lineage, retired_at}]
        """
        results: list[dict] = []
        if not RETIRED_SKILLS_DIR.exists():
            return results
        for skill_dir in sorted(RETIRED_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            path = skill_dir / "SKILL.md"
            if not path.exists():
                path = skill_dir / "SKILL_zh.md"
            if not path.exists():
                continue
            try:
                meta, _ = self._parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            lineage = self._parse_lineage(meta.get("lineage", ""))
            results.append({
                "name": meta.get("name", skill_dir.name),
                "description": meta.get("description", ""),
                "status": "retired",
                "lineage": lineage,
                "retired_at": meta.get("retired_at", meta.get("updated_at", "")),
            })
        return results

    # ── 权限检查 ────────────────────────────────────────────────────────────

    def can_ai_modify(self, name: str) -> bool:
        """AI 操作前权限检查：技能存在且未锁定。"""
        doc = self.documents.get(name)
        if not doc:
            return False
        return not doc.manifest.locked

    # ── CRUD ────────────────────────────────────────────────────────────────

    def add_skill(
        self,
        name: str,
        description: str,
        body: str,
        source: str = "manual",
        locked: bool = False,
    ) -> None:
        """创建新 <slug>/SKILL.md 并刷新注册表。

        AI 自动创建/合并的技能（source=auto|merged）写入 __learned/，
        以便与手动技能隔离但仍被 _load_all() 加载使用；
        手动技能（source=manual）写入主 skills/ 目录。
        """
        slug = re.sub(r"[^\w\-]+", "-", name).strip("-").lower() or "skill"
        base_dir = LEARNED_SKILLS_DIR if source in ("auto", "merged") else self.skills_dir
        skill_dir = base_dir / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"

        today = date.today().isoformat()
        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"source: {source}\n"
            f"locked: {'true' if locked else 'false'}\n"
            f"created_at: {today}\n"
            f"updated_at: {today}\n"
            f"usage_count: 0\n"
            f"---\n\n"
            f"{body}\n"
        )
        path.write_text(content, encoding="utf-8")
        self.documents.clear()
        self._load_all()

    def update_skill(
        self,
        old_name: str,
        new_name: str,
        description: str,
        body: str,
    ) -> None:
        """更新 skill：可改名（自动迁移目录），刷新注册表。"""
        old_doc = self.documents.get(old_name)
        if not old_doc:
            return
        old_path = old_doc.manifest.path
        old_dir = old_path.parent

        # 保留原有 source/locked/created_at/usage_count
        source = old_doc.manifest.source
        locked = old_doc.manifest.locked
        created_at = old_doc.manifest.created_at or date.today().isoformat()
        usage_count = old_doc.manifest.usage_count
        today = date.today().isoformat()

        new_slug = re.sub(r"[^\w\-]+", "-", new_name).strip("-").lower() or "skill"
        # 保留技能原所在目录（__learned/ 或主 skills/），改名时不跨目录迁移
        new_dir = old_dir.parent / new_slug
        content = (
            f"---\n"
            f"name: {new_name}\n"
            f"description: {description}\n"
            f"source: {source}\n"
            f"locked: {'true' if locked else 'false'}\n"
            f"created_at: {created_at}\n"
            f"updated_at: {today}\n"
            f"usage_count: {usage_count}\n"
            f"---\n\n"
            f"{body}\n"
        )

        if old_dir != new_dir:
            if new_dir.exists():
                shutil.rmtree(new_dir)
            shutil.rmtree(old_dir, ignore_errors=True)
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / "SKILL.md").write_text(content, encoding="utf-8")
        else:
            old_path.write_text(content, encoding="utf-8")

        self.documents.clear()
        self._load_all()

    def delete_skill(self, name: str) -> bool:
        """删除指定 skill 的整个目录，刷新注册表。"""
        doc = self.documents.get(name)
        if not doc:
            return False
        skill_dir = doc.manifest.path.parent
        shutil.rmtree(skill_dir, ignore_errors=True)
        self.documents.clear()
        self._load_all()
        return True

    # ── 锁定管理（仅用户调用）─────────────────────────────────────────────────

    def set_locked(self, name: str, locked: bool) -> bool:
        """切换技能锁定状态。锁定后 AI 只能读取。"""
        doc = self.documents.get(name)
        if not doc:
            return False
        if not doc.manifest.visible:
            return False

        path = doc.manifest.path
        try:
            text = path.read_text(encoding="utf-8")
            # 更新 frontmatter 中的 locked 字段
            if re.search(r"^locked:\s*(true|false)\s*$", text, re.MULTILINE):
                text = re.sub(
                    r"^locked:\s*(true|false)\s*$",
                    f"locked: {'true' if locked else 'false'}",
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                # 插入 locked 字段到 frontmatter
                text = text.replace("---\n", f"---\nlocked: {'true' if locked else 'false'}\n", 1)
            path.write_text(text, encoding="utf-8")
        except OSError:
            return False

        self.documents.clear()
        self._load_all()
        return True

    # ── 使用计数 ────────────────────────────────────────────────────────────

    def record_usage(self, name: str) -> None:
        """记录技能被使用一次。"""
        doc = self.documents.get(name)
        if not doc:
            return

        doc.manifest.usage_count += 1
        path = doc.manifest.path

        try:
            text = path.read_text(encoding="utf-8")
            new_count = doc.manifest.usage_count
            if re.search(r"^usage_count:\s*\d+\s*$", text, re.MULTILINE):
                text = re.sub(
                    r"^usage_count:\s*\d+\s*$",
                    f"usage_count: {new_count}",
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
                path.write_text(text, encoding="utf-8")
        except OSError:
            pass

    # ── AI 自动创建 ──────────────────────────────────────────────────────────

    def auto_create(
        self,
        task_description: str,
        execution_log: list[dict],
        result: str,
    ) -> str | None:
        """从执行日志自动创建技能。

        分析工具调用序列，生成技能草稿。
        新建技能默认 source="auto", locked=False。

        Args:
            task_description: 任务描述
            execution_log: 执行日志（工具调用序列）
            result: 执行结果摘要

        Returns:
            新创建的技能名称，或 None（如果不需要创建）
        """
        if not execution_log or len(execution_log) < 3:
            return None

        # 提取工具调用序列
        tool_sequence = []
        for step in execution_log:
            tool_name = step.get("tool", "")
            if tool_name:
                tool_sequence.append(tool_name)

        if len(tool_sequence) < 3:
            return None

        # 生成技能名称和描述
        unique_tools = list(dict.fromkeys(tool_sequence))
        name_parts = [t.replace("_", " ") for t in unique_tools[:3]]
        skill_name = "-".join(name_parts).lower().replace(" ", "-")

        # 检查是否已存在相似技能
        for existing_name, existing_doc in self.documents.items():
            existing_tools = re.findall(r"\b\w+_\w+\b", existing_doc.body)
            overlap = set(unique_tools) & set(existing_tools)
            if len(overlap) >= len(unique_tools) * 0.7:
                return None  # 已有覆盖度 > 70% 的技能

        # 生成技能正文
        seq_str = " → ".join(tool_sequence)
        body = (
            f"## 触发条件\n"
            f"当用户要求执行类似「{task_description[:100]}」的任务时。\n\n"
            f"## 工具调用序列\n"
            f"```\n{seq_str}\n```\n\n"
            f"## 步骤\n"
        )
        for i, tool in enumerate(tool_sequence, 1):
            body += f"{i}. 调用 `{tool}`\n"

        body += f"\n## 预期结果\n{result[:200]}\n"

        self.add_skill(
            name=skill_name,
            description=f"自动提取: {task_description[:80]}",
            body=body,
            source="auto",
            locked=False,
        )

        return skill_name

    # ── 技能融合 ────────────────────────────────────────────────────────────

    def suggest_merge(self, threshold: float = 0.6) -> list[tuple[str, str, float]]:
        """检测可合并的相似技能对（仅未锁定技能参与）。

        使用 MinHash Jaccard 相似度比较技能正文。

        Args:
            threshold: 相似度阈值（超过此值视为可合并）

        Returns:
            [(skill_a, skill_b, similarity), ...] 按相似度降序
        """
        unlocked = [
            (name, doc)
            for name, doc in self.documents.items()
            if not doc.manifest.locked
        ]

        if len(unlocked) < 2:
            return []

        # 计算 MinHash 签名
        signatures: dict[str, MinHash] = {}
        for name, doc in unlocked:
            text = f"{doc.manifest.name} {doc.manifest.description} {doc.body}"
            signatures[name] = _skill_minhash(text)

        # 两两比较
        pairs: list[tuple[str, str, float]] = []
        names = list(signatures.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                sim = signatures[names[i]].jaccard(signatures[names[j]])
                if sim >= threshold:
                    pairs.append((names[i], names[j], sim))

        pairs.sort(key=lambda x: -x[2])
        return pairs

    def merge_skills(self, name_a: str, name_b: str, merged_body: str) -> str | None:
        """合并两个未锁定技能。

        Args:
            name_a: 技能 A 名称
            name_b: 技能 B 名称
            merged_body: 合并后的正文

        Returns:
            合并后的技能名称，或 None（操作失败）
        """
        doc_a = self.documents.get(name_a)
        doc_b = self.documents.get(name_b)

        if not doc_a or not doc_b:
            return None
        if doc_a.manifest.locked or doc_b.manifest.locked:
            return None  # 锁定技能不参与合并

        # 新名称取较长的
        new_name = name_a if len(name_a) >= len(name_b) else name_b
        new_desc = f"合并自 {name_a} + {name_b}"

        # 保留较高的 usage_count
        total_usage = doc_a.manifest.usage_count + doc_b.manifest.usage_count

        # 根据参与合并的双方 source 决定合并后的 source
        source_a = doc_a.manifest.source if doc_a else "auto"
        source_b = doc_b.manifest.source if doc_b else "auto"
        new_source = "manual" if (source_a == "manual" or source_b == "manual") else "merged"

        # 创建合并技能
        self.add_skill(
            name=new_name,
            description=new_desc,
            body=merged_body,
            source=new_source,
            locked=False,
        )

        # 更新 usage_count
        merged_doc = self.documents.get(new_name)
        if merged_doc:
            merged_doc.manifest.usage_count = total_usage

        # 删除原始技能（如果不是同名）
        if new_name != name_a:
            self.delete_skill(name_a)
        if new_name != name_b:
            self.delete_skill(name_b)

        return new_name

    # ── 关键词搜索（用于 evolve_skills_from_memories）─────────────────────────

    def find_by_keyword(self, keywords: list[str]) -> list[str]:
        """查找包含指定关键词的技能。

        Args:
            keywords: 关键词列表

        Returns:
            匹配的技能名称列表
        """
        if not keywords:
            return []

        matches: list[str] = []
        kw_lower = [k.lower() for k in keywords]

        for name, doc in self.documents.items():
            text = f"{name} {doc.manifest.description} {doc.body}".lower()
            if any(kw in text for kw in kw_lower):
                matches.append(name)

        return matches

    def has_guard(self, topic: str) -> bool:
        """检查是否已存在针对指定主题的防御性技能。"""
        topic_lower = topic.lower()
        for name, doc in self.documents.items():
            if topic_lower in name.lower() or topic_lower in doc.manifest.description.lower():
                return True
        return False

    def create_guard_skill(self, topic: str, mistake_content: str) -> str | None:
        """创建防御性技能（从 mistake 记忆触发）。

        质量门槛：内容太短、纯工具输出不创建。

        Args:
            topic: 错误主题
            mistake_content: 错误详情

        Returns:
            新创建的技能名称
        """
        if self.has_guard(topic):
            return None

        # ── 质量门槛 ──
        content_stripped = mistake_content.strip()
        if len(content_stripped) < 50:
            return None
        if _is_tool_output(mistake_content):
            return None

        safe_topic = re.sub(r"[^a-zA-Z0-9\-]", "-", topic.lower())[:30]
        name = f"guard-{safe_topic}"
        body = (
            f"## 触发条件\n"
            f"在执行涉及「{topic}」的操作前，必须检查以下防护措施。\n\n"
            f"## 背景\n"
            f"此守卫技能由历史错误记忆自动创建：\n"
            f"> {mistake_content[:300]}\n\n"
            f"## 防护步骤\n"
            f"1. 确认已备份相关数据\n"
            f"2. 检查前置条件是否满足\n"
            f"3. 准备回滚方案\n"
        )

        self.add_skill(
            name=name,
            description=f"防御性守卫: 防止「{topic}」相关错误重犯",
            body=body,
            source="auto",
            locked=False,
        )
        return name

    # ── 自进化闭环（Phase 3 §4.7）──────────────────────────────────────

    def evolve_skills_from_memories(self, memory_manager) -> dict:
        """自进化闭环 — 从记忆模式驱动技能进化。

        三种跨层链接场景：
        1. feedback 记忆 + 技能 → 技能修正（在技能正文追加约束）
        2. mistake 记忆 → 防御性技能（调用 create_guard_skill）
        3. skill_learned 记忆 → 工具序列聚类 → 新技能候选（通过 auto_create）

        Args:
            memory_manager: MemoryManager 实例（需已 load_all）

        Returns:
            {"corrected": [...], "guarded": [...], "created": [...]}
        """
        result: dict[str, list[str]] = {
            "corrected": [],
            "guarded": [],
            "created": [],
        }

        if not hasattr(memory_manager, "memories"):
            return result

        memories = memory_manager.memories

        # ── 场景 1: feedback 记忆 → 技能修正 ──
        for name, mem in memories.items():
            if mem.get("type") != "feedback":
                continue
            content = mem.get("content", "")
            # 长度过滤：碎片消息跳过
            if len(content.strip()) < 10:
                continue
            # 内容过滤：不含技术约束/规则/要求的聊天消息跳过
            _technical_indicators = (
                "禁止", "必须", "不要", "不允许", "规范", "约束", "规则",
                "要求", "应该", "总是", "始终", "确保", "避免", "优先",
                "forbidden", "must", "always", "never", "should", "require",
            )
            if not any(ind in content for ind in _technical_indicators):
                continue
            # 提取关键词，查找相关技能（提高阈值）
            keywords = self._extract_keywords_from_text(
                f"{name} {mem.get('description', '')} {content}"
            )
            if not keywords:
                continue
            # 使用相关性评分代替宽泛的 find_by_keyword
            related_skills = self._find_skills_by_relevance(keywords, threshold=0.3, min_hits=2)
            for skill_name in related_skills:
                if not self.can_ai_modify(skill_name):
                    continue
                doc = self.documents.get(skill_name)
                if not doc:
                    continue
                # 去重检查：基于记忆名判断是否已追加过
                dedup_marker = f"来自记忆 {name}"
                if dedup_marker in doc.body:
                    continue
                # 在技能正文追加约束
                constraint = f"\n\n> ⚠️ 用户反馈约束（{dedup_marker}）:\n> {content[:200]}\n"
                new_body = doc.body + constraint
                self.update_skill(
                    old_name=skill_name,
                    new_name=skill_name,
                    description=doc.manifest.description,
                    body=new_body,
                )
                result["corrected"].append(skill_name)

        # ── 场景 2: mistake 记忆 → 防御性技能（仅高重要性）──
        for name, mem in memories.items():
            if mem.get("type") != "mistake":
                continue
            # 重要性过滤：低重要性的 mistake 不值得创建守卫
            importance = float(mem.get("importance", 0.5))
            if importance < 0.5:
                continue
            topic = name.replace("_", " ").replace("-", " ")
            if not self.has_guard(topic):
                guard_name = self.create_guard_skill(
                    topic=topic,
                    mistake_content=mem.get("content", mem.get("description", "")),
                )
                if guard_name:
                    result["guarded"].append(guard_name)

        # ── 场景 3: skill_learned 记忆 → 新技能候选 ──
        for name, mem in memories.items():
            if mem.get("type") != "skill_learned":
                continue
            content = mem.get("content", "")
            # 尝试从记忆内容中提取工具序列
            tool_sequence = re.findall(r"`(\w+)`", content)
            if len(tool_sequence) >= 3:
                execution_log = [{"tool": t} for t in tool_sequence]
                created = self.auto_create(
                    task_description=mem.get("description", name),
                    execution_log=execution_log,
                    result=content[:200],
                )
                if created:
                    result["created"].append(created)

        total = sum(len(v) for v in result.values())
        if total > 0:
            print(
                f"[SkillEvolution] 自进化: "
                f"修正 {len(result['corrected'])} 个技能, "
                f"创建 {len(result['guarded'])} 个守卫, "
                f"新建 {len(result['created'])} 个技能"
            )

        return result

    # ── 清理低质量守卫 ────────────────────────────────────────────────────

    def cleanup_low_quality_guards(self) -> list[str]:
        """删除低质量自动生成的守卫技能。

        清理条件：source="auto" + name starts with "guard-" + usage_count=0
        （未被使用过的自动生成守卫视为低质量。）

        Returns:
            被删除的技能名称列表
        """
        to_remove = []
        for name, doc in self.documents.items():
            if (name.startswith("guard-")
                    and doc.manifest.source == "auto"
                    and doc.manifest.usage_count == 0):
                to_remove.append(name)
        for name in to_remove:
            self.delete_skill(name)
        if to_remove:
            print(f"[SkillRegistry] 清理了 {len(to_remove)} 个低质量守卫技能")
        return to_remove

    @staticmethod
    def _extract_keywords_from_text(text: str, top_k: int = 5) -> list[str]:
        """从文本中提取关键词（简单版）。"""
        # 去除标点，提取有意义的词
        words = re.findall(r"[a-zA-Z_]\w{2,}|[一-鿿]{2,}", text)
        # 过滤常见停用词
        stop = {
            "the", "and", "for", "this", "that", "with", "from",
            "not", "are", "was", "but", "all", "can", "had", "has",
            "使用", "需要", "可以", "已经", "没有", "通过",
        }
        filtered = [w.lower() for w in words if w.lower() not in stop]
        # 按频率排序
        freq: dict[str, int] = {}
        for w in filtered:
            freq[w] = freq.get(w, 0) + 1
        sorted_words = sorted(freq, key=lambda w: -freq[w])
        return sorted_words[:top_k]

    def _find_skills_by_relevance(
        self, keywords: list[str], threshold: float = 0.3, min_hits: int = 2
    ) -> list[str]:
        """按相关性评分查找技能，避免宽泛匹配。

        要求关键词命中数 / 总关键词数 >= threshold，且至少 min_hits 个关键词命中。

        Args:
            keywords: 关键词列表
            threshold: 命中比例阈值
            min_hits: 最少命中关键词数

        Returns:
            匹配的技能名称列表
        """
        if not keywords:
            return []

        matches: list[str] = []
        kw_lower = [k.lower() for k in keywords]
        total_kw = len(kw_lower)

        for name, doc in self.documents.items():
            text = f"{name} {doc.manifest.description} {doc.body}".lower()
            hits = sum(1 for kw in kw_lower if kw in text)
            ratio = hits / total_kw if total_kw > 0 else 0
            if hits >= min_hits and ratio >= threshold:
                matches.append(name)

        return matches


SKILL_REGISTRY = SkillRegistry(SKILLS_DIR)
