"""Instagram profile scraping: header, posts, reels.

Selectors verified on Instagram 440.1.0.46.86 / RMX3395 by walking the live
hierarchy.

Three findings that shape this module:

* The profile GRID self-describes item type. A reel reads "Reel by X at row 1,
  column 3"; a post reads "2 photos and 5 videos by X at row 1, column 1". So
  posts and reels are separable with no taps at all.
* The REELS grid carries view counts directly - "Reel by X. View count 3,891."
  View-tier sampling therefore needs nothing opened to rank; read the grid, sort
  in code. There is no sort control on another user's Reels tab, and none is
  needed.
* Opening one grid post lands on a SCROLLING FEED of that user's posts, not a
  single post. Posts are collected by scrolling that feed, which is far cheaper
  than returning to the grid between each one.

Post URLs are NOT in the tree (0 occurrences of instagram.com). Instagram Lite
was tested as an alternative source and is worse - it renders as an opaque
`main_layout` with no semantic ids at all. Links therefore cost a
share-sheet round trip and are opt-in.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from ... import device as dev
from ... import state
from ... import ui as uix

IG_PKG = "com.instagram.android"

_NUM = re.compile(r"([\d,]+(?:\.\d+)?)\s*([KMB])?", re.I)
_DATE = re.compile(
    r"^(?:\d{1,2}\s+\w+|\w+\s+\d{1,2}|\d+\s+\w+\s+ago|yesterday|today)",
    re.I)


def parse_count(raw: str) -> Optional[int]:
    """Parse '1,376', '11.1K', '560K', '2.3M' into an int."""
    if not raw:
        return None
    m = _NUM.search(raw.strip())
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    if m.group(2):
        val *= mult.get(m.group(2).upper(), 1)
    return int(round(val))


def _dump():
    d = dev.u2()
    xml = d.dump_hierarchy()
    els = uix.parse(xml)
    state.remember(els, IG_PKG)
    return els


def _first(els, anchor, prefer="any"):
    return uix.first_value(els, anchor, prefer=prefer)



def _current_handle(els=None) -> Optional[str]:
    els = els if els is not None else _dump()
    return _first(els, "action_bar_title", "text")


def _wrong_profile(expect: str) -> Optional[dict]:
    """Guard against silently scraping the wrong account.

    A failed sub-step can leave the app on a PREVIOUS profile still in the back
    stack; without this check the data is captured and mislabelled, which is
    worse than an error. Returns an error dict when the on-screen handle does
    not match, else None.
    """
    if not expect:
        return None
    seen = _current_handle()
    if seen and seen.strip().lower() == expect.strip().lower():
        return None
    return {"error": "wrong profile on screen - refusing to scrape",
            "expected": expect, "on_screen": seen,
            "fix": "re-open the profile with ig_open_profile before this step"}

def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Open an Instagram profile by handle using a deep link, which skips "
            "the search flow. Returns how long navigation took."
        )
    )
    def ig_open_profile(handle: str, settle_s: float = 5.0) -> dict:
        h = handle.lstrip("@").strip()
        t0 = time.time()
        dev.shell("am start -a android.intent.action.VIEW "
                  f"-d 'instagram://user?username={h}'")
        time.sleep(max(0.0, settle_s))
        els = _dump()
        title = _first(els, "action_bar_title", "text")
        ok = bool(title and title.lower() == h.lower())
        return {"handle": h, "opened": ok, "action_bar_title": title,
                "seconds": round(time.time() - t0, 2),
                "hint": None if ok else
                        "title mismatch - profile may be private, renamed, or "
                        "still loading; raise settle_s"}

    @mcp.tool(
        description=(
            "Scrape the profile header: display name, followers, following, "
            "post count, bio, verified and follow state. Numbers are returned "
            "as integers alongside their raw strings."
        )
    )
    def ig_profile_stats() -> dict:
        t0 = time.time()
        els = _dump()
        handle = _first(els, "action_bar_title", "text")
        if not handle:
            return {"error": "not on a profile screen",
                    "foreground": dev.foreground()}

        def val(anchor):
            return _first(els, anchor, "text")

        posts_raw = val("profile_header_familiar_post_count_value")
        foll_raw = val("profile_header_familiar_followers_value")
        fing_raw = val("profile_header_familiar_following_value")

        # Bio lives on profile_user_info_compose_view; several children may
        # carry fragments, so take the longest rather than the first.
        bio_parts = [v for v in
                     uix.values_by_anchor(els).get("profile_user_info_compose_view", [])
                     if v]
        bio = max(bio_parts, key=len) if bio_parts else None

        # The verified badge NODE exists whether or not the account is verified,
        # so presence alone is a false positive. Require non-zero bounds, i.e.
        # the badge is actually laid out on screen.
        verified = any(
            e.rid == "action_bar_title_verified_badge"
            and (e.bounds[2] - e.bounds[0]) > 0 and (e.bounds[3] - e.bounds[1]) > 0
            for e in els
        )
        # A PRIVATE account renders an explicit notice and no grid. Detect it
        # so an empty result is reported as "private", not as a scrape failure -
        # the two need very different follow-up.
        notice = " ".join(
            v for a, vals in uix.values_by_anchor(els).items()
            if a.startswith("row_profile_header_empty_profile_notice")
            for v in vals
        ).lower()
        private = "private" in notice

        # The bio link renders on its own text_view, separate from the bio text.
        link = None
        for e in els:
            v = (e.text or e.desc or "").strip()
            if e.rid == "text_view" and ("." in v) and len(v) < 120 and " " not in v.strip().rstrip(" and"):
                link = v.strip().rstrip(" and").strip()
                break

        return {
            "handle": handle,
            "display_name": val("profile_header_full_name_above_vanity"),
            "private": private,
            "external_link": link,
            "posts": {"raw": posts_raw, "value": parse_count(posts_raw or "")},
            "followers": {"raw": foll_raw, "value": parse_count(foll_raw or "")},
            "following": {"raw": fing_raw, "value": parse_count(fing_raw or "")},
            "bio": bio,
            "verified": verified,
            "follow_state": _first(els, "profile_header_follow_button", "text"),
            "seconds": round(time.time() - t0, 2),
        }

    @mcp.tool(
        description=(
            "Read the profile GRID and classify every tile as post or reel "
            "without opening anything. Grid tiles self-describe: 'Reel by X at "
            "row..' vs 'N photos and M videos by X at row..'. Scrolls to gather "
            "up to max_items."
        )
    )
    def ig_scan_grid(max_items: int = 30, max_swipes: int = 10,
                     settle_s: float = 1.2) -> dict:
        t0 = time.time()
        seen: set[str] = set()
        posts: list[dict] = []
        reels: list[dict] = []
        barren = 0
        for swipe in range(max_swipes + 1):
            els = _dump()
            fresh = 0
            for e in els:
                if e.rid != "image_button":
                    continue
                desc = (e.desc or "").strip()
                if not desc or desc in seen:
                    continue
                seen.add(desc)
                fresh += 1
                rc = re.search(r"row (\d+), column (\d+)", desc)
                item = {"desc": desc,
                        "row": int(rc.group(1)) if rc else None,
                        "col": int(rc.group(2)) if rc else None}
                if desc.lower().startswith("reel by"):
                    reels.append(item)
                else:
                    m = re.match(r"(\d+)\s+photos?(?:\s+and\s+(\d+)\s+videos?)?",
                                 desc, re.I)
                    item["photos"] = int(m.group(1)) if m else None
                    item["videos"] = int(m.group(2)) if (m and m.group(2)) else 0
                    posts.append(item)
            barren = 0 if fresh else barren + 1
            if len(posts) + len(reels) >= max_items or barren >= 2:
                break
            dev.shell("input swipe 540 1600 540 900 300")
            time.sleep(settle_s)
        return {"posts": posts, "reels": reels,
                "post_count": len(posts), "reel_count": len(reels),
                "swipes": swipe, "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "Read the REELS tab grid, returning each reel's view count without "
            "opening it. This is what makes view-tier sampling cheap: rank in "
            "code rather than tapping a sort control (none exists for other "
            "users' profiles)."
        )
    )
    def ig_scan_reels_grid(max_reels: int = 20, max_swipes: int = 8,
                           settle_s: float = 1.2,
                           expect_handle: str = "") -> dict:
        t0 = time.time()
        bad = _wrong_profile(expect_handle)
        if bad:
            return bad
        els = _dump()
        tabs = [e for e in els if e.rid == "profile_tab_icon_view"
                and (e.desc or "").strip().lower() == "reels"]
        if tabs:
            x, y = tabs[0].center
            dev.shell(f"input tap {x} {y}")
            time.sleep(2.5)

        seen: set[str] = set()
        reels: list[dict] = []
        barren = 0
        for swipe in range(max_swipes + 1):
            els = _dump()
            fresh = 0
            for e in els:
                if e.rid != "preview_clip_thumbnail":
                    continue
                desc = (e.desc or "").strip()
                if not desc or desc in seen:
                    continue
                seen.add(desc)
                fresh += 1
                vm = re.search(r"View count ([\d.,]+\s*[KMB]?)", desc, re.I)
                reels.append({
                    "grid_index": len(reels),      # grid order == newest first
                    "views_raw": vm.group(1).strip() if vm else None,
                    "views": parse_count(vm.group(1)) if vm else None,
                    "desc": desc,
                })
            barren = 0 if fresh else barren + 1
            if len(reels) >= max_reels or barren >= 2:
                break
            dev.shell("input swipe 540 1600 540 900 300")
            time.sleep(settle_s)

        sample = reels[:max_reels]
        ranked = sorted([r for r in sample if r["views"] is not None],
                        key=lambda r: -r["views"])
        return {
            "collected": len(sample),
            "swipes": swipe,
            "reels": sample,
            "ranked_by_views": [r["grid_index"] for r in ranked],
            "seconds": round(time.time() - t0, 2),
            "note": ("grid order is newest-first, so grid_index 0..n are the "
                     "latest; ranked_by_views gives the view ordering"),
        }

    @mcp.tool(
        description=(
            "Collect posts from the post FEED. Opens the first grid post once, "
            "then scrolls the resulting feed - far cheaper than returning to the "
            "grid per post. Captures type, location, audio, likes, comments, "
            "reposts, carousel size, caption and date."
        )
    )
    def ig_collect_posts(max_posts: int = 12, max_swipes: int = 20,
                         settle_s: float = 1.4,
                         open_first: bool = True) -> dict:
        t0 = time.time()
        if open_first:
            # ig_scan_grid leaves the grid SCROLLED, so a profile with few posts
            # can have its only post tile off-screen by now. Scroll back up
            # before looking, or collect_posts silently returns 0.
            for _ in range(4):
                dev.shell("input swipe 540 800 540 1700 200")
                time.sleep(0.35)
            time.sleep(0.8)
            els = _dump()
            tiles = [e for e in els if e.rid == "image_button"
                     and not (e.desc or "").lower().startswith("reel by")]
            if not tiles:
                return {"error": "no non-reel grid tiles found; is the Grid "
                                 "View tab selected?",
                        "opened_feed": False, "collected": 0, "posts": [],
                        "seconds": round(time.time() - t0, 2)}
            x, y = tiles[0].center
            dev.shell(f"input tap {x} {y}")
            time.sleep(3.5)

        # MERGE, do not append. A post scrolling into view is captured before
        # its caption renders, so the same post is seen twice - once partial,
        # once complete. Appending produced visible duplicates where one copy
        # had caption=None. Key on engagement+media, which are stable from the
        # first frame, and fill in fields as later passes reveal them.
        merged: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        for swipe in range(max_swipes + 1):
            els = _dump()
            fresh = 0
            for p in _assemble_posts(els):
                key = _post_key(p)
                if not key:
                    continue
                if key not in merged:
                    merged[key] = p
                    order.append(key)
                    fresh += 1
                else:
                    for k, v in p.items():
                        if v is not None and merged[key].get(k) is None:
                            merged[key][k] = v
                            fresh += 1
            barren = 0 if fresh else barren + 1
            if len(merged) >= max_posts or barren >= 2:
                break
            dev.shell("input swipe 540 1700 540 700 320")
            time.sleep(settle_s)
        posts = [merged[k] for k in order][:max_posts]
        return {"collected": len(posts), "swipes": swipe,
                "opened_feed": bool(open_first),
                "posts": posts,
                "seconds": round(time.time() - t0, 2)}


def _post_key(p: dict) -> str:
    """Identity from fields present in the FIRST frame a post renders.

    Caption arrives late, so it cannot be part of the key.
    """
    bits = [p.get("likes"), p.get("comments"), p.get("media_count"),
            (p.get("location") or "")[:24]]
    if all(b in (None, "") for b in bits):
        return ""
    return "|".join(str(b) for b in bits)


# Header shape: "user posted a carousel in <LOCATION> on <DATE>"
_HEADER = re.compile(
    r"^(\S+)\s+posted\s+an?\s+(\w+)"
    r"(?:\s+in\s+(?P<loc>.+?))?"
    r"(?:\s+on\s+(?P<date>\d{1,2}\s+\w+|\w+\s+\d{1,2}))?"
    r"[.,]?$"
)


def _assemble_posts(els) -> list[dict]:
    """Group feed elements into posts, split on row_feed_profile_header."""
    out: list[dict] = []
    cur: Optional[dict] = None
    misc: list[str] = []

    def flush():
        nonlocal cur, misc
        if cur is None:
            return
        # `list` holds caption AND date with no distinguishing id; the date is
        # short and matches a date shape, the caption is the longest fragment.
        dates = [m for m in misc if _DATE.match(m.strip()) and len(m) < 30]
        caps = [m for m in misc if m not in dates and len(m) > 12]
        if dates and not cur.get("date"):
            cur["date"] = dates[-1]
        if caps:
            cur["caption"] = max(caps, key=len)
        out.append(cur)
        cur, misc = None, []

    for e in els:
        anchor = e.anchor or e.rid
        val = (e.text or e.desc or "").strip()
        if not val:
            continue
        if anchor == "row_feed_profile_header" and " posted " in val:
            flush()
            cur = {"header": val}
            m = _HEADER.match(val)
            if m:
                cur["username"] = m.group(1)
                cur["type"] = m.group(2)
                # Location and date arrive fused as "<LOC> on <DATE>"; splitting
                # them here also yields the date without waiting for the caption
                # region to render.
                if m.group("loc"):
                    cur["location"] = m.group("loc").strip()
                if m.group("date"):
                    cur["date"] = m.group("date").strip()
            continue
        if cur is None:
            continue
        if anchor == "secondary_label":
            # secondary_label carries the AUDIO track for some posts and the
            # LOCATION for others. If it merely repeats the location already
            # parsed from the header, it is not audio.
            loc = (cur.get("location") or "").lower()
            if loc and (val.lower() == loc or val.lower() in loc
                        or loc.startswith(val.lower())):
                cur.setdefault("location", val)
            else:
                cur["audio"] = val
        elif anchor == "carousel_video_media_group":
            cur["media_desc"] = val
            lm = re.search(r"([\d,]+)\s+likes?", val)
            cm = re.search(r"([\d,]+)\s+comments?", val)
            tm = re.search(r"(?:Photo|Video)\s+\d+\s+of\s+(\d+)", val, re.I)
            if lm:
                cur["likes"] = parse_count(lm.group(1))
            if cm:
                cur["comments"] = parse_count(cm.group(1))
            if tm:
                cur["media_count"] = int(tm.group(1))
        elif anchor == "carousel_index_indicator_text_view":
            cur["carousel"] = val
        elif anchor == "reposts_ufi_icon" and val.replace(",", "").isdigit():
            cur["reposts"] = parse_count(val)
        elif anchor == "list":
            misc.append(val)
    flush()
    return out


def _sample_indices(reels: list[dict], latest: int = 5, top: int = 4,
                    mid: int = 3, low: int = 2) -> dict:
    """Choose which reels to open, per the sampling spec.

    Grid order is newest-first, so `latest` is simply the first N. The view
    tiers come from ranking the SAME sample - no sort control is needed, and
    none exists on another user's Reels tab.
    """
    idx_latest = [r["grid_index"] for r in reels[:latest]]
    ranked = sorted([r for r in reels if r.get("views") is not None],
                    key=lambda r: -r["views"])
    n = len(ranked)
    idx_top = [r["grid_index"] for r in ranked[:top]]
    mid_start = max(0, n // 2 - mid // 2)
    idx_mid = [r["grid_index"] for r in ranked[mid_start:mid_start + mid]]
    idx_low = [r["grid_index"] for r in ranked[-low:]] if n >= low else []
    order, seen = [], set()
    tiers = {}
    for tier, group in (("latest", idx_latest), ("top", idx_top),
                        ("mid", idx_mid), ("low", idx_low)):
        tiers[tier] = group
        for i in group:
            if i not in seen:
                seen.add(i)
                order.append(i)
    return {"tiers": tiers, "open_order": sorted(order),
            "unique_to_open": len(order)}


def register_orchestrator(mcp) -> None:

    @mcp.tool(
        description=(
            "Plan which reels to open from a reels-grid scan, per the sampling "
            "spec: N latest plus top/mid/low view tiers drawn from the same "
            "sample. Returns deduplicated grid indices - overlaps between "
            "'latest' and 'top' are opened once."
        )
    )
    def ig_plan_reel_sample(reels: list, latest: int = 5, top: int = 4,
                            mid: int = 3, low: int = 2) -> dict:
        return _sample_indices(reels, latest, top, mid, low)

    @mcp.tool(
        description=(
            "Open reels from the grid and extract each one's detail, swiping "
            "through in grid order and capturing only the requested indices. "
            "Reuses the reels_viewer selectors already in the registry."
        )
    )
    def ig_collect_reel_details(indices: list, poll_s: float = 0.35,
                                overlay_timeout_s: float = 2.5,
                                skip_settle_s: float = 0.45,
                                max_swipes: int = 40,
                                expect_handle: str = "") -> dict:
        from ...selectors import registry as reg
        t0 = time.time()
        bad = _wrong_profile(expect_handle)
        if bad:
            return bad
        want = sorted(set(int(i) for i in indices))
        if not want:
            return {"error": "no indices given"}
        version = dev.app_version(IG_PKG) or ""

        els = _dump()
        thumbs = [e for e in els if e.rid == "preview_clip_thumbnail"]
        if not thumbs:
            return {"error": "no reel thumbnails visible; run ig_scan_reels_grid "
                             "first so the Reels tab is selected"}
        x, y = thumbs[0].center
        dev.shell(f"input tap {x} {y}")

        def extract_when_ready() -> dict:
            """Poll until the engagement overlay renders, then extract.

            Replaces a blind 2s sleep with a wait that ends the instant counts
            appear - usually well under 1s. Also rides out the known first-reel
            overlay-missing bug by continuing to poll instead of accepting nulls.
            """
            deadline = time.time() + overlay_timeout_s
            last = {}
            while time.time() < deadline:
                els = _dump()
                f = reg.extract_fields("instagram", version, "reels_viewer", els)
                fields = f.get("fields", f)
                if fields.get("like_count") is not None:
                    return f
                last = f
                time.sleep(poll_s)
            return last            # timed out: return whatever we have

        out: list[dict] = []
        pos = 0
        for _ in range(max_swipes + 1):
            if pos in want:
                f = extract_when_ready()
                f["grid_index"] = pos
                out.append(f)
            if pos >= max(want):
                break
            # Reels between targets are not extracted, so only pause long enough
            # for the swipe to register - the big saving over a full settle.
            dev.shell("input swipe 540 1700 540 500 250")
            time.sleep(skip_settle_s)
            pos += 1

        dev.shell("input keyevent KEYCODE_BACK")
        time.sleep(0.8)
        return {"requested": want, "collected": len(out), "reels": out,
                "seconds": round(time.time() - t0, 2)}


def register_about(mcp) -> None:

    # Menu rows inside the "About this reel" sheet. NONE of these may ever be
    # tapped: Interested / Not Interested write feed preferences, Report files a
    # report, Save bookmarks the post. A blind tap here previously set
    # "We'll suggest more posts like this for 30 days" - an unintended write.
    _NEVER_TAP = {"interested", "not interested", "report", "save",
                  "playback", "why you're seeing this post", "ask meta ai"}

    @mcp.tool(
        description=(
            "Read Instagram's own AI-generated 'About this reel' description - "
            "Meta's content understanding of the reel, a strong metadata signal. "
            "Opens the More sheet, waits for the text to generate, reads it, and "
            "closes with BACK.\n"
            "STRICTLY READ-ONLY: it never taps any sheet row. The sheet contains "
            "Interested / Not Interested / Report / Save, all of which write to "
            "your account."
        )
    )
    def ig_about_reel(wait_s: float = 8.0, poll_s: float = 0.7,
                      close_after: bool = True) -> dict:
        t0 = time.time()
        els = _dump()
        more = [e for e in els if e.rid == "clips_ufi_more_button_component"]
        if not more:
            return {"error": "More button not found - is a reel open?",
                    "foreground": dev.foreground()}
        x, y = more[0].center
        dev.shell(f"input tap {x} {y}")

        # Poll until the sheet exists AND the AI text has been generated: the
        # description arrives a beat after the sheet renders, so dumping once
        # returns the header with no body.
        desc = None
        options: list[str] = []
        deadline = time.time() + wait_s
        opened = False
        while time.time() < deadline:
            time.sleep(poll_s)
            els = _dump()
            vals = uix.values_by_anchor(els)
            sheet = vals.get("recycler_view", []) or []
            if any("about this reel" in v.lower() for v in sheet):
                opened = True
                body = [v for v in sheet
                        if "about this reel" not in v.lower() and len(v) > 40]
                if body:
                    desc = max(body, key=len)
                    options = [v for v in vals.get("control_option_text", []) if v]
                    break
        if not opened:
            return {"error": "About sheet did not open",
                    "hint": "the More control's touch target is unreliable; "
                            "retry, or open it by hand",
                    "seconds": round(time.time() - t0, 2)}

        # Re-read options even if the description timed out.
        if not options:
            options = [v for v in uix.values_by_anchor(_dump())
                       .get("control_option_text", []) if v]

        if close_after:
            dev.shell("input keyevent KEYCODE_BACK")
            time.sleep(0.8)

        return {
            "about_this_reel": desc,
            "generated": bool(desc),
            "menu_options": options,
            "seconds": round(time.time() - t0, 2),
            "source": "meta_ai_generated",
            "safety": "read-only; no sheet row was tapped",
            "note": None if desc else
                    "sheet opened but the AI text had not generated in time - "
                    "raise wait_s",
        }
