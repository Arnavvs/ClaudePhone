"""The observation runtime: cache a screen, detect when it changes, diff it.

This exists because of one measurement (docs/BENCHMARKS.md section 5), which
overturned the design we were about to build:

    full dump_hierarchy                260 ms      45 KB payload
    one targeted .info query           225 ms
    three targeted .info queries       678 ms      <- 2.6x a FULL dump

The jsonrpc round trip is a ~220 ms floor and the payload is nearly free. So
"only fetch the fields the observer needs" is *slower* than fetching the whole
screen. **One full dump per observation, always.** Everything selective happens
afterwards, in Python, where it costs nothing.

That 220 ms floor turned out to be a property of uiautomator2, not of Android.
The on-device AccessibilityService (`runtime/bridge.py`) reads the same screen
in ~13 ms, because it walks a tree it already holds in process instead of
crossing a socket. This module prefers it automatically and falls back to u2.

What that leaves worth optimising is not the dump but the two things around it:

* **Blind sleeps.** `swipe(); sleep(2)` is either too short - you read the old
  screen - or wasted time. `wait_for_change` polls the cheap signature instead
  and returns the moment the screen actually moved.
* **Tokens.** A dump is ~1.5 KB of compact elements; ten dumps in a row are
  mostly the same nav bar. `diff` returns only what appeared, which is a couple
  of hundred bytes. The 260 ms is paid either way - the saving is in the
  model's context, not the clock.

Two signatures, because "changed" means different things:

    structure  - the set of resource-ids. Changes when you move between SCREENS.
    content    - a hash of the text/desc values. Changes when the same screen
                 shows different CONTENT, which is what a feed does when it
                 scrolls. The structure of a reel viewer never changes.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import device as dev
from .. import state
from .. import ui as uix


def content_signature(elements) -> str:
    """Hash of the VALUES on screen.

    `ui.screen_signature` hashes resource-ids, which is right for recognising a
    screen and useless for noticing that a feed advanced - the ids are
    identical from one reel to the next. This hashes what the elements say.
    """
    vals = [(e.text or e.desc) for e in elements if (e.text or e.desc)]
    return hashlib.sha1("\x1f".join(vals).encode()).hexdigest()[:12]


@dataclass
class Observation:
    elements: list = field(default_factory=list)
    package: str = ""
    activity: str = ""
    screen: Optional[str] = None
    structure: str = ""
    content: str = ""
    dump_ms: int = 0
    at: float = 0.0

    def values(self) -> set:
        """(anchor, value) pairs - the unit a diff is taken over."""
        out = set()
        for e in self.elements:
            v = e.text or e.desc
            if v:
                out.add((e.anchor or e.rid or "", v))
        return out

    def compact(self, limit: int = 120) -> list[dict]:
        return uix.compact(self.elements, limit=limit)


class Observer:
    """A persistent view of the screen, with a cache and a diff.

    Hold ONE of these per session: the u2 connection is the expensive part to
    create, and the cache is the whole point.
    """

    def __init__(self, serial: str = "") -> None:
        self.serial = serial
        self.last: Optional[Observation] = None
        self.reads = 0
        self.total_s = 0.0

    # -- reading -------------------------------------------------------------

    @property
    def backend(self) -> str:
        """'bridge' when the AccessibilityService is up, else 'u2'."""
        from . import bridge as br
        return "bridge" if br.available(self.serial) else "u2"

    def look(self, remember: bool = True) -> Observation:
        """One full read of the screen. The only way we ever look."""
        from . import bridge as br

        t0 = time.time()
        activity = ""
        if br.available(self.serial):
            # The bridge reports the package itself, so skip dev.foreground() -
            # that is an adb round trip costing ~200 ms, which would throw away
            # the entire point of an 11 ms read.
            r = br.bridge(self.serial).tree()
            elements, pkg = r["elements"], r["package"]
        else:
            xml = dev.u2(self.serial).dump_hierarchy()
            elements = uix.parse(xml)
            fg = dev.foreground(serial=self.serial)
            pkg = fg.get("package") or ""
            activity = fg.get("activity") or ""
        ms = int((time.time() - t0) * 1000)
        self.reads += 1
        self.total_s += time.time() - t0

        obs = Observation(
            elements=elements,
            package=pkg,
            activity=activity,
            structure=uix.screen_signature(elements),
            content=content_signature(elements),
            dump_ms=ms,
            at=time.time(),
        )
        # Keep the shared tap-index cache honest: tap(i=...) resolves against
        # whatever was dumped most recently, so a look() that did not update it
        # would leave the agent aiming at a stale screen.
        state.remember(elements, pkg)
        if remember:
            self.last = obs
        return obs

    # -- change detection ----------------------------------------------------

    def wait_for_change(self, kind: str = "content", timeout_s: float = 8.0,
                        poll_s: float = 0.25,
                        baseline: Optional[Observation] = None
                        ) -> tuple[Optional[Observation], float]:
        """Poll until the screen differs from `baseline`, or give up.

        Returns (observation, seconds_waited); observation is None on timeout.
        Replaces every blind sleep in the codebase.
        """
        from . import bridge as br

        base = baseline or self.last or self.look()
        want = base.content if kind == "content" else base.structure
        t0 = time.time()

        # With the bridge up this is a PUSH, not a poll: /changed blocks inside
        # the AccessibilityService and returns the instant the window content
        # changes (measured: 133 ms after a swipe). The u2 path below has no
        # equivalent and has to keep dumping.
        if br.available(self.serial):
            b = br.bridge(self.serial)
            try:
                since = b.health().get("changes", 0)
                while True:
                    # LOOK FIRST, then wait. The caller has usually just acted,
                    # so the events this action caused may already have been
                    # counted before we read the counter - waiting on the *next*
                    # event would then block until timeout for a change that has
                    # in fact already landed. A look costs ~13 ms, so checking
                    # before every wait is nearly free insurance.
                    obs = self.look()
                    got = obs.content if kind == "content" else obs.structure
                    if got != want:
                        return obs, round(time.time() - t0, 2)
                    remaining = timeout_s - (time.time() - t0)
                    if remaining <= 0:
                        return None, round(time.time() - t0, 2)
                    ev = b.changed(since, int(max(0.2, remaining) * 1000))
                    since = ev.get("changes", since)
            except br.BridgeError:
                pass    # service died mid-run; fall through to polling

        while time.time() - t0 < timeout_s:
            obs = self.look()
            got = obs.content if kind == "content" else obs.structure
            if got != want:
                return obs, round(time.time() - t0, 2)
            time.sleep(poll_s)
        return None, round(time.time() - t0, 2)

    def wait_until_stable(self, quiet_s: float = 0.6, timeout_s: float = 10.0,
                          poll_s: float = 0.25) -> tuple[Observation, float]:
        """Wait for the screen to STOP changing - i.e. loading has finished.

        The complement of wait_for_change: useful after opening an app, where
        the interesting moment is when the spinner stops rather than when the
        first pixel moves.
        """
        t0 = time.time()
        obs = self.look()
        steady_since = time.time()
        while time.time() - t0 < timeout_s:
            time.sleep(poll_s)
            nxt = self.look()
            if nxt.content == obs.content:
                if time.time() - steady_since >= quiet_s:
                    return nxt, round(time.time() - t0, 2)
            else:
                steady_since = time.time()
            obs = nxt
        return obs, round(time.time() - t0, 2)

    # -- diffing -------------------------------------------------------------

    @staticmethod
    def diff(before: Observation, after: Observation,
             limit: int = 40) -> dict:
        """What appeared and vanished between two observations.

        On a feed this separates content from chrome for free: what changed is
        the post, what stayed is the navigation bar.
        """
        b, a = before.values(), after.values()
        appeared = [{"anchor": k, "value": v} for k, v in sorted(a - b)]
        vanished = [{"anchor": k, "value": v} for k, v in sorted(b - a)]
        return {
            "appeared": appeared[:limit],
            "vanished": vanished[:limit],
            "appeared_count": len(appeared),
            "vanished_count": len(vanished),
            "screen_changed": before.structure != after.structure,
            "content_changed": before.content != after.content,
            "truncated": len(appeared) > limit or len(vanished) > limit,
        }

    def act_and_observe(self, action: Callable[[], None],
                        kind: str = "content", timeout_s: float = 8.0,
                        poll_s: float = 0.25) -> dict:
        """Do something, wait for the screen to react, report only the delta.

        This is the shape every compound action is built from: one model turn
        buys an action plus its verified consequence, instead of three.
        """
        before = self.last or self.look()
        action()
        after, waited = self.wait_for_change(kind=kind, timeout_s=timeout_s,
                                             poll_s=poll_s, baseline=before)
        if after is None:
            return {"changed": False, "waited_s": waited,
                    "hint": ("nothing changed within " + str(timeout_s) + "s. "
                             "The action may not have registered, the screen "
                             "may already have been at the end, or a dialog "
                             "may be blocking it.")}
        out = self.diff(before, after)
        out["changed"] = True
        out["waited_s"] = waited
        out["package"] = after.package
        out["screen"] = after.screen
        return out

    def stats(self) -> dict:
        return {"reads": self.reads,
                "avg_ms": int(self.total_s / self.reads * 1000) if self.reads
                else None}

    # -- diagnosis -----------------------------------------------------------
    #
    # A sleeping or locked phone produces a dump that is technically valid and
    # entirely useless: a handful of nodes and an empty package. Every tool
    # here then reports its own local failure ("nothing changed", "app did not
    # arrive") and the agent goes hunting for the wrong bug. Checking costs a
    # ~200 ms dumpsys, so it is only paid when a result already looks wrong.

    def health(self) -> dict:
        """Why is the screen not showing what we expect? Costs one dumpsys."""
        out: dict = {}
        try:
            power = dev.shell("dumpsys power | grep -m1 mWakefulness",
                              serial=self.serial, check=False)
            out["awake"] = "Awake" in power
        except Exception:
            out["awake"] = None
        try:
            win = dev.shell("dumpsys window | grep -m1 mDreamingLockscreen",
                            serial=self.serial, check=False)
            out["locked"] = "mDreamingLockscreen=true" in win
        except Exception:
            out["locked"] = None
        return out

    def explain_empty(self, obs: Observation) -> Optional[str]:
        """A plain-language reason for a screen that looks empty, or None.

        Returned as a `blocked_by` hint so the agent fixes the actual problem
        instead of retrying the tap that was never going to work.
        """
        if obs.package and len(obs.elements) > 12:
            return None
        h = self.health()
        if h.get("awake") is False:
            return ("the phone screen is OFF - nothing can be read or tapped. "
                    "Wake it with press_key('wake'), then unlock it.")
        if h.get("locked"):
            return ("the phone is on the LOCK SCREEN - the app behind it "
                    "cannot be read or tapped. Swipe up to dismiss, and enter "
                    "the passcode if one is set.")
        if not obs.package:
            return ("no app is reporting as foreground and the screen has "
                    + str(len(obs.elements)) + " elements - the device may be "
                    "mid-transition; wait_stable() and look again.")
        return None


# One observer per process. The cache is worthless if every tool call builds a
# new one, and the u2 connection costs ~1.5 s to create.
_observer: Optional[Observer] = None


def observer(serial: str = "") -> Observer:
    global _observer
    if _observer is None or _observer.serial != serial:
        _observer = Observer(serial)
    return _observer
