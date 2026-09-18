"""Stagnation detection (B4), without a phone or a model.

The point of these tests is the distinction the feature rests on: repeating a
call that MOVES the screen is progress, repeating one that does not is a loop.

    python -m pytest tests/test_stagnation.py
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import state  # noqa: E402
from claudephone.harness.loop import Agent, Budget  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.harness.stagnation import Stagnation  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"


def el(rid, text=""):
    return Element(i=0, rid=rid, anchor=rid, text=text, desc="", cls="Button",
                   bounds=(0, 0, 10, 10), clickable=True)


def show(rid):
    """Put a distinct screen in front of the tracker."""
    state.remember([el(rid, text=rid)], IG)


@pytest.fixture(autouse=True)
def clean_state():
    state.remember([el("start")], IG)
    yield


def run(stag, calls):
    """Feed (tool, args, screen_after) triples. -> the advisories produced."""
    out = []
    for i, (tool, args, screen) in enumerate(calls, start=1):
        before = stag.before()
        show(screen)
        out.append(stag.observe(i, tool, args, before))
    return out


def test_repeating_a_call_on_an_unchanged_screen_warns_then_stops():
    stag = Stagnation()
    adv = run(stag, [("tap", {"ref": "1_3"}, "a")] * 4)
    assert adv[0] is None                                   # first attempt: fine
    assert adv[1] is None                                   # screen changed into "a"
    assert adv[2]["reason"] == "possibly_stuck" and not adv[2]["stop"]
    assert adv[3]["stop"] and adv[3]["reason"] == "stagnation"
    assert "not responding" in adv[3]["message"]


def test_a_call_that_moves_the_screen_is_never_stagnation():
    stag = Stagnation()
    adv = run(stag, [("swipe", {"direction": "up"}, "reel%d" % i) for i in range(8)])
    assert all(a is None or a["reason"] == "revisited" for a in adv)
    assert not any(a and a.get("stop") for a in adv)


def test_the_counter_resets_as_soon_as_the_screen_moves():
    stag = Stagnation()
    adv = run(stag, [("tap", {"ref": "1_3"}, "a"),
                     ("tap", {"ref": "1_3"}, "a"),     # no change
                     ("tap", {"ref": "1_3"}, "b"),     # moved: reset
                     ("tap", {"ref": "1_3"}, "b"),
                     ("tap", {"ref": "1_3"}, "b")])
    assert not any(a and a.get("stop") for a in adv)


def test_different_arguments_are_different_attempts():
    stag = Stagnation()
    adv = run(stag, [("tap", {"ref": "1_3"}, "a"),
                     ("tap", {"ref": "1_4"}, "a"),
                     ("tap", {"ref": "1_5"}, "a"),
                     ("tap", {"ref": "1_6"}, "a")])
    assert not any(a and a.get("stop") for a in adv)


def test_reading_between_two_taps_does_not_hide_the_loop():
    """The normal loop is tap -> look -> tap -> look; the taps still count."""
    stag = Stagnation()
    calls = []
    for _ in range(4):
        calls += [("tap", {"ref": "1_3"}, "a"), ("ui_dump", {}, "a")]
    adv = run(stag, calls)
    assert any(a and a["reason"] == "possibly_stuck" for a in adv)
    assert any(a and a.get("stop") for a in adv)


def test_a_screen_seen_much_earlier_is_reported_once_and_does_not_stop_the_run():
    stag = Stagnation(revisit_gap=3)
    adv = run(stag, [("tap", {"n": i}, s) for i, s in
                     enumerate(["home", "b", "c", "d", "e", "home", "home2"])])
    hints = [a for a in adv if a and a["reason"] == "revisited"]
    assert len(hints) == 1 and not hints[0]["stop"]
    assert "step 1" in hints[0]["message"]


def test_stop_after_zero_warns_forever_but_never_stops():
    stag = Stagnation(stop_after=0)
    adv = run(stag, [("tap", {"ref": "1_3"}, "a")] * 6)
    assert any(a and a["reason"] == "possibly_stuck" for a in adv)
    assert not any(a and a.get("stop") for a in adv)


def test_a_tool_that_never_reads_the_screen_still_counts(monkeypatch):
    """Measured live on the Samsung: 25 identical swipes with no ui_dump between
    them ran all the way to max_steps without a word, because "no fingerprint at
    all" was being treated as "the screen moved"."""
    monkeypatch.setattr(state, "last", {})           # nothing has read the screen
    stag = Stagnation()
    out = [stag.observe(i + 1, "swipe", {"direction": "down"}, "") for i in range(4)]
    assert any(a and a["reason"] == "possibly_stuck" for a in out)
    assert out[-1] and out[-1]["stop"]


