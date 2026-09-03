"""Generic app learning: let an agent map an UNKNOWN app at runtime.

Hand-authoring a selector JSON per app does not scale and makes whoever writes
it the bottleneck. These tools let an agent discover an app's structure itself,
propose a field map, and persist it - turning the registry from something a
human writes into something the agent maintains.

The core insight for feeds and lists: on a screen showing content, the anchors
whose VALUES CHANGE when you scroll are the content fields; the ones that stay
put are chrome. `diff_after_action` exploits exactly that, so content fields can
be identified without knowing anything about the app in advance.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Optional

from .. import device as dev
from .. import state
from .. import ui as uix
from ..selectors import registry as reg

# Anchors that are framework chrome on every Android app.
_CHROME = (
    "action_bar_root", "content", "container", "decor", "navigationBar",
    "statusBar", "status_bar", "toolbar", "tab_bar", "bottom_navigation",
)


def _is_chrome(anchor: str) -> bool:
    a = anchor.lower()
    return any(a.startswith(c) or a == c for c in _CHROME)


def _snapshot() -> tuple[list, dict[str, list[str]], set[str], dict]:
    d = dev.u2()
    xml = d.dump_hierarchy()
    els = uix.parse(xml)
    fg = dev.foreground()
    state.remember(els, fg.get("package") or "")
    return els, uix.values_by_anchor(els), uix.all_resource_ids(xml), fg


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Analyse an UNKNOWN screen and report its structure: which anchors "
            "hold which values, which repeat (list items), and which are "
            "interactive. Use this first when meeting an app that is not yet in "
            "the selector registry - it is the discovery step before "
            "propose_selectors."
        )
    )
    def explore_screen(max_anchors: int = 40, value_chars: int = 70) -> dict:
        els, by_anchor, all_ids, fg = _snapshot()
        pkg = fg.get("package") or ""
        version = dev.app_version(pkg) if pkg else None

        anchors = []
        for anchor, values in by_anchor.items():
            if _is_chrome(anchor):
                continue
            uniq = list(dict.fromkeys(values))
            anchors.append({
                "anchor": anchor,
                "n": len(values),
                "unique": len(uniq),
                "sample": [v[:value_chars] for v in uniq[:3]],
            })
        # Repeating anchors are the strongest signal for list/feed items.
        anchors.sort(key=lambda a: (-a["n"], a["anchor"]))

        repeating = [a for a in anchors if a["n"] > 1]
        singular = [a for a in anchors if a["n"] == 1]

        interactive = [
            e.to_dict() for e in els
            if e.clickable and (e.text or e.desc or e.rid)
        ][:25]

        scrollable = [
            {"i": e.i, "id": e.rid or e.anchor, "cls": e.cls}
            for e in els if e.scrollable
        ][:8]

        # Values with no resource-id ancestor at all: only reachable positionally.
        orphan = [
            (e.text or e.desc)[:value_chars]
            for e in els if (e.text or e.desc) and not e.anchor
        ][:10]

        return {
            "package": pkg,
            "activity": fg.get("activity"),
            "app_version": version,
            "signature": uix.screen_signature(els),
            "total_elements": len(els),
            "known_app": state.APP_FOR_PKG.get(pkg),
            "repeating_anchors": repeating[:max_anchors],
            "singular_anchors": singular[:max_anchors],
            "scrollable_containers": scrollable,
            "interactive_sample": interactive,
            "values_without_anchor": orphan,
            "next_step": (
                "For a feed, call diff_after_action(action='swipe_up') to see "
                "which anchors change - those are the content fields. Then "
                "propose_selectors and learn_screen."
            ),
        }

    @mcp.tool(
        description=(
            "Snapshot the screen, perform an action, snapshot again, and report "
            "which anchors CHANGED value. On a feed this identifies the content "
            "fields automatically: what changes when you scroll is content, what "
            "stays is chrome. action: swipe_up|swipe_down|swipe_left|swipe_right|"
            "none."
        )
    )
    def diff_after_action(action: str = "swipe_up",
                          settle_s: float = 2.0) -> dict:
        _, before, _, fg = _snapshot()
        moves = {
            "swipe_up": "up", "swipe_down": "down",
            "swipe_left": "left", "swipe_right": "right",
        }
        if action != "none":
            if action not in moves:
                return {"error": f"bad action {action!r}",
                        "valid": list(moves) + ["none"]}
            info = dev.device_info()
            try:
                w, h = (int(v) for v in info.screen.lower().split("x"))
            except Exception:
                w, h = 1080, 2400
            cx, cy = w // 2, h // 2
            dy, dx = int(h * 0.32), int(w * 0.35)
            vec = {
                "up": (cx, cy + dy, cx, cy - dy),
                "down": (cx, cy - dy, cx, cy + dy),
                "left": (cx + dx, cy, cx - dx, cy),
                "right": (cx - dx, cy, cx + dx, cy),
            }[moves[action]]
            dev.shell(f"input swipe {vec[0]} {vec[1]} {vec[2]} {vec[3]} 300")
            time.sleep(max(0.0, settle_s))

        _, after, _, _ = _snapshot()

        changed, stable, appeared, vanished = [], [], [], []
        for anchor in sorted(set(before) | set(after)):
            if _is_chrome(anchor):
                continue
            b, a = before.get(anchor), after.get(anchor)
            if b is None:
                appeared.append(anchor)
            elif a is None:
                vanished.append(anchor)
            elif b != a:
                changed.append({
                    "anchor": anchor,
                    "before": b[0][:60] if b else None,
                    "after": a[0][:60] if a else None,
                })
            else:
                stable.append(anchor)

        return {
            "action": action,
            "package": fg.get("package"),
            "content_fields": changed,
            "content_field_note": (
                "These anchors changed value - they hold per-item content and "
                "are your extraction candidates."
            ),
            "static_anchors": stable[:30],
            "appeared": appeared[:20],
            "vanished": vanished[:20],
        }

    @mcp.tool(
        description=(
            "Draft a selector spec for the current screen from what is visible. "
            "Returns a candidate field map you can review and pass to "
            "learn_screen. Anchors seen to change across a diff should be marked "
            "as content; pass them via `content_anchors` to prioritise them."
        )
    )
    def propose_selectors(app: str = "", screen: str = "",
                          content_anchors: Optional[list[str]] = None) -> dict:
        els, by_anchor, all_ids, fg = _snapshot()
        pkg = fg.get("package") or ""
        app_name = app or state.APP_FOR_PKG.get(pkg) or pkg.split(".")[-1]
        version = dev.app_version(pkg) or "unknown"

        prefer_by_anchor: dict[str, str] = {}
        for e in els:
            if not e.anchor:
                continue
            if e.anchor not in prefer_by_anchor:
                prefer_by_anchor[e.anchor] = "text" if e.text else "desc"

        wanted = set(content_anchors or [])
        fields: dict[str, Any] = {}
        for anchor, values in by_anchor.items():
            if _is_chrome(anchor):
                continue
            if wanted and anchor not in wanted:
                continue
            uniq = list(dict.fromkeys(values))
            sample = uniq[0] if uniq else ""
            spec: dict[str, Any] = {
                "anchor": anchor,
                "prefer": prefer_by_anchor.get(anchor, "any"),
                "sample": sample[:100],
            }
            if reg.parse_number(sample) is not None and len(sample) < 60:
                spec["type"] = "number"
            if len(values) > 1:
                spec["repeating"] = True
                spec["note"] = (f"appears {len(values)}x - a list item field; "
                                f"extract per-row, not as a single value")
            fields[anchor] = spec

        # `requires` should be structural, not content-bearing.
        structural = [
            e.rid for e in els
            if e.rid and e.scrollable and not _is_chrome(e.rid)
        ]
        return {
            "app": app_name,
            "app_version": version,
            "screen": screen or "UNNAMED - pass a name to learn_screen",
            "proposed_requires": sorted(set(structural))[:5],
            "proposed_fields": fields,
            "field_count": len(fields),
            "review_note": (
                "Field names default to the anchor id. Rename them to something "
                "meaningful before calling learn_screen. Verify each `sample` "
                "actually matches what you expect on screen - a plausible-looking "
                "anchor whose value comes from elsewhere is the classic trap."
            ),
        }

    @mcp.tool(
        description=(
            "Scroll a feed and collect items, deduplicated. Works on any screen "
            "whose registry entry has a `repeating` field, so it is not tied to "
            "one app. Stops on max_items, max_swipes, or when scrolling stops "
            "yielding anything new."
        )
    )
    def collect_feed(field: str = "post", max_items: int = 20,
                     max_swipes: int = 25, settle_s: float = 1.6,
                     app: str = "", screen: str = "") -> dict:
        d = dev.u2()
        info = dev.device_info()
        try:
            w, h = (int(v) for v in info.screen.lower().split("x"))
        except Exception:
            w, h = 1080, 2400
        cx, cy, dy = w // 2, h // 2, int(h * 0.32)

        items: list[dict] = []
        seen: set[str] = set()
        barren = 0
        swipes = 0

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
                        "collected": len(items), "items": items}

            rows = reg.extract_repeating(app_name, version, scr, field, els)
            fresh = 0
            for r in rows:
                key = r.get("_raw", "")
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append(r)
                fresh += 1
            # Two barren scrolls in a row means the feed is not advancing -
            # end of list, a stall, or a load failure. Stop rather than spin.
            barren = 0 if fresh else barren + 1
            if len(items) >= max_items or barren >= 2 or swipes == max_swipes:
                break
            dev.shell(f"input swipe {cx} {cy+dy} {cx} {cy-dy} 300")
            time.sleep(settle_s)

        return {
            "collected": len(items),
            "swipes": swipes,
            "field": field,
            "stopped": ("max_items" if len(items) >= max_items else
                        "feed_not_advancing" if barren >= 2 else "max_swipes"),
            "items": items[:max_items],
        }

    @mcp.tool(
        description=(
            "Persist an agent-authored screen definition into the selector "
            "registry, pinned to the current app version. This is how the agent "
            "teaches itself a new app: explore -> diff -> propose -> learn."
        )
    )
    def learn_screen(app: str, screen: str, fields: dict,
                     requires: Optional[list[str]] = None,
                     note: str = "") -> dict:
        _, _, all_ids, fg = _snapshot()
        pkg = fg.get("package") or ""
        version = dev.app_version(pkg) or "unknown"

        # `composite` MUST be in this whitelist: without it, composite patterns
        # are silently dropped and extraction degrades to returning the raw
        # label with no sub-fields - which looks like it worked.
        allowed = ("anchor", "prefer", "type", "optional", "repeating",
                   "composite", "note", "sample", "availability")
        clean: dict[str, Any] = {}
        for name, spec in (fields or {}).items():
            if isinstance(spec, str):
                spec = {"anchor": spec}
            if not isinstance(spec, dict) or "anchor" not in spec:
                return {"error": f"field {name!r} needs an 'anchor'",
                        "got": spec}
            dropped = [k for k in spec if k not in allowed]
            clean[name] = {k: v for k, v in spec.items() if k in allowed}
            if dropped:
                clean[name].setdefault("note", "")
                clean[name]["_dropped_keys"] = dropped

        screens = {screen: {
            "requires": list(requires or []),
            "fields": clean,
            "learned_by": "agent",
            "note": note or "discovered at runtime via explore/diff/propose",
        }}
        path = reg.record_baseline(app, version, screen, sorted(all_ids),
                                   screens=screens)

        # Make the app resolvable by package on future dumps.
        added_mapping = bool(pkg and pkg not in state.APP_FOR_PKG)
        if added_mapping:
            state.APP_FOR_PKG[pkg] = app

        out = {
            "learned": {"app": app, "version": version, "screen": screen,
                        "fields": len(clean), "ids": len(all_ids),
                        "composite_fields": [n for n, s in clean.items()
                                             if s.get("composite")]},
            "registry": path,
            "verify_next": "call extract_fields() to confirm it resolves",
        }
        if added_mapping:
            out["persistence_note"] = (
                f"package->app mapping for {pkg} is in-memory for this session "
                f"only; add it to state.APP_FOR_PKG to persist across restarts"
            )
        return out
