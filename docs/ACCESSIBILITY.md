# The accessibility bridge (phase 2)

An AccessibilityService that reads the screen in **~13 ms** instead of ~260 ms,
pushes an event when the screen changes instead of being polled, and needs no
adb at all. Source in [`android/bridge`](../android/bridge), built by
[`android/build.sh`](../android/build.sh).

This was planned from the beginning as "phase 2", behind the same tool surface.
Two things about it turned out differently from the plan, and both are recorded
below rather than quietly fixed.

---

## Why it is so much faster

`uiautomator2` crosses a socket to an instrumentation process, which walks the
window, serialises it as XML, and ships it back. An AccessibilityService is
*already inside* the process that holds the node tree, so `/tree` is a walk over
live objects.

Measured on RMX3395 / Android 14, same screen, back to back:

| Read path | Median |
|---|---|
| `uiautomator dump` CLI | 2540 ms |
| uiautomator2 `dump_hierarchy()` | 215–260 ms |
| **bridge `/tree`** | **10–13 ms** |
| bridge `/tree`, first call after connect | 97 ms |

**~20× faster than u2, ~200× faster than the CLI.**

It also removes a cost that would otherwise eat the win: the bridge reports the
foreground package itself, so `Observer.look()` skips `dev.foreground()` — an
adb round trip costing ~200 ms, which alone would have made a 13 ms read
pointless.

## Events, not polling

`/changed` long-polls: it blocks inside the service and returns the moment
`onAccessibilityEvent` fires.

```
$ curl "http://127.0.0.1:8766/changed?since=4&timeout=12000"
{"changes":5,"changed":true,"package":"com.android.settings","ms":1109}
```

Measured waking **133 ms after a swipe**. In practice `swipe_and_see` now
returns in **0.14 s**, against a blind `sleep(1.5)` that was both slower and
capable of reading the wrong screen.

