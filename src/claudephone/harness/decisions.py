"""Decision logging: what was on screen, what was chosen, what happened (B8).

Every step of a run writes one `decision` record to the run file:

    {"type": "decision", "step": 7,
     "screen": {"pkg": ..., "ver": "5", "age_s": 0.4},
     "candidates": [{"i": 3, "ref": "5_3", "id": ..., "text": ..., "b": [...]}, ...],
     "chosen": {"tool": "tap", "args": {"ref": "5_3"}, "target_i": 3},
     "outcome": {"screen_read": true, "changed": true, "pkg": ..., "ver": "6",
                 "appeared": [...], "error": null}}

That is V-Droid's training format - the candidate actions extracted from the
tree, the one taken, and its consequence - logged for free while the loop runs.
Nothing trains on it now; it is here because it cannot be recovered later.
What the model saw is already in the run file; which of the on-screen options
it picked, resolved to an element, is not.

Candidates are the clickable or scrollable elements of the screen the model
last read. A step on the same screen as the previous decision says so
(`candidates_as_step`) instead of repeating the list.

These records are written to the run file only. They are not streamed to the
CLI or an HTTP client: a 60-element candidate list per step is for later
analysis, not for someone watching the run.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import state

MAX_CANDIDATES = 80
TEXT_MAX = 60
APPEARED_MAX = 20


def _short(v: Any) -> str:
    s = str(v or "").replace("\n", " ").strip()
    return s[:TEXT_MAX]


def candidates(elements, ver: str) -> list[dict]:
    out = []
    for e in elements or []:
        if not (getattr(e, "clickable", False) or getattr(e, "scrollable", False)):
            continue
        if getattr(e, "hidden", False):
            continue
        d = {"i": e.i, "ref": "%s_%d" % (ver, e.i)}
        if getattr(e, "rid", ""):
            d["id"] = e.rid
        if getattr(e, "text", ""):
            d["text"] = _short(e.text)
        if getattr(e, "desc", ""):
            d["desc"] = _short(e.desc)
        d["cls"] = (getattr(e, "cls", "") or "").rsplit(".", 1)[-1]
        d["b"] = list(getattr(e, "bounds", (0, 0, 0, 0)))
        if getattr(e, "scrollable", False):
            d["scroll"] = True
        out.append(d)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _target(args: dict, elements, ver: str) -> Optional[int]:
    """Which element the call aimed at, as an index into the screen it saw."""
    args = args or {}
    ref = args.get("ref")
    if isinstance(ref, str) and ref:
        p = state.parse_ref(ref)
        if p and p[0] == ver:
            return p[1]
        return None                   # a stale ref names no element on this screen
    i = args.get("i")
    if isinstance(i, int) and i >= 0:
        return i
    x, y = args.get("x"), args.get("y")
    if isinstance(x, int) and isinstance(y, int) and x >= 0 and y >= 0:
        # the smallest clickable box containing the point
        best, area = None, None
        for e in elements or []:
            if not getattr(e, "clickable", False):
                continue
            l, t, r, b = getattr(e, "bounds", (0, 0, 0, 0))
            if l <= x <= r and t <= y <= b:
                a = (r - l) * (b - t)
                if area is None or a < area:
                    best, area = e.i, a
        return best
    return None


class DecisionLog:
    """Builds one record per step. One per run."""

    def __init__(self) -> None:
        self._last_ver: Optional[str] = None
        self._last_step = 0

    def before(self) -> dict:
        """Snapshot of the screen the model is acting on."""
        last = state.last or {}
        els = list(last.get("elements") or [])
        return {"ver": last.get("ver") or "", "pkg": last.get("pkg") or "",
                "at": last.get("at") or 0.0, "fp": last.get("fp") or "",
                "elements": els,
                "values": {(getattr(e, "text", "") or getattr(e, "desc", "")).strip()
                           for e in els} - {""}}

    def record(self, step: int, tool: str, args: dict, result: Any,
               before: dict, now: float) -> dict:
        last = state.last or {}
        ver = before["ver"]
        rec: dict[str, Any] = {
            "type": "decision", "step": step,
            "screen": {"pkg": before["pkg"], "ver": ver,
                       "age_s": round(now - before["at"], 2) if before["at"] else None},
        }
        if not before["elements"]:
            rec["candidates"] = []
            rec["screen"]["read"] = False
        elif ver and ver == self._last_ver:
            rec["candidates_as_step"] = self._last_step
        else:
            rec["candidates"] = candidates(before["elements"], ver)
            self._last_ver, self._last_step = ver, step

        chosen: dict[str, Any] = {"tool": tool, "args": args}
        t = _target(args, before["elements"], ver)
        if t is not None:
            chosen["target_i"] = t
            if 0 <= t < len(before["elements"]):
                e = before["elements"][t]
                chosen["target"] = {k: v for k, v in (
                    ("id", getattr(e, "rid", "")), ("text", _short(getattr(e, "text", ""))),
                    ("desc", _short(getattr(e, "desc", "")))) if v}
        rec["chosen"] = chosen

        read = bool(last.get("at")) and last.get("at") != before["at"]
        after_vals = {(getattr(e, "text", "") or getattr(e, "desc", "")).strip()
                      for e in (last.get("elements") or [])} - {""}
        res = result if isinstance(result, dict) else {}
        rec["outcome"] = {
            "screen_read": read,
            "changed": read and (last.get("fp") or "") != before["fp"],
            "pkg": last.get("pkg") or "",
            "ver": last.get("ver") or "",
            "appeared": sorted(after_vals - before["values"], key=len,
                               reverse=True)[:APPEARED_MAX] if read else [],
            "error": _short(res.get("error")) if "error" in res else None,
        }
        return rec


def final_screen(now: float) -> dict:
    """The last screen read, for checks against the final tree."""
    last = state.last or {}
    els = last.get("elements") or []
    return {"kind": "final_screen", "pkg": last.get("pkg") or "",
            "ver": last.get("ver") or "",
            "age_s": round(now - last["at"], 2) if last.get("at") else None,
            "texts": [t for t in ((getattr(e, "text", "") or getattr(e, "desc", ""))
                                  .strip() for e in els) if t][:300]}
