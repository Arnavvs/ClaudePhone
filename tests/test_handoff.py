"""Human handoff and the checkpoint guard (B3), without a phone or a model.

The one that matters most is the guard: doctrine says a phone meeting a login,
2FA or "unusual activity" screen stops that account, and that must not depend on
the model deciding to comply.

    python -m pytest tests/test_handoff.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import state  # noqa: E402
from claudephone.harness import handoff  # noqa: E402
from claudephone.harness.loop import Agent, Budget  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.tools import handoff_tools  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"


def el(rid="", text="", desc="", cls="TextView"):
    return Element(i=0, rid=rid, anchor=rid, text=text, desc=desc, cls=cls,
                   bounds=(0, 0, 10, 10), clickable=False)


@pytest.fixture(autouse=True)
def fresh():
    handoff.configure(ask=None)
    state.remember([el("feed", text="ordinary screen")], IG)
    yield
    handoff.configure(ask=None)


# -- the guard -------------------------------------------------------------------

@pytest.mark.parametrize("screen,expected", [
    ([el(text="Suspicious login attempt")], True),
    ([el(text="We detected unusual activity on your account")], True),
    ([el(desc="Confirm it's you")], True),
    ([el(text="Enter the 6-digit code we sent")], True),
    ([el(text="Two-factor authentication")], True),
    ([el(text="Action blocked")], True),
    ([el(text="Try again later")], True),
    ([el(text="Your account has been disabled")], True),
    ([el(text="Security check")], True),
    ([el("password_edit", cls="EditText")], True),
    ([el(text="Log in"), el(text="Sign up")], False),       # ordinary, not a challenge
    ([el(text="Followers"), el(text="Following")], False),
    ([el(text="Activity"), el(text="Your activity")], False),
])
def test_checkpoint_detection(screen, expected):
    assert (handoff.checkpoint_on_screen(screen, IG) is not None) == expected


def test_the_guard_can_be_turned_off_but_is_on_by_default():
    assert handoff.CONFIG.checkpoint_guard is True
    handoff.configure(ask=None, checkpoint_guard=False)
    assert handoff.checkpoint_on_screen([el(text="Suspicious login attempt")], IG) is None


# -- the tools -------------------------------------------------------------------

def registry():
    reg = ToolRegistry()
    with reg.pack("core"):
        handoff_tools.register(reg)

        @reg.tool(description="Look at a screen.")
        def look(screen: str = "ok") -> dict:
            if screen == "checkpoint":
                state.remember([el(text="We detected unusual activity")], IG)
            else:
                state.remember([el("feed", text=screen)], IG)
            return {"screen": screen}
    return reg


def test_request_human_marks_the_result_for_the_loop():
    r = registry().call("request_human", {"reason": "2FA prompt"})
    assert r["_handoff"]["kind"] == "stop" and r["_handoff"]["reason"] == "2FA prompt"
    assert "error" in registry().call("request_human", {"reason": "  "})


def test_ask_operator_without_a_channel_stops_rather_than_guessing():
    r = registry().call("ask_operator", {"question": "which account?"})
    assert r["_handoff"]["kind"] == "stop" and r["_handoff"]["why"] == "no_channel"


def test_ask_operator_returns_the_answer_when_someone_replies():
    handoff.configure(ask=lambda q, t: "use @aisha_xmehra")
    r = registry().call("ask_operator", {"question": "which account?"})
    assert r["answer"] == "use @aisha_xmehra" and r["_handoff"]["kind"] == "answered"


def test_an_unanswered_question_stops_the_run():
    handoff.configure(ask=lambda q, t: None)
    r = registry().call("ask_operator", {"question": "which account?"})
    assert r["_handoff"]["kind"] == "stop" and r["_handoff"]["why"] == "no_answer"


# -- through the agent loop ------------------------------------------------------

class FakeChat(Chat):
    def __init__(self, replies):
        super().__init__(ModelConfig(provider="local", model="fake"))
        self._replies = list(replies)

    def complete(self, messages, tools=None):
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 10
        return self._replies.pop(0) if self._replies else Reply(content="done")


def call(name, args, i=1):
    return {"id": "c%d" % i, "name": name, "args": args}


def test_the_run_ends_when_the_agent_asks_for_a_human():
    chat = FakeChat([Reply(content="", tool_calls=[
        call("request_human", {"reason": "checkpoint on the login screen"})])])
    final = [e for e in Agent(chat, registry()).run("do a thing")
             if e["type"] == "final"][-1]
    assert final["stopped_by"] == "human_required"
    assert "checkpoint" in final["content"]


def test_a_checkpoint_screen_stops_the_run_even_if_the_model_ignores_it():
    """The model here never calls request_human - it just keeps looking."""
    chat = FakeChat([Reply(content="", tool_calls=[call("look", {"screen": "checkpoint"})]),
                     Reply(content="", tool_calls=[call("look", {"screen": "ok"}, 2)])])
    events = list(Agent(chat, registry(), budget=Budget(max_steps=10)).run("scrape"))
    final = [e for e in events if e["type"] == "final"][-1]
    assert final["stopped_by"] == "human_required"
    assert final["steps"] == 1                       # stopped on the first sight of it
    assert final["handoff"]["why"] == "unusual activity notice"
    assert any(e.get("reason") == "checkpoint" for e in events if e["type"] == "note")


def test_an_answered_question_lets_the_run_continue():
    answers = []
    chat = FakeChat([
        Reply(content="", tool_calls=[call("ask_operator", {"question": "which?"})]),
        Reply(content="thanks, done"),
    ])
    agent = Agent(chat, registry(), on_ask_operator=lambda q, t: answers.append(q) or "@a")
    events = list(agent.run("pick an account"))
    final = [e for e in events if e["type"] == "final"][-1]
    assert answers == ["which?"]
    assert final.get("stopped_by") in (None, "")     # ran to a normal finish
    assert any(e.get("reason") == "operator" for e in events if e["type"] == "note")


# -- the server's side of the conversation ---------------------------------------

def test_the_real_reply_endpoint_releases_a_waiting_task():
    """Drives the actual server: a slot waits, POST /reply frees it."""
    from http.server import ThreadingHTTPServer

    from claudephone import server

    server.Handler.require_auth = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    slot = {"question": "which account?", "answer": None, "event": threading.Event()}
    got = {}

    def waiting_task():
        server.PENDING["sid123"] = slot
        got["released"] = slot["event"].wait(timeout=6)
        got["answer"] = slot["answer"]
        server.PENDING.pop("sid123", None)

    t = threading.Thread(target=waiting_task, daemon=True)
    t.start()
    slot["event"].wait(0.05)

    def post(path, body):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    try:
        code, body = post("/reply", {"session_id": "nope", "answer": "x"})
        assert code == 404 and "waiting" in body

        code, body = post("/reply", {"session_id": "sid123",
                                     "answer": "@aisha_xmehra"})
        assert code == 200 and body["question"] == "which account?"
        t.join(timeout=6)
        assert got["released"] is True and got["answer"] == "@aisha_xmehra"
    finally:
        httpd.shutdown()
        server.PENDING.pop("sid123", None)
