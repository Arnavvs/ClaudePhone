"""Write every run to disk as it happens.

The loop already yields a complete, well-shaped account of a run - the goal, the
model's reasoning, each tool call with its arguments, each result with its
timing, the budget outcome and the token spend. Until this module existed all
three consumers threw it away: the CLI printed it, the HTTP server streamed it
to a client, the MCP bridge returned it to a laptop model. Nothing was kept.

That is the wrong default for a project whose whole point is an agent driving a
real device. The first live runs are the most informative ones there will ever
be, and they only happen once.

## Where it hooks in, and why there

Recording happens in `Agent.run`, not in `cli.py`. Every entry point - the CLI,
`POST /task`, and therefore the laptop bridge too - goes through that one
generator, so hooking it there captures all of them for free and cannot be
forgotten when a fourth entry point is added.

It records the event **as yielded**, before `_compact()` has touched anything.
That matters more than it looks: compaction turns tool results older than six
steps into a one-line capsule (B7), so by the end of a 30-step run the
conversation no longer contains the screens the model was actually looking at
when it chose. Those screens are the input half of every training example. The
recorder keeps them whole.

## Rules

* **Never break a run.** Every filesystem operation is guarded. A recorder that
  can kill a live agent driving a real phone is worse than no recorder, so a
  failure disables recording for the rest of the run and is reported once, in
  the file's own `end` record if it can still be written, and never raised.
* **Flush every line.** Agent runs get interrupted - Ctrl-C, a client
  disconnecting, a budget stop. A buffered writer loses exactly the run you most
  wanted to look at.
* **One line per event, unmodified.** The stream on disk is the stream the
  consumer saw, plus `seq` and `at`. No reshaping, so a later reader never has
  to guess what was dropped. The file holds more than the stream, never less:
  per-step `decision` records, a `final_screen` record and automatic `label`
  rows (B8) are written here and not streamed.

## Format

JSONL. First line `kind: "meta"`, then one line per event, then `kind: "end"`.
The `kind` key matches `tools/trace_human.py`, which records the same shape for
a *human* driving the phone - the two are meant to be read by the same code.

    artifacts/runs/20260905-213700-a3f2.jsonl

## Privacy

Runs are recorded in full, including tool results from `phone_sms_list`,
`phone_contacts`, `phone_call_log` and `clipboard_get`. That is the intended
behaviour on a dedicated handset, but it means `artifacts/runs/` holds real
personal data and is not something to hand around. It is gitignored along with
the rest of `artifacts/`.

Set `CLAUDEPHONE_RECORD=0` to turn recording off entirely.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Optional

from .. import state

VERSION = "1"

# Every run lands here unless told otherwise. Under artifacts/, which .gitignore
# already excludes - runs contain real screen contents and must not be committed.
RUNS_DIR = os.environ.get(
    "CLAUDEPHONE_RUNS_DIR", os.path.join(state.ARTIFACT_DIR, "runs"))


def enabled() -> bool:
    return os.environ.get("CLAUDEPHONE_RECORD", "1") not in ("0", "false", "no")


def _new_run_id() -> str:
    return (datetime.now().strftime("%Y%m%d-%H%M%S") + "-"
            + os.urandom(2).hex())


def _device_facts() -> dict:
    """Best-effort device identification. Never blocks a run from starting.

    Worth the one adb round trip: a trace whose device is unknown cannot be
    compared against another phone later, and screen size is needed to make
    sense of any coordinate in the run.
    """
    from .. import device as dev
    out: dict[str, Any] = {}
    try:
        out["on_device"] = dev.on_device()
        out["serial"] = dev.default_serial()
    except Exception:
        return out
    try:
        info = dev.device_info()
        out.update(model=info.model, android=info.android, screen=info.screen)
    except Exception:
        out["device_info"] = "unavailable"
    return out


class RunRecorder:
    """An open run file. Created by `start()`; never constructed directly."""

    def __init__(self, run_id: str, path: str, fh) -> None:
        self.run_id = run_id
        self.path = path
        self._fh = fh
        self._seq = 0
        self._started = time.time()
        self._error = ""

    # -- writing -------------------------------------------------------------

    def _write(self, obj: dict) -> None:
        """Append one JSON line. Swallows and latches any failure."""
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(obj, default=str, ensure_ascii=False)
                           + "\n")
            self._fh.flush()
        except Exception as e:
            # Disable rather than retry: a full disk or a vanished directory
            # will not fix itself mid-run, and a per-event failure would print
            # once per step for the rest of the run.
            self._error = type(e).__name__ + ": " + str(e)[:200]
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def event(self, ev: dict) -> None:
        self._seq += 1
        self._write(dict(ev, seq=self._seq, at=round(time.time(), 3)))

    def close(self, outcome: str = "") -> None:
        if self._fh is None:
            return
        self._write({"kind": "end", "outcome": outcome or "unknown",
                     "events": self._seq,
                     "seconds": round(time.time() - self._started, 2),
                     "at": round(time.time(), 3)})
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None

    @property
    def failed(self) -> str:
        return self._error


def start(goal: str, agent) -> Optional[RunRecorder]:
    """Open a run file and write its meta line. None if disabled or unwritable.

    Returning None rather than raising is deliberate - `Agent.run` treats a
    missing recorder as "do not record", so a read-only filesystem degrades a
    run to unrecorded instead of failing it.
    """
    if not enabled():
        return None
    run_id = _new_run_id()
    path = os.path.join(RUNS_DIR, run_id + ".jsonl")
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        fh = open(path, "w", encoding="utf-8")
    except Exception:
        return None

    rec = RunRecorder(run_id, path, fh)
    cfg = agent.chat.cfg
    pol = agent.policy
    bud = agent.budget
    rec._write({
        "kind": "meta",
        "format_version": VERSION,
        "run_id": run_id,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "goal": goal,
        "provider": cfg.provider,
        "model": cfg.model,
        "tool_mode": cfg.tool_mode,
        "tool_convention": agent.chat.tool_convention,
        "temperature": cfg.temperature,
        "policy": {"mode": pol.mode, "allow": sorted(pol.allow),
                   "deny": sorted(pol.deny)},
        "budget": {"max_steps": bud.max_steps, "max_seconds": bud.max_seconds,
                   "max_tokens": bud.max_tokens},
        "packs": sorted(agent.reg.active_packs),
        "tools_loaded": len(agent.reg.specs()),
        "tools_registered": len(agent.reg.tools),
        "operator_notes": agent.operator_notes or "",
        "device": _device_facts(),
        "at": round(time.time(), 3),
    })
    return rec


# --------------------------------------------------------------------------
# reading back
# --------------------------------------------------------------------------

def run_path(run_id: str) -> str:
    return os.path.join(RUNS_DIR, run_id + ".jsonl")


def load(run_id: str) -> list[dict]:
    """Every record in a run, in order. Tolerates a truncated final line.

    A run killed mid-write leaves a partial last line. That is normal - the
    interesting runs are often the ones that were interrupted - so a bad tail is
    skipped rather than treated as a corrupt file.
    """
    rows: list[dict] = []
    try:
        with open(run_path(run_id), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def summarise(run_id: str) -> dict:
    """One row per run for listing: goal, model, steps, outcome, cost."""
    rows = load(run_id)
    if not rows:
        return {"run_id": run_id, "error": "unreadable or empty"}
    meta = rows[0] if rows[0].get("kind") == "meta" else {}
    end = rows[-1] if rows[-1].get("kind") == "end" else {}
    final = next((r for r in reversed(rows) if r.get("type") == "final"), {})
    calls = [r for r in rows if r.get("type") == "tool_call"]
    results = [r for r in rows if r.get("type") == "tool_result"]
    return {
        "run_id": run_id,
        "started": meta.get("started"),
        "goal": meta.get("goal"),
        "model": meta.get("model"),
        "provider": meta.get("provider"),
        "steps": len(calls),
        "failed_steps": sum(1 for r in results if not r.get("ok")),
        "tools": [r.get("tool") for r in calls],
        "outcome": end.get("outcome") or ("final" if final else "unknown"),
        "stopped_by": final.get("stopped_by"),
        "seconds": final.get("seconds") or end.get("seconds"),
        "tokens": (final.get("usage") or {}).get("total_tokens"),
        "answer": final.get("content"),
        "labels": [r for r in rows if r.get("kind") == "label"],
        # B8: the newest automatic verdict, if the run was verified.
        "verdict": next((r.get("verdict") for r in reversed(rows)
                         if r.get("kind") == "label" and r.get("by") == "verify"),
                        None),
    }


def list_runs(limit: int = 25) -> list[str]:
    """Run ids, newest first. The id sorts chronologically by construction."""
    try:
        names = [f[:-6] for f in os.listdir(RUNS_DIR) if f.endswith(".jsonl")]
    except OSError:
        return []
    return sorted(names, reverse=True)[:limit]


def label(run_id: str, **fields) -> dict:
    """Append a judgement about a finished run.

    The recorder can say what happened but not whether it was any good - there
    is no ground truth for "did this achieve the goal". So the outcome label is
    appended afterwards, by whoever watched the run:

        recorder.label("20260905-213700-a3f2", success=True,
                       note="found the caption, took two extra dumps")

    Appending rather than rewriting keeps the run file immutable and lets a run
    carry several judgements - yours now, a reviewer's later.
    """
    path = run_path(run_id)
    if not os.path.isfile(path):
        return {"error": "no such run: " + run_id}
    row = dict(fields, kind="label",
               at=round(time.time(), 3),
               labelled=datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    except OSError as e:
        return {"error": str(e)[:200]}
    return {"run_id": run_id, "labelled": row}
