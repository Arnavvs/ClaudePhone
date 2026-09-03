"""Run a feed-shaping campaign: search a topic, engage with it, then work the
main feed - repeated per keyword.

This is the account owner's own method, written down and automated. Their words,
2026-09-03: search 3-4 current keywords, spend time engaging with those tweets
(open, like, follow a couple of accounts), then return to the main feed and
scroll fast past anything off-topic while dwelling on anything on-topic.

It is the fastest lever available, and it is unambiguously a WRITE. Likes and
follows are scored actions in xai-org/x-algorithm, so this steers the ranker
rather than observing it. Two consequences kept deliberately visible:

  * Every run is journalled with its parameters and its counts, so a later
    composition change can be attributed to a specific campaign rather than
    guessed at.
  * Do not run it against a timeline you are also using as a measurement
    baseline. It is the treatment.

Decisions are made against what is ON SCREEN, read through `feed.read.Reader`
at ~0.2-0.5 s per read. Classification here is a fast regex pre-filter, not
judgement: `read.TOPICS` is editable and deliberately crude, and an agent that
can read the returned text should overrule it.
"""

from __future__ import annotations

import random
import time
from typing import Optional

from .. import device as dev
from . import read as rd
from . import x as xf


def _swipe(serial: str, fast: bool = False) -> None:
    dur = random.randint(120, 200) if fast else random.randint(260, 420)
    dev.shell("input swipe 540 1700 540 800 %d" % dur, serial=serial)


def follow_visible(reader: rd.Reader, want: int = 2, topic: str = "football",
                   apply: bool = False, serial: str = "") -> dict:
    """Follow up to `want` accounts whose visible post matches `topic`.

    Follows from the post overflow rather than a profile page: it is one screen
    instead of three, and the menu already carries `Follow @handle`.
    """
    done, tried, plans = [], 0, []
    while len(done) < want and tried < want * 3:
        tried += 1
        posts = reader.posts()
        if not posts:
            _swipe(serial)
            time.sleep(0.6)
            continue
        if topic and topic not in (posts[0].get("topics") or []):
            _swipe(serial)
            time.sleep(0.6)
            continue

        menu = xf.post_options(0, serial=serial)
        if "error" in menu:
            _swipe(serial)
            continue
        hit = next((i for i in menu["items"]
                    if i["label"].lower().startswith("follow @")), None)
        if not hit:
            xf.close_sheet(serial)          # already followed, or no such row
            _swipe(serial)
            time.sleep(0.6)
            continue
        plans.append(hit["label"])
        if apply:
            xf._tap(*hit["center"], serial=serial, settle=1.2)
            xf._journal({"action": "follow", "label": hit["label"],
                         "topic": topic})
            done.append(hit["label"])
        else:
            xf.close_sheet(serial)
            done.append(hit["label"])       # counted as planned
        _swipe(serial)
        time.sleep(0.6)
    return {"followed" if apply else "would_follow": done, "considered": tried}


