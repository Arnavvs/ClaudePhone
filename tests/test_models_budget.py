"""Model roles, cost accounting and the dollar / free-quota caps (B5).

No network: the HTTP layer is replaced, so these pin the behaviour against the
shapes OpenRouter documents - `usage.cost` as a float, 429 with Retry-After,
402 with no credits, and GET /key's free_model_daily_requests.

    python -m pytest tests/test_models_budget.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone.harness import models  # noqa: E402
from claudephone.harness.loop import Agent, Budget  # noqa: E402
from claudephone.harness.models import (Chat, ModelConfig, ModelError,  # noqa: E402
                                        Reply, is_free_model)
from claudephone.harness.registry import ToolRegistry  # noqa: E402

FREE = "deepseek/deepseek-v4-flash-0731:free"
PAID = "deepseek/deepseek-v4-flash-0731"


def completion(content="ok", cost=0.0, calls=None, tokens=100):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return {"model": "m", "choices": [{"message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens - 10, "completion_tokens": 10,
                      "total_tokens": tokens, "cost": cost, "is_byok": False,
                      "prompt_tokens_details": {"cached_tokens": 0}}}


# -- roles and config -------------------------------------------------------------

def test_decider_and_helper_come_from_separate_variables(monkeypatch):
    monkeypatch.setenv("CLAUDEPHONE_MODEL", FREE)
    monkeypatch.setenv("CLAUDEPHONE_HELPER_MODEL", "liquid/lfm-2.5-2.6b:free")
    assert ModelConfig.from_env().model == FREE
    assert ModelConfig.from_env(role="helper").model == "liquid/lfm-2.5-2.6b:free"
    monkeypatch.delenv("CLAUDEPHONE_HELPER_MODEL")
    assert ModelConfig.from_env(role="helper").model == FREE      # falls back


def test_the_key_can_come_from_a_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    f = tmp_path / "k.txt"
    f.write_text("sk-or-v1-test\n")
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(f))
    assert ModelConfig.from_env().api_key == "sk-or-v1-test"


@pytest.mark.parametrize("model,free", [
    (FREE, True), ("openrouter/free", True), (PAID, False), ("", False)])
def test_free_model_detection(model, free):
    assert is_free_model(model) is free


# -- cost accounting ----------------------------------------------------------------

def test_cost_is_accumulated_as_a_float_and_bools_are_ignored(monkeypatch):
    chat = Chat(ModelConfig(model=PAID))
    replies = iter([completion(cost=0.0012), completion(cost=0.0003)])
    monkeypatch.setattr(chat, "_post", lambda path, payload: next(replies))
    chat.complete([{"role": "user", "content": "a"}])
    chat.complete([{"role": "user", "content": "b"}])
    assert chat.cost == pytest.approx(0.0015)
    assert chat.total_usage["total_tokens"] == 200
    assert "is_byok" not in chat.total_usage              # a bool, not a count
    assert chat.requests == 2 and chat.free_requests == 0


def test_free_requests_are_counted(monkeypatch):
    chat = Chat(ModelConfig(model=FREE))
    monkeypatch.setattr(chat, "_post", lambda path, payload: completion())
    for _ in range(3):
        chat.complete([{"role": "user", "content": "x"}])
    assert chat.free_requests == 3 and chat.cost == 0.0


# -- the HTTP edge cases OpenRouter documents ----------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body="{}", headers=None):
    import email.message
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://x", code, "err", h, io.BytesIO(body.encode()))


def test_a_429_is_waited_out_and_retried(monkeypatch):
    chat = Chat(ModelConfig(model=FREE, base_url="https://openrouter.ai/api/v1"))
    calls = {"n": 0}
    slept = []

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, headers={"Retry-After": "2"})
        return _Resp(json.dumps(completion()).encode())

    monkeypatch.setattr(models.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(models.time, "sleep", lambda s: slept.append(s))
    r = chat.complete([{"role": "user", "content": "x"}])
    assert r.content == "ok" and calls["n"] == 2
    assert slept == [2.0] and chat.rate_limited == 1


def test_a_429_that_never_clears_surfaces_after_the_retries(monkeypatch):
    chat = Chat(ModelConfig(model=FREE, base_url="https://openrouter.ai/api/v1"))
    monkeypatch.setattr(models.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_error(429)))
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    with pytest.raises(ModelError) as e:
        chat.complete([{"role": "user", "content": "x"}])
    assert "429" in str(e.value)
    assert chat.rate_limited == len(Chat.RETRY_WAITS)


def test_a_402_says_what_to_do(monkeypatch):
    chat = Chat(ModelConfig(model=PAID, base_url="https://openrouter.ai/api/v1"))
    monkeypatch.setattr(models.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(
                            _http_error(402, '{"error": "Insufficient credits"}')))
    with pytest.raises(ModelError) as e:
        chat.complete([{"role": "user", "content": "x"}])
    assert "402" in str(e.value) and ":free" in str(e.value)


def test_key_status_never_returns_the_key_or_label(monkeypatch):
    body = {"data": {"label": "sk-or-v1-abc...xyz", "is_free_tier": True,
                     "usage": 0, "limit_remaining": None,
                     "expires_at": "2026-09-25T11:22:00Z",
                     "free_model_daily_requests": {"used": 3, "limit": 50,
                                                   "remaining": 47}}}
    monkeypatch.setattr(models.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(json.dumps(body).encode()))
    st = models.key_status(ModelConfig(api_key="sk-or-v1-secret",
                                       base_url="https://openrouter.ai/api/v1"))
    assert st["ok"] and st["free_remaining"] == 47 and st["free_limit"] == 50
    assert "sk-or" not in json.dumps(st)


# -- the budget ------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,expect", [
    ({"usd": 0.30}, "max_usd"),
    ({"usd": 0.10}, ""),
    ({"free_left": 2}, "free_quota"),
    ({"free_left": 3}, ""),
    ({"free_left": None}, ""),
])
def test_budget_caps(kwargs, expect):
    import time
    got = Budget(max_usd=0.25, free_reserve=2).exceeded(1, time.time(), 10, **kwargs)
    assert got.startswith(expect) if expect else got == ""


def test_max_usd_zero_means_no_cap():
    import time
    assert Budget(max_usd=0).exceeded(1, time.time(), 10, usd=99.0) == ""


# -- through the loop ------------------------------------------------------------

class ScriptedChat(Chat):
    """Replays replies and reports a per-reply cost, like OpenRouter does."""

    def __init__(self, model, replies, cost_each=0.0):
        super().__init__(ModelConfig(model=model, provider="openrouter"))
        self._replies = list(replies)
        self._cost = cost_each

    def complete(self, messages, tools=None):
        self.requests += 1
        if is_free_model(self.cfg.model):
            self.free_requests += 1
        self.total_usage["cost"] = self.total_usage.get("cost", 0.0) + self._cost
        self.total_usage["total_tokens"] = self.total_usage.get("total_tokens", 0) + 50
        return self._replies.pop(0) if self._replies else Reply(content="done")


def reg():
    r = ToolRegistry()
    with r.pack("core"):
        @r.tool(description="Do a thing.")
        def poke(n: int = 0) -> dict:
            return {"poked": n}
    return r


def looping(n):
    return [Reply(content="", tool_calls=[{"id": "c%d" % i, "name": "poke",
                                            "args": {"n": i}}]) for i in range(n)]


def test_the_dollar_cap_stops_a_run(monkeypatch):
    chat = ScriptedChat(PAID, looping(40), cost_each=0.06)
    final = [e for e in Agent(chat, reg(), budget=Budget(max_usd=0.25)).run("go")
             if e["type"] == "final"][-1]
    assert final["stopped_by"].startswith("max_usd")
    assert 0.25 <= final["cost_usd"] < 0.40               # stopped promptly


def test_the_free_quota_stops_a_run_with_requests_to_spare(monkeypatch):
    import claudephone.harness.loop as loop
    monkeypatch.setattr(loop, "key_status", lambda cfg: {
        "ok": True, "free_remaining": 6, "expires_at": "2026-09-25"})
    chat = ScriptedChat(FREE, looping(40))
    events = list(Agent(chat, reg(), budget=Budget(free_reserve=2)).run("go"))
    start = events[0]
    final = [e for e in events if e["type"] == "final"][-1]
    assert start["free_requests_left"] == 6
    assert final["stopped_by"].startswith("free_quota")
    assert final["free_requests_left"] == 2 and chat.free_requests == 4


def test_paid_models_never_read_the_free_quota(monkeypatch):
    import claudephone.harness.loop as loop
    monkeypatch.setattr(loop, "key_status",
                        lambda cfg: pytest.fail("should not be called"))
    chat = ScriptedChat(PAID, [Reply(content="done")])
    final = [e for e in Agent(chat, reg()).run("go") if e["type"] == "final"][-1]
    assert "free_requests_left" not in final


def test_the_summary_goes_to_the_helper_not_the_decider(monkeypatch):
    import claudephone.harness.loop as loop
    monkeypatch.setattr(loop, "key_status", lambda cfg: {"ok": False})
    decider = ScriptedChat(PAID, looping(2) + [Reply(content="finished, found X")],
                           cost_each=0.001)
    helper = ScriptedChat("small/helper", [Reply(content="It found X in 2 steps.")],
                          cost_each=0.0001)
    events = list(Agent(decider, reg(), helper=helper, summarize=True).run("find X"))
    summary = [e for e in events if e["type"] == "summary"][-1]
    assert summary["content"] == "It found X in 2 steps."
    assert summary["model"] == "small/helper"
    assert helper.requests == 1 and decider.requests == 3
    final = [e for e in events if e["type"] == "final"][-1]
    assert final["cost_usd"] == pytest.approx(0.003)       # decider only, at the end
    assert summary["cost_usd"] == pytest.approx(0.0031)    # helper added after
