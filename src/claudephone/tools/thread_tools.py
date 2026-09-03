"""Comment-thread extraction with nesting.

Comment UIs are harder than feeds for two reasons:

1. Header and body are SEPARATE anchors emitted in document order, not one
   element per comment. A comment must be assembled by pairing a header with
   the body nodes that follow it, up to the next header. One comment can emit
   several body nodes (quotes, links), so "first body after header" loses text.

2. Nesting depth has to come from somewhere reliable. On Reddit it is stated
   outright - "Level 2 comment by ..." - which beats guessing. X-indent is the
   usual fallback but it is NOT trustworthy here: level 4 and level 5 comments
   were both observed at x=616. Prefer a declared level; fall back to indent
   clustering only when no level is available, and say which was used.

Read-only by construction: the composer anchor is recorded so it can be
explicitly excluded from any tap.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from .. import device as dev
from .. import state
from .. import ui as uix
from ..selectors import registry as reg


def _thread_spec(app: str, version: str, screen: str) -> dict:
    base = reg.baseline_for(app, version) or {}
    return base.get("screens", {}).get(screen, {}).get("thread", {}) or {}


def assemble(elements, spec: dict) -> list[dict]:
    """Pair headers with their following bodies, in document order."""
    header_a = spec.get("header_anchor")
    body_a = spec.get("body_anchor")
    footer_a = spec.get("footer_anchor")
    ad_a = spec.get("ad_anchor")
    header_pat = spec.get("header_composite", {})
    level_re = spec.get("level_pattern")

    out: list[dict] = []
    cur: Optional[dict] = None

    for e in elements:               # Element.i is assigned in document order
        anchor = e.anchor or e.rid
        val = (e.text or e.desc or "").strip()
        if not val:
            continue

        if ad_a and anchor == ad_a:
            if cur:
                out.append(cur)
                cur = None
            continue

        if header_a and anchor == header_a:
            if cur:
                out.append(cur)
            cur = {"_header": val, "body": [], "level": None}
            if header_pat:
                parsed = reg.apply_composite(val, header_pat)
                parsed.pop("_raw", None)
                cur.update({k: v for k, v in parsed.items() if v is not None})
            if level_re:
                m = re.search(level_re, val)
                if m:
                    try:
                        cur["level"] = int(m.group(1))
                        cur["level_source"] = "declared"
                    except (ValueError, IndexError):
                        pass
            if cur.get("level") is None:
                cur["level"] = None
                cur["level_source"] = "unknown"
            cur["x"] = e.center[0]
            continue

        if body_a and anchor == body_a and cur is not None:
            cur["body"].append(val)
            continue

        if footer_a and anchor == footer_a and cur is not None:
            cur["footer"] = val

    if cur:
        out.append(cur)

    for c in out:
        c["text"] = " ".join(c.pop("body")).strip()
    return out


def infer_levels_by_indent(comments: list[dict], tol: int = 20) -> None:
    """Fallback when no level is declared: cluster x-offsets into depths.

    Marked `level_source: "indent"` so downstream can weigh it - this is a
    heuristic and it is wrong whenever two depths share an x, which does happen.
    """
    xs = sorted({c["x"] for c in comments if c.get("x") is not None})
    if not xs:
        return
    buckets: list[int] = []
    for x in xs:
        if not buckets or x - buckets[-1] > tol:
            buckets.append(x)
    for c in comments:
        if c.get("level") is not None or c.get("x") is None:
            continue
        best = min(range(len(buckets)), key=lambda i: abs(buckets[i] - c["x"]))
        c["level"] = best + 1
        c["level_source"] = "indent"


def nest(comments: list[dict]) -> list[dict]:
    """Turn a flat level-tagged list into a tree using a depth stack."""
    roots: list[dict] = []
    stack: list[tuple[int, dict]] = []
    for c in comments:
        node = {k: v for k, v in c.items() if not k.startswith("_")}
        node.pop("x", None)
        node["replies"] = []
        lvl = node.get("level") or 1
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        if stack:
            stack[-1][1]["replies"].append(node)
        else:
            roots.append(node)
        stack.append((lvl, node))
    return roots


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Collect a comment thread with nested replies. Scrolls, pairs each "
            "header with the body nodes that follow it, deduplicates, and "
            "returns either a nested tree or a flat list. Requires the screen "
            "to have a `thread` spec in the selector registry - use "
            "explore_screen + learn_thread on an unmapped app first.\n"
            "Read-only: never touches the reply composer."
        )
    )
    def collect_thread(max_comments: int = 60, max_swipes: int = 30,
                       settle_s: float = 1.5, nested: bool = True,
                       app: str = "", screen: str = "") -> dict:
        d = dev.u2()
        info = dev.device_info()
        try:
            w, h = (int(v) for v in info.screen.lower().split("x"))
        except Exception:
            w, h = 1080, 2400
        cx, cy, dy = w // 2, h // 2, int(h * 0.30)

        collected: list[dict] = []
        seen: set[str] = set()
        barren = 0
        swipes = 0
        spec: dict = {}

        for swipes in range(max_swipes + 1):
            xml = d.dump_hierarchy()
            els = uix.parse(xml)
            fg = dev.foreground()
            pkg = fg.get("package") or ""
            state.remember(els, pkg)
            app_name = app or state.APP_FOR_PKG.get(pkg, "")
            if not app_name:
                return {"error": f"no registry for {pkg!r}",
                        "known_apps": reg.known_apps()}
            version = dev.app_version(pkg) or ""
            scr = screen or reg.detect_screen(
                app_name, version, uix.all_resource_ids(xml))
            if not scr:
                return {"error": "screen not recognised", "app": app_name,
                        "hint": "run explore_screen then learn_thread"}
            spec = _thread_spec(app_name, version, scr)
            if not spec:
                return {"error": f"no `thread` spec for {app_name}/{scr}",
                        "hint": "call learn_thread to teach this screen"}

            fresh = 0
            for c in assemble(els, spec):
                key = f"{c.get('author','')}|{c.get('text','')[:120]}"
                if not c.get("text") or key in seen:
                    continue
                seen.add(key)
                collected.append(c)
                fresh += 1

            barren = 0 if fresh else barren + 1
            if len(collected) >= max_comments or barren >= 2 or swipes == max_swipes:
                break
            dev.shell(f"input swipe {cx} {cy+dy} {cx} {cy-dy} 300")
            time.sleep(settle_s)

        if any(c.get("level") is None for c in collected):
            infer_levels_by_indent(collected)

        levels = sorted({c.get("level") for c in collected
                         if c.get("level") is not None})
        sources = {c.get("level_source") for c in collected}
        result: dict[str, Any] = {
            "collected": len(collected),
            "swipes": swipes,
            "levels_seen": levels,
            "level_source": sorted(s for s in sources if s),
            "stopped": ("max_comments" if len(collected) >= max_comments else
                        "thread_not_advancing" if barren >= 2 else "max_swipes"),
        }
        if "indent" in sources:
            result["level_warning"] = (
                "Some depths were inferred from x-indent, which is unreliable - "
                "distinct depths can share an x. Treat those as approximate."
            )

        # A jump of more than one level means intermediate comments were never
        # seen - scrolled past, or collapsed behind a "more replies" control.
        # nest() attaches such a node to the nearest shallower ancestor, which
        # yields a plausible but WRONG parent. Say so rather than let a clean
        # looking tree imply completeness.
        gaps = []
        prev_lvl = None
        for c in collected[:max_comments]:
            lvl = c.get("level")
            if lvl is not None and prev_lvl is not None and lvl - prev_lvl > 1:
                gaps.append({"after_author": c.get("author"),
                             "from_level": prev_lvl, "to_level": lvl})
            if lvl is not None:
                prev_lvl = lvl
        if gaps:
            result["nesting_gaps"] = gaps[:10]
            result["nesting_warning"] = (
                f"{len(gaps)} level jump(s) > 1: intermediate comments were not "
                f"captured, so those replies are attached to the nearest "
                f"shallower ancestor and their true parent is unknown. Causes: "
                f"collapsed 'more replies' controls (not yet auto-expanded), or "
                f"scrolling past rows before they rendered."
            )
        cleaned = [{k: v for k, v in c.items()
                    if not k.startswith("_") and k != "x"}
                   for c in collected[:max_comments]]
        result["comments"] = nest(collected[:max_comments]) if nested else cleaned
        return result

    @mcp.tool(
        description=(
            "Teach the registry how a comment screen is structured, so "
            "collect_thread works on it. Give the anchors for the comment "
            "header and body, and a regex whose first group is the nesting "
            "level if the app declares one."
        )
    )
    def learn_thread(app: str, screen: str, header_anchor: str,
                     body_anchor: str, footer_anchor: str = "",
                     ad_anchor: str = "", composer_anchor: str = "",
                     level_pattern: str = "",
                     header_composite: Optional[dict] = None,
                     requires: Optional[list[str]] = None) -> dict:
        d = dev.u2()
        xml = d.dump_hierarchy()
        fg = dev.foreground()
        pkg = fg.get("package") or ""
        version = dev.app_version(pkg) or "unknown"
        all_ids = sorted(uix.all_resource_ids(xml))

        thread = {
            "header_anchor": header_anchor,
            "body_anchor": body_anchor,
            "footer_anchor": footer_anchor or None,
            "ad_anchor": ad_anchor or None,
            "composer_anchor": composer_anchor or None,
            "level_pattern": level_pattern or None,
            "header_composite": header_composite or {},
        }
        screens = {screen: {
            "requires": list(requires or []),
            "fields": {},
            "thread": {k: v for k, v in thread.items() if v not in (None, {})},
            "readonly": True,
            "readonly_reason": (
                f"composer at {composer_anchor!r} must never be tapped"
                if composer_anchor else "comment screens are read-only"
            ),
            "learned_by": "agent",
        }}
        path = reg.record_baseline(app, version, screen, all_ids, screens=screens)

        els = uix.parse(xml)
        preview = assemble(els, thread)
        return {
            "learned": {"app": app, "version": version, "screen": screen},
            "registry": path,
            "preview_count": len(preview),
            "preview": [
                {"level": c.get("level"), "author": c.get("author"),
                 "text": c.get("text", "")[:80]}
                for c in preview[:5]
            ],
            "verify_next": "call collect_thread() to walk the whole thread",
        }
