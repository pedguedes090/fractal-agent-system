"""Adapter that drives `claude-agent-sdk` for Project Doctor patches.

The Agent SDK runs its own tool loop — Read, Edit, Bash — so we don't ask
for a fenced code block back; we ask the agent to edit the file in-place.
Every assistant text token is forwarded to `emit("doctor.patch.chunk", …)`
so the UI renders the LLM typing live, exactly as in a chat client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

EmitFn = Callable[[str, str], None]


def _has_sdk() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:
        return False
    return True


class ClaudeAgentSDKProvider:
    """Thin wrapper that exposes the surface the patcher expects.

    The patcher only needs one operation: "rewrite this file to fix this
    issue, stream tokens as you go." The Agent SDK does the file I/O for
    us (via its Read/Edit tools), so the provider returns the streamed
    response text after the loop finishes.
    """

    def __init__(self, *, cwd: Path, model: str | None = None, api_key: str | None = None) -> None:
        if not _has_sdk():
            raise RuntimeError("claude-agent-sdk is not installed in this venv")
        self.cwd = cwd
        self.model = model or "claude-opus-4-8"
        self.api_key = api_key or ""

    def edit_file_with_stream(
        self,
        *,
        rel_path: str,
        instructions: str,
        emit: EmitFn,
    ) -> tuple[bool, int]:
        """Ask the agent to edit `rel_path` per `instructions`.

        Returns (success, streamed_chunk_count). The agent writes the file
        directly via its Edit tool; we don't apply patches ourselves.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            query,
        )

        chunks = 0

        async def _run() -> tuple[bool, int]:
            nonlocal chunks
            options = ClaudeAgentOptions(
                cwd=str(self.cwd),
                # Full tool set — repo owner runs the doctor against their own
                # project, so the agent gets Bash too (run lint, pip install,
                # restart dev server, etc.). No confirmation gate.
                allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
                model=self.model,
                permission_mode="acceptEdits",
            )
            if self.api_key:
                # claude-agent-sdk reads ANTHROPIC_API_KEY from env; setting
                # via env keeps the call site explicit while letting the SDK
                # honor whatever auth resolution it normally does.
                import os
                os.environ.setdefault("ANTHROPIC_API_KEY", self.api_key)

            prompt = (
                f"Fix the following file: {rel_path}\n\n"
                f"Problem details:\n{instructions}\n\n"
                "Edit the file in place using the Edit tool. "
                "When done, write a one-line summary of what you changed."
            )

            ok = False
            try:
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text = getattr(block, "text", "") or ""
                                if text:
                                    chunks += 1
                                    emit("doctor.patch.chunk", text)
                            elif isinstance(block, ToolUseBlock):
                                name = getattr(block, "name", "?")
                                emit("doctor.patch.tool", f"using {name}")
                    elif isinstance(message, ResultMessage):
                        sub = getattr(message, "subtype", "") or ""
                        is_error = bool(getattr(message, "is_error", False))
                        ok = not is_error and sub != "error"
                        emit("doctor.patch.result", f"{sub or 'done'} (error={is_error})")
            except Exception as exc:
                emit("doctor.patch.error", f"Agent SDK error: {exc}")
                return False, chunks
            return ok, chunks

        try:
            return asyncio.run(_run())
        except RuntimeError as exc:
            # If we're already inside a running loop (e.g. async server),
            # fall back to a fresh loop in a dedicated thread.
            if "asyncio.run() cannot be called" not in str(exc):
                raise
            import threading
            result: dict[str, Any] = {"ok": False, "chunks": 0}

            def _worker() -> None:
                loop = asyncio.new_event_loop()
                try:
                    ok, n = loop.run_until_complete(_run())
                    result["ok"], result["chunks"] = ok, n
                finally:
                    loop.close()

            t = threading.Thread(target=_worker, name="agent-sdk-patch", daemon=True)
            t.start()
            t.join()
            return bool(result["ok"]), int(result["chunks"])


def maybe_build_provider(
    *, cwd: Path, model: str | None, api_key: str | None
) -> ClaudeAgentSDKProvider | None:
    """Build the provider if the SDK is available; otherwise return None.

    Callers fall back to the lower-level anthropic provider (or skip the
    LLM patch path) when this returns None.
    """
    if not _has_sdk():
        return None
    try:
        return ClaudeAgentSDKProvider(cwd=cwd, model=model, api_key=api_key)
    except Exception:
        return None
