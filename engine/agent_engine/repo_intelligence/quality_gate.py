"""Analysis Quality Gate — Planner only runs when analysis meets thresholds."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ContextPack, RepoIntelConfig


@dataclass
class GateCheck:
    check_name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"check_name": self.check_name, "passed": self.passed, "detail": self.detail}


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.0
    checks: list[dict] = field(default_factory=list)


class AnalysisQualityGate:
    """Validates ContextPack meets quality thresholds before Planner can run.

    If gate fails, the agent must do targeted follow-up analysis — not guess.
    """

    def __init__(self, config: RepoIntelConfig | None = None) -> None:
        self._config = config or RepoIntelConfig()
        self.confidence_threshold = self._config.confidence_threshold

    def check(self, context_pack: ContextPack) -> GateResult:
        checks: list[GateCheck] = [
            self._check_entrypoint(context_pack),
            self._check_execution_flow(context_pack),
            self._check_source_evidence(context_pack),
            self._check_impact_scope(context_pack),
            self._check_tests(context_pack),
            self._check_critical_unknowns(context_pack),
            self._check_confidence(context_pack),
        ]

        failures = [c.detail for c in checks if not c.passed]
        warnings = []
        passed = len(failures) == 0

        return GateResult(
            passed=passed,
            failures=failures,
            warnings=warnings,
            confidence=context_pack.analysis_confidence,
            checks=[c.to_dict() for c in checks],
        )

    def _check_entrypoint(self, pack: ContextPack) -> GateCheck:
        if not pack.entrypoints:
            return GateCheck(
                "entrypoint_found",
                False,
                "No entrypoint identified. Cannot determine where execution begins.",
            )
        return GateCheck(
            "entrypoint_found",
            True,
            f"Found {len(pack.entrypoints)} entrypoint(s): {', '.join(pack.entrypoints[:3])}",
        )

    def _check_execution_flow(self, pack: ContextPack) -> GateCheck:
        if not pack.current_execution_flow:
            return GateCheck(
                "execution_flow_reconstructed",
                False,
                "Current execution flow not reconstructed. Cannot plan changes without understanding existing flow.",
            )
        # Verify at least one flow has stages (handle both dataclass and dict shapes)
        for flow in pack.current_execution_flow:
            stages = None
            if hasattr(flow, "stages"):
                stages = flow.stages
            elif isinstance(flow, dict):
                stages = flow.get("stages")
            if stages:
                return GateCheck(
                    "execution_flow_reconstructed",
                    True,
                    f"Execution flow reconstructed with {len(stages)} stage(s)",
                )
        return GateCheck(
            "execution_flow_reconstructed",
            False,
            "Execution flow has no stages. Reconstruction incomplete.",
        )

    def _check_source_evidence(self, pack: ContextPack) -> GateCheck:
        if not pack.evidence:
            return GateCheck(
                "source_evidence",
                False,
                "No source-verified evidence. Critical conclusions must be backed by actual code.",
            )
        source_count = sum(1 for e in pack.evidence if e.evidence_type in {"source", "schema", "config"})
        if source_count < 1:
            return GateCheck(
                "source_evidence",
                False,
                f"{len(pack.evidence)} evidence item(s) but none from source/schema/config verification.",
            )
        return GateCheck(
            "source_evidence",
            True,
            f"{source_count} source-verified evidence item(s) out of {len(pack.evidence)} total",
        )

    def _check_impact_scope(self, pack: ContextPack) -> GateCheck:
        if not pack.change_impact_map:
            return GateCheck(
                "impact_scope",
                False,
                "No change impact map. Cannot assess scope of changes.",
            )
        levels = set(i.level for i in pack.change_impact_map)
        if "critical" in levels:
            return GateCheck(
                "impact_scope",
                True,
                f"Impact map includes critical-level changes — requires extra care. {len(pack.change_impact_map)} impact(s) total.",
            )
        return GateCheck(
            "impact_scope",
            True,
            f"Impact map covers {len(pack.change_impact_map)} impact(s) across levels: {', '.join(sorted(levels))}",
        )

    def _check_tests(self, pack: ContextPack) -> GateCheck:
        if not pack.related_tests:
            # Not a failure if project has no tests — but flag as covered
            if pack.related_tests is not None:
                return GateCheck(
                    "tests_identified",
                    True,
                    "No related tests found — confirmed absent in this project area.",
                )
            return GateCheck(
                "tests_identified",
                False,
                "Test analysis not performed. Must search for related tests.",
            )

        test_paths = [t.get("path", "") for t in pack.related_tests]
        return GateCheck(
            "tests_identified",
            True,
            f"Found {len(pack.related_tests)} related test(s): {', '.join(test_paths[:5])}",
        )

    def _check_critical_unknowns(self, pack: ContextPack) -> GateCheck:
        critical_unknowns = [
            u for u in pack.unknowns if u.get("severity", "medium") == "critical"
        ]
        if critical_unknowns:
            return GateCheck(
                "critical_unknowns",
                False,
                f"{len(critical_unknowns)} critical unknown(s): {', '.join(u['question'][:80] for u in critical_unknowns[:3])}",
            )
        return GateCheck(
            "critical_unknowns",
            True,
            f"No critical unknowns ({len(pack.unknowns)} unknown(s) at lower severity)",
        )

    def _check_confidence(self, pack: ContextPack) -> GateCheck:
        ok = pack.analysis_confidence >= self.confidence_threshold
        return GateCheck(
            "confidence_threshold",
            ok,
            f"Analysis confidence {pack.analysis_confidence:.2f} {'≥' if ok else '<'} threshold {self.confidence_threshold}",
        )
