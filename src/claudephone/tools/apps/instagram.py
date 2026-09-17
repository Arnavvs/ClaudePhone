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
        from ...policy import reads
        gate = reads.acquire("feed_reel", "ig", target="reels_tab")
        if not gate.allowed:
            return reads.refusal(gate, reset=False)
        x, y = hits[0].center
        dev.shell(f"input tap {x} {y}")
        reads.commit(gate, target="reels_tab")
        time.sleep(max(0.0, settle_seconds))
        return {"reset": True, "tapped": [x, y], "read": gate.to_dict(),
                "note": "re-run extract_fields; discard any reel still "
                        "reporting reels_overlay_missing"}
