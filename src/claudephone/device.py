"""Device access - one code path that runs on a laptop OR on the phone itself.

The central trick this project rests on: **Termux can `adb connect 127.0.0.1:5555`
to the phone's own adbd** and obtain uid=2000(shell). The Termux app uid
(u0_a308) is denied `input`, `settings`, `uiautomator`; the loopback shell is
not. So the *same* adb-based code that drives the phone from a laptop drives it
from inside the phone, with only the serial changing.

    laptop  : adb -s 192.168.1.5:5555  shell input tap ...
    on-phone: adb -s 127.0.0.1:5555    shell input tap ...

Verified on realme narzo 50 Pro 5G (RMX3395, Android 14), unrooted, no Shizuku.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

TERMUX_PREFIX = "/data/data/com.termux/files/usr"

# The adbd port the phone listens on for its own loopback client.
LOOPBACK = os.environ.get("CLAUDEPHONE_LOOPBACK", "127.0.0.1:5555")


@lru_cache(maxsize=1)
def on_device() -> bool:
    """True when this process is running inside Termux on the phone."""
    if os.environ.get("CLAUDEPHONE_ON_DEVICE"):
        return os.environ["CLAUDEPHONE_ON_DEVICE"] not in ("0", "", "false")
    return os.path.isdir(TERMUX_PREFIX) and os.path.isdir("/system/bin")


def _adb_candidates() -> list[str]:
    return [
        os.environ.get("ADB_PATH", ""),
        TERMUX_PREFIX + "/bin/adb",
        r"C:\Users\HP\AppData\Local\Android\Sdk\platform-tools\adb.exe",
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        "adb",
    ]


@lru_cache(maxsize=1)
def _find_adb() -> str:
    for c in _adb_candidates():
        if not c:
            continue
        if os.path.isfile(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    raise RuntimeError(
        "adb not found. Install it (Termux: `pkg install android-tools`) "
        "or set ADB_PATH."
    )


def ADB() -> str:
    """Path to the adb binary. Callable so a bare box can still import us."""
    return _find_adb()


class DeviceError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def default_serial() -> str:
    """Which device to talk to when the caller does not say.

    On-device this is always the loopback; on a laptop it is whatever the user
    configured, else the single attached device.
    """
    for var in ("CLAUDEPHONE_SERIAL", "MOBILEAGENT_SERIAL"):
        if os.environ.get(var):
            return os.environ[var]
    if on_device():
        return LOOPBACK
    devs = [d["serial"] for d in list_devices() if d["state"] == "device"]
    if not devs:
        return ""
    if len(devs) == 1:
        return devs[0]
    # The same phone commonly shows up twice - once on USB, once on TCP. USB is
    # the faster and more stable transport, so prefer a serial without a port.
    usb = [s for s in devs if ":" not in s]
    return usb[0] if usb else devs[0]


def adb(*args: str, serial: str = "", timeout: int = 60,
        check: bool = True) -> str:
    """Run an adb command and return stdout."""
    cmd = [ADB()]
    s = serial or default_serial()
    if s:
        cmd += ["-s", s]
    cmd += list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0 and check:
        raise DeviceError(
            "adb " + " ".join(args) + " failed (" + str(p.returncode) + "): "
            + (p.stderr or p.stdout).strip()[:400]
        )
    return p.stdout


def shell(cmd: str, serial: str = "", timeout: int = 60,
          check: bool = True) -> str:
    return adb("shell", cmd, serial=serial, timeout=timeout, check=check)


def list_devices() -> list[dict]:
    out = subprocess.run([_find_adb(), "devices", "-l"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    rows = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        rows.append({
            "serial": parts[0],
            "state": parts[1] if len(parts) > 1 else "?",
            "detail": " ".join(parts[2:]),
        })
    return rows


def ensure_loopback(timeout: int = 30) -> dict:
    """On-device: make sure adb is connected to the phone's own adbd.

    First run pops the 'Allow USB debugging?' dialog - a one-time human tap.
    After 'Always allow', the key persists in /data/misc/adb/adb_keys.
    """
    if not on_device():
        return {"on_device": False, "skipped": True}
    out = subprocess.run([_find_adb(), "connect", LOOPBACK], capture_output=True,
                         text=True, timeout=timeout).stdout.strip()
    state = next((d["state"] for d in list_devices() if d["serial"] == LOOPBACK),
                 "missing")
    res = {"on_device": True, "target": LOOPBACK, "connect": out, "state": state}
    if state == "unauthorized":
        res["action_required"] = (
            "Tap 'Always allow this computer for debugging' then Allow on the "
            "phone screen, and re-run. This is a one-time grant."
        )
    elif state == "missing":
        res["action_required"] = (
            "adbd is not listening on TCP. Enable Wireless debugging, or run "
            "`adb tcpip 5555` once from a USB-attached computer."
        )
    return res


@dataclass
class DeviceInfo:
    serial: str
    model: str
    android: str
    sdk: str
    build: str
    screen: str
    density: str
    battery: str


def device_info(serial: str = "") -> DeviceInfo:
    props = shell(
        "getprop ro.product.model; getprop ro.build.version.release; "
        "getprop ro.build.version.sdk; getprop ro.build.display.id",
        serial=serial,
    ).strip().splitlines()
    props += [""] * (4 - len(props))
    size = shell("wm size", serial=serial).strip().split(":")[-1].strip()
    dens = shell("wm density", serial=serial).strip().split(":")[-1].strip()
    batt = ""
    try:
        for line in shell("dumpsys battery", serial=serial).splitlines():
            if "level:" in line:
                batt = line.split(":")[-1].strip()
                break
    except DeviceError:
        pass
    return DeviceInfo(
        serial=serial or default_serial(),
        model=props[0], android=props[1], sdk=props[2], build=props[3],
        screen=size, density=dens, battery=batt,
    )


def app_version(package: str, serial: str = "") -> Optional[str]:
    """versionName of an installed package, or None."""
    try:
        out = shell("dumpsys package " + package + " | grep -m1 versionName",
                    serial=serial)
    except DeviceError:
        return None
    for line in out.splitlines():
        if "versionName=" in line:
            return line.split("versionName=")[-1].strip()
    return None


def _activity_from(line: str) -> Optional[dict]:
    """Pull `package/activity` out of a dumpsys line, whatever wraps it.

    The value sits inside `ActivityRecord{...}` or `Window{...}`, so the braces
    are split off before tokenising - otherwise the closing brace rides along on
    the activity name.
    """
    for tok in line.replace("{", " ").replace("}", " ").split():
        if "/" not in tok or "." not in tok:
            continue
        pkg, _, act = tok.partition("/")
        if not pkg or "." not in pkg:
            continue
        return {"package": pkg, "activity": pkg + act if act.startswith(".")
                else act}
    return None


# Which dumpsys line names the foreground activity varies by Android version
# AND by OEM, so probing one pattern is not enough:
#
#   realme UI V14 / Android 14   topResumedActivity
#   Samsung One UI 4 / Android 12   mResumedActivity and ResumedActivity,
#                                   but NEVER topResumedActivity
#
# Measured on SM-M215F, 2026-09-08. The single-pattern grep this replaced
# matched nothing there and returned {"package": None} *silently*, which then
# disabled screen detection, the selector registry, drift checking and
# extract_fields - the caller was told the screen was unrecognised rather than
# that the probe had failed. Fail loud beats fail quiet: `_probe` is reported
# so a None answer can be told apart from an unsupported dumpsys format.
_FG_PROBES = (
    ("activities", "dumpsys activity activities | grep -m1 -E "
                   "'topResumedActivity|mResumedActivity|ResumedActivity'"),
    ("window", "dumpsys window | grep -m1 -E 'mCurrentFocus|mFocusedApp'"),
)


def foreground(serial: str = "") -> dict:
    """Currently resumed package/activity.

    Falls back through the probes above, so an OEM that renames the line costs
    one extra round trip rather than silently breaking every app-aware feature.
    """
    for name, cmd in _FG_PROBES:
        try:
            out = shell(cmd, serial=serial)
        except DeviceError:
            continue
        for line in out.splitlines():
            hit = _activity_from(line)
            if hit:
                hit["_probe"] = name
                return hit
    return {"package": None, "activity": None, "_probe": None}


# --- uiautomator2 -----------------------------------------------------------
#
# MEASURED, and the reason this layer exists at all (docs/BENCHMARKS.md):
#
#   `uiautomator dump` CLI  over Wi-Fi   2540 ms   <- spawns instrumentation
#   `uiautomator dump` CLI  on-device    2520 ms      on every single call
#   u2 persistent server    over Wi-Fi    220 ms
#
# The transport was never the bottleneck; the dump method was. Always go
# through u2() rather than shelling out to `uiautomator dump`.

_u2_conn = None
_u2_serial = None


def u2(serial: str = ""):
    """Shared uiautomator2 connection.

    uiautomator2 is a UiAutomation. Whether it coexists with the ClaudePhone
    bridge (an AccessibilityService) depends on the phone: on the realme
    (Android 14) both serve at once; on the Samsung M21 (Android 12, u2 3.7.0)
    Android UNBINDS the bridge for as long as u2's UiAutomation runs, measured
    2026-09-17. runtime/bridge.py hands the phone back with release_u2() when a
    bridge request fails while this process holds a u2 session.
    """
    global _u2_conn, _u2_serial
    s = serial or default_serial()
    if _u2_conn is not None and _u2_serial == s:
        return _u2_conn
    import uiautomator2
    _u2_conn = uiautomator2.connect(s) if s else uiautomator2.connect()
    _u2_serial = s
    return _u2_conn


def u2_active(serial: str = "") -> bool:
    """True if this process started a u2 session on that phone (or any, if no serial)."""
    return _u2_conn is not None and (not serial or _u2_serial in ("", serial))


def release_u2() -> bool:
    """Stop this process's uiautomator2 server so an AccessibilityService it
    suppressed can rebind. The next u2() call starts a fresh one."""
    global _u2_conn, _u2_serial
    if _u2_conn is None:
        return False
    try:
        _u2_conn.stop_uiautomator()
    except Exception:
        pass
    _u2_conn, _u2_serial = None, None
    return True
