"""Files on the phone.

The awkward part this module hides: **the two uids see different filesystems.**

  /sdcard, /data/local/tmp   readable by uid 2000 (shell) -> go via adb
  /data/data/com.termux/...  readable by uid 10308 only   -> go via local fs

So `adb shell cat` works for the first and silently fails for the second, while
plain `open()` works for the second and only exists when we are on the phone.
`_route()` picks per path, so the agent never has to think about it.
"""

from __future__ import annotations

import os
import posixpath
import shlex

from .. import device as dev
from .. import state

TERMUX_ROOTS = ("/data/data/com.termux", "/data/user/0/com.termux")
TEXT_MAX = 60_000


def _is_termux_path(path: str) -> bool:
    return any(path.startswith(r) for r in TERMUX_ROOTS)


def _route(path: str) -> str:
    """'local' to use the Python filesystem, 'adb' to shell out."""
    if dev.on_device() and (_is_termux_path(path) or os.path.exists(path)):
        return "local"
    return "adb"


def register(reg) -> None:

    @reg.tool(
        description="List a directory on the phone. Works across both the "
                    "Android filesystem (/sdcard, /data/local/tmp) and the "
                    "Termux filesystem, choosing the right access route "
                    "automatically."
    )
    def list_dir(path: str = "/sdcard", limit: int = 200) -> dict:
        if _route(path) == "local":
            try:
                names = sorted(os.listdir(path))
            except OSError as e:
                return {"error": str(e)}
            rows = []
            for n in names[:limit]:
                full = posixpath.join(path, n)
                try:
                    st = os.stat(full)
                    rows.append({"name": n, "dir": os.path.isdir(full),
                                 "size": st.st_size})
                except OSError:
                    rows.append({"name": n})
            return {"path": path, "route": "local", "count": len(names),
                    "entries": rows}
        out = dev.shell("ls -la " + shlex.quote(path), check=False)
        lines = [l for l in out.splitlines() if l.strip()][:limit]
        return {"path": path, "route": "adb", "entries": lines}

    @reg.tool(
        description="Read a text file from the phone. Returns up to ~60 KB; "
                    "use offset/limit for anything larger rather than pulling "
                    "the whole file into context."
    )
    def read_file(path: str, offset: int = 0, limit: int = 0) -> dict:
        if _route(path) == "local":
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                return {"error": str(e)}
            route = "local"
        else:
            text = dev.shell("cat " + shlex.quote(path), check=False)
            route = "adb"
        lines = text.splitlines()
        if offset or limit:
            end = offset + limit if limit else len(lines)
            chunk = lines[offset:end]
        else:
            chunk = lines
        body = "\n".join(chunk)
        return {"path": path, "route": route, "total_lines": len(lines),
                "returned_lines": len(chunk),
                "content": body[:TEXT_MAX],
                "truncated": len(body) > TEXT_MAX}

    @reg.tool(
        description="Write a text file on the phone, creating parent "
                    "directories as needed. Overwrites any existing file.",
        dangerous=True,
    )
    def write_file(path: str, content: str, append: bool = False) -> dict:
        if _route(path) == "local":
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a" if append else "w", encoding="utf-8") as f:
                    f.write(content)
            except OSError as e:
                return {"error": str(e)}
            return {"path": path, "route": "local", "bytes": len(content),
                    "mode": "append" if append else "overwrite"}
        import base64
        b64 = base64.b64encode(content.encode()).decode()
        redirect = ">>" if append else ">"
        dev.shell("mkdir -p " + shlex.quote(posixpath.dirname(path) or "/"),
                  check=False)
        dev.shell("echo " + b64 + " | base64 -d " + redirect
                  + " " + shlex.quote(path), check=False)
        return {"path": path, "route": "adb", "bytes": len(content),
                "mode": "append" if append else "overwrite"}

    @reg.tool(
        description="Replace an exact string in a file on the phone. Fails "
                    "loudly if the string is absent or appears more than once, "
                    "so a bad edit cannot silently hit the wrong place.",
        dangerous=True,
    )
    def edit_file(path: str, find: str, replace: str) -> dict:
        got = read_file(path)
        if "error" in got:
            return got
        text = got["content"]
        n = text.count(find)
        if n == 0:
            return {"error": "string not found in " + path}
        if n > 1:
            return {"error": "string appears " + str(n) + " times; make it "
                             "unique so the edit is unambiguous"}
        return write_file(path, text.replace(find, replace))

    @reg.tool(
        description="Search file contents on the phone with grep. Returns "
                    "matching lines with their file and line number."
    )
    def grep_files(pattern: str, path: str = "/sdcard", limit: int = 60,
                   ignore_case: bool = True) -> dict:
        flags = "-rnI" + ("i" if ignore_case else "")
        out = dev.shell("grep " + flags + " " + shlex.quote(pattern) + " "
                        + shlex.quote(path) + " 2>/dev/null | head -"
                        + str(limit), check=False)
        hits = [l for l in out.splitlines() if l.strip()]
        return {"pattern": pattern, "path": path, "matches": len(hits),
                "lines": hits}

    @reg.tool(
        description="Find files by name pattern on the phone, e.g. '*.mp4'."
    )
    def find_files(name: str, path: str = "/sdcard", limit: int = 100) -> dict:
        out = dev.shell("find " + shlex.quote(path) + " -iname "
                        + shlex.quote(name) + " 2>/dev/null | head -"
                        + str(limit), check=False)
        found = [l for l in out.splitlines() if l.strip()]
        return {"name": name, "path": path, "count": len(found),
                "files": found}

    @reg.tool(
        description="Delete a file or directory on the phone. Irreversible - "
                    "the phone has no trash.",
        dangerous=True,
    )
    def delete_file(path: str, recursive: bool = False) -> dict:
        if path.rstrip("/") in ("", "/", "/sdcard", "/data", "/system"):
            return {"error": "refusing to delete " + path}
        flag = "-rf " if recursive else ""
        out = dev.shell("rm " + flag + shlex.quote(path), check=False)
        return {"deleted": path, "recursive": recursive,
                "output": out.strip()[:400] or None}

    @reg.tool(
        description="Copy a file OFF the phone to the machine running this "
                    "agent. On-device that is a local copy into the artifacts "
                    "directory; from a laptop it is an adb pull."
    )
    def pull_file(remote_path: str, name: str = "") -> dict:
        dest = os.path.join(state.ARTIFACT_DIR,
                            name or posixpath.basename(remote_path))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if _route(remote_path) == "local":
            import shutil
            try:
                shutil.copyfile(remote_path, dest)
            except OSError as e:
                return {"error": str(e)}
        else:
            dev.adb("pull", remote_path, dest, check=False)
        if not os.path.exists(dest):
            return {"error": "pull produced no file at " + dest}
        return {"remote": remote_path, "local": dest,
                "bytes": os.path.getsize(dest)}
