"""The phone-side HTTP server: how a laptop hands work to the phone.

Two endpoints matter, and the difference between them is the whole argument for
this project:

  POST /tool   one tool call, one response.        <- proxy mode
  POST /task   a goal; the phone runs the entire   <- delegate mode
               agent loop locally and streams back
               what it did.

Proxy mode is what an MCP setup does today: every step crosses the network, and
the laptop's model waits on each one. Delegate mode sends one message, and the
phone does thirty tool calls against localhost before answering. Proxy mode is
kept because it is genuinely better for debugging and for one-off pokes.

Streaming is NDJSON, one event object per line, flushed as it happens - so a
watching client sees each tap as it lands rather than a report at the end.

## Security

This endpoint can do anything to the phone. It binds 127.0.0.1 by default; to
expose it on the LAN you must pass --host explicitly, and a bearer token is
then required. The token is generated on first run and written to
~/.claudephone/token with 0600.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import device as dev
from .agent import build_agent, build_registry
from .harness.loop import Budget

CONFIG_DIR = os.path.expanduser("~/.claudephone")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")


def get_token(create: bool = True) -> str:
    if os.environ.get("CLAUDEPHONE_TOKEN"):
        return os.environ["CLAUDEPHONE_TOKEN"]
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    if not create:
        return ""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    with open(TOKEN_PATH, "w") as f:
        f.write(tok)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudePhone"
    token = ""
    require_auth = False

    def log_message(self, fmt, *args):
        print("[http] " + (fmt % args), flush=True)

    # -- plumbing ------------------------------------------------------------

    def _authorised(self) -> bool:
        if not self.require_auth:
            return True
        got = self.headers.get("Authorization", "")
        return got == "Bearer " + self.token

    def _send(self, code: int, obj, ctype: str = "application/json") -> None:
        body = (obj if isinstance(obj, bytes)
                else json.dumps(obj, default=str).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return {}

    def _stream_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _emit(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
        self.wfile.flush()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    # -- routes --------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, self._health())
        if not self._authorised():
            return self._send(401, {"error": "bad or missing bearer token"})
        if path == "/tools":
            reg = build_registry()
            return self._send(200, {
                "total": len(reg.tools),
                "packs": reg.packs(),
                "tools": [{"name": t.name, "pack": t.pack,
                           "dangerous": t.dangerous,
                           "description": t.description.split(". ")[0][:200],
                           "parameters": t.schema}
                          for t in reg.tools.values()],
            })
        return self._send(404, {"error": "no such path: " + path})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._authorised():
            return self._send(401, {"error": "bad or missing bearer token"})
        body = self._body()
        if path == "/tool":
            return self._do_tool(body)
        if path == "/task":
            return self._do_task(body)
        return self._send(404, {"error": "no such path: " + path})

    # -- handlers ------------------------------------------------------------

    def _health(self) -> dict:
        info: dict = {"ok": True, "service": "claudephone",
                      "on_device": dev.on_device(), "time": time.time()}
        try:
            info["serial"] = dev.default_serial()
            fg = dev.foreground()
            info["foreground"] = fg.get("package")
            info["adb"] = "ok"
        except Exception as e:
            info["adb"] = "unavailable: " + str(e)[:200]
            info["ok"] = False
        return info

    def _do_tool(self, body: dict):
        name = body.get("tool") or body.get("name")
        if not name:
            return self._send(400, {"error": "give a 'tool' name"})
        reg = build_registry()
        reg.active_packs = set(reg.packs())      # proxy mode: everything visible
        t0 = time.time()
        result = reg.call(name, body.get("args") or {})
        return self._send(200, {"tool": name, "result": result,
                                "ms": int((time.time() - t0) * 1000)})

    def _do_task(self, body: dict):
        goal = (body.get("goal") or body.get("task") or "").strip()
        if not goal:
            return self._send(400, {"error": "give a 'goal'"})
        budget = Budget(
            max_steps=int(body.get("max_steps") or 30),
            max_seconds=float(body.get("max_seconds") or 900),
        )
        try:
            agent = build_agent(
                provider=body.get("provider") or "",
                model=body.get("model") or "",
                mode=body.get("mode") or "auto",
                allow=body.get("allow") or [],
                deny=body.get("deny") or [],
                packs=body.get("packs") or [],
                budget=budget,
                operator_notes=body.get("notes") or "",
                allow_writes=body.get("allow_writes") or [],
                allow_rules=body.get("allow_rules") or [],
                allow_uncounted_reads=bool(body.get("allow_uncounted_reads")),
                stagnation=bool(body.get("stagnation", True)),
            )
        except Exception as e:
            return self._send(500, {"error": "could not build agent: "
                                             + str(e)[:400]})
        self._stream_start()
        try:
            for event in agent.run(goal):
                self._emit(event)
        except BrokenPipeError:
            print("[http] client disconnected mid-task", flush=True)
        except Exception as e:
            try:
                self._emit({"type": "error", "where": "server",
                            "error": type(e).__name__ + ": " + str(e)[:400]})
            except OSError:
                pass


def serve(host: str = "127.0.0.1", port: int = 8765,
          token: Optional[str] = None) -> None:
    exposed = host not in ("127.0.0.1", "localhost")
    Handler.token = token or get_token()
    Handler.require_auth = exposed or bool(os.environ.get("CLAUDEPHONE_TOKEN"))

    httpd = ThreadingHTTPServer((host, port), Handler)
    where = lan_ip() if exposed else "127.0.0.1"

    print("ClaudePhone server on http://" + host + ":" + str(port), flush=True)
    print("  on_device : " + str(dev.on_device()), flush=True)
    print("  reachable : http://" + where + ":" + str(port), flush=True)
    if Handler.require_auth:
        print("  auth      : REQUIRED", flush=True)
        print("  token     : " + Handler.token, flush=True)
    else:
        print("  auth      : none (loopback only)", flush=True)
    print("  endpoints : GET /health  GET /tools  POST /tool  POST /task",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
        httpd.shutdown()
