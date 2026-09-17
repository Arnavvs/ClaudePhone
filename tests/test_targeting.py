"""Pre-tap verification (B1) without a phone.

Covers the screen version cache in state.py and runtime/targeting.py: refs,
settle, re-finding an element, and every refusal reason. The last tests replay
the measured Samsung failure - Settings' search icon read mid-animation at
y=659 and settled at y=209.

    python -m pytest tests/test_targeting.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from claudephone import state  # noqa: E402
from claudephone.runtime import targeting as tg  # noqa: E402
from claudephone.ui import Element  # noqa: E402


def el(i, rid="", text="", desc="", bounds=(0, 0, 100, 100), clickable=True,
       cls="Button", anchor="", hidden=False, window=""):
    return Element(i=i, rid=rid, anchor=anchor or rid, text=text, desc=desc,
                   cls=cls, bounds=bounds, clickable=clickable, hidden=hidden,
                   window=window)


def screen(*elements, obstructions=()):
    return {"elements": list(elements), "obstructions": list(obstructions),
            "backend": "fake"}


# -- versions and refs ---------------------------------------------------------

def test_version_is_stable_for_the_same_screen_and_bumps_when_it_moves():
    a = [el(0, "search", desc="Search", bounds=(900, 100, 1000, 200))]
    v1 = state.remember(a, "com.x")
    v2 = state.remember([el(0, "search", desc="Search", bounds=(900, 100, 1000, 200))])
    assert v1 == v2, "re-reading an identical screen must not invalidate refs"
    v3 = state.remember([el(0, "search", desc="Search", bounds=(900, 140, 1000, 240))])
    assert v3 != v2, "a 40 px move is a different screen for tapping purposes"


def test_parse_ref():
    assert state.parse_ref("1a_12") == ("1a", 12)
    assert state.parse_ref("12") is None
    assert state.parse_ref("") is None


def test_from_cache_refuses_an_old_version():
    state.remember([el(0, "a", text="A")])
    old = state.ref(0)
    state.remember([el(0, "b", text="B")])
    got, err = tg.from_cache(ref=old)
    assert got is None and err["error"] == "VERSION_MISMATCH"
    got, err = tg.from_cache(ref=state.ref(0))
    assert err is None and got.rid == "b"


def test_from_cache_bad_ref_and_range():
    state.remember([el(0, "a", text="A")])
    assert "bad ref" in tg.from_cache(ref="nope")[1]["error"]
    assert "out of range" in tg.from_cache(i=5)[1]["error"]


# -- classify ------------------------------------------------------------------

def test_same_within_tolerance_taps_the_fresh_centre():
    t = el(0, "follow", text="Follow", bounds=(100, 100, 300, 200))
    r = tg.classify(t, [el(3, "follow", text="Follow", bounds=(104, 100, 304, 200))])
    assert r["status"] == "same" and r["tap"] == [204, 150]


def test_shifted_follows_a_unique_element():
    t = el(0, "search", desc="Search settings", bounds=(943, 575, 1080, 743))
    r = tg.classify(t, [el(9, "search", desc="Search settings",
                           bounds=(943, 125, 1080, 293))])
    assert r["status"] == "shifted"
    assert r["tap"] == [1011, 209] and r["moved_from"] == [1011, 659]


def test_ambiguous_when_several_matches_and_none_where_it_was():
    t = el(0, "follow", text="Follow", bounds=(800, 500, 1000, 580))
    fresh = [el(1, "follow", text="Follow", bounds=(800, 900, 1000, 980)),
             el(2, "follow", text="Follow", bounds=(800, 1300, 1000, 1380))]
    r = tg.classify(t, fresh)
    assert r["status"] == "ambiguous" and "tap" not in r


def test_several_matches_but_one_still_in_place_is_same():
    t = el(0, "follow", text="Follow", bounds=(800, 500, 1000, 580))
    fresh = [el(1, "follow", text="Follow", bounds=(800, 500, 1000, 580)),
             el(2, "follow", text="Follow", bounds=(800, 1300, 1000, 1380))]
    r = tg.classify(t, fresh)
    assert r["status"] == "same" and r["tap"] == [900, 540]


def test_hidden_is_refused():
    t = el(0, "row", text="Wi-Fi", bounds=(0, 2300, 1080, 2400))
    r = tg.classify(t, [el(4, "row", text="Wi-Fi", bounds=(0, 2300, 1080, 2400),
                           hidden=True)])
    assert r["status"] == "hidden" and "tap" not in r


def test_disappeared_and_occupied():
    t = el(0, "not_interested", text="Not interested", bounds=(0, 1000, 1080, 1100))
    assert tg.classify(t, [])["status"] == "disappeared"
    other = el(7, "follow", text="Follow", bounds=(0, 980, 1080, 1120))
    r = tg.classify(t, [other])
    assert r["status"] == "occupied"
    assert r["now_at_point"]["id"] == "follow"


def test_obstructed_by_a_keyboard():
    t = el(0, "send", desc="Send", bounds=(950, 2100, 1060, 2200))
    kb = {"type": "input_method", "pkg": "com.google.android.inputmethod.latin",
          "b": [0, 1500, 1080, 2340]}
    r = tg.classify(t, [el(2, "send", desc="Send", bounds=(950, 2100, 1060, 2200))],
                    obstructions=[kb])
    assert r["status"] == "obstructed" and "tap" not in r
    assert r["obstructed_by"]["type"] == "input_method"


def test_element_inside_the_obstructing_window_is_not_obstructed():
    t = el(0, "key_a", text="a", bounds=(0, 1800, 100, 1900), window="input_method")
    kb = {"type": "input_method", "b": [0, 1500, 1080, 2340]}
    r = tg.classify(t, [el(1, "key_a", text="a", bounds=(0, 1800, 100, 1900),
                           window="input_method")], obstructions=[kb])
    assert r["status"] == "same"


def test_anonymous_target_is_only_trusted_in_place():
    t = el(0, bounds=(0, 0, 200, 200), cls="FrameLayout")
    assert tg.classify(t, [el(5, bounds=(0, 0, 200, 200), cls="FrameLayout")])[
        "status"] == "same"
    # Moved: with no id and no label, "a FrameLayout somewhere" proves nothing.
    assert tg.classify(t, [el(5, bounds=(0, 900, 200, 1100), cls="FrameLayout",
                              clickable=False)])["status"] == "disappeared"


# -- settle: the measured Samsung failure -----------------------------------------

def _replay(positions):
    """A fake reader returning the search icon at successive y positions."""
    seq = iter(positions)

    def reader(_serial=""):
        try:
            top = next(seq)
        except StopIteration:
            top = positions[-1]
        return screen(el(0, "", desc="Search settings", cls="ImageButton",
                         bounds=(943, top, 1080, top + 168)))
    return reader


def test_settle_waits_out_the_collapsing_header():
    target = el(0, "", desc="Search settings", cls="ImageButton",
                bounds=(943, 575, 1080, 743))          # read at the first change event
    reader = _replay([410, 150, 125, 125])
    res = tg.check_target(target, reader=reader)
    assert res["settled"] is True
    assert res["status"] == "shifted" and res["tap"] == [1011, 209]
    assert res["reads"] == 4


def test_settle_gives_up_but_still_classifies():
    target = el(0, "", desc="Search settings", cls="ImageButton",
                bounds=(943, 575, 1080, 743))
    reader = _replay(list(range(600, 0, -10)))     # never stops moving
    fresh, settled, reads = tg.settled_read(tg.identity(target), reader=reader,
                                            max_s=0.3, interval_s=0.05)
    assert settled is False and reads >= 2


def test_check_point_reports_hit_and_obstruction():
    def reader(_s=""):
        return screen(el(0, "ok", text="OK", bounds=(400, 1000, 700, 1100)),
                      obstructions=[{"type": "system", "pkg": "com.android.systemui",
                                     "b": [888, 533, 1080, 1198]}])
    assert tg.check_point(500, 1050, reader=reader)["hits"]["id"] == "ok"
    assert tg.check_point(900, 600, reader=reader)["obstructed_by"]["type"] == "system"


def test_fast_path_single_read_when_nothing_moved():
    target = el(0, "follow", text="Follow", bounds=(100, 100, 300, 200))
    calls = []

    def reader(_s=""):
        calls.append(1)
        return screen(el(4, "follow", text="Follow", bounds=(100, 100, 300, 200)))
    res = tg.check_target(target, reader=reader)
    assert res["status"] == "same" and res["reads"] == 1 and len(calls) == 1
