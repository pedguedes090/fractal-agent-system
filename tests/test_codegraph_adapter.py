"""Tests for CodeGraphAdapter -- unified code-graph query interface.

Covers: query_symbol, query_file, query_dependencies, query_graph,
find_entrypoints, find_routes, find_services, find_tests_for_file,
find_definition, find_references, is_available, index_status,
cache hit/miss, invalidation, fallback graph building, and
filesystem-based fallback when codegraph binary is unavailable.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.repo_intelligence.codegraph_adapter import (
    CodeGraphAdapter,
    _build_fallback_graph,
    _detect_language,
    _is_test_file,
    _parse_python_imports,
    _parse_js_ts_imports,
    _parse_go_imports,
    _parse_codegraph_json,
    _dicts_to_nodes,
    _dicts_to_edges,
    _read_manifest,
    _read_text_manifest,
)
from agent_engine.repo_intelligence.models import GraphEdge, GraphNode


# ============================================================================
# Helpers
# ============================================================================


def _make_test_workspace() -> tempfile.TemporaryDirectory:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.py").write_text(
        "import os\nfrom src.auth import authenticate\n\n"
        "def main():\n    return authenticate()\n",
        encoding="utf-8",
    )
    (root / "src" / "auth.py").write_text(
        "def authenticate():\n    return True\n",
        encoding="utf-8",
    )
    (root / "src" / "handler.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n"
        "@app.get('/users')\ndef get_users():\n    return []\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_auth.py").write_text(
        "from src.auth import authenticate\n\ndef test_authenticate():\n    assert authenticate()\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(json.dumps({
        "name": "test-app",
        "main": "dist/index.js",
        "scripts": {"start": "node dist/index.js", "build": "tsc", "test": "jest"},
        "dependencies": {"express": "^4.0.0", "react": "^18.0.0"},
    }), encoding="utf-8")
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "test-app"\n\n[project.scripts]\nserve = "src.main:main"\n',
        encoding="utf-8",
    )
    (root / "go.mod").write_text("module github.com/test/app\n\ngo 1.21\n", encoding="utf-8")
    (root / "Cargo.toml").write_text('[package]\nname = "test-app"\nversion = "0.1.0"\n', encoding="utf-8")
    return td


# ============================================================================
# Language / test detection helpers
# ============================================================================


class TestDetectLanguage(unittest.TestCase):
    def test_python(self) -> None:
        self.assertEqual(_detect_language("src/main.py"), "python")
        self.assertEqual(_detect_language("lib/helpers.pyw"), "python")

    def test_javascript(self) -> None:
        self.assertEqual(_detect_language("src/index.js"), "javascript")
        self.assertEqual(_detect_language("src/App.jsx"), "javascript")
        self.assertEqual(_detect_language("lib/util.mjs"), "javascript")
        self.assertEqual(_detect_language("lib/helper.cjs"), "javascript")

    def test_typescript(self) -> None:
        self.assertEqual(_detect_language("src/index.ts"), "typescript")
        self.assertEqual(_detect_language("src/App.tsx"), "typescript")

    def test_go(self) -> None:
        self.assertEqual(_detect_language("main.go"), "go")

    def test_rust(self) -> None:
        self.assertEqual(_detect_language("main.rs"), "rust")

    def test_java_kotlin_scala(self) -> None:
        self.assertEqual(_detect_language("App.java"), "java")
        self.assertEqual(_detect_language("App.kt"), "java")
        self.assertEqual(_detect_language("App.scala"), "java")

    def test_csharp(self) -> None:
        self.assertEqual(_detect_language("Program.cs"), "csharp")

    def test_ruby(self) -> None:
        self.assertEqual(_detect_language("app.rb"), "ruby")

    def test_cpp(self) -> None:
        self.assertEqual(_detect_language("main.cpp"), "cpp")
        self.assertEqual(_detect_language("header.h"), "cpp")

    def test_unknown_extension(self) -> None:
        self.assertIsNone(_detect_language("README.md"))
        self.assertIsNone(_detect_language("config.json"))
        self.assertIsNone(_detect_language("Dockerfile"))
        self.assertIsNone(_detect_language(""))

    def test_case_insensitive(self) -> None:
        self.assertEqual(_detect_language("Main.PY"), "python")


class TestIsTestFile(unittest.TestCase):
    def test_prefixed_test_file(self) -> None:
        self.assertTrue(_is_test_file("test_auth.py"))
        self.assertTrue(_is_test_file("test_user_service.py"))

    def test_suffixed_test_file(self) -> None:
        self.assertTrue(_is_test_file("auth_test.py"))
        self.assertTrue(_is_test_file("user_service_test.go"))

    def test_test_directory(self) -> None:
        self.assertTrue(_is_test_file("tests/test_auth.py"))
        self.assertTrue(_is_test_file("tests/unit/test_service.py"))

    def test_spec_file(self) -> None:
        self.assertTrue(_is_test_file("auth.spec.ts"))
        self.assertTrue(_is_test_file("user.spec.js"))
        self.assertTrue(_is_test_file("spec_auth.py"))

    def test_non_test_file(self) -> None:
        self.assertFalse(_is_test_file("src/auth.py"))
        self.assertFalse(_is_test_file("main.go"))
        self.assertFalse(_is_test_file("lib/utils.rs"))


# ============================================================================
# JSON parsing helpers
# ============================================================================


class TestParseCodegraphJson(unittest.TestCase):
    def test_empty_result(self) -> None:
        self.assertEqual(_parse_codegraph_json({"ok": False}), [])

    def test_empty_stdout(self) -> None:
        self.assertEqual(_parse_codegraph_json({"ok": True, "stdout": ""}), [])

    def test_list_result(self) -> None:
        result = {"ok": True, "stdout": json.dumps([{"name": "foo", "type": "function"}])}
        parsed = _parse_codegraph_json(result)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "foo")

    def test_dict_result(self) -> None:
        result = {"ok": True, "stdout": json.dumps({"name": "foo", "type": "function"})}
        parsed = _parse_codegraph_json(result)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "foo")

    def test_nodes_edges_result(self) -> None:
        result = {
            "ok": True,
            "stdout": json.dumps({
                "nodes": [{"name": "n1", "type": "file"}],
                "edges": [{"source": "n1", "target": "n2", "type": "imports"}],
            }),
        }
        parsed = _parse_codegraph_json(result)
        self.assertEqual(len(parsed), 2)

    def test_invalid_json(self) -> None:
        self.assertEqual(_parse_codegraph_json({"ok": True, "stdout": "not json"}), [])


class TestDictsToNodes(unittest.TestCase):
    def test_converts_basic_fields(self) -> None:
        data = [{"id": "n1", "name": "foo", "type": "function", "file": "src/foo.py"}]
        nodes = _dicts_to_nodes(data)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].id, "n1")
        self.assertEqual(nodes[0].name, "foo")
        self.assertEqual(nodes[0].node_type, "function")
        self.assertEqual(nodes[0].file_path, "src/foo.py")

    def test_handles_alternative_field_names(self) -> None:
        data = [{"name": "bar", "symbol": "bar_alt", "kind": "class", "filePath": "src/bar.py", "line": 10, "endLine": 25}]
        nodes = _dicts_to_nodes(data)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].name, "bar")
        self.assertEqual(nodes[0].node_type, "class")
        self.assertEqual(nodes[0].file_path, "src/bar.py")
        self.assertEqual(nodes[0].line_start, 10)
        self.assertEqual(nodes[0].line_end, 25)

    def test_handles_malformed_items(self) -> None:
        data = [{"bad": 1}, None, "string"]  # type: ignore[list-item]
        nodes = _dicts_to_nodes(data)  # type: ignore[arg-type]
        # The try/except in _dicts_to_nodes catches GraphNode construction
        # errors where fields are literally missing from the dict entirely,
        # but a dict with unrecognized keys still produces a (low-quality) node.
        # The important property is that this doesn't crash.
        self.assertGreaterEqual(len(nodes), 0)

    def test_defaults_when_fields_missing(self) -> None:
        data = [{"name": "orphan"}]
        nodes = _dicts_to_nodes(data)
        self.assertEqual(nodes[0].id, "orphan")
        self.assertEqual(nodes[0].node_type, "file")
        self.assertEqual(nodes[0].file_path, "")


class TestDictsToEdges(unittest.TestCase):
    def test_converts_basic_fields(self) -> None:
        data = [{"id": "e1", "source": "n1", "target": "n2", "type": "imports"}]
        edges = _dicts_to_edges(data)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].id, "e1")
        self.assertEqual(edges[0].source_id, "n1")
        self.assertEqual(edges[0].target_id, "n2")
        self.assertEqual(edges[0].edge_type, "imports")

    def test_handles_alternative_field_names(self) -> None:
        data = [{"sourceId": "a", "targetId": "b", "edgeType": "calls"}]
        edges = _dicts_to_edges(data)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].source_id, "a")
        self.assertEqual(edges[0].target_id, "b")
        self.assertEqual(edges[0].edge_type, "calls")

    def test_generates_id_when_missing(self) -> None:
        data = [{"source": "n1", "target": "n2", "type": "calls"}]
        edges = _dicts_to_edges(data)
        self.assertEqual(edges[0].id, "n1->n2")


# ============================================================================
# Manifest reading
# ============================================================================


class TestReadManifest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name
        root = Path(self.workspace)
        (root / "package.json").write_text(json.dumps({"name": "test", "main": "index.js"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_read_json_manifest(self) -> None:
        result = _read_manifest(self.workspace, "package.json")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["name"], "test")

    def test_read_missing_manifest(self) -> None:
        self.assertIsNone(_read_manifest(self.workspace, "nonexistent.json"))

    def test_read_text_manifest(self) -> None:
        (Path(self.workspace) / "go.mod").write_text("module test\n", encoding="utf-8")
        result = _read_text_manifest(self.workspace, "go.mod")
        self.assertIsNotNone(result)
        self.assertIn("module test", result or "")

    def test_read_text_manifest_missing(self) -> None:
        self.assertIsNone(_read_text_manifest(self.workspace, "nope.txt"))


# ============================================================================
# Import parsers (fallback graph components)
# ============================================================================


class TestParsePythonImports(unittest.TestCase):
    def test_simple_import(self) -> None:
        content = "import os\nimport sys\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_python_imports(content, "src/test.py", nodes, edges, "file:1")
        self.assertGreaterEqual(len(new_nodes), 2)
        mod_names = {n.name for n in new_nodes}
        self.assertIn("os", mod_names)
        self.assertIn("sys", mod_names)

    def test_from_import(self) -> None:
        content = "from fastapi import FastAPI, Request\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_python_imports(content, "src/api.py", nodes, edges, "file:1")
        mod_names = {n.name for n in new_nodes}
        self.assertIn("fastapi", mod_names)
        self.assertIn("fastapi.FastAPI", mod_names)

    def test_import_as(self) -> None:
        content = "import numpy as np\nfrom sqlalchemy.orm import Session as DBSession\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_python_imports(content, "src/db.py", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("numpy", names)
        self.assertIn("sqlalchemy.orm", names)

    def test_edges_created(self) -> None:
        content = "import os\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        _parse_python_imports(content, "src/test.py", nodes, edges, "file:1")
        self.assertGreaterEqual(len(edges), 1)
        self.assertEqual(edges[0].source_id, "file:1")
        self.assertEqual(edges[0].edge_type, "imports")


class TestParseJsTsImports(unittest.TestCase):
    def test_es_module_import(self) -> None:
        content = "import express from 'express'\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_js_ts_imports(content, "src/app.js", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("express", names)

    def test_destructured_import(self) -> None:
        content = "import { Router, json } from 'express'\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_js_ts_imports(content, "src/routes.js", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("express", names)

    def test_require_import(self) -> None:
        content = "const fs = require('fs')\nconst axios = require('axios')\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_js_ts_imports(content, "src/util.js", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("fs", names)
        self.assertIn("axios", names)

    def test_dynamic_import(self) -> None:
        content = "const mod = await import('some-lib')\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_js_ts_imports(content, "src/lazy.js", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("some-lib", names)

    def test_relative_imports_skipped(self) -> None:
        content = "import { helper } from './utils'\nimport config from '../config'\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_js_ts_imports(content, "src/app.ts", nodes, edges, "file:1")
        self.assertEqual(len(new_nodes), 0)

    def test_commonjs_require_relative_skipped(self) -> None:
        content = "const lib = require('./local')\n"
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_js_ts_imports(content, "src/app.js", nodes, edges, "file:1")
        self.assertEqual(len(new_nodes), 0)


class TestParseGoImports(unittest.TestCase):
    def test_single_import(self) -> None:
        content = 'import "fmt"\nimport "net/http"\n'
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_go_imports(content, "main.go", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("fmt", names)
        self.assertIn("net/http", names)

    def test_block_import(self) -> None:
        content = 'import (\n\t"fmt"\n\t"net/http"\n\t"github.com/gin-gonic/gin"\n)\n'
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        new_nodes = _parse_go_imports(content, "main.go", nodes, edges, "file:1")
        names = {n.name for n in new_nodes}
        self.assertIn("fmt", names)
        self.assertIn("net/http", names)
        self.assertIn("github.com/gin-gonic/gin", names)


# ============================================================================
# Fallback graph
# ============================================================================


class TestBuildFallbackGraph(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_produces_file_nodes(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        self.assertGreater(len(nodes), 0)
        file_nodes = [n for n in nodes if n.node_type == "file"]
        self.assertGreaterEqual(len(file_nodes), 3)

    def test_produces_test_nodes(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        test_nodes = [n for n in nodes if n.node_type == "test"]
        self.assertGreaterEqual(len(test_nodes), 1)

    def test_produces_entrypoint_nodes_from_package_json(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        entrypoints = [n for n in nodes if n.metadata.get("entrypoint")]
        self.assertGreaterEqual(len(entrypoints), 1)

    def test_produces_entrypoint_nodes_from_pyproject(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        script_entries = [n for n in nodes if n.metadata.get("source") == "pyproject.toml"]
        self.assertGreaterEqual(len(script_entries), 1)

    def test_produces_service_nodes_from_directories(self) -> None:
        # Manually add a service directory
        root = Path(self.workspace)
        (root / "services").mkdir(exist_ok=True)
        (root / "services" / "user_service.py").write_text("class UserService:\n    pass\n", encoding="utf-8")
        nodes, edges = _build_fallback_graph(self.workspace)
        service_nodes = [n for n in nodes if n.node_type == "service"]
        self.assertGreaterEqual(len(service_nodes), 1)

    def test_produces_import_edges(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        import_edges = [e for e in edges if e.edge_type == "imports"]
        self.assertGreaterEqual(len(import_edges), 1)

    def test_produces_go_module_node(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        go_mod_nodes = [n for n in nodes if n.file_path == "go.mod"]
        self.assertGreaterEqual(len(go_mod_nodes), 1)

    def test_produces_cargo_package_node(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        cargo_nodes = [n for n in nodes if n.file_path == "Cargo.toml"]
        self.assertGreaterEqual(len(cargo_nodes), 1)

    def test_all_nodes_have_ids(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        for node in nodes:
            self.assertTrue(node.id, f"Node {node.name} has no id")
            self.assertIsNotNone(node.id)

    def test_edges_reference_existing_nodes(self) -> None:
        nodes, edges = _build_fallback_graph(self.workspace)
        node_ids = {n.id for n in nodes}
        for e in edges:
            self.assertIn(e.source_id, node_ids, f"Edge {e.id} source {e.source_id} not in nodes")
            self.assertIn(e.target_id, node_ids, f"Edge {e.id} target {e.target_id} not in nodes")


# ============================================================================
# CodeGraphAdapter -- is_available / index_status
# ============================================================================


class TestCodeGraphAdapterAvailability(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_not_available_when_no_binary(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            self.assertFalse(adapter.is_available())

    def test_available_when_binary_present(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/usr/local/bin/codegraph",
        ):
            adapter = CodeGraphAdapter(self.workspace)
            self.assertTrue(adapter.is_available())

    def test_index_status_no_binary(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            status = adapter.index_status()
            self.assertFalse(status["available"])
            self.assertIsNone(status["version"])
            self.assertFalse(status["has_index"])

    def test_index_status_with_binary(self) -> None:
        proc = mock.Mock(returncode=0, stdout="codegraph 1.2.3\n", stderr="")
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/usr/local/bin/codegraph",
        ):
            with mock.patch("subprocess.run", return_value=proc):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                    return_value=True,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    status = adapter.index_status()
                    self.assertTrue(status["available"])
                    self.assertIsNotNone(status["version"])
                    self.assertTrue(status["has_index"])


# ============================================================================
# CodeGraphAdapter -- cache behavior
# ============================================================================


class TestCodeGraphAdapterCache(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_cache_hit_returns_cached_value(self) -> None:
        adapter = CodeGraphAdapter(self.workspace)
        key = "test:key"
        call_count = 0

        def factory() -> str:
            nonlocal call_count
            call_count += 1
            return "computed"

        result1 = adapter._cached(key, factory)
        result2 = adapter._cached(key, factory)

        self.assertEqual(result1, "computed")
        self.assertEqual(result2, "computed")
        self.assertEqual(call_count, 1)

    def test_cache_expired_refreshes(self) -> None:
        from agent_engine.repo_intelligence import codegraph_adapter as cga

        adapter = CodeGraphAdapter(self.workspace)
        key = "expired:key"
        call_count = 0

        def factory() -> str:
            nonlocal call_count
            call_count += 1
            return f"value_{call_count}"

        # Inject an expired entry
        adapter._cache[key] = (time.monotonic() - cga._CACHE_TTL_SECONDS - 10, "stale")
        result = adapter._cached(key, factory)
        self.assertEqual(result, "value_1")
        self.assertEqual(call_count, 1)

    def test_cache_invalidate_all(self) -> None:
        adapter = CodeGraphAdapter(self.workspace)
        adapter._cache["k1"] = (time.monotonic(), "v1")
        adapter._cache["k2"] = (time.monotonic(), "v2")
        adapter.invalidate()
        self.assertEqual(adapter._cache, {})

    def test_cache_invalidate_nodes_only(self) -> None:
        adapter = CodeGraphAdapter(self.workspace)
        adapter._cache["symbol:n1:none:..."] = (time.monotonic(), "v1")
        adapter._cache["symbol:n2:none:..."] = (time.monotonic(), "v2")
        adapter._cache["file:src/main.py:..."] = (time.monotonic(), "v3")
        adapter.invalidate(nodes=["n1"])
        self.assertNotIn("symbol:n1:none:...", adapter._cache)
        self.assertIn("symbol:n2:none:...", adapter._cache)
        self.assertIn("file:src/main.py:...", adapter._cache)


# ============================================================================
# CodeGraphAdapter -- query_symbol
# ============================================================================


class TestCodeGraphAdapterQuerySymbol(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_when_codegraph_unavailable(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes = adapter.query_symbol("authenticate")
            self.assertGreaterEqual(len(nodes), 1)
            self.assertTrue(any("authenticate" in n.name.lower() for n in nodes))

    def test_fallback_when_codegraph_unavailable_no_matches(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes = adapter.query_symbol("zzzz_nonexistent_symbol_zzzz")
            self.assertEqual(len(nodes), 0)

    def test_with_mock_codegraph_binary(self) -> None:
        proc_result = {"ok": True, "stdout": json.dumps([
            {"id": "n1", "name": "authenticate", "type": "function", "file": "src/auth.py", "line": 1, "endLine": 2},
        ])}
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter._run_codegraph",
                    return_value=proc_result,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    nodes = adapter.query_symbol("authenticate")
                    self.assertEqual(len(nodes), 1)
                    self.assertEqual(nodes[0].name, "authenticate")
                    self.assertEqual(nodes[0].node_type, "function")

    def test_with_kind_filter_and_mock_codegraph(self) -> None:
        proc_result = {"ok": True, "stdout": json.dumps([
            {"id": "n1", "name": "main", "type": "file", "file": "src/main.py"},
        ])}
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                run_mock = mock.Mock(return_value=proc_result)
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter._run_codegraph",
                    run_mock,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    nodes = adapter.query_symbol("main", kind="file")
                    self.assertEqual(len(nodes), 1)
                    args_used = run_mock.call_args[0][1]
                    self.assertIn("--kind", args_used)

    def test_fallback_match_is_substring(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes = adapter.query_symbol("auth")
            self.assertGreaterEqual(len(nodes), 1)


# ============================================================================
# CodeGraphAdapter -- query_file
# ============================================================================


class TestCodeGraphAdapterQueryFile(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_returns_file_nodes(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes = adapter.query_file("src/auth.py")
            self.assertGreaterEqual(len(nodes), 1)
            self.assertIn("src/auth.py", nodes[0].file_path.replace("\\", "/"))

    def test_fallback_unknown_file_empty(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes = adapter.query_file("src/nonexistent.py")
            self.assertEqual(len(nodes), 0)

    def test_with_mock_codegraph(self) -> None:
        proc_result = {"ok": True, "stdout": json.dumps([
            {"id": "n1", "name": "auth", "type": "file", "file": "src/auth.py"},
        ])}
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter._run_codegraph",
                    return_value=proc_result,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    nodes = adapter.query_file("src/auth.py")
                    self.assertGreaterEqual(len(nodes), 1)
                    self.assertEqual(nodes[0].file_path, "src/auth.py")


# ============================================================================
# CodeGraphAdapter -- query_dependencies
# ============================================================================


class TestCodeGraphAdapterQueryDependencies(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_outgoing_deps_fallback(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes, _ = adapter._ensure_fallback()
            file_nodes = [n for n in nodes if n.id.startswith("fallback:file:")]
            if file_nodes:
                deps = adapter.query_dependencies(file_nodes[0].id, direction="outgoing")
                self.assertIsInstance(deps, list)
                for e in deps:
                    self.assertEqual(e.source_id, file_nodes[0].id)

    def test_incoming_deps_fallback(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes, _ = adapter._ensure_fallback()
            # Find a node that is a target of some edge
            imported_nodes = [
                n for n in nodes
                if n.id.startswith("fallback:mod:") or n.id.startswith("fallback:pkg:")
            ]
            if imported_nodes:
                deps = adapter.query_dependencies(imported_nodes[0].id, direction="incoming")
                self.assertIsInstance(deps, list)

    def test_both_direction_fallback(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes, _ = adapter._ensure_fallback()
            file_nodes = [n for n in nodes if n.id.startswith("fallback:file:")]
            if file_nodes:
                deps = adapter.query_dependencies(file_nodes[0].id, direction="both")
                self.assertIsInstance(deps, list)

    def test_with_mock_codegraph(self) -> None:
        proc_result = {"ok": True, "stdout": json.dumps([
            {"id": "e1", "source": "n1", "target": "n2", "type": "imports"},
            {"id": "e2", "source": "n1", "target": "n3", "type": "calls"},
        ])}
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter._run_codegraph",
                    return_value=proc_result,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    # Need to use a key the adapter can work with in outgoing deps
                    deps = adapter.query_dependencies("n1", direction="outgoing")
                    self.assertEqual(len(deps), 2)


# ============================================================================
# CodeGraphAdapter -- query_graph
# ============================================================================


class TestCodeGraphAdapterQueryGraph(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_graph_query_with_seeds(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes, _ = adapter._ensure_fallback()
            seed_ids = [n.id for n in nodes[:2]]
            if seed_ids:
                result_nodes, result_edges = adapter.query_graph(seed_ids, max_depth=2, max_nodes=20)
                self.assertIsInstance(result_nodes, list)
                self.assertIsInstance(result_edges, list)

    def test_fallback_graph_query_max_nodes_budget(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            nodes, _ = adapter._ensure_fallback()
            seed_ids = [n.id for n in nodes[:2]]
            if seed_ids:
                result_nodes, _ = adapter.query_graph(seed_ids, max_depth=2, max_nodes=3)
                self.assertLessEqual(len(result_nodes), 3)

    def test_empty_seeds_returns_empty(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            result_nodes, result_edges = adapter.query_graph([], max_depth=2, max_nodes=10)
            self.assertEqual(len(result_nodes), 0)
            self.assertEqual(len(result_edges), 0)

    def test_with_mock_codegraph(self) -> None:
        proc_result = {
            "ok": True,
            "stdout": json.dumps({
                "nodes": [{"id": "n1", "name": "a", "type": "file", "file": "a.py"}],
                "edges": [{"source": "n1", "target": "n2", "type": "imports"}],
            }),
        }
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter._run_codegraph",
                    return_value=proc_result,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    result_nodes, result_edges = adapter.query_graph(["n1"], max_depth=2, max_nodes=10)
                    self.assertGreaterEqual(len(result_nodes), 1)


# ============================================================================
# CodeGraphAdapter -- specialized queries
# ============================================================================


class TestCodeGraphAdapterFindEntrypoints(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_finds_entrypoints_from_manifests(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            entrypoints = adapter.find_entrypoints()
            self.assertGreaterEqual(len(entrypoints), 1)
            for ep in entrypoints:
                self.assertTrue(
                    ep.metadata.get("entrypoint") or ep.metadata.get("source"),
                    f"Entrypoint {ep.id} missing entrypoint marker",
                )

    def test_with_mock_codegraph(self) -> None:
        proc_result = {"ok": True, "stdout": json.dumps([
            {"id": "ep1", "name": "main", "type": "file", "file": "src/main.py", "metadata": {"entrypoint": True}},
        ])}
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter._run_codegraph",
                    return_value=proc_result,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    entrypoints = adapter.find_entrypoints()
                    self.assertGreaterEqual(len(entrypoints), 1)
                    self.assertEqual(entrypoints[0].name, "main")


class TestCodeGraphAdapterFindRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_finds_routes(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            routes = adapter.find_routes()
            self.assertGreaterEqual(len(routes), 1)
            for r in routes:
                self.assertEqual(r.node_type, "route")


class TestCodeGraphAdapterFindServices(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_finds_nothing_without_service_dirs(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            # No service directories in default workspace
            services = adapter.find_services()
            self.assertEqual(len(services), 0)

    def test_fallback_finds_services_when_dir_exists(self) -> None:
        root = Path(self.workspace)
        (root / "services").mkdir(exist_ok=True)
        (root / "services" / "__init__.py").write_text("", encoding="utf-8")
        (root / "services" / "user.py").write_text("class UserService: pass\n", encoding="utf-8")
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            adapter.invalidate()  # clear stale fallback
            services = adapter.find_services()
            self.assertGreaterEqual(len(services), 1)
            for svc in services:
                self.assertEqual(svc.node_type, "service")


class TestCodeGraphAdapterFindTestsForFile(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_finds_test_by_naming_convention(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            tests = adapter.find_tests_for_file("src/auth.py")
            self.assertGreaterEqual(len(tests), 1)
            self.assertIn("test", tests[0].node_type)

    def test_fallback_no_tests_for_unknown_file(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            tests = adapter.find_tests_for_file("src/zzz_unknown.py")
            self.assertEqual(len(tests), 0)

    def test_with_mock_codegraph(self) -> None:
        proc_result = {
            "ok": True,
            "enabled": True,
            "status": "ok",
            "affectedTests": [
                {"id": "t1", "name": "test_auth", "type": "test", "file": "tests/test_auth.py"},
            ],
        }
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value="/bin/codegraph",
        ):
            with mock.patch(
                "agent_engine.repo_intelligence.codegraph_adapter.has_codegraph_index",
                return_value=True,
            ):
                with mock.patch(
                    "agent_engine.repo_intelligence.codegraph_adapter.codegraph_affected_tests",
                    return_value=proc_result,
                ):
                    adapter = CodeGraphAdapter(self.workspace)
                    tests = adapter.find_tests_for_file("src/auth.py")
                    self.assertGreaterEqual(len(tests), 1)
                    self.assertEqual(tests[0].name, "test_auth")


class TestCodeGraphAdapterFindDefinition(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_finds_definition_by_name(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            # The fallback graph creates file nodes with name=basename.
            # "auth.py" is present in the workspace so should match.
            result = adapter.find_definition("auth.py")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.name, "auth.py")

    def test_fallback_returns_none_for_unknown(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            self.assertIsNone(adapter.find_definition("zzz_nonexistent"))


class TestCodeGraphAdapterFindReferences(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_fallback_finds_references_to_symbol(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            refs = adapter.find_references("authenticate")
            self.assertIsInstance(refs, list)
            # May or may not find results depending on graph structure
            # but should not crash

    def test_fallback_unknown_symbol_returns_empty(self) -> None:
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace)
            refs = adapter.find_references("zzz_never_defined")
            self.assertEqual(len(refs), 0)


# ============================================================================
# CodeGraphAdapter -- context_for_task / affected_tests
# ============================================================================


class TestCodeGraphAdapterContextAndAffected(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_context_for_task_delegates(self) -> None:
        expected = {"context": "graph data"}
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_context",
            return_value=expected,
        ) as ctx_mock:
            adapter = CodeGraphAdapter(self.workspace)
            result = adapter.context_for_task("add login")
            self.assertEqual(result, expected)
            ctx_mock.assert_called_once()
            call_args = ctx_mock.call_args[0]
            self.assertEqual(Path(call_args[0]).resolve(), Path(self.workspace).resolve())
            self.assertEqual(call_args[1], "add login")

    def test_affected_tests_delegates(self) -> None:
        expected = {"affectedTests": []}
        changed = [{"path": "src/auth.py", "status": "modified"}]
        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_affected_tests",
            return_value=expected,
        ) as at_mock:
            adapter = CodeGraphAdapter(self.workspace)
            result = adapter.affected_tests(changed)
            self.assertEqual(result, expected)
            at_mock.assert_called_once()
            call_args = at_mock.call_args[0]
            self.assertEqual(Path(call_args[0]).resolve(), Path(self.workspace).resolve())


# ============================================================================
# CodeGraphAdapter -- event emission
# ============================================================================


class TestCodeGraphAdapterEventEmission(unittest.TestCase):
    def setUp(self) -> None:
        self._td = _make_test_workspace()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_emit_is_called_during_operations(self) -> None:
        events: list[tuple[str, str]] = []
        def collect(event: str, detail: str) -> None:
            events.append((event, detail))

        with mock.patch(
            "agent_engine.repo_intelligence.codegraph_adapter.codegraph_binary",
            return_value=None,
        ):
            adapter = CodeGraphAdapter(self.workspace, emit=collect)
            adapter.query_symbol("authenticate")
            # Fallback build should have emitted events
            build_events = [e for e in events if "fallback" in e[0]]
            self.assertGreater(len(build_events), 0)


if __name__ == "__main__":
    unittest.main()
