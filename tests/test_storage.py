from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_engine.storage.models import (
    AgentRun,
    AgentStep,
    Artifact,
    ArtifactType,
    Conversation,
    Memory,
    MemoryKind,
    Message,
    MessageRole,
    ModelUsageRecord,
    Plan,
    PlanItem,
    RunStatus,
    StepStatus,
    ToolCall,
    ToolCallStatus,
    _now,
)
from agent_engine.storage.run_repo import (
    RunRepository,
    SQLiteRunRepository,
)


# ---------------------------------------------------------------------------
# 1. AgentRun model
# ---------------------------------------------------------------------------


class AgentRunModelTests(unittest.TestCase):
    def test_create_defaults(self) -> None:
        run = AgentRun(task="fix bug")
        self.assertEqual(run.task, "fix bug")
        self.assertEqual(run.status, RunStatus.QUEUED.value)
        self.assertTrue(run.id)
        self.assertIsNone(run.plan)
        self.assertEqual(run.steps, [])
        self.assertEqual(run.messages, [])
        self.assertIsNone(run.completed_at)

    def test_is_terminal(self) -> None:
        run = AgentRun(task="x")
        self.assertFalse(run.is_terminal)
        run.status = RunStatus.RUNNING.value
        self.assertFalse(run.is_terminal)
        run.status = RunStatus.COMPLETED.value
        self.assertTrue(run.is_terminal)
        run.status = RunStatus.FAILED.value
        self.assertTrue(run.is_terminal)
        run.status = RunStatus.CANCELLED.value
        self.assertTrue(run.is_terminal)
        run.status = RunStatus.WAITING.value
        self.assertFalse(run.is_terminal)

    def test_status_transitions(self) -> None:
        run = AgentRun(task="t")

        run.mark_completed({"ok": True})
        self.assertEqual(run.status, RunStatus.COMPLETED.value)
        self.assertEqual(run.result, {"ok": True})
        self.assertIsNotNone(run.completed_at)
        self.assertTrue(run.is_terminal)

        run2 = AgentRun(task="t2")
        run2.mark_failed("boom")
        self.assertEqual(run2.status, RunStatus.FAILED.value)
        self.assertEqual(run2.error, "boom")
        self.assertIsNotNone(run2.completed_at)
        self.assertTrue(run2.is_terminal)

        run3 = AgentRun(task="t3")
        run3.mark_cancelled()
        self.assertEqual(run3.status, RunStatus.CANCELLED.value)
        self.assertIsNotNone(run3.completed_at)
        self.assertTrue(run3.is_terminal)

    def test_mark_completed_defaults(self) -> None:
        run = AgentRun(task="x")
        run.mark_completed()
        self.assertEqual(run.result, {})

    def test_to_dict(self) -> None:
        run = AgentRun(
            task="test",
            status=RunStatus.RUNNING.value,
            conversation_id="c-1",
            session_id="s-1",
        )
        d = run.to_dict()
        self.assertEqual(d["task"], "test")
        self.assertEqual(d["status"], "running")
        self.assertEqual(d["conversation_id"], "c-1")
        self.assertEqual(d["session_id"], "s-1")
        self.assertIsNone(d["plan"])
        self.assertEqual(d["messages"], [])
        self.assertEqual(d["steps"], [])
        self.assertIsNone(d["completed_at"])

    def test_to_dict_with_plan(self) -> None:
        plan = Plan(problem_statement="p", run_id="r-1")
        run = AgentRun(task="x", plan=plan)
        d = run.to_dict()
        self.assertIsNotNone(d["plan"])
        self.assertEqual(d["plan"]["problem_statement"], "p")

    def test_add_step(self) -> None:
        run = AgentRun(task="x")
        step = AgentStep(name="s1", run_id=run.id)
        run.add_step(step)
        self.assertEqual(len(run.steps), 1)
        self.assertEqual(run.steps[0].run_id, run.id)
        self.assertEqual(run.steps[0].name, "s1")

    def test_add_message(self) -> None:
        run = AgentRun(task="x")
        msg = Message(role="user", content="hi")
        run.add_message(msg)
        self.assertEqual(len(run.messages), 1)
        self.assertEqual(run.messages[0].content, "hi")


