"""Repository Intelligence Agent — codebase understanding and scoping.

Architecture:
  - models.py: domain models (Evidence, GraphNode, GraphEdge,
    RelevanceScore, DependencyPath, CurrentExecutionFlow,
    ArchitectureBoundary, StorageImpact, ChangeImpact,
    TaskClassification, RecommendedScope, ContextPack)
  - codegraph_adapter.py: CodeGraphAdapter — interface to the
    structured code graph
  - repo_intelligence_agent.py: RepoIntelligenceAgent — the
    analysis agent that produces ContextPack
  - quality_gate.py: AnalysisQualityGate — validates analysis
    quality before handoff to Planner
  - scoring.py: RelevanceScorer — scores node relevance for a task
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Models are always available
from .models import (
    ArchitectureBoundary,
    ChangeImpact,
    ContextPack,
    CurrentExecutionFlow,
    DependencyPath,
    Evidence,
    GraphEdge,
    GraphNode,
    RecommendedScope,
    RelevanceScore,
    RepoIntelConfig,
    StorageImpact,
    TaskClassification,
)

# Service classes — imported lazily so the package can be loaded
# before the adapter / agent / gate / scorer modules exist.
if TYPE_CHECKING:
    from .codegraph_adapter import CodeGraphAdapter
    from .repo_intelligence_agent import RepoIntelligenceAgent
    from .quality_gate import AnalysisQualityGate
    from .scoring import RelevanceScorer


def _lazy_import(module_name: str, attr_name: str):
    """Defer import until the attribute is accessed."""
    import importlib

    mod = importlib.import_module(module_name, __package__)
    return getattr(mod, attr_name)


def __getattr__(name: str):
    _LAZY = {
        "CodeGraphAdapter": ".codegraph_adapter",
        "RepoIntelligenceAgent": ".repo_intelligence_agent",
        "AnalysisQualityGate": ".quality_gate",
        "RelevanceScorer": ".scoring",
    }
    if name in _LAZY:
        return _lazy_import(_LAZY[name], name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Models
    "Evidence",
    "GraphNode",
    "GraphEdge",
    "RelevanceScore",
    "DependencyPath",
    "CurrentExecutionFlow",
    "ArchitectureBoundary",
    "StorageImpact",
    "ChangeImpact",
    "TaskClassification",
    "RecommendedScope",
    "ContextPack",
    "RepoIntelConfig",
    # Services (lazy)
    "CodeGraphAdapter",
    "RepoIntelligenceAgent",
    "AnalysisQualityGate",
    "RelevanceScorer",
]
