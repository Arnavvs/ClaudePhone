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

    @classmethod
    def from_env(cls, provider: str = "") -> "ModelConfig":
        provider = (provider or os.environ.get("CLAUDEPHONE_PROVIDER")
                    or "openrouter").lower()
        if provider == "local":
            return cls(
                provider="local",
                base_url=os.environ.get("CLAUDEPHONE_LOCAL_URL", LOCAL_BASE),
                api_key=os.environ.get("CLAUDEPHONE_LOCAL_KEY", "sk-none"),
                model=os.environ.get("CLAUDEPHONE_LOCAL_MODEL",
                                     DEFAULT_LOCAL_MODEL),
                tool_mode=os.environ.get("CLAUDEPHONE_TOOL_MODE", "auto"),
            )
        return cls(
            provider="openrouter",
            base_url=os.environ.get("CLAUDEPHONE_BASE_URL", OPENROUTER_BASE),
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            model=os.environ.get("CLAUDEPHONE_MODEL", DEFAULT_REMOTE_MODEL),
            tool_mode=os.environ.get("CLAUDEPHONE_TOOL_MODE", "auto"),
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

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg
        self._native_ok = cfg.tool_mode != "json"
        self.total_usage: dict[str, int] = {}

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
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:800]
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
            if isinstance(v, int):
                self.total_usage[k] = self.total_usage.get(k, 0) + v
        return Reply(content=content, tool_calls=calls, usage=usage,
                     finish_reason=choice.get("finish_reason") or "",
                     model=data.get("model") or self.cfg.model,
                     ms=int((time.time() - t0) * 1000))

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
