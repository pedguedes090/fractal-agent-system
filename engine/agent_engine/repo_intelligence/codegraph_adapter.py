"""CodeGraphAdapter -- unified interface for code graph queries.

Wraps the CodeGraph CLI binary with a proper adapter interface.
When codegraph is unavailable, falls back to filesystem-based
analysis: import parsing, project manifest detection, and
convention-based graph construction.

Imports:
    ..workspace   -- codegraph_binary, has_codegraph_index, _run_codegraph,
                     codegraph_context, codegraph_affected_tests,
                     ensure_codegraph_index, walk_workspace, read_file
    ..debug_log   -- write_debug_event
    .models       -- GraphNode, GraphEdge, NodeType, EdgeType
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .models import EdgeType, GraphEdge, GraphNode, NodeType
from ..debug_log import write_debug_event
from ..workspace import (
    _run_codegraph,
    codegraph_affected_tests,
    codegraph_binary,
    codegraph_context,
    ensure_codegraph_index,
    has_codegraph_index,
    read_file,
    walk_workspace,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FALLBACK_MAX_FILES = 200
_CACHE_TTL_SECONDS = 300  # 5 minutes

# File extension -> language mapping for fallback analysis
_PYTHON_EXTS = frozenset({".py", ".pyw"})
_JS_EXTS = frozenset({".js", ".jsx", ".mjs", ".cjs"})
_TS_EXTS = frozenset({".ts", ".tsx", ".mts", ".cts"})
_GO_EXTS = frozenset({".go"})
_RUST_EXTS = frozenset({".rs"})
_JAVA_EXTS = frozenset({".java", ".kt", ".kts", ".scala"})
_CPP_EXTS = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx"})
_CSHARP_EXTS = frozenset({".cs"})
_RUBY_EXTS = frozenset({".rb"})
_ALL_CODE_EXTS = frozenset().union(
    _PYTHON_EXTS, _JS_EXTS, _TS_EXTS, _GO_EXTS, _RUST_EXTS,
    _JAVA_EXTS, _CPP_EXTS, _CSHARP_EXTS, _RUBY_EXTS,
)

# Test file naming patterns
_TEST_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^test_", r"_test\.", r"test\.", r"\.test\.",
        r"\.spec\.", r"\.test\.", r"_spec\.", r"^spec_",
        r"tests?[/\\]", r"__tests__[/\\]",
    ]
]

# Route detection patterns per language
_ROUTE_PATTERNS: dict[str, list[re.Pattern]] = {
    "python": [
        re.compile(r"@(?:app|router|bp|blueprint)\.(?:get|post|put|delete|patch|route|add_url_rule)\s*\("),
        re.compile(r"@(?:get|post|put|delete|patch|options|head)\s*\("),
        re.compile(r"\.add_route\s*\(\s*[\"']"),
        re.compile(r"@api\.(?:route|get|post|put|delete)\s*\("),
    ],
    "javascript": [
        re.compile(r"\.(?:get|post|put|delete|patch|all|use)\s*\(\s*[\"']/"),
        re.compile(r"Router\(\)\.(?:get|post|put|delete|patch)"),
        re.compile(r"createRouter\s*\(.*?\)\.(?:get|post|put|delete)"),
        re.compile(r"@(?:Get|Post|Put|Delete|Patch)\s*\("),
    ],
    "typescript": [
        re.compile(r"\.(?:get|post|put|delete|patch|all|use)\s*\(\s*[\"']/"),
        re.compile(r"Router\(\)\.(?:get|post|put|delete|patch)"),
        re.compile(r"createRouter\s*\(.*?\)\.(?:get|post|put|delete)"),
        re.compile(r"@(?:Get|Post|Put|Delete|Patch)\s*\("),
    ],
    "go": [
        re.compile(r"\.Handle(?:Func)?\s*\(\s*[\"']/"),
        re.compile(r"\.(?:GET|POST|PUT|DELETE|PATCH)\s*\(\s*[\"']/"),
        re.compile(r"mux\.(?:NewRouter|HandleFunc)"),
    ],
    "rust": [
        re.compile(r"#\[(?:get|post|put|delete|patch)\s*\(\s*[\"']/"),
        re.compile(r"\.route\s*\(\s*[\"']/"),
        re.compile(r"web::(?:get|post|put|delete)"),
    ],
}

# Entrypoint detection patterns
_ENTRYPOINT_PATTERNS: dict[str, list[re.Pattern]] = {
    "python": [
        re.compile(r'if\s+__name__\s*==\s*["\']__main__["\']'),
    ],
    "go": [
        re.compile(r"^package\s+main\s*$", re.MULTILINE),
        re.compile(r"^func\s+main\s*\(\s*\)", re.MULTILINE),
    ],
    "rust": [
        re.compile(r"^fn\s+main\s*\(\s*\)", re.MULTILINE),
    ],
}

# Service detection from directory naming
_SERVICE_DIR_NAMES = frozenset({
    "services", "service", "handlers", "controllers",
    "routes", "endpoints", "api", "repositories", "daos",
    "middleware", "workers", "consumers", "producers",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_commit_hash(workspace: str) -> str | None:
    """Get the current HEAD commit hash, or None if not a git repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def _latest_mtime(workspace: str) -> float:
    """Get the most recent modification time among tracked files."""
    try:
        root = Path(workspace)
        max_mtime = 0.0
        for item in walk_workspace(workspace, max_files=_FALLBACK_MAX_FILES, max_depth=6):
            fp = root / item["path"]
            try:
                mtime = fp.stat().st_mtime
                if mtime > max_mtime:
                    max_mtime = mtime
            except OSError:
                continue
        return max_mtime
    except Exception:
        return 0.0


