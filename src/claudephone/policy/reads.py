"""Counted reads: every budgeted READ goes through the per-account ledger (B2b).

The one restriction this project has suffered came from reads, not writes: 316
profile opens in 27 minutes (PROJECT-CONTEXT §7.2). datacollect's phases count
every profile open, grid scan, reel, sheet, comment read, feed reel and search,
and pace to the per-minute ceiling. ClaudePhone's tools did not, so an agent
could exceed the account limit by reading alone.

Same ledger, same actions, same semantics as the phases:

    profile_open  one per profile          grid_scan     one per grid scan
    reel_open     one per reel opened      reel_walk     one per lockstep walk
    sheet_open    one per More/caption     comment_read  one per comment sheet
    feed_reel     one per reel in a feed   search        one per query
    tg_read       one per Telegram chat opened

Usage, for a tool that performs N units:

    d = reads.acquire("profile_open", "ig", target=handle)   # hour/day check, paces the minute
    if not d.allowed:
        return reads.refusal(d)
    ... do the read ...
    reads.commit(d, target=handle)                           # one ledger row

For loops, `acquire(n=N)` reserves the batch against the hour/day ceilings and
`unit(d, target)` paces and records each item as it happens.

Fails CLOSED: an unknown phone or no reachable ledger refuses the read - on the
phone itself there is no collect.db - unless the operator allowed uncounted
reads for the run (--allow-uncounted-reads / CLAUDEPHONE_UNCOUNTED_READS=1), in
which case the result says loudly that nothing was counted.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from .. import device as dev
from . import writes as wr

PACE_MAX_WAIT_S = 75.0
PACE_POLL_S = 5.0

# Instagram screens: which action a forward swipe on a reel viewer is.
_REEL_MARKERS = ("clips_author_username", "clips_ufi_more_button_component")
_VIEWER = "clips_viewer_view_pager"


def uncounted_allowed() -> bool:
    env = os.environ.get("CLAUDEPHONE_UNCOUNTED_READS", "").lower() in ("1", "true", "yes")
    return env or wr.CONFIG.allow_uncounted_reads


@dataclass
class ReadDecision:
    allowed: bool
    action: str
    platform: str
    n: int = 1
    account: str = ""
    why: str = ""
    counted: bool = True
    paced_s: float = 0.0
    recorded: int = 0

    def to_dict(self) -> dict:
        d = {"allowed": self.allowed, "action": self.action, "n": self.n}
        for k in ("account", "why", "paced_s", "recorded"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if not self.counted:
            d["counted"] = False
        return d


def refusal(d: ReadDecision, **extra) -> dict:
    return {"error": "ledger refused this read", "read": d.to_dict(), **extra}


def _pace(led, action: str, max_wait_s: float) -> tuple[bool, float]:
    """Wait until the per-minute ceiling has room. -> (ok, seconds waited)."""
    per_min = led.budget(action)["per_min"]
    t0 = time.time()
    while led.count(action, 1) >= per_min:
        if time.time() - t0 >= max_wait_s:
            return False, round(time.time() - t0, 1)
        time.sleep(PACE_POLL_S)
    return True, round(time.time() - t0, 1)


def acquire(action: str, platform: str, target: str = "", n: int = 1,
            serial: Optional[str] = None, pace: bool = True,
            max_wait_s: float = PACE_MAX_WAIT_S) -> ReadDecision:
    """Reserve `n` units of a counted read before doing any of them."""
    serial = dev.default_serial() if serial is None else serial
    account = wr.account_for(serial, platform)
    if not account:
        if uncounted_allowed():
            return ReadDecision(True, action, platform, n, counted=False,
                                why="UNCOUNTED: no ledger account for this phone; "
                                    "the operator allowed uncounted reads")
        return ReadDecision(False, action, platform, n,
                            why="no ledger account known for phone '" + (serial or "?")
                                + "' on '" + platform + "' - counted reads refused "
                                "(allow uncounted reads for the run to override)")
    try:
        led = wr.ledger_for(account, serial)
    except wr.LedgerUnavailable as e:
        if uncounted_allowed():
            return ReadDecision(True, action, platform, n, account, counted=False,
                                why="UNCOUNTED: ledger unavailable (" + str(e)[:80] + ")")
        return ReadDecision(False, action, platform, n, account,
                            why="ledger unavailable, read refused: " + str(e))
    ok, why = led.can(action, n=n)
    if not ok:
        return ReadDecision(False, action, platform, n, account, why="ledger: " + why)
    paced = 0.0
    if pace:
        ok, paced = _pace(led, action, max_wait_s)
        if not ok:
            return ReadDecision(False, action, platform, n, account, paced_s=paced,
                                why="per-minute ceiling for " + action + " still full "
                                    "after waiting " + str(paced) + " s")
    return ReadDecision(True, action, platform, n, account, why=why, paced_s=paced)


def unit(d: ReadDecision, target: str = "", serial: Optional[str] = None,
         max_wait_s: float = PACE_MAX_WAIT_S) -> bool:
    """Pace and record ONE unit of an acquired batch. False = stop the loop."""
    if not d.allowed:
        return False
    if not d.counted:
        return True
    serial = dev.default_serial() if serial is None else serial
    try:
        led = wr.ledger_for(d.account, serial)
    except wr.LedgerUnavailable:
        return False
    if d.recorded >= d.n:                            # beyond the reservation
        ok, why = led.can(d.action, n=1)
        if not ok:
            d.why = "stopped: ledger: " + why
            return False
    if d.recorded:                                   # the first unit was paced by acquire
        ok, paced = _pace(led, d.action, max_wait_s)
        d.paced_s += paced
        if not ok:
            d.why = "stopped: per-minute ceiling for " + d.action + " still full"
            return False
    led.record(d.action, target=target, note="claudephone read")
    d.recorded += 1
    return True


def commit(d: ReadDecision, target: str = "", serial: Optional[str] = None,
           n: int = 1) -> None:
    """Record `n` executed units (single-shot tools)."""
    if not d.allowed or not d.counted:
        return
    serial = dev.default_serial() if serial is None else serial
    led = wr.ledger_for(d.account, serial)
    for _ in range(max(0, n)):
        led.record(d.action, target=target, note="claudephone read")
        d.recorded += 1


# ------------------------------------------------------------------ generic taps and swipes

def classify_count(e, package: str = "") -> tuple[str, str]:
    """(rule id, ledger action) if tapping `e` opens a counted read, else ("", "")."""
    if e is None:
        return "", ""
    app = (wr.rules().get("apps") or {}).get(package or "") or {}
    for rule in app.get("counted") or []:
        if wr._matches(rule, e):
            return rule["id"], rule["count"]
    return "", ""


def classify_tap_count(target, elements, x: int, y: int, package: str = "") -> tuple[str, str]:
    rule, action = classify_count(target, package)
    if action:
        return rule, action
    return classify_count(wr.at_point(elements, x, y), package)


def reel_advance_action(elements, package: str) -> str:
    """What a forward swipe counts as on this screen: feed_reel in the Reels tab,
    reel_open in a viewer opened from a grid, "" anywhere else.

    In the Reels tab the swipe counts even when no reel is on screen: the tab
    interleaves cards ("Suggested for you", seen live 2026-09-17) and the swipe
    off one of them lands on a reel.
    """
    if package != "com.instagram.android":
        return ""
    rids = {e.rid for e in elements or []}
    reels_tab = next((e for e in elements or [] if e.rid == "clips_tab"), None)
    in_reels_tab = reels_tab is not None and reels_tab.selected
    if in_reels_tab and (_VIEWER in rids or any(m in rids for m in _REEL_MARKERS)):
        return "feed_reel"
    if not any(m in rids for m in _REEL_MARKERS):
        return ""
    return "reel_open"


def platform_of(package: str) -> str:
    return ((wr.rules().get("apps") or {}).get(package or "") or {}).get("platform", "")


def _editable(e) -> bool:
    """A text field. Class names vary: EditText, app subclasses (IgEditText),
    and AutoCompleteTextView - which is what Instagram 446's comment box
    reports (live, Samsung, 2026-09-17)."""
    c = (e.cls or "").lower()
    return "edittext" in c or "autocompletetextview" in c


def composer_on_screen(elements, package: str) -> Optional[dict]:
    """A comment / DM / reply box on screen, if any: typing there is a send."""
    spec = wr.rules().get("composers") or {}
    ids = set((spec.get("rids") or {}).get(package or "", []))
    import re
    hint = re.compile(spec.get("hint", "$^"))
    for e in elements or []:
        label = " ".join(((e.text or "") or (e.desc or "")).split()).lower()
        if (e.rid and e.rid in ids) or (_editable(e) and label and hint.search(label)):
            return {"rid": e.rid, "label": label[:40]}
    return None


def search_box_on_screen(elements, package: str) -> bool:
    return package == "com.instagram.android" and any(
        _editable(e) and ("search" in (e.rid or "") or
                                 "search" in ((e.text or e.desc or "").lower()))
        for e in elements or [])
