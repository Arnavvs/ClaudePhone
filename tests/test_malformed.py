"""Malformed tool-call recovery (B11).

    python -m pytest tests/test_malformed.py
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import state  # noqa: E402
from claudephone.harness import history, recorder  # noqa: E402
from claudephone.harness.loop import MAX_MALFORMED, Agent  # noqa: E402
from claudephone.harness.models import (Chat, ModelConfig, Reply,  # noqa: E402
                                        diagnose_attempt, parse_json_tool_call)
from claudephone.harness.registry import ToolRegistry  # noqa: E402

GOOD = '```json\n{"tool": "poke", "args": {"n": 1}}\n```'
BROKEN = {
    "single quotes": "```json\n{'tool': 'poke', 'args': {'n': 1}}\n```",
    "trailing comma": '```json\n{"tool": "poke", "args": {"n": 1},}\n```',
    "unbalanced": '```json\n{"tool": "poke", "args": {"n": 1}\n```',
    "no tool name": '```json\n{"function": "poke", "parameters": {"n": 1}}\n```',
    "bare tag": "<tool_call>poke n=1</tool_call>",
}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder, "RUNS_DIR", str(tmp_path / "runs"))
    history.current.reset()
    state.last.update(elements=[], at=0.0, pkg=None, ver="", fp="")


# -- diagnosis ---------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(BROKEN))
def test_broken_calls_are_recognised_as_attempts(kind):
    prose, calls = parse_json_tool_call(BROKEN[kind])
    assert calls == []
    assert diagnose_attempt(BROKEN[kind])


def test_the_reason_is_specific_enough_to_act_on():
    assert "double quotes" in diagnose_attempt(BROKEN["single quotes"])
    assert "unbalanced" in diagnose_attempt(BROKEN["unbalanced"])
    assert '"tool" name' in diagnose_attempt(BROKEN["no tool name"])


@pytest.mark.parametrize("answer", [
    "The screen timeout is 10 minutes.",
    'The profile shows {"followers": 1204} in its header data.',
    "Name: Arnav. Arguments aside, the bio says Delhi.",
    "Done - the result was {ok}.",
])
def test_answers_are_not_mistaken_for_broken_calls(answer):
    assert parse_json_tool_call(answer)[1] == []
    assert diagnose_attempt(answer) == ""


# -- the loop ----------------------------------------------------------------

class Scripted(Chat):
    def __init__(self, texts, native=False):
        super().__init__(ModelConfig(provider="local", model="scripted"))
        self._native_ok = native
        self._texts = list(texts)
        self.seen: list = []

    def complete(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 10
        item = self._texts.pop(0) if self._texts else "finished"
        if isinstance(item, Reply):
            return item
        prose, calls = parse_json_tool_call(item)
        return Reply(content=prose, tool_calls=calls,
                     malformed="" if calls else diagnose_attempt(item))


def registry(log):
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Poke.")
        def poke(n: int = 0) -> dict:
            log.append(n)
            return {"poked": n}
    return r


def test_a_broken_call_is_corrected_without_echo_and_the_run_carries_on():
    log = []
    chat = Scripted([BROKEN["single quotes"], GOOD, "The answer is 1."])
    agent = Agent(chat, registry(log))
    events = list(agent.run("poke once"))
    final = events[-1] if events[-1]["type"] == "final" else \
        next(e for e in reversed(events) if e["type"] == "final")
    assert final["content"] == "The answer is 1." and log == [1]
    notes = [e for e in events if e.get("reason") == "malformed_call"]
    assert len(notes) == 1 and "double quotes" in notes[0]["message"]
    # the second request carries the correction, and not the broken text
    second = chat.seen[1]
    assert "could not be read" in second[-1]["content"]
    assert not any("'tool'" in str(m.get("content")) for m in second)


def test_three_in_a_row_stop_the_run():
    chat = Scripted([BROKEN["unbalanced"]] * 5)
    events = list(Agent(chat, registry([])).run("poke"))
    final = next(e for e in events if e["type"] == "final")
    assert final["stopped_by"].startswith("malformed_calls")
    assert len(chat.seen) == MAX_MALFORMED


def test_the_count_resets_after_a_good_call():
    log = []
    chat = Scripted([BROKEN["unbalanced"], BROKEN["unbalanced"], GOOD,
                     BROKEN["trailing comma"], BROKEN["trailing comma"], GOOD, "done"])
    events = list(Agent(chat, registry(log)).run("poke twice"))
    assert log == [1, 1]
    assert next(e for e in events if e["type"] == "final").get("stopped_by") is None


def test_native_arguments_that_are_not_json_never_reach_the_tool():
    log = []
    bad = Reply(content="", tool_calls=[{"id": "a", "name": "poke",
                                         "args": {"_unparsed": "{n: 1"}}])
    chat = Scripted([bad, "gave up"], native=True)
    events = list(Agent(chat, registry(log)).run("poke"))
    res = next(e for e in events if e["type"] == "tool_result")
    assert log == [] and "not valid JSON" in res["result"]["error"]
