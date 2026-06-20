from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_engine.tool_registry import (
    Tool,
    ToolInput,
    ToolOutput,
    ToolRegistry,
    WorkspaceOnlyPermission,
    _resolve_path,
    _tool_file_read,
    _tool_file_write,
    _tool_file_list,
    _tool_command_run,
    _tool_search_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_executor(inp: ToolInput) -> ToolOutput:
    echo = inp.params.get("echo", "")
    return ToolOutput(tool_name=inp.tool_name, call_id=inp.call_id, output={"echo": echo})


# ---------------------------------------------------------------------------
# Tool definition & validate_params
# ---------------------------------------------------------------------------


class ToolDefinitionTests(unittest.TestCase):
    def test_tool_construction_with_input_output_schema(self) -> None:
        tool = Tool(
            name="echo",
            description="Echoes input.",
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Thing to echo"},
                },
                "required": ["message"],
            },
            output_schema={"type": "object", "properties": {"echoed": {"type": "string"}}},
        )
        self.assertEqual(tool.name, "echo")
        self.assertEqual(tool.description, "Echoes input.")
        self.assertIsNotNone(tool.input_schema)
        self.assertIsNotNone(tool.output_schema)

    def test_to_dict_exports_definition(self) -> None:
        tool = Tool(
            name="echo",
            description="Echoes input.",
            input_schema={"type": "object", "properties": {}, "required": []},
            category="test",
            require_approval=False,
            timeout_seconds=5.0,
        )
        d = tool.to_dict()
        self.assertEqual(d["name"], "echo")
        self.assertEqual(d["category"], "test")
        self.assertFalse(d["require_approval"])
        self.assertEqual(d["timeout_seconds"], 5.0)

    # --- validate_params --------------------------------------------------------

    def test_validate_params_requires_required_fields(self) -> None:
        tool = Tool(
            name="adder",
            description="Adds two numbers.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
            executor=_simple_executor,
        )
        errors = tool.validate_params({"a": 1})
        self.assertEqual(len(errors), 1)
        self.assertIn("b", errors[0])

        errors = tool.validate_params({})
        self.assertEqual(len(errors), 2)

        errors = tool.validate_params({"a": 1, "b": 2})
        self.assertEqual(errors, [])

    def test_validate_params_type_checks(self) -> None:
        tool = Tool(
            name="typed",
            description="Tests type checking.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "score": {"type": "number"},
                    "active": {"type": "boolean"},
                    "items": {"type": "array"},
                    "meta": {"type": "object"},
                },
                "required": [],
            },
            executor=_simple_executor,
        )
        errors = tool.validate_params({
            "name": "ok",
            "count": 3,
            "score": 9.5,
            "active": True,
            "items": [1, 2],
            "meta": {"key": "val"},
        })
        self.assertEqual(errors, [])

    def test_validate_params_rejects_wrong_types(self) -> None:
        tool = Tool(
            name="typed",
            description="Tests type checking.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "score": {"type": "number"},
                    "active": {"type": "boolean"},
                    "items": {"type": "array"},
                    "meta": {"type": "object"},
                },
                "required": [],
            },
            executor=_simple_executor,
        )
        errors = tool.validate_params({
            "name": 42,          # not string
            "count": "three",    # not integer
            "score": "high",     # not number
            "active": "yes",     # not boolean
            "items": "one,two",  # not array
            "meta": "json",      # not object
        })
        self.assertEqual(len(errors), 6)

    def test_validate_params_rejects_oversized_strings(self) -> None:
        tool = Tool(
            name="reader",
            description="Read input.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": [],
            },
            executor=_simple_executor,
        )
        errors = tool.validate_params({"text": "x" * 100_001})
        self.assertEqual(len(errors), 1)
        self.assertIn("100KB", errors[0])

    def test_validate_params_handles_empty_schema(self) -> None:
        tool = Tool(name="noop", description="No schema.", input_schema={}, executor=_simple_executor)
        self.assertEqual(tool.validate_params({"anything": 1}), [])


# ---------------------------------------------------------------------------
# Tool.execute
# ---------------------------------------------------------------------------


