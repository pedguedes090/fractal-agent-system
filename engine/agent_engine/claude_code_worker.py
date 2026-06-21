"""Coding worker driven by claude-agent-sdk (replaces OpenHands SDK path).

The agent runs Read/Edit/Write/Glob/Grep/Bash tools directly in the workspace
and streams every assistant TextBlock as an `llm_chunk` event so the FlowView
Stream subtab renders typing live. ToolUse blocks emit a `tool_call` event so
the Tools tab shows live file edits / Bash invocations.

This module mirrors the return contract of `run_openhands_worker` so the
graph node can swap providers transparently.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .debug_log import write_debug_event
from .workspace import file_snapshots
from . import codebase_memory as _cbm


def _has_sdk() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:
        return False
    return True


def _compact(value: Any, limit: int = 400) -> str:
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
    return text[: limit - 1] + "…" if len(text) > limit else text


def _build_codebase_context(workspace: str, goal: str) -> str:
    """Pre-fetch top hits from codebase-memory-mcp and architecture overview."""
    if not _cbm.is_available():
        return ""
    parts: list[str] = []
    try:
        hits = _cbm.search_graph(workspace, goal, limit=8) or []
        if hits:
            parts.append("# Relevant code symbols (from knowledge graph)")
            for h in hits[:8]:
                name = h.get("qualified_name") or h.get("name") or "?"
                label = h.get("label") or h.get("kind") or ""
                file_ = h.get("file") or ""
                parts.append(f"- [{label}] {name}  ({file_})")
            parts.append("")
    except Exception:
        pass
    try:
        arch = _cbm.get_architecture(workspace) or {}
        clusters = arch.get("clusters") or []
        if clusters:
            parts.append("# Architecture clusters (Leiden over call/import graph)")
            for c in clusters[:5]:
                label = c.get("label") or "?"
                members = c.get("members") or c.get("member_count") or "?"
                tops = ", ".join((c.get("top_nodes") or [])[:3])
                parts.append(f"- {label}  ({members} members)  → {tops}")
            parts.append("")
    except Exception:
        pass
    return "\n".join(parts)


def _build_prompt(worker_task_spec: dict[str, Any], rework_context: dict[str, Any] | None, codebase_context: str = "") -> str:
    parts: list[str] = []
    intent = str(worker_task_spec.get("intent") or worker_task_spec.get("goal") or "").strip()
    if intent:
        parts.append(f"# Goal\n{intent}\n")
    plan = worker_task_spec.get("plan") or worker_task_spec.get("steps") or []
    if isinstance(plan, list) and plan:
        parts.append("# Plan steps")
        for i, step in enumerate(plan, 1):
            parts.append(f"{i}. {_compact(step, 300)}")
        parts.append("")
    files = worker_task_spec.get("targetFiles") or worker_task_spec.get("filesToEdit") or []
    if isinstance(files, list) and files:
        parts.append("# Target files (priority)")
        for f in files[:20]:
            parts.append(f"- {f}")
        parts.append("")
    commands = worker_task_spec.get("commandsToRun") or []
    if isinstance(commands, list) and commands:
        parts.append("# Suggested verification commands (run via Bash tool)")
        for c in commands[:10]:
            parts.append(f"$ {c}")
        parts.append("")
    if rework_context:
        parts.append("# Rework feedback from previous iteration")
        parts.append("Fix the following issues. Do NOT touch unrelated code.")
        for b in (rework_context.get("blockers") or [])[:8]:
            parts.append(f"- BLOCKER: {_compact(str(b), 800)}")
        for w in (rework_context.get("warnings") or [])[:4]:
            parts.append(f"- WARNING: {_compact(str(w), 400)}")
        for cr in (rework_context.get("commandResults") or [])[:6]:
            if cr.get("code") not in (0, None) and not cr.get("skipped"):
                parts.append(f"- FAILED COMMAND: `{cr.get('command','?')}` (exit {cr.get('code','?')})")
                parts.append(f"  STDOUT: {_compact(cr.get('stdout',''), 600)}")
                parts.append(f"  STDERR: {_compact(cr.get('stderr',''), 600)}")
        notes = rework_context.get("notes") or rework_context.get("feedback") or ""
        if notes:
            parts.append(f"- NOTES: {_compact(notes, 600)}")
        parts.append("")
    if codebase_context:
        parts.append(codebase_context)
    parts.append(
        "# MUTATION_REQUIRED: This is a write task. You have FULL WRITE permission.\n"
        "# You MUST produce file changes, not a report. You MUST run commands. You MUST verify with browser.\n\n"
        "# AUTONOMOUS NO-QUESTION PRODUCT BUILD MODE\n"
        "You are an autonomous coding agent operating in a VM/container with full write access.\n"
        "You MUST NOT ask the user any question. You MUST NOT halt, clarify, confirm, or wait.\n"
        "For every decision (stack, theme, architecture, scope), pick the DEFAULT and proceed.\n"
        "Record your choices in assumptions, not as questions.\n\n"
        "DEFAULT DECISION POLICY (apply when unspecified):\n"
        "- Stack: Vite + React + TypeScript + CSS variables/modules.\n"
        "- Design: premium minimal dark, single accent color, no neon/gradient abuse.\n"
        "- Responsive: mobile (390px) / tablet (768px) / desktop (1440px).\n"
        "- Music: HTMLAudioElement, local-first demo catalog, data adapter for later API swap.\n"
        "- Chat: localStorage history + sample bot reply.\n"
        "- Blog: markdown rendering.\n"
        "- Shop: product grid + cart.\n"
        "- Dashboard: 3-4 charts with mock data.\n"
        "- Backend: frontend-first + localStorage. Only add server when capability truly needs it.\n"
        "- Port conflict: auto-switch to another port.\n"
        "- Dependency failure: swap to alternative or implement minimal version.\n"
        "- Unrelated workspace: create new project in `music-app/` or appropriate dir.\n"
        "Do NOT integrate into an unrelated repo. Do NOT edit agent platform files\n"
        "(FlowView, bottleneck exporter, agent_engine, autonomy, classifier, claude_adapter).\n\n"
        "ANTI-HALT: these outputs are FORBIDDEN:\n"
        "- 'Cần làm rõ...' / 'Halt' / 'chờ câu trả lời' / 'Xác nhận trước'\n"
        "- Any list of numbered choices for the user\n"
        "- 'Workspace không liên quan' (just create your own project)\n"
        "- 'Before I proceed' / 'Do you want me to' / 'Which approach'\n"
        "- Any output ending in '?' directed at a missing user\n"
        "If you catch yourself about to ask, STOP, pick default #1, record the assumption, continue.\n\n"
        "Follow workerTaskSpec.prioritizedTasks[] order if present.\n"
        "Your final output must be a product change visible in the browser — not a report, plan, or TODO.\n"
        "When done, write a one-paragraph summary of what you built and why."
    )
    return "\n".join(parts)


def run_claude_code_worker(
    *,
    workspace: str,
    model: str,
    api_key: str,
    worker_task_spec: dict[str, Any],
    rework_context: dict[str, Any] | None,
    emit: Callable[..., None],
    execution_id: str | None = None,
    worker_attempt: int = 1,
) -> dict[str, Any]:
    """Run the coding task via claude-agent-sdk and return an OpenHands-shaped result."""

    if not _has_sdk():
        return {
            "summary": "claude-agent-sdk is not installed",
            "error": "missing_sdk",
            "model": model,
            "sandboxDiff": [],
            "policyViolations": [],
            "appliedChanges": [],
            "scaffoldFallback": None,
            "sandboxed": False,
            "events": ["claude_code_worker: SDK missing"],
        }

    if api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)

    goal_text = str(worker_task_spec.get("intent") or worker_task_spec.get("goal") or "")
    try:
        _cbm.ensure_indexed(workspace)
    except Exception:
        pass
    codebase_ctx = _build_codebase_context(workspace, goal_text)
    prompt = _build_prompt(worker_task_spec, rework_context, codebase_context=codebase_ctx)
    emit(
        "openhands_worker",
        "claude-agent-sdk worker starting" + (" (with codebase-memory MCP)" if _cbm.is_available() else ""),
        node="openhands_worker", agent_role="coder", status="running",
    )

    pre_snap = file_snapshots(workspace)
    events: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    ok = False
    chunks = 0
    err: str | None = None

    async def _run() -> None:
        nonlocal ok, chunks, err
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            query,
        )
        try:
            from claude_agent_sdk import StreamEvent  # type: ignore
        except Exception:
            StreamEvent = ()  # sentinel: isinstance(x, ()) is always False
        mcp_servers: dict[str, Any] = {}
        mcp_cfg = _cbm.McpServerConfig.detect()
        if mcp_cfg:
            mcp_servers["codebase-memory"] = mcp_cfg.as_mcp_server_entry()
        allowed = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
        if mcp_servers:
            for t in ("search_graph", "trace_path", "get_architecture", "get_code_snippet", "search_code", "index_status"):
                allowed.append(f"mcp__codebase-memory__{t}")
        # Cap agent turns so it cannot loop forever (was the 1h hang root cause).
        _max_turns = int(os.environ.get("AGENT_CODER_MAX_TURNS", "40"))
        opts_kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "allowed_tools": allowed,
            "model": model or "claude-opus-4-8",
            "permission_mode": "acceptEdits",
            "include_partial_messages": True,
            "max_turns": _max_turns,
        }
        if mcp_servers:
            opts_kwargs["mcp_servers"] = mcp_servers
        try:
            options = ClaudeAgentOptions(**opts_kwargs)
        except TypeError:
            # Retry without options the installed SDK doesn't accept.
            opts_kwargs.pop("include_partial_messages", None)
            try:
                options = ClaudeAgentOptions(**opts_kwargs)
            except TypeError:
                opts_kwargs.pop("mcp_servers", None)
                options = ClaudeAgentOptions(**opts_kwargs)
        try:
            saw_partial = False
            async for message in query(prompt=prompt, options=options):
                # ── Token-by-token deltas (when include_partial_messages works) ──
                if StreamEvent and isinstance(message, StreamEvent):
                    raw = getattr(message, "event", None) or getattr(message, "raw", None) or {}
                    delta = (raw.get("delta") or {}) if isinstance(raw, dict) else {}
                    piece = ""
                    if isinstance(delta, dict) and delta.get("type") in ("text_delta", None):
                        piece = delta.get("text") or ""
                    if piece:
                        saw_partial = True
                        chunks += 1
                        try:
                            emit(
                                "openhands_worker",
                                piece[:200],
                                node="openhands_worker",
                                event_type="llm_chunk",
                                tool="llm",
                                status="running",
                                chunk_text=piece,
                            )
                        except Exception:
                            pass
                    continue
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text = getattr(block, "text", "") or ""
                            # When partial deltas already streamed this turn's
                            # text token-by-token, skip the full-block re-emit to
                            # avoid duplicating it in the Stream subtab.
                            if text and not saw_partial:
                                chunks += 1
                                try:
                                    emit(
                                        "openhands_worker",
                                        text[:200],
                                        node="openhands_worker",
                                        event_type="llm_chunk",
                                        tool="llm",
                                        status="running",
                                        chunk_text=text,
                                    )
                                except Exception:
                                    pass
                        elif isinstance(block, ToolUseBlock):
                            name = getattr(block, "name", "?")
                            tinput = getattr(block, "input", {}) or {}
                            tool_calls.append({"name": name, "input": tinput})
                            try:
                                emit(
                                    "openhands_worker",
                                    f"tool: {name}",
                                    node="openhands_worker",
                                    event_type="tool_call",
                                    tool=str(name),
                                    status="running",
                                    tool_input=tinput,
                                )
                            except Exception:
                                pass
                            events.append(f"tool_use: {name}")
                elif isinstance(message, ResultMessage):
                    sub = getattr(message, "subtype", "") or ""
                    is_error = bool(getattr(message, "is_error", False))
                    ok = not is_error and sub != "error"
                    events.append(f"result: {sub or 'done'} error={is_error}")
        except Exception as exc:
            err = str(exc)
            write_debug_event("claude_code_worker.error", {"error": err})

    try:
        try:
            asyncio.run(_run())
        except RuntimeError as exc:
            if "asyncio.run() cannot be called" not in str(exc):
                raise
            holder: dict[str, Any] = {}

            def _worker_thread() -> None:
                loop = asyncio.new_event_loop()
                try:
                    holder["res"] = loop.run_until_complete(_run())
                except Exception as e2:
                    holder["err"] = str(e2)
                finally:
                    loop.close()

            t = threading.Thread(target=_worker_thread, name="claude-code-worker", daemon=True)
            t.start()
            t.join()
            if "err" in holder and not err:
                err = holder["err"]
    except Exception as exc:
        err = str(exc)

    post_snap = file_snapshots(workspace)
    changed: list[dict[str, str]] = []
    for path, content in post_snap.items():
        if pre_snap.get(path) != content:
            changed.append({"path": path})

    summary = (
        f"Agent SDK đã chạy {chunks} chunk · {len(tool_calls)} tool call · {len(changed)} file thay đổi."
        if ok
        else f"Agent SDK lỗi: {err or 'unknown'}"
    )

    return {
        "summary": summary,
        "error": err,
        "model": model,
        "sandboxDiff": [],
        "policyViolations": [],
        "appliedChanges": changed,
        "changedFiles": changed,  # consumer contract (graph reads this key)
        "scaffoldFallback": None,
        "sandboxed": False,
        "events": events,
        "toolCalls": tool_calls,
        "streamedChunks": chunks,
        "providerKind": "claude-agent-sdk",
        "ok": ok,
    }
