"""Phone-level functionality: notifications, clipboard, network, media, files.

These are the "it's a phone, not just a screen" capabilities. Everything here
works unrooted over adb.

Deliberately NOT included: sending SMS, placing calls, and reading message
bodies. Those either act outwardly on the user's behalf or expose third-party
personal data, and they should be an explicit, separate decision rather than
something an agent finds in a tool list. Ask before adding them.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

from .. import device as dev
from .. import state


def register(mcp) -> None:

    # -- state ------------------------------------------------------------

    @mcp.tool(
        description="Battery, charging state, temperature and health."
    )
    def battery_status() -> dict:
        out = dev.shell("dumpsys battery")
        d: dict = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.strip().partition(":")
            k, v = k.strip(), v.strip()
            if k in ("level", "scale", "temperature", "voltage", "status",
                     "health", "plugged", "AC powered", "USB powered"):
                d[k] = v
        lvl = d.get("level")
        temp = d.get("temperature")
        res = {
            "level_pct": int(lvl) if lvl and lvl.isdigit() else None,
            "temperature_c": (int(temp) / 10.0) if temp and temp.lstrip("-").isdigit() else None,
            "charging": d.get("AC powered") == "true" or d.get("USB powered") == "true",
            "raw": d,
        }
        if res["level_pct"] is not None and res["level_pct"] < 15 and not res["charging"]:
            res["warning"] = "battery low and not charging"
        return res

    @mcp.tool(description="Wi-Fi SSID, IP address and connectivity state.")
    def network_info() -> dict:
        res: dict = {}
        try:
            ip = dev.shell("ip -f inet addr show wlan0")
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", ip)
            if m:
                res["ip"] = m.group(1)
                res["prefix"] = int(m.group(2))
        except dev.DeviceError:
            pass
        try:
            w = dev.shell("dumpsys wifi | grep -m3 -iE 'mWifiInfo|SSID'")
            m = re.search(r'SSID:\s*"?([^",]+)"?', w)
            if m:
                res["ssid"] = m.group(1).strip()
        except dev.DeviceError:
            pass
        try:
            res["airplane_mode"] = dev.shell(
                "settings get global airplane_mode_on").strip() == "1"
        except dev.DeviceError:
            pass
        return res or {"note": "no network details available"}

    @mcp.tool(description="Free/used space on internal storage, and RAM.")
    def storage_info() -> dict:
        res: dict = {}
        try:
            df = dev.shell("df -h /data").strip().splitlines()
            if len(df) > 1:
                p = df[-1].split()
                if len(p) >= 5:
                    res["data"] = {"size": p[1], "used": p[2],
                                   "avail": p[3], "use_pct": p[4]}
        except dev.DeviceError:
            pass
        try:
            mem = dev.shell("grep -e MemTotal -e MemAvailable /proc/meminfo")
            for line in mem.splitlines():
                k, _, v = line.partition(":")
                kb = int(v.strip().split()[0])
                res[k.strip()] = f"{kb/1024/1024:.2f} GB"
        except (dev.DeviceError, ValueError, IndexError):
            pass
        return res

    @mcp.tool(description="Whether the screen is on, and whether it is locked.")
    def screen_state() -> dict:
        res: dict = {}
        try:
            p = dev.shell("dumpsys power | grep -m1 mWakefulness")
            res["wakefulness"] = p.split("=")[-1].strip()
            res["screen_on"] = "Awake" in p
        except dev.DeviceError:
            pass
        try:
            w = dev.shell("dumpsys window | grep -m1 mDreamingLockscreen")
            res["locked"] = "mDreamingLockscreen=true" in w
        except dev.DeviceError:
            pass
        return res

    @mcp.tool(
        description="Turn the screen on or off. action: on|off. Does not unlock."
    )
    def screen_power(action: str = "on") -> dict:
        a = action.strip().lower()
        if a not in ("on", "off"):
            return {"error": "action must be 'on' or 'off'"}
        key = "KEYCODE_WAKEUP" if a == "on" else "KEYCODE_SLEEP"
        dev.shell(f"input keyevent {key}")
        time.sleep(0.4)
        p = dev.shell("dumpsys power | grep -m1 mWakefulness")
        return {"action": a, "wakefulness": p.split("=")[-1].strip()}

    # -- notifications ----------------------------------------------------

    @mcp.tool(
        description=(
            "Read currently posted notifications (package, title, text). "
            "Read-only; does not dismiss or act on anything."
        )
    )
    def notifications(limit: int = 25, package: str = "") -> dict:
        try:
            out = dev.shell("dumpsys notification --noredact")
        except dev.DeviceError:
            out = dev.shell("dumpsys notification")
        items, cur = [], None
        for line in out.splitlines():
            s = line.strip()
            m = re.match(r"NotificationRecord\(.*?pkg=(\S+)", s)
            if m:
                if cur:
                    items.append(cur)
                cur = {"package": m.group(1)}
                continue
            if cur is None:
                continue
            for key, attr in (("android.title=", "title"),
                              ("android.text=", "text"),
                              ("android.subText=", "subtext")):
                if key in s:
                    val = s.split(key, 1)[1].strip()
                    val = re.sub(r"^(String|CharSequence|Spannable\w*)\s*\(?", "", val)
                    cur.setdefault(attr, val.strip(" ()")[:200])
        if cur:
            items.append(cur)
        if package:
            pk = state.resolve_pkg(package)
            items = [i for i in items if i.get("package") == pk]
        seen, uniq = set(), []
        for i in items:
            k = (i.get("package"), i.get("title"), i.get("text"))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(i)
        return {"count": len(uniq), "notifications": uniq[:limit]}

    # -- clipboard --------------------------------------------------------

    @mcp.tool(
        description=(
            "Read the clipboard. Requires the uiautomator2 agent, since Android "
            "restricts clipboard reads to the foreground app and `cmd clipboard` "
            "is unavailable on many OEM builds."
        )
    )
    def clipboard_get() -> dict:
        try:
            d = dev.u2()
            return {"text": d.clipboard}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}",
                    "note": "clipboard access needs the u2 agent running"}

    @mcp.tool(description="Set the clipboard contents.")
    def clipboard_set(text: str) -> dict:
        try:
            d = dev.u2()
            d.set_clipboard(text)
            return {"set": True, "length": len(text)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # -- media / intents --------------------------------------------------

    @mcp.tool(
        description="Set or read media volume. Omit `level` to read. "
                    "level is 0..max."
    )
    def media_volume(level: Optional[int] = None) -> dict:
        if level is None:
            try:
                out = dev.shell("cmd media_session volume --stream 3 --get")
                return {"raw": out.strip()}
            except dev.DeviceError as e:
                return {"error": str(e)}
        try:
            dev.shell(f"cmd media_session volume --stream 3 --set {int(level)}")
            return {"set": int(level)}
        except dev.DeviceError as e:
            return {"error": str(e)}

    @mcp.tool(
        description="Open a URL in the device's default browser via an intent."
    )
    def open_url(url: str) -> dict:
        if not re.match(r"^https?://", url):
            return {"error": "url must start with http:// or https://"}
        safe = url.replace("&", "\\&")
        dev.shell(f"am start -a android.intent.action.VIEW -d '{safe}'")
        time.sleep(1.5)
        return {"opened": url, "foreground": dev.foreground()}

    # -- files ------------------------------------------------------------

    @mcp.tool(description="List files in a directory on the device.")
    def list_files(path: str = "/sdcard", limit: int = 60) -> dict:
        try:
            out = dev.shell(f"ls -la '{path}'")
        except dev.DeviceError as e:
            return {"error": str(e)}
        rows = []
        for line in out.splitlines()[1:]:
            p = line.split(None, 7)
            if len(p) >= 8:
                rows.append({"mode": p[0], "size": p[4], "name": p[7]})
        return {"path": path, "count": len(rows), "entries": rows[:limit]}

    @mcp.tool(
        description="Copy a file from the device to the host artifacts "
                    "directory. Returns the local path."
    )
    def pull_file(remote_path: str, name: str = "") -> dict:
        fn = name or os.path.basename(remote_path) or "pulled.bin"
        local = os.path.join(state.ARTIFACT_DIR, fn)
        try:
            dev.adb("pull", remote_path, local)
        except dev.DeviceError as e:
            return {"error": str(e)}
        return {"path": local,
                "bytes": os.path.getsize(local) if os.path.isfile(local) else 0}

    @mcp.tool(description="Copy a local file onto the device.")
    def push_file(local_path: str, remote_path: str) -> dict:
        if not os.path.isfile(local_path):
            return {"error": f"no such local file: {local_path}"}
        try:
            dev.adb("push", local_path, remote_path)
        except dev.DeviceError as e:
            return {"error": str(e)}
        return {"pushed": remote_path,
                "bytes": os.path.getsize(local_path)}

    # -- automation primitives -------------------------------------------

    @mcp.tool(
        description=(
            "Poll until an element matching `query` or `resource_id` appears, "
            "or timeout. Returns the matching elements. Use instead of blind "
            "sleeps after an action that triggers loading."
        )
    )
    def wait_for(query: str = "", resource_id: str = "",
                 timeout_s: float = 10.0, poll_s: float = 0.6) -> dict:
        from .. import ui as uix
        d = dev.u2()
        deadline = time.time() + max(0.5, timeout_s)
        polls = 0
        while time.time() < deadline:
            polls += 1
            xml = d.dump_hierarchy()
            els = uix.parse(xml)
            state.remember(els)
            hits = uix.find(els, query=query, rid=resource_id)
            if hits:
                return {"found": True, "polls": polls,
                        "waited_s": round(time.time() - (deadline - timeout_s), 2),
                        "elements": uix.compact(hits, limit=10)}
            time.sleep(poll_s)
        return {"found": False, "polls": polls, "timeout_s": timeout_s,
                "hint": "check the query, or the screen may not have loaded"}

    @mcp.tool(
        description=(
            "Swipe repeatedly until `query` appears or max_swipes is reached. "
            "Useful for reaching an off-screen item in a long list."
        )
    )
    def scroll_until(query: str, direction: str = "up", max_swipes: int = 8,
                     settle_s: float = 0.8) -> dict:
        from .. import ui as uix
        d = dev.u2()
        info = dev.device_info()
        try:
            w, h = (int(v) for v in info.screen.lower().split("x"))
        except Exception:
            w, h = 1080, 2400
        cx, cy = w // 2, h // 2
        dy = int(h * 0.30)
        moves = {"up": (cx, cy + dy, cx, cy - dy),
                 "down": (cx, cy - dy, cx, cy + dy)}
        if direction not in moves:
            return {"error": "direction must be up or down"}
        for n in range(max_swipes + 1):
            els = uix.parse(d.dump_hierarchy())
            state.remember(els)
            hits = uix.find(els, query=query)
            if hits:
                return {"found": True, "swipes": n,
                        "elements": uix.compact(hits, limit=8)}
            if n == max_swipes:
                break
            x1, y1, x2, y2 = moves[direction]
            dev.shell(f"input swipe {x1} {y1} {x2} {y2} 300")
            time.sleep(settle_s)
        return {"found": False, "swipes": max_swipes,
                "hint": "item not reached; raise max_swipes or check the query"}
