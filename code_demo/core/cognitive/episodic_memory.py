"""流动式情景记忆系统 — 四层衰减架构核心实现

层级：Hot → Skeleton → Trash → Core
- Hot：即时缓冲，按 category_path 分桶
- Skeleton：骨架摘要，同话题合并
- Trash：衰减后细节暂存，按日期+分类分区，3天清理
- Core：用户强调/高频命中 → 永久保留
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from .episodic_index import CategoryIndex, EpisodicCategory

logger = logging.getLogger(__name__)


def get_topic_key(category_path: str) -> str:
    """从 category_path 提取 topic 作为分桶 key。兼容旧格式（纯字符串直接返回）。"""
    try:
        parsed = json.loads(category_path)
        if isinstance(parsed, dict) and "topic" in parsed:
            return parsed["topic"]
    except (json.JSONDecodeError, TypeError):
        pass
    return category_path  # 旧格式：原样返回


@dataclass
class EpisodicEvent:
    """单条情景记忆事件"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # 事件唯一标识
    timestamp: float = field(default_factory=time.time)  # 事件发生时间
    event_type: str = "conversation"        # conversation / action / observation / emotion
    summary: str = ""  # 事件摘要
    detail: str = ""  # 事件详细内容
    category_path: str = "日常互动"  # 事件所属记忆分类路径
    date_key: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))  # 事件对应的日期索引键
    participants: list[str] = field(default_factory=lambda: ["user", "ai"])  # 参与该事件的角色
    emotion: str = "neutral"  # 事件关联的情绪标签
    causal_link: str | None = None          # 因果前驱事件 id
    layer: str = "hot"                      # hot / skeleton / trash / core
    access_count: int = 0  # 事件被访问的次数
    last_accessed: float = 0  # 事件最近一次访问时间
    user_emphasis: int = 0                  # 用户强调次数
    decay_at: float = 0                     # 预定衰减时间戳
    trash_entered_at: float = 0             # 进入 trash 的时间戳
    linked_detail_ids: list[str] = field(default_factory=list)  # 关联的详细事件 ID
    recall_miss_count: int = 0              # 骨架层未命中计数
    needs_correction: bool = False  # 事件是否等待用户纠正
    correction_context: str = ""  # 用户纠正事件时提供的上下文

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的字典"""
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodicEvent":
        """从字典反序列化"""
        # 过滤掉不属于 dataclass 字段的键
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


class EpisodicMemory:
    """流动式情景记忆主控 — 管理四层存储与衰减生命周期"""

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)  # 情景记忆数据的根目录
        self._ensure_dirs()

        # --- Hot 层：按 category_path 分桶 ---
        self.hot_events: dict[str, list[EpisodicEvent]] = defaultdict(list)  # 按日期保存的近期事件

        # --- Skeleton 层：category_path → EpisodicEvent ---
        self.skeleton_events: dict[str, EpisodicEvent] = {}  # 按主题保存的摘要骨架事件

        # --- Trash 层：date_key → category_path → [events] ---
        self.trash_store: dict[str, dict[str, list[EpisodicEvent]]] = defaultdict(
            lambda: defaultdict(list)
        )  # 已衰减并暂存的事件数据

        # --- Core 层 ---
        self.core_events: list[EpisodicEvent] = []  # 长期保留的核心事件

        # --- 分类索引 ---
        self.category_index = CategoryIndex()  # 情景记忆分类与日期索引

        # --- LLM 回调（由 engine 注入）---
        self._llm_callback: Optional[Callable] = None  # 调用语言模型辅助整理记忆的回调

        # 加载已有数据
        self.load()

    def _ensure_dirs(self):
        """确保存储目录结构存在"""
        for sub in ["hot", "skeleton", "trash"]:
            (self.storage_dir / sub).mkdir(parents=True, exist_ok=True)

    # ========== 写入 ==========

    def record_event(
        self,
        event_type: str,
        content: str,
        category_path: str,
        emotion: str = "neutral",
        causal_link: str | None = None,
    ) -> EpisodicEvent:
        """Hot 层写入一条新事件"""
        from ..config import EPISODIC_HOT_DURATION

        now = time.time()
        date_key = datetime.now().strftime("%Y-%m-%d")
        event = EpisodicEvent(
            event_type=event_type,
            summary=self._extract_one_line_summary(content),
            detail=content,
            category_path=category_path,
            date_key=date_key,
            emotion=emotion,
            causal_link=causal_link,
            layer="hot",
            decay_at=now + EPISODIC_HOT_DURATION,
        )

        bucket_key = get_topic_key(category_path)
        self.hot_events[bucket_key].append(event)

        # 更新分类索引
        cat = self.category_index.get_or_create(date_key, bucket_key)
        cat.detail_ids.append(event.id)
        cat.total_events += 1

        return event

    # ========== 衰减 ==========

    async def decay_hot_events(self):
        """衰减清洗：将过期的 Hot 事件合并到 Skeleton 或移入 Trash"""
        now = time.time()
        categories_to_process = list(self.hot_events.keys())

        for cat_path in categories_to_process:
            events = self.hot_events[cat_path]
            expired = [e for e in events if e.decay_at <= now]
            remaining = [e for e in events if e.decay_at > now]

            if not expired:
                continue

            # 更新 hot 层
            if remaining:
                self.hot_events[cat_path] = remaining
            else:
                del self.hot_events[cat_path]

            # 合并到 Skeleton
            if cat_path in self.skeleton_events:
                # 已有骨架 → 合并更新（cat_path 已是 topic key）
                existing = self.skeleton_events[cat_path]
                merged_summary = await self._llm_merge_skeleton(
                    existing.summary, expired
                )
                existing.summary = merged_summary
                MAX_LINKED_IDS = 200
                existing.linked_detail_ids.extend(e.id for e in expired)
                if len(existing.linked_detail_ids) > MAX_LINKED_IDS:
                    existing.linked_detail_ids = existing.linked_detail_ids[-MAX_LINKED_IDS:]
                existing.last_accessed = now
            else:
                # 新建骨架
                skeleton_summary = await self._llm_summarize_to_skeleton(expired)
                skeleton_event = EpisodicEvent(
                    event_type="skeleton",
                    summary=skeleton_summary,
                    detail="",
                    category_path=expired[0].category_path,  # 保留完整 JSON
                    date_key=expired[0].date_key,
                    layer="skeleton",
                    linked_detail_ids=[e.id for e in expired],
                )
                self.skeleton_events[cat_path] = skeleton_event  # key 为 topic

                # 更新分类索引中的 skeleton_id
                idx_cat = self.category_index.get_or_create(
                    expired[0].date_key, cat_path
                )
                idx_cat.skeleton_id = skeleton_event.id

            # 过期事件移入 Trash
            for e in expired:
                e.layer = "trash"
                e.trash_entered_at = now
                self.trash_store[e.date_key][cat_path].append(e)

    def cleanup_trash(self):
        """按分区 3 天清理 Trash 层过期事件"""
        from ..config import EPISODIC_TRASH_TTL

        now = time.time()
        empty_dates = []

        for date_key, cat_dict in self.trash_store.items():
            empty_cats = []
            for cat_path, events in cat_dict.items():
                # 过滤保留未过期的
                alive = [e for e in events if (now - e.trash_entered_at) < EPISODIC_TRASH_TTL]
                if alive:
                    cat_dict[cat_path] = alive
                else:
                    empty_cats.append(cat_path)
            for c in empty_cats:
                del cat_dict[c]
            if not cat_dict:
                empty_dates.append(date_key)

        for d in empty_dates:
            del self.trash_store[d]

    # ========== 检索 ==========

    async def deep_recall(self, query: str, skeleton: EpisodicEvent | None = None) -> list[EpisodicEvent]:
        """分类定向 Trash 检索 — 支持跨日期查找同一话题"""
        results: list[EpisodicEvent] = []

        # 确定目标分类（使用 topic key 匹配 trash_store 的桶 key）
        target_categories: list[str] = []
        if skeleton:
            target_categories.append(get_topic_key(skeleton.category_path))
            skeleton.access_count += 1
            skeleton.last_accessed = time.time()
        else:
            # 根据 query 在所有分类中查找
            for cat_path in set(
                path
                for cats in self.trash_store.values()
                for path in cats.keys()
            ):
                target_categories.append(cat_path)

        # 在 Trash 中按分类检索
        for date_key, cat_dict in self.trash_store.items():
            for cat_path in target_categories:
                if cat_path in cat_dict:
                    results.extend(cat_dict[cat_path])

        # 按相关性排序
        ranked = self._rank_by_relevance(query, results)
        return ranked[:20]

    def check_skeleton_decay(self):
        """骨架层未命中降级：25 次未命中则从 Skeleton 移除"""
        from ..config import EPISODIC_SKELETON_MISS_LIMIT

        to_remove = []
        for cat_path, skel in self.skeleton_events.items():
            if skel.recall_miss_count >= EPISODIC_SKELETON_MISS_LIMIT:
                to_remove.append(cat_path)

        for cat_path in to_remove:
            skel = self.skeleton_events.pop(cat_path)
            skel.layer = "trash"
            skel.trash_entered_at = time.time()
            # cat_path 已是 topic key（skeleton_events 的 key）
            self.trash_store[skel.date_key][cat_path].append(skel)

    async def merge_cross_date_skeletons(self, category_path: str):
        """跨时间合并：将同一 category_path 不同日期的骨架合并为单个条目"""
        from ..config import EPISODIC_CROSS_DATE_MERGE_DAYS

        cats = self.category_index.find_categories_by_path(category_path)
        if len(cats) < 2:
            return

        # 按时间排序
        cats.sort(key=lambda c: c.date_key)

        # 收集相关骨架
        skeletons_to_merge: list[EpisodicEvent] = []
        for cat in cats:
            if cat.skeleton_id:
                skel = self._get_event_by_id(cat.skeleton_id)
                if skel:
                    skeletons_to_merge.append(skel)

        if len(skeletons_to_merge) < 2:
            return

        # 合并
        base = skeletons_to_merge[0]
        others = skeletons_to_merge[1:]

        merged_summary = await self._llm_merge_skeleton(
            base.summary,
            others
        )
        base.summary = merged_summary
        base.last_accessed = time.time()
        MAX_LINKED_IDS = 200
        for other in others:
            base.linked_detail_ids.extend(other.linked_detail_ids)
        if len(base.linked_detail_ids) > MAX_LINKED_IDS:
            base.linked_detail_ids = base.linked_detail_ids[-MAX_LINKED_IDS:]

        # 移除已合并的骨架
        for other in others:
            topic_key = get_topic_key(other.category_path)
            if topic_key in self.skeleton_events and self.skeleton_events[topic_key].id == other.id:
                del self.skeleton_events[topic_key]

        # 确保合并后的骨架在索引中
        self.skeleton_events[category_path] = base

        # 更新索引
        for cat in cats:
            cat.skeleton_id = base.id
            cat.last_merged_at = time.time()

    # ========== 意图处理 ==========

    async def handle_memory_intent(self, intent: str, user_input: str) -> str:
        """处理记忆相关意图：recall_request / emphasis / correction"""
        from ..config import EPISODIC_EMPHASIS_THRESHOLD

        if intent == "recall_request":
            # 先查 skeleton
            skeleton = self._find_skeleton_by_topic(user_input)
            if skeleton:
                skeleton.access_count += 1
                skeleton.last_accessed = time.time()
                # 尝试 deep recall 获取细节
                details = await self.deep_recall(user_input, skeleton)
                if details:
                    detail_text = "\n".join(f"- {d.summary}" for d in details[:5])
                    return f"[骨架] {skeleton.summary}\n[细节]\n{detail_text}"
                return f"[骨架] {skeleton.summary}"
            else:
                # 直接搜索所有层
                related = self._find_related_events_by_topic(user_input)
                if related:
                    return "\n".join(f"- {e.summary}" for e in related[:5])
                return ""

        elif intent == "emphasis":
            # 用户强调 → 升级为 Core
            related = self._find_related_events_by_topic(user_input)
            for event in related:
                event.user_emphasis += 1
                if event.user_emphasis >= EPISODIC_EMPHASIS_THRESHOLD:
                    event.layer = "core"
                    self.core_events.append(event)
                    # 从原位置移除
                    self._remove_from_current_layer(event)
            return f"已标记强调（共 {len(related)} 条相关记忆）"

        elif intent == "correction":
            related = self._find_related_events_by_topic(user_input)
            for event in related[:3]:
                event.needs_correction = True
                event.correction_context = user_input
            return f"已标记需更正（{len(related[:3])} 条）"

        return ""

    # ========== 上下文摘要 ==========

    def get_recent_summary(self, max_items: int = 5) -> str:
        """获取最近事件摘要 — 供 companion 注入上下文"""
        recent: list[EpisodicEvent] = []

        # 从 Hot 层取最近事件
        for events in self.hot_events.values():
            recent.extend(events)

        # 按时间倒序
        recent.sort(key=lambda e: e.timestamp, reverse=True)
        recent = recent[:max_items]

        if not recent:
            return ""

        lines = []
        for e in recent:
            time_str = datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
            topic = get_topic_key(e.category_path)
            lines.append(f"[{time_str}] ({topic}) {e.summary}")
        return "\n".join(lines)

    # ========== 持久化 ==========

    def save(self):
        """持久化所有层到磁盘"""
        self._ensure_dirs()

        # 1. Hot 层 — 按日期分文件
        hot_by_date: dict[str, list[dict]] = defaultdict(list)
        for cat_path, events in self.hot_events.items():
            for e in events:
                hot_by_date[e.date_key].append(e.to_dict())
        # 清空 hot 目录旧文件
        hot_dir = self.storage_dir / "hot"
        for f in hot_dir.glob("*.json"):
            f.unlink()
        for date_key, event_dicts in hot_by_date.items():
            (hot_dir / f"{date_key}.json").write_text(
                json.dumps(event_dicts, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # 2. Skeleton 层 — index.json
        skel_dir = self.storage_dir / "skeleton"
        skel_data = {
            path: e.to_dict() for path, e in self.skeleton_events.items()
        }
        (skel_dir / "index.json").write_text(
            json.dumps(skel_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 3. Trash 层 — 按日期+分类
        trash_dir = self.storage_dir / "trash"
        # 清空旧文件
        for sub in trash_dir.iterdir():
            if sub.is_dir():
                for f in sub.glob("*.json"):
                    f.unlink()
                if not any(sub.iterdir()):
                    sub.rmdir()
        for date_key, cat_dict in self.trash_store.items():
            date_dir = trash_dir / date_key
            date_dir.mkdir(exist_ok=True)
            for cat_path, events in cat_dict.items():
                safe_name = cat_path.replace("/", "__")
                (date_dir / f"{safe_name}.json").write_text(
                    json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )

        # 4. Core 层
        (self.storage_dir / "core.json").write_text(
            json.dumps([e.to_dict() for e in self.core_events], ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # 5. 分类索引
        (self.storage_dir / "category_index.json").write_text(
            json.dumps(self.category_index.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def load(self):
        """从磁盘加载所有层 — 文件不存在时优雅降级"""
        # 1. Hot 层
        hot_dir = self.storage_dir / "hot"
        if hot_dir.exists():
            for f in hot_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    for item in data:
                        event = EpisodicEvent.from_dict(item)
                        bucket_key = get_topic_key(event.category_path)
                        self.hot_events[bucket_key].append(event)
                except (json.JSONDecodeError, KeyError):
                    continue

        # 2. Skeleton 层
        skel_file = self.storage_dir / "skeleton" / "index.json"
        if skel_file.exists():
            file_size = skel_file.stat().st_size
            if file_size > 50 * 1024 * 1024:  # 50 MB 保护阈值
                logger.warning(
                    f"skeleton/index.json 膨胀 ({file_size / 1024 / 1024:.1f} MB)，执行流式压缩..."
                )
                self._compress_skeleton_file(skel_file)
                # 压缩后重新读取
                try:
                    data = json.loads(skel_file.read_text(encoding="utf-8"))
                    for path, event_data in data.items():
                        self.skeleton_events[path] = EpisodicEvent.from_dict(event_data)
                except (json.JSONDecodeError, KeyError):
                    pass
            else:
                try:
                    data = json.loads(skel_file.read_text(encoding="utf-8"))
                    for path, event_data in data.items():
                        self.skeleton_events[path] = EpisodicEvent.from_dict(event_data)
                except (json.JSONDecodeError, KeyError):
                    pass

        # 3. Trash 层
        trash_dir = self.storage_dir / "trash"
        if trash_dir.exists():
            for date_dir in trash_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                date_key = date_dir.name
                for f in date_dir.glob("*.json"):
                    cat_path = f.stem.replace("__", "/")
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        for item in data:
                            event = EpisodicEvent.from_dict(item)
                            self.trash_store[date_key][cat_path].append(event)
                    except (json.JSONDecodeError, KeyError):
                        continue

        # 4. Core 层
        core_file = self.storage_dir / "core.json"
        if core_file.exists():
            try:
                data = json.loads(core_file.read_text(encoding="utf-8"))
                self.core_events = [EpisodicEvent.from_dict(item) for item in data]
            except (json.JSONDecodeError, KeyError):
                pass

        # 5. 分类索引
        idx_file = self.storage_dir / "category_index.json"
        if idx_file.exists():
            try:
                data = json.loads(idx_file.read_text(encoding="utf-8"))
                self.category_index = CategoryIndex.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                pass

    # ========== Skeleton 压缩 ==========

    def _compress_skeleton_file(self, filepath: Path):
        """流式压缩超大 skeleton/index.json。

        两级处理策略：
        - 50-500 MB：内存加载 + 截断 linked_detail_ids + 原子写回
        - >500 MB：备份 + 重置（无法安全内存加载）
        """
        MAX_LINKED_IDS = 200
        file_size = filepath.stat().st_size

        if file_size > 500 * 1024 * 1024:
            # 超大文件（>500MB）：备份后重置
            backup = filepath.with_name(filepath.name + ".overflow-backup")
            shutil.move(str(filepath), str(backup))
            filepath.write_text("{}", encoding="utf-8")
            logger.warning(
                f"skeleton 文件过大 ({file_size / 1024 / 1024:.0f}MB) "
                f"无法内存压缩，已备份到 {backup.name} 并重置"
            )
            return

        # 50-500 MB：内存加载 + 压缩
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)

            # 对每个 entry 截断 linked_detail_ids
            for key, entry in raw_data.items():
                if isinstance(entry, dict) and "linked_detail_ids" in entry:
                    ids = entry["linked_detail_ids"]
                    if isinstance(ids, list) and len(ids) > MAX_LINKED_IDS:
                        # 去重 + 保留最近的 MAX_LINKED_IDS 个
                        seen = set()
                        unique = []
                        for id_ in reversed(ids):
                            if id_ not in seen:
                                seen.add(id_)
                                unique.append(id_)
                            if len(unique) >= MAX_LINKED_IDS:
                                break
                        entry["linked_detail_ids"] = list(reversed(unique))

            # 写入临时文件后原子替换
            tmp_path = filepath.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(raw_data, f, ensure_ascii=False)

            os.replace(str(tmp_path), str(filepath))
            new_size = filepath.stat().st_size
            logger.info(
                f"skeleton 压缩完成: {file_size / 1024 / 1024:.1f}MB → {new_size / 1024:.1f}KB"
            )

        except Exception as e:
            logger.error(f"skeleton 压缩失败 ({e})，备份并重置")
            backup = filepath.with_name(filepath.name + ".compress-failed-backup")
            try:
                shutil.copy2(str(filepath), str(backup))
            except Exception:
                pass
            filepath.write_text("{}", encoding="utf-8")

    # ========== LLM 回调 ==========

    async def _llm_summarize_to_skeleton(self, events: list[EpisodicEvent]) -> str:
        """调用 LLM 生成骨架摘要"""
        if not self._llm_callback:
            # 降级：拼接各事件的 summary
            return " → ".join(e.summary for e in events[:5])
        prompt = (
            "将以下事件压缩为一段骨架摘要（保留关键结论、因果链、情绪走向）：\n"
            + "\n".join(f"- {e.summary}" for e in events)
        )
        return await self._llm_callback(prompt)

    async def _llm_merge_skeleton(self, existing_summary: str, new_events: list[EpisodicEvent]) -> str:
        """合并已有骨架和新事件"""
        if not self._llm_callback:
            new_part = " → ".join(e.summary for e in new_events[:3])
            return f"{existing_summary}；后续：{new_part}"
        prompt = (
            f"已有骨架摘要：{existing_summary}\n新增事件：\n"
            + "\n".join(f"- {e.summary}" for e in new_events)
            + "\n请合并为一段更新后的骨架摘要。"
        )
        return await self._llm_callback(prompt)

    # ========== 内部工具方法 ==========

    def _extract_one_line_summary(self, content: str) -> str:
        """程序化提取摘要：取第一句或前50字"""
        if not content:
            return ""
        # 按句号/问号/感叹号切第一句
        for sep in ("。", "？", "！", ".", "?", "!"):
            idx = content.find(sep)
            if 0 < idx < 80:
                return content[: idx + 1]
        # 截取前50字
        return content[:50] + ("..." if len(content) > 50 else "")

    def _rank_by_relevance(self, query: str, events: list[EpisodicEvent]) -> list[EpisodicEvent]:
        """简单关键词匹配排序"""
        if not query or not events:
            return events

        query_chars = set(query)

        def score(e: EpisodicEvent) -> float:
            text = e.summary + " " + e.detail
            # 字符级重叠度
            overlap = len(query_chars & set(text))
            # 关键词命中
            keyword_hits = sum(1 for w in query if len(w) > 1 and w in text)
            # 时间新鲜度加权
            recency = 1.0 / (1.0 + (time.time() - e.timestamp) / 86400)
            return overlap * 0.3 + keyword_hits * 2.0 + recency * 0.5

        events.sort(key=score, reverse=True)
        return events

    def _find_skeleton_by_topic(self, topic: str) -> EpisodicEvent | None:
        """在 Skeleton 层按话题查找"""
        best_score = 0
        best_skel = None
        for cat_path, skel in self.skeleton_events.items():
            # 简单匹配：分类路径或摘要包含关键词
            score = sum(1 for w in topic if w in cat_path or w in skel.summary)
            if score > best_score:
                best_score = score
                best_skel = skel
        # 未命中计数
        if best_skel and best_score > len(topic) * 0.3:
            return best_skel
        # 所有骨架都未命中 → 增加 miss count
        for skel in self.skeleton_events.values():
            skel.recall_miss_count += 1
        return None

    def _find_related_events_by_topic(self, topic: str) -> list[EpisodicEvent]:
        """在所有层中查找与 topic 相关的事件"""
        all_events: list[EpisodicEvent] = []

        # Hot 层
        for events in self.hot_events.values():
            all_events.extend(events)
        # Skeleton 层
        all_events.extend(self.skeleton_events.values())
        # Core 层
        all_events.extend(self.core_events)

        return self._rank_by_relevance(topic, all_events)[:10]

    def _get_event_by_id(self, event_id: str) -> EpisodicEvent | None:
        """全局按 ID 查找事件"""
        # Hot
        for events in self.hot_events.values():
            for e in events:
                if e.id == event_id:
                    return e
        # Skeleton
        for skel in self.skeleton_events.values():
            if skel.id == event_id:
                return skel
        # Trash
        for cat_dict in self.trash_store.values():
            for events in cat_dict.values():
                for e in events:
                    if e.id == event_id:
                        return e
        # Core
        for e in self.core_events:
            if e.id == event_id:
                return e
        return None

    def _remove_from_current_layer(self, event: EpisodicEvent):
        """从事件当前所在层移除"""
        topic_key = get_topic_key(event.category_path)
        # Hot
        if topic_key in self.hot_events:
            self.hot_events[topic_key] = [
                e for e in self.hot_events[topic_key] if e.id != event.id
            ]
            if not self.hot_events[topic_key]:
                del self.hot_events[topic_key]
        # Skeleton
        if topic_key in self.skeleton_events:
            if self.skeleton_events[topic_key].id == event.id:
                del self.skeleton_events[topic_key]
        # Trash
        for cat_dict in self.trash_store.values():
            if topic_key in cat_dict:
                cat_dict[topic_key] = [
                    e for e in cat_dict[topic_key] if e.id != event.id
                ]
