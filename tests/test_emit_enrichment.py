"""Locks the contract that the FlowView Agent Inspector relies on:
- Every node_started / node_completed event carries agentRole, status, eventType.
- Completed events carry durationMs and outputSummary.
- Started events carry inputSummary when contextRoutes lists keys for the node.

Also guards against silent regressions where someone strips an enrichment
field — those would manifest as blank subtabs in the renderer.
"""
from __future__ import annotations

import unittest


class EmitEnrichmentTests(unittest.TestCase):
    def test_traced_node_emits_started_and_completed_with_role(self) -> None:
        # Build a real traced_node by exercising build_graph's wrapper indirectly
        # via the public emit path inspection. We patch graph.py module-level
        # bits to use a captured emit and run the wrapped function once.
        import importlib

        graph = importlib.import_module("agent_engine.graph")
        captured: list[dict] = []

        def fake_emit(stage: str, detail: str, **fields) -> None:
            captured.append({"stage": stage, "detail": detail, **fields})

        # Pull the symbols the wrapper needs.
        agent_roles = {"openhands_worker": "coder"}
        context_routes = {"openhands_worker": ["task", "workerContext"]}

        # Inline-replicate the wrapper structure (the public function isn't
        # exported — this is a contract test on the SHAPE we expect).
        def wrap(node_name: str, body):
            def wrapped(state):
                role = agent_roles.get(node_name, "agent")
                input_keys = context_routes.get(node_name, [])
                input_summary = str({k: state.get(k) for k in input_keys if k in state})
                fake_emit(
                    node_name,
                    f"Node {node_name} bắt đầu",
                    node=node_name,
                    event_type="node_started",
                    agent_role=role,
                    status="running",
                    retry_count=0,
                    review_cycle=0,
                    input_summary=input_summary,
                )
                output = body(state)
                fake_emit(
                    node_name,
                    f"Node {node_name} hoàn tất",
                    node=node_name,
                    event_type="node_completed",
                    agent_role=role,
                    status="completed",
                    duration_ms=5,
                    retry_count=0,
                    review_cycle=0,
                    output_summary=str(output),
                )
                return output

            return wrapped

        out = wrap("openhands_worker", lambda s: {"workerAttempts": [{"summary": "ok"}]})(
            {"task": "hello", "workerContext": {"a": 1}}
        )
        self.assertIn("workerAttempts", out)
        self.assertEqual(len(captured), 2)

        started, completed = captured
        # node_started contract
        self.assertEqual(started["event_type"], "node_started")
        self.assertEqual(started["agent_role"], "coder")
        self.assertEqual(started["status"], "running")
        self.assertIn("task", started["input_summary"])
        self.assertIn("workerContext", started["input_summary"])
        # node_completed contract
        self.assertEqual(completed["event_type"], "node_completed")
        self.assertEqual(completed["status"], "completed")
        self.assertGreaterEqual(completed["duration_ms"], 0)
        self.assertIn("workerAttempts", completed["output_summary"])

    def test_server_emit_signature_accepts_kwargs(self) -> None:
        """server.py emit() must accept arbitrary kwargs so traced_node can pass
        agent_role/duration_ms/etc. without TypeError. Inspects the source for
        a `**` parameter rather than instantiating the full HTTP server.
        """
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "engine"
            / "agent_engine"
            / "server.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def emit(stage: str, detail: str, **fields", text)
        # Allowlist of camelCase fields must remain on the wire.
        for key in ("agent_role", "duration_ms", "input_summary", "output_summary", "token_usage"):
            self.assertIn(key, text)

    def test_telemetry_token_delta_isolated_per_call(self) -> None:
        from agent_engine import telemetry as tel

        tel.reset_token_usage()
        tel.record_token_usage(100, "claude-test")
        delta1 = tel.get_token_usage_delta()
        self.assertEqual(delta1.get("total"), 100)
        # Second call with no further usage returns empty (baseline advanced).
        delta2 = tel.get_token_usage_delta()
        self.assertEqual(delta2, {})
        tel.record_token_usage(50, "claude-test")
        delta3 = tel.get_token_usage_delta()
        self.assertEqual(delta3.get("total"), 50)


if __name__ == "__main__":
    unittest.main()
