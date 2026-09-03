"""Instagram-specific helpers."""

from __future__ import annotations

import time

from ... import device as dev
from ... import ui as uix


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Re-enter the Instagram Reels tab. Recovers from the first-reel bug "
            "(overlay renders with no counts/caption) and re-seeds the feed."
        )
    )
    def reset_reels_feed(settle_seconds: float = 3.0) -> dict:
        d = dev.u2()
        elements = uix.parse(d.dump_hierarchy())
        hits = uix.find(elements, rid="clips_tab")
        if not hits:
            return {"error": "clips_tab not found - is Instagram foreground?",
                    "foreground": dev.foreground()}
        x, y = hits[0].center
        dev.shell(f"input tap {x} {y}")
        time.sleep(max(0.0, settle_seconds))
        return {"reset": True, "tapped": [x, y],
                "note": "re-run extract_fields; discard any reel still "
                        "reporting reels_overlay_missing"}
