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
            notes: str = "", provider: str = "", build=None,
            deny: Optional[list] = None) -> dict:
    """One decider, one goal. -> a row for the comparison table.

    `deny` removes tools for the run - how a comparison takes away shortcuts
    such as a shell that could read a setting without opening the screen.
    """
    if build is None:
        from ..agent import build_agent as build
    kw = dict(provider=provider, model=model, budget=budget,
              operator_notes=notes, stagnation=True)
    if deny:
        kw["deny"] = list(deny)
    agent = build(**kw)
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


# -- pre-registered plans, run across days --------------------------------------
#
# A comparison worth trusting needs several trials per model, and the free tier
# allows 50 requests a day. So a plan is written down first - tasks, answers,
# models, trials, order - and executed in pieces: each run is saved as soon as it
# ends, the next invocation picks up at the first run not yet done, and a run
# the day's quota could not finish is never started.

def plan_items(plan: dict) -> list:
    """The runs a plan calls for, in order. Model order alternates by round so
    neither model always goes first on a task (phone state, time of day)."""
    items = []
    models = list(plan["models"])
    for rnd in range(1, int(plan.get("trials", 1)) + 1):
        for k, task in enumerate(plan["tasks"]):
            order = models if (rnd + k) % 2 else list(reversed(models))
            for m in order:
                items.append({"id": "r%d-%s-%s" % (rnd, task["id"], m),
                              "round": rnd, "task": task["id"], "model": m})
    return items


def run_plan(plan: dict, state_path: str, quota=None, reset=None, build=None,
             provider: str = "", log=print, shell=None) -> dict:
    """Run every pending item the quota allows. Saves after each one."""
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {"plan": plan.get("name", ""), "results": {}}
    tasks = {t["id"]: t for t in plan["tasks"]}
    for it in plan_items(plan):
        if it["id"] in st["results"]:
            continue
        task = tasks[it["task"]]
        budget = Budget(max_steps=int(task["max_steps"]),
                        max_seconds=float(task.get("max_seconds", 360)),
                        max_usd=float(plan.get("max_usd", 0.05)),
                        free_reserve=int(plan.get("free_reserve", 2)))
        left = quota() if quota else None
        need = budget.max_steps + budget.free_reserve + 1
        if left is not None and left < need:
            log("stop: %d free requests left, %s needs up to %d" % (left, it["id"], need))
            st["stopped"] = {"at": time.time(), "left": left, "next": it["id"]}
            break
        if reset:
            reset(plan.get("reset") or [])
        log("run %s" % it["id"])
        row = run_one(it["model"], task["goal"], budget, expect=task["expect"],
                      notes=plan.get("notes", ""), provider=provider, build=build,
                      deny=plan.get("deny") or [])
        row.update({"round": it["round"], "task": it["task"], "at": time.time()})
        # A read-only task must leave the phone as it found it. Where a setting
        # sits one tap from being changed, check it afterwards, put it back, and
        # record the run as a safety failure - whatever its answer was.
        g = task.get("guard")
        if g and shell is not None:
            seen = str(shell(g["cmd"]) or "").strip()
            if seen != str(g["value"]):
                shell(g["restore"])
                row["changed_setting"] = {"cmd": g["cmd"], "was": seen,
                                          "restored_to": g["value"]}
                log("  !! %s changed a setting (%s) - restored" % (it["id"], seen))
        st["results"][it["id"]] = row
        st.pop("stopped", None)
        os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, ensure_ascii=False)
        log("  -> %s, correct=%s, %s steps" % (row["stopped_by"], row["correct"], row["steps"]))
    return st


def summarise(plan: dict, st: dict) -> list:
    """Per task and model: how many runs, how many correct, and how they ended."""
    out = []
    for task in plan["tasks"]:
        for m in plan["models"]:
            rows = [r for r in st.get("results", {}).values()
                    if r.get("task") == task["id"] and r.get("model") == m]
            steps = sorted(r.get("steps") or 0 for r in rows)
            secs = sorted(r.get("seconds") or 0 for r in rows)
            ends: dict = {}
            for r in rows:
                k = str(r.get("stopped_by") or "?").split(" ")[0]
                ends[k] = ends.get(k, 0) + 1
            out.append({"task": task["id"], "model": m, "runs": len(rows),
                        "correct": sum(1 for r in rows if r.get("correct")),
                        "knew_but_unsaid": sum(1 for r in rows if r.get("knew")),
                        "changed_a_setting": sum(1 for r in rows if r.get("changed_setting")),
                        "median_steps": steps[len(steps) // 2] if steps else None,
                        "median_seconds": secs[len(secs) // 2] if secs else None,
                        "ended": ends})
    return out
