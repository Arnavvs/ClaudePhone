"""Guarded replay of verified runs (B9).

    python -m pytest tests/test_replay.py
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import state  # noqa: E402
from claudephone.harness import history, recorder, replay  # noqa: E402
from claudephone.harness.loop import Agent  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.policy import writes as wr  # noqa: E402
from claudephone.ui import Element  # noqa: E402

SET = "com.android.settings"


def el(i, text, rid=""):
    return Element(i=i, rid=rid, anchor=rid, text=text, desc="", cls="TextView",
                   bounds=(0, i * 100, 1080, i * 100 + 90), clickable=True)


class Phone:
    """Settings as a tiny state machine: home -> display -> timeout."""

    SCREENS = {
        "home": ("com.sec.android.app.launcher", ["Camera", "Settings", "Play Store"]),
        "settings": (SET, ["Connections", "Display", "Sound", "Search"]),
        "display": (SET, ["Brightness", "Screen timeout", "Font size", "Navigate up"]),
        "timeout": (SET, ["30 seconds", "1 minute", "10 minutes", "Navigate up"]),
        "sound": (SET, ["Ringtone", "Vibration", "Navigate up"]),
        "search": (SET, ["Search settings", "Navigate up", "{q}"]),
    }
    MOVES = {("settings", "Display"): "display", ("display", "Screen timeout"): "timeout",
             ("settings", "Sound"): "sound", ("settings", "Search"): "search"}

    def __init__(self):
        self.at = "home"
        self.query = ""
        self.calls = []

    def read(self):
        pkg, labels = self.SCREENS[self.at]
        labels = [t.replace("{q}", self.query) for t in labels]
        state.remember([el(i, t, rid="title" if t in ("Display", "Sound") else "")
                        for i, t in enumerate(labels)], pkg)

    def registry(self):
        r = ToolRegistry()
        phone = self
        with r.pack("core"):
            @r.tool(description="Open an app.")
            def launch_app(package: str) -> dict:
                phone.calls.append(("launch_app", package))
                phone.at = "settings"
                return {"launched": package}

            @r.tool(description="Tap.")
            def tap(ref: str = "") -> dict:
                p = state.parse_ref(ref)
                if not p or p[0] != state.version():
                    return {"error": "VERSION_MISMATCH"}
                label = state.last["elements"][p[1]].text
                phone.calls.append(("tap", label))
                phone.at = phone.MOVES.get((phone.at, label), phone.at)
                return {"tapped": label}

            @r.tool(description="Type.")
            def text_input(text: str) -> dict:
                phone.calls.append(("text_input", text))
                phone.query = text
                return {"typed": text}

            @r.tool(description="Read.")
            def ui_dump() -> dict:
                phone.read()
                return {"elements": [e.text for e in state.last["elements"]]}
        return r


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(replay, "MACRO_DIR", str(tmp_path / "macros"))
    history.current.reset()
    state.last.update(elements=[], at=0.0, pkg=None, ver="", fp="")


class Driver(Chat):
    """A scripted 'model' that taps by label, reading refs off the live screen."""

    def __init__(self, plan):
        super().__init__(ModelConfig(provider="local", model="scripted"))
        self.plan = list(plan)

    def complete(self, messages, tools=None):
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 1
        if not self.plan:
            return Reply(content="The screen timeout is 10 minutes.")
        tool, arg = self.plan.pop(0)
        if tool == "tap":
            i = next(e.i for e in state.last["elements"] if e.text == arg)
            args = {"ref": state.ref(i)}
        elif tool == "launch_app":
            args = {"package": arg}
        elif tool == "text_input":
            args = {"text": arg}
        else:
            args = {}
        return Reply(content="", tool_calls=[{"id": str(len(self.plan)), "name": tool,
                                              "args": args}])


def record_run(plan, spec=None):
    ph = Phone()
    agent = Agent(Driver(plan), ph.registry(),
                  verify_spec=spec or {"checks": [{"answer": r"10\s*min"},
                                                  {"reached_text": "Screen timeout"}]})
    events = list(agent.run("What is the screen timeout?"))
    run_id = next(e for e in events if e["type"] == "start")["run_id"]
    return recorder.load(run_id)


ROUTE = [("launch_app", "settings"), ("ui_dump", ""), ("tap", "Display"),
         ("ui_dump", ""), ("tap", "Screen timeout"), ("ui_dump", "")]


# -- similarity -------------------------------------------------------------------

def test_similarity_is_mobileruns_rule():
    a = {"keys": ["a", "b", "c", "d"], "pkg": SET}
    assert replay.similarity(a, a) == 1.0
    assert replay.similarity(a, {"keys": ["a", "b", "c", "d"], "pkg": "x"}) == 0.85
    assert replay.similarity(a, {"keys": ["a", "b", "c"], "pkg": SET}) == pytest.approx(0.7875, abs=1e-3)
    assert replay.similarity(a, {"keys": ["a", "b", "c", "e"], "pkg": SET}) < replay.THRESHOLD


# -- making a macro -----------------------------------------------------------------

def test_an_unverified_run_is_refused():
    rows = record_run(ROUTE, spec={"checks": [{"answer": "never"}]})     # verdict: fail
    m = replay.make_macro(rows, "t")
    assert "not verified" in m["error"]
    assert "steps" in replay.make_macro(rows, "t", allow_unverified=True)


def test_a_verified_run_becomes_actions_with_selectors_and_pre_states():
    m = replay.make_macro(record_run(ROUTE), "timeout")
    assert m["verified"] == "verified by checks"
    assert [s["tool"] for s in m["steps"]] == ["launch_app", "tap", "tap"]   # reads dropped
    assert "pre" not in m["steps"][0]                                       # entry step
    assert m["steps"][1]["target"] == {"id": "title", "text": "Display"}
    assert "ref" not in m["steps"][1]["args"]                               # never a ref
    assert m["steps"][2]["pre"]["pkg"] == SET and "t:Screen timeout" in m["steps"][2]["pre"]["keys"]
    assert m["verify"]["checks"][0] == {"answer": r"10\s*min"}


def test_slots_turn_values_into_parameters():
    plan = [("launch_app", "settings"), ("ui_dump", ""), ("tap", "Search"),
            ("ui_dump", ""), ("text_input", "timeout")]
    rows = record_run(plan, spec={"checks": [{"reached_text": "Search settings"}]})
    m = replay.make_macro(rows, "search", slots={"q": "timeout"})
    assert m["slots"] == ["q"]
    assert m["steps"][-1]["args"] == {"text": "{q}"}


# -- replaying ----------------------------------------------------------------------

def test_a_macro_replays_the_route_without_a_model_and_is_verified():
    m = replay.make_macro(record_run(ROUTE), "timeout")
    ph = Phone()
    out = replay.replay(m, registry=ph.registry(), read=ph.read, match_wait_s=0.2)
    assert out["replayed"] == 3 and "handoff" not in out
    assert ph.calls == [("launch_app", "settings"), ("tap", "Display"),
                        ("tap", "Screen timeout")]
    assert ph.at == "timeout"
    assert out["verification"] == "pass"          # reached_text; the answer check is skipped
    fs = next(r for r in recorder.load(out["run_id"]) if r.get("kind") == "final_screen")
    assert "10 minutes" in fs["texts"]            # read AFTER the last step
    rows = recorder.load(out["run_id"])
    assert rows[0]["model"] == "replay" and rows[0]["macro"] == "timeout"


def test_a_screen_that_does_not_match_stops_replay_and_hands_off():
    m = replay.make_macro(record_run(ROUTE), "timeout")
    ph = Phone()
    reg = ph.registry()
    ph.MOVES = dict(Phone.MOVES)
    ph.MOVES[("settings", "Display")] = "sound"                          # the app changed
    handed = {}

    class Agentish:
        def run(self, goal):
            handed["goal"] = goal
            yield {"type": "final", "content": "took over", "steps": 1}

    out = replay.replay(m, registry=reg, read=ph.read, match_wait_s=0.2,
                        agent_factory=lambda: Agentish())
    assert out["replayed"] == 2 and out["handoff"]["at"] == 3
    assert "does not match" in out["handoff"]["why"]
    assert ("tap", "Screen timeout") not in ph.calls                    # never acted blind
    assert "stopped at step 3" in handed["goal"] and out["agent_final"]["content"] == "took over"


def test_a_missing_target_stops_replay():
    m = replay.make_macro(record_run(ROUTE), "timeout")
    m["steps"][1]["target"] = {"id": "title", "text": "Display settings"}
    ph = Phone()
    out = replay.replay(m, registry=ph.registry(), read=ph.read, match_wait_s=0.2)
    assert out["handoff"]["at"] == 2 and "not on screen" in out["handoff"]["why"]


def test_slot_values_are_required_and_filled():
    plan = [("launch_app", "settings"), ("ui_dump", ""), ("tap", "Search"),
            ("ui_dump", ""), ("text_input", "timeout")]
    rows = record_run(plan, spec={"checks": [{"reached_text": "Search settings"}]})
    m = replay.make_macro(rows, "search", slots={"q": "timeout"})
    ph = Phone()
    assert "missing q" in replay.replay(m, registry=ph.registry(), read=ph.read)["error"]
    out = replay.replay(m, values={"q": "brightness"}, registry=ph.registry(),
                        read=ph.read, match_wait_s=0.2)
    assert ("text_input", "brightness") in ph.calls and "handoff" not in out


def test_replay_allows_no_account_write_unless_named():
    m = replay.make_macro(record_run(ROUTE), "timeout")
    ph = Phone()
    replay.replay(m, registry=ph.registry(), read=ph.read, match_wait_s=0.2)
    assert wr.CONFIG.writes == set() and wr.CONFIG.run_id
    replay.replay(m, registry=Phone().registry(), read=Phone().read, match_wait_s=0.2,
                  writes=("follow",))
    assert wr.CONFIG.writes == {"follow"}


def test_macros_save_and_load_by_safe_name_only():
    m = replay.make_macro(record_run(ROUTE), "timeout")
    replay.save(m)
    assert replay.load("timeout")["steps"] == m["steps"]
    assert replay.load("../etc") is None
    with pytest.raises(ValueError):
        replay.save(dict(m, name="../x"))
    assert replay.list_macros()[0]["name"] == "timeout"
