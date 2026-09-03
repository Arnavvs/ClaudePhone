"""X / Twitter: timeline, keyword search, tweet detail and nested replies.

Verified on com.twitter.android 12.21.1-prod.05.

X's tree is much FLATTER than Instagram's or Reddit's: almost everything anchors
to the scroll container `scaffold_home_tabbed`, and a tweet is a run of sibling
values in document order rather than one composite label or a set of named
fields. The only per-tweet landmark is the `timeline_post` header group:

    timeline_post  "MIA.."          <- display name
    timeline_post  "Verified"       <- optional
    timeline_post  "@IamMiaHq"      <- handle  (the reliable boundary)
    timeline_post  "- 19h"          <- age
    ...            "Hey #grok, ..." <- tweet text
    ...            "Image"/"Video"  <- media type
    ...            "Reply" "15"     <- metric label then value
    ...            "Repost" "187"
    ...            "Like" "4.6K"
    ...            "Impressions" "355K"

So tweets are split on the handle and metrics are read as label->next-value
pairs. Worth noting X exposes IMPRESSIONS (view count) and full URLs in tweet
text - neither of which Instagram gives for posts.

Read-only: the composer is never touched, and nothing is liked, reposted or
replied to.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from ... import device as dev
from ... import state
from ... import ui as uix

X_PKG = "com.twitter.android"

# Chrome / control labels that are never tweet content.
_CONTROL = {
    "post options", "explain this post with grok", "unmute", "mute",
    "reply", "repost", "like", "impressions", "bookmark", "share",
    "verified", "verified business", "ad", "video", "image", "gif",
    "show navigation drawer", "scroll to top", "find people to follow",
    "for you", "following", "add", "post", "home", "explore", "grok",
    "notifications tab", "messages", "translate post", "show more",
    "quote", "views", "subscribe",
}
_METRICS = {"reply": "replies", "repost": "reposts", "like": "likes",
            "impressions": "impressions", "bookmark": "bookmarks"}
_AGE = re.compile(r"^[·.\-]\s*(\d+[smhdwy]|\w+\s+\d+)$")

# Rows that look like body text but are NOT: social proof, follow prompts and
# similar chrome. Without this, "Profile images for A, B, C" was captured as the
# text of nearly every tweet - it renders once but sits adjacent to many headers.
_NOT_BODY = re.compile(
    r"^(profile images? for|liked by|reposted by|followed by|"
    r"trending in|promoted by|who to follow|new to x|"
    r"see new posts|show more replies|show this thread|"
    # account and translation badges. These sit between the header and the
    # body, so without them a snapshot records "Parody account" as the text of
    # every parody post and "Translated from X" for every translated one -
    # which quietly poisons any topic analysis run over the capture.
    r"translated from|show original|automated by|subscribe to|"
    # the badge family is open-ended - parody, commentary, fan, business and
    # more all render as "<word> account" on its own row. Match the shape, not
    # a list of words, or the next variant silently becomes tweet text again.
    r"\w+ account$)", re.I)
_NUM = re.compile(r"^([\d,]+(?:\.\d+)?)\s*([KMB])?$", re.I)
_URL = re.compile(r"\b((?:https?://)?[\w.-]+\.[a-z]{2,}(?:/[^\s]*)?)", re.I)


def parse_metric(raw: str) -> Optional[int]:
    m = _NUM.match((raw or "").strip())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2):
        v *= {"K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]
    return int(round(v))


def _stream(els) -> list[tuple[str, str]]:
    """(anchor, value) in document order, chrome rows removed."""
    out = []
    for e in els:
        v = (e.text or e.desc or "").strip()
        if not v:
            continue
        anc = e.anchor or e.rid or ""
        if anc in ("statusIcons", "mobile_combo", "mobile_signal",
                   "notificationIcons", "MainLanding"):
            continue
        out.append((anc, v))
    return out


def assemble_tweets(els) -> list[dict]:
    """Split the document-order stream into tweets on the @handle landmark."""
    seq = _stream(els)
    tweets: list[dict] = []
    cur: Optional[dict] = None
    i = 0
    while i < len(seq):
        anc, val = seq[i]
        low = val.lower()

        if anc == "timeline_post" and val.startswith("@"):
            # New tweet. The display name and Verified flag precede the handle.
            cur = {"handle": val, "display_name": None, "verified": False,
                   "age": None, "text": None, "media": None, "is_ad": False,
                   "links": [], "metrics": {}}
            for j in range(max(0, i - 3), i):
                a2, v2 = seq[j]
                if a2 != "timeline_post":
                    continue
                if v2.lower().startswith("verified"):
                    cur["verified"] = True
                elif not v2.startswith("@") and not _AGE.match(v2):
                    cur["display_name"] = v2
            tweets.append(cur)
            i += 1
            continue

        if cur is not None:
            if _AGE.match(val):
                cur["age"] = val.lstrip("·.- ").strip()
            elif low == "ad":
                cur["is_ad"] = True
            elif low in ("video", "image", "gif"):
                cur["media"] = low
            elif low in _METRICS:
                # metric label; the value is the next row
                if i + 1 < len(seq):
                    n = parse_metric(seq[i + 1][1])
                    if n is not None:
                        cur["metrics"][_METRICS[low]] = n
                        i += 2
                        continue
                cur["metrics"].setdefault(_METRICS[low], 0)
            elif low not in _CONTROL and len(val) > 2:
                # first substantial non-control run is the tweet body
                if _NOT_BODY.match(val):
                    pass                       # social proof, not tweet content
                elif cur["text"] is None:
                    cur["text"] = val
                    cur["links"] = [m.group(1) for m in _URL.finditer(val)]
        i += 1

    return [t for t in tweets if t.get("text") or t.get("metrics")]


def _dump():
    els = uix.parse(dev.u2().dump_hierarchy())
    state.remember(els, X_PKG)
    return els


def register(mcp) -> None:

    @mcp.tool(
        description="Open X/Twitter. tab: for_you | following. Returns the "
                    "active tab so you can confirm which timeline you are on."
    )
    def x_open(tab: str = "", wait_s: float = 6.0) -> dict:
        dev.shell(f"monkey -p {X_PKG} -c android.intent.category.LAUNCHER 1")
        time.sleep(wait_s)
        if tab:
            want = "For you" if tab.lower().startswith("for") else "Following"
            els = _dump()
            hits = [e for e in els
                    if (e.text or e.desc or "").strip().lower() == want.lower()]
            if hits:
                x, y = hits[0].center
                dev.shell(f"input tap {x} {y}")
                time.sleep(2.0)
        return {"opened": True, "foreground": dev.foreground(),
                "version": dev.app_version(X_PKG)}

    @mcp.tool(
        description=(
            "Collect tweets from the current timeline, scrolling and "
            "deduplicating. Captures handle, display name, verified, age, text, "
            "links, media type, ad flag, and metrics including IMPRESSIONS."
        )
    )
    def x_collect_timeline(max_tweets: int = 25, max_swipes: int = 20,
                           settle_s: float = 1.3,
                           include_ads: bool = False) -> dict:
        t0 = time.time()
        merged: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        swipe = 0
        for swipe in range(max_swipes + 1):
            els = _dump()
            fresh = 0
            for t in assemble_tweets(els):
                if t.get("is_ad") and not include_ads:
                    continue
                key = f"{t.get('handle')}|{(t.get('text') or '')[:70]}"
                if key not in merged:
                    merged[key] = t
                    order.append(key)
                    fresh += 1
                else:
                    # a tweet scrolling into view is seen before its metrics
                    # render; fill gaps instead of duplicating.
                    for k, v in t.items():
                        if k == "metrics":
                            merged[key]["metrics"].update(
                                {mk: mv for mk, mv in v.items()
                                 if mk not in merged[key]["metrics"]})
                        elif v not in (None, False, [], {}) and \
                                merged[key].get(k) in (None, False, [], {}):
                            merged[key][k] = v
                            fresh += 1
            barren = 0 if fresh else barren + 1
            if len(merged) >= max_tweets or barren >= 2:
                break
            dev.shell("input swipe 540 1700 540 800 300")
            time.sleep(settle_s)
        tweets = [merged[k] for k in order][:max_tweets]
        return {"collected": len(tweets), "swipes": swipe,
                "tweets": tweets, "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "Keyword search on X. Opens Explore, types the query, submits, and "
            "optionally switches result tab (top|latest|people|media). Returns "
            "the collected result tweets."
        )
    )
    def x_search(query: str, tab: str = "latest", max_tweets: int = 20,
                 max_swipes: int = 12) -> dict:
        t0 = time.time()
        els = _dump()
        # Explore tab
        exp = [e for e in els
               if (e.desc or e.text or "").strip().lower() == "explore"]
        if not exp:
            return {"error": "Explore tab not found; is X in the foreground?",
                    "foreground": dev.foreground()}
        x, y = exp[0].center
        dev.shell(f"input tap {x} {y}")
        time.sleep(2.5)

        els = _dump()
        box = [e for e in els if "search" in
               ((e.desc or "") + " " + (e.text or "")).lower()]
        if not box:
            return {"error": "search box not found on Explore"}
        x, y = box[0].center
        dev.shell(f"input tap {x} {y}")
        time.sleep(1.5)
        safe = query.replace("'", "'\\''").replace(" ", "%s")
        dev.shell(f"input text '{safe}'")
        time.sleep(0.8)
        dev.shell("input keyevent KEYCODE_ENTER")
        time.sleep(3.5)

        if tab:
            els = _dump()
            want = tab.strip().lower()
            hits = [e for e in els
                    if (e.text or e.desc or "").strip().lower() == want]
            if hits:
                x, y = hits[0].center
                dev.shell(f"input tap {x} {y}")
                time.sleep(2.5)

        merged: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        for _ in range(max_swipes + 1):
            fresh = 0
            for t in assemble_tweets(_dump()):
                key = f"{t.get('handle')}|{(t.get('text') or '')[:70]}"
                if key not in merged:
                    merged[key] = t
                    order.append(key)
                    fresh += 1
            barren = 0 if fresh else barren + 1
            if len(merged) >= max_tweets or barren >= 2:
                break
            dev.shell("input swipe 540 1700 540 800 300")
            time.sleep(1.3)
        tweets = [merged[k] for k in order][:max_tweets]
        return {"query": query, "tab": tab, "collected": len(tweets),
                "tweets": tweets, "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "Open the tweet currently at the top of the timeline (or index `i` "
            "from the last ui_dump) and collect its reply thread. Replies are "
            "returned in order with an inferred depth; X does not declare depth "
            "the way Reddit does, so depth is approximate and labelled as such."
        )
    )
    def x_collect_replies(i: Optional[int] = None, max_replies: int = 30,
                          max_swipes: int = 15, settle_s: float = 1.3) -> dict:
        t0 = time.time()
        if i is not None:
            els = state.last.get("elements") or []
            if 0 <= i < len(els):
                x, y = els[i].center
                dev.shell(f"input tap {x} {y}")
                time.sleep(3.0)
        else:
            els = _dump()
            posts = [e for e in els if e.anchor == "timeline_post"
                     and (e.text or "").startswith("@")]
            if not posts:
                return {"error": "no tweet found to open"}
            x, y = posts[0].center
            dev.shell(f"input tap {x} {y}")
            time.sleep(3.0)

        merged: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        for _ in range(max_swipes + 1):
            fresh = 0
            for t in assemble_tweets(_dump()):
                key = f"{t.get('handle')}|{(t.get('text') or '')[:70]}"
                if key not in merged:
                    merged[key] = t
                    order.append(key)
                    fresh += 1
            barren = 0 if fresh else barren + 1
            if len(merged) >= max_replies or barren >= 2:
                break
            dev.shell("input swipe 540 1700 540 800 300")
            time.sleep(settle_s)

        items = [merged[k] for k in order][:max_replies]
        root = items[0] if items else None
        return {
            "root": root,
            "replies": items[1:],
            "collected": len(items),
            "seconds": round(time.time() - t0, 2),
            "depth_note": (
                "X does not declare reply depth in the accessibility tree the "
                "way Reddit does ('Level N comment'), so replies are returned "
                "in display order without a reliable nesting level. Treat the "
                "order as the thread's visual order, not a parent-child tree."
            ),
        }


def register_nav(mcp) -> None:

    @mcp.tool(
        description=(
            "Jump to the top of the X timeline. Uses the 'Scroll to top' "
            "affordance if present, else taps the Home tab (which also returns "
            "to top when already on Home). Far cheaper than swiping down "
            "repeatedly, and avoids over-scrolling into a refresh."
        )
    )
    def x_scroll_to_top() -> dict:
        els = _dump()

        # 1) explicit affordance - X exposes it as an accessibility label
        for e in els:
            lab = ((e.desc or "") + " " + (e.text or "")).strip().lower()
            if lab.startswith("scroll to top"):
                x, y = e.center
                dev.shell(f"input tap {x} {y}")
                time.sleep(1.5)
                return {"method": "scroll_to_top_button", "at": [x, y]}

        # 2) Home tab: tapping it while already on Home scrolls to top.
        #    It only reappears after scrolling up slightly, so nudge first.
        dev.shell("input swipe 540 800 540 1100 200")
        time.sleep(1.0)
        els = _dump()
        for e in els:
            lab = ((e.desc or "") + " " + (e.text or "")).strip().lower()
            if lab in ("home", "home tab"):
                x, y = e.center
                dev.shell(f"input tap {x} {y}")
                time.sleep(1.5)
                return {"method": "home_tab", "at": [x, y]}

        return {"method": "none",
                "error": "neither 'Scroll to top' nor Home tab was found",
                "hint": "scroll up slightly - both only appear after a small "
                        "upward scroll"}


# The timeline tab strip sits just under the top bar. Bounded by y so the
# bottom nav ("Home"/"Explore") and the top icons are not mistaken for tabs.
_TAB_Y_MIN, _TAB_Y_MAX = 280, 430
_TAB_SKIP = {"add", "scroll to top", "show navigation drawer",
             "find people to follow", "post"}



def _ensure_home(settle_s: float = 2.0) -> bool:
    """Return to the Home timeline before touching the tab strip.

    The tab strip exists ONLY on Home. Two failure modes were hit here:
      * a previous step leaves the app on a tweet detail, where every timeline
        lookup fails with "not found" - a wrong-screen error, not a missing tab;
      * blind BACK presses walk out of X entirely and land in Instagram, which
        also has a "Home" tab, so the guard must anchor on the PACKAGE.

    X also restores its last screen on launch, so a plain relaunch can reopen a
    tweet detail. Force-stop first: it is the only way to guarantee Home.
    """
    def on_home() -> bool:
        return any(
            _TAB_Y_MIN <= e.center[1] <= _TAB_Y_MAX
            and (e.text or e.desc or "").strip().lower() in ("for you", "following")
            for e in _dump()
        )

    if dev.foreground().get("package") == X_PKG and on_home():
        return True

    # bottom-nav Home first (cheap, keeps app state)
    if dev.foreground().get("package") == X_PKG:
        home = [e for e in _dump()
                if ((e.desc or "") + (e.text or "")).strip().lower()
                in ("home", "home tab") and e.center[1] > 2000]
        if home:
            x, y = home[0].center
            dev.shell(f"input tap {x} {y}")
            time.sleep(settle_s)
            if on_home():
                return True

    # guaranteed path: cold start
    dev.shell(f"am force-stop {X_PKG}")
    time.sleep(1.0)
    dev.shell(f"monkey -p {X_PKG} -c android.intent.category.LAUNCHER 1")
    time.sleep(settle_s + 5.0)
    return on_home()


def register_timelines(mcp) -> None:

    @mcp.tool(
        description=(
            "List the timeline tabs currently available on X - 'For you', "
            "'Following', and any custom timelines/Lists the account has pinned. "
            "Use before x_switch_timeline to see what exists."
        )
    )
    def x_list_timelines() -> dict:
        els = _dump()
        tabs, seen = [], set()
        for e in els:
            lab = (e.text or e.desc or "").strip()
            if not lab or len(lab) > 28:
                continue
            cy = e.center[1]
            if not (_TAB_Y_MIN <= cy <= _TAB_Y_MAX):
                continue
            low = lab.lower()
            if low in _TAB_SKIP or low in seen:
                continue
            seen.add(low)
            tabs.append({"name": lab, "x": e.center[0], "y": cy,
                         "selected": e.selected})
        tabs.sort(key=lambda t: t["x"])
        return {"count": len(tabs), "timelines": tabs,
                "note": "custom timelines/Lists appear here alongside "
                        "'For you' and 'Following'"}

    @mcp.tool(
        description=(
            "Switch to a named timeline tab on X (e.g. 'Following', or a custom "
            "timeline such as 'Soccer'). Matches case-insensitively on the tab "
            "label. Follow with x_collect_timeline to scrape whatever is active."
        )
    )
    def x_switch_timeline(name: str, settle_s: float = 2.5) -> dict:
        want = name.strip().lower()
        if not _ensure_home():
            return {"error": "could not reach the Home timeline",
                    "foreground": dev.foreground(),
                    "hint": "the tab strip only exists on Home"}
        els = _dump()
        for e in els:
            lab = (e.text or e.desc or "").strip()
            cy = e.center[1]
            if lab.lower() == want and _TAB_Y_MIN <= cy <= _TAB_Y_MAX:
                x, y = e.center
                dev.shell(f"input tap {x} {y}")
                time.sleep(settle_s)
                return {"switched_to": lab, "at": [x, y]}
        avail = []
        for e in els:
            lab = (e.text or e.desc or "").strip()
            if lab and _TAB_Y_MIN <= e.center[1] <= _TAB_Y_MAX \
                    and lab.lower() not in _TAB_SKIP:
                avail.append(lab)
        return {"error": f"timeline {name!r} not found",
                "available": sorted(set(avail)),
                "hint": "the tab strip scrolls horizontally; swipe left/right "
                        "on it to reveal more timelines"}

    @mcp.tool(
        description=(
            "Scrape several X timelines in one pass: switch to each named "
            "timeline, collect tweets, and return them grouped by timeline. "
            "Returns to the top of each before collecting so runs are "
            "comparable."
        )
    )
    def x_collect_multi(names: list, max_tweets: int = 15,
                        max_swipes: int = 12) -> dict:
        out: dict[str, Any] = {}
        t0 = time.time()
        _ensure_home()
        for nm in names:
            sw = x_switch_timeline(nm)
            if sw.get("error"):
                out[nm] = {"error": sw["error"], "available": sw.get("available")}
                continue
            # start each timeline from the top so samples are comparable
            for e in _dump():
                lab = ((e.desc or "") + (e.text or "")).strip().lower()
                if lab.startswith("scroll to top"):
                    x, y = e.center
                    dev.shell(f"input tap {x} {y}")
                    time.sleep(1.2)
                    break
            merged: dict[str, dict] = {}
            order: list[str] = []
            barren = 0
            for _ in range(max_swipes + 1):
                fresh = 0
                for t in assemble_tweets(_dump()):
                    if t.get("is_ad"):
                        continue
                    key = f"{t.get('handle')}|{(t.get('text') or '')[:70]}"
                    if key not in merged:
                        merged[key] = t
                        order.append(key)
                        fresh += 1
                barren = 0 if fresh else barren + 1
                if len(merged) >= max_tweets or barren >= 2:
                    break
                dev.shell("input swipe 540 1700 540 800 300")
                time.sleep(1.3)
            out[nm] = {"collected": len(order),
                       "tweets": [merged[k] for k in order][:max_tweets]}
        return {"timelines": out, "seconds": round(time.time() - t0, 2)}