def _cache_key(workspace: str) -> str:
    """Stable cache key: commit hash if git, else latest mtime."""
    commit = _git_commit_hash(workspace)
    if commit:
        return f"{workspace}@{commit}"
    mtime = _latest_mtime(workspace)
    return f"{workspace}@{mtime:.0f}"


def _parse_codegraph_json(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse JSON output from a codegraph CLI result dict."""
    if not result.get("ok"):
        return []
    raw = (result.get("stdout") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Some codegraph commands return { "nodes": [...], "edges": [...] }
        nodes = parsed.get("nodes", [])
        edges = parsed.get("edges", [])
        if nodes or edges:
            combined = list(nodes)
            for e in edges:
                combined.append(e)
            return combined
        return [parsed]
    return []


def _dicts_to_nodes(data: list[dict[str, Any]]) -> list[GraphNode]:
    """Convert raw dicts from codegraph JSON to GraphNode objects."""
    nodes: list[GraphNode] = []
    for item in data:
        try:
            nodes.append(GraphNode(
                id=str(item.get("id") or item.get("name") or ""),
                name=str(item.get("name") or item.get("symbol") or item.get("label") or ""),
                node_type=str(item.get("type") or item.get("kind") or item.get("nodeType") or "file"),
                file_path=str(item.get("file") or item.get("filePath") or item.get("path") or ""),
                line_start=int(item.get("lineStart") or item.get("line") or item.get("startLine") or 0),
                line_end=int(item.get("lineEnd") or item.get("endLine") or 0),
                parent_id=item.get("parentId") or item.get("parent"),
                metadata=item.get("metadata") or item.get("properties") or item.get("attrs") or {},
            ))
        except Exception:
            continue
    return nodes


def _dicts_to_edges(data: list[dict[str, Any]]) -> list[GraphEdge]:
    """Convert raw dicts from codegraph JSON to GraphEdge objects."""
    edges: list[GraphEdge] = []
    for item in data:
        try:
            edges.append(GraphEdge(
                id=str(item.get("id") or f"{item.get('source')}->{item.get('target')}"),
                source_id=str(item.get("source") or item.get("sourceId") or item.get("from") or ""),
                target_id=str(item.get("target") or item.get("targetId") or item.get("to") or ""),
                edge_type=str(item.get("type") or item.get("edgeType") or item.get("relation") or "imports"),
                metadata=item.get("metadata") or item.get("properties") or {},
            ))
        except Exception:
            continue
    return edges


def _detect_language(file_path: str) -> str | None:
    """Map file extension to language for fallback analysis."""
    ext = Path(file_path).suffix.lower()
    if ext in _PYTHON_EXTS:
        return "python"
    if ext in _TS_EXTS:
        return "typescript"
    if ext in _JS_EXTS:
        return "javascript"
    if ext in _GO_EXTS:
        return "go"
    if ext in _RUST_EXTS:
        return "rust"
    if ext in _JAVA_EXTS:
        return "java"
    if ext in _CSHARP_EXTS:
        return "csharp"
    if ext in _RUBY_EXTS:
        return "ruby"
    if ext in _CPP_EXTS:
        return "cpp"
    return None


def _is_test_file(file_path: str) -> bool:
    """Check if a file path matches test naming conventions."""
    basename = Path(file_path).name
    for pat in _TEST_PATTERNS:
        if pat.search(file_path) or pat.search(basename):
            return True
    return False


# ---------------------------------------------------------------------------
# Fallback graph builder (filesystem-based, no codegraph binary)
# ---------------------------------------------------------------------------


def _parse_python_imports(content: str, file_path: str, nodes: dict[str, GraphNode], edges: list[GraphEdge], file_node_id: str) -> list[GraphNode]:
    """Parse import statements from Python source. Returns any new GraphNodes."""
    new_nodes: list[GraphNode] = []
    import_re = re.compile(
        r"^(?:from\s+(\S+)\s+import\s+(.+?)|\s*import\s+(.+?))\s*(?:#.*)?$",
        re.MULTILINE,
    )
    for match in import_re.finditer(content):
        from_mod, from_imports, direct_imports = match.groups()
        if from_mod:
            targets = [from_mod]
            for name in from_imports.split(","):
                name = name.strip().split(" as ")[0].strip()
                if name:
                    targets.append(f"{from_mod}.{name}")
        else:
            targets = [
                name.strip().split(" as ")[0].strip()
                for name in (direct_imports or "").split(",")
            ]

        for target in targets:
            if not target:
                continue
            node_id = f"fallback:mod:{target}"
            if node_id not in nodes:
                node = GraphNode(
                    id=node_id,
                    name=target,
                    node_type="module",
                    file_path="",
                )
                nodes[node_id] = node
                new_nodes.append(node)
            edges.append(GraphEdge(
                source_id=file_node_id,
                target_id=node_id,
                edge_type="imports",
            ))
    return new_nodes


def _parse_js_ts_imports(content: str, file_path: str, nodes: dict[str, GraphNode], edges: list[GraphEdge], file_node_id: str) -> list[GraphNode]:
    """Parse import/require statements from JS/TS source."""
    new_nodes: list[GraphNode] = []
    # ES module imports
    es_import_re = re.compile(
        r"""(?:import\s+(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s*(?:,\s*(?:\{[^}]*\}|\*\s+as\s+\w+|\w+))?\s*from\s*['"]([^'"]+)['"])""",
        re.MULTILINE,
    )
    for match in es_import_re.finditer(content):
        target = match.group(1)
        if target.startswith("."):
            continue  # relative import, harder to resolve in fallback
        node_id = f"fallback:pkg:{target}"
        if node_id not in nodes:
            node = GraphNode(id=node_id, name=target, node_type="module", file_path="")
            nodes[node_id] = node
            new_nodes.append(node)
        edges.append(GraphEdge(source_id=file_node_id, target_id=node_id, edge_type="imports"))

    # CommonJS require
    require_re = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
    for match in require_re.finditer(content):
        target = match.group(1)
        if target.startswith("."):
            continue
        node_id = f"fallback:pkg:{target}"
        if node_id not in nodes:
            node = GraphNode(id=node_id, name=target, node_type="module", file_path="")
            nodes[node_id] = node
            new_nodes.append(node)
        edges.append(GraphEdge(source_id=file_node_id, target_id=node_id, edge_type="imports"))

    # Dynamic import()
    dyn_import_re = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""")
    for match in dyn_import_re.finditer(content):
        target = match.group(1)
        if target.startswith("."):
            continue
        node_id = f"fallback:pkg:{target}"
        if node_id not in nodes:
            node = GraphNode(id=node_id, name=target, node_type="module", file_path="")
            nodes[node_id] = node
            new_nodes.append(node)
        edges.append(GraphEdge(source_id=file_node_id, target_id=node_id, edge_type="imports"))

    return new_nodes


