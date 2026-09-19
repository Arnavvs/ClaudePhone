"""Following a verified deep link, and proving it landed (B6).

A deep link replaces a whole navigation sequence - on Instagram, search + typing +
tapping a result becomes one intent - so it saves steps and counted reads. It
also fails in ways that look like success, which is why this is careful:

* **Only registered, device-verified links are followed** (cards/deeplinks.json).
  A link nobody has watched land is a guess, and a guess that opens the wrong
  app reads as a working step.
* **Landing is checked.** The foreground package must become the one the
  registry names; anything else is an error, not a success.
* **A locked or sleeping phone is refused up front.** PROJECT-CONTEXT §6: on a
  lock screen every deep link "succeeds" and every read comes back empty.
* **The URL goes into a shell command**, and it comes from the model, so anything
  outside a URL-safe character set is refused rather than escaped.
* **Counted reads stay counted.** Opening a profile by link is still a profile
  open; the registry names the ledger action and the read is acquired first.
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from .. import cards
from .. import device as dev
from .. import state
from .. import ui as uix

_SAFE = re.compile(r"^[A-Za-z0-9:/?=&._\-%+@#~]+$")


def open_verified(url: str, wait_s: float = 6.0, allow_unverified: bool = False,
                  settle_s: float = 4.0) -> dict:
    """Open `url` if the registry trusts it, and prove it landed."""
    from ..runtime import screen as scr
    from ..runtime.observer import observer

    url = (url or "").strip()
    entry = cards.match(url)
    if entry is None:
        return {"error": "not in the deep-link registry; only links verified on a "
                         "device are followed",
                "known": [e["prefix"] for e in cards.links() if e.get("verified")]}
    if not entry.get("verified") and not allow_unverified:
        return {"error": "this link has not been verified on a device yet "
                         "(claudephone deeplinks --verify)", "prefix": entry["prefix"]}
    if not _SAFE.match(url) or url == entry["prefix"]:
        return {"error": "refused: the link is empty after its prefix, or has "
                         "characters a shell would interpret"}

    o = observer()
    health = o.health()
    if health.get("locked") or health.get("awake") is False:
        return {"error": "the phone is locked or asleep - a deep link would look "
                         "like it worked and every read would come back empty",
                **health}

    gate = None
    if entry.get("count"):
        from ..policy import reads
        gate = reads.acquire(entry["count"], entry.get("platform", ""), target=url[:80])
        if not gate.allowed:
            return reads.refusal(gate, opened=False)

    pkg = entry["package"]
    dev.shell("am start -a android.intent.action.VIEW -d '%s' -p %s" % (url, pkg),
              check=False)
    t0, fg = time.time(), ""
    while time.time() - t0 < wait_s:
        fg = (dev.foreground() or {}).get("package") or ""
        if fg == pkg:
            break
        time.sleep(0.5)
    if fg != pkg:
        # Nothing was read on the account, so nothing is recorded.
        return {"error": "the link did not land in %s (foreground: %s)" % (pkg, fg or "?"),
                "opened": url, "landed": False}
    if gate is not None:
        from ..policy import reads
        reads.commit(gate, target=url[:80])

    obs, waited = o.wait_until_stable(quiet_s=1.0, timeout_s=settle_s)
    c = scr.context()
    out = {"opened": url, "landed": True, "package": fg,
           "arrived_s": round(time.time() - t0, 1), "note": entry.get("note"),
           "ver": state.version(), "total_elements": len(c["elements"]),
           "elements": state.with_refs(uix.compact(c["elements"], limit=60))}
    if gate is not None:
        out["read"] = gate.to_dict()
    return out


def verify(prefix: str = "", write: bool = True) -> list:
    """Open each registry link's `sample` on the phone; mark the ones that land.

    Every open still goes through the ledger - verifying a profile link is a
    profile open. -> [{prefix, landed, error?, app_version?}]
    """
    import datetime
    reg = json.load(open(cards.LINKS_PATH, encoding="utf-8"))
    results = []
    for e in reg["links"]:
        if prefix and e["prefix"] != prefix:
            continue
        r = open_verified(e["prefix"] + e.get("sample", ""), allow_unverified=True)
        row = {"prefix": e["prefix"], "landed": bool(r.get("landed")),
               "error": r.get("error")}
        if r.get("landed"):
            info = dev.device_info()
            e["verified"] = {
                "date": datetime.date.today().isoformat(),
                "device": getattr(info, "model", ""),
                "android": getattr(info, "android", ""),
                "app_version": dev.app_version(e["package"]) or "",
            }
            row["app_version"] = e["verified"]["app_version"]
        dev.shell("input keyevent KEYCODE_HOME", check=False)
        time.sleep(1.0)
        results.append(row)
    if write:
        with open(cards.LINKS_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(reg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return results


def register(reg) -> None:
    verified = [e["prefix"] for e in cards.links() if e.get("verified")]

    @reg.tool(
        description=(
            "Open a screen directly with a deep link instead of navigating to it - "
            "fewer steps and, on Instagram, fewer counted reads. Only links in the "
            "verified registry are followed, and the tool checks the right app "
            "actually came to the front. Verified prefixes: "
            + (", ".join(verified) if verified else "none yet") + "."
        )
    )
    def open_link(url: str, wait_s: float = 6.0) -> Optional[dict]:
        return open_verified(url, wait_s=wait_s)
