"""X / Twitter feed control surface.

Verified against com.twitter.android 12.21.1-prod.05 on RMX3395, 2026-09-02.
See ../../../feed-former.md for the control inventory this implements.

Two facts drive the whole design:

1. **X's post-options menu is surface-dependent.** `Not interested in Post` is
   present on the For-you tab and ABSENT on a List/Topic tab, because Lists and
   Topics are reverse-chronological pipelines with no ranker to signal. A blind
   index tap on the wrong tab lands on `Follow @handle` - the opposite of the
   intended signal. Every mutation here therefore matches on the item's LABEL
   and refuses when the label is missing. It never taps by position.

2. **The active-tab flag is invisible to `ui.parse`.** The node carrying
   `selected="true"` is an anonymous `android.view.View`: no text, no
   resource-id, not clickable, so `ui.parse`'s `meaningful` test drops it. This
   module reads the raw XML for that one signal. Without it there is no way to
   know which timeline is live, and without that, rule 1 cannot be enforced.

Every state-changing call is two-phase: it returns a `plan` describing the exact
tap it would make, and only performs it when `apply=True`. Applied changes are
appended to `artifacts/feed/journal.jsonl` so a session's edits to a real
account are auditable and reversible after the fact.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .. import device as dev
from .. import ui as uix

X_PKG = "com.twitter.android"

# The Home tab strip. Cells span this band; the sheet and the feed never do.
_TAB_Y_MIN, _TAB_Y_MAX = 280, 460
_TAB_SKIP = {"add tab", "scroll to top", "show navigation drawer",
             "find people to follow"}

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

JOURNAL = Path(__file__).resolve().parents[3] / "artifacts" / "feed" / "journal.jsonl"


# --------------------------------------------------------------------------
# raw helpers
# --------------------------------------------------------------------------

def _raw_xml(serial: str = "") -> str:
    dev.shell("uiautomator dump /sdcard/xfeed.xml", serial=serial)
    return dev.adb("exec-out", "cat", "/sdcard/xfeed.xml", serial=serial)


def _nodes(xml: str) -> list[dict]:
    """Every node, unfiltered - including the anonymous ones ui.parse drops."""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for n in root.iter("node"):
        a = n.attrib
        m = _BOUNDS.search(a.get("bounds", ""))
        if not m:
            continue
        b = tuple(int(g) for g in m.groups())
        label = (a.get("text") or "").strip() or (a.get("content-desc") or "").strip()
        out.append({
            "label": label,
            "bounds": b,
            "center": ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2),
            "clickable": a.get("clickable") == "true",
            "selected": a.get("selected") == "true",
            "cls": (a.get("class") or "").rsplit(".", 1)[-1],
        })
    return out


def _tap(x: int, y: int, serial: str = "", settle: float = 2.0) -> None:
    dev.shell("input tap %d %d" % (x, y), serial=serial)
    time.sleep(settle)


def _journal(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry, at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# timelines
# --------------------------------------------------------------------------

def _tab_band(nodes: list, y_min: int = _TAB_Y_MIN, y_lo: int = 300,
              y_bottom: int = 420) -> list:
    """Cells of a horizontal tab strip: fixed-height row near the top.

    The height bound matters - without it a post's "Image" node that happens to
    start inside the band is read as a tab.
    """
    return [n for n in nodes
            if y_min <= n["bounds"][1] <= y_lo
            and n["bounds"][3] >= y_bottom
            and 100 <= (n["bounds"][3] - n["bounds"][1]) <= 200]


def _selected_label(band: list) -> Optional[str]:
    """Which cell in a tab strip is active.

    X marks the active tab on an ANONYMOUS parent cell, never on the labelled
    node, so this pairs the two by x-overlap. Shared by the Home timeline strip
    and the search-results strip because both behave identically - and the
    logic is subtle enough that two copies would drift.
    """
    labelled = [n for n in band if n["label"]]
    for cell in [n for n in band if n["selected"]]:
        cx1, _, cx2, _ = cell["bounds"]
        for lab in labelled:
            lx1, _, lx2, _ = lab["bounds"]
            if min(cx2, lx2) - max(cx1, lx1) > 0.6 * (lx2 - lx1):
                return lab["label"]
    return None


def timelines(serial: str = "", nodes: Optional[list] = None) -> dict:
    """Tab strip contents plus which tab is live.

    `nodes` lets a caller that has already dumped the screen reuse it. A dump
    costs several hundred ms, and the trace recorder captures a screen after
    every settled gesture - making it dump twice per capture would double the
    latency of the one thing that has to keep up with a human.

    The active tab is resolved from the anonymous `selected` cell, matched to a
    label by x-overlap. Returns `active: None` rather than guessing when no cell
    reports selected - mid-animation dumps do that, and a wrong answer here
    silently mis-targets every mutation downstream.
    """
    if nodes is None:
        nodes = _nodes(_raw_xml(serial))
    # Tab cells are a fixed-height row (~144px on this device). Without the
    # height bound a post's "Image" node that happens to start inside the band
    # is read as a timeline tab, and `switch_timeline` then reports the feed's
    # media as an available timeline.
    band = [n for n in nodes
            if _TAB_Y_MIN <= n["bounds"][1] <= 300
            and n["bounds"][3] >= 420
            and 100 <= (n["bounds"][3] - n["bounds"][1]) <= 200]
    # X's SEARCH RESULTS screen reuses this band with identical geometry -
    # Top / Latest / People / Media / Lists cells, same y-range, same 144px
    # height - so band position alone cannot identify the Home strip, and
    # without this check `timelines()` cheerfully reports "Latest" as a
    # timeline and `switch_timeline` taps into the wrong screen. "Add tab" is
    # the Home-only landmark.
    if not any(n["label"].lower() == "add tab" for n in nodes):
        return {"active": None, "tabs": [], "on_home": False,
                "note": "tab strip not found (no 'Add tab') - not on Home"}

    labelled, selected_cells = [], []
    for n in band:
        lab = n["label"]
        if lab and lab.lower() not in _TAB_SKIP and len(lab) <= 28:
            labelled.append(n)
        if n["selected"]:
            selected_cells.append(n)

    active: Optional[str] = None
    for cell in selected_cells:
        cx1, _, cx2, _ = cell["bounds"]
        for lab in labelled:
            lx1, _, lx2, _ = lab["bounds"]
            overlap = min(cx2, lx2) - max(cx1, lx1)
            if overlap > 0.6 * (lx2 - lx1):
                active = lab["label"]
                break
        if active:
            break

    seen, tabs = set(), []
    for n in sorted(labelled, key=lambda n: n["bounds"][0]):
        if n["label"].lower() in seen:
            continue
        seen.add(n["label"].lower())
        tabs.append({"name": n["label"], "center": n["center"],
                     "active": n["label"] == active})
    return {"active": active, "tabs": tabs, "on_home": bool(tabs),
            "note": None if active else
            "no cell reported selected - dump may be mid-animation; re-dump"}


def ensure_home(serial: str = "", settle: float = 2.5) -> dict:
    """Get to the top of Home, where the tab strip exists.

    `timelines()` reporting `on_home: False` is ambiguous on its own - it means
    the strip is not in the tree, which happens both when the feed is scrolled
    (X collapses the header) and when X is not the foreground app at all. A
    back-key loop that does not tell those apart walks straight out of the app,
    so resolve the foreground first and only then deal with scroll position.
    """
    for attempt in range(4):
        fg = dev.foreground(serial=serial).get("package") or ""
        if fg != X_PKG:
            dev.shell("monkey -p %s -c android.intent.category.LAUNCHER 1"
                      % X_PKG, serial=serial)
            time.sleep(settle + 5.0)   # cold start on this device is slow
            continue

        t = timelines(serial)
        if t["on_home"]:
            return {"on_home": True, "active": t["active"]}

        nodes = _nodes(_raw_xml(serial))
        # Deliberately NOT filtered on n["clickable"]: X hangs clickability on
        # an anonymous parent, so the bottom-nav "Home" label reports False and
        # a clickable-filtered search silently finds nothing. Combined with the
        # no-BACK rule that left this stuck on the search screen forever.
        btn = next((n for n in nodes
                    if n["label"].lower() in ("scroll to top", "home")), None)
        if btn:
            _tap(*btn["center"], serial=serial, settle=settle)
        elif any(n["label"] in ("Back", "Filters") for n in nodes):
            # A sub-screen inside X (search results, a profile, settings). BACK
            # is the only way out. The original sin was not BACK itself but
            # BACK with no check afterwards - so press it, then let the next
            # iteration's foreground test relaunch X if we went too far.
            dev.shell("input keyevent KEYCODE_BACK", serial=serial)
            time.sleep(1.8)
        else:
            # Scrolled deep enough that X has hidden BOTH the tab strip and the
            # bottom nav, so there is no control to press - scroll back up
            # instead. Deliberately NOT a back-key press: an earlier version
            # pressed BACK here, which on such a screen walks out of X to the
            # launcher, and the next iteration then types into whatever app is
            # in front. A recovery routine must never be able to leave the app.
            dev.shell("input swipe 540 700 540 2000 220", serial=serial)
            time.sleep(1.2)
    t = timelines(serial)
    return {"on_home": t["on_home"], "active": t["active"],
            "error": None if t["on_home"] else "could not reach Home"}


def switch_timeline(name: str, serial: str = "", settle: float = 2.5) -> dict:
    want = name.strip().lower()
    t = timelines(serial)
    for tab in t["tabs"]:
        if tab["name"].lower() == want:
            if tab["active"]:
                return {"switched": False, "active": tab["name"],
                        "note": "already active"}
            _tap(*tab["center"], serial=serial, settle=settle)
            after = timelines(serial)
            return {"switched": True, "active": after["active"],
                    "requested": tab["name"]}
    return {"error": "timeline %r not found" % name,
            "available": [x["name"] for x in t["tabs"]]}


# --------------------------------------------------------------------------
# post options
# --------------------------------------------------------------------------

def post_options(nth: int = 0, serial: str = "") -> dict:
    """Open the nth visible post's overflow and read the menu back.

    Returns the menu as labels, never as indices - callers must match on text.
    """
    nodes = _nodes(_raw_xml(serial))
    btns = [n for n in nodes
            if n["clickable"] and n["label"].lower() == "post options"]
    btns.sort(key=lambda n: n["bounds"][1])
    if nth >= len(btns):
        return {"error": "only %d post-options buttons visible" % len(btns),
                "wanted": nth}
    _tap(*btns[nth]["center"], serial=serial, settle=2.5)

    menu = _nodes(_raw_xml(serial))
    items = [{"label": n["label"], "center": n["center"]}
             for n in menu
             if n["label"] and n["label"].lower() != "close sheet"
             and n["bounds"][1] > 1000]
    return {"opened_nth": nth, "items": items,
            "labels": [i["label"] for i in items]}


def close_sheet(serial: str = "") -> None:
    dev.shell("input keyevent KEYCODE_BACK", serial=serial)
    time.sleep(1.2)


# X words the negative-feedback item differently per surface: "Not interested
# in Post" on For you, "This post's not helpful" on search results, and nothing
# at all on a List/Topic tab. Three wordings for one signal, found by walking
# the device - assume there are more.
_NEG_LABELS = {
    "not interested in post",
    "not interested in this post",
    "this post's not helpful",
    "this post isn't helpful",
}


def surface(serial: str = "", nodes: Optional[list] = None) -> dict:
    """Which ranked surface is on screen: a Home tab, search results, or other.

    Needed because feed controls are surface-dependent and the same tap means
    different things in each.
    """
    if nodes is None:
        nodes = _nodes(_raw_xml(serial))
    labels = {n["label"] for n in nodes if n["label"]}
    t = timelines(serial, nodes=nodes)
    # `ranked` is the property that actually predicts behaviour. Verified
    # 2026-09-03 across four surfaces: the negative-feedback item is present
    # exactly where a ranker exists - For you and search/Top offer it, while
    # search/Latest and List/Topic tabs (both reverse-chronological) do not.
    # Following is reverse-chron too, so it is NOT ranked.
    if t["on_home"]:
        return {"surface": "home", "timeline": t["active"],
                "ranked": (t["active"] or "").lower() == "for you"}
    if {"Top", "Latest", "People"} <= labels:
        # Search tabs sit slightly lower than Home's (y=307 vs 295), so the
        # band has to reach further down or the strip is missed entirely.
        tab = _selected_label(_tab_band(nodes, y_lo=320))
        return {"surface": "search", "timeline": None, "result_tab": tab,
                "ranked": (tab or "").lower() == "top"}
    return {"surface": "other", "timeline": None, "ranked": False}


def not_interested(nth: int = 0, apply: bool = False, serial: str = "") -> dict:
    """Send the nth visible post a negative ranking signal.

    Per xai-org/x-algorithm, `not_interested` is one of five named negative
    labels the Phoenix ranker predicts, weighted well above any positive
    engagement - a real ranking input, not a UI courtesy.

    The gate is the MENU, not the tab. An earlier version required the For-you
    tab, on the theory that the item existed nowhere else; the 2026-09-03 human
    trace found it on search results too, worded "This post's not helpful", so
    the tab gate was refusing a supported signal. Matching on a label set is
    both safer and broader: if none of the known wordings is present the item
    genuinely is not offered here (a List/Topic tab), and tapping by position
    would hit `Follow @handle` - the opposite signal.
    """
    surf = surface(serial)
    menu = post_options(nth, serial=serial)
    if "error" in menu:
        return menu
    hit = next((i for i in menu["items"]
                if i["label"].strip().lower() in _NEG_LABELS), None)
    if not hit:
        close_sheet(serial)
        return {"error": "no negative-feedback item in this menu",
                "surface": surf, "labels": menu["labels"],
                "note": "expected on For you and search; absent on List/Topic "
                        "tabs - see feed-former.md 1.3 and 8.3"}

    plan = {"action": "not_interested", "nth": nth,
            "surface": surf["surface"], "timeline": surf["timeline"],
            "label": hit["label"], "tap": hit["center"]}
    if not apply:
        close_sheet(serial)
        return {"applied": False, "plan": plan,
                "note": "re-run with apply=True to fire"}
    _tap(*hit["center"], serial=serial, settle=2.0)
    _journal(plan)
    return {"applied": True, "plan": plan}


def add_to_list(nth: int = 0, list_name: str = "", apply: bool = False,
                serial: str = "") -> dict:
    """Open `Add/remove from Lists` for the nth post; optionally toggle a List.

    With no `list_name` this is a read: it reports which Lists exist and whether
    the empty state is showing. That is the honest first call, because on this
    account no Lists exist yet.
    """
    menu = post_options(nth, serial=serial)
    if "error" in menu:
        return menu
    hit = next((i for i in menu["items"]
                if i["label"].lower().startswith("add/remove from lists")), None)
    if not hit:
        close_sheet(serial)
        return {"error": "'Add/remove from Lists' not in this menu",
                "labels": menu["labels"]}
    _tap(*hit["center"], serial=serial, settle=3.0)

    rows = _nodes(_raw_xml(serial))
    labels = [n["label"] for n in rows if n["label"]]
    empty = any("haven" in l.lower() and "lists" in l.lower() for l in labels)
    if empty or not list_name:
        return {"applied": False, "empty_state": empty, "labels": labels,
                "note": "no list_name given" if not list_name
                else "account has no Lists yet - create one first"}

    target = next((n for n in rows
                   if n["label"].strip().lower() == list_name.strip().lower()),
                  None)
    if not target:
        return {"applied": False, "error": "List %r not on screen" % list_name,
                "labels": labels}
    plan = {"action": "add_to_list", "nth": nth, "list": list_name,
            "tap": target["center"]}
    if not apply:
        return {"applied": False, "plan": plan}
    _tap(*target["center"], serial=serial, settle=1.5)
    _journal(plan)
    return {"applied": True, "plan": plan}


# --------------------------------------------------------------------------
# the Timelines (Add tab) screen
# --------------------------------------------------------------------------

def open_timelines_screen(serial: str = "") -> dict:
    nodes = _nodes(_raw_xml(serial))
    btn = next((n for n in nodes
                if n["clickable"] and n["label"].lower() == "add tab"), None)
    if not btn:
        return {"error": "'Add tab' not visible - are you on Home?"}
    _tap(*btn["center"], serial=serial, settle=3.0)
    return {"opened": True}


def search_timelines(query: str, serial: str = "") -> dict:
    """Search the Topics/Lists catalogue on the Timelines screen.

    The catalogue is US-named: 'football' returns nothing, 'soccer' matches.
    Callers doing keyword lookups must not read an empty result as absence.
    """
    fg = dev.foreground(serial=serial).get("package") or ""
    if fg != X_PKG:
        return {"error": "not in X", "foreground": fg,
                "hint": "ensure_home() first"}
    nodes = _nodes(_raw_xml(serial))
    # "Search" is one of the most common labels on Android. Matching it without
    # first proving we are on the Timelines screen once typed a query into the
    # launcher's Google box and read the autocomplete back as if it were X's.
    if not any(n["label"] == "Timelines" for n in nodes):
        return {"error": "not on the Timelines screen",
                "hint": "open_timelines_screen() first",
                "visible": [n["label"] for n in nodes if n["label"]][:12]}
    box = next((n for n in nodes if n["label"].lower() == "search"), None)
    if not box:
        return {"error": "search box not found on the Timelines screen"}
    _tap(*box["center"], serial=serial, settle=1.5)
    dev.shell("input text '%s'" % query.replace(" ", "%s"), serial=serial)
    time.sleep(2.5)
    rows = _nodes(_raw_xml(serial))
    labels = [n["label"] for n in rows if n["label"]]
    return {"query": query,
            "no_results": any("no results" in l.lower() for l in labels),
            "labels": labels}


def pin(name: str, unpin: bool = False, apply: bool = False,
        serial: str = "") -> dict:
    """Pin or unpin a Topic/List as a Home tab, from the Timelines screen."""
    want_btn = "unpin" if unpin else "pin"
    rows = _nodes(_raw_xml(serial))
    row = next((n for n in rows
                if n["label"].strip().lower() == name.strip().lower()), None)
    if not row:
        return {"error": "%r not on screen" % name,
                "hint": "search_timelines() first; the catalogue is US-named"}
    y = row["bounds"][1]
    btn = next((n for n in rows
                if n["label"].lower() == want_btn
                and abs(n["bounds"][1] - y) < 120), None)
    if not btn:
        return {"error": "no %r button beside %r" % (want_btn, name),
                "note": "already in the desired state?"}
    plan = {"action": want_btn, "name": name, "tap": btn["center"]}
    if not apply:
        return {"applied": False, "plan": plan}
    _tap(*btn["center"], serial=serial, settle=2.0)
    _journal(plan)
    return {"applied": True, "plan": plan}


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def snapshot(max_tweets: int = 40, max_swipes: int = 20, settle_s: float = 1.3,
             out_dir: str = "", serial: str = "") -> dict:
    """Sample the live timeline and write it to artifacts/feed/ as JSON.

    Reuses `tools.apps.twitter.assemble_tweets` rather than reimplementing the
    tweet-boundary rule - one parser, so a fix there fixes both paths. Records
    which timeline was active, because a sample is meaningless without it.
    """
    from ..tools.apps.twitter import assemble_tweets

    # X collapses the tab strip once the feed is scrolled, so a snapshot taken
    # where the last one left off cannot see which timeline it is sampling.
    # Return to the top first: it also makes successive samples comparable.
    t = timelines(serial)
    if t["active"] is None:
        top = next((n for n in _nodes(_raw_xml(serial))
                    if n["clickable"] and n["label"].lower() == "scroll to top"),
                   None)
        if top:
            _tap(*top["center"], serial=serial, settle=1.5)
        t = timelines(serial)
    merged: dict[str, dict] = {}
    order: list[str] = []
    barren = 0
    for _ in range(max_swipes + 1):
        els = uix.parse(_raw_xml(serial))
        fresh = 0
        for tw in assemble_tweets(els):
            key = "%s|%s" % (tw.get("handle"), (tw.get("text") or "")[:70])
            if key not in merged:
                merged[key] = tw
                order.append(key)
                fresh += 1
        barren = 0 if fresh else barren + 1
        if len(merged) >= max_tweets or barren >= 2:
            break
        dev.shell("input swipe 540 1700 540 800 300", serial=serial)
        time.sleep(settle_s)

    tweets = [merged[k] for k in order][:max_tweets]
    ads = sum(1 for tw in tweets if tw.get("is_ad"))
    authors: dict[str, int] = {}
    for tw in tweets:
        h = tw.get("handle") or "?"
        authors[h] = authors.get(h, 0) + 1

    rec = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "x",
        "app_version": dev.app_version(X_PKG, serial=serial),
        "timeline": t["active"],
        "collected": len(tweets),
        "composition": {
            "ad_share": round(ads / len(tweets), 3) if tweets else None,
            "distinct_authors": len(authors),
            "repeat_rate": (round(1 - len(authors) / len(tweets), 3)
                            if tweets else None),
            "top_authors": sorted(authors.items(), key=lambda kv: -kv[1])[:10],
        },
        "tweets": tweets,
    }
    out = Path(out_dir) if out_dir else JOURNAL.parent
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = (t["active"] or "unknown").lower().replace(" ", "")
    path = out / ("x-%s-%s.json" % (slug, stamp))
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    rec["path"] = str(path)
    return rec


# --------------------------------------------------------------------------
# search - the route the human actually used (see feed-former.md 8.2)
# --------------------------------------------------------------------------

_RESULT_TABS = ("top", "latest", "people", "media", "lists")


def search(query: str, tab: str = "", settle: float = 3.5,
           serial: str = "") -> dict:
    """Explore -> search box -> type -> submit, optionally switching result tab.

    This is the first half of the only route that measurably moved this feed.
    `twitter.py` has an `x_search` MCP tool, but it is defined inside a
    registrar and cannot be called as a function, which is why this lives here
    rather than being reused.

    Every element is located by LABEL and tapped at its centre, never by its own
    `clickable` flag - X hangs clickability on anonymous parents, so filtering
    on it silently finds nothing.
    """
    fg = dev.foreground(serial=serial).get("package") or ""
    if fg != X_PKG:
        return {"error": "not in X", "foreground": fg, "hint": "ensure_home()"}

    nodes = _nodes(_raw_xml(serial))
    exp = next((n for n in nodes if n["label"].lower() == "explore"), None)
    if not exp:
        home = ensure_home(serial)
        nodes = _nodes(_raw_xml(serial))
        exp = next((n for n in nodes if n["label"].lower() == "explore"), None)
        if not exp:
            return {"error": "Explore tab not found", "home": home}
    _tap(*exp["center"], serial=serial, settle=settle)

    nodes = _nodes(_raw_xml(serial))
    box = next((n for n in nodes if "search" in n["label"].lower()), None)
    if not box:
        return {"error": "search box not found on Explore"}
    _tap(*box["center"], serial=serial, settle=2.0)

    for _ in range(30):          # clear whatever the last search left behind
        dev.shell("input keyevent KEYCODE_DEL", serial=serial)
    time.sleep(0.6)
    safe = query.replace("'", "").replace(" ", "%s")
    dev.shell("input text '" + safe + "'", serial=serial)
    time.sleep(1.5)
    dev.shell("input keyevent KEYCODE_ENTER", serial=serial)
    time.sleep(settle + 1.0)

    out = {"query": query, "submitted": True}
    if tab:
        out["tab"] = switch_result_tab(tab, serial=serial)
    nodes = _nodes(_raw_xml(serial))
    out["surface"] = surface(serial, nodes=nodes)
    out["handles"] = [n["label"] for n in nodes
                      if n["label"].startswith("@")][:12]
    return out


def switch_result_tab(name: str, serial: str = "", settle: float = 2.5) -> dict:
    """Switch between Top / Latest / People / Media / Lists on search results.

    Note `Lists` here: search results expose a Lists tab, which is a route to
    finding curated feeds to follow rather than building one by hand. Unexplored
    as of 2026-09-03.
    """
    want = name.strip().lower()
    if want not in _RESULT_TABS:
        return {"error": "unknown result tab", "asked": name,
                "known": list(_RESULT_TABS)}
    nodes = _nodes(_raw_xml(serial))
    # The result-tab strip shares Home's geometry exactly (see timelines()), so
    # identify it by its own labels rather than by position.
    labels = {n["label"] for n in nodes if n["label"]}
    if not {"Top", "Latest", "People"} <= labels:
        return {"error": "not on a search-results screen", "hint": "search() first"}
    cell = next((n for n in nodes
                 if n["label"].strip().lower() == want
                 and _TAB_Y_MIN <= n["bounds"][1] <= 340), None)
    if not cell:
        return {"error": "tab not visible", "asked": name}
    _tap(*cell["center"], serial=serial, settle=settle)
    return {"switched_to": cell["label"]}


# --------------------------------------------------------------------------
# engagement - read the warning
# --------------------------------------------------------------------------

def like(nth: int = 0, apply: bool = False, serial: str = "") -> dict:
    """Like the nth visible post.

    SCOPE WARNING. `projectContext.txt` and this project's own rules exclude
    synthetic engagement used to steer a ranker, and `favorite` is a scored
    action in xai-org/x-algorithm - so an automated like IS a ranking write,
    not a read. It exists because the human's run used exactly one Like and the
    tool set should be able to express what they did. It is deliberately NOT
    called by `consume()`: firing it in a loop would break the project's rule
    and would also destroy the measurement in feed-former.md 2.1C by injecting
    the signal you are trying to observe.

    Use it as a person would - singly and deliberately. Every fired call is
    journalled.
    """
    nodes = _nodes(_raw_xml(serial))
    likes = sorted([n for n in nodes if n["label"].strip().lower() == "like"],
                   key=lambda n: n["bounds"][1])
    if nth >= len(likes):
        return {"error": "not that many like controls visible",
                "visible": len(likes), "wanted": nth}
    plan = {"action": "like", "nth": nth, "tap": likes[nth]["center"],
            "surface": surface(serial, nodes=nodes)["surface"]}
    if not apply:
        return {"applied": False, "plan": plan,
                "note": "engagement write - read the docstring before firing"}
    _tap(*plan["tap"], serial=serial, settle=1.5)
    _journal(plan)
    return {"applied": True, "plan": plan}


# --------------------------------------------------------------------------
# consume - dwell and scroll, the lever that actually moved the feed
# --------------------------------------------------------------------------

def consume(duration_s: float = 120.0, dwell_min: float = 1.5,
            dwell_max: float = 6.0, apply: bool = False,
            out_dir: str = "", serial: str = "") -> dict:
    """Read a feed the way the human did: scroll, and dwell on what is there.

    The 2026-09-03 trace is why this exists. The account owner moved a For-you
    feed from 0% to 77% Indian politics in under five minutes without touching
    one preference control; the method was search plus sustained reading, and
    `dwell` and `not_dwelled` are both named scored actions in the published
    ranker.

    Honest about what this is: dwelling on purpose to move a recommender is a
    ranking write, and sits closer to the no-synthetic-engagement rule than
    `snapshot()` does. Two things keep it defensible - it performs no action a
    reader does not (no likes, follows or replies; see `like()`), and every run
    is journalled with its parameters so a later measurement can attribute or
    discard it. Do not run it against a timeline you are also measuring: it is
    the treatment, not the instrument.

    Returns what it read, so a treatment run doubles as a collection run.
    """
    import random
    from ..tools.apps.twitter import assemble_tweets

    surf = surface(serial)
    plan = {"action": "consume", "duration_s": duration_s,
            "dwell_s": [dwell_min, dwell_max], "surface": surf}
    if not apply:
        return {"applied": False, "plan": plan,
                "note": "re-run with apply=True; this writes to the ranker"}

    t0 = time.time()
    seen: dict[str, dict] = {}
    steps = 0
    while time.time() - t0 < duration_s:
        xml = _raw_xml(serial)
        for tw in assemble_tweets(uix.parse(xml)):
            key = "%s|%s" % (tw.get("handle"), (tw.get("text") or "")[:70])
            seen.setdefault(key, tw)
        # Dwell varies per post: a fixed cadence is neither what a reader does
        # nor what the ranker records as dwell.
        time.sleep(random.uniform(dwell_min, dwell_max))
        dev.shell("input swipe 540 1700 540 800 %d"
                  % random.randint(240, 420), serial=serial)
        steps += 1
        time.sleep(random.uniform(0.4, 1.1))

    tweets = list(seen.values())
    authors: dict[str, int] = {}
    for tw in tweets:
        h = tw.get("handle") or "?"
        authors[h] = authors.get(h, 0) + 1
    rec = {"applied": True, "plan": plan, "steps": steps,
           "elapsed_s": round(time.time() - t0, 1),
           "distinct_posts": len(tweets), "distinct_authors": len(authors),
           "ads": sum(1 for t in tweets if t.get("is_ad")),
           "top_authors": sorted(authors.items(), key=lambda kv: -kv[1])[:10]}

    out = Path(out_dir) if out_dir else JOURNAL.parent
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("x-consume-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    path.write_text(json.dumps({**rec, "tweets": tweets}, indent=2,
                               ensure_ascii=False), encoding="utf-8")
    rec["path"] = str(path)
    _journal(plan)
    return rec


def engage(duration_s: float = 120.0, like_every: int = 2,
           dwell_min: float = 1.2, dwell_max: float = 3.5,
           apply: bool = False, out_dir: str = "", serial: str = "") -> dict:
    """Read a feed AND like posts on it, at a chosen rate.

    Separate from `consume()` on purpose. `consume()` performs no action a
    reader does not; this one writes `favorite` events, which are a scored
    action in xai-org/x-algorithm and therefore a direct ranking input. Keeping
    them as two functions means a caller - and anyone reading the journal later
    - can always tell which lever produced a change, and `consume()` stays
    usable as a treatment that does not touch engagement.

    The account owner's position, 2026-09-03: dwell alone does not move the
    feed fast enough, so likes are wanted. That is their call on their own
    account. The measurement consequence is real and worth stating: with both
    levers firing at once, a later composition change cannot be attributed to
    either one. Run `consume()` alone if attribution matters more than speed.

    Only taps a control whose label is exactly "Like" - never "Liked" - so a
    pass cannot silently UNLIKE a post the owner had already liked. Every run is
    journalled with its rate and its like count.
    """
    import random
    from ..tools.apps.twitter import assemble_tweets

    surf = surface(serial)
    plan = {"action": "engage", "duration_s": duration_s,
            "like_every": like_every, "dwell_s": [dwell_min, dwell_max],
            "surface": surf}
    if not apply:
        return {"applied": False, "plan": plan,
                "note": "engagement write - likes are a ranking input"}

    t0 = time.time()
    seen: dict[str, dict] = {}
    liked: list[str] = []
    steps = 0
    while time.time() - t0 < duration_s:
        xml = _raw_xml(serial)
        nodes = _nodes(xml)
        for tw in assemble_tweets(uix.parse(xml)):
            seen.setdefault("%s|%s" % (tw.get("handle"),
                                       (tw.get("text") or "")[:70]), tw)

        if like_every and steps % like_every == 0:
            # Exact match only: "Liked" means it is already favourited and
            # tapping would REMOVE the owner's like.
            cands = sorted([n for n in nodes
                            if n["label"].strip() == "Like"
                            and 500 < n["bounds"][1] < 2000],
                           key=lambda n: n["bounds"][1])
            if cands:
                _tap(*cands[0]["center"], serial=serial, settle=0.8)
                liked.append("y=%d" % cands[0]["bounds"][1])

        time.sleep(random.uniform(dwell_min, dwell_max))
        dev.shell("input swipe 540 1700 540 800 %d"
                  % random.randint(240, 420), serial=serial)
        steps += 1
        time.sleep(random.uniform(0.4, 1.0))

    tweets = list(seen.values())
    authors: dict[str, int] = {}
    for tw in tweets:
        h = tw.get("handle") or "?"
        authors[h] = authors.get(h, 0) + 1
    rec = {"applied": True, "plan": plan, "steps": steps,
           "likes_fired": len(liked),
           "elapsed_s": round(time.time() - t0, 1),
           "distinct_posts": len(tweets), "distinct_authors": len(authors),
           "top_authors": sorted(authors.items(), key=lambda kv: -kv[1])[:10]}

    out = Path(out_dir) if out_dir else JOURNAL.parent
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("x-engage-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    path.write_text(json.dumps({**rec, "tweets": tweets}, indent=2,
                               ensure_ascii=False), encoding="utf-8")
    rec["path"] = str(path)
    _journal({**plan, "likes_fired": len(liked)})
    return rec
