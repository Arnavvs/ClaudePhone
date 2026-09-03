"""Client for the on-device AccessibilityService (android/bridge).

This is phase 2 of the backend plan, and it is a much bigger win than expected.
Measured on the target device, same screen, back to back:

    uiautomator2 dump_hierarchy()      215-260 ms
    bridge /tree                        10-11 ms      <- 24x faster
    bridge /tree, first call (warm)          97 ms

The difference is structural rather than incremental. uiautomator2 crosses a
socket to an instrumentation process, which serialises the entire window as XML
and ships it back. An AccessibilityService already holds the node tree in
process, so /tree is a walk over live objects.

Two capabilities uiautomator2 cannot offer at all:

  * **Events.** `/changed` long-polls and returns the moment the window content
    actually changes - measured waking 133 ms after a swipe. That replaces a
    250 ms polling loop with a push, and it is what makes "wake the agent only
    when something happened" possible.
  * **Reboot survival.** The service is enabled once in secure settings and
    comes back on its own. Wireless adb does not.

**Both backends run at the same time.** This project previously assumed they
could not - that uiautomator2, being a UiAutomation and therefore itself a
special AccessibilityService, would exclude a custom one. Tested, and false on
Android 14: with the bridge enabled, u2 still dumped normally at 215 ms. So the
bridge is an addition, not a swap, and `Observer` falls back to u2 whenever the
service is not installed.

The service binds 127.0.0.1 only. From a laptop it is reached through an adb
port forward, which this module sets up on demand.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from .. import device as dev
from ..ui import Element

PORT = int(os.environ.get("CLAUDEPHONE_BRIDGE_PORT", "8766"))
PACKAGE = "com.claudephone.bridge"
SERVICE = PACKAGE + "/" + PACKAGE + ".BridgeService"


class BridgeError(RuntimeError):
    pass


def _elements_from(rows: list) -> list[Element]:
    """Bridge JSON -> the same ui.Element objects u2's parser produces.

    Keeping one element type is what lets every existing tool - extract_fields,
    the selector registry, collect_feed - work against either backend without
    knowing which one answered.
    """
    out = []
    for i, r in enumerate(rows):
        b = r.get("b") or []
        if len(b) == 4:
            bounds = (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
        else:
            c = r.get("c") or [0, 0]
            bounds = (int(c[0]), int(c[1]), int(c[0]), int(c[1]))
        flags = r.get("f") or ""
        out.append(Element(
            i=r.get("i", i),
            rid=r.get("id", "") or "",
            anchor=r.get("anchor", "") or r.get("id", "") or "",
            text=r.get("text", "") or "",
            desc=r.get("desc", "") or "",
            cls=r.get("cls", "") or "",
            bounds=bounds,
            clickable="C" in flags,
            scrollable="S" in flags,
            selected="*" in flags,
            checked="x" in flags,
        ))
    return out


class Bridge:
    def __init__(self, port: int = PORT, serial: str = "") -> None:
        self.port = port
        self.serial = serial
        self.base = "http://127.0.0.1:" + str(port)
        self._forwarded = False

    # -- transport -----------------------------------------------------------

    def _ensure_forward(self) -> None:
        """Off-device, reach the loopback-bound service through adb forward."""
        if dev.on_device() or self._forwarded:
            return
        try:
            dev.adb("forward", "tcp:" + str(self.port), "tcp:" + str(self.port),
                    serial=self.serial, timeout=15, check=False)
            self._forwarded = True
        except Exception:
            pass

    def _get(self, path: str, timeout: float = 10.0, _retry: bool = True) -> dict:
        self._ensure_forward()
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            # Off-device we reach the service through an adb forward, and that
            # forward is not ours alone: uiautomator2 manages its own forwards
            # and tears ours down when it (re)connects. The symptom is a dead
            # socket mid-run - "Remote end closed connection without response".
            # Re-establish and retry once before reporting failure.
            if _retry and not dev.on_device():
                self._forwarded = False
                self._ensure_forward()
                return self._get(path, timeout=timeout, _retry=False)
            if isinstance(e, urllib.error.URLError):
                raise BridgeError("bridge unreachable at " + self.base + path
                                  + ": " + str(e.reason)) from None
            raise BridgeError(type(e).__name__ + ": " + str(e)[:200]) from None

    # -- status --------------------------------------------------------------

    def available(self, timeout: float = 1.5) -> bool:
        try:
            return bool(self._get("/health", timeout=timeout).get("ok"))
        except BridgeError:
            return False

    def health(self) -> dict:
        return self._get("/health", timeout=5)

    @staticmethod
    def installed(serial: str = "") -> bool:
        out = dev.shell("pm list packages " + PACKAGE, serial=serial,
                        check=False)
        return PACKAGE in out

    @staticmethod
    def enabled(serial: str = "") -> bool:
        out = dev.shell("settings get secure enabled_accessibility_services",
                        serial=serial, check=False)
        return PACKAGE in out

    @staticmethod
    def enable(serial: str = "") -> dict:
        """Turn the service on without tapping through Settings.

        Needs uid 2000, which the loopback adb connection provides. Some OEM
        builds revoke WRITE_SECURE_SETTINGS from adb - if this returns
        enabled=False, the toggle in Settings > Accessibility still works.
        """
        cur = dev.shell("settings get secure enabled_accessibility_services",
                        serial=serial, check=False).strip()
        if PACKAGE in cur:
            new = cur
        elif cur in ("", "null"):
            new = SERVICE
        else:
            new = cur + ":" + SERVICE
        dev.shell("settings put secure enabled_accessibility_services '"
                  + new + "'", serial=serial, check=False)
        dev.shell("settings put secure accessibility_enabled 1", serial=serial,
                  check=False)
        time.sleep(2)
        return {"enabled": Bridge.enabled(serial), "services": new}

    # -- reading -------------------------------------------------------------

    def tree(self, limit: int = 300) -> dict:
        t0 = time.time()
        r = self._get("/tree?limit=" + str(limit))
        return {
            "elements": _elements_from(r.get("elements") or []),
            "package": r.get("package") or "",
            "changes": r.get("changes", 0),
            "server_ms": r.get("ms"),
            "ms": int((time.time() - t0) * 1000),
        }

    def changed(self, since: int, timeout_ms: int = 10000) -> dict:
        """Block until the window content changes. The event-driven primitive."""
        return self._get("/changed?since=" + str(since) + "&timeout="
                         + str(timeout_ms),
                         timeout=timeout_ms / 1000.0 + 5)

    # -- acting --------------------------------------------------------------

    def tap(self, x: int, y: int, ms: int = 50) -> bool:
        return bool(self._get("/tap?x=%d&y=%d&ms=%d" % (x, y, ms)).get("ok"))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 250) -> bool:
        return bool(self._get("/swipe?x1=%d&y1=%d&x2=%d&y2=%d&ms=%d"
                              % (x1, y1, x2, y2, ms)).get("ok"))

    def key(self, name: str) -> bool:
        return bool(self._get("/key?name=" + name).get("ok"))

    def text(self, value: str) -> bool:
        from urllib.parse import quote
        return bool(self._get("/text?value=" + quote(value)).get("ok"))


_bridge: Optional[Bridge] = None
_checked: Optional[bool] = None


def bridge(serial: str = "") -> Bridge:
    global _bridge
    if _bridge is None or _bridge.serial != serial:
        _bridge = Bridge(serial=serial)
    return _bridge


def available(serial: str = "", recheck: bool = False) -> bool:
    """Is the bridge usable right now? Cached - probing costs a round trip.

    OFF-DEVICE THIS DEFAULTS TO FALSE, and the reason is a measured
    incompatibility rather than caution:

    From a laptop the service is reached through `adb forward`. Once
    uiautomator2 is used **in the same Python process**, every adb-forwarded
    connection from that process starts returning an empty response - verified
    on two different local ports, while `curl` from another process kept
    working. So the breakage is process-local to adb's client, not the service.

    Since the legacy tools (`ui_dump`, `tap`, `swipe`) all go through u2, a host
    session would mix the two and break. On the phone the question does not
    arise: Termux talks to 127.0.0.1:8766 directly and there is no forward.

    Set CLAUDEPHONE_BRIDGE=1 to force it on a host anyway - useful for testing
    the bridge itself, as long as nothing calls u2 in the same process.
    """
    global _checked
    force = os.environ.get("CLAUDEPHONE_BRIDGE", "").lower() in ("1", "true", "yes")
    if not dev.on_device() and not force:
        return False
    if _checked is None or recheck:
        _checked = bridge(serial).available()
    return _checked
