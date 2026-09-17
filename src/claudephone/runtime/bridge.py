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

**Auth (bridge v0.2).** Loopback is shared by every app on the phone, so v0.1
let any installed app read the screen and inject taps. v0.2 requires an
`X-Bridge-Token` header on everything except a minimal `/health`. The token is
readable only by adb shell or root, through a ContentProvider:

    adb shell content query --uri content://com.claudephone.bridge.auth/token

Both clients already have shell - the laptop through adb, Termux through its
loopback adb connection - so this module fetches it with `dev.shell` on first
use, caches it, and re-fetches once if the service answers 401 (the token was
rotated). `CLAUDEPHONE_BRIDGE_TOKEN` overrides the lookup. A v0.1 bridge is still
accepted and reported as `auth: legacy_unauthenticated` so it can be upgraded.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from .. import device as dev
from ..ui import Element

PORT = int(os.environ.get("CLAUDEPHONE_BRIDGE_PORT", "8766"))
PACKAGE = "com.claudephone.bridge"
SERVICE = PACKAGE + "/" + PACKAGE + ".BridgeService"
TOKEN_URI = "content://com.claudephone.bridge.auth/token"
TOKEN_HEADER = "X-Bridge-Token"
_TOKEN_RE = re.compile(r"token=([0-9a-f]{32,})")


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
            # u2's parser makes a node with an id its OWN anchor and passes
            # that id down to anonymous children; the service sends the
            # inherited anchor and the id separately, so own id wins here.
            # Measured on an IG profile 2026-09-18: with the service's anchor
            # preferred, values_by_anchor put "686M" under
            # profile_header_followers_stacked_familiar (the PARENT) and every
            # header lookup in ig_profile_stats came back empty.
            anchor=r.get("id", "") or r.get("anchor", "") or "",
            text=r.get("text", "") or "",
            desc=r.get("desc", "") or "",
            cls=r.get("cls", "") or "",
            bounds=bounds,
            clickable="C" in flags,
            scrollable="S" in flags,
            selected="*" in flags,
            checked="x" in flags,
            hidden="h" in flags,
            window=r.get("w", "") or "",
        ))
    return out


