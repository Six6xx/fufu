"""查询路由 — 意图分类 + 检索策略分配"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QueryPlan:
    """查询执行计划"""
    query: str                     # 原始查询
    query_type: str                # memory / skill / knowledge / structure / hybrid
    strategies: list[str]          # 检索策略列表，如 ["fts5", "vector", "graph"]
    token_budget: int = 2000       # 返回结果的 token 预算
    confidence: float = 0.0        # 分类置信度


# 查询模式 — 正则匹配
QUERY_PATTERNS: dict[str, list[str]] = {
    "memory": [
        r"还记得|上次|之前|以前|偏好|喜欢|习惯|记得|说过|提过",
        r"我(?:曾|上次|之前)(?:说|提|聊|问|做)",
        r"你(?:还)?记得",
    ],
    "skill": [
        r"怎么做|步骤|流程|部署|教程|方法|操作|指南",
        r"如何|怎样|怎么(?:操作|配置|设置|安装|使用)",
        r"(?:帮我|请)(?:做|执行|运行|部署)",
    ],
    "knowledge": [
        r"什么是|解释|原理|概念|定义|含义|区别",
        r"(?:为什么|为啥|原因)(?:是|会)",
        r"介绍|说明|描述|概述",
    ],
    "structure": [
        r"调用|依赖|模块|函数|类|结构|引用|节点|社区",
        r"哪个(?:文件|模块|函数|类)(?:实现|包含|调用)",
        r"代码(?:结构|架构|关系|依赖)",
    ],
}


class QueryRouter:
    """查询路由器

    基于正则模式匹配对用户查询进行意图分类，
    然后分配对应的检索策略组合。

    路由策略映射：
    - memory → ThreeLayerMemory.recall()
    - skill → SkillRegistry.search() + ThreeLayerMemory.recall()
    - knowledge → HybridRetriever.search() + VectorStore.search()
    - structure → graph_tools.graph_query()
    - hybrid → 多策略合并 + Reranker 重排
    """

    # 按查询意图选择的检索策略
    STRATEGY_MAP: dict[str, list[str]] = {
        "memory": ["fts5", "markdown", "profile"],
        "skill": ["skill_registry", "fts5"],
        "knowledge": ["vector", "bm25", "hybrid"],
        "structure": ["graph_query", "graph_neighbors"],
        "hybrid": ["fts5", "vector", "bm25", "graph_query", "skill_registry"],
    }  # 不同查询意图使用的检索策略

    # 各查询意图对应的 Token 预算
    BUDGET_MAP: dict[str, int] = {
        "memory": 1500,
        "skill": 2000,
        "knowledge": 3000,
        "structure": 2000,
        "hybrid": 2500,
    }  # 不同查询意图对应的检索 Token 预算

    # 触发 fallback 的置信度阈值
    FALLBACK_THRESHOLD = 0.1  # 低于该置信度时改用 fallback 路由

    def __init__(self, custom_patterns: Optional[dict[str, list[str]]] = None):
        """
        Args:
            custom_patterns: 自定义匹配模式（合并到默认模式）
        """
        self.patterns: dict[str, list[str]] = {
            qtype: list(patterns)
            for qtype, patterns in QUERY_PATTERNS.items()
        }  # 查询类型到识别规则的映射
        if custom_patterns:
            for qtype, patterns in custom_patterns.items():
                self.patterns.setdefault(qtype, []).extend(patterns)

    def route(self, query: str) -> QueryPlan:
        """对查询进行意图分类并生成执行计划

        流程：
        1. 逐类型正则匹配，计算匹配分数
        2. 最高分类型作为 query_type
        3. 无明确匹配 → fallback 到 hybrid
        4. 根据 STRATEGY_MAP 分配策略

        Args:
            query: 用户查询文本
        Returns:
            QueryPlan 执行计划
        """
        query_type, confidence = self._classify(query)
        return QueryPlan(
            query=query,
            query_type=query_type,
            strategies=list(self.STRATEGY_MAP[query_type]),
            token_budget=self.BUDGET_MAP[query_type],
            confidence=round(confidence, 3),
        )

    def route_batch(self, queries: list[str]) -> list[QueryPlan]:
        """批量路由"""
        return [self.route(q) for q in queries]

    def explain(self, query: str) -> str:
        """解释路由决策（调试用）

        Returns:
            人类可读的路由决策说明
        """
        plan = self.route(query)
        matched = self._collect_matches(query)

        lines = [
            f"查询: {query}",
            f"分类结果: {plan.query_type} (置信度 {plan.confidence})",
            f"分配策略: {', '.join(plan.strategies)}",
            f"Token 预算: {plan.token_budget}",
        ]

        if plan.query_type in matched and matched[plan.query_type]:
            lines.append(f"命中模式: {', '.join(matched[plan.query_type])}")
        elif plan.query_type == "hybrid":
            lines.append("未命中任何明确模式，fallback 到 hybrid 多策略合并。")

        other_hits = [
            (qtype, patterns)
            for qtype, patterns in matched.items()
            if qtype != plan.query_type and patterns
        ]
        if other_hits:
            parts = [f"{qtype}({', '.join(patterns)})" for qtype, patterns in other_hits]
            lines.append(f"其他类型命中: {'; '.join(parts)}")

        return "\n".join(lines)

    def _match_score(self, query: str, patterns: list[str]) -> float:
        """计算查询与模式列表的匹配分数

        策略：匹配到的模式数 / 总模式数 + 首个匹配位置权重
        """
        if not patterns:
            return 0.0

        matched_count = 0
        first_pos: Optional[int] = None
        for pattern in patterns:
            match = re.search(pattern, query)
            if match:
                matched_count += 1
                if first_pos is None or match.start() < first_pos:
                    first_pos = match.start()

        if matched_count == 0:
            return 0.0

        base_score = matched_count / len(patterns)
        # 首个匹配位置越靠前，加分越多；最长查询长度限制 bonus 上限
        position_bonus = 0.0
        if first_pos is not None and len(query) > 0:
            position_bonus = (1 - min(first_pos, len(query) - 1) / len(query)) * 0.1

        return min(base_score + position_bonus, 1.0)

    def _classify(self, query: str) -> tuple[str, float]:
        """分类查询意图

        Returns:
            (query_type, confidence)
        """
        scores: dict[str, float] = {
            qtype: self._match_score(query, patterns)
            for qtype, patterns in self.patterns.items()
        }

        best_type = max(scores, key=lambda k: scores[k])
        best_score = scores[best_type]

        if best_score < self.FALLBACK_THRESHOLD:
            return "hybrid", best_score
        return best_type, best_score

    def _collect_matches(self, query: str) -> dict[str, list[str]]:
        """收集每个类型实际命中的正则模式（用于 explain）"""
        matched: dict[str, list[str]] = {}
        for qtype, patterns in self.patterns.items():
            hits = [p for p in patterns if re.search(p, query)]
            if hits:
                matched[qtype] = hits
        return matched
