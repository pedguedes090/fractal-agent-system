from __future__ import annotations

import os
import json
import re
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import telemetry
from .container_sandbox import ContainerTerminalTool, PolicyFileEditorTool, container_status
from .debug_log import write_debug_event
from .durable_execution import execution_artifact_dir, is_transient_error, record_checkpoint
from .project_scaffold import scaffold_project_fallback, should_scaffold_todo_fallback
from .workspace import apply_sandbox_changes, create_workspace_sandbox, enforce_change_policy, file_snapshots, pick_execution_root


def _safe_model_dump(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list, str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return value.model_dump(exclude_none=True)
        except Exception:
            return str(value)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def _compact_text(value: Any, limit: int = 420) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.replace("\r", "\n").split())
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def _find_first(value: Any, names: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for name in names:
            item = value.get(name)
            if item not in (None, ""):
                return item
        for item in value.values():
            found = _find_first(item, names)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first(item, names)
            if found not in (None, ""):
                return found
    return None


def _event_summary(event: Any) -> tuple[str, str]:
    name = event.__class__.__name__
    tool_name = str(getattr(event, "tool_name", "") or "")

    if name == "ActionEvent":
        action = _safe_model_dump(getattr(event, "action", None))
        tool_call = _safe_model_dump(getattr(event, "tool_call", None))
        payload = {"action": action, "tool_call": tool_call}
        summary = _compact_text(getattr(event, "summary", "") or "")

        if tool_name == "terminal":
            command = _find_first(payload, ("command", "cmd", "input", "code"))
            return "openhands_action", f"terminal: {_compact_text(command) or summary or 'running command'}"

        if tool_name == "file_editor":
            path = _find_first(payload, ("path", "file_path", "filename", "target_file"))
            operation = _find_first(payload, ("command", "operation", "action", "tool_call_name"))
            bits = [str(item) for item in (operation, path) if item]
            return "openhands_action", f"file_editor: {_compact_text(' '.join(bits)) or summary or 'editing file'}"

        if tool_name == "task_tracker":
            task = _find_first(payload, ("task", "todo", "text", "content", "title"))
            return "openhands_action", f"task_tracker: {_compact_text(task) or summary or 'updating tasks'}"

        detail = summary or _compact_text(payload)
        return "openhands_action", f"{tool_name or name}: {detail}"

    if name == "ObservationEvent":
        observation = getattr(event, "observation", None)
        text = getattr(observation, "text", None)
        if not text:
            text = _safe_model_dump(observation)
        return "openhands_observation", f"{tool_name or 'tool'}: {_compact_text(text, 520)}"

    if name == "MessageEvent":
        skills = getattr(event, "activated_skills", None) or []
        if skills:
            return "openhands_message", f"Activated skills: {_compact_text(skills)}"
        return "openhands_message", "Agent is thinking"

    return "openhands_event", name


def _load_json_config(paths: list[Path]) -> tuple[Path | None, Any]:
    for path in paths:
        if not path.exists():
            continue
        return path, json.loads(path.read_text(encoding="utf-8"))
    return None, None


_MCP_SAFE_COMMANDS = {"node", "python", "python3", "uv", "uvx", "npx", "pnpm", "bun"}
_MCP_SECRET_ENV_RE = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private[_-]?key|authorization)",
    re.IGNORECASE,
)
_MCP_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_MCP_SHELL_META_RE = re.compile(r"[;&|`<>$]")


