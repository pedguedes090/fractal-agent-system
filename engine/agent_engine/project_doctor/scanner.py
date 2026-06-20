"""Static scanner — deterministic checks before any LLM involvement.

The scanner only reads the project; it never edits. Its job is to populate
ScanReport with concrete, line-anchored Issue records that the planner
and patcher can act on.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .models import Issue, IssueGroup, IssueSeverity, ScanReport

# Walked-into directories that almost never contain user-fixable issues.
_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".tools", ".agent-state", ".codegraph", "dist", "build", "out",
    ".pytest-tmp", ".next", ".nuxt", "target", "vendor",
}

# Patterns that strongly suggest a real secret left in source.
# We require the assignment shape so plain mentions in docs/markdown don't trip.
_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*["\']([A-Za-z0-9_\-]{16,})["\']'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),
    re.compile(r'xox[bp]-[A-Za-z0-9-]{10,}'),
]


def _issue_id() -> str:
    return uuid.uuid4().hex[:12]


def _walk_files(root: Path, exts: set[str]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in exts:
                out.append(Path(dirpath) / name)
    return out


def _check_python_syntax(root: Path, report: ScanReport) -> None:
    for path in _walk_files(root, {".py"}):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            report.add(Issue(
                id=_issue_id(),
                group=IssueGroup.CRITICAL,
                severity=IssueSeverity.BLOCKER,
                file=str(path.relative_to(root)),
                line=exc.lineno,
                title=f"Python syntax error: {exc.msg}",
                detail=f"{exc.msg} at line {exc.lineno}, offset {exc.offset}",
                root_cause="Source file cannot be parsed by the Python AST.",
                suggested_fix=f"Fix the syntax near line {exc.lineno}.",
                autofix_safe=False,  # syntax fixes need the LLM patch path
            ))


def _check_js_syntax(root: Path, report: ScanReport) -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        return
    for path in _walk_files(root, {".js", ".mjs", ".cjs"}):
        try:
            result = subprocess.run(
                [node_bin, "--check", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            continue
        stderr = result.stderr.strip() or "node --check failed"
        line_match = re.search(r":(\d+)", stderr.splitlines()[0]) if stderr else None
        line = int(line_match.group(1)) if line_match else None
        # Distill the message — first 200 chars of last meaningful line.
        msg = next((ln.strip() for ln in stderr.splitlines() if "Error" in ln or "SyntaxError" in ln), stderr.splitlines()[0])
        report.add(Issue(
            id=_issue_id(),
            group=IssueGroup.CRITICAL,
            severity=IssueSeverity.BLOCKER,
            file=str(path.relative_to(root)),
            line=line,
            title=f"JS syntax error: {msg[:120]}",
            detail=stderr[:800],
            root_cause="`node --check` rejected the file.",
            suggested_fix="Restore the missing brace/paren/semicolon at the indicated location.",
            autofix_safe=False,
        ))


def _scan_secrets(root: Path, report: ScanReport) -> None:
    text_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".env", ".cfg", ".toml"}
    for path in _walk_files(root, text_exts):
        # Skip lockfiles and obvious example/template files
        name = path.name.lower()
        if name.endswith(".lock") or name == "package-lock.json" or "example" in name or name == ".env.example":
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(source):
                line = source[:match.start()].count("\n") + 1
                report.add(Issue(
                    id=_issue_id(),
                    group=IssueGroup.SECURITY,
                    severity=IssueSeverity.MAJOR,
                    file=str(path.relative_to(root)),
                    line=line,
                    title="Possible secret committed in source",
                    detail=f"Pattern matched near line {line}: {match.group(0)[:60]}",
                    root_cause="Long, secret-shaped literal sits in tracked source.",
                    suggested_fix="Move the value to an environment variable and read via os.getenv / process.env.",
                    autofix_safe=False,
                ))


def _check_gitignore_basics(root: Path, report: ScanReport) -> None:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return
    content = gitignore.read_text(encoding="utf-8", errors="ignore")
    must_have = [".env", "node_modules", "__pycache__"]
    missing = [item for item in must_have if item not in content]
    if missing:
        report.add(Issue(
            id=_issue_id(),
            group=IssueGroup.SECURITY,
            severity=IssueSeverity.MINOR,
            file=".gitignore",
            line=None,
            title=f".gitignore missing common entries: {', '.join(missing)}",
            detail=f"Entries that should be ignored: {missing}",
            root_cause="Project may accidentally commit env files or dependency caches.",
            suggested_fix=f"Append the missing entries to .gitignore: {missing}",
            autofix_safe=True,
        ))


def _check_dep_lock_drift(root: Path, report: ScanReport) -> None:
    pkg = root / "package.json"
    lock = root / "pnpm-lock.yaml"
    if not pkg.exists():
        return
    try:
        manifest = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    declared = set(manifest.get("dependencies", {}).keys()) | set(manifest.get("devDependencies", {}).keys())
    if not declared or not lock.exists():
        return
    try:
        lock_text = lock.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    # Cheap heuristic: every declared dep should appear in the lockfile text.
    missing = [name for name in declared if f"'{name}'" not in lock_text and f'"{name}"' not in lock_text and f"\n  {name}:" not in lock_text]
    if missing:
        report.add(Issue(
            id=_issue_id(),
            group=IssueGroup.CRITICAL,
            severity=IssueSeverity.MAJOR,
            file="pnpm-lock.yaml",
            line=None,
            title=f"Lockfile out of sync with package.json ({len(missing)} deps)",
            detail=f"Declared but missing in lockfile: {missing[:10]}",
            root_cause="pnpm install was not re-run after package.json was edited.",
            suggested_fix="Run `pnpm install` to regenerate the lockfile.",
            autofix_safe=True,
        ))


def scan_project(root: Path) -> ScanReport:
    report = ScanReport(project_root=str(root))
    started = time.monotonic()
    _check_python_syntax(root, report)
    _check_js_syntax(root, report)
    _scan_secrets(root, report)
    _check_gitignore_basics(root, report)
    _check_dep_lock_drift(root, report)
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report
