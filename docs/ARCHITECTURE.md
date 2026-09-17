# Architecture

How ClaudePhone works, and why it is built this way. Where a decision was made
against an obvious alternative, the measurement that decided it is included.

- [The problem](#the-problem)
- [The privilege trick](#the-privilege-trick)
- [Layers](#layers)
- [Packs](#packs)
- [The agent loop](#the-agent-loop)
- [Two tool-calling conventions](#two-tool-calling-conventions)
- [Delegate vs proxy](#delegate-vs-proxy)
- [Permissions](#permissions)
- [Selectors and drift](#selectors-and-drift)
- [What this is not](#what-this-is-not)

---

## The problem

An agent that drives a phone from a laptop has a structural weakness: the model
doing the reasoning is not where the work happens. Every `tap` is a network
round trip, every screen read ships kilobytes back across a link, and the whole
arrangement stops the moment the laptop sleeps or the Wi-Fi drops.

The obvious fix is to move the agent onto the phone. The obvious justification
for that fix — "local tool calls will be faster" — turns out to be **wrong**,
and it is worth being precise about why, because the real justification is
different and leads to a different design.

Measured on the target device (full table in [BENCHMARKS.md](BENCHMARKS.md)):

| Path | Median per screen read |
|---|---|
| `uiautomator dump` CLI, over Wi-Fi | 2540 ms |
| `uiautomator dump` CLI, on-device via loopback | 2520 ms |
| **uiautomator2 persistent server, over Wi-Fi** | **220 ms** |
| **uiautomator2 persistent server, on-device** | **306 ms** |
| **accessibility bridge, on-device** (phase 2) | **13 ms** |

Two things fall out of this:

1. **The transport was never the bottleneck.** Moving on-device saved 20 ms out
   of 2540. What saved 2300 ms was abandoning the `uiautomator dump` CLI — which
   spawns a fresh instrumentation process per call — for a persistent server.
   The remaining 220 ms turned out to be a property of *uiautomator2*, not of
   Android: the accessibility bridge reads the same screen in **13 ms**,
   because it walks a tree it already holds in process.
2. **On-device is measurably *slower* per call** (306 ms vs 220 ms). The phone's
   A78 cores serialise the view hierarchy more slowly than the laptop consumes
   it over Wi-Fi.

So on-device execution is not a latency optimisation. It is justified by three
other things, and the design leans on those instead:

- **Autonomy.** The phone runs untethered. Nothing breaks when the laptop
  sleeps, the Wi-Fi flaps, or you walk out of the house.
- **Where the expensive turn happens.** With the laptop as the brain, *every*
  step costs a turn of a large frontier model carrying a large context. With
  the phone as the brain, each step costs a turn of a cheap model, and the
  laptop spends exactly one delegation. That is the actual saving, and it is
  large — see [Delegate vs proxy](#delegate-vs-proxy).
- **Compound tools.** `collect_feed` scrolls a timeline thirty times and returns
  one deduplicated list. On-device, those thirty screen reads never touch the
  network at all.

---

## The privilege trick

Everything rests on one fact about Android that is easy to miss.

Termux runs as an ordinary app uid (`u0_a308` here). That uid is **denied** the
commands an automation agent actually needs:

```
$ input tap 1 1                          # from Termux directly
  DENIED
$ settings get system screen_brightness
  DENIED
```

The Android *shell* uid (2000) is allowed all of them. Normally you reach uid
2000 from a computer over USB. But `adbd` listening on TCP does not care where
the client is — including a client on the same device:

```
$ adb connect 127.0.0.1:5555     # run from inside Termux
$ adb -s 127.0.0.1:5555 shell id
uid=2000(shell) gid=2000(shell) groups=2000(shell),1004(input),1007(log),...
```

**The phone drives itself at shell privilege, unrooted, with no Shizuku.** One
Android dialog authorises the Termux-generated adb key, once, and
`/data/misc/adb/adb_keys` remembers it.

The consequence for the code is that there is only one code path.
`device.shell()` shells out to `adb -s <serial>`; the serial is a LAN address
from a laptop and `127.0.0.1:5555` on the phone. Nothing above that layer knows
or cares which.

```
┌── laptop ───────────┐          ┌── phone ─────────────────────────────┐
│ claudephone         │          │  Termux (uid 10308)                  │
│   device.shell() ───┼── Wi-Fi ─┼─▶ adbd ──▶ uid 2000 ──▶ input/uiauto │
└─────────────────────┘          │     ▲                                │
                                 │     │ 127.0.0.1:5555                 │
                                 │  claudephone (same code)             │
                                 └──────────────────────────────────────┘
```

**Caveat.** Wireless adb generally stops listening after a reboot, which takes
the loopback with it. `claudephone doctor` reports this precisely, and
[SETUP-PHONE.md](SETUP-PHONE.md#surviving-a-reboot) covers the options.

---

## Layers

```
  cli.py            run · serve · tools · tool · doctor · mcp
  ────────────────────────────────────────────────────────────
  server.py         POST /task  (delegate)   POST /tool (proxy)
  bridge/           laptop-side MCP: phone_task, phone_tool
  ────────────────────────────────────────────────────────────
  harness/loop.py   see → act → see, budgets, compaction
  harness/models.py OpenRouter | local llama.cpp  (one wire format)
  harness/registry  132 tools in 10 packs, JSON schemas
  ────────────────────────────────────────────────────────────
  tools/            device · ui · input · system · shell · files
                    phone · learn · instagram · x
  ────────────────────────────────────────────────────────────
  runtime/          observation cache, diffs, event-driven change detection
  runtime/bridge    the 13 ms AccessibilityService backend (phase 2)
  ui.py             XML hierarchy → compact typed elements
  selectors/        versioned per-app field maps + drift detection
  device.py         adb + uiautomator2, serial-agnostic
```

The bottom three layers are inherited from
[MobileAgentMCP](https://github.com/Arnavvs/MobileAgentMCP) and were ported
unchanged. That was possible because `ToolRegistry` deliberately exposes the
same `.tool(description=...)` decorator an MCP server does, so every existing
tool module registered against the new harness without being rewritten.

### Screens as data, not pixels

A raw uiautomator hierarchy for one Instagram reel is ~70 KB of XML.
`ui.parse()` flattens it to typed elements, typically 1–3 KB:

```jsonc
{
  "package": "com.instagram.android",
  "screen": "reels_viewer",
  "dump_ms": 224,
  "elements": [
    {"i": 12, "id": "clips_author_username", "text": "ally.verma_", "c": [402,1964], "f": "C"},
    {"i": 18, "id": "like_count", "text": "The like number is 9732.", "c": [990,1041], "f": "C"}
  ]
}
```

`i` is the element index; every read also returns `ver`, and taps take `ref="<ver>_<i>"`. Before tapping, the element is found again on a fresh read (`runtime/targeting.py`): if it moved the tap follows it, and if it is gone, replaced, hidden or covered by another window the tap is refused with the reason. A ref from an older screen version is refused outright. `c` is the tap centre. `f` flags
`C`lickable / `S`crollable / selected. `screenshot` exists but returns a **file
path**, never inline image data, so it cannot silently flood a context window.

One hard-won detail encoded here: **values often sit on anonymous child nodes**,
not on the resource-id that names them. Instagram's caption lives under
`clips_caption_component`; `media_album_art_button` merely carries the literal
string `"Audio"`. Every element therefore reports its `anchor` — the nearest
ancestor id — so a value can be found by the container that owns it.

---

## Packs

132 tools do not fit in a cheap model's head. Measured, as JSON schema:

| Exposed | Tools | Tokens per turn |
|---|---|---|
| `core` only | 19 | ~1,600 |
| everything | 132 | ~12,200 |

That is **7.8× the schema overhead on every single turn**, before the task is
even described — and cheap models also get measurably worse at picking the right
tool as the list grows.

So tools are grouped into packs and only `core` is loaded. The model widens its
own surface:

```
list_tool_packs()          → what exists, what is loaded
find_tool("send a text")   → searches unloaded packs too
use_tools("phone")         → loads it for the rest of the run
drop_tools("phone")        → hands the context back
```

`core` is chosen to be exactly enough to orient on an unknown screen and find
everything else: device info, `ui_dump`, `find_element`, the four input verbs,
and the four meta-tools.

---

## The agent loop

`harness/loop.py`. A generator, not a function — it yields events as they
happen, which is what makes both the live CLI output and the NDJSON stream
possible from one implementation.

```
start → [ thought → tool_call → tool_result ]* → final
                  ↘ note / budget / error
```

Three parts are not boilerplate:

**History compaction.** Screen dumps are large and highly repetitive — ten in a
row are mostly the same nav bar. Tool results older than `KEEP_FULL_RESULTS`
(6) are clipped in place to a stub. Without this, a 30-step run on a cheap model
either overruns the context window or costs several times what it should. The
message *shape* is preserved, so the model still sees that a call happened and
what it was.

**Budgets are enforced, not suggested.** Steps, wall-clock and cumulative tokens
each terminate the run, and the reason is reported in the `final` event rather
than being silently swallowed. An agent driving a real phone with no ceiling is
a bad idea.

**Errors are returned, not raised.** `ToolRegistry.call` never throws. A bad
argument comes back as `{"error": ..., "expected": <schema>}` so the model can
correct itself on the next turn instead of the run dying.

**Every run is written to disk.** `harness/recorder.py` appends each event to
`artifacts/runs/<run_id>.jsonl` as it is yielded. The hook is in `run()` rather
than in the CLI, so the CLI, `POST /task` and the laptop bridge behind it are
all covered by one place, and a fourth entry point cannot forget it.

Two details are the whole point of recording *here*:

- **Before compaction.** The event is recorded as yielded, so the file keeps the
  full screen dump the model was looking at when it chose. The conversation does
  not — after six steps that result is a 220-character stub. The screen is the
  input half of every training example; the stub is useless.
- **It cannot break a run.** Every filesystem call is guarded, a failure latches
  recording off for the rest of the run, and `start()` returns `None` on an
  unwritable directory so the run proceeds unrecorded rather than failing. An
  agent halfway through driving a real phone must not die because a disk filled.

```bash
claudephone runs                 # what it has done, newest first
claudephone runs <run_id>        # replay one, step by step
claudephone runs <run_id> --json # raw events, for a training set
```

The recorder knows what happened but not whether it was any good, so the outcome
label is appended afterwards by whoever watched:

```python
from claudephone.harness import recorder
recorder.label("20260905-213700-a3f2", success=True, note="two extra dumps")
```

Runs contain whatever the tools returned, including SMS, contacts and clipboard
contents. `artifacts/` is gitignored. `CLAUDEPHONE_RECORD=0` turns it off.

---

## Two tool-calling conventions

Cheap and local models are uneven about function calling, so the client supports
both and can switch mid-run:

- **`native`** — the OpenAI `tools` / `tool_calls` fields. Correct when
  supported, and what OpenRouter's hosted models use.
- **`json`** — the model writes a fenced object and the harness parses it out.
  Necessary for small local models (LFM2 and most sub-3B GGUFs) which have no
  tool-call training at all.

```json
{"tool": "ui_dump", "args": {"limit": 40}}
```

`auto` starts native. If the endpoint rejects the `tools` parameter, the client
permanently falls back to json **and the system prompt is rebuilt** to teach the
protocol — a fallback that silently left the model uninstructed would look like
the model simply refusing to act.

In json mode the loop enforces one call per turn, because small models reliably
lose track when asked to batch.

---

## Delegate vs proxy

The server exposes both, and the difference is the point of the project.

```
PROXY  (POST /tool)                 DELEGATE  (POST /task)
laptop model reasons                laptop sends one goal
  → tool call over network            → phone model reasons
  → phone acts                        → tool call over loopback
  → result over network               → phone acts
  → laptop model reasons              → repeat, locally
  → ... N expensive turns             → one result back
```

For a 30-step task, proxy mode spends 30 turns of a large model with a growing
context, plus 30 network round trips. Delegate mode spends **one** laptop turn
and 30 turns of a cheap model against localhost.

Proxy mode is kept deliberately: it is better for debugging, for one-off pokes,
and for tasks where you genuinely want the stronger model making every call.
`bridge/mcp_bridge.py` exposes both to a laptop Claude Code session as
`phone_task` and `phone_tool`.

Streaming is NDJSON — one event object per line, flushed as it happens — so a
watching client sees each tap land rather than a report at the end.

---

## Permissions

The project brief was "full phone control, no exceptions" on a dedicated
experimental handset, and the tool surface reflects that. The gate is therefore
about *avoiding accidents*, not about containing an attacker.

Every tool carries a `dangerous` flag. `Policy` sits in front of every call:

| Mode | Behaviour |
|---|---|
| `auto` | dangerous tools run (default) |
| `ask` | dangerous tools prompt on the terminal |
| `readonly` | dangerous tools are blocked |

Two tools are additionally in a **default deny list** regardless of mode,
because they reach other people and cost money:

```
phone_sms_send      phone_call
```

They run only when named explicitly: `--allow phone_sms_send`. `device_shell`
also refuses a small list of unrecoverable command patterns (`rm -rf /`, `mkfs`,
`fastboot`, …) unless `confirm_destructive=true` — a speed bump for a confused
agent, not a security boundary, since the shell is fully general.

The HTTP server binds `127.0.0.1` by default. Exposing it on the LAN requires
an explicit `--host` and then **mandates** a bearer token, generated on first
run into `~/.claudephone/token` (0600). This endpoint can do anything to the
phone; treat the token accordingly.

---

## Selectors and drift

App UIs change without warning. Selectors live as **data** in
`selectors/<app>.json`, pinned to the app version they were verified against.

```
check_drift()      → exactly which resource-ids vanished or appeared
record_baseline()  → pin the new version once you have re-verified
```

Three rules the registry enforces, each learned the hard way:

1. **Drift must be computed from the raw hierarchy**, not the filtered element
   list. Pure-layout containers are dropped from the element list as noise yet
   remain valid anchors, so checking the filtered set reports working selectors
   as missing — a false alarm that sends you chasing a non-existent app update.
2. **Intermittent anchors are marked `optional`** so they never read as drift.
   Instagram's `scrubber` appears in ~29% of dumps; treating it as required
   makes most dumps look broken and trains you to ignore the warning that
   matters.
3. **Fail loud.** A missing anchor returns `null` and appears in `_unavailable`.
   It never falls back to a guess — a plausible wrong value is worse than a gap.

The `learn` pack is the other half: `explore_screen` → `diff_after_action` →
`propose_selectors` → `learn_screen` lets the agent map an app nobody has
written selectors for. `diff_after_action` is the clever one — scroll, and
whatever changed is content while whatever stayed is chrome.

---

## What this is not

Stated plainly, because the boundary is a design decision rather than an
oversight:

- **Not an anti-detection tool.** No device-identity spoofing, no CAPTCHA
  solving, no root exploits, no consent-dialog bypass. Android's security model
  is treated as a boundary. MediaProjection consent in particular is one tap per
  capture session and cannot legitimately be avoided — not even by a
  device-owner app.
- **Not a way around platform terms.** Automated collection generally breaches
  the terms of the platforms in the app packs regardless of transport; wrapping
  it in an agent changes the convenience, not the permission.
- **Not multi-tenant.** One phone, one operator, full trust. The token protects
  against a stranger on your network, not against the operator.
- **Not a replacement for an AccessibilityService.** uiautomator2 *is* a
  `UiAutomation`, which is itself a special AccessibilityService, and Android
  permitted exactly one.
  **Corrected 2026-09-04: tested on Android 14, and false.** With the bridge
  enabled and serving, u2 still dumped normally at 215 ms. The bridge shipped
  as an **addition**, and reads the screen in ~13 ms - see
  [ACCESSIBILITY.md](ACCESSIBILITY.md).
