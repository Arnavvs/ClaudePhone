"""Guarded replay of verified runs (B9).

The biggest cost and reliability lever in the survey: a routine that has
already been done correctly once can be done again without a model. AgentRR
replayed 60-85% of actions on realistic task mixes at >99% correctness, and
mobilerun, agent-device and PhoneCLI all ship the same idea. Replayed steps
cost no tokens, and they do not wander.

## Making a macro

`make_macro(run_id)` reads a recorded run, and refuses unless it was
**verified** - an automatic `pass` from B8, or a person's `success=True` label.
A macro built from an unverified run would replay its mistakes faithfully.

From the run's B8 decision records it keeps, per ACTION step:

* the **pre-state**: the actionable elements of the screen the model was
  looking at when it chose, as keys (`id`, or the label for id-less nodes),
  plus the app;
* the **action**, with the element it aimed at stored as a SELECTOR (id, text,
  description) - never as a ref, which names an element on one read only.

Pure reads (`ui_dump`, `recall`, ...) are dropped: they were for the model.

**Slots.** `slots={"handle": "creator_a"}` turns every occurrence of
"creator_a" in the steps into `{handle}`, so the same macro runs for another
creator: `replay(macro, values={"handle": "creator_b"})`.

## Replaying

Each step waits (briefly) for the live screen to match its pre-state -
mobilerun's rule: Jaccard of element keys x 0.85 + same app x 0.15 >= 0.85 -
then finds the target by its selector on the live screen, and acts through the
same tool registry the agent uses. So every write still passes B2's target
rules and the ledger, and every counted read is still counted.

Steps that establish a screen rather than act on one (`launch_app`,
`open_link`, `open_and_wait`, `press_key home`) are not held to a pre-state.

On the first mismatch - a screen that does not match, a target that is not
there, a tool error - replay stops and returns a **handoff**: what was done,
where it stopped and why. With an agent factory it hands the remaining goal to
the agent on the spot.

Every replay is recorded like a run (`model: "replay"`), so `claudephone runs`
lists it and B8 can verify it against the macro's own checks.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .. import state

MACRO_DIR = os.path.join(state.ARTIFACT_DIR, "macros")
THRESHOLD = 0.85
MATCH_WAIT_S = 6.0

# Tools that act on the phone. Anything else in a run was the model looking.
ACTIONS = {"tap", "long_press", "tap_and_see", "press_and_see", "text_input",
           "swipe", "swipe_and_see", "press_key", "launch_app", "open_link",
           "open_and_wait", "scroll_to", "wait_for", "wait_stable", "stop_app"}
# Actions that put the phone on a known screen: no pre-state to match.
ENTRY = {"launch_app", "open_link", "open_and_wait", "stop_app"}
# Actions aimed at one element.
TARGETED = {"tap", "long_press", "tap_and_see", "press_and_see"}


# -- keys and similarity ---------------------------------------------------------

def _key(c: dict) -> str:
    """An element's identity across reads: its id, else its label."""
    return c.get("id") or ("t:" + (c.get("text") or c.get("desc") or "")[:40])


def keys_of(candidates: list) -> list[str]:
    return sorted({_key(c) for c in candidates or [] if _key(c) not in ("", "t:")})


def similarity(saved: dict, live: dict) -> float:
    """mobilerun's screen match: element keys x 0.85 + same app x 0.15."""
    a, b = set(saved.get("keys") or []), set(live.get("keys") or [])
    jac = (len(a & b) / len(a | b)) if (a | b) else 1.0
    same = 1.0 if (saved.get("pkg") or "") == (live.get("pkg") or "") else 0.0
    return round(jac * 0.85 + same * 0.15, 3)


# -- making a macro --------------------------------------------------------------

def _verified(rows: list) -> Optional[str]:
    for r in reversed(rows):
        if r.get("kind") != "label":
            continue
        if r.get("by") == "verify" and r.get("verdict") == "pass":
            return "verified by checks"
        if r.get("success") is True and r.get("by") != "verify":
            return "labelled a success by a person"
    return None


