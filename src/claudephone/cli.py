"""Command line: `claudephone run | serve | tools | tool | doctor | mcp`.

The event printer is not decoration. When an agent is driving a phone you are
holding, the useful question is "what is it about to touch" - so each tool call
prints before it runs, with its arguments, and its result prints as a single
line summary rather than a wall of JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import device as dev

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
BLUE, GREEN, RED, YELLOW, CYAN = ("\033[34m", "\033[32m", "\033[31m",
                                  "\033[33m", "\033[36m")


def _colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, col: str) -> str:
    return (col + text + RESET) if _colour() else text


def _summarise(result: dict, width: int = 150) -> str:
    """One line that says what actually came back."""
    if not isinstance(result, dict):
        return str(result)[:width]
    if "error" in result:
        return c("error: " + str(result["error"])[:width], RED)
    bits = []
    for key in ("screen", "package", "total_elements", "returned", "count",
                "matches", "launched", "path", "text", "battery", "location",
                "stdout", "content"):
        if key in result and result[key] not in (None, "", [], {}):
            val = result[key]
            if isinstance(val, (list, dict)):
                val = "<" + str(len(val)) + " items>"
            bits.append(key + "=" + str(val)[:60].replace("\n", " "))
    if not bits:
        bits = [k + "=" + str(v)[:40] for k, v in list(result.items())[:3]
                if not k.startswith("_")]
    return "  ".join(bits)[:width] or "ok"


def print_event(ev: dict) -> None:
    kind = ev.get("type")
    if kind == "start":
        print(c("▸ " + ev.get("goal", ""), BOLD))
        print(c("  " + ev.get("provider", "") + " / " + ev.get("model", "")
                + "  ·  " + str(ev.get("tools_loaded")) + " tools ("
                + ", ".join(ev.get("packs", [])) + ")  ·  "
                + ev.get("tool_convention", "") + " tool calls", DIM))
        if ev.get("run_id"):
            print(c("  recording " + ev["run_id"], DIM))
    elif kind == "thought":
        text = (ev.get("content") or "").strip()
        if text:
            for line in text.splitlines():
                print(c("  │ ", DIM) + line)
    elif kind == "tool_call":
        args = ev.get("args") or {}
        shown = ", ".join(k + "=" + json.dumps(v, default=str)[:40]
                          for k, v in args.items())
        print(c("  " + str(ev.get("step")) + ". ", DIM)
              + c(ev.get("tool", ""), CYAN) + c("(" + shown + ")", DIM))
    elif kind == "tool_result":
        ms = ev.get("ms")
        tick = c("✓", GREEN) if ev.get("ok") else c("✗", RED)
        print("     " + tick + " " + _summarise(ev.get("result") or {})
              + (c("  " + str(ms) + "ms", DIM) if ms else ""))
    elif kind == "note":
        print(c("  ! " + ev.get("message", ""), YELLOW))
    elif kind == "budget":
        print(c("  ⏹ stopped by " + ev.get("stopped_by", ""), YELLOW))
    elif kind == "error":
        print(c("  ✗ " + ev.get("where", "") + ": " + ev.get("error", ""), RED))
    elif kind == "final":
        content = (ev.get("content") or "").strip()
        if content:
            print()
            print(content)
        u = ev.get("usage") or {}
        print(c("\n— " + str(ev.get("steps")) + " steps · "
                + str(ev.get("seconds")) + "s · "
                + str(u.get("total_tokens", "?")) + " tokens", DIM))
    elif kind == "verification":
        print_verification(ev)


def print_verification(ev: dict) -> None:
    """B8: the automatic verdict, one line per check."""
    v = ev.get("verdict")
    col = GREEN if v == "pass" else RED if v == "fail" else YELLOW
    print(c("\n  verified: " + str(v).upper(), col)
          + c("  (by " + str(ev.get("decided_by")) + ")", DIM))
    for ch in ev.get("checks") or []:
        mark = {"pass": c("✓", GREEN), "fail": c("✗", RED)}.get(
            ch.get("status"), c("?", YELLOW))
        print("    " + mark + " " + c(json.dumps(ch.get("check"), default=str)[:60], DIM)
              + "  " + str(ch.get("why"))[:100])
    j = ev.get("judge")
    if j:
        print(c("    judge " + str(j.get("model") or "") + ": " + str(j.get("verdict"))
                + " - " + str(j.get("why") or j.get("error") or "")[:160], DIM))


def _load_spec(a) -> dict:
    """--verify FILE and/or --expect / --reached, merged into one spec."""
    spec: dict = {"checks": []}
    if getattr(a, "verify", ""):
        with open(a.verify, encoding="utf-8") as fh:
            spec = json.load(fh)
        spec.setdefault("checks", [])
    for rx in getattr(a, "expect", None) or []:
        spec["checks"].append({"answer": rx})
    for rx in getattr(a, "reached", None) or []:
        spec["checks"].append({"reached_text": rx})
    return spec if spec["checks"] else {}


# -- commands ----------------------------------------------------------------

def cmd_run(a) -> int:
    from .agent import build_agent
    from .harness.loop import Budget

    def ask(name: str, args: dict) -> bool:
        print(c("  ? allow " + name + "(" + json.dumps(args, default=str)[:120]
                + ") [y/N] ", YELLOW), end="", flush=True)
        return input().strip().lower().startswith("y")

    def ask_operator(question: str, timeout_s: float):
        """The agent has one question and is waiting on the answer (B3)."""
        if not sys.stdin or not sys.stdin.isatty():
            return None                      # nobody at the keyboard: run stops
        print(c("\n  ? the agent asks: " + question, YELLOW), flush=True)
        print(c("    answer (empty to stop the run): ", YELLOW), end="", flush=True)
        try:
            return input().strip()
        except (EOFError, KeyboardInterrupt):
            return None

    agent = build_agent(
        provider=a.provider, model=a.model, mode=a.mode,
        allow=a.allow or [], deny=a.deny or [], packs=a.pack or [],
        budget=Budget(max_steps=a.max_steps, max_seconds=a.max_seconds,
                      max_usd=a.max_usd, free_reserve=a.free_reserve),
        operator_notes=a.notes or "", on_ask=ask,
        allow_writes=a.allow_write or [], allow_rules=a.allow_rule or [],
        allow_uncounted_reads=a.allow_uncounted_reads,
        stagnation=not a.no_stagnation_stop,
        on_ask_operator=ask_operator,
        helper_model=a.helper_model,
        summarize=a.summarize,
        verify_spec=_load_spec(a) or None,
        judge=a.judge,
    )
    goal = " ".join(a.goal)
    failed = False
    for ev in agent.run(goal):
        print_event(ev)
        if ev.get("type") == "error":
            failed = True
    return 1 if failed else 0


def cmd_verify(a) -> int:
    """Check a recorded run against a task spec and label it (B8)."""
    from datetime import datetime

    from .harness import recorder, verify
    from .harness.models import Chat, ModelConfig
    rid = a.run_id
    if rid == "latest":
        ids = recorder.list_runs(limit=1)
        if not ids:
            print(c("no runs recorded yet", DIM))
            return 1
        rid = ids[0]
    rows = recorder.load(rid)
    if not rows:
        print(c("no such run: " + rid, RED))
        return 1
    spec = _load_spec(a)
    if not spec:
        print(c("give checks: --verify SPEC.json, --expect REGEX or --reached REGEX", RED))
        return 2
    chat = None
    if a.judge:
        cfg = ModelConfig.from_env(role="helper")
        if not cfg.model:
            cfg = ModelConfig.from_env()
        chat = Chat(cfg)
    result = verify.verify(rows, spec, chat=chat)
    print(c(rid, BOLD))
    print_verification({"verdict": result["verdict"], "decided_by": result.get("by"),
                        "checks": result["checks"], "judge": result.get("judge")})
    if not a.no_label:
        recorder.label(rid, **dict(verify.label_fields(result, spec),
                                   labelled_at=datetime.now().astimezone()
                                   .isoformat(timespec="seconds")))
    return 0 if result["verdict"] == "pass" else 1


def _pairs(items) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit("expected name=value, got " + repr(it))
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def cmd_macro(a) -> int:
    """Make, list, show and run guarded macros (B9)."""
    from .harness import recorder, replay
    if a.action == "list":
        rows = replay.list_macros()
        if not rows:
            print(c("no macros yet in " + replay.MACRO_DIR, DIM))
        for m in rows:
            print(c(m["name"], CYAN) + c("  %s steps  slots=%s  %s" % (
                m["steps"], m["slots"], m["verified"]), DIM))
            print("   " + str(m["goal"] or "")[:90])
        return 0
    if not a.target:
        print(c("give a run id (make) or a macro name (show/run)", RED))
        return 2
    if a.action == "make":
        rid = a.target
        if rid == "latest":
            ids = recorder.list_runs(limit=1)
            rid = ids[0] if ids else ""
        rows = recorder.load(rid)
        if not rows:
            print(c("no such run: " + rid, RED))
            return 1
        m = replay.make_macro(rows, a.name or rid, slots=_pairs(a.slot),
                              allow_unverified=a.allow_unverified)
        if "error" in m:
            print(c(m["error"], RED) + ("\n  " + m["next"] if m.get("next") else ""))
            return 1
        path = replay.save(m)
        print(c("macro " + m["name"], BOLD) + c("  from " + rid + "  " + m["verified"], DIM))
        for n, st in enumerate(m["steps"], 1):
            bits = [st["tool"] + "(" + json.dumps(st.get("args"), ensure_ascii=False)[:60] + ")"]
            if st.get("target"):
                bits.append("-> " + json.dumps(st["target"], ensure_ascii=False)[:60])
            if st.get("pre"):
                bits.append(c("[%d keys, %s]" % (len(st["pre"]["keys"]), st["pre"]["pkg"]), DIM))
            if st.get("unreplayable"):
                bits.append(c("UNREPLAYABLE: " + st["unreplayable"], RED))
            print("  %d. %s" % (n, " ".join(bits)))
        print(c("saved " + path, DIM))
        return 0
    m = replay.load(a.target)
    if m is None:
        print(c("no such macro: " + a.target, RED))
        return 1
    if a.action == "show":
        print(json.dumps(m, indent=1, ensure_ascii=False))
        return 0
    factory = None
    if a.handoff:
        from .agent import build_agent

        def factory():
            return build_agent(provider=a.provider, model=a.model,
                               allow_writes=a.allow_write or [])
    out = replay.replay(m, values=_pairs(a.set), agent_factory=factory,
                        writes=tuple(a.allow_write or []))
    if "error" in out:
        print(c(out["error"], RED))
        return 1
    for st in out["steps"]:
        mark = c("✓", GREEN) if st["ok"] else c("✗", RED)
        sim = "" if st["similarity"] is None else c("  match %.2f" % st["similarity"], DIM)
        print("  " + mark + " " + str(st["step"]) + ". " + st["tool"] + sim)
    print(c("\n  replayed %d of %d steps in %ss" % (out["replayed"], out["of"],
                                                   out["seconds"]), BOLD))
    if out.get("handoff"):
        h = out["handoff"]
        print(c("  stopped at step %s: %s" % (h["at"], h["why"]), YELLOW))
        if out.get("agent_final"):
            print(c("  agent took over: " + json.dumps(out["agent_final"],
                                                       ensure_ascii=False)[:300], DIM))
        elif not a.handoff:
            print(c("  (--handoff would give the rest to the agent)", DIM))
    if out.get("verification"):
        print(c("  verified: " + out["verification"].upper(),
                GREEN if out["verification"] == "pass" else RED))
    if out.get("run_id"):
        print(c("  recorded as " + out["run_id"], DIM))
    return 0 if not out.get("handoff") else 3


def cmd_serve(a) -> int:
    from .server import serve
    serve(host=a.host, port=a.port)
    return 0


def cmd_tools(a) -> int:
    from .agent import build_registry
    reg = build_registry()
    if a.pack:
        rows = [t for t in reg.tools.values() if t.pack in a.pack]
    else:
        rows = list(reg.tools.values())
    if a.json:
        print(json.dumps([{"name": t.name, "pack": t.pack,
                           "dangerous": t.dangerous,
                           "description": t.description,
                           "parameters": t.schema} for t in rows],
                         indent=2))
        return 0
    by_pack: dict[str, list] = {}
    for t in rows:
        by_pack.setdefault(t.pack, []).append(t)
    for pack in sorted(by_pack):
        loaded = " (loaded by default)" if pack in reg.active_packs else ""
        print(c("\n" + pack + loaded, BOLD))
        for t in sorted(by_pack[pack], key=lambda x: x.name):
            params = ", ".join((t.schema.get("properties") or {}).keys())
            flag = c(" !", RED) if t.dangerous else ""
            print("  " + c(t.name, CYAN) + flag + c("(" + params + ")", DIM))
            print(c("      " + t.description.split(". ")[0][:110], DIM))
    print(c("\n" + str(len(rows)) + " tools in " + str(len(by_pack))
            + " packs", DIM))
    return 0


def cmd_tool(a) -> int:
    from .agent import build_registry
    reg = build_registry()
    reg.active_packs = set(reg.packs())
    try:
        args = json.loads(a.args) if a.args else {}
    except json.JSONDecodeError as e:
        print(c("bad --args JSON: " + str(e), RED))
        return 2
    result = reg.call(a.name, args)
    print(json.dumps(result, indent=2, default=str))
    return 0 if "error" not in result else 1


def cmd_doctor(a) -> int:
    from .agent import build_registry
    from .harness.models import Chat, ModelConfig

    ok = True
    print(c("ClaudePhone doctor", BOLD))

    print(c("\nenvironment", BOLD))
    print("  running on phone : " + str(dev.on_device()))
    try:
        print("  adb binary       : " + dev.ADB())
    except Exception as e:
        print(c("  adb binary       : NOT FOUND - " + str(e), RED))
        ok = False

    print(c("\ndevice", BOLD))
    if dev.on_device():
        lb = dev.ensure_loopback()
        print("  loopback         : " + str(lb.get("state")))
        if lb.get("action_required"):
            print(c("    -> " + lb["action_required"], YELLOW))
            ok = False
    try:
        devices = dev.list_devices()
        for d in devices:
            print("  " + d["serial"] + "  " + d["state"])
        if not devices:
            print(c("  no devices attached", RED))
            ok = False
        else:
            info = dev.device_info()
            print("  selected         : " + info.serial)
            print("  model            : " + info.model + "  Android "
                  + info.android + "  " + info.screen)
            print("  battery          : " + info.battery + "%")
    except Exception as e:
        print(c("  device error: " + str(e)[:200], RED))
        ok = False

    print(c("\nui backend", BOLD))
    try:
        t0 = time.time()
        d = dev.u2()
        xml = d.dump_hierarchy()
        print("  uiautomator2     : ok, dump "
              + str(int((time.time() - t0) * 1000)) + " ms, "
              + str(len(xml)) + " bytes")
    except Exception as e:
        print(c("  uiautomator2     : " + str(e)[:200], RED))
        print(c("    -> pip install uiautomator2, then retry", YELLOW))
        ok = False

    print(c("\ntools", BOLD))
    reg = build_registry()
    print("  registered       : " + str(len(reg.tools)) + " in "
          + str(len(reg.packs())) + " packs")
    print("  default surface  : " + str(len(reg.specs())) + " tools")

    print(c("\nmodel", BOLD))
    cfg = ModelConfig.from_env(a.provider)
    hcfg = ModelConfig.from_env(a.provider, role="helper")
    print("  provider         : " + cfg.provider)
    print("  decider          : " + cfg.model)
    print("  helper           : " + hcfg.model
          + ("" if hcfg.model != cfg.model else "  (same as decider)"))
    print("  base_url         : " + cfg.base_url)
    if cfg.provider == "openrouter" and not cfg.api_key:
        print(c("    -> no key: set OPENROUTER_API_KEY or OPENROUTER_API_KEY_FILE",
                YELLOW))
        ok = False
    elif cfg.provider == "openrouter" and not a.probe:
        # Reading the key's status costs nothing; a completion would spend one of
        # the day's free requests, so it is opt-in (--probe).
        from .harness.models import key_status, is_free_model
        st = key_status(cfg)
        if st.get("ok"):
            print("  key              : free tier = " + str(st.get("is_free_tier"))
                  + ", expires " + str(st.get("expires_at")))
            print("  free requests    : " + str(st.get("free_remaining")) + " of "
                  + str(st.get("free_limit")) + " left today")
            if not is_free_model(cfg.model) and st.get("is_free_tier"):
                print(c("    -> decider is a paid model on a key with no credits; "
                        "it will 402. Use a ':free' model.", YELLOW))
                ok = False
        else:
            print(c("  key              : " + st.get("error", "?")[:200], RED))
            ok = False
    else:
        h = Chat(cfg).health()
        if h.get("ok"):
            print(c("  reachable        : yes, " + str(h.get("ms"))
                    + " ms, tool calling = " + h.get("tool_convention", "?"),
                    GREEN))
        else:
            print(c("  reachable        : NO - " + h.get("error", "")[:200],
                    RED))
            ok = False

    print()
    print(c("all good" if ok else "problems found - see above",
            GREEN if ok else RED))
    return 0 if ok else 1


def cmd_runs(a) -> int:
    """Browse recorded runs. Every run is written by harness/recorder.py."""
    from .harness import recorder

    if a.run_id:
        rows = recorder.load(a.run_id)
        if not rows:
            print(c("no such run: " + a.run_id, RED))
            return 1
        if a.json:
            for r in rows:
                print(json.dumps(r, default=str))
            return 0
        s = recorder.summarise(a.run_id)
        print(c(s["run_id"], BOLD) + c("  " + (s.get("started") or ""), DIM))
        print(c("  goal   ", DIM) + (s.get("goal") or ""))
        print(c("  model  ", DIM) + str(s.get("provider")) + " / "
              + str(s.get("model")))
        bits = [str(s.get("outcome"))]
        if s.get("stopped_by"):
            bits.append("stopped_by=" + str(s["stopped_by"]))
        if s.get("seconds") is not None:
            bits.append(str(s["seconds"]) + "s")
        if s.get("tokens") is not None:
            bits.append(str(s["tokens"]) + " tokens")
        print(c("  result ", DIM) + "  ".join(bits))
        print()
        for r in rows:
            if r.get("type") == "tool_call":
                args = ", ".join(k + "=" + json.dumps(v, default=str)[:40]
                                 for k, v in (r.get("args") or {}).items())
                print(c("  " + str(r.get("step")) + ". ", DIM)
                      + c(r.get("tool", ""), CYAN) + c("(" + args + ")", DIM))
            elif r.get("type") == "tool_result":
                tick = c("✓", GREEN) if r.get("ok") else c("✗", RED)
                print("     " + tick + " "
                      + _summarise(r.get("result") or {}))
            elif r.get("type") == "thought":
                for line in (r.get("content") or "").strip().splitlines():
                    print(c("  │ ", DIM) + line)
        if s.get("answer"):
            print("\n" + s["answer"])
        for lab in s.get("labels") or []:
            print(c("\n  label: " + json.dumps(
                {k: v for k, v in lab.items()
                 if k not in ("kind", "at")}, default=str), YELLOW))
        return 0

    ids = recorder.list_runs(limit=a.limit)
    if not ids:
        print(c("no runs recorded yet in " + recorder.RUNS_DIR, DIM))
        return 0
    for rid in ids:
        s = recorder.summarise(rid)
        if s.get("error"):
            print(c(rid + "  " + s["error"], RED))
            continue
        bad = s.get("failed_steps") or 0
        flag = (c(" " + str(bad) + " failed", RED) if bad else "")
        print(c(rid, CYAN) + c("  " + str(s.get("steps")) + " steps  "
                               + str(s.get("seconds") or "?") + "s  "
                               + str(s.get("outcome")), DIM) + flag
              + (c("  " + s["verdict"], GREEN if s["verdict"] == "pass" else
                   RED if s["verdict"] == "fail" else YELLOW)
                 if s.get("verdict") else ""))
        print("   " + (s.get("goal") or "")[:90])
    print(c("\n" + str(len(ids)) + (" run" if len(ids) == 1 else " runs")
            + " in " + recorder.RUNS_DIR, DIM))
    return 0


def cmd_ab(a) -> int:
    """Run one goal against several deciders and compare (B5)."""
    from .harness import ab
    from .harness.loop import Budget
    from .harness.models import ModelConfig, key_status

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if len(models) < 1:
        print(c("give --models a,b", RED))
        return 2
    budget = Budget(max_steps=a.max_steps, max_seconds=a.max_seconds,
                    max_usd=a.max_usd, free_reserve=a.free_reserve)

    def reset():
        # Same starting screen for every decider: home, then settle.
        dev.shell("input keyevent KEYCODE_HOME")
        time.sleep(1.5)

    def quota():
        st = key_status(ModelConfig.from_env(a.provider))
        return st.get("free_remaining") if st.get("ok") else None

    goal = " ".join(a.goal)
    print(c("A/B: " + goal, BOLD))
    rows = ab.run_ab(goal, models, budget, expect=a.expect, notes=a.notes or "",
                     provider=a.provider, reset=reset, quota=quota)
    print(ab.table(rows))
    for r in rows:
        if r.get("answer") or r.get("error"):
            print(c(chr(10) + r["model"] + ":", BOLD), (r.get("answer") or r.get("error"))[:300])
    from . import state
    path = ab.save(rows, goal, a.expect, os.path.join(state.ARTIFACT_DIR, "ab"))
    print(c(chr(10) + "saved " + path, DIM))
    return 0


def cmd_abplan(a) -> int:
    """Run a pre-registered decider comparison, resuming where it stopped."""
    from .harness import ab
    from .harness.models import ModelConfig, key_status
    from . import state

    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    state_path = a.state or os.path.join(state.ARTIFACT_DIR, "ab",
                                         plan.get("name", "plan") + ".state.json")
    items = ab.plan_items(plan)
    if a.dry_run:
        print(c(plan.get("name", "plan") + ": " + str(len(items)) + " runs", BOLD))
        worst = sum(int(next(t for t in plan["tasks"] if t["id"] == i["task"])
                        ["max_steps"]) + 1 for i in items)
        print("  worst case " + str(worst) + " requests; state -> " + state_path)
        for i in items:
            print("  " + i["id"])
        return 0

    def quota():
        st = key_status(ModelConfig.from_env(a.provider))
        return st.get("free_remaining") if st.get("ok") else None

    def reset(cmds):
        for cmd in cmds:
            dev.shell(cmd)
        time.sleep(1.5)

    # Hold datacollect's per-phone lock for the whole plan, so an unattended run
    # can never interleave with a collection phase on the same handset.
    lock, guard = None, None
    try:
        from .policy import writes as wr
        sys.path.insert(0, wr._datacollect_dir())
        import guard                                    # type: ignore
        serial = dev.default_serial()
        lock = guard.acquire(serial, "abplan:" + plan.get("name", "plan"))
        if not lock:
            print(c("phone " + serial + " is held by " + str(guard.lock_holder(serial)),
                    RED))
            return 5
    except ImportError:
        guard = None                                   # no datacollect: no lock to take
    try:
        st = ab.run_plan(plan, state_path, quota=quota, reset=reset,
                         provider=a.provider,
                         shell=lambda cmd: dev.shell(cmd, check=False))
    finally:
        if lock and guard is not None:
            guard.release(lock)
    done = len(st.get("results", {}))
    print(c(chr(10) + str(done) + " of " + str(len(items)) + " runs done", BOLD))
    for r in ab.summarise(plan, st):
        print("  %-3s %-40s %d/%d correct, median %s steps, ended %s%s" % (
            r["task"], r["model"][:40], r["correct"], r["runs"],
            r["median_steps"], json.dumps(r["ended"]),
            ("  CHANGED A SETTING x%d" % r["changed_a_setting"])
            if r["changed_a_setting"] else ""))
    if st.get("stopped"):
        print(c("  paused for the free quota; run again tomorrow to continue", YELLOW))
    return 0


def cmd_deeplinks(a) -> int:
    """List the deep-link registry, or verify its links on the phone (B6)."""
    from . import cards
    from .tools import deeplink_tools as dl
    if not a.verify:
        for e in cards.links():
            v = e.get("verified") or {}
            print("  %-30s -> %-26s %s" % (e["prefix"], e["package"],
                  ("verified %s on %s, app %s" % (v.get("date"), v.get("device"),
                                                  v.get("app_version")))
                  if v else c("UNVERIFIED", YELLOW)))
        return 0
    lock, guard = None, None
    try:
        from .policy import writes as wr
        sys.path.insert(0, wr._datacollect_dir())
        import guard                                    # type: ignore
        serial = dev.default_serial()
        lock = guard.acquire(serial, "deeplinks-verify")
        if not lock:
            print(c("phone is held by " + str(guard.lock_holder(serial)), RED))
            return 5
    except ImportError:
        guard = None
    try:
        rows = dl.verify(prefix=a.prefix)
    finally:
        if lock and guard is not None:
            guard.release(lock)
    for r in rows:
        print("  %-30s %s" % (r["prefix"], "landed, app " + str(r.get("app_version"))
                              if r["landed"] else c("NOT landed: " + str(r.get("error")), RED)))
    return 0 if all(r["landed"] for r in rows) else 1


def cmd_mcp(a) -> int:
    """Serve the same tools over MCP stdio, for a laptop Claude Code session."""
    from .mcp_server import main as mcp_main
    mcp_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claudephone",
        description="An agent that runs on your Android phone and drives it.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="give the agent a goal and watch it work")
    r.add_argument("goal", nargs="+")
    r.add_argument("--provider", default="", help="openrouter | local")
    r.add_argument("--model", default="")
    r.add_argument("--mode", default="auto",
                   choices=["auto", "ask", "readonly"])
    r.add_argument("--pack", action="append",
                   help="load a tool pack up front (repeatable)")
    r.add_argument("--allow", action="append", help="permit a denied tool")
    r.add_argument("--deny", action="append")
    r.add_argument("--allow-write", action="append", dest="allow_write",
                   help="enable one budgeted account write for this run, e.g. "
                        "follow (still checked against the ledger)")
    r.add_argument("--allow-rule", action="append", dest="allow_rule",
                   help="allow one forbidden write rule id from "
                        "policy/writes.json for this run")
    r.add_argument("--no-stagnation-stop", action="store_true",
                   dest="no_stagnation_stop",
                   help="do not end a run that repeats one call on an unchanged "
                        "screen; the model is still warned (B4)")
    r.add_argument("--allow-uncounted-reads", action="store_true",
                   dest="allow_uncounted_reads",
                   help="let budgeted reads (profile opens, reels, searches...) run "
                        "when no ledger is reachable, e.g. on the phone; results "
                        "say counted=false")
    r.add_argument("--max-steps", type=int, default=30, dest="max_steps")
    r.add_argument("--helper-model", default="", dest="helper_model",
                   help="model for side work (summaries); defaults to "
                        "CLAUDEPHONE_HELPER_MODEL, then the decider")
    r.add_argument("--max-usd", type=float, default=0.25, dest="max_usd",
                   help="stop the run once OpenRouter reports this much spent "
                        "(decider + helper); 0 = no cap")
    r.add_argument("--free-reserve", type=int, default=2, dest="free_reserve",
                   help="on ':free' models, stop with this many of the day's "
                        "free requests left")
    r.add_argument("--summarize", action="store_true",
                   help="after the run, one helper call writes what happened")
    r.add_argument("--verify", default="",
                   help="task spec (JSON with 'checks'); the run is checked and "
                        "labelled automatically when it ends (B8)")
    r.add_argument("--expect", action="append",
                   help="shorthand check: the final answer must match this regex")
    r.add_argument("--reached", action="append",
                   help="shorthand check: this regex must appear on a screen read")
    r.add_argument("--judge", action="store_true",
                   help="if the checks are inconclusive, one helper-model call "
                        "decides (costs one request)")
    r.add_argument("--max-seconds", type=float, default=900.0,
                   dest="max_seconds")
    r.add_argument("--notes", default="", help="extra operator instructions")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("serve", help="accept tasks over HTTP")
    s.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to accept from the LAN (token required)")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    t = sub.add_parser("tools", help="list the tool surface")
    t.add_argument("--pack", action="append")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_tools)

    o = sub.add_parser("tool", help="call one tool directly")
    o.add_argument("name")
    o.add_argument("--args", default="", help="JSON object of arguments")
    o.set_defaults(fn=cmd_tool)

    d = sub.add_parser("doctor", help="check the whole setup end to end")
    d.add_argument("--provider", default="")
    d.add_argument("--probe", action="store_true",
                   help="also send one completion (spends a free request)")
    d.set_defaults(fn=cmd_doctor)

    n = sub.add_parser("runs", help="list or replay recorded runs")
    n.add_argument("run_id", nargs="?", default="",
                   help="show one run; omit to list")
    n.add_argument("--limit", type=int, default=25)
    n.add_argument("--json", action="store_true",
                   help="with a run_id, dump its raw events")
    n.set_defaults(fn=cmd_runs)

    b = sub.add_parser("ab", help="compare decider models on one goal (B5)")
    b.add_argument("goal", nargs="+")
    b.add_argument("--models", required=True,
                   help="comma-separated decider models, run in this order")
    b.add_argument("--expect", default="",
                   help="regex the answer must contain to count as correct")
    b.add_argument("--provider", default="")
    b.add_argument("--notes", default="")
    b.add_argument("--max-steps", type=int, default=8, dest="max_steps")
    b.add_argument("--max-seconds", type=float, default=300.0, dest="max_seconds")
    b.add_argument("--max-usd", type=float, default=0.10, dest="max_usd")
    b.add_argument("--free-reserve", type=int, default=2, dest="free_reserve")
    b.set_defaults(fn=cmd_ab)

    pl = sub.add_parser("abplan", help="run a pre-registered decider A/B, "
                                       "resuming across days (B5)")
    pl.add_argument("plan", help="plan JSON, e.g. evals/no_shortcut_ab.json")
    pl.add_argument("--state", default="", help="results file (resumable)")
    pl.add_argument("--provider", default="")
    pl.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the schedule and worst-case request count")
    pl.set_defaults(fn=cmd_abplan)

    k = sub.add_parser("deeplinks", help="list or verify the deep-link registry (B6)")
    k.add_argument("--verify", action="store_true",
                   help="open each link's sample on the phone; mark those that land "
                        "(each open is a counted read)")
    k.add_argument("--prefix", default="", help="verify only this prefix")
    k.set_defaults(fn=cmd_deeplinks)

    v = sub.add_parser("verify", help="check a recorded run against a task spec "
                                      "and label it (B8)")
    v.add_argument("run_id", help="a run id, or 'latest'")
    v.add_argument("--verify", "--spec", dest="verify", default="",
                   help="task spec JSON with 'checks'")
    v.add_argument("--expect", action="append", help="final answer regex")
    v.add_argument("--reached", action="append", help="screen text regex")
    v.add_argument("--judge", action="store_true",
                   help="one helper-model call if the checks are inconclusive")
    v.add_argument("--no-label", action="store_true", dest="no_label",
                   help="print the verdict without writing a label")
    v.set_defaults(fn=cmd_verify)

    mc = sub.add_parser("macro", help="guarded macros from verified runs (B9)")
    mc.add_argument("action", choices=["make", "list", "show", "run"])
    mc.add_argument("target", nargs="?", default="",
                    help="make: a run id or 'latest'; show/run: a macro name")
    mc.add_argument("--name", default="", help="make: the macro's name")
    mc.add_argument("--slot", action="append",
                    help="make: name=value - that value becomes a parameter")
    mc.add_argument("--allow-unverified", action="store_true", dest="allow_unverified",
                    help="make: build from a run that was never verified")
    mc.add_argument("--set", action="append", help="run: slot=value")
    mc.add_argument("--handoff", action="store_true",
                    help="run: on a mismatch, give the rest of the goal to the agent "
                         "(uses the model)")
    mc.add_argument("--allow-write", action="append", dest="allow_write",
                    help="run: permit one budgeted account write (still ledger-checked)")
    mc.add_argument("--provider", default="")
    mc.add_argument("--model", default="")
    mc.set_defaults(fn=cmd_macro)

    m = sub.add_parser("mcp", help="serve tools over MCP stdio")
    m.set_defaults(fn=cmd_mcp)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