class ToolExecuteTests(unittest.TestCase):
    def test_execute_returns_output_on_success(self) -> None:
        tool = Tool(
            name="echo",
            description="Echo.",
            input_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": [],
            },
            executor=_simple_executor,
        )
        result = tool.execute(ToolInput(tool_name="echo", params={"echo": "hello"}, call_id="c1"))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, {"echo": "hello"})
        self.assertEqual(result.tool_name, "echo")
        self.assertEqual(result.call_id, "c1")

    def test_execute_returns_error_when_no_executor(self) -> None:
        tool = Tool(name="ghost", description="No executor.", input_schema={})
        result = tool.execute(ToolInput(tool_name="ghost", params={}, call_id="c1"))
        self.assertFalse(result.ok)
        self.assertIn("no executor", result.error or "")

    def test_execute_returns_validation_error(self) -> None:
        tool = Tool(
            name="echo",
            description="Echo.",
            input_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
            },
            executor=_simple_executor,
        )
        result = tool.execute(ToolInput(tool_name="echo", params={}, call_id="c1"))
        self.assertFalse(result.ok)
        self.assertIn("Validation failed", result.error or "")

    def test_execute_catches_executor_exception(self) -> None:
        def _failing(_inp: ToolInput) -> ToolOutput:
            raise RuntimeError("boom")

        tool = Tool(
            name="faulty",
            description="Always fails.",
            input_schema={},
            executor=_failing,
        )
        result = tool.execute(ToolInput(tool_name="faulty", params={}, call_id="c1"))
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError: boom", result.error or "")

    def test_execute_truncates_large_output(self) -> None:
        tool = Tool(
            name="big",
            description="Returns large string.",
            input_schema={},
            executor=lambda _inp: ToolOutput(
                tool_name="big", call_id="", output="x" * 60_000
            ),
        )
        result = tool.execute(ToolInput(tool_name="big", params={}, call_id="c1"))
        self.assertTrue(result.truncated)
        self.assertTrue(len(result.output) < 60_000)


# ---------------------------------------------------------------------------
# ToolRegistry: register / get / list / unregister
# ---------------------------------------------------------------------------


class ToolRegistryRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = ToolRegistry()

    def test_register_and_get(self) -> None:
        tool = Tool(name="a", description="A tool.", input_schema={}, executor=_simple_executor)
        self.reg.register(tool)
        self.assertIs(self.reg.get("a"), tool)
        self.assertIsNone(self.reg.get("missing"))

    def test_list_names(self) -> None:
        self.reg.register(Tool(name="a", description="A.", input_schema={}))
        self.reg.register(Tool(name="b", description="B.", input_schema={}))
        self.assertCountEqual(self.reg.list_names(), ["a", "b"])

    def test_duplicate_registration_raises(self) -> None:
        self.reg.register(Tool(name="dup", description="First.", input_schema={}))
        with self.assertRaises(ValueError) as ctx:
            self.reg.register(Tool(name="dup", description="Second.", input_schema={}))
        self.assertIn("already registered", str(ctx.exception))

    def test_unregister_removes_tool(self) -> None:
        tool = Tool(name="rm", description="Will be removed.", input_schema={}, category="filesystem")
        self.reg.register(tool)
        self.reg.unregister("rm")
        self.assertIsNone(self.reg.get("rm"))
        self.assertEqual(self.reg.list_by_category("filesystem"), [])

    def test_unregister_missing_tool_no_error(self) -> None:
        self.reg.unregister("nope")  # should not raise

    def test_list_by_category(self) -> None:
        a = Tool(name="cat_a", description="", input_schema={}, category="alpha")
        b = Tool(name="cat_b", description="", input_schema={}, category="beta")
        c = Tool(name="cat_c", description="", input_schema={}, category="alpha")
        self.reg.register(a)
        self.reg.register(b)
        self.reg.register(c)
        alpha = self.reg.list_by_category("alpha")
        beta = self.reg.list_by_category("beta")
        self.assertEqual(len(alpha), 2)
        self.assertEqual(len(beta), 1)
        self.assertIs(alpha[0], a)
        self.assertIs(alpha[1], c)

    def test_list_by_category_unknown_returns_empty(self) -> None:
        self.assertEqual(self.reg.list_by_category("nada"), [])

    def test_list_categories(self) -> None:
        self.reg.register(Tool(name="x", description="", input_schema={}, category="cat1"))
        self.reg.register(Tool(name="y", description="", input_schema={}, category="cat2"))
        self.assertCountEqual(self.reg.list_categories(), ["cat1", "cat2"])

    def test_list_tool_defs(self) -> None:
        self.reg.register(Tool(name="t1", description="D1.", input_schema={}))
        self.reg.register(Tool(name="t2", description="D2.", input_schema={}))
        defs = self.reg.list_tool_defs()
        self.assertEqual(len(defs), 2)
        names = {d["name"] for d in defs}
        self.assertEqual(names, {"t1", "t2"})


