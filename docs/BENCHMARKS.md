# Benchmarks

Every number here was measured on the target device during the build, not
estimated. Where a result contradicted the assumption the project started with,
that is stated rather than quietly dropped.

**Device.** realme narzo 50 Pro 5G (RMX3395), Android 14, Dimensity 920,
2× Cortex-A78 @ 2.5 GHz + 6× A55, 7.36 GiB usable RAM, unrooted.
**Host.** Windows 11, i5-1235U, connected over 2.4/5 GHz Wi-Fi on the same LAN.
**Date.** 2026-09-03. **Screen under test.** Launcher home (27.4 KB hierarchy).

---

## 1. Screen reading

The dominant cost in any phone-automation loop, and the measurement that
reshaped this project's design.

| Method | Transport | Median | Payload |
|---|---|---|---|
| `uiautomator dump` CLI | Wi-Fi from host | **2540 ms** | 8.3 KB |
| `uiautomator dump` CLI | on-device loopback | **2520 ms** | 8.3 KB |
| uiautomator2 persistent server | Wi-Fi from host | **220 ms** | 27.4 KB |
| uiautomator2 persistent server | on-device loopback | **306 ms** | 27.9 KB |

Raw runs:

```
CLI over Wi-Fi        2645 / 2546 / 2539 ms
CLI on-device         2536 / 2521 / 2511 ms
u2 over Wi-Fi          314 /  244 /  219 / 220 ms   (first call includes warm-up)
u2 on-device           291 /  306 /  311 / 342 / 289 / 263 ms
```

### What this means

**The CLI is 11× slower than the persistent server.** `uiautomator dump` spawns
a fresh instrumentation process on every invocation; uiautomator2 keeps one
alive and answers over a socket. Any tool in this repo that reads the screen
goes through `device.u2()` for this reason, never through `adb shell uiautomator`.

**Moving on-device did not make tool calls faster.** It saved 20 ms out of 2540
on the CLI path, and on the fast path it is *slower* — 306 ms on-device against
220 ms over Wi-Fi. The phone's A78 cores serialise the hierarchy more slowly
than the laptop consumes it across the network.

