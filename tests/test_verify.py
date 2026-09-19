"""Decision logging and automatic run verification (B8).

    python -m pytest tests/test_verify.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import cli, state  # noqa: E402
from claudephone.harness import decisions, history, recorder, verify  # noqa: E402
from claudephone.harness.loop import Agent  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.ui import Element  # noqa: E402

SETTINGS = "com.android.settings"


def el(i, text="", clickable=True, bounds=None, rid="", hidden=False, scrollable=False):
    return Element(i=i, rid=rid, anchor=rid, text=text, desc="", cls="android.widget.TextView",
                   bounds=bounds or (0, i * 100, 1080, i * 100 + 90), clickable=clickable,
                   scrollable=scrollable, hidden=hidden)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder, "RUNS_DIR", str(tmp_path / "runs"))
    history.current.reset()
    state.last.update(elements=[], at=0.0, pkg=None, ver="", fp="")
    yield


# -- decision records ----------------------------------------------------------

def test_candidates_are_the_actionable_visible_elements():
    els = [el(0, "Settings", clickable=False), el(1, "Display"), el(2, "Hidden", hidden=True),
           el(3, "", scrollable=True, clickable=False, rid="recycler")]
    got = decisions.candidates(els, "7")
    assert [c["i"] for c in got] == [1, 3]
    assert got[0]["ref"] == "7_1" and got[0]["cls"] == "TextView"
    assert got[1]["scroll"] is True


def test_the_chosen_element_is_resolved_from_ref_index_or_point():
    state.remember([el(0, "Wide", bounds=(0, 0, 1080, 400)),
                    el(1, "Inner", bounds=(100, 100, 300, 200))], SETTINGS)
    ver = state.version()
    log = decisions.DecisionLog()
    b = log.before()
    assert log.record(1, "tap", {"ref": ver + "_1"}, {}, b, 1)["chosen"]["target_i"] == 1
    assert "target_i" not in log.record(2, "tap", {"ref": "zz_1"}, {}, b, 1)["chosen"]
    point = log.record(3, "tap", {"x": 150, "y": 150}, {}, b, 1)["chosen"]
    assert point["target_i"] == 1 and point["target"]["text"] == "Inner"   # smallest box


def test_a_step_on_the_same_screen_points_back_instead_of_repeating():
    state.remember([el(0, "Display"), el(1, "Sound")], SETTINGS)
    log = decisions.DecisionLog()
    first = log.record(1, "swipe", {}, {}, log.before(), 1)
    second = log.record(2, "swipe", {}, {}, log.before(), 1)
    assert len(first["candidates"]) == 2 and second["candidates_as_step"] == 1


def test_the_outcome_says_what_the_screen_did():
    state.remember([el(0, "Display")], SETTINGS)
    log = decisions.DecisionLog()
    before = log.before()
    state.last["at"] = before["at"] + 1
    state.remember([el(0, "Screen timeout"), el(1, "10 minutes", clickable=False)], SETTINGS)
    out = log.record(1, "tap", {"i": 0}, {}, before, 2)["outcome"]
    assert out["screen_read"] and out["changed"]
    assert set(out["appeared"]) == {"Screen timeout", "10 minutes"}
    err = log.record(2, "tap", {}, {"error": "VERSION_MISMATCH"}, log.before(), 3)["outcome"]
    assert err["error"] == "VERSION_MISMATCH" and err["screen_read"] is False


# -- checks --------------------------------------------------------------------

def rows_for(answer="The screen timeout is 10 minutes.", final_texts=("Screen timeout", "10 minutes"),
             final_pkg=SETTINGS, age=1.0, with_final_screen=True):
    rows = [{"kind": "meta", "goal": "What is the screen timeout?"},
            {"type": "tool_call", "step": 1, "tool": "find_element", "args": {"query": "Screen timeout"}},
            {"type": "tool_result", "step": 1, "tool": "find_element",
             "result": {"error": "no element matching 'Screen timeout'"}},
            {"type": "tool_call", "step": 2, "tool": "ui_dump", "args": {}},
            {"type": "tool_result", "step": 2, "tool": "ui_dump",
             "result": {"elements": [{"i": 0, "text": "Display"}, {"i": 1, "text": "Sound"}]}},
            {"type": "decision", "step": 3, "outcome": {"pkg": SETTINGS,
                                                        "appeared": ["Screen timeout"]}},
            {"type": "tool_result", "step": 4, "tool": "extract_fields",
             "result": {"fields": {"followers": None, "timeout": "10 minutes"}}},
            {"type": "final", "content": answer, "stopped_by": None if answer else "max_steps (10)"}]
    if with_final_screen:
        rows.append({"kind": "final_screen", "pkg": final_pkg, "age_s": age,
                     "texts": list(final_texts)})
    return rows


def spec(*checks):
    return {"checks": list(checks)}


def test_a_run_that_did_the_task_passes_every_check():
    out = verify.check_run(rows_for(), spec(
        {"answer": r"\b10\s*min"}, {"reached_text": "screen timeout"},
        {"final_text": "10 minutes"}, {"package": SETTINGS},
        {"reached_package": SETTINGS}, {"field": "timeout"},
        {"milestones": ["Display", "Screen timeout"]}))
    assert out["verdict"] == "pass", out
    reached = next(c for c in out["checks"] if "reached_text" in c["check"])
    assert reached["step"] == 3                  # from the screen, not step 1's error text


def test_a_tools_own_words_are_not_evidence_of_a_screen():
    rows = [r for r in rows_for() if r.get("type") != "decision" and r.get("kind") != "final_screen"]
    out = verify.check_run(rows, spec({"reached_text": "Screen timeout"}))
    assert out["verdict"] == "fail"


def test_no_answer_or_a_wrong_one_fails():
    assert verify.check_run(rows_for(answer=""), spec({"answer": "10"}))["verdict"] == "fail"
    assert verify.check_run(rows_for(answer="30 seconds"), spec({"answer": r"\b10\s*min"}))[
        "verdict"] == "fail"


def test_a_null_field_does_not_count():
    assert verify.check_run(rows_for(), spec({"field": "followers"}))["verdict"] == "fail"


def test_milestones_must_come_in_order():
    out = verify.check_run(rows_for(), spec({"milestones": ["Screen timeout", "Display"]}))
    assert out["verdict"] == "fail" and "next: Display" in out["checks"][0]["why"]


@pytest.mark.parametrize("kw,why", [({"with_final_screen": False}, "before B8"),
                                    ({"age": 500.0}, "before the end")])
def test_an_old_or_stale_final_screen_is_inconclusive_not_a_fail(kw, why):
    out = verify.check_run(rows_for(**kw), spec({"answer": "10"}, {"final_text": "10 minutes"}))
    assert out["verdict"] == "inconclusive" and why in out["checks"][1]["why"]


def test_any_failure_outranks_inconclusive():
    out = verify.check_run(rows_for(with_final_screen=False),
                           spec({"answer": "never"}, {"final_text": "x"}))
    assert out["verdict"] == "fail"


# -- the judge -----------------------------------------------------------------

class FakeChat(Chat):
    def __init__(self, replies, native=True):
        super().__init__(ModelConfig(provider="local", model="judge-fake"))
        self._replies = list(replies)
        self._native_ok = native
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 10
        r = self._replies.pop(0) if self._replies else "done"
        return r if isinstance(r, Reply) else Reply(content=r)


def test_the_judge_is_only_asked_when_the_checks_cannot_decide():
    j = FakeChat(['{"verdict": "pass", "why": "answer states 10 minutes"}'])
    decided = verify.verify(rows_for(), spec({"answer": "10"}), chat=j)
    assert decided["by"] == "checks" and j.calls == 0
    unsure = verify.verify(rows_for(with_final_screen=False),
                           spec({"final_text": "10 minutes"}), chat=j)
    assert unsure["verdict"] == "pass" and unsure["by"] == "judge" and j.calls == 1


def test_a_judge_that_hedges_or_rambles_leaves_it_inconclusive():
    for reply in ('{"verdict": "unsure", "why": "no screen"}', "I think it worked!"):
        out = verify.verify(rows_for(with_final_screen=False),
                            spec({"final_text": "x"}), chat=FakeChat([reply]))
        assert out["verdict"] == "inconclusive" and out["by"] == "checks"


# -- in a run ------------------------------------------------------------------

def registry():
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Read the screen.")
        def ui_dump() -> dict:
            state.remember([el(0, "Display"), el(1, "Screen timeout"),
                            el(2, "10 minutes", clickable=False)], SETTINGS)
            return {"elements": [{"i": e.i, "text": e.text} for e in state.last["elements"]]}

        @r.tool(description="Tap.")
        def tap(ref: str = "") -> dict:
            return {"tapped": ref}
    return r


def run_verified(answer, judge=False, judge_reply=None):
    replies = [Reply(content="", tool_calls=[{"id": "a", "name": "ui_dump", "args": {}}]),
               "TAP", Reply(content=answer)]
    chat = FakeChat([])
    seq = iter(replies)

    def complete(messages, tools=None):
        chat.calls += 1
        r = next(seq, Reply(content="?"))
        if r == "TAP":
            return Reply(content="", tool_calls=[{"id": "b", "name": "tap",
                                                  "args": {"ref": state.version() + "_1"}}])
        return r
    chat.complete = complete
    helper = FakeChat([judge_reply]) if judge_reply else None
    agent = Agent(chat, registry(), helper=helper, judge=judge,
                  verify_spec=spec({"answer": r"\b10\s*min"}, {"final_text": "10 minutes"},
                                   {"milestones": ["Display", "Screen timeout"]}))
    events = list(agent.run("What is the screen timeout?"))
    run_id = next(e for e in events if e["type"] == "start")["run_id"]
    return events, recorder.load(run_id), run_id


def test_a_run_is_checked_and_labelled_when_it_ends():
    events, rows, run_id = run_verified("It is 10 minutes.")
    v = next(e for e in events if e["type"] == "verification")
    assert v["verdict"] == "pass" and v["decided_by"] == "checks"
    lab = [r for r in rows if r.get("kind") == "label"]
    assert lab and lab[-1]["by"] == "verify" and lab[-1]["success"] is True
    assert recorder.summarise(run_id)["verdict"] == "pass"


def test_decisions_go_to_the_file_not_the_stream():
    events, rows, _ = run_verified("It is 10 minutes.")
    assert not any(e.get("type") == "decision" for e in events)
    dec = [r for r in rows if r.get("type") == "decision"]
    assert [d["step"] for d in dec] == [1, 2]
    assert dec[1]["chosen"]["target"]["text"] == "Screen timeout"
    assert {c["text"] for c in dec[1]["candidates"]} == {"Display", "Screen timeout"}
    assert any(r.get("kind") == "final_screen" for r in rows)


def test_a_wrong_answer_is_labelled_a_failure_and_no_judge_is_asked():
    events, rows, _ = run_verified("It is 30 seconds.", judge=True,
                                   judge_reply='{"verdict": "pass", "why": "x"}')
    v = next(e for e in events if e["type"] == "verification")
    assert v["verdict"] == "fail" and v["judge"] is None


def test_the_cli_verifies_a_recorded_run_and_labels_it(capsys):
    _, _, run_id = run_verified("It is 10 minutes.")
    rc = cli.main(["verify", run_id, "--expect", "30 seconds"])
    assert rc == 1 and "FAIL" in capsys.readouterr().out
    labels = [r for r in recorder.load(run_id) if r.get("kind") == "label"]
    assert [lab["verdict"] for lab in labels] == ["pass", "fail"]    # appended, not replaced
    assert cli.main(["verify", "latest", "--reached", "screen timeout", "--no-label"]) == 0
    assert len([r for r in recorder.load(run_id) if r.get("kind") == "label"]) == 2
