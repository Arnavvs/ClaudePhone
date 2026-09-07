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
* **Every run is written to disk** as it happens, by `harness/recorder.py`.
  Recording sits in `run()` so all three entry points get it for free, and it
  captures each event before compaction clips it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from . import recorder
from .models import Chat, ModelError
from .prompt import build_system
from .registry import ToolRegistry

# Tool results this many steps back get clipped to a stub.
KEEP_FULL_RESULTS = 6
CLIP_TO = 220


@dataclass
class Budget:
    max_steps: int = 30
    max_seconds: float = 900.0
    max_tokens: int = 250_000

    def exceeded(self, steps: int, started: float, tokens: int) -> str:
        if steps >= self.max_steps:
            return "max_steps (" + str(self.max_steps) + ")"
        if time.time() - started > self.max_seconds:
            return "max_seconds (" + str(int(self.max_seconds)) + ")"
        if tokens > self.max_tokens:
            return "max_tokens (" + str(self.max_tokens) + ")"
        return ""


@dataclass
class Policy:
    """What the agent may do without a human."""
    mode: str = "auto"                       # auto | ask | readonly
    allow: set[str] = field(default_factory=set)
    deny: set[str] = field(default_factory=set)
    on_ask: Optional[Callable[[str, dict], bool]] = None

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
                 operator_notes: str = "") -> None:
        self.chat = chat
        self.reg = registry
        self.policy = policy or Policy()
        self.budget = budget or Budget()
        self.operator_notes = operator_notes
        self.messages: list[dict] = []

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
        if rec is None:
            yield from self._run(goal)
            return

        outcome = "abandoned"
        try:
            for ev in self._run(goal):
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

    def _run(self, goal: str) -> Iterator[dict]:
        started = time.time()
        convention = self.chat.tool_convention
        self.messages = [
            {"role": "system",
             "content": build_system(convention, self._tool_lines(),
                                     self.operator_notes)},
            {"role": "user", "content": goal},
        ]
        yield {"type": "start", "goal": goal, "model": self.chat.cfg.model,
               "provider": self.chat.cfg.provider,
               "tool_convention": convention,
               "tools_loaded": len(self.reg.specs()),
               "packs": sorted(self.reg.active_packs), "at": time.time()}

        steps = 0
        while True:
            tokens = self.chat.total_usage.get("total_tokens", 0)
            stop = self.budget.exceeded(steps, started, tokens)
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
                result = ({"error": "blocked: " + why} if not ok
                          else self.reg.call(name, args))

                yield {"type": "tool_result", "step": steps, "tool": name,
                       "ok": "error" not in result,
                       "ms": result.get("_ms"), "result": result}

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
                    break
