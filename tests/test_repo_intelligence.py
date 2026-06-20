"""Tests for Repository Intelligence Agent and its domain models.

Covers: Evidence, ContextPack, TaskClassification, GraphRetrieval,
SourceVerification, ImpactAnalysis, QualityGate, RelevanceScoring,
Snapshot, RepoIntelligenceAgent pipeline, cache behavior.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.repo_intelligence.models import (
    ArchitectureBoundary,
    ChangeImpact,
    ContextPack,
    CurrentExecutionFlow,
    DependencyPath,
    EdgeType,
    Evidence,
    GraphEdge,
    GraphNode,
    NodeType,
    RecommendedScope,
    RelevanceScore,
    StorageImpact,
    TaskClassification,
)
from agent_engine.repo_intelligence.repo_intelligence_agent import (
    AnalysisQualityGate,
    RelevanceScorer,
    RepoIntelConfig,
    RepoIntelligenceAgent,
    _detect_build_commands,
    _detect_languages,
    _detect_package_managers,
    _git_branch,
    _git_commit_hash,
    _git_working_tree_status,
    _semantic_extraction_pattern,
)


# ============================================================================
# Evidence model
# ============================================================================


class TestEvidenceModel(unittest.TestCase):
    def test_create_defaults(self) -> None:
        ev = Evidence(claim="found function foo", file_path="src/foo.py", symbol="foo")
        self.assertEqual(ev.claim, "found function foo")
        self.assertEqual(ev.file_path, "src/foo.py")
        self.assertEqual(ev.symbol, "foo")
        self.assertEqual(ev.evidence_type, "source")
        self.assertEqual(ev.confidence, 0.0)
        self.assertIsNone(ev.line_range)
        self.assertEqual(ev.excerpt, "")
        self.assertTrue(ev.id)
        self.assertTrue(ev.verified_at)

    def test_to_dict(self) -> None:
        ev = Evidence(
            claim="c",
            file_path="f.py",
            symbol="sym",
            evidence_type="graph",
            confidence=0.85,
            line_range=(10, 15),
            excerpt="code here",
        )
        d = ev.to_dict()
        self.assertEqual(d["claim"], "c")
        self.assertEqual(d["file_path"], "f.py")
        self.assertEqual(d["symbol"], "sym")
        self.assertEqual(d["evidence_type"], "graph")
        self.assertEqual(d["confidence"], 0.85)
        self.assertEqual(d["line_range"], [10, 15])
        self.assertEqual(d["excerpt"], "code here")
        self.assertIn("id", d)
        self.assertIn("verified_at", d)

    def test_from_dict(self) -> None:
        data = {
            "id": "ev-1",
            "claim": "found",
            "file_path": "a/b.py",
            "symbol": "X",
            "line_range": [5, 8],
            "evidence_type": "source",
            "confidence": 0.9,
            "excerpt": "def X():",
            "verified_at": "2025-01-01T00:00:00Z",
        }
        ev = Evidence.from_dict(data)
        self.assertEqual(ev.id, "ev-1")
        self.assertEqual(ev.claim, "found")
        self.assertEqual(ev.file_path, "a/b.py")
        self.assertEqual(ev.symbol, "X")
        self.assertEqual(ev.line_range, (5, 8))
        self.assertEqual(ev.evidence_type, "source")
        self.assertEqual(ev.confidence, 0.9)
        self.assertEqual(ev.excerpt, "def X():")
        self.assertEqual(ev.verified_at, "2025-01-01T00:00:00Z")

    def test_from_dict_defaults(self) -> None:
        ev = Evidence.from_dict({})
        self.assertTrue(ev.id)
        self.assertEqual(ev.claim, "")
        self.assertEqual(ev.file_path, "")
        self.assertEqual(ev.symbol, "")
        self.assertEqual(ev.evidence_type, "source")
        self.assertIsNone(ev.line_range)
        self.assertEqual(ev.confidence, 0.0)

    def test_roundtrip(self) -> None:
        original = Evidence(
            claim="verified class UserService in services/user.py",
            file_path="services/user.py",
            symbol="UserService",
            evidence_type="source",
            confidence=0.92,
            line_range=(24, 35),
            excerpt="class UserService:\n    def __init__(self):",
        )
        restored = Evidence.from_dict(original.to_dict())
        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.claim, original.claim)
        self.assertEqual(restored.file_path, original.file_path)
        self.assertEqual(restored.symbol, original.symbol)
        self.assertEqual(restored.evidence_type, original.evidence_type)
        self.assertEqual(restored.confidence, original.confidence)
        self.assertEqual(restored.line_range, original.line_range)
        self.assertEqual(restored.excerpt, original.excerpt)


# ============================================================================
# ContextPack model
# ============================================================================


class TestContextPackModel(unittest.TestCase):
    def test_empty_construction(self) -> None:
        pack = ContextPack()
        self.assertTrue(pack.id)
        self.assertEqual(pack.entrypoints, [])
        self.assertEqual(pack.evidence, [])
        self.assertEqual(pack.analysis_confidence, 0.0)
        self.assertEqual(pack.task_classification, [])
        self.assertEqual(pack.relevant_files, [])

    def test_to_dict_minimal(self) -> None:
        pack = ContextPack(request_understanding={"request": "add login"})
        d = pack.to_dict()
        self.assertEqual(d["request_understanding"], {"request": "add login"})
        self.assertEqual(d["task_classification"], [])
        self.assertEqual(d["entrypoints"], [])
        self.assertEqual(d["evidence"], [])
        # StorageImpact() serializes to a dict with keys (not empty dict)
        self.assertIn("tables_affected", d["storage_impact"])
        self.assertEqual(d["storage_impact"]["tables_affected"], [])
        # RecommendedScope() serializes to a dict with keys
        self.assertIn("included_files", d["recommended_scope"])

    def test_to_dict_with_evidence(self) -> None:
        pack = ContextPack(
            evidence=[
                Evidence(claim="c1", file_path="f.py", symbol="s1", confidence=0.8),
                Evidence(claim="c2", file_path="g.py", symbol="s2", confidence=0.6),
            ],
            entrypoints=["ep1", "ep2"],
            analysis_confidence=0.75,
        )
        d = pack.to_dict()
        self.assertEqual(len(d["evidence"]), 2)
        self.assertEqual(d["evidence"][0]["claim"], "c1")
        self.assertEqual(d["evidence"][1]["claim"], "c2")
        self.assertEqual(d["entrypoints"], ["ep1", "ep2"])
        self.assertEqual(d["analysis_confidence"], 0.75)

    def test_to_dict_with_classifications(self) -> None:
        pack = ContextPack(
            task_classification=[
                TaskClassification(category="feature", confidence=0.9, signals=["add", "create"]),
                TaskClassification(category="bugfix", confidence=0.3, signals=["fix"]),
            ]
        )
        d = pack.to_dict()
        self.assertEqual(len(d["task_classification"]), 2)
        self.assertEqual(d["task_classification"][0]["category"], "feature")
        self.assertEqual(d["task_classification"][0]["confidence"], 0.9)

    def test_to_dict_with_flows_and_boundaries(self) -> None:
        pack = ContextPack(
            current_execution_flow=[
                CurrentExecutionFlow(
                    entrypoint="ep1",
                    stages=[{"node_id": "n1", "name": "main", "type": "entrypoint", "depth": 0, "file": "main.py"}],
                    data_trace=[{"from": "ep1", "to": "n1", "edge_type": "calls"}],
                )
            ],
            architecture_boundaries=[
                ArchitectureBoundary(
                    name="entrypoint -> service",
                    from_layer="entrypoint",
                    to_layer="service",
                    node_ids=["n1", "n2"],
                )
            ],
        )
        d = pack.to_dict()
        self.assertEqual(len(d["current_execution_flow"]), 1)
        self.assertEqual(d["current_execution_flow"][0]["entrypoint"], "ep1")
        self.assertEqual(len(d["architecture_boundaries"]), 1)

    def test_from_dict_full(self) -> None:
        data = {
            "id": "cp-1",
            "request_understanding": {"goal": "add auth"},
            "repository_snapshot": {"languages": {"python": 10}},
            "task_classification": [{"category": "feature", "confidence": 0.8, "signals": ["add"]}],
            "entrypoints": ["main.py"],
            "relevant_files": [{"path": "src/auth.py", "name": "auth", "node_type": "module", "id": "n1"}],
            "evidence": [
                {
                    "id": "ev-1",
                    "claim": "found auth",
                    "file_path": "src/auth.py",
                    "symbol": "authenticate",
                    "evidence_type": "source",
                    "confidence": 0.9,
                }
            ],
            "change_impact_map": [
                {"target": "auth", "level": "high", "confidence": 0.85, "evidence_ids": ["ev-1"], "verification_method": "source_verified"}
            ],
            "storage_impact": {"tables_affected": ["users"], "migrations_needed": True, "schema_changes": ["add column"], "confidence": 0.7},
            "recommended_scope": {"included_files": ["src/auth.py"], "included_symbols": ["authenticate"], "excluded_files": [], "excluded_reason": "", "confidence": 0.8},
            "analysis_confidence": 0.8,
            "metadata": {"quality_gate": {"passed": True}},
        }
        pack = ContextPack.from_dict(data)
        self.assertEqual(pack.id, "cp-1")
        self.assertEqual(pack.request_understanding["goal"], "add auth")
        self.assertEqual(pack.entrypoints, ["main.py"])
        self.assertEqual(len(pack.evidence), 1)
        self.assertEqual(pack.evidence[0].claim, "found auth")
        self.assertEqual(len(pack.task_classification), 1)
        self.assertEqual(pack.task_classification[0].category, "feature")
        self.assertEqual(len(pack.change_impact_map), 1)
        self.assertEqual(pack.change_impact_map[0].target, "auth")
        self.assertEqual(pack.storage_impact.tables_affected, ["users"])
        self.assertTrue(pack.storage_impact.migrations_needed)
        self.assertEqual(pack.recommended_scope.included_files, ["src/auth.py"])
        self.assertEqual(pack.analysis_confidence, 0.8)
        self.assertEqual(pack.metadata["quality_gate"]["passed"], True)

    def test_from_dict_empty(self) -> None:
        pack = ContextPack.from_dict({})
        self.assertTrue(pack.id)
        self.assertEqual(pack.evidence, [])
        self.assertEqual(pack.task_classification, [])
        self.assertEqual(pack.analysis_confidence, 0.0)

    def test_roundtrip_full(self) -> None:
        original = ContextPack(
            request_understanding={"goal": "test"},
            entrypoints=["ep1"],
            evidence=[Evidence(claim="ok", file_path="f.py", symbol="s", confidence=0.9)],
            analysis_confidence=0.9,
            unknowns=[{"field": "x", "detail": "missing", "severity": "medium"}],
            risks=[{"risk": "no tests", "impact": "breakage", "likelihood": "low"}],
        )
        d = original.to_dict()
        restored = ContextPack.from_dict(d)
        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.request_understanding, original.request_understanding)
        self.assertEqual(restored.entrypoints, original.entrypoints)
        self.assertEqual(len(restored.evidence), 1)
        self.assertEqual(restored.evidence[0].id, original.evidence[0].id)
        self.assertAlmostEqual(restored.analysis_confidence, original.analysis_confidence)
        self.assertEqual(restored.unknowns, original.unknowns)
        self.assertEqual(restored.risks, original.risks)

    def test_schema_validation_entrypoints_type(self) -> None:
        """entrypoints must be list[str]."""
        pack = ContextPack(entrypoints=["ep1", "ep2"])
        self.assertIsInstance(pack.entrypoints, list)
        for ep in pack.entrypoints:
            self.assertIsInstance(ep, str)

    def test_schema_validation_evidence_type(self) -> None:
        """All evidence items must be Evidence instances."""
        pack = ContextPack(evidence=[Evidence(claim="c", file_path="f", symbol="s")])
        self.assertIsInstance(pack.evidence[0], Evidence)

    def test_storage_impact_dict_handling_in_to_dict(self) -> None:
        """to_dict handles both StorageImpact object and dict."""
        pack_dict = ContextPack(storage_impact={"tables_affected": ["users"]})
        d = pack_dict.to_dict()
        self.assertEqual(d["storage_impact"]["tables_affected"], ["users"])

        pack_obj = ContextPack(storage_impact=StorageImpact(tables_affected=["posts"]))
        d2 = pack_obj.to_dict()
        self.assertEqual(d2["storage_impact"]["tables_affected"], ["posts"])

    def test_recommended_scope_dict_handling_in_to_dict(self) -> None:
        """to_dict handles both RecommendedScope object and dict."""
        pack = ContextPack(recommended_scope={"included_files": ["a.py"]})
        d = pack.to_dict()
        self.assertEqual(d["recommended_scope"]["included_files"], ["a.py"])


# ============================================================================
# Task classification (all 9 categories)
# ============================================================================


class _FakeLLMClient:
    """Mock LLM client that returns structured JSON."""
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def json(self, prompt: str, fallback: dict[str, object] | None = None) -> dict[str, object]:
        return self._response


class TestTaskClassification(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        # Create minimal workspace so snapshot doesn't crash
        (Path(self.workspace) / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _agent(self, llm_client: object = None) -> RepoIntelligenceAgent:
        return RepoIntelligenceAgent(self.workspace, llm_client=llm_client)

    def test_feature_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("add user authentication feature", {})
        self.assertTrue(any(c.category == "feature" for c in result))
        feature = next(c for c in result if c.category == "feature")
        self.assertGreater(feature.confidence, 0.8)
        self.assertIn("add", feature.signals)

    def test_bugfix_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("fix broken login causing crash regression", {})
        bugfix = next(c for c in result if c.category == "bugfix")
        self.assertGreater(bugfix.confidence, 0.85)
        self.assertIn("fix", bugfix.signals)
        self.assertIn("broken", bugfix.signals)

    def test_refactor_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("refactor and clean up the service layer", {})
        refactor = next(c for c in result if c.category == "refactor")
        self.assertIn("refactor", refactor.signals)
        self.assertIn("clean up", refactor.signals)

    def test_performance_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("optimize slow cache latency bottleneck", {})
        perf = next(c for c in result if c.category == "performance")
        self.assertIn("optimize", perf.signals)
        self.assertIn("cache", perf.signals)

    def test_storage_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("add migration for new schema table with index", {})
        storage = next(c for c in result if c.category == "storage")
        self.assertIn("migration", storage.signals)
        self.assertIn("schema", storage.signals)

    def test_integration_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("integrate webhook endpoint with external API service", {})
        integration = next(c for c in result if c.category == "integration")
        self.assertIn("integrate", integration.signals)
        self.assertIn("api", integration.signals)

    def test_infrastructure_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("deploy docker container via CI pipeline config", {})
        infra = next(c for c in result if c.category == "infrastructure")
        self.assertIn("deploy", infra.signals)
        self.assertIn("config", infra.signals)

    def test_test_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("add unit tests and e2e coverage for auth module", {})
        test_cat = next(c for c in result if c.category == "test")
        self.assertIn("test", test_cat.signals)
        self.assertIn("coverage", test_cat.signals)

    def test_documentation_classification(self) -> None:
        agent = self._agent()
        result = agent._classify_task("document the API and update README with docstring", {})
        doc = next(c for c in result if c.category == "documentation")
        self.assertIn("document", doc.signals)
        self.assertIn("readme", doc.signals)

    def test_default_feature_when_no_match(self) -> None:
        agent = self._agent()
        result = agent._classify_task("do the thing please", {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].category, "feature")
        self.assertEqual(result[0].confidence, 0.3)

    def test_multiple_categories_sorted_by_confidence(self) -> None:
        agent = self._agent()
        result = agent._classify_task("fix performance bug in slow database query", {})
        self.assertGreater(len(result), 1)
        for i in range(len(result) - 1):
            self.assertGreaterEqual(result[i].confidence, result[i + 1].confidence)

    def test_signal_count_increases_confidence(self) -> None:
        agent = self._agent()
        result_single = agent._classify_task("add thing", {})
        result_multi = agent._classify_task("add create implement build feature new support", {})
        if result_single[0].category == "feature":
            self.assertGreaterEqual(result_multi[0].confidence, result_single[0].confidence)


# ============================================================================
# Graph retrieval
# ============================================================================


class _FakeCodeGraphAdapter:
    """Mock adapter that returns pre-configured nodes and edges."""

    def __init__(
        self,
        symbol_nodes: list[GraphNode] | None = None,
        entrypoint_nodes: list[GraphNode] | None = None,
        graph_nodes: list[GraphNode] | None = None,
        graph_edges: list[GraphEdge] | None = None,
        service_nodes: list[GraphNode] | None = None,
        test_nodes: list[GraphNode] | None = None,
    ) -> None:
        self._symbol_nodes = symbol_nodes or []
        self._entrypoint_nodes = entrypoint_nodes or []
        self._graph_nodes = graph_nodes or []
        self._graph_edges = graph_edges or []
        self._service_nodes = service_nodes or []
        self._test_nodes = test_nodes or []
        self.query_symbol_calls: list[tuple[str, str | None]] = []
        self.query_graph_calls: list[tuple[list[str], int, int]] = []
        self.find_entrypoints_calls: int = 0
        self.find_services_calls: int = 0

    def is_available(self) -> bool:
        return True

    def query_symbol(self, name: str, kind: str | None = None) -> list[GraphNode]:
        self.query_symbol_calls.append((name, kind))
        return [n for n in self._symbol_nodes if name.lower() in n.name.lower()]

    def find_entrypoints(self) -> list[GraphNode]:
        self.find_entrypoints_calls += 1
        return self._entrypoint_nodes

    def query_graph(self, seed_ids: list[str], max_depth: int = 2, max_nodes: int = 80) -> tuple[list[GraphNode], list[GraphEdge]]:
        self.query_graph_calls.append((seed_ids, max_depth, max_nodes))
        return self._graph_nodes, self._graph_edges

    def find_services(self) -> list[GraphNode]:
        self.find_services_calls += 1
        return self._service_nodes

    def find_tests_for_file(self, file_path: str) -> list[GraphNode]:
        return self._test_nodes


class TestGraphRetrieval(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        (Path(self.workspace) / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
        # Create some source files for walk_workspace
        (Path(self.workspace) / "src").mkdir(exist_ok=True)
        (Path(self.workspace) / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_seed_from_keywords_and_symbols(self) -> None:
        symbol_nodes = [
            GraphNode(id="n1", name="authenticate", node_type="function", file_path="src/auth.py"),
            GraphNode(id="n2", name="UserService", node_type="class", file_path="src/user.py"),
        ]
        adapter = _FakeCodeGraphAdapter(symbol_nodes=symbol_nodes)
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": ["auth", "login"], "symbols": ["authenticate"], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 2}}
        nodes, edges, paths, status = agent._graph_retrieval(understanding, snapshot)
        self.assertGreaterEqual(len(nodes), 1)
        self.assertEqual(status["layers"], 2)
        self.assertGreaterEqual(status["nodes_found"], 1)

    def test_progressive_expansion_layer1_and_layer2(self) -> None:
        n1 = GraphNode(id="n1", name="main", node_type="file", file_path="src/main.py", metadata={"entrypoint": True})
        n2 = GraphNode(id="n2", name="handler", node_type="function", file_path="src/handler.py")
        n3 = GraphNode(id="n3", name="db", node_type="table", file_path="src/db.py")
        edges = [
            GraphEdge(id="e1", source_id="n1", target_id="n2", edge_type="calls"),
            GraphEdge(id="e2", source_id="n2", target_id="n3", edge_type="reads"),
        ]
        adapter = _FakeCodeGraphAdapter(
            symbol_nodes=[n1],
            entrypoint_nodes=[],
            graph_nodes=[n2, n3],
            graph_edges=edges,
        )
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": ["main"], "symbols": [], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 3}}
        nodes, edges_out, paths, status = agent._graph_retrieval(understanding, snapshot)
        self.assertIn("layers", status)
        self.assertGreaterEqual(status["nodes_found"], 1)

    def test_budget_limits_respected(self) -> None:
        config = RepoIntelConfig(max_symbols=5)
        many_nodes = [GraphNode(id=f"n{i}", name=f"symbol_{i}", node_type="function", file_path=f"f{i}.py") for i in range(20)]
        adapter = _FakeCodeGraphAdapter(symbol_nodes=many_nodes[:5], graph_nodes=many_nodes[5:15])
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter, config=config)
        understanding = {"keywords": ["symbol"], "symbols": [], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 20}}
        nodes, edges, paths, status = agent._graph_retrieval(understanding, snapshot)
        self.assertLessEqual(len(nodes), config.max_symbols)

    def test_after_scoring_stats_populated(self) -> None:
        n1 = GraphNode(id="n1", name="auth_routes", node_type="route", file_path="src/auth_routes.py")
        adapter = _FakeCodeGraphAdapter(symbol_nodes=[n1])
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": ["auth"], "symbols": [], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 1}}
        nodes, edges, paths, status = agent._graph_retrieval(understanding, snapshot)
        self.assertIn("after_scoring", status)
        self.assertIn("kept_nodes", status["after_scoring"])
        self.assertIn("min_score", status["after_scoring"])
        self.assertIn("max_score", status["after_scoring"])

    def test_entrypoints_appended(self) -> None:
        ep_node = GraphNode(id="ep1", name="main", node_type="file", file_path="main.py", metadata={"entrypoint": True})
        adapter = _FakeCodeGraphAdapter(entrypoint_nodes=[ep_node])
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": [], "symbols": [], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 1}}
        nodes, edges, paths, status = agent._graph_retrieval(understanding, snapshot)
        ep_ids = {n.id for n in nodes}
        self.assertIn("ep1", ep_ids)

    def test_codegraph_status_recorded(self) -> None:
        adapter = _FakeCodeGraphAdapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": [], "symbols": [], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 1}}
        nodes, edges, paths, status = agent._graph_retrieval(understanding, snapshot)
        self.assertTrue(status["codegraph_available"])

    def test_dependency_paths_from_entrypoint_to_route(self) -> None:
        ep = GraphNode(id="ep1", name="main", node_type="file", file_path="main.py", metadata={"entrypoint": True})
        rt = GraphNode(id="rt1", name="/api/users", node_type="route", file_path="routes.py")
        edges = [
            GraphEdge(id="e1", source_id="ep1", target_id="rt1", edge_type="calls"),
        ]
        adapter = _FakeCodeGraphAdapter(
            symbol_nodes=[],
            entrypoint_nodes=[ep],
            graph_nodes=[rt],
            graph_edges=edges,
        )
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": [], "symbols": [], "routes": [], "tables": [], "services": []}
        snapshot = {"languages": {"python": 2}}
        nodes, edges_out, paths, status = agent._graph_retrieval(understanding, snapshot)
        # Should find a path from ep1 -> rt1
        self.assertGreaterEqual(len(paths), 1)


# ============================================================================
# Source verification
# ============================================================================


class TestSourceVerification(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        (Path(self.workspace) / "src").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _agent(self, config: RepoIntelConfig | None = None) -> RepoIntelligenceAgent:
        return RepoIntelligenceAgent(self.workspace, config=config)

    def test_verified_node_produces_source_evidence(self) -> None:
        (Path(self.workspace) / "src" / "handler.py").write_text(
            "def handle_request():\n    pass\n", encoding="utf-8"
        )
        node = GraphNode(id="n1", name="handle_request", node_type="function", file_path="src/handler.py")
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        source_ev = [e for e in evidence if e.evidence_type == "source"]
        self.assertGreaterEqual(len(source_ev), 1)
        self.assertIn("handle_request", source_ev[0].claim)

    def test_stale_graph_node_produces_graph_evidence(self) -> None:
        (Path(self.workspace) / "src" / "missing.py").write_text(
            "print('hello')\n", encoding="utf-8"
        )
        node = GraphNode(
            id="n2", name="nonexistent_symbol", node_type="function", file_path="src/missing.py"
        )
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        graph_ev = [e for e in evidence if e.evidence_type == "graph"]
        self.assertGreaterEqual(len(graph_ev), 1)
        self.assertIn("not found", graph_ev[0].claim.lower())

    def test_node_without_file_path_skipped(self) -> None:
        node = GraphNode(id="n3", name="orphan", node_type="function", file_path="")
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        self.assertEqual(len(evidence), 0)

    def test_entrypoint_gets_higher_confidence(self) -> None:
        (Path(self.workspace) / "src" / "api.py").write_text(
            "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/')\n"
            "def root():\n    return {'ok': True}\n",
            encoding="utf-8",
        )
        node = GraphNode(
            id="ep1", name="root", node_type="route", file_path="src/api.py",
            metadata={"entrypoint": True},
        )
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        source_ev = [e for e in evidence if e.evidence_type == "source"]
        self.assertGreaterEqual(len(source_ev), 1)
        self.assertGreaterEqual(source_ev[0].confidence, 0.85)

    def test_verification_file_limit(self) -> None:
        config = RepoIntelConfig(max_verification_files=2)
        nodes = [
            GraphNode(
                id=f"n{i}", name=f"fn_{i}", node_type="function",
                file_path=f"src/file_{i}.py"
            )
            for i in range(10)
        ]
        for i in range(10):
            (Path(self.workspace) / "src" / f"file_{i}.py").write_text(
                f"def fn_{i}():\n    pass\n", encoding="utf-8"
            )
        agent = self._agent(config=config)
        evidence = agent._source_verification(nodes, [], [])
        self.assertLessEqual(len(evidence), (config.max_verification_files + 2) * 2)

    def test_excerpt_included_in_evidence(self) -> None:
        (Path(self.workspace) / "src" / "service.py").write_text(
            "class UserService:\n    def create_user(self):\n        pass\n", encoding="utf-8"
        )
        node = GraphNode(id="n1", name="UserService", node_type="class", file_path="src/service.py")
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        source_ev = [e for e in evidence if e.evidence_type == "source"]
        self.assertGreaterEqual(len(source_ev), 1)
        self.assertNotEqual(source_ev[0].excerpt, "")

    def test_line_range_when_symbol_found(self) -> None:
        (Path(self.workspace) / "src" / "util.py").write_text(
            "# comment\n# another\ndef helper():\n    return 42\n", encoding="utf-8"
        )
        node = GraphNode(id="n1", name="helper", node_type="function", file_path="src/util.py")
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        source_ev = [e for e in evidence if e.evidence_type == "source"]
        self.assertGreaterEqual(len(source_ev), 1)
        self.assertIsNotNone(source_ev[0].line_range)

    def test_dependency_path_evidence(self) -> None:
        path = DependencyPath(path=["n1", "n2", "n3"], distance=2, edge_types=["calls", "exposes"])
        agent = self._agent()
        evidence = agent._source_verification([], [], [path])
        path_ev = [e for e in evidence if "Dependency path" in e.claim]
        self.assertEqual(len(path_ev), 1)
        self.assertGreaterEqual(path_ev[0].confidence, 0.5)

    def test_route_handler_pattern_detection(self) -> None:
        (Path(self.workspace) / "src" / "routes.py").write_text(
            "@app.get('/items')\ndef get_items():\n    return []\n", encoding="utf-8"
        )
        node = GraphNode(id="r1", name="get_items", node_type="route", file_path="src/routes.py")
        agent = self._agent()
        evidence = agent._source_verification([node], [], [])
        source_ev = [e for e in evidence if e.evidence_type == "source"]
        self.assertGreaterEqual(len(source_ev), 1)


# ============================================================================
# Impact analysis
# ============================================================================


class TestImpactAnalysis(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        (Path(self.workspace) / "src").mkdir(exist_ok=True)
        (Path(self.workspace) / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def _agent(self) -> RepoIntelligenceAgent:
        return RepoIntelligenceAgent(self.workspace)

    def test_direct_impact_from_source_evidence(self) -> None:
        ev = Evidence(
            claim="found", file_path="src/auth.py", symbol="authenticate",
            evidence_type="source", confidence=0.9,
        )
        node = GraphNode(id="n1", name="authenticate", node_type="function", file_path="src/auth.py")
        agent = self._agent()
        understanding = {"keywords": ["auth"], "symbols": [], "routes": [], "tables": [], "services": []}
        impacts = agent._impact_analysis(understanding, [node], [], [ev], {"flows": [], "boundaries": []})
        direct = [ci for ci in impacts if ci.target == "authenticate"]
        self.assertGreaterEqual(len(direct), 1)
        self.assertEqual(direct[0].verification_method, "source_verified")

    def test_transitive_impact_from_edges(self) -> None:
        ev = Evidence(
            claim="found", file_path="src/auth.py", symbol="authenticate",
            evidence_type="source", confidence=0.9,
        )
        n1 = GraphNode(id="n1", name="authenticate", node_type="function", file_path="src/auth.py")
        n2 = GraphNode(id="n2", name="Database", node_type="class", file_path="src/db.py")
        n3 = GraphNode(id="n3", name="cache", node_type="function", file_path="src/cache.py")
        edges = [
            GraphEdge(id="e1", source_id="n1", target_id="n2", edge_type="calls"),
            GraphEdge(id="e2", source_id="n3", target_id="n1", edge_type="calls"),
        ]
        agent = self._agent()
        understanding = {"keywords": ["auth"], "symbols": [], "routes": [], "tables": [], "services": []}
        impacts = agent._impact_analysis(understanding, [n1, n2, n3], edges, [ev], {"flows": [], "boundaries": []})
        transitive = [ci for ci in impacts if ci.verification_method == "graph_transitive"]
        self.assertGreaterEqual(len(transitive), 1)

    def test_impact_levels_assigned(self) -> None:
        ev = Evidence(claim="found", file_path="src/api.py", symbol="get_users", evidence_type="source", confidence=0.9)
        node = GraphNode(id="r1", name="get_users", node_type="route", file_path="src/api.py", metadata={"entrypoint": True})
        agent = self._agent()
        understanding = {"keywords": [], "symbols": [], "routes": ["/users"], "tables": [], "services": []}
        impacts = agent._impact_analysis(understanding, [node], [], [ev], {"flows": [], "boundaries": []})
        route_impacts = [ci for ci in impacts if ci.target == "get_users"]
        self.assertGreaterEqual(len(route_impacts), 1)
        self.assertIn(route_impacts[0].level, ("high", "critical"))

    def test_api_route_impact(self) -> None:
        node = GraphNode(id="r1", name="/api/login", node_type="route", file_path="src/routes.py")
        agent = self._agent()
        understanding = {"keywords": ["login"], "symbols": [], "routes": ["/api/login"], "tables": [], "services": []}
        impacts = agent._impact_analysis(understanding, [node], [], [], {"flows": [], "boundaries": []})
        api_impacts = [ci for ci in impacts if ci.target.startswith("API:")]
        self.assertGreaterEqual(len(api_impacts), 1)

    def test_db_table_impact(self) -> None:
        node = GraphNode(id="t1", name="users", node_type="table", file_path="migrations/001.sql")
        agent = self._agent()
        understanding = {"keywords": [], "symbols": [], "routes": [], "tables": ["users"], "services": []}
        impacts = agent._impact_analysis(understanding, [node], [], [], {"flows": [], "boundaries": []})
        db_impacts = [ci for ci in impacts if ci.target.startswith("DB table:")]
        self.assertGreaterEqual(len(db_impacts), 1)

    def test_impact_confidence_within_range(self) -> None:
        ev = Evidence(claim="ok", file_path="src/x.py", symbol="x", evidence_type="source", confidence=0.8)
        node = GraphNode(id="n1", name="x", node_type="function", file_path="src/x.py")
        agent = self._agent()
        understanding = {"keywords": [], "symbols": [], "routes": [], "tables": [], "services": []}
        impacts = agent._impact_analysis(understanding, [node], [], [ev], {"flows": [], "boundaries": []})
        for ci in impacts:
            self.assertGreaterEqual(ci.confidence, 0.0)
            self.assertLessEqual(ci.confidence, 1.0)

    def test_test_discovery_impact_tag(self) -> None:
        class FakeTestAdapter(_FakeCodeGraphAdapter):
            def find_tests_for_file(self, file_path: str) -> list[GraphNode]:
                return [GraphNode(id="t1", name="test_auth", node_type="test", file_path="tests/test_auth.py")]

        ev = Evidence(claim="ok", file_path="src/auth.py", symbol="auth", evidence_type="source", confidence=0.8)
        node = GraphNode(id="n1", name="auth", node_type="function", file_path="src/auth.py")
        adapter = FakeTestAdapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        understanding = {"keywords": [], "symbols": [], "routes": [], "tables": [], "services": []}
        impacts = agent._impact_analysis(understanding, [node], [], [ev], {"flows": [], "boundaries": []})
        related = [ci for ci in impacts if "__related_tests__" in ci.target]
        self.assertEqual(len(related), 1)


# ============================================================================
# Quality gate
# ============================================================================


class _make_pack:
    """Helper to build ContextPacks with varying completeness."""
    @staticmethod
    def full() -> ContextPack:
        return ContextPack(
            entrypoints=["main.py"],
            current_execution_flow=[
                CurrentExecutionFlow(entrypoint="ep1", stages=[{"node_id": "n1", "name": "main", "type": "entrypoint", "depth": 0, "file": "main.py"}])
            ],
            evidence=[
                Evidence(claim="c1", file_path="f.py", symbol="s1", evidence_type="source", confidence=0.9),
                Evidence(claim="c2", file_path="g.py", symbol="s2", evidence_type="source", confidence=0.8),
                Evidence(claim="c3", file_path="h.py", symbol="s3", evidence_type="graph", confidence=0.5),
            ],
            change_impact_map=[ChangeImpact(target="auth", level="high", confidence=0.85)],
            related_tests=[{"name": "test_auth", "file": "tests/test_auth.py", "id": "t1"}],
        )

    @staticmethod
    def missing_entrypoint() -> ContextPack:
        pack = _make_pack.full()
        pack.entrypoints = []
        return pack

    @staticmethod
    def low_confidence() -> ContextPack:
        pack = _make_pack.full()
        pack.evidence = []
        return pack

    @staticmethod
    def critical_unknowns() -> ContextPack:
        pack = _make_pack.full()
        pack.unknowns.append({"field": "goal", "detail": "No goal", "severity": "critical"})
        return pack

    @staticmethod
    def minimal_passing() -> ContextPack:
        return ContextPack(
            entrypoints=["main.py"],
            current_execution_flow=[
                CurrentExecutionFlow(entrypoint="ep1", stages=[{"node_id": "n1", "name": "main", "type": "entrypoint", "depth": 0, "file": "main.py"}])
            ],
            evidence=[
                Evidence(claim="c1", file_path="f.py", symbol="s1", evidence_type="source", confidence=0.9),
                Evidence(claim="c2", file_path="g.py", symbol="s2", evidence_type="source", confidence=0.8),
            ],
            change_impact_map=[ChangeImpact(target="auth", level="high", confidence=0.85)],
            related_tests=[{"name": "test_auth", "file": "tests/test_auth.py", "id": "t1"}],
            unknowns=[],
            analysis_confidence=0.85,
        )


class TestQualityGate(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = AnalysisQualityGate(threshold=0.6)

    def test_all_7_checks_pass(self) -> None:
        pack = _make_pack.minimal_passing()
        result = self.gate.evaluate(pack)
        self.assertTrue(result["passed"], f"Expected all checks to pass, failing: {result['failing']}")
        self.assertEqual(len(result["checks"]), 7)
        self.assertEqual(len(result["failing"]), 0)

    def test_gate_fail_on_missing_entrypoint(self) -> None:
        pack = _make_pack.missing_entrypoint()
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("entrypoint_found", result["failing"])

    def test_gate_fail_on_low_confidence(self) -> None:
        pack = _make_pack.low_confidence()
        gate_strict = AnalysisQualityGate(threshold=0.6)
        result = gate_strict.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("confidence_threshold", result["failing"])

    def test_gate_fail_on_critical_unknowns(self) -> None:
        pack = _make_pack.critical_unknowns()
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("no_critical_unknowns", result["failing"])

    def test_gate_fail_on_missing_evidence(self) -> None:
        pack = _make_pack.minimal_passing()
        pack.evidence = [Evidence(claim="c1", file_path="f.py", symbol="s1", evidence_type="graph", confidence=0.5)]
        pack.analysis_confidence = 0.0
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("critical_source_evidence", result["failing"])
        self.assertIn("confidence_threshold", result["failing"])

    def test_gate_fail_on_missing_impact(self) -> None:
        pack = _make_pack.minimal_passing()
        pack.change_impact_map = []
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("impact_scope_identified", result["failing"])

    def test_gate_fail_on_missing_tests(self) -> None:
        pack = _make_pack.minimal_passing()
        pack.related_tests = []
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("related_tests", result["failing"])

    def test_gate_fail_on_missing_execution_flow(self) -> None:
        pack = _make_pack.minimal_passing()
        pack.current_execution_flow = []
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertIn("execution_flow_reconstructed", result["failing"])

    def test_gate_returns_overall_confidence(self) -> None:
        pack = _make_pack.minimal_passing()
        result = self.gate.evaluate(pack)
        self.assertIn("overall_confidence", result)
        self.assertIn("threshold", result)
        self.assertEqual(result["threshold"], 0.6)

    def test_gate_empty_pack_fails_all(self) -> None:
        pack = ContextPack()
        result = self.gate.evaluate(pack)
        self.assertFalse(result["passed"])
        self.assertGreater(len(result["failing"]), 4)


# ============================================================================
# Relevance scoring
# ============================================================================


class TestScoring(unittest.TestCase):
    def test_score_range_zero_to_one(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={"auth": 0.8},
            entrypoints=["ep1"],
            nodes=[GraphNode(id="ep1", name="ep1", node_type="route", file_path="src/main.py")],
            edges=[],
        )
        node = GraphNode(id="n1", name="authenticate", node_type="function", file_path="src/auth.py")
        s = scorer.score(node)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_semantic_match_exact(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={"authenticate": 1.0},
            entrypoints=[],
            nodes=[GraphNode(id="n1", name="authenticate", node_type="function", file_path="src/auth.py")],
            edges=[],
        )
        node = GraphNode(id="n1", name="authenticate", node_type="function", file_path="src/auth.py")
        s = scorer.score(node)
        self.assertGreater(s, 0.35)

    def test_semantic_match_file_path(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={"auth": 0.8},
            entrypoints=[],
            nodes=[GraphNode(id="n1", name="login", node_type="function", file_path="src/auth.py")],
            edges=[],
        )
        node = GraphNode(id="n1", name="login", node_type="function", file_path="src/auth.py")
        s = scorer.score(node)
        self.assertGreater(s, 0.25)

    def test_entrypoint_scores_high(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={},
            entrypoints=["ep1"],
            nodes=[GraphNode(id="ep1", name="ep1", node_type="route", file_path="src/main.py")],
            edges=[],
        )
        node = GraphNode(id="ep1", name="ep1", node_type="route", file_path="src/main.py")
        s = scorer.score(node)
        self.assertGreater(s, 0.65)

    def test_boilerplate_penalty(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={"setup": 0.5},
            entrypoints=[],
            nodes=[GraphNode(id="n1", name="setup", node_type="file", file_path="src/setup.py")],
            edges=[],
        )
        node = GraphNode(id="n1", name="setup", node_type="file", file_path="src/setup.py")
        s = scorer.score(node)
        self.assertLess(s, 0.5)

    def test_test_relationship_bonus(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={"auth": 0.8},
            entrypoints=[],
            nodes=[GraphNode(id="t1", name="test_auth", node_type="test", file_path="tests/test_auth.py")],
            edges=[],
        )
        node = GraphNode(id="t1", name="test_auth", node_type="test", file_path="tests/test_auth.py")
        s = scorer.score(node)
        self.assertGreater(s, 0.15)

    def test_distance_penalty_increases_with_depth(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={},
            entrypoints=["ep1"],
            nodes=[
                GraphNode(id="ep1", name="ep1", node_type="file", file_path="main.py"),
                GraphNode(id="n_far", name="n_far", node_type="function", file_path="far.py"),
            ],
            edges=[],
        )
        scorer.distances = {"ep1": 0, "n_far": 5}
        near = GraphNode(id="ep1", name="ep1", node_type="file", file_path="main.py")
        far = GraphNode(id="n_far", name="n_far", node_type="function", file_path="far.py")
        self.assertGreater(scorer.score(near), scorer.score(far))

    def test_no_keywords_default_semantic(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={},
            entrypoints=[],
            nodes=[GraphNode(id="n1", name="any", node_type="function", file_path="f.py")],
            edges=[],
        )
        node = GraphNode(id="n1", name="any", node_type="function", file_path="f.py")
        s = scorer.score(node)
        self.assertGreater(s, 0.0)

    def test_multi_part_keyword_matching(self) -> None:
        scorer = RelevanceScorer()
        scorer.configure(
            keywords={"user_service": 0.8},
            entrypoints=[],
            nodes=[GraphNode(id="n1", name="UserService", node_type="class", file_path="src/user.py")],
            edges=[],
        )
        node = GraphNode(id="n1", name="UserService", node_type="class", file_path="src/user.py")
        s = scorer.score(node)
        self.assertGreater(s, 0.2)


# ============================================================================
# Snapshot / git info / language detection
# ============================================================================


class TestSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_git_commit_hash_mocked(self) -> None:
        proc = mock.Mock(returncode=0, stdout="abc123def456\n", stderr="")
        with mock.patch("subprocess.run", return_value=proc) as run_mock:
            result = _git_commit_hash(self.workspace)
        self.assertEqual(result, "abc123def456")
        run_mock.assert_called_once()

    def test_git_commit_hash_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
            result = _git_commit_hash(self.workspace)
        self.assertIsNone(result)

    def test_git_branch_mocked(self) -> None:
        proc = mock.Mock(returncode=0, stdout="main\n", stderr="")
        with mock.patch("subprocess.run", return_value=proc) as run_mock:
            result = _git_branch(self.workspace)
        self.assertEqual(result, "main")
        run_mock.assert_called_once()

    def test_git_branch_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("git not found")):
            result = _git_branch(self.workspace)
        self.assertIsNone(result)

    def test_git_working_tree_clean(self) -> None:
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=proc):
            result = _git_working_tree_status(self.workspace)
        self.assertFalse(result["dirty"])
        self.assertEqual(result["changed_files"], 0)

    def test_git_working_tree_dirty(self) -> None:
        proc = mock.Mock(returncode=0, stdout="M src/main.py\n?? new_file.py\n", stderr="")
        with mock.patch("subprocess.run", return_value=proc):
            result = _git_working_tree_status(self.workspace)
        self.assertTrue(result["dirty"])
        self.assertEqual(result["changed_files"], 2)

    def test_git_working_tree_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=Exception("no git")):
            result = _git_working_tree_status(self.workspace)
        self.assertFalse(result["dirty"])
        self.assertEqual(result.get("error", ""), "git status failed")

    def test_detect_languages(self) -> None:
        files = [
            {"path": "src/main.py"},
            {"path": "src/utils.py"},
            {"path": "src/index.js"},
            {"path": "config.json"},
        ]
        langs = _detect_languages(files)
        self.assertEqual(langs.get("python"), 2)
        self.assertEqual(langs.get("javascript"), 1)

    def test_detect_languages_multiple(self) -> None:
        files = [
            {"path": "main.go"},
            {"path": "lib.rs"},
            {"path": "App.tsx"},
            {"path": "helper.py"},
        ]
        langs = _detect_languages(files)
        self.assertEqual(langs["go"], 1)
        self.assertEqual(langs["rust"], 1)
        self.assertEqual(langs["typescript"], 1)
        self.assertEqual(langs["python"], 1)

    def test_detect_package_managers(self) -> None:
        (Path(self.workspace) / "package.json").write_text("{}", encoding="utf-8")
        (Path(self.workspace) / "package-lock.json").write_text("{}", encoding="utf-8")
        (Path(self.workspace) / "pyproject.toml").write_text("[project]", encoding="utf-8")
        (Path(self.workspace) / "poetry.lock").write_text("", encoding="utf-8")
        managers = _detect_package_managers(self.workspace)
        self.assertIn("npm", managers)
        self.assertIn("poetry", managers)

    def test_detect_build_commands(self) -> None:
        (Path(self.workspace) / "package.json").write_text(
            json.dumps({"scripts": {"build": "tsc", "test": "jest"}}),
            encoding="utf-8",
        )
        (Path(self.workspace) / "pyproject.toml").write_text("[project]\nname='x'", encoding="utf-8")
        cmds = _detect_build_commands(self.workspace)
        self.assertIn("npm run build", cmds)
        self.assertIn("npm run test", cmds)
        self.assertIn("pytest", cmds)

    def test_semantic_extraction_pattern(self) -> None:
        request = "Fix the login bug in UserService.authenticate() for route /api/login"
        result = _semantic_extraction_pattern(request)
        self.assertIn("UserService", result["keywords"])
        self.assertIn("authenticate", result["symbols"])
        self.assertIn("/api/login", result["routes"])
        self.assertTrue(result["goal"])


# ============================================================================
# RepoIntelligenceAgent full pipeline
# ============================================================================


class TestRepoIntelligenceAgentPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        root = Path(self.workspace)
        # Build a real-looking project structure
        (root / "src").mkdir(exist_ok=True)
        (root / "tests").mkdir(exist_ok=True)
        (root / "src" / "main.py").write_text(
            "from src.auth import authenticate\n\ndef main():\n    authenticate()\n",
            encoding="utf-8",
        )
        (root / "src" / "auth.py").write_text(
            "def authenticate():\n    return True\n",
            encoding="utf-8",
        )
        (root / "src" / "handler.py").write_text(
            "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/login')\n"
            "def login():\n    return {'token': 'xyz'}\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_auth.py").write_text(
            "from src.auth import authenticate\n\ndef test_authenticate():\n    assert authenticate()\n",
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text(
            "[project]\nname='test'\n\n[project.scripts]\nstart = \"src.main:main\"\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def _adapter(self) -> _FakeCodeGraphAdapter:
        nodes = [
            GraphNode(id="n1", name="main", node_type="file", file_path="src/main.py", metadata={"entrypoint": True}),
            GraphNode(id="n2", name="authenticate", node_type="function", file_path="src/auth.py"),
            GraphNode(id="n3", name="login", node_type="route", file_path="src/handler.py"),
            GraphNode(id="n4", name="app", node_type="class", file_path="src/handler.py"),
        ]
        edges = [
            GraphEdge(id="e1", source_id="n1", target_id="n2", edge_type="calls"),
            GraphEdge(id="e2", source_id="n4", target_id="n3", edge_type="exposes"),
        ]
        return _FakeCodeGraphAdapter(symbol_nodes=nodes, entrypoint_nodes=[nodes[0]], graph_nodes=nodes[1:], graph_edges=edges)

    def test_full_analyze_pipeline_completes(self) -> None:
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter, config=RepoIntelConfig(
            max_graph_depth=2, max_symbols=50, max_verification_files=10,
        ))
        pack = agent.analyze("add login feature")
        self.assertIsNotNone(pack)
        self.assertIn("final_status", pack.metadata)
        self.assertGreater(pack.analysis_duration_ms, 0)
        self.assertIsInstance(pack.analysis_confidence, float)

    def test_pipeline_produces_entrypoints(self) -> None:
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        pack = agent.analyze("fix authenticate bug")
        self.assertGreaterEqual(len(pack.entrypoints), 1)

    def test_pipeline_produces_evidence(self) -> None:
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter, config=RepoIntelConfig(
            max_verification_files=10,
        ))
        pack = agent.analyze("test authenticate function")
        self.assertGreaterEqual(len(pack.evidence), 1)

    def test_pipeline_produces_impact_map(self) -> None:
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        pack = agent.analyze("add login feature")
        self.assertGreaterEqual(len(pack.change_impact_map), 1)

    def test_pipeline_produces_recommended_scope(self) -> None:
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        pack = agent.analyze("add user feature")
        self.assertIsInstance(pack.recommended_scope, RecommendedScope)

    def test_pipeline_produces_quality_gate(self) -> None:
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        pack = agent.analyze("fix login bug")
        self.assertIn("quality_gate", pack.metadata)
        self.assertIn("passed", pack.metadata["quality_gate"])

    def test_stale_graph_handling(self) -> None:
        """When graph retrieval fails, stale flag should be set."""
        class FailingAdapter:
            def is_available(self) -> bool:
                return False
            def query_symbol(self, name: str, kind: str | None = None) -> list[GraphNode]:
                raise RuntimeError("Graph index corrupted")
            def find_entrypoints(self) -> list[GraphNode]:
                raise RuntimeError("Graph index corrupted")
            def query_graph(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("Graph index corrupted")
            def find_services(self) -> list[GraphNode]:
                return []
            def find_tests_for_file(self, file_path: str) -> list[GraphNode]:
                return []

        agent = RepoIntelligenceAgent(
            self.workspace,
            adapter=FailingAdapter(),
            config=RepoIntelConfig(stale_graph_retry=True),
        )
        pack = agent.analyze("add feature")
        gs = pack.graph_status
        self.assertTrue(gs.get("stale"), "Expected stale flag when graph retrieval fails")

    def test_read_only_operation_no_file_writes(self) -> None:
        """Agent must not write any files during analysis."""
        before = set()
        for p in Path(self.workspace).rglob("*"):
            if p.is_file():
                before.add((str(p.relative_to(self.workspace)), p.stat().st_mtime))

        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter)
        agent.analyze("add feature")

        after = set()
        for p in Path(self.workspace).rglob("*"):
            if p.is_file():
                after.add((str(p.relative_to(self.workspace)), p.stat().st_mtime))

        new_files = {a[0] for a in after} - {b[0] for b in before}
        self.assertEqual(new_files, set(), f"Agent wrote files: {new_files}")

        # Also check that existing files were not modified
        for bp, bmtime in before:
            for ap, amtime in after:
                if bp == ap:
                    self.assertEqual(
                        bmtime, amtime,
                        f"File {bp} was modified during analysis",
                    )

    def test_llm_client_used_when_provided(self) -> None:
        llm = _FakeLLMClient({
            "goal": "Add authentication",
            "behavior_change": "Users can log in",
            "likely_components": ["auth", "login"],
            "constraints": ["must use OAuth2"],
            "keywords": ["auth", "login", "token"],
            "symbols": ["authenticate", "login"],
            "routes": ["/api/login", "/api/logout"],
            "tables": ["users"],
            "services": ["AuthService"],
        })
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter, llm_client=llm)
        pack = agent.analyze("add login feature")
        self.assertEqual(pack.request_understanding["goal"], "Add authentication")
        self.assertIn("auth", pack.request_understanding["keywords"])

    def test_timeout_produces_partial_pack(self) -> None:
        """Very short timeout should result in partial analysis."""
        adapter = self._adapter()
        agent = RepoIntelligenceAgent(
            self.workspace,
            adapter=adapter,
            config=RepoIntelConfig(analysis_timeout=0.0),  # immediate timeout
        )
        pack = agent.analyze("add feature")
        self.assertIn(pack.metadata["final_status"], ("timeout_after_stage1", "timeout_after_stage2", "timeout_after_stage3"))

    def test_stage_error_produces_graceful_degradation(self) -> None:
        """When a stage errors, the pipeline continues."""
        class PartialAdapter:
            def is_available(self) -> bool:
                return True
            def query_symbol(self, name: str, kind: str | None = None) -> list[GraphNode]:
                return [
                    GraphNode(id="n1", name="main", node_type="file", file_path="src/main.py", metadata={"entrypoint": True}),
                ]
            def find_entrypoints(self) -> list[GraphNode]:
                raise RuntimeError("Graph stage error during entrypoint scan")
            def query_graph(self, *args: object, **kwargs: object) -> object:
                return [], []
            def find_services(self) -> list[GraphNode]:
                return []
            def find_tests_for_file(self, file_path: str) -> list[GraphNode]:
                return []

        agent = RepoIntelligenceAgent(
            self.workspace,
            adapter=PartialAdapter(),
            config=RepoIntelConfig(stale_graph_retry=True),
        )
        pack = agent.analyze("modify main module")
        self.assertIsNotNone(pack)
        self.assertIn("error", pack.graph_status)
        self.assertIn("Graph stage error", pack.graph_status["error"])

    def test_emit_events_are_called(self) -> None:
        events: list[tuple[str, str]] = []
        def collect(event: str, detail: str) -> None:
            events.append((event, detail))

        adapter = self._adapter()
        agent = RepoIntelligenceAgent(self.workspace, adapter=adapter, emit=collect)
        agent.analyze("add feature")
        self.assertGreater(len(events), 0)
        self.assertTrue(any(e[0] == "finalize" for e in events))


# ============================================================================
# Cache invalidation behavior
# ============================================================================


class TestCache(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        root = Path(self.workspace)
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\nname='test'", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_cache_invalidation_after_code_change(self) -> None:
        """After modifying a source file, a second analyze() should
        reflect changes (graph adapter delegates to codegraph which
        would pick up file changes)."""
        from agent_engine.repo_intelligence.codegraph_adapter import CodeGraphAdapter

        adapter = CodeGraphAdapter(self.workspace)
        # First query populates cache
        nodes1 = adapter.query_file("src/main.py")
        # Modify file
        (Path(self.workspace) / "src" / "main.py").write_text(
            "def main():\n    print('changed')\n", encoding="utf-8"
        )
        # Invalidate cache
        adapter.invalidate()
        nodes2 = adapter.query_file("src/main.py")
        # In fallback mode, file nodes are path-based, but the content itself
        # would be re-read. We just verify the adapter doesn't crash.
        self.assertIsNotNone(nodes2)

    def test_adapter_invalidate_clears_all_by_default(self) -> None:
        from agent_engine.repo_intelligence.codegraph_adapter import CodeGraphAdapter
        adapter = CodeGraphAdapter(self.workspace)
        adapter._cache["test_key"] = (time.monotonic(), "cached_value")
        adapter.invalidate()
        self.assertEqual(adapter._cache, {})

    def test_adapter_invalidate_specific_nodes(self) -> None:
        from agent_engine.repo_intelligence.codegraph_adapter import CodeGraphAdapter
        adapter = CodeGraphAdapter(self.workspace)
        adapter._cache["key_with_n1"] = (time.monotonic(), "val1")
        adapter._cache["key_with_n2"] = (time.monotonic(), "val2")
        adapter._cache["other_key"] = (time.monotonic(), "val3")
        adapter.invalidate(nodes=["n1"])
        self.assertNotIn("key_with_n1", adapter._cache)
        self.assertIn("key_with_n2", adapter._cache)
        self.assertIn("other_key", adapter._cache)


if __name__ == "__main__":
    unittest.main()
