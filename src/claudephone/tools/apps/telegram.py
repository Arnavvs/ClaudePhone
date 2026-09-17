"""Telegram: chat list, message reading, global search, joining, replying.

Verified on org.telegram.messenger 12.10.1, realme narzo 50 Pro 5G (Android 14).

Telegram is the hardest app in this repo to read, for one reason: **it has no
resource-ids.** Chats, messages, buttons and the composer are all drawn on a
canvas, so every node comes back as `anchor=content` with a class of ViewGroup
or View. Nothing can be anchored the way Instagram's `clips_caption_component`
is. What Telegram gives instead is unusually *rich* accessibility text - a whole
message, its author, its timestamp, its reactions and its view count arrive as
one string on one node:

    "Short Term Swing \\n\\nIndo Rama \\n70.8 to 78\\n
     Received at Kapil Verma (SEBI REGISTERED RA), 15:03\\n
     13 people reacted with X\\nViewed 4360 times"

So this module is a *parser*, not a selector map. `parse_message` splits that
blob on the "Received at"/"Sent at" landmark: everything before it is the body,
everything after is metadata. That landmark is also the direction flag -
"Received" is inbound, "Sent" is your own message - and it carries the signing
author for channels that sign their posts.

Three things learned the hard way on the device, each of which silently breaks
automation if you do not know it:

1. **The Send button's accessibility node is 300px wide and mostly dead space.**
   Its bounds cover the whole right end of the composer row, but the tappable
   circle sits at the far right. Tapping the node's CENTER does nothing at all -
   no error, no send, the text just stays in the box. Tap `x2 - height/2`
   instead; that is what `_tap_send` does and the only reason it exists.
2. **Reach chats by deep link, never by search-and-tap.** `tg://resolve?domain=`
   opens a public channel directly, joined or not, in about four seconds.
   Search rows are ellipsized (below), so tapping one means tapping a row you
   cannot fully read.
3. **Search result labels are truncated by the renderer, not by us.** A row
   reads "Kkkkk, @stockmarketbulls2026, 323 subscri..." and a long username
   comes back cut in half. A handle from a search hit is therefore a *lead*,
   not an identifier - confirm it with `tg_info`, which reads the full invite
   link off the profile screen.

Anything that reaches other people - `tg_send`, `tg_reply` - is registered as
OUTBOUND in agent.py and has to be named on the command line before it runs.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from ... import device as dev
from ... import state
from ... import ui as uix

TG_PKG = "org.telegram.messenger"
TG_ACTIVITY = "org.telegram.ui.LaunchActivity"

# ui.parse caps labels at 300 chars by default. A Telegram message label holds
# the whole post AND its trailing "Received at HH:MM" - so at 300 chars every
# long post loses the landmark that identifies it as a message, and drops out
# of the transcript entirely. Measured on a channel that posts long notices:
# 3 messages read instead of 12.
MAX_LABEL = 4000

# The landmark every message row is split on. "Received"/"Sent" is the
# direction; the optional name before the clock is a channel's signing author.
_META = re.compile(
    r"(?P<dir>Received|Sent) at (?:(?P<author>.+?), )?(?P<time>\d{1,2}:\d{2})")
_VIEWS = re.compile(r"Viewed\s+([\d\s,\.]+[KM]?)\s+times")
_REACT_LIST = re.compile(r"Reactions:\s*(.+?)(?:\n|$)", re.S)
_REACT_ONE = re.compile(r"(?:(\d+)\s+people|\S+)?\s*reacted with\s+(.+?)(?:\n|$)")
_FWD = re.compile(r"^Forwarded from (.+)$", re.M)
# A media message names its kind on the first line, then the caption:
# "Photo\nBUY NIFTY 23500 CE". Trading channels post charts constantly, so
# without this every chart's caption arrives with "Photo" glued to its front.
_MEDIA = re.compile(
    r"^(Photo|Video|Audio|Voice message|Video message|Sticker|GIF|File|"
    # Media kinds carry their own trailing detail - "Video, 9 seconds",
    # "Voice message, 18 seconds, Not played, 372.8 KB" - so the detail is part
    # of the match rather than something the caption has to survive.
    r"Document|Poll|Location|Contact)((?:,\s*[^\n]*)?)\n", re.I)
_EDITED = re.compile(r"\bedited\b", re.I)
# Date separator rows: "Today", "Yesterday", "June 12", "8 September 2026".
_DATE_ROW = re.compile(
    r"^(Today|Yesterday|\d{1,2}\s+\w+(\s+\d{4})?|\w+\s+\d{1,2}(,\s*\d{4})?)$")
# A search hit: "Name, @username, 323 subscribers" - any part may be elided.
_HIT = re.compile(r"^(?P<name>.+?),\s*@(?P<username>[\w\d_]+)"
                  r"(?:,\s*(?P<count>[\d\s,\.]+[KM]?)\s*"
                  r"(?P<unit>subscribers?|users?|members?))?")
_COUNT = re.compile(r"([\d\s,\.]+)\s*([KM])?", re.I)
# Chat-list rows end in a clock or a date, then optionally an unread badge.
_ROW_TIME = re.compile(
    r"^(\d{1,2}:\d{2}|Today|Yesterday|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"\w{3}|\d{1,2}\s+\w+)$")
_ROW_UNREAD = re.compile(r"^(\d+)\s*(new messages?|unread|)$", re.I)
_ELLIPSIS = re.compile(r"[…]|\.\.\.$")

# Chrome that is never message or chat content.
_CHROME = {
    "go back", "more options", "search", "share", "join", "gift", "web tabs",
    "profile photo", "attach media", "record voice message", "send",
    "emoji, stickers, and gifs", "message", "chats", "contacts", "settings",
    "profile", "new message", "post story", "global search", "show more",
    "close", "mute", "unmute", "report", "leave channel", "delete",
    "welcome to telegram", "pinned message",
}


def parse_count(raw: str) -> Optional[int]:
    """"111.7K" / "323" / "73 606" -> int. Telegram space-groups thousands."""
    m = _COUNT.match((raw or "").strip())
    if not m:
        return None
    body = m.group(1).replace(" ", "").replace(",", "")
    try:
        v = float(body)
    except ValueError:
        return None
    if m.group(2):
        v *= {"K": 1e3, "M": 1e6}[m.group(2).upper()]
    return int(round(v))


def parse_reactions(tail: str) -> dict:
    """{emoji: count} from either rendering Telegram uses.

    Two shapes exist and both turn up in one screenful: an inline list
    "Reactions: X 21, Y 8" when several emoji are present, and prose
    "13 people reacted with X" when only one is. The prose form omits the
    number entirely for a single reactor.
    """
    out: dict[str, int] = {}
    m = _REACT_LIST.search(tail)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.rsplit(" ", 1)
            if len(bits) == 2 and parse_count(bits[1]) is not None:
                out[bits[0].strip()] = parse_count(bits[1]) or 0
            else:
                out[part] = 1
        return out
    m = _REACT_ONE.search(tail)
    if m:
        emoji = (m.group(2) or "").strip()
        if emoji:
            out[emoji] = int(m.group(1)) if m.group(1) else 1
    return out


def parse_message(text: str) -> Optional[dict]:
    """One message-row label -> a structured message, or None if it is not one.

    Returns None for date separators and stray chrome, so callers can feed it
    every ViewGroup on screen without pre-filtering.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = _META.search(raw)
    if not m:
        return None
    body = raw[:m.start()].strip()
    tail = raw[m.end():]

    media = None
    mm = _MEDIA.match(body)
    if mm:
        media = mm.group(1).strip().lower()
        detail = (mm.group(2) or "").lstrip(", ").strip()
        if detail:
            media += " (" + detail + ")"
        body = body[mm.end():].strip()

    fwd = None
    fm = _FWD.search(body)
    if fm:
        fwd = fm.group(1).strip()
        body = (body[:fm.start()] + body[fm.end():]).strip()

    views = None
    vm = _VIEWS.search(tail)
    if vm:
        views = parse_count(vm.group(1))

    return {
        "text": body or None,
        "author": (m.group("author") or "").strip() or None,
        "time": m.group("time"),
        "outgoing": m.group("dir") == "Sent",
        "forwarded_from": fwd,
        "media": media,
        "views": views,
        "reactions": parse_reactions(tail),
        "seen": ", Seen" in tail,
        "edited": bool(_EDITED.search(tail)),
        "reply_to": None,
        "date": None,
    }


