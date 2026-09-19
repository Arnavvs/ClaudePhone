"""The phone-side HTTP server: how a laptop hands work to the phone.

Two endpoints matter, and the difference between them is the whole argument for
this project:

  POST /tool   one tool call, one response.        <- proxy mode
  POST /task   a goal; the phone runs the entire   <- delegate mode
               agent loop locally and streams back
               what it did.
  POST /reply  answers a question the running task <- the other direction
               asked with `ask_operator`.

Task control (B12), so a laptop session can start a long task and get on with
something else - ARTEMIS's task-level surface:

  POST /task {"background": true}   start, return the session_id at once
  GET  /tasks                       every task this server has run
  GET  /task/<id>                   status, recent steps, a pending question
  POST /task/<id>/stop              end it at the next step boundary
  GET  /runs, /runs/<run_id|latest> recorded runs, with any verdict (B8)

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

# Questions a running task is blocked on (B3): id -> {question, answer, event}.
# The server is threaded, so a blocked /task does not block the /reply that
# unblocks it.
PENDING: dict = {}
PENDING_LOCK = threading.Lock()

# Tasks this server has run (B12): session_id -> state. Kept in memory only;
# the run file (harness/recorder.py) is the durable record.
TASKS: dict = {}
TASKS_LOCK = threading.Lock()
KEEP_EVENTS = 200
KEEP_TASKS = 50


def _compact_event(ev: dict) -> dict:
    """A task event small enough to poll: no full screen dumps."""
    kind = ev.get("type")
    if kind == "tool_result":
        res = ev.get("result") or {}
        return {"type": kind, "step": ev.get("step"), "tool": ev.get("tool"),
                "ok": ev.get("ok"),
                "result": ({"error": res.get("error"), "next": res.get("next")}
                           if "error" in res else
                           {k: v for k, v in list(res.items())[:6]
                            if not k.startswith("_") and k != "elements"})}
    if kind == "thought":
        return {"type": kind, "content": str(ev.get("content") or "")[:400]}
    return ev


def task_status(sid: str) -> Optional[dict]:
    with TASKS_LOCK:
        t = TASKS.get(sid)
        if t is None:
            return None
        events = list(t["events"])
    with PENDING_LOCK:
        ask = PENDING.get(sid)
    out = {"session_id": sid, "goal": t["goal"], "status": t["status"],
           "started": t["started"], "run_id": t.get("run_id"),
           "steps": t.get("steps", 0), "recent": events[-12:]}
    if ask is not None:
        out["waiting_for_answer"] = ask["question"]
        out["how"] = "phone_reply(session_id, answer) or POST /reply"
    if t.get("final") is not None:
        out["final"] = t["final"]
    if t.get("verification") is not None:
        out["verification"] = t["verification"]
    return out


def _track(sid: str, goal: str, agent) -> dict:
    from collections import deque
    t = {"goal": goal, "status": "running", "started": time.time(),
         "agent": agent, "events": deque(maxlen=KEEP_EVENTS), "steps": 0,
         "final": None}
    with TASKS_LOCK:
        TASKS[sid] = t
        if len(TASKS) > KEEP_TASKS:
            done = [k for k, v in TASKS.items() if v["status"] != "running"]
            for k in done[:len(TASKS) - KEEP_TASKS]:
                TASKS.pop(k, None)
    return t


def _note(t: dict, ev: dict) -> None:
    kind = ev.get("type")
    if kind == "start":
        t["run_id"] = ev.get("run_id")
    elif kind == "tool_call":
        t["steps"] = ev.get("step") or t["steps"]
    elif kind == "final":
        t["final"] = {k: ev.get(k) for k in ("content", "stopped_by", "steps",
                                             "seconds", "cost_usd")}
        t["status"] = ("stopped" if str(ev.get("stopped_by") or "")
                       .startswith("operator_stop") else "done")
    elif kind == "verification":
        t["verification"] = {"verdict": ev.get("verdict"),
                             "decided_by": ev.get("decided_by")}
    elif kind == "error":
        t["status"] = "error"
    t["events"].append(_compact_event(ev))


def run_summary(run_id: str) -> dict:
    """A recorded run, compact enough for a laptop model's context."""
    from .harness import recorder
    if run_id == "latest":
        ids = recorder.list_runs(limit=1)
        if not ids:
            return {"error": "no runs recorded yet"}
        run_id = ids[0]
    rows = recorder.load(run_id)
    if not rows:
        return {"error": "no such run: " + run_id,
                "next": "GET /runs lists recorded run ids"}
    s = recorder.summarise(run_id)
    steps = []
    for r in rows:
        if r.get("type") == "tool_call":
            steps.append({"step": r.get("step"), "tool": r.get("tool"),
                          "args": r.get("args")})
        elif r.get("type") == "tool_result" and steps:
            res = r.get("result") or {}
            steps[-1]["ok"] = r.get("ok")
            if "error" in res:
                steps[-1]["error"] = str(res["error"])[:200]
    s["steps_detail"] = steps[-40:]
    s["decisions_logged"] = sum(1 for r in rows if r.get("type") == "decision")
    return s

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
        if path == "/tasks":
            with TASKS_LOCK:
                ids = list(TASKS)
            return self._send(200, {"tasks": [task_status(i) for i in ids]})
        if path.startswith("/task/"):
            st = task_status(path[len("/task/"):])
            return self._send(200 if st else 404, st or {
                "error": "no such task", "next": "GET /tasks lists them"})
        if path == "/runs":
            from .harness import recorder
            return self._send(200, {"runs": [
                {k: v for k, v in recorder.summarise(i).items()
                 if k in ("run_id", "started", "goal", "model", "steps",
                          "outcome", "stopped_by", "verdict")}
                for i in recorder.list_runs(limit=20)]})
        if path.startswith("/runs/"):
            out = run_summary(path[len("/runs/"):])
            return self._send(404 if "error" in out else 200, out)
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
        if path == "/reply":
            return self._do_reply(body)      # body is already read, above
        if path == "/task":
            return self._do_task(body)
        if path.startswith("/task/") and path.endswith("/stop"):
            sid = path[len("/task/"):-len("/stop")]
            with TASKS_LOCK:
                t = TASKS.get(sid)
            if t is None:
                return self._send(404, {"error": "no such task",
                                        "next": "GET /tasks lists them"})
            if t["status"] != "running":
                return self._send(200, {"session_id": sid, "status": t["status"],
                                        "note": "already finished"})
            t["agent"].stop_requested = str(body.get("reason") or "requested")[:80]
            # A task blocked on a question would not reach a step boundary.
            with PENDING_LOCK:
                slot = PENDING.get(sid)
            if slot is not None:
                slot["event"].set()
            return self._send(200, {"session_id": sid, "stopping": True,
                                    "note": "stops at the next step boundary; "
                                            "a tool call in progress finishes"})
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

    def _do_reply(self, body: dict):
        sid = str(body.get("session_id") or "").strip()
        with PENDING_LOCK:
            slot = PENDING.get(sid)
        if slot is None:
            return self._send(404, {"error": "no task is waiting on that "
                                             "session_id",
                                    "waiting": sorted(PENDING)})
        slot["answer"] = str(body.get("answer") or body.get("reply") or "")
        slot["event"].set()
        return self._send(200, {"ok": True, "session_id": sid,
                                "question": slot["question"]})

    def _do_task(self, body: dict):
        goal = (body.get("goal") or body.get("task") or "").strip()
        if not goal:
            return self._send(400, {"error": "give a 'goal'"})
        budget = Budget(
            max_steps=int(body.get("max_steps") or 30),
            max_seconds=float(body.get("max_seconds") or 900),
            max_usd=float(body.get("max_usd") if body.get("max_usd") is not None
                          else 0.25),
            free_reserve=int(body.get("free_reserve") or 2),
        )
        session_id = secrets.token_hex(8)
        background = bool(body.get("background"))
        holder: dict = {}

        def ask_operator(question: str, timeout_s: float):
            """Block this task until POST /reply answers, or the wait ends."""
            slot = {"question": question, "answer": None,
                    "event": threading.Event()}
            with PENDING_LOCK:
                PENDING[session_id] = slot
            try:
                ask = {"type": "ask", "session_id": session_id,
                       "question": question, "timeout_s": timeout_s,
                       "how": "POST /reply {\"session_id\": \"" + session_id
                              + "\", \"answer\": \"...\"}"}
                if "task" in holder:
                    _note(holder["task"], ask)
                if not background:
                    self._emit(ask)
                slot["event"].wait(timeout=max(1.0, float(timeout_s)))
                return slot["answer"]
            finally:
                with PENDING_LOCK:
                    PENDING.pop(session_id, None)

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
                on_ask_operator=ask_operator,
                helper_model=body.get("helper_model") or "",
                summarize=bool(body.get("summarize")),
                verify_spec=body.get("verify") or None,
                judge=bool(body.get("judge")),
            )
        except Exception as e:
            return self._send(500, {"error": "could not build agent: "
                                             + str(e)[:400]})
        task = _track(session_id, goal, agent)
        holder["task"] = task

        if background:
            def work():
                try:
                    for event in agent.run(goal):
                        _note(task, event)
                except Exception as e:
                    _note(task, {"type": "error", "where": "server",
                                 "error": type(e).__name__ + ": " + str(e)[:400]})
                finally:
                    if task["status"] == "running":
                        task["status"] = "done"
            threading.Thread(target=work, daemon=True,
                             name="task-" + session_id).start()
            return self._send(200, {"session_id": session_id, "status": "running",
                                    "next": "GET /task/" + session_id
                                            + " for progress; POST /task/"
                                            + session_id + "/stop to end it"})

        self._stream_start()
        try:
            self._emit({"type": "session", "session_id": session_id})
            for event in agent.run(goal):
                _note(task, event)
                self._emit(event)
        except BrokenPipeError:
            print("[http] client disconnected mid-task", flush=True)
            # The generator is closed with the connection; the task is over.
            task["status"] = "abandoned"
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
    print("  endpoints : GET /health  GET /tools  POST /tool  POST /task  "
          "POST /reply  GET /tasks  GET /task/<id>  POST /task/<id>/stop  "
          "GET /runs[/<id>]", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
        httpd.shutdown()