# ---------------------------------------------------------------------------
# ToolRegistry.execute
# ---------------------------------------------------------------------------


class ToolRegistryExecuteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = ToolRegistry()
        self.reg.register(Tool(
            name="echo",
            description="Echoes.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": [],
            },
            executor=_simple_executor,
        ))

    def test_execute_successful(self) -> None:
        result = self.reg.execute("echo", {"echo": "hi"}, call_id="c42")
        self.assertTrue(result.ok)
        self.assertEqual(result.output, {"echo": "hi"})

    def test_execute_unknown_tool(self) -> None:
        result = self.reg.execute("ghost", {}, call_id="c1")
        self.assertFalse(result.ok)
        self.assertIn("Unknown tool", result.error or "")

    def test_execute_permission_denied(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            perm = WorkspaceOnlyPermission(workspace_dir=ws, allow_subprocess=False)
            self.reg.register(Tool(
                name="restricted",
                description="Needs path inside workspace.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                permission=perm,
                executor=_simple_executor,
            ))
            # Path outside workspace
            result = self.reg.execute("restricted", {"path": "/etc/passwd"}, workspace=ws)
            self.assertFalse(result.ok)
            self.assertIn("Permission denied", result.error or "")

    def test_execute_bypasses_permission_when_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            perm = WorkspaceOnlyPermission(workspace_dir=ws, allow_subprocess=False)
            self.reg.register(Tool(
                name="restricted",
                description="Needs path inside workspace.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": [],
                },
                permission=perm,
                executor=_simple_executor,
            ))
            result = self.reg.execute("restricted", {}, workspace="", call_id="c1")
            self.assertTrue(result.ok)  # no workspace -> no permission check


# ---------------------------------------------------------------------------
# WorkspaceOnlyPermission
# ---------------------------------------------------------------------------


