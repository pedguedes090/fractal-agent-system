"""Contract tests for the autonomous next-task selector.

These cover the priority ordering the Auto Loop UI relies on:
  security > test_coverage > maintainability > technical_debt > enhancement_idea
and guarantee the rotating-idea pool never starves (i.e. when findings are
exhausted or all completed, an idea is always returned until the pool is
fully drained too).
"""
from __future__ import annotations

import unittest

from agent_engine.autonomy import select_next_task, _ENHANCEMENT_IDEAS


def _f(fid: str, category: str, priority: float, *, title: str = "t") -> dict:
    return {
        "id": fid,
        "category": category,
        "title": title,
        "source": f"src/{fid}.py:1",
        "evidence": "evidence",
        "recommendation": "rec",
        "priorityScore": priority,
        "confidence": 0.8,
        "impact": 2.0,
        "effort": 1.0,
        "severity": "high",
    }


class SelectNextTaskTests(unittest.TestCase):
    def test_security_beats_test_coverage_beats_maintainability(self) -> None:
        report = {
            "findings": [
                _f("a", "maintainability", 9.0),
                _f("b", "test_coverage", 1.0),
                _f("c", "security", 0.5),
            ]
        }
        task = select_next_task(report, completed_ids=set())
        self.assertIsNotNone(task)
        self.assertEqual(task["id"], "c")
        self.assertEqual(task["category"], "security")
        self.assertEqual(task["kind"], "finding")

    def test_higher_priority_score_wins_within_category(self) -> None:
        report = {
            "findings": [
                _f("low", "security", 1.0),
                _f("high", "security", 5.0),
            ]
        }
        task = select_next_task(report, completed_ids=set())
        self.assertEqual(task["id"], "high")

    def test_completed_ids_skip_to_next(self) -> None:
        report = {"findings": [_f("a", "security", 5.0), _f("b", "security", 4.0)]}
        task = select_next_task(report, completed_ids={"a"})
        self.assertEqual(task["id"], "b")

    def test_falls_back_to_enhancement_pool_when_findings_empty(self) -> None:
        task = select_next_task({"findings": []}, completed_ids=set())
        self.assertIsNotNone(task)
        self.assertEqual(task["kind"], "enhancement_idea")
        # Default cursor=0 picks the first idea.
        self.assertEqual(task["id"], _ENHANCEMENT_IDEAS[0]["id"])

    def test_idea_cursor_rotates_through_pool(self) -> None:
        task1 = select_next_task({}, completed_ids=set(), idea_cursor=0)
        task2 = select_next_task({}, completed_ids=set(), idea_cursor=1)
        self.assertNotEqual(task1["id"], task2["id"])

    def test_returns_none_when_findings_and_ideas_exhausted(self) -> None:
        completed = {idea["id"] for idea in _ENHANCEMENT_IDEAS}
        task = select_next_task({"findings": []}, completed_ids=completed)
        self.assertIsNone(task)

    def test_finding_task_body_includes_recommendation(self) -> None:
        report = {"findings": [_f("x", "security", 5.0, title="risky pattern")]}
        task = select_next_task(report, completed_ids=set())
        self.assertIn("risky pattern", task["task"])
        self.assertIn("rec", task["task"])
        self.assertIn("src/x.py:1", task["task"])


if __name__ == "__main__":
    unittest.main()
