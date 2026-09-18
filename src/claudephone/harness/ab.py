"""Comparing deciders on identical tasks, instead of assuming (B5).

The case for splitting the decider from the helper rests on one ablation, and
the report's advice was to check it here rather than take it on trust. This runs
one goal against several decider models in turn, resets the phone between them,
and records what each actually did: how it stopped, in how many steps, at what
cost, and - when an expected answer is given - whether its answer contains it.

Free models are rationed per day, so a run that the remaining quota cannot finish
is skipped before it starts, not abandoned half way through.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Optional

from .loop import Budget


def _answer_ok(text: str, expect: str) -> Optional[bool]:
    if not expect:
        return None
    try:
        return re.search(expect, text or "", re.I) is not None
    except re.error:
        return expect.lower() in (text or "").lower()


def run_one(model: str, goal: str, budget: Budget, expect: str = "",
            notes: str = "", provider: str = "", build=None) -> dict:
    """One decider, one goal. -> a row for the comparison table."""
    if build is None:
        from ..agent import build_agent as build
    agent = build(provider=provider, model=model, budget=budget,
                  operator_notes=notes, stagnation=True)
    tools: list = []
    final: dict = {}
    t0 = time.time()
    error = ""
    try:
        for ev in agent.run(goal):
            if ev.get("type") == "tool_call":
                tools.append(ev.get("tool"))
            elif ev.get("type") == "final":
                final = ev
            elif ev.get("type") == "error":
                error = str(ev.get("error"))[:300]
    except Exception as e:                       # a crash is a result too
        error = type(e).__name__ + ": " + str(e)[:300]
    answer = str(final.get("content") or "")
    last = str(final.get("last_thought") or "")
    return {
        "model": model,
        "stopped_by": final.get("stopped_by") or ("error" if error else "done"),
        # Graded on the answer it GAVE. `knew` is reported separately: a run cut
        # off with the answer only in its last words did not finish the task.
        "correct": _answer_ok(answer, expect) if final else False,
        "knew": _answer_ok(last, expect) if (last and not answer) else None,
        "last_thought": last[:300],
        "steps": final.get("steps", len(tools)),
        "seconds": round(time.time() - t0, 1),
        "cost_usd": final.get("cost_usd", 0.0),
        "requests": final.get("requests", 0),
        "free_requests_left": final.get("free_requests_left"),
        "tool_convention": getattr(agent.chat, "tool_convention", ""),
        "tools": tools,
        "answer": answer[:400],
        "error": error,
    }


def run_ab(goal: str, models: list, budget: Budget, expect: str = "",
           notes: str = "", provider: str = "",
           reset: Optional[Callable[[], None]] = None,
           quota: Optional[Callable[[], Optional[int]]] = None,
           build=None) -> list:
    """Every model on the same goal, the phone reset in between."""
    rows = []
    for m in models:
        left = quota() if quota else None
        need = budget.max_steps + budget.free_reserve + 1
        if left is not None and left < need:
            rows.append({"model": m, "stopped_by": "skipped",
                         "error": "only %d free requests left today; a %d-step "
                                  "run needs up to %d" % (left, budget.max_steps, need)})
            continue
        if reset:
            reset()
        rows.append(run_one(m, goal, budget, expect=expect, notes=notes,
                            provider=provider, build=build))
    return rows


def table(rows: list) -> str:
    head = "%-40s %-22s %-7s %5s %7s %9s %5s" % (
        "decider", "stopped_by", "correct", "steps", "secs", "cost $", "reqs")
    out = [head, "-" * len(head)]
    for r in rows:
        out.append("%-40s %-22s %-7s %5s %7s %9s %5s" % (
            r["model"][:40], str(r.get("stopped_by"))[:22], str(r.get("correct")),
            r.get("steps", ""), r.get("seconds", ""),
            ("%.5f" % r["cost_usd"]) if isinstance(r.get("cost_usd"), (int, float)) else "",
            r.get("requests", "")))
    return "\n".join(out)


def save(rows: list, goal: str, expect: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "ab_%d.json" % int(time.time()))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"goal": goal, "expect": expect, "at": time.time(), "rows": rows},
                  f, indent=1, ensure_ascii=False)
    return path
