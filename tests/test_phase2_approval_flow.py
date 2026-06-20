from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from agent_engine.graph import _normalize_verification_result, _sanitize_review_claims, run_pipeline


class Phase2ApprovalFlowTests(unittest.TestCase):
    def test_missing_jest_is_verification_unavailable_not_test_failure(self) -> None:
        result = _normalize_verification_result(
            {
                "command": "npm test",
                "cwd": "vocabulary-app",
                "code": 1,
                "stdout": "",
                "stderr": "'jest' is not recognized as an internal or external command",
                "timedOut": False,
                "sandboxed": True,
            }
        )

        self.assertTrue(result["skipped"])
        self.assertTrue(result["verificationUnavailable"])
        self.assertEqual(result["originalCode"], 1)

    def test_missing_dependency_review_claims_are_downgraded_to_warnings(self) -> None:
        command_result = _normalize_verification_result(
            {
                "command": "npm test",
                "cwd": "vocabulary-app",
                "code": 1,
                "stdout": "",
                "stderr": "'jest' is not recognized as an internal or external command",
                "timedOut": False,
                "sandboxed": True,
            }
        )
        review = _sanitize_review_claims(
            {
                "blockers": [
                    "Lệnh 'npm test' thất bại vì 'jest' không được nhận diện.",
                    "At least one verification command failed.",
                    "Lỗi môi trường: Lệnh 'npm test' thất bại do thiếu các gói phụ thuộc (jest).",
                    "Thiếu bước cài đặt: Cần thực hiện 'npm install' trước khi chạy các lệnh kiểm thử.",
                    "Thiếu các gói phụ thuộc trong node_modules.",
                ],
                "warnings": [],
                "passed": False,
            },
            {"containerRequired": False},
            [command_result],
        )

        self.assertEqual(review["blockers"], [])
        self.assertEqual(len(review["warnings"]), 5)

    def test_stale_errno_review_claims_are_downgraded_after_successful_build(self) -> None:
        review = _sanitize_review_claims(
            {
                "blockers": [
                    "Agent không thể tạo cấu trúc thư mục (src/) hoặc tệp tin cần thiết do hạn chế policy và lỗi Errno 2.",
                    "Lỗi hệ thống tệp (Errno 2) ngăn cản việc tạo cấu trúc thư mục dự án.",
                ],
                "warnings": [],
                "passed": False,
            },
            {"containerRequired": False},
            [
                {
                    "command": "npm run build",
                    "cwd": "vocabulary-app",
                    "code": 0,
                    "stdout": "build completed",
                    "stderr": "",
                    "timedOut": False,
                    "sandboxed": True,
                }
            ],
        )

        self.assertEqual(review["blockers"], [])
        self.assertEqual(len(review["warnings"]), 2)

    def test_rework_approval_reuses_execution_id(self) -> None:
        root = Path(__file__).resolve().parents[1]
        backend = (root / "src" / "main" / "backendService.js").read_text(encoding="utf-8")
        main = (root / "src" / "main" / "main.js").read_text(encoding="utf-8")
        graph = (root / "engine" / "agent_engine" / "graph.py").read_text(encoding="utf-8")

        self.assertIn("humanGateApproval?.executionId || crypto.randomUUID()", backend)
        self.assertIn("executionId: pendingHumanGate.executionId || null", main)
        self.assertIn("(item.executionId || item.id) !== runIdentity", main)
        self.assertIn('"kind": "rework_limit"', graph)
        self.assertIn('"grantAdditionalAttempts": grant', graph)
        self.assertIn('f"{execution_id}:approval:"', graph)

    @pytest.mark.slow
    def test_write_task_continues_without_container_using_policy_limited_host_fallback(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir

            def fake_json(_self, prompt, fallback):
                result = copy.deepcopy(fallback)
                if "Plan Arbiter:" in prompt:
                    result["workerTaskSpec"] = {
                        **result["workerTaskSpec"],
                        "allowedFiles": ["app.py"],
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                    }
                elif "Tester Agent:" in prompt:
                    result["blockers"] = [
                        "At least one verification command failed.",
                        "Thiếu cấu hình kiểm thử: 'npm test' thất bại do thiếu script 'test' trong package.json.",
                    ]
                    result["passed"] = False
                elif "Security Reviewer Agent:" in prompt:
                    result["blockers"] = [
                        "Môi trường Docker/Podman không khả dụng, hệ thống đang chạy ở chế độ fallback cục bộ."
                    ]
                    result["passed"] = False
                elif "Code Reviewer Agent:" in prompt:
                    result["blockers"] = [
                        "Môi trường thực thi không đạt chuẩn: Docker/Podman không khả dụng.",
                        "Lệnh 'npm test' thất bại do thiếu script 'test' trong package.json.",
                    ]
                    result["passed"] = False
                return result

            worker_modes: list[str] = []

            def fake_worker(**kwargs):
                envelope = kwargs["worker_task_spec"]["contextEnvelope"]["inputs"]
                worker_modes.append(envelope["executionEnvironment"]["executionMode"])
                Path(kwargs["workspace"], "app.py").write_text("print('host fallback')\n", encoding="utf-8")
                return {
                    "summary": "host fallback worker",
                    "error": None,
                    "changedFiles": [{"path": "app.py", "status": "modified"}],
                    "appliedChanges": [{"path": "app.py", "status": "modified"}],
                    "sandboxDiff": [{"path": "app.py", "status": "modified"}],
                    "policyViolations": [],
                    "verificationSpec": {
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                        "verificationCwd": ".",
                    },
                    "events": [],
                }

            payload = {
                "executionId": "exec-host-fallback",
                "correlationId": "cid-host-fallback",
                "sessionId": "session-host-fallback",
                "content": "sửa app.py",
                "workspacePath": str(repo),
                "settings": {
                    "serverUrl": "http://model.invalid/v1",
                    "model": "test",
                    "apiKey": "",
                    "autoConfirmHumanGate": False,
                    "directWorkspaceMode": False,
                },
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": False, "runtime": None, "image": "test", "reason": "Docker or Podman unavailable"},
                    ),
                    mock.patch("agent_engine.graph.run_container_command") as container_command,
                    mock.patch(
                        "agent_engine.graph.run_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 0,
                            "stdout": "",
                            "stderr": "",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ) as host_command,
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                self.assertEqual(result["executionId"], "exec-host-fallback")
                self.assertEqual(worker_modes, ["host_fallback"])
                self.assertEqual(result["review"]["passed"], True)
                self.assertEqual(result["review"]["blockers"], [])
                self.assertTrue(any("Docker/Podman" in item for item in result["review"]["warnings"]))
                self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('host fallback')\n")
                container_command.assert_not_called()
                host_command.assert_called()
                worktrees = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(worktrees.count("worktree "), 1)
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    @pytest.mark.slow
    def test_rework_loop_stops_at_yaml_limit_and_emits_execution_gate(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            worker_calls: list[int] = []

            def fake_json(_self, prompt, fallback):
                result = copy.deepcopy(fallback)
                if "Plan Arbiter:" in prompt:
                    result["workerTaskSpec"] = {
                        **result["workerTaskSpec"],
                        "allowedFiles": ["app.py"],
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                    }
                return result

            def fake_worker(**kwargs):
                worker_calls.append(kwargs["worker_attempt"])
                Path(kwargs["workspace"], "app.py").write_text(
                    f"print('attempt {len(worker_calls)}')\n",
                    encoding="utf-8",
                )
                return {
                    "summary": "attempt",
                    "error": None,
                    "changedFiles": [{"path": "app.py", "status": "modified"}],
                    "appliedChanges": [{"path": "app.py", "status": "modified"}],
                    "sandboxDiff": [{"path": "app.py", "status": "modified"}],
                    "policyViolations": [],
                    "verificationSpec": {
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                        "verificationCwd": ".",
                    },
                    "events": [],
                }

            payload = {
                "executionId": "exec-rework-gate",
                "correlationId": "cid-rework-gate",
                "sessionId": "session-rework-gate",
                "content": "sửa app.py",
                "workspacePath": str(repo),
                "settings": {
                    "serverUrl": "http://model.invalid/v1",
                    "model": "test",
                    "apiKey": "",
                    "autoConfirmHumanGate": False,
                    "directWorkspaceMode": False,
                },
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": True, "runtime": "docker", "image": "python:test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.run_container_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 1,
                            "stdout": "",
                            "stderr": "forced failure",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ),
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                self.assertEqual(result["humanGate"]["status"], "pending")
                self.assertEqual(result["humanGate"]["kind"], "rework_limit")
                self.assertEqual(result["humanGate"]["retryCount"], 4)
                self.assertEqual(len(worker_calls), 4)
                self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('base')\n")

                approved_payload = {
                    **payload,
                    "humanGateApproval": {
                        **result["humanGate"],
                        "id": "approval-rework-1",
                        "status": "approved",
                        "approvedAt": "2026-06-18T00:00:00+00:00",
                    },
                }
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": True, "runtime": "docker", "image": "python:test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.run_container_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 1,
                            "stdout": "",
                            "stderr": "forced failure",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ),
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    approved_result = run_pipeline(approved_payload, lambda _stage, _detail: None)

                self.assertEqual(approved_result["humanGate"]["kind"], "rework_limit")
                self.assertEqual(approved_result["humanGate"]["retryCount"], 4)
                self.assertEqual(len(worker_calls), 4)
            finally:
                worktree_output = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                ).stdout
                worktree_paths = [
                    Path(line.removeprefix("worktree "))
                    for line in worktree_output.splitlines()
                    if line.startswith("worktree ")
                ]
                for path in worktree_paths[1:]:
                    subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo, capture_output=True)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

    @pytest.mark.slow
    def test_auto_confirm_rework_never_returns_pending_human_gate(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(repo_dir)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            worker_calls: list[int] = []

            def fake_json(_self, prompt, fallback):
                result = copy.deepcopy(fallback)
                if "Plan Arbiter:" in prompt:
                    result["workerTaskSpec"] = {
                        **result["workerTaskSpec"],
                        "allowedFiles": ["app.py"],
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                    }
                return result

            def fake_worker(**kwargs):
                worker_calls.append(kwargs["worker_attempt"])
                Path(kwargs["workspace"], "app.py").write_text("print('still failing')\n", encoding="utf-8")
                return {
                    "summary": "attempt",
                    "error": None,
                    "changedFiles": [{"path": "app.py", "status": "modified"}],
                    "appliedChanges": [{"path": "app.py", "status": "modified"}],
                    "sandboxDiff": [{"path": "app.py", "status": "modified"}],
                    "policyViolations": [],
                    "verificationSpec": {
                        "projectStack": "python",
                        "verificationCommands": ["python -m compileall ."],
                        "verificationCwd": ".",
                    },
                    "events": [],
                }

            payload = {
                "executionId": "exec-auto-rework",
                "correlationId": "cid-auto-rework",
                "sessionId": "session-auto-rework",
                "content": "sửa app.py",
                "workspacePath": str(repo),
                "settings": {
                    "serverUrl": "http://model.invalid/v1",
                    "model": "test",
                    "apiKey": "",
                    "autoConfirmHumanGate": True,
                    "directWorkspaceMode": False,
                },
                "messages": [],
            }
            try:
                with (
                    mock.patch("agent_engine.llm_client.ChatClient.json", autospec=True, side_effect=fake_json),
                    mock.patch(
                        "agent_engine.graph.codegraph_context",
                        return_value={"enabled": False, "status": "disabled", "reason": "test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.container_status",
                        return_value={"ready": True, "runtime": "docker", "image": "python:test"},
                    ),
                    mock.patch(
                        "agent_engine.graph.run_container_command",
                        return_value={
                            "command": "python -m compileall .",
                            "cwd": ".",
                            "code": 1,
                            "stdout": "",
                            "stderr": "forced assertion failure",
                            "timedOut": False,
                            "sandboxed": True,
                        },
                    ),
                    mock.patch("agent_engine.graph.run_openhands_worker", side_effect=fake_worker),
                ):
                    result = run_pipeline(payload, lambda _stage, _detail: None)

                self.assertNotEqual((result.get("humanGate") or {}).get("status"), "pending")
                self.assertEqual(len(worker_calls), 4)
                self.assertIn("không yêu cầu xác nhận", result["assistantText"])
                self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('base')\n")
            finally:
                worktree_output = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                ).stdout
                for line in worktree_output.splitlines():
                    if line.startswith("worktree "):
                        path = Path(line.removeprefix("worktree "))
                        if path.resolve() != repo.resolve():
                            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo, capture_output=True)
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir


if __name__ == "__main__":
    unittest.main()
