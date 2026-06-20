from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from anthropic.types import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    InputJSONDelta,
    Message,
    MessageDeltaEvent,
    MessageStartEvent,
    MessageStopEvent,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.message_delta_usage import MessageDeltaUsage
from anthropic.types.raw_message_delta_event import Delta

from agent_engine.claude_adapter import (
    ClaudeAdapterError,
    ClaudeAuthError,
    ClaudeCancelledError,
    ClaudeConfig,
    ClaudeMessage,
    ClaudeProvider,
    ClaudeRateLimitError,
    ClaudeResponse,
    ClaudeStreamEvent,
    ClaudeTimeoutError,
    ClaudeToolCall,
    ClaudeToolSpec,
    ModelUsage,
    _build_anthropic_messages,
    create_claude_provider,
    map_anthropic_error,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_usage(input_tokens=100, output_tokens=50):
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def _mock_text_block(text):
    return TextBlock(text=text, type="text")


def _mock_tool_use_block(id_, name, input_):
    return ToolUseBlock(id=id_, name=name, input=input_, type="tool_use")


def _make_message(id_, model, content, usage, stop_reason="end_turn"):
    """Create a mock Message-like object with the attributes the adapter reads."""
    msg = MagicMock()
    msg.id = id_
    msg.model = model
    msg.content = content
    msg.usage = usage
    msg.stop_reason = stop_reason
    return msg


def _make_real_message(id_, text):
    """Build a real anthropic Message for use in MessageStartEvent."""
    return Message(
        id=id_,
        content=[TextBlock(text=text, type="text")],
        model="claude-sonnet-4-6",
        role="assistant",
        stop_reason="end_turn",
        type="message",
        usage=Usage(input_tokens=0, output_tokens=0),
    )


def _make_real_delta(stop_reason="end_turn"):
    """Build a real Delta for use in MessageDeltaEvent."""
    return Delta(stop_reason=stop_reason)


# ---------------------------------------------------------------------------
# ClaudeConfig.from_env() tests
# ---------------------------------------------------------------------------


class ClaudeConfigFromEnvTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None)
            for k in (
                "ANTHROPIC_API_KEY",
                "CLAUDE_MODEL",
                "CLAUDE_MAX_TOKENS",
                "CLAUDE_TEMPERATURE",
                "CLAUDE_TIMEOUT_SECONDS",
                "CLAUDE_MAX_RETRIES",
                "CLAUDE_BACKOFF_SECONDS",
                "CLAUDE_WORKSPACE_DIR",
            )
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_defaults_when_no_env_vars_set(self):
        cfg = ClaudeConfig.from_env()
        self.assertEqual(cfg.api_key, "")
        self.assertEqual(cfg.model, "claude-sonnet-4-6")
        self.assertEqual(cfg.max_tokens, 8192)
        self.assertEqual(cfg.temperature, 0.2)
        self.assertEqual(cfg.timeout_seconds, 300.0)
        self.assertEqual(cfg.max_retries, 2)
        self.assertEqual(cfg.backoff_seconds, 0.5)
        self.assertIsNone(cfg.workspace_dir)

    def test_reads_all_env_vars_when_set(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-key"
        os.environ["CLAUDE_MODEL"] = "claude-opus-5-1"
        os.environ["CLAUDE_MAX_TOKENS"] = "4096"
        os.environ["CLAUDE_TEMPERATURE"] = "0.7"
        os.environ["CLAUDE_TIMEOUT_SECONDS"] = "120"
        os.environ["CLAUDE_MAX_RETRIES"] = "5"
        os.environ["CLAUDE_BACKOFF_SECONDS"] = "1.5"
        os.environ["CLAUDE_WORKSPACE_DIR"] = "/tmp/workspace"

        cfg = ClaudeConfig.from_env()
        self.assertEqual(cfg.api_key, "sk-test-key")
        self.assertEqual(cfg.model, "claude-opus-5-1")
        self.assertEqual(cfg.max_tokens, 4096)
        self.assertEqual(cfg.temperature, 0.7)
        self.assertEqual(cfg.timeout_seconds, 120.0)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.backoff_seconds, 1.5)
        self.assertEqual(cfg.workspace_dir, "/tmp/workspace")

    def test_negative_max_retries_clamped_to_zero(self):
        os.environ["CLAUDE_MAX_RETRIES"] = "-3"
        cfg = ClaudeConfig.from_env()
        self.assertEqual(cfg.max_retries, 0)

    def test_partial_env_vars_override_only_set_keys(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-custom"
        os.environ["CLAUDE_MODEL"] = "claude-haiku-4-1"
        cfg = ClaudeConfig.from_env()
        self.assertEqual(cfg.api_key, "sk-custom")
        self.assertEqual(cfg.model, "claude-haiku-4-1")
        self.assertEqual(cfg.max_tokens, 8192)  # default
        self.assertEqual(cfg.backoff_seconds, 0.5)  # default


# ---------------------------------------------------------------------------
# _build_anthropic_messages tests
# ---------------------------------------------------------------------------


class BuildAnthropicMessagesTests(unittest.TestCase):
    def test_converts_string_content_to_text_block_list(self):
        msgs = [ClaudeMessage(role="user", content="hello")]
        api_msgs, system = _build_anthropic_messages(msgs)
        self.assertEqual(len(api_msgs), 1)
        self.assertEqual(api_msgs[0]["role"], "user")
        self.assertEqual(api_msgs[0]["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(system, "")

    def test_passes_list_content_through_unchanged(self):
        content = [{"type": "text", "text": "hello"}, {"type": "image", "source": {"data": "..."}}]
        msgs = [ClaudeMessage(role="user", content=content)]
        api_msgs, system = _build_anthropic_messages(msgs)
        self.assertEqual(api_msgs[0]["content"], content)

    def test_includes_name_when_present(self):
        msgs = [ClaudeMessage(role="user", content="hi", name="alice")]
        api_msgs, _ = _build_anthropic_messages(msgs)
        self.assertEqual(api_msgs[0]["name"], "alice")

    def test_omits_name_when_none(self):
        msgs = [ClaudeMessage(role="user", content="hi")]
        api_msgs, _ = _build_anthropic_messages(msgs)
        self.assertNotIn("name", api_msgs[0])

    def test_extracts_system_message_from_role_system(self):
        msgs = [
            ClaudeMessage(role="system", content="You are helpful."),
            ClaudeMessage(role="user", content="hi"),
        ]
        api_msgs, system = _build_anthropic_messages(msgs)
        self.assertEqual(len(api_msgs), 1)
        self.assertEqual(api_msgs[0]["role"], "user")
        self.assertEqual(system, "You are helpful.")

    def test_last_system_message_wins(self):
        msgs = [
            ClaudeMessage(role="system", content="First system."),
            ClaudeMessage(role="system", content="Second system."),
        ]
        api_msgs, system = _build_anthropic_messages(msgs)
        self.assertEqual(len(api_msgs), 0)
        self.assertEqual(system, "Second system.")

    def test_joins_multiple_text_blocks_in_system_message(self):
        msgs = [
            ClaudeMessage(role="system", content=[{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]),
        ]
        _, system = _build_anthropic_messages(msgs)
        self.assertEqual(system, "A\nB")

    def test_non_string_content_converted_to_string(self):
        msgs = [ClaudeMessage(role="user", content=42)]
        api_msgs, _ = _build_anthropic_messages(msgs)
        self.assertEqual(api_msgs[0]["content"], [{"type": "text", "text": "42"}])


# ---------------------------------------------------------------------------
# ClaudeResponse and ClaudeStreamEvent dataclass tests
# ---------------------------------------------------------------------------


class ClaudeResponseTests(unittest.TestCase):
    def setUp(self):
        self.usage = ModelUsage(input_tokens=10, output_tokens=5)

    def test_has_tool_calls_true(self):
        resp = ClaudeResponse(
            id="r1",
            model="claude-sonnet-4-6",
            content=[{"type": "tool_use", "id": "t1", "name": "search", "input": {}}],
            text="",
            tool_calls=[ClaudeToolCall(id="t1", name="search", input={"q": "test"})],
            stop_reason="tool_use",
            usage=self.usage,
        )
        self.assertTrue(resp.has_tool_calls)
        self.assertFalse(resp.has_text)

    def test_has_text_true(self):
        resp = ClaudeResponse(
            id="r1",
            model="claude-sonnet-4-6",
            content=[{"type": "text", "text": "Hello world"}],
            text="Hello world",
            tool_calls=[],
            stop_reason="end_turn",
            usage=self.usage,
        )
        self.assertTrue(resp.has_text)
        self.assertFalse(resp.has_tool_calls)

    def test_both_text_and_tool_calls(self):
        resp = ClaudeResponse(
            id="r1",
            model="claude-sonnet-4-6",
            content=[
                {"type": "text", "text": "Let me search."},
                {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}},
            ],
            text="Let me search.",
            tool_calls=[ClaudeToolCall(id="t1", name="search", input={"q": "x"})],
            stop_reason="tool_use",
            usage=self.usage,
        )
        self.assertTrue(resp.has_text)
        self.assertTrue(resp.has_tool_calls)


class ClaudeStreamEventTests(unittest.TestCase):
    def test_default_values(self):
        ev = ClaudeStreamEvent(type="text_delta")
        self.assertEqual(ev.type, "text_delta")
        self.assertEqual(ev.text, "")
        self.assertEqual(ev.tool_use_id, "")
        self.assertEqual(ev.tool_name, "")
        self.assertEqual(ev.tool_input_delta, "")
        self.assertIsNone(ev.usage)
        self.assertEqual(ev.stop_reason, "")

    def test_full_text_delta_event(self):
        ev = ClaudeStreamEvent(type="text_delta", text="hello")
        self.assertEqual(ev.type, "text_delta")
        self.assertEqual(ev.text, "hello")

    def test_full_tool_use_start_event(self):
        ev = ClaudeStreamEvent(
            type="tool_use_start",
            tool_use_id="toolu_1",
            tool_name="search",
        )
        self.assertEqual(ev.type, "tool_use_start")
        self.assertEqual(ev.tool_use_id, "toolu_1")
        self.assertEqual(ev.tool_name, "search")

    def test_message_stop_event_with_usage(self):
        usage = ModelUsage(input_tokens=20, output_tokens=10)
        ev = ClaudeStreamEvent(type="message_stop", usage=usage, stop_reason="end_turn")
        self.assertEqual(ev.type, "message_stop")
        self.assertEqual(ev.stop_reason, "end_turn")
        self.assertEqual(ev.usage.input_tokens, 20)


# ---------------------------------------------------------------------------
# ClaudeProvider.generate() tests
# ---------------------------------------------------------------------------


class _TestableClaudeProvider(ClaudeProvider):
    """ClaudeProvider that accepts a pre-built mock client."""

    def __init__(self, config, mock_client):
        super().__init__(config)
        self._client = mock_client


class ClaudeProviderGenerateTests(unittest.TestCase):
    def setUp(self):
        self.config = ClaudeConfig(
            api_key="sk-test",
            model="claude-sonnet-4-6",
            max_tokens=8192,
        )
        self.mock_client = MagicMock()

    def _make_provider(self):
        return _TestableClaudeProvider(self.config, self.mock_client)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_plain_text_response(self, mock_debug):
        mock_resp = _make_message(
            "msg_1",
            "claude-sonnet-4-6",
            [_mock_text_block("Hello from Claude")],
            _mock_usage(100, 50),
        )

        self.mock_client.messages.create.return_value = mock_resp

        provider = self._make_provider()
        result = provider.generate(
            messages=[ClaudeMessage(role="user", content="Say hello")],
        )

        self.assertIsInstance(result, ClaudeResponse)
        self.assertEqual(result.id, "msg_1")
        self.assertEqual(result.text, "Hello from Claude")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertEqual(result.usage.output_tokens, 50)
        self.assertFalse(result.has_tool_calls)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_with_tool_calls(self, mock_debug):
        mock_resp = _make_message(
            "msg_2",
            "claude-sonnet-4-6",
            [_mock_tool_use_block("toolu_1", "search", {"query": "weather"})],
            _mock_usage(80, 30),
            stop_reason="tool_use",
        )

        self.mock_client.messages.create.return_value = mock_resp

        provider = self._make_provider()
        result = provider.generate(
            messages=[ClaudeMessage(role="user", content="What is the weather?")],
            tools=[ClaudeToolSpec(name="search", description="Search", input_schema={"type": "object"})],
        )

        self.assertTrue(result.has_tool_calls)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].id, "toolu_1")
        self.assertEqual(result.tool_calls[0].name, "search")
        self.assertEqual(result.tool_calls[0].input["query"], "weather")
        self.assertEqual(result.stop_reason, "tool_use")

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_with_system_prompt(self, mock_debug):
        mock_resp = _make_message(
            "msg_3",
            "claude-sonnet-4-6",
            [_mock_text_block("OK")],
            _mock_usage(20, 5),
        )

        self.mock_client.messages.create.return_value = mock_resp

        provider = self._make_provider()
        result = provider.generate(
            messages=[ClaudeMessage(role="user", content="hi")],
            system="You are a helpful assistant.",
        )

        call_kwargs = self.mock_client.messages.create.call_args[1]
        self.assertEqual(call_kwargs["system"], "You are a helpful assistant.")
        self.assertEqual(result.text, "OK")

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_merges_explicit_system_with_message_system(self, mock_debug):
        mock_resp = _make_message(
            "msg_4",
            "claude-sonnet-4-6",
            [_mock_text_block("OK")],
            _mock_usage(),
        )

        self.mock_client.messages.create.return_value = mock_resp

        provider = self._make_provider()
        provider.generate(
            messages=[
                ClaudeMessage(role="system", content="System from messages."),
                ClaudeMessage(role="user", content="hi"),
            ],
            system="Explicit system.",
        )

        call_kwargs = self.mock_client.messages.create.call_args[1]
        self.assertIn("Explicit system.", call_kwargs["system"])
        self.assertIn("System from messages.", call_kwargs["system"])

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_overrides_temperature(self, mock_debug):
        mock_resp = _make_message(
            "msg_5",
            "claude-sonnet-4-6",
            [_mock_text_block("OK")],
            _mock_usage(),
        )

        self.mock_client.messages.create.return_value = mock_resp

        provider = self._make_provider()
        provider.generate(
            messages=[ClaudeMessage(role="user", content="hi")],
            temperature=0.9,
        )

        call_kwargs = self.mock_client.messages.create.call_args[1]
        self.assertEqual(call_kwargs["temperature"], 0.9)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_overrides_max_tokens(self, mock_debug):
        mock_resp = _make_message(
            "msg_6",
            "claude-sonnet-4-6",
            [_mock_text_block("OK")],
            _mock_usage(),
        )

        self.mock_client.messages.create.return_value = mock_resp

        provider = self._make_provider()
        provider.generate(
            messages=[ClaudeMessage(role="user", content="hi")],
            max_tokens=512,
        )

        call_kwargs = self.mock_client.messages.create.call_args[1]
        self.assertEqual(call_kwargs["max_tokens"], 512)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_accumulates_total_usage(self, mock_debug):
        mock_resp_1 = _make_message(
            "msg_7",
            "claude-sonnet-4-6",
            [_mock_text_block("First")],
            _mock_usage(30, 15),
        )
        mock_resp_2 = _make_message(
            "msg_8",
            "claude-sonnet-4-6",
            [_mock_text_block("Second")],
            _mock_usage(20, 10),
        )

        self.mock_client.messages.create.side_effect = [mock_resp_1, mock_resp_2]

        provider = self._make_provider()
        provider.generate(messages=[ClaudeMessage(role="user", content="A")])
        self.assertEqual(provider.total_usage.input_tokens, 30)
        self.assertEqual(provider.total_usage.output_tokens, 15)

        provider.generate(messages=[ClaudeMessage(role="user", content="B")])
        self.assertEqual(provider.total_usage.input_tokens, 50)
        self.assertEqual(provider.total_usage.output_tokens, 25)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_generate_retries_on_connection_error(self, mock_debug):
        import anthropic

        mock_resp = _make_message(
            "msg_retry",
            "claude-sonnet-4-6",
            [_mock_text_block("Retry ok")],
            _mock_usage(),
        )

        # APIConnectionError in anthropic v0.111.0 requires 'request' kwarg
        self.mock_client.messages.create.side_effect = [
            anthropic.APIConnectionError(message="network issue", request=MagicMock()),
            mock_resp,
        ]

        provider = _TestableClaudeProvider(
            ClaudeConfig(
                api_key="sk-test",
                model="claude-sonnet-4-6",
                max_retries=2,
                backoff_seconds=0,
            ),
            self.mock_client,
        )
        result = provider.generate(messages=[ClaudeMessage(role="user", content="hi")])
        self.assertEqual(result.text, "Retry ok")
        self.assertEqual(self.mock_client.messages.create.call_count, 2)


# ---------------------------------------------------------------------------
# ClaudeProvider.stream() tests
# ---------------------------------------------------------------------------


class ClaudeProviderStreamTests(unittest.TestCase):
    def setUp(self):
        self.config = ClaudeConfig(
            api_key="sk-test",
            model="claude-sonnet-4-6",
            max_tokens=8192,
        )
        self.mock_client = MagicMock()

    def _make_provider(self):
        return _TestableClaudeProvider(self.config, self.mock_client)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_stream_text_response(self, mock_debug):
        stream_events = [
            MessageStartEvent(
                message=_make_real_message("msg_stream_1", ""),
                type="message_start",
            ),
            ContentBlockStartEvent(
                index=0,
                content_block=TextBlock(text="", type="text"),
                type="content_block_start",
            ),
            ContentBlockDeltaEvent(
                index=0,
                delta=TextDelta(text="Hello", type="text_delta"),
                type="content_block_delta",
            ),
            ContentBlockDeltaEvent(
                index=0,
                delta=TextDelta(text=" world", type="text_delta"),
                type="content_block_delta",
            ),
            ContentBlockStopEvent(index=0, type="content_block_stop"),
            MessageDeltaEvent(
                delta=_make_real_delta("end_turn"),
                usage=MessageDeltaUsage(output_tokens=25),
                type="message_delta",
            ),
            MessageStopEvent(type="message_stop"),
        ]

        mock_stream = MagicMock()
        mock_stream.__iter__.return_value = stream_events
        self.mock_client.messages.stream.return_value.__enter__.return_value = mock_stream

        provider = self._make_provider()
        events, response = provider.stream(
            messages=[ClaudeMessage(role="user", content="Say hi")],
        )

        self.assertIsInstance(response, ClaudeResponse)
        self.assertIn("Hello world", response.text)
        self.assertEqual(response.stop_reason, "end_turn")
        self.assertGreater(len(events), 0)

        text_delta_texts = [e.text for e in events if e.type == "text_delta" and e.text]
        self.assertEqual(len(text_delta_texts), 2)
        self.assertEqual(text_delta_texts[0], "Hello")
        self.assertEqual(text_delta_texts[1], " world")

        stop_events = [e for e in events if e.type == "message_stop"]
        self.assertEqual(len(stop_events), 1)

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_stream_with_tool_use(self, mock_debug):
        stream_events = [
            MessageStartEvent(
                message=_make_real_message("msg_stream_2", ""),
                type="message_start",
            ),
            ContentBlockStartEvent(
                index=0,
                content_block=ToolUseBlock(id="toolu_a", name="fetch", input={}, type="tool_use"),
                type="content_block_start",
            ),
            ContentBlockDeltaEvent(
                index=0,
                delta=InputJSONDelta(partial_json='{"url":', type="input_json_delta"),
                type="content_block_delta",
            ),
            ContentBlockDeltaEvent(
                index=0,
                delta=InputJSONDelta(partial_json='"https://example.com"}', type="input_json_delta"),
                type="content_block_delta",
            ),
            ContentBlockStopEvent(index=0, type="content_block_stop"),
            MessageStopEvent(type="message_stop"),
        ]

        mock_stream = MagicMock()
        mock_stream.__iter__.return_value = stream_events
        self.mock_client.messages.stream.return_value.__enter__.return_value = mock_stream

        provider = self._make_provider()
        events, response = provider.stream(
            messages=[ClaudeMessage(role="user", content="Fetch example.com")],
        )

        tool_start = [e for e in events if e.type == "tool_use_start"]
        self.assertEqual(len(tool_start), 1)
        self.assertEqual(tool_start[0].tool_name, "fetch")
        self.assertEqual(tool_start[0].tool_use_id, "toolu_a")

        self.assertTrue(response.has_tool_calls)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "fetch")
        self.assertEqual(response.tool_calls[0].id, "toolu_a")
        self.assertEqual(response.tool_calls[0].input["url"], "https://example.com")

    @patch("agent_engine.claude_adapter.write_debug_event")
    def test_stream_cancelled_raises(self, mock_debug):
        provider = self._make_provider()

        delta_before = ContentBlockDeltaEvent(
            index=0,
            delta=TextDelta(text="Before cancel", type="text_delta"),
            type="content_block_delta",
        )
        delta_after = ContentBlockDeltaEvent(
            index=0,
            delta=TextDelta(text="After cancel", type="text_delta"),
            type="content_block_delta",
        )

        def _cancel_on_iter(stream_self):
            yield delta_before
            provider.cancel()
            yield delta_after

        mock_stream = MagicMock()
        mock_stream.__iter__ = _cancel_on_iter
        self.mock_client.messages.stream.return_value.__enter__.return_value = mock_stream

        with self.assertRaises(RuntimeError) as ctx:
            provider.stream(messages=[ClaudeMessage(role="user", content="hi")])
        self.assertIn("cancelled", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# map_anthropic_error tests
# ---------------------------------------------------------------------------


class MapAnthropicErrorTests(unittest.TestCase):
    def test_authentication_error_maps_to_claude_auth_error(self):
        import anthropic

        err = anthropic.AuthenticationError("Invalid API key", response=MagicMock(), body={})
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeAuthError)
        self.assertEqual(str(mapped), str(err))

    def test_rate_limit_error_maps_to_claude_rate_limit_error(self):
        import anthropic

        err = anthropic.RateLimitError("Too many requests", response=MagicMock(), body={})
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeRateLimitError)
        self.assertEqual(str(mapped), str(err))

    def test_api_timeout_error_maps_to_claude_timeout_error(self):
        import anthropic

        err = anthropic.APITimeoutError(request=MagicMock())
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeTimeoutError)

    def test_builtin_timeout_error_maps_to_claude_timeout_error(self):
        err = TimeoutError("Timed out")
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeTimeoutError)

    def test_runtime_error_with_cancelled_maps_to_claude_cancelled_error(self):
        err = RuntimeError("Request was cancelled by user")
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeCancelledError)

    def test_runtime_error_without_cancelled_falls_through_to_base(self):
        err = RuntimeError("Something else broke")
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeAdapterError)
        self.assertNotIsInstance(mapped, ClaudeCancelledError)

    def test_already_claude_adapter_error_passes_through(self):
        original = ClaudeAuthError("Already mapped")
        mapped = map_anthropic_error(original)
        self.assertIs(mapped, original)

    def test_generic_exception_maps_to_base_adapter_error(self):
        err = ValueError("Something went wrong")
        mapped = map_anthropic_error(err)
        self.assertIsInstance(mapped, ClaudeAdapterError)
        self.assertNotIsInstance(mapped, (ClaudeAuthError, ClaudeRateLimitError, ClaudeTimeoutError, ClaudeCancelledError))