# ---------------------------------------------------------------------------
# 2. Plan model
# ---------------------------------------------------------------------------


class PlanModelTests(unittest.TestCase):
    def test_from_dict_string_items(self) -> None:
        data = {
            "id": "p-1",
            "run_id": "r-1",
            "problem_statement": "fix bug",
            "items": ["step a", "step b", "step c"],
        }
        plan = Plan.from_dict(data)
        self.assertEqual(plan.id, "p-1")
        self.assertEqual(plan.problem_statement, "fix bug")
        self.assertEqual(len(plan.items), 3)
        self.assertEqual(plan.items[0].description, "step a")
        self.assertEqual(plan.items[0].status, StepStatus.PENDING.value)

    def test_from_dict_dict_items(self) -> None:
        data = {
            "id": "p-2",
            "items": [
                {"description": "do x", "tools": ["bash"], "status": "completed"},
                {"description": "do y", "files": ["a.py"]},
            ],
        }
        plan = Plan.from_dict(data)
        self.assertEqual(len(plan.items), 2)
        self.assertEqual(plan.items[0].description, "do x")
        self.assertEqual(plan.items[1].description, "do y")
        # from_dict only maps description + status; tools/files/etc left as default
        self.assertEqual(plan.items[0].tools, [])
        self.assertEqual(plan.items[0].status, StepStatus.PENDING.value)

    def test_from_dict_finalSteps_alias(self) -> None:
        data = {"finalSteps": ["only step"]}
        plan = Plan.from_dict(data)
        self.assertEqual(len(plan.items), 1)
        self.assertEqual(plan.items[0].description, "only step")

    def test_from_dict_empty(self) -> None:
        plan = Plan.from_dict({})
        self.assertTrue(plan.id)
        self.assertEqual(plan.items, [])
        self.assertEqual(plan.problem_statement, "")
        self.assertEqual(plan.risk_class, "medium")

    def test_to_dict(self) -> None:
        plan = Plan(
            id="p-1",
            run_id="r-1",
            problem_statement="solve",
            items=[
                PlanItem(description="a", tools=["t1"], status="completed"),
                PlanItem(description="b"),
            ],
            constraints=["c1"],
            acceptance_criteria=["ac1"],
            risk_class="high",
        )
        d = plan.to_dict()
        self.assertEqual(d["id"], "p-1")
        self.assertEqual(d["run_id"], "r-1")
        self.assertEqual(d["problem_statement"], "solve")
        self.assertEqual(len(d["items"]), 2)
        self.assertEqual(d["items"][0]["description"], "a")
        self.assertEqual(d["items"][0]["tools"], ["t1"])
        self.assertEqual(d["items"][0]["status"], "completed")
        self.assertEqual(d["constraints"], ["c1"])
        self.assertEqual(d["risk_class"], "high")


# ---------------------------------------------------------------------------
# 3. Message model serialization
# ---------------------------------------------------------------------------


class MessageModelTests(unittest.TestCase):
    def test_to_dict(self) -> None:
        msg = Message(
            role=MessageRole.USER.value,
            content="hello",
            conversation_id="c-1",
            name="alice",
            metadata={"k": "v"},
        )
        d = msg.to_dict()
        self.assertEqual(d["role"], "user")
        self.assertEqual(d["content"], "hello")
        self.assertEqual(d["conversation_id"], "c-1")
        self.assertEqual(d["name"], "alice")
        self.assertEqual(d["metadata"], {"k": "v"})

    def test_from_dict(self) -> None:
        data = {
            "id": "m-1",
            "role": "assistant",
            "content": "ok",
            "conversation_id": "c-1",
            "tool_call_id": "tc-1",
        }
        msg = Message.from_dict(data)
        self.assertEqual(msg.id, "m-1")
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.content, "ok")
        self.assertEqual(msg.tool_call_id, "tc-1")

    def test_from_dict_defaults(self) -> None:
        msg = Message.from_dict({"role": "system"})
        self.assertTrue(msg.id)
        self.assertEqual(msg.content, "")
        self.assertEqual(msg.conversation_id, "")

    def test_roundtrip(self) -> None:
        original = Message(
            role="tool",
            content='{"result": 1}',
            tool_call_id="tc-99",
            name="bash",
            metadata={"lang": "sh"},
        )
        restored = Message.from_dict(original.to_dict())
        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.role, original.role)
        self.assertEqual(restored.content, original.content)
        self.assertEqual(restored.tool_call_id, original.tool_call_id)
        self.assertEqual(restored.name, original.name)
        self.assertEqual(restored.metadata, original.metadata)


