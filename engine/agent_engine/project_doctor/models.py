from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class IssueGroup(str, Enum):
    CRITICAL = "critical"      # project fails to start
    LOGIC = "logic"            # business logic / state / race
    SECURITY = "security"      # secrets, auth, injection
    PERFORMANCE = "performance"
    UI_UX = "ui_ux"
    HYGIENE = "hygiene"        # lint / dead code / formatting


class IssueSeverity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


@dataclass
class Issue:
    id: str
    group: IssueGroup
    severity: IssueSeverity
    file: str
    line: int | None
    title: str
    detail: str
    root_cause: str = ""
    suggested_fix: str = ""
    autofix_safe: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["group"] = self.group.value
        data["severity"] = self.severity.value
        return data


@dataclass
class Patch:
    issue_id: str
    file: str
    old: str
    new: str
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanReport:
    project_root: str
    issues: list[Issue] = field(default_factory=list)
    counts_by_group: dict[str, int] = field(default_factory=dict)
    counts_by_severity: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)
        self.counts_by_group[issue.group.value] = self.counts_by_group.get(issue.group.value, 0) + 1
        self.counts_by_severity[issue.severity.value] = self.counts_by_severity.get(issue.severity.value, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectRoot": self.project_root,
            "issues": [i.to_dict() for i in self.issues],
            "countsByGroup": self.counts_by_group,
            "countsBySeverity": self.counts_by_severity,
            "durationMs": self.duration_ms,
        }


@dataclass
class FixReport:
    applied: list[Patch] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    streamed_chunks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": [p.to_dict() for p in self.applied],
            "skipped": self.skipped,
            "verification": self.verification,
            "streamedChunks": self.streamed_chunks,
        }
