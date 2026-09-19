"""Acting on the device: tap, swipe, type, hardware keys."""

from __future__ import annotations

from typing import Optional

from .. import device as dev
from .. import state

KEYMAP = {
    "back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER",
    "recents": "KEYCODE_APP_SWITCH", "power": "KEYCODE_POWER",
    "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP",
    "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN",
    "delete": "KEYCODE_DEL", "search": "KEYCODE_SEARCH",
    "tab": "KEYCODE_TAB", "escape": "KEYCODE_ESCAPE",
}


def _screen_wh() -> tuple[int, int]:
    try:
        w, h = (int(v) for v in dev.device_info().screen.lower().split("x"))
        return w, h
    except Exception:
        return 1080, 2400


def _dispatch_tap(x: int, y: int, hold_ms: int = 0) -> dict:
    """Tap (or hold) through the bridge when it is up - it reports `lands_on` -
    otherwise through `input`."""
    from ..runtime import bridge as br
    if br.available():
        b = br.bridge()
        ok = b.tap(int(x), int(y), ms=hold_ms or 50)
        out = {"ok": ok, "via": "bridge"}
        lands = (b.last_tap or {}).get("lands_on") or {}
        if lands.get("covered") and not lands.get("bar"):
            out["landed_on"] = lands
        return out
    if hold_ms:
        dev.shell(f"input swipe {x} {y} {x} {y} {hold_ms}")
    else:
        dev.shell(f"input tap {x} {y}")
    return {"ok": True, "via": "input"}


def _tap(ref: str, i: Optional[int], x: Optional[int], y: Optional[int],
         verify: bool, hold_ms: int) -> dict:
    """Shared body of tap and long_press: resolve, verify, gate writes, dispatch.

    The write gate (policy/writes.py, B2) runs on EVERY path, including
    verify=false: skipping the re-read must never skip the ledger.
    """
    from ..policy import writes as wr
    from ..runtime import targeting as tg
    verb = "long_pressed" if hold_ms else "tapped"
    serial = dev.default_serial()
    if ref or i is not None:
        el, err = tg.from_cache(i=i, ref=ref)
        if err:
            return err
        if verify:
            chk = tg.check_target(el)
            if chk["status"] not in tg.PROCEED:
                return {"error": "not " + verb + ": " + chk["status"],
                        "check": tg.public(chk)}
            px, py = chk["tap"]
            target, fresh, pkg = chk["_element"], chk["_fresh"], chk["package"]
            check_out = tg.public(chk)
        else:
            px, py = el.center
            target, fresh = el, state.last.get("elements") or []
            pkg = state.last.get("pkg") or ""
            check_out = "skipped (verify=false)"
    else:
        if x is None or y is None:
            return {"error": "give `ref`, `i`, or both x and y"}
        px, py = int(x), int(y)
        pt = tg.check_point(px, py)       # always read: the gate needs it
        if verify and pt.get("obstructed_by"):
            return {"error": "not " + verb + ": obstructed", "check": tg.public(pt),
                    "hint": "a window covers that point; dismiss it, or pass "
                            "verify=false if you really mean to hit it"}
        fresh, pkg = pt["_fresh"], pt["package"]
        target = wr.at_point(fresh, px, py)
        check_out = tg.public(pt)

    decision = wr.decide(wr.classify_tap(target, fresh, px, py, pkg), serial)
    if not decision.allowed:
        return {"error": "not " + verb + ": write refused",
                "write": decision.to_dict(), "check": check_out}
    # B2b: a tap that opens a budgeted read (profile, sheet, comments, reel).
    from ..policy import reads
    read_gate = None
    _, count_action = reads.classify_tap_count(target, fresh, px, py, pkg)
    if count_action and decision.verdict.kind == "read":
        read_gate = reads.acquire(count_action, reads.platform_of(pkg), serial=serial)
        if not read_gate.allowed:
            return reads.refusal(read_gate, check=check_out)
    res = {verb: {"x": px, "y": py}, "check": check_out}
    res.update(_dispatch_tap(px, py, hold_ms))
    if decision.verdict.kind != "read":
        res["write"] = decision.to_dict()
        warn = wr.commit(decision, serial=serial) if res.get("ok") else None
        if warn:
            res["write_warning"] = warn
    if read_gate is not None and res.get("ok"):
        reads.commit(read_gate, target=(target.text or target.desc or target.rid)
                     if target is not None else "", serial=serial)
        res["read"] = read_gate.to_dict()
    return res


def _screen_now():
    """Fresh elements + package, without touching the agent's screen version."""
    from ..runtime import targeting as tg
    r = tg.read_screen()
    return r["elements"], r.get("package", "")