def _parse_go_imports(content: str, file_path: str, nodes: dict[str, GraphNode], edges: list[GraphEdge], file_node_id: str) -> list[GraphNode]:
    """Parse Go import blocks."""
    new_nodes: list[GraphNode] = []
    # Single imports: import "pkg"
    single_re = re.compile(r'^import\s+"([^"]+)"', re.MULTILINE)
    # Block imports: import ( ... )
    block_re = re.compile(r'import\s*\(\s*((?:[^)]*"(?:[^"\\]|\\.)*"[^)]*)*)\s*\)', re.MULTILINE)
    quoted_re = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')

    for match in single_re.finditer(content):
        target = match.group(1)
        node_id = f"fallback:pkg:{target}"
        if node_id not in nodes:
            node = GraphNode(id=node_id, name=target, node_type="module", file_path="")
            nodes[node_id] = node
            new_nodes.append(node)
        edges.append(GraphEdge(source_id=file_node_id, target_id=node_id, edge_type="imports"))

    for block_match in block_re.finditer(content):
        for qm in quoted_re.finditer(block_match.group(1)):
            target = qm.group(1)
            node_id = f"fallback:pkg:{target}"
            if node_id not in nodes:
                node = GraphNode(id=node_id, name=target, node_type="module", file_path="")
                nodes[node_id] = node
                new_nodes.append(node)
            edges.append(GraphEdge(source_id=file_node_id, target_id=node_id, edge_type="imports"))

    return new_nodes


def _detect_routes_in_file(content: str, lang: str, file_node_id: str, nodes: dict[str, GraphNode], edges: list[GraphEdge]) -> list[GraphNode]:
    """Detect route definitions in source and create route nodes."""
    new_nodes: list[GraphNode] = []
    patterns = _ROUTE_PATTERNS.get(lang, [])
    for pat in patterns:
        for match in pat.finditer(content):
            route_text = match.group(0).strip()
            node_id = f"fallback:route:{file_node_id}:{len(new_nodes)}"
            node = GraphNode(
                id=node_id,
                name=route_text[:80],
                node_type="route",
                file_path="",
                parent_id=file_node_id,
                metadata={"language": lang, "pattern": route_text},
            )
            nodes[node_id] = node
            new_nodes.append(node)
            edges.append(GraphEdge(
                source_id=file_node_id,
                target_id=node_id,
                edge_type="exposes",
            ))
    return new_nodes


