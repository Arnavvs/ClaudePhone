# Device differences

Everything in this project was built and measured against one handset. A second
one was attached on 2026-09-08, and roughly a third of what was assumed to be
"how Android behaves" turned out to be "how that ROM behaves".

This file records what actually differs, so the universal layer can be written
against evidence rather than against one device's habits.

| | realme narzo 50 Pro 5G | Samsung Galaxy M21 |
|---|---|---|
| Model | RMX3395 | SM-M215F |
| Android | 14 · realme UI V14 | 12 · One UI 4 |
| SoC | Dimensity 920 | Exynos 9611 |
| RAM | 7.36 GiB | 3.49 GiB (**0.71 GiB available**) |
| Screen | 1080×2400 | 1080×2340 |
| Density | — | 420 |
| Serial | HIDMFQ8X894DIVLZ | RZ8N70HYQSB |
| Transport tested | Wi-Fi + USB | USB |

---

## 1. Foreground detection — the one that broke silently

**The single biggest portability bug found so far**, and it failed in the worst
possible way: quietly, and with a misleading error downstream.

`device.foreground()` grepped `dumpsys activity activities` for
`topResumedActivity`. Measured:

| ROM | Emits |
|---|---|
| realme UI V14 / Android 14 | `topResumedActivity` |
| One UI 4 / Android 12 | `mResumedActivity` and `ResumedActivity`, **never** `topResumedActivity` |

So on the Samsung the grep matched nothing and `foreground()` returned
`{"package": None, "activity": None}` without complaint. Everything downstream
then degraded without saying why:

- `ui_dump` reported `package: null`
- with no package there is no app name, so no version lookup
- with no version there is no baseline, so `screen` was always `null`
- **selector detection, drift checking and `extract_fields` were all disabled**
- `extract_fields` reported *"screen not recognised"* — blaming the screen for a
  probe failure two layers down

Fixed by probing in order and reporting which probe answered:

```
dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity|ResumedActivity'
dumpsys window              | grep -m1 -E 'mCurrentFocus|mFocusedApp'
```

The result now carries `_probe`, so a `None` answer can be told apart from an
unsupported dumpsys format. Costs one round trip as before (112 ms on the
Samsung); the second probe only runs if the first finds nothing.

**Rule for the universal layer:** never probe a ROM with a single pattern, and
always report which probe answered. A silent `None` that disables four features
is worse than a loud failure.

## 2. `/sdcard` is a symlink — a bug on both devices

`list_dir` ran `ls -la <path>`, which on a symlink prints the *link*, not the
directory. `/sdcard` → `/storage/self/primary` on every Android device, and
`/sdcard` is this tool's own **default argument**, so `list_dir()` with no
arguments returned one useless line.

Not a device difference at all — it was equally wrong on the realme, and simply
had never been called. Fixed with `ls -laL`, which dereferences and still
behaves correctly on a plain file.

**Rule:** a second device is worth attaching even for bugs that have nothing to
do with it. Different usage exercises different paths.

## 3. Termux: the on-device path does NOT work here

The project's central trick — Termux runs `adb connect 127.0.0.1:5555` and gets
uid 2000 — depends on how Termux was installed. The two handsets differ:

| | realme | Samsung |
|---|---|---|
| Termux version | 0.118.3 | 0.118.3 |
| Signing cert | `db86cf3c` (GitHub) | `7c3fcce` (**F-Droid**) |
| `flags=` | includes `DEBUGGABLE` | `HAS_CODE ALLOW_CLEAR_USER_DATA` only |
| `run-as com.termux` | works | ~~`package not debuggable`~~ **fixed** |
| Bootstrap unpacked | 389 binaries | ~~0~~ **389 — fixed** |
| adbd on TCP | yes | ~~not listening~~ **5555 — fixed** |

What that cost, before it was fixed:

- **`run-as` failed**, so every host-side `phone_*` tool was unavailable — they
  route through `adb shell run-as com.termux` when off-device.
- `tmx.ps1` could not work.
- On-device mode was not blocked by the signature (the loopback trick needs adb
  *inside* Termux, not `run-as`), but Termux had never been opened, so there was
  no bootstrap and no `adb` binary to run.

**Resolved 2026-09-08.** The F-Droid trio was uninstalled and replaced with the
official GitHub releases — `termux-app v0.118.3+github-debug_arm64-v8a`,
`termux-api v0.53.0+github.debug`, `termux-boot v0.8.1+github.debug`, the
add-on checksums verified against the published `checksums-sha256.txt`. All
three now report signature `db86cf3c`, matching the realme exactly, and
`com.termux` carries `DEBUGGABLE`. Termux was opened once (389 binaries, same
count as the realme), `python android-tools termux-api git python-lxml
python-pillow` installed, and adbd switched to TCP with `adb tcpip 5555`.

Remaining: the loopback `adb connect 127.0.0.1:5555` reaches adbd but sits at
`unauthorized` until the on-device *Allow USB debugging?* dialog is accepted.
That is a one-time human tap by design and is deliberately not scripted.

**Rule:** `run-as` availability is a property of the *install source*, not of
the OEM. Probe it rather than assuming it.

## 3a. `settings put` works here — it does not on the realme

