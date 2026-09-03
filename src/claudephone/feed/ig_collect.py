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
            prime: bool = True) -> dict:
    """Walk n reels and record their link, author, description and engagement.

    Ads and interstitials are counted but never opened. Reels whose description
    has not generated are recorded as such rather than dropped - how often that
    happens is itself a finding about how much of the feed is legible to us.
    """
    ig.ensure_reels(serial)
    rows, ads, nodesc = [], 0, 0
    linked, mismatches = 0, 0
    t0 = time.time()

    tail = lk.ClipTail(serial).start() if links else None
    item = None
    try:
        if tail and prime:
            # A sheet left open by whatever ran last swallows the priming swipe:
            # nothing scrolls, no id is logged, and the first reel comes back
            # linkless for a reason that has nothing to do with the feed.
            ig.close_sheet(serial)
            ig.next_reel(serial)
            item = tail.wait()

        for i in range(n):
            nodes = _nodes(_raw_xml(serial))
            ad = ig.is_ad(serial, nodes=nodes)

            # The logged id is a second, independent opinion on what this slot
            # is: an ad's id carries no owner. It is not a tie-break, it is a
            # catch - on the first live pass it caught an ad the screen missed,
            # a branded-content reel carrying none of the "sponsored" strings
            # `is_ad` looks for, whose sheet turned out to offer "Report ad".
            # EITHER witness is enough to leave a slot alone, because the cost
            # of opening an ad's sheet is real and the cost of skipping a reel
            # is one row.
            log_ad = bool(item) and item["kind"] == "ad"
            is_ad_slot = ad["is_ad"] or log_ad
            row = {"i": i, "kind": "ad" if is_ad_slot else "reel"}
            if is_ad_slot:
                row["ad_seen_by"] = ("both" if ad["is_ad"] and log_ad
                                     else "log" if log_ad else "screen")
            if item:
                row["feed_pos"] = item["pos"]
                row["url"] = item["url"]
                if item["kind"] == "reel":
                    row["shortcode"] = item["shortcode"]
                    row["media_pk"] = item["media_pk"]
                    row["owner_id"] = item["owner_id"]
                    linked += 1
                else:
                    row["ad_id"] = item["ad_id"]
                if log_ad != ad["is_ad"]:
                    mismatches += 1
                    row["url_note"] = ("log says %s, screen says %s"
                                       % (item["kind"],
                                          "ad" if ad["is_ad"] else "reel"))
            elif links:
                row["url"] = None
                row["url_note"] = ("no clipsItemId logged - Instagram may have "
                                   "renamed the line; see feed.ig_links")

            if is_ad_slot:
                ads += 1
                row["markers"] = ad["markers"]
                rows.append(row)
                ig.next_reel(serial)
                item = tail.wait() if tail else None
                continue

            info = ig.reel_info(serial, nodes=nodes)
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
                ig.next_reel(serial)
                item = tail.wait() if tail else None
                continue

            desc = sheet.get("description")
            if not desc:
                nodesc += 1
            # A collab reel names both accounts in one label ("a and b"), so a
            # profile URL built from it blindly is a link to nothing.
            author = info.get("author")
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
            for _ in range(2):
                if ig.close_sheet(serial).get("closed"):
                    break
            ig.next_reel(serial)
            item = tail.wait() if tail else None
    finally:
        if tail:
            tail.stop()

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
        "requested": n, "reels": len(described) + nodesc, "ads": ads,
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
