"""Skill Router — deterministic two-tier skill selection.

ABSOLUTELY FORBIDDEN:
  - Random skill selection
  - Selection by name similarity alone
  - Semantic similarity as sole criterion
  - Default assignment when confidence is low
  - Self-declaration by agents without evidence

Two tiers:
  1. Deterministic Eligibility Filter (binary — pass/fail)
  2. Evidence-based Ranker (scored, must exceed quality gate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Skill, SkillEligibility, SkillMatch, SkillSelection


class SkillRouter:
    """Deterministic skill selection — no random fallback."""

    ELIGIBILITY_CHECKS = [
        "trigger_match",
        "lifecycle_stage_match",
        "role_allowed",
        "tools_available",
        "no_conflict",
    ]

    def __init__(self, registry: dict[str, Skill]) -> None:
        self.registry = registry

    def select(
        self,
        task_id: str,
        task_type: str,
        lifecycle_stage: str,
        agent_role: str,
        available_tools: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        confidence_threshold: float = 0.3,
    ) -> SkillSelection:
        """Select skills for a task. Returns SkillSelection with reasons + evidence."""
        available_tools = available_tools or []
        required_capabilities = required_capabilities or []

        eligible: list[Skill] = []
        rejected: list[SkillEligibility] = []

        for skill in self.registry.values():
            eligibility = self._check_eligibility(
                skill, task_type, lifecycle_stage, agent_role, available_tools
            )
            if eligibility.eligible:
                eligible.append(skill)
            else:
                rejected.append(eligibility)

        if not eligible:
            return SkillSelection(
                task_id=task_id,
                selected_skills=[],
                rejected_skills=rejected,
                confidence=0.0,
            )

        # Rank eligible skills
        ranked = self._rank(eligible, task_type, required_capabilities, available_tools, agent_role)
        # Filter by confidence threshold
        selected = [r for r in ranked if r.confidence >= confidence_threshold]

        total_confidence = (
            sum(r.confidence for r in selected) / len(selected) if selected else 0.0
        )

        return SkillSelection(
            task_id=task_id,
            selected_skills=selected,
            rejected_skills=rejected,
            confidence=total_confidence,
            deterministic=True,
        )

    def _check_eligibility(
        self,
        skill: Skill,
        task_type: str,
        lifecycle_stage: str,
        agent_role: str,
        available_tools: list[str],
    ) -> SkillEligibility:
        checks: dict[str, bool] = {}

        # 1. Trigger match
        trigger_keywords = skill.trigger.lower().split()
        task_lower = task_type.lower()
        trigger_ok = any(kw in task_lower for kw in trigger_keywords) if trigger_keywords else True
        checks["trigger_match"] = trigger_ok

        # 2. Lifecycle stage match
        stage_ok = skill.lifecycle_stage == lifecycle_stage or skill.lifecycle_stage == "any"
        checks["lifecycle_stage_match"] = stage_ok

        # 3. Role allowed
        role_ok = not skill.agent_roles or agent_role in skill.agent_roles
        checks["role_allowed"] = role_ok

        # 4. Required tools available — need at least core tools (file_read/file_write), not all
        if not skill.required_tools:
            tool_ok = True
        else:
            core_tools_needed = [t for t in skill.required_tools if t in {"file_read", "file_write", "command_run"}]
            tool_ok = all(t in available_tools for t in core_tools_needed)
        checks["tools_available"] = tool_ok

        # 5. No conflict with active skills
        conflict_ok = True  # checked by caller
        checks["no_conflict"] = conflict_ok

        eligible = all(checks.values())
        reason = ""
        if not eligible:
            failures = [k for k, v in checks.items() if not v]
            reason = f"Failed checks: {', '.join(failures)}"

        return SkillEligibility(skill_id=skill.id, eligible=eligible, reason=reason, checks=checks)

    def _rank(
        self,
        skills: list[Skill],
        task_type: str,
        required_capabilities: list[str],
        available_tools: list[str],
        agent_role: str,
    ) -> list[SkillMatch]:
        scored: list[tuple[float, SkillMatch]] = []

        for skill in skills:
            score = 0.0
            evidence: list[str] = []

            # Trigger match depth (0-0.3)
            trigger_depth = self._trigger_depth(skill.trigger, task_type)
            score += trigger_depth * 0.3
            if trigger_depth > 0:
                evidence.append(f"Trigger '{skill.trigger}' matches task type '{task_type}' at depth {trigger_depth:.2f}")

            # Capability coverage (0-0.25)
            cap_score = self._capability_coverage(skill, required_capabilities)
            score += cap_score * 0.25
            if cap_score > 0 and required_capabilities:
                evidence.append(f"Covers {cap_score:.0%} of required capabilities")

            # Tool compatibility (0-0.2)
            tool_score = self._tool_compatibility(skill, available_tools)
            score += tool_score * 0.2
            if skill.required_tools:
                evidence.append(f"Tools: {tool_score:.0%} available ({', '.join([t for t in skill.required_tools if t in available_tools])})")

            # Role suitability (0-0.15)
            role_score = 1.0 if agent_role in skill.agent_roles else 0.3
            score += role_score * 0.15
            evidence.append(f"Role '{agent_role}' {'matched' if agent_role in skill.agent_roles else 'partial'} for skill roles {skill.agent_roles}")

            # Lifecycle precision (0-0.1)
            lifecycle_bonus = 0.5 if skill.lifecycle_stage != "any" else 0.0
            score += lifecycle_bonus * 0.1

            match = SkillMatch(
                skill_id=skill.id,
                task_id="",
                confidence=min(1.0, score),
                evidence=evidence,
                selection_reason=f"Scored {score:.2f}: trigger={trigger_depth:.2f} caps={cap_score:.2f} tools={tool_score:.2f}",
            )
            scored.append((score, match))

        scored.sort(key=lambda x: x[0], reverse=True)
        for rank, (_, match) in enumerate(scored):
            match.rank = rank + 1

        return [m for _, m in scored]

    @staticmethod
    def _trigger_depth(trigger: str, task_type: str) -> float:
        trigger_words = set(trigger.lower().split())
        task_lower = task_type.lower()
        if not trigger_words:
            return 0.5
        # Check if ANY trigger word appears in the full task text (not just word-by-word)
        word_hits = sum(1 for tw in trigger_words if tw in task_lower)
        if word_hits == 0:
            return 0.0
        # Also check substring matches for compound words
        for tw in trigger_words:
            if len(tw) >= 4 and tw in task_lower:
                word_hits += 0.5
        return min(1.0, word_hits / max(1, len(trigger_words)))

    @staticmethod
    def _capability_coverage(skill: Skill, required: list[str]) -> float:
        if not required:
            return 0.5
        skill_cap_names = {c.name.lower() for c in skill.capabilities}
        required_lower = {r.lower() for r in required}
        if not required_lower:
            return 0.5
        overlap = skill_cap_names & required_lower
        return len(overlap) / len(required_lower)

    @staticmethod
    def _tool_compatibility(skill: Skill, available: list[str]) -> float:
        if not skill.required_tools:
            return 1.0
        available_set = {t.lower() for t in available}
        required_set = {t.lower() for t in skill.required_tools}
        overlap = required_set & available_set
        return len(overlap) / len(required_set)
