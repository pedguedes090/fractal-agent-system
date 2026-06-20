from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from agent_engine.agent_loop import (
    AgentLoop,
    LoopConfig,
    LoopPhase,
    create_agent_loop,
)
from agent_engine.storage.models import RunStatus


class AgentLoopHappyPathTests(unittest.TestCase):
    """Full happy path through all phases."""

    def setUp(self) -> None:
        self.loop = AgentLoop()
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(
            return_value={"workspacePath": "/tmp/ws", "files": ["a.py"]}
        )
        self.loop.on_plan = mock.Mock(
            return_value={
                "summary": "Plan to fix a.py",
                "riskClass": "medium",
            }
        )
        self.loop.on_execute = mock.Mock(
            return_value={
                "summary": "Fixed a.py",
                "changedFiles": [{"path": "a.py", "status": "modified"}],
            }
        )
        self.loop.on_verify = mock.Mock(
            return_value={"passed": True, "blockers": [], "warnings": []}
        )

    def test_run_completes_successfully(self) -> None:
        run = self.loop.run("fix a.py")

        self.assertEqual(run.status, RunStatus.COMPLETED.value)
        self.assertEqual(run.task, "fix a.py")
        self.assertIsNotNone(run.result)
        self.assertIn("text", run.result)
        self.assertIn("Fixed a.py", run.result["text"])
        self.assertIn("changedFiles", run.result)
        self.assertIn("review", run.result)

        # Verify all phases called
        self.loop.on_classify.assert_called_once_with("fix a.py")
        self.loop.on_gather_context.assert_called_once()
        self.loop.on_plan.assert_called_once()
        self.loop.on_execute.assert_called_once()
        self.loop.on_verify.assert_called_once()

        # Verify step sequence
        step_names = [s.name for s in run.steps]
        self.assertIn("classify", step_names)
        self.assertIn("gather_context", step_names)
        self.assertIn("plan", step_names)
        self.assertIn("execute", step_names)
        self.assertIn("verify", step_names)


class AgentLoopReadOnlyTests(unittest.TestCase):
    """Read-only tasks skip execute/verify."""

    def setUp(self) -> None:
        self.loop = AgentLoop()
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "query", "requiresWorker": False, "readOnly": True, "riskClass": "low"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={"workspacePath": "/tmp"})
        self.loop.on_plan = mock.Mock(return_value={"summary": "Read-only analysis complete."})
        self.loop.on_execute = mock.Mock()
        self.loop.on_verify = mock.Mock()

    def test_read_only_skips_execute_and_verify(self) -> None:
        run = self.loop.run("list files")

        # NOTE: read-only path currently does NOT call mark_completed(),
        # so status stays "running".
        self.assertEqual(run.status, RunStatus.RUNNING.value)
        self.assertIn("text", run.result)
        self.assertIn("Read-only", run.result["text"])

        # Execute and verify should NOT be called
        self.loop.on_execute.assert_not_called()
        self.loop.on_verify.assert_not_called()

        step_names = [s.name for s in run.steps]
        self.assertIn("classify", step_names)
        self.assertIn("gather_context", step_names)
        self.assertIn("plan", step_names)
        self.assertNotIn("execute", step_names)
        self.assertNotIn("verify", step_names)


class AgentLoopHighRiskTests(unittest.TestCase):
    """High risk tasks pause at wait_approval and return early."""

    def setUp(self) -> None:
        self.loop = AgentLoop()
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "high"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(
            return_value={"summary": "Dangerous plan", "riskClass": "high", "needsApproval": True}
        )
        self.loop.on_execute = mock.Mock()
        self.loop.on_verify = mock.Mock()

    def test_high_risk_returns_at_wait_approval(self) -> None:
        run = self.loop.run("delete production db")

        self.assertEqual(run.status, RunStatus.PENDING_APPROVAL.value)
        # Execute and verify should NOT be called
        self.loop.on_execute.assert_not_called()
        self.loop.on_verify.assert_not_called()

    def test_high_risk_from_plan_output_risk_class(self) -> None:
        # Even if classify says medium, plan output can escalate
        self.loop.on_classify.return_value = {
            "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
        }
        self.loop.on_plan.return_value = {"summary": "Plan", "riskClass": "high"}

        run = self.loop.run("delete production db")
        self.assertEqual(run.status, RunStatus.PENDING_APPROVAL.value)


