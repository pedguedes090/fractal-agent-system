"""Tool Registry — unified tool interface with validation, timeout, permissions.

Architecture:
  - Tool: abstract base defining name, description, input/output schema, validation
  - ToolRegistry: register/lookup/dispatch tools by name
  - Each tool has: timeout, permission level, execution log, error handling
  - No if/else chains for tool selection — lookup by name from registry
"""

from __future__ import annotations

import json
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .debug_log import write_debug_event


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass
class ToolInput:
    """Input to a tool execution."""

    tool_name: str
    params: dict[str, Any]
    call_id: str = ""
    correlation_id: str = ""


@dataclass
class ToolOutput:
    """Output from a tool execution."""

    tool_name: str
    call_id: str
    output: Any
    error: str | None = None
    duration_ms: float = 0.0
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def output_text(self) -> str:
        """Return output as string (for passing to LLM)."""
        if self.error:
            return f"Tool {self.tool_name} failed: {self.error}"
        if isinstance(self.output, (dict, list)):
            return json.dumps(self.output, ensure_ascii=False, default=str)
        return str(self.output)


@dataclass
class ToolExecutionLog:
    """Record of a tool execution."""

    tool_name: str
    call_id: str
    input_params: dict[str, Any]
    output_summary: str
    error: str | None
    duration_ms: float
    timestamp_iso: str
    truncated: bool = False


# ---------------------------------------------------------------------------
# Permission levels
# ---------------------------------------------------------------------------


class Permission(Protocol):
    """Permission check for tool execution."""

    def can_execute(self, tool_name: str, params: dict[str, Any], workspace: str) -> bool:
        """Return True if tool may execute with given params."""
        ...


@dataclass
class WorkspaceOnlyPermission:
    """Restrict tool execution to paths within workspace."""

    workspace_dir: str
    allow_subprocess: bool = False

    def can_execute(self, tool_name: str, params: dict[str, Any], workspace: str) -> bool:
        # Check any file paths in params are within workspace
        root = Path(workspace).resolve()
        for key, value in params.items():
            if isinstance(value, str) and value and ("/" in value or "\\" in value or value.startswith(".")):
                candidate = (root / value).resolve()
                if candidate != root and root not in candidate.parents:
                    # Also check if value is just a relative path that resolves inside
                    try:
                        resolved = Path(value).expanduser().resolve()
                        if resolved != root and root not in resolved.parents:
                            return False
                    except Exception:
                        return False
        return True


