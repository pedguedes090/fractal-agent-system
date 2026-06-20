from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import fnmatch
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from openhands.tools.terminal.definition import TerminalAction, TerminalObservation
from openhands.tools.terminal.metadata import CmdOutputMetadata
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation
from openhands.tools.file_editor.impl import FileEditorExecutor

from . import telemetry
from .durable_execution import checkpoint_step

if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState


DEFAULT_IMAGES = {
    "node": "node:24-bookworm-slim",
    "python": "python:3.12-slim",
    "go": "golang:1.24-bookworm",
    "rust": "rust:1.87-bookworm",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:9.0",
    "generic": "debian:bookworm-slim",
}


def detect_container_runtime() -> str | None:
    explicit = str(os.getenv("AGENT_CONTAINER_RUNTIME") or "").strip()
    if explicit:
        return shutil.which(explicit) or (explicit if Path(explicit).exists() else None)
    return shutil.which("docker") or shutil.which("podman")


def image_for_stack(stack: str) -> str:
    normalized = str(stack or "generic").lower()
    env_name = f"AGENT_SANDBOX_IMAGE_{normalized.upper()}"
    return str(os.getenv(env_name) or DEFAULT_IMAGES.get(normalized) or DEFAULT_IMAGES["generic"])


def infer_stack_from_command(command: str, fallback: str = "generic") -> str:
    value = str(command or "").lower()
    if re.search(r"\b(npm|pnpm|yarn|node|npx)\b", value):
        return "node"
    if re.search(r"\b(python|pytest|pip|uv)\b", value):
        return "python"
    if re.search(r"\b(go test|go build|golang)\b", value):
        return "go"
    if re.search(r"\b(cargo|rustc)\b", value):
        return "rust"
    if re.search(r"\b(dotnet)\b", value):
        return "dotnet"
    return fallback


