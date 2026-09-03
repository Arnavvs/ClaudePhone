"""Real-time feed reading: what is on screen right now, as structured posts.

Built because every feed-shaping decision needs to be made against what is
actually visible - "is this post football or politics" cannot be answered from a
tap coordinate. The agent needs to read before it acts, at a rate that keeps up
with scrolling.

The enabling measurement (RMX3395, USB, 2026-09-03):

    shell `uiautomator dump` + `cat`   2.47 s per read
    uiautomator2 `dump_hierarchy()`    0.21 s per read      <- 12x faster

So this module holds ONE persistent `uiautomator2` connection and reuses it,
rather than shelling out per read. 0.21 s is roughly five reads a second, which
is fast enough to read a feed while scrolling it. `feed/x.py` still uses the
shell path for control actions, where a 2 s cost is irrelevant next to the
settle time after a tap.

A second reason to prefer u2 here: the shell dump returns a COMPRESSED tree
(~8 KB for a screen where u2 returns ~25 KB), so it drops nodes. More tree is
strictly better for reading.

Nothing in this module writes to the device. It is the instrument, not the
treatment.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import Optional

from .. import device as dev
from .. import ui as uix

X_PKG = "com.twitter.android"

# Deliberately broad and deliberately editable. These are a starting point for
# sorting a feed, not a claim about what any post is really about - a human
# should be able to read the list and disagree with it.
TOPICS: dict[str, re.Pattern] = {
    "football": re.compile(
        r"football|soccer|barcelona|barca|real madrid|messi|ronaldo|yamal|"
        r"mbappe|haaland|premier league|la ?liga|uefa|fifa|champions league|"
        r"arsenal|chelsea|liverpool|man utd|manchester|tottenham|psg|bayern|"
        r"juventus|inter milan|striker|midfielder|goalkeeper|transfer window|"
        r"matchday|\bgoal\b|\bgoals\b|penalty|offside|\bfc\b|\bcf\b", re.I),
    "politics": re.compile(
        r"modi|bjp|congress|rahul gandhi|aap\b|kejriwal|jaishankar|"
        r"\bpolitic|election|parliament|lok sabha|rajya sabha|minister|"
        r"government|govt\b|vote|voter|democracy|protest|policy|"
        r"trump|biden|senate|republican|democrat|"
        r"[ऀ-ॿ]", re.I),
    "bollywood": re.compile(
        # Current storylines first - a live film title matches far more posts
        # than the word "bollywood" ever does. Refresh these per campaign.
        r"toxic|dhurandhar|bhooth bangla|border 2|"
        r"bollywood|tollywood|box office|teaser|trailer|"
        r"salman|shah ?rukh|srk\b|aamir|ranbir|deepika|alia|katrina|"
        r"kareena|priyanka|vicky|kartik|ranveer|yash\b|prabhas|"
        r"karan johar|bhansali|film ?fare|kapoor|khan\b|"
        r"first day|opening day|crore|advance booking", re.I),
    "ad": None,          # filled from the post's own is_ad flag
}


class Reader:
    """A persistent, low-latency view of what X is showing.

    Hold one of these for the length of a session: the u2 connection is the
    expensive part, and re-creating it per read throws away the speed this
    module exists for.
    """

    def __init__(self, serial: str = ""):
        self.serial = serial
        self._d = None
        self.reads = 0
        self.total_s = 0.0

    @property
    def d(self):
        if self._d is None:
            self._d = dev.u2(self.serial)
        return self._d

    # ---------------- core read ----------------

    def raw(self) -> str:
        t0 = time.time()
        xml = self.d.dump_hierarchy()
        self.reads += 1
        self.total_s += time.time() - t0
        return xml

    def posts(self, classify: bool = True) -> list[dict]:
        """Every post currently on screen, in document order.

        Reuses `assemble_tweets` so there is one tweet-boundary rule in the
        project rather than two that drift apart.
        """
        from ..tools.apps.twitter import assemble_tweets

        xml = self.raw()
        out = []
        for t in assemble_tweets(uix.parse(xml)):
            p = {
                "handle": t.get("handle"),
                "name": t.get("name"),
                "text": (t.get("text") or "").strip(),
                "age": t.get("age"),
                "is_ad": bool(t.get("is_ad")),
                "metrics": t.get("metrics") or {},
            }
            if classify:
                p["topics"] = tag(p)
            out.append(p)
        return out

    def screen(self) -> dict:
        """Posts plus the context needed to decide what to do with them."""
        from . import x as xf

        xml = self.raw()
        nodes = xf._nodes(xml)
        fg = dev.foreground(serial=self.serial)
        return {
            "package": fg.get("package"),
            "in_x": fg.get("package") == X_PKG,
            "surface": xf.surface(self.serial, nodes=nodes),
            "labels": [n["label"] for n in nodes if n["label"]][:60],
            "nodes": nodes,
        }

    def stats(self) -> dict:
        return {"reads": self.reads,
                "avg_s": round(self.total_s / self.reads, 3) if self.reads else None}


def tag(post: dict) -> list[str]:
    """Topic labels for one post. Cheap, transparent, and overridable."""
    hay = "%s %s" % (post.get("text") or "", post.get("handle") or "")
    out = [name for name, pat in TOPICS.items() if pat and pat.search(hay)]
    if post.get("is_ad"):
        out.append("ad")
    return out


def summarise(posts: list[dict]) -> dict:
    """Composition of what is on screen - the number that decides an action."""
    if not posts:
        return {"n": 0}
    counts: dict[str, int] = {}
    for p in posts:
        for t in p.get("topics") or ["untagged"]:
            counts[t] = counts.get(t, 0) + 1
    return {"n": len(posts), "counts": counts,
            "football_share": round(counts.get("football", 0) / len(posts), 2),
            "politics_share": round(counts.get("politics", 0) / len(posts), 2)}


# --------------------------------------------------------------------------
# embedding relevance, served from the phone
# --------------------------------------------------------------------------
#
# `tag()` below is a keyword pre-filter and its failures are structural, not
# tuning: it called "Started & runs 37signals, makers of Basecamp" POLITICS
# because `mp\b` matches inside "Basecamp", and "Nuclear-armed states ramped up
# arsenals" FOOTBALL because of `arsenal`. A keyword has no notion of meaning.
#
# So relevance is now a similarity against the QUERY, computed by
# `phone/relevance_server.py` running in Termux on the device itself
# (potion-base-8M static embeddings, 256-dim, ~1 ms for a screenful). Measured
# on the same two failures:
#
#     text                                       football  bollywood  politics
#     Nuclear-armed states ramped up arsenals       0.038     -0.092     0.150
#     ... makers of Basecamp and HEY                0.005     -0.035    -0.004
#     Arsenal beat Chelsea 2-0                      0.291      0.092    -0.003
#     Toxic advance booking record for Yash        -0.039      0.196    -0.008
#     Rahul Gandhi on vote chori in Lok Sabha      -0.047      0.092     0.469
#
# Reach it with: adb forward tcp:8765 tcp:8765
# It degrades to the regex when the phone is not serving, so a campaign never
# stops just because the server is down - but it says which one it used.

RELEVANCE_URL = os.environ.get("RELEVANCE_URL", "http://127.0.0.1:8765")

# Calibrated on live posts, not invented. Clear matches land at 0.19-0.47 and
# clear non-matches at -0.09..0.08, so the gap is wide - but the boundary is
# softer than that suggests: short or list-like posts score low even when they
# are on topic ("Biggest Loss-Making Films of 2026" scored 0.118, "Directors &
# their Highest Grossers" 0.018), because mean pooling rewards longer,
# keyword-dense text.
#
# So prefer RANKING to thresholding wherever a caller can. Ordering was right in
# every live sample even where the cut-off was not; picking the best k posts on
# screen is more robust than asking whether one clears a bar.
RELEVANCE_THRESHOLD = 0.10

_relevance_up: Optional[bool] = None


def _health(timeout: float) -> bool:
    try:
        with urllib.request.urlopen(RELEVANCE_URL + "/health", timeout=timeout) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:
        return False


def relevance_available(timeout: float = 2.0, serial: str = "") -> bool:
    """Whether the on-device scorer is reachable. Cached after the first check.

    The server runs in Termux and is reached over an `adb forward`, and that
    forward is the fragile half: it does not survive an adb server restart or
    the device re-enumerating, while the server itself keeps running. When it
    goes, every score silently disappears from a pass that otherwise looks
    healthy - which is how a 20-reel collection came back unscored with the
    model up the whole time. So a failed check re-establishes the forward once
    and asks again, rather than reporting a working scorer as absent.
    """
    global _relevance_up
    if _relevance_up is not None:
        return _relevance_up
    _relevance_up = _health(timeout)
    if not _relevance_up and "127.0.0.1" in RELEVANCE_URL:
        port = RELEVANCE_URL.rsplit(":", 1)[-1]
        try:
            dev.adb("forward", "tcp:%s" % port, "tcp:%s" % port, serial=serial)
            _relevance_up = _health(timeout)
        except Exception:
            pass
    return _relevance_up


def relevance(query: str, texts: list, timeout: float = 8.0) -> Optional[list]:
    """Cosine similarity of each text to `query`. None when unavailable."""
    if not texts or not relevance_available():
        return None
    try:
        req = urllib.request.Request(
            RELEVANCE_URL + "/score",
            data=json.dumps({"query": query, "texts": texts}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("scores")
    except Exception:
        return None


def best_post(posts: list, query: str) -> Optional[int]:
    """Index of the most relevant post on screen, or None.

    The ranking form of `score_posts`, and the one to prefer: it asks which post
    is most on-topic rather than whether any clears a threshold, which is the
    judgement that survives short posts and odd phrasing.
    """
    if not posts:
        return None
    meta = score_posts(posts, query)
    if meta["backend"] != "embedding":
        hits = [i for i, p in enumerate(posts) if p.get("relevant")]
        return hits[0] if hits else None
    best = max(range(len(posts)), key=lambda i: posts[i].get("relevance") or -9)
    return best if (posts[best].get("relevance") or -9) > 0 else None


def score_posts(posts: list, query: str,
                threshold: float = RELEVANCE_THRESHOLD) -> dict:
    """Annotate posts in place with `relevance` and `relevant`.

    Falls back to the regex tag when the device scorer is unreachable, and
    reports which path was taken - a run scored by keywords should never be
    mistaken for one scored by meaning.
    """
    # Text ONLY. Appending the handle measurably hurts: a handle tokenises into
    # meaningless subwords that pull the mean toward noise, and mean pooling has
    # no way to down-weight them. Measured against "bollywood film gossip":
    # "Toxic advance booking record for Yash" scores 0.196 alone and 0.072 with
    # a handle glued on - enough to cross a threshold in the wrong direction.
    # Fall back to the handle only when there is no text at all.
    texts = [(p.get("text") or p.get("handle") or "") for p in posts]
    scores = relevance(query, texts)
    if scores is None:
        topic = query.split()[0].lower() if query else ""
        for p in posts:
            p["relevance"] = None
            p["relevant"] = topic in (p.get("topics") or tag(p))
        return {"backend": "regex", "threshold": None, "n": len(posts)}
    for p, s in zip(posts, scores):
        p["relevance"] = s
        p["relevant"] = s >= threshold
    return {"backend": "embedding", "threshold": threshold, "n": len(posts),
            "matched": sum(1 for p in posts if p["relevant"])}
