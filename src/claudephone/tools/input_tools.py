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
    res = {verb: {"x": px, "y": py}, "check": check_out}
    res.update(_dispatch_tap(px, py, hold_ms))
    if decision.verdict.kind != "read":
        res["write"] = decision.to_dict()
        warn = wr.commit(decision, serial=serial) if res.get("ok") else None
        if warn:
            res["write_warning"] = warn
    return res


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
        dev.shell(f"input swipe {x1} {y1} {x2} {y2} {int(duration_ms)}")
        return {"swiped": {"from": [x1, y1], "to": [x2, y2],
                           "duration_ms": duration_ms}}

    @mcp.tool(
        description="Type text into the focused field. Tap the field first."
    )
    def text_input(text: str) -> dict:
        safe = text.replace("'", "'\\''").replace(" ", "%s")
        dev.shell(f"input text '{safe}'")
        return {"typed": text}

    @mcp.tool(
        description="Press a hardware/navigation key: back, home, enter, "
                    "recents, wake, sleep, delete, search, volume_up/down."
    )
    def press_key(key: str) -> dict:
        k = KEYMAP.get(key.strip().lower())
        if not k:
            return {"error": f"unknown key {key!r}", "valid": sorted(KEYMAP)}
        dev.shell(f"input keyevent {k}")
        return {"pressed": key}
