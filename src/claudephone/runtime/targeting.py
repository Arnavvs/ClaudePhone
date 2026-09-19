"""Pre-tap verification: never tap an element from a screen that has moved.

`tap(i)` used to tap the centre of element i from the last cached read, and
only warned once that read was 20 s old. Three things make that unsafe on the
apps this project drives, all measured:

* Instagram's overlay lags a swipe, sometimes by a whole reel, and roughly one
  swipe in 14 does not advance the viewer at all.
* The FIRST change event after a swipe arrives mid-animation. On the Samsung,
  Settings' search icon read at y=659 / 410 / 471 at that moment and settled at
  y=209; a tap on the in-flight position hit nothing (2026-09-17).
* A keyboard or system window can sit over the target.

A stale index next to Follow or Not interested is an unplanned write on a
rate-limited account. So before any tap on an element, this module:

1. reads the screen again - ~10 ms through the bridge, a u2 dump otherwise -
   WITHOUT touching the agent's cached screen version;
2. keeps reading until the target's position stops changing (settle);
3. finds the target again by identity (resource-id, anchor, label, class,
   window), not by index;
4. classifies what happened, the way ARTEMIS's pre-execution safety net does:

    same         found where it was                      -> tap
    shifted      found once, moved                       -> tap the NEW centre
    ambiguous    several matches, none where it was      -> refuse
    hidden       found, but reported not visible to user -> refuse
    obstructed   a keyboard / system window covers it    -> refuse
    occupied     gone, something else is at that point  -> refuse, say what
    disappeared  gone                                    -> refuse

Refusing is cheap - one more model turn. Tapping the wrong thing is not.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import device as dev
from .. import ui as uix

MOVE_TOLERANCE_PX = 12
SETTLE_INTERVAL_S = 0.12
SETTLE_MAX_S = 1.5
PROCEED = ("same", "shifted")


def _label(e) -> str:
    return ((e.text or e.desc or "").strip().lower())[:40]


def identity(e) -> tuple:
    """What makes an element the SAME element on a later read."""
    return (e.rid or "", e.anchor or "", _label(e), e.cls or "",
            getattr(e, "window", "") or "")


def _strong(e) -> bool:
    """An identity worth trusting on its own: has an id or a label."""
    return bool(e.rid or _label(e))


def _center_dist(a: tuple, b: tuple) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _contains(bounds, x: int, y: int) -> bool:
    l, t, r, b = bounds
    return l <= x < r and t <= y < b


def read_screen(serial: str = "") -> dict:
    """A fresh read that does NOT update state (the agent's refs stay valid)."""
    from . import bridge as br
    t0 = time.time()
    if br.available(serial):
        r = br.bridge(serial).tree(limit=400)
        return {"elements": r["elements"], "obstructions": r.get("obstructions") or [],
                "package": r.get("foreground") or r.get("package") or "",
                "backend": "bridge", "ms": int((time.time() - t0) * 1000)}
    xml = dev.u2(serial).dump_hierarchy()
    pkg = (dev.foreground(serial=serial) or {}).get("package") or ""
    return {"elements": uix.parse(xml), "obstructions": [], "package": pkg,
            "backend": "u2", "ms": int((time.time() - t0) * 1000)}


def _positions(elements, key: tuple) -> list:
    return sorted(e.bounds for e in elements if identity(e) == key)


def settled_read(key: tuple, serial: str = "", max_s: float = SETTLE_MAX_S,
                 interval_s: float = SETTLE_INTERVAL_S, reader=None,
                 expected: Optional[tuple] = None):
    """Read until the target's position is the same on two reads in a row.

    Only the target's own bounds are compared, not the whole screen: a playing
    reel repaints its scrubber every frame, so "the screen stopped changing"
    never happens there, while the Like button stays put.

    Fast path: if the first read shows the target at exactly the bounds the
    agent saw (`expected`), nothing moved between the agent's read and now, so a
    second read would only add the settle interval. This is the common case.
    """
    reader = reader or read_screen      # resolved at call time, not import time
    t0 = time.time()
    prev = reader(serial)
    reads = 1
    if expected is not None and expected in _positions(prev["elements"], key):
        return prev, True, reads
    while True:
        if time.time() - t0 >= max_s:
            return prev, False, reads
        time.sleep(interval_s)
        cur = reader(serial)
        reads += 1
        if _positions(cur["elements"], key) == _positions(prev["elements"], key):
            return cur, True, reads
        prev = cur


def classify(target, elements, obstructions=()) -> dict:
    """Decide whether and where to tap `target` given a fresh read."""
    key = identity(target)
    old = target.center
    if _strong(target):
        cands = [e for e in elements if identity(e) == key]
    else:
        # An anonymous container: same class and window, at the same place.
        cands = [e for e in elements if identity(e) == key
                 and _center_dist(e.center, old) <= MOVE_TOLERANCE_PX]

    out: dict = {"target": uix.compact([target], limit=1)[0]}
    if cands:
        cands.sort(key=lambda e: _center_dist(e.center, old))
        best = cands[0]
        dist = _center_dist(best.center, old)
        if dist > MOVE_TOLERANCE_PX and len(cands) > 1:
            out.update(status="ambiguous", matches=len(cands),
                       hint="several elements look like this one and none is "
                            "where it was - read the screen again and pick one")
            return out
        if getattr(best, "hidden", False):
            out.update(status="hidden",
                       hint="the element exists but is not visible (scrolled out "
                            "of its container) - scroll it into view first")
            return out
        x, y = best.center
        status = "same" if dist <= MOVE_TOLERANCE_PX else "shifted"
        out.update(status=status, tap=[x, y], _element=best)
        if status == "shifted":
            out["moved_from"] = list(old)
    else:
        occupant = _occupant(elements, old)
        if occupant is not None:
            out.update(status="occupied",
                       now_at_point=uix.compact([occupant], limit=1)[0],
                       hint="the screen changed and something else is where the "
                            "target was - read the screen again")
        else:
            out.update(status="disappeared",
                       hint="the target is no longer on screen - read the "
                            "screen again")
        return out

    blocker = _obstruction_at(obstructions, *out["tap"],
                              own_window=getattr(best, "window", ""))
    if blocker:
        out.update(status="obstructed", obstructed_by=blocker,
                   hint="a " + str(blocker.get("type")) + " window covers the "
                        "target; dismiss it (e.g. press back to close a keyboard)")
        out.pop("tap", None)
    return out


def _occupant(elements, point: tuple):
    """The smallest clickable element covering `point`, if any."""
    hits = [e for e in elements if e.clickable and _contains(e.bounds, *point)]
    if not hits:
        return None
    return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) *
                                   (e.bounds[3] - e.bounds[1]))