# ---------------------------------------------------------------------------
# create_claude_provider factory tests
# ---------------------------------------------------------------------------


class CreateClaudeProviderTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("ANTHROPIC_API_KEY", "CLAUDE_MODEL")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    @patch("anthropic.Anthropic")
    def test_returns_claude_provider_with_defaults(self, mock_anthropic):
        provider = create_claude_provider()
        self.assertIsInstance(provider, ClaudeProvider)
        self.assertEqual(provider.config.model, "claude-sonnet-4-6")

    @patch("anthropic.Anthropic")
    def test_overrides_api_key_and_model(self, mock_anthropic):
        provider = create_claude_provider(
            api_key="sk-custom-key",
            model="claude-opus-5-1",
        )
        self.assertEqual(provider.config.api_key, "sk-custom-key")
        self.assertEqual(provider.config.model, "claude-opus-5-1")

    @patch("anthropic.Anthropic")
    def test_passes_extra_kwargs_to_config(self, mock_anthropic):
        provider = create_claude_provider(
            api_key="sk-extra",
            max_tokens=2048,
            temperature=0.5,
            max_retries=3,
        )
        self.assertEqual(provider.config.max_tokens, 2048)
        self.assertEqual(provider.config.temperature, 0.5)
        self.assertEqual(provider.config.max_retries, 3)

    @patch("anthropic.Anthropic")
    def test_ignores_unknown_kwargs(self, mock_anthropic):
        provider = create_claude_provider(
            api_key="sk-safe",
            not_a_real_field="should be ignored",
        )
        self.assertIsInstance(provider, ClaudeProvider)
        self.assertFalse(hasattr(provider.config, "not_a_real_field"))


