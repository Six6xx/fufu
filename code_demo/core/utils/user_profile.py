"""code_demo/core/utils/user_profile.py — 渐进式用户画像模块

从对话中自动提取用户偏好、风格和项目背景信息，
持久化存储为 JSON 文件，并提供 get_relevant(query) 检索接口，
与 ThreeLayerMemory 配合使用。

设计决策：
  - 不依赖 LLM：extract() 使用正则+关键词规则提取，零 token 成本
  - 持久化格式：JSON 文件存储在 ai_configuration/.memory/profile/
  - 去重机制：同 category + key 的事实只保留最新/最高置信度版本
  - 检索方式：基于 jieba 分词的关键词匹配 + category 亲和度
  - 渐进积累：每次会话结束后调用 extract() + update() 逐步丰富画像
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..config import MEMORY_DIR


# ── 数据模型 ──────────────────────────────────────────────────────────────────

VALID_CATEGORIES = ("preference", "style", "background", "habit")

# category 亲和度权重：不同类别对不同查询类型的匹配加分
CATEGORY_AFFINITY = {
    "preference": {"偏好", "喜欢", "习惯", "选择", "框架", "工具", "语言"},
    "style": {"回复", "回答", "输出", "格式", "语言风格", "简洁", "详细"},
    "background": {"项目", "技术栈", "架构", "技术", "开发", "代码库"},
    "habit": {"每次", "总是", "记住", "不要", "必须", "规则"},
}


# ── 提取规则 ──────────────────────────────────────────────────────────────────

EXTRACT_PATTERNS: dict[str, list[str]] = {
    "preference": [
        r"我(?:喜欢|偏好|倾向|习惯)(?:用|使用)?(.+)",
        r"(?:用|使用)(.+?)(?:而不是|不用|别用)(.+)",
        r"我(?:更|比较)(?:喜欢|习惯)(.+)",
    ],
    "style": [
        r"(?:回复|回答|输出)(?:要)?(简洁|详细|用中文|用英文|精简)",
        r"(?:不要|别)(?:太|过于)?(啰嗦|冗长|简短)",
        r"(?:代码|注释)(?:要)?(用中文|用英文)",
    ],
    "background": [
        r"(?:这个|这|我的)项目(?:是|用的是|基于)(.+)",
        r"(?:我们|我)(?:在做|正在开发|在写)(.+)",
        r"技术栈(?:是|用的)(.+)",
    ],
    "habit": [
        r"(?:每次|总是|一定要|记住)(.+)",
        r"(?:不要|别|禁止)(?:再)?(.+)",
        r"(?:从现在开始|以后)(.+)",
    ],
}

# key 提取辅助：从匹配到的 value 中提取简短标识
_KEY_PATTERNS = [
    (r"(?:pytest|unittest|jest|mocha)", "test_framework"),
    (r"(?:python|java|go|rust|typescript|javascript)", "language"),
    (r"(?:react|vue|next\.?js|flask|django|fastapi)", "framework"),
    (r"(?:docker|k8s?|kubernetes)", "infra"),
    (r"(?:git|svn)", "vcs"),
    (r"(?:中文|英文|english|chinese)", "language_pref"),
    (r"(?:简洁|详细|精简|啰嗦)", "verbosity"),
]


# ── ProfileFact ────────────────────────────────────────────────────────────────

@dataclass
class ProfileFact:
    """用户画像事实。"""

    category: str       # "preference" | "style" | "background" | "habit"
    key: str            # 简短标识，如 "test_framework"
    value: str          # 具体内容，如 "偏好 pytest 而非 unittest"
    confidence: float   # 0.0-1.0 置信度
    source: str         # 来源会话 ID 或描述
    created_at: float   # 时间戳
    updated_at: float   # 最后更新时间戳

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ProfileFact:
        return cls(**d)


# ── 中文分词辅助 ──────────────────────────────────────────────────────────────

def _segment(text: str) -> list[str]:
    """使用 jieba 分词（如果可用），否则按字符拆分 CJK。"""
    try:
        import jieba  # type: ignore[import-untyped]
        words = list(jieba.cut(text))
        return [w.strip() for w in words if w.strip()]
    except ImportError:
        # fallback: CJK 逐字 + Latin 整词
        return re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text)


def _extract_key(value: str) -> str:
    """从匹配值中提取简短 key 标识。"""
    for pattern, key in _KEY_PATTERNS:
        if re.search(pattern, value, re.IGNORECASE):
            return key
    # fallback: 取前 20 字符并清理
    clean = re.sub(r"[^\w\u4e00-\u9fff]", "_", value)[:20].strip("_")
    return clean if clean else "misc"


# ── UserProfile ────────────────────────────────────────────────────────────────

class UserProfile:
    """渐进式用户建模。

    从对话中自动提取偏好/风格/项目背景，持久化为 JSON 文件。
    支持按查询相关性检索画像片段。
    """

    def __init__(self, profile_dir: Optional[Path] = None):
        """
        Args:
            profile_dir: 画像存储目录（默认 ai_configuration/.memory/profile/）
        """
        self._profile_dir = profile_dir or (MEMORY_DIR / "profile")  # 用户画像数据目录
        self._facts: list[ProfileFact] = []  # 已提取并保存的用户画像事实
        self._load()

    # ── 核心方法：提取 ──────────────────────────────────────────────────────

    def extract(self, messages: list[dict], source: str = "") -> list[ProfileFact]:
        """从对话消息中提取用户画像事实（不调用 LLM，基于规则+关键词）。

        提取规则：
          - "我喜欢/偏好/习惯..." → preference
          - "回复简洁/详细/用中文..." → style
          - "这个项目是/用的是..." → background
          - "每次都要/不要/记住..." → habit

        Args:
            messages: 对话消息列表 [{"role": "user/assistant", "content": "..."}]
            source: 来源标识（如会话 ID）

        Returns:
            提取到的事实列表
        """
        extracted: list[ProfileFact] = []
        now = time.time()

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue

            for category, patterns in EXTRACT_PATTERNS.items():
                for pattern in patterns:
                    match = re.search(pattern, content)
                    if not match:
                        continue

                    # 拼接所有捕获组
                    groups = [g for g in match.groups() if g]
                    value = " ".join(groups).strip()
                    if not value:
                        continue

                    key = _extract_key(value)
                    # 同 category+key 不重复提取（保留首次匹配）
                    if any(f.category == category and f.key == key
                           for f in extracted):
                        continue

                    fact = ProfileFact(
                        category=category,
                        key=key,
                        value=value,
                        confidence=0.7,  # 规则提取默认置信度
                        source=source or "conversation",
                        created_at=now,
                        updated_at=now,
                    )
                    extracted.append(fact)

        return extracted

    # ── 核心方法：检索 ──────────────────────────────────────────────────────

    def get_relevant(self, query: str, top_k: int = 3) -> list[str]:
        """检索与查询相关的用户画像片段。

        策略：关键词匹配 + category 权重

        Args:
            query: 查询字符串
            top_k: 最多返回条数

        Returns:
            格式化的画像片段列表
        """
        if not self._facts:
            return []

        query_terms = set(_segment(query))
        if not query_terms:
            return []

        scored: list[tuple[float, ProfileFact]] = []

        for fact in self._facts:
            score = 0.0

            # (1) 关键词匹配：在 value 中命中查询词
            fact_terms = set(_segment(fact.value))
            overlap = query_terms & fact_terms
            if overlap:
                score += len(overlap) / max(len(query_terms), 1) * 3.0

            # (2) key 精确匹配
            if fact.key in query.lower():
                score += 2.0

            # (3) category 亲和度
            affinity_words = CATEGORY_AFFINITY.get(fact.category, set())
            if query_terms & affinity_words:
                score += 1.5

            # (4) 置信度加权
            score *= (0.5 + 0.5 * fact.confidence)

            if score > 0:
                scored.append((score, fact))

        if not scored:
            return []

        scored.sort(key=lambda x: -x[0])
        results: list[str] = []
        for _, fact in scored[:top_k]:
            results.append(f"[{fact.category}] {fact.key}: {fact.value}")
        return results

    # ── 核心方法：更新 ──────────────────────────────────────────────────────

    def update(self, facts: list[ProfileFact]) -> None:
        """更新画像库。

        - 同 category + key 的事实：更新 value 和 confidence（取较高值）
        - 新 key：直接添加
        """
        now = time.time()

        for new_fact in facts:
            # 查找同 category + key 的已有事实
            existing_idx = None
            for i, existing in enumerate(self._facts):
                if existing.category == new_fact.category and existing.key == new_fact.key:
                    existing_idx = i
                    break

            if existing_idx is not None:
                old = self._facts[existing_idx]
                # 取较高置信度版本
                if new_fact.confidence >= old.confidence:
                    old.value = new_fact.value
                    old.confidence = new_fact.confidence
                    old.source = new_fact.source
                old.updated_at = now
            else:
                self._facts.append(new_fact)

        self._save()

    # ── 辅助方法 ────────────────────────────────────────────────────────────

    def get_all_facts(self) -> list[ProfileFact]:
        """获取所有画像事实。"""
        return list(self._facts)

    def get_summary(self) -> str:
        """生成画像摘要文本（用于 prompt 注入）。"""
        if not self._facts:
            return ""

        sections: list[str] = ["# User Profile", ""]

        for cat in VALID_CATEGORIES:
            cat_facts = [f for f in self._facts if f.category == cat]
            if not cat_facts:
                continue

            cat_label = {
                "preference": "偏好",
                "style": "风格",
                "background": "项目背景",
                "habit": "习惯",
            }.get(cat, cat)

            sections.append(f"## {cat_label}")
            for f in cat_facts:
                sections.append(f"- **{f.key}**: {f.value}")
            sections.append("")

        return "\n".join(sections)

    # ── 持久化 ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """从 JSON 文件加载。"""
        json_path = self._profile_dir / "user_profile.json"
        if not json_path.exists():
            self._facts = []
            return

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self._facts = [ProfileFact.from_dict(d) for d in data]
        except (json.JSONDecodeError, TypeError, KeyError):
            self._facts = []

    def _save(self) -> None:
        """持久化到 JSON 文件。"""
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        json_path = self._profile_dir / "user_profile.json"

        data = [f.to_dict() for f in self._facts]
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._facts)

    def __repr__(self) -> str:
        return f"UserProfile(facts={len(self._facts)})"
