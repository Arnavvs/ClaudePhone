"""The decider A/B harness (B5), without a phone or a model."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone.harness import ab  # noqa: E402
from claudephone.harness.loop import Budget  # noqa: E402


class FakeAgent:
    def __init__(self, model, answer, steps=3):
        self.model, self.answer, self.steps = model, answer, steps
        self.chat = type("C", (), {"tool_convention": "native"})()

    def run(self, goal):
        for i in range(self.steps):
            yield {"type": "tool_call", "tool": "ui_dump", "step": i + 1}
        yield {"type": "final", "content": self.answer, "steps": self.steps,
               "cost_usd": 0.0, "requests": self.steps + 1, "free_requests_left": 40}


def builder(answers):
    def build(provider="", model="", budget=None, operator_notes="", stagnation=True):
        return FakeAgent(model, answers[model])
    return build


def test_each_decider_runs_on_the_same_goal_and_is_judged_by_the_regex():
    resets = []
    rows = ab.run_ab("android version?", ["big:free", "tiny:free"], Budget(max_steps=8),
                     expect=r"\b12\b", reset=lambda: resets.append(1),
                     build=builder({"big:free": "It runs Android 12.",
                                    "tiny:free": "I could not find it."}))
    assert [r["model"] for r in rows] == ["big:free", "tiny:free"]
    assert [r["correct"] for r in rows] == [True, False]
    assert len(resets) == 2                       # same starting screen for both
    assert rows[0]["tools"] == ["ui_dump"] * 3


def test_a_run_the_quota_cannot_finish_is_skipped_before_it_starts():
    left = iter([20, 5])
    rows = ab.run_ab("x", ["a:free", "b:free"], Budget(max_steps=8, free_reserve=2),
                     quota=lambda: next(left),
                     build=builder({"a:free": "ok", "b:free": "ok"}))
    assert rows[0]["stopped_by"] == "done"
    assert rows[1]["stopped_by"] == "skipped" and "5 free requests" in rows[1]["error"]


def test_the_table_renders_skipped_and_finished_rows():
    rows = [{"model": "a", "stopped_by": "done", "correct": True, "steps": 3,
             "seconds": 4.2, "cost_usd": 0.0, "requests": 4},
            {"model": "b", "stopped_by": "skipped", "error": "quota"}]
    t = ab.table(rows)
    assert "a" in t and "skipped" in t
