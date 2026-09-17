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


def _swipe_gate(o: Observer, direction: str):
    """Ledger check for a swipe that advances an Instagram reel (B2b).

    -> (decision or None, refusal dict or None). A forward swipe on a reel
    viewer is a feed_reel (Reels tab) or reel_open (viewer opened from a grid);
    anything else is not counted.
    """
    from ..policy import reads
    # _swipe treats anything it does not know as "up", so the gate must too.
    if DIRECTIONS.get(direction, DIRECTIONS["up"]) != DIRECTIONS["up"]:
        return None, None
    cur = o.last or o.look()
    action = reads.reel_advance_action(cur.elements, cur.package)
    if not action:
        if cur.package == "com.twitter.android":
            refused = reads.bucketed("x_scroll", "x", target="swipe")
            if refused is not None:
                return refused, reads.refusal(refused)
        return None, None
    d = reads.acquire(action, "ig", target="swipe")
    if not d.allowed:
        return d, reads.refusal(d)
    return d, None


def _swipe_commit(d) -> None:
    if d is not None:
        from ..policy import reads
        reads.commit(d, target="swipe")


def _obstructed(obs) -> Optional[list]:
    """Compact description of windows over the app, or None when clear."""
    rows = getattr(obs, "obstructions", None) or []
    if not rows:
        return None
    return [{"type": w.get("type"), "pkg": w.get("pkg"), "b": w.get("b")}
            for w in rows[:4]]


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
                   "ver": state.version(),
                   "dump_ms": after.dump_ms,
                   "total_elements": len(after.elements),
                   "elements": after.compact(limit=limit)}
            blocked = o.explain_empty(after)
            if blocked:
                out["blocked_by"] = blocked
            over = _obstructed(after)
            if over:
                out["obstructed_by"] = over
            return out
        d = Observer.diff(before, after, limit=limit)
        d["package"] = after.package
        d["ver"] = state.version()
        d["dump_ms"] = after.dump_ms
        over = _obstructed(after)
        if over:
            d["obstructed_by"] = over
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
            "wait, and the verification in a single call. Give `ref` "
            "('<ver>_<i>' from your latest look), a bare `i`, or x/y. The "
            "element is found again on a fresh read first: if it moved the tap "
            "follows it; if it is gone, covered or replaced the tap is refused "
            "with the reason. Returns the delta, not the whole screen."
        ),
        dangerous=True,
    )
    def tap_and_see(ref: str = "", i: int = -1, x: int = -1, y: int = -1,
                    timeout_s: float = 6.0, verify: bool = True) -> dict:
        from ..policy import writes as wr
        from ..runtime import targeting as tg
        o = observer()
        serial = o.serial or dev.default_serial()
        check = None
        if ref or i >= 0:
            el, err = tg.from_cache(i=None if ref else i, ref=ref)
            if err:
                return err
            if verify:
                check = tg.check_target(el, serial=o.serial)
                if check["status"] not in tg.PROCEED:
                    return {"error": "not tapped: " + check["status"],
                            "check": tg.public(check)}
                x, y = check["tap"]
                target, fresh, pkg = check["_element"], check["_fresh"], check["package"]
            else:
                x, y = el.center
                target, fresh = el, state.last.get("elements") or []
                pkg = state.last.get("pkg") or ""
        else:
            if x < 0 or y < 0:
                return {"error": "give ref, i (from your last look) or x and y"}
            pt = tg.check_point(int(x), int(y), serial=o.serial)
            fresh, pkg = pt["_fresh"], pt["package"]
            target = wr.at_point(fresh, int(x), int(y))
        # B2: judge what the tap will hit; never skipped, even with verify=false.
        decision = wr.decide(wr.classify_tap(target, fresh, int(x), int(y), pkg), serial)
        if not decision.allowed:
            return {"error": "not tapped: write refused", "write": decision.to_dict(),
                    **({"check": tg.public(check)} if check else {})}
        # B2b: a tap that opens a budgeted read (profile, sheet, comments, reel).
        from ..policy import reads
        read_gate = None
        _, count_action = reads.classify_tap_count(target, fresh, int(x), int(y), pkg)
        if count_action and decision.verdict.kind == "read":
            read_gate = reads.acquire(count_action, reads.platform_of(pkg),
                                      serial=serial)
            if not read_gate.allowed:
                return reads.refusal(read_gate)
        kind, d = _act(o)
        act = ((lambda: d.tap(int(x), int(y))) if kind == "bridge"
               else (lambda: d.click(x, y)))
        res = o.act_and_observe(act, timeout_s=timeout_s)
        if kind == "bridge":
            lands = (d.last_tap or {}).get("lands_on") or {}
            if lands.get("covered") and not lands.get("bar"):
                # The tap went to a window over the app (keyboard, alert, chat
                # head). Say so - otherwise "nothing changed" reads as a dead app.
                res["tap_landed_on"] = lands
        if check is not None and check["status"] != "same":
            res["check"] = tg.public(check)
        if decision.verdict.kind != "read":
            res["write"] = decision.to_dict()
            warn = wr.commit(decision, serial=serial)
            if warn:
                res["write_warning"] = warn
        if read_gate is not None:
            reads.commit(read_gate, target=(target.text or target.desc or target.rid)
                         if target is not None else "", serial=serial)
            res["read"] = read_gate.to_dict()
        res["ver"] = state.version()
        return res

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
        gate, refused = _swipe_gate(o, direction)
        if refused:
            return refused
        res = o.act_and_observe(lambda: _swipe(o, direction),
                                timeout_s=timeout_s)
        _swipe_commit(gate)
        if gate is not None:
            res["read"] = gate.to_dict()
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
        gate, refused = _swipe_gate(o, direction)
        if refused:
            return refused
        res = o.act_and_observe(lambda: _swipe(o, direction),
                                timeout_s=timeout_s)
        _swipe_commit(gate)
        if gate is not None:
            res["read"] = gate.to_dict()
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
        counted: dict = {}

        o.look()
        for n in range(count):
            if time.time() - started > max_seconds:
                stopped = "max_seconds"
                break
            before = o.last
            gate, refused = _swipe_gate(o, direction)
            if refused:
                stopped = "ledger: " + refused["read"].get("why", "refused")
                break
            _swipe(o, direction)
            _swipe_commit(gate)
            if gate is not None:
                counted[gate.action] = counted.get(gate.action, 0) + 1
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
            **({"ledger_counted": counted} if counted else {}),
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
            gate, refused = _swipe_gate(o, direction)
            if refused:
                return {"found": False, "after_swipes": n, **refused}
            _swipe(o, direction)
            _swipe_commit(gate)
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
            "Report whether this run's account writes and budgeted reads will "
            "be counted: which account this phone maps to, whether the ledger "
            "is the local database or the laptop's ledger service, and what is "
            "left today for the actions that matter. Check this before a "
            "collection run - a refusal mid-run costs more than a question."
        )
    )
    def ledger_status(actions: list = None) -> dict:
        from ..policy import ledger_service as ls
        from ..policy import reads
        from ..policy import writes as wr
        o = observer()
        serial = o.serial or dev.default_serial() or ""
        out: dict = {"serial": serial,
                     "source": "service" if ls.configured_url() else "local database",
                     "uncounted_reads_allowed": reads.uncounted_allowed()}
        if ls.configured_url():
            out["service"] = ls.available()
        asked = [str(a) for a in (actions or [])]
        # What each platform actually spends. x_* and tg_search have no BUDGET
        # entry, so they run on the ledger's DEFAULT until Arnav sets ceilings.
        per_platform = {
            "ig": ["profile_open", "grid_scan", "reel_open", "reel_walk", "feed_reel",
                   "sheet_open", "comment_read", "search", "follow", "not_interested"],
            "x": ["x_scroll", "x_search", "x_consume", "x_sheet_open", "follow",
                  "not_interested"],
            "tg": ["tg_read", "tg_search", "tg_join"],
            "li": ["li_search", "li_scroll", "li_profile_open"],
        }
        for platform in ("ig", "x", "tg", "li"):
            want = asked or per_platform[platform]
            account = wr.account_for(serial, platform)
            if not account:
                continue
            row: dict = {"account": account}
            try:
                led = wr.ledger_for(account, serial)
            except wr.LedgerUnavailable as e:
                row["error"] = str(e)[:160]
                out[platform] = row
                continue
            for action in want:
                try:
                    b = led.budget(action)
                    ok, why = led.can(action, 1)
                    row[action] = {"ok": bool(ok), "per_min": b["per_min"], "why": why}
                except Exception as e:                       # one bad action, not the lot
                    row[action] = {"error": type(e).__name__ + ": " + str(e)[:80]}
            out[platform] = row
        if not any(k in out for k in ("ig", "x", "tg", "li")):
            out["hint"] = ("this phone is in no account map, so writes and counted "
                           "reads are refused; set CLAUDEPHONE_ACCOUNT_MAP, or run "
                           "with --allow-uncounted-reads for reads only")
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
        out["auth"] = br.bridge(o.serial).auth
        if br.last_heal:
            out["self_healed"] = dict(br.last_heal)
        if br.last_yield:
            out["u2_yielded"] = dict(br.last_yield)
        if out["reachable"]:
            try:
                out["health"] = br.bridge(o.serial).health()
            except br.BridgeError as e:
                out["health_error"] = str(e)[:200]
        elif out["auth"] == "rejected":
            out["hint"] = ("the bridge refused our token - read it again with "
                           "`content query --uri " + br.TOKEN_URI + "`")
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