def _slot(value: Any, slots: dict) -> Any:
    if isinstance(value, str):
        for name, raw in slots.items():
            if raw:
                value = value.replace(str(raw), "{" + name + "}")
        return value
    if isinstance(value, dict):
        return {k: _slot(v, slots) for k, v in value.items()}
    if isinstance(value, list):
        return [_slot(v, slots) for v in value]
    return value


def make_macro(rows: list, name: str, slots: Optional[dict] = None,
               allow_unverified: bool = False) -> dict:
    slots = dict(slots or {})
    meta = rows[0] if rows and rows[0].get("kind") == "meta" else {}
    why = _verified(rows)
    if not why and not allow_unverified:
        return {"error": "this run was not verified; a macro from it would "
                         "replay its mistakes",
                "next": "claudephone verify <run_id> --expect/--reached ... "
                        "first, or label it by hand"}
    decisions = {r["step"]: r for r in rows if r.get("type") == "decision"}
    if not decisions:
        return {"error": "no decision records in this run (recorded before B8)"}
    screens: dict[int, dict] = {}
    steps: list[dict] = []
    # A step's pre-state is the screen the model last READ. If it acted since
    # that read, the saved screen is stale - it shows where the phone was, not
    # where it is - so the step gets no screen guard (a targeted step is still
    # guarded by having to find its element). Found live: `wait_stable` right
    # after a tap carried the PREVIOUS screen and failed to match at 0.30.
    last_read, last_action = 0, 0
    for n in sorted(decisions):
        d = decisions[n]
        fresh = last_read > last_action or last_action == 0
        if "candidates" in d:
            screens[n] = {"keys": keys_of(d["candidates"]),
                          "pkg": (d.get("screen") or {}).get("pkg") or ""}
        pre = screens.get(d.get("candidates_as_step"), screens.get(n)) \
            if "candidates_as_step" in d else screens.get(n)
        chosen = d.get("chosen") or {}
        tool = chosen.get("tool")
        if tool in ACTIONS:
            last_action = n
        if (d.get("outcome") or {}).get("screen_read"):
            last_read = n
        if tool not in ACTIONS:
            continue
        res = next((r for r in rows if r.get("type") == "tool_result"
                    and r.get("step") == n), {})
        if not res.get("ok", True):
            continue                                  # a failed step is not part of the route
        args = {k: v for k, v in (chosen.get("args") or {}).items()
                if k not in ("ref", "i", "x", "y")}
        step: dict[str, Any] = {"tool": tool, "args": _slot(args, slots),
                                "from_step": n}
        if tool in TARGETED:
            target = chosen.get("target")
            if not target:
                step["unreplayable"] = "the element it tapped was not recorded"
            else:
                step["target"] = _slot(target, slots)
        if tool not in ENTRY and not (tool == "press_key" and
                                      str(args.get("key")).lower() == "home"):
            if pre and pre["keys"] and fresh:
                step["pre"] = pre
        steps.append(step)
    if not steps:
        return {"error": "the run has no action steps to replay"}
    spec = next((r.get("spec") for r in reversed(rows)
                 if r.get("kind") == "label" and r.get("spec")), None)
    return {"name": name, "goal": _slot(meta.get("goal") or "", slots),
            "from_run": meta.get("run_id"), "device": meta.get("device"),
            "made": datetime.now().astimezone().isoformat(timespec="seconds"),
            "verified": why or "NOT VERIFIED (allow_unverified)",
            "slots": sorted(slots), "steps": steps,
            "verify": _slot(spec, slots) if spec else None}


