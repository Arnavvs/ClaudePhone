"""Step capsules, remember and recall (B7).

    python -m pytest tests/test_history.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import state  # noqa: E402
from claudephone.harness import history, recorder  # noqa: E402
from claudephone.harness import loop as loop_mod  # noqa: E402
from claudephone.harness.loop import Agent  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.harness.stagnation import Stagnation  # noqa: E402
from claudephone.tools import memory_tools  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"
BANNED = ("success", "fail", "navigated")


def el(text, i=0, rid=""):
    return Element(i=i, rid=rid, anchor=rid, text=text, desc="", cls="TextView",
                   bounds=(0, i * 10, 10, i * 10 + 10), clickable=False)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder, "RUNS_DIR", str(tmp_path / "runs"))
    history.current.reset()
    state.last.update(elements=[], at=0.0, pkg=None, ver="", fp="")
    yield


# -- capsules -----------------------------------------------------------------

def snap(values, pkg=IG, at=1.0, fp=None):
    return {"fp": fp or ("fp-" + "|".join(sorted(values))), "at": at, "pkg": pkg,
            "values": set(values)}


def test_a_changed_screen_says_what_appeared_and_how_to_get_it_back():
    line = history.capsule(9, "tap", {"ref": "5_12"}, {"tapped": True},
                           snap({"Follow"}), snap({"Following", "1,204"}, at=2.0),
                           at=100 + 92, started=100)
    assert line.startswith("T+01:32 #9 tap(ref='5_12') -> content changed")
    assert "'Following'" in line and "'1,204'" in line
    assert "recall(steps=[9])" in line
    assert "'Follow'" not in line.split("appeared:")[1]      # vanished, not appeared


@pytest.mark.parametrize("result,before,after,expect", [
    ({"ok": 1}, snap({"a"}), snap({"a"}), "no screen read"),
    ({"ok": 1}, snap({"a"}), snap({"a"}, at=2.0), "screen unchanged"),
    ({"ok": 1}, snap({"a"}), snap({"a"}, at=2.0, fp="scrolled"),
     "same items, positions moved"),
    ({"error": "VERSION_MISMATCH"}, snap({"a"}), snap({"b"}, at=2.0),
     "returned an error: VERSION_MISMATCH"),
    ({"ok": 1}, snap({"a"}, pkg="com.android.settings"), snap({"b"}, at=2.0),
     "app changed com.android.settings -> " + IG),
])
def test_capsules_describe_observations_never_verdicts(result, before, after, expect):
    line = history.capsule(3, "swipe", {"dir": "up"}, result, before, after, 5, 0)
    assert expect in line
    assert not any(w in line.lower() for w in BANNED)


def test_long_values_and_many_appearances_stay_on_one_short_line():
    after = snap({"x" * 500, "y" * 300, "z", "w", "v"}, at=2.0)
    line = history.capsule(4, "ui_dump", {}, {"elements": []}, snap(set()), after, 1, 0)
    assert "\n" not in line and len(line) < 300 and "+2 more" in line


# -- notes --------------------------------------------------------------------

def test_notes_are_kept_cleared_and_capped():
    h = history.RunHistory()
    assert h.remember("followers", "1,204")["remembered"] == "followers"
    assert "- followers: 1,204" in h.notes_block()
    assert h.remember("followers", "")["forgot"] == "followers"
    assert h.notes_block() == ""
    for n in range(history.MAX_NOTES):
        h.remember("k%d" % n, "v")
    assert "limit" in h.remember("one-too-many", "v")["error"]
    assert "remembered" in h.remember("k0", "overwrite is fine")


def test_a_verdict_in_a_note_is_kept_but_flagged():
    r = history.RunHistory().remember("follow", "successfully followed")
    assert r["remembered"] and "not a verdict" in r["hint"]


# -- recall -------------------------------------------------------------------

def _events():
    return [
        {"type": "tool_call", "step": 1, "tool": "open_link", "args": {"url": "u"}},
        {"type": "tool_result", "step": 1, "tool": "open_link",
         "result": {"elements": [{"t": "1,204 followers"}]}},
        {"type": "thought", "content": "The bio says Delhi based"},
        {"type": "tool_call", "step": 2, "tool": "recall", "args": {"query": "followers"}},
        {"type": "tool_result", "step": 2, "tool": "recall",
         "result": {"hits": [{"snippet": "1,204 followers"}]}},
    ]


def test_recall_reads_the_runs_own_file(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in
                              [{"kind": "meta"}] + _events()) + "\n{partial",
                    encoding="utf-8")
    h = history.RunHistory()
    h.reset(str(path))
    got = h.recall(steps=[1, 7])["steps"]
    assert got[0]["tool"] == "open_link" and "1,204 followers" in got[0]["result"]
    assert "no such step" in got[1]["error"]
    hits = h.recall(query="FOLLOWERS")["hits"]
    assert [x.get("step") for x in hits] == [1]           # not recall's own result
    assert h.recall(query="delhi")["hits"][0]["thought"] is True


def test_recall_works_from_memory_when_recording_is_off():
    h = history.RunHistory()
    for e in _events():
        h.event(e)
    assert h.recall(steps=[1])["steps"][0]["tool"] == "open_link"
    assert h.recall(query="nothing like this")["note"]
    assert "error" in h.recall()


# -- in the loop --------------------------------------------------------------

class FakeChat(Chat):
    def __init__(self, replies, native=True):
        super().__init__(ModelConfig(provider="local", model="fake"))
        self._replies = list(replies)
        self._native_ok = native
        self.seen: list = []

    def complete(self, messages, tools=None):
        self.seen.append(messages)
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 10
        return self._replies.pop(0) if self._replies else Reply(content="done")


def registry():
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Open a profile page.")
        def open_profile(n: int = 0) -> dict:
            state.remember([el("creator_%d" % n), el("%d,204 followers" % n, 1)], IG)
            return {"elements": [e.text for e in state.last["elements"]] + ["pad" * 200]}
    memory_tools.register(r)
    return r


def call(name, args, cid):
    return Reply(content="", tool_calls=[{"id": cid, "name": name, "args": args}])


def run_ten(native):
    replies = [call("open_profile", {"n": 1}, "c1"),
               call("remember", {"key": "followers", "value": "1,204"}, "c2")]
    replies += [call("open_profile", {"n": n}, "c%d" % n) for n in range(3, 11)]
    replies += [call("recall", {"steps": [1]}, "c11"), Reply(content="1,204")]
    chat = FakeChat(replies, native=native)
    agent = Agent(chat, registry(), stagnation=Stagnation(stop_after=0))
    events = list(agent.run("How many followers does creator_1 have?"))
    return chat, agent, events


@pytest.mark.parametrize("native", [True, False])
def test_old_results_become_capsules_in_both_tool_conventions(native):
    chat, agent, events = run_ten(native)
    wire = chat.seen[-1]
    results = [m for m in wire if m.get("role") == "tool"
               or str(m.get("content", "")).startswith("Result of ")]
    capsules = [m for m in results if "recall(steps=[" in str(m["content"])]
    assert capsules, "nothing was compacted"
    assert all(len(m["content"]) < 400 for m in capsules)
    first = str(results[0]["content"])
    assert "#1 open_profile(n=1) -> " in first and "'1,204 followers'" in first
    assert "pad" * 50 not in first
    # the six most recent results are still whole
    assert all("recall(steps=[" not in str(m["content"]) for m in results[-6:])
    assert not any(k.startswith("_") for m in wire for k in m)


def test_notes_ride_in_the_system_message_and_recall_brings_step_one_back():
    chat, agent, events = run_ten(True)
    assert "NOTES YOU SAVED" in chat.seen[-1][0]["content"]
    assert "- followers: 1,204" in chat.seen[-1][0]["content"]
    assert "NOTES YOU SAVED" not in agent.messages[0]["content"]   # not baked in
    rec = [e for e in events if e.get("type") == "tool_result" and e["tool"] == "recall"]
    got = rec[0]["result"]["steps"][0]
    assert got["step"] == 1 and "1,204 followers" in got["result"]


def test_pinning_notes_does_not_count_as_an_idle_screen():
    st = Stagnation(warn_after=2, stop_after=0, idle_warn=2)
    state.remember([el("same")], IG)
    fp = st.before()
    for n in range(4):
        assert st.observe(n + 1, "remember", {"key": "k%d" % n, "value": "v"}, fp) is None
    assert st.idle == 0


def test_keep_window_is_what_the_docs_say():
    assert loop_mod.KEEP_FULL_RESULTS == 6
