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
        d = dev.u2()
        xml = d.dump_hierarchy()
        elements = uix.parse(xml)
        live_ids = uix.all_resource_ids(xml)
        fg = dev.foreground()
        pkg = fg.get("package") or ""
        app_name = app or state.APP_FOR_PKG.get(pkg, "")
        if not app_name:
            return {"error": f"no registry for {pkg!r}",
                    "known_apps": reg.known_apps()}
        version = dev.app_version(pkg) or ""
        scr = screen or reg.detect_screen(app_name, version, live_ids)
        if not scr:
            return {"error": "screen not recognised", "app": app_name,
                    "app_version": version,
                    "signature": uix.screen_signature(elements),
                    "live_ids_sample": sorted(live_ids)[:40]}
        return reg.check_drift(app_name, version, scr, live_ids).to_dict()

    @mcp.tool(
        description=(
            "Record the current screen's resource-ids as the baseline for this "
            "app version. Use after verifying a new version, so later drift "
            "checks have something to compare against."
        )
    )
    def record_baseline(app: str = "", screen: str = "") -> dict:
        d = dev.u2()
        xml = d.dump_hierarchy()
        fg = dev.foreground()
        pkg = fg.get("package") or ""
        app_name = app or state.APP_FOR_PKG.get(pkg, "")
        if not app_name or not screen:
            return {"error": "both `app` and `screen` are required",
                    "detected_package": pkg}
        version = dev.app_version(pkg) or "unknown"
        ids = sorted(uix.all_resource_ids(xml))
        path = reg.record_baseline(app_name, version, screen, ids)
        return {"recorded": {"app": app_name, "version": version,
                             "screen": screen, "ids": len(ids)},
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
