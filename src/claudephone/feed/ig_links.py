"""A permalink for every reel, taken from Instagram's own logcat chatter.

Verified against com.instagram.android 440.1.0.46.86 on RMX3395 (Android 14),
2026-09-04.

`ig_collect` used to say a reel link was unobtainable. The tree carries no URL,
and the one route to it - Share > Copy link - writes to a clipboard Android
will not hand to `adb`: `cmd clipboard` is unimplemented, `dumpsys clipboard`
prints nothing, and `service call clipboard` answers "No items". The repo's own
`clipboard_get` can still read it through the uiautomator2 agent, and a paste
target or Termux:API would too - but all of them leave the per-reel cost
untouched, because the expensive part is opening the share sheet and tapping
Copy link on every single reel, over a sheet that also carries a Play Store
"Send to device" target a mistimed tap will open.

There is a free route. The Reels viewer logs one line per item as it swaps
video in:

    W/06Ih: prepareVideo: clipsItemId=3975305609373152955_4612644018, pos=9, ...

`clipsItemId` is `<media_pk>_<owner_user_id>`, and the media pk is the reel's
identity: base64 it with Instagram's alphabet and you have the shortcode in the
public URL. So the link costs nothing but a `logcat` reader running alongside
the pass - no taps, no sheets, no app switch, no extra seconds.

Proven, not assumed: two ids captured this way were converted to URLs and
opened back on the phone with an ACTION_VIEW intent. `DcrH2PATfa7` landed on
`anshikaaprakash` and `DaM-EsKB5Z8` on `deeksha_.02`, matching the authors read
off the screen when those ids were logged.

Two things to know before trusting it:

* **The tag is obfuscated.** `06Ih` is a minified class name and will differ in
  the next Instagram build, so nothing here matches on the tag - only on the
  message text, which is a literal in their source and survives longer. If even
  that changes, `ClipTail` goes quiet and the collector records `url: None`
  rather than guessing.
* **An ad's id has no underscore.** Ads log `clipsItemId=120248852404520644` -
  an ad id, not a media pk - so they are recognisable, and they give the
  collector a second, independent opinion about which slots were ads to check
  its own screen-reading against.

There is no way to get the link for the reel already on screen: the line is
emitted when the item is swapped in, so the id arrives with the swipe. A pass
that wants a link for its first reel has to advance onto it.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from typing import Optional

from .. import device as dev

# Instagram's base64 alphabet for shortcodes - standard base64 order with the
# URL-safe last two characters.
_ALPHABET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz"
             "0123456789-_")

# Matched against the message, never the tag. `pos` is the item's index within
# the current viewer session - it restarts when a new Reels surface opens (a
# deep link opens its own), so it orders a run and must not be treated as a key.
_LINE = re.compile(r"clipsItemId=([0-9]+)(?:_([0-9]+))?, pos=(\d+)")


def shortcode(media_pk: int) -> str:
    """Encode a media pk as the shortcode that appears in the public URL."""
    if media_pk <= 0:
        return ""
    out = []
    while media_pk > 0:
        media_pk, rem = divmod(media_pk, 64)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def describe(item_id: str) -> dict:
    """Turn a logged clipsItemId into a link, or say why there isn't one.

    An organic reel's id is `<media_pk>_<owner_id>`; an ad's is a bare ad id
    with no owner, and no public reel URL exists for it.
    """
    pk, _, owner = item_id.partition("_")
    if not owner:
        return {"kind": "ad", "item_id": item_id, "ad_id": pk, "url": None}
    code = shortcode(int(pk))
    return {"kind": "reel", "item_id": item_id, "media_pk": pk,
            "owner_id": owner, "shortcode": code,
            "url": "https://www.instagram.com/reel/%s/" % code}


class ClipTail:
    """A background `logcat` reader that collects reel ids as they are swapped in.

    Filtered device-side with `logcat -e`, so the phone sends roughly one line
    per reel instead of the ~1000 lines a second the Reels viewer otherwise
    produces. Reading happens on a thread because an unread pipe eventually
    blocks the writer, and that writer is adb.
    """

    def __init__(self, serial: str = "", pattern: str = "clipsItemId"):
        self._serial = serial or dev.DEFAULT_SERIAL
        self._pattern = pattern
        self._items: deque = deque()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def _cmd(self, *args: str) -> list:
        cmd = [dev.ADB]
        if self._serial:
            cmd += ["-s", self._serial]
        return cmd + list(args)

    def start(self) -> "ClipTail":
        # Clear first: the buffer holds the previous pass's reels, and replaying
        # those would attach last night's links to tonight's feed.
        subprocess.run(self._cmd("logcat", "-c"), capture_output=True)
        self._proc = subprocess.Popen(
            self._cmd("logcat", "-v", "time", "-e", self._pattern),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            m = _LINE.search(line)
            if not m:
                continue
            pk, owner, pos = m.groups()
            item = describe(pk + ("_" + owner if owner else ""))
            item["pos"] = int(pos)
            with self._lock:
                self._items.append(item)

    def drain(self) -> list:
        """Take everything logged since the last drain, oldest first."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
        return out

    def latest(self) -> Optional[dict]:
        """The newest item since the last drain - the reel a swipe just landed on.

        Normally a swipe produces exactly one line, but the viewer sometimes
        prepares more than one item at once; the highest `pos` is the one now on
        screen, and the rest are prefetch running ahead of us.
        """
        items = self.drain()
        return max(items, key=lambda it: it["pos"]) if items else None

    def wait(self, timeout_s: float = 2.5, poll_s: float = 0.15) -> Optional[dict]:
        """`latest()`, but give the line a moment to arrive first.

        The id is logged when the video is swapped in, which is a beat after the
        swipe gesture returns. Waiting for it is the difference between a link
        and a `None` that would read as "this reel has no link".
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._lock:
                if self._items:
                    break
            time.sleep(poll_s)
        return self.latest()

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def __enter__(self) -> "ClipTail":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