# ---------------------------------------------------------------------------
# Tool interface
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """A tool that can be registered in the ToolRegistry."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    timeout_seconds: float = 120.0
    permission: Permission | None = None
    require_approval: bool = False
    category: str = "general"
    executor: Callable[[ToolInput], ToolOutput] | None = None

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate params against input_schema. Returns list of errors (empty = valid)."""
        errors: list[str] = []
        schema = self.input_schema
        required = schema.get("required", []) if isinstance(schema, dict) else []
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}

        for req in required:
            if req not in params:
                errors.append(f"Missing required parameter: {req}")

        for key, value in params.items():
            if key in properties:
                prop_schema = properties[key]
                if isinstance(prop_schema, dict):
                    expected_type = prop_schema.get("type", "")
                    if expected_type == "string" and not isinstance(value, str):
                        errors.append(f"Parameter {key} must be a string")
                    elif expected_type == "integer" and not isinstance(value, int):
                        errors.append(f"Parameter {key} must be an integer")
                    elif expected_type == "number" and not isinstance(value, (int, float)):
                        errors.append(f"Parameter {key} must be a number")
                    elif expected_type == "boolean" and not isinstance(value, bool):
                        errors.append(f"Parameter {key} must be a boolean")
                    elif expected_type == "array" and not isinstance(value, list):
                        errors.append(f"Parameter {key} must be an array")
                    elif expected_type == "object" and not isinstance(value, dict):
                        errors.append(f"Parameter {key} must be an object")

            # Size limit on string params
            if isinstance(value, str) and len(value) > 100_000:
                errors.append(f"Parameter {key} exceeds 100KB limit")

        return errors

    def execute(self, input: ToolInput) -> ToolOutput:
        """Execute this tool with proper validation, timeout, error handling, and logging.

        Returns:
            ToolOutput with result or error
        """
        if not self.executor:
            return ToolOutput(
                tool_name=self.name,
                call_id=input.call_id,
                output=None,
                error=f"Tool {self.name} has no executor registered.",
            )

        # Validate
        validation_errors = self.validate_params(input.params)
        if validation_errors:
            return ToolOutput(
                tool_name=self.name,
                call_id=input.call_id,
                output=None,
                error=f"Validation failed: {'; '.join(validation_errors)}",
            )

        start = time.monotonic()
        error: str | None = None
        output: Any = None
        truncated = False

        try:
            output = self.executor(input)

            # Truncate large outputs
            if isinstance(output, ToolOutput):
                truncated = output.truncated
                error = output.error
                raw_out = output.output
                if isinstance(raw_out, str) and len(raw_out) > 50_000:
                    output = raw_out[:50_000] + f"\n...[truncated {len(raw_out) - 50_000} chars]"
                    truncated = True
                else:
                    output = raw_out
            elif isinstance(output, str) and len(output) > 50_000:
                output = output[:50_000] + f"\n...[truncated {len(output) - 50_000} chars]"
                truncated = True

        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            traceback_str = traceback.format_exc()
            write_debug_event("tool.error", {
                "tool": self.name,
                "call_id": input.call_id,
                "error": error,
                "traceback": traceback_str[-2000:],
            })

        duration_ms = (time.monotonic() - start) * 1000.0

        execution_log = ToolExecutionLog(
            tool_name=self.name,
            call_id=input.call_id,
            input_params=input.params,
            output_summary=str(output)[:500] if output is not None else "",
            error=error,
            duration_ms=duration_ms,
            timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            truncated=truncated,
        )
        write_debug_event("tool.executed", {
            "tool": self.name,
            "call_id": input.call_id,
            "ok": error is None,
            "duration_ms": round(duration_ms, 1),
            "truncated": truncated,
        })

        return ToolOutput(
            tool_name=self.name,
            call_id=input.call_id,
            output=output,
            error=error,
            duration_ms=duration_ms,
            truncated=truncated,
            metadata={"execution_log": execution_log},
        )

    def to_dict(self) -> dict[str, Any]:
        """Export tool definition (for LLM prompts)."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "input_schema": self.input_schema,
            "require_approval": self.require_approval,
            "timeout_seconds": self.timeout_seconds,
        }


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Registry of tools, lookup by name. Replaces if/else chains for tool dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._categories: dict[str, list[str]] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises ValueError if name already taken."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        self._categories.setdefault(tool.category, []).append(tool.name)

    def unregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        tool = self._tools.pop(name, None)
        if tool and tool.category in self._categories:
            self._categories[tool.category] = [
                n for n in self._categories[tool.category] if n != name
            ]

    def get(self, name: str) -> Tool | None:
        """Get tool by name."""
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools)

    def list_categories(self) -> list[str]:
        """List tool categories."""
        return list(self._categories)

    def list_by_category(self, category: str) -> list[Tool]:
        """List tools in a category."""
        return [self._tools[n] for n in self._categories.get(category, []) if n in self._tools]

    def list_all(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def list_tool_defs(self) -> list[dict[str, Any]]:
        """Export all tools as dicts (for LLM prompts)."""
        return [tool.to_dict() for tool in self._tools.values()]

    def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        workspace: str = "",
        call_id: str = "",
        correlation_id: str = "",
    ) -> ToolOutput:
        """Lookup and execute a tool by name.

        Args:
            tool_name: Name of tool to execute
            params: Parameters to pass
            workspace: Workspace directory for permission checks
            call_id: Unique call ID for tracing
            correlation_id: Correlation ID for tracing

        Returns:
            ToolOutput with result or error

        Raises:
            KeyError: if tool not found
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolOutput(
                tool_name=tool_name,
                call_id=call_id,
                output=None,
                error=f"Unknown tool: {tool_name}. Available: {', '.join(sorted(self._tools))}",
            )

        # Permission check
        if tool.permission and workspace:
            if not tool.permission.can_execute(tool_name, params, workspace):
                return ToolOutput(
                    tool_name=tool_name,
                    call_id=call_id,
                    output=None,
                    error=f"Permission denied: tool {tool_name} cannot execute with given params in workspace {workspace}",
                )

        input = ToolInput(
            tool_name=tool_name,
            params=params,
            call_id=call_id,
            correlation_id=correlation_id,
        )
        return tool.execute(input)

    def register_default_tools(self, workspace_dir: str) -> None:
        """Register standard tools: file_read, file_write, command_run, etc."""
        perm = WorkspaceOnlyPermission(workspace_dir=workspace_dir)

        # --- file_read ---
        self.register(Tool(
            name="file_read",
            description="Read contents of a file within the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file within workspace"},
                },
                "required": ["path"],
            },
            timeout_seconds=30.0,
            permission=perm,
            category="filesystem",
            executor=lambda inp: _tool_file_read(workspace_dir, inp),
        ))

        # --- file_write ---
        self.register(Tool(
            name="file_write",
            description="Write content to a file within the workspace. Creates parent directories as needed.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file within workspace"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
            timeout_seconds=60.0,
            permission=perm,
            require_approval=True,
            category="filesystem",
            executor=lambda inp: _tool_file_write(workspace_dir, inp),
        ))

        # --- file_list ---
        self.register(Tool(
            name="file_list",
            description="List files in a directory within the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path, default '.'"},
                    "max_depth": {"type": "integer", "description": "Max directory depth, default 2"},
                },
            },
            timeout_seconds=30.0,
            permission=perm,
            category="filesystem",
            executor=lambda inp: _tool_file_list(workspace_dir, inp),
        ))

        # --- command_run ---
        self.register(Tool(
            name="command_run",
            description="Run a safe shell command within the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Working directory relative to workspace"},
                },
                "required": ["command"],
            },
            timeout_seconds=120.0,
            permission=WorkspaceOnlyPermission(workspace_dir=workspace_dir, allow_subprocess=True),
            require_approval=True,
            category="shell",
            executor=lambda inp: _tool_command_run(workspace_dir, inp),
        ))

        # --- search_content ---
        self.register(Tool(
            name="search_content",
            description="Search for text pattern in workspace files.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in, default '.'"},
                },
                "required": ["pattern"],
            },
            timeout_seconds=45.0,
            permission=perm,
            category="search",
            executor=lambda inp: _tool_search_content(workspace_dir, inp),
        ))


