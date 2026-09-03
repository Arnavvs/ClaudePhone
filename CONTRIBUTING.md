# Contributing

The most useful contributions are **new tools** and **new app packs**. Both are
small, self-contained, and testable against a real phone in a few minutes.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first — particularly
[Packs](docs/ARCHITECTURE.md#packs), which explains why tools are gated and why
adding everything to `core` is the one change that will be refused.

---

## Getting set up

```bash
git clone https://github.com/Arnavvs/ClaudePhone && cd ClaudePhone
pip install -e . uiautomator2
claudephone doctor
```

You need a real Android device. An emulator works for the `core`, `files` and
`shell` packs but not for `phone` (no telephony/sensors) or the app packs.

To develop **on the phone**, see [docs/SETUP-PHONE.md](docs/SETUP-PHONE.md).

---

## Adding a tool

A tool is one decorated function. Return a `dict`; the registry builds the JSON
schema from your type hints and defaults.

```python
# src/claudephone/tools/system_tools.py

@reg.tool(
    description="Report the current screen orientation and whether rotation "
                "is locked. Use before assuming coordinates are stable."
)
def screen_orientation() -> dict:
    raw = dev.shell("settings get system user_rotation", check=False).strip()
    return {"rotation": raw, "degrees": {"0": 0, "1": 90, "2": 180,
                                         "3": 270}.get(raw)}
```

Then regenerate the catalogue:

```bash
python scripts/tools_catalogue.py --write
```

### What makes a good tool description

The description **is** the interface — it is all the model sees. Compare:

```python
# bad: says what it is, not when to use it
description="Swipes the screen."

# good: says what it does, when, and what a caller gets wrong
description=("Swipe. direction: up|down|left|right, or give explicit "
             "x1,y1,x2,y2. `up` advances content (next reel/post).")
```

Rules that keep the surface usable:

- **First sentence must stand alone.** `docs/TOOLS.md` and the CLI show only
  that, and cheap models weight it heaviest.
- **Say when *not* to use it.** `screenshot` says "expensive next to ui_dump —
  use only when pixels genuinely matter". That sentence prevents a lot of waste.
- **Name the failure.** If a tool needs a permission or a precondition, say so
  in the description, and return a `permission_hint` when it is missing.
- **Return structure, not prose.** `{"followers": {"raw": "1.2M", "value": 1200000}}`
  beats `"1.2M followers"`. Include the raw string so a parse can be audited.
- **Never guess.** A missing value is `null` plus an entry in `_unavailable`. A
  plausible wrong number is worse than a gap.

### Picking a pack

| Put it in | If it |
|---|---|
| `core` | is needed to orient on *any* screen. Very high bar — 19 tools today |
| `system` | reports device state |
| `phone` | uses a radio, sensor, camera or telephony |
| `files` | touches the filesystem |
| `shell` | executes arbitrary commands |
| `learn` | helps map an unknown app |
| a new pack | is specific to one app |

Mark `dangerous=True` if it changes device state, costs money, or reaches
another person. If it reaches another person, also add it to `OUTBOUND` in
`agent.py` so it is denied unless named explicitly.

---

## Adding an app pack

1. **Explore the app with the agent itself** — this is what the `learn` pack is
   for:

   ```bash
   claudephone run --pack learn "open WhatsApp and map the chat list screen"
   ```

   `explore_screen` → `diff_after_action` → `propose_selectors` → `learn_screen`.
   Whatever changes when you scroll is content; whatever stays is chrome.

2. **Save selectors as data**, in `src/claudephone/selectors/<app>.json`, pinned
   to the app version you verified against. Mark intermittent anchors
   `optional` — Instagram's `scrubber` appears in ~29% of dumps, and treating it
   as required makes every dump look broken.

3. **Write the module** in `src/claudephone/tools/apps/<app>.py` exposing
   `register(reg)`, and add a `with reg.pack("<app>")` block in `agent.py`.

4. **Record what you learned about the app** — bugs, intermittent elements,
   surfaces where the same button means different things. Encode it in the
   registry so it is not rediscovered as "drift".

---

## Testing

There is no mock device, deliberately: every bug worth catching here came from a
real handset behaving unlike the docs.

```bash
claudephone doctor                                # the whole chain
claudephone tool <name> --args '{"...":"..."}'    # one tool, no model
claudephone run --mode readonly "<goal>"          # a loop that cannot mutate
```

Before opening a PR:

- [ ] `claudephone doctor` passes
- [ ] your tool works from **both** a laptop and on-device (the two uids see
      different filesystems — see `file_tools._route`)
- [ ] `python scripts/tools_catalogue.py --write` committed
- [ ] no secrets, session files, or `artifacts/` output

---

## Style

Match the surrounding code. A few conventions that are load-bearing:

- **Comments explain *why*, and cite the measurement.** `device.py` says the CLI
  is 2540 ms against 220 ms for the persistent server, because that is why the
  code is shaped the way it is. Do not write comments that restate the code.
- **Fail loud, return the error.** `ToolRegistry.call` never raises; tools
  should return `{"error": ...}` with something actionable, so the model can
  correct itself rather than the run dying.
- Standard library over dependencies — this installs on a phone, and every
  C-extension is a thing that will not build (`lxml` and `Pillow` both need
  Termux's prebuilt packages).
- No emoji in code or commit messages.

---

## Out of scope

Contributions in these areas will be declined regardless of quality:

- CAPTCHA solving, bot-detection evasion, device-identity spoofing
- Root exploits or attempts to bypass consent dialogs (MediaProjection consent
  is one tap per session and cannot legitimately be avoided)
- Anything whose purpose is to make automated activity look human in order to
  defeat a platform's enforcement

Android's security model is treated as a boundary, not an obstacle. Automation
that a platform's terms forbid remains forbidden when an agent does it.

---

## Wanted

- App packs: WhatsApp, YouTube, Maps, Gmail, Spotify
- A local-model prompt profile that survives 20 steps
- An on-device AccessibilityService backend behind the same tool surface
- Reboot-survivable adb-over-TCP on ColorOS (does `persist.adb.tcp.port` work?)
- iOS via WebDriverAgent
