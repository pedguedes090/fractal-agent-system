"""Project Doctor — autonomous scan→plan→patch→verify pipeline.

Read the source tree, classify issues, write patches, re-run verification,
and stream every step via the standard `emit(stage, detail)` callback so
the HTTP/NDJSON pipeline and CLI both observe the same event tape.
"""

from __future__ import annotations

from .doctor import Doctor, run_doctor
from .models import FixReport, Issue, IssueGroup, IssueSeverity, Patch, ScanReport

__all__ = [
    "Doctor",
    "FixReport",
    "Issue",
    "IssueGroup",
    "IssueSeverity",
    "Patch",
    "ScanReport",
    "run_doctor",
]