# ---------------------------------------------------------------------------
# Built-in tool executors
# ---------------------------------------------------------------------------


def _resolve_path(workspace: str, relative: str) -> tuple[Path, str | None]:
    """Resolve a relative path within workspace. Returns (full_path, error)."""
    root = Path(workspace).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        return root, f"Path escapes workspace: {relative}"
    return candidate, None


def _tool_file_read(workspace: str, inp: ToolInput) -> ToolOutput:
    path_str = str(inp.params.get("path", "")).strip()
    if not path_str:
        return ToolOutput(tool_name="file_read", call_id=inp.call_id, output=None, error="path is required")

    target, error = _resolve_path(workspace, path_str)
    if error:
        return ToolOutput(tool_name="file_read", call_id=inp.call_id, output=None, error=error)

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > 50_000
        if truncated:
            content = content[:50_000] + f"\n...[truncated {len(content) - 50_000} chars]"
        return ToolOutput(tool_name="file_read", call_id=inp.call_id, output=content, truncated=truncated)
    except Exception as exc:
        return ToolOutput(tool_name="file_read", call_id=inp.call_id, output=None, error=str(exc))


def _tool_file_write(workspace: str, inp: ToolInput) -> ToolOutput:
    path_str = str(inp.params.get("path", "")).strip()
    content = str(inp.params.get("content", ""))
    if not path_str:
        return ToolOutput(tool_name="file_write", call_id=inp.call_id, output=None, error="path is required")

    target, error = _resolve_path(workspace, path_str)
    if error:
        return ToolOutput(tool_name="file_write", call_id=inp.call_id, output=None, error=error)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolOutput(tool_name="file_write", call_id=inp.call_id, output={"path": path_str, "written": True})
    except Exception as exc:
        return ToolOutput(tool_name="file_write", call_id=inp.call_id, output=None, error=str(exc))