def _obstruction_at(obstructions, x: int, y: int, own_window: str = "") -> Optional[dict]:
    for w in obstructions or []:
        b = w.get("b") or [0, 0, 0, 0]
        if len(b) == 4 and _contains(b, x, y) and w.get("type") != own_window:
            return {"type": w.get("type"), "pkg": w.get("pkg"), "b": b}
    return None


def from_cache(i: Optional[int] = None, ref: str = ""):
    """Resolve `ref` ("<ver>_<i>") or a bare index against the cached screen.

    Returns (element, None) or (None, error dict). A ref from an older screen
    version is refused outright; a bare index is accepted but still goes through
    check_target, which is what catches the move.
    """
    from .. import state
    if ref:
        parsed = state.parse_ref(ref)
        if parsed is None:
            return None, {"error": "bad ref " + repr(ref),
                          "hint": "a ref is '<ver>_<i>', e.g. '1a_12', built from "
                                  "`ver` and `i` in your latest screen read"}
        ver, i = parsed
        if ver != state.version():
            return None, {"error": "VERSION_MISMATCH",
                          "ref": ref, "current_version": state.version(),
                          "hint": "the screen has changed since that read - read "
                                  "it again and use the new refs"}
    els = state.last.get("elements") or []
    if not els:
        return None, {"error": "no cached screen - read it first (look / ui_dump)"}
    if i is None or i < 0 or i >= len(els):
        return None, {"error": "index " + str(i) + " out of range (0.."
                               + str(len(els) - 1) + ")"}
    return els[i], None


def public(result: dict) -> dict:
    """The result without the private `_element` / `_fresh` objects, for the model."""
    return {k: v for k, v in result.items() if not k.startswith("_")}


def check_target(target, serial: str = "", reader=None) -> dict:
    """Settle, re-find and classify. The result says whether to tap and where.

    `_element` (the re-found element) and `_fresh` (the fresh read) are for the
    caller's write gate; strip them with public() before returning to a model."""
    t0 = time.time()
    fresh, settled, reads = settled_read(identity(target), serial, reader=reader,
                                         expected=tuple(target.bounds))
    res = classify(target, fresh["elements"], fresh["obstructions"])
    res.update(settled=settled, reads=reads, backend=fresh["backend"],
               check_ms=int((time.time() - t0) * 1000),
               package=fresh.get("package", ""), _fresh=fresh["elements"])
    return res


def check_point(x: int, y: int, serial: str = "", reader=None) -> dict:
    """For a raw coordinate tap: what is there now, and is it covered?"""
    fresh = (reader or read_screen)(serial)
    occ = _occupant(fresh["elements"], (x, y))
    res: dict = {"backend": fresh["backend"], "package": fresh.get("package", ""),
                 "_fresh": fresh["elements"]}
    if occ is not None:
        res["hits"] = uix.compact([occ], limit=1)[0]
    blocker = _obstruction_at(fresh["obstructions"], x, y)
    if blocker:
        res["obstructed_by"] = blocker
    return res