def parse_chat_row(label: str) -> Optional[dict]:
    """A chat-list row label -> {name, preview, when, unread}.

    Rows are comma-joined and the renderer elides them, so this keeps the raw
    label alongside the split. Trailing fields are peeled off the end - an
    unread badge last, a timestamp before it - because the NAME is the field
    that must stay right, and only the tail has a recognisable shape.
    """
    raw = (label or "").strip()
    if not raw or "," not in raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    unread = None
    when = None
    m = _ROW_UNREAD.match(parts[-1]) if len(parts) > 1 else None
    if m and (m.group(2) or len(parts) > 2):
        unread = int(m.group(1))
        parts = parts[:-1]
    if len(parts) > 1 and _ROW_TIME.match(parts[-1]):
        when = parts[-1]
        parts = parts[:-1]
    if not parts:
        return None
    name = parts[0]
    if not name or name.lower() in _CHROME:
        return None
    preview = ", ".join(parts[1:]).strip() or None
    return {"name": name, "preview": preview, "when": when, "unread": unread,
            "truncated": bool(_ELLIPSIS.search(raw)), "raw": raw}


def _contains(bounds, point) -> bool:
    x1, y1, x2, y2 = bounds
    return x1 <= point[0] <= x2 and y1 <= point[1] <= y2


def assemble_messages(els) -> list[dict]:
    """Every message on screen, in display order (oldest first, top to bottom).

    Date separators are folded into the messages that follow them, and reply
    quotes are attached by GEOMETRY rather than document order: a quote renders
    as its own node inside the bubble it belongs to, and the tree emits it
    after that bubble, so pairing by adjacency attaches quotes to the wrong
    message.
    """
    quotes = [e for e in els
              if (e.desc or "").startswith("Reply,")
              and e.bounds != (0, 0, 0, 0)]
    out: list[dict] = []
    current_date: Optional[str] = None
    for e in els:
        label = (e.text or "").strip()
        if not label:
            continue
        if _META.search(label) is None:
            if _DATE_ROW.match(label) and len(label) < 32:
                current_date = label
            continue
        msg = parse_message(label)
        if not msg:
            continue
        msg["date"] = current_date
        for q in quotes:
            if _contains(e.bounds, q.center):
                # "Reply, <chat or author>, <quoted text>"
                parts = (q.desc or "")[len("Reply,"):].strip().split(", ", 1)
                msg["reply_to"] = {
                    "author": parts[0].strip() if parts else None,
                    "text": parts[1].strip() if len(parts) > 1 else None,
                }
                break
        msg["c"] = list(e.center)
        out.append(msg)
    return out


def assemble_hits(els) -> list[dict]:
    """Global-search rows -> {name, username, subscribers}.

    `username` may be truncated: Telegram elides the whole row label to fit the
    cell, so a long handle arrives cut. The flag says so rather than handing
    back a plausible-looking wrong handle.
    """
    hits = []
    for e in els:
        label = (e.text or "").strip()
        if not label or "@" not in label:
            continue
        if label.lower() in _CHROME:
            continue
        m = _HIT.match(label)
        if not m:
            continue
        hits.append({
            "name": m.group("name").strip(),
            "username": "@" + m.group("username"),
            "subscribers": parse_count(m.group("count") or ""),
            "kind": (m.group("unit") or "").rstrip("s") or None,
            "truncated": bool(_ELLIPSIS.search(label)),
            "raw": label,
            "c": list(e.center),
        })
    return hits


# --- device plumbing --------------------------------------------------------

_SIZE: Optional[tuple] = None


def _screen() -> tuple:
    global _SIZE
    if _SIZE is None:
        try:
            w, h = (int(v) for v in dev.device_info().screen.lower().split("x"))
            _SIZE = (w, h)
        except Exception:
            _SIZE = (1080, 2400)
    return _SIZE


