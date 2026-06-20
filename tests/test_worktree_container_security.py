from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openhands.tools.file_editor.definition import FileEditorAction

from agent_engine.container_sandbox import (
    PolicyFileEditorExecutor,
    _container_command,
    container_status,
    run_container_command,
)
from agent_engine.worktree_manager import (
    cleanup_execution_worktree,
    merge_execution_worktree,
    prepare_execution_worktree,
)


def git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


class WorktreeContainerSecurityTests(unittest.TestCase):
    def test_policy_file_editor_create_makes_allowed_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            executor = PolicyFileEditorExecutor(
                str(workspace),
                allowed_files=["vocabulary-app/**"],
                forbidden_paths=["**/.env"],
            )

            executor(
                FileEditorAction(
                    command="create",
                    path="vocabulary-app/src/hooks/useLocalStorage.js",
                    file_text="export const storageKey = 'vocabulary';\n",
                )
            )

            created = workspace / "vocabulary-app" / "src" / "hooks" / "useLocalStorage.js"
            self.assertEqual(created.read_text(encoding="utf-8"), "export const storageKey = 'vocabulary';\n")

    def test_policy_file_editor_does_not_create_disallowed_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            executor = PolicyFileEditorExecutor(
                str(workspace),
                allowed_files=["vocabulary-app/**"],
                forbidden_paths=[],
            )

            executor(
                FileEditorAction(
                    command="create",
                    path="outside/src/app.js",
                    file_text="blocked\n",
                )
            )

            self.assertFalse((workspace / "outside").exists())

    def test_empty_workspace_bootstraps_git_before_creating_worktree(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            workspace = Path(workspace_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                info = prepare_execution_worktree(str(workspace), "exec-empty-workspace")

                self.assertTrue(info["ready"])
                self.assertTrue(info["bootstrappedRepo"])
                self.assertTrue((workspace / ".git").is_dir())
                commit_count = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD"],
                    cwd=workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertEqual(commit_count, "1")

                worktree = Path(info["workspacePath"])
                (worktree / "app.txt").write_text("created by agent\n", encoding="utf-8")
                merged = merge_execution_worktree(info)

                self.assertEqual(merged["conflicts"], [])
                self.assertEqual(merged["policyViolations"], [])
                self.assertEqual((workspace / "app.txt").read_text(encoding="utf-8"), "created by agent\n")
                self.assertTrue(cleanup_execution_worktree(info)["removed"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_non_empty_workspace_without_git_remains_blocked(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            workspace = Path(workspace_dir)
            (workspace / "existing.txt").write_text("keep me\n", encoding="utf-8")
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                info = prepare_execution_worktree(str(workspace), "exec-non-git-workspace")

                self.assertFalse(info["ready"])
                self.assertIn("contains files", info["reason"])
                self.assertFalse((workspace / ".git").exists())
                self.assertEqual((workspace / "existing.txt").read_text(encoding="utf-8"), "keep me\n")
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_repository_without_commits_gets_initial_head_before_worktree(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as state_dir:
            workspace = Path(workspace_dir)
            git(workspace, "init")
            (workspace / "existing.txt").write_text("uncommitted baseline\n", encoding="utf-8")
            git(workspace, "add", "existing.txt")
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                missing_head = subprocess.run(
                    ["git", "rev-parse", "--verify", "HEAD"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(missing_head.returncode, 0)

                info = prepare_execution_worktree(str(workspace), "exec-unborn-head")

                self.assertTrue(info["ready"], info.get("reason"))
                self.assertTrue(info["initializedHead"])
                self.assertEqual(
                    (Path(info["workspacePath"]) / "existing.txt").read_text(encoding="utf-8"),
                    "uncommitted baseline\n",
                )
                head = subprocess.run(
                    ["git", "rev-parse", "--verify", "HEAD"],
                    cwd=workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertTrue(head)
                status = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertIn("A  existing.txt", status)
                committed_file = subprocess.run(
                    ["git", "cat-file", "-e", "HEAD:existing.txt"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(committed_file.returncode, 0)
                self.assertTrue(cleanup_execution_worktree(info)["removed"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_preserves_dirty_baseline_and_merges_only_reviewed_delta(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "app.txt").write_text("committed", encoding="utf-8")
                git(repo, "add", "app.txt")
                git(repo, "commit", "-m", "initial")
                (repo / "app.txt").write_text("dirty baseline", encoding="utf-8")
                (repo / "local.txt").write_text("untracked baseline", encoding="utf-8")

                info = prepare_execution_worktree(str(repo), "exec-worktree")
                self.assertTrue(info["ready"])
                worktree = Path(info["workspacePath"])
                self.assertEqual((worktree / "app.txt").read_text(encoding="utf-8"), "dirty baseline")
                self.assertEqual((worktree / "local.txt").read_text(encoding="utf-8"), "untracked baseline")

                (worktree / "app.txt").write_text("agent change", encoding="utf-8")
                (worktree / "new.txt").write_text("new", encoding="utf-8")
                merged = merge_execution_worktree(info)

                self.assertEqual(merged["conflicts"], [])
                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "agent change")
                self.assertEqual((repo / "new.txt").read_text(encoding="utf-8"), "new")
                self.assertTrue(cleanup_execution_worktree(info)["removed"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_merge_refuses_to_overwrite_concurrent_source_change(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "app.txt").write_text("base", encoding="utf-8")
                git(repo, "add", "app.txt")
                git(repo, "commit", "-m", "initial")
                info = prepare_execution_worktree(str(repo), "exec-conflict")
                worktree = Path(info["workspacePath"])
                (worktree / "app.txt").write_text("agent", encoding="utf-8")
                (repo / "app.txt").write_text("human", encoding="utf-8")

                merged = merge_execution_worktree(info)

                self.assertEqual(len(merged["conflicts"]), 1)
                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "human")
                cleanup_execution_worktree(info)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_excludes_sensitive_files_and_final_merge_rechecks_policy(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "app.txt").write_text("base", encoding="utf-8")
                (repo / ".env").write_text("TOKEN=do-not-copy", encoding="utf-8")
                git(repo, "add", "app.txt", ".env")
                git(repo, "commit", "-m", "initial")

                info = prepare_execution_worktree(str(repo), "exec-sensitive")
                worktree = Path(info["workspacePath"])
                self.assertFalse((worktree / ".env").exists())

                (worktree / "app.txt").write_text("agent", encoding="utf-8")
                (worktree / "outside.txt").write_text("blocked", encoding="utf-8")
                (worktree / ".env").write_text("TOKEN=agent", encoding="utf-8")
                merged = merge_execution_worktree(
                    info,
                    allowed_patterns=["app.txt"],
                    forbidden_patterns=[".env"],
                )

                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base")
                self.assertEqual((repo / ".env").read_text(encoding="utf-8"), "TOKEN=do-not-copy")
                self.assertFalse((repo / "outside.txt").exists())
                self.assertEqual(
                    {item["path"] for item in merged["policyViolations"]},
                    {".env", "outside.txt"},
                )
                cleanup_execution_worktree(info)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_worktree_excludes_runtime_state_dir_inside_workspace_and_git_file_from_diff(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = str(repo / ".agent-state-real")
            try:
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.com")
                git(repo, "config", "user.name", "Test")
                (repo / "README.md").write_text("base\n", encoding="utf-8")
                (repo / ".agent-state-real").mkdir()
                (repo / ".agent-state-real" / "debug.sqlite").write_text("runtime", encoding="utf-8")
                git(repo, "add", "README.md")
                git(repo, "commit", "-m", "initial")

                info = prepare_execution_worktree(str(repo), "exec-runtime-state")
                worktree = Path(info["workspacePath"])
                self.assertFalse((worktree / ".agent-state-real").exists())

                (worktree / "app.txt").write_text("agent", encoding="utf-8")
                merged = merge_execution_worktree(info, allowed_patterns=["app.txt"])

                self.assertEqual(merged["policyViolations"], [])
                self.assertEqual([item["path"] for item in merged["applied"]], ["app.txt"])
                self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "agent")
                cleanup_execution_worktree(info)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    def test_container_command_has_hardening_flags_and_no_network(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            args = _container_command(
                runtime="docker",
                image="python:3.12-slim",
                workspace=workspace,
                command="pytest",
                cwd=".",
                dependency_workspace=None,
                allow_pull=False,
            )
        joined = " ".join(map(str, args))
        self.assertIn("--network none", joined)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        self.assertIn("--pull never", joined)
        self.assertEqual(args[-3:], ["sh", "-lc", "pytest"])

    def test_container_execution_is_fail_closed_without_runtime(self) -> None:
        with mock.patch("agent_engine.container_sandbox.detect_container_runtime", return_value=None):
            status = container_status("python")
            result = run_container_command(".", "python -m pytest", stack="python")

        self.assertFalse(status["ready"])
        self.assertFalse(result["sandboxed"])
        self.assertIsNone(result["code"])
        self.assertIn("Docker or Podman", result["stderr"])

    def test_container_execution_rejects_cwd_escape_before_runtime(self) -> None:
        with mock.patch("agent_engine.container_sandbox.container_status") as status:
            result = run_container_command(".", "pytest", cwd="../outside", stack="python")

        status.assert_not_called()
        self.assertFalse(result["sandboxed"])
        self.assertIn("escapes", result["stderr"])


if __name__ == "__main__":
    unittest.main()
