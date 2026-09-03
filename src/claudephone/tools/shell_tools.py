"""Arbitrary shell, at both privilege levels the phone offers.

Two distinct uids are reachable and the difference matters constantly:

  device_shell()  uid 2000 (shell) via adb   - input, uiautomator, pm, settings,
                                               dumpsys, am. The privileged one.
  termux_shell()  uid 10308 (app)  directly  - the Termux userland: python, git,
                                               ffmpeg, curl, the filesystem.

An agent that does not know which one it needs will flail, so both are exposed
explicitly rather than hidden behind one "run a command" tool.
"""

from __future__ import annotations

import os
import subprocess

from .. import device as dev

# Substrings that indicate a command which cannot be undone by re-running it.
# This is a speed bump for an agent that is confused, not a security boundary -
# the shell is fully general and anything here can be spelled another way.
_DESTRUCTIVE = (
    "rm -rf /", "mkfs", "dd if=", "fastboot", "wipe", "format",
    "pm uninstall --user 0 android", "settings delete",
    "recovery --wipe_data", ">/dev/block", "> /dev/block",
)


def _guard(cmd: str) -> str:
    low = " ".join(cmd.lower().split())
    for pat in _DESTRUCTIVE:
        if pat in low:
            return pat
    return ""


def register(reg) -> None:

    @reg.tool(
        description=(
            "Run a shell command AS THE ANDROID SHELL USER (uid 2000) via adb. "
            "This is the privileged level: `input`, `uiautomator`, `pm`, `am`, "
            "`settings`, `dumpsys`, `svc`, `content` all work here. Use it for "
            "anything that touches the Android system rather than the Termux "
            "userland. Returns stdout, stderr and exit code."
        ),
        dangerous=True,
    )
    def device_shell(command: str, timeout_s: int = 60,
                     confirm_destructive: bool = False) -> dict:
        hit = _guard(command)
        if hit and not confirm_destructive:
            return {"error": "refused: command matches destructive pattern "
                             + repr(hit),
                    "hint": "pass confirm_destructive=true if you truly mean it"}
        try:
            out = dev.adb("shell", command, timeout=timeout_s, check=False)
        except Exception as e:
            return {"error": type(e).__name__ + ": " + str(e)[:400]}
        return {"uid": "2000(shell)", "command": command,
                "stdout": out[:20000],
                "truncated": len(out) > 20000}

    @reg.tool(
        description=(
            "Run a shell command in the TERMUX userland (uid 10308) - python, "
            "git, ffmpeg, curl, pkg, and the Termux filesystem. Cannot use "
            "`input`/`uiautomator`/`settings`: those need device_shell. Only "
            "meaningful when ClaudePhone is running on the phone itself."
        ),
        dangerous=True,
    )
    def termux_shell(command: str, timeout_s: int = 120,
                     cwd: str = "", confirm_destructive: bool = False) -> dict:
        hit = _guard(command)
        if hit and not confirm_destructive:
            return {"error": "refused: command matches destructive pattern "
                             + repr(hit),
                    "hint": "pass confirm_destructive=true if you truly mean it"}
        if not dev.on_device():
            return {"error": "not running on the phone; termux_shell is "
                             "on-device only. Use device_shell instead."}
        try:
            p = subprocess.run(["bash", "-lc", command], capture_output=True,
                               text=True, timeout=timeout_s,
                               cwd=cwd or None, encoding="utf-8",
                               errors="replace")
        except subprocess.TimeoutExpired:
            return {"error": "timed out after " + str(timeout_s) + "s"}
        except Exception as e:
            return {"error": type(e).__name__ + ": " + str(e)[:400]}
        return {"uid": "termux", "command": command, "exit_code": p.returncode,
                "stdout": (p.stdout or "")[:20000],
                "stderr": (p.stderr or "")[:4000]}

    @reg.tool(
        description="Run a snippet of Python in this agent's own interpreter "
                    "and return whatever it prints plus the value of the last "
                    "expression. Useful for arithmetic, parsing and reshaping "
                    "data you already fetched, without another round trip."
    )
    def python_eval(code: str, timeout_s: int = 30) -> dict:
        import contextlib
        import io
        buf = io.StringIO()
        env: dict = {}
        try:
            with contextlib.redirect_stdout(buf):
                lines = code.strip().splitlines()
                last = lines[-1] if lines else ""
                try:
                    compiled = compile(last, "<last>", "eval")
                    exec("\n".join(lines[:-1]), env)
                    value = eval(compiled, env)
                except SyntaxError:
                    exec(code, env)
                    value = None
        except Exception as e:
            return {"error": type(e).__name__ + ": " + str(e)[:400],
                    "stdout": buf.getvalue()[:4000]}
        return {"stdout": buf.getvalue()[:8000],
                "value": repr(value)[:2000] if value is not None else None}

    @reg.tool(
        description="Report which uids and privileged capabilities are "
                    "actually available right now. Run this when a command "
                    "fails with a permission error and you need to know "
                    "whether to switch shells."
    )
    def privileges() -> dict:
        res: dict = {"on_device": dev.on_device(),
                     "serial": dev.default_serial()}
        try:
            res["device_shell_id"] = dev.shell("id", check=False).strip()[:200]
        except Exception as e:
            res["device_shell_id"] = "unavailable: " + str(e)[:160]
        if dev.on_device():
            res["termux_uid"] = str(os.getuid())
        probes = {
            "input": "input keyevent 0",
            "settings_read": "settings get system screen_brightness",
            "pm_list": "pm list packages -s",
            "dumpsys": "dumpsys battery",
        }
        caps = {}
        for label, cmd in probes.items():
            try:
                dev.shell(cmd, timeout=15)
                caps[label] = True
            except Exception:
                caps[label] = False
        res["capabilities"] = caps
        return res
