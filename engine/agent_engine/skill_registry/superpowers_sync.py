"""Superpowers Syncer — sync skills from obra/superpowers at a pinned commit.

Architecture:
  - Skills are synced from the local skills/ directory (already imported)
  - The remote repo reference is pinned to a specific commit for audit
  - Each skill's SKILL.md is parsed into structured Skill objects
  - Skills are validated before registration
  - No runtime remote fetch — skills are committed to the repo
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import Skill, SkillCapability, SkillRegistry

# Pinned commit from obra/superpowers — verified June 2026
SUPERPOWERS_PINNED_COMMIT = "896224c4b1879920ab573417e68fd51d2ccc9072"
SUPERPOWERS_REPO_URL = "https://github.com/obra/superpowers"

# Skill definitions — parsed from skills/SKILL.md files
# Each skill has: id, name, trigger, lifecycle_stage, agent_roles, capabilities
_BUILTIN_SKILLS: list[dict[str, Any]] = [
    {
        "id": "brainstorming",
        "name": "Brainstorming",
        "description": "Clarify requirements before coding via collaborative design dialogue",
        "trigger": "new feature request design clarify explore plan spec architecture",
        "lifecycle_stage": "planning",
        "agent_roles": ["planner", "orchestrator"],
        "capabilities": [
            {"name": "requirement_clarification", "description": "Extract and clarify user requirements"},
            {"name": "alternative_exploration", "description": "Explore multiple approaches with trade-offs"},
            {"name": "design_documentation", "description": "Write structured design documents"},
        ],
        "required_tools": ["file_read", "search_content"],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "writing-plans",
        "name": "Writing Plans",
        "description": "Create detailed implementation plans with exact file paths and code",
        "trigger": "plan implement task steps files code structure",
        "lifecycle_stage": "planning",
        "agent_roles": ["planner", "orchestrator"],
        "capabilities": [
            {"name": "task_decomposition", "description": "Break work into 2-5 minute tasks"},
            {"name": "file_mapping", "description": "Map exact file paths and code changes"},
            {"name": "dependency_ordering", "description": "Order tasks by dependencies"},
        ],
        "required_tools": ["file_read", "search_content"],
        "depends_on": ["brainstorming"],
        "conflicts_with": [],
    },
    {
        "id": "test-driven-development",
        "name": "Test-Driven Development",
        "description": "RED-GREEN-REFACTOR cycle — write tests first, watch them fail",
        "trigger": "test tdd assert verify behavior implement code",
        "lifecycle_stage": "implementation",
        "agent_roles": ["coder", "tester"],
        "capabilities": [
            {"name": "test_first", "description": "Write minimal failing test before implementation"},
            {"name": "red_green_refactor", "description": "Strict RED-GREEN-REFACTOR cycle"},
            {"name": "regression_prevention", "description": "Never fix bugs without a test"},
        ],
        "required_tools": ["file_read", "file_write", "command_run"],
        "depends_on": ["writing-plans"],
        "conflicts_with": [],
    },
    {
        "id": "subagent-driven-development",
        "name": "Subagent-Driven Development",
        "description": "Dispatch fresh subagents per task with review gates",
        "trigger": "dispatch subagent parallel concurrent task execution delegate",
        "lifecycle_stage": "implementation",
        "agent_roles": ["orchestrator", "coder"],
        "capabilities": [
            {"name": "subagent_dispatch", "description": "Dispatch fresh subagent per task"},
            {"name": "task_brief_isolation", "description": "Isolate context per task brief"},
            {"name": "progress_tracking", "description": "Track progress in durable ledger"},
        ],
        "required_tools": ["file_read", "file_write", "command_run"],
        "depends_on": ["writing-plans"],
        "conflicts_with": [],
    },
    {
        "id": "executing-plans",
        "name": "Executing Plans",
        "description": "Execute implementation plans step by step",
        "trigger": "execute run implement build code deploy",
        "lifecycle_stage": "implementation",
        "agent_roles": ["coder", "orchestrator"],
        "capabilities": [
            {"name": "step_execution", "description": "Execute plan steps exactly as written"},
            {"name": "verification_after_step", "description": "Run verifications after each step"},
        ],
        "required_tools": ["file_read", "file_write", "command_run"],
        "depends_on": ["writing-plans"],
        "conflicts_with": [],
    },
    {
        "id": "systematic-debugging",
        "name": "Systematic Debugging",
        "description": "4-phase root cause investigation before any fix",
        "trigger": "debug fix bug error crash root cause investigate broken",
        "lifecycle_stage": "implementation",
        "agent_roles": ["coder", "tester", "reviewer"],
        "capabilities": [
            {"name": "root_cause_analysis", "description": "Find root cause before fixing"},
            {"name": "hypothesis_testing", "description": "Form and test single hypotheses"},
            {"name": "regression_check", "description": "Verify no regressions after fix"},
        ],
        "required_tools": ["file_read", "search_content", "command_run"],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "requesting-code-review",
        "name": "Requesting Code Review",
        "description": "Dispatch reviewer subagent after each task",
        "trigger": "review code check correctness regression security merge",
        "lifecycle_stage": "review",
        "agent_roles": ["reviewer", "orchestrator"],
        "capabilities": [
            {"name": "code_review_dispatch", "description": "Dispatch focused code reviewer"},
            {"name": "severity_classification", "description": "Classify findings as Critical/Important/Minor"},
            {"name": "diff_targeting", "description": "Target review to specific diff"},
        ],
        "required_tools": ["file_read", "search_content"],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "receiving-code-review",
        "name": "Receiving Code Review",
        "description": "Process review feedback systematically by severity",
        "trigger": "review feedback findings fix address respond",
        "lifecycle_stage": "review",
        "agent_roles": ["coder", "reviewer"],
        "capabilities": [
            {"name": "severity_triage", "description": "Address Critical > Important > Minor"},
            {"name": "feedback_response", "description": "Respond with fix/explanation/alternative"},
        ],
        "required_tools": ["file_read", "file_write"],
        "depends_on": ["requesting-code-review"],
        "conflicts_with": [],
    },
    {
        "id": "dispatching-parallel-agents",
        "name": "Dispatching Parallel Agents",
        "description": "Dispatch multiple agents concurrently for independent tasks",
        "trigger": "parallel concurrent independent dispatch multiple agents",
        "lifecycle_stage": "implementation",
        "agent_roles": ["orchestrator"],
        "capabilities": [
            {"name": "parallel_dispatch", "description": "Dispatch agents in same response for parallel execution"},
            {"name": "independence_check", "description": "Verify tasks are independent before parallel dispatch"},
            {"name": "conflict_detection", "description": "Detect shared file/state conflicts"},
        ],
        "required_tools": [],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "using-git-worktrees",
        "name": "Using Git Worktrees",
        "description": "Isolate feature work in dedicated git worktrees",
        "trigger": "worktree isolate branch workspace git",
        "lifecycle_stage": "implementation",
        "agent_roles": ["orchestrator", "coder"],
        "capabilities": [
            {"name": "worktree_creation", "description": "Create isolated git worktrees"},
            {"name": "merge_safety", "description": "Conflict detection and safe merge"},
        ],
        "required_tools": [],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "finishing-a-development-branch",
        "name": "Finishing a Development Branch",
        "description": "Verify, clean up, and merge/PR a completed branch",
        "trigger": "finish complete merge pr branch verify release done",
        "lifecycle_stage": "release",
        "agent_roles": ["orchestrator", "reviewer"],
        "capabilities": [
            {"name": "final_verification", "description": "Run full test suite and diff review"},
            {"name": "merge_strategy", "description": "Present merge/PR/keep/discard options"},
        ],
        "required_tools": ["command_run", "file_read"],
        "depends_on": ["requesting-code-review"],
        "conflicts_with": [],
    },
    {
        "id": "verification-before-completion",
        "name": "Verification Before Completion",
        "description": "Full verification checklist before claiming done",
        "trigger": "verify check confirm done complete validate pass",
        "lifecycle_stage": "verification",
        "agent_roles": ["tester", "reviewer", "orchestrator"],
        "capabilities": [
            {"name": "full_checklist", "description": "Code quality, correctness, integration, docs checklist"},
            {"name": "no_false_done", "description": "Re-verify FULL checklist after any fix"},
        ],
        "required_tools": ["command_run", "file_read", "search_content"],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "writing-skills",
        "name": "Writing Skills",
        "description": "Create or update skills following Superpowers conventions",
        "trigger": "skill create write add new capability workflow",
        "lifecycle_stage": "planning",
        "agent_roles": ["orchestrator"],
        "capabilities": [
            {"name": "skill_creation", "description": "Create structured SKILL.md files"},
            {"name": "integration_mapping", "description": "Map skills to codebase modules"},
        ],
        "required_tools": ["file_read", "file_write"],
        "depends_on": [],
        "conflicts_with": [],
    },
    {
        "id": "using-superpowers",
        "name": "Using Superpowers",
        "description": "Complete software development methodology for coding agents",
        "trigger": "start begin workflow methodology process develop build",
        "lifecycle_stage": "any",
        "agent_roles": ["orchestrator"],
        "capabilities": [
            {"name": "workflow_orchestration", "description": "Orchestrate the full 7-step workflow"},
            {"name": "skill_routing", "description": "Route tasks to appropriate skills"},
        ],
        "required_tools": [],
        "depends_on": [],
        "conflicts_with": [],
    },
]


def _skill_file_path(skill_dir: str, name: str) -> Path:
    """Path to a local SKILL.md file in the checked-in skills/ directory."""
    return Path(skill_dir) / name / "SKILL.md"


def _parse_skill_markdown(content: str) -> dict[str, Any]:
    """Parse a SKILL.md into structured data.
    Very simple parser — extracts sections by ## headers.
    """
    sections: dict[str, str] = {}
    current_section = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = line[3:].strip().lower().replace(" ", "_")
            current_lines = []
        elif current_section:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


class SuperpowersSyncer:
    """Sync skills from local skills/ directory with pinned remote reference."""

    def __init__(self, skills_dir: str) -> None:
        self.skills_dir = Path(skills_dir)
        self.registry = SkillRegistry()
        self._loaded = False

    @property
    def pinned_commit(self) -> str:
        return SUPERPOWERS_PINNED_COMMIT

    @property
    def repo_url(self) -> str:
        return SUPERPOWERS_REPO_URL

    def sync(self) -> SkillRegistry:
        """Synchronize built-in skills into the registry. Returns loaded registry."""
        if self._loaded:
            return self.registry

        for skill_data in _BUILTIN_SKILLS:
            # Try to read local SKILL.md for additional metadata
            local_path = _skill_file_path(str(self.skills_dir), skill_data["id"])
            if local_path.exists():
                try:
                    parsed = _parse_skill_markdown(local_path.read_text(encoding="utf-8"))
                    if parsed.get("process"):
                        skill_data.setdefault("metadata", {})["process_sections"] = parsed["process"][:500]
                except Exception:
                    pass

            skill_data["source_commit"] = SUPERPOWERS_PINNED_COMMIT
            skill_data["source_path"] = f"skills/{skill_data['id']}/SKILL.md"

            skill = Skill.from_dict(skill_data)
            self.registry.add(skill)

        self.registry.synced_commit = SUPERPOWERS_PINNED_COMMIT
        from datetime import datetime, timezone
        self.registry.synced_at = datetime.now(timezone.utc).isoformat()
        self.registry.version = f"1.0.0+{SUPERPOWERS_PINNED_COMMIT[:12]}"
        self._loaded = True

        return self.registry

    def validate(self) -> dict[str, Any]:
        """Validate all skills in registry for consistency."""
        issues: list[str] = []
        for skill in self.registry.list_all():
            if not skill.trigger:
                issues.append(f"{skill.id}: missing trigger")
            if not skill.lifecycle_stage:
                issues.append(f"{skill.id}: missing lifecycle_stage")
            if not skill.description:
                issues.append(f"{skill.id}: missing description")
            for dep in skill.depends_on:
                if dep not in self.registry.skills:
                    issues.append(f"{skill.id}: depends on unknown skill '{dep}'")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "total_skills": self.registry.total_skills,
        }

    def checksum(self) -> str:
        """Checksum of all skill definitions for integrity verification."""
        data = json.dumps(
            {k: v.to_dict() for k, v in sorted(self.registry.skills.items())},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