def _detect_entrypoints_in_file(content: str, lang: str, file_path: str, file_node_id: str, nodes: dict[str, GraphNode]) -> list[GraphNode]:
    """Detect entrypoints in source."""
    new_nodes: list[GraphNode] = []
    patterns = _ENTRYPOINT_PATTERNS.get(lang, [])
    for pat in patterns:
        if pat.search(content):
            node_id = f"fallback:entry:{file_node_id}"
            node = GraphNode(
                id=node_id,
                name=f"entry:{file_path}",
                node_type="file",
                file_path=file_path,
                parent_id=file_node_id,
                metadata={"entrypoint": True, "language": lang},
            )
            nodes[node_id] = node
            new_nodes.append(node)
            return new_nodes  # one entrypoint per file
    return new_nodes


def _read_manifest(workspace: str, filename: str) -> dict[str, Any] | None:
    """Read and parse a JSON manifest from the workspace root."""
    try:
        raw = read_file(workspace, filename, 50000)
        return json.loads(raw)
    except Exception:
        return None


def _read_text_manifest(workspace: str, filename: str) -> str | None:
    """Read a text manifest (e.g. go.mod, Cargo.toml)."""
    try:
        return read_file(workspace, filename, 20000)
    except Exception:
        return None


def _build_fallback_graph(workspace: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Build a code graph from filesystem scanning when codegraph is unavailable.

    Returns (nodes, edges) representing the workspace structure,
    import relationships, entrypoints, routes, and tests.
    """
    root = Path(workspace).resolve()
    files = walk_workspace(workspace, max_files=_FALLBACK_MAX_FILES, max_depth=6)
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    all_nodes: list[GraphNode] = []

    # Phase 1: create file nodes
    for item in files:
        path = item["path"]
        if not item.get("text"):
            continue
        lang = _detect_language(path)
        node_type: NodeType = "test" if _is_test_file(path) else "file"
        node_id = f"fallback:file:{path}"
        node = GraphNode(
            id=node_id,
            name=Path(path).name,
            node_type=node_type,
            file_path=path,
            metadata={"language": lang or "unknown", "size": item.get("size", 0)},
        )
        nodes[node_id] = node
        all_nodes.append(node)

    # Phase 2: parse imports and detect symbols per file
    for item in files:
        path = item["path"]
        if not item.get("text"):
            continue
        lang = _detect_language(path)
        file_node_id = f"fallback:file:{path}"
        if lang is None:
            continue

        try:
            content = read_file(workspace, path, 30000)
        except Exception:
            content = ""

        if lang == "python":
            _parse_python_imports(content, path, nodes, edges, file_node_id)
        elif lang in ("javascript", "typescript"):
            _parse_js_ts_imports(content, path, nodes, edges, file_node_id)
        elif lang == "go":
            _parse_go_imports(content, path, nodes, edges, file_node_id)

        _detect_routes_in_file(content, lang, file_node_id, nodes, edges)
        _detect_entrypoints_in_file(content, lang, path, file_node_id, nodes)

    # Phase 3: detect manifest-based entrypoints
    pkg_json = _read_manifest(workspace, "package.json")
    if pkg_json:
        main_file = pkg_json.get("main", "")
        if main_file:
            entry_id = f"fallback:entry:manifest:{main_file}"
            node = GraphNode(
                id=entry_id,
                name=f"main:{main_file}",
                node_type="file",
                file_path=main_file,
                metadata={"entrypoint": True, "source": "package.json#main"},
            )
            nodes[entry_id] = node
            all_nodes.append(node)

        for script_name, script_cmd in (pkg_json.get("scripts") or {}).items():
            if script_name in ("start", "dev", "serve", "build"):
                entry_id = f"fallback:entry:script:{script_name}"
                node = GraphNode(
                    id=entry_id,
                    name=f"script:{script_name}",
                    node_type="configuration",
                    file_path="package.json",
                    metadata={"entrypoint": True, "command": str(script_cmd), "source": "package.json#scripts"},
                )
                nodes[entry_id] = node
                all_nodes.append(node)

    pyproject = _read_text_manifest(workspace, "pyproject.toml")
    if pyproject:
        # Simple detection of [project.scripts] or [tool.poetry.scripts]
        script_match = re.search(
            r'\[(?:project\.scripts|tool\.poetry\.scripts)\]\s*\n((?:\s*[a-zA-Z_]\w*\s*=\s*"[^"]*"\s*\n?)+)',
            pyproject,
        )
        if script_match:
            for line in script_match.group(1).strip().split("\n"):
                parts = line.strip().split("=", 1)
                if len(parts) == 2:
                    entry_name = parts[0].strip()
                    entry_id = f"fallback:entry:pyproject:{entry_name}"
                    node = GraphNode(
                        id=entry_id,
                        name=f"script:{entry_name}",
                        node_type="configuration",
                        file_path="pyproject.toml",
                        metadata={"entrypoint": True, "command": parts[1].strip().strip('"'), "source": "pyproject.toml"},
                    )
                    nodes[entry_id] = node
                    all_nodes.append(node)

    go_mod = _read_text_manifest(workspace, "go.mod")
    if go_mod:
        mod_match = re.search(r"^module\s+(\S+)", go_mod, re.MULTILINE)
        if mod_match:
            mod_name = mod_match.group(1)
            node_id = f"fallback:mod:go:{mod_name}"
            node = GraphNode(
                id=node_id,
                name=mod_name,
                node_type="module",
                file_path="go.mod",
                metadata={"language": "go", "source": "go.mod"},
            )
            nodes[node_id] = node
            all_nodes.append(node)

    cargo_toml = _read_text_manifest(workspace, "Cargo.toml")
    if cargo_toml:
        name_match = re.search(r'^name\s*=\s*"([^"]+)"', cargo_toml, re.MULTILINE)
        if name_match:
            node_id = f"fallback:mod:rust:{name_match.group(1)}"
            node = GraphNode(
                id=node_id,
                name=name_match.group(1),
                node_type="module",
                file_path="Cargo.toml",
                metadata={"language": "rust", "source": "Cargo.toml"},
            )
            nodes[node_id] = node
            all_nodes.append(node)

    # Phase 4: detect service directories
    for item in files:
        path = item["path"]
        parts = Path(path).parts
        for part in parts[:-1]:  # skip filename
            if part.lower() in _SERVICE_DIR_NAMES:
                svc_id = f"fallback:service:{part}"
                if svc_id not in nodes:
                    node = GraphNode(
                        id=svc_id,
                        name=part,
                        node_type="service",
                        file_path=str(Path(*parts[:parts.index(part) + 1])).replace("\\", "/"),
                    )
                    nodes[svc_id] = node
                    all_nodes.append(node)
                file_node_id = f"fallback:file:{path}"
                edges.append(GraphEdge(
                    source_id=svc_id,
                    target_id=file_node_id,
                    edge_type="registers",
                ))
                break

    # Add any remaining nodes from the dict that weren't in all_nodes
    for nid, node in nodes.items():
        if node not in all_nodes:
            all_nodes.append(node)

    return all_nodes, edges


# ---------------------------------------------------------------------------
# CodeGraphAdapter
# ---------------------------------------------------------------------------


class CodeGraphAdapter:
    """Unified interface for code graph queries.

    Wraps the CodeGraph CLI binary with structured query methods.
    When the binary or index is unavailable, falls back to
    filesystem-based graph analysis (import parsing, manifest
    detection, convention-based node construction).
    """

    def __init__(self, workspace: str, emit: Callable[[str, str], None] | None = None) -> None:
        self._workspace = str(Path(workspace).resolve())
        self._emit = emit or (lambda _t, _m: None)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._fallback_nodes: list[GraphNode] | None = None
        self._fallback_edges: list[GraphEdge] | None = None
        self._fallback_built = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_event(self, event: str, detail: str) -> None:
        """Emit a structured event and log to debug."""
        try:
            self._emit(event, detail)
        except Exception:
            pass
        try:
            write_debug_event(
                f"codegraph_adapter.{event}",
                {"detail": detail, "workspace": self._workspace},
            )
        except Exception:
            pass

    def _cached(self, key: str, factory: Callable[[], Any]) -> Any:
        """Return cached value or compute + cache."""
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is not None:
            ts, value = entry
            if now - ts < _CACHE_TTL_SECONDS:
                return value
        value = factory()
        self._cache[key] = (now, value)
        return value

    def _run_json(self, args: list[str], timeout: int = 45) -> list[dict[str, Any]]:
        """Run codegraph with --json and parse output."""
        result = _run_codegraph(self._workspace, [*args, "--json"], timeout=timeout)
        return _parse_codegraph_json(result)

    def _run_json_single(self, args: list[str], timeout: int = 45) -> dict[str, Any] | None:
        """Run codegraph with --json, return first dict or None."""
        items = self._run_json(args, timeout=timeout)
        return items[0] if items else None

    # ------------------------------------------------------------------
    # Fallback graph (lazy init)
    # ------------------------------------------------------------------

    def _ensure_fallback(self) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Build the fallback graph once and cache it."""
        if not self._fallback_built:
            key = f"fallback_graph:{_cache_key(self._workspace)}"
            def _build():
                self._emit_event("fallback_build", "Building filesystem-based code graph")
                return _build_fallback_graph(self._workspace)
            nodes, edges = self._cached(key, _build)
            self._fallback_nodes = nodes
            self._fallback_edges = edges
            self._fallback_built = True
            self._emit_event("fallback_built", f"{len(nodes)} nodes, {len(edges)} edges")
        return self._fallback_nodes or [], self._fallback_edges or []

    def _fallback_node_by_id(self, node_id: str) -> GraphNode | None:
        nodes, _ = self._ensure_fallback()
        for n in nodes:
            if n.id == node_id:
                return n
        return None

    def _fallback_lookup(self, name: str, kind: str | None = None) -> list[GraphNode]:
        nodes, _ = self._ensure_fallback()
        results: list[GraphNode] = []
        lower_name = name.lower()
        for n in nodes:
            if lower_name in n.name.lower():
                if kind and n.node_type != kind:
                    continue
                results.append(n)
        return results

    def _fallback_for_file(self, path: str) -> list[GraphNode]:
        nodes, _ = self._ensure_fallback()
        normalized = path.replace("\\", "/").strip("/")
        results: list[GraphNode] = []
        for n in nodes:
            if n.file_path.replace("\\", "/").strip("/") == normalized:
                results.append(n)
        if not results:
            # Try to create one on the fly
            file_node_id = f"fallback:file:{normalized}"
            for n in nodes:
                if n.id == file_node_id:
                    results.append(n)
                    break
        return results

    def _fallback_deps(self, node_id: str, direction: str = "outgoing") -> list[GraphEdge]:
        _, edges = self._ensure_fallback()
        results: list[GraphEdge] = []
        for e in edges:
            if direction == "outgoing" and e.source_id == node_id:
                results.append(e)
            elif direction == "incoming" and e.target_id == node_id:
                results.append(e)
            elif direction == "both" and (e.source_id == node_id or e.target_id == node_id):
                results.append(e)
        return results

    def _fallback_graph_query(
        self, seed_ids: list[str], max_depth: int = 2, max_nodes: int = 80
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """BFS from seed nodes up to max_depth."""
        nodes, edges = self._ensure_fallback()
        node_map = {n.id: n for n in nodes}
        adj: dict[str, list[str]] = {}
        for e in edges:
            adj.setdefault(e.source_id, []).append(e.target_id)
            adj.setdefault(e.target_id, []).append(e.source_id)

        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(sid, 0) for sid in seed_ids if sid in node_map]
        included_edges: list[GraphEdge] = []

        for sid, _d in queue:
            visited.add(sid)

        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            if len(visited) >= max_nodes:
                break
            for neighbor in adj.get(current, []):
                for e in edges:
                    if (e.source_id == current and e.target_id == neighbor) or \
                       (e.target_id == current and e.source_id == neighbor):
                        if e not in included_edges:
                            included_edges.append(e)
                if neighbor not in visited and len(visited) < max_nodes:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))

        result_nodes = [node_map[nid] for nid in visited if nid in node_map]
        return result_nodes, included_edges

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check whether the codegraph binary is present."""
        return codegraph_binary() is not None

    def index_status(self) -> dict[str, Any]:
        """Return index availability, version, and last-indexed time.

        Returns:
            dict with keys: available, version, last_indexed, has_index
        """
        binary = codegraph_binary()
        if not binary:
            return {"available": False, "version": None, "last_indexed": None, "has_index": False}

        version: str | None = None
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                version = (proc.stdout or proc.stderr or "").strip().split("\n")[0]
        except Exception:
            pass

        has_index = has_codegraph_index(self._workspace)
        last_indexed: str | None = None
        if has_index:
            index_dir = Path(self._workspace) / ".codegraph"
            try:
                stat = index_dir.stat()
                last_indexed = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(stat.st_mtime),
                )
            except OSError:
                pass

        return {
            "available": True,
            "version": version,
            "last_indexed": last_indexed,
            "has_index": has_index,
        }

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def invalidate(self, nodes: list[str] | None = None) -> None:
        """Invalidate the adapter cache.

        Args:
            nodes: Specific node IDs to invalidate, or None to clear all.
        """
        if nodes is None:
            self._cache.clear()
            self._fallback_nodes = None
            self._fallback_edges = None
            self._fallback_built = False
            self._emit_event("cache_invalidate", "full cache cleared")
        else:
            # Invalidate entries related to specific nodes
            to_remove: list[str] = []
            for key in self._cache:
                for nid in nodes:
                    if nid in key:
                        to_remove.append(key)
                        break
            for key in to_remove:
                self._cache.pop(key, None)
            self._emit_event("cache_invalidate", f"{len(to_remove)} entries for {len(nodes)} nodes")

    # ------------------------------------------------------------------
    # Core queries
    # ------------------------------------------------------------------

    def query_symbol(self, name: str, kind: str | None = None) -> list[GraphNode]:
        """Find graph nodes matching a symbol name.

        Uses codegraph ``find`` command when available, otherwise
        falls back to substring matching against the filesystem graph.
        """
        cache_name = f"symbol:{name}:{kind or 'any'}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                args = ["find", "--symbol", name]
                if kind:
                    args.extend(["--kind", kind])
                raw = self._run_json(args, timeout=45)
                if raw:
                    self._emit_event("query_symbol", f"codegraph: {name} -> {len(raw)} results")
                    return _dicts_to_nodes(raw)
            # Fallback
            nodes = self._fallback_lookup(name, kind)
            self._emit_event("query_symbol", f"fallback: {name} -> {len(nodes)} results")
            return nodes

        return list(self._cached(cache_name, _query))

    def query_file(self, path: str) -> list[GraphNode]:
        """Find graph nodes associated with a file path."""
        cache_name = f"file:{path}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                raw = self._run_json(["info", path], timeout=30)
                if raw:
                    self._emit_event("query_file", f"codegraph: {path} -> {len(raw)} results")
                    return _dicts_to_nodes(raw)
            # Fallback
            nodes = self._fallback_for_file(path)
            self._emit_event("query_file", f"fallback: {path} -> {len(nodes)} results")
            return nodes

        return list(self._cached(cache_name, _query))

    def query_dependencies(self, node_id: str, direction: str = "outgoing") -> list[GraphEdge]:
        """Find edges connected to a node.

        Args:
            node_id: The node to query.
            direction: "outgoing" (dependencies), "incoming" (dependents),
                       or "both".
        """
        cache_name = f"deps:{node_id}:{direction}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                raw = self._run_json(
                    ["deps", node_id, "--direction", direction],
                    timeout=45,
                )
                if raw or self._run_codegraph_failed_but_ok():
                    if raw:
                        self._emit_event("query_deps", f"codegraph: {node_id} -> {len(raw)} edges")
                        return _dicts_to_edges(raw)
            # Fallback
            edges = self._fallback_deps(node_id, direction)
            self._emit_event("query_deps", f"fallback: {node_id} -> {len(edges)} edges")
            return edges

        return list(self._cached(cache_name, _query))

    def _run_codegraph_failed_but_ok(self) -> bool:
        """Check if the last run produced a non-ok result.

        Used as a guard: if codegraph returned ok=False we should not
        try to parse empty data -- just fall through to fallback.
        """
        return False  # _parse_codegraph_json already handles ok=False

    def query_graph(
        self,
        seed_ids: list[str],
        max_depth: int = 2,
        max_nodes: int = 80,
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Query a subgraph around seed node IDs via BFS traversal.

        Args:
            seed_ids: Starting node IDs.
            max_depth: Maximum BFS depth from seeds.
            max_nodes: Maximum nodes to include.

        Returns:
            (nodes, edges) tuple.
        """
        seed_key = ",".join(sorted(seed_ids))
        cache_name = f"graph:{seed_key}:d{max_depth}:n{max_nodes}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace) and seed_ids:
                raw = self._run_json(
                    [
                        "graph",
                        "--seeds", ",".join(seed_ids),
                        "--max-depth", str(max_depth),
                        "--max-nodes", str(max_nodes),
                    ],
                    timeout=60,
                )
                if raw:
                    # codegraph graph may return {nodes: [...], edges: [...]}
                    nodes_data: list[dict[str, Any]] = []
                    edges_data: list[dict[str, Any]] = []
                    for item in raw:
                        if item.get("type") in _EDGE_TYPE_NAMES or item.get("source"):
                            edges_data.append(item)
                        else:
                            nodes_data.append(item)
                    if not edges_data and raw:
                        # May be a dict with nodes+edges keys
                        first = raw[0]
                        if isinstance(first, dict):
                            nd = first.get("nodes", [])
                            ed = first.get("edges", [])
                            if nd or ed:
                                nodes_data = nd if isinstance(nd, list) else []
                                edges_data = ed if isinstance(ed, list) else []
                    nodes = _dicts_to_nodes(nodes_data)
                    edges = _dicts_to_edges(edges_data)
                    self._emit_event(
                        "query_graph",
                        f"codegraph: {len(nodes)} nodes, {len(edges)} edges",
                    )
                    return nodes, edges
            # Fallback
            nodes, edges = self._fallback_graph_query(seed_ids, max_depth, max_nodes)
            self._emit_event(
                "query_graph",
                f"fallback: {len(nodes)} nodes, {len(edges)} edges",
            )
            return nodes, edges

        return self._cached(cache_name, _query)

    # ------------------------------------------------------------------
    # Specialized queries
    # ------------------------------------------------------------------

    def find_entrypoints(self) -> list[GraphNode]:
        """Find entrypoint nodes (main files, scripts, binary targets)."""
        cache_name = f"entrypoints:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                raw = self._run_json(["list", "--kind", "entrypoint"], timeout=30)
                if raw:
                    self._emit_event("find_entrypoints", f"codegraph: {len(raw)} results")
                    return _dicts_to_nodes(raw)
            # Fallback
            nodes, _ = self._ensure_fallback()
            results = [n for n in nodes if n.metadata.get("entrypoint")]
            if not results:
                # Broader search: any file with entrypoint markers
                for n in nodes:
                    if n.node_type == "configuration" and n.metadata.get("entrypoint"):
                        results.append(n)
            self._emit_event("find_entrypoints", f"fallback: {len(results)} results")
            return results

        return list(self._cached(cache_name, _query))

    def find_routes(self) -> list[GraphNode]:
        """Find route/handler/endpoint nodes."""
        cache_name = f"routes:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                raw = self._run_json(["list", "--kind", "route"], timeout=30)
                if raw:
                    self._emit_event("find_routes", f"codegraph: {len(raw)} results")
                    return _dicts_to_nodes(raw)
            # Fallback
            nodes, _ = self._ensure_fallback()
            results = [n for n in nodes if n.node_type == "route"]
            self._emit_event("find_routes", f"fallback: {len(results)} results")
            return results

        return list(self._cached(cache_name, _query))

    def find_services(self) -> list[GraphNode]:
        """Find service/module boundary nodes."""
        cache_name = f"services:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                raw = self._run_json(["list", "--kind", "service"], timeout=30)
                if raw:
                    self._emit_event("find_services", f"codegraph: {len(raw)} results")
                    return _dicts_to_nodes(raw)
            # Fallback
            nodes, _ = self._ensure_fallback()
            results = [n for n in nodes if n.node_type == "service"]
            self._emit_event("find_services", f"fallback: {len(results)} results")
            return results

        return list(self._cached(cache_name, _query))

    def find_tests_for_file(self, file_path: str) -> list[GraphNode]:
        """Find test nodes related to a given source file.

        Uses codegraph ``affected`` when available (tests that cover
        the file). Falls back to naming-convention matching.
        """
        cache_name = f"tests_for:{file_path}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                # Reuse the workspace-level affected_tests helper
                affected = codegraph_affected_tests(
                    self._workspace,
                    [{"path": file_path, "status": "modified"}],
                    max_chars=16000,
                )
                if affected.get("enabled") and affected.get("status") == "ok":
                    raw_tests = affected.get("affectedTests")
                    if isinstance(raw_tests, list):
                        nodes = _dicts_to_nodes(raw_tests)
                        self._emit_event(
                            "find_tests_for_file",
                            f"codegraph: {file_path} -> {len(nodes)} tests",
                        )
                        return nodes

            # Fallback: naming convention
            nodes, _ = self._ensure_fallback()
            # Derive test file candidates from source path
            src_path = Path(file_path)
            src_stem = src_path.stem
            src_parent = src_path.parent
            candidates: list[GraphNode] = []
            for n in nodes:
                if n.node_type != "test":
                    continue
                n_path = Path(n.file_path)
                # Match by stem: test_foo.py <-> foo.py, foo_test.py <-> foo.py
                n_stem = n_path.stem
                if n_stem in (
                    f"test_{src_stem}",
                    f"{src_stem}_test",
                    f"{src_stem}.test",
                    f"{src_stem}.spec",
                ):
                    candidates.append(n)
                # Match by directory: tests/ mirrors src/
                elif "test" in n_path.parts and src_stem in n_stem.replace("test_", "").replace("_test", ""):
                    candidates.append(n)
            self._emit_event(
                "find_tests_for_file",
                f"fallback: {file_path} -> {len(candidates)} tests",
            )
            return candidates

        return list(self._cached(cache_name, _query))

    def find_definition(self, symbol_name: str) -> GraphNode | None:
        """Find the definition node for a symbol.

        Returns the single best-match GraphNode or None.
        """
        cache_name = f"def:{symbol_name}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                result = self._run_json_single(
                    ["find", "--symbol", symbol_name, "--definition"],
                    timeout=30,
                )
                if result:
                    nodes = _dicts_to_nodes([result])
                    if nodes:
                        self._emit_event("find_definition", f"codegraph: {symbol_name}")
                        return nodes[0]

            # Fallback: search filesystem graph for definition-like match
            nodes, _ = self._ensure_fallback()
            lower_name = symbol_name.lower()
            # Prefer nodes that look like definitions (class, function, method)
            for n in nodes:
                if n.node_type in ("class", "function", "method", "module"):
                    if n.name.lower() == lower_name:
                        self._emit_event("find_definition", f"fallback: {symbol_name}")
                        return n
            # Broader match
            for n in nodes:
                if n.name.lower() == lower_name:
                    self._emit_event("find_definition", f"fallback(substring): {symbol_name}")
                    return n
            return None

        return self._cached(cache_name, _query)

    def find_references(self, symbol_name: str) -> list[GraphNode]:
        """Find all nodes that reference a given symbol."""
        cache_name = f"refs:{symbol_name}:{_cache_key(self._workspace)}"

        def _query():
            if self.is_available() and has_codegraph_index(self._workspace):
                raw = self._run_json(
                    ["find", "--symbol", symbol_name, "--references"],
                    timeout=45,
                )
                if raw:
                    self._emit_event(
                        "find_references",
                        f"codegraph: {symbol_name} -> {len(raw)} results",
                    )
                    return _dicts_to_nodes(raw)

            # Fallback: search import edges targeting this symbol
            nodes, edges = self._ensure_fallback()
            matching_node_ids: set[str] = set()
            lower_name = symbol_name.lower()
            for n in nodes:
                if n.name.lower() == lower_name:
                    matching_node_ids.add(n.id)
            results: list[GraphNode] = []
            for e in edges:
                if e.target_id in matching_node_ids:
                    src = self._fallback_node_by_id(e.source_id)
                    if src and src not in results:
                        results.append(src)
            self._emit_event(
                "find_references",
                f"fallback: {symbol_name} -> {len(results)} results",
            )
            return results

        return list(self._cached(cache_name, _query))

    # ------------------------------------------------------------------
    # Convenience: full context (wraps workspace.codegraph_context)
    # ------------------------------------------------------------------

    def context_for_task(self, task: str, auto_init: bool = True) -> dict[str, Any]:
        """Get semantic context for a task (delegates to codegraph_context)."""
        return codegraph_context(
            self._workspace, task, auto_init=auto_init,
        )

    def affected_tests(self, changed: list[dict[str, Any]]) -> dict[str, Any]:
        """Get affected tests for changed files (delegates to codegraph_affected_tests)."""
        return codegraph_affected_tests(self._workspace, changed)


# Edge type names for distinguishing edges from nodes in graph responses.
_EDGE_TYPE_NAMES = frozenset({
    "imports", "calls", "implements", "extends", "reads", "writes",
    "exposes", "registers", "publishes", "consumes", "tests", "configures",
})
