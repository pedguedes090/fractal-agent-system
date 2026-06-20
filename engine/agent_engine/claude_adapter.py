"""Claude Code adapter layer wrapping Anthropic SDK.

This module provides a clean interface for the agent to call Claude models,
with tool use, streaming, usage tracking, timeout, cancellation, and retry.

Architecture:
  - ModelProvider: abstract interface for any LLM provider
  - ClaudeProvider: Anthropic SDK implementation
  - ClaudeToolSpec: canonical tool definition format
  - format_claude_tools(): convert internal tool specs to Anthropic format
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import anthropic
from anthropic.types import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    Message,
    MessageDeltaEvent,
    MessageStartEvent,
    MessageStopEvent,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)

from . import telemetry
from .debug_log import write_debug_event

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass
class ModelUsage:
    """Token usage record for one model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model: str = ""
    server_tool_use: dict[str, Any] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_anthropic(cls, usage: Usage, model: str = "") -> "ModelUsage":
        return cls(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            model=model,
            server_tool_use=(
                usage.server_tool_use.model_dump(mode="json", exclude_none=True)
                if hasattr(usage.server_tool_use, "model_dump")
                else None
            ),
        )


@dataclass
class ClaudeMessage:
    """A single message in the conversation with Claude."""

    role: str  # "user" | "assistant"
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None


