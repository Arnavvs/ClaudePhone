"""One screen read for the tools, bridge first (2e).

Every tool that read the screen used to call `dev.u2().dump_hierarchy()`. On the
Samsung that suppresses the accessibility service (docs/ACCESSIBILITY.md), so a
session mixing the two backends paid a ~1.7 s hand-back on each alternation, and
the Instagram tools moved to the bridge in 2d. This is the rest of them.

The awkward part is `live_ids`. Drift checks and recorded baselines compare
against every id in the RAW hierarchy, including pure-layout containers that
`uix.parse` filters out of the element list - checking the filtered set reports
working selectors as missing. `uix.all_resource_ids(xml)` gives that from a u2
dump; the bridge's equivalent is a tree read with **all windows**, because the
active window alone leaves out the status and navigation bars.

Measured 2026-09-18 on the Samsung, both on Settings and on Instagram Reels:

    raw XML ids                 107 (IG) / 106 (Settings)
    bridge, active window only   70      /  63      -> 42-43 missing, all system bars
    bridge, all windows         119      / 120      -> 0 missing, 12-14 extra

So an all-windows bridge read is a superset of the XML ids, and a baseline
recorded through either backend checks out against the other. The extra ids are
containers the bridge keeps and the XML parser never exposed; extras cannot
cause false drift, since drift is a baseline id MISSING from the live screen.

`elements` stays the app's own window, denoised the way `uix.parse` denoises a
dump, so tools see the same kind of list from either backend.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import device as dev
from .. import state
from .. import ui as uix

# uix.parse drops these when they carry no value and cannot be interacted with.
# It compares full class names before shortening; the bridge sends short ones.
_NOISE = {"FrameLayout", "LinearLayout", "RelativeLayout", "ViewGroup", "View"}


def denoise(elements: list) -> list:
    """Bridge elements, minus the layout containers a u2 dump would have hidden."""
    out = []
    for e in elements:
        if (e.text or e.desc) or e.clickable or e.scrollable:
            out.append(e)
        elif e.rid and e.cls not in _NOISE:
            out.append(e)
    return out


def context(serial: str = "", limit: int = 600, remember: bool = True,
            keep_noise: bool = False) -> dict:
    """Read the screen and everything the tools derive from it.

    -> elements, all_elements, live_ids, fg, package, app, app_version,
       backend, ms (and `xml`, only on the u2 path).
    """
    from . import bridge as br
    t0 = time.time()
    xml: Optional[str] = None
    if br.available(serial):
        r = br.bridge(serial).tree(limit=limit, all_windows=True)
        every = r["elements"]
        # Elements from other windows carry a window tag; the app's own do not.
        own = [e for e in every if not getattr(e, "window", "")]
        # keep_noise is ui_dump's include_system: a u2 dump has the status and
        # navigation bars in the same XML, so "everything" means every window
        # here, not just the app's containers. Without it the bridge returns
        # about 6 fewer elements than u2 on the same screen - clock, battery,
        # signal, back, home, recents - which is the whole difference measured
        # on Instagram (49 vs 60 on Reels, 78 vs 85 on a profile).
        elements = every if keep_noise else denoise(own)
        live_ids = {e.rid for e in every if e.rid}
        pkg = r.get("foreground") or r.get("package") or ""
        fg = {"package": pkg, "activity": (dev.foreground(serial=serial) or {}).get("activity")}
        backend = "bridge"
    else:
        xml = dev.u2(serial).dump_hierarchy()
        elements = uix.parse(xml, keep_noise=keep_noise)
        every = elements
        live_ids = set(uix.all_resource_ids(xml))
        fg = dev.foreground(serial=serial) or {}
        pkg = fg.get("package") or ""
        backend = "u2"
    if remember:
        state.remember(elements, pkg)
    return {
        "elements": elements,
        "all_elements": every,
        "live_ids": live_ids,
        "fg": fg,
        "package": pkg,
        "app": state.APP_FOR_PKG.get(pkg),
        "app_version": dev.app_version(pkg) if pkg else None,
        "backend": backend,
        "xml": xml,
        "ms": int((time.time() - t0) * 1000),
    }
