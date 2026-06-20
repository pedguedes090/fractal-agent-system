"""Skill Registry — eligibility-based deterministic skill selection.

Architecture:
  - models.py: Skill, SkillEligibility, SkillMatch, SkillRegistry schema
  - skill_router.py: SkillRouter — two-tier selection (deterministic filter + evidence ranker)
  - superpowers_sync.py: SuperpowersSyncer — sync skills from obra/superpowers repo at pinned commit
"""

from .models import (
    Skill,
    SkillCapability,
    SkillEligibility,
    SkillMatch,
    SkillRegistry as SkillRegistryModel,
)
from .skill_router import SkillRouter, SkillSelection
from .superpowers_sync import SuperpowersSyncer, SUPERPOWERS_PINNED_COMMIT

__all__ = [
    "Skill",
    "SkillCapability",
    "SkillEligibility",
    "SkillMatch",
    "SkillRegistryModel",
    "SkillRouter",
    "SkillSelection",
    "SuperpowersSyncer",
    "SUPERPOWERS_PINNED_COMMIT",
]
