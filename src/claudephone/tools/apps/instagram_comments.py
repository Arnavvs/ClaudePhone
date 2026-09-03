"""Instagram reel/post comment collection.

Instagram's comment sheet differs from Reddit's in a way that matters:
everything hangs off ONE anchor, `sticky_header_list`, rather than separate
header/body/footer anchors. A comment is a run of sibling values in document
order:

    "Go to <user>'s profile"          # start marker
    "<user>"                          # username, standalone
    "8 August"                        # date
    "<user> said <body>"              # body, username-prefixed
    "Reply"
    "See translation"                 # optional
    "N likes. Double-tap to like..."  # like count

So comments are assembled by walking that stream, keyed on the "<user> said
<body>" line. Coverage is viewport-limited and replies are collapsed behind
"View replies (N)"; both are reported rather than hidden, matching the
completeness honesty used for Reddit threads.

Read-only: the composer (`layout_comment_thread_edittext_multiline`,
`comment_composer_*`) is never touched.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from ... import device as dev
from ... import state
from ... import ui as uix
from .instagram_profile import IG_PKG, parse_count, _dump

_LIKES = re.compile(r"([\d,]+)\s+likes?\.", re.I)
_DATE = re.compile(
    r"^(?:\d{1,2}\s+\w+|\w+\s+\d{1,2}|\d+\s*[wdhms]|\d+\s+\w+\s+ago"
    r"|just now|yesterday)$", re.I)
_SAID = re.compile(r"^(.+?)\s+said\s+(.+)$", re.S)
_VIEW_REPLIES = re.compile(r"View replies?\s*\((\d+)\)", re.I)


def _assemble_comments(els) -> list[dict]:
    """Walk sticky_header_list values into comments, in document order."""
    stream = [(e.text or e.desc or "").strip() for e in els
              if (e.anchor == "sticky_header_list" or e.rid == "sticky_header_list")
              and (e.text or e.desc)]

    out: list[dict] = []
    i = 0
    n = len(stream)
    pending_date: Optional[str] = None
    pending_user: Optional[str] = None

    while i < n:
        val = stream[i]

        rep = _VIEW_REPLIES.search(val)
        if rep and out:
            out[-1]["reply_count"] = int(rep.group(1))
            out[-1]["has_hidden_replies"] = True
            i += 1
            continue

        if val.startswith("Go to ") and val.endswith("'s profile"):
            pending_user = val[len("Go to "):-len("'s profile")]
            i += 1
            continue

        if _DATE.match(val):
            pending_date = val
            i += 1
            continue

        m = _SAID.match(val)
        if m:
            user = m.group(1).strip()
            body = m.group(2).strip()
            # Prefer the explicit "Go to X's profile" username; the said-prefix
            # can be a display name that differs.
            c = {"username": pending_user or user, "text": body,
                 "date": pending_date}
            # look ahead for the like count within a few lines
            for j in range(i + 1, min(i + 6, n)):
                lm = _LIKES.search(stream[j])
                if lm:
                    c["likes"] = parse_count(lm.group(1))
                    break
            out.append(c)
            pending_user = pending_date = None
        i += 1

    return out


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Open the comment sheet on the current reel/post and collect "
            "comments. Scrolls the sheet (upward only - swiping down dismisses "
            "it), assembles username/text/date/likes, and reports collapsed "
            "reply counts. Read-only; never types. Requires a reel or post to be "
            "open first."
        )
    )
    def ig_collect_reel_comments(max_comments: int = 30, max_swipes: int = 12,
                                 settle_s: float = 1.2,
                                 open_sheet: bool = True) -> dict:
        t0 = time.time()

        if open_sheet:
            els = _dump()
            btn = [e for e in els if e.rid == "comment_button"]
            if not btn:
                return {"error": "no comment_button on screen; open a reel/post "
                                 "first", "opened_sheet": False}
            x, y = btn[0].center
            dev.shell(f"input tap {x} {y}")
            time.sleep(3.0)

        # confirm the sheet actually opened
        els = _dump()
        if not any(e.rid in ("layout_container_bottom_sheet",
                             "sticky_header_list") for e in els):
            return {"error": "comment sheet did not open",
                    "opened_sheet": False,
                    "foreground": dev.foreground()}

        seen: set[str] = set()
        comments: list[dict] = []
        hidden_replies = 0
        barren = 0
        swipe = 0
        for swipe in range(max_swipes + 1):
            els = _dump()
            fresh = 0
            for c in _assemble_comments(els):
                key = f"{c.get('username','')}|{c.get('text','')[:80]}"
                if not c.get("text") or key in seen:
                    continue
                seen.add(key)
                comments.append(c)
                if c.get("has_hidden_replies"):
                    hidden_replies += c.get("reply_count", 0)
                fresh += 1
            barren = 0 if fresh else barren + 1
            if len(comments) >= max_comments or barren >= 2:
                break
            # Sheet dismisses on swipe-down, so only ever swipe UP here.
            dev.shell("input swipe 540 1700 540 900 300")
            time.sleep(settle_s)

        result = {
            "collected": len(comments),
            "swipes": swipe,
            "comments": comments[:max_comments],
            "seconds": round(time.time() - t0, 2),
        }
        if hidden_replies:
            result["hidden_reply_count"] = hidden_replies
            result["completeness_note"] = (
                f"{hidden_replies} replies are collapsed behind 'View replies' "
                f"controls and were not expanded. Top-level comments only; the "
                f"sheet is also viewport-limited, so this is a sample."
            )
        return result

    @mcp.tool(
        description="Close the comment sheet (or any Instagram bottom sheet) "
                    "with a single back press."
    )
    def ig_close_sheet() -> dict:
        dev.shell("input keyevent KEYCODE_BACK")
        time.sleep(0.8)
        return {"closed": True}