class AgentLoopExecuteErrorTests(unittest.TestCase):
    """Execute errors produce failed status."""

    def setUp(self) -> None:
        self.loop = AgentLoop()
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(return_value={})
        self.loop.on_verify = mock.Mock(return_value={"passed": True, "blockers": []})

    def test_execute_error_sets_failed_status(self) -> None:
        self.loop.on_execute = mock.Mock(
            return_value={"error": "Command failed with exit code 1", "changedFiles": []}
        )

        run = self.loop.run("broken command")

        self.assertEqual(run.status, RunStatus.FAILED.value)
        self.assertIn("text", run.result)
        self.assertIn("Execution failed", run.result["text"])
        self.assertIn("error", run.result)

    def test_execute_exception_handled_as_error(self) -> None:
        self.loop.on_execute = mock.Mock(side_effect=RuntimeError("Boom!"))

        run = self.loop.run("explode")

        self.assertEqual(run.status, RunStatus.FAILED.value)
        self.assertIn("error", run.result)
        self.assertIn("Boom!", run.result["error"])


class AgentLoopReplanTests(unittest.TestCase):
    """Verify failure triggers replan → re-execute → success."""

    def setUp(self) -> None:
        self.config = LoopConfig(max_replan_attempts=3)
        self.loop = AgentLoop(config=self.config)
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(return_value={"summary": "Initial plan"})

    def test_verify_failure_replan_then_success(self) -> None:
        # First verify fails, second succeeds
        verify_responses = [
            {"passed": False, "blockers": ["Test failure in module X"], "warnings": []},
            {"passed": True, "blockers": [], "warnings": []},
        ]
        self.loop.on_verify = mock.Mock(side_effect=verify_responses)

        execute_responses = [
            {"summary": "First attempt", "changedFiles": [{"path": "a.py", "status": "modified"}]},
            {"summary": "Second attempt fixed", "changedFiles": [{"path": "a.py", "status": "modified"}]},
        ]
        self.loop.on_execute = mock.Mock(side_effect=execute_responses)

        self.loop.on_replan = mock.Mock(return_value={"summary": "Revised plan"})

        run = self.loop.run("fix a.py")

        self.assertEqual(run.status, RunStatus.COMPLETED.value)
        # Called twice: initial + re-execute
        self.assertEqual(self.loop.on_execute.call_count, 2)
        self.assertEqual(self.loop.on_verify.call_count, 2)
        self.loop.on_replan.assert_called_once()

        # Should have replan_1 and execute_1 steps
        step_names = [s.name for s in run.steps]
        self.assertIn("replan_1", step_names)
        self.assertIn("execute_1", step_names)

    def test_replan_budget_exhausted_marks_failed(self) -> None:
        self.config.max_replan_attempts = 2
        # Always fail verification
        self.loop.on_verify = mock.Mock(
            return_value={"passed": False, "blockers": ["Persistent failure"]}
        )
        self.loop.on_execute = mock.Mock(
            return_value={"summary": "Attempt", "changedFiles": []}
        )
        self.loop.on_replan = mock.Mock(return_value={"summary": "Retry plan"})

        run = self.loop.run("fix a.py")

        self.assertEqual(run.status, RunStatus.FAILED.value)
        self.assertIn("Verification failed", run.result["text"])
        # execute called: initial + 2 replans = 3
        self.assertEqual(self.loop.on_execute.call_count, 3)
        self.assertEqual(self.loop.on_replan.call_count, 2)

    def test_replan_exception_breaks_loop(self) -> None:
        self.loop.on_verify = mock.Mock(
            return_value={"passed": False, "blockers": ["Fail"]}
        )
        self.loop.on_execute = mock.Mock(
            return_value={"summary": "Attempt", "changedFiles": []}
        )
        self.loop.on_replan = mock.Mock(side_effect=RuntimeError("Cannot replan"))

        run = self.loop.run("unfixable")

        self.assertEqual(run.status, RunStatus.FAILED.value)


