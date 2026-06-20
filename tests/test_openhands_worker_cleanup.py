from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.openhands_worker import _load_mcp_config, run_openhands_worker
from agent_engine.project_scaffold import should_scaffold_todo_fallback


class FakeLLM:
    def __init__(self, **_kwargs):
        pass


class FakeBrokenLLM:
    def __init__(self, **_kwargs):
        raise RuntimeError("llm setup failed")


class FakeCondenser:
    def __init__(self, **_kwargs):
        pass


class FakeAgent:
    def __init__(self, **_kwargs):
        pass


class FakeConversation:
    instances = []

    def __init__(self, **kwargs):
        self.workspace = Path(kwargs["workspace"])
        self.closed = False
        FakeConversation.instances.append(self)

    def send_message(self, _task: str) -> None:
        return None

    def run(self) -> str:
        (self.workspace / "todo.txt").write_text("done", encoding="utf-8")
        return "ok"

    def close(self) -> None:
        self.closed = True


class FakeNoopConversation(FakeConversation):
    def run(self) -> str:
        return "ok"


class FakeErrorConversation(FakeConversation):
    def run(self) -> str:
        (self.workspace / "todo.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("boom")


class FakeTransientConversation(FakeConversation):
    run_count = 0
    sent_messages = 0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        persistence_dir = Path(kwargs["persistence_dir"])
        conversation_id = kwargs["conversation_id"]
        storage = persistence_dir / conversation_id.hex
        storage.mkdir(parents=True, exist_ok=True)
        (storage / "base_state.json").write_text("{}", encoding="utf-8")

    def send_message(self, _task: str) -> None:
        FakeTransientConversation.sent_messages += 1

    def run(self) -> str:
        FakeTransientConversation.run_count += 1
        if FakeTransientConversation.run_count == 1:
            (self.workspace / "before-retry.txt").write_text("persisted", encoding="utf-8")
            raise ConnectionError("connection reset")
        (self.workspace / "after-retry.txt").write_text("completed", encoding="utf-8")
        return "ok"


def assert_worker_contract(testcase: unittest.TestCase, result: dict) -> None:
    for key in ("sandboxDiff", "policyViolations", "appliedChanges", "selectedExecutionRoot", "events", "error"):
        testcase.assertIn(key, result)
    testcase.assertEqual(result["changedFiles"], result["appliedChanges"])
    testcase.assertIsInstance(result["sandboxDiff"], list)
    testcase.assertIsInstance(result["policyViolations"], list)
    testcase.assertIsInstance(result["appliedChanges"], list)
    testcase.assertIsInstance(result["selectedExecutionRoot"], str)
    testcase.assertIsInstance(result["events"], list)


class OpenHandsWorkerCleanupTests(unittest.TestCase):
    def test_worker_tries_model_before_todo_scaffold_without_container(self) -> None:
        FakeConversation.instances = []
        with tempfile.TemporaryDirectory() as workspace:
            spec = {
                "objective": "Create a responsive Todo App",
                "projectStack": "node",
                "targetProjectDir": "todo-app",
                "projectRoot": "todo-app",
                "verificationCwd": "todo-app",
                "allowedFiles": ["todo-app/**"],
                "forbiddenPaths": [".env", ".git/**"],
                "verificationCommands": ["npm run build"],
            }
            with (
                mock.patch("agent_engine.openhands_worker.container_status", return_value={"ready": False, "reason": "no runtime"}),
                mock.patch("openhands.sdk.LLM", FakeLLM),
                mock.patch("openhands.sdk.Agent", FakeAgent),
                mock.patch("openhands.sdk.Tool", lambda name, **_kwargs: name),
                mock.patch("openhands.sdk.Conversation", FakeNoopConversation),
                mock.patch("openhands.sdk.context.condenser.LLMSummarizingCondenser", FakeCondenser),
            ):
                result = run_openhands_worker(
                    workspace=workspace,
                    server_url="http://model.test/v1",
                    model="test-model",
                    api_key="",
                    worker_task_spec=spec,
                    rework_context=None,
                    emit=lambda _stage, _detail: None,
                    dependency_workspace=None,
                    worktree_isolated=False,
                )

            self.assertIsNone(result["error"])
            self.assertEqual(result["scaffoldFallback"]["kind"], "todo_app")
            self.assertTrue(result["sandboxed"])
            self.assertTrue(FakeConversation.instances)
            self.assertTrue((Path(workspace) / "todo-app" / "package.json").exists())
            self.assertTrue(any(item["path"] == "todo-app/package.json" for item in result["changedFiles"]))

    def test_vocabulary_goal_never_selects_todo_scaffold_from_context_noise(self) -> None:
        spec = {
            "objective": "Chuyển ứng dụng todo cũ sang ứng dụng học từ vựng tiếng Anh",
            "projectStack": "node",
            "targetProjectDir": "vocabulary-app",
            "acceptanceCriteria": ["Người dùng thêm và ôn từ mới"],
            "contextEnvelope": {
                "inputs": {
                    "workerContext": {
                        "history": "todo todo todo",
                    }
                }
            },
        }

        self.assertFalse(should_scaffold_todo_fallback(spec))

    def test_mcp_config_loads_only_trusted_sanitized_servers(self) -> None:
        emitted: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".openhands").mkdir()
            (root / "tools").mkdir()
            (root / "tools" / "docs-mcp.js").write_text("console.log('mcp')", encoding="utf-8")
            (root / ".openhands" / "mcp.json").write_text(
                """
                {
                  "trustedServers": ["docs"],
                  "mcpServers": {
                    "docs": {
                      "command": "node",
                      "args": ["./tools/docs-mcp.js"],
                      "env": {"DOCS_API_TOKEN": "${DOCS_API_TOKEN}"}
                    },
                    "remoteOps": {
                      "trusted": true,
                      "url": "https://mcp.example.test/sse"
                    },
                    "notTrusted": {
                      "command": "node",
                      "args": []
                    },
                    "literalSecret": {
                      "trusted": true,
                      "command": "node",
                      "env": {"API_TOKEN": "plain-secret"}
                    },
                    "escape": {
                      "trusted": true,
                      "command": "../outside-server.js"
                    }
                  }
                }
                """,
                encoding="utf-8",
            )

            with mock.patch(
                "agent_engine.codebase_memory.McpServerConfig.detect",
                return_value=None,
            ):
                config = _load_mcp_config(str(root), lambda stage, detail: emitted.append((stage, detail)))

        self.assertEqual(set(config["mcpServers"].keys()), {"docs", "remoteOps"})
        self.assertNotIn("trusted", config["mcpServers"]["remoteOps"])
        self.assertEqual(config["mcpServers"]["docs"]["env"]["DOCS_API_TOKEN"], "${DOCS_API_TOKEN}")
        self.assertTrue(any("not listed in trustedServers" in detail for _stage, detail in emitted))
        self.assertTrue(any("secret field API_TOKEN" in detail for _stage, detail in emitted))
        self.assertTrue(any("command must stay inside workspace" in detail for _stage, detail in emitted))

    def test_worker_closes_conversation_before_sandbox_cleanup(self) -> None:
        FakeConversation.instances = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                mock.patch("openhands.sdk.LLM", FakeLLM),
                mock.patch("openhands.sdk.Agent", FakeAgent),
                mock.patch("openhands.sdk.Tool", lambda name, **_kwargs: name),
                mock.patch("openhands.sdk.Conversation", FakeConversation),
                mock.patch("openhands.sdk.context.condenser.LLMSummarizingCondenser", FakeCondenser),
            ):
                result = run_openhands_worker(
                    workspace=str(root),
                    server_url="http://model.test/v1",
                    model="test-model",
                    api_key="",
                    worker_task_spec={"allowedFiles": ["todo.txt"], "forbiddenPaths": []},
                    rework_context=None,
                    emit=lambda _stage, _detail: None,
                )

        self.assertEqual(result["error"], None)
        assert_worker_contract(self, result)
        self.assertEqual(result["sandboxDiff"], [{"path": "todo.txt", "status": "created"}])
        self.assertEqual(result["appliedChanges"], [{"path": "todo.txt", "status": "created"}])
        self.assertEqual(result["changedFiles"], [{"path": "todo.txt", "status": "created"}])
        self.assertTrue(FakeConversation.instances)
        self.assertTrue(FakeConversation.instances[0].closed)

    def test_worker_scaffolds_todo_app_when_openhands_makes_no_changes(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        FakeConversation.instances = []
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as state_dir:
            root = Path(temp_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                with (
                    mock.patch("openhands.sdk.LLM", FakeLLM),
                    mock.patch("openhands.sdk.Agent", FakeAgent),
                    mock.patch("openhands.sdk.Tool", lambda name, **_kwargs: name),
                    mock.patch("openhands.sdk.Conversation", FakeNoopConversation),
                    mock.patch("openhands.sdk.context.condenser.LLMSummarizingCondenser", FakeCondenser),
                ):
                    result = run_openhands_worker(
                        workspace=str(root),
                        server_url="http://model.test/v1",
                        model="test-model",
                        api_key="",
                        worker_task_spec={
                            "projectStack": "node",
                            "objective": "Create a responsive todo web app",
                            "targetProjectDir": "todo-app",
                            "allowedFiles": ["todo-app/**"],
                            "forbiddenPaths": [],
                        },
                        rework_context=None,
                        emit=lambda _stage, _detail: None,
                    )
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

            self.assertEqual(result["error"], None)
            assert_worker_contract(self, result)
            self.assertTrue(result["scaffoldFallback"]["used"])
            self.assertTrue((root / "todo-app" / "package.json").exists())
            self.assertEqual(result["selectedExecutionRoot"], "todo-app")
            self.assertIn({"path": "todo-app/package.json", "status": "created"}, result["appliedChanges"])

    def test_worker_contract_is_present_when_openhands_errors(self) -> None:
        FakeConversation.instances = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                mock.patch("openhands.sdk.LLM", FakeLLM),
                mock.patch("openhands.sdk.Agent", FakeAgent),
                mock.patch("openhands.sdk.Tool", lambda name, **_kwargs: name),
                mock.patch("openhands.sdk.Conversation", FakeErrorConversation),
                mock.patch("openhands.sdk.context.condenser.LLMSummarizingCondenser", FakeCondenser),
            ):
                result = run_openhands_worker(
                    workspace=str(root),
                    server_url="http://model.test/v1",
                    model="test-model",
                    api_key="",
                    worker_task_spec={"allowedFiles": ["todo.txt"], "forbiddenPaths": []},
                    rework_context=None,
                    emit=lambda _stage, _detail: None,
                )

        assert_worker_contract(self, result)
        self.assertIn("boom", result["error"])
        self.assertEqual(result["sandboxDiff"], [{"path": "todo.txt", "status": "created"}])
        self.assertEqual(result["appliedChanges"], [{"path": "todo.txt", "status": "created"}])

    def test_worker_contract_is_present_when_openhands_setup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch("openhands.sdk.LLM", FakeBrokenLLM):
                result = run_openhands_worker(
                    workspace=str(root),
                    server_url="http://model.test/v1",
                    model="test-model",
                    api_key="",
                    worker_task_spec={"allowedFiles": ["todo.txt"], "forbiddenPaths": []},
                    rework_context=None,
                    emit=lambda _stage, _detail: None,
                )

        assert_worker_contract(self, result)
        self.assertIn("llm setup failed", result["error"])
        self.assertEqual(result["sandboxDiff"], [])
        self.assertEqual(result["appliedChanges"], [])

    def test_transient_failure_resumes_persisted_conversation_and_durable_sandbox(self) -> None:
        old_state_dir = os.environ.get("AGENT_ENGINE_STATE_DIR")
        FakeTransientConversation.instances = []
        FakeTransientConversation.run_count = 0
        FakeTransientConversation.sent_messages = 0
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as state_dir:
            root = Path(temp_dir)
            os.environ["AGENT_ENGINE_STATE_DIR"] = state_dir
            try:
                with (
                    mock.patch("openhands.sdk.LLM", FakeLLM),
                    mock.patch("openhands.sdk.Agent", FakeAgent),
                    mock.patch("openhands.sdk.Tool", lambda name, **_kwargs: name),
                    mock.patch("openhands.sdk.Conversation", FakeTransientConversation),
                    mock.patch("openhands.sdk.context.condenser.LLMSummarizingCondenser", FakeCondenser),
                ):
                    with self.assertRaises(ConnectionError):
                        run_openhands_worker(
                            workspace=str(root),
                            server_url="http://model.test/v1",
                            model="test-model",
                            api_key="",
                            worker_task_spec={"allowedFiles": ["*.txt"], "forbiddenPaths": []},
                            rework_context=None,
                            emit=lambda _stage, _detail: None,
                            execution_id="exec-transient",
                            worker_attempt=1,
                        )

                    result = run_openhands_worker(
                        workspace=str(root),
                        server_url="http://model.test/v1",
                        model="test-model",
                        api_key="",
                        worker_task_spec={"allowedFiles": ["*.txt"], "forbiddenPaths": []},
                        rework_context=None,
                        emit=lambda _stage, _detail: None,
                        execution_id="exec-transient",
                        worker_attempt=1,
                    )
            finally:
                if old_state_dir is None:
                    os.environ.pop("AGENT_ENGINE_STATE_DIR", None)
                else:
                    os.environ["AGENT_ENGINE_STATE_DIR"] = old_state_dir

            self.assertEqual(result["error"], None)
            self.assertTrue((root / "before-retry.txt").exists())
            self.assertTrue((root / "after-retry.txt").exists())
            self.assertEqual(FakeTransientConversation.sent_messages, 1)
            changed_paths = {item["path"] for item in result["changedFiles"]}
            self.assertEqual(changed_paths, {"before-retry.txt", "after-retry.txt"})


if __name__ == "__main__":
    unittest.main()
