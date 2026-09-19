"""scroll_to only calls a match found when it can be tapped (found live in B9).

    python -m pytest tests/test_scroll_to.py
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone.harness.registry import ToolRegistry  # noqa: E402
from claudephone.policy import reads  # noqa: E402
from claudephone.runtime.observer import Observation  # noqa: E402
from claudephone.tools import compound_tools as ct  # noqa: E402
from claudephone.ui import Element  # noqa: E402


def row(i, text, y, hidden=False):
    return Element(i=i, rid="title", anchor="title", text=text, desc="", cls="TextView",
                   bounds=(0, y - 20, 1080, y + 20), hidden=hidden)


class FakeObserver:
    serial = "SER"
    _size = (1080, 2340)

    def __init__(self, screens):
        self.screens = list(screens)
        self.last = None
        self.reads = 0

    def look(self, remember=True):
        self.reads += 1
        els = self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)
        self.last = Observation(elements=els, package="com.android.settings")
        return self.last


@pytest.fixture
def world(monkeypatch):
    swipes = []

    class Act:
        def swipe(self, x1, y1, x2, y2, ms=250):
            swipes.append((y1, y2, ms))

    def make(screens, reel=False):
        o = FakeObserver(screens)
        monkeypatch.setattr(ct, "observer", lambda *a, **k: o)
        monkeypatch.setattr(ct, "_act", lambda obs: ("bridge", Act()))
        monkeypatch.setattr(reads, "reel_advance_action",
                            lambda els, pkg: "feed_reel" if reel else "")
        r = ToolRegistry()
        ct.register(r)
        return r, swipes
    return make


def test_a_match_hidden_under_the_toolbar_is_nudged_into_view(world):
    hidden = [row(0, "Screen timeout", 290, hidden=True), row(1, "10 minutes", 316)]
    shown = [row(0, "Screen timeout", 520), row(1, "10 minutes", 546)]
    reg, swipes = world([hidden, shown])
    r = reg.call("scroll_to", {"query": "screen timeout", "settle_s": 0})
    assert r["found"] is True and "visible" not in r
    y1, y2, ms = swipes[0]
    assert y2 > y1 and ms >= 400                 # dragged DOWN, slowly: toward the top
    assert r["elements"][0]["text"] == "Screen timeout"


def test_a_match_that_stays_hidden_is_reported_as_not_visible(world):
    hidden = [row(0, "Screen timeout", 290, hidden=True)]
    reg, swipes = world([hidden])
    r = reg.call("scroll_to", {"query": "screen timeout", "settle_s": 0})
    assert r["found"] is True and r["visible"] is False and len(swipes) == 2


def test_no_nudging_on_a_reel_viewer(world):
    hidden = [row(0, "caption", 290, hidden=True)]
    reg, swipes = world([hidden], reel=True)
    r = reg.call("scroll_to", {"query": "caption", "settle_s": 0})
    assert r["visible"] is False and swipes == []


def test_a_visible_match_returns_at_once(world):
    reg, swipes = world([[row(0, "Display", 700)]])
    r = reg.call("scroll_to", {"query": "display", "settle_s": 0})
    assert r["found"] and r["after_swipes"] == 0 and swipes == []
