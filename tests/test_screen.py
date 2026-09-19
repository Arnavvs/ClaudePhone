"""The shared screen read (2e), without a phone.

What matters here is that the bridge path hands the tools the same SHAPE the u2
path did: the app's own elements, denoised, plus `live_ids` covering the raw
hierarchy - which for the bridge means every window, not just the active one.

    python -m pytest tests/test_screen.py
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import device as dev  # noqa: E402
from claudephone import ui as uix  # noqa: E402
from claudephone.runtime import bridge as br  # noqa: E402
from claudephone.runtime import screen as scr  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"


def el(rid="", text="", desc="", cls="Button", window="", clickable=False):
    return Element(i=0, rid=rid, anchor=rid, text=text, desc=desc, cls=cls,
                   bounds=(0, 0, 10, 10), clickable=clickable, window=window)


APP = [
    el("clips_author_username", text="creator"),          # value
    el("like_button", desc="Like", clickable=True),        # interactive
    el("clips_caption_component", cls="ViewGroup"),        # container WITH an id
    el("", cls="FrameLayout"),                             # pure noise
    el("profile_header_container", cls="LinearLayout"),    # container WITH an id
]
BARS = [el("clock", text="19:51", window="system"),
        el("navigation_bar_frame", cls="FrameLayout", window="system")]

XML = ('<hierarchy><node resource-id="com.instagram.android:id/clips_author_username" '
       'class="android.widget.TextView" text="creator" bounds="[0,0][10,10]"/>'
       '<node resource-id="com.instagram.android:id/clips_caption_component" '
       'class="android.view.ViewGroup" text="" bounds="[0,0][10,10]"/></hierarchy>')


@pytest.fixture
def on_bridge(monkeypatch):
    class FakeBridge:
        def tree(self, limit=300, all_windows=False):
            els = APP + (BARS if all_windows else [])
            return {"elements": els, "package": IG, "foreground": IG,
                    "obstructions": [], "windows": [], "ms": 9}
    monkeypatch.setattr(br, "available", lambda serial="", recheck=False: True)
    monkeypatch.setattr(br, "bridge", lambda serial="": FakeBridge())
    monkeypatch.setattr(dev, "foreground", lambda serial="": {"package": IG, "activity": ".Main"})
    monkeypatch.setattr(dev, "app_version", lambda pkg: "446.0.0")
    return FakeBridge()


@pytest.fixture
def on_u2(monkeypatch):
    class FakeU2:
        def dump_hierarchy(self):
            return XML
    monkeypatch.setattr(br, "available", lambda serial="", recheck=False: False)
    monkeypatch.setattr(dev, "u2", lambda serial="": FakeU2())
    monkeypatch.setattr(dev, "foreground", lambda serial="": {"package": IG, "activity": ".Main"})
    monkeypatch.setattr(dev, "app_version", lambda pkg: "446.0.0")


def test_bridge_context_returns_the_apps_own_elements_denoised(on_bridge):
    c = scr.context()
    rids = [e.rid for e in c["elements"]]
    assert c["backend"] == "bridge"
    assert "clips_author_username" in rids and "like_button" in rids
    assert "clips_caption_component" not in rids      # id, but a layout container
    assert "" not in rids                             # anonymous FrameLayout dropped
    assert all(not e.window for e in c["elements"])   # no status/nav bar rows


def test_live_ids_cover_every_window_not_just_the_active_one(on_bridge):
    c = scr.context()
    assert "clock" in c["live_ids"] and "navigation_bar_frame" in c["live_ids"]
    # and the containers the element list dropped are still there for drift
    assert {"clips_caption_component", "profile_header_container"} <= c["live_ids"]


def test_keep_noise_is_include_system_so_it_returns_every_window(on_bridge):
    c = scr.context(keep_noise=True)
    rids = [e.rid for e in c["elements"]]
    assert "clips_caption_component" in rids            # containers kept
    assert "clock" in rids and "navigation_bar_frame" in rids   # and the bars


def test_u2_fallback_still_works_and_says_so(on_u2):
    c = scr.context()
    assert c["backend"] == "u2" and c["xml"] == XML
    assert c["package"] == IG and c["app_version"] == "446.0.0"
    assert "clips_caption_component" in c["live_ids"]        # from the raw hierarchy
    assert [e.rid for e in c["elements"]] == ["clips_author_username"]


def test_both_backends_agree_on_the_ids_a_drift_check_uses(on_bridge, monkeypatch):
    bridge_ids = scr.context()["live_ids"]
    monkeypatch.setattr(br, "available", lambda serial="", recheck=False: False)
    monkeypatch.setattr(dev, "u2", lambda serial="": type("U", (), {"dump_hierarchy": lambda s: XML})())
    u2_ids = scr.context()["live_ids"]
    assert u2_ids <= bridge_ids          # the bridge is a superset, never short


def test_context_caches_the_screen_so_refs_resolve(on_bridge):
    from claudephone import state
    state.remember([el("something_else", text="old")], IG)
    before = state.version()
    c = scr.context()
    assert state.last["elements"] == c["elements"]      # tap(ref) resolves against this
    assert state.version() != before                    # the screen changed, so a new ver
    ver = state.version()
    again = scr.context()                               # same screen -> same ver
    assert state.version() == ver and again["elements"] == c["elements"]


def test_remember_false_leaves_the_agents_refs_alone(on_bridge):
    from claudephone import state
    state.remember([el("pinned", text="keep")], IG)
    ver, last = state.version(), state.last["elements"]
    scr.context(remember=False)
    assert state.version() == ver and state.last["elements"] == last


def test_denoise_keeps_anything_with_a_value_or_a_touch_target():
    keep = scr.denoise([el("", text="hello", cls="FrameLayout"),
                        el("", cls="ViewGroup", clickable=True),
                        el("recycler_view", cls="RecyclerView")])
    assert len(keep) == 3
    assert scr.denoise([el("", cls="View"), el("box", cls="FrameLayout")]) == []


def test_parse_and_bridge_shape_the_same_element_type(on_bridge, on_u2):
    assert isinstance(uix.parse(XML)[0], Element)
    assert isinstance(scr.context()["elements"][0], Element)


def test_a_filtered_read_is_renumbered_so_its_refs_resolve(on_bridge):
    """Found live: ui_dump showed i=55 on a 34-element screen, because the
    denoised list kept indices from the full tree; tap(ref) resolves by position."""
    from claudephone import state
    from claudephone.runtime import targeting as tg
    for n, e in enumerate(APP + BARS):
        e.i = 50 + n                                 # indices from a bigger tree
    c = scr.context()
    assert [e.i for e in c["elements"]] == list(range(len(c["elements"])))
    last = c["elements"][-1]
    found, err = tg.from_cache(last.i, state.ref(last.i))
    assert err is None and found is last