def _workspace_relative(root: Path, value: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    return None


def _safe_mcp_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _MCP_SHELL_META_RE.search(text):
        return None
    return text


def _validate_mcp_secrets(container: dict[str, Any], label: str, emit: Callable[[str, str], None]) -> bool:
    for key, value in container.items():
        if not _MCP_SECRET_ENV_RE.search(str(key)):
            continue
        if isinstance(value, str) and _MCP_PLACEHOLDER_RE.match(value.strip()):
            continue
        emit("openhands_mcp", f"Ignored MCP server {label}: secret field {key} must use ${{ENV_VAR}} placeholder")
        return False
    return True


def _sanitize_mcp_server(
    *,
    root: Path,
    name: str,
    config: Any,
    trusted_servers: set[str],
    allowed_commands: set[str],
    emit: Callable[[str, str], None],
) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        emit("openhands_mcp", f"Ignored MCP server {name}: expected object config")
        return None
    if config.get("enabled", True) is False:
        return None
    if name not in trusted_servers and not bool(config.get("trusted") or config.get("allow")):
        emit("openhands_mcp", f"Ignored MCP server {name}: not listed in trustedServers")
        return None

    sanitized = {key: value for key, value in config.items() if key not in {"trusted", "allow", "enabled"}}
    command = sanitized.get("command")
    url = sanitized.get("url")
    if command:
        command_text = _safe_mcp_string(command)
        if not command_text:
            emit("openhands_mcp", f"Ignored MCP server {name}: unsafe command")
            return None
        path_like = any(separator in command_text for separator in ("/", "\\")) or command_text.startswith(".")
        if path_like:
            # Allow an out-of-workspace absolute path ONLY when the user explicitly
            # listed the exact command string in allowedCommands (e.g. auto-injected
            # codebase-memory-mcp binary from %LOCALAPPDATA%\Programs\...).
            if command_text in allowed_commands and Path(command_text).exists():
                sanitized["command"] = command_text
            else:
                resolved = _workspace_relative(root, command_text)
                if not resolved or not resolved.exists():
                    emit("openhands_mcp", f"Ignored MCP server {name}: command must stay inside workspace and exist")
                    return None
                sanitized["command"] = str(resolved)
        elif command_text not in allowed_commands:
            emit("openhands_mcp", f"Ignored MCP server {name}: command {command_text} is not allowlisted")
            return None

        args = sanitized.get("args", [])
        if args is None:
            args = []
        if not isinstance(args, list):
            emit("openhands_mcp", f"Ignored MCP server {name}: args must be an array")
            return None
        normalized_args: list[str] = []
        for arg in args:
            arg_text = _safe_mcp_string(arg)
            if arg_text is None:
                emit("openhands_mcp", f"Ignored MCP server {name}: unsafe arg")
                return None
            parsed = urlparse(arg_text)
            if not parsed.scheme and (arg_text.startswith(("/", "\\", "../", "..\\", "./", ".\\"))):
                if not _workspace_relative(root, arg_text):
                    emit("openhands_mcp", f"Ignored MCP server {name}: arg path escapes workspace")
                    return None
            normalized_args.append(arg_text)
        sanitized["args"] = normalized_args

        cwd = sanitized.get("cwd") or sanitized.get("workingDirectory")
        if cwd:
            resolved_cwd = _workspace_relative(root, str(cwd))
            if not resolved_cwd or not resolved_cwd.exists() or not resolved_cwd.is_dir():
                emit("openhands_mcp", f"Ignored MCP server {name}: cwd must be an existing workspace directory")
                return None
            sanitized["cwd"] = str(resolved_cwd)
            sanitized.pop("workingDirectory", None)

    if url:
        parsed = urlparse(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            emit("openhands_mcp", f"Ignored MCP server {name}: url must be http(s)")
            return None

    if not command and not url:
        emit("openhands_mcp", f"Ignored MCP server {name}: missing command or url")
        return None

    env = sanitized.get("env")
    if env is not None:
        if not isinstance(env, dict) or not _validate_mcp_secrets(env, name, emit):
            return None

    headers = sanitized.get("headers")
    if headers is not None:
        if not isinstance(headers, dict) or not _validate_mcp_secrets(headers, name, emit):
            return None

    return sanitized


def _sanitize_mcp_config(root: Path, raw: dict[str, Any], emit: Callable[[str, str], None]) -> dict[str, Any]:
    servers = raw.get("mcpServers") if isinstance(raw.get("mcpServers"), dict) else raw.get("servers")
    if not isinstance(servers, dict):
        emit("openhands_mcp", "Ignored MCP config: expected mcpServers object")
        return {}

    trusted = raw.get("trustedServers", [])
    if isinstance(trusted, str):
        trusted_servers = {trusted}
    elif isinstance(trusted, list):
        trusted_servers = {str(item) for item in trusted if str(item).strip()}
    else:
        trusted_servers = set()

    allowed = raw.get("allowedCommands", [])
    allowed_commands = set(_MCP_SAFE_COMMANDS)
    if isinstance(allowed, list):
        allowed_commands.update(str(item).strip() for item in allowed if str(item).strip())

    sanitized_servers: dict[str, Any] = {}
    for name, config in servers.items():
        server_name = str(name)
        sanitized = _sanitize_mcp_server(
            root=root,
            name=server_name,
            config=config,
            trusted_servers=trusted_servers,
            allowed_commands=allowed_commands,
            emit=emit,
        )
        if sanitized:
            sanitized_servers[server_name] = sanitized

    if not sanitized_servers:
        return {}
    return {"mcpServers": sanitized_servers}


def _load_plugin_sources(workspace: str, emit: Callable[[str, str], None]) -> list[Any]:
    try:
        from openhands.sdk.plugin import PluginSource
    except Exception as exc:
        emit("openhands_plugins", f"Plugin API unavailable: {exc}")
        return []

    root = Path(workspace)
    try:
        config_path, raw = _load_json_config([root / ".openhands" / "plugins.json", root / ".openhands-plugins.json"])
    except Exception as exc:
        emit("openhands_plugins", f"Ignored plugin config: {exc}")
        return []
    if raw is None:
        return []

    entries = raw.get("plugins") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        emit("openhands_plugins", f"Ignored {config_path.name}: expected a plugins array")
        return []

    plugins = []
    for entry in entries:
        if isinstance(entry, str):
            source, ref, repo_path, enabled = entry, None, None, True
        elif isinstance(entry, dict):
            source = entry.get("source")
            ref = entry.get("ref")
            repo_path = entry.get("repo_path") or entry.get("repoPath")
            enabled = entry.get("enabled", True)
        else:
            continue
        if not enabled or not source:
            continue
        source = str(source)
        if not (source.startswith(("github:", "http://", "https://", "git@")) or Path(source).is_absolute()):
            source = str((root / source).resolve())
        plugins.append(PluginSource(source=source, ref=ref, repo_path=repo_path))

    if plugins:
        emit("openhands_plugins", f"Loaded {len(plugins)} plugin(s) from {config_path.relative_to(root)}")
    return plugins


def _augment_with_codebase_memory(
    root: Path,
    raw: dict[str, Any] | None,
    emit: Callable[[str, str], None],
) -> dict[str, Any] | None:
    """If codebase-memory-mcp binary is installed, auto-inject it into the MCP config.

    Bypasses the workspace-relative path check (binary lives in the user's install dir)
    by short-circuiting the sanitizer for this specific known-trusted server.
    """
    from . import codebase_memory  # local import — avoids circular at module load

    cfg = codebase_memory.McpServerConfig.detect()
    if cfg is None:
        return raw
    raw = dict(raw or {})
    servers = dict(raw.get("mcpServers") or {})
    if cfg.name in servers:
        return raw  # user defined their own — respect it
    servers[cfg.name] = cfg.as_mcp_server_entry()
    raw["mcpServers"] = servers
    trusted = list(raw.get("trustedServers") or [])
    if cfg.name not in trusted:
        trusted.append(cfg.name)
        raw["trustedServers"] = trusted
    allowed = list(raw.get("allowedCommands") or [])
    if cfg.command not in allowed:
        allowed.append(cfg.command)
        raw["allowedCommands"] = allowed
    # Best-effort: make sure the workspace is indexed so the MCP tools have data to query.
    try:
        codebase_memory.ensure_indexed(str(root))
    except Exception:
        pass
    emit("openhands_mcp", f"Auto-injected codebase-memory-mcp from {cfg.command}")
    return raw


def _load_mcp_config(workspace: str, emit: Callable[[str, str], None]) -> dict[str, Any]:
    root = Path(workspace)
    try:
        config_path, raw = _load_json_config([root / ".openhands" / "mcp.json", root / ".mcp.json"])
    except Exception as exc:
        emit("openhands_mcp", f"Ignored MCP config: {exc}")
        return {}
    # Auto-augment with codebase-memory-mcp if available — even if no file exists.
    raw = _augment_with_codebase_memory(root.resolve(), raw, emit)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        emit("openhands_mcp", f"Ignored {config_path.name if config_path else 'config'}: expected a JSON object")
        return {}
    sanitized = _sanitize_mcp_config(root.resolve(), raw, emit)
    if not sanitized:
        source = config_path.relative_to(root) if config_path else "(auto-detected)"
        emit("openhands_mcp", f"No trusted MCP servers loaded from {source}")
        return {}
    server_count = len(sanitized.get("mcpServers") or {})
    source = config_path.relative_to(root) if config_path else "(auto-detected)"
    emit("openhands_mcp", f"Loaded {server_count} trusted MCP server(s) from {source}")
    write_debug_event(
        "openhands.mcp_loaded",
        {
            "configPath": str(source),
            "serverNames": sorted((sanitized.get("mcpServers") or {}).keys()),
        },
    )
    return sanitized


def _worker_result(
    *,
    workspace: str,
    worker_task_spec: dict[str, Any],
    summary: str,
    error: str | None,
    model: str | None,
    raw_result: Any = None,
    sandbox_diff: list[dict[str, Any]] | None = None,
    policy_violations: list[dict[str, Any]] | None = None,
    applied_changes: list[dict[str, Any]] | None = None,
    scaffold_fallback: dict[str, Any] | None = None,
    sandboxed: bool = True,
    events: list[str] | None = None,
) -> dict[str, Any]:
    applied = list(applied_changes or [])
    result = {
        "summary": summary,
        "error": error,
        "model": model,
        "rawResult": str(raw_result)[:4000] if raw_result is not None else "",
        "sandboxDiff": list(sandbox_diff or []),
        "policyViolations": list(policy_violations or []),
        "appliedChanges": applied,
        "changedFiles": applied,
        "selectedExecutionRoot": pick_execution_root(workspace, {"changedFiles": applied}, worker_task_spec),
        "verificationSpec": {
            "commandsToRun": list(worker_task_spec.get("commandsToRun") or []),
            "verificationCommands": list(worker_task_spec.get("verificationCommands") or []),
            "verificationCwd": worker_task_spec.get("verificationCwd"),
            "projectRoot": worker_task_spec.get("projectRoot"),
            "projectStack": worker_task_spec.get("projectStack"),
        },
        "scaffoldFallback": scaffold_fallback,
        "sandboxed": sandboxed,
        "events": list(events or [])[-120:],
    }
    write_debug_event(
        "coder_agent.worker_result",
        {
            "summary": result["summary"],
            "error": result["error"],
            "sandboxDiff": result["sandboxDiff"],
            "policyViolations": result["policyViolations"],
            "appliedChanges": result["appliedChanges"],
            "selectedExecutionRoot": result["selectedExecutionRoot"],
        },
    )
    return result


def _execution_environment(worker_task_spec: dict[str, Any]) -> dict[str, Any]:
    envelope = worker_task_spec.get("contextEnvelope") or {}
    inputs = envelope.get("inputs") if isinstance(envelope, dict) else {}
    environment = (inputs or {}).get("executionEnvironment") if isinstance(inputs, dict) else {}
    return environment if isinstance(environment, dict) else {}


def _container_available(worker_task_spec: dict[str, Any]) -> bool:
    environment = _execution_environment(worker_task_spec)
    if "containerAvailable" in environment:
        return bool(environment.get("containerAvailable"))
    status = container_status(str(worker_task_spec.get("projectStack") or "generic"))
    return bool(status.get("ready"))


def _run_deterministic_scaffold_fallback(
    *,
    workspace: str,
    worker_task_spec: dict[str, Any],
    emit: Callable[[str, str], None],
    dependency_workspace: str | None,
    worktree_isolated: bool,
    reason: str,
) -> dict[str, Any]:
    before_snapshot = file_snapshots(dependency_workspace) if worktree_isolated and dependency_workspace else file_snapshots(workspace)
    emit("coder_agent", reason)
    fallback_result = scaffold_project_fallback(workspace, worker_task_spec)
    write_debug_event("coder_agent.scaffold_fallback", {**fallback_result, "reason": reason})
    policy = enforce_change_policy(
        workspace,
        before_snapshot,
        list(worker_task_spec.get("allowedFiles") or []),
        list(worker_task_spec.get("forbiddenPaths") or []),
    )
    applied = list(policy["changedFiles"])
    violations = policy["violations"]
    summary = "Coder agent used deterministic local scaffold fallback."
    error = None
    if violations:
        summary = "Deterministic local scaffold changes were filtered by allowedFiles policy."
        error = "Deterministic scaffold attempted to change files outside allowedFiles or inside forbiddenPaths."
    elif fallback_result.get("used") and not applied:
        summary = "Deterministic local scaffold produced no file changes."
        error = "Scaffold fallback completed without creating project files."
    return _worker_result(
        workspace=workspace,
        worker_task_spec=worker_task_spec,
        summary=summary,
        error=error,
        model=None,
        sandbox_diff=policy.get("sandboxDiff"),
        policy_violations=violations,
        applied_changes=applied,
        scaffold_fallback=fallback_result,
        sandboxed=False,
        events=[f"coder_agent: {reason}"],
    )


def run_openhands_worker(
    *,
    workspace: str,
    server_url: str,
    model: str,
    api_key: str,
    worker_task_spec: dict[str, Any],
    rework_context: dict[str, Any] | None,
    emit: Callable[[str, str], None],
    execution_id: str | None = None,
    worker_attempt: int = 1,
    dependency_workspace: str | None = None,
    worktree_isolated: bool = False,
) -> dict[str, Any]:
    container_available = _container_available(worker_task_spec)
    emit("coder_agent", "Starting OpenHands coding agent" if container_available else "Starting policy-limited coding worker without Docker/Podman")
    events: list[str] = []

    try:
        from openhands.sdk import Agent, Conversation, LLM, Tool
        from openhands.sdk.context.condenser import LLMSummarizingCondenser
        from openhands.tools.task_tracker import TaskTrackerTool
    except Exception as exc:
        if should_scaffold_todo_fallback(worker_task_spec):
            return _run_deterministic_scaffold_fallback(
                workspace=workspace,
                worker_task_spec=worker_task_spec,
                emit=emit,
                dependency_workspace=dependency_workspace,
                worktree_isolated=worktree_isolated,
                reason=f"OpenHands SDK unavailable; using deterministic local Todo scaffold fallback: {exc}",
            )
        return _worker_result(
            workspace=workspace,
            worker_task_spec=worker_task_spec,
            summary="OpenHands SDK is not available.",
            error=str(exc),
            model=None,
            sandbox_diff=[],
            policy_violations=[],
            applied_changes=[],
            sandboxed=False,
            events=events,
        )

    fallback_result: dict[str, Any] | None = None
    run_result: Any = None

    def on_event(event: Any) -> None:
        stage, detail = _event_summary(event)
        record_checkpoint(
            "openhands_event",
            event.__class__.__name__,
            {
                "stage": stage,
                "detail": detail,
                "toolName": str(getattr(event, "tool_name", "") or ""),
                "event": _safe_model_dump(event),
            },
        )
        with telemetry.start_span(
            "tool.openhands_event",
            {
                "openhands.stage": stage,
                "openhands.detail": detail[:500],
                "agent.role": "coder",
            },
        ):
            pass
        events.append(f"{stage}: {detail}")
        emit(stage, detail)

    openhands_model = model
    if server_url and not model.startswith(("openai/", "azure/", "anthropic/", "gemini/", "ollama/", "openrouter/")):
        openhands_model = f"openai/{model}"
    if server_url and model.startswith("gemini/"):
        openhands_model = f"openai/{model}"

    try:
        llm = LLM(
            model=openhands_model,
            api_key=api_key or os.getenv("LLM_API_KEY", "local-no-key"),
            base_url=server_url,
            stream=False,
            timeout=300,
            native_tool_calling=False,
        )
        plugins = _load_plugin_sources(workspace, emit)
        mcp_config = _load_mcp_config(workspace, emit)
        condenser = LLMSummarizingCondenser(llm=llm)
        emit("openhands_context", "LLM summarizing condenser enabled")
        tools = [
            Tool(
                name=PolicyFileEditorTool.name,
                params={
                    "allowed_files": list(worker_task_spec.get("allowedFiles") or []),
                    "forbidden_paths": list(worker_task_spec.get("forbiddenPaths") or []),
                },
            ),
            Tool(name=TaskTrackerTool.name),
        ]
        if container_available:
            tools.insert(
                0,
                Tool(
                    name=ContainerTerminalTool.name,
                    params={
                        "stack": str(worker_task_spec.get("projectStack") or "generic"),
                        "dependency_workspace": dependency_workspace,
                    },
                ),
            )
        else:
            emit("coder_agent", "Docker/Podman unavailable; shell tool disabled and file edits remain policy-limited")
        agent = Agent(
            llm=llm,
            condenser=condenser,
            mcp_config=mcp_config,
            tool_concurrency_limit=1,
            tools=tools,
        )
        shell_policy = (
            "Use container_terminal for every shell command; it runs with no network, dropped capabilities, "
            "and a read-only container root filesystem. "
            if container_available
            else "Docker/Podman is not available in this environment. Do not run shell commands; no terminal tool is available. "
            "Use policy_file_editor for file changes and rely on the later verifier to run allowlisted commands on an isolated copy. "
        )
        task = (
            "You are the Coder Agent in a multi-agent LangGraph pipeline.\n"
            "Follow this worker task spec exactly. Do not edit files outside allowedFiles. "
            "For project creation, create files beneath targetProjectDir and keep all nested paths there. "
            "The policy_file_editor automatically creates missing parent directories for create operations. "
            "Do not create tests unless the spec explicitly asks for tests. "
            "You receive only an explicit context envelope. Do not assume access to any prior conversation "
            "or agent output that is absent from it. "
            f"{shell_policy}"
            "Use policy_file_editor for files; it enforces allowedFiles and forbiddenPaths on reads/writes.\n\n"
            f"WORKER_TASK_SPEC:\n{worker_task_spec}\n\n"
            f"REWORK_CONTEXT:\n{rework_context or {}}\n"
        )
    except Exception as exc:
        return _worker_result(
            workspace=workspace,
            worker_task_spec=worker_task_spec,
            summary="Coder agent failed during OpenHands setup.",
            error=str(exc),
            model=openhands_model,
            sandbox_diff=[],
            policy_violations=[],
            applied_changes=[],
            sandboxed=False,
            events=events,
        )
    policy: dict[str, Any] = {"sandboxDiff": [], "changedFiles": [], "violations": []}
    applied: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    artifact_root = execution_artifact_dir(execution_id) / f"coder-{max(1, int(worker_attempt))}" if execution_id else None
    persistence_dir = artifact_root / "conversations" if artifact_root else None
    conversation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"hethongagent:{execution_id}:coder:{worker_attempt}") if execution_id else None
    durable_sandbox_path = artifact_root / "sandbox" if artifact_root else None
    if persistence_dir:
        persistence_dir.mkdir(parents=True, exist_ok=True)
    conversation_storage = persistence_dir / conversation_id.hex if persistence_dir and conversation_id else None
    resume_conversation = bool(conversation_storage and conversation_storage.exists() and any(conversation_storage.iterdir()))
    try:
        sandbox = None if worktree_isolated else create_workspace_sandbox(workspace, durable_path=durable_sandbox_path)
        sandbox_context = nullcontext(workspace) if worktree_isolated else sandbox
        with sandbox_context as temp_dir:
            sandbox_workspace = str(Path(temp_dir) if worktree_isolated else Path(temp_dir) / "workspace")
            # For a resumed durable sandbox, compare against the untouched source
            # workspace so edits completed before the interruption are included.
            if worktree_isolated and dependency_workspace:
                before_snapshot = file_snapshots(dependency_workspace)
            else:
                before_snapshot = file_snapshots(workspace) if durable_sandbox_path else file_snapshots(sandbox_workspace)
            emit(
                "coder_agent",
                "Execution worktree mounted for container tools"
                if worktree_isolated
                else ("Durable sandbox workspace ready" if durable_sandbox_path else "Sandbox workspace ready"),
            )
            try:
                conversation = Conversation(
                    agent=agent,
                    workspace=sandbox_workspace,
                    plugins=plugins or None,
                    persistence_dir=persistence_dir,
                    conversation_id=conversation_id,
                    callbacks=[on_event],
                    max_iteration_per_run=80,
                    visualizer=None,
                    delete_on_close=False,
                )
                with telemetry.start_span(
                    "tool.openhands_conversation",
                    {
                        "agent.role": "coder",
                        "llm.model": openhands_model,
                        "workspace.path": sandbox_workspace,
                    },
                ):
                    if resume_conversation:
                        emit("coder_agent", f"Resuming persisted OpenHands conversation {conversation_id}")
                    else:
                        try:
                            conversation.send_message(task)
                        except Exception as mcp_exc:
                            # codebase-memory-mcp binary may fail MCP handshake
                            # (v0.8.1 parse error on initialize). Retry once
                            # without MCP tools so the coder can still work.
                            if (
                                not is_transient_error(mcp_exc)
                                and bool(agent.mcp_config.get("mcpServers"))
                                and (worker_attempt == 1 or not resume_conversation)
                            ):
                                _mcp_server_names = sorted((agent.mcp_config.get("mcpServers") or {}).keys())
                                emit(
                                    "openhands_mcp",
                                    f"MCP init failed; retrying without MCP tools "
                                    f"(servers: {_mcp_server_names}, error: {str(mcp_exc)[:200]})",
                                )
                                agent = Agent(
                                    llm=llm, condenser=condenser, mcp_config={},
                                    tool_concurrency_limit=1, tools=tools,
                                )
                                conversation = Conversation(
                                    agent=agent,
                                    workspace=sandbox_workspace,
                                    plugins=plugins or None,
                                    persistence_dir=persistence_dir,
                                    conversation_id=conversation_id,
                                    callbacks=[on_event],
                                    max_iteration_per_run=80,
                                    visualizer=None,
                                    delete_on_close=False,
                                )
                                conversation.send_message(task)
                            else:
                                raise
                    run_result = conversation.run()
            except Exception as exc:
                if is_transient_error(exc):
                    emit("resume", f"OpenHands transient failure persisted for retry: {exc}")
                    record_checkpoint(
                        "openhands",
                        "transient_failure",
                        {
                            "conversationId": str(conversation_id or ""),
                            "persistenceDir": str(persistence_dir or ""),
                            "sandbox": sandbox_workspace,
                            "error": str(exc),
                        },
                    )
                    raise
                policy = enforce_change_policy(
                    sandbox_workspace,
                    before_snapshot,
                    list(worker_task_spec.get("allowedFiles") or []),
                    list(worker_task_spec.get("forbiddenPaths") or []),
                )
                applied = list(policy["changedFiles"]) if worktree_isolated else apply_sandbox_changes(workspace, sandbox_workspace, policy["changedFiles"])
                if sandbox:
                    sandbox.complete()
                return _worker_result(
                    workspace=workspace,
                    worker_task_spec=worker_task_spec,
                    summary="Coder agent failed inside sandbox.",
                    error=str(exc),
                    model=openhands_model,
                    raw_result=run_result,
                    sandbox_diff=policy.get("sandboxDiff"),
                    policy_violations=policy["violations"],
                    applied_changes=applied,
                    scaffold_fallback=fallback_result,
                    sandboxed=True,
                    events=events,
                )
            finally:
                close = getattr(locals().get("conversation"), "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        emit("coder_agent", f"OpenHands cleanup warning: {exc}")
            policy = enforce_change_policy(
                sandbox_workspace,
                before_snapshot,
                list(worker_task_spec.get("allowedFiles") or []),
                list(worker_task_spec.get("forbiddenPaths") or []),
            )
            files = policy["changedFiles"]
            violations = policy["violations"]
            if not files and not violations and should_scaffold_todo_fallback(worker_task_spec):
                emit("coder_agent", "OpenHands produced no files; using deterministic todo scaffold fallback")
                fallback_result = scaffold_project_fallback(sandbox_workspace, worker_task_spec)
                write_debug_event("coder_agent.scaffold_fallback", fallback_result)
                policy = enforce_change_policy(
                    sandbox_workspace,
                    before_snapshot,
                    list(worker_task_spec.get("allowedFiles") or []),
                    list(worker_task_spec.get("forbiddenPaths") or []),
                )
                files = policy["changedFiles"]
                violations = policy["violations"]
            applied = list(files) if worktree_isolated else apply_sandbox_changes(workspace, sandbox_workspace, files)
            if sandbox:
                sandbox.complete()
    except Exception as exc:
        if is_transient_error(exc):
            raise
        return _worker_result(
            workspace=workspace,
            worker_task_spec=worker_task_spec,
            summary="Coder agent failed while preparing or cleaning sandbox.",
            error=str(exc),
            model=openhands_model,
            raw_result=run_result,
            sandbox_diff=policy.get("sandboxDiff"),
            policy_violations=violations,
            applied_changes=applied,
            scaffold_fallback=fallback_result,
            sandboxed=True,
            events=events,
        )
    summary = "Coder agent completed in sandbox."
    error = None
    if violations:
        summary = "Coder agent changes were filtered by allowedFiles policy."
        error = "Coder attempted to change files outside allowedFiles or inside forbiddenPaths."
    elif fallback_result and fallback_result.get("used"):
        summary = "Coder agent used deterministic todo scaffold fallback after OpenHands produced no file changes."
    elif fallback_result and fallback_result.get("used") and not applied:
        summary = "Coder agent produced no file changes."
        error = "Coder completed without creating project files."
    return _worker_result(
        workspace=workspace,
        worker_task_spec=worker_task_spec,
        summary=summary,
        error=error,
        model=openhands_model,
        raw_result=run_result,
        sandbox_diff=policy.get("sandboxDiff"),
        policy_violations=violations,
        applied_changes=applied,
        scaffold_fallback=fallback_result,
        sandboxed=True,
        events=events,
    )