@dataclass
class ClaudeToolCall:
    """A tool call received from Claude."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ClaudeToolResult:
    """Result of executing a tool call."""

    tool_call_id: str
    name: str
    output: str
    is_error: bool = False


@dataclass
class ClaudeResponse:
    """Complete response from a Claude API call."""

    id: str
    model: str
    content: list[dict[str, Any]]
    text: str
    tool_calls: list[ClaudeToolCall]
    stop_reason: str
    usage: ModelUsage

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def has_text(self) -> bool:
        return bool(self.text)


@dataclass
class ClaudeStreamEvent:
    """A single event from a streaming Claude response."""

    type: str  # "text_delta" | "tool_use_start" | "tool_use_delta" | "message_stop"
    text: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    tool_input_delta: str = ""
    usage: ModelUsage | None = None
    stop_reason: str = ""


@dataclass
class ClaudeToolSpec:
    """Canonical tool definition for Claude."""

    name: str
    description: str
    input_schema: dict[str, Any]
    timeout_seconds: float = 300.0
    require_approval: bool = False
    allowed_paths: list[str] | None = None

    def to_anthropic(self) -> dict[str, Any]:
        """Convert to Anthropic SDK tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ClaudeConfig:
    """Configuration for Claude provider."""

    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8192
    temperature: float = 0.2
    timeout_seconds: float = 300.0
    max_retries: int = 2
    backoff_seconds: float = 0.5
    workspace_dir: str | None = None

    @classmethod
    def from_env(cls) -> "ClaudeConfig":
        """Create config from environment variables."""
        return cls(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS", "8192")),
            temperature=float(os.getenv("CLAUDE_TEMPERATURE", "0.2")),
            timeout_seconds=float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "300")),
            max_retries=max(0, int(os.getenv("CLAUDE_MAX_RETRIES", "2"))),
            backoff_seconds=float(os.getenv("CLAUDE_BACKOFF_SECONDS", "0.5")),
            workspace_dir=os.getenv("CLAUDE_WORKSPACE_DIR"),
        )


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class ModelProvider(Protocol):
    """Interface for any LLM provider."""

    def generate(
        self,
        messages: list[ClaudeMessage],
        tools: list[ClaudeToolSpec] | None = None,
        system: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ClaudeResponse: ...

    def stream(
        self,
        messages: list[ClaudeMessage],
        tools: list[ClaudeToolSpec] | None = None,
        system: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[list[ClaudeStreamEvent], ClaudeResponse]: ...

    def cancel(self) -> None: ...


# ---------------------------------------------------------------------------
# Claude Provider (Anthropic SDK)
# ---------------------------------------------------------------------------


def _build_anthropic_messages(
    msgs: list[ClaudeMessage],
) -> tuple[list[dict[str, Any]], str]:
    """Convert ClaudeMessage list to Anthropic API format."""
    api_messages: list[dict[str, Any]] = []
    for msg in msgs:
        content = msg.content
        if isinstance(content, list):
            api_content = content
        elif isinstance(content, str):
            api_content = [{"type": "text", "text": content}]
        else:
            api_content = [{"type": "text", "text": str(content)}]

        entry: dict[str, Any] = {"role": msg.role, "content": api_content}
        if msg.name:
            entry["name"] = msg.name
        api_messages.append(entry)

    # Extract system message (last one wins in our convention)
    system_content = ""
    user_assistant = []
    for msg in api_messages:
        if msg["role"] == "system":
            system_content = "\n".join(
                block.get("text", "") for block in msg["content"] if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            user_assistant.append(msg)

    return user_assistant, system_content


class ClaudeProvider:
    """Anthropic SDK implementation of ModelProvider."""

    def __init__(self, config: ClaudeConfig) -> None:
        self.config = config
        self._client: anthropic.Anthropic | None = None
        self._cancelled = False
        self._abort_callbacks: list[Callable[[], None]] = []
        self._total_usage = ModelUsage()

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self.config.api_key,
                max_retries=self.config.max_retries,
                timeout=self.config.timeout_seconds,
            )
        return self._client

    @property
    def total_usage(self) -> ModelUsage:
        return self._total_usage

    def cancel(self) -> None:
        """Cancel any in-flight request."""
        self._cancelled = True
        for cb in self._abort_callbacks:
            try:
                cb()
            except Exception:
                pass

    def _check_cancelled(self) -> None:
        if self._cancelled:
            self._cancelled = False
            raise RuntimeError("Claude request was cancelled")

    def _build_tools(self, tools: list[ClaudeToolSpec] | None) -> list[dict[str, Any]]:
        """Convert ClaudeToolSpec list to Anthropic API tool format."""
        if not tools:
            return []
        return [tool.to_anthropic() for tool in tools]

    def generate(
        self,
        messages: list[ClaudeMessage],
        tools: list[ClaudeToolSpec] | None = None,
        system: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ClaudeResponse:
        """Non-streaming call to Claude."""
        self._cancelled = False

        api_messages, system_merged = _build_anthropic_messages(messages)
        if system and not system_merged:
            system_merged = system
        elif system:
            system_merged = f"{system}\n\n{system_merged}"

        tool_defs = self._build_tools(tools)

        with telemetry.start_span("claude.api_call", {
            "model": self.config.model,
            "message_count": len(api_messages),
            "tool_count": len(tool_defs),
        }) as span:

            last_error: Exception | None = None
            for attempt in range(self.config.max_retries + 1):
                self._check_cancelled()
                try:
                    kwargs: dict[str, Any] = {
                        "model": self.config.model,
                        "max_tokens": max_tokens or self.config.max_tokens,
                        "messages": api_messages,
                        "temperature": temperature if temperature is not None else self.config.temperature,
                    }
                    if system_merged:
                        kwargs["system"] = system_merged
                    if tool_defs:
                        kwargs["tools"] = tool_defs

                    response: Message = self.client.messages.create(**kwargs)  # type: ignore[arg-type]

                    # Parse response
                    text_parts: list[str] = []
                    tool_calls: list[ClaudeToolCall] = []
                    content_blocks: list[dict[str, Any]] = []

                    for block in response.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                            content_blocks.append({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            tc = ClaudeToolCall(
                                id=block.id,
                                name=block.name,
                                input=dict(block.input) if isinstance(block.input, dict) else {"raw": str(block.input)},
                            )
                            tool_calls.append(tc)
                            content_blocks.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": tc.input,
                            })
                        elif hasattr(block, "type"):
                            content_blocks.append({"type": str(block.type), "data": str(block)})

                    usage = ModelUsage.from_anthropic(response.usage, self.config.model) if response.usage else ModelUsage()
                    self._total_usage = ModelUsage(
                        input_tokens=self._total_usage.input_tokens + usage.input_tokens,
                        output_tokens=self._total_usage.output_tokens + usage.output_tokens,
                        model=self.config.model,
                    )

                    write_debug_event("claude.generate", {
                        "model": self.config.model,
                        "attempt": attempt,
                        "stop_reason": response.stop_reason,
                        "text_length": len("".join(text_parts)),
                        "tool_calls": len(tool_calls),
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    })

                    return ClaudeResponse(
                        id=response.id or str(uuid.uuid4()),
                        model=response.model,
                        content=content_blocks,
                        text="".join(text_parts),
                        tool_calls=tool_calls,
                        stop_reason=str(response.stop_reason or "end_turn"),
                        usage=usage,
                    )

                except anthropic.APIStatusError as exc:
                    last_error = exc
                    if attempt >= self.config.max_retries:
                        raise
                    if exc.status_code == 429 or exc.status_code >= 500:
                        delay = self.config.backoff_seconds * (2**attempt)
                        time.sleep(delay)
                        continue
                    raise
                except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                    last_error = exc
                    if attempt >= self.config.max_retries:
                        raise
                    delay = self.config.backoff_seconds * (2**attempt)
                    time.sleep(delay)
                    continue

            raise last_error or RuntimeError("Claude generate failed unexpectedly")

    def stream(
        self,
        messages: list[ClaudeMessage],
        tools: list[ClaudeToolSpec] | None = None,
        system: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[list[ClaudeStreamEvent], ClaudeResponse]:
        """Streaming call to Claude. Returns events + final assembled response."""
        self._cancelled = False

        api_messages, system_merged = _build_anthropic_messages(messages)
        if system and not system_merged:
            system_merged = system
        elif system:
            system_merged = f"{system}\n\n{system_merged}"

        tool_defs = self._build_tools(tools)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "messages": api_messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "stream": True,
        }
        if system_merged:
            kwargs["system"] = system_merged
        if tool_defs:
            kwargs["tools"] = tool_defs

        events: list[ClaudeStreamEvent] = []
        text_blocks: dict[int, str] = {}
        tool_use_blocks: dict[int, dict[str, Any]] = {}
        final_usage = ModelUsage()
        final_stop_reason = "end_turn"
        message_id = ""

        with self.client.messages.stream(**kwargs) as stream:  # type: ignore[arg-type]
            for event in stream:
                self._check_cancelled()

                if isinstance(event, MessageStartEvent):
                    message_id = event.message.id
                    if event.message.usage:
                        final_usage = ModelUsage.from_anthropic(event.message.usage, self.config.model)

                elif isinstance(event, MessageDeltaEvent):
                    if event.usage:
                        final_usage = ModelUsage.from_anthropic(event.usage, self.config.model)
                    if event.delta and hasattr(event.delta, "stop_reason"):
                        final_stop_reason = str(event.delta.stop_reason or "end_turn")

                elif isinstance(event, ContentBlockStartEvent):
                    block = event.content_block
                    if isinstance(block, TextBlock):
                        events.append(ClaudeStreamEvent(type="text_delta", text=block.text))
                        text_blocks[event.index] = block.text
                    elif isinstance(block, ToolUseBlock):
                        tool_data = {
                            "id": block.id,
                            "name": block.name,
                            "input_json": "",
                        }
                        tool_use_blocks[event.index] = tool_data
                        events.append(ClaudeStreamEvent(
                            type="tool_use_start",
                            tool_use_id=block.id,
                            tool_name=block.name,
                            tool_input_delta="",
                        ))

                elif isinstance(event, ContentBlockDeltaEvent):
                    delta = event.delta
                    if isinstance(delta, TextDelta):
                        text_blocks.setdefault(event.index, "")
                        text_blocks[event.index] += delta.text
                        events.append(ClaudeStreamEvent(type="text_delta", text=delta.text))
                    elif hasattr(delta, "type") and delta.type == "input_json_delta":
                        blob = tool_use_blocks.setdefault(event.index, {"id": "", "name": "", "input_json": ""})
                        blob["input_json"] += getattr(delta, "partial_json", "")
                        events.append(ClaudeStreamEvent(
                            type="tool_use_delta",
                            tool_use_id=blob.get("id", ""),
                            tool_name=blob.get("name", ""),
                            tool_input_delta=getattr(delta, "partial_json", ""),
                        ))

                elif isinstance(event, ContentBlockStopEvent):
                    events.append(ClaudeStreamEvent(type="tool_use_delta"))

                elif isinstance(event, MessageStopEvent):
                    events.append(ClaudeStreamEvent(type="message_stop", usage=final_usage, stop_reason=final_stop_reason))

        # Build final ClaudeResponse from streamed data
        content_blocks: list[dict[str, Any]] = []
        tool_calls: list[ClaudeToolCall] = []
        full_text = ""

        for idx in sorted(text_blocks):
            text = text_blocks[idx]
            full_text += text
            content_blocks.append({"type": "text", "text": text})

        for idx in sorted(tool_use_blocks):
            blob = tool_use_blocks[idx]
            import json as _json
            try:
                parsed_input = _json.loads(blob.get("input_json", "{}"))
            except _json.JSONDecodeError:
                parsed_input = {"raw": blob.get("input_json", "")}
            tc = ClaudeToolCall(
                id=blob.get("id", ""),
                name=blob.get("name", ""),
                input=parsed_input,
            )
            tool_calls.append(tc)
            content_blocks.append({
                "type": "tool_use",
                "id": blob.get("id", ""),
                "name": blob.get("name", ""),
                "input": parsed_input,
            })

        self._total_usage = ModelUsage(
            input_tokens=self._total_usage.input_tokens + final_usage.input_tokens,
            output_tokens=self._total_usage.output_tokens + final_usage.output_tokens,
            model=self.config.model,
        )

        response = ClaudeResponse(
            id=message_id or str(uuid.uuid4()),
            model=self.config.model,
            content=content_blocks,
            text=full_text,
            tool_calls=tool_calls,
            stop_reason=final_stop_reason,
            usage=final_usage,
        )

        write_debug_event("claude.stream", {
            "model": self.config.model,
            "stop_reason": final_stop_reason,
            "text_length": len(full_text),
            "tool_calls": len(tool_calls),
            "event_count": len(events),
            "input_tokens": final_usage.input_tokens,
            "output_tokens": final_usage.output_tokens,
        })

        return events, response

    def count_tokens(self, text: str) -> int:
        """Count tokens in text using Claude's tokenizer."""
        try:
            result = self.client.messages.count_tokens(
                model=self.config.model,
                messages=[{"role": "user", "content": text}],
            )
            return result.input_tokens
        except Exception:
            # Rough estimate: ~4 chars per token
            return len(text) // 4

    def generate_with_tool_loop(
        self,
        messages: list[ClaudeMessage],
        tools: list[ClaudeToolSpec],
        tool_executor: Callable[[ClaudeToolCall], str],
        system: str = "",
        *,
        max_tool_rounds: int = 10,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ClaudeResponse:
        """Generate with a tool-calling loop: call Claude, execute tools, repeat until done."""
        conv = list(messages)
        self._cancelled = False

        for round_idx in range(max_tool_rounds):
            self._check_cancelled()

            response = self.generate(
                messages=conv,
                tools=tools,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            if not response.has_tool_calls:
                return response

            # Build assistant message with tool uses + results
            assistant_content = response.content[:]

            # Execute tools and build result blocks
            for tc in response.tool_calls:
                try:
                    result_text = tool_executor(tc)
                    assistant_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result_text,
                    })
                except Exception as exc:
                    assistant_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": f"Error executing tool {tc.name}: {exc}",
                        "is_error": True,
                    })

            conv.append(ClaudeMessage(role="assistant", content=assistant_content))
            write_debug_event("claude.tool_round", {
                "round": round_idx + 1,
                "tool_calls": len(response.tool_calls),
            })

        # Max rounds reached — return last response
        return response


def create_claude_provider(
    *,
    api_key: str = "",
    model: str = "",
    server_url: str = "",
    **kwargs: Any,
) -> ClaudeProvider:
    """Factory function to create ClaudeProvider from common settings.

    If server_url is provided (OpenAI-compatible endpoint), we still create
    a ClaudeProvider for use with Anthropic-compatible routing. For pure
    OpenAI endpoints, use the existing ChatClient (llm_client.py) instead.
    """
    config = ClaudeConfig.from_env()

    if api_key:
        config.api_key = api_key
    if model:
        config.model = model

    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)

    return ClaudeProvider(config)


# ---------------------------------------------------------------------------
# Standard error types
# ---------------------------------------------------------------------------


class ClaudeAdapterError(Exception):
    """Base error for Claude adapter."""


class ClaudeTimeoutError(ClaudeAdapterError):
    """Request timed out."""


class ClaudeCancelledError(ClaudeAdapterError):
    """Request was cancelled."""


class ClaudeAuthError(ClaudeAdapterError):
    """Authentication failed."""


class ClaudeRateLimitError(ClaudeAdapterError):
    """Rate limited."""


def map_anthropic_error(exc: Exception) -> ClaudeAdapterError:
    """Map Anthropic SDK errors to internal error types."""
    if isinstance(exc, ClaudeAdapterError):
        return exc

    msg = str(exc)

    if isinstance(exc, anthropic.AuthenticationError):
        return ClaudeAuthError(msg)
    if isinstance(exc, anthropic.RateLimitError):
        return ClaudeRateLimitError(msg)
    if isinstance(exc, (anthropic.APITimeoutError, TimeoutError)):
        return ClaudeTimeoutError(msg)
    if isinstance(exc, RuntimeError) and "cancelled" in msg.lower():
        return ClaudeCancelledError(msg)

    return ClaudeAdapterError(msg)
