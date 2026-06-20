from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.workspace import (
    create_workspace_sandbox,
    enforce_change_policy,
    file_snapshots,
    normalize_verification_commands,
    run_command,
    run_setup_commands,
)


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        self._temp_state = tempfile.TemporaryDirectory()
        os.environ["AGENT_ENGINE_STATE_DIR"] = self._temp_state.name

    def tearDown(self) -> None:
        if self._old_state_dir is None:
            os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
        else:
            os.environ["AGENT_ENGINE_STATE_DIR"] = self._old_state_dir
        self._temp_state.cleanup()

    def test_enforce_change_policy_rolls_back_outside_allowed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "allowed.txt").write_text("before", encoding="utf-8")
            (root / "secret.txt").write_text("original secret", encoding="utf-8")
            before = file_snapshots(str(root))

            (root / "allowed.txt").write_text("after", encoding="utf-8")
            (root / "secret.txt").write_text("leaked", encoding="utf-8")

            result = enforce_change_policy(str(root), before, ["allowed.txt"])

            self.assertEqual((root / "secret.txt").read_text(encoding="utf-8"), "original secret")
            self.assertEqual(
                result["sandboxDiff"],
                [
                    {"path": "allowed.txt", "status": "modified"},
                    {"path": "secret.txt", "status": "modified"},
                ],
            )
            self.assertEqual(result["changedFiles"], [{"path": "allowed.txt", "status": "modified"}])
            self.assertEqual(result["violations"][0]["path"], "secret.txt")

    def test_enforce_change_policy_allows_nested_project_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = file_snapshots(str(root))

            (root / "todo-app").mkdir()
            (root / "todo-app" / "package.json").write_text('{"scripts":{"build":"echo ok"}}', encoding="utf-8")
            (root / "todo-app" / "index.html").write_text("<main>Todo</main>", encoding="utf-8")

            result = enforce_change_policy(str(root), before, ["todo-app/**"])

            self.assertEqual(result["violations"], [])
            self.assertEqual(
                result["changedFiles"],
                [
                    {"path": "todo-app/index.html", "status": "created"},
                    {"path": "todo-app/package.json", "status": "created"},
                ],
            )

    def test_run_command_skips_dev_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_command(temp_dir, "npm start")

        self.assertTrue(result["skipped"])
        self.assertIn("allowlist", result["reason"])

    def test_normalize_verification_commands_filters_long_running_servers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text('{"scripts":{"build":"node --check src/index.js"}}', encoding="utf-8")
            commands = normalize_verification_commands(str(root), ["npm run dev", "npm run build"], spec={})

        self.assertEqual(commands, [{"cwd": ".", "command": "npm run build"}])

    def test_normalize_verification_commands_uses_verification_cwd_for_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "todo-app").mkdir()
            (root / "todo-app" / "package.json").write_text('{"scripts":{"build":"echo ok"}}', encoding="utf-8")
            commands = normalize_verification_commands(
                str(root),
                ["npm run build"],
                spec={"verificationCwd": "todo-app", "targetProjectDir": "todo-app"},
            )

        self.assertEqual(commands, [{"cwd": "todo-app", "command": "npm run build"}])

    def test_normalize_verification_commands_skips_missing_package_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text(
                '{"scripts":{"build":"node scripts/build.js"}}',
                encoding="utf-8",
            )
            commands = normalize_verification_commands(
                str(root),
                ["npm test", "npm run build"],
                spec={},
            )

        self.assertEqual(commands, [{"cwd": ".", "command": "npm run build"}])

    def test_setup_commands_run_install_inside_opened_target_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "vocabulary-app"
            project.mkdir()
            (project / "package.json").write_text('{"name":"vocabulary-app"}', encoding="utf-8")
            completed = mock.Mock(returncode=0, stdout="installed", stderr="")
            with mock.patch("agent_engine.workspace.subprocess.run", return_value=completed) as run:
                results = run_setup_commands(
                    str(root),
                    ["cd vocabulary-app", "npm install"],
                    target_project_dir="vocabulary-app",
                )

        self.assertEqual(results[-1]["code"], 0)
        self.assertTrue(results[-1]["directWorkspace"])
        self.assertEqual(str(Path(run.call_args.kwargs["cwd"]).resolve()), str(project.resolve()))

    def test_setup_commands_reject_shell_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("agent_engine.workspace.subprocess.run") as run:
                results = run_setup_commands(temp_dir, ["npm install && echo unsafe"])

        run.assert_not_called()
        self.assertTrue(results[0]["skipped"])
        self.assertIn("allowlist", results[0]["reason"])

    def test_workspace_sandbox_cleans_up_after_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "file.txt").write_text("content", encoding="utf-8")
            with create_workspace_sandbox(str(root)) as sandbox_dir:
                sandbox_path = Path(sandbox_dir)
                self.assertTrue((sandbox_path / "workspace" / "file.txt").exists())

            self.assertFalse(sandbox_path.exists())


if __name__ == "__main__":
    unittest.main()