def _composer_refusal(elements, pkg: str, what: str) -> Optional[dict]:
    """Typing into, or pressing Enter in, a comment / DM / reply box is a send."""
    from ..policy import reads
    from ..policy import writes as wr
    box = reads.composer_on_screen(elements, pkg)
    if box is None or "any.composer" in wr.CONFIG.allow_rules:
        return None
    return {"error": what + " refused: a comment / message box is on screen",
            "composer": box,
            "write": {"allowed": False, "kind": "forbidden", "rule": "any.composer",
                      "why": "typing or pressing Enter in a composer sends a comment "
                             "or message; allow rule 'any.composer' for the run to "
                             "override"}}


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Tap an element. Prefer `ref` ('<ver>_<i>': `ver` and `i` from your "
            "latest screen read); a bare `i` also works; x/y is a last resort. "
            "Before tapping, the screen is read again and the element found "
            "again: if it moved the tap follows it, and if it is gone, covered "
            "or replaced the tap is REFUSED with the reason. A refusal means "
            "read the screen again - do not retry the same ref."
        )
    )
    def tap(ref: str = "", i: Optional[int] = None, x: Optional[int] = None,
            y: Optional[int] = None, verify: bool = True) -> dict:
        return _tap(ref, i, x, y, verify, hold_ms=0)

    @mcp.tool(
        description=(
            "Long-press an element or coordinate, e.g. to open a context menu. "
            "Same `ref` / `i` / x,y rules and pre-tap check as tap. duration_ms "
            "defaults to 700."
        )
    )
    def long_press(ref: str = "", i: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None,
                   duration_ms: int = 700, verify: bool = True) -> dict:
        return _tap(ref, i, x, y, verify, hold_ms=int(duration_ms))

    @mcp.tool(
        description="Swipe. direction: up|down|left|right, or give explicit "
                    "x1,y1,x2,y2. `up` advances content (next reel/post)."
    )
    def swipe(direction: str = "", x1: int = 0, y1: int = 0, x2: int = 0,
              y2: int = 0, duration_ms: int = 300) -> dict:
        if direction:
            w, h = _screen_wh()
            cx, cy = w // 2, h // 2
            dy, dx = int(h * 0.32), int(w * 0.35)
            moves = {
                "up": (cx, cy + dy, cx, cy - dy),
                "down": (cx, cy - dy, cx, cy + dy),
                "left": (cx + dx, cy, cx - dx, cy),
                "right": (cx - dx, cy, cx + dx, cy),
            }
            if direction.lower() not in moves:
                return {"error": f"bad direction {direction!r}",
                        "valid": list(moves)}
            x1, y1, x2, y2 = moves[direction.lower()]
        gate = None
        if y1 - y2 > abs(x1 - x2):              # upward: advances a feed
            from ..policy import reads
            els, pkg = _screen_now()
            action = reads.reel_advance_action(els, pkg)
            if action:
                gate = reads.acquire(action, "ig", target="swipe")
                if not gate.allowed:
                    return reads.refusal(gate)
            elif pkg == "com.twitter.android":
                refused = reads.bucketed("x_scroll", "x", target="swipe")
                if refused is not None:
                    return reads.refusal(refused)
        dev.shell(f"input swipe {x1} {y1} {x2} {y2} {int(duration_ms)}")
        out = {"swiped": {"from": [x1, y1], "to": [x2, y2],
                          "duration_ms": duration_ms}}
        if gate is not None:
            from ..policy import reads
            reads.commit(gate, target="swipe")
            out["read"] = gate.to_dict()
        return out

    @mcp.tool(
        description=(
            "Put text in the focused field (tap the field first) and check that "
            "the field now holds it. Replaces what is there; mode='append' adds "
            "to it. Any language, emoji and symbols work. The result says which "
            "channel typed it and whether it was verified; an error means the "
            "field does NOT hold your text."
        )
    )
    def text_input(text: str, mode: str = "replace") -> dict:
        from ..policy import reads
        els, pkg = _screen_now()
        refused = _composer_refusal(els, pkg, "typing")
        if refused:
            return refused
        gate = None
        # Clearing the box is not a search. Found live on IG 447: a clean-up
        # text_input("") wrote a `search` row with an empty target.
        if (text or "").strip() and reads.search_box_on_screen(els, pkg):
            gate = reads.acquire("search", "ig", target=text[:60])
            if not gate.allowed:
                return reads.refusal(gate)
        from ..runtime import text_entry
        out = text_entry.enter(text, mode=mode)
        if "error" in out:
            return out                  # the field does not hold it: not counted
        if gate is not None:
            reads.commit(gate, target=text[:60])
            out["read"] = gate.to_dict()
        return out

    @mcp.tool(
        description="Press a hardware/navigation key: back, home, enter, "
                    "recents, wake, sleep, delete, search, volume_up/down."
    )
    def press_key(key: str) -> dict:
        k = KEYMAP.get(key.strip().lower())
        if not k:
            return {"error": f"unknown key {key!r}", "valid": sorted(KEYMAP)}
        if k == "KEYCODE_ENTER":
            els, pkg = _screen_now()
            refused = _composer_refusal(els, pkg, "Enter")
            if refused:
                return refused
        dev.shell(f"input keyevent {k}")
        return {"pressed": key}
