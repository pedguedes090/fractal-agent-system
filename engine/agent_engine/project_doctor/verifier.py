"""Verifier — re-run the project's own check commands after patches.

We auto-detect which checks belong to the stack:
  * pyproject.toml ➜ pytest
  * package.json + a `check` script ➜ `pnpm run check`
  * fallback ➜ python -m compileall

Verification is read-only relative to source; it only produces a verdict
the doctor can stream and a transcript snippet for the report.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

EmitFn = Callable[[str, str], None]


def _has_pyproject(root: Path) -> bool:
    return (root / "pyproject.toml").exists()


def _has_package_check_script(root: Path) -> tuple[bool, bool]:
    pkg = root / "package.json"
    if not pkg.exists():
        return False, False
    try:
        manifest = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, False
    scripts = manifest.get("scripts") or {}
    return True, "check" in scripts


def _resolve_python(root: Path) -> str:
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return "python"


def _run(cmd: list[str], cwd: Path, emit: EmitFn, stage: str, timeout: int) -> dict[str, Any]:
    emit(stage, f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        emit(stage, f"command failed to launch: {exc}")
        return {"command": cmd, "ok": False, "code": -1, "stderr": str(exc), "stdout": ""}
    stdout_tail = "\n".join(result.stdout.splitlines()[-30:])
    stderr_tail = "\n".join(result.stderr.splitlines()[-30:])
    emit(stage, f"exit {result.returncode}")
    if stdout_tail:
        emit(stage, stdout_tail[-2000:])
    if stderr_tail and result.returncode != 0:
        emit(stage, stderr_tail[-2000:])
    return {
        "command": cmd,
        "ok": result.returncode == 0,
        "code": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
    }


def verify(root: Path, emit: EmitFn) -> dict[str, Any]:
    """Run the most informative checks the stack provides; return aggregate."""
    runs: list[dict[str, Any]] = []

    has_pkg, has_check = _has_package_check_script(root)
    if has_pkg and has_check and (root / "node_modules").exists():
        pnpm = "pnpm.cmd" if os.name == "nt" else "pnpm"
        runs.append(_run([pnpm, "run", "check"], root, emit, "doctor.verify.pnpm", timeout=600))

    if _has_pyproject(root):
        python_bin = _resolve_python(root)
        # The test suite is the source of truth; cap timeout to keep runs bounded.
        runs.append(_run(
            [python_bin, "-m", "pytest", "tests/", "-q", "--timeout=60"],
            root, emit, "doctor.verify.pytest", timeout=900,
        ))

    if not runs:
        # Last-resort compile check so we still emit something useful.
        runs.append(_run(
            ["python", "-m", "compileall", "-q", str(root)],
            root, emit, "doctor.verify.compileall", timeout=300,
        ))

    overall_ok = all(item["ok"] for item in runs)
    return {"ok": overall_ok, "runs": runs}
