"""Shared session state.

The last UI dump is cached so `tap(i=...)` can resolve an element index without
re-dumping. Kept in one module because both the ui and input tool groups touch
it, and a stale cache is the most likely cause of a mis-aimed tap.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any, Optional

# Elements from the most recent dump, plus when it was taken, and its version.
last: dict[str, Any] = {"elements": [], "at": 0.0, "pkg": None, "ver": "",
                        "fp": ""}

# Screen versions (B1). Every read that shows something DIFFERENT gets a new
# version, and a ref is "<ver>_<i>". A ref from an older version is refused,
# because index i on a screen that has since moved is a different element - the
# same failure zafiro's version tokens and ARTEMIS's pre-execution check exist
# for. A re-read that shows the SAME screen keeps the version, so looking again
# does not invalidate refs the agent is holding.
_seq = 0
_REF = re.compile(r"^([0-9a-z]{1,12})_(\d+)$")

# A cached dump older than this is probably no longer what is on screen.
STALE_AFTER_S = 20.0

ARTIFACT_DIR = os.environ.get(
    "MOBILEAGENT_ARTIFACTS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "artifacts",
    ),
)
os.makedirs(ARTIFACT_DIR, exist_ok=True)

PKG_ALIASES = {
    "instagram": "com.instagram.android",
    "ig": "com.instagram.android",
    "reddit": "com.reddit.frontpage",
    "chrome": "com.android.chrome",
    "twitter": "com.twitter.android",
    "x": "com.twitter.android",
    "telegram": "org.telegram.messenger",
    "tg": "org.telegram.messenger",
    "termux": "com.termux",
    "settings": "com.android.settings",
}

APP_FOR_PKG = {
    "com.instagram.android": "instagram",
    "com.reddit.frontpage": "reddit",
    "org.telegram.messenger": "telegram",
}


def resolve_pkg(name: str) -> str:
    return PKG_ALIASES.get(name.strip().lower(), name.strip())


def fingerprint(elements) -> str:
    """What is on screen and WHERE: ids, labels and bounds of every element.

    Bounds are part of it on purpose. A list that scrolled by 40 px has the same
    labels but every tap centre moved, which is exactly when an old index lies.
    """
    h = hashlib.sha1()
    for e in elements:
        h.update(("%s|%s|%s|%s\x1f" % (getattr(e, "rid", ""),
                                       getattr(e, "text", "") or getattr(e, "desc", ""),
                                       getattr(e, "bounds", ""),
                                       getattr(e, "window", ""))).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def remember(elements, pkg: str = "") -> str:
    """Cache a screen read. Returns its version (new only if the screen changed).

    Every element's `i` is made equal to its position here. A ref resolves by
    position (`elements[i]`), but callers often filter a read before caching it -
    the bridge path drops layout noise and other windows - and the elements keep
    the index they had in the full tree. Found by the first live model run: the
    decider was shown `i=55` on a 34-element screen, tapped ref "5_55" with the
    right version, and got "index 55 out of range". Renumbering here fixes every
    filtering caller at once, including ones not written yet.
    """
    global _seq
    for n, e in enumerate(elements or []):
        if getattr(e, "i", n) != n:
            try:
                e.i = n
            except (AttributeError, TypeError):
                pass
    fp = fingerprint(elements)
    if fp != last.get("fp") or not last.get("ver"):
        _seq += 1
        last["ver"] = format(_seq, "x")
        last["fp"] = fp
    last["elements"] = elements
    last["at"] = time.time()
    if pkg:
        last["pkg"] = pkg
    return last["ver"]


def version() -> str:
    return last.get("ver") or ""


def ref(i: int) -> str:
    return "%s_%d" % (version(), i)


def with_refs(compact: list) -> list:
    """Stamp each compacted element with a ready-to-use ref for the CURRENT read.

    Every tool that hands back elements must hand back refs that tap() accepts.
    Leaving the model to assemble "<ver>_<i>" failed live: scroll_to returned
    elements with no version, the decider reused one from an earlier read, and
    the tap was refused as VERSION_MISMATCH. Call right after the read is
    remembered, so the version belongs to these elements.
    """
    v = version()
    for d in compact:
        if isinstance(d, dict) and "i" in d:
            d["ref"] = "%s_%d" % (v, d["i"])
    return compact


def parse_ref(r: str) -> Optional[tuple[str, int]]:
    m = _REF.match((r or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def cache_age() -> float:
    return time.time() - float(last.get("at") or 0)
