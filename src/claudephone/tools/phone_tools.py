"""The phone as a phone: radios, sensors, camera, telephony, notifications.

Everything here is a thin wrapper over the `termux-*` CLI provided by the
Termux:API add-on. Two practical notes:

* **Permissions are runtime, and silence is the failure mode.** `termux-sms-list`
  with no SMS permission returns empty output rather than an error, which reads
  exactly like "you have no messages". `phone_permissions()` grants them in bulk
  using the loopback shell (uid 2000 can `pm grant`), and every wrapper reports
  `permission_hint` when it gets a suspicious empty result.
* **Off-device these still work**, routed through `adb shell run-as com.termux`.
  Slower, but it means a laptop session is not a second-class citizen.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess

from .. import device as dev

PREFIX = "/data/data/com.termux/files/usr"
HOME = "/data/data/com.termux/files/home"

# Runtime permissions the API add-on needs. Granting is idempotent.
PERMISSIONS = {
    "sms": ["android.permission.READ_SMS", "android.permission.SEND_SMS",
            "android.permission.RECEIVE_SMS"],
    "phone": ["android.permission.READ_PHONE_STATE",
              "android.permission.CALL_PHONE",
              "android.permission.READ_CALL_LOG"],
    "contacts": ["android.permission.READ_CONTACTS"],
    "location": ["android.permission.ACCESS_FINE_LOCATION",
                 "android.permission.ACCESS_COARSE_LOCATION"],
    "camera": ["android.permission.CAMERA"],
    "microphone": ["android.permission.RECORD_AUDIO"],
    "storage": ["android.permission.READ_EXTERNAL_STORAGE",
                "android.permission.WRITE_EXTERNAL_STORAGE"],
}


def _termux(cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a termux-* command, on-device directly or via run-as from a host.

    Returns a timeout as `(124, "", ...)` rather than raising. Several
    `termux-*` binaries block indefinitely when their Termux:API service never
    answers - `termux-notification-list` without notification-listener access is
    the one that found this - and a raised TimeoutExpired surfaced to the model
    as a 900-character subprocess traceback naming the base64 payload, which
    says nothing about the cause. 124 is what `timeout(1)` uses.
    """
    if dev.on_device():
        try:
            p = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                               text=True, timeout=timeout, encoding="utf-8",
                               errors="replace")
        except subprocess.TimeoutExpired:
            return 124, "", "timed out after " + str(timeout) + "s"
        return p.returncode, p.stdout, p.stderr
    prelude = (
        "export PREFIX=" + PREFIX + "; export HOME=" + HOME +
        "; export PATH=" + PREFIX + "/bin:/system/bin"
        "; export LD_LIBRARY_PATH=" + PREFIX + "/lib"
        "; export TMPDIR=" + PREFIX + "/tmp; cd $HOME\n"
    )
    b64 = base64.b64encode((prelude + cmd).encode()).decode()
    inner = ("echo " + b64 + " | toybox base64 -d | " + PREFIX + "/bin/bash")
    try:
        out = dev.adb("shell", "run-as", "com.termux", "sh", "-c",
                      shlex.quote(inner), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after " + str(timeout) + "s"
    # NOTE: rc is 0 here regardless of what ran, because `adb shell` does not
    # relay the remote exit status on this path. Callers cannot tell a failure
    # from success by rc alone - judge by the output.
    return 0, out, ""


def _json(cmd: str, timeout: int = 60):
    rc, out, err = _termux(cmd, timeout=timeout)
    body = (out or "").strip()
    if not body:
        return None, (err or "").strip()
    try:
        return json.loads(body), ""
    except json.JSONDecodeError:
        return body, ""


def register(reg) -> None:

    # -- permissions ---------------------------------------------------------

    @reg.tool(
        description="Grant the Android runtime permissions the phone tools "
                    "need (sms, phone, contacts, location, camera, "
                    "microphone, storage). Uses the privileged shell, so no "
                    "tapping through dialogs. Pass 'all' or a comma list.",
        dangerous=True,
    )
    def phone_permissions(groups: str = "all") -> dict:
        want = (sorted(PERMISSIONS) if groups.strip().lower() == "all"
                else [g.strip() for g in groups.split(",") if g.strip()])
        results: dict[str, str] = {}
        for g in want:
            for perm in PERMISSIONS.get(g, []):
                for pkg in ("com.termux", "com.termux.api"):
                    out = dev.shell("pm grant " + pkg + " " + perm,
                                    check=False).strip()
                    results[pkg + " " + perm.split(".")[-1]] = out or "granted"
        return {"requested": want, "results": results,
                "note": "Permissions the ROM refuses to grant this way must be "
                        "toggled in Settings > Apps > Termux > Permissions."}

    # -- power / radios ------------------------------------------------------

    @reg.tool(description="Battery level, status, temperature and health.")
    def phone_battery() -> dict:
        data, err = _json("termux-battery-status")
        return {"battery": data} if data else {"error": err or "no output"}

    @reg.tool(description="Wi-Fi connection details: SSID, BSSID, IP, link "
                          "speed, signal strength.")
    def phone_wifi() -> dict:
        data, err = _json("termux-wifi-connectioninfo")
        return {"wifi": data} if data else {"error": err or "no output"}

    @reg.tool(description="Scan for nearby Wi-Fi networks. Android rate-limits "
                          "scans, so repeated calls may return a cached list.")
    def phone_wifi_scan() -> dict:
        data, err = _json("termux-wifi-scaninfo")
        if data is None:
            return {"error": err or "no output"}
        return {"networks": data,
                "count": len(data) if isinstance(data, list) else None}

    @reg.tool(description="Cellular network and SIM details.")
    def phone_cellular() -> dict:
        info, _ = _json("termux-telephony-deviceinfo")
        cells, _ = _json("termux-telephony-cellinfo")
        return {"device": info, "cells": cells}

    @reg.tool(description="Read or set the screen brightness (0-255), or set "
                          "it to automatic.", dangerous=True)
    def phone_brightness(level: int = -1, auto: bool = False) -> dict:
        if auto:
            _termux("termux-brightness auto")
            return {"brightness": "auto"}
        if level < 0:
            out = dev.shell("settings get system screen_brightness",
                            check=False).strip()
            return {"brightness": out}
        _termux("termux-brightness " + str(max(0, min(255, level))))
        return {"brightness": level}

    # -- output: sound, light, haptics ---------------------------------------

    @reg.tool(description="Speak text aloud through the phone's speaker using "
                          "text-to-speech.")
    def phone_speak(text: str, pitch: float = 1.0, rate: float = 1.0) -> dict:
        rc, out, err = _termux("termux-tts-speak -p " + str(pitch) + " -r "
                               + str(rate) + " " + shlex.quote(text))
        return {"spoke": text[:200], "error": err.strip() or None}

    @reg.tool(description="Vibrate the phone for a number of milliseconds.")
    def phone_vibrate(duration_ms: int = 500, force: bool = False) -> dict:
        _termux("termux-vibrate -d " + str(duration_ms)
                + (" -f" if force else ""))
        return {"vibrated_ms": duration_ms}

    @reg.tool(description="Turn the camera flash on or off as a torch.")
    def phone_torch(on: bool = True) -> dict:
        _termux("termux-torch " + ("on" if on else "off"))
        return {"torch": "on" if on else "off"}

    @reg.tool(description="Show a brief toast message on the phone screen.")
    def phone_toast(text: str, duration_long: bool = False) -> dict:
        _termux("termux-toast " + ("-l " if duration_long else "")
                + shlex.quote(text))
        return {"toast": text[:200]}

    @reg.tool(description="Read or set the volume of an audio stream "
                          "(music, call, ring, alarm, notification, system).")
    def phone_volume(stream: str = "", level: int = -1) -> dict:
        if not stream or level < 0:
            data, err = _json("termux-volume")
            return {"volumes": data} if data else {"error": err or "no output"}
        _termux("termux-volume " + shlex.quote(stream) + " " + str(level))
        return {"stream": stream, "level": level}

    # -- notifications -------------------------------------------------------

    @reg.tool(description="List the notifications currently in the shade, with "
                          "their package, title and text. Read-only.")
    def phone_notifications(limit: int = 30) -> dict:
        # `termux-notification-list` blocks until its listener service answers,
        # and if Termux:API does not hold notification-listener access nothing
        # ever answers - so the tool hung for the full subprocess timeout and
        # then reported a TimeoutExpired traceback, which says nothing about the
        # actual cause. Measured on SM-M215F, 2026-09-08: even after granting
        # access via `settings put secure enabled_notification_listeners` it
        # returned empty, while the dumpsys-backed `notifications` tool read all
        # six notifications on the same device.
        #
        # So: check the grant first, cap the wait, and always name the tool that
        # does work rather than leaving the agent to guess.
        alt = ("use the `notifications` tool instead - it reads the same data "
               "via dumpsys, needs no Termux:API grant, and is faster")
        try:
            listeners = dev.shell(
                "settings get secure enabled_notification_listeners",
                check=False) or ""
        except Exception:
            listeners = ""
        if "com.termux.api" not in listeners:
            return {"error": "Termux:API does not hold notification-listener "
                             "access, so this would block with no result",
                    "fix": "Settings > Notifications > Notification access > "
                           "Termux:API",
                    "use_instead": alt}
        data, err = _json("termux-notification-list", timeout=20)
        if data is None:
            return {"error": err or "no output from termux-notification-list",
                    "use_instead": alt}
        rows = data if isinstance(data, list) else [data]
        return {"count": len(rows), "notifications": rows[:limit]}

    @reg.tool(description="Post a notification from the phone itself - useful "
                          "for reporting the result of a long unattended run.")
    def phone_notify(title: str, content: str = "", notification_id: str = "",
                     ongoing: bool = False) -> dict:
        cmd = ("termux-notification -t " + shlex.quote(title)
               + " -c " + shlex.quote(content or " "))
        if notification_id:
            cmd += " -i " + shlex.quote(notification_id)
        if ongoing:
            cmd += " --ongoing"
        _termux(cmd)
        return {"posted": title}

    # -- sensors and location ------------------------------------------------

    @reg.tool(description="Read the phone's GPS location. 'gps' is accurate "
                          "but needs sky view and can take ~10s; 'network' is "
                          "fast and coarse.")
    def phone_location(provider: str = "gps", timeout_s: int = 30) -> dict:
        data, err = _json("termux-location -p " + shlex.quote(provider),
                          timeout=timeout_s + 10)
        if data is None:
            return {"error": err or "no fix",
                    "permission_hint": "run phone_permissions('location')"}
        return {"location": data, "provider": provider}

    @reg.tool(description="List the hardware sensors this phone exposes.")
    def phone_sensors() -> dict:
        data, err = _json("termux-sensor -l")
        return {"sensors": data} if data else {"error": err or "no output"}

    @reg.tool(description="Take one reading from a named hardware sensor, e.g. "
                          "'accelerometer', 'light', 'proximity'. Use "
                          "phone_sensors first to see valid names.")
    def phone_sensor_read(sensor: str, timeout_s: int = 15) -> dict:
        data, err = _json("termux-sensor -s " + shlex.quote(sensor) + " -n 1",
                          timeout=timeout_s + 10)
        return {"sensor": sensor, "reading": data} if data else {
            "error": err or "no reading"}

    # -- camera and microphone ----------------------------------------------

    @reg.tool(description="List the phone's cameras with their ids and specs.")
    def phone_camera_info() -> dict:
        data, err = _json("termux-camera-info")
        return {"cameras": data} if data else {"error": err or "no output"}

    @reg.tool(description="Take a photo with a physical camera and save it to "
                          "the phone. camera_id 0 is usually the rear camera, "
                          "1 the front. Returns the file path.",
              dangerous=True)
    def phone_photo(path: str = "", camera_id: int = 0) -> dict:
        dest = path or ("/sdcard/claudephone-photo-"
                        + str(int(__import__("time").time())) + ".jpg")
        rc, out, err = _termux("termux-camera-photo -c " + str(camera_id)
                               + " " + shlex.quote(dest), timeout=60)
        size = dev.shell("stat -c %s " + shlex.quote(dest),
                         check=False).strip()
        return {"path": dest, "camera_id": camera_id, "bytes": size or None,
                "error": (err.strip() or None) if not size else None}

    @reg.tool(description="Record audio from the microphone for a fixed number "
                          "of seconds. Returns the file path.", dangerous=True)
    def phone_record_audio(seconds: int = 10, path: str = "") -> dict:
        dest = path or ("/sdcard/claudephone-audio-"
                        + str(int(__import__("time").time())) + ".m4a")
        _termux("termux-microphone-record -f " + shlex.quote(dest)
                + " -l " + str(seconds), timeout=seconds + 30)
        return {"path": dest, "seconds": seconds}

    @reg.tool(description="Transcribe speech from the microphone to text using "
                          "the phone's own recogniser.")
    def phone_speech_to_text(timeout_s: int = 30) -> dict:
        rc, out, err = _termux("termux-speech-to-text", timeout=timeout_s + 10)
        return {"text": (out or "").strip(), "error": err.strip() or None}

    # -- telephony and messaging --------------------------------------------
    #
    # Reading is ordinary; sending is not. sms_send and phone_call reach other
    # people and cost money, so they are marked dangerous and are in the
    # default deny list (see harness/policy defaults). Allow them explicitly.

    @reg.tool(description="Read SMS messages from the phone. box: inbox, sent, "
                          "draft, outbox, all.")
    def phone_sms_list(limit: int = 20, box: str = "inbox",
                       offset: int = 0) -> dict:
        data, err = _json("termux-sms-list -l " + str(limit) + " -o "
                          + str(offset) + " -t " + shlex.quote(box))
        if data is None:
            return {"error": err or "no messages returned",
                    "permission_hint": "run phone_permissions('sms')"}
        return {"box": box, "count": len(data) if isinstance(data, list) else 1,
                "messages": data}

    @reg.tool(description="Read the call log: who called, when, how long.")
    def phone_call_log(limit: int = 20) -> dict:
        data, err = _json("termux-call-log -l " + str(limit))
        if data is None:
            return {"error": err or "no entries",
                    "permission_hint": "run phone_permissions('phone')"}
        return {"count": len(data) if isinstance(data, list) else 1,
                "calls": data}

    @reg.tool(description="Read the phone's contact list.")
    def phone_contacts(limit: int = 100) -> dict:
        data, err = _json("termux-contact-list")
        if data is None:
            return {"error": err or "no contacts",
                    "permission_hint": "run phone_permissions('contacts')"}
        rows = data if isinstance(data, list) else [data]
        return {"count": len(rows), "contacts": rows[:limit]}

    @reg.tool(
        description="SEND an SMS to a phone number. This reaches a real person "
                    "and may cost money. Denied unless explicitly allowed by "
                    "the operator.",
        dangerous=True,
    )
    def phone_sms_send(number: str, text: str) -> dict:
        rc, out, err = _termux("termux-sms-send -n " + shlex.quote(number)
                               + " " + shlex.quote(text))
        return {"sent_to": number, "text": text[:200],
                "error": err.strip() or None}

    @reg.tool(
        description="PLACE a phone call to a number. Reaches a real person and "
                    "may cost money. Denied unless explicitly allowed.",
        dangerous=True,
    )
    def phone_call(number: str) -> dict:
        rc, out, err = _termux("termux-telephony-call " + shlex.quote(number))
        return {"calling": number, "error": err.strip() or None}

    # -- clipboard and sharing ----------------------------------------------

    @reg.tool(description="Read the phone's clipboard.")
    def phone_clipboard_get() -> dict:
        # Android 10+ only lets the FOREGROUND app read the clipboard, and
        # Termux:API is not foreground when driven over run-as - so this returns
        # an empty string that is indistinguishable from a genuinely empty
        # clipboard. Measured on SM-M215F, 2026-09-08: phone_clipboard_set wrote
        # "claudephone-test" and reported success, this read back "", and the
        # u2-backed `clipboard_get` read the value correctly.
        rc, out, err = _termux("termux-clipboard-get")
        text = (out or "").rstrip("\n")
        if text:
            return {"clipboard": text}
        return {"clipboard": "",
                "warning": "empty - on Android 10+ this may mean the read was "
                           "refused rather than that the clipboard is empty; "
                           "Termux:API can only read it while in the foreground",
                "use_instead": "the `clipboard_get` tool reads it through "
                               "uiautomator2 and is not subject to this limit"}

    @reg.tool(description="Set the phone's clipboard contents.")
    def phone_clipboard_set(text: str) -> dict:
        _termux("termux-clipboard-set " + shlex.quote(text))
        return {"clipboard": text[:400]}

    @reg.tool(description="Download a URL directly to the phone's storage "
                          "using its own network connection.", dangerous=True)
    def phone_download(url: str, path: str = "") -> dict:
        dest = path or ("/sdcard/Download/"
                        + (url.rstrip("/").split("/")[-1] or "download.bin"))
        rc, out, err = _termux("curl -sL -o " + shlex.quote(dest) + " "
                               + shlex.quote(url), timeout=300)
        size = dev.shell("stat -c %s " + shlex.quote(dest),
                         check=False).strip()
        return {"url": url, "path": dest, "bytes": size or None,
                "error": err.strip() or None}

    @reg.tool(description="Full Termux/device diagnostic dump: versions, "
                          "storage, and whether the API add-on is responding.")
    def phone_info() -> dict:
        data, err = _json("termux-info", timeout=60)
        return {"info": data if data else err,
                "on_device": dev.on_device()}
