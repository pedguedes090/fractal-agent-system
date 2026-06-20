"""Codebase-memory-mcp integration.

Thin Python wrapper around the codebase-memory-mcp binary (https://github.com/DeusData/codebase-memory-mcp).
The binary is a single-file C executable that maintains a persistent knowledge graph of the
workspace (functions, classes, call chains, routes, etc.) and exposes 14 MCP tools over stdio.

This module:
  - Detects the binary at the installer's standard location (or via env override / PATH).
  - Runs the binary's "cli <tool>" subcommand to call individual tools without the MCP stdio loop.
  - Lazily indexes a workspace once per run, and exposes structured queries
    (search_graph, get_architecture, trace_path, ...) for use by repo_intelligence and prompts.

All operations are best-effort: if the binary is missing, every public function returns None
and the caller falls back to its previous behavior.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .debug_log import write_debug_event


_BINARY_NAME = "codebase-memory-mcp"
_BINARY_EXE = _BINARY_NAME + (".exe" if os.name == "nt" else "")
_GITHUB_REPO = "DeusData/codebase-memory-mcp"
_DOWNLOAD_LOCK = threading.Lock()
_PROJECT_TOOLS_DIRNAME = ".tools/codebase-memory-mcp"

# Single indexing per (binary, project) per process — re-indexing every pipeline
# step would dominate latency for large repos.
_INDEXED_PROJECTS: set[tuple[str, str]] = set()


def _project_root() -> Path:
    """The HeThongAgent repo root — where .tools/ lives."""
    return Path(__file__).resolve().parents[2]


def _project_local_binary() -> Path:
    return _project_root() / _PROJECT_TOOLS_DIRNAME / _BINARY_EXE


def binary_path() -> Path | None:
    """Locate the codebase-memory-mcp binary.

    Resolution order:
      1. CODEBASE_MEMORY_MCP_BIN env override
      2. Project-local .tools/codebase-memory-mcp/codebase-memory-mcp(.exe)
      3. Installer's user-scope dirs (%LOCALAPPDATA%\\Programs\\..., ~/.local/bin)
      4. PATH lookup via where/which
    """
    override = os.environ.get("CODEBASE_MEMORY_MCP_BIN")
    if override:
        p = Path(override)
        return p if p.exists() else None
    local = _project_local_binary()
    if local.exists():
        return local
    candidates: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "Programs" / _BINARY_NAME / _BINARY_EXE)
    home = Path.home()
    candidates.extend([
        home / ".local" / "bin" / _BINARY_EXE,
        home / "AppData" / "Local" / "Programs" / _BINARY_NAME / _BINARY_EXE,
        Path("/usr/local/bin") / _BINARY_NAME,
    ])
    for path in candidates:
        if path.exists():
            return path
    try:
        result = subprocess.run(
            ["where" if os.name == "nt" else "which", _BINARY_NAME],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3, check=False,
        )
        line = (result.stdout or "").splitlines()
        if line and line[0].strip():
            p = Path(line[0].strip())
            return p if p.exists() else None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def is_available() -> bool:
    return binary_path() is not None


# ── Auto-download into the project's .tools/ ──────────────────────────────────
def _platform_archive_name() -> str | None:
    """Return the GitHub release archive name for this OS/arch, or None if unsupported."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows" and machine in {"amd64", "x86_64"}:
        return "codebase-memory-mcp-windows-amd64.zip"
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "codebase-memory-mcp-linux-amd64.tar.gz"
        if machine in {"aarch64", "arm64"}:
            return "codebase-memory-mcp-linux-arm64.tar.gz"
    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "codebase-memory-mcp-darwin-arm64.tar.gz"
        if machine in {"x86_64", "amd64"}:
            return "codebase-memory-mcp-darwin-amd64.tar.gz"
    return None


