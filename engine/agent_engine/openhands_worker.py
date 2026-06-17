from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Callable

from .workspace import changed_files, file_hashes


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


def _load_mcp_config(workspace: str, emit: Callable[[str, str], None]) -> dict[str, Any]:
    root = Path(workspace)
    try:
        config_path, raw = _load_json_config([root / ".openhands" / "mcp.json", root / ".mcp.json"])
    except Exception as exc:
        emit("openhands_mcp", f"Ignored MCP config: {exc}")
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        emit("openhands_mcp", f"Ignored {config_path.name}: expected a JSON object")
        return {}
    emit("openhands_mcp", f"Loaded MCP config from {config_path.relative_to(root)}")
    return raw


def run_openhands_worker(
    *,
    workspace: str,
    server_url: str,
    model: str,
    worker_task_spec: dict[str, Any],
    rework_context: dict[str, Any] | None,
    emit: Callable[[str, str], None],
) -> dict[str, Any]:
    before = file_hashes(workspace)
    emit("openhands_worker", "Starting OpenHands SDK single writer")

    try:
        from openhands.sdk import Agent, Conversation, LLM, Tool
        from openhands.sdk.context.condenser import LLMSummarizingCondenser
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.task_tracker import TaskTrackerTool
        from openhands.tools.terminal import TerminalTool
    except Exception as exc:
        return {
            "summary": "OpenHands SDK is not available.",
            "error": str(exc),
            "changedFiles": [],
            "events": [],
        }

    events: list[str] = []

    def on_event(event: Any) -> None:
        stage, detail = _event_summary(event)
        events.append(f"{stage}: {detail}")
        emit(stage, detail)

    openhands_model = model
    if server_url and not model.startswith(("openai/", "azure/", "anthropic/", "gemini/", "ollama/", "openrouter/")):
        openhands_model = f"openai/{model}"
    if server_url and model.startswith("gemini/"):
        openhands_model = f"openai/{model}"

    llm = LLM(
        model=openhands_model,
        api_key=os.getenv("LLM_API_KEY", "local-no-key"),
        base_url=server_url,
        stream=False,
        timeout=300,
        native_tool_calling=False,
    )
    plugins = _load_plugin_sources(workspace, emit)
    mcp_config = _load_mcp_config(workspace, emit)
    condenser = LLMSummarizingCondenser(llm=llm)
    emit("openhands_context", "LLM summarizing condenser enabled")
    agent = Agent(
        llm=llm,
        condenser=condenser,
        mcp_config=mcp_config,
        tool_concurrency_limit=1,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )
    task = (
        "You are the single coding writer in a LangGraph orchestrated pipeline.\n"
        "Follow this worker task spec exactly. Do not edit files outside allowedFiles. "
        "Do not create tests unless the spec explicitly asks for tests. "
        "If codegraphContext is enabled, use it as code data for orientation and impact analysis; "
        "prefer it over broad grep/read discovery, but read live files directly before editing them.\n\n"
        f"WORKER_TASK_SPEC:\n{worker_task_spec}\n\n"
        f"REWORK_CONTEXT:\n{rework_context or {}}\n"
    )
    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        plugins=plugins or None,
        callbacks=[on_event],
        max_iteration_per_run=80,
        visualizer=None,
        delete_on_close=False,
    )
    try:
        conversation.send_message(task)
        run_result = conversation.run()
    except Exception as exc:
        after = file_hashes(workspace)
        return {
            "summary": "OpenHands worker failed.",
            "error": str(exc),
            "model": openhands_model,
            "changedFiles": changed_files(before, after),
            "events": events[-120:],
        }
    after = file_hashes(workspace)
    files = changed_files(before, after)
    return {
        "summary": "OpenHands worker completed.",
        "model": openhands_model,
        "rawResult": str(run_result)[:4000],
        "changedFiles": files,
        "events": events[-120:],
    }