class AgentLoopCancelTests(unittest.TestCase):
    """Cancellation mid-run raises RuntimeError."""

    def setUp(self) -> None:
        self.loop = AgentLoop()
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(return_value={})

    def test_cancel_raises_runtime_error(self) -> None:
        # Cancel during execute
        def execute_and_cancel(*args, **kwargs):
            self.loop.cancel()
            return {}

        self.loop.on_execute = mock.Mock(side_effect=execute_and_cancel)
        self.loop.on_verify = mock.Mock()

        with self.assertRaises(RuntimeError) as ctx:
            self.loop.run("interrupt me")
        self.assertIn("cancelled", str(ctx.exception).lower())


class LoopConfigFromEnvTests(unittest.TestCase):
    """LoopConfig.from_env() reads environment variables."""

    def setUp(self) -> None:
        self._original = {k: os.environ.get(k) for k in [
            "AGENT_MAX_ITERATIONS", "AGENT_MAX_REPLAN_ATTEMPTS",
            "AGENT_MAX_TOOL_ROUNDS", "AGENT_TIMEOUT_SECONDS",
        ]}

    def tearDown(self) -> None:
        for k, v in self._original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_from_env_reads_all_vars(self) -> None:
        os.environ["AGENT_MAX_ITERATIONS"] = "20"
        os.environ["AGENT_MAX_REPLAN_ATTEMPTS"] = "5"
        os.environ["AGENT_MAX_TOOL_ROUNDS"] = "15"
        os.environ["AGENT_TIMEOUT_SECONDS"] = "3600"

        config = LoopConfig.from_env()
        self.assertEqual(config.max_iterations, 20)
        self.assertEqual(config.max_replan_attempts, 5)
        self.assertEqual(config.max_tool_rounds, 15)
        self.assertEqual(config.timeout_seconds, 3600.0)

    def test_from_env_uses_defaults_when_not_set(self) -> None:
        for k in ["AGENT_MAX_ITERATIONS", "AGENT_MAX_REPLAN_ATTEMPTS",
                   "AGENT_MAX_TOOL_ROUNDS", "AGENT_TIMEOUT_SECONDS"]:
            os.environ.pop(k, None)

        config = LoopConfig.from_env()
        self.assertEqual(config.max_iterations, 10)
        self.assertEqual(config.max_replan_attempts, 3)
        self.assertEqual(config.max_tool_rounds, 10)
        self.assertEqual(config.timeout_seconds, 1800.0)

    def test_negative_replan_clamped_to_zero(self) -> None:
        os.environ["AGENT_MAX_REPLAN_ATTEMPTS"] = "-5"
        config = LoopConfig.from_env()
        self.assertEqual(config.max_replan_attempts, 0)


class AgentLoopTimeoutTests(unittest.TestCase):
    """Timeout triggers TimeoutError."""

    def setUp(self) -> None:
        self.config = LoopConfig(timeout_seconds=0.001)  # Immediate timeout
        self.loop = AgentLoop(config=self.config)
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(return_value={})

    def test_timeout_raises_timeout_error(self) -> None:
        # Slow classify triggers timeout check at gather_context phase
        def slow_classify(task):
            time.sleep(0.05)
            return {"taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"}}

        self.loop.on_classify = mock.Mock(side_effect=slow_classify)

        with self.assertRaises(TimeoutError):
            self.loop.run("slow task")


