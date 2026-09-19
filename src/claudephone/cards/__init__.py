"""App cards: what this project already learned about an app, handed to the model
the first time that app is on screen (B6).

An agent on the phone never read PROJECT-CONTEXT, so without these it would
rediscover each trap - Instagram 446's search going silent after one query, the
reel overlay lagging the swipe, a sheet left open stranding the next step - at
the cost of steps and, on Instagram, of reads counted against the account.

A card is a short markdown file named after the package. It is injected once per
run, when the foreground first becomes that package, so a run that never opens
Instagram never pays for the Instagram card. Keep cards short: every line is
tokens on every later step of the run.

deeplinks.json beside them is the registry of links `open_link` will follow -
only ones verified on a device, each with the package it must land in.
"""

from __future__ import annotations

import json
import os
from typing import Optional

CARD_DIR = os.path.dirname(os.path.abspath(__file__))
LINKS_PATH = os.path.join(CARD_DIR, "deeplinks.json")
_cache: dict = {}


def load(package: str) -> Optional[str]:
    """The card for a package, or None. Package names are the file names."""
    if not package or "/" in package or "\\" in package or ".." in package:
        return None
    if package in _cache:
        return _cache[package]
    path = os.path.join(CARD_DIR, package + ".md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        text = None
    _cache[package] = text
    return text


def available() -> list:
    return sorted(f[:-3] for f in os.listdir(CARD_DIR) if f.endswith(".md"))


def links(path: str = "") -> list:
    """The deep-link registry: [{prefix, package, note, verified, ...}]."""
    with open(path or LINKS_PATH, encoding="utf-8") as f:
        return json.load(f).get("links", [])


def match(url: str, registry: Optional[list] = None) -> Optional[dict]:
    """The registry entry whose prefix `url` starts with; longest prefix wins."""
    u = (url or "").strip()
    best = None
    for e in registry if registry is not None else links():
        p = e.get("prefix") or ""
        if p and u.startswith(p) and (best is None or len(p) > len(best["prefix"])):
            best = e
    return best
