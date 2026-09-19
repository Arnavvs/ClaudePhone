"""MCP hygiene: annotations, batch, next steps, diagnose, task control (B12).

    python -m pytest tests/test_mcp_hygiene.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

from claudephone import device as dev  # noqa: E402
from claudephone import mcp_hints, mcp_server, server, state  # noqa: E402
from claudephone.agent import OUTBOUND, build_registry  # noqa: E402
from claudephone.harness import history, recorder  # noqa: E402
from claudephone.harness.loop import Agent  # noqa: E402
from claudephone.harness.models import Chat, ModelConfig, Reply  # noqa: E402
from claudephone.harness.registry import ToolRegistry, next_step  # noqa: E402
from claudephone.tools import diagnose_tools as dg  # noqa: E402

REG = build_registry()


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder, "RUNS_DIR", str(tmp_path / "runs"))
    history.current.reset()
    state.last.update(elements=[], at=0.0, pkg=None, ver="", fp="")


# -- annotations ----------------------------------------------------------------

def test_every_read_only_name_is_a_real_tool():
    assert mcp_hints.READ_ONLY <= set(REG.tools), mcp_hints.READ_ONLY - set(REG.tools)
    assert mcp_hints.DESTRUCTIVE_EXTRA <= set(REG.tools)


def test_nothing_read_only_can_do_harm():
    for name in mcp_hints.READ_ONLY:
        t = REG.tools[name]
        assert not t.dangerous and name not in OUTBOUND
        assert name not in mcp_hints.DESTRUCTIVE_EXTRA


@pytest.mark.parametrize("name", ["phone_sms_list", "phone_contacts", "phone_call_log",
                                  "clipboard_get", "notifications", "read_file",
                                  "ig_open_profile", "tap", "text_input"])
def test_private_reads_and_actions_are_never_auto_approvable(name):
    h = mcp_hints.hints(REG.tools[name])
    assert h["readOnlyHint"] is False and h.get("destructiveHint") is not False


def test_outbound_dangerous_and_account_writes_are_destructive():
    for t in REG.tools.values():
        if t.dangerous or t.name in OUTBOUND or t.name in mcp_hints.DESTRUCTIVE_EXTRA:
            assert mcp_hints.hints(t)["destructiveHint"] is True, t.name
    assert mcp_hints.hints(REG.tools["x_feed_like"])["destructiveHint"] is True


def test_the_mcp_server_publishes_the_annotations(monkeypatch):
    from mcp.server import MCPServer
    captured = {}
    monkeypatch.setattr(MCPServer, "run", lambda self, **k: captured.setdefault("s", self))
    mcp_server.main()
    tools = {t.name: t for t in asyncio.run(captured["s"].list_tools())}
    assert "batch" in tools and len(tools) == len(REG.tools) + 1
    wire = {n: t.annotations.model_dump(by_alias=True, exclude_none=True)
            for n, t in tools.items()}
    assert wire["ui_dump"]["readOnlyHint"] is True
    assert wire["tg_send"]["destructiveHint"] is True
    assert "destructiveHint" not in wire["tap"]               # "may be", per spec
    assert tools["ui_dump"].annotations.read_only_hint is True


def test_the_laptop_bridge_registers_task_control(monkeypatch):
    from mcp.server import MCPServer
    import mcp_bridge
    captured = {}
    monkeypatch.setattr(MCPServer, "run", lambda self, **k: captured.setdefault("s", self))
    mcp_bridge.main()
    tools = {t.name: t for t in asyncio.run(captured["s"].list_tools())}
    assert {"phone_task", "phone_task_status", "phone_task_stop", "phone_reply",
            "phone_run_inspect"} <= set(tools)
    assert tools["phone_run_inspect"].annotations.read_only_hint is True
    assert tools["phone_task"].annotations.read_only_hint is False


# -- batch ------------------------------------------------------------------------

def small_registry(log):
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Look.")
        def ui_dump(limit: int = 60) -> dict:
            log.append("ui_dump")
            return {"elements": []}

        @r.tool(description="Fails.")
        def broken() -> dict:
            log.append("broken")
            return {"error": "nope"}

        @r.tool(description="Sends a message.")
        def tg_send(text: str = "") -> dict:
            log.append("tg_send")
            return {"sent": True}
    return r


def test_batch_runs_in_order_and_stops_on_error():
    log = []
    r = small_registry(log)
    out = mcp_server.run_batch(r, [{"tool": "ui_dump"}, {"tool": "broken"},
                                   {"tool": "ui_dump"}])
    assert log == ["ui_dump", "broken"] and out["stopped_at"] == 1 and out["skipped"] == 1
    log.clear()
    out = mcp_server.run_batch(r, [{"tool": "broken"}, {"tool": "ui_dump"}],
                               stop_on_error=False, list_at_end=True)
    assert log == ["broken", "ui_dump", "ui_dump"] and "screen" in out


def test_batch_refuses_destructive_and_nested_calls_before_running_anything():
    log = []
    r = small_registry(log)
    out = mcp_server.run_batch(r, [{"tool": "ui_dump"}, {"tool": "tg_send", "args": {"text": "hi"}}])
    assert "refused" in out["error"] and log == []
    assert "batch" in mcp_server.run_batch(r, [{"tool": "batch"}])["error"]
    assert "at most" in mcp_server.run_batch(r, [{"tool": "ui_dump"}] * 21)["error"]
    assert "next" in mcp_server.run_batch(r, [])


# -- next steps -------------------------------------------------------------------

def test_errors_say_what_to_do_next():
    r = small_registry([])
    unknown = r.call("ui_dumpp", {})
    assert "ui_dump" in unknown["did_you_mean"] and "find_tool" in unknown["next"]
    with r.pack("core"):
        @r.tool(description="Needs x.")
        def needs(x: int) -> dict:
            return {"x": x}
    assert "expected" in r.call("needs", {})["next"]           # missing argument


@pytest.mark.parametrize("exc,expect", [
    (dev.DeviceError("adb shell failed: device offline"), "diagnose()"),
    (dev.DeviceError("adb: device unauthorized"), "person must accept"),
    (RuntimeError("bridge unreachable"), "bridge_status"),
    (ValueError("weird"), "do not repeat"),
])
def test_next_step_by_cause(exc, expect):
    assert expect in next_step(exc)


# -- diagnose ---------------------------------------------------------------------

@pytest.fixture
def phone(monkeypatch):
    calls = {"restart": 0, "shell": []}

    def make(state_="device", held=None, on_device=False):
        monkeypatch.setattr(dev, "default_serial", lambda: "SER")
        monkeypatch.setattr(dev, "on_device", lambda: on_device)
        monkeypatch.setattr(dev, "list_devices", lambda: [{"serial": "SER", "state": state_}])
        monkeypatch.setattr(dev, "shell", lambda cmd, *a, **k: calls["shell"].append(cmd) or "")
        monkeypatch.setattr(dg, "_held_by_others", lambda: held or [])
        monkeypatch.setattr(dg, "_restart_adb", lambda: calls.__setitem__("restart", calls["restart"] + 1))
        from claudephone.runtime import bridge as br
        from claudephone.runtime import observer as obs
        monkeypatch.setattr(br.Bridge, "installed", staticmethod(lambda s="": True))
        monkeypatch.setattr(br.Bridge, "enabled", staticmethod(lambda s="": True))
        monkeypatch.setattr(br, "available", lambda s="", recheck=False: False)
        monkeypatch.setattr(br, "heal", lambda s="": True)

        class O:
            def health(self):
                return {"awake": True, "locked": False}
        monkeypatch.setattr(obs, "observer", lambda *a, **k: O())
        return calls
    return make


def test_an_offline_phone_is_not_fixed_while_another_process_holds_one(phone):
    calls = phone("offline", held=[{"serial": "OTHER", "phase": "abplan", "pid": 1}])
    out = dg.diagnose(attempt_fix=True)
    assert calls["restart"] == 0 and not out["ok"]
    assert "abplan" in out["fixes"][0]["why"]


def test_an_offline_phone_gets_an_adb_restart_when_nothing_else_is_running(phone):
    calls = phone("offline")
    dg.diagnose(attempt_fix=True)
    assert calls["restart"] == 1


def test_on_the_phone_adb_is_never_restarted(phone):
    calls = phone("offline", on_device=True)
    out = dg.diagnose(attempt_fix=True)
    assert calls["restart"] == 0 and "loopback" in out["fixes"][0]["why"]


def test_without_attempt_fix_nothing_is_changed(phone):
    calls = phone("offline")
    dg.diagnose()
    assert calls["restart"] == 0


def test_an_unbound_bridge_is_re_enabled_and_uiautomator_is_never_killed(phone):
    calls = phone("device")
    out = dg.diagnose(attempt_fix=True)
    bridge = next(c for c in out["checks"] if c["check"] == "bridge")
    assert bridge["status"] == "ok" and out["fixes"][0]["fix"].startswith("re-enable")
    assert not any("pkill" in c or "kill" in c for c in calls["shell"])
    assert all("pkill" not in " ".join(a) for a in dg.SAFE_FIX_COMMANDS)


# -- task control over HTTP ---------------------------------------------------------

class Scripted(Chat):
    def __init__(self, n):
        super().__init__(ModelConfig(provider="local", model="scripted"))
        self.n = n

    def complete(self, messages, tools=None):
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 1
        if self.n <= 0:
            return Reply(content="all done")
        self.n -= 1
        return Reply(content="", tool_calls=[{"id": "c%d" % self.n, "name": "slow",
                                              "args": {}}])


def slow_registry():
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Takes a moment.")
        def slow() -> dict:
            time.sleep(0.15)
            return {"ok": True}
    return r


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(server, "build_agent",
                        lambda **k: Agent(Scripted(k.get("_n", 0) or 40), slow_registry()))
    server.Handler.require_auth = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    def req(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(base + path, data=data, method="POST" if data else "GET",
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read().decode())
    yield req
    httpd.shutdown()


def _wait(req, sid, status, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = req("/task/" + sid)
        if st["status"] == status:
            return st
        time.sleep(0.1)
    raise AssertionError("never reached " + status + ": " + json.dumps(st)[:400])


def test_a_background_task_can_be_followed_and_stopped(http):
    started = http("/task", {"goal": "poke slowly", "background": True})
    sid = started["session_id"]
    assert started["status"] == "running"
    time.sleep(0.5)
    st = http("/task/" + sid)
    assert st["status"] == "running" and st["steps"] >= 1
    assert all("elements" not in json.dumps(e) for e in st["recent"])
    http("/task/" + sid + "/stop", {"reason": "test"})
    st = _wait(http, sid, "stopped")
    assert st["final"]["stopped_by"].startswith("operator_stop (test)")
    assert any(t["session_id"] == sid for t in http("/tasks")["tasks"])
    run = http("/runs/" + st["run_id"])
    assert run["stopped_by"].startswith("operator_stop") and run["steps_detail"]
    assert http("/runs/latest")["run_id"] == st["run_id"]
