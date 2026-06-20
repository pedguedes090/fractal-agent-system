"""Planning Council — multi-planner debate + judging for complex tasks.

Architecture:
  - 5 independent planner agents produce CandidatePlans
  - Cross-critique + judging + synthesis → FinalPlan
  - Critical veto power (correctness/security/data-loss)
  - Max 2 debate rounds
  - Structured ballots with evidence, not majority vote
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CandidatePlan:
    """A plan proposal from one planner agent."""
    plan_id: str
    planner_role: str  # minimal | architecture | test_first | risk | contrarian
    assumptions: list[str] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    estimated_complexity: str = "medium"
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "planner_role": self.planner_role,
            "assumptions": self.assumptions,
            "tasks": self.tasks,
            "dependencies": self.dependencies,
            "risks": self.risks,
            "tests": self.tests,
            "tradeoffs": self.tradeoffs,
            "evidence_ids": self.evidence_ids,
            "estimated_complexity": self.estimated_complexity,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidatePlan":
        return cls(
            plan_id=data.get("plan_id", ""),
            planner_role=data.get("planner_role", ""),
            assumptions=data.get("assumptions", []),
            tasks=data.get("tasks", []),
            dependencies=data.get("dependencies", []),
            risks=data.get("risks", []),
            tests=data.get("tests", []),
            tradeoffs=data.get("tradeoffs", []),
            evidence_ids=data.get("evidence_ids", []),
            estimated_complexity=data.get("estimated_complexity", "medium"),
            confidence=data.get("confidence", 0.0),
        )


@dataclass
class RubricScore:
    """Score for one evaluation criterion."""
    criterion: str
    score: float  # 0.0 - 1.0
    reasoning: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class Critique:
    """Critique of a candidate plan."""
    critique_id: str
    target_plan_id: str
    critic_role: str
    missing: list[str] = field(default_factory=list)  # missing requirements/tasks
    unnecessary: list[str] = field(default_factory=list)  # unnecessary tasks
    wrong_deps: list[str] = field(default_factory=list)  # wrong dependencies
    unhandled_risks: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class Ballot:
    """A judge's evaluation of a plan."""
    ballot_id: str
    judge_role: str
    target_plan_id: str
    scores: list[RubricScore] = field(default_factory=list)
    veto: bool = False
    veto_reason: str = ""
    total_score: float = 0.0


@dataclass
class CouncilDecision:
    """Final decision from Planning Council."""
    plan_id: str
    verdict: Literal["approved", "revisions_needed", "rejected"]
    scores: dict[str, float] = field(default_factory=dict)  # plan_id -> total_score
    vetoes: list[dict[str, Any]] = field(default_factory=list)
    synthesis_notes: list[str] = field(default_factory=list)
    debate_round: int = 0
    confidence: float = 0.0


# Evaluation rubric criteria
RUBRIC_CRITERIA = [
    "correctness",
    "requirement_coverage",
    "repository_evidence",
    "minimal_scope",
    "dependency_ordering",
    "testability",
    "security_and_regression_risk",
    "rollback_capability",
    "cost_and_complexity",
]

# Veto criteria — a single critical veto overrides majority
VETO_TRIGGERS = [
    "data_loss",
    "security_breach",
    "production_outage",
    "irreversible_migration",
    "incorrect_architecture",
]


