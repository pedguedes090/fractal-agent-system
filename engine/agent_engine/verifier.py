"""Post-execution verifier — checks that code changes meet requirements.

Architecture:
  - Verifier: runs post-execution checks on coder output
  - Checks: tool errors, file changes, build/lint/typecheck, tests, scope
  - Returns structured verdict with blockers and warnings
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .debug_log import write_debug_event


@dataclass
class VerificationCheck:
    """A single verification check result."""

    name: str
    passed: bool
    detail: str = ""
    is_blocker: bool = True
    duration_ms: float = 0.0


@dataclass
class Verdict:
    """Complete verification verdict."""

    passed: bool = True
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[VerificationCheck] = field(default_factory=list)
    command_results: list[dict[str, Any]] = field(default_factory=list)
    affected_tests: list[str] = field(default_factory=list)

    def add_blocker(self, message: str, name: str = "") -> None:
        self.blockers.append(message)
        if name:
            self.checks.append(VerificationCheck(name=name, passed=False, detail=message))
        self.passed = False

    def add_warning(self, message: str, name: str = "") -> None:
        self.warnings.append(message)
        if name:
            self.checks.append(VerificationCheck(name=name, passed=True, detail=message, is_blocker=False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "is_blocker": c.is_blocker,
                    "duration_ms": c.duration_ms,
                }
                for c in self.checks
            ],
            "command_results": self.command_results,
            "affected_tests": self.affected_tests,
        }


@dataclass
class VerifierConfig:
    """Configuration for the verifier."""

    skip_build: bool = False
    skip_lint: bool = False
    skip_typecheck: bool = False
    skip_test: bool = False
    timeout_per_command: int = 120
    docker_available: bool = False

    @classmethod
    def from_env(cls) -> "VerifierConfig":
        import os
        return cls(
            skip_build=os.getenv("VERIFIER_SKIP_BUILD", "").lower() in {"1", "true"},
            skip_test=os.getenv("VERIFIER_SKIP_TEST", "").lower() in {"1", "true"},
            timeout_per_command=int(os.getenv("VERIFIER_TIMEOUT", "120")),
        )


class Verifier:
    """Post-execution verification engine."""

    def __init__(self, config: VerifierConfig | None = None) -> None:
        self.config = config or VerifierConfig()

    def verify(
        self,
        workspace: str,
        changed_files: list[dict[str, Any]],
        plan: dict[str, Any],
        worker_result: dict[str, Any],
        *,
        emit: Callable[[str, str], None] | None = None,
    ) -> Verdict:
        """Run all verification checks on coder output.

        Args:
            workspace: Path to workspace
            changed_files: Files changed by coder
            plan: The execution plan
            worker_result: Full coder output
            emit: Progress callback

        Returns:
            Verdict with blockers and warnings
        """
        verdict = Verdict()

        # 1. Check for tool errors
        self._check_tool_errors(worker_result, verdict, emit)

        # 2. Check files actually changed
        self._check_file_changes(workspace, changed_files, plan, verdict, emit)

        # 3. Check for out-of-scope changes
        self._check_scope(workspace, changed_files, plan, verdict, emit)

        # 4. Build/lint/typecheck
        if not self.config.skip_build:
            self._check_build(workspace, changed_files, plan, verdict, emit)

        # 5. Run relevant tests
        if not self.config.skip_test:
            self._check_tests(workspace, changed_files, plan, verdict, emit)

        # 6. Verify against acceptance criteria
        self._check_acceptance(workspace, changed_files, plan, worker_result, verdict, emit)

        write_debug_event("verifier.complete", {
            "passed": verdict.passed,
            "blockers": len(verdict.blockers),
            "warnings": len(verdict.warnings),
            "checks": len(verdict.checks),
        })

        return verdict

    def _check_tool_errors(
        self, worker_result: dict[str, Any], verdict: Verdict, emit: Callable[[str, str], None] | None
    ) -> None:
        """Check if any tool calls resulted in errors."""
        error = worker_result.get("error")
        if error:
            verdict.add_blocker(f"Coder agent reported error: {error}", "tool_error")
            if emit:
                emit("verify", f"Tool error: {error[:120]}")

    def _check_file_changes(
        self,
        workspace: str,
        changed_files: list[dict[str, Any]],
        plan: dict[str, Any],
        verdict: Verdict,
        emit: Callable[[str, str], None] | None,
    ) -> None:
        """Check that expected files were actually changed."""
        root = Path(workspace).resolve()

        for f in changed_files:
            path = f.get("path", "")
            full_path = root / path
            status = f.get("status", "")

            if status == "created" and not full_path.exists():
                verdict.add_blocker(f"Expected file was not created: {path}", "file_created")
            elif status == "modified" and not full_path.exists():
                verdict.add_blocker(f"Modified file does not exist: {path}", "file_modified")

        plan_files = set(str(p) for p in (plan.get("files", []) or []))
        changed_paths = set(str(f.get("path", "")) for f in changed_files)
        missing = plan_files - changed_paths
        if missing and plan_files:
            verdict.add_warning(f"Plan expected these files but they were not changed: {', '.join(sorted(missing))}", "plan_files_unchanged")

        if not changed_files:
            verdict.add_warning("No files were changed by the coder", "no_changes")

    def _check_scope(
        self,
        workspace: str,
        changed_files: list[dict[str, Any]],
        plan: dict[str, Any],
        verdict: Verdict,
        emit: Callable[[str, str], None] | None,
    ) -> None:
        """Check for changes outside allowed scope."""
        allowed_patterns = set(str(p) for p in (plan.get("allowedFiles", []) or []))
        if not allowed_patterns:
            return

        import fnmatch
        for f in changed_files:
            path = str(f.get("path", "")).replace("\\", "/")
            if not any(fnmatch.fnmatch(path, pat) for pat in allowed_patterns if pat):
                verdict.add_blocker(f"File changed outside allowed scope: {path}", "scope_violation")

    def _check_build(
        self,
        workspace: str,
        changed_files: list[dict[str, Any]],
        plan: dict[str, Any],
        verdict: Verdict,
        emit: Callable[[str, str], None] | None,
    ) -> None:
        """Run build/lint/typecheck based on project type."""
        root = Path(workspace).resolve()

        # Detect project type from plan or changed files
        has_node = any(str(f.get("path", "")).endswith((".js", ".ts", ".jsx", ".tsx")) for f in changed_files)
        has_python = any(str(f.get("path", "")).endswith(".py") for f in changed_files)

        if has_node and (root / "package.json").exists():
            self._run_verify_command(
                root,
                verdict,
                "npm_compile",
                "npm run build" if "build" in self._get_npm_scripts(root) else "node --check .",
                is_blocker=False,  # npm build may fail without deps installed
                emit=emit,
            )

        if has_python:
            self._run_verify_command(
                root,
                verdict,
                "python_compile",
                "python -m compileall .",
                is_blocker=True,
                emit=emit,
            )

    def _check_tests(
        self,
        workspace: str,
        changed_files: list[dict[str, Any]],
        plan: dict[str, Any],
        verdict: Verdict,
        emit: Callable[[str, str], None] | None,
    ) -> None:
        """Run relevant tests based on changed files."""
        root = Path(workspace).resolve()
        spec = plan.get("workerTaskSpec") or plan or {}
        verification_commands = spec.get("verificationCommands") or spec.get("commandsToRun") or []

        for cmd in verification_commands[:3]:
            self._run_verify_command(
                root,
                verdict,
                f"verification_cmd",  # noqa
                cmd if isinstance(cmd, str) else cmd.get("command", str(cmd)),
                is_blocker=True,
                emit=emit,
            )

    def _check_acceptance(
        self,
        workspace: str,
        changed_files: list[dict[str, Any]],
        plan: dict[str, Any],
        worker_result: dict[str, Any],
        verdict: Verdict,
        emit: Callable[[str, str], None] | None,
    ) -> None:
        """Verify acceptance criteria from plan."""
        criteria = plan.get("acceptanceCriteria", []) or []
        # Basic check: if criteria mention specific files, ensure those files exist/changed
        for criterion in criteria:
            if not isinstance(criterion, str):
                continue
            for f in changed_files:
                if f.get("path", "") in criterion:
                    if emit:
                        emit("verify", f"Acceptance criterion met: {criterion[:80]}")
                    break
            else:
                # Criterion mentions no changed files — this is informational
                pass

    def _run_verify_command(
        self,
        workspace: Path,
        verdict: Verdict,
        name: str,
        command: str,
        *,
        is_blocker: bool = True,
        emit: Callable[[str, str], None] | None = None,
    ) -> VerificationCheck:
        """Run a verification command and record result."""
        import time as _time

        if not command or not isinstance(command, str):
            return VerificationCheck(name=name, passed=True, detail="no command to run", is_blocker=is_blocker)

        start = _time.monotonic()

        try:
            proc = subprocess.run(
                command,
                cwd=str(workspace),
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_per_command,
            )
            duration_ms = (_time.monotonic() - start) * 1000
            passed = proc.returncode == 0
            detail = f"exit {proc.returncode}; {proc.stdout[-200:].strip() or proc.stderr[-200:].strip()}"

            result = {
                "command": command,
                "cwd": str(workspace),
                "code": proc.returncode,
                "stdout": proc.stdout[-5000:],
                "stderr": proc.stderr[-5000:],
            }
            verdict.command_results.append(result)

        except subprocess.TimeoutExpired:
            duration_ms = (_time.monotonic() - start) * 1000
            passed = False
            detail = f"timed out after {self.config.timeout_per_command}s"
            verdict.command_results.append({"command": command, "timed_out": True})

        except Exception as exc:
            duration_ms = (_time.monotonic() - start) * 1000
            passed = False
            detail = str(exc)

        check = VerificationCheck(name=name, passed=passed, detail=detail, is_blocker=is_blocker, duration_ms=duration_ms)

        if not passed:
            if is_blocker:
                verdict.add_blocker(f"[{name}] {detail}", name)
            else:
                verdict.add_warning(f"[{name}] {detail}", name)
        else:
            verdict.checks.append(check)

        if emit:
            emit("verify", f"{'PASS' if passed else 'FAIL'}: {name} ({detail[:80]})")

        return check

    def _get_npm_scripts(self, root: Path) -> set[str]:
        """Parse package.json scripts."""
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            return set((pkg.get("scripts") or {}).keys())
        except Exception:
            return set()