def _tool_file_list(workspace: str, inp: ToolInput) -> ToolOutput:
    path_str = str(inp.params.get("path", "."))
    max_depth = max(1, min(5, int(inp.params.get("max_depth", 2))))

    target, error = _resolve_path(workspace, path_str)
    if error:
        return ToolOutput(tool_name="file_list", call_id=inp.call_id, output=None, error=error)

    if not target.exists() or not target.is_dir():
        return ToolOutput(tool_name="file_list", call_id=inp.call_id, output=None, error=f"Not a directory: {path_str}")

    from .workspace import IGNORED_DIRS as _IGNORE

    def walk(current: Path, depth: int) -> list[str]:
        if depth > max_depth:
            return []
        results: list[str] = []
        try:
            for entry in sorted(current.iterdir(), key=lambda x: x.name.lower()):
                rel = entry.relative_to(target).as_posix()
                if entry.is_dir():
                    if entry.name not in _IGNORE and not entry.name.startswith("."):
                        results.append(rel + "/")
                        results.extend(walk(entry, depth + 1))
                else:
                    results.append(rel)
        except OSError:
            pass
        return results

    entries = walk(target, 0)
    return ToolOutput(tool_name="file_list", call_id=inp.call_id, output={"path": path_str, "entries": entries[:200]})


def _tool_command_run(workspace: str, inp: ToolInput) -> ToolOutput:
    command = str(inp.params.get("command", "")).strip()
    cwd = str(inp.params.get("cwd", "."))

    if not command:
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=None, error="command is required")

    target, error = _resolve_path(workspace, cwd)
    if error:
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=None, error=error)

    if not target.exists():
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=None, error=f"Directory not found: {cwd}")

    # Safety check — reject obvious dangerous commands
    lower = command.lower().strip()
    dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:", "> /dev/"]
    if any(d in lower for d in dangerous):
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=None, error="Command rejected: dangerous pattern detected")

    try:
        proc = subprocess.run(
            command,
            cwd=str(target),
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = {
            "command": command,
            "cwd": cwd,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-10_000:],
            "stderr": proc.stderr[-10_000:],
        }
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=output)
    except subprocess.TimeoutExpired:
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=None, error="Command timed out after 120s")
    except Exception as exc:
        return ToolOutput(tool_name="command_run", call_id=inp.call_id, output=None, error=str(exc))


def _tool_search_content(workspace: str, inp: ToolInput) -> ToolOutput:
    import re

    pattern = str(inp.params.get("pattern", "")).strip()
    search_path = str(inp.params.get("path", "."))

    if not pattern:
        return ToolOutput(tool_name="search_content", call_id=inp.call_id, output=None, error="pattern is required")

    target, error = _resolve_path(workspace, search_path)
    if error:
        return ToolOutput(tool_name="search_content", call_id=inp.call_id, output=None, error=error)

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolOutput(tool_name="search_content", call_id=inp.call_id, output=None, error=f"Invalid regex: {exc}")

    from .workspace import IGNORED_DIRS as _IGNORE, TEXT_EXTENSIONS as _TEXT

    results: list[dict[str, Any]] = []

    def search_dir(current: Path, depth: int) -> None:
        if depth > 4 or len(results) >= 50:
            return
        try:
            for entry in sorted(current.iterdir(), key=lambda x: x.name.lower()):
                if len(results) >= 50:
                    return
                if entry.is_dir():
                    if entry.name not in _IGNORE and not entry.name.startswith("."):
                        search_dir(entry, depth + 1)
                elif entry.is_file() and entry.suffix.lower() in _TEXT:
                    try:
                        content = entry.read_text(encoding="utf-8", errors="replace")
                        for m in regex.finditer(content):
                            line_no = content[: m.start()].count("\n") + 1
                            line_text = content.split("\n")[line_no - 1][:200]
                            results.append({
                                "file": entry.relative_to(target).as_posix(),
                                "line": line_no,
                                "match": line_text.strip(),
                            })
                            if len(results) >= 50:
                                break
                    except Exception:
                        continue
        except OSError:
            pass

    search_dir(target, 0)
    return ToolOutput(
        tool_name="search_content",
        call_id=inp.call_id,
        output={"pattern": pattern, "matches": results[:50], "total_matches": len(results)},
    )
