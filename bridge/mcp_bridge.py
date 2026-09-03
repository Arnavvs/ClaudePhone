"""Laptop-side MCP bridge: hand a whole task to the phone.

Register this with Claude Code on your laptop and you get four tools. The
important one is `phone_task`, which sends a *goal* and returns a transcript of
what the phone did on its own. The laptop model spends one round trip; the
phone spends thirty, all against localhost.

    laptop                          phone
    ------                          -----
    phone_task("...")   ------->    plan
                                    ui_dump    (local)
                                    tap        (local)
                                    ui_dump    (local)      x30
                                    ...
                        <-------    transcript + answer

`phone_tool` is the escape hatch for a single call when you are debugging and
want to poke one thing.

Run the phone side first:

    claudephone serve --host 0.0.0.0        # on the phone
    export CLAUDEPHONE_URL=http://<phone-ip>:8765
    export CLAUDEPHONE_TOKEN=<token printed by serve>
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

URL = os.environ.get("CLAUDEPHONE_URL", "http://127.0.0.1:8765").rstrip("/")
TOKEN = os.environ.get("CLAUDEPHONE_TOKEN", "")
TIMEOUT = int(os.environ.get("CLAUDEPHONE_TIMEOUT", "1200"))


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    return h


def _get(path: str, timeout: int = 30):
    req = urllib.request.Request(URL + path, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_json(path: str, payload: dict, timeout: int = TIMEOUT):
    req = urllib.request.Request(URL + path, data=json.dumps(payload).encode(),
                                 headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_stream(path: str, payload: dict, timeout: int = TIMEOUT):
    """Consume the NDJSON event stream, yielding each event as it arrives."""
    req = urllib.request.Request(URL + path, data=json.dumps(payload).encode(),
                                 headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "raw", "line": line[:400]}


def _unreachable(e: Exception) -> dict:
    return {
        "error": "cannot reach the phone at " + URL + ": " + str(e)[:200],
        "checklist": [
            "is `claudephone serve --host 0.0.0.0` running on the phone?",
            "is CLAUDEPHONE_URL pointing at the phone's current LAN IP?",
            "is CLAUDEPHONE_TOKEN the token that `serve` printed?",
            "are laptop and phone on the same network, without AP isolation?",
        ],
    }


def main() -> None:
    from mcp.server import MCPServer

    mcp = MCPServer(
        name="claudephone-bridge",
        instructions=(
            "Delegates work to an agent running on an Android phone. Prefer "
            "phone_task: describe the GOAL and let the phone run its own loop "
            "locally, which is far faster than driving it step by step. Use "
            "phone_tool only to inspect or poke a single thing."
        ),
    )

    @mcp.tool(
        description=(
            "Hand a whole goal to the phone agent and get back what it did. "
            "The phone runs its own see-act-see loop locally, so describe the "
            "OUTCOME you want ('open Instagram and tell me the top 3 reels' "
            "captions'), not individual taps. Returns the tool-by-tool "
            "transcript plus the phone's final answer."
        )
    )
    def phone_task(goal: str, max_steps: int = 30, max_seconds: int = 900,
                   packs: list[str] = [], mode: str = "auto",
                   model: str = "", provider: str = "",
                   notes: str = "") -> dict:
        payload = {"goal": goal, "max_steps": max_steps,
                   "max_seconds": max_seconds, "packs": packs, "mode": mode,
                   "model": model, "provider": provider, "notes": notes}
        steps, final, errors = [], None, []
        try:
            for ev in _post_stream("/task", payload):
                kind = ev.get("type")
                if kind == "tool_call":
                    steps.append({"step": ev.get("step"), "tool": ev.get("tool"),
                                  "args": ev.get("args")})
                elif kind == "tool_result" and steps:
                    res = ev.get("result") or {}
                    steps[-1]["ok"] = ev.get("ok")
                    steps[-1]["ms"] = ev.get("ms")
                    steps[-1]["result"] = (
                        {"error": res["error"]} if "error" in res
                        else {k: v for k, v in list(res.items())[:6]
                              if not k.startswith("_")})
                elif kind == "final":
                    final = ev
                elif kind == "error":
                    errors.append(ev)
        except (urllib.error.URLError, OSError) as e:
            return _unreachable(e)

        out: dict = {"goal": goal, "steps_taken": len(steps),
                     "transcript": steps}
        if final:
            out["answer"] = final.get("content")
            out["seconds"] = final.get("seconds")
            out["usage"] = final.get("usage")
            if final.get("stopped_by"):
                out["stopped_by"] = final["stopped_by"]
        if errors:
            out["errors"] = errors
        return out

    @mcp.tool(
        description=(
            "Call ONE tool on the phone and return its raw result. For "
            "inspection and debugging - for real work use phone_task, which "
            "does not pay a network round trip per step."
        )
    )
    def phone_tool(tool: str, args: dict = {}) -> dict:
        try:
            return _post_json("/tool", {"tool": tool, "args": args})
        except (urllib.error.URLError, OSError) as e:
            return _unreachable(e)

    @mcp.tool(description="List every tool the phone offers, with its pack and "
                          "parameters.")
    def phone_tools(pack: str = "") -> dict:
        try:
            data = _get("/tools")
        except (urllib.error.URLError, OSError) as e:
            return _unreachable(e)
        tools = data.get("tools", [])
        if pack:
            tools = [t for t in tools if t.get("pack") == pack]
        return {"total": data.get("total"), "packs": data.get("packs"),
                "tools": tools}

    @mcp.tool(description="Check that the phone is reachable and which app is "
                          "in its foreground.")
    def phone_status() -> dict:
        try:
            return _get("/health")
        except (urllib.error.URLError, OSError) as e:
            return _unreachable(e)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