def _read():
    """One screen read, Telegram's own nodes only.

    Prefers the on-device AccessibilityService (~13 ms) and falls back to a
    bare uiautomator2 dump (~260 ms). Deliberately NOT `observer.look()`: that
    also calls `dev.foreground()`, a second ~200 ms adb round trip, which is
    most of the cost of a read on the u2 path and buys nothing here - the
    screen's own nodes already say whether we are in Telegram.

    Bridge elements carry no package, so the filter keeps unlabelled nodes.
    The IME alone contributes ~90 nodes of key_pos_* on the u2 path.
    """
    from ...runtime import bridge as br
    if br.available():
        # MAX_LABEL, not the server's 300-character default: a channel post is
        # the payload here, and truncating it loses the message rather than a
        # button's label.
        els = br.bridge().tree(max_text=MAX_LABEL)["elements"]
    else:
        els = uix.parse(dev.u2().dump_hierarchy(), max_text=MAX_LABEL)
    tg = [e for e in els if not e.pkg or e.pkg == TG_PKG]
    state.remember(tg, TG_PKG)
    return tg


def _sig(els) -> str:
    """What is on screen, as one comparable string. Cheap change detection.

    Keyed on the messages, NOT on the raw labels, and that distinction is the
    whole trick. A channel's view counters tick live - "Viewed 5302 times"
    becomes 5303 a second later - so a signature over raw text never repeats
    twice in a row, and `_settle` below would burn its entire budget on every
    swipe waiting for a screen that is never going to hold still. A message key
    is author + clock + body, none of which move.
    """
    keys = [_msg_key(m) for m in assemble_messages(els)]
    if keys:
        return "\x1f".join(keys)
    return "\x1f".join((e.text or e.desc or "")[:60] for e in els)


def _wait_for(pred, timeout_s: float = 8.0, poll_s: float = 0.12):
    """Poll until `pred(elements)` holds. -> (elements, seconds_waited).

    This replaces the blind `sleep(4.5)` that used to follow every intent. An
    app launch is not a fixed cost - a warm Telegram opens a channel in well
    under a second - so waiting the worst case every time was most of the
    runtime of every tool in this module.
    """
    t0 = time.time()
    while True:
        els = _read()
        if pred(els):
            return els, round(time.time() - t0, 2)
        if time.time() - t0 >= timeout_s:
            return els, round(time.time() - t0, 2)
        time.sleep(poll_s)


def _settle(max_s: float = 1.4, quiet_s: float = 0.25):
    """Wait for the screen to stop moving after a fling. -> elements.

    The complement of _wait_for: after a swipe the interesting moment is when
    the list STOPS, and a fling that has already finished should cost one read
    rather than a fixed settle time.
    """
    t0 = time.time()
    els = _read()
    sig = _sig(els)
    steady = time.time()
    while time.time() - t0 < max_s:
        time.sleep(0.05)
        nxt = _read()
        nsig = _sig(nxt)
        if nsig == sig:
            if time.time() - steady >= quiet_s:
                return nxt
        else:
            steady = time.time()
        els, sig = nxt, nsig
    return els


def _find(els, needle: str, exact: bool = False):
    n = needle.strip().lower()
    for e in els:
        for v in ((e.text or "").strip().lower(),
                  (e.desc or "").strip().lower()):
            if not v:
                continue
            if (v == n) if exact else (n in v):
                return e
    return None


def _tap(e):
    x, y = e.center
    dev.shell(f"input tap {x} {y}")
    return x, y


def _tap_send(e):
    """Tap the send button's actual circle, not its node centre.

    See the module docstring: the node spans the full right end of the composer
    and its centre lands in padding. The circle is one row-height in from the
    right edge.
    """
    x1, y1, x2, y2 = e.bounds
    x = x2 - (y2 - y1) // 2
    y = (y1 + y2) // 2
    dev.shell(f"input tap {x} {y}")
    return [x, y]


def _type(text: str) -> dict:
    """`input text` into the focused field.

    `input text` is ASCII-only in practice: anything outside it is dropped by
    the shell input command without complaint, so non-ASCII is reported rather
    than silently lost.
    """
    dropped = [c for c in text if ord(c) > 127]
    body = "".join(c for c in text if ord(c) <= 127)
    safe = body.replace("'", "'\\''").replace(" ", "%s")
    dev.shell(f"input text '{safe}'")
    res: dict[str, Any] = {"typed": body}
    if dropped:
        res["dropped_non_ascii"] = "".join(dropped)
        res["note"] = ("`input text` cannot type non-ASCII characters; those "
                       "were dropped. Keep messages ASCII, or install an IME "
                       "that accepts broadcast input.")
    return res


def deeplink(chat: str) -> str:
    """A chat reference in any form -> the intent URI that opens it.

    Accepts @handle, a bare handle, t.me/handle, t.me/s/handle (the web-preview
    form the listicles publish), and private invites (t.me/+HASH,
    t.me/joinchat/HASH), which must stay https - tg://resolve only takes public
    usernames.
    """
    c = (chat or "").strip()
    if c.startswith("tg://"):
        return c
    m = re.search(r"t\.me/(?:s/)?(\+[\w-]+|joinchat/[\w-]+|[\w\d_]{3,})", c)
    if m:
        tok = m.group(1)
        if tok.startswith("+") or tok.startswith("joinchat/"):
            return "https://t.me/" + tok
        return "tg://resolve?domain=" + tok
    return "tg://resolve?domain=" + c.lstrip("@")


def _open(chat: str = "", wait_s: float = 4.5, reset: bool = True) -> dict:
    """Open Telegram, or one chat inside it. Reports whether the chat resolved.

    `reset` is not a nicety. A deep link to a handle that does not exist does
    NOT clear the screen - Telegram simply stays where it was. Land on the chat
    list first and an unresolved handle leaves the chat list showing, which is
    detectable; skip it and the caller reads the header of whatever channel was
    open a moment ago and believes it opened the one it asked for. That is how
    a join run reported two channels under handles that were never resolved.
    """
    waited = 0.0
    if chat and reset:
        els = _read()
        # The reset exists to stop a chat we did not open being mistaken for
        # one we did. Two cases need no protection: the chat list, where there
        # is nothing stale to confuse us, and a chat THIS module opened by this
        # same handle and can still see the title of. The second is the whole
        # polling loop - "read what is new in channel X" every minute - and
        # skipping the reset there is worth about two seconds a call.
        if not _on_chat_list(els) and not _known_open(chat, els):
            dev.shell(f"am start -n {TG_PKG}/{TG_ACTIVITY}")
            _wait_for(lambda e: bool(e), timeout_s=2.0)
            _to_chat_list(4)
    if chat:
        url = deeplink(chat)
        dev.shell(f'am start -a android.intent.action.VIEW -d "{url}" {TG_PKG}')
        # Landed when the chat list is gone AND a chat screen is up. Polling
        # both keeps an unresolved handle from looking like a slow load.
        els, waited = _wait_for(
            lambda e: bool(e) and not _on_chat_list(e) and _is_chat(e),
            timeout_s=wait_s)
        resolved = bool(els) and not _on_chat_list(els) and _is_chat(els)
        if resolved:
            _LAST_OPEN.update(link=deeplink(chat).lower(),
                              title=_header(els).get("title") or "",
                              at=time.time())
    else:
        dev.shell(f"am start -n {TG_PKG}/{TG_ACTIVITY}")
        els, waited = _wait_for(lambda e: bool(e), timeout_s=wait_s)
        resolved = True
    in_tg = bool(els)
    res = {"opened": chat or "chat list", "in_telegram": in_tg,
           "waited_s": waited}
    if not in_tg:
        # Nothing of Telegram's on screen - only now is the adb round trip for
        # the foreground package worth paying for.
        res["foreground"] = dev.foreground()
    if chat:
        res["resolved"] = resolved
    return res


