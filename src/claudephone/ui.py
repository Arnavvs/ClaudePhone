"""Structured UI extraction.

Design constraint from the project owner: agents consume UI as STRUCTURED
ELEMENTS, not screenshots. A raw uiautomator hierarchy for one Instagram Reel is
~70 KB of XML; the compact form below is typically 1-3 KB for the same screen.
Screenshots remain available but are opt-in and return a file path rather than
inline image data.

Two things learned the hard way during research and encoded here:

1. Values often sit on ANONYMOUS CHILD nodes, not on the resource-id that names
   them. Instagram's caption lives under `clips_caption_component` and the audio
   track under `clips_author_info_component`, while `media_album_art_button`
   merely carries the literal string "Audio". Every element therefore reports
   `anchor` = nearest ancestor resource-id, so a value can be located by the
   container that owns it.
2. Some fields blank momentarily during rendering. Callers comparing screens
   across time should ignore transient nulls rather than treating them as change.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Optional

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Layout classes that carry no semantics on their own.
_NOISE_CLASSES = {
    "android.widget.FrameLayout",
    "android.widget.LinearLayout",
    "android.widget.RelativeLayout",
    "android.view.ViewGroup",
    "android.view.View",
}

# System UI resource-ids that pollute every dump.
_NOISE_RID = re.compile(
    r"^(com\.android\.systemui|android):id/"
    r"(clock|battery|stat_|statusIcons|navigationBar|statusBar|wifi_|phone_)"
)


@dataclass
class Element:
    i: int
    rid: str = ""
    anchor: str = ""
    text: str = ""
    desc: str = ""
    cls: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    scrollable: bool = False
    selected: bool = False
    checked: bool = False
    pkg: str = ""

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    def to_dict(self, with_bounds: bool = True) -> dict:
        d: dict = {"i": self.i}
        if self.rid:
            d["id"] = self.rid
        if self.anchor and self.anchor != self.rid:
            d["anchor"] = self.anchor
        if self.text:
            d["text"] = self.text
        if self.desc and self.desc != self.text:
            d["desc"] = self.desc
        if self.cls:
            d["cls"] = self.cls
        if with_bounds:
            d["c"] = list(self.center)
        flags = "".join([
            "C" if self.clickable else "",
            "S" if self.scrollable else "",
            "*" if self.selected else "",
            "x" if self.checked else "",
        ])
        if flags:
            d["f"] = flags
        return d


def _short_rid(rid: str) -> str:
    return rid.split(":id/")[-1] if ":id/" in rid else rid


def _short_cls(cls: str) -> str:
    return cls.rsplit(".", 1)[-1] if cls else ""


def parse(xml: str, keep_noise: bool = False) -> list[Element]:
    """Flatten a hierarchy dump into semantic elements."""
    out: list[Element] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out

    idx = 0

    def walk(node, chain: list[str]):
        nonlocal idx
        a = node.attrib
        raw_rid = a.get("resource-id", "") or ""
        rid = _short_rid(raw_rid)
        new_chain = chain + [rid] if rid else chain

        text = (a.get("text") or "").strip()
        desc = (a.get("content-desc") or "").strip()
        cls = a.get("class", "") or ""

        noisy = bool(raw_rid and _NOISE_RID.match(raw_rid))
        has_value = bool(text or desc)
        interactive = a.get("clickable") == "true" or a.get("scrollable") == "true"
        meaningful = has_value or (rid and cls not in _NOISE_CLASSES) or interactive

        if meaningful and (keep_noise or not noisy):
            m = _BOUNDS.search(a.get("bounds", ""))
            b = tuple(int(g) for g in m.groups()) if m else (0, 0, 0, 0)
            out.append(Element(
                i=idx,
                rid=rid,
                anchor=(new_chain[-1] if new_chain else ""),
                text=text[:300],
                desc=desc[:300],
                cls=_short_cls(cls),
                bounds=b,  # type: ignore[arg-type]
                clickable=a.get("clickable") == "true",
                scrollable=a.get("scrollable") == "true",
                selected=a.get("selected") == "true",
                checked=a.get("checked") == "true",
                pkg=a.get("package", "") or "",
            ))
            idx += 1

        for c in node:
            walk(c, new_chain)

    walk(root, [])
    return out


def values_by_anchor(elements: Iterable[Element]) -> dict[str, list[str]]:
    """Map anchor -> the text/desc values found beneath it.

    This is how you retrieve a value that sits on an anonymous child, e.g.
    values_by_anchor(...)['clips_caption_component'] -> ['the caption text'].
    """
    bag: dict[str, list[str]] = {}
    for e in elements:
        v = e.text or e.desc
        if not v or not e.anchor:
            continue
        bag.setdefault(e.anchor, []).append(v)
    return bag


def first_value(elements: Iterable[Element], anchor: str,
                prefer: str = "any") -> Optional[str]:
    """First non-empty value under `anchor`. prefer: 'text' | 'desc' | 'any'."""
    for e in elements:
        if e.anchor != anchor:
            continue
        if prefer == "text" and e.text:
            return e.text
        if prefer == "desc" and e.desc:
            return e.desc
        if prefer == "any" and (e.text or e.desc):
            return e.text or e.desc
    return None


def find(elements: Iterable[Element], query: str = "", rid: str = "",
         clickable_only: bool = False) -> list[Element]:
    q = query.lower().strip()
    hits = []
    for e in elements:
        if clickable_only and not e.clickable:
            continue
        if rid and e.rid != rid and e.anchor != rid:
            continue
        if q:
            hay = f"{e.rid} {e.text} {e.desc}".lower()
            if q not in hay:
                continue
        hits.append(e)
    return hits


def screen_signature(elements: Iterable[Element]) -> str:
    """Stable fingerprint of a screen from its resource-id set.

    Used to (a) recognise a known screen and (b) detect selector drift after an
    app update - the signature changes when ids are added, removed or renamed.
    """
    ids = sorted({e.rid for e in elements if e.rid})
    return hashlib.sha1("|".join(ids).encode()).hexdigest()[:12]


def resource_ids(elements: Iterable[Element], pkg_prefix: str = "") -> list[str]:
    """Resource-ids of the SURVIVING elements. Do not use for drift checks."""
    ids = {e.rid for e in elements if e.rid}
    if pkg_prefix:
        ids = {i for i in ids if not i.startswith("com.android")}
    return sorted(ids)


_RID_ATTR = re.compile(r'resource-id="([^"]+)"')


def all_resource_ids(xml: str) -> set[str]:
    """Every resource-id in the RAW hierarchy, including filtered containers.

    Drift checks must use this, not resource_ids(). Pure-layout containers such
    as `clips_caption_component` are dropped from the element list as noise (a
    ViewGroup with no text of its own), yet they remain valid anchors because
    their anonymous children carry the value. Checking drift against the
    filtered set reports those containers as "missing" while extraction is in
    fact working - a false alarm that would send you chasing a non-existent app
    update.
    """
    return {_short_rid(m) for m in _RID_ATTR.findall(xml)}


def compact(elements: list[Element], limit: int = 120,
            with_bounds: bool = True) -> list[dict]:
    return [e.to_dict(with_bounds=with_bounds) for e in elements[:limit]]
