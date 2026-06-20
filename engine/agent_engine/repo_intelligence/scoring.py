"""Relevance scoring for graph nodes — configurable weights, not hardcoded."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import GraphNode, RelevanceScore, RepoIntelConfig


@dataclass
class ScoreWeights:
    semantic_match: float = 0.30
    graph_proximity: float = 0.25
    runtime_importance: float = 0.20
    change_frequency: float = 0.10
    test_relationship: float = 0.15
    distance_penalty: float = 0.05

    def normalize(self) -> ScoreWeights:
        total = (
            self.semantic_match
            + self.graph_proximity
            + self.runtime_importance
            + self.change_frequency
            + self.test_relationship
        )
        if total == 0:
            return ScoreWeights(0.2, 0.2, 0.2, 0.2, 0.2, self.distance_penalty)
        return ScoreWeights(
            semantic_match=self.semantic_match / total,
            graph_proximity=self.graph_proximity / total,
            runtime_importance=self.runtime_importance / total,
            change_frequency=self.change_frequency / total,
            test_relationship=self.test_relationship / total,
            distance_penalty=self.distance_penalty,
        )


class RelevanceScorer:
    """Scores graph nodes by relevance to a request."""

    def __init__(self, weights: ScoreWeights | None = None, config: RepoIntelConfig | None = None) -> None:
        self._config = config or RepoIntelConfig()
        self.weights = (weights or ScoreWeights()).normalize()

    def score_nodes(
        self,
        nodes: list[GraphNode],
        seed_keywords: list[str],
        understanding: dict,
        *,
        graph_distances: dict[str, int] | None = None,
        change_history: dict[str, int] | None = None,
        test_relationships: dict[str, list[str]] | None = None,
    ) -> list[RelevanceScore]:
        distances = graph_distances or {}
        history = change_history or {}
        test_map = test_relationships or {}
        results: list[RelevanceScore] = []

        for node in nodes:
            sm = self._semantic_match(node, seed_keywords)
            gp = self._graph_proximity(node.id, distances)
            ri = self._runtime_importance(node, understanding)
            cf = self._change_frequency(node.id, history)
            tr = self._test_relationship(node.id, test_map)
            dist = distances.get(node.id, 1)

            total = (
                sm * self.weights.semantic_match
                + gp * self.weights.graph_proximity
                + ri * self.weights.runtime_importance
                + cf * self.weights.change_frequency
                + tr * self.weights.test_relationship
                - (dist * self.weights.distance_penalty)
            )

            results.append(RelevanceScore(
                node_id=node.id,
                semantic_match=sm,
                graph_proximity=gp,
                runtime_importance=ri,
                change_frequency=cf,
                test_relationship=tr,
                distance_penalty=dist * self.weights.distance_penalty,
                total=max(0.0, min(1.0, total)),
            ))

        return sorted(results, key=lambda s: s.total, reverse=True)

    def _semantic_match(self, node: GraphNode, keywords: list[str]) -> float:
        if not keywords:
            return 0.3
        text = f"{node.name} {node.file_path}".lower()
        hits = sum(1 for kw in keywords if kw.lower() in text)
        # Partial match bonus
        for kw in keywords:
            parts = re.split(r"[_.\-/]", kw)
            for part in parts:
                if len(part) >= 3 and part.lower() in text:
                    hits += 0.3
        return min(1.0, hits / max(1, len(keywords)))

    def _graph_proximity(self, node_id: str, distances: dict[str, int]) -> float:
        if not distances or node_id not in distances:
            return 0.5
        d = distances[node_id]
        if d <= 1:
            return 1.0
        return max(0.0, 1.0 - (d - 1) * 0.2)

    def _runtime_importance(self, node: GraphNode, understanding: dict) -> float:
        score = 0.3
        name_lower = node.name.lower()
        # Entrypoints score higher
        if node.node_type in {"route", "service", "class"}:
            score += 0.2
        # Matches goal keywords
        goal = str(understanding.get("goal", "")).lower()
        if goal and any(part.lower() in name_lower for part in goal.split() if len(part) >= 3):
            score += 0.3
        # Core infrastructure
        if node.node_type in {"configuration", "repository", "table"}:
            score += 0.1
        return min(1.0, score)

    def _change_frequency(self, node_id: str, history: dict[str, int]) -> float:
        if not history:
            return 0.3
        count = history.get(node_id, 0)
        if count >= 10:
            return 1.0
        if count >= 5:
            return 0.7
        return max(0.1, count * 0.1)

    def _test_relationship(self, node_id: str, test_map: dict[str, list[str]]) -> float:
        if not test_map:
            return 0.3
        tests = test_map.get(node_id, [])
        if not tests:
            return 0.0
        return min(1.0, len(tests) * 0.25)