# What this module last deep-linked to, so a repeat visit can skip the reset.
_LAST_OPEN = {"link": "", "title": "", "at": 0.0}
_LAST_OPEN_TTL_S = 600.0


def _known_open(chat: str, els) -> bool:
    """Is this exact chat on screen because we put it there recently?

    Both halves matter: the handle has to match what we last opened, and the
    title on screen has to still be the title we saw then. Without the second
    check a chat someone navigated away from would keep its claim.
    """
    if _LAST_OPEN["link"] != deeplink(chat).lower() or not _LAST_OPEN["title"]:
        return False
    if time.time() - _LAST_OPEN["at"] > _LAST_OPEN_TTL_S:
        return False
    return _header(els).get("title") == _LAST_OPEN["title"]


def _chat_rows(els=None) -> list:
    """The chat-list cells, found by SHAPE because they have no text.

    A DialogCell is a full-width ViewGroup a little over 200px tall carrying no
    text and no content-desc - Telegram draws the name, preview, clock and
    unread badge onto a canvas and publishes none of them. Matching on the
    rectangle is all that is left, and it is stable: nothing else on this
    screen has that shape.

    Takes its own dump with `keep_layout=True`, because the ordinary reader
    discards silent nodes and these rows are nothing but silence.
    """
    if els is None:
        els = uix.parse(dev.u2().dump_hierarchy(), max_text=MAX_LABEL,
                        keep_layout=True)
        els = [e for e in els if not e.pkg or e.pkg == TG_PKG]
    w, h = _screen()
    out = []
    for e in els:
        x1, y1, x2, y2 = e.bounds
        if e.text or e.desc:
            continue
        if e.cls != "ViewGroup" or x1 > 4 or x2 < w - 4:
            continue
        if not (140 <= (y2 - y1) <= 320) or y1 < 380:
            continue
        # The last row runs under the bottom tab strip. Its rectangle is real,
        # but tapping its centre hits Contacts or Settings instead of the chat.
        if (y1 + y2) // 2 > h - 270:
            continue
        out.append(e)
    return sorted(out, key=lambda e: e.bounds[1])


def _peek_row(row) -> Optional[dict]:
    """Open one chat-list row, read its title, and come back. ~2 s.

    Counted as a tg_read; a refusal comes back as {"error": ..., "read": ...}.
    """
    from ...policy import reads
    gate = reads.acquire("tg_read", "tg", target="chat_list_row")
    if not gate.allowed:
        return reads.refusal(gate)
    _tap(row)
    reads.commit(gate, target="chat_list_row")
    els, _ = _wait_for(lambda e: _is_chat(e) and not _on_chat_list(e),
                       timeout_s=4.0)
    if _on_chat_list(els):
        return None
    header = _header(els)
    msgs = assemble_messages(els)
    last = msgs[-1] if msgs else None
    dev.shell("input keyevent KEYCODE_BACK")
    _wait_for(_on_chat_list, timeout_s=3.0)
    return {
        "title": header.get("title"),
        "subtitle": header.get("subtitle"),
        "members": header.get("members"),
        "last_message": (last or {}).get("text"),
        "last_time": (last or {}).get("time"),
    }


def _on_chat_list(els) -> bool:
    """The chat list proper - NOT the search overlay that replaces it.

    Only the New Message FAB tells them apart. The search box is no help: it
    exists on both, and with an empty query its text still reads "Search
    Chats", so matching on it made `tg_chats` scrape global-search hits as if
    they were the user's own chats, and made `_to_chat_list` report success
    while sitting on the search screen.
    """
    return _find(els, "new message") is not None


def _is_chat(els) -> bool:
    """A conversation screen: a two-line title bar, or a way to act on it."""
    if any("\n" in (e.desc or "") and e.bounds[1] < 400 for e in els):
        return True
    return (_find(els, "join", exact=True) is not None
            or any(e.cls == "EditText" and e.bounds[1] > 1500 for e in els))


def _header(els) -> dict:
    """Title and subtitle from the chat's top bar.

    The bar publishes both as one two-line content-desc on the clickable header
    AND as two separate TextViews. The desc is used because it survives the
    profile screen, where the collapsing toolbar duplicates the TextViews and
    the first one found is as likely to be the shrinking copy.
    """
    for e in els:
        d = (e.desc or "")
        if "\n" in d and e.bounds[1] < 400:
            title, _, sub = d.partition("\n")
            return {"title": title.strip(), "subtitle": sub.strip(),
                    "members": parse_count(sub)}
    for e in els:
        if e.cls == "TextView" and e.bounds[1] < 260 and (e.text or "").strip():
            return {"title": e.text.strip(), "subtitle": None, "members": None}
    return {"title": None, "subtitle": None, "members": None}


# Swipe span, as a fraction of screen height. Wide on purpose: a signals
# channel's bubbles are tall, and a 45% swipe was yielding about two new
# messages per screen - five swipes to collect ten. 68% still overlaps enough
# that nothing is skipped, and roughly halves the swipes for the same haul.
_SPAN = (0.20, 0.88)


def _scroll_up(max_s: float = 1.4):
    """Reveal OLDER messages. History runs upward, so the swipe goes down."""
    w, h = _screen()
    dev.shell(f"input swipe {w // 2} {int(h * _SPAN[0])} "
              f"{w // 2} {int(h * _SPAN[1])} 300")
    return _settle(max_s)


