"""Reading the screen: structured dumps, search, field extraction, screenshots."""

from __future__ import annotations

import os
import time
from typing import Any

from .. import device as dev
from .. import state
from .. import ui as uix
from ..selectors import registry as reg


def _context(xml: str):
    """Shared preamble: parse, cache, and identify what we are looking at."""
    elements = uix.parse(xml)
    fg = dev.foreground()
    pkg = fg.get("package") or ""
    state.remember(elements, pkg)
    app = state.APP_FOR_PKG.get(pkg)
    version = dev.app_version(pkg) if pkg else None
    # Drift MUST be computed from the raw hierarchy: pure-layout containers are
    # filtered out of `elements` as noise yet remain valid anchors, so checking
    # the filtered set reports working selectors as missing.
    live_ids = uix.all_resource_ids(xml)
    return elements, fg, pkg, app, version, live_ids


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Read the current screen as structured elements. This is the primary "
            "way to see the device - use it instead of screenshot. Returns "
            "elements with index `i` (for tap), resource-id, text, content-desc, "
            "tap centre `c`, and flags `f` (C=clickable S=scrollable *=selected). "
            "Also names the screen and reports selector drift for known apps."
        )
    )
    def ui_dump(query: str = "", clickable_only: bool = False,
                limit: int = 120, include_system: bool = False) -> dict:
        d = dev.u2()
        t0 = time.time()
        xml = d.dump_hierarchy()
        ms = int((time.time() - t0) * 1000)

        elements, fg, pkg, app, version, live_ids = _context(xml)
        if include_system:
            elements = uix.parse(xml, keep_noise=True)
            state.remember(elements, pkg)

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
            "signature": uix.screen_signature(elements),
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
        d = dev.u2()
        elements, *_ = _context(d.dump_hierarchy())
        hits = uix.find(elements, query=query, rid=resource_id,
                        clickable_only=clickable_only)
        return {"matches": len(hits), "elements": uix.compact(hits, limit=limit)}

    @mcp.tool(
        description=(
            "Extract clean typed fields for a recognised screen via the "
            "versioned selector registry. Numbers arrive as {raw, value} so a "
            "parse can be audited. Missing fields are listed in `_unavailable` "
            "rather than guessed."
        )
    )
    def extract_fields(app: str = "", screen: str = "") -> dict:
        d = dev.u2()
        elements, fg, pkg, detected, version, live_ids = _context(
            d.dump_hierarchy())
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