class CheckpointSavedTests(unittest.TestCase):
    """Checkpoints saved after plan, execute, verify phases."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        self.loop = create_agent_loop(db_path=self.db_path)
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(return_value={"summary": "Plan"})
        self.loop.on_execute = mock.Mock(
            return_value={"summary": "Done", "changedFiles": []}
        )
        self.loop.on_verify = mock.Mock(return_value={"passed": True, "blockers": []})

    def tearDown(self) -> None:
        if self.loop.repo:
            self.loop.repo.close()
        self.tmpdir.cleanup()

    def test_checkpoints_saved_for_plan_execute_verify(self) -> None:
        run = self.loop.run("checkpoint test")

        self.assertIsNotNone(self.loop.repo)
        chk = self.loop.repo.get_latest_checkpoint(run.id)
        self.assertIsNotNone(chk)
        self.assertEqual(chk["run_id"], run.id)

    def test_no_checkpoint_when_no_repo(self) -> None:
        loop = AgentLoop()
        loop.on_classify = self.loop.on_classify
        loop.on_gather_context = self.loop.on_gather_context
        loop.on_plan = self.loop.on_plan
        loop.on_execute = self.loop.on_execute
        loop.on_verify = self.loop.on_verify

        run = loop.run("no repo test")
        self.assertEqual(run.status, RunStatus.COMPLETED.value)


class CreateAgentLoopFactoryTests(unittest.TestCase):
    """create_agent_loop() factory function."""

    def test_create_without_db_has_no_repo(self) -> None:
        loop = create_agent_loop()
        self.assertIsNone(loop.repo)

    def test_create_with_db_sets_sqlite_repo(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        try:
            db_path = os.path.join(tmpdir.name, "test.db")
            loop = create_agent_loop(db_path=db_path)
            self.assertIsNotNone(loop.repo)
            loop.repo.close()
        finally:
            tmpdir.cleanup()

    def test_create_with_custom_config(self) -> None:
        config = LoopConfig(max_iterations=42)
        loop = create_agent_loop(config=config)
        self.assertEqual(loop.config.max_iterations, 42)

    def test_create_with_emit_callback(self) -> None:
        events: list[tuple[str, str]] = []

        def collect(stage: str, detail: str) -> None:
            events.append((stage, detail))

        loop = create_agent_loop(emit=collect)
        self.assertIsNotNone(loop.emit)

        # Setup minimal callbacks and run to verify emit works
        loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        loop.on_gather_context = mock.Mock(return_value={})
        loop.on_plan = mock.Mock(return_value={})
        loop.on_execute = mock.Mock(
            return_value={"summary": "X", "changedFiles": []}
        )
        loop.on_verify = mock.Mock(return_value={"passed": True, "blockers": []})

        loop.run("emit test")
        self.assertTrue(len(events) > 0)


class AgentRunStatusTransitionTests(unittest.TestCase):
    """Status transitions during agent run lifecycle."""

    def setUp(self) -> None:
        self.loop = AgentLoop()
        self.loop.on_classify = mock.Mock(
            return_value={
                "taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"},
            }
        )
        self.loop.on_gather_context = mock.Mock(return_value={})
        self.loop.on_plan = mock.Mock(return_value={})

    def test_queued_to_running_to_completed(self) -> None:
        self.loop.on_execute = mock.Mock(
            return_value={"summary": "Done", "changedFiles": []}
        )
        self.loop.on_verify = mock.Mock(return_value={"passed": True, "blockers": []})

        run = self.loop.run("success task")
        self.assertEqual(run.status, RunStatus.COMPLETED.value)

    def test_queued_to_running_to_failed(self) -> None:
        self.loop.on_execute = mock.Mock(
            return_value={"error": "Build failure", "changedFiles": []}
        )
        self.loop.on_verify = mock.Mock(return_value={"passed": True, "blockers": []})

        run = self.loop.run("failing task")
        self.assertEqual(run.status, RunStatus.FAILED.value)

    def test_queued_to_running_to_cancelled(self) -> None:
        def cancel_during(*args, **kwargs):
            self.loop.cancel()
            return {}

        self.loop.on_execute = mock.Mock(side_effect=cancel_during)
        self.loop.on_verify = mock.Mock()

        with self.assertRaises(RuntimeError):
            self.loop.run("doomed task")
