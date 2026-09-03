"""Compound actions: one tool call, one whole interaction sequence.

The reason this pack exists, in one measurement each:

    a screen read, bridge backend       ~13 ms
    a screen read, uiautomator2         ~260 ms
    a cheap model turn                 1000-3000 ms

**A model turn costs 100x a screen read on the fast backend, and 5-10x on the
slow one.** Either way the model, not the phone, is the expensive part. So the
win is not faster observation - it is doing the mechanical parts without waking
the model at all.

    before: model -> swipe -> model -> dump -> model -> "did it work?" -> dump
    after:  model -> feed_collect(count=20) -> 20 posts

The old loop spends ~60 model turns to read 20 posts. This spends one. Every
tool here does its own see-act-see internally and returns only what changed,
so the model gets consequences rather than raw screens.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import device as dev
from .. import state
from .. import ui as uix
from ..runtime.observer import Observer, observer

DIRECTIONS = {
    "up":    (0.5, 0.75, 0.5, 0.25),
    "down":  (0.5, 0.25, 0.5, 0.75),
    "left":  (0.75, 0.5, 0.25, 0.5),
    "right": (0.25, 0.5, 0.75, 0.5),
}


def _screen_size(obs: Observer) -> tuple:
    """Screen size, cached, without touching u2 when the bridge is up."""
    if getattr(obs, "_size", None):
        return obs._size
    from ..runtime import bridge as br
    size = None
    if br.available(obs.serial):
        # Derive the screen from the tree we already have - free, no adb call.
        # Take the MAXIMUM extent, not the first non-empty bounds: elements
        # arrive in document order and the first one with positive bounds can
        # easily be a status-bar icon, which would yield a ~1050x60 "screen"
        # and put every swipe in the notification shade.
        cur = obs.last or obs.look()
        w = max((e.bounds[2] for e in cur.elements), default=0)
        h = max((e.bounds[3] for e in cur.elements), default=0)
        if w > 200 and h > 200:
            size = (w, h)
    if size is None:
        raw = dev.shell("wm size", serial=obs.serial, check=False)
        part = raw.strip().split(":")[-1].strip()
        try:
            w, h = part.split("x")
            size = (int(w), int(h))
        except ValueError:
            size = dev.u2(obs.serial).window_size()
    obs._size = size
    return size


def _act(obs: Observer):
    """Whichever backend can act right now.

    When the bridge is up, gestures go through it and u2 is never touched. That
    is not just tidiness: mixing the two in one host process breaks adb's
    forwarded connections (see runtime/bridge.available).
    """
    from ..runtime import bridge as br
    if br.available(obs.serial):
        return ("bridge", br.bridge(obs.serial))
    return ("u2", dev.u2(obs.serial))


def _swipe(obs: Observer, direction: str, duration_ms: int = 220) -> None:
    w, h = _screen_size(obs)
    x1, y1, x2, y2 = DIRECTIONS.get(direction, DIRECTIONS["up"])
    kind, d = _act(obs)
    if kind == "bridge":
        d.swipe(int(w * x1), int(h * y1), int(w * x2), int(h * y2), duration_ms)
    else:
        d.swipe(int(w * x1), int(h * y1), int(w * x2), int(h * y2),
                duration=duration_ms / 1000.0)


def _item_text(diff: dict, min_len: int = 2) -> list[str]:
    """The human-meaningful values that appeared, longest first."""
    seen, out = set(), []
    for row in diff.get("appeared") or []:
        v = (row.get("value") or "").strip()
        if len(v) >= min_len and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def register(reg) -> None:

    # -- observation ---------------------------------------------------------

    @reg.tool(
        description=(
            "Look at the screen and return ONLY what changed since your last "
            "look. The first call returns the full screen; later calls return "
            "a small delta, which is what makes a long run affordable. Pass "
            "full=true when you genuinely need the whole screen again, e.g. "
            "after losing track of where you are."
        )
    )
    def look(full: bool = False, limit: int = 40) -> dict:
        o = observer()
        before = o.last
        after = o.look()
        if full or before is None:
            out = {"package": after.package, "activity": after.activity,
                   "dump_ms": after.dump_ms,
                   "total_elements": len(after.elements),
                   "elements": after.compact(limit=limit)}
            blocked = o.explain_empty(after)
            if blocked:
                out["blocked_by"] = blocked
            return out
        d = Observer.diff(before, after, limit=limit)
        d["package"] = after.package
        d["dump_ms"] = after.dump_ms
        if not d["content_changed"]:
            d["note"] = ("screen is unchanged since your last look - acting "
                         "again, or waiting, is more useful than looking again")
        return d

    @reg.tool(
        description=(
            "Wait until the screen stops changing, i.e. loading has finished. "
            "Use after opening an app or tapping something that loads. Far "
            "better than guessing a sleep duration: it returns as soon as the "
            "screen is quiet, and tells you if it never settled."
        )
    )
    def wait_stable(quiet_s: float = 0.6, timeout_s: float = 10.0) -> dict:
        o = observer()
        obs, waited = o.wait_until_stable(quiet_s=quiet_s, timeout_s=timeout_s)
        return {"settled": waited < timeout_s, "waited_s": waited,
                "package": obs.package, "screen_elements": len(obs.elements),
                "reads": o.reads}

    # -- act and observe in one turn ----------------------------------------

    @reg.tool(
        description=(
            "Tap something and report what changed as a result - the tap, the "
            "wait, and the verification in a single call. Give either `i` (an "
            "element index from your last look) or x/y. Returns the delta, not "
            "the whole screen, and tells you plainly if nothing happened."
        ),
        dangerous=True,
    )
    def tap_and_see(i: int = -1, x: int = -1, y: int = -1,
                    timeout_s: float = 6.0) -> dict:
        o = observer()
        if i >= 0:
            els = state.last.get("elements") or []
            if i >= len(els):
                return {"error": "index " + str(i) + " is beyond the "
                        + str(len(els)) + " elements in the last look",
                        "hint": "call look() again - the screen has moved on"}
            x, y = els[i].center
        if x < 0 or y < 0:
            return {"error": "give either i (from your last look) or x and y"}
        kind, d = _act(o)
        act = ((lambda: d.tap(int(x), int(y))) if kind == "bridge"
               else (lambda: d.click(x, y)))
        return o.act_and_observe(act, timeout_s=timeout_s)

    @reg.tool(
        description=(
            "Swipe and report what changed as a result. direction: "
            "up|down|left|right, where `up` advances a feed to the next item. "
            "Waits for the screen to actually react rather than sleeping."
        ),
        dangerous=True,
    )
    def swipe_and_see(direction: str = "up", timeout_s: float = 6.0) -> dict:
        o = observer()
        res = o.act_and_observe(lambda: _swipe(o, direction),
                                timeout_s=timeout_s)
        if not res.get("changed") and o.last is not None:
            blocked = o.explain_empty(o.last)
            if blocked:
                res["blocked_by"] = blocked
        return res

    @reg.tool(
        description=(
            "Press a hardware key and report what changed. key: back, home, "
            "enter, recents, delete, search, volume_up, volume_down."
        ),
        dangerous=True,
    )
    def press_and_see(key: str = "back", timeout_s: float = 5.0) -> dict:
        o = observer()
        kind, d = _act(o)
        act = ((lambda: d.key(key)) if kind == "bridge"
               else (lambda: d.press(key)))
        return o.act_and_observe(act, kind="structure", timeout_s=timeout_s)

    # -- the compound loops --------------------------------------------------

    @reg.tool(
        description=(
            "Advance a feed by one item and return ONLY the new item's "
            "content. Swipes, waits for the content to actually change, and "
            "diffs away the navigation chrome. This is the unit a feed is read "
            "in - use feed_collect to do many at once."
        ),
        dangerous=True,
    )
    def feed_next(direction: str = "up", timeout_s: float = 6.0,
                  settle_s: float = 0.4) -> dict:
        o = observer()
        res = o.act_and_observe(lambda: _swipe(o, direction),
                                timeout_s=timeout_s)
        if settle_s:
            time.sleep(settle_s)
            after = o.look()
            if o.last is not None:
                res["values"] = _item_text(
                    {"appeared": [{"anchor": a, "value": v}
                                  for a, v in sorted(after.values())]})[:12]
        if res.get("changed"):
            res["item"] = _item_text(res)[:12]
        return res

    @reg.tool(
        description=(
            "Read a whole feed: swipe, wait, extract, repeat - entirely on the "
            "phone, without waking the model between items. Returns the list "
            "of items with duplicates removed. THIS IS THE TOOL TO REACH FOR "
            "when asked to read, collect or summarise a feed; calling "
            "feed_next in a loop costs one model turn per item and is the "
            "thing this pack exists to avoid. Stops early when scrolling stops "
            "producing anything new."
        ),
        dangerous=True,
    )
    def feed_collect(count: int = 10, direction: str = "up",
                     settle_s: float = 0.8, timeout_s: float = 6.0,
                     max_seconds: float = 240.0,
                     stop_after_repeats: int = 3,
                     min_chars: int = 2) -> dict:
        o = observer()
        started = time.time()
        items: list[dict] = []
        seen: set = set()
        repeats = 0
        stopped = "count reached"

        o.look()
        for n in range(count):
            if time.time() - started > max_seconds:
                stopped = "max_seconds"
                break
            before = o.last
            _swipe(o, direction)
            after, waited = o.wait_for_change(timeout_s=timeout_s,
                                              baseline=before)
            if after is None:
                stopped = "feed stopped changing"
                break
            if settle_s:
                time.sleep(settle_s)
                after = o.look()
            d = Observer.diff(before, after, limit=60)
            vals = [v for v in _item_text(d, min_len=min_chars)]
            fresh = [v for v in vals if v not in seen]
            if not fresh:
                repeats += 1
                if repeats >= stop_after_repeats:
                    stopped = "nothing new after " + str(repeats) + " swipes"
                    break
                continue
            repeats = 0
            for v in fresh:
                seen.add(v)
            items.append({"n": len(items) + 1, "waited_s": waited,
                          "values": fresh[:12]})

        blocked = o.explain_empty(o.last) if (not items and o.last) else None
        return {
            "collected": len(items),
            "blocked_by": blocked,
            "requested": count,
            "stopped_because": stopped,
            "seconds": round(time.time() - started, 1),
            "screen_reads": o.reads,
            "package": (o.last.package if o.last else None),
            "items": items,
        }

    @reg.tool(
        description=(
            "Scroll until something matching `query` appears on screen, then "
            "stop. Does the whole search loop on the phone. Use it to reach an "
            "off-screen item instead of swiping one call at a time."
        ),
        dangerous=True,
    )
    def scroll_to(query: str, direction: str = "up", max_swipes: int = 25,
                  settle_s: float = 0.5) -> dict:
        o = observer()
        q = query.lower().strip()
        for n in range(max_swipes + 1):
            obs = o.look()
            hits = [e for e in obs.elements
                    if q in ((e.text or "") + " " + (e.desc or "")
                             + " " + (e.rid or "")).lower()]
            if hits:
                return {"found": True, "after_swipes": n,
                        "matches": len(hits),
                        "elements": uix.compact(hits, limit=8)}
            if n == max_swipes:
                break
            _swipe(o, direction)
            time.sleep(settle_s)
        return {"found": False, "after_swipes": max_swipes,
                "hint": "not on screen within " + str(max_swipes) + " swipes; "
                        "try the other direction or a shorter query"}

    @reg.tool(
        description=(
            "Open an app and wait until it has finished loading, returning "
            "what is on screen when it settles. Replaces launch_app followed "
            "by a guessed sleep and a separate dump."
        ),
        dangerous=True,
    )
    def open_and_wait(package: str, quiet_s: float = 0.8,
                      timeout_s: float = 20.0, limit: int = 30) -> dict:
        o = observer()
        pkg = state.resolve_pkg(package)
        dev.shell("monkey -p " + pkg + " -c android.intent.category.LAUNCHER 1",
                  serial=o.serial, check=False)
        obs, waited = o.wait_until_stable(quiet_s=quiet_s, timeout_s=timeout_s)
        ok = obs.package == pkg
        out = {"launched": pkg, "foreground": obs.package, "arrived": ok,
               "waited_s": waited, "total_elements": len(obs.elements),
               "elements": obs.compact(limit=limit)}
        if not ok:
            blocked = o.explain_empty(obs)
            out["blocked_by"] = blocked or (
                "foreground is " + (obs.package or "nothing") + ", not " + pkg
                + ". The app may have failed to start, or a permission dialog "
                "may be in front.")
        return out

    @reg.tool(
        description=(
            "Report which screen-reading backend is active and whether the "
            "on-device accessibility bridge is available. The bridge reads the "
            "screen in ~11ms versus ~260ms for uiautomator2, and can wake on "
            "content-change events instead of polling."
        )
    )
    def bridge_status() -> dict:
        from ..runtime import bridge as br
        o = observer()
        out: dict = {"backend_in_use": o.backend,
                     "installed": br.Bridge.installed(o.serial),
                     "enabled": br.Bridge.enabled(o.serial),
                     "reachable": br.available(o.serial, recheck=True)}
        if out["reachable"]:
            try:
                out["health"] = br.bridge(o.serial).health()
            except br.BridgeError as e:
                out["health_error"] = str(e)[:200]
        elif out["installed"] and not out["enabled"]:
            out["hint"] = ("installed but not enabled - call bridge_enable(), "
                           "or toggle it in Settings > Accessibility")
        elif not out["installed"]:
            out["hint"] = ("not installed - build and install it with "
                           "android/build.sh install")
        return out

    @reg.tool(
        description="Enable the on-device accessibility bridge so screen reads "
                    "become ~24x faster and can be event-driven. Writes a "
                    "secure setting, which needs the privileged shell.",
        dangerous=True,
    )
    def bridge_enable() -> dict:
        from ..runtime import bridge as br
        o = observer()
        if not br.Bridge.installed(o.serial):
            return {"error": "com.claudephone.bridge is not installed",
                    "hint": "build and install it: android/build.sh install"}
        res = br.Bridge.enable(o.serial)
        res["reachable"] = br.available(o.serial, recheck=True)
        res["backend_in_use"] = o.backend
        return res

    @reg.tool(
        description="Report how many screen reads this session has done and "
                    "their average cost. Useful for checking whether a loop "
                    "is observing more than it needs to."
    )
    def observe_stats() -> dict:
        o = observer()
        s = o.stats()
        s["backend"] = o.backend
        s["cached_screen"] = {
            "package": o.last.package, "elements": len(o.last.elements),
            "age_s": round(time.time() - o.last.at, 1),
        } if o.last else None
        return s
