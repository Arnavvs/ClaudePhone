"""Shared session state.

The last UI dump is cached so `tap(i=...)` can resolve an element index without
re-dumping. Kept in one module because both the ui and input tool groups touch
it, and a stale cache is the most likely cause of a mis-aimed tap.
"""

from __future__ import annotations

import os
import time
from typing import Any

# Elements from the most recent dump, plus when it was taken.
last: dict[str, Any] = {"elements": [], "at": 0.0, "pkg": None}

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
    "termux": "com.termux",
    "settings": "com.android.settings",
}

APP_FOR_PKG = {
    "com.instagram.android": "instagram",
    "com.reddit.frontpage": "reddit",
}


def resolve_pkg(name: str) -> str:
    return PKG_ALIASES.get(name.strip().lower(), name.strip())


def remember(elements, pkg: str = "") -> None:
    last["elements"] = elements
    last["at"] = time.time()
    if pkg:
        last["pkg"] = pkg


def cache_age() -> float:
    return time.time() - float(last.get("at") or 0)
