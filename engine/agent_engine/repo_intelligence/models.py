"""Domain models for Repository Intelligence Agent.

Architecture:
  Evidence-backed claims, code-graph nodes/edges, relevance scoring,
  dependency paths, execution flows, architecture boundaries,
  storage impact, change impact, task classification, scope recommendation,
  and the aggregate ContextPack passed to the Planner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Literal type aliases (keeps annotations readable)
# ---------------------------------------------------------------------------

EvidenceType = Literal["source", "graph", "config", "schema", "test"]
NodeType = Literal[
    "file", "module", "class", "function", "method", "route",
    "service", "repository", "table", "event", "tool", "test",
    "configuration",
]
EdgeType = Literal[
    "imports", "calls", "implements", "extends", "reads", "writes",
    "exposes", "registers", "publishes", "consumes", "tests", "configures",
]
DependencyDirection = Literal["inward", "outward", "circular"]
ImpactLevel = Literal["low", "medium", "high", "critical"]
TaskCategory = Literal[
    "feature", "bugfix", "refactor", "performance", "storage",
    "integration", "infrastructure", "test", "documentation",
]
Severity = Literal["low", "medium", "high", "critical"]


# ---------------------------------------------------------------------------
# 1. Evidence
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """A verified claim backed by source code."""

    claim: str
    file_path: str
    symbol: str
    evidence_type: EvidenceType = "source"
    confidence: float = 0.0  # 0-1
    line_range: tuple[int, int] | None = None
    excerpt: str = ""  # max 500 chars
    verified_at: str = field(default_factory=_now)
    id: str = field(default_factory=_new_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim": self.claim,
            "file_path": self.file_path,
            "symbol": self.symbol,
            "line_range": list(self.line_range) if self.line_range else None,
            "evidence_type": self.evidence_type,
            "confidence": self.confidence,
            "excerpt": self.excerpt,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        lr = data.get("line_range")
        return cls(
            id=data.get("id", _new_id()),
            claim=data.get("claim", ""),
            file_path=data.get("file_path", ""),
            symbol=data.get("symbol", ""),
            line_range=tuple(lr) if lr else None,
            evidence_type=data.get("evidence_type", "source"),
            confidence=data.get("confidence", 0.0),
            excerpt=data.get("excerpt", ""),
            verified_at=data.get("verified_at", _now()),
        )


# ---------------------------------------------------------------------------
# 2. GraphNode
# ---------------------------------------------------------------------------


@dataclass
class GraphNode:
    """A node in the code graph representing a code entity."""

    name: str
    file_path: str
    node_type: NodeType
    id: str = field(default_factory=_new_id)
    line_start: int = 0
    line_end: int = 0
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "node_type": self.node_type,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "parent_id": self.parent_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphNode":
        return cls(
            id=data.get("id", _new_id()),
            name=data.get("name", ""),
            node_type=data.get("node_type", "file"),
            file_path=data.get("file_path", ""),
            line_start=data.get("line_start", 0),
            line_end=data.get("line_end", 0),
            parent_id=data.get("parent_id"),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# 3. GraphEdge
# ---------------------------------------------------------------------------


@dataclass
class GraphEdge:
    """An edge representing a relationship between two code-graph nodes."""

    source_id: str
    target_id: str
    edge_type: EdgeType
    id: str = field(default_factory=_new_id)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        return cls(
            id=data.get("id", _new_id()),
            source_id=data.get("source_id", ""),
            target_id=data.get("target_id", ""),
            edge_type=data.get("edge_type", "imports"),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# 4. RelevanceScore
# ---------------------------------------------------------------------------


@dataclass
class RelevanceScore:
    """Relevance scoring for a code-graph node relative to a task."""

    node_id: str
    semantic_match: float = 0.0
    graph_proximity: float = 0.0
    runtime_importance: float = 0.0
    change_frequency: float = 0.0
    test_relationship: float = 0.0
    distance_penalty: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "semantic_match": self.semantic_match,
            "graph_proximity": self.graph_proximity,
            "runtime_importance": self.runtime_importance,
            "change_frequency": self.change_frequency,
            "test_relationship": self.test_relationship,
            "distance_penalty": self.distance_penalty,
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelevanceScore":
        return cls(
            node_id=data.get("node_id", ""),
            semantic_match=data.get("semantic_match", 0.0),
            graph_proximity=data.get("graph_proximity", 0.0),
            runtime_importance=data.get("runtime_importance", 0.0),
            change_frequency=data.get("change_frequency", 0.0),
            test_relationship=data.get("test_relationship", 0.0),
            distance_penalty=data.get("distance_penalty", 0.0),
            total=data.get("total", 0.0),
        )


# ---------------------------------------------------------------------------
# 5. DependencyPath
# ---------------------------------------------------------------------------


@dataclass
class DependencyPath:
    """An ordered path through the code graph from entrypoint to target."""

    path: list[str] = field(default_factory=list)  # ordered node IDs
    distance: int = 0
    edge_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "distance": self.distance,
            "edge_types": self.edge_types,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DependencyPath":
        return cls(
            path=data.get("path", []),
            distance=data.get("distance", 0),
            edge_types=data.get("edge_types", []),
        )


# ---------------------------------------------------------------------------
# 6. CurrentExecutionFlow
# ---------------------------------------------------------------------------


@dataclass
class CurrentExecutionFlow:
    """Reconstructed execution flow through the codebase."""

    entrypoint: str  # node ID
    stages: list[dict[str, Any]] = field(default_factory=list)
    data_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entrypoint": self.entrypoint,
            "stages": self.stages,
            "data_trace": self.data_trace,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CurrentExecutionFlow":
        return cls(
            entrypoint=data.get("entrypoint", ""),
            stages=data.get("stages", []),
            data_trace=data.get("data_trace", []),
        )


# ---------------------------------------------------------------------------
# 7. ArchitectureBoundary
# ---------------------------------------------------------------------------


@dataclass
class ArchitectureBoundary:
    """A layer boundary in the architecture."""

    name: str
    from_layer: str
    to_layer: str
    dependency_direction: DependencyDirection = "inward"
    node_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "from_layer": self.from_layer,
            "to_layer": self.to_layer,
            "dependency_direction": self.dependency_direction,
            "node_ids": self.node_ids,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchitectureBoundary":
        return cls(
            name=data.get("name", ""),
            from_layer=data.get("from_layer", ""),
            to_layer=data.get("to_layer", ""),
            dependency_direction=data.get("dependency_direction", "inward"),
            node_ids=data.get("node_ids", []),
        )


# ---------------------------------------------------------------------------
# 8. StorageImpact
# ---------------------------------------------------------------------------


@dataclass
class StorageImpact:
    """Impact assessment on database / storage layer."""

    tables_affected: list[str] = field(default_factory=list)
    migrations_needed: bool = False
    schema_changes: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables_affected": self.tables_affected,
            "migrations_needed": self.migrations_needed,
            "schema_changes": self.schema_changes,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StorageImpact":
        return cls(
            tables_affected=data.get("tables_affected", []),
            migrations_needed=data.get("migrations_needed", False),
            schema_changes=data.get("schema_changes", []),
            confidence=data.get("confidence", 0.0),
        )


# ---------------------------------------------------------------------------
# 9. ChangeImpact
# ---------------------------------------------------------------------------


@dataclass
class ChangeImpact:
    """Predicted impact of a change."""

    target: str
    level: ImpactLevel = "medium"
    confidence: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    verification_method: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "level": self.level,
            "confidence": self.confidence,
            "evidence_ids": self.evidence_ids,
            "verification_method": self.verification_method,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeImpact":
        return cls(
            target=data.get("target", ""),
            level=data.get("level", "medium"),
            confidence=data.get("confidence", 0.0),
            evidence_ids=data.get("evidence_ids", []),
            verification_method=data.get("verification_method", ""),
        )


# ---------------------------------------------------------------------------
# 10. TaskClassification
# ---------------------------------------------------------------------------


@dataclass
class TaskClassification:
    """Classification of a task into a standard category."""

    category: TaskCategory = "feature"
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "signals": self.signals,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskClassification":
        return cls(
            category=data.get("category", "feature"),
            confidence=data.get("confidence", 0.0),
            signals=data.get("signals", []),
        )


# ---------------------------------------------------------------------------
# 11. RecommendedScope
# ---------------------------------------------------------------------------


@dataclass
class RecommendedScope:
    """Recommended scope for a task — what to include and exclude."""

    included_files: list[str] = field(default_factory=list)
    included_symbols: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)
    excluded_reason: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "included_files": self.included_files,
            "included_symbols": self.included_symbols,
            "excluded_files": self.excluded_files,
            "excluded_reason": self.excluded_reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecommendedScope":
        return cls(
            included_files=data.get("included_files", []),
            included_symbols=data.get("included_symbols", []),
            excluded_files=data.get("excluded_files", []),
            excluded_reason=data.get("excluded_reason", ""),
            confidence=data.get("confidence", 0.0),
        )


# ---------------------------------------------------------------------------
# 12. ContextPack
# ---------------------------------------------------------------------------


def _serialize_list(items: list[Any]) -> list[dict[str, Any]]:
    """Serialize a list of objects that may have to_dict()."""
    return [item.to_dict() if hasattr(item, "to_dict") else item for item in items]


def _deserialize_list(
    data: list[dict[str, Any]], cls: type
) -> list[Any]:
    """Deserialize a list of dicts into dataclass instances."""
    return [cls.from_dict(item) if hasattr(cls, "from_dict") else item for item in data]


@dataclass
class ContextPack:
    """The complete analysis output passed from RIA to the Planner.

    This is the canonical handoff between repository understanding and
    task planning.  Every field is optional to support incremental
    construction and partial analysis.
    """

    request_understanding: dict[str, Any] = field(default_factory=dict)
    repository_snapshot: dict[str, Any] = field(default_factory=dict)
    task_classification: list[TaskClassification] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    relevant_files: list[dict[str, Any]] = field(default_factory=list)
    relevant_symbols: list[dict[str, Any]] = field(default_factory=list)
    dependency_paths: list[DependencyPath] = field(default_factory=list)
    current_execution_flow: list[CurrentExecutionFlow] = field(default_factory=list)
    architecture_boundaries: list[ArchitectureBoundary] = field(default_factory=list)
    storage_impact: StorageImpact = field(default_factory=StorageImpact)
    change_impact_map: list[ChangeImpact] = field(default_factory=list)
    related_tests: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    recommended_scope: RecommendedScope = field(default_factory=RecommendedScope)
    excluded_scope: list[str] = field(default_factory=list)
    graph_status: dict[str, Any] = field(default_factory=dict)
    analysis_confidence: float = 0.0
    created_at: str = field(default_factory=_now)
    analysis_duration_ms: float = 0.0
    id: str = field(default_factory=_new_id)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        si = self.storage_impact
        if isinstance(si, StorageImpact):
            si_dict = si.to_dict()
        elif isinstance(si, dict):
            si_dict = si
        else:
            si_dict = {}

        rs = self.recommended_scope
        if isinstance(rs, RecommendedScope):
            rs_dict = rs.to_dict()
        elif isinstance(rs, dict):
            rs_dict = rs
        else:
            rs_dict = {}

        return {
            "id": self.id,
            "request_understanding": self.request_understanding,
            "repository_snapshot": self.repository_snapshot,
            "task_classification": _serialize_list(self.task_classification),
            "entrypoints": self.entrypoints,
            "relevant_files": self.relevant_files,
            "relevant_symbols": self.relevant_symbols,
            "dependency_paths": _serialize_list(self.dependency_paths),
            "current_execution_flow": _serialize_list(self.current_execution_flow),
            "architecture_boundaries": _serialize_list(self.architecture_boundaries),
            "storage_impact": si_dict,
            "change_impact_map": _serialize_list(self.change_impact_map),
            "related_tests": self.related_tests,
            "constraints": self.constraints,
            "assumptions": self.assumptions,
            "unknowns": self.unknowns,
            "risks": self.risks,
            "evidence": _serialize_list(self.evidence),
            "recommended_scope": rs_dict,
            "excluded_scope": self.excluded_scope,
            "graph_status": self.graph_status,
            "analysis_confidence": self.analysis_confidence,
            "created_at": self.created_at,
            "analysis_duration_ms": self.analysis_duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextPack":
        si_raw = data.get("storage_impact", {})
        si = (
            StorageImpact.from_dict(si_raw)
            if isinstance(si_raw, dict)
            else StorageImpact()
        )

        rs_raw = data.get("recommended_scope", {})
        rs = (
            RecommendedScope.from_dict(rs_raw)
            if isinstance(rs_raw, dict)
            else RecommendedScope()
        )

        return cls(
            id=data.get("id", _new_id()),
            request_understanding=data.get("request_understanding", {}),
            repository_snapshot=data.get("repository_snapshot", {}),
            task_classification=_deserialize_list(
                data.get("task_classification", []), TaskClassification
            ),
            entrypoints=data.get("entrypoints", []),
            relevant_files=data.get("relevant_files", []),
            relevant_symbols=data.get("relevant_symbols", []),
            dependency_paths=_deserialize_list(
                data.get("dependency_paths", []), DependencyPath
            ),
            current_execution_flow=_deserialize_list(
                data.get("current_execution_flow", []), CurrentExecutionFlow
            ),
            architecture_boundaries=_deserialize_list(
                data.get("architecture_boundaries", []), ArchitectureBoundary
            ),
            storage_impact=si,
            change_impact_map=_deserialize_list(
                data.get("change_impact_map", []), ChangeImpact
            ),
            related_tests=data.get("related_tests", []),
            constraints=data.get("constraints", []),
            assumptions=data.get("assumptions", []),
            unknowns=data.get("unknowns", []),
            risks=data.get("risks", []),
            evidence=_deserialize_list(data.get("evidence", []), Evidence),
            recommended_scope=rs,
            excluded_scope=data.get("excluded_scope", []),
            graph_status=data.get("graph_status", {}),
            analysis_confidence=data.get("analysis_confidence", 0.0),
            created_at=data.get("created_at", _now()),
            analysis_duration_ms=data.get("analysis_duration_ms", 0.0),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Aggregated type for convenience
# ---------------------------------------------------------------------------

# All domain model classes exported from this module
_ALL_MODEL_CLASSES: list[type] = [
    Evidence,
    GraphNode,
    GraphEdge,
    RelevanceScore,
    DependencyPath,
    CurrentExecutionFlow,
    ArchitectureBoundary,
    StorageImpact,
    ChangeImpact,
    TaskClassification,
    RecommendedScope,
    ContextPack,
]


@dataclass
class RepoIntelConfig:
    """Configuration for Repository Intelligence Agent — all limits configurable."""

    analysis_timeout: float = 300.0
    max_graph_depth: int = 3
    max_files: int = 60
    max_symbols: int = 200
    token_budget: int = 40000
    confidence_threshold: float = 0.6
    max_verification_files: int = 30
    stale_graph_retry: bool = True
