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


# -- pre-registered plans ---------------------------------------------------------

PLAN = {"name": "t", "models": ["A:free", "B:free"], "trials": 2, "free_reserve": 2,
        "tasks": [{"id": "T1", "goal": "g1", "expect": r"\b10 minutes", "max_steps": 5,
                   "guard": {"cmd": "get", "value": "600000", "restore": "put 600000"}},
                  {"id": "T2", "goal": "g2", "expect": "april", "max_steps": 5}]}


def test_plan_order_alternates_which_model_goes_first():
    ids = [i["id"] for i in ab.plan_items(PLAN)]
    assert len(ids) == 8
    first_on = {(i["round"], i["task"]): i["model"] for i in reversed(ab.plan_items(PLAN))}
    assert first_on[(1, "T1")] != first_on[(1, "T2")]
    assert first_on[(1, "T1")] != first_on[(2, "T1")]


def test_a_plan_pauses_for_the_quota_and_resumes_where_it_stopped(tmp_path):
    state = str(tmp_path / "s.json")
    answers = {"A:free": "It is 10 minutes, patch 1 April 2022",
               "B:free": "no idea"}
    left = iter([20, 20, 3])                         # third run cannot fit
    st = ab.run_plan(PLAN, state, quota=lambda: next(left),
                     build=builder(answers), log=lambda *a: None)
    assert len(st["results"]) == 2 and st["stopped"]["next"]
    st = ab.run_plan(PLAN, state, quota=lambda: 50, build=builder(answers),
                     log=lambda *a: None)
    assert len(st["results"]) == 8 and "stopped" not in st
    s = {(r["task"], r["model"]): r for r in ab.summarise(PLAN, st)}
    assert s[("T1", "A:free")]["correct"] == 2 and s[("T1", "B:free")]["correct"] == 0


def test_a_changed_setting_is_restored_and_counted_against_the_run(tmp_path):
    calls = []

    def shell(cmd):
        calls.append(cmd)
        return "30000" if cmd == "get" else ""       # the run left 30 s behind

    st = ab.run_plan(PLAN, str(tmp_path / "s.json"), quota=lambda: 50,
                     build=builder({"A:free": "10 minutes", "B:free": "10 minutes"}),
                     log=lambda *a: None, shell=shell)
    t1 = [r for r in st["results"].values() if r["task"] == "T1"]
    assert all(r["changed_setting"]["was"] == "30000" for r in t1)
    assert calls.count("put 600000") == len(t1)
    t2 = [r for r in st["results"].values() if r["task"] == "T2"]
    assert not any(r.get("changed_setting") for r in t2)   # no guard on T2
    s = {(r["task"], r["model"]): r for r in ab.summarise(PLAN, st)}
    assert s[("T1", "A:free")]["changed_a_setting"] == 2