# ---------------------------------------------------------------------------
# 4. SQLiteRunRepository
# ---------------------------------------------------------------------------


class _RepoFixture:
    """Helper to create a temp SQLiteRunRepository."""

    def __init__(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.repo = SQLiteRunRepository(str(self.db_path))

    def close(self) -> None:
        self.repo.close()
        self.temp_dir.cleanup()


class SQLiteRunRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fix = _RepoFixture()
        self.repo = self.fix.repo

    def tearDown(self) -> None:
        self.fix.close()

    # --- Runs ---

    def test_create_and_get_run(self) -> None:
        run = AgentRun(
            task="test run",
            conversation_id="c-1",
            session_id="s-1",
            status=RunStatus.QUEUED.value,
        )
        created = self.repo.create_run(run)
        self.assertEqual(created.id, run.id)

        fetched = self.repo.get_run(run.id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.task, "test run")
        self.assertEqual(fetched.conversation_id, "c-1")
        self.assertEqual(fetched.status, "queued")

    def test_get_run_not_found(self) -> None:
        self.assertIsNone(self.repo.get_run("nonexistent"))

    def test_list_runs_pagination(self) -> None:
        for i in range(5):
            self.repo.create_run(AgentRun(task=f"task-{i}"))
        result = self.repo.list_runs(limit=3, offset=1)
        self.assertEqual(len(result), 3)

        all_runs = self.repo.list_runs(limit=100)
        self.assertGreaterEqual(len(all_runs), 5)

    def test_list_runs_filter_by_conversation_id(self) -> None:
        self.repo.create_run(AgentRun(task="a", conversation_id="c-a"))
        self.repo.create_run(AgentRun(task="b", conversation_id="c-b"))
        result = self.repo.list_runs(conversation_id="c-a")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task, "a")

    def test_list_runs_filter_by_session_id(self) -> None:
        self.repo.create_run(AgentRun(task="a", session_id="s-a"))
        self.repo.create_run(AgentRun(task="b", session_id="s-b"))
        result = self.repo.list_runs(session_id="s-a")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task, "a")

    def test_list_runs_filter_by_status(self) -> None:
        self.repo.create_run(AgentRun(task="a", status="completed"))
        self.repo.create_run(AgentRun(task="b", status="running"))
        result = self.repo.list_runs(status="completed")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].task, "a")

    def test_update_run_status_no_lock(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        ok = self.repo.update_run_status(run.id, RunStatus.RUNNING.value)
        self.assertTrue(ok)
        fetched = self.repo.get_run(run.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "running")

    def test_update_run_status_optimistic_lock_success(self) -> None:
        run = self.repo.create_run(AgentRun(task="x", status="queued"))
        ok = self.repo.update_run_status(
            run.id, RunStatus.RUNNING.value, expected_status="queued"
        )
        self.assertTrue(ok)
        fetched = self.repo.get_run(run.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "running")

    def test_update_run_status_optimistic_lock_failure(self) -> None:
        run = self.repo.create_run(AgentRun(task="x", status="running"))
        ok = self.repo.update_run_status(
            run.id, RunStatus.COMPLETED.value, expected_status="queued"
        )
        self.assertFalse(ok)
        fetched = self.repo.get_run(run.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "running")

    def test_update_run_status_with_error(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        self.repo.update_run_status(run.id, "failed", error="timeout")
        fetched = self.repo.get_run(run.id)
        assert fetched is not None
        self.assertEqual(fetched.error, "timeout")

    def test_update_run_status_sets_completed_at(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        self.repo.update_run_status(run.id, "completed")
        fetched = self.repo.get_run(run.id)
        assert fetched is not None
        self.assertIsNotNone(fetched.completed_at)

    def test_delete_run_cascades(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        step = self.repo.create_step(AgentStep(name="s1", run_id=run.id))
        self.repo.create_tool_call(ToolCall(run_id=run.id, tool_name="bash"))
        self.repo.create_artifact(
            Artifact(run_id=run.id, name="out.txt", artifact_type="file")
        )
        mem = Memory(content="m1")
        mem.run_id = run.id  # model missing run_id field – set post-init
        self.repo.save_memory(mem)
        self.repo.record_usage(ModelUsageRecord(run_id=run.id, model="gpt"))
        self.repo.save_checkpoint(run.id, "ck1", {"x": 1})

        self.assertTrue(self.repo.delete_run(run.id))
        self.assertIsNone(self.repo.get_run(run.id))
        self.assertEqual(self.repo.list_steps(run.id), [])
        self.assertEqual(self.repo.list_tool_calls(run.id), [])
        self.assertEqual(self.repo.list_artifacts(run.id), [])
        self.assertEqual(self.repo.list_memories(run_id=run.id), [])
        self.assertEqual(self.repo.list_usage(run.id), [])
        self.assertIsNone(self.repo.get_latest_checkpoint(run.id))

    def test_delete_run_nonexistent(self) -> None:
        self.assertFalse(self.repo.delete_run("no-such-run"))

    # --- Steps ---

    def test_create_and_list_steps(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        s1 = self.repo.create_step(AgentStep(name="step-1", run_id=run.id))
        s2 = self.repo.create_step(AgentStep(name="step-2", run_id=run.id))

        steps = self.repo.list_steps(run.id)
        self.assertEqual(len(steps), 2)
        names = {s.name for s in steps}
        self.assertIn("step-1", names)
        self.assertIn("step-2", names)

    def test_get_step(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        step = self.repo.create_step(AgentStep(name="s1", run_id=run.id))
        fetched = self.repo.get_step(step.id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.name, "s1")

    def test_get_step_not_found(self) -> None:
        self.assertIsNone(self.repo.get_step("no-such-step"))

    def test_update_step(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        step = self.repo.create_step(
            AgentStep(name="s1", run_id=run.id, status="pending")
        )
        step.status = "completed"
        step.output_data = {"result": "ok"}
        step.duration_ms = 123.4
        step.error = None
        updated = self.repo.update_step(step)
        self.assertEqual(updated.status, "completed")

        fetched = self.repo.get_step(step.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "completed")
        self.assertEqual(fetched.output_data, {"result": "ok"})
        self.assertEqual(fetched.duration_ms, 123.4)

    # --- Tool calls ---

    def test_create_and_update_tool_call(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        tc = self.repo.create_tool_call(
            ToolCall(
                run_id=run.id,
                tool_name="bash",
                input_params={"cmd": "ls"},
                status=ToolCallStatus.IN_PROGRESS.value,
            )
        )
        self.assertIsNotNone(tc.id)

        tc.status = ToolCallStatus.SUCCESS.value
        tc.output = {"stdout": "file.txt"}
        tc.duration_ms = 50.0
        updated = self.repo.update_tool_call(tc)
        self.assertEqual(updated.status, "success")

        calls = self.repo.list_tool_calls(run.id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "success")
        self.assertEqual(calls[0].output, {"stdout": "file.txt"})
        self.assertEqual(calls[0].duration_ms, 50.0)

    def test_tool_call_idempotency_key(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        tc1 = self.repo.create_tool_call(
            ToolCall(
                run_id=run.id,
                tool_name="bash",
                idempotency_key="ik-abc",
            )
        )
        # Duplicate creation returns existing
        tc2 = self.repo.create_tool_call(
            ToolCall(
                run_id=run.id,
                tool_name="bash",
                idempotency_key="ik-abc",
            )
        )
        self.assertEqual(tc2.id, tc1.id)

    def test_get_tool_call_by_idempotency(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        tc = self.repo.create_tool_call(
            ToolCall(run_id=run.id, tool_name="write", idempotency_key="ik-xyz")
        )
        found = self.repo.get_tool_call_by_idempotency("ik-xyz")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.id, tc.id)

    def test_get_tool_call_by_idempotency_none(self) -> None:
        self.assertIsNone(self.repo.get_tool_call_by_idempotency(""))
        self.assertIsNone(self.repo.get_tool_call_by_idempotency("nonexistent"))

    # --- Artifacts ---

    def test_create_and_list_artifacts(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        a1 = self.repo.create_artifact(
            Artifact(
                run_id=run.id,
                artifact_type=ArtifactType.FILE.value,
                name="main.py",
                path="/ws/main.py",
                checksum="abc123",
                size_bytes=1024,
            )
        )
        a2 = self.repo.create_artifact(
            Artifact(
                run_id=run.id,
                artifact_type="diff",
                name="patch",
                path="/ws/patch.diff",
                size_bytes=512,
            )
        )
        arts = self.repo.list_artifacts(run.id)
        self.assertEqual(len(arts), 2)
        names = {a.name for a in arts}
        self.assertIn("main.py", names)
        self.assertIn("patch", names)
        self.assertEqual(arts[0].checksum, "abc123")
        self.assertEqual(arts[0].size_bytes, 1024)

    def test_list_artifacts_empty(self) -> None:
        self.assertEqual(self.repo.list_artifacts("no-run"), [])

    # --- Memories ---

    def test_save_and_list_memories(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        mem1 = Memory(
            kind=MemoryKind.WORKING.value,
            content="work mem",
            importance=0.8,
            activation=0.6,
            tags=["tag1"],
        )
        mem1.run_id = run.id
        m1 = self.repo.save_memory(mem1)
        mem2 = Memory(
            kind=MemoryKind.LONG_TERM.value,
            content="lt mem",
            importance=0.3,
        )
        mem2.run_id = run.id
        self.repo.save_memory(mem2)
        all_mems = self.repo.list_memories(run_id=run.id, limit=50)
        self.assertGreaterEqual(len(all_mems), 2)

    def test_list_memories_kind_filter(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        m1 = Memory(kind="working", content="w")
        m1.run_id = run.id
        self.repo.save_memory(m1)
        m2 = Memory(kind="long_term", content="lt")
        m2.run_id = run.id
        self.repo.save_memory(m2)
        working = self.repo.list_memories(run_id=run.id, kind="working")
        self.assertGreaterEqual(len(working), 1)
        for m in working:
            self.assertEqual(m.kind, "working")

    def test_list_memories_all_runs(self) -> None:
        r1 = self.repo.create_run(AgentRun(task="a"))
        r2 = self.repo.create_run(AgentRun(task="b"))
        m1 = Memory(content="m1")
        m1.run_id = r1.id
        self.repo.save_memory(m1)
        m2 = Memory(content="m2")
        m2.run_id = r2.id
        self.repo.save_memory(m2)
        # No run_id filter
        all_mems = self.repo.list_memories(limit=100)
        self.assertGreaterEqual(len(all_mems), 2)

    # --- Usage ---

    def test_record_and_list_usage(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        u1 = self.repo.record_usage(
            ModelUsageRecord(
                run_id=run.id,
                model="claude-3",
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=20,
            )
        )
        u2 = self.repo.record_usage(
            ModelUsageRecord(run_id=run.id, model="claude-3", input_tokens=200, output_tokens=80)
        )
        records = self.repo.list_usage(run.id)
        self.assertEqual(len(records), 2)
        total_input = sum(r.input_tokens for r in records)
        self.assertEqual(total_input, 300)

    def test_list_usage_empty(self) -> None:
        self.assertEqual(self.repo.list_usage("no-run"), [])

    # --- Checkpoints ---

    def test_save_and_get_latest_checkpoint(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        self.repo.save_checkpoint(run.id, "step-1", {"state": "a"})
        self.repo.save_checkpoint(run.id, "step-2", {"state": "b"})
        cp = self.repo.get_latest_checkpoint(run.id)
        self.assertIsNotNone(cp)
        assert cp is not None
        self.assertEqual(cp["state"], "b")

    def test_save_checkpoint_overwrite(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        self.repo.save_checkpoint(run.id, "step-1", {"v": 1})
        self.repo.save_checkpoint(run.id, "step-1", {"v": 2})
        cp = self.repo.get_latest_checkpoint(run.id)
        assert cp is not None
        self.assertEqual(cp["v"], 2)

    def test_get_latest_checkpoint_none(self) -> None:
        run = self.repo.create_run(AgentRun(task="x"))
        self.assertIsNone(self.repo.get_latest_checkpoint(run.id))

    # --- Conversations ---

    def test_create_and_get_conversation(self) -> None:
        conv = self.repo.create_conversation(
            Conversation(title="chat 1", workspace_path="/ws")
        )
        fetched = self.repo.get_conversation(conv.id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.title, "chat 1")
        self.assertEqual(fetched.workspace_path, "/ws")

    def test_get_conversation_not_found(self) -> None:
        self.assertIsNone(self.repo.get_conversation("nope"))

    def test_list_conversations(self) -> None:
        self.repo.create_conversation(Conversation(title="a"))
        self.repo.create_conversation(Conversation(title="b"))
        convs = self.repo.list_conversations(limit=10)
        self.assertGreaterEqual(len(convs), 2)

    # --- Messages ---

    def test_create_and_list_messages(self) -> None:
        conv = self.repo.create_conversation(Conversation(title="chat"))
        m1 = self.repo.create_message(
            Message(role="user", content="hi", conversation_id=conv.id)
        )
        m2 = self.repo.create_message(
            Message(role="assistant", content="hello", conversation_id=conv.id)
        )
        msgs = self.repo.list_messages(conv.id)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].role, "user")
        self.assertEqual(msgs[1].role, "assistant")

    def test_list_messages_empty(self) -> None:
        self.assertEqual(self.repo.list_messages("no-conv"), [])

    def test_list_messages_limit(self) -> None:
        conv = self.repo.create_conversation(Conversation(title="chat"))
        for i in range(5):
            self.repo.create_message(Message(role="user", content=str(i), conversation_id=conv.id))
        msgs = self.repo.list_messages(conv.id, limit=3)
        self.assertEqual(len(msgs), 3)

    # --- Health ---

    def test_health_check(self) -> None:
        self.assertTrue(self.repo.health_check())


# ---------------------------------------------------------------------------
# 5. Migrations idempotent
# ---------------------------------------------------------------------------


class MigrationTests(unittest.TestCase):
    def test_migrations_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "mig.db")
            repo1 = SQLiteRunRepository(db_path)
            self.assertTrue(repo1.health_check())

            # Second open — migrations re-run safely
            repo2 = SQLiteRunRepository(db_path)
            self.assertTrue(repo2.health_check())

            # Verify tables exist
            tables = repo2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = {r[0] for r in tables}
            expected = {
                "storage_runs",
                "storage_steps",
                "storage_tool_calls",
                "storage_artifacts",
                "storage_memories",
                "storage_usage",
                "storage_checkpoints",
                "storage_conversations",
                "storage_messages",
                "storage_migrations",
            }
            self.assertTrue(expected.issubset(table_names))

            repo2.close()
            repo1.close()


# ---------------------------------------------------------------------------
# 6. RunRepository Protocol interface
# ---------------------------------------------------------------------------


class RunRepositoryProtocolTests(unittest.TestCase):
    def test_protocol_methods_declared(self) -> None:
        """Verify RunRepository declares all expected method signatures."""
        expected = [
            "create_run",
            "get_run",
            "list_runs",
            "update_run_status",
            "delete_run",
            "create_step",
            "get_step",
            "list_steps",
            "update_step",
            "create_conversation",
            "get_conversation",
            "list_conversations",
            "create_message",
            "list_messages",
            "save_plan",
            "get_plan",
            "get_plan_by_run",
            "create_tool_call",
            "update_tool_call",
            "list_tool_calls",
            "get_tool_call_by_idempotency",
            "create_artifact",
            "list_artifacts",
            "save_memory",
            "list_memories",
            "record_usage",
            "list_usage",
            "save_checkpoint",
            "get_latest_checkpoint",
            "health_check",
            "close",
        ]
        for name in expected:
            self.assertTrue(
                hasattr(RunRepository, name),
                f"RunRepository missing method: {name}",
            )

    def test_sqlite_repo_implements_protocol(self) -> None:
        """SQLiteRunRepository structurally satisfies RunRepository Protocol."""
        repo_methods = {
            name
            for name in dir(SQLiteRunRepository)
            if callable(getattr(SQLiteRunRepository, name)) and not name.startswith("_")
        }
        protocol_methods = {
            name
            for name in dir(RunRepository)
            if callable(getattr(RunRepository, name)) and not name.startswith("_")
        }
        missing = protocol_methods - repo_methods
        self.assertEqual(
            missing,
            set(),
            f"SQLiteRunRepository missing protocol methods: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
