"""Skill Registry domain models — no runtime deps on framework or DB."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SkillCapability:
    """A specific capability a skill provides."""
    name: str
    description: str
    required_tools: list[str] = field(default_factory=list)
    evidence_patterns: list[str] = field(default_factory=list)


@dataclass
class Skill:
    """A skill from the Superpowers registry, parsed from SKILL.md."""
    id: str
    name: str
    description: str
    trigger: str
    lifecycle_stage: str  # "planning" | "implementation" | "review" | "verification" | "release"
    agent_roles: list[str] = field(default_factory=list)
    capabilities: list[SkillCapability] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    conflicts_with: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    source_commit: str = ""
    source_path: str = ""  # relative path in skills/ directory
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "lifecycle_stage": self.lifecycle_stage,
            "agent_roles": self.agent_roles,
            "capabilities": [{"name": c.name, "description": c.description, "required_tools": c.required_tools} for c in self.capabilities],
            "required_tools": self.required_tools,
            "conflicts_with": self.conflicts_with,
            "depends_on": self.depends_on,
            "version": self.version,
            "source_commit": self.source_commit,
            "source_path": self.source_path,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        caps = [SkillCapability(**c) if isinstance(c, dict) else SkillCapability(name=str(c), description="") for c in data.get("capabilities", [])]
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            trigger=data.get("trigger", ""),
            lifecycle_stage=data.get("lifecycle_stage", "implementation"),
            agent_roles=data.get("agent_roles", []),
            capabilities=caps,
            required_tools=data.get("required_tools", []),
            conflicts_with=data.get("conflicts_with", []),
            depends_on=data.get("depends_on", []),
            version=data.get("version", "1.0.0"),
            source_commit=data.get("source_commit", ""),
            source_path=data.get("source_path", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class SkillEligibility:
    """Result of deterministic eligibility filter."""
    skill_id: str
    eligible: bool
    reason: str = ""
    checks: dict[str, bool] = field(default_factory=dict)


@dataclass
class SkillMatch:
    """A skill matched to a task with evidence."""
    skill_id: str
    task_id: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    rank: int = 0
    selection_reason: str = ""


@dataclass
class SkillSelection:
    """Result of skill routing for a task."""
    task_id: str
    selected_skills: list[SkillMatch]
    rejected_skills: list[SkillEligibility]
    confidence: float
    router_version: str = "1.0.0"
    deterministic: bool = True

    @property
    def no_match(self) -> bool:
        return len(self.selected_skills) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "selected_skills": [
                {"skill_id": s.skill_id, "confidence": s.confidence, "rank": s.rank, "reason": s.selection_reason}
                for s in self.selected_skills
            ],
            "rejected_skills": [
                {"skill_id": r.skill_id, "eligible": r.eligible, "reason": r.reason}
                for r in self.rejected_skills
            ],
            "confidence": self.confidence,
            "router_version": self.router_version,
            "deterministic": self.deterministic,
        }


@dataclass
class SkillRegistry:
    """Registry of skills with metadata."""
    version: str = "1.0.0"
    skills: dict[str, Skill] = field(default_factory=dict)
    synced_commit: str = ""
    synced_at: str = ""
    total_skills: int = 0

    def add(self, skill: Skill) -> None:
        self.skills[skill.id] = skill
        self.total_skills = len(self.skills)

    def get(self, skill_id: str) -> Skill | None:
        return self.skills.get(skill_id)

    def list_by_lifecycle(self, stage: str) -> list[Skill]:
        return [s for s in self.skills.values() if s.lifecycle_stage == stage]

    def list_by_role(self, role: str) -> list[Skill]:
        return [s for s in self.skills.values() if role in s.agent_roles]

    def list_all(self) -> list[Skill]:
        return list(self.skills.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "synced_commit": self.synced_commit,
            "synced_at": self.synced_at,
            "total_skills": self.total_skills,
            "skills": {k: v.to_dict() for k, v in self.skills.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillRegistry":
        reg = cls(
            version=data.get("version", "1.0.0"),
            synced_commit=data.get("synced_commit", ""),
            synced_at=data.get("synced_at", ""),
        )
        for sid, sdata in data.get("skills", {}).items():
            reg.add(Skill.from_dict(sdata))
        return reg
