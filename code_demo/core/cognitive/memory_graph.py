"""认知图谱记忆系统 — 三级召回 + LLM 动态聚类。

复用 memory_relevance.py 的 4 信号评分和 dream.py 的整合管道，
扩展为支持动态簇管理和三级召回策略的认知记忆。

三级召回策略：
  Level 1（默认）: 模糊/联想搜索 — 语义相似度 + 4 信号评分
  Level 2（explicit_key 非空）: 精确查询 — 直接匹配 node title/tag
  Level 3（精确未命中）: 自动回退 Level 1

设计理念：记忆默认是联想式的（像人回忆），只有刻意想某件具体事时才精确检索，精确失败不阻塞。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..companion_agent import CompanionAgent

from ..utils.memory_relevance import (
    MemoryGraph,
    MemoryNode,
    build_memory_graph,
    calculate_relevance,
    retrieve_relevant_memories,
)
from ..utils.dream import DreamConsolidator, Fragment
from ..config import MEMORY_DIR


# ── 数据结构 ──────────────────────────────────────────────────────────────────


@dataclass
class Cluster:
    """记忆簇：一组语义相关的记忆节点。"""

    tag: str  # 簇标签（如 "architecture_concern", "user_emotion_pattern"）
    description: str = ""  # 簇描述（LLM 生成）
    node_ids: list[str] = field(default_factory=list)  # 属于此簇的节点 ID
    created_at: float = field(default_factory=time.time)  # 记忆簇创建时间
    access_count: int = 0  # 被访问次数（用于热度排序）
    last_accessed: float = 0.0  # 最近一次访问该簇的时间


# ── 主类 ──────────────────────────────────────────────────────────────────────


class CognitiveMemory:
    """图谱驱动的认知记忆系统。

    核心能力：
    1. 三级召回策略（模糊优先 → 精确 → 回退模糊）
    2. LLM 动态聚类（新记忆自动归簇）
    3. 与 DreamConsolidator 协作（去重/衰减/合并）
    """

    CLUSTER_FILE = "cognitive_clusters.json"  # 簇持久化文件
    MAX_CLUSTERS = 50  # 最大簇数量

    def __init__(
        self,
        companion: "CompanionAgent",
        memory_dir: Path | None = None,
    ):
        self.companion = companion  # 关联的伴侣对象
        self.memory_dir = memory_dir or MEMORY_DIR  # 认知记忆文件所在目录
        # 确保目录存在
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # 图谱（延迟构建）
        self._graph: Optional[MemoryGraph] = None  # 当前加载的记忆关系图
        self._graph_version: int = 0  # 已加载记忆图的版本号

        # 簇管理
        self.clusters: dict[str, Cluster] = {}  # tag -> Cluster
        self._load_clusters()

        # 整合器
        self.consolidator = DreamConsolidator(self.memory_dir)  # 整理和合并记忆的组件

    @property
    def graph(self) -> MemoryGraph:
        """延迟构建/重建记忆图谱。"""
        if self._graph is None:
            self._graph = build_memory_graph(self.memory_dir, self._graph_version)
        return self._graph

    def invalidate_graph(self):
        """标记图谱需要重建（记忆变更后调用）。"""
        self._graph = None
        self._graph_version += 1

    # ────────────────────────────────────────────────────
    # 三级召回
    # ────────────────────────────────────────────────────

    async def recall(
        self,
        focus: str,
        top_k: int = 5,
        explicit_key: Optional[str] = None,
        context_sources: Optional[set[str]] = None,
    ) -> list[tuple[MemoryNode, float]]:
        """三级召回策略。

        Level 1（默认）: 模糊/联想搜索
        Level 2（explicit_key 非空）: 精确匹配 title/tag
        Level 3（精确未命中）: 自动回退 Level 1

        Args:
            focus: 当前思维焦点（用于模糊检索）
            top_k: 返回最多 N 条
            explicit_key: 若非空，先尝试精确匹配
            context_sources: 上下文来源（提升相关性）

        Returns:
            [(MemoryNode, score)] 列表，按 score 降序
        """
        if explicit_key:
            # Level 2: 精确匹配
            exact_results = self._exact_match(explicit_key, top_k)
            if exact_results:
                # 更新簇访问计数
                self._update_access_stats(exact_results)
                return exact_results
            # Level 3: 精确未命中，回退模糊

        # Level 1: 模糊/联想检索
        fuzzy_results = self._fuzzy_recall(focus, top_k, context_sources)
        if fuzzy_results:
            self._update_access_stats(fuzzy_results)
        return fuzzy_results

    def _exact_match(self, key: str, top_k: int) -> list[tuple[MemoryNode, float]]:
        """精确匹配：按 title 或 tag 直接查找。"""
        results: list[tuple[MemoryNode, float]] = []
        key_lower = key.lower()

        for node in self.graph.nodes.values():
            score = 0.0

            # 标题完全匹配（最高优先级）
            title_lower = node.title.lower()
            if title_lower == key_lower:
                score = 12.0
            elif key_lower in title_lower:
                score = 10.0
            elif title_lower in key_lower:
                score = 9.0

            # 簇标签匹配
            if score == 0.0:
                node_clusters = self._get_node_clusters(node.id)
                for cluster_tag in node_clusters:
                    if key_lower in cluster_tag.lower() or cluster_tag.lower() in key_lower:
                        score = 8.0
                        break

            if score > 0.0:
                results.append((node, score))

        # 按分数降序
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def _fuzzy_recall(
        self,
        focus: str,
        top_k: int,
        context_sources: Optional[set[str]] = None,
    ) -> list[tuple[MemoryNode, float]]:
        """模糊/联想检索：利用现有 4 信号评分。"""
        return retrieve_relevant_memories(
            query=focus,
            graph=self.graph,
            context_sources=context_sources,
            context_memory_ids=None,
            top_k=top_k,
        )

    def _get_node_clusters(self, node_id: str) -> list[str]:
        """获取节点所属的所有簇标签。"""
        return [tag for tag, cluster in self.clusters.items() if node_id in cluster.node_ids]

    def _update_access_stats(self, results: list[tuple[MemoryNode, float]]):
        """更新召回结果中涉及的簇的访问统计。"""
        now = time.time()
        touched_tags: set[str] = set()
        for node, _ in results:
            for tag in self._get_node_clusters(node.id):
                touched_tags.add(tag)

        for tag in touched_tags:
            if tag in self.clusters:
                self.clusters[tag].access_count += 1
                self.clusters[tag].last_accessed = now

        if touched_tags:
            self._save_clusters()

    # ────────────────────────────────────────────────────
    # 随机联想召回（为 Task 108 联想种子准备）
    # ────────────────────────────────────────────────────

    def random_recall(self, exclude_topic: str = "") -> str | None:
        """随机抽取一条非当前焦点的记忆（用于联想种子）。

        优先选择 confidence=high 且最近未被召回的记忆。
        """
        # 收集所有可用记忆节点
        candidates: list[MemoryNode] = []
        for node in self.graph.nodes.values():
            # 排除当前焦点相关的
            if exclude_topic and exclude_topic in (node.title or ""):
                continue
            candidates.append(node)

        if not candidates:
            return None

        # 加权随机：high 权重3，medium 权重2，low 权重1
        weights: list[int] = []
        for node in candidates:
            conf = getattr(node, "confidence", "medium")
            if conf == "high":
                weights.append(3)
            elif conf == "medium":
                weights.append(2)
            else:
                weights.append(1)

        selected = random.choices(candidates, weights=weights, k=1)[0]
        return selected.title or (selected.path.split("/")[-1] if selected.path else None)

    # ────────────────────────────────────────────────────
    # 记忆写入（memorize）
    # ────────────────────────────────────────────────────

    async def memorize(
        self,
        content: str,
        cluster_tag: str = "",
        source: str = "cognitive_loop",
        confidence: float = 0.8,
        source_type: str = "",
    ) -> Optional[str]:
        """将想法写入长期记忆图谱。

        Args:
            content: 记忆内容
            cluster_tag: 目标簇（为空则 LLM 自动分配）
            source: 来源标识
            confidence: 置信度数值（用于 autonomous_action 来源的分级映射）
            source_type: 来源类型（user_explicit/ai_inference/task_result/cognitive_personality）

        Returns:
            新记忆的 ID，或 None（失败时）
        """
        # ── 置信度分级：根据 source_type 自动推断等级 ──
        confidence_level = self._resolve_confidence_level(source_type, confidence)

        # 1. 构建 Fragment
        fragment = Fragment(
            name=content[:30],
            description=content,
            mem_type="skill_learned",  # 默认类型
            content=content,
            keywords=self._extract_keywords(content),
            source=source,
            confidence=confidence,
        )

        # 2. 确定目标簇
        if not cluster_tag:
            cluster_tag = await self._auto_cluster(content)

        # 3. 保存为 markdown 文件（含置信度分级）
        memory_id = self._save_memory_file(
            fragment,
            confidence_level=confidence_level,
            source_type=source_type,
        )
        if not memory_id:
            return None

        # 4. 归入簇
        self._add_to_cluster(memory_id, cluster_tag, content)

        # 5. 图谱失效（下次访问时重建）
        self.invalidate_graph()

        return memory_id

    @staticmethod
    def _resolve_confidence_level(source_type: str, confidence: float) -> str:
        """根据来源类型自动推断置信度等级。

        映射规则：
          - user_explicit → high（用户明确陈述）
          - task_result / autonomous_action → 按数值映射（>0.7→high, 0.4-0.7→medium, <0.4→low）
          - cognitive_personality → medium
          - ai_inference / 其他 → medium
        """
        if source_type == "user_explicit":
            return "high"
        elif source_type in ("task_result", "autonomous_action"):
            if confidence > 0.7:
                return "high"
            elif confidence >= 0.4:
                return "medium"
            else:
                return "low"
        elif source_type == "cognitive_personality":
            return "medium"
        else:
            # ai_inference 或未指定
            return "medium"

    def _extract_keywords(self, text: str) -> list[str]:
        """简单关键词提取（无 LLM 依赖）。"""
        # 提取中文词语（2-4字）+ 英文单词（3字符以上）
        chinese = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
        english = re.findall(r"[a-zA-Z_]{3,}", text)
        return (chinese + english)[:10]

    def _save_memory_file(
        self,
        fragment: Fragment,
        confidence_level: str = "medium",
        source_type: str = "",
    ) -> Optional[str]:
        """保存记忆为 markdown 文件（含置信度分级元数据）。"""
        import hashlib

        # 生成 ID
        memory_id = hashlib.md5(
            f"{fragment.content}{time.time()}".encode()
        ).hexdigest()[:12]

        # 写入文件
        file_path = self.memory_dir / f"{memory_id}.md"
        try:
            content = (
                f"---\n"
                f"name: {fragment.name}\n"
                f"type: {fragment.mem_type}\n"
                f"source: {fragment.source}\n"
                f"keywords: {', '.join(fragment.keywords)}\n"
                f"created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"confidence: {confidence_level}\n"
                f"verified: false\n"
                f"source_type: {source_type}\n"
                f"---\n\n"
                f"{fragment.description}\n"
            )
            file_path.write_text(content, encoding="utf-8")
            return memory_id
        except Exception as e:
            print(f"[CognitiveMemory] Failed to save memory: {e}")
            return None

    # ────────────────────────────────────────────────────
    # 簇管理
    # ────────────────────────────────────────────────────

    async def _auto_cluster(self, content: str) -> str:
        """LLM 自动分配簇标签。"""
        if not self.clusters:
            return "general"

        # 构建现有簇列表
        existing = [f"- {tag}: {c.description}" for tag, c in self.clusters.items()]
        existing_text = "\n".join(existing[:20])

        messages = [
            {
                "role": "system",
                "content": (
                    "你是记忆分类器。根据内容选择最合适的簇标签，或创建新标签。"
                    "只输出一个 JSON 对象，格式：{\"tag\": \"...\", \"new\": true/false, \"description\": \"...\"}"
                ),
            },
            {
                "role": "user",
                "content": f"现有簇：\n{existing_text}\n\n内容：{content}\n\n输出JSON：",
            },
        ]

        try:
            client = self.companion.client
            loop = asyncio.get_event_loop()

            def _run():
                return client.chat.completions.create(
                    model=os.getenv("COGNITIVE_MODEL", "") or self.companion.companion_model,
                    messages=messages,
                    max_tokens=100,
                    temperature=0.3,
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                )

            response = await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=5.0,
            )
            if response.choices:
                data = json.loads(response.choices[0].message.content)
                tag = data.get("tag", "general")
                if data.get("new") and tag not in self.clusters:
                    # 簇数量上限控制
                    if len(self.clusters) >= self.MAX_CLUSTERS:
                        self._evict_coldest_cluster()
                    self.clusters[tag] = Cluster(
                        tag=tag,
                        description=data.get("description", ""),
                    )
                return tag
        except Exception as e:
            print(f"[CognitiveMemory] Auto-cluster failed: {e}")

        return "general"

    def _add_to_cluster(self, node_id: str, tag: str, description: str = ""):
        """将节点加入簇。"""
        if tag not in self.clusters:
            self.clusters[tag] = Cluster(tag=tag, description=description[:50])

        cluster = self.clusters[tag]
        if node_id not in cluster.node_ids:
            cluster.node_ids.append(node_id)
        cluster.access_count += 1
        cluster.last_accessed = time.time()

        self._save_clusters()

    def _evict_coldest_cluster(self):
        """淘汰最冷的簇（访问次数最少且最久未访问）。"""
        if not self.clusters:
            return

        # 找到最冷簇：按 (access_count, last_accessed) 升序
        coldest_tag = min(
            self.clusters.keys(),
            key=lambda t: (self.clusters[t].access_count, self.clusters[t].last_accessed),
        )
        del self.clusters[coldest_tag]

    # ────────────────────────────────────────────────────
    # 簇持久化
    # ────────────────────────────────────────────────────

    def _load_clusters(self):
        """从文件加载簇配置。"""
        cluster_file = self.memory_dir / self.CLUSTER_FILE
        if not cluster_file.exists():
            return

        try:
            data = json.loads(cluster_file.read_text(encoding="utf-8"))
            for tag, info in data.items():
                self.clusters[tag] = Cluster(
                    tag=tag,
                    description=info.get("description", ""),
                    node_ids=info.get("node_ids", []),
                    created_at=info.get("created_at", time.time()),
                    access_count=info.get("access_count", 0),
                    last_accessed=info.get("last_accessed", 0.0),
                )
        except Exception as e:
            print(f"[CognitiveMemory] Failed to load clusters: {e}")

    def _save_clusters(self):
        """持久化簇配置。"""
        cluster_file = self.memory_dir / self.CLUSTER_FILE
        data = {}
        for tag, cluster in self.clusters.items():
            data[tag] = {
                "description": cluster.description,
                "node_ids": cluster.node_ids,
                "created_at": cluster.created_at,
                "access_count": cluster.access_count,
                "last_accessed": cluster.last_accessed,
            }
        try:
            cluster_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[CognitiveMemory] Failed to save clusters: {e}")

    # ────────────────────────────────────────────────────
    # 查询接口
    # ────────────────────────────────────────────────────

    def get_cluster_summary(self) -> dict[str, dict]:
        """获取所有簇的摘要信息。"""
        return {
            tag: {
                "description": cluster.description,
                "size": len(cluster.node_ids),
                "access_count": cluster.access_count,
                "last_accessed": cluster.last_accessed,
            }
            for tag, cluster in self.clusters.items()
        }

    def get_hot_clusters(self, top_k: int = 5) -> list[Cluster]:
        """获取最热门的簇（按访问频率降序）。"""
        sorted_clusters = sorted(
            self.clusters.values(),
            key=lambda c: (c.access_count, c.last_accessed),
            reverse=True,
        )
        return sorted_clusters[:top_k]