The realme has `WRITE_SECURE_SETTINGS` revoked from uid 2000 in **all three**
scopes (global, secure, system), which blocked animation-scale, stay-awake and
anything else settable from the shell. One UI 4 allows it:

```
settings put global verifier_verify_adb_installs 0   -> works
settings put global package_verifier_user_consent -1 -> works
settings put secure enabled_notification_listeners … -> works
```

This matters more than a convenience. Play Protect was silently holding the
`adb install` of Termux open indefinitely — no error, no visible dialog, the
command simply never returned. Turning the adb verifier off made the same
install succeed in seconds. **A hanging `adb install` is Play Protect until
proven otherwise.**

**Rule:** treat `settings put` as a probed capability, not a given. Where it
works, setup can be fully scripted; where it does not, the operator has to tap.

## 3b. Termux:API tools fail where the adb equivalents work

Two `phone_*` tools were broken on this device, both silently, and both have a
`system`-pack equivalent that worked perfectly on the same phone:

| Broken | Why | Works instead |
|---|---|---|
| `phone_notifications` | `termux-notification-list` blocks until its listener answers. Termux:API had no notification-listener access, so nothing ever answered — the tool hung for the full 60 s then returned a subprocess traceback. Granting access via `settings put secure enabled_notification_listeners` did **not** fix it; it then returned empty. | `notifications` (dumpsys) — read all 6 |
| `phone_clipboard_get` | Android 10+ restricts clipboard reads to the **foreground** app, and Termux:API is not foreground under `run-as`. Returned `""`, indistinguishable from an empty clipboard. `phone_clipboard_set` had reported success. | `clipboard_get` (uiautomator2) — read the value back correctly |

Both now fail loud and name the tool that works. `_termux()` also no longer
raises `TimeoutExpired`: it returns `(124, "", "timed out after Ns")`, because a
raised timeout reached the model as a 900-character traceback quoting the base64
payload, which said nothing about the cause. That restores the project's own
"errors are returned, not raised" rule for the whole `phone` pack.

**Rule:** where a capability has both a Termux:API and an adb/u2 path, prefer
the adb path and treat Termux:API as the fallback — it depends on a service
binding, a runtime permission *and* a special access grant, each of which fails
differently and quietly.

## 4. Screen reading — comparable, slightly slower

| | realme (Wi-Fi) | realme (on-device) | Samsung (USB) |
|---|---|---|---|
| u2 `dump_hierarchy` | 220 ms | 306 ms | **296 ms** (median of 6) |
| Raw XML | 27.4 KB | 27.9 KB | 48.1 KB (launcher) |

The Exynos 9611 is a much weaker SoC than the Dimensity 920, yet the read cost
is within ~35% of the realme's fastest path — more evidence for the conclusion
in [BENCHMARKS.md](BENCHMARKS.md) that the round trip dominates and the device
is not the bottleneck.

`ui.parse` reduced a 48.1 KB hierarchy to 40 elements and **3.8 KB** of compact
JSON — a 12.6× reduction on a screen it had never seen.

## 5. Gesture geometry follows screen size correctly

`swipe(direction="up")` on the 2340-high Samsung produced
`540, 1918 → 540, 422`, against `540, 1968 → 540, 432` on the 2400-high realme.
The fractional geometry ports correctly.

The app modules that hardcode `input swipe 540 1700 540 800` do **not** port —
`540` is half of 1080 and happens to be right here only because both handsets
are 1080 wide. On any other width they aim off-centre.

## 6. Things that behaved identically

Worth recording so the universal layer does not defend against non-problems:

- `adb shell id` → `uid=2000(shell)` on both, with the same group memberships.
- The capability probe — `input`, `settings_read`, `pm_list`, `dumpsys` — is
  `true` for all four on both.
- uiautomator2 installed its agent and connected without intervention.
- `ui_dump`, `find_element`, `tap`, `swipe`, `battery_status`, `network_info`,
  `storage_info`, `media_volume`, `device_shell` all worked unchanged. A full
  see-act-see cycle (launch calculator → find `7` → tap → `+` → `8` → `=` → read
  `15` back from `calc_edt_formula`) passed on the first attempt.
- `dumpsys | grep` prints `Broken pipe` on both ROMs. Still harmless.

---

## Toward a universal layer

What this comparison suggests, in priority order:

1. **A capability probe run once per device, cached.** Which foreground pattern
   answers, whether `run-as` works, whether adbd is on TCP, screen size. Most of
   `privileges()` already does this; it should also record the *dumpsys dialect*
   and be persisted per serial rather than recomputed.
2. **No single-pattern ROM probes anywhere.** Grep alternatives, and report
   which one answered.
3. **Kill the hardcoded coordinates** in the app modules. `540` is a 1080-wide
   assumption sitting in ~20 call sites.
4. **A device profile file** keyed by serial, holding the probe results and the
   per-device selector baselines, so a second handset does not re-derive them.
5. **Treat `on_device` as a spectrum, not a boolean.** The Samsung is
   USB-attached with a working shell but no Termux userland; the realme is both.
   `termux_shell` already refuses off-device, but `phone_*` tools assume `run-as`
   works whenever off-device, which is now known to be false.
