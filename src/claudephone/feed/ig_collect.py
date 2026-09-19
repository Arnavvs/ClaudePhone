"""Collect what Instagram is actually serving, without changing anything.

Read-only by construction: it opens each reel's More sheet to read Meta's
AI-generated description, then closes it with BACK. It never taps a sheet row -
Interested, Not interested, Save and Report all write to the account.

Each reel is recorded with its public permalink, read from Instagram's own
logcat line as the video is swapped in (see `ig_links` for how, and for the
clipboard dead end it replaces). The link costs no taps and no extra seconds -
only a `logcat` reader running alongside the pass. Where it fails it says so:
`url` is None and `url_note` says why, rather than a missing key that reads
like a reel with no link.

Because the id is logged on the swipe ONTO a reel, the reel already on screen
when a pass starts has none. The pass therefore advances one reel before
recording anything, so that all `n` rows carry a link; `prime=False` keeps that
first reel at the cost of a row without one.

A WALK IS NOT A LOOP OVER SWIPES. That was this module's first assumption, and
a run on 2026-09-05 asked for 45 reels, returned 3, and called the other 42
slots ads. The feed was fine. The phone was parked on the full-screen
"Instagram is better with friends" follow carousel, which the old advance swipe
does not move (see `instagram.ADVANCE_SWIPE`), so the same frozen screen was
re-read 42 times - and because that card carries "Suggested for you", every
re-read scored as advertising. Three defences follow from that, and all three
live here now:

  * The screen is FINGERPRINTED every slot. An unchanged fingerprint after an
    advance means the advance failed, and `instagram.unstick` escalates: edge
    swipe, second edge swipe, re-enter the Reels tab. `stuck_events` reports
    how often that happened, because a pass that needed recovery is a different
    fact about the session than one that did not.
  * AN INTERSTITIAL IS NOT AN AD. A slot with no author is not a reel at all
    and is counted on its own line. "This feed is 93% advertising" is a claim
    about Instagram; "the phone showed a follow prompt" is a claim about the
    session, and the first version could not tell them apart.
  * `n` COUNTS REELS, NOT SLOTS. Ads, interstitials and repeats are skipped
    without spending the budget, so `n=45` means 45 reels - bounded by
    `max_slots` so a feed made of nothing else still terminates. Both numbers
    come back, so a caller can always see the difference.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import ig_links as lk
from . import instagram as ig
from . import read as rd
from .x import JOURNAL, _nodes, _raw_xml


def collect(n: int = 20, out_dir: str = "", serial: str = "",
            queries: tuple = (), links: bool = True,
            prime: bool = True, max_slots: int = 0) -> dict:
    """Walk the feed until `n` REELS are recorded, with link, author and description.

    `n` counts reels captured, not slots visited: ads, interstitials and reels
    the feed has already shown are skipped without spending it. `max_slots`
    (default `3 * n`) bounds the walk anyway, so a feed that is all advertising
    ends rather than running forever - the returned `reels`, `slots`, `ads` and
    `interstitials` say which happened.

    Reels whose description has not generated are recorded as such rather than
    dropped - how often that happens is itself a finding about how much of the
    feed is legible to us.
    """
    ig.ensure_reels(serial)
    max_slots = max_slots or max(n * 3, n + 5)
    rows = []
    ads = interstitials = repeats = nodesc = stuck_events = 0
    linked, mismatches = 0, 0
    slots, reels = 0, 0
    seen: set = set()
    stopped = None
    t0 = time.time()

    tail = lk.ClipTail(serial).start() if links else None
    item = None
    last_fp = None

    def advance():
        """Swipe to the next slot and pick up the id logged as it lands."""
        ig.next_reel(serial)
        return tail.wait() if tail else None

    try:
        if tail and prime:
            # A sheet left open by whatever ran last swallows the priming swipe:
            # nothing scrolls, no id is logged, and the first reel comes back
            # linkless for a reason that has nothing to do with the feed.
            ig.close_sheet(serial)
            item = advance()

        hard_stuck = 0
        while reels < n and slots < max_slots:
            slots += 1
            nodes = _nodes(_raw_xml(serial))
            fp = ig.screen_id(nodes=nodes)

            # Did the last advance actually land? Nothing else in this loop can
            # tell: a frozen screen reads as a perfectly ordinary slot, over and
            # over, and the pass reports a feed it never saw.
            if fp == last_fp:
                stuck_events += 1
                rec = ig.unstick(serial, before=fp)
                nodes = _nodes(_raw_xml(serial))
                fp = ig.screen_id(nodes=nodes)
                if tail:
                    item = tail.wait(timeout_s=1.5) or item
                rows.append({"i": len(rows), "slot": slots, "kind": "stuck",
                             "recovered": rec["moved"],
                             "recovery": rec["tried"],
                             "reentered_reels": rec["reentered"]})
                if not rec["moved"]:
                    hard_stuck += 1
                    # Once is bad luck; twice is a screen nothing here can move
                    # - an update dialog, a logged-out session. Stop and say so
                    # rather than spend the rest of the budget on it.
                    if hard_stuck >= 2:
                        stopped = ("feed would not advance after %d recovery "
                                   "attempts - stopped at slot %d"
                                   % (hard_stuck, slots))
                        break
                    continue
                hard_stuck = 0
            last_fp = fp

            info = ig.reel_info(serial, nodes=nodes)
            author = info.get("author")
            slot_kind = ig.is_ad(serial, nodes=nodes, author=author)

            # The logged id is a second, independent opinion on what this slot
            # is: an ad's id carries no owner. It is not a tie-break, it is a
            # catch - on the first live pass it caught an ad the screen missed,
            # a branded-content reel carrying none of the "sponsored" strings
            # `is_ad` looks for, whose sheet turned out to offer "Report ad".
            # EITHER witness is enough to leave a slot alone, because the cost
            # of opening an ad's sheet is real and the cost of skipping a reel
            # is one row. It cannot promote an interstitial to an ad, though:
            # with no author on screen the logged id belongs to a slot that is
            # no longer showing.
            log_ad = bool(item) and item["kind"] == "ad"
            kind = slot_kind["kind"]
            if kind == "reel" and log_ad:
                kind = "ad"

            row = {"i": len(rows), "slot": slots, "kind": kind}

            # No author, no reel - and no link either: the logged id belongs to
            # whatever the feed showed BEFORE this card, so attaching it here
            # would invent a permalink for a follow prompt.
            if kind == "interstitial":
                interstitials += 1
                row["markers"] = slot_kind["markers"]
                row["note"] = ("no author on screen - a follow suggestion, an "
                               "'all caught up' card or a login nag, not a reel")
                rows.append(row)
                item = advance()
                continue

            if kind == "ad":
                row["ad_seen_by"] = ("both" if slot_kind["is_ad"] and log_ad
                                     else "log" if log_ad else "screen")
            if item:
                row["feed_pos"] = item["pos"]
                row["url"] = item["url"]
                if item["kind"] == "reel":
                    row["shortcode"] = item["shortcode"]
                    row["media_pk"] = item["media_pk"]
                    row["owner_id"] = item["owner_id"]
                else:
                    row["ad_id"] = item["ad_id"]
                if log_ad != slot_kind["is_ad"]:
                    mismatches += 1
                    row["url_note"] = ("log says %s, screen says %s"
                                       % (item["kind"], slot_kind["kind"]))
            elif links:
                row["url"] = None
                row["url_note"] = ("no clipsItemId logged - Instagram may have "
                                   "renamed the line; see feed.ig_links")

            if kind == "ad":
                ads += 1
                row["markers"] = slot_kind["markers"]
                rows.append(row)
                item = advance()
                continue

            # The feed loops back on itself, and a recovery swipe can land back
            # on a reel already recorded. A second copy is not a second reel.
            sc = row.get("shortcode")
            if sc and sc in seen:
                repeats += 1
                row["kind"] = "repeat"
                rows.append(row)
                item = advance()
                continue

            sheet = ig.open_more(serial)
            # Last net, for an ad neither the screen nor the log named: the
            # sheet itself says what it belongs to. Nothing has been tapped, so
            # this costs only the row.
            opts = " ".join(sheet.get("options") or []).lower()
            if "report ad" in opts or "about this ad" in opts:
                ads += 1
                row["kind"] = "ad"
                row["ad_seen_by"] = "sheet"
                rows.append(row)
                for _ in range(2):
                    if ig.close_sheet(serial).get("closed"):
                        break
                item = advance()
                continue

            desc = sheet.get("description")
            if not desc:
                nodesc += 1
            # A collab reel names both accounts in one label ("a and b"), so a
            # profile URL built from it blindly is a link to nothing.
            single = author if author and " " not in author else None
            row.update({
                "author": author,
                "profile": ("https://instagram.com/%s" % single)
                           if single else None,
                "caption": info.get("caption"),
                "audio": info.get("audio"),
                "badge": info.get("badge"),
                "social_proof": info.get("social_proof"),
                "likes": info.get("likes"), "comments": info.get("comments"),
                "reposts": info.get("reposts"), "saves": info.get("saves"),
                "meta_description": desc,
                "desc_waited_s": sheet.get("waited_s"),
                "desc_note": sheet.get("note"),
                "options": sheet.get("options"),
            })
            rows.append(row)
            reels += 1
            if sc:
                seen.add(sc)
                linked += 1
            for _ in range(2):
                if ig.close_sheet(serial).get("closed"):
                    break
            item = advance()
    finally:
        if tail:
            tail.stop()

    if stopped is None and reels < n:
        stopped = ("slot budget spent: %d slots for %d reels (ads %d, "
                   "interstitials %d, repeats %d)"
                   % (slots, reels, ads, interstitials, repeats))

    # Optional: score every description against each query in one batch, so a
    # collection doubles as a labelled set for choosing thresholds later.
    # Say why a pass carries no scores. A run once came back silently unscored
    # because the phone's scorer was up but `adb forward tcp:8765 tcp:8765` was
    # not, and a missing key looks exactly like a feed nobody asked to score.
    described = [r for r in rows if r.get("meta_description")]
    scoring = None
    if not queries:
        scoring = "no queries given"
    elif not described:
        scoring = "nothing to score"
    elif not rd.relevance_available(serial=serial):
        scoring = ("scorer unreachable at %s - is it running in Termux, and is "
                   "`adb forward tcp:8765 tcp:8765` set up?" % rd.RELEVANCE_URL)
    else:
        texts = [r["meta_description"] for r in described]
        for q in queries:
            scores = rd.relevance(q, texts) or []
            for r, sc in zip(described, scores):
                r.setdefault("scores", {})[q] = sc

    out = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": "instagram", "surface": "reels",
        # `requested` and `reels` both count REELS; `slots` is how much feed
        # that took. slots == reels + ads + interstitials + repeats + stuck
        # slots, and a wide gap between the first two is itself the finding.
        "requested": n, "reels": reels, "complete": reels >= n,
        "slots": slots, "max_slots": max_slots,
        "ads": ads, "interstitials": interstitials, "repeats": repeats,
        "stuck_events": stuck_events, "stopped_early": stopped,
        "no_description": nodesc,
        "linked": linked, "link_mismatches": mismatches,
        "described": len(described), "scoring": scoring,
        "seconds": round(time.time() - t0, 1),
        "rows": rows,
    }
    d = Path(out_dir) if out_dir else JOURNAL.parent
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("ig-reels-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    out["path"] = str(p)
    return out
