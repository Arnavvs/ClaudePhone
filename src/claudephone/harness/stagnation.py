"""Noticing that a run is stuck, and saying so before it burns the budget (B4).

Cheap models loop, and these apps give them reasons to: Instagram 446's search
silently returns nothing after the first query, swipes do not always advance, and
a tap on a control that has scrolled away does nothing at all. Left alone, a
model repeats the same call until `max_steps` ends the run, which is the most
expensive possible way to fail and reports the wrong reason.

What counts as stagnation here is deliberately narrow: **the same tool, the same
arguments, and a screen that did not change**. Repeating a call that does move
the screen is progress through a feed. Repeating a call on an unchanged screen
twice earns a warning the model sees; a third time ends the run with
`stopped_by="stagnation"`, which is a truthful reason a human can act on.

A second, softer signal: if the screen matches one seen several steps back, the
model is probably going in a circle - it is told where it has been rather than
stopped, because a revisit is sometimes the correct route.

The screen fingerprint comes from `state.remember`, which every read already
computes, so this costs nothing extra.

Others arrived at the same numbers: Mobile-Agent-E escalates to its planner after
two failures, PhoneCLI aborts after three repeats, and ARTEMIS keeps a revisit
hint of exactly this kind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .. import state

# Tools whose whole job is to look; repeating one is not stagnation on its own,
# and a model re-reading a screen it just read is following the loop we told it
# to follow.
_READ_ONLY = {"ui_dump", "look", "find_element", "extract_fields", "foreground_app",
              "bridge_status", "ledger_status", "list_tool_packs", "find_tool",
              "screen_signature"}


def _key(tool: str, args: dict) -> str:
    try:
        return tool + ":" + json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        return tool + ":" + repr(args)[:200]


@dataclass
class Stagnation:
    """Tracks repeats of an identical call on an unchanged screen.

    warn_after: identical no-change attempts before the model is told.
    stop_after: attempts before the run ends. 0 disables that half.
    revisit_gap: how many steps back a matching screen must be to be worth a hint.
    """

    warn_after: int = 2
    stop_after: int = 3
    revisit_gap: int = 5
    # Same screen for this many steps in a row, whatever was called. Catches
    # the loop the per-call rule cannot: ui_dump / ui_dump(query=X) / ui_dump /
    # ... - different arguments each time, so no single call ever repeats.
    idle_warn: int = 4

    repeats: dict = field(default_factory=dict)      # key -> consecutive no-change count
    idle: int = 0                                    # consecutive steps, screen unchanged
    seen: dict = field(default_factory=dict)         # screen fingerprint -> first step
    last_key: str = ""
    warned: set = field(default_factory=set)

    def _fp(self) -> str:
        last = getattr(state, "last", None) or {}
        return str(last.get("fp") or "")

    def before(self) -> str:
        return self._fp()

    def observe(self, step: int, tool: str, args: dict, fp_before: str) -> Optional[dict]:
        """Called after each tool result. -> None, or an advisory / stop.

        {"stop": True, ...} means end the run; otherwise it is a note to hand the
        model once, phrased as something it can act on.
        """
        fp_after = self._fp()
        key = _key(tool, args)
        # A first read (nothing -> something) counts as a change; a read that
        # returns the same screen, or no read at all, does not.
        changed = bool(fp_after) and fp_after != fp_before

        # No fingerprint at all means no EVIDENCE of progress, which is not the
        # same as progress. A model that swipes blindly without ever reading the
        # screen is the case this feature exists for; measured live on the
        # Samsung, treating "unknown" as "changed" let 25 identical swipes run to
        # max_steps without a word.
        self.idle = 0 if changed else self.idle + 1
        if changed:
            self.repeats.pop(key, None)
        elif tool in _READ_ONLY and key != self.last_key:
            pass                                      # a look between two actions
        else:
            self.repeats[key] = self.repeats.get(key, 0) + 1
        self.last_key = key

        n = self.repeats.get(key, 0)
        if self.stop_after and n >= self.stop_after:
            return {
                "stop": True,
                "reason": "stagnation",
                "tool": tool, "args": args, "attempts": n,
                "message": (
                    "Stopping: " + tool + " was called " + str(n) + " times with the "
                    "same arguments and the screen never changed. Something on this "
                    "screen is not responding to it."),
            }
        if n >= self.warn_after and key not in self.warned:
            self.warned.add(key)
            return {
                "stop": False,
                "reason": "possibly_stuck",
                "tool": tool, "args": args, "attempts": n,
                "message": (
                    "POSSIBLY STUCK: you have called " + tool + " " + str(n) + " times "
                    "with the same arguments and the screen has not changed. Do not "
                    "call it a third time. Read the screen, and either act on a "
                    "different element, back out and re-enter, or report that this "
                    "step cannot be completed."),
            }

        if self.idle_warn and self.idle >= self.idle_warn and "idle" not in self.warned:
            self.warned.add("idle")
            return {
                "stop": False,
                "reason": "idle_screen",
                "tool": tool, "args": args, "attempts": self.idle,
                "message": (
                    "The screen has not changed in " + str(self.idle) + " steps. "
                    "Reading it again will not change that. If what you need is "
                    "not visible it is probably further down - use "
                    "scroll_to(query=...) or swipe up - or tap into the section "
                    "that holds it. If you already have the answer, give it."),
            }

        # Circling: this screen was already seen a while ago.
        if fp_after and changed:
            first = self.seen.get(fp_after)
            if first is None:
                self.seen[fp_after] = step
            elif step - first >= self.revisit_gap and fp_after not in self.warned:
                self.warned.add(fp_after)
                return {
                    "stop": False,
                    "reason": "revisited",
                    "first_seen_step": first, "attempts": 0,
                    "tool": tool, "args": args,
                    "message": (
                        "You were on this exact screen at step " + str(first) + ". If "
                        "that was not deliberate, the route you are taking loops - "
                        "try a different one."),
                }
        return None
