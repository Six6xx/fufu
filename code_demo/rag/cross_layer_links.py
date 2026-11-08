"""跨层链接检测 — 三策略发现知识图谱节点间隐含关联"""

import hashlib
import random
import re
import time
from dataclasses import dataclass

from .unified_graph import UnifiedGraph, GraphNode, GraphEdge


@dataclass
class DetectedLink:
    """检测到的跨层链接"""

    source_id: str  # 关联关系的起始节点 ID
    target_id: str  # 关联关系的目标节点 ID
    relation: str  # feedback_to / guards / implements / related_to
    confidence: str  # HIGH / MEDIUM / LOW
    evidence: str  # 检测依据描述
    strategy: str  # exact_match / keyword_overlap / semantic_similarity


class CrossLayerLinkDetector:
    """跨层链接检测器

    三种检测策略：
    1. 名称精确匹配（HIGH 置信度）：归一化后比对节点名称
    2. 关键词重叠（MEDIUM 置信度）：≥2 个共享关键词
    3. MinHash 语义推断（LOW 置信度）：内容 Jaccard > 0.5
    """

    STOP_WORDS = {
        "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
        "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    }
    _CONFIDENCE_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    _MH_PRIME = (1 << 61) - 1
    _MH_MAX = (1 << 32) - 1

    def __init__(
        self,
        graph: UnifiedGraph,
        keyword_threshold: int = 2,
        jaccard_threshold: float = 0.5,
    ):
        """
        Args:
            graph: 统一图谱实例
            keyword_threshold: 关键词重叠阈值
            jaccard_threshold: Jaccard 相似度阈值
        """
        self.graph = graph  # 待分析跨层关系的统一图谱
        self.keyword_threshold = keyword_threshold  # 关键词相似度判为关联的阈值
        self.jaccard_threshold = jaccard_threshold  # Jaccard 相似度判为关联的阈值
        self._mh_rng = random.Random(42)  # 生成 MinHash 参数的固定随机数生成器
        self._mh_a: list[int] = []  # MinHash 线性哈希族的乘数参数
        self._mh_b: list[int] = []  # MinHash 线性哈希族的偏移参数

    def detect_all(self) -> list[DetectedLink]:
        """执行全量跨层链接检测，返回按置信度降序排列的链接。"""
        nodes_by_type = self._nodes_by_type()
        type_names = sorted(nodes_by_type.keys())
        links: list[DetectedLink] = []
        for i, type_a in enumerate(type_names):
            for type_b in type_names[i + 1 :]:
                nodes_a, nodes_b = nodes_by_type[type_a], nodes_by_type[type_b]
                for strategy in (self._strategy_exact_match, self._strategy_keyword_overlap, self._strategy_semantic_similarity):
                    links.extend(strategy(nodes_a, nodes_b))
                    links.extend(strategy(nodes_b, nodes_a))
        return self._dedup_and_sort(links)

    def detect_for_node(self, node_id: str) -> list[DetectedLink]:
        """检测指定节点的跨层链接。"""
        source = self.graph.get_node(node_id)
        if source is None:
            return []
        others = [n for n in self._all_nodes() if n.id != node_id]
        links: list[DetectedLink] = []
        for strategy in (self._strategy_exact_match, self._strategy_keyword_overlap, self._strategy_semantic_similarity):
            links.extend(strategy([source], others))
            links.extend(strategy(others, [source]))
        return self._dedup_and_sort(links)

    def apply_links(
        self, links: list[DetectedLink], min_confidence: str = "MEDIUM"
    ) -> int:
        """将检测到的链接写入图谱，返回实际写入的边数。"""
        min_value = self._CONFIDENCE_ORDER[min_confidence]
        count = 0
        now = time.time()
        for link in links:
            if self._CONFIDENCE_ORDER[link.confidence] < min_value:
                continue
            self.graph.add_edge(
                GraphEdge(
                    source_id=link.source_id,
                    target_id=link.target_id,
                    relation=link.relation,
                    confidence=link.confidence,
                    weight=self._CONFIDENCE_ORDER[link.confidence] / 3.0,
                    layer="cross",
                    created_at=now,
                )
            )
            count += 1
        return count

    def _strategy_exact_match(
        self, nodes_a: list[GraphNode], nodes_b: list[GraphNode]
    ) -> list[DetectedLink]:
        """策略1：名称精确匹配 → HIGH 置信度。"""
        links: list[DetectedLink] = []
        for a in nodes_a:
            norm_a = self._normalize_name(a.label)
            if not norm_a:
                continue
            for b in nodes_b:
                if a.id != b.id and norm_a == self._normalize_name(b.label):
                    links.append(
                        DetectedLink(
                            source_id=a.id,
                            target_id=b.id,
                            relation=self._determine_relation(a, b),
                            confidence="HIGH",
                            evidence=f"normalized label match: '{norm_a}'",
                            strategy="exact_match",
                        )
                    )
        return links

    def _strategy_keyword_overlap(
        self, nodes_a: list[GraphNode], nodes_b: list[GraphNode]
    ) -> list[DetectedLink]:
        """策略2：关键词重叠 → MEDIUM 置信度。"""
        links: list[DetectedLink] = []
        keywords = {a.id: self._extract_keywords(a) for a in nodes_a}
        for a in nodes_a:
            kw_a = keywords[a.id]
            if len(kw_a) < self.keyword_threshold:
                continue
            for b in nodes_b:
                if a.id != b.id:
                    shared = kw_a & self._extract_keywords(b)
                    if len(shared) >= self.keyword_threshold:
                        links.append(
                            DetectedLink(
                                source_id=a.id,
                                target_id=b.id,
                                relation=self._determine_relation(a, b),
                                confidence="MEDIUM",
                                evidence=f"shared keywords: {len(shared)} ({', '.join(sorted(shared))})",
                                strategy="keyword_overlap",
                            )
                        )
        return links

    def _strategy_semantic_similarity(
        self, nodes_a: list[GraphNode], nodes_b: list[GraphNode]
    ) -> list[DetectedLink]:
        """策略3：MinHash 语义推断 → LOW 置信度。"""
        links: list[DetectedLink] = []
        signatures = {
            node.id: self._minhash_signature(self._node_text(node))
            for node in nodes_a + nodes_b
        }
        for a in nodes_a:
            sig_a = signatures[a.id]
            for b in nodes_b:
                if a.id != b.id:
                    jaccard = self._jaccard_from_minhash(sig_a, signatures[b.id])
                    if jaccard > self.jaccard_threshold:
                        links.append(
                            DetectedLink(
                                source_id=a.id,
                                target_id=b.id,
                                relation=self._determine_relation(a, b),
                                confidence="LOW",
                                evidence=f"MinHash Jaccard estimate: {jaccard:.3f}",
                                strategy="semantic_similarity",
                            )
                        )
        return links

    def _extract_keywords(self, node: GraphNode) -> set[str]:
        """从节点提取关键词集合。"""
        text = self._node_text(node)
        try:
            import jieba
            tokens = list(jieba.lcut(text))
        except Exception:
            tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower())
        keywords: set[str] = set()
        for tok in tokens:
            tok = tok.strip().lower()
            if tok and tok not in self.STOP_WORDS and (len(tok) > 1 or "\u4e00" <= tok <= "\u9fff"):
                keywords.add(tok)
        return keywords

    def _normalize_name(self, name: str) -> str:
        """归一化名称用于精确匹配。"""
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", name.lower()).strip()

    def _determine_relation(self, source: GraphNode, target: GraphNode) -> str:
        """根据节点类型推断关系类型。"""
        if source.node_type == "memory" and target.node_type == "skill":
            memory_type = source.properties.get("memory_type", "")
            if memory_type == "feedback":
                return "feedback_to"
            if memory_type == "mistake":
                return "guards"
        if source.node_type in ("session", "code_entity") and target.node_type == "skill":
            return "implements"
        return "related_to"

    def _minhash_signature(self, text: str, num_perm: int = 128) -> list[int]:
        """计算 MinHash 签名。"""
        text = self._normalize_name(text)
        shingles = [text[i : i + 3] for i in range(len(text) - 2)] if len(text) >= 3 else ([text] if text else [])
        if not shingles:
            return [0] * num_perm
        self._ensure_hash_params(num_perm)
        base_hashes = [int(hashlib.sha1(s.encode("utf-8")).hexdigest()[:8], 16) for s in shingles]
        signature: list[int] = []
        for i in range(num_perm):
            min_val = self._MH_MAX
            ai, bi = self._mh_a[i], self._mh_b[i]
            for h in base_hashes:
                perm = ((ai * h + bi) % self._MH_PRIME) & self._MH_MAX
                if perm < min_val:
                    min_val = perm
            signature.append(min_val)
        return signature

    def _jaccard_from_minhash(self, sig_a: list[int], sig_b: list[int]) -> float:
        """从 MinHash 签名估算 Jaccard 相似度。"""
        if not sig_a or not sig_b or len(sig_a) != len(sig_b):
            return 0.0
        return sum(a == b for a, b in zip(sig_a, sig_b)) / len(sig_a)

    def _ensure_hash_params(self, num_perm: int) -> None:
        """按需扩展 MinHash 哈希函数参数。"""
        while len(self._mh_a) < num_perm:
            self._mh_a.append(self._mh_rng.randint(1, self._MH_PRIME))
            self._mh_b.append(self._mh_rng.randint(0, self._MH_PRIME))

    def _all_nodes(self) -> list[GraphNode]:
        """获取图谱中所有节点。"""
        cur = self.graph._conn.execute(
            "SELECT id, label, type, layer, source, properties_json, created_at, updated_at FROM nodes"
        )
        return [self.graph._row_to_node(row) for row in cur.fetchall()]

    def _nodes_by_type(self) -> dict[str, list[GraphNode]]:
        """按节点类型分组。"""
        groups: dict[str, list[GraphNode]] = {}
        for node in self._all_nodes():
            groups.setdefault(node.node_type, []).append(node)
        return groups

    def _node_text(self, node: GraphNode) -> str:
        """合并节点可用于相似度计算的文本。"""
        parts = [node.label]
        for key in ("description", "content", "name", "source"):
            value = node.properties.get(key)
            if isinstance(value, str):
                parts.append(value)
        if node.source:
            parts.append(node.source)
        return " ".join(parts)

    def _dedup_and_sort(self, links: list[DetectedLink]) -> list[DetectedLink]:
        """按 (source_id, target_id) 去重保留最高置信度，再按置信度降序。"""
        best: dict[tuple[str, str], DetectedLink] = {}
        for link in links:
            key = (link.source_id, link.target_id)
            existing = best.get(key)
            if existing is None or self._CONFIDENCE_ORDER[link.confidence] > self._CONFIDENCE_ORDER[existing.confidence]:
                best[key] = link
        return sorted(
            best.values(),
            key=lambda x: (-self._CONFIDENCE_ORDER[x.confidence], x.strategy, x.source_id, x.target_id),
        )
