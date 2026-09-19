"""Automatic run verification: milestone checks, a model only when unsure (B8).

`recorder.label(success=...)` was the only success signal, and it was manual,
so almost no run had one. Replay (B9) and any learned verifier later need runs
labelled reliably, and a person will not label hundreds of them.

A task declares what success looks like, in checks a program can decide:

    {"checks": [
        {"answer": "\\b10\\s*min"},                 # regex on the final answer
        {"reached_text": "Screen timeout"},         # seen on any screen read
        {"final_text": "10 minutes"},               # on the last screen read
        {"package": "com.android.settings"},        # the last screen's app
        {"reached_package": "com.instagram.android"},
        {"field": "followers"},                     # non-null in some tool result
        {"milestones": ["Display", "Screen timeout"]}  # reached in this order
    ]}

Each check comes back `pass`, `fail` or `inconclusive` with the step that
decided it. The run is `pass` only if every check passes, `fail` if any check
fails, otherwise `inconclusive`. Only an inconclusive run is handed to a model
(`judge=`), and only when asked for - MobiFlow's escalating checkers. The
model gets the record, not the phone, and may answer `unsure`.

Text on screen is taken from screen reads only - element texts in tool
results, the `appeared` lists of decision records, and the final screen - never
from a tool's own words. A `find` that says "no element matching 'Screen
timeout'" must not count as having reached it.

Labels are appended to the run file (`kind: "label"`, `by: "verify"`), next
to any human label, never replacing one.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

FINAL_STALE_S = 120.0


def _texts_in(obj: Any, out: list) -> None:
    """Element texts in a tool result: values under `elements`-like lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("elements", "items", "matches", "visible") and isinstance(v, list):
                for e in v:
                    if isinstance(e, dict):
                        for f in ("text", "desc", "t", "d"):
                            s = e.get(f)
                            if isinstance(s, str) and s.strip():
                                out.append(s.strip())
                    elif isinstance(e, str) and e.strip():
                        out.append(e.strip())
            elif k == "appeared" and isinstance(v, list):
                for e in v:
                    s = e.get("value") if isinstance(e, dict) else e
                    if isinstance(s, str) and s.strip():
                        out.append(s.strip())
            else:
                _texts_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _texts_in(v, out)


def screens(rows: list) -> list[tuple[int, str]]:
    """(step, text) for every piece of text seen on a screen, in order."""
    seen: list[tuple[int, str]] = []
    for r in rows:
        if r.get("type") == "tool_result":
            texts: list = []
            _texts_in(r.get("result"), texts)
            seen += [(r.get("step") or 0, t) for t in texts]
        elif r.get("type") == "decision":
            seen += [(r.get("step") or 0, t)
                     for t in (r.get("outcome") or {}).get("appeared") or []]
    return seen


def _field(obj: Any, name: str) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == name and v not in (None, "", [], {}):
                return True
            if _field(v, name):
                return True
    elif isinstance(obj, list):
        return any(_field(v, name) for v in obj)
    return False


def _rx(p: str):
    return re.compile(p, re.I)