class PlanningCouncil:
    """Orchestrates multi-planner debate with judging and synthesis.

    Flow:
      1. Dispatch 5 independent planners (each gets ContextPack, not other plans)
      2. Cross-critique: each planner critiques up to 2 other plans
      3. Judges score against rubric
      4. Synthesizer combines best parts
      5. Critical veto overrides majority
    """

    PLANNER_ROLES = ["minimal", "architecture", "test_first", "risk", "contrarian"]
    MAX_DEBATE_ROUNDS = 2

    def __init__(self) -> None:
        self.plans: dict[str, CandidatePlan] = {}
        self.critiques: list[Critique] = []
        self.ballots: list[Ballot] = []
        self.decisions: list[CouncilDecision] = []

    def register_plan(self, plan: CandidatePlan) -> None:
        self.plans[plan.plan_id] = plan

    def register_critique(self, critique: Critique) -> None:
        self.critiques.append(critique)

    def register_ballot(self, ballot: Ballot) -> None:
        self.ballots.append(ballot)

    def synthesize(self) -> CouncilDecision:
        """Produce the final council decision from registered plans + critiques + ballots."""
        if not self.plans:
            return CouncilDecision(
                plan_id="",
                verdict="rejected",
                scores={},
                confidence=0.0,
            )

        # Aggregate ballot scores per plan
        scores: dict[str, list[float]] = {pid: [] for pid in self.plans}
        vetoes: list[dict[str, Any]] = []

        for ballot in self.ballots:
            scores.setdefault(ballot.target_plan_id, []).append(ballot.total_score)
            if ballot.veto:
                vetoes.append({
                    "plan_id": ballot.target_plan_id,
                    "judge": ballot.judge_role,
                    "reason": ballot.veto_reason,
                })

        # Average scores
        avg_scores: dict[str, float] = {}
        for pid, score_list in scores.items():
            avg_scores[pid] = sum(score_list) / len(score_list) if score_list else 0.0

        # Find winner
        if avg_scores:
            best_plan_id = max(avg_scores, key=avg_scores.get)
            best_score = avg_scores[best_plan_id]
        else:
            best_plan_id = list(self.plans)[0] if self.plans else ""
            best_score = 0.0

        # Check vetoes
        best_vetoes = [v for v in vetoes if v["plan_id"] == best_plan_id]
        if best_vetoes:
            # Try next-best plan without vetoes
            remaining = {pid: s for pid, s in avg_scores.items() if not any(v["plan_id"] == pid for v in vetoes)}
            if remaining:
                best_plan_id = max(remaining, key=remaining.get)
                best_score = remaining[best_plan_id]
            else:
                return CouncilDecision(
                    plan_id=best_plan_id,
                    verdict="rejected",
                    scores=avg_scores,
                    vetoes=vetoes,
                    confidence=0.0,
                )

        # Confidence = winner_score / sum of all scores (normalized)
        total = sum(avg_scores.values()) or 1.0
        confidence = best_score / (total / len(avg_scores)) if avg_scores else 0.0

        return CouncilDecision(
            plan_id=best_plan_id,
            verdict="approved" if confidence >= 0.5 else "revisions_needed",
            scores=avg_scores,
            vetoes=vetoes,
            synthesis_notes=[
                f"Best plan: {best_plan_id} (score: {best_score:.2f})",
                f"Total plans evaluated: {len(self.plans)}",
                f"Total critiques: {len(self.critiques)}",
                f"Total ballots: {len(self.ballots)}",
                f"Vetoes: {len(vetoes)}",
            ],
            debate_round=len(self.decisions) + 1,
            confidence=min(1.0, confidence),
        )

    def reset(self) -> None:
        self.plans.clear()
        self.critiques.clear()
        self.ballots.clear()
        # Keep decisions for audit trail

    def to_dict(self) -> dict[str, Any]:
        return {
            "plans": {pid: p.to_dict() for pid, p in self.plans.items()},
            "critiques": [
                {
                    "critique_id": c.critique_id,
                    "target_plan_id": c.target_plan_id,
                    "critic_role": c.critic_role,
                    "missing": c.missing,
                    "summary": c.summary,
                }
                for c in self.critiques
            ],
            "ballots": [
                {
                    "ballot_id": b.ballot_id,
                    "judge_role": b.judge_role,
                    "target_plan_id": b.target_plan_id,
                    "total_score": b.total_score,
                    "veto": b.veto,
                }
                for b in self.ballots
            ],
            "decisions": [
                {
                    "plan_id": d.plan_id,
                    "verdict": d.verdict,
                    "scores": d.scores,
                    "confidence": d.confidence,
                }
                for d in self.decisions
            ],
        }
