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


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Tap the screen. Give either `i` (element index from the last "
            "ui_dump) or explicit x/y. `i` is preferred: it is resolution "
            "independent and self-documenting."
        )
    )
    def tap(i: Optional[int] = None, x: Optional[int] = None,
            y: Optional[int] = None) -> dict:
        if i is not None:
            els = state.last.get("elements") or []
            if not els:
                return {"error": "no cached ui_dump; call ui_dump first"}
            if i < 0 or i >= len(els):
                return {"error": f"index {i} out of range (0..{len(els)-1})"}
            px, py = els[i].center
            dev.shell(f"input tap {px} {py}")
            r = {"tapped": {"i": i, "x": px, "y": py},
                 "element": els[i].to_dict()}
            age = state.cache_age()
            if age > state.STALE_AFTER_S:
                r["stale_warning"] = (
                    f"cached dump was {int(age)}s old; re-dump if this misfired"
                )
            return r
        if x is None or y is None:
            return {"error": "give either `i` or both x and y"}
        dev.shell(f"input tap {int(x)} {int(y)}")
        return {"tapped": {"x": int(x), "y": int(y)}}

    @mcp.tool(
        description=(
            "Long-press an element or coordinate, e.g. to open a context menu. "
            "duration_ms defaults to 700."
        )
    )
    def long_press(i: Optional[int] = None, x: Optional[int] = None,
                   y: Optional[int] = None, duration_ms: int = 700) -> dict:
        if i is not None:
            els = state.last.get("elements") or []
            if not els or i >= len(els):
                return {"error": "bad index; call ui_dump first"}
            x, y = els[i].center
        if x is None or y is None:
            return {"error": "give either `i` or both x and y"}
        dev.shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} "
                  f"{int(duration_ms)}")
        return {"long_pressed": {"x": int(x), "y": int(y),
                                 "duration_ms": duration_ms}}

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