def check_run(rows: list, spec: dict) -> dict:
    final = next((r for r in reversed(rows) if r.get("type") == "final"), {})
    fscreen = next((r for r in reversed(rows) if r.get("kind") == "final_screen"), None)
    seen = screens(rows)
    if fscreen:
        last_step = max([r.get("step") or 0 for r in rows
                         if r.get("type") == "tool_result"] or [0])
        seen += [(last_step, t) for t in fscreen.get("texts") or []]
    results: list[dict] = []

    def add(check, status, why, step=None):
        d = {"check": check, "status": status, "why": why}
        if step is not None:
            d["step"] = step
        results.append(d)

    def final_ok() -> Optional[str]:
        if fscreen is None:
            return "no final_screen record (a run from before B8)"
        age = fscreen.get("age_s")
        if age is None:
            return "no screen was read in this run"
        if age > FINAL_STALE_S:
            return "the last screen read was %ds before the end" % age
        return None

    for c in spec.get("checks") or []:
        if "answer" in c:
            ans = str(final.get("content") or "")
            if not ans.strip():
                add(c, "fail", "no answer (stopped_by=%s)" % final.get("stopped_by"))
            elif _rx(c["answer"]).search(ans):
                add(c, "pass", "answer matches")
            else:
                add(c, "fail", "answer does not match: " + ans[:120])
        elif "reached_text" in c:
            rx = _rx(c["reached_text"])
            hit = next(((s, t) for s, t in seen if rx.search(t)), None)
            add(c, "pass" if hit else "fail",
                ("seen: " + hit[1][:80]) if hit else "never on a screen read",
                hit[0] if hit else None)
        elif "final_text" in c:
            why = final_ok()
            if why:
                add(c, "inconclusive", why)
            else:
                rx = _rx(c["final_text"])
                hit = next((t for t in fscreen["texts"] if rx.search(t)), None)
                add(c, "pass" if hit else "fail",
                    ("on the final screen: " + hit[:80]) if hit
                    else "not on the final screen")
        elif "package" in c:
            why = final_ok()
            if why:
                add(c, "inconclusive", why)
            else:
                p = fscreen.get("pkg")
                add(c, "pass" if p == c["package"] else "fail", "final app: " + str(p))
        elif "reached_package" in c:
            pk = [r.get("step") for r in rows if r.get("type") == "decision"
                  and (r.get("outcome") or {}).get("pkg") == c["reached_package"]]
            if fscreen and fscreen.get("pkg") == c["reached_package"]:
                pk.append(None)
            if pk:
                add(c, "pass", "reached", pk[0])
            elif not any(r.get("type") == "decision" for r in rows) and fscreen is None:
                add(c, "inconclusive", "no decision records (a run from before B8)")
            else:
                add(c, "fail", "never in the foreground on a screen read")
        elif "field" in c:
            step = next((r.get("step") for r in rows if r.get("type") == "tool_result"
                         and _field(r.get("result"), c["field"])), None)
            add(c, "pass" if step else "fail",
                "non-null" if step else "never non-null in a tool result", step)
        elif "milestones" in c:
            at, reached = 0, []
            for m in c["milestones"]:
                rx = _rx(m)
                s = next((s for s, t in seen if s >= at and rx.search(t)), None)
                if s is None:
                    break
                reached.append((m, s))
                at = s
            ok = len(reached) == len(c["milestones"])
            add(c, "pass" if ok else "fail",
                "reached %d of %d%s" % (len(reached), len(c["milestones"]),
                                        "" if ok else "; next: " +
                                        c["milestones"][len(reached)]),
                reached[-1][1] if reached else None)
        else:
            add(c, "inconclusive", "unknown check")

    st = [r["status"] for r in results]
    verdict = ("inconclusive" if not st else "fail" if "fail" in st
               else "inconclusive" if "inconclusive" in st else "pass")
    return {"verdict": verdict, "checks": results}


def judge(rows: list, spec: dict, checked: dict, chat) -> dict:
    """One model call for an inconclusive run. Sees the record, not the phone."""
    meta = rows[0] if rows and rows[0].get("kind") == "meta" else {}
    final = next((r for r in reversed(rows) if r.get("type") == "final"), {})
    fscreen = next((r for r in reversed(rows) if r.get("kind") == "final_screen"), {})
    calls = [r for r in rows if r.get("type") == "tool_call"][-20:]
    steps = "\n".join("%s. %s(%s)" % (r.get("step"), r.get("tool"),
                                      json.dumps(r.get("args"), default=str)[:80])
                      for r in calls)
    prompt = (
        "You are checking whether a phone agent achieved its goal, from its "
        "record alone.\n\nGoal: " + str(meta.get("goal") or "") +
        "\n\nWhat success requires: " + json.dumps(spec.get("checks"), default=str) +
        "\n\nAutomatic checks: " + json.dumps(checked["checks"], default=str)[:1500] +
        "\n\nTool calls:\n" + (steps or "(none)") +
        "\n\nFinal answer: " + str(final.get("content") or "(none)")[:600] +
        "\n\nText on the last screen read: " +
        json.dumps((fscreen.get("texts") or [])[:80], ensure_ascii=False)[:2000] +
        "\n\nReply with JSON only: {\"verdict\": \"pass\" | \"fail\" | \"unsure\", "
        "\"why\": \"one sentence citing the record\"}. Say unsure unless the "
        "record shows it.")
    try:
        r = chat.complete([{"role": "user", "content": prompt}])
    except Exception as e:                        # ModelError, network
        return {"verdict": "inconclusive", "error": str(e)[:200]}
    text = (r.content or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except ValueError:
        out = {}
    v = str(out.get("verdict") or "").lower()
    return {"verdict": v if v in ("pass", "fail") else "inconclusive",
            "why": str(out.get("why") or text)[:300],
            "model": getattr(getattr(chat, "cfg", None), "model", "")}


def verify(rows: list, spec: dict, chat=None) -> dict:
    """Checks, then - only if inconclusive and a chat is given - one model call."""
    out = check_run(rows, spec)
    out["by"] = "checks"
    if out["verdict"] == "inconclusive" and chat is not None:
        j = judge(rows, spec, out, chat)
        out["judge"] = j
        if j["verdict"] in ("pass", "fail"):
            out["verdict"] = j["verdict"]
            out["by"] = "judge"
    return out


def label_fields(result: dict, spec: dict) -> dict:
    """What goes into the run file's label row."""
    return {"by": "verify", "success": {"pass": True, "fail": False}.get(
                result["verdict"]),
            "verdict": result["verdict"], "decided_by": result.get("by"),
            "checks": result["checks"], "judge": result.get("judge"),
            "spec": spec}
