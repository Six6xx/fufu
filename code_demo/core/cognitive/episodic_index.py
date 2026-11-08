"""情景记忆分类索引 — 层级索引核心
支持 日期→大类→子类 的三级分类组织，避免全量遍历。
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class EpisodicCategory:
    """话题分类节点 — 作为 Skeleton 和 Trash 的索引中间层"""
    category_path: str          # 如 "技术讨论/认知循环优化"
    date_key: str               # "2026-06-19"
    skeleton_id: str | None = None  # 该分类的摘要骨架事件 ID
    detail_ids: list[str] = field(default_factory=list)  # 该分类关联的详细事件 ID
    last_merged_at: float = 0  # 最近一次合并时间
    created_at: float = field(default_factory=time.time)  # 分类记录创建时间
    total_events: int = 0  # 该分类累计事件数


@dataclass
class CategoryIndex:
    """全局分类索引 — 二级字典 date_key→category_path→EpisodicCategory"""
    index: dict[str, dict[str, EpisodicCategory]] = field(default_factory=dict)  # 按日期和分类保存的事件索引
    known_categories: list[str] = field(default_factory=lambda: [
        "技术讨论", "日常互动", "情绪/感受", "计划/安排", "学习/知识"
    ])  # 当前已知的记忆分类名称

    def get_category(self, date_key: str, category_path: str) -> EpisodicCategory | None:
        return self.index.get(date_key, {}).get(category_path)

    def get_or_create(self, date_key: str, category_path: str) -> EpisodicCategory:
        if date_key not in self.index:
            self.index[date_key] = {}
        if category_path not in self.index[date_key]:
            self.index[date_key][category_path] = EpisodicCategory(
                category_path=category_path, date_key=date_key
            )
        return self.index[date_key][category_path]

    def find_categories_by_path(self, path_prefix: str) -> list[EpisodicCategory]:
        """跨日期查找某个分类路径的所有实例"""
        results = []
        for date_cats in self.index.values():
            for path, cat in date_cats.items():
                if path.startswith(path_prefix):
                    results.append(cat)
        return results

    def all_date_keys(self) -> list[str]:
        return sorted(self.index.keys())

    def to_dict(self) -> dict:
        """序列化为可JSON化的字典"""
        result = {"known_categories": self.known_categories, "index": {}}
        for date_key, cats in self.index.items():
            result["index"][date_key] = {}
            for path, cat in cats.items():
                result["index"][date_key][path] = {
                    "category_path": cat.category_path,
                    "date_key": cat.date_key,
                    "skeleton_id": cat.skeleton_id,
                    "detail_ids": cat.detail_ids,
                    "last_merged_at": cat.last_merged_at,
                    "created_at": cat.created_at,
                    "total_events": cat.total_events,
                }
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "CategoryIndex":
        """从字典反序列化"""
        idx = cls(known_categories=data.get("known_categories", [
            "技术讨论", "日常互动", "情绪/感受", "计划/安排", "学习/知识"
        ]))
        for date_key, cats in data.get("index", {}).items():
            idx.index[date_key] = {}
            for path, cat_data in cats.items():
                idx.index[date_key][path] = EpisodicCategory(**cat_data)
        return idx
