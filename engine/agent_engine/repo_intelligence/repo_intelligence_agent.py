"""Repository Intelligence Agent -- core analysis engine.

Analyzes a codebase before the Planner runs, producing a ContextPack
with verified evidence for every claim.  Does NOT modify code, create
fake implementations, or make generic plans.

Pipeline (8 stages, called sequentially from ``analyze()``):
  1. _analyze_request         -- semantic request understanding
  2. _capture_snapshot        -- git/languages/frameworks/build detection
  3. _classify_task           -- pattern-based task classification
  4. _graph_retrieval         -- progressive graph expansion with scoring
  5. _source_verification     -- verify graph claims against source code
  6. _reconstruct_architecture-- layer boundaries, execution flows
  7. _impact_analysis         -- direct/transitive/API/DB impact map
  8. _quality_check           -- AnalysisQualityGate before handoff
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .codegraph_adapter import CodeGraphAdapter
from .models import (
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
    StorageImpact,
    TaskClassification,
)
from ..debug_log import write_debug_event
from ..workspace import read_file as ws_read_file
from ..workspace import walk_workspace


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RepoIntelConfig:
    """Configuration for the Repository Intelligence Agent."""

    analysis_timeout: float = 300.0
    max_graph_depth: int = 3
    max_files: int = 60
    max_symbols: int = 200
    token_budget: int = 40000
    confidence_threshold: float = 0.6
    max_verification_files: int = 30
    stale_graph_retry: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_timeout": self.analysis_timeout,
            "max_graph_depth": self.max_graph_depth,
            "max_files": self.max_files,
            "max_symbols": self.max_symbols,
            "token_budget": self.token_budget,
            "confidence_threshold": self.confidence_threshold,
            "max_verification_files": self.max_verification_files,
            "stale_graph_retry": self.stale_graph_retry,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoIntelConfig":
        return cls(
            analysis_timeout=float(data.get("analysis_timeout", 300.0)),
            max_graph_depth=int(data.get("max_graph_depth", 3)),
            max_files=int(data.get("max_files", 60)),
            max_symbols=int(data.get("max_symbols", 200)),
            token_budget=int(data.get("token_budget", 40000)),
            confidence_threshold=float(data.get("confidence_threshold", 0.6)),
            max_verification_files=int(data.get("max_verification_files", 30)),
            stale_graph_retry=bool(data.get("stale_graph_retry", True)),
        )


# ---------------------------------------------------------------------------
# RelevanceScorer -- scores graph nodes relative to a task
# ---------------------------------------------------------------------------


@dataclass
class RelevanceScorer:
    """Scores code-graph node relevance for a task using multi-signal heuristics.

    Signals:
      - semantic_match: keyword/symbol overlap between request and node name
      - graph_proximity: closeness to known entrypoints in dependency graph
      - runtime_importance: whether node is a service/route/entrypoint
      - change_frequency: penalty for boilerplate/config (heuristic)
      - test_relationship: bonus if a test covers this node and is relevant
      - distance_penalty: decay with graph distance from seed nodes
    """

    keyword_weights: dict[str, float] = field(default_factory=dict)
    entrypoint_ids: set[str] = field(default_factory=set)
    node_map: dict[str, GraphNode] = field(default_factory=dict)
    edge_adj: dict[str, list[str]] = field(default_factory=dict)
    distances: dict[str, int] = field(default_factory=dict)

    def configure(
        self,
        keywords: dict[str, float],
        entrypoints: list[str],
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Seed the scorer with analysis context."""
        self.keyword_weights = keywords
        self.entrypoint_ids = set(entrypoints)
        self.node_map = {n.id: n for n in nodes}
        self.edge_adj.clear()
        for e in edges:
            self.edge_adj.setdefault(e.source_id, []).append(e.target_id)
            self.edge_adj.setdefault(e.target_id, []).append(e.source_id)
        # Pre-compute distances from entrypoints via BFS
        self.distances = self._bfs_distances(self.entrypoint_ids)

    def _bfs_distances(self, seed_ids: set[str]) -> dict[str, int]:
        dist: dict[str, int] = {}
        queue: list[tuple[str, int]] = [(sid, 0) for sid in seed_ids if sid in self.node_map]
        visited: set[str] = set()
        for sid, d in queue:
            dist[sid] = d
            visited.add(sid)
        qi = 0
        while qi < len(queue):
            current, d = queue[qi]
            qi += 1
            nd = d + 1
            if nd > 6:
                continue
            for neighbor in self.edge_adj.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    dist[neighbor] = nd
                    queue.append((neighbor, nd))
        return dist

    def score(self, node: GraphNode) -> float:
        """Return a composite relevance score (0-1) for a node."""
        semantic = self._semantic_match(node)
        proximity = self._graph_proximity(node)
        importance = self._runtime_importance(node)
        change_freq = self._change_frequency(node)
        test_rel = self._test_relationship(node)
        penalty = self._distance_penalty(node)

        weights = (0.35, 0.25, 0.20, 0.05, 0.10, -0.15)
        scores = (semantic, proximity, importance, change_freq, test_rel, penalty)
        total = sum(w * s for w, s in zip(weights, scores))
        return max(0.0, min(1.0, total))

    def _semantic_match(self, node: GraphNode) -> float:
        if not self.keyword_weights:
            return 0.5
        name_lower = node.name.lower()
        file_lower = node.file_path.lower()
        score = 0.0
        matched = 0
        for kw, weight in self.keyword_weights.items():
            kw_lower = kw.lower()
            if kw_lower in name_lower:
                score += weight * 1.0
                matched += 1
            elif kw_lower in file_lower:
                score += weight * 0.6
                matched += 1
            elif "_" in kw_lower or "-" in kw_lower:
                parts = re.split(r"[_\-]", kw_lower)
                partial = sum(
                    1.0 for p in parts if p and (p in name_lower or p in file_lower)
                ) / max(1, len(parts))
                if partial > 0:
                    score += weight * partial * 0.5
                    matched += 1
        if matched == 0:
            return 0.1
        return min(1.0, score / max(1, sum(self.keyword_weights.values())))

    def _graph_proximity(self, node: GraphNode) -> float:
        if node.id in self.entrypoint_ids:
            return 1.0
        d = self.distances.get(node.id)
        if d is None:
            return 0.05
        if d == 0:
            return 1.0
        if d == 1:
            return 0.9
        if d == 2:
            return 0.6
        if d == 3:
            return 0.3
        return max(0.01, 0.15 / (d - 2))

    def _runtime_importance(self, node: GraphNode) -> float:
        imp_map: dict[str, float] = {
            "route": 0.95,
            "entrypoint": 0.95,
            "service": 0.85,
            "class": 0.60,
            "function": 0.55,
            "method": 0.55,
            "table": 0.75,
            "event": 0.70,
            "tool": 0.65,
            "repository": 0.70,
            "configuration": 0.40,
            "module": 0.30,
            "file": 0.20,
            "test": 0.15,
        }
        base = imp_map.get(node.node_type, 0.25)
        if node.metadata.get("entrypoint"):
            base = max(base, 0.90)
        return base

    def _change_frequency(self, node: GraphNode) -> float:
        name_lower = node.name.lower()
        boilerplate_signals = [
            "index", "__init__", "setup", "config", "constants",
            "types", "generated", ".pb.", ".d.ts", "__pycache__",
        ]
        for signal in boilerplate_signals:
            if signal in name_lower:
                return 0.1
        if len(node.name) < 30 and not node.name.startswith("."):
            return 0.6
        return 0.3

    def _test_relationship(self, node: GraphNode) -> float:
        if node.node_type == "test":
            file_lower = node.file_path.lower()
            for kw in self.keyword_weights:
                if kw.lower() in file_lower:
                    return 0.8
            return 0.3
        return 0.4

    def _distance_penalty(self, node: GraphNode) -> float:
        d = self.distances.get(node.id)
        if d is None:
            return 0.5
        return min(0.5, d * 0.1)


