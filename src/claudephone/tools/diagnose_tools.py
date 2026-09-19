"""diagnose: what is wrong with the phone link, and the fixes that are safe (B12).

`claudephone doctor` answers the same question for a person at a terminal. A
model, or a laptop Claude Code session, needs it as a tool that returns data,
and one that can apply the few fixes known to be harmless:

* **adb offline, or the device missing** on a laptop -> `adb kill-server` then
  `adb start-server`. Refused while another process holds a phone lock
  (datacollect's guard: a collection run or the scheduled A/B would lose its
  connection mid-run), and refused on the phone itself, where it would cut the
  agent's own loopback connection.
* **the bridge installed but not bound** -> re-enable the accessibility
  service, which is what `bridge.heal` already does on its own.

Never applied, only reported:

* `pkill -f uiautomator` - never, by project rule. Killing the instrumentation
  from outside leaves u2 and the accessibility bridge in states that took a
  reboot to clear.
* an unauthorised device, or a locked screen - both need a person. No attempt
  is made to get past a lock screen.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .. import device as dev

# Commands this module may run as a fix. Anything else is out of scope, and a
# test holds the module to it.
SAFE_FIX_COMMANDS = (("kill-server",), ("start-server",))


def _held_by_others() -> list[dict]:
    """Phone locks held by other live processes (datacollect guard)."""
    from ..policy import writes as wr
    d = wr._datacollect_dir()
    if not os.path.isfile(os.path.join(d, "guard.py")):
        return []
    import sys
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import guard                                      # type: ignore
    except Exception:
        return []
    out = []
    for row in dev.list_devices():
        try:
            h = guard.lock_holder(row["serial"])
        except Exception:
            h = None
        if h and int(h.get("pid") or 0) != os.getpid():
            out.append(dict(h, serial=row["serial"]))
    return out


def _restart_adb() -> None:
    import subprocess
    for args in SAFE_FIX_COMMANDS:
        subprocess.run([dev.ADB()] + list(args), capture_output=True, timeout=30)


def diagnose(serial: str = "", attempt_fix: bool = False) -> dict:
    checks: list[dict] = []
    fixes: list[dict] = []
    serial = serial or dev.default_serial() or ""
    on_device = dev.on_device()

    def add(name: str, status: str, detail: str, next_step: str = "") -> dict:
        row: dict[str, Any] = {"check": name, "status": status, "detail": detail}
        if next_step:
            row["next"] = next_step
        checks.append(row)
        return row

    # 1. adb
    def adb_row() -> Optional[dict]:
        rows = dev.list_devices()
        return next((r for r in rows if r["serial"] == serial), None) if serial \
            else (rows[0] if rows else None)

    row = adb_row()
    state = (row or {}).get("state") or "missing"
    if state != "device" and attempt_fix and state in ("offline", "missing"):
        if on_device:
            fixes.append({"fix": "restart adb", "applied": False,
                          "why": "on the phone, kill-server would cut this "
                                 "agent's own loopback connection"})
        else:
            held = _held_by_others()
            if held:
                fixes.append({"fix": "restart adb", "applied": False,
                              "why": "another process holds a phone: " + ", ".join(
                                  "%s (%s)" % (h.get("serial"), h.get("phase") or "?")
                                  for h in held)})
            else:
                _restart_adb()
                row = adb_row()
                state = (row or {}).get("state") or "missing"
                fixes.append({"fix": "restart adb", "applied": True,
                              "now": state})
    if state == "device":
        add("adb", "ok", "connected: " + (row or {}).get("serial", serial))
    elif state == "unauthorized":
        add("adb", "fail", "the phone has not authorised this computer",
            "a person must accept the USB-debugging prompt on the phone")
    else:
        add("adb", "fail", "device " + (serial or "(none)") + " is " + state,
            "check the cable or Wi-Fi pairing; diagnose(attempt_fix=True) "
            "restarts the adb server when nothing else is using a phone")
        return {"ok": False, "serial": serial, "checks": checks, "fixes": fixes}
    serial = (row or {}).get("serial", serial)

    # 2. screen
    try:
        from ..runtime.observer import observer
        h = observer(serial).health()
    except Exception as e:
        h = {"error": str(e)[:120]}
    if h.get("awake") is False:
        add("screen", "warn", "the screen is off",
            "press_key('wake'); reads and taps do nothing while it is off")
    elif h.get("locked"):
        add("screen", "warn", "the phone is on its lock screen",
            "a person must unlock it; this tool never tries to")
    elif "error" in h:
        add("screen", "warn", "could not read the screen state: " + h["error"])
    else:
        add("screen", "ok", "awake and unlocked")

    # 3. the accessibility bridge
    from ..runtime import bridge as br
    try:
        installed = br.Bridge.installed(serial)
        enabled = installed and br.Bridge.enabled(serial)
        up = installed and br.available(serial, recheck=True)
    except Exception as e:
        installed = enabled = up = False
        add("bridge", "warn", "could not check: " + str(e)[:120])
    else:
        if up:
            try:
                ver = br.bridge(serial).health().get("version")
            except Exception:
                ver = None
            add("bridge", "ok", "reachable, version " + str(ver))
        elif not installed:
            add("bridge", "warn", "not installed; screen reads fall back to "
                                  "uiautomator2 (~20x slower)",
                "install it: android/build.sh install")
        else:
            if attempt_fix:
                ok = br.heal(serial)
                fixes.append({"fix": "re-enable the bridge service",
                              "applied": True, "now_reachable": ok})
                up = ok
            if up:
                add("bridge", "ok", "re-enabled and reachable")
            else:
                add("bridge", "fail", "installed but " + (
                    "not enabled" if not enabled else "not answering"),
                    "diagnose(attempt_fix=True) re-enables it; if u2 is running "
                    "it unbinds the bridge device-wide until u2 stops")

    # 4. the ledger (counted reads and writes)
    try:
        from ..policy import writes as wr
        acct = wr.account_for(serial, "ig")
        if acct:
            wr.ledger_for(acct, serial)
            add("ledger", "ok", "reachable; this phone is " + acct)
        else:
            add("ledger", "warn", "this phone is in no account map",
                "counted reads and writes will be refused")
    except Exception as e:
        add("ledger", "warn", "unavailable: " + str(e)[:120],
            "counted reads are refused unless --allow-uncounted-reads")

    bad = [c for c in checks if c["status"] == "fail"]
    return {"ok": not bad, "serial": serial, "on_device": on_device,
            "checks": checks, "fixes": fixes,
            "never": "pkill -f uiautomator is never run, by project rule"}


def register(reg) -> None:

    @reg.tool(
        name="diagnose",
        description=(
            "Find out why the phone is not responding as expected: adb "
            "connection, screen on/locked, the accessibility bridge, the "
            "ledger. Each check says what to do next. attempt_fix=True applies "
            "only safe fixes (restart the adb server when no other process is "
            "using a phone; re-enable the bridge service). It never unlocks "
            "the phone."
        )
    )
    def diagnose_tool(serial: str = "", attempt_fix: bool = False) -> dict:
        return diagnose(serial, attempt_fix)
