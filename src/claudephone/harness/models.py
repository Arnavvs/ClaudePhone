"""Model clients: OpenRouter and any local OpenAI-compatible server.

Both speak the same wire format, so there is one client and the only difference
is a base URL and a key. `llama.cpp`'s `llama-server`, `ollama`, `vllm` and
OpenRouter are all reachable this way.

Only the standard library is used for HTTP. That is a deliberate constraint:
this runs inside Termux on a phone, and every avoided dependency is one less
thing to compile against aarch64 at setup time.

## Two tool-calling conventions

Cheap models are the target, and they are uneven about function calling:

* `native` - the OpenAI `tools` / `tool_calls` fields. Correct when supported.
* `json`   - the model writes a fenced JSON object and we parse it out. Needed
             for small local models (LFM2, most sub-3B GGUFs) which have no
             tool-call training at all.

`auto` starts native, and permanently falls back to json for the session the
first time the endpoint rejects the `tools` parameter.

## Two roles (B5)

The only large ablation of phone agents (minitap, arXiv 2602.07787) found the
DECISION role is where cheap models collapse - 100% to 11.2% with a budget
decider - while the supporting roles held 50-58%. So the model that picks the
next action and the model that does supporting work are configured separately:

    CLAUDEPHONE_MODEL         the decider - every step goes through it
    CLAUDEPHONE_HELPER_MODEL  summaries and other side work (defaults to the decider)

## What a run costs

OpenRouter now returns `usage.cost` on every completion, in credits (= USD), so
cost is accumulated as a float alongside the token counts and the loop can stop
a run on a dollar cap. Free models (`:free`) cost nothing but are rationed: 20
requests a minute, and 50 a day on an account that has never bought $10 of
credits (1000 a day after). Each request to one is counted, so the loop can stop
before the day's allowance is gone rather than on a 429 halfway through a task.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
LOCAL_BASE = "http://127.0.0.1:8080/v1"

# Cheap, tool-calling-capable defaults. Override with CLAUDEPHONE_MODEL - model
# availability and pricing on OpenRouter move faster than this file does.
DEFAULT_REMOTE_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_LOCAL_MODEL = "local"


class ModelError(RuntimeError):
    pass


def is_free_model(model: str) -> bool:
    """OpenRouter's rationed free tier: `:free` variants and the free router."""
    m = (model or "").lower()
    return m.endswith(":free") or m == "openrouter/free"


