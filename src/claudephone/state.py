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
    """Cache a screen read. Returns its version (new only if the screen changed)."""
    global _seq
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


def parse_ref(r: str) -> Optional[tuple[str, int]]:
    m = _REF.match((r or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def cache_age() -> float:
    return time.time() - float(last.get("at") or 0)