def work_topic(keyword: str, seconds: float = 90.0, likes: int = 6,
               follows: int = 2, topic: str = "football",
               apply: bool = False, serial: str = "", query: str = "") -> dict:
    """Search one keyword and engage with the results.

    Opens nothing it has not read: a post is only liked when the reader says it
    matches `topic`, so an off-topic result in a topic search does not get a
    positive signal by accident.
    """
    query = query or topic
    reader = rd.Reader(serial)
    out = {"keyword": keyword, "applied": apply,
           "scorer": "embedding" if rd.relevance_available() else "regex"}
    out["search"] = {k: v for k, v in
                     xf.search(keyword, tab="top", serial=serial).items()
                     if k != "handles"}
    if out["search"].get("error"):
        return out

    liked, seen, opened = 0, {}, 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        posts = reader.posts()
        for p in posts:
            seen.setdefault((p["handle"], (p["text"] or "")[:40]), p)
        # Judge by MEANING and pick the best post on screen, not merely the top
        # one. The regex could not tell "nuclear arsenals" from football, and
        # only ever considered posts[0] - so a relevant post sitting second was
        # scrolled past. `best_post` falls back to the regex if the phone is not
        # serving, so this degrades rather than breaking.
        idx = rd.best_post(posts, query)
        on_topic = idx is not None

        if on_topic and liked < likes:
            # OPEN the post rather than scrolling past it. Scrolling past a
            # relevant post sends nothing; opening it sends a click, real dwell
            # in the detail view, and the replies are more on-topic text. The
            # account owner's point, 2026-09-03, and it matches the ranker:
            # click, dwell and favorite are three separate scored actions.
            if apply:
                r = xf.engage_post(0, like_it=True, reply_scrolls=2,
                                   apply=True, serial=serial,
                                   expect=posts[idx].get("text") or "")
                if r.get("open", {}).get("opened"):
                    opened += 1
                    if r.get("like", {}).get("applied"):
                        liked += 1
                else:
                    # could not open (ad, or a tap that went nowhere) - do not
                    # burn the budget on it, just move on
                    time.sleep(random.uniform(0.4, 0.9))
            else:
                liked += 1
        else:
            time.sleep(random.uniform(0.3, 0.7))

        _swipe(serial, fast=not on_topic)
        time.sleep(0.4)

    out["read"] = rd.summarise(list(seen.values()))
    out["liked"] = liked
    out["opened"] = opened
    out["follow"] = follow_visible(reader, follows, topic, apply, serial)
    out["reader"] = reader.stats()
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def feed_pass(seconds: float = 60.0, topic: str = "football",
              avoid: str = "politics", apply: bool = False,
              serial: str = "", max_opens: int = 3, query: str = "") -> dict:
    """Work the main feed: linger on `topic`, scroll straight past `avoid`.

    The asymmetry is the whole point. Dwell is a positive signal and a fast
    scroll past is the absence of one, so a feed pass teaches the ranker with
    nothing but timing - no likes required.
    """
    query = query or topic
    reader = rd.Reader(serial)
    xf.ensure_home(serial)
    xf.switch_timeline("For you", serial=serial)
    # Go to the HEAD of the timeline. Landing where the last pass stopped means
    # reading the old ranking, not the response to what was just done.
    xf.scroll_to_top(serial)

    seen: dict = {}
    dwelt = skipped = opened = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        posts = reader.posts()
        for p in posts:
            seen.setdefault((p["handle"], (p["text"] or "")[:40]), p)
        idx = rd.best_post(posts, query)
        if idx is not None:
            # Same rule on the main feed: a relevant post that appears here is
            # worth opening, not just lingering on. This is the signal that
            # tells the ranker its guess landed.
            if apply and opened < max_opens:
                r = xf.engage_post(0, like_it=True, reply_scrolls=1,
                                   apply=True, serial=serial,
                                   expect=posts[idx].get("text") or "")
                if r.get("open", {}).get("opened"):
                    opened += 1
            else:
                time.sleep(random.uniform(2.0, 4.0))
            dwelt += 1
            _swipe(serial)
        else:
            if posts and avoid in (posts[0].get("topics") or []):
                skipped += 1
            time.sleep(random.uniform(0.15, 0.35))
            _swipe(serial, fast=True)
        time.sleep(0.3)

    return {"applied": apply, "dwelt_on_topic": dwelt,
            "scorer": "embedding" if rd.relevance_available() else "regex",
            "opened": opened, "scrolled_past": skipped,
            "read": rd.summarise(list(seen.values())),
            "reader": reader.stats(),
            "elapsed_s": round(time.time() - t0, 1)}


def run(keywords: list, topic: str = "football", avoid: str = "politics",
        topic_seconds: float = 90.0, feed_seconds: float = 45.0,
        likes: int = 6, follows: int = 2, apply: bool = False,
        serial: str = "", query: str = "") -> dict:
    """Full campaign: for each keyword, engage the topic then work the feed."""
    t0 = time.time()
    result = {"applied": apply, "keywords": keywords, "topic": topic,
              "rounds": []}
    if apply:
        xf._journal({"action": "campaign", "keywords": keywords,
                     "topic": topic, "likes": likes, "follows": follows})
    for kw in keywords:
        round_ = {"keyword": kw}
        round_["topic_work"] = work_topic(kw, topic_seconds, likes, follows,
                                          topic, apply, serial, query=query)
        round_["feed_pass"] = feed_pass(feed_seconds, topic, avoid, apply,
                                        serial, query=query)
        result["rounds"].append(round_)
    result["elapsed_s"] = round(time.time() - t0, 1)
    return result
