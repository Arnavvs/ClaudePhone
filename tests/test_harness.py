"""Harness tests that need no phone and no API key.

The point is to exercise the parts that are easy to get subtly wrong - schema
generation, the json tool-call fallback, compaction, budgets and the permission
gate - without depending on a device being attached.

    python tests/test_harness.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from claudephone.harness.loop import Agent, Budget, Policy  # noqa: E402
from claudephone.harness.models import (Chat, ModelConfig, Reply,  # noqa: E402
                                        parse_json_tool_call)
from claudephone.harness.registry import ToolRegistry  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + label)
    else:
        FAIL += 1
        print("  FAIL " + label + ("  -> " + detail if detail else ""))


class FakeChat(Chat):
    """A Chat that replays a scripted list of replies."""

    def __init__(self, replies, convention="native"):
        super().__init__(ModelConfig(provider="local", model="fake"))
        self._replies = list(replies)
        self._native_ok = convention == "native"
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.seen.append(messages)
        self.total_usage["total_tokens"] = (
            self.total_usage.get("total_tokens", 0) + 100)
        return self._replies.pop(0) if self._replies else Reply(content="done")


def demo_registry() -> ToolRegistry:
    reg = ToolRegistry()

    with reg.pack("core"):
        @reg.tool(description="Echo a value back.")
        def echo(value: str, times: int = 1) -> dict:
            return {"echoed": value * times}

        @reg.tool(description="Always explodes.")
        def boom() -> dict:
            raise ValueError("intentional")

    with reg.pack("risky", dangerous=True):
        @reg.tool(description="Pretends to wipe something.")
        def wipe(target: str) -> dict:
            return {"wiped": target}

    return reg


def test_schema():
    print("\nschema generation")
    reg = demo_registry()
    s = reg.tools["echo"].schema
    check("required arg detected", s.get("required") == ["value"], json.dumps(s))
    check("default captured", s["properties"]["times"].get("default") == 1)
    check("int mapped", s["properties"]["times"]["type"] == "integer")
    check("str mapped", s["properties"]["value"]["type"] == "string")
    check("no-arg tool has empty props", reg.tools["boom"].schema
          .get("properties") == {})


def test_call():
    print("\ntool execution")
    reg = demo_registry()
    r = reg.call("echo", {"value": "ab", "times": 2})
    check("returns result", r.get("echoed") == "abab", str(r))
    check("timing recorded", isinstance(r.get("_ms"), int))

    r = reg.call("echo", {"value": "x", "bogus": 1})
    check("unknown arg dropped, not fatal", r.get("echoed") == "x")
    check("dropped arg reported", r.get("_ignored_args") == ["bogus"])

    r = reg.call("boom", {})
    check("exception becomes error dict", "error" in r and "intentional" in r["error"])

    r = reg.call("nope", {})
    check("unknown tool is an error", "error" in r)

    r = reg.call("echo", {})
    check("missing required arg explained", "error" in r and "expected" in r)


def test_packs():
    print("\npacks")
    reg = demo_registry()
    check("only core visible", len(reg.specs()) == 2, str(len(reg.specs())))
    check("risky hidden", all(s["function"]["name"] != "wipe"
                              for s in reg.specs()))
    reg.active_packs.add("risky")
    check("risky visible once loaded", len(reg.specs()) == 3)
    check("search finds unloaded", any(h["name"] == "wipe"
                                       for h in demo_registry().search("wipe")))


def test_json_protocol():
    print("\njson tool-call parsing")
    prose, calls = parse_json_tool_call(
        'I will look.\n```json\n{"tool": "ui_dump", "args": {"limit": 5}}\n```')
    check("fenced block parsed", calls and calls[0]["name"] == "ui_dump")
    check("args parsed", calls[0]["args"] == {"limit": 5})
    check("prose preserved", prose.startswith("I will look"))

    _, calls = parse_json_tool_call('{"tool": "tap", "arguments": {"i": 3}}')
    check("bare object + 'arguments' alias", calls and calls[0]["args"] == {"i": 3})

    _, calls = parse_json_tool_call("no tool call here")
    check("plain prose yields nothing", calls == [])

    _, calls = parse_json_tool_call('```json\n{"broken": ,,,}\n```')
    check("malformed json does not raise", calls == [])


def test_loop():
    print("\nagent loop")
    reg = demo_registry()
    chat = FakeChat([
        Reply(content="looking", tool_calls=[
            {"id": "c1", "name": "echo", "args": {"value": "hi"}}]),
        Reply(content="all done"),
    ])
    events = list(Agent(chat, reg).run("say hi"))
    kinds = [e["type"] for e in events]
    check("emits start", kinds[0] == "start")
    check("emits tool_call", "tool_call" in kinds)
    check("emits tool_result", "tool_result" in kinds)
    check("ends with final", kinds[-1] == "final")
    final = events[-1]
    check("final carries answer", final.get("content") == "all done")
    check("final counts steps", final.get("steps") == 1, str(final))
    result = [e for e in events if e["type"] == "tool_result"][0]
    check("result marked ok", result["ok"] is True)


def test_budget():
    print("\nbudgets")
    reg = demo_registry()
    looping = [Reply(content="again", tool_calls=[
        {"id": "c", "name": "echo", "args": {"value": "x"}}]) for _ in range(20)]
    chat = FakeChat(looping)
    events = list(Agent(chat, reg, budget=Budget(max_steps=3)).run("loop"))
    check("budget event emitted", any(e["type"] == "budget" for e in events))
    final = events[-1]
    check("stopped_by reported", "max_steps" in (final.get("stopped_by") or ""))
    calls = [e for e in events if e["type"] == "tool_call"]
    check("stopped at the limit", len(calls) == 3, str(len(calls)))


def test_policy():
    print("\npermission gate")
    reg = demo_registry()
    reg.active_packs.add("risky")

    def one_wipe():
        return [Reply(content="", tool_calls=[
            {"id": "c", "name": "wipe", "args": {"target": "everything"}}]),
            Reply(content="stopped")]

    events = list(Agent(FakeChat(one_wipe()), reg,
                        policy=Policy(mode="readonly")).run("wipe it"))
    res = [e for e in events if e["type"] == "tool_result"][0]["result"]
    check("readonly blocks dangerous tool", "error" in res and "readonly" in res["error"],
          str(res))

    events = list(Agent(FakeChat(one_wipe()), reg,
                        policy=Policy(mode="auto")).run("wipe it"))
    res = [e for e in events if e["type"] == "tool_result"][0]["result"]
    check("auto permits it", res.get("wiped") == "everything", str(res))

    events = list(Agent(FakeChat(one_wipe()), reg,
                        policy=Policy(mode="ask",
                                      on_ask=lambda n, a: False)).run("wipe it"))
    res = [e for e in events if e["type"] == "tool_result"][0]["result"]
    check("ask honours a refusal", "error" in res and "declined" in res["error"])


def test_compaction():
    print("\nhistory compaction")
    reg = demo_registry()
    replies = [Reply(content="", tool_calls=[
        {"id": "c" + str(i), "name": "echo",
         "args": {"value": "y" * 500}}]) for i in range(9)]
    replies.append(Reply(content="fin"))
    agent = Agent(FakeChat(replies), reg, budget=Budget(max_steps=20))
    list(agent.run("spam"))
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    check("all tool results retained in shape", len(tool_msgs) == 9,
          str(len(tool_msgs)))
    clipped = [m for m in tool_msgs if m.get("_clipped")]
    check("older results clipped", len(clipped) >= 2, str(len(clipped)))
    check("newest kept whole", not tool_msgs[-1].get("_clipped"))
    check("clip marker present", all("clipped]" in m["content"] for m in clipped))
    check("private keys stripped from wire",
          all("_clipped" not in m for m in agent._wire_messages()))


def test_real_registry():
    print("\nreal registry")
    from claudephone.agent import OUTBOUND, build_registry
    reg = build_registry()
    check("tools registered", len(reg.tools) > 100, str(len(reg.tools)))
    # core carries the compound-action pack now, which is deliberate: those are
    # the tools the agent should reach for by default. The ceiling still matters
    # though - core is what every turn pays for - so keep it honest.
    core_specs = len(reg.specs())
    check("core stays small", core_specs <= 35, str(core_specs))
    import json
    core_tokens = len(json.dumps(reg.specs({"core"}))) // 4
    check("core schema under 4k tokens", core_tokens < 4000, str(core_tokens))
    all_tokens = len(json.dumps(reg.specs(set(reg.packs())))) // 4
    check("packs still save >3x", all_tokens / max(core_tokens, 1) > 3.0,
          "%d vs %d" % (all_tokens, core_tokens))
    check("every tool has a description",
          all(t.description.strip() for t in reg.tools.values()))
    check("every tool has a schema",
          all(t.schema.get("type") == "object" for t in reg.tools.values()))
    check("outbound tools exist", all(n in reg.tools for n in OUTBOUND))
    from claudephone.agent import build_policy
    pol = build_policy("auto")
    for n in OUTBOUND:
        ok, why = pol.check(reg, n, {})
        check(n + " denied by default", not ok, why)
    pol = build_policy("auto", allow=["phone_sms_send"])
    ok, _ = pol.check(reg, "phone_sms_send", {})
    check("explicit allow overrides", ok)


if __name__ == "__main__":
    for fn in (test_schema, test_call, test_packs, test_json_protocol,
               test_loop, test_budget, test_policy, test_compaction,
               test_real_registry):
        fn()
    print("\n" + str(PASS) + " passed, " + str(FAIL) + " failed")
    raise SystemExit(1 if FAIL else 0)
