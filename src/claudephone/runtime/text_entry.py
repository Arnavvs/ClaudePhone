"""Text entry that proves it worked (B10).

`adb shell input text` was the only channel, and it is ASCII-only in practice:
Devanagari Hinglish, the rupee sign, emoji and names with diacritics were
dropped without an error. And nothing ever looked at the field afterwards, so
"typed" meant "a command was sent", not "the field holds this".

Now:

1. **The bridge first** (`/text`, ACTION_SET_TEXT on the input-focused field).
   Any Unicode goes through. Bridge 0.2.2 re-reads the live node and returns
   what the field holds, whether it was showing a hint, and whether it is a
   password field.
2. **An independent read-back** from a fresh tree read: the focused editable
   element must hold the text too. `performAction` can return true and change
   nothing (a residual focus node; an app that ignores programmatic text), so
   neither the action's return value nor one reading is trusted alone.
3. **`adb input text` only as a fallback, and only for plain ASCII** - when the
   bridge is unreachable, or the app ignored ACTION_SET_TEXT. Non-ASCII with no
   working bridge is refused up front rather than typed with pieces missing.

Every result names the channel that put the text there, and a mismatch is an
error that says what the field actually holds.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .. import device as dev

ADB_CLEAR_MAX = 200
# Per-element text cap asked of the bridge for a read-back.
TREE_TEXT = 2000


def is_plain_ascii(text: str) -> bool:
    return all(32 <= ord(c) < 127 for c in text)


def _adb_type(text: str, serial: str = "") -> None:
    safe = text.replace("'", "'\\''").replace(" ", "%s")
    dev.shell("input text '" + safe + "'", serial=serial or None)


def _adb_clear(n: int, serial: str = "") -> None:
    """Delete up to n characters: cursor to the end, then backspace."""
    n = min(max(n, 0), ADB_CLEAR_MAX)
    if n:
        dev.shell("input keyevent KEYCODE_MOVE_END " + " ".join(["KEYCODE_DEL"] * n),
                  serial=serial or None)


def focused_field(serial: str = "") -> Optional[dict]:
    """The input-focused editable element on a fresh bridge read, or None.

    None also when the bridge predates the E/F flags (0.2.2): that is "cannot
    tell", not "no field".
    """
    from . import bridge as br
    try:
        els = br.bridge(serial).tree(limit=600, max_text=TREE_TEXT)["elements"]
    except Exception:
        return None
    for e in els:
        if getattr(e, "editable", False) and getattr(e, "focused", False):
            # A field showing only its hint ("Search") is empty.
            return {"text": "" if getattr(e, "hint", False) else (e.text or ""),
                    "id": e.rid, "password": e.password}
    return None


def _visible_texts(serial: str = "") -> list[str]:
    from . import screen
    try:
        ctx = screen.context(serial=serial, remember=False)
    except Exception:
        return []
    return [(getattr(e, "text", "") or "") for e in ctx.get("all_elements")
            or ctx.get("elements") or []]


def _matches(want: str, got: str) -> bool:
    # The tree caps text per element: a long field can only be compared as
    # far as the read shows it.
    if len(got) >= min(300, TREE_TEXT) and len(want) > len(got):
        return want.startswith(got)
    return want == got


def enter(text: str, mode: str = "replace", serial: str = "") -> dict:
    """Put `text` in the focused field and prove it. -> result dict."""
    if mode not in ("replace", "append"):
        return {"error": "mode must be 'replace' or 'append'"}
    text = text if text is not None else ""
    from . import bridge as br
    tried: list[dict] = []

    if br.available(serial):
        try:
            r = br.bridge(serial).text(text, mode)
        except br.BridgeError as e:
            r = {"ok": False, "error": "bridge: " + str(e)[:160]}
        field = r.get("field") or {}
        step = {"channel": "bridge_set_text", "acted": r.get("acted"),
                "matched": r.get("matched"), "error": r.get("error")}
        tried.append(step)
        # No focus or no editable field: adb would type into the same nothing.
        if r.get("error") and ("focused" in r["error"] or "editable" in r["error"]):
            return {"error": r["error"], "channel": "bridge_set_text",
                    "field": field or None}
        if "readback" in r:
            want = (r.get("before", "") if mode == "append" else "") + text
            if field.get("password"):
                return {"typed": text, "channel": "bridge_set_text", "verified": None,
                        "note": "password field: it reads back masked, so it "
                                "cannot be verified", "field": field}
            if r.get("matched"):
                time.sleep(0.2)
                ff = focused_field(serial)
                if ff is None or _matches(want, ff["text"]):
                    out = {"typed": text, "channel": "bridge_set_text",
                           "verified": True, "field": field,
                           "readback": r.get("readback")}
                    out["checked_by"] = ("bridge node + tree read" if ff is not None
                                         else "bridge node")
                    return out
                step["tree_readback"] = ff["text"][:200]
            else:
                step["readback"] = (r.get("readback") or "")[:200]
            before = r.get("before", "")
        else:
            # A bridge older than 0.2.2 says only ok; judge by the tree alone.
            before = ""
            want = text
            time.sleep(0.4)
            hit = any(_matches(want, t) for t in _visible_texts(serial))
            if r.get("ok") and hit:
                return {"typed": text, "channel": "bridge_set_text", "verified": True,
                        "checked_by": "screen read (bridge < 0.2.2)"}
            step["note"] = "bridge < 0.2.2; the text is not on screen"
    else:
        before = None
        tried.append({"channel": "bridge_set_text", "skipped": "bridge not reachable"})

    # Fallback: adb input text - ASCII only.
    if not is_plain_ascii(text):
        return {"error": "the text did not take, and the only other channel "
                         "(adb input text) cannot type non-ASCII characters "
                         "such as " + repr("".join(c for c in text if not is_plain_ascii(c))[:10]),
                "tried": tried}
    if mode == "replace":
        cur = before
        if cur is None:
            ff = focused_field(serial)
            cur = ff["text"] if ff else ""
        _adb_clear(len(cur or ""), serial)
    _adb_type(text, serial)
    time.sleep(0.5)
    ff = focused_field(serial)
    if ff is not None:
        got = ff["text"]
        want = text if mode == "replace" else (before or "") + text
        ok = _matches(want, got) or (mode == "append" and got.endswith(text))
        checked = "tree read"
    else:
        texts = _visible_texts(serial)
        got = next((t for t in texts if text in t), "")
        ok = bool(got)
        checked = "screen read"
    out: dict[str, Any] = {"channel": "adb_input_text", "tried": tried,
                           "checked_by": checked}
    if ok:
        out.update(typed=text, verified=True)
        return out
    out.update(error="the field does not hold the text after typing",
               expected=text, got=(got or "")[:200])
    return out
