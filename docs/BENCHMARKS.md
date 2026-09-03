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
| `adb shell` (trivial command) | ~90 ms | ~60 ms |
| `phone_battery` via termux-api | 1598 ms | ~250 ms |
| `privileges` (4 shell probes) | 356 ms | — |
| u2 connection warm-up | 1540 ms | ~1900 ms |

`phone_battery` is the one place the transport genuinely dominates: from a host
it is routed through `adb shell run-as com.termux`, which pays for process
setup on every call. On-device it is a direct subprocess. **A 6× gap — the
`phone` pack is the one that most rewards running on-device.**

---

## 5. What has not been measured yet

Stated so the gaps are not mistaken for results:

- **End-to-end agent runs.** Requires an `OPENROUTER_API_KEY`, which was not
  available at build time. Steps-to-completion and cost per task on a cheap
  model are unknown.
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
