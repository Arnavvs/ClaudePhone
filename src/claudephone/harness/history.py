"""What the model keeps of its own run: step capsules, notes, recall (B7).

Before this, a tool result older than six steps was cut to 220 characters. On a
profile pass the follower count read at step 3 was gone by step 10, and the
model either re-read the screen (a counted read on Instagram) or guessed.

Three pieces replace the clip:

* **Capsules.** When a result ages out of the recent window it becomes one
  mechanical line built from what the harness already knows - the call, the
  screen before and after, what appeared - with no model call:

      T+01:32 #9 tap(ref='5_12') -> content changed (com.instagram.android);
      appeared: 'Following' | full result: recall(steps=[9])

* **Notes.** `remember(key, value)` pins a fact for the whole run. Notes are
  never compacted; they ride in the system message.
* **Recall.** `recall(query=...)` or `recall(steps=[...])` reads this run's own
  record - the JSONL the recorder writes - so nothing the model saw is lost,
  only moved out of the way.

**Neutral wording.** Capsules say what was observed ("content changed",
"returned an error", "no screen read"), never "successfully", "failed" or
"navigated to". A verdict written into history gets believed later, even when
it was wrong (ARTEMIS bans those words in its ledger for the same reason).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

MAX_NOTES = 40
NOTE_KEY_MAX = 60
NOTE_VALUE_MAX = 300
APPEARED_MAX = 3
VALUE_MAX = 40
RECALL_RESULT_MAX = 4000
RECALL_HITS = 8
RECALL_SNIPPET = 120

# Words that turn an observation into a verdict. Notes may contain them - the
# model owns its notes - but it is told, because a verdict is what gets
# believed later without being re-checked.
VERDICT = re.compile(r"\b(successfully|succeeded|failed|navigated to)\b", re.I)

# Tools that neither read nor change the screen. Recording them in a capsule as
# "no screen read" would be true but useless; their capsule says what they did.
NO_SCREEN = {"remember", "recall"}


class RunHistory:
    """Per-run notes, capsules and the events recall reads."""

    def __init__(self) -> None:
        self.reset()

    def reset(self, path: str = "", started: Optional[float] = None) -> None:
        self.path = path
        self.started = started or time.time()
        self.notes: dict[str, str] = {}
        self.capsules: dict[int, str] = {}
        # Kept in memory as well as on disk, so recall still works on a run with
        # recording turned off (CLAUDEPHONE_RECORD=0).
        self.events: list[dict] = []

    # -- notes ---------------------------------------------------------------

    def remember(self, key: str, value: str) -> dict:
        key = (key or "").strip()[:NOTE_KEY_MAX]
        value = str(value if value is not None else "").strip()
        if not key:
            return {"error": "give the note a short key, e.g. 'followers'"}
        if not value:
            removed = self.notes.pop(key, None)
            return {"forgot": key} if removed is not None else {
                "error": "no note called " + repr(key)}
        if key not in self.notes and len(self.notes) >= MAX_NOTES:
            return {"error": "%d notes is the limit; overwrite or clear one "
                             "(remember(key, '') clears it)" % MAX_NOTES}
        clipped = len(value) > NOTE_VALUE_MAX
        self.notes[key] = value[:NOTE_VALUE_MAX]
        out: dict[str, Any] = {"remembered": key, "notes": len(self.notes)}
        if clipped:
            out["clipped_to"] = NOTE_VALUE_MAX
        if VERDICT.search(value):
            out["hint"] = ("notes are read later as fact - record what you saw "
                           "(the text, the number), not a verdict about it")
        return out

    def notes_block(self) -> str:
        if not self.notes:
            return ""
        return ("NOTES YOU SAVED (kept for the whole run; remember(key, '') "
                "clears one):\n" + "\n".join(
                    "- %s: %s" % (k, v) for k, v in self.notes.items()))

    # -- events --------------------------------------------------------------

    def event(self, ev: dict) -> None:
        if ev.get("type") in ("tool_call", "tool_result", "thought"):
            self.events.append(ev)

    def _rows(self) -> list[dict]:
        """This run's events: the recorder's file when there is one."""
        if self.path:
            rows = []
            try:
                with open(self.path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue        # a line being written right now
            except OSError:
                rows = []
            rows = [r for r in rows
                    if r.get("type") in ("tool_call", "tool_result", "thought")]
            if rows:
                return rows
        return list(self.events)

    # -- recall --------------------------------------------------------------

    def recall(self, query: str = "", steps: Optional[list] = None) -> dict:
        rows = self._rows()
        if steps:
            try:
                want = [int(s) for s in (steps if isinstance(steps, list)
                                         else [steps])][:5]
            except (TypeError, ValueError):
                return {"error": "steps must be step numbers, e.g. [3, 9]"}
            out = []
            for n in want:
                call = next((r for r in rows if r.get("type") == "tool_call"
                             and r.get("step") == n), None)
                res = next((r for r in rows if r.get("type") == "tool_result"
                            and r.get("step") == n), None)
                if call is None:
                    out.append({"step": n, "error": "no such step in this run"})
                    continue
                text = json.dumps((res or {}).get("result"), default=str,
                                  ensure_ascii=False)
                item = {"step": n, "tool": call.get("tool"),
                        "args": call.get("args"),
                        "result": text[:RECALL_RESULT_MAX]}
                if len(text) > RECALL_RESULT_MAX:
                    item["clipped_chars"] = len(text) - RECALL_RESULT_MAX
                out.append(item)
            return {"steps": out}

        q = (query or "").strip().lower()
        if not q:
            return {"error": "give a query (text to look for) or steps=[n, ...]"}
        hits = []
        step_of_thought = 0
        for r in rows:
            kind = r.get("type")
            if kind == "tool_call":
                step_of_thought = r.get("step") or step_of_thought
                continue
            if kind == "tool_result":
                if r.get("tool") in NO_SCREEN:
                    continue                  # never find your own recall
                text = json.dumps(r.get("result"), default=str, ensure_ascii=False)
                where = {"step": r.get("step"), "tool": r.get("tool")}
            else:
                text = str(r.get("content") or "")
                where = {"after_step": step_of_thought, "thought": True}
            low = text.lower()
            i = low.find(q)
            if i < 0:
                continue
            a = max(0, i - RECALL_SNIPPET)
            hits.append(dict(where, snippet=text[a:i + len(q) + RECALL_SNIPPET]))
            if len(hits) >= RECALL_HITS:
                break
        return {"query": query, "hits": hits,
                "note": "" if hits else "nothing in this run's record matches"}


# One per process, like `state.last`: tools reach it without a handle on the
# agent, and a run resets it at its start.
current = RunHistory()


# -- capsules ----------------------------------------------------------------

def screen_values(last: dict) -> set:
    out = set()
    for e in (last or {}).get("elements") or []:
        v = (getattr(e, "text", "") or getattr(e, "desc", "") or "").strip()
        if v:
            out.add(v)
    return out


def snapshot(last: dict) -> dict:
    """What a capsule needs to know about the screen before a call."""
    last = last or {}
    return {"fp": last.get("fp") or "", "at": last.get("at") or 0.0,
            "pkg": last.get("pkg") or "", "values": screen_values(last)}


def _short(v: Any, n: int = VALUE_MAX) -> str:
    s = str(v).replace("\n", " ").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _args(args: dict) -> str:
    parts = []
    for k, v in (args or {}).items():
        parts.append("%s=%s" % (k, repr(_short(v, 30)) if isinstance(v, str)
                                else _short(v, 30)))
    return ", ".join(parts)[:90]


def capsule(step: int, tool: str, args: dict, result: Any, before: dict,
            after: dict, at: float, started: float) -> str:
    """One neutral line for a step that has left the recent window."""
    t = max(0, int(at - started))
    head = "T+%02d:%02d #%d %s(%s)" % (t // 60, t % 60, step, tool, _args(args))
    res = result if isinstance(result, dict) else {}

    if "error" in res:
        what = "returned an error: " + _short(res.get("error"), 90)
    elif tool == "remember":
        what = "saved a note (see NOTES)"
    elif tool == "recall":
        what = "read back from this run's record"
    elif not after.get("at") or after.get("at") == before.get("at"):
        what = "no screen read"
    elif after.get("fp") == before.get("fp"):
        what = "screen unchanged"
    elif (after.get("pkg") == before.get("pkg")
          and after.get("values") == before.get("values")):
        # The fingerprint includes bounds: a list that scrolled without
        # revealing anything new. Seen live on Settings, which the bridge
        # reads past the fold.
        what = "same items, positions moved (" + (after.get("pkg") or "?") + ")"
    else:
        pkg_a, pkg_b = before.get("pkg"), after.get("pkg")
        what = ("content changed (" + pkg_b + ")" if pkg_a == pkg_b or not pkg_a
                else "app changed " + (pkg_a or "?") + " -> " + (pkg_b or "?"))
        new = sorted(after.get("values", set()) - before.get("values", set()),
                     key=len, reverse=True)
        if new:
            what += "; appeared: " + ", ".join(
                repr(_short(v)) for v in new[:APPEARED_MAX])
            if len(new) > APPEARED_MAX:
                what += " +%d more" % (len(new) - APPEARED_MAX)
    return head + " -> " + what + " | full result: recall(steps=[%d])" % step