def container_status(stack: str = "generic") -> dict[str, Any]:
    runtime = detect_container_runtime()
    image = image_for_stack(stack)
    if not runtime:
        return {
            "ready": False,
            "runtime": None,
            "image": image,
            "reason": "Docker or Podman is not available; falling back to policy-limited local execution when the workflow allows it.",
        }
    try:
        version = subprocess.run([runtime, "version"], capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return {"ready": False, "runtime": runtime, "image": image, "reason": str(exc)}
    if version.returncode != 0:
        return {
            "ready": False,
            "runtime": runtime,
            "image": image,
            "reason": (version.stderr or version.stdout or "Container runtime is not healthy.").strip(),
        }
    allow_pull = str(os.getenv("AGENT_SANDBOX_ALLOW_PULL") or "").lower() in {"1", "true", "yes"}
    if not allow_pull:
        inspect = subprocess.run([runtime, "image", "inspect", image], capture_output=True, text=True, timeout=20)
        if inspect.returncode != 0:
            return {
                "ready": False,
                "runtime": runtime,
                "image": image,
                "reason": f"Sandbox image {image} is not present locally. Pre-pull it or set AGENT_SANDBOX_ALLOW_PULL=true.",
            }
    return {
        "ready": True,
        "runtime": runtime,
        "image": image,
        "network": "none",
        "rootFilesystem": "read-only",
        "capabilities": "drop-all",
        "allowPull": allow_pull,
    }


def _container_command(
    *,
    runtime: str,
    image: str,
    workspace: Path,
    command: str,
    cwd: str,
    dependency_workspace: Path | None,
    allow_pull: bool,
) -> list[str]:
    workspace = workspace.resolve()
    raw_cwd = str(cwd or ".").replace("\\", "/").strip()
    if raw_cwd.startswith("/") or re.match(r"^[A-Za-z]:", raw_cwd):
        raise ValueError(f"Container cwd must be relative to the execution worktree: {cwd}")
    candidate = (workspace / raw_cwd).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(f"Container cwd escapes the execution worktree: {cwd}")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Container cwd does not exist in the execution worktree: {cwd}")
    relative_cwd = candidate.relative_to(workspace).as_posix()
    workdir = "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}"
    args = [
        runtime,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(max(32, int(os.getenv("AGENT_SANDBOX_PIDS_LIMIT", "256")))),
        "--memory",
        str(os.getenv("AGENT_SANDBOX_MEMORY", "2g")),
        "--cpus",
        str(os.getenv("AGENT_SANDBOX_CPUS", "2")),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        f"type=bind,source={workspace},target=/workspace",
        "--workdir",
        workdir,
    ]
    if platform.system() != "Windows" and hasattr(os, "getuid") and hasattr(os, "getgid"):
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    dependency_root = dependency_workspace.resolve() if dependency_workspace else None
    if dependency_root and (dependency_root / "node_modules").exists() and not (workspace / "node_modules").exists():
        args.extend(
            [
                "--mount",
                f"type=bind,source={dependency_root / 'node_modules'},target=/workspace/node_modules,readonly",
            ]
        )
    if not allow_pull:
        args.extend(["--pull", "never"])
    args.extend([image, "sh", "-lc", command])
    return args


def run_container_command(
    workspace: str,
    command: str,
    *,
    cwd: str = ".",
    timeout: int = 120,
    stack: str = "generic",
    dependency_workspace: str | None = None,
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    selected_stack = infer_stack_from_command(command, stack)
    try:
        args = _container_command(
            runtime="__runtime__",
            image="__image__",
            workspace=root,
            command=command,
            cwd=cwd,
            dependency_workspace=Path(dependency_workspace) if dependency_workspace else None,
            allow_pull=False,
        )
    except ValueError as exc:
        return {
            "command": command,
            "cwd": cwd,
            "code": None,
            "stdout": "",
            "stderr": str(exc),
            "timedOut": False,
            "sandboxed": False,
            "container": {"ready": False, "reason": str(exc)},
        }
    status = container_status(selected_stack)
    if not status.get("ready"):
        return {
            "command": command,
            "cwd": cwd,
            "code": None,
            "stdout": "",
            "stderr": status.get("reason", "Container sandbox unavailable."),
            "timedOut": False,
            "sandboxed": False,
            "container": status,
        }
    runtime = str(status["runtime"])
    image = str(status["image"])
    args[0] = runtime
    args[-4] = image
    if status.get("allowPull"):
        pull_index = args.index("--pull")
        del args[pull_index : pull_index + 2]
    with checkpoint_step(
        "tool",
        "container_command",
        {"command": command, "cwd": cwd, "workspace": str(root), "runtime": runtime, "image": image},
    ) as durable_step:
        with telemetry.start_span(
            "sandbox.container_command",
            {
                "tool.command": command,
                "tool.cwd": cwd,
                "container.runtime": runtime,
                "container.image": image,
                "container.network": "none",
            },
        ) as span:
            try:
                proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
                telemetry.set_span_attrs(span, {"tool.exit_code": proc.returncode, "tool.timed_out": False})
                result = {
                    "command": command,
                    "cwd": cwd,
                    "code": proc.returncode,
                    "stdout": proc.stdout[-20000:],
                    "stderr": proc.stderr[-20000:],
                    "timedOut": False,
                    "sandboxed": True,
                    "container": status,
                }
            except subprocess.TimeoutExpired as exc:
                telemetry.record_sandbox_failure("container_command_timeout")
                telemetry.set_span_attrs(span, {"tool.timed_out": True})
                result = {
                    "command": command,
                    "cwd": cwd,
                    "code": None,
                    "stdout": str(exc.stdout or "")[-20000:],
                    "stderr": str(exc.stderr or "")[-20000:],
                    "timedOut": True,
                    "sandboxed": True,
                    "container": status,
                }
            except Exception as exc:
                telemetry.record_sandbox_failure(exc.__class__.__name__)
                telemetry.set_span_attrs(span, {"tool.error": str(exc)})
                result = {
                    "command": command,
                    "cwd": cwd,
                    "code": None,
                    "stdout": "",
                    "stderr": str(exc),
                    "timedOut": False,
                    "sandboxed": False,
                    "container": status,
                }
        durable_step.set_output(result)
        return result


class ContainerTerminalExecutor(ToolExecutor[TerminalAction, TerminalObservation]):
    def __init__(self, workspace: str, stack: str, dependency_workspace: str | None = None) -> None:
        self.workspace = workspace
        self.stack = stack
        self.dependency_workspace = dependency_workspace

    def __call__(self, action: TerminalAction, conversation: Any = None) -> TerminalObservation:
        if action.is_input:
            return TerminalObservation.from_text(
                "Interactive process input is disabled in the ephemeral container sandbox.",
                is_error=True,
                command=action.command,
                exit_code=2,
                metadata=CmdOutputMetadata(exit_code=2, working_dir="/workspace"),
            )
        result = run_container_command(
            self.workspace,
            action.command,
            timeout=max(1, int(action.timeout or 120)),
            stack=self.stack,
            dependency_workspace=self.dependency_workspace,
        )
        text = "\n".join(part for part in [result.get("stdout", ""), result.get("stderr", "")] if part).strip()
        code = result.get("code")
        return TerminalObservation.from_text(
            text,
            is_error=code not in (0, None) or not result.get("sandboxed"),
            command=action.command,
            exit_code=code,
            timeout=bool(result.get("timedOut")),
            metadata=CmdOutputMetadata(exit_code=code if code is not None else -1, working_dir="/workspace"),
        )


class ContainerTerminalTool(ToolDefinition[TerminalAction, TerminalObservation]):
    name = "container_terminal"

    def declared_resources(self, action: Action) -> DeclaredResources:
        return DeclaredResources(keys=("container:workspace",), declared=True)

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",
        stack: str = "generic",
        dependency_workspace: str | None = None,
    ) -> Sequence["ContainerTerminalTool"]:
        executor = ContainerTerminalExecutor(
            workspace=str(conv_state.workspace.working_dir),
            stack=stack,
            dependency_workspace=dependency_workspace,
        )
        return [
            cls(
                action_type=TerminalAction,
                observation_type=TerminalObservation,
                description=(
                    "Run exactly one non-interactive shell command inside a locked-down ephemeral container. "
                    "The project is mounted at /workspace, network access is disabled, Linux capabilities are "
                    "dropped, and the container root filesystem is read-only. Use file_editor for file changes."
                ),
                annotations=ToolAnnotations(
                    title="container_terminal",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]


register_tool(ContainerTerminalTool.name, ContainerTerminalTool)


def _policy_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("./").strip("/")


def _matches_path(path: str, patterns: list[str]) -> bool:
    normalized = _policy_path(path)
    for raw in patterns:
        pattern = _policy_path(raw)
        if not pattern:
            continue
        if pattern == normalized or fnmatch.fnmatch(normalized, pattern):
            return True
        if pattern.endswith("/**") and (normalized == pattern[:-3] or normalized.startswith(pattern[:-2])):
            return True
    return False


class PolicyFileEditorExecutor(ToolExecutor[FileEditorAction, FileEditorObservation]):
    def __init__(self, workspace: str, allowed_files: list[str], forbidden_paths: list[str]) -> None:
        self.workspace = Path(workspace).resolve()
        self.allowed_files = list(allowed_files)
        self.forbidden_paths = list(forbidden_paths)
        self.delegate = FileEditorExecutor(workspace_root=str(self.workspace))

    def __call__(self, action: FileEditorAction, conversation: Any = None) -> FileEditorObservation:
        raw_path = str(action.path)
        if raw_path.replace("\\", "/").startswith("/workspace/"):
            target = self.workspace / raw_path.replace("\\", "/").removeprefix("/workspace/")
        else:
            candidate = Path(raw_path)
            target = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
        if target != self.workspace and self.workspace not in target.parents:
            return FileEditorObservation.from_text(
                "File access is outside the execution worktree.",
                command=action.command,
                is_error=True,
            )
        relative = target.relative_to(self.workspace).as_posix()
        if _matches_path(relative, self.forbidden_paths):
            return FileEditorObservation.from_text(
                f"File access is blocked by forbiddenPaths policy: {relative}",
                command=action.command,
                is_error=True,
            )
        if action.command != "view" and not _matches_path(relative, self.allowed_files):
            return FileEditorObservation.from_text(
                f"File edit is outside allowedFiles policy: {relative}",
                command=action.command,
                is_error=True,
            )
        if action.command == "create":
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return FileEditorObservation.from_text(
                    f"Could not create parent directories for {relative}: {exc}",
                    command=action.command,
                    is_error=True,
                )
        return self.delegate(action.model_copy(update={"path": str(target)}), conversation)


class PolicyFileEditorTool(ToolDefinition[FileEditorAction, FileEditorObservation]):
    name = "policy_file_editor"

    def declared_resources(self, action: Action) -> DeclaredResources:
        return DeclaredResources(keys=(f"file:{action.path}",), declared=True)

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",
        allowed_files: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
    ) -> Sequence["PolicyFileEditorTool"]:
        executor = PolicyFileEditorExecutor(
            workspace=str(conv_state.workspace.working_dir),
            allowed_files=list(allowed_files or []),
            forbidden_paths=list(forbidden_paths or []),
        )
        return [
            cls(
                action_type=FileEditorAction,
                observation_type=FileEditorObservation,
                description=(
                    "Read or edit files only inside the execution worktree. Reads matching forbiddenPaths "
                    "are denied, and writes must match allowedFiles. The /workspace prefix maps to the "
                    "execution worktree. Creating a file also creates its missing parent directories."
                ),
                annotations=ToolAnnotations(
                    title="policy_file_editor",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
                executor=executor,
            )
        ]


register_tool(PolicyFileEditorTool.name, PolicyFileEditorTool)
