"""Reading the screen: structured dumps, search, field extraction, screenshots."""

from __future__ import annotations

import os
import time
from typing import Any

from .. import device as dev
from .. import state
from .. import ui as uix
from ..runtime import screen as scr
from ..selectors import registry as reg


def _context(keep_noise: bool = False):
    """Shared preamble: read the screen, cache it, identify what we are on.

    Bridge first (2e). `live_ids` still comes from the raw hierarchy - every id
    including the layout containers `elements` filters out - which the bridge
    supplies as an all-windows read. See runtime/screen.py.
    """
    c = scr.context(keep_noise=keep_noise)
    return (c["elements"], c["fg"], c["package"], c["app"], c["app_version"],
            c["live_ids"], c)


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Read the current screen as structured elements. This is the primary "
            "way to see the device - use it instead of screenshot. Returns "
            "elements with index `i`, resource-id, text, content-desc, tap centre "
            "`c`, and flags `f` (C=clickable S=scrollable *=selected h=hidden), "
            "plus `ver`: tap with ref='<ver>_<i>'. "
            "Also names the screen and reports selector drift for known apps."
        )
    )
    def ui_dump(query: str = "", clickable_only: bool = False,
                limit: int = 120, include_system: bool = False) -> dict:
        elements, fg, pkg, app, version, live_ids, c = _context(
            keep_noise=include_system)
        ms = c["ms"]

        screen = drift = None
        if app and version:
            screen = reg.detect_screen(app, version, live_ids)
            if screen:
                drift = reg.check_drift(app, version, screen, live_ids).to_dict()

        shown = uix.find(elements, query=query, clickable_only=clickable_only)
        res: dict[str, Any] = {
            "package": pkg,
            "activity": fg.get("activity"),
            "app_version": version,
            "screen": screen,
            "dump_ms": ms,
            "backend": c["backend"],
            "signature": uix.screen_signature(elements),
            # Refs for tap/long_press are "<ver>_<i>".
            "ver": state.version(),
            "total_elements": len(elements),
            "returned": min(len(shown), limit),
            "elements": uix.compact(shown, limit=limit),
        }
        if drift and drift.get("status") == "DRIFT":
            res["drift_warning"] = drift
        if len(shown) > limit:
            res["truncated"] = (f"{len(shown) - limit} more; narrow with "
                                f"`query` or raise `limit`")
        return res

    @mcp.tool(
        description="Search the current screen for elements matching text or a "
                    "resource-id. Re-dumps first, so results are always fresh."
    )
    def find_element(query: str = "", resource_id: str = "",
                     clickable_only: bool = False, limit: int = 25) -> dict:
        elements, *_ = _context()
        hits = uix.find(elements, query=query, rid=resource_id,
                        clickable_only=clickable_only)
        return {"matches": len(hits), "ver": state.version(),
                "elements": uix.compact(hits, limit=limit)}

    @mcp.tool(
        description=(
            "Extract clean typed fields for a recognised screen via the "
            "versioned selector registry. Numbers arrive as {raw, value} so a "
            "parse can be audited. Missing fields are listed in `_unavailable` "
            "rather than guessed."
        )
    )
    def extract_fields(app: str = "", screen: str = "") -> dict:
        elements, fg, pkg, detected, version, live_ids, _c = _context()
        app_name = app or detected or ""
        if not app_name:
            return {"error": f"no registry for package {pkg!r}",
                    "known_apps": reg.known_apps()}
        version = version or ""
        scr = screen or reg.detect_screen(app_name, version, live_ids)
        if not scr:
            return {"error": "screen not recognised", "app": app_name,
                    "app_version": version,
                    "signature": uix.screen_signature(elements),
            # Refs for tap/long_press are "<ver>_<i>".
            "ver": state.version(),
                    "hint": "inspect with ui_dump, then record_baseline"}

        fields = reg.extract_fields(app_name, version, scr, elements)
        drift = reg.check_drift(app_name, version, scr, live_ids)
        out = {"app": app_name, "app_version": version, "screen": scr,
               "fields": fields}

        issue = _known_issue(app_name, version, scr, fields)
        if issue:
            out["data_warning"] = issue
            return out
        if not drift.ok:
            out["drift_warning"] = drift.to_dict()
        return out

    @mcp.tool(
        description=(
            "Take a screenshot and save it, returning the PATH (not the image). "
            "Expensive next to ui_dump - use only when pixels genuinely matter, "
            "e.g. content the accessibility tree cannot express."
        )
    )
    def screenshot(name: str = "") -> dict:
        fn = (name or f"shot_{int(time.time())}").replace(" ", "_")
        if not fn.endswith(".png"):
            fn += ".png"
        remote, local = f"/sdcard/{fn}", os.path.join(state.ARTIFACT_DIR, fn)
        dev.shell(f"screencap -p {remote}")
        dev.adb("pull", remote, local)
        dev.shell(f"rm -f {remote}")
        return {"path": local,
                "bytes": os.path.getsize(local) if os.path.isfile(local) else 0,
                "note": "prefer ui_dump unless pixels are required"}


def _known_issue(app: str, version: str, screen: str, fields: dict):
    """Match extracted fields against registry-declared app defects.

    Keeps app bugs from being misread as selector drift - the two demand
    opposite responses (retry vs re-baseline).
    """
    base = reg.baseline_for(app, version) or {}
    spec = base.get("screens", {}).get(screen, {})
    for issue in spec.get("known_issues", []):
        if issue.get("id") != "reels_overlay_missing":
            continue
        counts = ("like_count", "comment_count", "save_count")
        if fields.get("username") and all(
                fields.get(c) is None for c in counts):
            return {
                "issue": issue["id"],
                "detail": issue.get("symptom"),
                "cause": issue.get("cause"),
                "recovery": issue.get("recovery", []),
                "action": "discard this observation rather than storing nulls",
            }
    return None