`Observer.wait_for_change` uses this automatically, and **looks before it
waits** — see [the race](#the-race-worth-knowing-about).

---

## Two corrections to earlier project beliefs

### 1. u2 and a custom AccessibilityService DO coexist — on some phones

Every earlier document in this project (and MobileAgentMCP before it) stated
that they could not:

> uiautomator2 *is* a UiAutomation, which is itself a special
> AccessibilityService, and Android permits only one. Phase 2 is a swap, not a
> merge.

**Tested on Android 14, and false.** With the bridge enabled and serving, u2
still dumped normally at 215 ms:

```
accessibility service is ENABLED; can uiautomator2 still run?
  u2 dump_hierarchy: OK, 44556 bytes, median 215 ms
  COEXISTENCE: both backends work simultaneously
```

So the bridge is an **addition**, not a replacement. `Observer` prefers it and
falls back to u2 when it is not installed, which is why every existing tool got
faster without being touched.

**Not on the Samsung M21 (Android 12, uiautomator2 3.7.0), measured 2026-09-17.**
While u2's UiAutomation runs, Android unbinds the bridge: `dumpsys accessibility`
drops it from *bound services*, port 8766 stops listening, and even an on-device
`nc 127.0.0.1 8766` gets nothing. Enabled state and process are untouched.
`stop_uiautomator()` rebinds it in about 3 s; ending the Python process that
started u2 does too. u2 itself keeps working. See
[Do not mix backends](#do-not-mix-backends-in-one-host-process) for what the
client does about it.

### 2. `settings put secure` is not blocked after all

Session 1 recorded that realme had revoked `WRITE_SECURE_SETTINGS` from adb in
all scopes. That is no longer true on this device, and it matters here: it means
the service can be **enabled from a script** rather than by tapping through
Settings → Accessibility.

```bash
adb shell settings put secure enabled_accessibility_services \
    com.claudephone.bridge/com.claudephone.bridge.BridgeService
adb shell settings put secure accessibility_enabled 1
```

`android/build.sh install` does this for you. If your ROM does block it, the
manual toggle in Settings → Accessibility works and nothing else changes.

---

## Build and install

No Gradle, no network, no dependencies — `aapt2 → javac → d8 → apksigner`, all
from the SDK you already have. The APK is ~20 KB.

```bash
cd android
./build.sh            # build only
./build.sh install    # build, install, enable, verify
```

Requirements: a JDK (any recent one) and an Android SDK with build-tools and one
platform. Set `ANDROID_HOME` if it is somewhere unusual. Set `ANDROID_SERIAL`
when more than one device is attached.

Verify from the phone:

```bash
claudephone tool bridge_status
```

```json
{"backend_in_use": "bridge", "installed": true, "enabled": true, "reachable": true}
```

---

## Auth (v0.2)

**v0.1 was an open door.** Binding to 127.0.0.1 keeps the LAN out, but loopback
is shared by every app on the phone. Verified 2026-09-17 on the realme: from an
ordinary app uid (Termux, `u0_a308`) `GET /tree` returned the live screen. Any
installed app — Instagram included — could read whatever was on screen (SMS, 2FA
codes) and call `/tap`.

v0.2 requires the header `X-Bridge-Token` on every path except `/health`.

- **Who can read the token:** only adb shell (uid 2000) or root, through a
  ContentProvider that checks the caller's uid in code:
  ```bash
  adb shell content query --uri content://com.claudephone.bridge.auth/token
  adb shell content call  --uri content://com.claudephone.bridge.auth --method rotate
  ```
  Both real clients already have shell: the laptop through adb, and Termux through
  its loopback adb. `runtime/bridge.py` fetches the token on first use, caches it,
  and re-reads it once on a 401, so a rotation from another client heals itself.
  `CLAUDEPHONE_BRIDGE_TOKEN` overrides the lookup.
- **Where the token lives:** it is generated once and kept in the app's private
  preferences. It survives service restarts and APK upgrades.
- **A header, never a query parameter.** A web page can make a browser send a
  GET with any query string (an `<img>` is enough), but not a custom header.
- **Browser requests are refused.** Any request with an `Origin` header gets 403.
- **Unauthenticated `/health` says only** `{"ok":true,"auth":"required"}`.
- **Limits:** request lines and headers are capped (431), reads time out after
  5 s, and connections are capped at 32 (503).

Verified on the realme (Android 14), 2026-09-17:

| request | result |
|---|---|
| app uid, no token: `/health` | minimal body, auth=required |
| app uid, no token: `/tree` | 401 |
| app uid, wrong token: `/tree` | 401 |
| app uid: reading the provider through `content` | refused (SecurityException) |
| token in the query string | 401 |
| right token plus an `Origin` header | 403 |
| 9 KB header | 431 |
| right token | 200 |
| token after an APK upgrade | same token, still valid |
| Termux via loopback adb | reads the token (uid 2000) |

A v0.1 bridge still works with the new client, which reports it as
`auth: legacy_unauthenticated` so it gets upgraded.

**Same suite on the Samsung SM-M215F (Android 12, One UI), 2026-09-17: all pass.**
It also passed:
- a slow client sending nothing, dropped at the 5 s read timeout;
- 40 parallel long-polls → exactly 32 served, 8 refused with 503, still serving
  afterwards;
- Termux (uid 10318) reading the token through loopback adb.

### Force-stop switches the bridge OFF (measured on the Samsung; not yet re-checked on the realme)

`am force-stop com.claudephone.bridge` does more than kill the process: Android
removes the service from `enabled_accessibility_services` and sets
`accessibility_enabled=0`. The process can come back (reading the token starts it),
but the port stays refused.

Anything that force-stops apps switches the bridge off until it is re-enabled: the
Force stop button, Samsung Device Care optimisation, a cleaner app.

The Python client now **self-heals**. When the bridge is unreachable, installed and
no longer enabled, `available()` re-enables it once per process through the shell —
the same write `android/build.sh install` makes. `bridge_status` reports it as
`self_healed`.
- Measured: force-stop → next `available()` → reachable again in 6.5 s, same token.
- `CLAUDEPHONE_BRIDGE_AUTOHEAL=0` turns this off.

### Other things measured on the Samsung
- **uiautomator2 is unaffected** by enabling the bridge. The same screens were dumped
  before and after:
  - Settings: 123 nodes, 74 ids, identical attributes;
  - Instagram: 164 → 165 nodes; the one new id is `video_states`, the playing
    video's state; content descriptions 35 → 35.
- **Screen off:** still serves while dozing, and after waking.
- **Footprint:** 45 MB RSS, 0% CPU idle, standby bucket 10 (active).
- **Text via `/text` works for Unicode** (`हिंदी ₹50 ✓` typed and read back), which
  `adb input text` cannot do.
- **Events:** `/changed` woke 0.07–0.13 s after a swipe. **Beware:** the first change
  event arrives *mid-animation*. The Settings search icon read at y=659 / 410 / 471 at
  the first event and settled at y=209, so tapping coordinates read at that moment
  misses. Wait for the screen to settle before tapping.
- **Windows:** One UI shows three systemui windows above apps — status bar (78 px),
  navigation bar (126 px), and a 194×83 strip at top-centre (the privacy indicator).
  All are correctly classified as bars. The volume panel shows as an obstruction.
  With the notification shade open, the foreground is `com.android.systemui` via
  `active_root_fallback`.
- **Cost, before the lazy-package fix:** `/windows` took 11–25 ms here, because every
  window's package costs a binder call into its node tree. The bridge now looks up
  packages only for application windows and obstructions; bars get their title
  instead. Result: `/windows` 3–5 ms and warm `/tree` 4–9 ms on the Samsung.

---

## API

Loopback only, port 8766. Arguments are query parameters, not JSON bodies —
that removes a parser from the trust path of a service that can tap anything.
Every endpoint except `/health` needs `X-Bridge-Token`.

| Endpoint | Does |
|---|---|
| `GET /health` | liveness; with a token also version, change counter, last package |
| `GET /tree?limit=N[&windows=all]` | the screen as compact elements, plus windows (below) |
| `GET /windows` | the window summary alone |
| `GET /changed?since=N&timeout=ms` | **blocks** until the content changes (max 60 s) |
| `GET /tap?x=&y=&ms=` | dispatch a tap; returns `lands_on` |
| `GET /swipe?x1=&y1=&x2=&y2=&ms=` | dispatch a swipe |
| `GET /key?name=` | back, home, recents, notifications, quicksettings, lock |
| `GET /text?value=` | set text on the focused input |

`/tree` emits the **same element shape** the Python side already consumes from
u2 — `{"i", "id", "anchor", "text", "desc", "cls", "c":[x,y], "b":[l,t,r,b],
"f"}` — so `extract_fields`, the selector registry and every app pack work
against either backend without knowing which answered. The active window's
elements come first and are numbered exactly as in v0.1.

### Windows (v0.2)

v0.1 read only `getRootInActiveWindow()`. It was blind to everything drawn in
another window — the keyboard, system alerts, chat heads, volume panels — so a
dump looked normal while taps landed on something on top. `/tree` now adds:

| field | meaning |
|---|---|
| `windows` | every window, topmost first: `id, type, layer, pkg, active, focused, b` |
| `foreground`, `foreground_reason`, `foreground_known` | which app is really in front and which rule said so: `active_app_window`, `focused_app_window`, `active_root_fallback`, or `unknown:<why>` |
| `ime_visible` | a keyboard window exists |
| `obstructions` | windows above the active one that are not thin status/nav bars |
| flag `h` | node reported not visible to the user (e.g. scrolled out of its list) |
| `windows=all` | appends other windows' elements, tagged `w` (type) and `wid` |

`/tap` returns `lands_on: {type, pkg, layer, active, bar, covered}`, computed
before dispatch. `covered: true` means the tap hit a window other than the one
being read. `tap_and_see` surfaces this as `tap_landed_on`, and `look` as
`obstructed_by`.

Verified in Settings on the realme:
- The status bar is correctly **not** an obstruction.
- Opening search produced `ime_visible: true` with the Gboard window listed as an
  obstruction.
- A tap on the keyboard reported `covered: true`.
- Cost on the realme: `/windows` 3.5–4 ms server-side; warm `/tree` 8–12 ms on the phone (before the lazy-package change; see the Samsung section).

---

## Things that bit us

### The race worth knowing about

The first version of `wait_for_change` read the change counter *after*
performing the action, then waited for the next event. Any event the action
itself caused had already been counted, so it waited the full timeout for a
change that had in fact already landed — reporting `changed=False` on a swipe
that visibly scrolled.

The fix is to **look first, then wait**, on every iteration. At 13 ms a look is
nearly free, so checking before each wait costs nothing and closes the race.

### Screen size from the tree

Deriving the screen from the node tree must take the **maximum** extent of all
elements, not the first non-empty bounds. Elements arrive in document order and
the first with positive bounds is often a status-bar icon — which yields a
~1050×60 "screen" and puts every swipe in the notification shade.

### One thread per connection

`/changed` deliberately blocks for seconds. On the original single-threaded
server that stalled every other request behind it, including the `/tree` the
caller needs the instant the wait returns. Each connection now gets its own
thread.

### Do not mix backends in one host process

From a laptop the service is reached through `adb forward`. Once uiautomator2 is
used **in the same Python process**, every adb-forwarded connection from that
process starts returning an empty response — verified on two different local
ports, while `curl` from another process kept working. The breakage is
process-local to adb's client, not the service.

Because the legacy tools (`ui_dump`, `tap`, `swipe`) all go through u2, the
bridge therefore **defaults to off when running on a host** and on when running
on the phone, where no forward exists. Force it with `CLAUDEPHONE_BRIDGE=1` if
you are testing the bridge alone.

**On the Samsung the cause is different and not process-local** (see
[correction 1](#1-u2-and-a-custom-accessibilityservice-do-coexist--on-some-phones)):
u2 suppresses the service for the whole phone, on-device clients included. The
client now hands the phone back: when a bridge request dies and this process
holds a u2 session, `Bridge._yield_u2()` stops u2 (`device.release_u2()`), waits
for `/health`, and retries once. `bridge_status` reports it as `u2_yielded`.
Measured: three u2-dump → bridge-tree rounds, each recovered in 1.7 s. The next
u2 dump restarts u2 (about 1.7 s), so alternating backends costs ~3.5 s a switch.
**Done for the Instagram tools (2026-09-18).** Their shared `_dump()` reads
through `targeting.read_screen` (bridge first, u2 fallback), so the scraping path
no longer alternates. Measured on an `@instagram` profile, same screen:

| | dump | `ig_profile_stats` | elements |
|---|---|---|---|
| bridge | **31 ms** | 0.04 s | 135 |
| u2 (forced) | 2200 ms | 0.49 s | 85 |

Every field the scraper returns matched - handle, name, posts, followers,
following, verified, follow state, link, private - **except the bio, where the
bridge is more accurate**: u2's XML dump flattens the emoji in
"Discover what's new on Instagram 🔎✨" to "..", while the bridge keeps it.

This needed one fix first. The two backends disagreed on what an *anchor* is: u2
makes a node with an id its own anchor, while the service sends the inherited
(parent) anchor alongside the id. `values_by_anchor` therefore filed "686M"
under `profile_header_followers_stacked_familiar` instead of
`profile_header_familiar_followers_value`, and every header lookup came back
empty. `_elements_from` now prefers the node's own id, matching u2.

`ui_dump`, the explore tools and the system tools still dump through u2 - they
return raw XML or use u2 for other things - so the host default stays opt-in.

---

## What it does not solve

- **Reading is not understanding.** The bridge returns the same nodes faster; it
  does not tell you what a reel is about.
- **Custom-drawn UI stays invisible.** Instagram renders much of its interface
  in custom views. Accessibility exposes what the app chooses to expose, and a
  canvas exposes nothing. That is a limit of the whole approach, not this
  implementation.
- **Enabling it is still a privileged act.** Either a secure-settings write or a
  human toggle. There is no third way, and there should not be.
- **Battery and thermals are unmeasured.** An always-on service plus a busy
  event loop has a cost nobody has quantified on this device yet.
