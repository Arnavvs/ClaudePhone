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

### 1. u2 and a custom AccessibilityService DO coexist

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

## API

Loopback only, port 8766. Arguments are query parameters, not JSON bodies —
that removes a parser from the trust path of a service that can tap anything.

| Endpoint | Does |
|---|---|
| `GET /health` | liveness, change counter, last package |
| `GET /tree?limit=N` | the screen as compact elements |
| `GET /changed?since=N&timeout=ms` | **blocks** until the content changes |
| `GET /tap?x=&y=&ms=` | dispatch a tap gesture |
| `GET /swipe?x1=&y1=&x2=&y2=&ms=` | dispatch a swipe |
| `GET /key?name=` | back, home, recents, notifications, quicksettings, lock |
| `GET /text?value=` | set text on the focused input |

`/tree` emits the **same element shape** the Python side already consumes from
u2 — `{"i", "id", "anchor", "text", "desc", "cls", "c":[x,y], "b":[l,t,r,b],
"f"}` — so `extract_fields`, the selector registry and every app pack work
against either backend without knowing which answered.

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
