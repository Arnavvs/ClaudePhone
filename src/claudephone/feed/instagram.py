"""Instagram feed control: read a reel's meaning, then tell the ranker.

Verified against com.instagram.android 440.1.0.46.86 on RMX3395, 2026-09-03.

Instagram is the richest of the three surfaces, and the only one where a
controlled experiment is possible - it has BOTH a per-item signal (Interested /
Not interested) AND a global reset (Reset suggested content). X has neither.

The thing that makes it work, though, is a piece of luck: **Meta hands over its
own content understanding, as text.** A reel carries no caption in the
accessibility tree - just counts - so there is nothing for a text model to read.
But the reel's More sheet contains an AI-generated description:

    "A young woman humorously captures India's middle-class budget struggles,
     where even spending 50 rupees feels like a major financial decision..."

Scored against that with the on-device embedder (`feed.read`):

    indian middle class comedy relatable humour   0.334
    personal finance money saving                 0.290
    football match                                0.140
    bollywood film celebrity gossip               0.098

So video needs no video model here. Meta has already watched the reel and
written down what it is about; we read the sentence. A frame-sampling CLIP model
would cost orders of magnitude more to reach a worse description of the same
thing. The description is not always present - older reels often lack it, and it
is sometimes vague - so `about_reel` says which case it hit rather than
returning an empty string that reads like an opinion.

Everything that writes is two-phase (`apply=False` plans) and journalled, as in
`feed.x`.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import device as dev
from . import read as rd
from .x import _journal, _nodes, _raw_xml, _tap

IG_PKG = "com.instagram.android"

# Rows in the reel/post More sheet. Instagram names them, unlike X.
_OPTION_RID = "control_option_text"
_MORE_RID = "clips_ufi_more_button_component"

# Meta's description sits under its own heading in the sheet, and the heading is
# the only reliable way to find it. Both nodes are unnamed, so the description
# is "the text node just after this one" - see `open_more`.
_ABOUT_HDR = "about this reel"

# Every one of these WRITES to the account. Named so a caller cannot tap one by
# accident while meaning to read the sheet.
_WRITES = {"interested", "not interested", "report", "save"}


def ensure_reels(serial: str = "", settle: float = 5.0) -> dict:
    """Get to the Reels tab, launching Instagram if needed."""
    fg = dev.foreground(serial=serial).get("package") or ""
    if fg != IG_PKG:
        dev.shell("monkey -p %s -c android.intent.category.LAUNCHER 1" % IG_PKG,
                  serial=serial)
        time.sleep(settle + 3.0)
    # Ask the tab bar, not the content. A reel in the HOME feed carries the same
    # More button as one in the Reels tab, so "there is a More button" is not
    # the question - "which tab is selected" is, and the tab bar answers it.
    def on_clips(ns: list) -> bool:
        return any(n["rid"] == "clips_tab" and n["selected"] for n in ns)

    nodes = _nodes(_raw_xml(serial))
    if on_clips(nodes):
        return {"on_reels": True}
    tab = next((n for n in nodes if n["rid"] == "clips_tab"), None)
    if not tab:
        return {"on_reels": False, "error": "Reels tab not found",
                "foreground": dev.foreground(serial=serial)}
    _tap(*tab["center"], serial=serial, settle=settle)
    return {"on_reels": on_clips(_nodes(_raw_xml(serial)))}


# Reels a pass must scroll straight past without opening anything. An ad's
# controls are not a post's controls: on X, tapping a promoted post's body
# opened the Play Store install sheet, and there is no reason to trust a
# sponsored reel's sheet to behave like an organic one. "Suggested for you" and
# follow-prompt interstitials are not reels at all - there is nothing to judge.
_NOT_A_REEL = ("sponsored", "paid partnership", "suggested for you",
               "sponsored ·", "promoted", "follow more accounts")

# CAREFUL if this is ever reused for the HOME feed: there, "Suggested for you"
# marks a RECOMMENDED post - precisely the ranked content whose Interested /
# Not interested controls exist to be used, and skipping it would skip the only
# thing worth shaping. In Reels the same string marks an interstitial with
# nothing to judge. Same words, opposite meaning, different surface.


def is_ad(serial: str = "", nodes: Optional[list] = None) -> dict:
    """Whether the current reel is an ad or an interstitial rather than content."""
    if nodes is None:
        nodes = _nodes(_raw_xml(serial))
    hits = []
    for n in nodes:
        low = (n["label"] or "").strip().lower()
        if not low or len(low) > 60:
            continue
        for mark in _NOT_A_REEL:
            if mark in low:
                hits.append(n["label"][:60])
                break
    return {"is_ad": bool(hits), "markers": hits[:4]}


# Affordances that sit inside the caption box and are not the caption. A long
# caption collapses behind "See more", and a reader that takes the first text
# node in the box records the word "See more" as what the creator wrote.
_NOT_TEXT = {"see more", "more", "less", "see less", "translate"}

# Labels that share the audio strip but are not audio.
_BADGES = {"ai content", "ai info", "paid partnership", "sponsored"}


def _inside(nodes: list, rid: str, skip_rids: tuple = ()) -> Optional[str]:
    """The longest real text inside the container with `rid`, if any."""
    box = next((n for n in nodes if n["rid"] == rid), None)
    if not box:
        return None
    x0, y0, x1, y1 = box["bounds"]
    hits = []
    for n in nodes:
        lab = (n["label"] or "").strip()
        if n is box or not lab or n["rid"] in skip_rids:
            continue
        if lab.lower() in _NOT_TEXT or lab.lower().startswith("profile picture of"):
            continue
        b = n["bounds"]
        if b[0] >= x0 and b[1] >= y0 and b[2] <= x1 and b[3] <= y1:
            hits.append(lab)
    return max(hits, key=len) if hits else None


def reel_info(serial: str = "", nodes: Optional[list] = None) -> dict:
    """Author and engagement for the reel on screen, from its labels."""
    if nodes is None:
        nodes = _nodes(_raw_xml(serial))
    by_rid = {}
    for n in nodes:
        if n["rid"] and n["label"]:
            by_rid.setdefault(n["rid"], n["label"])

    def num(rid: str) -> Optional[int]:
        s = by_rid.get(rid) or ""
        digits = "".join(c for c in s if c.isdigit())
        return int(digits) if digits else None

    # The caption and the audio credit carry no resource-id of their own, but
    # each sits inside a container that has one, so containment names them
    # exactly. Proximity does not: the audio row is 56px below the username and
    # the caption 136px below it, so "the first text under the author" is the
    # AUDIO every time - which is what an earlier version of this recorded as
    # the caption, on every reel, plausibly enough that the field read as real.
    caption = _inside(nodes, "clips_caption_component")
    audio = _inside(nodes, "clips_author_info_component",
                    skip_rids=("clips_author_username", "clips_author_profile_pic",
                               "inline_follow_button"))
    # That same strip rotates between the track credit and Instagram's own
    # badges, so a read can land on "AI content" instead of the music. It is a
    # real fact about the reel and worth keeping - it is just not the audio, and
    # writing it into an `audio` field would be the same mistake one field over.
    badge = None
    if audio and audio.strip().lower() in _BADGES:
        badge, audio = audio.strip(), None
    proof = next((n["label"] for n in nodes
                  if "liked this reel" in (n["label"] or "").lower()), None)

    return {
        "author": by_rid.get("clips_author_username"),
        # Truncated by the viewer, ellipsis and all - this is the collapsed
        # caption as shown, not the full text, which needs a tap to expand.
        "caption": caption,
        "audio": audio,
        "badge": badge,
        "social_proof": proof,
        "likes": num("like_count"),
        "comments": num("comment_count"),
        "reposts": num("repost_count"),
        "saves": num("save_count"),
    }


def _about_text(nodes: list, idx: int) -> Optional[str]:
    """The description body, which is the unnamed text node after the heading."""
    for n in nodes[idx + 1:idx + 6]:
        lab = (n["label"] or "").strip()
        if lab and not n["rid"] and len(lab) > 20:
            return lab
    return None


def open_more(serial: str = "", wait_s: float = 15.0, poll_s: float = 0.4,
              header_grace: float = 5.0) -> dict:
    """Open the reel's More sheet and read Meta's description from under its heading.

    The More control's touch target is unreliable - a single tap often does
    nothing - so opening retries rather than reporting a reel as having no
    description when the sheet never opened. Those two failures look identical
    from the outside and must not be confused.

    Finding the description used to mean taking the longest unnamed text node in
    the sheet, and that was wrong in a way that did not look wrong. The sheet
    also carries the audio credit ("Salim-Sulaiman, Sukhwinder Singh - Haule
    Haule") and the reel's caption, both unnamed and both long enough to pass a
    length test. The description generates a beat AFTER the sheet renders, so
    for that beat the longest node is the music, and a reader that returns the
    first candidate it sees returns the music. A 20-reel pass recorded 7 track
    listings as descriptions and reported them as a 95% success rate.

    So the anchor is the heading, not the length. "About this reel" is a node of
    its own and the description is the text node after it; until that text
    exists there is no description, however much other text the sheet holds.

    Waiting is split in two, because the two absences are different. A sheet
    that shows no heading within `header_grace` has no description section at
    all and never will - that is an older reel, and waiting longer only costs
    time. A heading whose body has not filled in is still generating, and gets
    the full `wait_s`.
    """
    for attempt in range(3):
        nodes = _nodes(_raw_xml(serial))
        if any(n["rid"] == _OPTION_RID for n in nodes):
            break                       # already open
        btn = next((n for n in nodes if n["rid"] == _MORE_RID), None)
        if not btn:
            return {"opened": False, "error": "no More button - is a reel open?"}
        dev.shell("input tap %d %d" % btn["center"], serial=serial)
        time.sleep(1.6)
    else:
        return {"opened": False, "error": "More sheet would not open"}

    t0 = time.time()
    desc, options, header_seen, audio = None, [], False, None
    while time.time() - t0 < wait_s:
        nodes = _nodes(_raw_xml(serial))
        options = [n["label"] for n in nodes if n["rid"] == _OPTION_RID]
        idx = next((i for i, n in enumerate(nodes)
                    if (n["label"] or "").strip().lower() == _ABOUT_HDR), None)
        if idx is not None:
            header_seen = True
            desc = _about_text(nodes, idx)
            if desc:
                break
        elif time.time() - t0 > header_grace:
            break                       # no section here; nothing to wait for
        time.sleep(poll_s)

    if desc:
        note = None
    elif not options:
        note = "sheet did not render - nothing was read"
    elif header_seen:
        note = ("'About this reel' is present but its text had not generated "
                "after %.0fs" % wait_s)
    else:
        note = ("this reel has no 'About this reel' section - Meta has not "
                "described it, and older reels often never get one")

    return {"opened": True, "options": options, "description": desc,
            "generated": bool(desc), "header": header_seen,
            "waited_s": round(time.time() - t0, 1), "note": note}


def close_sheet(serial: str = "", settle: float = 1.2) -> dict:
    """BACK out of the More sheet - but only if one is actually open.

    BACK is not a no-op on this surface: with no sheet up it leaves the Reels
    tab for Home, and Home looks enough like Reels from the outside that a pass
    will happily keep walking it. One unconditional BACK sent a 20-reel
    collection down the home feed, where it recorded 9 "Suggested for you"
    interstitials as ads, no descriptions and no links, and reported it as a
    thin night on Reels.
    """
    if not any(n["rid"] == _OPTION_RID for n in _nodes(_raw_xml(serial))):
        return {"closed": True, "was_open": False}
    dev.shell("input keyevent KEYCODE_BACK", serial=serial)
    time.sleep(settle)
    return {"closed": not any(n["rid"] == _OPTION_RID
                              for n in _nodes(_raw_xml(serial))),
            "was_open": True}


def signal(kind: str = "not interested", apply: bool = False,
           serial: str = "") -> dict:
    """Tap Interested or Not interested in an OPEN sheet.

    Matches the row by label, never by position - the sheet's contents differ
    between posts and reels, and between recommended and followed content.
    """
    want = kind.strip().lower()
    if want not in ("interested", "not interested"):
        return {"error": "kind must be 'interested' or 'not interested'"}
    nodes = _nodes(_raw_xml(serial))
    row = next((n for n in nodes if n["rid"] == _OPTION_RID
                and n["label"].strip().lower() == want), None)
    if not row:
        return {"error": "%r not in this sheet" % kind,
                "options": [n["label"] for n in nodes
                            if n["rid"] == _OPTION_RID]}
    plan = {"action": "ig_signal", "kind": want, "tap": row["center"]}
    if not apply:
        return {"applied": False, "plan": plan}
    _tap(*row["center"], serial=serial, settle=1.5)
    _journal(plan)
    return {"applied": True, "plan": plan}


def next_reel(serial: str = "", settle: float = 1.4) -> None:
    dev.shell("input swipe 540 1700 540 500 220", serial=serial)
    time.sleep(settle)


def reels_pass(query: str, reels: int = 8, apply: bool = False,
               like_threshold: float = 0.12, drop_threshold: float = 0.03,
               serial: str = "") -> dict:
    """Walk the Reels feed, judging each reel by Meta's own description.

    For each reel: open the sheet, read the description, score it against
    `query`, and tell the ranker - Interested when it is clearly on topic, Not
    interested when it is clearly off. Reels whose description never generated
    are LEFT ALONE: acting on a reel we could not read would be guessing, and a
    wrong Not-interested is as much a write as a right one.

    The thresholds leave a deliberate dead zone, and the first live pass sent NO
    signals because everything landed inside it (0.119, 0.049, 0.137 against a
    narrow query). That is the safe failure, but it is still a failure - a pass
    that writes nothing has shaped nothing. Widen the QUERY before widening the
    thresholds: "indian middle class comedy relatable humour" is far narrower
    than a feed's real variety, and a query naming the whole territory scores
    its members higher.
    """
    ensure_reels(serial)
    out = {"query": query, "applied": apply, "reels": [],
           "scorer": "embedding" if rd.relevance_available() else "unavailable"}
    interested = not_interested = skipped = 0

    ads = 0
    for i in range(reels):
        nodes = _nodes(_raw_xml(serial))
        ad = is_ad(serial, nodes=nodes)
        if ad["is_ad"]:
            # Scroll past without opening anything. Never open an ad's sheet.
            out["reels"].append({"action": "skipped (ad)",
                                 "markers": ad["markers"]})
            ads += 1
            next_reel(serial)
            continue

        info = reel_info(serial, nodes=nodes)
        sheet = open_more(serial)
        rec = {"author": info.get("author"), "likes": info.get("likes"),
               "description": (sheet.get("description") or "")[:200],
               "generated": sheet.get("generated", False)}

        if sheet.get("generated") and out["scorer"] == "embedding":
            s = rd.relevance(query, [sheet["description"]])
            rec["score"] = s[0] if s else None
        else:
            rec["score"] = None

        if rec["score"] is None:
            rec["action"] = "skipped (no description)"
            skipped += 1
        elif rec["score"] >= like_threshold:
            rec["action"] = "interested"
            r = signal("interested", apply=apply, serial=serial)
            rec["result"] = r.get("applied", False)
            interested += 1
        elif rec["score"] <= drop_threshold:
            rec["action"] = "not interested"
            r = signal("not interested", apply=apply, serial=serial)
            rec["result"] = r.get("applied", False)
            not_interested += 1
        else:
            rec["action"] = "left alone (middling)"
            skipped += 1

        out["reels"].append(rec)
        # ALWAYS confirm the sheet is shut before moving on. Tapping a row
        # sometimes dismisses it and sometimes does not, and a sheet left open
        # swallows the next reel entirely - a live pass returned two reels with
        # no author and no description for exactly this reason, which reads like
        # "those reels had no description" when they were never seen at all.
        for _ in range(2):
            if close_sheet(serial).get("closed"):
                break
        next_reel(serial)

    out["interested"] = interested
    out["not_interested"] = not_interested
    out["skipped"] = skipped
    out["ads_skipped"] = ads
    if apply:
        _journal({"action": "ig_reels_pass", "query": query, "n": reels,
                  "interested": interested, "not_interested": not_interested})
    return out
