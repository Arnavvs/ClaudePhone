"""App cards, the deep-link registry, and notes placed between turns (B6).

    python -m pytest tests/test_cards_links.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import cards, state  # noqa: E402
from claudephone import device as dev  # noqa: E402
from claudephone.harness.loop import Agent, Budget  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.harness.stagnation import Stagnation  # noqa: E402
from claudephone.tools import deeplink_tools as dl  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"
SAMSUNG = "RZ8N70HYQSB"
DC = os.path.normpath(os.path.join(ROOT, "..", "datacollect", "scripts"))


def el(rid="", text="", i=0):
    return Element(i=i, rid=rid, anchor=rid, text=text, desc="", cls="Button",
                   bounds=(0, 0, 10, 10), clickable=True)


# -- cards ------------------------------------------------------------------------

def test_cards_exist_for_the_apps_the_project_works_in():
    for pkg in (IG, "org.telegram.messenger", "com.twitter.android"):
        text = cards.load(pkg)
        assert text and text.startswith("APP CARD") and len(text) < 3000
    assert "Meta AI" in cards.load(IG)           # the IG 446 search trap
    assert cards.load("com.android.settings") is None


@pytest.mark.parametrize("bad", ["../secrets", "a/b", "..", "", "x\\y"])
def test_card_names_cannot_escape_the_folder(bad):
    assert cards.load(bad) is None


# -- the registry -----------------------------------------------------------------

def test_every_registry_entry_says_where_it_must_land_and_what_it_costs():
    for e in cards.links():
        assert e["prefix"] and e["package"] and e.get("sample")
        assert "verified" in e                    # null until seen on a device
        assert e.get("count") and e.get("platform")


def test_the_longest_matching_prefix_wins():
    reg = [{"prefix": "https://t.me/", "package": "a"},
           {"prefix": "https://t.me/s/", "package": "b"}]
    assert cards.match("https://t.me/s/chan", reg)["package"] == "b"
    assert cards.match("https://t.me/chan", reg)["package"] == "a"
    assert cards.match("https://example.com", reg) is None


# -- open_link --------------------------------------------------------------------

@pytest.fixture
def phone(monkeypatch, tmp_path):
    """A registry with one verified and one unverified link, a fake phone."""
    reg = {"links": [
        {"prefix": "instagram://user?username=", "package": IG, "count": "profile_open",
         "platform": "ig", "sample": "instagram", "verified": {"date": "2026-09-18"}},
        {"prefix": "twitter://user?screen_name=", "package": "com.twitter.android",
         "count": "x_profile_open", "platform": "x", "sample": "X", "verified": None}]}
    path = tmp_path / "deeplinks.json"
    path.write_text(json.dumps(reg))
    monkeypatch.setattr(cards, "LINKS_PATH", str(path))
    shell, fg = [], {"package": "com.sec.android.app.launcher"}

    def fake_shell(cmd, *a, **k):
        shell.append(cmd)
        if cmd.startswith("am start"):
            fg["package"] = fg.get("lands_in", IG)
        return ""

    monkeypatch.setattr(dev, "shell", fake_shell)
    monkeypatch.setattr(dev, "foreground", lambda *a, **k: dict(fg))
    monkeypatch.setattr(dev, "default_serial", lambda: SAMSUNG)

    from claudephone.runtime import observer as obs_mod
    from claudephone.runtime import screen as scr

    class FakeObserver:
        health_state = {"awake": True, "locked": False}

        def health(self):
            return dict(self.health_state)

        def wait_until_stable(self, quiet_s=1.0, timeout_s=4.0):
            return None, 0.1

    fo = FakeObserver()
    monkeypatch.setattr(obs_mod, "observer", lambda *a, **k: fo)
    monkeypatch.setattr(scr, "context", lambda **k: (
        state.remember([el("action_bar_title", text="instagram")], IG) and
        {"elements": state.last["elements"]}))
    return {"shell": shell, "fg": fg, "observer": fo}


def test_an_unregistered_link_is_refused(phone):
    r = dl.open_verified("https://example.com/x")
    assert "not in the deep-link registry" in r["error"] and not phone["shell"]


def test_an_unverified_link_is_refused(phone):
    r = dl.open_verified("twitter://user?screen_name=X")
    assert "not been verified" in r["error"] and not phone["shell"]


@pytest.mark.parametrize("url", ["instagram://user?username=a';reboot;'",
                                 "instagram://user?username=$(id)",
                                 "instagram://user?username=a b",
                                 "instagram://user?username="])
def test_shell_syntax_and_empty_targets_are_refused(phone, url):
    r = dl.open_verified(url)
    assert "refused" in r["error"] and not phone["shell"]


def test_a_locked_phone_is_refused_before_anything_opens(phone):
    phone["observer"].health_state = {"awake": True, "locked": True}
    r = dl.open_verified("instagram://user?username=creator_a")
    assert "locked" in r["error"] and not phone["shell"]


def test_a_link_that_lands_elsewhere_is_an_error_and_is_not_counted(phone, monkeypatch, tmp_path):
    if not os.path.isfile(os.path.join(DC, "ledger.py")):
        pytest.skip("datacollect not present")
    from claudephone.policy import writes as wr
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
    lg, store = wr._ledger_module()
    monkeypatch.setattr(store, "DB", str(tmp_path / "l.db"))
    phone["fg"]["lands_in"] = "com.android.chrome"
    r = dl.open_verified("instagram://user?username=creator_a", wait_s=0.6)
    assert r["landed"] is False and "did not land" in r["error"]
    n = store.connect().execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    assert n == 0


def test_a_landed_link_is_counted_once_and_returns_tappable_refs(phone, monkeypatch, tmp_path):
    if not os.path.isfile(os.path.join(DC, "ledger.py")):
        pytest.skip("datacollect not present")
    from claudephone.policy import writes as wr
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
    lg, store = wr._ledger_module()
    monkeypatch.setattr(store, "DB", str(tmp_path / "l.db"))
    r = dl.open_verified("instagram://user?username=creator_a")
    assert r["landed"] and r["package"] == IG
    assert r["elements"][0]["ref"] == state.ref(0) and r["ver"] == state.version()
    assert any("-p com.instagram.android" in c for c in phone["shell"])
    rows = store.connect().execute(
        "SELECT action, target FROM action_log").fetchall()
    assert [tuple(x) for x in rows] == [("profile_open", "instagram://user?username=creator_a")]


# -- notes and cards go in AFTER every tool result of a turn -----------------------

class FakeChat(Chat):
    def __init__(self, replies):
        super().__init__(ModelConfig(provider="local", model="fake"))
        self._replies = list(replies)

    def complete(self, messages, tools=None):
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 10
        return self._replies.pop(0) if self._replies else Reply(content="done")


def reg_with_screens():
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Go to an app.")
        def go(pkg: str = IG) -> dict:
            state.remember([el("x", text=pkg)], pkg)
            return {"at": pkg}

        @r.tool(description="Do nothing to the screen.")
        def poke(n: int = 0) -> dict:
            return {"poked": n}
    return r


def _assert_well_formed(messages):
    """Every assistant tool_call id is answered by a tool message before any
    other message - the OpenAI tool-calling contract."""
    for k, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            follow = messages[k + 1:k + 1 + len(ids)]
            assert [f.get("role") for f in follow] == ["tool"] * len(ids), follow
            assert [f.get("tool_call_id") for f in follow] == ids


def test_the_card_is_given_once_after_the_turns_tool_results():
    batch = Reply(content="", tool_calls=[
        {"id": "a", "name": "go", "args": {"pkg": IG}},
        {"id": "b", "name": "poke", "args": {"n": 1}}])
    again = Reply(content="", tool_calls=[{"id": "c", "name": "go", "args": {"pkg": IG}}])
    agent = Agent(FakeChat([batch, again, Reply(content="done")]), reg_with_screens())
    events = list(agent.run("look at instagram"))
    _assert_well_formed(agent.messages)
    cards_given = [m for m in agent.messages
                   if m.get("role") == "user" and "APP CARD" in str(m.get("content"))]
    assert len(cards_given) == 1                              # once per run
    assert sum(1 for e in events if e.get("reason") == "app_card") == 1


def test_a_stagnation_warning_mid_batch_no_longer_orphans_tool_calls():
    """The old code broke out of the batch on a warning, leaving later calls
    in the same turn with no result - a malformed next request."""
    state.remember([el("frozen", text="frozen")], "com.example")
    batch = Reply(content="", tool_calls=[
        {"id": "p%d" % i, "name": "poke", "args": {"n": 7}} for i in range(3)])
    agent = Agent(FakeChat([batch, Reply(content="ok")]), reg_with_screens(),
                  stagnation=Stagnation(warn_after=2, stop_after=0))
    list(agent.run("poke"))
    _assert_well_formed(agent.messages)
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 3                                 # all three answered
    assert any("POSSIBLY STUCK" in str(m.get("content")) for m in agent.messages
               if m.get("role") == "user")