# ---------------------------------------------------------------------------
# ModelUsage tests
# ---------------------------------------------------------------------------


class ModelUsageTests(unittest.TestCase):
    def test_total_tokens_sums_input_and_output(self):
        usage = ModelUsage(input_tokens=100, output_tokens=50)
        self.assertEqual(usage.total_tokens, 150)

    def test_defaults_zero(self):
        usage = ModelUsage()
        self.assertEqual(usage.input_tokens, 0)
        self.assertEqual(usage.output_tokens, 0)
        self.assertEqual(usage.total_tokens, 0)

    def test_from_anthropic_extracts_fields(self):
        mock_usage = Usage(input_tokens=200, output_tokens=100, cache_creation_input_tokens=10, cache_read_input_tokens=5)

        result = ModelUsage.from_anthropic(mock_usage, model="claude-sonnet-4-6")
        self.assertEqual(result.input_tokens, 200)
        self.assertEqual(result.output_tokens, 100)
        self.assertEqual(result.cache_creation_input_tokens, 10)
        self.assertEqual(result.cache_read_input_tokens, 5)
        self.assertEqual(result.model, "claude-sonnet-4-6")

    def test_from_anthropic_handles_none_values(self):
        mock_usage = Usage(input_tokens=0, output_tokens=0)

        result = ModelUsage.from_anthropic(mock_usage)
        self.assertEqual(result.input_tokens, 0)
        self.assertEqual(result.output_tokens, 0)


