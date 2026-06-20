from __future__ import annotations

import unittest
from pathlib import Path

from agent_engine.deterministic_workflow import DEFAULT_WORKFLOW, DeterministicWorkflow


class DeterministicWorkflowTests(unittest.TestCase):
    def test_yaml_routes_rework_to_execution_gate_after_fixed_limit(self) -> None:
        workflow = DeterministicWorkflow.load()

        self.assertEqual(
            workflow.route(
                "reviewer_decision",
                {
                    "worker_error": False,
                    "review_passed": False,
                    "can_rework": True,
                },
            ),
            "openhands_worker",
        )
        self.assertEqual(
            workflow.route(
                "reviewer_decision",
                {
                    "has_result": False,
                    "worker_error": False,
                    "review_passed": False,
                    "changes_required": False,
                    "replan_required": False,
                    "blocked": False,
                    "can_rework": False,
                },
            ),
            "execution_gate",
        )
        self.assertEqual(workflow.route("execution_gate", {"auto_rework_granted": True}), "openhands_worker")
        # When auto-rework is exhausted, route MUST land on the reporter so
        # assistantText is assembled and finalize_workspace runs — routing
        # to a no-op `reporter_end` is the v5 hang bug we're guarding against.
        self.assertEqual(workflow.route("execution_gate", {"auto_rework_granted": False}), "reporter")
        # Blocked verdict must also flow through reporter (assembles
        # assistantText including review.blockers) so the run completes.
        self.assertEqual(
            workflow.route(
                "reviewer_decision",
                {
                    "has_result": False,
                    "worker_error": False,
                    "review_passed": False,
                    "changes_required": False,
                    "replan_required": False,
                    "blocked": True,
                    "can_rework": False,
                },
            ),
            "reporter",
        )
        self.assertEqual(workflow.limits["maxReworkAttempts"], 3)
        self.assertEqual(workflow.limits["maxAutoApprovalCycles"], 1)

    def test_explicit_context_route_excludes_global_history_and_secrets(self) -> None:
        state = {
            "task": "fix",
            "problem": {"problemStatement": "fix"},
            "candidatePlans": [{"name": "minimal"}],
            "messages": [{"role": "user", "content": "secret history"}],
            "settings": {"apiKey": "secret"},
            "trustedRepoContext": {"files": ["not allowed here"]},
        }

        envelope = DEFAULT_WORKFLOW.context_for("planning_minimal", state)

        self.assertEqual(set(envelope["inputs"]), {"problem"})
        self.assertNotIn("messages", envelope["inputs"])
        self.assertNotIn("settings", envelope["inputs"])
        self.assertNotIn("trustedRepoContext", envelope["inputs"])

        repo_envelope = DEFAULT_WORKFLOW.context_for(
            "intake_repo_context",
            {
                "task": "fix",
                "taskIntent": {"mode": "modify"},
                "preflight": {"files": []},
                "trustedRepoContext": {"files": []},
                "codegraphContext": {"enabled": False},
                "longTermMemory": {"enabled": True, "memories": [{"source": "README.md"}]},
                "messages": [{"role": "user", "content": "secret history"}],
                "settings": {"apiKey": "secret"},
            },
        )
        self.assertIn("longTermMemory", repo_envelope["inputs"])
        self.assertNotIn("messages", repo_envelope["inputs"])
        self.assertNotIn("settings", repo_envelope["inputs"])

        reviewer_envelope = DEFAULT_WORKFLOW.context_for(
            "code_reviewer_agent",
            {
                "securityReview": {"passed": True},
                "testerResult": {"should": "not leak across the edge"},
                "workerAttempts": [{"should": "not leak across the edge"}],
            },
        )
        self.assertEqual(set(reviewer_envelope["inputs"]), {"securityReview"})

    def test_graph_topology_is_declared_in_yaml_not_python_edge_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        graph_source = (root / "engine" / "agent_engine" / "graph.py").read_text(encoding="utf-8")
        workflow_source = (root / "engine" / "agent_engine" / "workflows" / "default.yaml").read_text(encoding="utf-8")

        self.assertNotIn("builder.add_conditional_edges", graph_source)
        self.assertNotIn("builder.add_edge(", graph_source)
        self.assertIn("routes:", workflow_source)
        self.assertIn("when: can_rework", workflow_source)
        self.assertIn("default: execution_gate", workflow_source)


if __name__ == "__main__":
    unittest.main()
