"""Instagram via instaloader: profiles, reels and media WITHOUT the device.

This is the fast path. The device path costs ~25s per reel because it must play
the reel in real time to record it; here the same reel's metadata and its
`video_url` come back in one request, and the video downloads from the CDN
straight to the laptop at internet speed - no MediaProjection consent, no
per-reel dwell, no 2.54 MB/s ADB transport.

Device capture remains the FALLBACK, and it is not redundant: it is the only
way to capture what actually appeared in your feed, where no shortcode exists.

Session handling answers the cookie-expiry problem directly:
  ig_web_status()  -> is the saved session still valid?
  ig_web_login()   -> log in from .env, persist the session, continue
Sessions are saved to disk so a run resumes without re-authenticating, and
test_login() is checked before work rather than discovering expiry mid-run.

Credentials are read from .env and never logged, echoed, or written into any
artifact.

RATE LIMITING - measured, not theoretical. Instagram returned 429 after only
TWO web_profile_info requests, with a 666s backoff. Its own error text names
the cause: "do not use any Instagram App while Instaloader is running". The
phone app and this path share one account and compete for the same quota, so:

  * NEVER run device scraping and web fetching concurrently on one account;
  * batch web calls and space them out - the limit is per-account, not per-IP,
    so more machines does not help;
  * prefer ONE web call that returns many reels (ig_web_reels) over many small
    per-shortcode calls;
  * the device path is not merely a fallback for feed content - it is also the
    path that still works while the web quota is exhausted.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
ENV_PATH = os.environ.get(
    "IG_ENV_PATH",
    r"C:\Users\HP\OneDrive\Desktop\dev_test\InstaScrape\.env")
SESSION_DIR = os.path.join(ROOT, "pipeline", "sessions")
MEDIA_DIR = os.path.join(ROOT, "artifacts", "web_media")

_L = None          # cached Instaloader instance
_LOGGED_IN_AS = None


def _creds() -> tuple[Optional[str], Optional[str]]:
    """Read credentials from .env. Values are never returned to the caller."""
    if not os.path.exists(ENV_PATH):
        return None, None
    u = p = None
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip().lower(), v.strip().strip('"').strip("'")
            if k == "username":
                u = v
            elif k == "password":
                p = v
    return u, p


def _session_path(user: str) -> str:
    os.makedirs(SESSION_DIR, exist_ok=True)
    return os.path.join(SESSION_DIR, f"{user}.session")


def _loader(quiet: bool = True):
    global _L
    if _L is not None:
        return _L
    import instaloader
    _L = instaloader.Instaloader(
        quiet=quiet,
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, download_comments=False,
        save_metadata=False, compress_json=False,
        dirname_pattern=MEDIA_DIR,
    )
    return _L


def _ensure_session(auto_login: bool = True) -> dict:
    """Load a saved session, verify it, and re-login only if it has expired."""
    global _LOGGED_IN_AS
    user, pwd = _creds()
    if not user:
        return {"ok": False, "error": f"no username in {ENV_PATH}"}
    L = _loader()

    if _LOGGED_IN_AS == user:
        try:
            if L.test_login():
                return {"ok": True, "user": user, "source": "cached"}
        except Exception:
            pass

    sp = _session_path(user)
    if os.path.exists(sp):
        try:
            L.load_session_from_file(user, sp)
            if L.test_login():
                _LOGGED_IN_AS = user
                return {"ok": True, "user": user, "source": "session_file",
                        "session": sp}
        except Exception as e:
            # Expired or corrupt - fall through to a fresh login rather than
            # failing the whole run.
            pass

    if not auto_login:
        return {"ok": False, "error": "no valid session and auto_login=False"}
    if not pwd:
        return {"ok": False, "error": "session expired and no password in .env"}
    try:
        L.login(user, pwd)
        L.save_session_to_file(sp)
        _LOGGED_IN_AS = user
        return {"ok": True, "user": user, "source": "fresh_login",
                "session": sp}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "Instagram may require a checkpoint/2FA confirmation; "
                        "complete it in a browser or the app once, then retry"}


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "Check whether the saved Instagram web session is still valid. "
            "Does NOT log in. Use before a run so expiry is discovered up front "
            "rather than mid-scrape."
        )
    )
    def ig_web_status() -> dict:
        user, pwd = _creds()
        if not user:
            return {"logged_in": False, "error": f"no credentials at {ENV_PATH}"}
        sp = _session_path(user)
        res = _ensure_session(auto_login=False)
        return {"logged_in": bool(res.get("ok")),
                "user": user,
                "session_file": sp if os.path.exists(sp) else None,
                "credentials_available": bool(pwd),
                "detail": res.get("error") or res.get("source")}

    @mcp.tool(
        description=(
            "Log in to Instagram using credentials from .env and persist the "
            "session to disk, so later runs resume without re-authenticating. "
            "Called automatically by the fetch tools when a session expires."
        )
    )
    def ig_web_login(force: bool = False) -> dict:
        global _LOGGED_IN_AS
        if force:
            _LOGGED_IN_AS = None
            user, _ = _creds()
            if user:
                sp = _session_path(user)
                if os.path.exists(sp):
                    os.remove(sp)
        res = _ensure_session(auto_login=True)
        # never echo the password, and never return the session cookie itself
        return {k: v for k, v in res.items() if k != "password"}

    @mcp.tool(
        description=(
            "Fetch an Instagram profile without the device: followers, "
            "following, post count, bio, external link, verified and private "
            "flags. Typically ~1s versus ~12s for the on-device path."
        )
    )
    def ig_web_profile(handle: str) -> dict:
        t0 = time.time()
        s = _ensure_session()
        if not s.get("ok"):
            return {"error": "not authenticated", "detail": s}
        import instaloader
        try:
            p = instaloader.Profile.from_username(_loader().context,
                                                  handle.lstrip("@"))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "handle": handle}
        return {
            "handle": p.username, "user_id": p.userid,
            "display_name": p.full_name, "bio": p.biography,
            "external_link": p.external_url,
            "followers": p.followers, "following": p.followees,
            "post_count": p.mediacount,
            "verified": p.is_verified, "private": p.is_private,
            "seconds": round(time.time() - t0, 2),
            "session": s.get("source"),
        }

    @mcp.tool(
        description=(
            "List a profile's reels with metadata AND direct video URLs - no "
            "device, no playback, no recording. Returns shortcode, caption, "
            "views, likes, comments, duration and video_url per reel. This is "
            "the fast replacement for watching reels on the phone."
        )
    )
    def ig_web_reels(handle: str, limit: int = 20,
                     include_video_url: bool = True) -> dict:
        t0 = time.time()
        s = _ensure_session()
        if not s.get("ok"):
            return {"error": "not authenticated", "detail": s}
        import instaloader
        try:
            p = instaloader.Profile.from_username(_loader().context,
                                                  handle.lstrip("@"))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "handle": handle}
        if p.is_private:
            return {"handle": p.username, "private": True,
                    "reels": [], "note": "private account - no media visible"}

        out: list[dict] = []
        try:
            for post in p.get_posts():
                if not post.is_video:
                    continue
                r = {
                    "shortcode": post.shortcode,
                    "permalink": f"https://www.instagram.com/reel/{post.shortcode}/",
                    "caption": post.caption,
                    "hashtags": list(post.caption_hashtags or [])[:20],
                    "likes": post.likes,
                    "comments": post.comments,
                    "views": post.video_view_count,
                    "duration_s": post.video_duration,
                    "posted_utc": post.date_utc.isoformat() if post.date_utc else None,
                }
                if include_video_url:
                    r["video_url"] = post.video_url
                out.append(r)
                if len(out) >= limit:
                    break
        except Exception as e:
            return {"handle": p.username, "reels": out,
                    "error": f"{type(e).__name__}: {e}",
                    "partial": True, "seconds": round(time.time() - t0, 2)}

        return {"handle": p.username, "collected": len(out), "reels": out,
                "seconds": round(time.time() - t0, 2)}

    @mcp.tool(
        description=(
            "Download a reel's video by shortcode straight from Instagram's CDN "
            "to local disk. Bypasses the phone entirely - no MediaProjection, "
            "no ADB transfer at 2.54 MB/s."
        )
    )
    def ig_web_download(shortcode: str, subdir: str = "web_media") -> dict:
        import urllib.request
        t0 = time.time()
        s = _ensure_session()
        if not s.get("ok"):
            return {"error": "not authenticated", "detail": s}
        import instaloader
        try:
            post = instaloader.Post.from_shortcode(_loader().context, shortcode)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "shortcode": shortcode}
        if not post.is_video:
            return {"error": "not a video post", "shortcode": shortcode}

        dest_dir = os.path.join(ROOT, "artifacts", subdir)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{shortcode}.mp4")
        try:
            req = urllib.request.Request(
                post.video_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r, \
                    open(dest, "wb") as f:
                f.write(r.read())
        except Exception as e:
            return {"error": f"download failed: {type(e).__name__}: {e}",
                    "shortcode": shortcode}
        b = os.path.getsize(dest)
        el = time.time() - t0
        return {"shortcode": shortcode, "path": dest, "bytes": b,
                "seconds": round(el, 2),
                "mb_per_s": round(b / 1048576 / el, 2) if el else None,
                "duration_s": post.video_duration,
                "acquired": "cdn_download"}

    @mcp.tool(
        description=(
            "Fetch comments for a post/reel by shortcode, including replies "
            "where Instagram exposes them. Far more complete than the on-device "
            "sheet, which is viewport-limited to roughly 7 visible comments."
        )
    )
    def ig_web_comments(shortcode: str, limit: int = 50,
                        include_replies: bool = True) -> dict:
        t0 = time.time()
        s = _ensure_session()
        if not s.get("ok"):
            return {"error": "not authenticated", "detail": s}
        import instaloader
        try:
            post = instaloader.Post.from_shortcode(_loader().context, shortcode)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "shortcode": shortcode}

        out: list[dict] = []
        try:
            for c in post.get_comments():
                item = {
                    "id": getattr(c, "id", None),
                    "author": c.owner.username if c.owner else None,
                    "text": c.text,
                    "likes": getattr(c, "likes_count", None),
                    "posted_utc": c.created_at_utc.isoformat()
                                  if getattr(c, "created_at_utc", None) else None,
                    "depth": 0,
                }
                if include_replies:
                    reps = []
                    for r in (getattr(c, "answers", None) or []):
                        reps.append({
                            "author": r.owner.username if r.owner else None,
                            "text": r.text,
                            "likes": getattr(r, "likes_count", None),
                            "depth": 1,
                        })
                    if reps:
                        item["replies"] = reps
                out.append(item)
                if len(out) >= limit:
                    break
        except Exception as e:
            return {"shortcode": shortcode, "comments": out, "partial": True,
                    "error": f"{type(e).__name__}: {e}",
                    "seconds": round(time.time() - t0, 2)}

        total_replies = sum(len(c.get("replies", [])) for c in out)
        return {"shortcode": shortcode, "collected": len(out),
                "replies_collected": total_replies, "comments": out,
                "declared_total": post.comments,
                "seconds": round(time.time() - t0, 2)}


COOKIE_JSON = os.environ.get(
    "IG_COOKIE_JSON",
    r"C:\Users\HP\OneDrive\Desktop\dev_test\InstaScrape\cookie.json")


def register_session_import(mcp) -> None:

    @mcp.tool(
        description=(
            "Adopt an EXISTING Instagram session from InstaScrape's cookie.json "
            "instead of logging in. Strongly preferred over ig_web_login: a "
            "fresh login from a new IP triggers Instagram's checkpoint, whereas "
            "reusing already-validated cookies does not. Saves the imported "
            "session so later runs resume without re-importing."
        )
    )
    def ig_web_import_session(cookie_path: str = "") -> dict:
        global _LOGGED_IN_AS
        path = cookie_path or COOKIE_JSON
        if not os.path.exists(path):
            return {"ok": False, "error": f"no cookie file at {path}"}
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
        except Exception as e:
            return {"ok": False, "error": f"unreadable cookie file: {e}"}

        cookies = blob.get("cookies") or {}
        if "sessionid" not in cookies:
            return {"ok": False, "error": "cookie file has no sessionid",
                    "found": list(cookies)}

        expiry = blob.get("overall_expiry")
        if expiry and expiry < time.time():
            return {"ok": False, "error": "cookies have expired",
                    "expired_at": expiry,
                    "hint": "re-run InstaScrape's login, or use ig_web_login"}

        L = _loader()
        try:
            sess = L.context._session
            for name, value in cookies.items():
                sess.cookies.set(name, value, domain=".instagram.com")
            # instaloader identifies the logged-in user from the session
            user = L.test_login()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        if not user:
            return {"ok": False,
                    "error": "cookies loaded but Instagram did not accept them",
                    "hint": "session may be bound to another IP/device; "
                            "re-run InstaScrape login to mint fresh cookies"}

        L.context.username = user
        _LOGGED_IN_AS = user
        sp = _session_path(user)
        try:
            L.save_session_to_file(sp)
        except Exception:
            sp = None
        days = (expiry - time.time()) / 86400 if expiry else None
        return {"ok": True, "user": user, "source": "imported_cookies",
                "saved_session": sp,
                "days_remaining": round(days, 1) if days else None,
                "note": "no login performed - no checkpoint risk"}