def _scroll_down(max_s: float = 1.4):
    w, h = _screen()
    dev.shell(f"input swipe {w // 2} {int(h * _SPAN[1])} "
              f"{w // 2} {int(h * _SPAN[0])} 300")
    return _settle(max_s)


def _key(name: str, settle_s: float = 0.4) -> None:
    dev.shell(f"input keyevent {name}")
    time.sleep(settle_s)


def _msg_key(m: dict) -> str:
    return f"{m.get('author')}|{m.get('time')}|{(m.get('text') or '')[:80]}"


def _open_guard(chat: str, wait_s: float = 4.5):
    """Open a chat and prove it is the one on screen. -> (opened, error|None).

    Every tool that takes a `chat` goes through this, so an unresolved handle
    is reported as such instead of being acted on as whatever was open before.
    Opening a chat is a counted tg_read (B2b).
    """
    gate = None
    if chat:
        from ...policy import reads
        gate = reads.acquire("tg_read", "tg", target=chat)
        if not gate.allowed:
            return {}, reads.refusal(gate)
    opened = _open(chat, wait_s)
    if gate is not None:
        reads.commit(gate, target=chat)
    if not opened.get("in_telegram"):
        return opened, {"error": "Telegram did not come to the foreground",
                        **opened}
    if chat and not opened.get("resolved"):
        return opened, {
            "error": f"{chat!r} did not resolve - Telegram stayed on the chat "
                     "list. The handle may be wrong, private, or the chat may "
                     "no longer exist.",
            **opened}
    return opened, None


def _to_chat_list(tries: int = 4) -> bool:
    """Back out until the chat list is on screen. True if it got there.

    Backing out of an open search takes two presses on this build - one to
    clear the query, one to close the overlay - so `tries` needs headroom.
    """
    for _ in range(tries):
        els = _read()
        if _on_chat_list(els) or _find(els, "new message"):
            return True
        dev.shell("input keyevent KEYCODE_BACK")
        _wait_for(lambda e: _on_chat_list(e) or _find(e, "new message"),
                  timeout_s=1.5)
    return _on_chat_list(_read())


# --- reading ----------------------------------------------------------------

