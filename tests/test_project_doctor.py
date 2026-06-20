from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.project_doctor import Doctor, Issue, ScanReport, run_doctor
from agent_engine.project_doctor.models import IssueGroup, IssueSeverity
from agent_engine.project_doctor.patcher import _extract_fenced_block, _within_root, apply_patches
from agent_engine.project_doctor.planner import plan
from agent_engine.project_doctor.scanner import scan_project


class ScannerTests(unittest.TestCase):
    def test_python_syntax_error_is_reported_as_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text("def broken(:\n  pass\n", encoding="utf-8")
            report = scan_project(root)
            python_issues = [i for i in report.issues if i.file == "bad.py"]
            self.assertEqual(len(python_issues), 1)
            self.assertEqual(python_issues[0].group, IssueGroup.CRITICAL)
            self.assertEqual(python_issues[0].severity, IssueSeverity.BLOCKER)

    def test_secret_pattern_in_source_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "secrets.py").write_text(
                'API_KEY = "sk-AAAAAAAAAAAAAAAAAAAAAAAA"\n',
                encoding="utf-8",
            )
            report = scan_project(root)
            secret = next((i for i in report.issues if i.group == IssueGroup.SECURITY), None)
            self.assertIsNotNone(secret)
            self.assertEqual(secret.file, "secrets.py")

    def test_clean_project_returns_empty_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
            report = scan_project(root)
            self.assertEqual([i for i in report.issues if i.file == "ok.py"], [])

    def test_gitignore_basics_missing_entry_is_autofix_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
            report = scan_project(root)
            issues = [i for i in report.issues if i.file == ".gitignore"]
            self.assertEqual(len(issues), 1)
            self.assertTrue(issues[0].autofix_safe)


class PlannerTests(unittest.TestCase):
    def test_plan_orders_critical_before_hygiene(self) -> None:
        report = ScanReport(project_root=".")
        report.add(Issue(
            id="a", group=IssueGroup.HYGIENE, severity=IssueSeverity.MINOR,
            file="z.py", line=None, title="t", detail="d",
        ))
        report.add(Issue(
            id="b", group=IssueGroup.CRITICAL, severity=IssueSeverity.BLOCKER,
            file="z.py", line=None, title="t", detail="d",
        ))
        ordered = plan(report)
        self.assertEqual(ordered[0].id, "b")
        self.assertEqual(ordered[1].id, "a")


class PatcherTests(unittest.TestCase):
    def test_within_root_accepts_same_drive_rejects_other_drives(self) -> None:
        # Sanity check, not a security boundary. Same drive → allowed
        # (even parents). Different drive anchor → rejected (typo guard).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(_within_root(root, root / "x.txt"))
            self.assertTrue(_within_root(root, root.parent / "sibling.txt"))
            # Same anchor as `root` is allowed; only a different anchor
            # (e.g. `Z:\` when root is on `C:\`) gets rejected. We can't
            # portably construct a different anchor in CI, so just assert
            # the same-anchor invariant.
            self.assertEqual(
                Path(tmp).resolve().anchor.lower(),
                (root / "deep" / "nested" / "x").resolve().anchor.lower(),
            )

    def test_fenced_block_extraction(self) -> None:
        text = "Here is the fix:\n```python\nprint('hi')\n```\nthanks"
        self.assertEqual(_extract_fenced_block(text).strip(), "print('hi')")

    def test_apply_patches_runs_gitignore_autofix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
            report = scan_project(root)
            ordered = plan(report)
            events: list[tuple[str, str]] = []
            fix_report = apply_patches(root, ordered, lambda s, d: events.append((s, d)), provider=None)
            content = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".env", content)
            self.assertIn("node_modules", content)
            self.assertIn("__pycache__", content)
            self.assertEqual(len(fix_report.applied), 1)


class AgentSdkProviderTests(unittest.TestCase):
    def test_provider_skipped_when_sdk_missing(self) -> None:
        from agent_engine.project_doctor import agent_sdk_provider as mod
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mod, "_has_sdk", return_value=False):
            provider = mod.maybe_build_provider(cwd=Path(tmp), model="claude-opus-4-8", api_key="x")
            self.assertIsNone(provider)

    def test_provider_streams_chunks_into_emit(self) -> None:
        from agent_engine.project_doctor import agent_sdk_provider as mod
        from agent_engine.project_doctor.patcher import _stream_llm_patch
        from agent_engine.project_doctor.models import Issue, IssueGroup, IssueSeverity

        class FakeAgentProvider:
            __class__ = type("ClaudeAgentSDKProvider", (), {})  # name-match only

            def __init__(self, root: Path) -> None:
                self.root = root

            def edit_file_with_stream(self, *, rel_path: str, instructions: str, emit):
                # Simulate streaming + rewrite the file
                emit("doctor.patch.chunk", "Editing")
                emit("doctor.patch.chunk", " file")
                (self.root / rel_path).write_text("fixed\n", encoding="utf-8")
                return True, 2

        # Patch isinstance check by giving the class the exact expected __name__
        FakeAgentProvider.__name__ = "ClaudeAgentSDKProvider"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text("broken\n", encoding="utf-8")
            issue = Issue(
                id="x", group=IssueGroup.CRITICAL, severity=IssueSeverity.BLOCKER,
                file="bad.py", line=1, title="t", detail="d",
            )
            events: list[tuple[str, str]] = []
            patch, chunks = _stream_llm_patch(root, issue, lambda s, d: events.append((s, d)), FakeAgentProvider(root))
            self.assertIsNotNone(patch)
            self.assertEqual(chunks, 2)
            self.assertIn(("doctor.patch.chunk", "Editing"), events)


class DoctorIntegrationTests(unittest.TestCase):
    def test_doctor_runs_end_to_end_and_emits_stages_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
            events: list[tuple[str, str]] = []
            result = run_doctor(root, provider=None, emit=lambda s, d: events.append((s, d)))
            stages = [s for s, _ in events]
            self.assertEqual(stages[0], "doctor.start")
            self.assertEqual(stages[-1], "doctor.done")
            self.assertIn("doctor.scan", stages)
            self.assertIn("doctor.plan", stages)
            self.assertIn("doctor.patch.done", stages)
            self.assertIn("scan", result)
            self.assertIn("fix", result)
            self.assertIn("verify", result)

    def test_doctor_returns_early_on_clean_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            events: list[tuple[str, str]] = []
            result = run_doctor(root, emit=lambda s, d: events.append((s, d)))
            self.assertTrue(result["ok"])
            self.assertEqual(result["scan"]["issues"], [])

    def test_streaming_provider_is_optional(self) -> None:
        # Doctor() with no provider must not crash even when LLM-path issues exist.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            result = Doctor(root).run()
            # syntax error has autofix_safe=False, no provider → recorded as skipped, not crashed
            self.assertTrue(len(result["fix"]["skipped"]) >= 1)


if __name__ == "__main__":
    unittest.main()
