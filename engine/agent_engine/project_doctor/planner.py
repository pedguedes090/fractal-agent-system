"""Patch planner — ranks Issue records into a deterministic fix order.

Order follows the user's directive:
  1. Crash blockers
  2. Build / install failures
  3. Runtime crashes
  4. Logic bugs
  5. Security
  6. Performance
  7. UI/UX
  8. Hygiene
"""

from __future__ import annotations

from .models import Issue, IssueGroup, IssueSeverity, ScanReport

_GROUP_RANK = {
    IssueGroup.CRITICAL: 0,
    IssueGroup.LOGIC: 1,
    IssueGroup.SECURITY: 2,
    IssueGroup.PERFORMANCE: 3,
    IssueGroup.UI_UX: 4,
    IssueGroup.HYGIENE: 5,
}

_SEVERITY_RANK = {
    IssueSeverity.BLOCKER: 0,
    IssueSeverity.MAJOR: 1,
    IssueSeverity.MINOR: 2,
}


def plan(report: ScanReport) -> list[Issue]:
    """Return issues sorted by (group_rank, severity_rank, file)."""
    return sorted(
        report.issues,
        key=lambda issue: (
            _GROUP_RANK.get(issue.group, 99),
            _SEVERITY_RANK.get(issue.severity, 99),
            issue.file,
            issue.line or 0,
        ),
    )
