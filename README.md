# ClaudePhone

An agent that runs **on** your Android phone and drives it — Claude Code for a
handset. It reads the screen as structured data, taps, types, swipes, reads your
notifications, takes photos, and can be handed whole tasks from a laptop.

```
$ claudephone run "open Instagram, find the top reel on my feed, tell me the caption"

▸ open Instagram, find the top reel on my feed, tell me the caption
  openrouter / deepseek/deepseek-v4-flash-0731  ·  19 tools (core)  ·  native tool calls
  1. launch_app(package="instagram")
     ✓ launched=com.instagram.android  1204ms
  2. ui_dump(limit=40)
     ✓ screen=reels_viewer  total_elements=39  224ms
  3. extract_fields()
     ✓ caption="when the ball just doesn't bounce your way 😭"  188ms

The top reel is by @ally.verma_ — caption "when the ball just doesn't bounce
your way 😭", 9,732 likes.

— 3 steps · 6.1s · 4,120 tokens
```

Built and verified on a **realme narzo 50 Pro 5G (RMX3395, Android 14),
unrooted, no Shizuku, no root.**

> **This is a full-control agent on a device you should treat as expendable.**
> It has a shell, your SMS, your camera and your files. Run it on a spare or
> experimental phone, not your daily driver. See [Safety](#safety).

---

## How it works

Termux runs as a normal app uid, which Android denies `input`, `uiautomator`
and `settings`. But `adbd` listening on TCP does not care where the client
connects from — including from the same phone:

```bash
adb connect 127.0.0.1:5555        # run inside Termux
adb -s 127.0.0.1:5555 shell id
# uid=2000(shell) ...
```

**The phone drives itself at shell privilege, unrooted.** One Android dialog
authorises it, once. Everything else is built on that.

Full reasoning, measurements and design trade-offs: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Install

On the phone, in [Termux](https://github.com/termux/termux-app/releases)
(GitHub build — *not* the Play Store one; the add-ons need matching signatures):

```bash
curl -sL https://raw.githubusercontent.com/Arnavvs/ClaudePhone/main/scripts/bootstrap_phone.sh | bash
```

That installs the packages, performs the adb self-connect, waits for you to tap
**Allow**, installs the Python side, and runs `doctor` to prove the chain works.

Full walkthrough, including the reboot caveat:
**[docs/SETUP-PHONE.md](docs/SETUP-PHONE.md)**.

### Point it at a model

```bash
export OPENROUTER_API_KEY=sk-or-v1-...          # cheap hosted models
export CLAUDEPHONE_MODEL=deepseek/deepseek-v4-flash-0731
```

or run entirely offline against `llama.cpp` on the phone:

```bash
export CLAUDEPHONE_PROVIDER=local
export CLAUDEPHONE_TOOL_MODE=json               # small models lack native tool calls
```

Both are supported and can be swapped per-run. See **[docs/MODELS.md](docs/MODELS.md)**.

---

## Use

```bash
claudephone doctor                      # check every link in the chain
claudephone run "<goal>"                # give it a task, watch it work
claudephone tools --pack phone          # browse the tool surface
claudephone tool ui_dump --args '{"limit":10}'   # call one tool directly
claudephone serve --host 0.0.0.0        # accept tasks from your laptop
```

Useful flags on `run`:

| Flag | Effect |
|---|---|
| `--mode ask` | prompt before anything that changes device state |
| `--mode readonly` | refuse to change anything |
| `--pack phone` | pre-load a tool pack |
| `--allow phone_sms_send` | permit a default-denied tool |
| `--max-steps 50` | raise the step ceiling |

---

## Hand tasks to the phone from your laptop

Start the phone server, then register the bridge with Claude Code on the laptop:

```bash
# phone
claudephone serve --host 0.0.0.0

# laptop
export CLAUDEPHONE_URL=http://192.168.1.5:8765
export CLAUDEPHONE_TOKEN=<token printed by serve>
claude mcp add claudephone -- python bridge/mcp_bridge.py
```

The laptop then has `phone_task`, `phone_tool`, `phone_tools` and
`phone_status`. `phone_task` sends a **goal**; the phone runs its own loop
locally and streams back a transcript.

This matters more than it looks. Driving the phone step-by-step from the laptop
costs one turn of a large model *per tap*. Delegating costs one laptop turn
total, and the thirty follow-up turns run on a cheap model against localhost.
The saving is the expensive turns, not the network —
[why](docs/ARCHITECTURE.md#delegate-vs-proxy).

---

## The tool surface

**132 tools in 10 packs.** Only `core` (19) is loaded at the start of a run; the
agent widens its own surface with `use_tools(pack)`. Exposing all 132 would cost
~12,200 tokens of schema **per turn** against ~1,600 for core.

| Pack | Tools | What |
|---|---|---|
| `core` | 19 | Screen reading, tapping, typing, app launch, tool discovery |
| `phone` | 29 | SMS, calls, camera, GPS, sensors, torch, TTS, notifications, Wi-Fi |
| `x` | 23 | X/Twitter timelines, search, feed-shaping controls |
| `system` | 14 | Battery, network, storage, clipboard, waiting |
| `instagram` | 11 | Profiles, grids, reels, comment threads |
| `learn` | 10 | Teach the agent an app it has never seen |
| `files` | 8 | Read, write, search files across both filesystems |
| `instagram_web` | 7 | Instagram over HTTP — no device, far faster |
| `instagram_capture` | 7 | Segmented reel recording via MediaProjection |
| `shell` | 4 | Arbitrary commands at both privilege levels |

Full catalogue: **[docs/TOOLS.md](docs/TOOLS.md)**.

### Teaching it a new app

No selectors for the app you care about? The `learn` pack is the discovery loop:

```
explore_screen()      → what anchors exist, which repeat, which are interactive
diff_after_action()   → scroll; what changed is content, what stayed is chrome
propose_selectors()   → draft a field map
learn_screen()        → save it, pinned to the app version
```

---

## Performance

Measured on the target device — the numbers that shaped the design:

| Path | Per screen read |
|---|---|
| `uiautomator dump` CLI, over Wi-Fi | 2540 ms |
| `uiautomator dump` CLI, on-device | 2520 ms |
| **uiautomator2 server, over Wi-Fi** | **220 ms** |
| **uiautomator2 server, on-device** | **306 ms** |
| **accessibility bridge, on-device** | **13 ms** |

Two honest conclusions: the naive CLI is 11× slower than a persistent server,
and **on-device is not faster per call** — it is slightly slower. On-device is
worth it for autonomy and for moving the expensive model turns off the laptop,
not for transport latency. Details: **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**.

---

## Safety

- **Use an expendable phone.** This agent has shell access, your messages, your
  camera and your files.
- `phone_sms_send` and `phone_call` are **denied by default** even in `auto`
  mode — they reach real people and cost money. Enable by name.
- The HTTP server binds loopback only unless you pass `--host`, and then
  requires a bearer token. It can do anything to the phone.
- `--mode readonly` is a genuine read-only mode; use it when exploring.
- Automated collection generally breaches the terms of the platforms in the app
  packs. Wrapping it in an agent changes the convenience, not the permission.

Out of scope by design: CAPTCHA solving, device-identity spoofing, root
exploits, consent-dialog bypass. Android's security model is treated as a
boundary, not an obstacle.

---

## Contributing

New tools are genuinely easy to add — one decorated function in a pack. Adding
an app is a selector JSON plus a module. See
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

Wanted: more app packs (WhatsApp, YouTube, Maps, Gmail), a local-model prompt
profile that survives 20 steps, an on-device AccessibilityService backend, and
iOS via WebDriverAgent.

## Credits

The device layer, structured-UI extraction and selector registry come from
[MobileAgentMCP](https://github.com/Arnavvs/MobileAgentMCP), this project's
predecessor.

MIT licensed.