def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Open Telegram, optionally straight to a chat. `chat` takes @handle, "
            "a bare handle, a t.me link (including t.me/s/ and private +HASH "
            "invites) or a tg:// URI. Opening a public channel works whether or "
            "not you have joined it."
        )
    )
    def tg_open(chat: str = "", wait_s: float = 4.5) -> dict:
        gate = None
        if chat:
            from ...policy import reads
            gate = reads.acquire("tg_read", "tg", target=chat)
            if not gate.allowed:
                return reads.refusal(gate)
        res = _open(chat, wait_s)
        if gate is not None:
            reads.commit(gate, target=chat)
            res["read"] = gate.to_dict()
        if chat and not res.get("resolved"):
            res["error"] = (f"{chat!r} did not resolve - Telegram stayed on "
                            "the chat list.")
            return res
        els = _read()
        res["header"] = _header(els)
        res["version"] = dev.app_version(TG_PKG)
        res["joined"] = _find(els, "join", exact=True) is None
        return res

    @mcp.tool(
        description=(
            "List the chat list. NOTE: this Telegram build does not label its "
            "chat rows for accessibility - names, previews and unread badges "
            "are drawn, not published - so the fast mode can only count rows. "
            "Pass deep=true to open each row and read its real title, at about "
            "two seconds a chat. To follow channels you already know, "
            "tg_catchup is far cheaper than either."
        )
    )
    def tg_chats(max_chats: int = 25, deep: bool = False,
                 max_swipes: int = 6) -> dict:
        t0 = time.time()
        if dev.foreground().get("package") != TG_PKG:
            _open("", 4.5)
        if not _to_chat_list():
            return {"error": "could not get back to the chat list",
                    "foreground": dev.foreground()}
        rows = _chat_rows()
        note = (
            "Chat rows carry no text or content-desc in this build "
            f"({dev.app_version(TG_PKG)}): they are custom-drawn cells and "
            "the accessibility tree exposes only their rectangles. Row count "
            "and positions are real; names are not available without opening "
            "them (deep=true)."
        )
        if not deep:
            return {"visible_rows": len(rows), "chats": [], "deep": False,
                    "note": note, "seconds": round(time.time() - t0, 2)}

        seen: list[dict] = []
        titles: set = set()
        for _ in range(max_swipes + 1):
            rows = _chat_rows()
            progressed = False
            for r in rows:
                if len(seen) >= max_chats:
                    break
                info = _peek_row(r)
                if info and info.get("error"):
                    return {"visible_rows": len(rows), "collected": len(seen),
                            "chats": seen, "deep": True, "note": note,
                            "stopped": info["error"], "read": info.get("read"),
                            "seconds": round(time.time() - t0, 2)}
                if not info or not info.get("title"):
                    continue
                if info["title"] in titles:
                    continue
                titles.add(info["title"])
                seen.append(info)
                progressed = True
            if len(seen) >= max_chats or not progressed:
                break
            _scroll_down()
        return {"visible_rows": len(rows), "collected": len(seen),
                "chats": seen, "deep": True, "note": note,
                "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "Read the latest messages from several channels in one call and "
            "return them merged, newest last, tagged with the channel they came "
            "from. This is the way to follow a set of channels: it is one deep "
            "link per channel and no reliance on unread badges, which this "
            "Telegram build does not expose."
        )
    )
    def tg_catchup(chats: list, n: int = 5) -> dict:
        t0 = time.time()
        out: list[dict] = []
        problems: list[dict] = []
        for c in chats or []:
            r = tg_read(chat=c, max_messages=n, max_swipes=max(0, (n - 1) // 3))
            if "error" in r:
                problems.append({"chat": c, "error": r["error"]})
                continue
            title = (r.get("header") or {}).get("title")
            for m in r.get("messages", []):
                m["chat"] = c
                m["channel"] = title
                out.append(m)
        return {"channels": len(chats or []), "collected": len(out),
                "messages": out, "problems": problems or None,
                "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "Read messages from a chat. Give `chat` to open one first, or omit "
            "it to read whatever is already open. Scrolls back through history "
            "and returns messages oldest-first with author, time, direction, "
            "view count, reactions, forward source and any quoted reply."
        )
    )
    def tg_read(chat: str = "", max_messages: int = 25, max_swipes: int = 12,
                settle_s: float = 1.1) -> dict:
        t0 = time.time()
        if chat:
            _, err = _open_guard(chat)
            if err:
                return err
        # A chat screen is up before its bubbles render, so waiting on the
        # header alone reads an empty list and reports the channel as silent.
        els, _ = _wait_for(lambda e: bool(assemble_messages(e)), timeout_s=3.0)
        header = _header(els)
        merged: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        swipe = 0
        for swipe in range(max_swipes + 1):
            page: list[str] = []
            fresh = 0
            for m in assemble_messages(els):
                key = _msg_key(m)
                page.append(key)
                if key not in merged:
                    merged[key] = m
                    fresh += 1
                else:
                    # A bubble scrolling into view renders before its reaction
                    # and view-count rows; fill the gaps rather than duplicate.
                    for k, v in m.items():
                        if v in (None, {}, [], False):
                            continue
                        if merged[key].get(k) in (None, {}, [], False):
                            merged[key][k] = v
            # Each scroll step moves further back in history, so a page's
            # messages all precede everything gathered so far.
            order = [k for k in page if k not in order] + order
            barren = 0 if fresh else barren + 1
            if len(merged) >= max_messages or barren >= 2:
                break
            _scroll_up(settle_s)
            els = _read()
        msgs = [merged[k] for k in order][-max_messages:]
        for m in msgs:
            m.pop("c", None)
        return {"chat": chat or header.get("title"), "header": header,
                "collected": len(msgs), "swipes": swipe, "messages": msgs,
                "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "The last few messages of a channel, as fast as this can be done: "
            "one deep link and one screen read, scrolling only if the screenful "
            "held fewer than `n`. Use this for 'what is new in X' polling; use "
            "tg_read when you want to go back through history."
        )
    )
    def tg_last(chat: str = "", n: int = 6) -> dict:
        return tg_read(chat=chat, max_messages=n,
                       max_swipes=max(0, (n - 1) // 3))

    @mcp.tool(
        description=(
            "Search Telegram globally. `tab` picks what is searched: chats "
            "(default), channels, apps, media, or POSTS - which searches the "
            "text of public messages across all of Telegram rather than the "
            "names of chats, and is how you find who is talking about a ticker "
            "right now. Handles in hits can be TRUNCATED by the renderer - "
            "confirm one with tg_info before treating it as an identifier."
        )
    )
    def tg_search(query: str, tab: str = "", max_results: int = 20,
                  max_swipes: int = 6, settle_s: float = 1.2) -> dict:
        from ...policy import reads
        gate = reads.acquire("tg_search", "tg", target=query[:60])
        if not gate.allowed:
            return reads.refusal(gate)
        t0 = time.time()
        if dev.foreground().get("package") != TG_PKG:
            _open("", 4.5)
        if not _to_chat_list():
            return {"error": "could not get back to the chat list",
                    "foreground": dev.foreground()}
        box = _find(_read(), "search chats")
        if box is None:
            return {"error": "search box not found on the chat list",
                    "foreground": dev.foreground()}
        reads.commit(gate, target=query[:60])
        _tap(box)
        time.sleep(1.2)
        typed = _type(query)
        time.sleep(2.8)

        posts = False
        if tab:
            want = tab.strip().lower()
            hit = next((e for e in _read()
                        if (e.desc or "").strip().lower() == want), None)
            if hit is None:
                return {"error": f"no {tab!r} tab on the search screen",
                        "tabs": [e.desc for e in _read()
                                 if (e.desc or "") and e.bounds[1] < 500]}
            _tap(hit)
            time.sleep(2.2)
            posts = want in ("posts", "media")

        # "Show more" expands the global-search section from a teaser of four
        # rows to the full result list. Without it a search never returns more
        # than four channels no matter how far you scroll.
        more = _find(_read(), "show more", exact=True)
        if more:
            _tap(more)
            time.sleep(2.2)
        _key("KEYCODE_BACK", 1.0)      # drop the IME; it hides half the list

        if posts:
            # Searching the text of public posts across Telegram is a PREMIUM
            # feature. Without it the tab renders a pitch, not results - which
            # would otherwise be reported as "0 posts found" and read as "no
            # one is talking about this", the most misleading answer available.
            wall = _find(_read(), "premium feature")
            if wall is not None:
                _to_chat_list(4)
                return {
                    "query": query, "tab": tab, "collected": 0,
                    "error": "Telegram's global post search is a Premium "
                             "feature and this account does not have it. "
                             "Search inside a channel you have joined with "
                             "tg_search_in_chat, which is free, or use "
                             "tab='channels' to find channels by name.",
                    "seconds": round(time.time() - t0, 2)}
            # The Posts tab returns MESSAGES, not chats, so it is read with the
            # message parser rather than the chat-row one.
            found: dict[str, dict] = {}
            order_p: list[str] = []
            barren_p = 0
            for _ in range(max_swipes + 1):
                fresh = 0
                for m in assemble_messages(_read()):
                    key = _msg_key(m)
                    if key not in found:
                        found[key] = m
                        order_p.append(key)
                        fresh += 1
                barren_p = 0 if fresh else barren_p + 1
                if len(found) >= max_results or barren_p >= 2:
                    break
                _scroll_down(settle_s)
            msgs = [found[k] for k in order_p][:max_results]
            for m in msgs:
                m.pop("c", None)
            _to_chat_list(4)
            return {"query": query, "tab": tab, "collected": len(msgs),
                    "messages": msgs, "seconds": round(time.time() - t0, 2)}

        hits: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        for _ in range(max_swipes + 1):
            fresh = 0
            for h in assemble_hits(_read()):
                key = h["username"].lower()
                if key not in hits:
                    hits[key] = h
                    order.append(key)
                    fresh += 1
                elif h.get("subscribers") and not hits[key].get("subscribers"):
                    hits[key]["subscribers"] = h["subscribers"]
            barren = 0 if fresh else barren + 1
            if len(hits) >= max_results or barren >= 2:
                break
            _scroll_down(settle_s)
        results = [hits[k] for k in order][:max_results]
        for r in results:
            r.pop("c", None)
        # Close the search overlay. A tool that leaves it open hands the next
        # one a screen full of strangers' channels dressed as the chat list.
        _to_chat_list(4)
        out = {"query": query, "tab": tab or "chats",
               "collected": len(results), "results": results,
               "seconds": round(time.time() - t0, 2)}
        if typed.get("dropped_non_ascii"):
            out["typed"] = typed
        return out

    @mcp.tool(
        description=(
            "Open a chat's profile and read it: title, member/subscriber count, "
            "the full @username from the invite link, and the description. This "
            "is how you resolve a handle that a search hit truncated."
        )
    )
    def tg_info(chat: str = "", settle_s: float = 2.5) -> dict:
        if chat:
            _, err = _open_guard(chat)
            if err:
                return err
        els = _read()
        header = _header(els)
        hdr_node = next((e for e in els
                         if "\n" in (e.desc or "") and e.bounds[1] < 400), None)
        if hdr_node is None:
            return {"error": "no chat header on screen; open a chat first",
                    "foreground": dev.foreground()}
        _tap(hdr_node)
        time.sleep(settle_s)
        els = _read()

        link, username = None, None
        for e in els:
            m = re.search(r"t\.me/([\w\d_+]+)", (e.text or "").strip())
            if m and link is None:
                link = "t.me/" + m.group(1)
                if not m.group(1).startswith("+"):
                    username = "@" + m.group(1)
        # The description sits in a plain TextView below the link block; take
        # the longest body-looking string rather than guessing its position.
        bodies = [(e.text or "").strip() for e in els
                  if e.cls == "TextView" and len((e.text or "").strip()) > 40]
        desc = max(bodies, key=len) if bodies else None
        return {
            "title": header.get("title"),
            "subscribers": header.get("members"),
            "subtitle": header.get("subtitle"),
            "username": username,
            "invite_link": link,
            "description": desc,
            "joined": _find(els, "join", exact=True) is None,
            "on_profile_screen": True,
        }


def register_membership(mcp) -> None:
    """Joining, leaving and muting. Leaving is destructive enough to gate."""

    @mcp.tool(
        description=(
            "Join a public channel or group. Opens it by handle or link, taps "
            "Join, and verifies the button is gone before reporting success. "
            "Channels that vet members turn Join into a join REQUEST - that "
            "comes back as pending=true, not joined."
        )
    )
    def tg_join(chat: str, wait_s: float = 4.5, settle_s: float = 3.0) -> dict:
        t0 = time.time()
        _, err = _open_guard(chat, wait_s)
        if err:
            return {"chat": chat, "joined": False, **err}
        els = _read()
        header = _header(els)
        if not header.get("title"):
            return {"chat": chat, "joined": False,
                    "error": "chat opened but has no title bar - not a chat "
                             "screen",
                    "foreground": dev.foreground()}
        btn = _find(els, "join", exact=True) or _find(els, "request to join")
        if btn is None:
            return {"chat": chat, "title": header.get("title"),
                    "subscribers": header.get("members"),
                    "joined": True, "already_joined": True,
                    "seconds": round(time.time() - t0, 2)}
        asks_request = "request" in ((btn.text or "") + (btn.desc or "")).lower()
        # B2: joining is the Telegram write and the ban vector (ledger tg_join).
        from ...policy import writes as wr
        serial = dev.default_serial()
        decision = wr.gate_action("tg_join", "tg", serial, "tg.join")
        if not decision.allowed:
            return {"chat": chat, "title": header.get("title"), "joined": False,
                    "error": "write refused", "write": decision.to_dict()}
        _tap(btn)
        write_warning = wr.commit(decision, target=chat, serial=serial)
        time.sleep(settle_s)
        els = _read()
        still = _find(els, "join", exact=True)
        pending = asks_request or bool(_find(els, "request sent")) or \
            bool(_find(els, "cancel request"))
        return {
            "chat": chat,
            "title": header.get("title"),
            "subscribers": header.get("members"),
            "joined": still is None and not pending,
            "pending": pending,
            "seconds": round(time.time() - t0, 2),
            "note": ("Join is still on screen - the tap did not take, or the "
                     "channel refused it.") if still is not None else None,
            "write": decision.to_dict(),
            **({"write_warning": write_warning} if write_warning else {}),
        }

    @mcp.tool(
        description=(
            "Leave a channel or group. Opens it, uses the overflow menu, and "
            "confirms the dialog. Effectively irreversible for a private "
            "channel - rejoining one needs a fresh invite."
        ),
        dangerous=True,
    )
    def tg_leave(chat: str = "", settle_s: float = 2.0) -> dict:
        if chat:
            _, err = _open_guard(chat)
            if err:
                return err
        els = _read()
        header = _header(els)
        more = _find(els, "more options")
        if more is None:
            return {"error": "no overflow menu on screen; open the chat first"}
        _tap(more)
        time.sleep(settle_s)
        els = _read()
        item = (_find(els, "leave channel") or _find(els, "leave group")
                or _find(els, "delete and exit"))
        if item is None:
            _key("KEYCODE_BACK")
            return {"error": "no leave item in the overflow menu",
                    "menu": [(e.text or e.desc) for e in els
                             if (e.text or e.desc)][:20]}
        _tap(item)
        time.sleep(settle_s)
        els = _read()
        confirm = (_find(els, "leave channel") or _find(els, "leave group")
                   or _find(els, "ok", exact=True))
        if confirm is not None:
            _tap(confirm)
            time.sleep(settle_s)
        return {"left": header.get("title") or chat,
                "still_in_telegram": dev.foreground().get("package") == TG_PKG}

    @mcp.tool(
        description=(
            "Mute or unmute a chat. Muting is how you join a dozen noisy "
            "signals channels without burying the notification shade."
        )
    )
    def tg_mute(chat: str = "", mute: bool = True,
                settle_s: float = 2.0) -> dict:
        if chat:
            _, err = _open_guard(chat)
            if err:
                return err
        els = _read()
        want = "mute" if mute else "unmute"
        btn = _find(els, want, exact=True)
        if btn is None:
            more = _find(els, "more options")
            if more is None:
                return {"error": "no mute control and no overflow menu"}
            _tap(more)
            time.sleep(settle_s)
            els = _read()
            btn = _find(els, want, exact=True)
        if btn is None:
            _key("KEYCODE_BACK")
            return {"error": f"no '{want}' control found",
                    "menu": [(e.text or e.desc) for e in els
                             if (e.text or e.desc)][:20]}
        _tap(btn)
        time.sleep(settle_s)
        return {"muted": mute, "chat": chat or _header(_read()).get("title")}


def register_outbound(mcp) -> None:
    """Everything that reaches other people. Gated in agent.py as OUTBOUND."""

    @mcp.tool(
        description=(
            "Send a message to a chat. Give `chat` to open one first, or omit "
            "it to send into the chat already open. Fails cleanly when the "
            "chat has no composer - a broadcast channel you do not run has "
            "none. Verifies the message by finding its outgoing bubble."
        )
    )
    def tg_send(text: str, chat: str = "", settle_s: float = 2.5) -> dict:
        t0 = time.time()
        if chat:
            _, err = _open_guard(chat)
            if err:
                return {"sent": False, **err}
        els = _read()
        box = next((e for e in els if e.cls == "EditText"), None)
        if box is None:
            return {"sent": False,
                    "error": "no message box in this chat. Broadcast channels "
                             "you have not joined, or that disallow posting, "
                             "have no composer.",
                    "header": _header(els)}
        _tap(box)
        time.sleep(1.0)
        typed = _type(text)
        time.sleep(1.0)
        send = _find(_read(), "send", exact=True)
        if send is None:
            return {"sent": False, "error": "no Send button after typing",
                    **typed}
        at = _tap_send(send)
        time.sleep(settle_s)
        wanted = (typed.get("typed") or "")[:40].strip().lower()
        sent = any(m.get("outgoing") and wanted
                   and wanted in (m.get("text") or "").lower()
                   for m in assemble_messages(_read()))
        return {"sent": sent, "chat": chat or _header(els).get("title"),
                "tapped": at, "seconds": round(time.time() - t0, 2), **typed,
                "note": None if sent else
                        "no matching outgoing bubble appeared; the send may "
                        "have failed, or the chat may be slow to update"}

    @mcp.tool(
        description=(
            "Reply to a specific message. Pick the target with `match` (a "
            "substring of its text) or `i` (its index among the messages "
            "visible on screen); the newest visible message is used if neither "
            "is given. Long-presses the bubble, taps Reply, types and sends."
        )
    )
    def tg_reply(text: str, match: str = "", i: Optional[int] = None,
                 chat: str = "", settle_s: float = 2.0) -> dict:
        t0 = time.time()
        if chat:
            _, err = _open_guard(chat)
            if err:
                return {"sent": False, **err}
        msgs = assemble_messages(_read())
        if not msgs:
            return {"sent": False, "error": "no messages on screen"}
        if match:
            needle = match.strip().lower()
            target = next((m for m in msgs
                           if needle in (m.get("text") or "").lower()), None)
            if target is None:
                return {"sent": False,
                        "error": f"no visible message contains {match!r}",
                        "visible": [(m.get("text") or "")[:60] for m in msgs]}
        elif i is not None:
            if not (0 <= i < len(msgs)):
                return {"sent": False,
                        "error": f"index {i} out of range (0..{len(msgs) - 1})"}
            target = msgs[i]
        else:
            target = msgs[-1]

        x, y = target["c"]
        dev.shell(f"input swipe {x} {y} {x} {y} 700")
        time.sleep(settle_s)
        els = _read()
        reply = _find(els, "reply", exact=True)
        if reply is None:
            _key("KEYCODE_BACK")
            return {"sent": False,
                    "error": "no Reply action after long-press; this chat may "
                             "not allow replies",
                    "menu": [(e.text or e.desc) for e in els
                             if (e.text or e.desc)][:20]}
        _tap(reply)
        time.sleep(1.5)
        box = next((e for e in _read() if e.cls == "EditText"), None)
        if box is None:
            return {"sent": False, "error": "no composer after tapping Reply"}
        _tap(box)
        time.sleep(1.0)
        typed = _type(text)
        time.sleep(1.0)
        send = _find(_read(), "send", exact=True)
        if send is None:
            return {"sent": False, "error": "no Send button after typing",
                    **typed}
        _tap_send(send)
        time.sleep(settle_s)
        wanted = (typed.get("typed") or "")[:40].strip().lower()
        sent = any(m.get("outgoing") and wanted
                   and wanted in (m.get("text") or "").lower()
                   for m in assemble_messages(_read()))
        return {"sent": sent,
                "replied_to": (target.get("text") or "")[:120],
                "seconds": round(time.time() - t0, 2), **typed}


def register_search_in_chat(mcp) -> None:

    @mcp.tool(
        description=(
            "Search inside one chat and return the matching messages. Opens the "
            "chat, uses its in-chat search and reads the result list - the way "
            "to pull every mention of a ticker out of a signals channel."
        )
    )
    def tg_search_in_chat(query: str, chat: str = "", max_results: int = 20,
                          max_swipes: int = 8, settle_s: float = 1.2) -> dict:
        from ...policy import reads
        gate = reads.acquire("tg_search", "tg", target=query[:60])
        if not gate.allowed:
            return reads.refusal(gate)
        reads.commit(gate, target=query[:60])
        t0 = time.time()
        if chat:
            _, err = _open_guard(chat)
            if err:
                return err
        els = _read()
        header = _header(els)
        btn = _find(els, "search", exact=True)
        if btn is None:
            return {"error": "no in-chat search control on screen",
                    "header": header}
        _tap(btn)
        time.sleep(1.5)
        box = next((e for e in _read() if e.cls == "EditText"), None)
        if box is None:
            return {"error": "no search box appeared"}
        _tap(box)
        time.sleep(1.0)
        typed = _type(query)
        time.sleep(2.5)
        _key("KEYCODE_ENTER", 2.5)

        merged: dict[str, dict] = {}
        order: list[str] = []
        barren = 0
        for _ in range(max_swipes + 1):
            fresh = 0
            for m in assemble_messages(_read()):
                key = _msg_key(m)
                if key not in merged:
                    merged[key] = m
                    order.append(key)
                    fresh += 1
            barren = 0 if fresh else barren + 1
            if len(merged) >= max_results or barren >= 2:
                break
            _scroll_up(settle_s)
        hits = [merged[k] for k in order][:max_results]
        for m in hits:
            m.pop("c", None)
        out = {"query": query, "chat": chat or header.get("title"),
               "collected": len(hits), "messages": hits,
               "seconds": round(time.time() - t0, 2)}
        if typed.get("dropped_non_ascii"):
            out["typed"] = typed
        return out