def _read_key() -> str:
    """OPENROUTER_API_KEY, or the contents of OPENROUTER_API_KEY_FILE.

    The file form keeps the key out of shell history and process listings,
    which matters on a phone where anything in Termux can read `ps`.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("OPENROUTER_API_KEY_FILE", "").strip()
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""
    model: str = ""
    ms: int = 0


@dataclass
class ModelConfig:
    provider: str = "openrouter"       # openrouter | local | custom
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    tool_mode: str = "auto"            # auto | native | json
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: int = 180
    role: str = "decider"              # decider | helper (B5)

    @classmethod
    def from_env(cls, provider: str = "", role: str = "decider") -> "ModelConfig":
        provider = (provider or os.environ.get("CLAUDEPHONE_PROVIDER")
                    or "openrouter").lower()
        if provider == "local":
            model = os.environ.get("CLAUDEPHONE_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)
            if role == "helper":
                model = os.environ.get("CLAUDEPHONE_LOCAL_HELPER_MODEL", model)
            return cls(
                provider="local",
                base_url=os.environ.get("CLAUDEPHONE_LOCAL_URL", LOCAL_BASE),
                api_key=os.environ.get("CLAUDEPHONE_LOCAL_KEY", "sk-none"),
                model=model,
                tool_mode=os.environ.get("CLAUDEPHONE_TOOL_MODE", "auto"),
                role=role,
            )
        decider = os.environ.get("CLAUDEPHONE_MODEL", DEFAULT_REMOTE_MODEL)
        model = (os.environ.get("CLAUDEPHONE_HELPER_MODEL") or decider
                 if role == "helper" else decider)
        return cls(
            provider="openrouter",
            base_url=os.environ.get("CLAUDEPHONE_BASE_URL", OPENROUTER_BASE),
            api_key=_read_key(),
            model=model,
            tool_mode=os.environ.get("CLAUDEPHONE_TOOL_MODE", "auto"),
            role=role,
        )


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _balanced_objects(text: str):
    """Yield (start, end, source) for every balanced {...} span in `text`.

    A regex cannot do this: the arguments object is itself nested, so any
    non-greedy pattern stops at the *inner* closing brace and hands json.loads
    something unbalanced. Braces inside strings are skipped, so a caption
    containing '}' does not derail the scan.
    """
    depth = start = 0
    in_str = escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    yield start, i + 1, text[start:i + 1]


def parse_json_tool_call(text: str) -> tuple[str, list[dict]]:
    """Pull a {"tool": ..., "args": {...}} object out of prose.

    Returns (remaining_prose, tool_calls). Small models fence inconsistently and
    frequently emit the object bare, so both forms are accepted, as are the
    `name`/`arguments` spellings that some models copy from the OpenAI schema.
    """
    candidates: list[tuple[int, int, str]] = []
    m = _FENCE.search(text)
    if m:
        candidates.append((m.start(), m.end(), m.group(1)))
    candidates.extend(_balanced_objects(text))

    for start, end, raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name")
        if not name or not isinstance(name, str):
            continue
        args = obj.get("args")
        if args is None:
            args = obj.get("arguments")
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        prose = (text[:start] + text[end:]).strip()
        return prose, [{"id": "call_" + str(int(time.time() * 1000)),
                        "name": name, "args": args}]
    return text.strip(), []


class Chat:
    """One conversation endpoint. Stateless - the loop owns the messages."""

    # 429 back-off, in seconds, when the server does not say how long to wait.
    # OpenRouter's free tier allows 20 requests a minute.
    RETRY_WAITS = (4.0, 10.0, 25.0)

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self._native_ok = cfg.tool_mode != "json"
        # Token counts are ints; `cost` is a float in credits (= USD).
        self.total_usage: dict[str, Any] = {}
        self.requests = 0                    # completions that came back
        self.free_requests = 0               # ... of which on a rationed free model
        self.rate_limited = 0                # 429s absorbed by waiting

    # -- http ----------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        url = self.cfg.base_url.rstrip("/") + path
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (self.cfg.api_key or "sk-none"),
        }
        if self.cfg.provider == "openrouter":
            # OpenRouter asks for these; they also make the app identifiable in
            # your usage dashboard, which matters when you are watching spend.
            headers["HTTP-Referer"] = "https://github.com/Arnavvs/ClaudePhone"
            headers["X-Title"] = "ClaudePhone"
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        waits = list(self.RETRY_WAITS)
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:800]
                if e.code == 429 and waits:
                    # Rate limited, not refused: wait as long as asked, else back
                    # off. A daily free-tier cap also answers 429, and waiting
                    # cannot fix that - it surfaces once the retries run out.
                    try:
                        wait = float(e.headers.get("Retry-After") or waits[0])
                    except (TypeError, ValueError):
                        wait = waits[0]
                    waits.pop(0)
                    self.rate_limited += 1
                    time.sleep(min(max(wait, 1.0), 60.0))
                    continue
                if e.code == 402:
                    raise ModelError(
                        "HTTP 402 from " + url + ": no credits on this account. "
                        "Use a ':free' model (CLAUDEPHONE_MODEL=...:free) or add "
                        "credits. " + detail[:300]) from None
                raise ModelError("HTTP " + str(e.code) + " from " + url + ": "
                                 + detail) from None
            except urllib.error.URLError as e:
                raise ModelError(
                    "cannot reach " + url + " (" + str(e.reason) + "). "
                    + ("Is llama-server running?" if self.cfg.provider == "local"
                       else "Check network / OPENROUTER_API_KEY.")) from None

    # -- completion ----------------------------------------------------------

    def complete(self, messages: list[dict],
                 tools: Optional[list[dict]] = None) -> Reply:
        t0 = time.time()
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        use_native = bool(tools) and self._native_ok
        if use_native:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            data = self._post("/chat/completions", payload)
        except ModelError as e:
            # An endpoint that cannot do tools usually 400s on the parameter.
            if use_native and self.cfg.tool_mode == "auto" and "400" in str(e):
                self._native_ok = False
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                data = self._post("/chat/completions", payload)
            else:
                raise

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        calls: list[dict] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {"_unparsed": raw}
            calls.append({"id": tc.get("id") or "call", "name": fn.get("name"),
                          "args": args})
        if not calls and content:
            content, calls = parse_json_tool_call(content)

        usage = data.get("usage") or {}
        for k, v in usage.items():
            # bool is an int subclass; OpenRouter sends is_byok as one.
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                self.total_usage[k] = self.total_usage.get(k, 0) + v
        self.requests += 1
        if is_free_model(self.cfg.model):
            self.free_requests += 1
        return Reply(content=content, tool_calls=calls, usage=usage,
                     finish_reason=choice.get("finish_reason") or "",
                     model=data.get("model") or self.cfg.model,
                     ms=int((time.time() - t0) * 1000))

    @property
    def cost(self) -> float:
        """What this chat has cost so far, in credits (= USD)."""
        return float(self.total_usage.get("cost") or 0.0)

    @property
    def tool_convention(self) -> str:
        return "native" if self._native_ok else "json"

    def health(self) -> dict:
        """Cheap reachability probe - one token, so it costs almost nothing."""
        t0 = time.time()
        try:
            r = self.complete([{"role": "user", "content": "ping"}])
            return {"ok": True, "provider": self.cfg.provider,
                    "model": r.model, "ms": int((time.time() - t0) * 1000),
                    "tool_convention": self.tool_convention}
        except ModelError as e:
            return {"ok": False, "provider": self.cfg.provider,
                    "model": self.cfg.model, "error": str(e)[:300]}


def key_status(cfg: Optional[ModelConfig] = None, timeout: float = 20.0) -> dict:
    """What the OpenRouter key has left. Does not consume the free-model quota.

    -> {ok, is_free_tier, free_remaining, free_limit, limit_remaining, usage,
        expires_at} or {ok: False, error}. Never includes the key or its label.
    """
    cfg = cfg or ModelConfig.from_env()
    if cfg.provider != "openrouter":
        return {"ok": False, "error": "not an OpenRouter config"}
    if not cfg.api_key:
        return {"ok": False, "error": "no key: set OPENROUTER_API_KEY or "
                                      "OPENROUTER_API_KEY_FILE"}
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/key",
                                 headers={"Authorization": "Bearer " + cfg.api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = (json.loads(r.read().decode()) or {}).get("data") or {}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:200]}
    free = d.get("free_model_daily_requests") or {}
    return {"ok": True,
            "is_free_tier": d.get("is_free_tier"),
            "free_remaining": free.get("remaining"),
            "free_limit": free.get("limit"),
            "limit_remaining": d.get("limit_remaining"),
            "usage": d.get("usage"),
            "usage_daily": d.get("usage_daily"),
            "expires_at": d.get("expires_at")}
