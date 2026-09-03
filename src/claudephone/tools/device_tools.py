"""Device discovery, app lifecycle, package listing."""

from __future__ import annotations

import time

from .. import device as dev
from .. import state


def register(mcp) -> None:

    @mcp.tool(description="List connected devices and their state.")
    def devices() -> dict:
        return {"devices": dev.list_devices()}

    @mcp.tool(description="Model, Android version, screen size, density, battery.")
    def device_info() -> dict:
        i = dev.device_info()
        return {
            "serial": i.serial, "model": i.model, "android": i.android,
            "sdk": i.sdk, "build": i.build, "screen": i.screen,
            "density": i.density, "battery_pct": i.battery,
        }

    @mcp.tool(description="Package and activity currently in the foreground.")
    def foreground_app() -> dict:
        fg = dev.foreground()
        pkg = fg.get("package")
        if pkg:
            fg["version"] = dev.app_version(pkg)
        return fg

    @mcp.tool(
        description="Launch an app. Accepts a package name or an alias "
                    "(instagram, reddit, chrome, twitter, settings)."
    )
    def launch_app(package: str, wait_seconds: float = 2.0) -> dict:
        pkg = state.resolve_pkg(package)
        dev.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
        time.sleep(max(0.0, wait_seconds))
        return {"launched": pkg, "foreground": dev.foreground()}

    @mcp.tool(
        description="Force-stop an app. Use before relaunching when you need a "
                    "fresh start: `am start` on a live task only refocuses it."
    )
    def stop_app(package: str) -> dict:
        pkg = state.resolve_pkg(package)
        dev.shell(f"am force-stop {pkg}")
        return {"stopped": pkg}

    @mcp.tool(description="List installed packages, optionally filtered.")
    def list_apps(filter: str = "", limit: int = 60) -> dict:
        out = dev.shell("pm list packages")
        pkgs = sorted(l.replace("package:", "").strip()
                      for l in out.splitlines() if l.strip())
        if filter:
            f = filter.lower()
            pkgs = [p for p in pkgs if f in p.lower()]
        return {"count": len(pkgs), "packages": pkgs[:limit]}