class Bridge:
    def __init__(self, port: int = PORT, serial: str = "") -> None:
        self.port = port
        self.serial = serial
        self.base = "http://127.0.0.1:" + str(port)
        self._forwarded = False
        self._token: Optional[str] = os.environ.get("CLAUDEPHONE_BRIDGE_TOKEN") or None
        self._token_checked = bool(self._token)
        # "ok" | "legacy_unauthenticated" | "rejected" | "unavailable" | ""
        self.auth = ""
        self.last_tap: dict = {}

    # -- auth ----------------------------------------------------------------

    def _fetch_token(self) -> Optional[str]:
        """Read the token through adb shell. None if the provider is absent (v0.1)."""
        try:
            out = dev.shell("content query --uri " + TOKEN_URI,
                            serial=self.serial, timeout=15, check=False)
        except Exception:
            return None
        m = _TOKEN_RE.search(out or "")
        return m.group(1) if m else None

    def token(self, refresh: bool = False) -> Optional[str]:
        # Look it up once per client, not once per request: against a v0.1
        # bridge there is no provider, and an adb round trip before every
        # 13 ms read would erase the reason the bridge exists.
        if refresh or not self._token_checked:
            self._token = self._fetch_token() or self._token
            self._token_checked = True
        return self._token

    def rotate_token(self) -> Optional[str]:
        """Replace the token on the device. Every other client re-fetches on 401."""
        try:
            out = dev.shell("content call --uri content://com.claudephone.bridge.auth"
                            " --method rotate", serial=self.serial, timeout=15,
                            check=False)
        except Exception:
            return None
        m = _TOKEN_RE.search(out or "")
        self._token = m.group(1) if m else self._fetch_token()
        return self._token

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

    def _get(self, path: str, timeout: float = 10.0, _retry: bool = True,
             _reauth: bool = True) -> dict:
        self._ensure_forward()
        req = urllib.request.Request(self.base + path)
        tok = self.token()
        if tok:
            req.add_header(TOKEN_HEADER, tok)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # The server answered, so this is not a transport problem - do not
            # fall into the forward-repair retry below.
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = {}
            if e.code == 401 and _reauth:
                # Rotated, or never fetched: read it again once, then give up.
                self.token(refresh=True)
                return self._get(path, timeout=timeout, _retry=_retry,
                                 _reauth=False)
            if e.code == 401:
                self.auth = "rejected"
                raise BridgeError("bridge refused the token (401). Read it with: "
                                  "adb shell content query --uri " + TOKEN_URI) from None
            if e.code == 404:
                return body
            raise BridgeError("bridge HTTP " + str(e.code) + ": "
                              + str(body.get("error", ""))[:200]) from None
        except Exception as e:
            # A dead socket mid-run - "Remote end closed connection without
            # response" - has had two causes:
            #   1. uiautomator2 suppressing the service. Measured on the Samsung
            #      (Android 12): while u2's UiAutomation runs, Android unbinds the
            #      bridge and its HTTP server stops; stopping u2 rebinds it in
            #      about 3 s. If this process holds a u2 session, hand the phone
            #      back and wait for the bridge before retrying.
            #   2. the adb forward being replaced (off-device only). Re-forward.
            if _retry and dev.u2_active(self.serial) and self._yield_u2():
                return self._get(path, timeout=timeout, _retry=False,
                                 _reauth=_reauth)
            if _retry and not dev.on_device():
                self._forwarded = False
                self._ensure_forward()
                return self._get(path, timeout=timeout, _retry=False,
                                 _reauth=_reauth)
            if isinstance(e, urllib.error.URLError):
                raise BridgeError("bridge unreachable at " + self.base + path
                                  + ": " + str(e.reason)) from None
            raise BridgeError(type(e).__name__ + ": " + str(e)[:200]) from None

    def _yield_u2(self, wait_s: float = 8.0) -> bool:
        """Stop this process's u2 session and wait for the bridge to serve again."""
        global last_yield
        t0 = time.time()
        dev.release_u2()
        self._forwarded = False
        while time.time() - t0 < wait_s:
            time.sleep(0.5)
            try:
                self._ensure_forward()
                with urllib.request.urlopen(self.base + "/health", timeout=1.5) as r:
                    if json.loads(r.read().decode()).get("ok"):
                        last_yield = {"serial": self.serial, "ok": True,
                                      "seconds": round(time.time() - t0, 1)}
                        return True
            except Exception:
                self._forwarded = False
        last_yield = {"serial": self.serial, "ok": False,
                      "seconds": round(time.time() - t0, 1)}
        return False

    # -- status --------------------------------------------------------------

    def available(self, timeout: float = 1.5) -> bool:
        """Usable = reachable AND authenticated (or a v0.1 build with no auth).

        /health answers without a token, so `ok` alone proves nothing: a v0.2
        service that rejected our token still says ok, with auth=required.
        """
        try:
            h = self._get("/health", timeout=timeout)
            if h.get("auth") == "required":
                self.token(refresh=True)
                h = self._get("/health", timeout=timeout)
        except BridgeError:
            self.auth = self.auth or "unavailable"
            return False
        if not h.get("ok"):
            self.auth = "unavailable"
            return False
        auth = h.get("auth")
        if auth == "ok":
            self.auth = "ok"
            return True
        if auth is None:
            self.auth = "legacy_unauthenticated"     # v0.1: works, but open
            return True
        self.auth = "rejected"
        return False

    def health(self) -> dict:
        h = self._get("/health", timeout=5)
        if h.get("auth") is None and h.get("ok"):
            h["auth"] = "legacy_unauthenticated"
            h["hint"] = ("v0.1 bridge: any app on the phone can use it. "
                         "Rebuild and install: android/build.sh install")
        return h

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

    def tree(self, limit: int = 300, all_windows: bool = False,
             max_text: int = 0) -> dict:
        """The active window's elements, plus what else is on screen (v0.2).

        `obstructions` lists windows above the app that are not status or
        navigation bars - a keyboard, a system alert, a chat head. When it is
        non-empty, a tap aimed at the app may land on one of them.
        `max_text` raises the server's per-label cap (default 300 characters),
        which a chat message needs and a button label does not. A v0.2 build
        without the cap parameter ignores it and still truncates at 300.
        `all_windows=True` appends those windows' elements, tagged `window`.
        On a v0.1 bridge the extra keys are simply absent.
        """
        t0 = time.time()
        r = self._get("/tree?limit=" + str(limit)
                      + ("&windows=all" if all_windows else "")
                      + ("&tmax=" + str(int(max_text)) if max_text else ""))
        return {
            "elements": _elements_from(r.get("elements") or []),
            "package": r.get("package") or "",
            "changes": r.get("changes", 0),
            "foreground": r.get("foreground"),
            "foreground_reason": r.get("foreground_reason"),
            "foreground_known": r.get("foreground_known"),
            "ime_visible": r.get("ime_visible"),
            "obstructions": r.get("obstructions") or [],
            "windows": r.get("windows") or [],
            "server_ms": r.get("ms"),
            "ms": int((time.time() - t0) * 1000),
        }

    def windows(self) -> dict:
        return self._get("/windows")

    def changed(self, since: int, timeout_ms: int = 10000) -> dict:
        """Block until the window content changes. The event-driven primitive."""
        return self._get("/changed?since=" + str(since) + "&timeout="
                         + str(timeout_ms),
                         timeout=timeout_ms / 1000.0 + 5)

    # -- acting --------------------------------------------------------------

    def tap(self, x: int, y: int, ms: int = 50) -> bool:
        """Tap; returns ok. `self.last_tap["lands_on"]` says which window it hit."""
        self.last_tap = self._get("/tap?x=%d&y=%d&ms=%d" % (x, y, ms))
        return bool(self.last_tap.get("ok"))

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
_heal_tried: set = set()
# What the last u2 hand-back did (see Bridge._yield_u2), for bridge_status.
last_yield: dict = {}
# What the last self-heal did, for bridge_status: {"serial", "was_enabled", "now_reachable"}.
last_heal: dict = {}