class WorkspaceOnlyPermissionTests(unittest.TestCase):
    def test_allows_path_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            root = Path(ws)
            (root / "sub").mkdir()
            (root / "sub" / "file.txt").write_text("ok", encoding="utf-8")

            perm = WorkspaceOnlyPermission(workspace_dir=ws)
            self.assertTrue(perm.can_execute("file_read", {"path": "sub/file.txt"}, ws))
            self.assertTrue(perm.can_execute("file_read", {"path": "."}, ws))
            self.assertTrue(perm.can_execute("file_read", {"path": "sub"}, ws))

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            perm = WorkspaceOnlyPermission(workspace_dir=ws)
            self.assertFalse(perm.can_execute("file_read", {"path": "/etc/passwd"}, ws))
            self.assertFalse(perm.can_execute("file_read", {"path": "../../etc/passwd"}, ws))

    def test_allows_params_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            perm = WorkspaceOnlyPermission(workspace_dir=ws)
            self.assertTrue(perm.can_execute("echo", {"text": "hello"}, ws))

    def test_respects_allow_subprocess_flag(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            perm = WorkspaceOnlyPermission(workspace_dir=ws, allow_subprocess=True)
            self.assertTrue(perm.can_execute("command_run", {"command": "echo hi"}, ws))


# ---------------------------------------------------------------------------
# Built-in tools: file_read, file_write, file_list
# ---------------------------------------------------------------------------


class BuiltinFileToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_file_read_returns_content(self) -> None:
        root = Path(self.workspace)
        (root / "hello.txt").write_text("Hello World", encoding="utf-8")
        result = _tool_file_read(self.workspace, ToolInput(
            tool_name="file_read", params={"path": "hello.txt"}, call_id="r1",
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "Hello World")

    def test_file_read_rejects_path_escape(self) -> None:
        result = _tool_file_read(self.workspace, ToolInput(
            tool_name="file_read", params={"path": "../../etc/passwd"}, call_id="r1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error or "")

    def test_file_read_missing_file(self) -> None:
        result = _tool_file_read(self.workspace, ToolInput(
            tool_name="file_read", params={"path": "nope.txt"}, call_id="r1",
        ))
        self.assertFalse(result.ok)

    def test_file_write_writes_content(self) -> None:
        root = Path(self.workspace)
        result = _tool_file_write(self.workspace, ToolInput(
            tool_name="file_write",
            params={"path": "out/sub/file.txt", "content": "data"},
            call_id="w1",
        ))
        self.assertTrue(result.ok)
        self.assertTrue(result.output["written"])
        written = (root / "out" / "sub" / "file.txt").read_text(encoding="utf-8")
        self.assertEqual(written, "data")

    def test_file_write_rejects_path_escape(self) -> None:
        result = _tool_file_write(self.workspace, ToolInput(
            tool_name="file_write",
            params={"path": "../../etc/passwd", "content": "stuff"},
            call_id="w1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error or "")

    def test_file_list_lists_entries(self) -> None:
        root = Path(self.workspace)
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
        result = _tool_file_list(self.workspace, ToolInput(
            tool_name="file_list", params={"path": "."}, call_id="l1",
        ))
        self.assertTrue(result.ok)
        entries = result.output["entries"]
        self.assertIn("a.txt", entries)
        self.assertIn("sub/", entries)

    def test_file_list_non_existent_dir(self) -> None:
        result = _tool_file_list(self.workspace, ToolInput(
            tool_name="file_list", params={"path": "no-dir"}, call_id="l1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("Not a directory", result.error or "")


# ---------------------------------------------------------------------------
# Built-in tools: command_run (mocked subprocess)
# ---------------------------------------------------------------------------


class BuiltinCommandRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_command_run_executes_and_captures_output(self) -> None:
        proc = mock.Mock(
            returncode=0,
            stdout="hello",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=proc) as run_mock:
            result = _tool_command_run(self.workspace, ToolInput(
                tool_name="command_run",
                params={"command": "echo hello", "cwd": "."},
                call_id="c1",
            ))
        self.assertTrue(result.ok)
        self.assertEqual(result.output["exit_code"], 0)
        self.assertIn("hello", result.output["stdout"])

    def test_command_run_rejects_dangerous_commands(self) -> None:
        result = _tool_command_run(self.workspace, ToolInput(
            tool_name="command_run",
            params={"command": "rm -rf /", "cwd": "."},
            call_id="c1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("dangerous pattern", result.error or "")

    def test_command_run_missing_command(self) -> None:
        result = _tool_command_run(self.workspace, ToolInput(
            tool_name="command_run", params={"command": "", "cwd": "."}, call_id="c1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("command is required", result.error or "")

    def test_command_run_rejects_escape_path(self) -> None:
        result = _tool_command_run(self.workspace, ToolInput(
            tool_name="command_run",
            params={"command": "ls", "cwd": "../../etc"},
            call_id="c1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error or "")

    def test_command_run_timeout(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sleep 999", timeout=1),
        ):
            result = _tool_command_run(self.workspace, ToolInput(
                tool_name="command_run",
                params={"command": "sleep 999", "cwd": "."},
                call_id="c1",
            ))
        self.assertFalse(result.ok)
        self.assertIn("timed out", (result.error or "").lower())


# ---------------------------------------------------------------------------
# Built-in tools: search_content
# ---------------------------------------------------------------------------


class BuiltinSearchContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workspace = self._td.name

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_search_content_finds_matches(self) -> None:
        root = Path(self.workspace)
        (root / "notes.txt").write_text("hello world\nhello again", encoding="utf-8")
        result = _tool_search_content(self.workspace, ToolInput(
            tool_name="search_content",
            params={"pattern": r"hello", "path": "."},
            call_id="s1",
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.output["total_matches"], 2)

    def test_search_content_missing_pattern(self) -> None:
        result = _tool_search_content(self.workspace, ToolInput(
            tool_name="search_content", params={"pattern": "", "path": "."}, call_id="s1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("pattern is required", result.error or "")

    def test_search_content_invalid_regex(self) -> None:
        result = _tool_search_content(self.workspace, ToolInput(
            tool_name="search_content", params={"pattern": r"(unclosed", "path": "."}, call_id="s1",
        ))
        self.assertFalse(result.ok)
        self.assertIn("Invalid regex", result.error or "")

    def test_search_content_no_matches(self) -> None:
        root = Path(self.workspace)
        (root / "notes.txt").write_text("just text", encoding="utf-8")
        result = _tool_search_content(self.workspace, ToolInput(
            tool_name="search_content",
            params={"pattern": r"xyzzy", "path": "."},
            call_id="s1",
        ))
        self.assertTrue(result.ok)
        self.assertEqual(result.output["total_matches"], 0)


# ---------------------------------------------------------------------------
# register_default_tools
# ---------------------------------------------------------------------------


class RegisterDefaultToolsTests(unittest.TestCase):
    def test_registers_all_expected_tools(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            reg = ToolRegistry()
            reg.register_default_tools(ws)
            names = reg.list_names()
            self.assertCountEqual(
                names,
                ["file_read", "file_write", "file_list", "command_run", "search_content"],
            )

    def test_registered_tools_have_correct_categories(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            reg = ToolRegistry()
            reg.register_default_tools(ws)
            fs_tools = [t.name for t in reg.list_by_category("filesystem")]
            self.assertCountEqual(fs_tools, ["file_read", "file_write", "file_list"])
            shell_tools = [t.name for t in reg.list_by_category("shell")]
            self.assertCountEqual(shell_tools, ["command_run"])
            search_tools = [t.name for t in reg.list_by_category("search")]
            self.assertCountEqual(search_tools, ["search_content"])

    def test_registered_file_read_works(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            reg = ToolRegistry()
            reg.register_default_tools(ws)
            root = Path(ws)
            (root / "readme.md").write_text("# Hello", encoding="utf-8")
            result = reg.execute("file_read", {"path": "readme.md"}, workspace=ws)
            self.assertTrue(result.ok)
            self.assertEqual(result.output, "# Hello")

    def test_registered_file_write_works(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            reg = ToolRegistry()
            reg.register_default_tools(ws)
            result = reg.execute("file_write", {"path": "data.json", "content": '{"ok":true}'}, workspace=ws)
            self.assertTrue(result.ok)
            self.assertTrue(result.output["written"])
            content = (Path(ws) / "data.json").read_text(encoding="utf-8")
            self.assertEqual(content, '{"ok":true}')

    def test_registered_command_run_works(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            reg = ToolRegistry()
            reg.register_default_tools(ws)
            proc = mock.Mock(returncode=0, stdout="ok", stderr="")
            with mock.patch("subprocess.run", return_value=proc):
                result = reg.execute("command_run", {"command": "echo ok"}, workspace=ws)
            self.assertTrue(result.ok)
            self.assertEqual(result.output["exit_code"], 0)

    def test_registered_search_content_works(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            reg = ToolRegistry()
            reg.register_default_tools(ws)
            root = Path(ws)
            (root / "f.py").write_text("def foo():\n    pass\n", encoding="utf-8")
            result = reg.execute("search_content", {"pattern": "def foo", "path": "."}, workspace=ws)
            self.assertTrue(result.ok)
            self.assertEqual(result.output["total_matches"], 1)


# ---------------------------------------------------------------------------
# _resolve_path helper
# ---------------------------------------------------------------------------


class ResolvePathTests(unittest.TestCase):
    def test_resolves_relative_path_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            full, error = _resolve_path(ws, "sub/file.txt")
            self.assertIsNone(error)
            self.assertTrue(full.is_absolute())
            self.assertIn("sub", str(full))

    def test_rejects_absolute_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as ws:
            _, error = _resolve_path(ws, "/etc/passwd")
            self.assertIsNotNone(error)
            self.assertIn("escapes workspace", error)


if __name__ == "__main__":
    unittest.main()
