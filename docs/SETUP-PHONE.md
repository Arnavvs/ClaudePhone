# Setting up the phone

Every step below was performed and verified on a realme narzo 50 Pro 5G
(RMX3395, Android 14, unrooted). Where a step failed the first time, the failure
is written down too — those are the ones that cost hours.

- [Before you start](#before-you-start)
- [1. Termux](#1-termux)
- [2. Enable adb over TCP](#2-enable-adb-over-tcp)
- [3. Run the bootstrap](#3-run-the-bootstrap)
- [4. Verify](#4-verify)
- [Surviving a reboot](#surviving-a-reboot)
- [ColorOS / realme specifics](#coloros--realme-specifics)
- [Troubleshooting](#troubleshooting)

---

## Before you start

**Use a phone you can afford to lose.** This installs an agent with shell
access, your messages, your camera and your files.

You need: an Android phone, Wi-Fi, and — for the initial adb-over-TCP step
only — either a USB cable once, or Android 11+ Wireless debugging.

---

## 1. Termux

Install from **[GitHub releases](https://github.com/termux/termux-app/releases)**,
not the Play Store:

| App | Why |
|---|---|
| Termux | the userland |
| Termux:API | `phone_*` tools — SMS, camera, GPS, sensors, TTS |
| Termux:Boot | starting the server on boot (optional) |

> **All three must come from the same source.** Termux add-ons only bind to a
> Termux with a **matching signing key**. Mixing a Play Store Termux with a
> GitHub Termux:API produces add-ons that install fine and then silently do
> nothing. On this device all three share key `db86cf3c`.

The GitHub build is also flagged `DEBUGGABLE`, which is what lets a laptop run
commands inside Termux via `run-as com.termux` — used by the host-side
`phone_*` tools.

Open Termux once so it unpacks its bootstrap, then:

```bash
pkg update && pkg upgrade -y
```

---

## 2. Enable adb over TCP

This is the step everything depends on. Termux's own uid cannot run `input` or
`uiautomator`; it reaches uid 2000 by connecting adb to the phone's own adbd.

Enable **Developer options** (tap Build number 7×), then **USB debugging**.

Then pick one:

**A. Wireless debugging (no cable).** Developer options → Wireless debugging →
on. It shows an `IP:port`. Note that **the port is random and changes every
time it is toggled**, so pass it explicitly:

```bash
export CLAUDEPHONE_LOOPBACK=127.0.0.1:<port>
```

Pairing may be required the first time (Pair device with pairing code →
`adb pair 127.0.0.1:<pair-port>` inside Termux).

**B. Once over USB (more stable).** From a computer with adb:

```bash
adb tcpip 5555
```

adbd then listens on the fixed port 5555 and the default configuration works
with no extra environment variable. This is the recommended route.

---

## 3. Run the bootstrap

In Termux:

```bash
curl -sL https://raw.githubusercontent.com/Arnavvs/ClaudePhone/main/scripts/bootstrap_phone.sh | bash
```

Partway through, **Android will show a dialog on the phone**:

```
Allow USB debugging?
The computer's RSA key fingerprint is: ED:91:A5:...
[ ] Always allow this computer for debugging
                              Cancel    Allow
```

Tick **Always allow**, tap **Allow**. This authorises Termux's own adb key. It
is a one-time grant, remembered in `/data/misc/adb/adb_keys`. The script waits
up to 90 seconds for it.

### Native dependencies that will not build

`pip install uiautomator2` fails twice on Termux, and the errors are unhelpful:

```
ERROR: Failed to build 'lxml' when getting requirements to build wheel
ERROR: Failed building wheel for Pillow
```

Neither compiles from source here. Install the **prebuilt Termux packages
first** — the bootstrap does this for you, but if you are installing by hand:

```bash
pkg install -y python-lxml python-pillow
pip install uiautomator2
```

### Model credentials

```bash
echo 'OPENROUTER_API_KEY=sk-or-v1-...' >> ~/ClaudePhone/.env
```

or run fully offline — see [MODELS.md](MODELS.md).

---

## 4. Verify

```bash
claudephone doctor
```

A healthy result:

```
environment
  running on phone : True
  adb binary       : /data/data/com.termux/files/usr/bin/adb
device
  loopback         : device
  selected         : 127.0.0.1:5555
  model            : RMX3395  Android 14  1080x2400
ui backend
  uiautomator2     : ok, dump 291 ms, 27864 bytes
tools
  registered       : 132 in 10 packs
```

Then a real task:

```bash
claudephone run "what app is open, and what is on screen?"
```

Grant the phone-tool permissions while you are here — uid 2000 can do it
without dialogs:

```bash
claudephone tool phone_permissions --args '{"groups":"all"}'
```

> Notification *reading* cannot be granted this way. It needs a manual toggle:
> Settings → Notifications → Notification access → **Termux:API**.

---

## Surviving a reboot

**Wireless adb usually stops listening after a reboot**, which takes the
loopback — and therefore all privileged tools — with it. `doctor` reports this
precisely rather than failing obscurely.

Options, worst to best:

1. **Re-enable by hand** after each reboot (Wireless debugging toggle, or
   `adb tcpip 5555` from a computer), then re-run the bootstrap.
2. **Do not reboot.** With Termux:Boot and a wake lock, uptime of weeks is
   normal for a dedicated node.
3. **`persist.adb.tcp.port`.** Some ROMs honour a persistent property so adbd
   listens on TCP from boot. Whether ColorOS respects it is **untested here** —
   if you try it, please report back.

Termux:Boot autostart (installed by the bootstrap at
`~/.termux/boot/claudephone`) needs the Termux:Boot app installed *and* the
OEM's auto-start permission granted — see below.

---

## ColorOS / realme specifics

ColorOS kills background processes aggressively, and **the ADB battery
whitelist alone is not enough** — services die within seconds without the UI
toggles:

```
Settings > Apps > App management > Termux > Battery usage
   -> Allow background activity
   -> Allow auto launch
Settings > Apps > Auto-start manager       -> enable Termux
Settings > Battery > More settings > Sleep standby optimisation  -> OFF
Recents > swipe down on the Termux card    -> tap the lock icon
```

Plus, from a shell:

```bash
adb shell cmd deviceidle whitelist +com.termux
```

Shortcut to the app's settings page:

```bash
adb shell am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:com.termux
```

Other quirks found on this ROM:

- **`settings put` is blocked device-wide**, even for uid 2000 — realme revoked
  `WRITE_SECURE_SETTINGS` from adb in all scopes. Reading works fine. Try
  Developer options → "Disable permission monitoring" if you need writes.
- **`dumpsys ... | grep` prints `Broken pipe`.** Harmless; ignore it.
- **Realme telemetry logs every sideload**
  (`uploadInstallAppInfos: {app_pkg=…, source=pc, md5=…}`).
- `adb install` can fail `INSTALL_FAILED_VERIFICATION_FAILURE` when Play Protect
  cannot show its dialog (e.g. locked screen). Installing from
  `/data/local/tmp` does **not** bypass the verifier.

---

## Troubleshooting

**`adb devices` shows `unauthorized`**
The dialog was dismissed or missed. Re-run `adb connect 127.0.0.1:5555` and
watch the screen. If no dialog appears, revoke and retry: Developer options →
Revoke USB debugging authorisations.

**`adb devices` shows nothing for the loopback**
adbd is not listening on TCP. Go back to [step 2](#2-enable-adb-over-tcp). After
a reboot this is the expected state.

**`uiautomator2` errors on first use**
It installs a helper APK on first connect; allow it. If it hangs, `pkill
uiautomator` and retry. Remember uiautomator2 *is* a UiAutomation and cannot
coexist with another AccessibilityService — disable any accessibility app.

**`phone_*` tools return empty output, no error**
A missing runtime permission — termux-api returns empty rather than failing.
Run `phone_permissions`. If SMS still returns nothing, check
Settings → Apps → Termux:API → Permissions.

**Two devices show up, wrong one is chosen**
The same phone on USB and TCP. USB is preferred automatically; override with
`export CLAUDEPHONE_SERIAL=127.0.0.1:5555`.

**`emulator-5554` appears in `adb devices`**
A harmless artefact of adb's emulator scan on loopback. Ignore it; the explicit
serial is always used.