def heal(serial: str = "") -> bool:
    """Re-enable an installed bridge that Android switched off. Once per process.

    Measured 2026-09-17 on the Samsung (Android 12): `am force-stop
    com.claudephone.bridge` does not just kill the service - Android REMOVES it
    from `enabled_accessibility_services` and sets `accessibility_enabled=0`.
    The process can come back (reading the token starts it) while the port stays
    refused. Anything that force-stops apps - Device Care optimisation, the
    Force stop button, a cleaner - therefore switches the bridge off until
    someone re-enables it. The operator already chose to enable it, so the
    client restores that choice through the shell, the same write
    `android/build.sh install` makes. CLAUDEPHONE_BRIDGE_AUTOHEAL=0 disables this.
    """
    if os.environ.get("CLAUDEPHONE_BRIDGE_AUTOHEAL", "1").lower() in ("0", "false", "no"):
        return False
    if serial in _heal_tried:
        return False
    _heal_tried.add(serial)
    try:
        if not Bridge.installed(serial) or Bridge.enabled(serial):
            return False
        Bridge.enable(serial)
        time.sleep(2)
        ok = bridge(serial).available(timeout=3)
        last_heal.update({"serial": serial, "was_enabled": False,
                          "now_reachable": ok, "at": time.time()})
        return ok
    except Exception:
        return False


def bridge(serial: str = "") -> Bridge:
    global _bridge
    if _bridge is None or _bridge.serial != serial:
        _bridge = Bridge(serial=serial)
    return _bridge


def available(serial: str = "", recheck: bool = False) -> bool:
    """Is the bridge usable right now? Cached - probing costs a round trip.

    USED WHEREVER IT IS REACHABLE, on a host as well as on the phone
    (CLAUDEPHONE_BRIDGE=0 turns it off). That default was the other way round
    until 2e, for a reason that turned out to be wrong twice over.

    What was recorded here - "adb-forwarded connections go dead once u2 is used
    in this process, so the breakage is process-local to adb's client" - was not
    the cause. Measured on the Samsung (Android 12) 2026-09-17: while u2's
    UiAutomation runs, Android UNBINDS the service device-wide; the port stops
    listening for on-device clients too, and it rebinds about 3 s after u2 stops.
    `Bridge._yield_u2` handles that by stopping u2 and retrying (~1.7 s).

    The remaining objection was that a host run alternating backends pays ~3.5 s
    a switch, and most tools dumped through u2. They no longer do:
    `runtime/screen.py` reads for `ui_dump`, `find_element`, `extract_fields`,
    `explore`, `check_drift` and `record_baseline`, and the app tools read
    through `targeting.read_screen`. What is left on u2 does not dump the
    screen - the clipboard, screenshots, gestures.

    Measured on Instagram, same screens: a dump is 495-577 ms through the bridge
    against 2141-2866 ms through u2, with identical screen detection, identical
    drift status and 13/13 identical extracted fields - the caption differs only
    because u2's XML flattens emoji.
    """
    global _checked
    setting = os.environ.get("CLAUDEPHONE_BRIDGE", "").lower()
    if setting in ("0", "false", "no"):
        return False
    if _checked is None or recheck:
        _checked = bridge(serial).available()
        if not _checked and bridge(serial).auth in ("", "unavailable"):
            # Unreachable, not refused: maybe Android switched the service off.
            _checked = heal(serial)
    return _checked
