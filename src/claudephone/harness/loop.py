"""The agent loop.

Yields events as it goes rather than returning at the end, because the whole
point of running on the phone is that you can watch it work in real time - the
CLI prints these, and the HTTP server streams the same objects as NDJSON.

Three things here are not standard loop boilerplate and are worth knowing about:

* **History compaction.** Screen dumps are large and repetitive; ten of them in
  a row is mostly the same nav bar. Tool results older than `keep_full` steps
  are clipped to a stub. Without this a 30-step run on a cheap model either
  blows the context window or costs several times what it should.
* **A permission gate** sits in front of every call, so a tool marked dangerous
  (uninstalling apps, sending messages, factory-reset-adjacent shell) can be
  set to ask, allow or deny without touching tool code.
* **Budgets are enforced, not suggested.** Steps, wall-clock and token spend
  all terminate the run, and the reason is reported.
* **A checkpoint ends a run immediately** (`harness/handoff.py`). Doctrine is
  that a phone meeting a login, 2FA or "unusual activity" screen stops that
  account; the model is told the same in the prompt, but the harness is what
  enforces it, because a cheap model will keep tapping.
* **Stagnation ends a run early** (`harness/stagnation.py`). Repeating one call
  on a screen that does not change is the usual way a cheap model burns a
  budget; it gets one warning it can act on, then the run stops with
  `stopped_by="stagnation"` rather than `max_steps`.
* **Every run is written to disk** as it happens, by `harness/recorder.py`.
  Recording sits in `run()` so all three entry points get it for free, and it
  captures each event before compaction clips it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from . import handoff, recorder
from .models import Chat, ModelError, is_free_model, key_status
from .prompt import build_system
from .registry import ToolRegistry
from .stagnation import Stagnation

# Tool results this many steps back get clipped to a stub.
KEEP_FULL_RESULTS = 6
CLIP_TO = 220


@dataclass
class Budget:
    max_steps: int = 30
    max_seconds: float = 900.0
    max_tokens: int = 250_000
    # B5. Dollars as OpenRouter reports them (usage.cost), decider and helper
    # together; 0 turns the cap off. A $2 cap is what caught a runaway task in
    # agent-for-mobile - this default is an order of magnitude tighter because
    # this project's decider costs cents per run, not dollars.
    max_usd: float = 0.25
    # Free models are rationed per DAY (50 on a never-paid account). Stop with
    # this many left rather than on a 429 mid-task, so a person still has a few
    # requests for a doctor check or a question.
    free_reserve: int = 2

    def exceeded(self, steps: int, started: float, tokens: int,
                 usd: float = 0.0, free_left: Optional[int] = None) -> str:
        if steps >= self.max_steps:
            return "max_steps (" + str(self.max_steps) + ")"
        if time.time() - started > self.max_seconds:
            return "max_seconds (" + str(int(self.max_seconds)) + ")"
        if tokens > self.max_tokens:
            return "max_tokens (" + str(self.max_tokens) + ")"
        if self.max_usd and usd >= self.max_usd:
            return "max_usd ($%.4f of $%.2f)" % (usd, self.max_usd)
        if free_left is not None and free_left <= self.free_reserve:
            return "free_quota (" + str(free_left) + " free requests left today)"
        return ""


@dataclass
class Policy:
    """What the agent may do without a human."""
    mode: str = "auto"                       # auto | ask | readonly
    allow: set[str] = field(default_factory=set)
    deny: set[str] = field(default_factory=set)
    on_ask: Optional[Callable[[str, dict], bool]] = None
    # Account writes (B2, policy/writes.py). Empty = no budgeted write may run;
    # forbidden writes (likes, DMs, ...) need their rule id named here.
    writes: set[str] = field(default_factory=set)
    allow_rules: set[str] = field(default_factory=set)
    # Counted reads (B2b, policy/reads.py) refuse when no ledger is reachable;
    # True lets them run uncounted, flagged in every result.
    allow_uncounted_reads: bool = False

    def check(self, reg: ToolRegistry, name: str, args: dict) -> tuple[bool, str]:
        if name in self.deny:
            return False, "denied by policy"
        if name in self.allow:
            return True, ""
        tool = reg.tools.get(name)
        if tool is None:
            return True, ""                  # registry reports the real error
        if self.mode == "readonly" and tool.dangerous:
            return False, "readonly mode: " + name + " can change device state"
        if self.mode == "ask" and tool.dangerous:
            if self.on_ask is None:
                return False, "confirmation required but no prompt available"
            return (True, "") if self.on_ask(name, args) else (False,
                                                               "declined")
        return True, ""


def _clip(text: str, n: int = CLIP_TO) -> str:
    return text if len(text) <= n else text[:n] + " ...[" + str(
        len(text) - n) + " more chars clipped]"


class Agent:
    def __init__(self, chat: Chat, registry: ToolRegistry,
                 policy: Optional[Policy] = None,
                 budget: Optional[Budget] = None,
                 operator_notes: str = "",
                 stagnation: Optional[Stagnation] = None,
                 on_ask_operator=None,
                 helper: Optional[Chat] = None,
                 summarize: bool = False) -> None:
        self.chat = chat
        self.reg = registry
        self.policy = policy or Policy()
        self.budget = budget or Budget()
        self.operator_notes = operator_notes
        self.stagnation = stagnation if stagnation is not None else Stagnation()
        self.on_ask_operator = on_ask_operator
        # B5: the helper does side work so the decider's context and budget go
        # on decisions. Defaults to the decider when not configured separately.
        self.helper = helper
        self.summarize = summarize
        self._free_start: Optional[int] = None
        self.messages: list[dict] = []

    # -- spend (B5) ----------------------------------------------------------

    def spend_usd(self) -> float:
        seen, total = set(), 0.0
        for c in (self.chat, self.helper):
            if c is not None and id(c) not in seen:
                seen.add(id(c))
                total += getattr(c, "cost", 0.0) or 0.0
        return total

    def free_left(self) -> Optional[int]:
        """Free-model requests left today, counted locally from one /key read."""
        if self._free_start is None:
            return None
        seen, used = set(), 0
        for c in (self.chat, self.helper):
            if c is not None and id(c) not in seen:
                seen.add(id(c))
                used += getattr(c, "free_requests", 0)
        return self._free_start - used

    def _read_free_quota(self) -> dict:
        """One /key read at the start of a run, only if a free model is in play."""
        models = [c.cfg.model for c in (self.chat, self.helper)
                  if c is not None and getattr(c, "cfg", None) is not None]
        uses_free = any(is_free_model(m) for m in models)
        if not uses_free or getattr(self.chat.cfg, "provider", "") != "openrouter":
            self._free_start = None
            return {}
        st = key_status(self.chat.cfg)
        if st.get("ok") and isinstance(st.get("free_remaining"), int):
            self._free_start = st["free_remaining"]
        return st

    def accounting(self) -> dict:
        seen, requests, limited = set(), 0, 0
        for c in (self.chat, self.helper):
            if c is not None and id(c) not in seen:
                seen.add(id(c))
                requests += getattr(c, "requests", 0)
                limited += getattr(c, "rate_limited", 0)
        out = {"cost_usd": round(self.spend_usd(), 6), "requests": requests}
        if limited:
            out["rate_limited"] = limited
        left = self.free_left()
        if left is not None:
            out["free_requests_left"] = left
        return out

    # -- context -------------------------------------------------------------

    def _tool_lines(self) -> str:
        return "\n".join(
            "- " + s["function"]["name"] + "(" + ", ".join(
                (s["function"].get("parameters") or {}).get("properties", {})
            ) + ") - " + (s["function"].get("description") or "").split(". ")[0][:110]
            for s in self.reg.specs()
        )

    def _compact(self) -> None:
        """Clip old tool results in place. Keeps the shape of history intact."""
        seen = 0
        for msg in reversed(self.messages):
            if msg.get("role") != "tool":
                continue
            seen += 1
            if seen > KEEP_FULL_RESULTS and not msg.get("_clipped"):
                msg["content"] = _clip(msg["content"])
                msg["_clipped"] = True

    def _wire_messages(self) -> list[dict]:
        """History minus our private bookkeeping keys."""
        return [{k: v for k, v in m.items() if not k.startswith("_")}
                for m in self.messages]

    # -- the loop ------------------------------------------------------------

    def run(self, goal: str) -> Iterator[dict]:
        """Drive the goal to completion, recording the run as it happens.

        The recording wrapper lives here rather than in `cli.py` because every
        entry point - CLI, `POST /task`, and the laptop bridge behind it - comes
        through this generator. Hooking it here covers all of them and cannot be
        forgotten when a fourth is added.

        Events are recorded *as yielded*, which is before `_compact()` clips
        them, so the file keeps the screens the model actually saw rather than
        the stubs the conversation ends up holding.
        """
        rec = recorder.start(goal, self)
        handoff.configure(ask=self.on_ask_operator)
        from ..policy import writes as wr
        wr.configure(mode=self.policy.mode, writes=self.policy.writes,
                     allow_rules=self.policy.allow_rules,
                     run_id=rec.run_id if rec is not None else "",
                     allow_uncounted_reads=self.policy.allow_uncounted_reads)
        if rec is None:
            yield from self._accounted(goal)
            return

        outcome = "abandoned"
        try:
            for ev in self._accounted(goal):
                kind = ev.get("type")
                if kind == "start":
                    # Tell the consumer where this run is being written, so a
                    # CLI can print it and a streaming client can reference it.
                    ev = dict(ev, run_id=rec.run_id, run_path=rec.path)
                rec.event(ev)
                if kind == "final":
                    outcome = ev.get("stopped_by") or "completed"
                elif kind == "error":
                    outcome = "error"
                yield ev
        except GeneratorExit:
            # The consumer stopped reading - a disconnected HTTP client, or a
            # Ctrl-C in the CLI. That is abandonment, not a crash.
            raise
        except BaseException as e:
            outcome = "exception: " + type(e).__name__ + ": " + str(e)[:160]
            raise
        finally:
            rec.close(outcome)

    def _accounted(self, goal: str) -> Iterator[dict]:
        """_run, with spend attached to the final event and an optional summary."""
        trail: list[dict] = []
        for ev in self._run(goal):
            kind = ev.get("type")
            if kind == "tool_call":
                trail.append({"step": ev.get("step"), "tool": ev.get("tool"),
                              "args": ev.get("args")})
            if kind == "final":
                ev = dict(ev, **self.accounting())
                yield ev
                if self.summarize:
                    s = self._summary(goal, trail, ev)
                    if s:
                        yield s
                continue
            yield ev

    def _summary(self, goal: str, trail: list, final: dict) -> Optional[dict]:
        """One helper call: what happened, in three sentences. Opt-in (B5).

        This is the helper role's first job because it is the cheapest useful
        one - a single request per run, after the decider is done - and it is
        exactly the kind of side work the ablation found cheap models handle.
        """
        chat = self.helper or self.chat
        left = self.free_left()
        if left is not None and left <= self.budget.free_reserve:
            return {"type": "summary", "skipped": "free quota at reserve"}
        steps = "\n".join("%s. %s(%s)" % (t["step"], t["tool"],
                                          json.dumps(t["args"], default=str)[:80])
                          for t in trail[-25:])
        prompt = ("A phone agent was given this goal:\n" + goal + "\n\nIt made "
                  "these tool calls:\n" + (steps or "(none)") + "\n\nIt stopped "
                  "because: " + str(final.get("stopped_by") or "it said it was done")
                  + "\nIts last words: " + str(final.get("content") or "")[:600]
                  + "\n\nIn at most three sentences: did it achieve the goal, "
                  "what did it actually find, and if it failed, where. Say only "
                  "what the record supports.")
        try:
            r = chat.complete([{"role": "user", "content": prompt}])
        except ModelError as e:
            return {"type": "summary", "error": str(e)[:200]}
        return {"type": "summary", "model": chat.cfg.model,
                "role": getattr(chat.cfg, "role", ""),
                "content": (r.content or "").strip(),
                "cost_usd": round(self.spend_usd(), 6)}

    def _checkpoint(self) -> Optional[dict]:
        """Is a login / 2FA / challenge screen showing? Doctrine: stop here."""
        from .. import state
        last = getattr(state, "last", None) or {}
        return handoff.checkpoint_on_screen(last.get("elements") or [],
                                            last.get("pkg") or "")

    def _run(self, goal: str) -> Iterator[dict]:
        started = time.time()
        convention = self.chat.tool_convention
        self.messages = [
            {"role": "system",
             "content": build_system(convention, self._tool_lines(),
                                     self.operator_notes)},
            {"role": "user", "content": goal},
        ]
        quota = self._read_free_quota()
        yield {"type": "start", "goal": goal, "model": self.chat.cfg.model,
               "helper_model": (self.helper.cfg.model if self.helper is not None
                                else self.chat.cfg.model),
               "free_requests_left": self._free_start,
               "key_expires_at": quota.get("expires_at"),
               "provider": self.chat.cfg.provider,
               "tool_convention": convention,
               "tools_loaded": len(self.reg.specs()),
               "packs": sorted(self.reg.active_packs), "at": time.time()}

        steps = 0
        stag = self.stagnation
        while True:
            tokens = self.chat.total_usage.get("total_tokens", 0)
            stop = self.budget.exceeded(steps, started, tokens,
                                        usd=self.spend_usd(),
                                        free_left=self.free_left())
            if stop:
                yield {"type": "budget", "stopped_by": stop, "steps": steps}
                yield {"type": "final", "content": "",
                       "stopped_by": stop, "steps": steps,
                       "seconds": round(time.time() - started, 1),
                       "usage": dict(self.chat.total_usage)}
                return

            self._compact()
            native = self.chat.tool_convention == "native"
            try:
                reply = self.chat.complete(
                    self._wire_messages(),
                    tools=self.reg.specs() if native else None,
                )
            except ModelError as e:
                yield {"type": "error", "where": "model", "error": str(e)}
                return

            # The convention can flip on the first call; rebuild the prompt so
            # a json-mode model is actually told how to emit a call.
            if self.chat.tool_convention != convention:
                convention = self.chat.tool_convention
                self.messages[0]["content"] = build_system(
                    convention, self._tool_lines(), self.operator_notes)
                yield {"type": "note",
                       "message": "endpoint rejected native tools; "
                                  "switched to json protocol"}
                continue

            if reply.content:
                yield {"type": "thought", "content": reply.content,
                       "ms": reply.ms}

            if not reply.tool_calls:
                yield {"type": "final", "content": reply.content,
                       "steps": steps,
                       "seconds": round(time.time() - started, 1),
                       "usage": dict(self.chat.total_usage),
                       "finish_reason": reply.finish_reason}
                return

            assistant: dict[str, Any] = {"role": "assistant",
                                         "content": reply.content or None}
            if native:
                assistant["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c["args"])}}
                    for c in reply.tool_calls
                ]
            self.messages.append(assistant)

            for call in reply.tool_calls:
                steps += 1
                name, args = call["name"], call["args"]
                yield {"type": "tool_call", "step": steps, "tool": name,
                       "args": args}

                ok, why = self.policy.check(self.reg, name, args)
                fp_before = stag.before() if stag else ""
                result = ({"error": "blocked: " + why} if not ok
                          else self.reg.call(name, args))

                yield {"type": "tool_result", "step": steps, "tool": name,
                       "ok": "error" not in result,
                       "ms": result.get("_ms"), "result": result}

                advice = stag.observe(steps, name, args, fp_before) if stag else None

                # A checkpoint outranks everything, including whatever the model
                # meant to do next.
                cp = self._checkpoint()
                hand = result.get("_handoff") if isinstance(result, dict) else None
                if cp or (hand and hand.get("kind") == "stop"):
                    reason = (("checkpoint on screen: " + cp["why"]) if cp
                              else hand.get("reason", ""))
                    payload = {"type": "final",
                               "content": reason,
                               "stopped_by": "human_required",
                               "steps": steps,
                               "handoff": cp or hand,
                               "seconds": round(time.time() - started, 1),
                               "usage": dict(self.chat.total_usage)}
                    yield {"type": "note", "step": steps,
                           "reason": "checkpoint" if cp else "human_required",
                           "message": reason}
                    yield payload
                    return
                if hand and hand.get("kind") == "answered":
                    yield {"type": "note", "step": steps, "reason": "operator",
                           "message": "operator answered: "
                                      + str(hand.get("answer"))[:200]}

                payload = json.dumps(result, default=str)
                if native:
                    self.messages.append({"role": "tool",
                                          "tool_call_id": call["id"],
                                          "name": name, "content": payload})
                else:
                    self.messages.append({
                        "role": "user",
                        "content": "Result of " + name + ":\n" + payload})
                    # json mode: one call per turn, by protocol

                if advice:
                    yield {"type": "note", "step": steps,
                           "reason": advice["reason"],
                           "message": advice["message"]}
                    if advice.get("stop"):
                        yield {"type": "final", "content": advice["message"],
                               "stopped_by": "stagnation", "steps": steps,
                               "stagnation": {k: v for k, v in advice.items()
                                              if k != "stop"},
                               "seconds": round(time.time() - started, 1),
                               "usage": dict(self.chat.total_usage)}
                        return
                    # The model has to SEE the warning, so it goes into the
                    # conversation, not just the event stream.
                    self.messages.append({"role": "user",
                                          "content": advice["message"]})
                    break
