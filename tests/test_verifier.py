from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.verifier import (
    Verdict,
    VerificationCheck,
    Verifier,
    VerifierConfig,
)


class VerdictTests(unittest.TestCase):
    def test_add_blocker_updates_passed_to_false(self) -> None:
        verdict = Verdict()
        self.assertTrue(verdict.passed)
        verdict.add_blocker("something broke", "blocker_check")
        self.assertFalse(verdict.passed)
        self.assertIn("something broke", verdict.blockers)
        self.assertEqual(len(verdict.checks), 1)
        self.assertEqual(verdict.checks[0].name, "blocker_check")
        self.assertFalse(verdict.checks[0].passed)

    def test_add_warning_does_not_change_passed(self) -> None:
        verdict = Verdict()
        self.assertTrue(verdict.passed)
        verdict.add_warning("look out", "warning_check")
        self.assertTrue(verdict.passed)
        self.assertIn("look out", verdict.warnings)
        self.assertEqual(len(verdict.checks), 1)
        self.assertEqual(verdict.checks[0].name, "warning_check")
        self.assertTrue(verdict.checks[0].passed)

    def test_add_blocker_empty_name_omits_check_entry(self) -> None:
        verdict = Verdict()
        verdict.add_blocker("block", "")
        self.assertIn("block", verdict.blockers)
        self.assertEqual(len(verdict.checks), 0)

    def test_add_warning_empty_name_omits_check_entry(self) -> None:
        verdict = Verdict()
        verdict.add_warning("warn", "")
        self.assertIn("warn", verdict.warnings)
        self.assertEqual(len(verdict.checks), 0)

    def test_to_dict_serialization(self) -> None:
        verdict = Verdict()
        verdict.add_blocker("alpha", "block_alpha")
        verdict.add_warning("beta", "warn_beta")
        verdict.command_results.append({"command": "make test", "code": 0})
        verdict.affected_tests.append("test_thing.py")

        d = verdict.to_dict()
        self.assertFalse(d["passed"])
        self.assertEqual(d["blockers"], ["alpha"])
        self.assertEqual(d["warnings"], ["beta"])
        self.assertEqual(len(d["checks"]), 2)
        self.assertEqual(d["checks"][0]["name"], "block_alpha")
        self.assertFalse(d["checks"][0]["passed"])
        self.assertTrue(d["checks"][0]["is_blocker"])
        self.assertEqual(d["checks"][1]["name"], "warn_beta")
        self.assertTrue(d["checks"][1]["passed"])
        self.assertFalse(d["checks"][1]["is_blocker"])
        self.assertEqual(len(d["command_results"]), 1)
        self.assertEqual(d["command_results"][0]["command"], "make test")
        self.assertEqual(d["affected_tests"], ["test_thing.py"])

    def test_to_dict_defaults(self) -> None:
        verdict = Verdict()
        d = verdict.to_dict()
        self.assertTrue(d["passed"])
        self.assertEqual(d["blockers"], [])
        self.assertEqual(d["warnings"], [])
        self.assertEqual(d["checks"], [])
        self.assertEqual(d["command_results"], [])
        self.assertEqual(d["affected_tests"], [])


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_config(self, **overrides) -> VerifierConfig:
        defaults = {
            "skip_build": True,
            "skip_test": True,
            "timeout_per_command": 10,
        }
        defaults.update(overrides)
        return VerifierConfig(**defaults)

    # ---- verify(): all checks pass ----
    def test_verify_all_checks_pass(self) -> None:
        # Create a file that will be marked as created
        p = Path(self.workspace) / "src" / "foo.py"
        p.parent.mkdir(exist_ok=True)
        p.write_text("# hello", encoding="utf-8")

        config = self._make_config(skip_build=True, skip_test=True)
        verifier = Verifier(config)
        changed_files = [{"path": "src/foo.py", "status": "created"}]
        plan = {"files": ["src/foo.py"], "allowedFiles": ["src/*"]}
        worker_result = {}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result=worker_result,
        )
        self.assertTrue(verdict.passed)
        self.assertEqual(len(verdict.blockers), 0)

    # ---- verify(): tool error → blocker ----
    def test_verify_tool_error_becomes_blocker(self) -> None:
        config = self._make_config()
        verifier = Verifier(config)
        worker_result = {"error": "API call failed with 500"}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=[],
            plan={},
            worker_result=worker_result,
        )
        self.assertFalse(verdict.passed)
        self.assertTrue(any("API call failed" in b for b in verdict.blockers))
        self.assertTrue(any(c.name == "tool_error" for c in verdict.checks))

    # ---- verify(): file not created → blocker ----
    def test_verify_file_not_created_becomes_blocker(self) -> None:
        config = self._make_config()
        verifier = Verifier(config)
        changed_files = [
            {"path": "missing_file.py", "status": "created"},
            {"path": "also_missing.py", "status": "modified"},
        ]
        plan = {}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result={},
        )
        self.assertFalse(verdict.passed)
        self.assertTrue(any("missing_file.py" in b for b in verdict.blockers))
        self.assertTrue(any("also_missing.py" in b for b in verdict.blockers))
        self.assertTrue(any(c.name == "file_created" for c in verdict.checks))
        self.assertTrue(any(c.name == "file_modified" for c in verdict.checks))

    # ---- verify(): file exists passes check ----
    def test_verify_existing_created_file_passes(self) -> None:
        p = Path(self.workspace) / "real.py"
        p.write_text("ok", encoding="utf-8")

        config = self._make_config()
        verifier = Verifier(config)
        changed_files = [{"path": "real.py", "status": "created"}]
        plan = {}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result={},
        )
        self.assertTrue(verdict.passed)

    # ---- verify(): scope violation → blocker ----
    def test_verify_scope_violation_becomes_blocker(self) -> None:
        # Create a file outside allowed patterns
        p = Path(self.workspace) / "secret" / "env.txt"
        p.parent.mkdir(exist_ok=True)
        p.write_text("secret", encoding="utf-8")

        config = self._make_config()
        verifier = Verifier(config)
        changed_files = [
            {"path": "secret/env.txt", "status": "created"},
            {"path": "src/main.py", "status": "created"},
        ]
        plan = {"allowedFiles": ["src/*"]}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result={},
        )
        self.assertFalse(verdict.passed)
        self.assertTrue(any("secret/env.txt" in b for b in verdict.blockers))
        self.assertTrue(any(c.name == "scope_violation" for c in verdict.checks))

    # ---- verify(): no allowedFiles means no scope check ----
    def test_verify_no_allowed_files_no_scope_blocker(self) -> None:
        p = Path(self.workspace) / "anything.txt"
        p.write_text("ok", encoding="utf-8")

        config = self._make_config()
        verifier = Verifier(config)
        changed_files = [{"path": "anything.txt", "status": "created"}]
        plan = {}  # no allowedFiles

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result={},
        )
        self.assertTrue(verdict.passed)

    # ---- verify(): skip_build=True does not run build ----
    def test_verify_skip_build_does_not_run_build(self) -> None:
        p = Path(self.workspace) / "foo.py"
        p.write_text("ok", encoding="utf-8")

        config = self._make_config(skip_build=True)
        verifier = Verifier(config)
        changed_files = [{"path": "foo.py", "status": "created"}]
        plan = {}
        worker_result = {}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result=worker_result,
        )
        # No build-related command results recorded
        build_commands = [r for r in verdict.command_results if "compile" in str(r.get("command", ""))]
        self.assertEqual(len(build_commands), 0)

    # ---- verify(): skip_test=True does not run tests ----
    def test_verify_skip_test_does_not_run_tests(self) -> None:
        config = self._make_config(skip_test=True)
        verifier = Verifier(config)
        plan = {"workerTaskSpec": {"verificationCommands": ["echo test"]}}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=[],
            plan=plan,
            worker_result={},
        )
        self.assertTrue(verdict.passed)
        self.assertEqual(len(verdict.command_results), 0)

    # ---- verify(): build runs for python files ----
    @mock.patch("subprocess.run")
    def test_verify_build_runs_for_python_files(self, mock_run: mock.MagicMock) -> None:
        p = Path(self.workspace) / "foo.py"
        p.write_text("ok", encoding="utf-8")

        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Compiled ok"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        config = self._make_config(skip_build=False, skip_test=True)
        verifier = Verifier(config)
        changed_files = [{"path": "foo.py", "status": "created"}]
        plan = {}
        worker_result = {}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result=worker_result,
        )
        # Either build passes or the command was attempted
        self.assertTrue(verdict.passed)
        mock_run.assert_called()

    # ---- verify(): tests run commands from plan ----
    @mock.patch("subprocess.run")
    def test_verify_tests_run_verification_commands(self, mock_run: mock.MagicMock) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "tests passed"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        config = self._make_config(skip_test=False)
        verifier = Verifier(config)
        plan = {"workerTaskSpec": {"verificationCommands": ["pytest tests/ -q"]}}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=[],
            plan=plan,
            worker_result={},
        )
        self.assertTrue(verdict.passed)
        mock_run.assert_called()
        self.assertGreaterEqual(len(verdict.command_results), 1)
        self.assertEqual(verdict.command_results[0]["code"], 0)

    # ---- _check_acceptance(): criteria match changed files ----
    def test_check_acceptance_criteria_match_changed_files(self) -> None:
        p = Path(self.workspace) / "src" / "login.py"
        p.parent.mkdir(exist_ok=True)
        p.write_text("ok", encoding="utf-8")

        emitted = []

        def emit(kind: str, msg: str) -> None:
            emitted.append((kind, msg))

        config = self._make_config()
        verifier = Verifier(config)
        changed_files = [{"path": "src/login.py", "status": "created"}]
        plan = {"acceptanceCriteria": ["must touch src/login.py", "must pass security review"]}
        worker_result = {}

        # _check_acceptance is called inside verify() but its logic is per-criterion.
        # Call it directly for targeted testing.
        verdict = Verdict()
        verifier._check_acceptance(
            workspace=self.workspace,
            changed_files=changed_files,
            plan=plan,
            worker_result=worker_result,
            verdict=verdict,
            emit=emit,
        )
        # No blockers from _check_acceptance (it only emits / passes).
        self.assertTrue(verdict.passed)
        # At least one emit for the matching criterion.
        self.assertTrue(any("src/login.py" in m for _, m in emitted))

    # ---- _check_acceptance(): no criteria = no-op ----
    def test_check_acceptance_no_criteria_no_op(self) -> None:
        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        verifier._check_acceptance(
            workspace=self.workspace,
            changed_files=[],
            plan={},
            worker_result={},
            verdict=verdict,
            emit=None,
        )
        self.assertTrue(verdict.passed)

    # ---- _run_verify_command(): success ----
    @mock.patch("subprocess.run")
    def test_run_verify_command_success(self, mock_run: mock.MagicMock) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "all good"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        check = verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="my_check",
            command="echo hello",
            is_blocker=True,
            emit=None,
        )
        self.assertTrue(check.passed)
        self.assertEqual(check.name, "my_check")
        self.assertTrue(check.is_blocker)
        self.assertGreaterEqual(check.duration_ms, 0)
        self.assertEqual(len(verdict.command_results), 1)
        self.assertEqual(verdict.command_results[0]["command"], "echo hello")

        mock_run.assert_called_once_with(
            "echo hello",
            cwd=str(self.workspace),
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    # ---- _run_verify_command(): failure as blocker ----
    @mock.patch("subprocess.run")
    def test_run_verify_command_failure_is_blocker(self, mock_run: mock.MagicMock) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "error occurred"
        mock_run.return_value = mock_proc

        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        check = verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="fail_check",
            command="make fail",
            is_blocker=True,
            emit=None,
        )
        self.assertFalse(check.passed)
        self.assertFalse(verdict.passed)
        self.assertIn("fail_check", verdict.blockers[0])

    # ---- _run_verify_command(): failure as warning (non-blocker) ----
    @mock.patch("subprocess.run")
    def test_run_verify_command_failure_is_warning_when_not_blocker(self, mock_run: mock.MagicMock) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "warning: deprecated"
        mock_run.return_value = mock_proc

        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        check = verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="warn_check",
            command="make warn",
            is_blocker=False,
            emit=None,
        )
        self.assertFalse(check.passed)
        self.assertTrue(verdict.passed)  # warning, not blocker
        self.assertEqual(len(verdict.warnings), 1)
        self.assertIn("warn_check", verdict.warnings[0])

    # ---- _run_verify_command(): timeout ----
    @mock.patch("subprocess.run")
    def test_run_verify_command_timeout(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="slow", timeout=10)

        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        check = verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="timeout_check",
            command="slow",
            is_blocker=True,
            emit=None,
        )
        self.assertFalse(check.passed)
        self.assertFalse(verdict.passed)
        self.assertIn("timed out", check.detail)
        self.assertTrue(any(r.get("timed_out") for r in verdict.command_results))

    # ---- _run_verify_command(): exception ----
    @mock.patch("subprocess.run")
    def test_run_verify_command_exception(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = OSError("file not found")

        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        check = verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="crash_check",
            command="nonexistent",
            is_blocker=True,
            emit=None,
        )
        self.assertFalse(check.passed)
        self.assertIn("file not found", check.detail)

    # ---- _run_verify_command(): empty command returns early ----
    def test_run_verify_command_empty_string_returns_early(self) -> None:
        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        check = verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="noop",
            command="",
            is_blocker=True,
            emit=None,
        )
        self.assertTrue(check.passed)
        self.assertEqual(check.detail, "no command to run")

    # ---- _run_verify_command(): emit callback ----
    @mock.patch("subprocess.run")
    def test_run_verify_command_emits_on_success(self, mock_run: mock.MagicMock) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "ok"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        emitted = []

        def emit(kind: str, msg: str) -> None:
            emitted.append((kind, msg))

        config = self._make_config()
        verifier = Verifier(config)
        verdict = Verdict()
        verifier._run_verify_command(
            workspace=Path(self.workspace),
            verdict=verdict,
            name="emit_check",
            command="echo ok",
            is_blocker=True,
            emit=emit,
        )
        self.assertEqual(len(emitted), 1)
        kind, msg = emitted[0]
        self.assertEqual(kind, "verify")
        self.assertIn("PASS", msg)

    # ---- verify(): plan_files_unchanged warning ----
    def test_verify_plan_files_unchanged_warning(self) -> None:
        config = self._make_config()
        verifier = Verifier(config)
        plan = {"files": ["expected.py", "expected2.py"]}

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=[],
            plan=plan,
            worker_result={},
        )
        self.assertTrue(verdict.passed)  # warning only
        self.assertTrue(any("expected.py" in w for w in verdict.warnings))
        self.assertTrue(any(c.name == "plan_files_unchanged" for c in verdict.checks))

    # ---- verify(): no_changes warning when no files ----
    def test_verify_no_changes_warning(self) -> None:
        config = self._make_config()
        verifier = Verifier(config)

        verdict = verifier.verify(
            workspace=self.workspace,
            changed_files=[],
            plan={},
            worker_result={},
        )
        self.assertTrue(verdict.passed)  # warning only
        self.assertTrue(any("No files were changed" in w for w in verdict.warnings))

    # ---- VerifierConfig.from_env ----
    @mock.patch.dict("os.environ", {
        "VERIFIER_SKIP_BUILD": "1",
        "VERIFIER_SKIP_TEST": "true",
        "VERIFIER_TIMEOUT": "60",
    })
    def test_verifier_config_from_env(self) -> None:
        config = VerifierConfig.from_env()
        self.assertTrue(config.skip_build)
        self.assertTrue(config.skip_test)
        self.assertEqual(config.timeout_per_command, 60)

    # ---- VerificationCheck dataclass ----
    def test_verification_check_defaults(self) -> None:
        check = VerificationCheck(name="test", passed=True)
        self.assertEqual(check.detail, "")
        self.assertTrue(check.is_blocker)
        self.assertEqual(check.duration_ms, 0.0)

