"""Selector-registry maintenance: drift checks and baselines."""

from __future__ import annotations

from .. import device as dev
from .. import state
from .. import ui as uix
from ..selectors import registry as reg


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Check whether the live app UI still matches the recorded selector "
            "baseline. Run after an app update, or when extraction starts "
            "returning nulls."
        )
    )
    def check_drift(app: str = "", screen: str = "") -> dict:
        from ..runtime import screen as scr
        c = scr.context()
        elements, live_ids, pkg = c["elements"], c["live_ids"], c["package"]
        app_name = app or state.APP_FOR_PKG.get(pkg, "")
        if not app_name:
            return {"error": f"no registry for {pkg!r}",
                    "known_apps": reg.known_apps()}
        version = c["app_version"] or ""
        which = screen or reg.detect_screen(app_name, version, live_ids)
        if not which:
            return {"error": "screen not recognised", "app": app_name,
                    "app_version": version, "backend": c["backend"],
                    "signature": uix.screen_signature(elements),
                    "live_ids_sample": sorted(live_ids)[:40]}
        out = reg.check_drift(app_name, version, which, live_ids).to_dict()
        out["backend"] = c["backend"]
        return out

    @mcp.tool(
        description=(
            "Record the current screen's resource-ids as the baseline for this "
            "app version. Use after verifying a new version, so later drift "
            "checks have something to compare against."
        )
    )
    def record_baseline(app: str = "", screen: str = "") -> dict:
        from ..runtime import screen as scr
        c = scr.context()
        pkg = c["package"]
        app_name = app or state.APP_FOR_PKG.get(pkg, "")
        if not app_name or not screen:
            return {"error": "both `app` and `screen` are required",
                    "detected_package": pkg}
        version = c["app_version"] or "unknown"
        ids = sorted(c["live_ids"])
        path = reg.record_baseline(app_name, version, screen, ids)
        # Which backend recorded it: the two agree on the id SET (measured
        # 2026-09-18, 0 missing either way), but say so rather than assume it
        # forever - a mismatch here would look exactly like app drift.
        return {"recorded": {"app": app_name, "version": version,
                             "screen": screen, "ids": len(ids),
                             "backend": c["backend"]},
                "registry": path}

    @mcp.tool(
        description="Show the selector registry for an app: known versions, "
                    "screens and defined fields."
    )
    def registry_info(app: str = "instagram") -> dict:
        data = reg.load(app)
        return {
            "app": app,
            "known_apps": reg.known_apps(),
            "versions": {
                v: {"screens": list(spec.get("screens", {})),
                    "ids": len(spec.get("all_ids", [])),
                    "recorded_at": spec.get("recorded_at")}
                for v, spec in data.get("versions", {}).items()
            },
        }