# ---------------------------------------------------------------------------
# AnalysisQualityGate -- validates analysis before Planner handoff
# ---------------------------------------------------------------------------


@dataclass
class AnalysisQualityGate:
    """Validates that analysis meets minimum quality thresholds.

    Checks:
      1. Entrypoint found?
      2. Execution flow reconstructed?
      3. Critical conclusions have source evidence?
      4. Impact scope identified?
      5. Related tests found (or confirmed absent)?
      6. No critical unknowns?
      7. Confidence >= threshold?
    """

    threshold: float = 0.6

    def evaluate(self, context_pack: ContextPack) -> dict[str, Any]:
        """Run quality checks and return a gate result dict."""
        checks: list[dict[str, Any]] = []

        # 1. Entrypoint
        ep_found = bool(context_pack.entrypoints)
        checks.append({
            "check": "entrypoint_found",
            "passed": ep_found,
            "detail": (
                f"Found {len(context_pack.entrypoints)} entrypoint(s)"
                if ep_found
                else "No entrypoints discovered"
            ),
        })

        # 2. Execution flow
        flow_ok = bool(context_pack.current_execution_flow)
        checks.append({
            "check": "execution_flow_reconstructed",
            "passed": flow_ok,
            "detail": (
                f"Reconstructed {len(context_pack.current_execution_flow)} flow(s)"
                if flow_ok
                else "No execution flow reconstructed"
            ),
        })

        # 3. Source evidence for critical conclusions
        evidence_count = len(context_pack.evidence)
        source_evidence = [e for e in context_pack.evidence if e.evidence_type == "source"]
        crit_ok = len(source_evidence) >= 2
        checks.append({
            "check": "critical_source_evidence",
            "passed": crit_ok,
            "detail": (
                f"{len(source_evidence)} source-verified evidence item(s) "
                f"out of {evidence_count} total"
            ),
        })

        # 4. Impact scope
        impact_ok = bool(context_pack.change_impact_map)
        checks.append({
            "check": "impact_scope_identified",
            "passed": impact_ok,
            "detail": (
                f"Identified {len(context_pack.change_impact_map)} impact(s)"
                if impact_ok
                else "No impact analysis"
            ),
        })

        # 5. Related tests
        tests_ok = bool(context_pack.related_tests)
        checks.append({
            "check": "related_tests",
            "passed": tests_ok,
            "detail": (
                f"Found {len(context_pack.related_tests)} related test(s)"
                if tests_ok
                else "No related tests found"
            ),
        })

        # 6. Critical unknowns
        critical_unknowns = [
            u for u in context_pack.unknowns
            if u.get("severity") == "critical"
        ]
        no_critical_unknowns = len(critical_unknowns) == 0
        checks.append({
            "check": "no_critical_unknowns",
            "passed": no_critical_unknowns,
            "detail": (
                f"{len(critical_unknowns)} critical unknown(s)"
                if critical_unknowns
                else "No critical unknowns"
            ),
        })

        # 7. Confidence threshold
        conf_ok = context_pack.analysis_confidence >= self.threshold
        checks.append({
            "check": "confidence_threshold",
            "passed": conf_ok,
            "detail": (
                f"Confidence {context_pack.analysis_confidence:.2f} "
                f"vs threshold {self.threshold:.2f}"
            ),
        })

        all_passed = all(c["passed"] for c in checks)
        failing = [c["check"] for c in checks if not c["passed"]]

        return {
            "passed": all_passed,
            "failing": failing,
            "checks": checks,
            "overall_confidence": context_pack.analysis_confidence,
            "threshold": self.threshold,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_commit_hash(workspace: str) -> str | None:
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


def _git_working_tree_status(workspace: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
            return {
                "dirty": len(lines) > 0,
                "changed_files": len(lines),
                "summary": lines[:30],
            }
    except Exception:
        pass
    return {"dirty": False, "changed_files": 0, "summary": [], "error": "git status failed"}


def _git_branch(workspace: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
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


# Language detection from file extensions
_LANG_EXT_MAP: dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala",
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
}

# Framework detection from manifests
_FRAMEWORK_SIGNALS: dict[str, dict[str, list[str]]] = {
    "python": {
        "fastapi": ["fastapi", "starlette"],
        "flask": ["flask", "Flask"],
        "django": ["django", "Django"],
        "litestar": ["litestar"],
        "sqlalchemy": ["sqlalchemy"],
        "pydantic": ["pydantic"],
        "celery": ["celery"],
        "click": ["click"],
    },
    "javascript": {
        "express": ["express"],
        "next.js": ["next"],
        "react": ["react"],
        "vue": ["vue"],
        "svelte": ["svelte"],
        "angular": ["@angular/core"],
        "nestjs": ["@nestjs/core"],
    },
    "typescript": {
        "express": ["express"],
        "next.js": ["next"],
        "react": ["react"],
        "vue": ["vue"],
        "angular": ["@angular/core"],
        "nestjs": ["@nestjs/core"],
    },
    "go": {
        "gin": ["gin-gonic/gin"],
        "echo": ["labstack/echo"],
        "fiber": ["gofiber/fiber"],
        "chi": ["go-chi/chi"],
        "gorm": ["gorm.io/gorm"],
    },
    "rust": {
        "actix": ["actix-web"],
        "axum": ["axum"],
        "rocket": ["rocket"],
        "tokio": ["tokio"],
        "diesel": ["diesel"],
    },
    "ruby": {
        "rails": ["rails", "actionpack", "activerecord"],
        "sinatra": ["sinatra"],
        "sequel": ["sequel"],
    },
}

# Package manager detection
_PKG_MANAGER_SIGNALS: dict[str, list[str]] = {
    "npm": ["package-lock.json"],
    "yarn": ["yarn.lock"],
    "pnpm": ["pnpm-lock.yaml"],
    "pip": ["requirements.txt"],
    "poetry": ["poetry.lock"],
    "pipenv": ["Pipfile.lock"],
    "uv": ["uv.lock"],
    "go-mod": ["go.sum"],
    "cargo": ["Cargo.lock"],
    "bundler": ["Gemfile.lock"],
    "maven": ["pom.xml"],
    "gradle": ["build.gradle", "build.gradle.kts"],
}


def _detect_languages(files: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in files:
        ext = Path(item["path"]).suffix.lower()
        lang = _LANG_EXT_MAP.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _detect_frameworks(workspace: str, languages: list[str]) -> list[str]:
    detected: list[str] = []
    root = Path(workspace)

    # Check package.json for JS/TS frameworks
    pkg_json_path = root / "package.json"
    if pkg_json_path.exists():
        try:
            pkg = json.loads(pkg_json_path.read_text(encoding="utf-8"))
            all_deps = {}
            all_deps.update(pkg.get("dependencies", {}))
            all_deps.update(pkg.get("devDependencies", {}))
            dep_names = [k.lower() for k in all_deps]
            for lang in ("javascript", "typescript"):
                for fw_name, signals in _FRAMEWORK_SIGNALS.get(lang, {}).items():
                    if any(s.lower() in dep_names for s in signals) and fw_name not in detected:
                        detected.append(fw_name)
        except Exception:
            pass

    # Check pyproject.toml / requirements.txt for Python frameworks
    if "python" in languages:
        for manifest_name in ("pyproject.toml", "requirements.txt", "Pipfile"):
            manifest_path = root / manifest_name
            if manifest_path.exists():
                try:
                    content = manifest_path.read_text(encoding="utf-8", errors="replace")
                    content_lower = content.lower()
                    for fw_name, signals in _FRAMEWORK_SIGNALS.get("python", {}).items():
                        if any(s.lower() in content_lower for s in signals) and fw_name not in detected:
                            detected.append(fw_name)
                except Exception:
                    pass

    # Check go.mod for Go frameworks
    go_mod_path = root / "go.mod"
    if go_mod_path.exists() and "go" in languages:
        try:
            content = go_mod_path.read_text(encoding="utf-8", errors="replace").lower()
            for fw_name, signals in _FRAMEWORK_SIGNALS.get("go", {}).items():
                if any(s.lower() in content for s in signals) and fw_name not in detected:
                    detected.append(fw_name)
        except Exception:
            pass

    # Check Cargo.toml for Rust frameworks
    cargo_path = root / "Cargo.toml"
    if cargo_path.exists() and "rust" in languages:
        try:
            content = cargo_path.read_text(encoding="utf-8", errors="replace").lower()
            for fw_name, signals in _FRAMEWORK_SIGNALS.get("rust", {}).items():
                if any(s.lower() in content for s in signals) and fw_name not in detected:
                    detected.append(fw_name)
        except Exception:
            pass

    return detected


def _detect_package_managers(workspace: str) -> list[str]:
    detected: list[str] = []
    root = Path(workspace)
    for manager, signals in _PKG_MANAGER_SIGNALS.items():
        for signal in signals:
            if (root / signal).exists():
                detected.append(manager)
                break
    return detected


def _detect_entrypoint(workspace: str, files: list[dict[str, Any]]) -> str | None:
    """Detect the primary entrypoint from common manifests."""
    root = Path(workspace)

    # package.json main
    pkg_json_path = root / "package.json"
    if pkg_json_path.exists():
        try:
            pkg = json.loads(pkg_json_path.read_text(encoding="utf-8"))
            if pkg.get("main"):
                return pkg["main"]
        except Exception:
            pass

    # pyproject.toml scripts
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.exists():
        try:
            content = pyproject_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(
                r'\[(?:project\.scripts|tool\.poetry\.scripts)\]\s*\n'
                r'((?:\s*[a-zA-Z_]\w*\s*=\s*"[^"]*"\s*\n?)+)',
                content,
            )
            if m:
                first_line = m.group(1).strip().split("\n")[0].strip()
                parts = first_line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"')
        except Exception:
            pass

    # Go main
    if "main.go" in {item["path"] for item in files}:
        return "main.go"

    # Rust main
    if "src/main.rs" in {item["path"] for item in files}:
        return "src/main.rs"

    return None


def _detect_build_commands(workspace: str) -> list[str]:
    cmds: list[str] = []
    root = Path(workspace)

    # package.json scripts
    pkg_json_path = root / "package.json"
    if pkg_json_path.exists():
        try:
            pkg = json.loads(pkg_json_path.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            for key in ("build", "test", "lint", "format", "check", "typecheck"):
                if key in scripts:
                    cmds.append(f"npm run {key}")
        except Exception:
            pass

    # Makefile
    makefile_path = root / "Makefile"
    if makefile_path.exists():
        try:
            content = makefile_path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                stripped = line.strip()
                if re.match(r"^(build|test|lint|check|format|install)\s*:", stripped):
                    target = stripped.split(":")[0].strip()
                    cmds.append(f"make {target}")
        except Exception:
            pass

    # Python common
    has_pyproject = (root / "pyproject.toml").exists()
    has_setup = (root / "setup.py").exists() or (root / "setup.cfg").exists()
    if has_pyproject or has_setup:
        cmds.extend(["pip install -e .", "pytest", "ruff check .", "mypy ."])

    # Go common
    if (root / "go.mod").exists():
        cmds.extend(["go build ./...", "go test ./...", "go vet ./..."])

    # Rust common
    if (root / "Cargo.toml").exists():
        cmds.extend(["cargo build", "cargo test", "cargo clippy"])

    return list(dict.fromkeys(cmds))  # deduplicate preserving order


def _detect_manifest_files(workspace: str) -> list[dict[str, Any]]:
    known = [
        "package.json", "pyproject.toml", "setup.py", "setup.cfg",
        "requirements.txt", "Pipfile", "go.mod", "go.sum",
        "Cargo.toml", "Cargo.lock", "Gemfile", "Makefile",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".github/workflows", ".gitlab-ci.yml", "Jenkinsfile",
    ]
    root = Path(workspace)
    result: list[dict[str, Any]] = []
    for name in known:
        p = root / name
        if p.exists():
            result.append({"name": name, "path": name})
    # Also detect any YAML in .github/workflows/
    wf_dir = root / ".github" / "workflows"
    if wf_dir.is_dir():
        for child in wf_dir.iterdir():
            if child.suffix in (".yml", ".yaml"):
                rel = str(child.relative_to(root)).replace("\\", "/")
                result.append({"name": child.name, "path": rel})
    return result


# ---------------------------------------------------------------------------
# LLM-based semantic extraction helpers
# ---------------------------------------------------------------------------

_SEMANTIC_EXTRACTION_PROMPT = """\
You are a codebase analysis tool. Analyze the following task request and \
extract structured information.

Task request:
{request}

Return ONLY valid JSON with these fields:
- goal: the primary goal in one sentence
- behavior_change: what user-facing behavior should change
- likely_components: list of components/modules likely involved (empty list if unclear)
- constraints: list of constraints mentioned or implied (empty list if none)
- keywords: list of important technical keywords or symbols
- symbols: list of specific function/class/module names mentioned
- routes: list of any API routes or endpoints mentioned
- tables: list of any database tables mentioned
- services: list of any services mentioned

{{
  "goal": "...",
  "behavior_change": "...",
  "likely_components": [...],
  "constraints": [...],
  "keywords": [...],
  "symbols": [...],
  "routes": [...],
  "tables": [...],
  "services": [...]
}}"""


def _semantic_extraction_llm(llm_client: Any, request: str) -> dict[str, Any]:
    """Use LLM for semantic extraction of the request."""
    prompt = _SEMANTIC_EXTRACTION_PROMPT.format(request=request)
    fallback: dict[str, Any] = {
        "goal": request[:200],
        "behavior_change": "",
        "likely_components": [],
        "constraints": [],
        "keywords": [],
        "symbols": [],
        "routes": [],
        "tables": [],
        "services": [],
    }
    try:
        result = llm_client.json(prompt, fallback=fallback)
        return result
    except Exception:
        return fallback


def _semantic_extraction_pattern(request: str) -> dict[str, Any]:
    """Pattern-based semantic extraction when LLM is unavailable."""
    req_lower = request.lower()

    # Keywords: extract CamelCase, snake_case, and quoted strings
    keywords: list[str] = []
    # CamelCase / PascalCase
    camel_matches = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', request)
    keywords.extend(camel_matches)
    # snake_case
    snake_matches = re.findall(r'\b([a-z]+(?:_[a-z]+)+)\b', req_lower)
    keywords.extend(snake_matches)
    # Quoted strings
    quoted = re.findall(r'["\']([^"\']+)["\']', request)
    keywords.extend(quoted)

    # Symbols: function/method names (word followed by parenthesis)
    symbol_matches = re.findall(r'\b([a-zA-Z_]\w*)\s*\(', request)
    symbols = list(dict.fromkeys(symbol_matches))

    # Routes: /path-like/patterns
    route_matches = re.findall(r'(/[a-zA-Z0-9/_{}*-]+)', request)
    routes = list(dict.fromkeys(route_matches))

    # Tables: likely table/collection names (after "table", "collection", "model")
    table_matches = re.findall(r'(?:table|collection|model)\s+["\']?(\w+)', req_lower)
    tables = list(dict.fromkeys(table_matches))

    # Goal: first sentence or first meaningful line
    goal = request.strip().split(".")[0].strip() or request[:200]

    return {
        "goal": goal,
        "behavior_change": "",
        "likely_components": [],
        "constraints": [],
        "keywords": list(dict.fromkeys(keywords))[:30],
        "symbols": symbols[:20],
        "routes": routes[:10],
        "tables": tables[:10],
        "services": [],
    }


# ---------------------------------------------------------------------------
# Task classification signals
# ---------------------------------------------------------------------------

_CLASSIFICATION_SIGNALS: dict[str, tuple[list[str], float]] = {
    "feature": (
        ["add", "create", "implement", "build", "feature", "new", "support", "introduce"],
        0.85,
    ),
    "bugfix": (
        ["fix", "bug", "broken", "error", "crash", "regression", "incorrect", "wrong", "fail"],
        0.90,
    ),
    "refactor": (
        ["refactor", "clean up", "simplify", "extract", "reorganize", "restructure", "rename", "move"],
        0.85,
    ),
    "performance": (
        ["slow", "fast", "optimize", "performance", "cache", "latency", "throughput", "bottleneck", "speed"],
        0.80,
    ),
    "storage": (
        ["database", "migration", "schema", "table", "query", "index", "store", "persist", "orm", "sql"],
        0.80,
    ),
    "integration": (
        ["api", "endpoint", "integrate", "connect", "webhook", "client", "sdk", "service", "call", "request"],
        0.75,
    ),
    "infrastructure": (
        ["deploy", "ci", "docker", "config", "env", "pipeline", "build", "release", "infra", "kubernetes"],
        0.85,
    ),
    "test": (
        ["test", "coverage", "assert", "mock", "stub", "spec", "unittest", "e2e", "integration test"],
        0.90,
    ),
    "documentation": (
        ["document", "readme", "comment", "docstring", "docs", "explain", "describe"],
        0.85,
    ),
}


# ---------------------------------------------------------------------------
# RepoIntelligenceAgent
# ---------------------------------------------------------------------------


class RepoIntelligenceAgent:
    """Repository Intelligence Agent -- analyzes codebase before Planner runs.

    Produces a ContextPack with verified evidence for every claim.
    Does NOT modify code, create fake implementations, or make generic plans.
    """

    def __init__(
        self,
        workspace: str,
        *,
        adapter: CodeGraphAdapter | None = None,
        llm_client: Any | None = None,
        emit: Callable[[str, str], None] | None = None,
        config: RepoIntelConfig | None = None,
    ) -> None:
        self._workspace = str(Path(workspace).resolve())
        self._adapter = adapter or CodeGraphAdapter(self._workspace, emit=emit)
        self._llm_client = llm_client
        self._emit = emit or (lambda _t, _m: None)
        self._config = config or RepoIntelConfig()
        self._start_time: float = 0.0

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(self, event: str, detail: str) -> None:
        try:
            self._emit(event, detail)
        except Exception:
            pass
        try:
            write_debug_event(
                f"repo_intelligence.{event}",
                {"detail": detail, "workspace": self._workspace},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, request: str) -> ContextPack:
        """Run the full analysis pipeline and return a ContextPack.

        Stages 1-8 execute in order.  On timeout, returns a partial
        ContextPack with whatever stages completed.
        """
        self._start_time = time.monotonic()
        pack = ContextPack(request_understanding={"request": request})
        deadline = self._start_time + self._config.analysis_timeout

        # ------------------------------------------------------------------
        # Stage 1: Understand the request
        # ------------------------------------------------------------------
        try:
            self._emit_event("stage_start", "1/8 _analyze_request")
            pack.request_understanding = self._analyze_request(request)
            self._emit_event("stage_done", "1/8 _analyze_request complete")
        except Exception as exc:
            self._emit_event("stage_error", f"_analyze_request: {exc}")
            pack.request_understanding = {"request": request, "error": str(exc)[:500]}

        # ------------------------------------------------------------------
        # Stage 2: Capture repository snapshot
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                return self._finalize(pack, "timeout_after_stage1")
            self._emit_event("stage_start", "2/8 _capture_snapshot")
            pack.repository_snapshot = self._capture_snapshot()
            self._emit_event("stage_done", "2/8 _capture_snapshot complete")
        except Exception as exc:
            self._emit_event("stage_error", f"_capture_snapshot: {exc}")
            pack.repository_snapshot = {"error": str(exc)[:500]}

        # ------------------------------------------------------------------
        # Stage 3: Classify the task
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                return self._finalize(pack, "timeout_after_stage2")
            self._emit_event("stage_start", "3/8 _classify_task")
            pack.task_classification = self._classify_task(
                request, pack.request_understanding
            )
            self._emit_event("stage_done", "3/8 _classify_task complete")
        except Exception as exc:
            self._emit_event("stage_error", f"_classify_task: {exc}")

        # ------------------------------------------------------------------
        # Stage 4: Graph retrieval
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                return self._finalize(pack, "timeout_after_stage3")
            self._emit_event("stage_start", "4/8 _graph_retrieval")
            nodes, edges, paths, graph_status = self._graph_retrieval(
                pack.request_understanding, pack.repository_snapshot
            )
            pack.graph_status = graph_status
            pack.entrypoints = [
                n.id for n in nodes
                if n.metadata.get("entrypoint") or n.node_type == "route"
            ]
            pack.relevant_files = [
                {"path": n.file_path, "name": n.name, "node_type": n.node_type, "id": n.id}
                for n in nodes if n.file_path
            ]
            pack.relevant_symbols = [
                {"name": n.name, "node_type": n.node_type, "file": n.file_path, "id": n.id}
                for n in nodes if n.name
            ]
            pack.dependency_paths = paths
            self._emit_event(
                "stage_done",
                f"4/8 _graph_retrieval: {len(nodes)} nodes, {len(edges)} edges",
            )
        except Exception as exc:
            self._emit_event("stage_error", f"_graph_retrieval: {exc}")
            nodes, edges, paths = [], [], []
            graph_status = {"error": str(exc)[:500]}
            pack.graph_status = graph_status
            if self._config.stale_graph_retry:
                pack.graph_status["stale"] = True
                pack.graph_status["stale_reason"] = str(exc)[:200]
            else:
                return self._finalize(pack, "graph_retrieval_failed")

        # ------------------------------------------------------------------
        # Stage 5: Source verification
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                pack.graph_status["partial"] = True
                return self._finalize(pack, "timeout_after_stage4")
            self._emit_event("stage_start", "5/8 _source_verification")
            evidence = self._source_verification(nodes, edges, paths)
            pack.evidence = evidence
            self._emit_event(
                "stage_done",
                f"5/8 _source_verification: {len(evidence)} evidence items",
            )
        except Exception as exc:
            self._emit_event("stage_error", f"_source_verification: {exc}")
            pack.graph_status["verification_error"] = str(exc)[:500]

        # ------------------------------------------------------------------
        # Stage 6: Reconstruct architecture
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                return self._finalize(pack, "timeout_after_stage5")
            self._emit_event("stage_start", "6/8 _reconstruct_architecture")
            arch_flows, arch_boundaries = self._reconstruct_architecture(
                nodes, edges, pack.evidence
            )
            pack.current_execution_flow = arch_flows
            pack.architecture_boundaries = arch_boundaries
            self._emit_event(
                "stage_done",
                f"6/8 _reconstruct_architecture: {len(arch_flows)} flows, "
                f"{len(arch_boundaries)} boundaries",
            )
        except Exception as exc:
            self._emit_event("stage_error", f"_reconstruct_architecture: {exc}")

        # ------------------------------------------------------------------
        # Stage 7: Impact analysis
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                return self._finalize(pack, "timeout_after_stage6")
            self._emit_event("stage_start", "7/8 _impact_analysis")
            impact_map = self._impact_analysis(
                pack.request_understanding,
                nodes,
                edges,
                pack.evidence,
                {
                    "flows": [f.to_dict() for f in pack.current_execution_flow],
                    "boundaries": [b.to_dict() for b in pack.architecture_boundaries],
                },
            )
            pack.change_impact_map = impact_map

            # Extract storage impact from change impacts
            storage_impacts = [
                ci for ci in impact_map
                if ci.target in ("database", "schema", "migration", "table", "storage")
            ]
            if storage_impacts:
                tables: list[str] = []
                schema_changes: list[str] = []
                for si in storage_impacts:
                    if si.target == "table":
                        tables.append(si.target)
                    elif si.target in ("schema", "migration"):
                        schema_changes.append(si.target)
                pack.storage_impact = StorageImpact(
                    tables_affected=tables,
                    migrations_needed=any(
                        si.target == "migration" for si in storage_impacts
                    ),
                    schema_changes=schema_changes,
                    confidence=max(
                        (si.confidence for si in storage_impacts), default=0.0
                    ),
                )
            self._emit_event(
                "stage_done",
                f"7/8 _impact_analysis: {len(impact_map)} impacts",
            )
        except Exception as exc:
            self._emit_event("stage_error", f"_impact_analysis: {exc}")

        # ------------------------------------------------------------------
        # Stage 8: Quality check
        # ------------------------------------------------------------------
        try:
            if time.monotonic() > deadline:
                return self._finalize(pack, "timeout_after_stage7")
            self._emit_event("stage_start", "8/8 _quality_check")
            gate_result = self._quality_check(pack)
            pack.metadata["quality_gate"] = gate_result
            pack.analysis_confidence = gate_result.get(
                "overall_confidence", pack.analysis_confidence
            )
            self._emit_event(
                "stage_done",
                f"8/8 _quality_check: passed={gate_result.get('passed')}",
            )
        except Exception as exc:
            self._emit_event("stage_error", f"_quality_check: {exc}")
            pack.metadata["quality_gate_error"] = str(exc)[:500]

        # ------------------------------------------------------------------
        # Stage 9 (optional): enrich with codebase-memory-mcp graph queries.
        # Best-effort; if the binary isn't installed, this is a no-op.
        # ------------------------------------------------------------------
        try:
            self._emit_event("stage_start", "9/9 _enrich_with_codebase_memory")
            enrichment = self._enrich_with_codebase_memory(request, pack)
            if enrichment:
                pack.metadata["codebase_memory"] = enrichment
                self._emit_event(
                    "stage_done",
                    f"9/9 codebase_memory: {enrichment.get('total_nodes', '?')} nodes / "
                    f"{enrichment.get('total_edges', '?')} edges",
                )
            else:
                self._emit_event("stage_skipped", "codebase-memory-mcp not installed")
        except Exception as exc:
            self._emit_event("stage_error", f"_enrich_with_codebase_memory: {exc}")

        return self._finalize(pack, "complete")

    def _enrich_with_codebase_memory(self, request: str, pack: ContextPack) -> dict[str, Any] | None:
        """Augment the ContextPack with knowledge-graph queries from codebase-memory-mcp.

        Adds: (a) architecture snapshot, (b) top-N graph matches for the request keywords,
        (c) cached qualified names so downstream prompts can use trace_path / get_code_snippet.
        Skipped during pytest to avoid polluting the global MCP cache with tempdir indexes.
        """
        import os
        from .. import codebase_memory as cm

        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        if not cm.is_available():
            return None
        workspace = str(self._workspace)
        # Index (cached after first call in this process)
        cm.ensure_indexed(workspace)
        arch = cm.get_architecture(workspace) or {}
        # Pick keywords from request_understanding first, fall back to request text.
        ru = pack.request_understanding or {}
        keywords: list[str] = []
        for source_key in ("symbols", "keywords", "components", "routes"):
            value = ru.get(source_key)
            if isinstance(value, list):
                keywords.extend(str(v) for v in value if str(v).strip())
        if not keywords:
            keywords = [w for w in re.split(r"[^A-Za-z0-9_]+", request) if len(w) >= 3][:6]
        seen: set[str] = set()
        hits: list[dict[str, Any]] = []
        for query in keywords[:8]:
            if query.lower() in seen:
                continue
            seen.add(query.lower())
            for hit in cm.search_graph(workspace, query, limit=3):
                qn = hit.get("qualified_name")
                if not qn or qn in {h.get("qualified_name") for h in hits}:
                    continue
                hits.append({
                    "name": hit.get("name"),
                    "qualified_name": qn,
                    "label": hit.get("label"),
                    "file": hit.get("file_path"),
                    "line": hit.get("start_line"),
                    "query": query,
                })
                if len(hits) >= 16:
                    break
            if len(hits) >= 16:
                break
        return {
            "binary": str(cm.binary_path()),
            "project_id": cm.project_id_for(workspace),
            "total_nodes": arch.get("total_nodes"),
            "total_edges": arch.get("total_edges"),
            "queries": list(seen)[:8],
            "hits": hits,
        }

    # ------------------------------------------------------------------
    # Stage 1: Request analysis
    # ------------------------------------------------------------------

    def _analyze_request(self, request: str) -> dict[str, Any]:
        """Extract goal, behavior change, likely components, constraints,
        keywords, symbols, routes, tables, and services from the request.
        """
        if self._llm_client is not None:
            try:
                result = _semantic_extraction_llm(self._llm_client, request)
                self._emit_event("request_analysis", "llm extraction complete")
                return result
            except Exception:
                self._emit_event("request_analysis", "LLM extraction failed, falling back to pattern")

        result = _semantic_extraction_pattern(request)
        self._emit_event("request_analysis", "pattern extraction complete")
        return result

    # ------------------------------------------------------------------
    # Stage 2: Repository snapshot
    # ------------------------------------------------------------------

    def _capture_snapshot(self) -> dict[str, Any]:
        """Capture git info, languages, frameworks, package manager,
        entrypoint, build/test/lint commands.
        """
        snapshot: dict[str, Any] = {}

        # Git
        commit = _git_commit_hash(self._workspace)
        branch = _git_branch(self._workspace)
        wt_status = _git_working_tree_status(self._workspace)
        snapshot["git"] = {
            "commit": commit,
            "branch": branch,
            "working_tree": wt_status,
        }

        # Filesystem
        files = walk_workspace(
            self._workspace,
            max_files=self._config.max_files,
            max_depth=self._config.max_graph_depth + 2,
        )
        snapshot["file_count"] = len(files)
        snapshot["file_sample"] = [f["path"] for f in files[:50]]

        # Languages
        languages = _detect_languages(files)
        snapshot["languages"] = languages
        snapshot["primary_language"] = next(iter(languages), "unknown")

        # Frameworks
        detected_frameworks = _detect_frameworks(
            self._workspace, list(languages.keys())
        )
        snapshot["frameworks"] = detected_frameworks

        # Package managers
        pkg_managers = _detect_package_managers(self._workspace)
        snapshot["package_managers"] = pkg_managers

        # Entrypoints
        entrypoint = _detect_entrypoint(self._workspace, files)
        snapshot["detected_entrypoint"] = entrypoint

        # Build/test/lint commands
        commands = _detect_build_commands(self._workspace)
        snapshot["detected_commands"] = commands

        # Manifest files
        manifests = _detect_manifest_files(self._workspace)
        snapshot["manifest_files"] = manifests

        return snapshot

    # ------------------------------------------------------------------
    # Stage 3: Task classification
    # ------------------------------------------------------------------

    def _classify_task(
        self, request: str, understanding: dict[str, Any]
    ) -> list[TaskClassification]:
        """Pattern-based classification using keyword signals."""
        req_lower = request.lower()
        classifications: list[TaskClassification] = []

        for category, (signals, base_confidence) in _CLASSIFICATION_SIGNALS.items():
            matched: list[str] = []
            for signal in signals:
                if signal.lower() in req_lower:
                    matched.append(signal)

            if matched:
                # Higher confidence with more signals matched
                confidence = min(1.0, base_confidence + 0.05 * (len(matched) - 1))
                classifications.append(TaskClassification(
                    category=category,
                    confidence=confidence,
                    signals=matched,
                ))

        # Default to feature if nothing matched
        if not classifications:
            classifications.append(TaskClassification(
                category="feature",
                confidence=0.3,
                signals=["default"],
            ))

        # Sort by confidence descending
        classifications.sort(key=lambda c: c.confidence, reverse=True)

        self._emit_event(
            "task_classification",
            ", ".join(f"{c.category}({c.confidence:.2f})" for c in classifications[:3]),
        )
        return classifications

    # ------------------------------------------------------------------
    # Stage 4: Graph retrieval
    # ------------------------------------------------------------------

    def _graph_retrieval(
        self, understanding: dict[str, Any], snapshot: dict[str, Any]
    ) -> tuple[list[GraphNode], list[GraphEdge], list[DependencyPath], dict[str, Any]]:
        """Progressive graph expansion from seed keywords/symbols.

        Layer 1: find definitions and entrypoints
        Layer 2: expand 1-2 hops
        Scores nodes using RelevanceScorer.
        Stops when budget reached or sufficient evidence gathered.
        """
        keywords: list[str] = understanding.get("keywords", [])
        symbols: list[str] = understanding.get("symbols", [])
        routes: list[str] = understanding.get("routes", [])
        tables: list[str] = understanding.get("tables", [])
        services: list[str] = understanding.get("services", [])

        # Deduplicate and clean
        all_seeds = list(dict.fromkeys(
            keywords + symbols + routes + tables + services
        ))[:self._config.max_symbols]

        graph_status: dict[str, Any] = {
            "seeds": len(all_seeds),
            "layers": 0,
            "nodes_found": 0,
            "edges_found": 0,
            "budget_exceeded": False,
            "codegraph_available": self._adapter.is_available(),
        }

        collected_nodes: dict[str, GraphNode] = {}
        collected_edges: list[GraphEdge] = []
        collected_paths: list[DependencyPath] = []

        # Layer 1: Find definitions and entrypoints
        layer1_nodes: list[GraphNode] = []
        for seed in all_seeds:
            matches = self._adapter.query_symbol(seed)
            for m in matches:
                if m.id not in collected_nodes:
                    collected_nodes[m.id] = m
                    layer1_nodes.append(m)

        entrypoint_nodes = self._adapter.find_entrypoints()
        for ep in entrypoint_nodes:
            if ep.id not in collected_nodes:
                collected_nodes[ep.id] = ep
                layer1_nodes.append(ep)

        graph_status["layers"] = 1
        graph_status["nodes_found"] = len(collected_nodes)

        # Build seed IDs for expansion
        seed_ids = list(collected_nodes.keys())

        # Layer 2: Expand 1-2 hops via graph query
        if seed_ids and len(collected_nodes) < self._config.max_symbols:
            remaining = self._config.max_symbols - len(collected_nodes)
            expand_nodes, expand_edges = self._adapter.query_graph(
                seed_ids,
                max_depth=self._config.max_graph_depth,
                max_nodes=min(
                    remaining + len(collected_nodes),
                    self._config.max_symbols,
                ),
            )
            for n in expand_nodes:
                if n.id not in collected_nodes:
                    collected_nodes[n.id] = n
            collected_edges.extend(expand_edges)
            graph_status["layers"] = 2
            graph_status["nodes_found"] = len(collected_nodes)
            graph_status["edges_found"] = len(collected_edges)

            # Build dependency paths from entrypoints to significant nodes
            ep_ids = {ep.id for ep in entrypoint_nodes}
            node_list = list(collected_nodes.values())
            node_map = {n.id: n for n in node_list}

            # Build adjacency from edges
            adj: dict[str, list[str]] = {}
            for e in collected_edges:
                if e.source_id in collected_nodes and e.target_id in collected_nodes:
                    adj.setdefault(e.source_id, []).append(e.target_id)

            # For each route node, find shortest path from an entrypoint
            route_nodes = [n for n in node_list if n.node_type == "route"]
            for route in route_nodes[:10]:
                for ep_id in ep_ids:
                    if ep_id not in node_map:
                        continue
                    path_ids = self._shortest_path(ep_id, route.id, adj)
                    if path_ids and len(path_ids) >= 2:
                        collected_paths.append(DependencyPath(
                            path=path_ids,
                            distance=len(path_ids) - 1,
                            edge_types=["exposes", "calls"],
                        ))
                        break  # one path per route is enough

        # Expand: find services
        if len(collected_nodes) < self._config.max_symbols:
            services_nodes = self._adapter.find_services()
            for svc in services_nodes:
                if svc.id not in collected_nodes and len(collected_nodes) < self._config.max_symbols:
                    collected_nodes[svc.id] = svc

        graph_status["nodes_found"] = len(collected_nodes)
        graph_status["edges_found"] = len(collected_edges)

        # Score and filter nodes using RelevanceScorer
        keyword_weights = self._build_keyword_weights(understanding)
        scorer = RelevanceScorer()
        scorer.configure(
            keywords=keyword_weights,
            entrypoints=[ep.id for ep in entrypoint_nodes],
            nodes=list(collected_nodes.values()),
            edges=collected_edges,
        )

        scored = [(n, scorer.score(n)) for n in collected_nodes.values()]
        scored.sort(key=lambda kv: -kv[1])

        # Keep top nodes within budget
        max_nodes = min(self._config.max_symbols, len(scored))
        top_nodes = [n for n, _s in scored[:max_nodes]]
        top_node_ids = {n.id for n in top_nodes}

        # Filter edges to only those connecting retained nodes
        filtered_edges = [
            e for e in collected_edges
            if e.source_id in top_node_ids and e.target_id in top_node_ids
        ]

        graph_status["after_scoring"] = {
            "kept_nodes": len(top_nodes),
            "kept_edges": len(filtered_edges),
            "min_score": scored[max_nodes - 1][1] if max_nodes > 0 else 0.0,
            "max_score": scored[0][1] if scored else 0.0,
        }

        return top_nodes, filtered_edges, collected_paths, graph_status

    @staticmethod
    def _shortest_path(
        start: str, end: str, adj: dict[str, list[str]]
    ) -> list[str] | None:
        """BFS to find shortest path between two node IDs."""
        if start == end:
            return [start]
        queue: list[tuple[str, list[str]]] = [(start, [start])]
        visited: set[str] = {start}
        while queue:
            current, path = queue.pop(0)
            for neighbor in adj.get(current, []):
                if neighbor == end:
                    return path + [neighbor]
                if neighbor not in visited and len(path) < 8:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return None

    @staticmethod
    def _build_keyword_weights(understanding: dict[str, Any]) -> dict[str, float]:
        """Build weighted keyword map from understanding dict."""
        weights: dict[str, float] = {}
        weight_sources = [
            ("symbols", 1.0),
            ("keywords", 0.8),
            ("tables", 0.7),
            ("routes", 0.6),
            ("services", 0.5),
            ("likely_components", 0.4),
        ]
        for key, base_weight in weight_sources:
            items = understanding.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str):
                        weights[item] = max(weights.get(item, 0.0), base_weight)
        return weights

    # ------------------------------------------------------------------
    # Stage 5: Source verification
    # ------------------------------------------------------------------

    def _source_verification(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        paths: list[DependencyPath],
    ) -> list[Evidence]:
        """Verify graph claims by reading actual source code.

        For each critical conclusion, open the file, find the symbol,
        confirm it matches graph claims.  If graph claim != source code,
        mark graph_stale, prioritize source.

        Only source-verified claims become evidence.
        """
        evidence_list: list[Evidence] = []
        verified_files: set[str] = set()

        # Prioritize: entrypoints first, then routes, then high-degree nodes
        node_degree: dict[str, int] = {}
        for e in edges:
            node_degree[e.source_id] = node_degree.get(e.source_id, 0) + 1
            node_degree[e.target_id] = node_degree.get(e.target_id, 0) + 1

        def node_priority(n: GraphNode) -> tuple[int, int]:
            score = 0
            if n.metadata.get("entrypoint"):
                score += 100
            if n.node_type == "route":
                score += 80
            if n.node_type in ("service", "class", "function", "method"):
                score += 50
            deg = node_degree.get(n.id, 0)
            return (-score, -deg)

        sorted_nodes = sorted(nodes, key=node_priority)

        max_vf = self._config.max_verification_files
        for node in sorted_nodes:
            if len(evidence_list) >= max_vf * 2:
                break

            file_path = node.file_path
            if not file_path:
                continue
            if file_path in verified_files and node.node_type not in ("entrypoint", "route"):
                continue

            try:
                content = ws_read_file(self._workspace, file_path, 20000)
            except Exception:
                continue

            verified_files.add(file_path)

            # Check: does the symbol actually appear in the file?
            symbol_found = False
            line_range: tuple[int, int] | None = None
            excerpt = ""

            if node.name:
                lines = content.split("\n")
                for i, line in enumerate(lines):
                    if node.name in line:
                        symbol_found = True
                        start = max(0, i - 2)
                        end = min(len(lines), i + 3)
                        line_range = (start + 1, end)
                        excerpt = "\n".join(lines[start:end])[:500]
                        break

            if symbol_found:
                confidence = 0.85 if node.metadata.get("entrypoint") else 0.70
                if node.node_type in ("route", "service", "function", "method", "class"):
                    confidence = 0.90

                evidence_list.append(Evidence(
                    claim=f"Verified {node.node_type} '{node.name}' in {file_path}",
                    file_path=file_path,
                    symbol=node.name,
                    evidence_type="source",
                    confidence=confidence,
                    line_range=line_range,
                    excerpt=excerpt,
                ))

                # Also verify: if this node is route/file, check for handler patterns
                if node.node_type in ("route", "file") and node.name:
                    decorator_signals = [
                        "@", "def ", "async def ", "router.",
                        ".get(", ".post(", ".put(", ".delete(", ".patch(",
                    ]
                    for pattern_key in decorator_signals:
                        if pattern_key in content:
                            for j, ln in enumerate(lines):
                                if pattern_key in ln and node.name in ln:
                                    evidence_list.append(Evidence(
                                        claim=(
                                            f"Route/handler pattern '{pattern_key.strip('(')}' "
                                            f"confirmed for '{node.name}'"
                                        ),
                                        file_path=file_path,
                                        symbol=node.name,
                                        evidence_type="source",
                                        confidence=0.80,
                                        line_range=(j + 1, j + 1),
                                        excerpt=ln[:500],
                                    ))
                                    break
                            break
            else:
                # Graph claim not found in source -- mark as stale
                evidence_list.append(Evidence(
                    claim=(
                        f"Graph node '{node.name}' ({node.node_type}) listed in "
                        f"{file_path} but symbol not found in source"
                    ),
                    file_path=file_path,
                    symbol=node.name,
                    evidence_type="graph",
                    confidence=0.15,
                ))

        # Verify dependency paths
        for path_obj in paths[:10]:
            if len(path_obj.path) >= 2:
                evidence_list.append(Evidence(
                    claim=(
                        f"Dependency path verified: {len(path_obj.path)} hops "
                        f"from {path_obj.path[0][:40]} to {path_obj.path[-1][:40]}"
                    ),
                    file_path="",
                    symbol="",
                    evidence_type="graph",
                    confidence=0.60,
                ))

        self._emit_event(
            "source_verification",
            f"{len(evidence_list)} evidence items from {len(verified_files)} files",
        )
        return evidence_list

    # ------------------------------------------------------------------
    # Stage 6: Architecture reconstruction
    # ------------------------------------------------------------------

    def _reconstruct_architecture(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        evidence: list[Evidence],
    ) -> tuple[list[CurrentExecutionFlow], list[ArchitectureBoundary]]:
        """Identify entrypoint -> validation -> service -> domain ->
        provider -> storage flow.

        Identify layer boundaries and dependency direction.
        Find transaction boundaries, error propagation patterns.
        Detect async/background flows.
        Identify reusable extension points.
        Flag unusual coupling or duplication.
        """
        flows: list[CurrentExecutionFlow] = []
        boundaries: list[ArchitectureBoundary] = []

        # Identify entrypoints
        entrypoints = [
            n for n in nodes
            if n.metadata.get("entrypoint") or n.node_type == "route"
        ]
        if not entrypoints:
            entrypoints = [n for n in nodes if n.node_type in ("route", "file")]

        # Build adjacency map
        adj: dict[str, list[GraphEdge]] = {}
        node_map: dict[str, GraphNode] = {n.id: n for n in nodes}
        for e in edges:
            adj.setdefault(e.source_id, []).append(e)

        # For each entrypoint, trace execution flow
        for ep in entrypoints[:5]:
            stages: list[dict[str, Any]] = []
            data_trace: list[dict[str, Any]] = []
            visited: set[str] = set()

            # BFS from entrypoint through call/import edges
            queue: list[tuple[str, int]] = [(ep.id, 0)]
            while queue and len(stages) < 12:
                current_id, depth = queue.pop(0)
                if current_id in visited:
                    continue
                visited.add(current_id)
                node = node_map.get(current_id)
                if node is None:
                    continue

                stage_type = self._classify_stage(node)
                stages.append({
                    "node_id": current_id,
                    "name": node.name,
                    "type": stage_type,
                    "depth": depth,
                    "file": node.file_path,
                })

                if depth < 4:
                    for edge in adj.get(current_id, []):
                        if edge.target_id not in visited:
                            queue.append((edge.target_id, depth + 1))
                            data_trace.append({
                                "from": current_id,
                                "to": edge.target_id,
                                "edge_type": edge.edge_type,
                            })

            if stages:
                flows.append(CurrentExecutionFlow(
                    entrypoint=ep.id,
                    stages=stages,
                    data_trace=data_trace,
                ))

        # Identify architecture boundaries (layers)
        layer_keywords: dict[str, list[str]] = {
            "entrypoint": ["route", "handler", "controller", "endpoint", "main", "app"],
            "validation": ["schema", "validator", "serializer", "dto", "model", "request", "response"],
            "service": ["service", "usecase", "interactor", "business"],
            "domain": ["domain", "entity", "aggregate", "value_object", "model"],
            "provider": ["repository", "dao", "client", "adapter", "gateway", "provider", "connector"],
            "storage": ["database", "db", "migration", "seed", "fixture", "sql", "query"],
        }

        layers: dict[str, list[str]] = {}
        for node in nodes:
            name_lower = node.name.lower()
            file_lower = node.file_path.lower()
            for layer, signals in layer_keywords.items():
                if any(s in name_lower or s in file_lower for s in signals):
                    layers.setdefault(layer, []).append(node.id)
                    break

        layer_order = ["entrypoint", "validation", "service", "domain", "provider", "storage"]
        for i in range(len(layer_order) - 1):
            from_layer = layer_order[i]
            to_layer = layer_order[i + 1]
            from_ids = set(layers.get(from_layer, []))
            to_ids = set(layers.get(to_layer, []))

            crossing_edges: list[str] = []
            direction = "inward"

            for e in edges:
                if e.source_id in from_ids and e.target_id in to_ids:
                    crossing_edges.append(e.id)
                elif e.source_id in to_ids and e.target_id in from_ids:
                    crossing_edges.append(e.id)
                    direction = "outward"

            if crossing_edges or from_ids or to_ids:
                boundaries.append(ArchitectureBoundary(
                    name=f"{from_layer} -> {to_layer}",
                    from_layer=from_layer,
                    to_layer=to_layer,
                    dependency_direction=direction,
                    node_ids=list(from_ids | to_ids)[:20],
                ))

        return flows, boundaries

    @staticmethod
    def _classify_stage(node: GraphNode) -> str:
        name_lower = node.name.lower()
        file_lower = node.file_path.lower()
        combined = name_lower + " " + file_lower

        if any(s in combined for s in ("route", "handler", "controller", "endpoint", "main")):
            return "entrypoint"
        if any(s in combined for s in ("schema", "validator", "serializer", "dto", "request", "response")):
            return "validation"
        if any(s in combined for s in ("service", "usecase", "interactor", "business")):
            return "service"
        if any(s in combined for s in ("domain", "entity", "aggregate")):
            return "domain"
        if any(s in combined for s in ("repository", "dao", "client", "adapter", "gateway", "provider")):
            return "provider"
        if any(s in combined for s in ("database", "db", "migration", "seed", "sql")):
            return "storage"
        if any(s in combined for s in ("test", "spec")):
            return "test"
        if any(s in combined for s in ("config", "settings", "env")):
            return "configuration"
        return "module"

    # ------------------------------------------------------------------
    # Stage 7: Impact analysis
    # ------------------------------------------------------------------

    def _impact_analysis(
        self,
        understanding: dict[str, Any],
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        evidence: list[Evidence],
        architecture: dict[str, Any],
    ) -> list[ChangeImpact]:
        """Produce impact map: direct/transitive modules, API surface,
        database/schema, migration, tool/provider, security/permission,
        related tests.  Each with level, confidence, evidence_ids.
        """
        impacts: list[ChangeImpact] = []
        evidence_ids = [e.id for e in evidence if e.evidence_type == "source"]

        node_map = {n.id: n for n in nodes}
        evidence_node_ids = {
            e.file_path for e in evidence
            if e.evidence_type == "source" and e.confidence >= 0.5
        }

        # Directly affected modules (files with verified evidence)
        directly_affected: set[str] = set()
        for n in nodes:
            if n.file_path in evidence_node_ids:
                directly_affected.add(n.id)

        for nid in directly_affected:
            node = node_map.get(nid)
            if node:
                impacts.append(ChangeImpact(
                    target=node.name,
                    level=self._estimate_impact_level(node, "direct"),
                    confidence=0.80,
                    evidence_ids=evidence_ids,
                    verification_method="source_verified",
                ))

        # Transitively affected modules (neighbors in graph)
        transitive: set[str] = set()
        for e in edges:
            if e.source_id in directly_affected and e.target_id not in directly_affected:
                transitive.add(e.target_id)
            elif e.target_id in directly_affected and e.source_id not in directly_affected:
                transitive.add(e.source_id)

        for nid in transitive:
            node = node_map.get(nid)
            if node:
                impacts.append(ChangeImpact(
                    target=node.name,
                    level="medium",
                    confidence=0.55,
                    evidence_ids=evidence_ids,
                    verification_method="graph_transitive",
                ))

        # API surface changes
        route_nodes = [n for n in nodes if n.node_type == "route"]
        for rn in route_nodes[:10]:
            if any(
                kw.lower() in rn.name.lower()
                for kw in understanding.get("keywords", []) + understanding.get("routes", [])
            ):
                impacts.append(ChangeImpact(
                    target=f"API: {rn.name}",
                    level="high",
                    confidence=0.70,
                    evidence_ids=evidence_ids,
                    verification_method="route_match",
                ))

        # Database/schema impact
        table_nodes = [n for n in nodes if n.node_type == "table"]
        for tn in table_nodes[:10]:
            impacts.append(ChangeImpact(
                target=f"DB table: {tn.name}",
                level="high",
                confidence=0.65,
                evidence_ids=evidence_ids,
                verification_method="schema_graph",
            ))

        # Related tests
        verified_files = [e.file_path for e in evidence if e.evidence_type == "source"]
        all_tests: list[dict[str, Any]] = []
        for vf in verified_files[:5]:
            try:
                tests = self._adapter.find_tests_for_file(vf)
                for t in tests:
                    all_tests.append({
                        "name": t.name,
                        "file": t.file_path,
                        "id": t.id,
                    })
                    impacts.append(ChangeImpact(
                        target=f"Test: {t.name}",
                        level="low",
                        confidence=0.50,
                        evidence_ids=evidence_ids,
                        verification_method="test_convention",
                    ))
            except Exception:
                continue

        # Store related tests in the impacts context (used later by quality check)
        if all_tests:
            impacts.append(ChangeImpact(
                target=f"__related_tests__:{json.dumps(all_tests[:20])}",
                level="low",
                confidence=0.50,
                evidence_ids=evidence_ids,
                verification_method="test_discovery",
            ))

        return impacts

    @staticmethod
    def _estimate_impact_level(node: GraphNode, scope: str) -> str:
        """Estimate impact level based on node type."""
        if node.metadata.get("entrypoint"):
            return "critical"
        if node.node_type in ("route", "service"):
            return "high"
        if node.node_type in ("class", "function", "method"):
            return "medium"
        if node.node_type == "table":
            return "high"
        if node.node_type == "configuration":
            return "medium"
        return "low"

    # ------------------------------------------------------------------
    # Stage 8: Quality gate
    # ------------------------------------------------------------------

    def _quality_check(self, context_pack: ContextPack) -> dict[str, Any]:
        """Validate analysis quality before handoff to Planner."""
        # Re-extract related tests from the impact map
        related_tests: list[dict[str, Any]] = []
        for ci in context_pack.change_impact_map:
            if ci.target.startswith("__related_tests__:"):
                try:
                    payload = ci.target.split(":", 1)[1]
                    related_tests = json.loads(payload)
                except Exception:
                    pass

        if related_tests:
            context_pack.related_tests = related_tests

        gate = AnalysisQualityGate(threshold=self._config.confidence_threshold)

        # Compute overall confidence
        if context_pack.evidence:
            confidences = [
                e.confidence for e in context_pack.evidence if e.confidence > 0
            ]
            avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        else:
            avg_conf = 0.0

        # Boost if we have source evidence
        source_evidence = [
            e for e in context_pack.evidence if e.evidence_type == "source"
        ]
        if source_evidence:
            src_ratio = len(source_evidence) / max(1, len(context_pack.evidence))
            avg_conf = max(avg_conf, src_ratio * 0.8)

        context_pack.analysis_confidence = avg_conf
        result = gate.evaluate(context_pack)
        return result

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def _finalize(self, pack: ContextPack, status: str) -> ContextPack:
        elapsed = time.monotonic() - self._start_time
        pack.analysis_duration_ms = elapsed * 1000.0
        pack.metadata["final_status"] = status
        pack.metadata["total_stages"] = 8
        pack.metadata["config"] = self._config.to_dict()

        # Generate recommended scope from evidence
        evidence_files = list(dict.fromkeys(
            e.file_path for e in pack.evidence
            if e.evidence_type == "source" and e.confidence >= 0.5
        ))
        evidence_symbols = list(dict.fromkeys(
            e.symbol for e in pack.evidence
            if e.evidence_type == "source" and e.symbol
        ))

        pack.recommended_scope = RecommendedScope(
            included_files=evidence_files[:self._config.max_files],
            included_symbols=evidence_symbols[:self._config.max_symbols],
            excluded_files=[],
            excluded_reason="",
            confidence=pack.analysis_confidence,
        )

        # Extract unknowns and risks from request analysis gaps
        understanding = pack.request_understanding
        if not understanding.get("goal"):
            pack.unknowns.append({
                "field": "goal",
                "detail": "Could not extract goal from request",
                "severity": "high",
            })
        if not understanding.get("keywords") and not understanding.get("symbols"):
            pack.unknowns.append({
                "field": "keywords/symbols",
                "detail": "No technical keywords or symbols extracted from request",
                "severity": "medium",
            })

        if not pack.entrypoints:
            pack.risks.append({
                "risk": "No entrypoints identified",
                "impact": "Architecture analysis may be incomplete",
                "likelihood": "medium",
            })

        if not pack.evidence:
            pack.risks.append({
                "risk": "No source-verified evidence",
                "impact": "Planner may operate on unverified assumptions",
                "likelihood": "high",
            })

        self._emit_event(
            "finalize",
            f"status={status}, duration={elapsed:.2f}s, "
            f"confidence={pack.analysis_confidence:.2f}",
        )
        return pack