This directly contradicts the premise the project began with ("running on the
phone will be much faster than relaying tool calls"). The transport was never
the bottleneck. The real arguments for on-device execution are autonomy and
moving the *expensive model turns* off the laptop — see
[ARCHITECTURE.md](ARCHITECTURE.md#delegate-vs-proxy).

**Payload size is not the issue either.** 27 KB over Wi-Fi is a few
milliseconds. What matters is that `ui.parse()` reduces that 27 KB hierarchy to
1–3 KB of typed elements before it ever reaches a model, where it would cost
real tokens.

---

## 2. Tool schema overhead

Measured by serialising the JSON schemas the model receives each turn.

| Exposed surface | Tools | Characters | ≈ Tokens per turn |
|---|---|---|---|
| `core` only | 19 | 6,303 | **~1,575** |
| everything | 132 | 48,930 | **~12,232** |

**7.8×**, paid on every single turn before the task is even described. Per pack:

| Pack | ≈ Tokens |
|---|---|
| `core` | 1,575 |
| `x` | 2,470 |
| `phone` | 1,977 |
| `instagram` | 1,316 |

This is why packs exist and why only `core` loads by default. On a 30-step run
the difference is roughly 320k tokens of pure schema overhead versus 47k — on
cheap models that is the difference between a run costing cents and costing
dollars, and it is before counting the accuracy loss from asking a small model
to choose among 132 options.

---

## 3. Privilege and transport

| Check | Result |
|---|---|
| Termux app uid | `u0_a308` |
| `input tap` as Termux uid | **denied** |
| `settings get` as Termux uid | **denied** |
| `pm list packages` as Termux uid | allowed |
| `dumpsys` as Termux uid | allowed |
| Termux → `127.0.0.1:5555` TCP | **open** |
| `adb -s 127.0.0.1:5555 shell id` | **`uid=2000(shell)`** |
| adb key authorisation | one dialog, persists in `/data/misc/adb/adb_keys` |

Capability probe through the loopback shell (`claudephone tool privileges`):

```json
{"input": true, "settings_read": true, "pm_list": true, "dumpsys": true}
```

---

## 4. Round-trip costs

| Operation | Host over Wi-Fi | On-device |
|---|---|---|
| `device_info` (4 getprops + 2 shells) | 257 ms | 295 ms |
| `privileges` (4 shell probes) | 356 ms | 364 ms |
| `phone_battery` via termux-api | 1598 ms | 1247 ms |
| `phone_wifi` via termux-api | — | 1215 ms |
| u2 connection warm-up | 1540 ms | ~1900 ms |

The `phone` pack was expected to be the one clear win for on-device execution,
because from a host it is routed through `adb shell run-as com.termux` and pays
process setup per call. It is a win, but a **1.3× one, not the 6× first
guessed** — because the dominant cost is the `termux-*` CLI itself binding to
the Android API service, which is paid identically either way. Roughly 1.2 s of
the 1.25 s is Termux:API, not transport.

This is the same lesson as §1 in a different place: on this stack, transport is
almost never what costs you. Every on-device figure here is within ~15% of its
host equivalent, and two of the four are slightly *worse*.

---

## 5. Targeted queries are slower than a full dump

Testing the idea that an "observer" should fetch only the fields it needs
instead of the whole screen. Settings home, 45 KB hierarchy, on-device.

| Read | Median |
|---|---|
| full `dump_hierarchy()` | 260 ms |
| one targeted `.info` query | 225 ms |
| **three targeted `.info` queries** | **678 ms** |

**A three-field observer costs 2.6x a full dump.** The jsonrpc round trip is a
~220 ms floor and the payload is nearly free - 45 KB serialises in the same time
as one field. So selective fetching is a pessimisation, and the rule is: **one
full read per observation, always**, then filter in Python where it is free.

This killed a design that was about to be built. The diff idea it was competing
with still wins, but on **tokens** rather than latency: a dump is ~1.5 KB of
compact elements and a diff is ~200 bytes.

---

## 6. The accessibility bridge (phase 2)

The 220 ms "floor" above turned out to be a property of uiautomator2, not of
Android. Same screen, same device, back to back:

| Read path | Median |
|---|---|
| `uiautomator dump` CLI | 2540 ms |
| uiautomator2 `dump_hierarchy()` | 215-260 ms |
| **bridge `/tree`** | **10-13 ms** |
| bridge `/tree`, first call after connect | 97 ms |

**~20x faster than u2, ~200x faster than the CLI.** An AccessibilityService is
already inside the process holding the node tree, so a read is a walk over live
objects rather than a socket round trip plus XML serialisation.

Through the tool registry, end to end:

| Call | Before (u2) | After (bridge) |
|---|---|---|
| `look()` on-device | ~300 ms | **13 ms** |
| `look()` from host via adb forward | ~300 ms | 40 ms |
| `swipe_and_see()` wait for change | blind 1-2 s sleep | **0.14 s** (event) |

### Events

`/changed` blocks inside the service and returns when `onAccessibilityEvent`
fires. Measured waking **133 ms after a swipe** - a push, not a poll.

### Coexistence: both backends run at once

The project previously recorded that uiautomator2 and a custom
AccessibilityService were mutually exclusive. **Tested on Android 14 and false:**

```
accessibility service is ENABLED; can uiautomator2 still run?
  u2 dump_hierarchy: OK, 44556 bytes, median 215 ms
  COEXISTENCE: both backends work simultaneously
```

With one caveat that is not about Android: from a **host**, once u2 is used in
the same Python process, every adb-forwarded connection from that process starts
returning empty - verified on two ports, while curl from another process kept
working. So the bridge defaults to on-device only. Details in
[ACCESSIBILITY.md](ACCESSIBILITY.md).

### Cost of the APK

20 KB, no dependencies, built with `aapt2 -> javac -> d8 -> apksigner`. No
Gradle, no network.

---

## 7. What has not been measured yet

Stated so the gaps are not mistaken for results:

- **End-to-end agent runs.** Requires an `OPENROUTER_API_KEY`, which was not
  available at build time. Steps-to-completion and cost per task on a cheap
  model are unknown.
- **Battery and thermals** of an always-on accessibility service plus a busy
  event loop. Unmeasured, and likely the binding constraint on continuous
  watching.
- **Local model throughput.** No GGUF has been loaded on this device yet.
  [SESSION-STATE](../../Dev/SESSION-STATE.md) budgets LFM2-1.2B Q4 at ~800 MB
  against a ~4–4.5 GB real ceiling, but tokens/sec is unmeasured.
- **Delegate vs proxy, end to end.** The argument in ARCHITECTURE.md is
  structural and follows from turn counts; it has not been timed on a real task.
- **Battery and thermals** under a sustained unattended run.
- **Reboot survival** of the loopback adb connection.

---

## Reproducing

```bash
# screen reading, both paths
claudephone tool ui_dump --args '{"limit":1}'

# privileges
claudephone tool privileges

# schema overhead
python - <<'EOF'
import json; from claudephone.agent import build_registry
r = build_registry()
for name in ("core", *sorted(r.packs())):
    s = json.dumps(r.specs({name}))
    print("%-18s %6d chars  ~%5d tokens" % (name, len(s), len(s)//4))
EOF
```