def save(macro: dict) -> str:
    os.makedirs(MACRO_DIR, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", macro["name"]):
        raise ValueError("macro names are letters, digits, _ . - only")
    path = os.path.join(MACRO_DIR, macro["name"] + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(macro, fh, indent=1, ensure_ascii=False)
    return path


def load(name: str) -> Optional[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", name or ""):
        return None
    try:
        with open(os.path.join(MACRO_DIR, name + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return None


def list_macros() -> list[dict]:
    try:
        names = sorted(f[:-5] for f in os.listdir(MACRO_DIR) if f.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        m = load(n) or {}
        out.append({"name": n, "goal": m.get("goal"), "steps": len(m.get("steps") or []),
                    "slots": m.get("slots"), "verified": m.get("verified")})
    return out


# -- replaying -----------------------------------------------------------------

def _fill(value: Any, values: dict) -> Any:
    if isinstance(value, str):
        for k, v in values.items():
            value = value.replace("{" + k + "}", str(v))
        return value
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


def live_screen(read: Callable[[], None]) -> dict:
    """Read the screen now; -> {"keys", "pkg", "candidates"}."""
    from .decisions import candidates
    read()
    last = state.last or {}
    ver = last.get("ver") or ""
    els = last.get("elements") or []
    cands = candidates(els, ver)
    # Targets are matched against every VISIBLE element, not just clickable
    # ones: a model taps a row's label ("Display"), which is often a plain
    # TextView inside the clickable row.
    visible = []
    for e in els:
        if getattr(e, "hidden", False):
            continue
        d = {"i": e.i, "ref": "%s_%d" % (ver, e.i)}
        for k, v in (("id", e.rid), ("text", (e.text or "")[:60]), ("desc", (e.desc or "")[:60])):
            if v:
                d[k] = v
        visible.append(d)
    return {"keys": keys_of(cands), "pkg": last.get("pkg") or "",
            "candidates": cands, "visible": visible}


def find_target(target: dict, cands: list) -> Optional[dict]:
    """The live element a saved selector names, or None if it is ambiguous/absent."""
    tid, text, desc = target.get("id"), target.get("text"), target.get("desc")
    tries = []
    if tid and (text or desc):
        tries.append(lambda c: c.get("id") == tid and
                     (c.get("text") == text if text else c.get("desc") == desc))
    if tid:
        tries.append(lambda c: c.get("id") == tid)
    if text:
        tries.append(lambda c: c.get("text") == text)
    if desc:
        tries.append(lambda c: c.get("desc") == desc)
    for t in tries:
        hits = [c for c in cands if t(c)]
        if len(hits) == 1:
            return hits[0]
    return None


def replay(macro: dict, values: Optional[dict] = None, registry=None,
           read: Optional[Callable[[], None]] = None, serial: str = "",
           agent_factory: Optional[Callable[[], Any]] = None,
           match_wait_s: float = MATCH_WAIT_S, record: bool = True,
           writes: tuple = ()) -> dict:
    """Run a macro while the screen keeps matching; hand off on the first miss."""
    values = dict(values or {})
    missing = [s for s in macro.get("slots") or [] if s not in values]
    if missing:
        return {"error": "give a value for every slot: missing " + ", ".join(missing)}
    if registry is None:
        from ..agent import build_registry
        registry = build_registry()
    if read is None:
        from ..runtime import screen

        def read():
            screen.context(serial=serial)
    goal = _fill(macro.get("goal") or "", values)
    rec = _start_record(macro, goal) if record else None
    # B2 for replay: tools run outside Agent.run here, so the write policy is
    # set up the same way it would be for an agent run - no budgeted write
    # unless named, and every ledger row tagged with this replay's run id.
    from ..policy import writes as wr
    wr.configure(mode="auto", writes=set(writes or ()),
                 run_id=rec.run_id if rec is not None else "replay")
    done: list[dict] = []
    stop: Optional[dict] = None
    t_start = time.time()

    def log(ev):
        if rec is not None:
            rec.event(ev)

    for k, step in enumerate(macro.get("steps") or []):
        n = k + 1
        if step.get("unreplayable"):
            stop = {"at": n, "why": step["unreplayable"]}
            break
        live, sim = None, None
        if step.get("pre"):
            deadline = time.time() + match_wait_s
            while True:
                live = live_screen(read)
                sim = similarity(step["pre"], live)
                if sim >= THRESHOLD or time.time() >= deadline:
                    break
                time.sleep(0.4)
            if sim < THRESHOLD:
                stop = {"at": n, "why": "screen does not match (%.2f < %.2f)"
                        % (sim, THRESHOLD), "live_pkg": live["pkg"],
                        "expected_pkg": step["pre"].get("pkg")}
                break
        args = _fill(dict(step.get("args") or {}), values)
        if step.get("target"):
            if live is None:
                live = live_screen(read)
            tgt = find_target(_fill(step["target"], values), live["visible"])
            if tgt is None:
                stop = {"at": n, "why": "the element it acted on is not on screen: "
                        + json.dumps(_fill(step["target"], values), ensure_ascii=False)[:120]}
                break
            args["ref"] = tgt["ref"]
        if live is not None:
            # The screen this step was matched against, as evidence for the
            # verifier and in the same shape as an agent run's decisions.
            last = state.last or {}
            log({"type": "decision", "step": n,
                 "screen": {"pkg": live["pkg"], "ver": last.get("ver") or ""},
                 "candidates": live["candidates"],
                 "screen_texts": [t for t in ((getattr(e, "text", "") or
                                               getattr(e, "desc", "")).strip()
                                              for e in last.get("elements") or []) if t][:200],
                 "chosen": {"tool": step["tool"], "args": args,
                            "target": step.get("target")},
                 "similarity": sim})
        log({"type": "tool_call", "step": n, "tool": step["tool"], "args": args,
             "replay": True, "similarity": sim})
        res = registry.call(step["tool"], args)
        ok = not (isinstance(res, dict) and "error" in res)
        log({"type": "tool_result", "step": n, "tool": step["tool"], "ok": ok,
             "result": res})
        done.append({"step": n, "tool": step["tool"], "similarity": sim, "ok": ok})
        if not ok:
            stop = {"at": n, "why": "the tool returned an error: "
                    + str(res.get("error"))[:160]}
            break

    out: dict[str, Any] = {"macro": macro.get("name"), "goal": goal,
                           "replayed": len([d for d in done if d["ok"]]),
                           "of": len(macro.get("steps") or []), "steps": done,
                           "seconds": round(time.time() - t_start, 1)}
    if stop:
        out["handoff"] = dict(stop, remaining=len(macro.get("steps") or []) - stop["at"] + 1)
    final = {"type": "final", "content": "" if stop else "replayed",
             "stopped_by": ("replay_mismatch: " + stop["why"][:80]) if stop else None,
             "steps": len(done), "seconds": out["seconds"]}
    log(final)
    if rec is not None:
        out["run_id"] = rec.run_id
        # A replay produces no answer, so only the macro's SCREEN checks apply;
        # an `answer` check would fail every replay by construction.
        spec = macro.get("verify") or {}
        screen_checks = [c for c in spec.get("checks") or [] if "answer" not in c]
        if screen_checks and not stop:
            from . import decisions, recorder, verify
            try:
                read()                  # the screen as the last step left it
            except Exception:
                pass
            fs = decisions.final_screen(time.time())
            rec.event(fs)
            sub = dict(spec, checks=screen_checks)
            v = verify.check_run(recorder.load(rec.run_id) + [fs], sub)
            out["verification"] = v["verdict"]
            rec.event(dict(verify.label_fields(dict(v, by="checks"), sub),
                           kind="label"))
        rec.close("replayed" if not stop else "handoff")

    if stop and agent_factory is not None:
        agent = agent_factory()
        so_far = ", ".join("%s" % d["tool"] for d in done if d["ok"]) or "nothing"
        handoff_goal = (goal + "\n\n(A replay of a verified routine did these "
                        "steps already: " + so_far + ". It stopped at step "
                        + str(stop["at"]) + " because " + stop["why"]
                        + ". Continue from the screen as it is now.)")
        out["agent_events"] = list(agent.run(handoff_goal))
        fin = next((e for e in reversed(out["agent_events"])
                    if e.get("type") == "final"), {})
        out["agent_final"] = {k: fin.get(k) for k in ("content", "stopped_by", "steps")}
    return out


def _start_record(macro: dict, goal: str):
    from . import recorder
    if not recorder.enabled():
        return None
    run_id = recorder._new_run_id()
    path = os.path.join(recorder.RUNS_DIR, run_id + ".jsonl")
    try:
        os.makedirs(recorder.RUNS_DIR, exist_ok=True)
        fh = open(path, "w", encoding="utf-8")
    except OSError:
        return None
    rec = recorder.RunRecorder(run_id, path, fh)
    rec._write({"kind": "meta", "format_version": recorder.VERSION,
                "run_id": run_id, "goal": goal, "provider": "replay",
                "model": "replay", "macro": macro.get("name"),
                "replay_of": macro.get("from_run"),
                "started": datetime.now().astimezone().isoformat(timespec="seconds"),
                "at": round(time.time(), 3)})
    return rec