# ---------------------------------------------------------------------------
# ClaudeConfig defaults tests
# ---------------------------------------------------------------------------


class ClaudeConfigDefaultsTests(unittest.TestCase):
    def test_default_values_match_expected(self):
        cfg = ClaudeConfig()
        self.assertEqual(cfg.api_key, "")
        self.assertEqual(cfg.model, "claude-sonnet-4-6")
        self.assertEqual(cfg.max_tokens, 8192)
        self.assertEqual(cfg.temperature, 0.2)
        self.assertEqual(cfg.timeout_seconds, 300.0)
        self.assertEqual(cfg.max_retries, 2)
        self.assertEqual(cfg.backoff_seconds, 0.5)
        self.assertIsNone(cfg.workspace_dir)


# ---------------------------------------------------------------------------
# ClaudeToolSpec tests
# ---------------------------------------------------------------------------


class ClaudeToolSpecTests(unittest.TestCase):
    def test_to_anthropic_converts_correctly(self):
        spec = ClaudeToolSpec(
            name="search",
            description="Search the web",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        result = spec.to_anthropic()
        self.assertEqual(result["name"], "search")
        self.assertEqual(result["description"], "Search the web")
        self.assertEqual(result["input_schema"]["type"], "object")

    def test_default_timeout_and_approval(self):
        spec = ClaudeToolSpec(
            name="tool",
            description="desc",
            input_schema={"type": "object"},
        )
        self.assertEqual(spec.timeout_seconds, 300.0)
        self.assertFalse(spec.require_approval)
        self.assertIsNone(spec.allowed_paths)


# ---------------------------------------------------------------------------
# ClaudeProvider.build_tools tests
# ---------------------------------------------------------------------------


class ClaudeProviderBuildToolsTests(unittest.TestCase):
    def setUp(self):
        self.config = ClaudeConfig(api_key="sk-test")
        self.mock_client = MagicMock()

    def test_empty_tools_returns_empty_list(self):
        provider = _TestableClaudeProvider(self.config, self.mock_client)
        result = provider._build_tools(None)
        self.assertEqual(result, [])

    def test_builds_single_tool(self):
        provider = _TestableClaudeProvider(self.config, self.mock_client)
        tool = ClaudeToolSpec(
            name="run",
            description="Run command",
            input_schema={"type": "object"},
        )
        result = provider._build_tools([tool])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "run")

    def test_builds_multiple_tools(self):
        provider = _TestableClaudeProvider(self.config, self.mock_client)
        tools = [
            ClaudeToolSpec(name="t1", description="d1", input_schema={"type": "object"}),
            ClaudeToolSpec(name="t2", description="d2", input_schema={"type": "object"}),
        ]
        result = provider._build_tools(tools)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "t1")
        self.assertEqual(result[1]["name"], "t2")


# ---------------------------------------------------------------------------
# ClaudeProvider client lazy-init
# ---------------------------------------------------------------------------


class ClaudeProviderClientTests(unittest.TestCase):
    @patch("anthropic.Anthropic")
    def test_client_lazy_initialized_on_first_access(self, mock_anthropic):
        config = ClaudeConfig(
            api_key="sk-lazy",
            model="claude-sonnet-4-6",
            max_retries=3,
            timeout_seconds=120.0,
        )
        provider = ClaudeProvider(config)
        self.assertIsNone(provider._client)

        _ = provider.client
        mock_anthropic.assert_called_once_with(
            api_key="sk-lazy",
            max_retries=3,
            timeout=120.0,
        )

    @patch("anthropic.Anthropic")
    def test_client_cached_after_first_access(self, mock_anthropic):
        config = ClaudeConfig(api_key="sk-cache")
        provider = ClaudeProvider(config)
        _ = provider.client
        _ = provider.client
        self.assertEqual(mock_anthropic.call_count, 1)


if __name__ == "__main__":
    unittest.main()