def _http_get(url: str, *, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hethongagent/codebase-memory"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ensure_local_binary(*, force: bool = False) -> Path | None:
    """Download the codebase-memory-mcp binary into .tools/ if missing.

    Returns the resolved binary path, or None when:
      - Already on disk (via env / system install / cached local copy) and force=False
      - Platform is unsupported (e.g. exotic arch)
      - Download fails (network, checksum, extraction)

    Best-effort: any failure is logged via write_debug_event and returns None.
    Callers must keep their previous fallback behavior (no-op if no binary).
    """
    if not force:
        existing = binary_path()
        if existing:
            return existing
    archive_name = _platform_archive_name()
    if not archive_name:
        write_debug_event("codebase_memory.download.unsupported_platform", {
            "system": platform.system(), "machine": platform.machine(),
        })
        return None
    with _DOWNLOAD_LOCK:
        # Re-check under lock — another thread may have completed the download.
        if not force:
            existing = binary_path()
            if existing:
                return existing
        try:
            base = f"https://github.com/{_GITHUB_REPO}/releases/latest/download"
            archive_url = f"{base}/{archive_name}"
            checksum_url = f"{base}/checksums.txt"
            write_debug_event("codebase_memory.download.start", {"archive": archive_name})

            archive_bytes = _http_get(archive_url, timeout=180.0)
            try:
                checksum_text = _http_get(checksum_url, timeout=30.0).decode("utf-8", "replace")
                expected = None
                for line in checksum_text.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[-1].endswith(archive_name):
                        expected = parts[0].lower()
                        break
                if expected:
                    actual = hashlib.sha256(archive_bytes).hexdigest().lower()
                    if actual != expected:
                        write_debug_event("codebase_memory.download.checksum_mismatch", {
                            "expected": expected, "actual": actual,
                        })
                        return None
            except (urllib.error.URLError, OSError):
                # Checksum file occasionally absent on prereleases — proceed without
                # but log so the user can see why.
                write_debug_event("codebase_memory.download.checksum_skipped", {})

            dest_dir = _project_root() / _PROJECT_TOOLS_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_bin = dest_dir / _BINARY_EXE

            if archive_name.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                    member = next((n for n in zf.namelist() if n.endswith(_BINARY_EXE)), None)
                    if not member:
                        write_debug_event("codebase_memory.download.binary_missing_in_zip", {})
                        return None
                    with zf.open(member) as src, dest_bin.open("wb") as dst:
                        dst.write(src.read())
            elif archive_name.endswith(".tar.gz"):
                import tarfile
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
                    member = next((m for m in tf.getmembers() if m.name.endswith(_BINARY_NAME)), None)
                    if not member:
                        write_debug_event("codebase_memory.download.binary_missing_in_tar", {})
                        return None
                    f = tf.extractfile(member)
                    if f is None:
                        return None
                    with dest_bin.open("wb") as dst:
                        dst.write(f.read())
                # Make executable on POSIX
                try:
                    os.chmod(dest_bin, 0o755)
                except OSError:
                    pass
            else:
                return None

            write_debug_event("codebase_memory.download.installed", {
                "path": str(dest_bin), "size": dest_bin.stat().st_size,
            })
            return dest_bin
        except (urllib.error.URLError, OSError, zipfile.BadZipFile, Exception) as exc:
            write_debug_event("codebase_memory.download.error", {"error": str(exc)})
            return None


def ensure_local_binary_async() -> None:
    """Kick off the download in a daemon thread. Safe to call from server boot."""
    if binary_path():
        return
    threading.Thread(target=ensure_local_binary, name="cbmcp-download", daemon=True).start()


def project_id_for(workspace: str | Path) -> str:
    """Replicates the binary's path → project-id derivation: drive letter + path with / → -."""
    p = Path(workspace).resolve()
    text = str(p).replace("\\", "/").replace(":", "")
    return text.strip("/").replace("/", "-")


def _run_tool(tool: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any] | None:
    bin_path = binary_path()
    if not bin_path:
        return None
    try:
        result = subprocess.run(
            [str(bin_path), "cli", tool, json.dumps(params)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        write_debug_event("codebase_memory.error", {"tool": tool, "error": str(exc)})
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    # The binary writes log lines to stderr; stdout is pure JSON. Be defensive in case
    # of merged streams and pick the last JSON line.
    last_json = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            last_json = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last_json


def ensure_indexed(workspace: str | Path, *, force: bool = False) -> dict[str, Any] | None:
    """Index the workspace if not already done in this process. Returns the index summary."""
    bin_path = binary_path()
    if not bin_path:
        return None
    project = project_id_for(workspace)
    key = (str(bin_path), project)
    if not force and key in _INDEXED_PROJECTS:
        return {"project": project, "cached": True}
    result = _run_tool(
        "index_repository",
        {"repo_path": str(Path(workspace).resolve()).replace("\\", "/")},
        timeout=300.0,
    )
    if result and result.get("status") == "indexed":
        _INDEXED_PROJECTS.add(key)
        write_debug_event("codebase_memory.indexed", {
            "project": project,
            "nodes": result.get("nodes"),
            "edges": result.get("edges"),
        })
    return result


def search_graph(workspace: str | Path, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    project = project_id_for(workspace)
    out = _run_tool("search_graph", {"project": project, "query": query, "limit": limit}, timeout=10.0)
    return list((out or {}).get("results") or [])


def get_architecture(workspace: str | Path, aspects: list[str] | None = None) -> dict[str, Any] | None:
    project = project_id_for(workspace)
    params: dict[str, Any] = {"project": project}
    if aspects:
        params["aspects"] = aspects
    return _run_tool("get_architecture", params, timeout=15.0)


def trace_path(workspace: str | Path, source: str, target: str, *, max_depth: int = 6) -> dict[str, Any] | None:
    project = project_id_for(workspace)
    return _run_tool("trace_path", {
        "project": project, "source": source, "target": target, "max_depth": max_depth,
    }, timeout=10.0)


def get_code_snippet(workspace: str | Path, qualified_name: str) -> dict[str, Any] | None:
    project = project_id_for(workspace)
    return _run_tool("get_code_snippet", {"project": project, "qualified_name": qualified_name}, timeout=5.0)


def index_status(workspace: str | Path) -> dict[str, Any] | None:
    project = project_id_for(workspace)
    return _run_tool("index_status", {"project": project}, timeout=5.0)


@dataclass(frozen=True)
class McpServerConfig:
    """Used by openhands_worker to auto-inject this MCP into the OpenHands agent."""
    name: str = "codebase-memory-mcp"
    command: str = ""

    @classmethod
    def detect(cls) -> "McpServerConfig | None":
        bin_path = binary_path()
        if not bin_path:
            return None
        return cls(command=str(bin_path))

    def as_mcp_server_entry(self) -> dict[str, Any]:
        return {"command": self.command, "args": []}
