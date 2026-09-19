"""Telegram parser tests. No phone, no API key.

Every string below was captured verbatim off org.telegram.messenger 12.10.1 on
the target device. That matters: Telegram publishes no resource-ids, so the
whole app module is a parser over accessibility LABELS, and the only way these
tests are worth anything is if the labels are real rather than invented.

    python tests/test_telegram.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from claudephone import ui as uix                      # noqa: E402
from claudephone.tools.apps import telegram as tg      # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + label)
    else:
        FAIL += 1
        print("  FAIL " + label + ("  -> " + detail if detail else ""))


class E:
    """The bits of ui.Element the assemblers touch."""

    def __init__(self, text="", desc="", cls="ViewGroup", bounds=(0, 0, 100, 50),
                 pkg=tg.TG_PKG):
        self.text, self.desc, self.cls = text, desc, cls
        self.bounds, self.pkg = bounds, pkg
        self.rid = self.anchor = ""

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)


# A signed channel post with an inline reaction list and a view count.
SIGNED = ("....Nifty support Levels\n\n23610-23650\n\nlet's see how it reacts\n"
          "Received at Kapil Verma (SEBI REGISTERED RA), 14:40"
          "Reactions: ❤ 21, \U0001f44d 8, \U0001f525 6\n\nViewed 5302 times")
# The other reaction rendering: prose, single emoji, count may be absent.
PROSE = ("Short Term Swing \n\nIndo Rama \n70.8 to 78\n"
         "Received at Kapil Verma (SEBI REGISTERED RA), 15:03\n"
         "13 people reacted with ❤\nViewed 4360 times")
FORWARDED = ("Forwarded from A SG Options Training Group\nNIFTY \n\nHIT TGT\n"
             "Received at Kapil Verma, 15:11\nViewed 3713 times")
OUTGOING = "Claudephone telegram tool test\nSent at 16:41, Seen"
UNSIGNED = ("480 TO 550 #SENSEX 74000 CE \nBOOK PARTIAL PROFIT\n"
            "Received at 09:40\nViewed 1512 times")
VOICE = ("Voice message, 18 seconds, Not played, 372.8 KB\n"
         "100% TRUST WORK ....\nReceived at 09:12")


def test_message_parsing():
    print("message parsing")
    m = tg.parse_message(SIGNED)
    check("signed author", m["author"] == "Kapil Verma (SEBI REGISTERED RA)",
          repr(m["author"]))
    check("time", m["time"] == "14:40")
    check("inbound", m["outgoing"] is False)
    check("views", m["views"] == 5302, repr(m["views"]))
    check("inline reactions", m["reactions"] == {"❤": 21, "\U0001f44d": 8,
                                                 "\U0001f525": 6},
          repr(m["reactions"]))
    check("body keeps its newlines", m["text"].startswith("....Nifty support"))
    check("body stops at the landmark", "Received at" not in m["text"])

    m = tg.parse_message(PROSE)
    check("prose reactions", m["reactions"] == {"❤": 13},
          repr(m["reactions"]))

    m = tg.parse_message(FORWARDED)
    check("forward source", m["forwarded_from"] == "A SG Options Training Group",
          repr(m["forwarded_from"]))
    check("forward line is off the body", "Forwarded" not in m["text"])

    m = tg.parse_message(OUTGOING)
    check("outgoing direction", m["outgoing"] is True)
    check("seen flag", m["seen"] is True)
    check("outgoing has no views", m["views"] is None)

    m = tg.parse_message(UNSIGNED)
    check("unsigned post has no author", m["author"] is None, repr(m["author"]))

    m = tg.parse_message(VOICE)
    check("media kind and detail",
          m["media"] == "voice message (18 seconds, Not played, 372.8 KB)",
          repr(m["media"]))
    check("media caption survives", m["text"] == "100% TRUST WORK ....",
          repr(m["text"]))

    for junk in ("June 12", "September 8", "Global search", "", "Join",
                 "You joined this channel\nReceived at null"):
        check(f"not a message: {junk[:24]!r}", tg.parse_message(junk) is None)


def test_counts():
    print("counts")
    for raw, want in (("111.7K", 111700), ("323", 323), ("73 606", 73606),
                      ("6 177", 6177), ("1.2M", 1200000), ("", None),
                      ("nonsense", None)):
        check(f"parse_count({raw!r})", tg.parse_count(raw) == want,
              repr(tg.parse_count(raw)))


def test_assemble_and_quotes():
    print("assembly")
    # A reply quote renders as its own node INSIDE the bubble, and the tree
    # emits it after that bubble - so pairing by adjacency is wrong and
    # containment is right. The quote here sits inside the SECOND message.
    els = [
        E(text="June 12"),
        E(text="first\nReceived at 09:00", bounds=(0, 100, 1080, 300)),
        E(text="second\nReceived at 09:05", bounds=(0, 300, 1080, 600)),
        E(desc="Reply, Some Channel, quoted text", bounds=(50, 320, 900, 380)),
    ]
    msgs = tg.assemble_messages(els)
    check("two messages", len(msgs) == 2, str(len(msgs)))
    check("date folded in", msgs[0]["date"] == "June 12", repr(msgs[0]["date"]))
    check("quote lands on the containing bubble",
          msgs[0]["reply_to"] is None and msgs[1]["reply_to"] is not None)
    check("quote text", (msgs[1]["reply_to"] or {}).get("text") == "quoted text",
          repr(msgs[1]["reply_to"]))


def test_search_hits():
    print("search hits")
    hits = tg.assemble_hits([
        E(text="NFT Spotlight, @dkjghjkdsgkdhsgkhsdgk"),
        E(text="Kkkkk, @stockmarketbulls2026, 323 subscri…"),
        E(text="Saved Messages, @MeanIt_Bot, 73 606 users"),
        E(text="Global search"),
        E(text="just some words with no handle"),
    ])
    check("three hits", len(hits) == 3, str(len(hits)))
    check("handle", hits[0]["username"] == "@dkjghjkdsgkdhsgkhsdgk")
    check("elided row is flagged", hits[1]["truncated"] is True)
    check("space-grouped count", hits[2]["subscribers"] == 73606,
          repr(hits[2]["subscribers"]))
    check("raw label is kept", hits[1]["raw"].endswith("subscri…"))


def test_deeplink():
    print("deep links")
    cases = {
        "@STOCKGAINERSS": "tg://resolve?domain=STOCKGAINERSS",
        "STOCKGAINERSS": "tg://resolve?domain=STOCKGAINERSS",
        "https://t.me/s/abhayvarn": "tg://resolve?domain=abhayvarn",
        "t.me/+iJXDrzNRT3w1ZWE1": "https://t.me/+iJXDrzNRT3w1ZWE1",
        "t.me/joinchat/AAAA": "https://t.me/joinchat/AAAA",
        "tg://resolve?domain=x": "tg://resolve?domain=x",
    }
    for src, want in cases.items():
        check(f"{src} -> {want}", tg.deeplink(src) == want, tg.deeplink(src))


def test_label_cap():
    """A long post must keep the landmark that makes it a message at all."""
    print("label cap")
    long_body = "SIGNAL " * 80                     # ~560 chars
    label = long_body + "\nReceived at 09:40\nViewed 10 times"
    xml = ('<hierarchy><node text="' + label.replace("\n", "&#10;") +
           '" class="android.view.ViewGroup" package="' + tg.TG_PKG +
           '" bounds="[0,100][1080,400]" /></hierarchy>')
    short = uix.parse(xml)[0]
    full = uix.parse(xml, max_text=tg.MAX_LABEL)[0]
    check("default cap truncates", len(short.text) == 300, str(len(short.text)))
    check("default cap loses the message",
          tg.parse_message(short.text) is None)
    check("raised cap keeps it", tg.parse_message(full.text) is not None)


def test_chat_row_shapes():
    """Chat-list cells are found by rectangle: they publish no text at all."""
    print("chat rows")
    w, h = 1080, 2400
    tg._SIZE = (w, h)
    els = [
        E(cls="RecyclerView", bounds=(0, 0, w, h)),
        E(bounds=(0, 427, w, 638)),
        E(bounds=(0, 638, w, 849)),
        E(bounds=(0, 849, w, 1060)),
        E(text="Search Chats", cls="EditText", bounds=(33, 289, 1047, 409)),
        E(bounds=(0, 2115, w, 2326)),          # runs under the tab strip
        E(bounds=(400, 900, 700, 1000)),       # not full width
        E(text="labelled", bounds=(0, 1060, w, 1271)),
    ]
    rows = tg._chat_rows(els)
    check("three usable rows", len(rows) == 3, str(len(rows)))
    check("sorted top to bottom",
          [r.bounds[1] for r in rows] == [427, 638, 849])


def test_registered():
    print("registration")
    from claudephone.agent import OUTBOUND, build_registry
    reg = build_registry()
    names = {n for n, t in reg.tools.items() if t.pack == "telegram"}
    for want in ("tg_open", "tg_read", "tg_last", "tg_catchup", "tg_search",
                 "tg_info", "tg_join", "tg_leave", "tg_mute", "tg_send",
                 "tg_reply", "tg_chats", "tg_search_in_chat"):
        check("registered: " + want, want in names)
    check("telegram is not in the default packs",
          "telegram" not in reg.active_packs)
    check("tg_leave is dangerous", reg.tools["tg_leave"].dangerous)
    check("tg_send is outbound", "tg_send" in OUTBOUND)
    check("tg_reply is outbound", "tg_reply" in OUTBOUND)


if __name__ == "__main__":
    for fn in (test_message_parsing, test_counts, test_assemble_and_quotes,
               test_search_hits, test_deeplink, test_label_cap,
               test_chat_row_shapes, test_registered):
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
