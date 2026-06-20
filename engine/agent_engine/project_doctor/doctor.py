"""Project Doctor orchestrator.

Pipeline shape:
  scan ➜ plan ➜ patch (stream) ➜ verify ➜ done

The orchestrator never raises on a per-issue failure; it records the issue
as skipped and continues. The only place an exception escapes is when the
caller passed an unreachable project root or no events were emitted at all
— those would be programmer errors, not user-fixable conditions.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import FixReport, Issue, ScanReport
from .patcher import apply_patches
from .planner import plan
from .scanner import scan_project
from .verifier import verify

EmitFn = Callable[[str, str], None]


class Doctor:
    def __init__(self, root: Path, provider: Any | None = None, emit: EmitFn | None = None) -> None:
        self.root = root.resolve()
        self.provider = provider
        self.emit: EmitFn = emit or (lambda _stage, _detail: None)

    def run(self) -> dict[str, Any]:
        self.emit("doctor.start", f"Project root: {self.root}")

        # 1. Scan
        report: ScanReport = scan_project(self.root)
        self.emit("doctor.scan", f"{len(report.issues)} issues in {report.duration_ms}ms")
        for group, count in sorted(report.counts_by_group.items()):
            self.emit("doctor.scan.group", f"{group}: {count}")
        if not report.issues:
            self.emit("doctor.done", "No issues found.")
            return {
                "scan": report.to_dict(),
                "plan": [],
                "fix": FixReport().to_dict(),
                "verify": {"ok": True, "runs": []},
                "ok": True,
            }

        # 2. Plan
        ordered: list[Issue] = plan(report)
        self.emit("doctor.plan", f"Ordered {len(ordered)} patches by group + severity")
        for i, issue in enumerate(ordered[:10], 1):
            self.emit("doctor.plan.item", f"{i}. {issue.group.value}/{issue.severity.value} · {issue.file} · {issue.title}")

        # 3. Patch (stream tokens via emit("doctor.patch.chunk", ...))
        fix_report: FixReport = apply_patches(self.root, ordered, self.emit, self.provider)
        self.emit("doctor.patch.done", f"{len(fix_report.applied)} applied · {len(fix_report.skipped)} skipped · {fix_report.streamed_chunks} chunks")

        # 4. Verify
        verification = verify(self.root, self.emit)
        self.emit(
            "doctor.verify",
            f"verdict: {'PASS' if verification['ok'] else 'FAIL'} · {len(verification['runs'])} command(s)",
        )

        ok = verification["ok"]
        self.emit("doctor.done", "PASS" if ok else "FAIL — see verification.runs for failed commands")
        return {
            "scan": report.to_dict(),
            "plan": [i.to_dict() for i in ordered],
            "fix": fix_report.to_dict(),
            "verify": verification,
            "ok": ok,
        }


def run_doctor(root: str | Path, *, provider: Any | None = None, emit: EmitFn | None = None) -> dict[str, Any]:
    return Doctor(Path(root), provider=provider, emit=emit).run()
