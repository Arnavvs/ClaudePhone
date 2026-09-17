"""The ledger, reachable from the phone (B2c).

The gate in `writes.py` / `reads.py` reads ceilings through datacollect's
`ledger.budget_for` and records through `Ledger.record`. Both need
`datacollect/collect.db`, which exists on the laptop and not in Termux, so an
agent running ON the phone had every budgeted write and counted read refused.

This is the missing half: the laptop serves the ledger on loopback, and the
phone reaches it through `adb reverse`, the mirror of the bridge's `adb forward`.

    laptop$  python -m claudephone.policy.ledger_service --serial RZ8N70HYQSB
    phone$   export CLAUDEPHONE_LEDGER_URL=http://127.0.0.1:8770
    phone$   export CLAUDEPHONE_LEDGER_TOKEN=<printed by the service>

Nothing changes when `CLAUDEPHONE_LEDGER_URL` is unset: the gate opens the local
database exactly as before.

**The service is the only writer to the real numbers.** The phone cannot pass a
ceiling of its own: it sends an account, an action and a count, and the service
answers from `budget_for` (so `ACCOUNT_SCALE`, so @saravbhaita at 25%). A client
that cannot reach the service gets `LedgerUnavailable`, which fails closed.

Auth mirrors the bridge: a token in `~/.claudephone/ledger_token` (0600),
required on every request except `/health`, compared in constant time. The
service binds 127.0.0.1 and refuses to bind anything else - `adb reverse` gives
the phone a path to loopback without exposing the database to the network.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

PORT = 8770
TOKEN_HEADER = "X-Ledger-Token"
TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".claudephone", "ledger_token")


class LedgerUnavailable(RuntimeError):
    """No usable ledger: no local database, or the service is not answering.

    Defined here rather than in writes.py so a remote failure mid-run is the
    same exception as a missing local one, and every catch site fails closed.
    policy/writes.py re-exports it under this name.
    """


LedgerServiceError = LedgerUnavailable


# ---------------------------------------------------------------- token

def token(path: str = TOKEN_PATH, create: bool = True) -> str:
    """The shared token, created on first use with 0600 permissions."""
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except FileNotFoundError:
        pass
    if not create:
        return ""
    t = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(t)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return t


# ---------------------------------------------------------------- client

class RemoteLedger:
    """Stands in for datacollect's `Ledger`, over HTTP.

    Only the four calls the gate makes are implemented - `budget`, `can`,
    `count`, `record` - with the same signatures and return shapes, so
    policy/writes.py and policy/reads.py cannot tell the difference.
    """

    def __init__(self, url: str, account: str, device: str = "",
                 run_id: str = "", tok: str = "", timeout: float = 8.0):
        self.url = url.rstrip("/")
        self.account = account
        self.device = device
        self.run_id = run_id
        self.timeout = timeout
        self.token = tok or os.environ.get("CLAUDEPHONE_LEDGER_TOKEN", "") or token(create=False)

    def _get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        q = urllib.parse.urlencode(dict(account=self.account, **params))
        req = urllib.request.Request(self.url + path + "?" + q)
        if self.token:
            req.add_header(TOKEN_HEADER, self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = json.loads(e.read().decode() or "{}").get("error", "")
            except Exception:
                pass
            raise LedgerServiceError("ledger service HTTP %d: %s" % (e.code, body or "")) from None
        except Exception as e:
            raise LedgerServiceError("ledger service unreachable at " + self.url
                                     + ": " + str(e)[:120]) from None

    def budget(self, action: str) -> dict:
        return self._get("/budget", action=action)["budget"]

    def can(self, action: str, n: int = 1):
        r = self._get("/can", action=action, n=n)
        return bool(r["ok"]), r.get("why", "")

    def count(self, action: str, minutes: float) -> int:
        return int(self._get("/count", action=action, minutes=minutes)["count"])

    def record(self, action: str, target: str = "", ok: bool = True,
               note: str = "") -> None:
        self._get("/record", action=action, target=target, note=note,
                  ok=int(bool(ok)), device=self.device, run_id=self.run_id)


def configured_url() -> str:
    return os.environ.get("CLAUDEPHONE_LEDGER_URL", "").strip()


def available(url: str = "", timeout: float = 3.0) -> dict:
    url = (url or configured_url()).rstrip("/")
    if not url:
        return {"ok": False, "why": "CLAUDEPHONE_LEDGER_URL is not set"}
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            h = json.loads(r.read().decode())
        return {"ok": bool(h.get("ok")), "url": url, **h}
    except Exception as e:
        return {"ok": False, "url": url, "why": type(e).__name__ + ": " + str(e)[:120]}


# ---------------------------------------------------------------- server

def _ledger_module():
    from . import writes as wr
    return wr._ledger_module()


class _Handler(BaseHTTPRequestHandler):
    server_version = "ClaudePhoneLedger/1.0"
    expected_token = ""

    def log_message(self, fmt, *args):        # quiet; the run log is the ledger
        pass

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:                 # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/health":
            return self._send(200, {"ok": True, "service": "claudephone-ledger",
                                    "auth": "required" if self.expected_token else "open"})
        if self.expected_token:
            got = self.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(got, self.expected_token):
                return self._send(401, {"error": "bad or missing " + TOKEN_HEADER})
        account = (q.get("account") or "").strip()
        action = (q.get("action") or "").strip()
        if not account or not action:
            return self._send(400, {"error": "account and action are required"})
        try:
            lg, store = _ledger_module()
            led = lg.Ledger(store.connect(), account=account,
                            device=q.get("device", ""), run_id=q.get("run_id", ""))
            if u.path == "/budget":
                return self._send(200, {"budget": lg.budget_for(account, action),
                                        "scale_note": lg.scale_note(account)})
            if u.path == "/can":
                ok, why = led.can(action, n=int(q.get("n", 1)))
                return self._send(200, {"ok": bool(ok), "why": why})
            if u.path == "/count":
                return self._send(200, {"count": led.count(action, float(q.get("minutes", 1)))})
            if u.path == "/record":
                led.record(action, target=q.get("target", ""),
                           ok=bool(int(q.get("ok", 1))), note=q.get("note", ""))
                return self._send(200, {"recorded": True})
        except Exception as e:
            return self._send(500, {"error": type(e).__name__ + ": " + str(e)[:200]})
        return self._send(404, {"error": "no such path"})


def serve(port: int = PORT, tok: str = "", block: bool = True):
    """Serve the ledger on 127.0.0.1. -> the HTTPServer (already started)."""
    _Handler.expected_token = tok if tok is not None else ""
    httpd = HTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=httpd.serve_forever, name="claudephone-ledger",
                         daemon=True)
    t.start()
    if block:
        try:
            t.join()
        except KeyboardInterrupt:
            httpd.shutdown()
    return httpd


def main(argv=None) -> int:
    import argparse

    from .. import device as dev

    ap = argparse.ArgumentParser(
        prog="python -m claudephone.policy.ledger_service",
        description="Serve the datacollect ledger to a phone over adb reverse.")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--serial", default="", help="set up adb reverse for this phone")
    ap.add_argument("--no-auth", action="store_true",
                    help="serve without a token (loopback only; not recommended)")
    a = ap.parse_args(argv)

    tok = "" if a.no_auth else token()
    try:
        lg, store = _ledger_module()
    except Exception as e:
        print("no ledger to serve:", e)
        return 2
    print("serving", store.DB, "on http://127.0.0.1:%d" % a.port)
    if a.serial:
        dev.adb("reverse", "tcp:%d" % a.port, "tcp:%d" % a.port,
                serial=a.serial, check=False)
        print("adb reverse set for", a.serial)
    print("\non the phone:")
    print("  export CLAUDEPHONE_LEDGER_URL=http://127.0.0.1:%d" % a.port)
    if tok:
        print("  export CLAUDEPHONE_LEDGER_TOKEN=%s" % tok)
    print("\nCtrl-C to stop.")
    serve(a.port, tok, block=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