# -- through the real agent loop ------------------------------------------------

class FakeChat(Chat):
    def __init__(self, reply_factory):
        super().__init__(ModelConfig(provider="local", model="fake"))
        self._make = reply_factory
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        self.seen = messages
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 10
        return self._make(self.calls)


def stuck_registry():
    reg = ToolRegistry()
    with reg.pack("core"):
        @reg.tool(description="Tap something that never works.")
        def tap(ref: str = "") -> dict:
            show("frozen")                      # same screen, every time
            return {"tapped": ref}
    return reg


def test_the_agent_stops_with_stagnation_not_max_steps():
    calls = [{"id": "c1", "name": "tap", "args": {"ref": "1_3"}}]
    agent = Agent(FakeChat(lambda n: Reply(content="again", tool_calls=calls)),
                  stuck_registry(), budget=Budget(max_steps=30))
    events = list(agent.run("tap the thing"))
    final = [e for e in events if e["type"] == "final"][-1]
    assert final["stopped_by"] == "stagnation"
    assert final["steps"] < 10                       # long before max_steps
    assert final["stagnation"]["tool"] == "tap"
    notes = [e for e in events if e["type"] == "note"]
    assert any(n["reason"] == "possibly_stuck" for n in notes)
    # the model must actually SEE the warning, not just the event stream
    assert any(m.get("role") == "user" and "POSSIBLY STUCK" in str(m.get("content"))
               for m in agent.messages)


def test_disabling_the_stop_lets_the_budget_end_it_instead():
    calls = [{"id": "c1", "name": "tap", "args": {"ref": "1_3"}}]
    agent = Agent(FakeChat(lambda n: Reply(content="again", tool_calls=calls)),
                  stuck_registry(), budget=Budget(max_steps=6),
                  stagnation=Stagnation(stop_after=0))
    final = [e for e in agent.run("tap") if e["type"] == "final"][-1]
    assert final["stopped_by"].startswith("max_steps")


def test_alternating_reads_on_a_frozen_screen_are_caught():
    """Measured live: ui_dump {} / ui_dump {"query": "About"} / ui_dump {} ...
    Different arguments every time, so no single call ever repeated."""
    stag = Stagnation()
    calls = [("ui_dump", {}, "settings"), ("ui_dump", {"query": "About"}, "settings")] * 3
    adv = run(stag, calls)
    idle = [a for a in adv if a and a["reason"] == "idle_screen"]
    assert len(idle) == 1 and not idle[0]["stop"]
    assert "scroll_to" in idle[0]["message"]


def test_a_budget_stop_reports_the_models_last_words():
    agent_replies = [Reply(content="The device reports Android 12. Let me also check Settings.",
                           tool_calls=[{"id": "c1", "name": "tap", "args": {"ref": str(i)}}])
                     for i in range(5)]
    agent = Agent(FakeChat(lambda n: agent_replies[min(n - 1, 4)]), stuck_registry(),
                  budget=Budget(max_steps=2), stagnation=Stagnation(stop_after=0))
    final = [e for e in agent.run("android version?") if e["type"] == "final"][-1]
    assert final["stopped_by"].startswith("max_steps")
    assert final["content"] == "" and "Android 12" in final["last_thought"]
