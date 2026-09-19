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
(6) become one-line **step capsules** (below). Without this, a 30-step run on a
cheap model either overruns the context window or costs several times what it
should. The message *shape* is preserved, so the model still sees that a call
happened and what it was.

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
  not — after six steps that result is a one-line capsule. The screen is the
  input half of every training example; the capsule is not enough for that.
- **It cannot break a run.** Every filesystem call is guarded, a failure latches
  recording off for the rest of the run, and `start()` returns `None` on an
  unwritable directory so the run proceeds unrecorded rather than failing. An
  agent halfway through driving a real phone must not die because a disk filled.

```bash
claudephone runs                 # what it has done, newest first
claudephone runs <run_id>        # replay one, step by step
claudephone runs <run_id> --json # raw events, for a training set
```

The recorder knows what happened but not whether it was any good. A person can
append a label afterwards:

```python
from claudephone.harness import recorder
recorder.label("20260905-213700-a3f2", success=True, note="two extra dumps")
```

### Automatic verification and decision records (B8)

Manual labels meant almost no run had one. Replay (B9) and any learned verifier
later need runs labelled reliably. So a task can now declare what success looks
like, in checks a program decides (`harness/verify.py`):

| check | passes when |
|---|---|
| `answer` | the final answer matches a regex |
| `reached_text` | a regex matches text on any screen read |
| `final_text` | a regex matches text on the last screen read |
| `package` / `reached_package` | that app was in front at the end / at any point |
| `field` | a tool result carried that field with a non-null value |
| `milestones` | several texts were reached, in order (MobiFlow's DAG, as a sequence) |

- **Three outcomes, not two.** Each check is `pass`, `fail` or `inconclusive`. A
  run passes only if every check passes; one fail fails it. `final_text` on a
  run from before B8, or on a last screen read more than 120 s before the end, is
  inconclusive, not failed.
- **A model only when unsure, and only when asked.** With `--judge`, an
  inconclusive run gets one helper call. The helper sees the record, not the
  phone, and may answer `unsure`. It is never asked when the checks decided.
- **Screen evidence is screen evidence.** Text counts as reached only if it came
  from a screen read: element texts, `appeared` lists, the final screen. A tool's
  own words never count. `find_element` saying "no element matching 'Screen
  timeout'" is not evidence of reaching the Screen timeout screen.
- **The verdict is appended as a `label` row** (`by: "verify"`), next to any
  human label, never replacing one. `claudephone runs` shows it.

```bash
claudephone run "What is the screen timeout?" --expect '\b10\s*min' --reached 'screen timeout'
claudephone run "<goal>" --verify evals/tasks/screen_timeout.json [--judge]
claudephone verify latest --expect 'april\s*2022'        # check an old run
claudephone verify <run_id> --verify spec.json --no-label   # look, don't write
```

**Decision records.** Every step also writes one `decision` record to the run
file:
- the clickable and scrollable elements of the screen the model last read (the
  candidates);
- the call it made, with the element it aimed at resolved from its ref, index or
  tap point;
- what the screen did next.

That is V-Droid's training format. Nothing trains on it now; it is logged because
it cannot be recovered later. A step on an already-listed screen points back
(`candidates_as_step`) instead of repeating the list. Decision records and a
`final_screen` record go to the file only, never the event stream.

Verified 2026-09-19:
- **The six A/B runs recorded that day,** checked read-only: every verdict matched
  the A/B's own grading. One DeepSeek run *had* reached a screen showing "Screen
  timeout" before it ran out of steps. The grader could not see that; the
  `reached_text` check did.
- **A live DeepSeek run on the Samsung:** checked and labelled `fail` automatically
  when it ended, with 8 decision records and a final screen 0.13 s old.

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

### Account writes are judged by target, and counted by the ledger

The `dangerous` flag is per TOOL, so it cannot tell a tap on "Close" from a tap
on "Follow". `policy/writes.py` judges every tap by the element it will actually
hit - the re-found target and whatever sits under the tap point, whichever is
stricter - using `policy/writes.json`:

| verdict | examples (Instagram ids verified on IG 440/446) | what happens |
|---|---|---|
| read | author name, Comment, Share, Playback | tap |
| forbidden | `like_button`, `save_button`, Repost, share-sheet recipients, Add to story, comment likes, the comment gift button, More-sheet Save/Report, unfollow | refused, always - unless the operator allows that rule id for the run (`--allow-rule`) |
| write | Follow, Interested, Not interested, Telegram Join, X Not interested | only if the run enabled it (`--allow-write follow`), the mode is not readonly, the phone's account is known, and datacollect's ledger (`budget_for`) has room this minute, hour and day; then recorded in the ledger with the run id |

Every "no" fails closed: no ledger reachable (e.g. on the phone itself), unknown
phone, or an account at its ceiling all refuse. `verify=false` skips the re-read
but never the gate. The X feed tools and `tg_join` use the same gate for their
`apply=true` writes. Verified live on the Samsung with a dry-run dispatcher: Like
and Save refused with zero dispatches, More-sheet rows classified as above.

### Budgeted reads are counted too (B2b)

The one restriction this project has had came from READ volume (316 profile
opens in 27 minutes), not writes. `policy/reads.py` puts every budgeted read
through the same ledger, with the same action names and pacing as datacollect's
phases:

| read | counted by |
|---|---|
| `profile_open` | `ig_open_profile`; taps on a reel author, avatar, search-result user, "Go to X's profile" |
| `grid_scan` | `ig_scan_grid`, `ig_scan_reels_grid` (one per call) |
| `reel_open` | taps on grid tiles; each post `ig_collect_posts` reads; a forward swipe in a reel viewer opened from a grid |
| `reel_walk` | `ig_collect_reel_details` (one per walk, as phase F counts it) |
| `feed_reel` | `reset_reels_feed`, a tap on the Reels tab, each forward swipe in the Reels tab (including off a "Suggested for you" card) |
| `sheet_open` | `ig_about_reel`; taps on the reel More button |
| `comment_read` | `ig_collect_reel_comments(open_sheet=true)`; taps on Comment / the caption |
| `search` | `text_input` into an Instagram search box |
| `tg_read` | every Telegram chat opened: `tg_open(chat)`, every tool that takes `chat`, each row `tg_chats(deep=true)` opens |

Before the read: hour and day through `Ledger.can` (so `budget_for` and
`ACCOUNT_SCALE`), then a wait of up to 75 s for the per-minute ceiling. After
it: one ledger row, `run_id` `claudephone:<run>`, note `claudephone read`. Loops
re-check each item and stop at a ceiling rather than overrun it. Refusals are
returned as `{"error": "ledger refused this read", "read": {...}}` before
anything is tapped or deep-linked.

Fails closed like writes: an unknown phone or no reachable ledger refuses the
read — which means **on the phone itself these tools refuse** until a ledger
service exists, unless the operator runs with `--allow-uncounted-reads` (or
`CLAUDEPHONE_UNCOUNTED_READS=1`); results then say `counted: false`.

Typing is also judged by target: `text_input` and Enter are refused while a
comment / DM / reply box is on screen (`composers` in `writes.json`: resource
ids plus a hint pattern), rule id `any.composer`.

X and Telegram reads count too, under names datacollect's `BUDGET` has no entry
for: `x_search`, `x_scroll` (one timeline read), `x_consume` (one dwell session),
`x_sheet_open` and `tg_search`. `budget_for` falls back to `DEFAULT` (4/min,
60/hour, 300/day) for an unknown action and still applies `ACCOUNT_SCALE`, so
they are bounded without editing the ceiling table - giving them explicit
ceilings is Arnav's call. Generic swipes in X count in **batches of ten**
(`reads.bucketed`), which bounds a runaway loop at 40 swipes a minute without
pacing a dwell-based read down to one swipe every 15 s.

`ledger_status` reports, per platform, which account this phone maps to, whether
the ledger is local or the service, and what is left for each action today.

Still not counted: LinkedIn (ClaudePhone has no LinkedIn tools; that leg lives in
datacollect), and swipes on other apps' screens. datacollect's phases call MobileAgentMCP, not these tools,
so nothing is counted twice. Verified live on the Samsung (IG 446): 10 counted
reads, 10 ledger rows, typing and Enter refused in the comment sheet, and an
author tap with no ledger refused with zero dispatches.

### The ledger, reachable from the phone (B2c)

The gate needs `datacollect/collect.db`, which is on the laptop, not in Termux,
so an agent running ON the phone had every budgeted write and counted read
refused. `policy/ledger_service.py` closes that: the laptop serves the ledger on
loopback and the phone reaches it through `adb reverse` - the mirror of the
bridge's `adb forward`.

```bash
python -m claudephone.policy.ledger_service --serial RZ8N70HYQSB   # laptop
export CLAUDEPHONE_LEDGER_URL=http://127.0.0.1:8770                # phone
export CLAUDEPHONE_LEDGER_TOKEN=...                                # printed above
```

`RemoteLedger` answers the same four calls as datacollect's `Ledger` - `budget`,
`can`, `count`, `record` - so `writes.py` and `reads.py` cannot tell which one
they hold, and nothing changes when the URL is unset. **The laptop stays the only
thing that reads a ceiling or writes a row:** the phone sends an account, an
action and a count, and the service answers from `budget_for`, scaling included.
A service that stops answering raises the same `LedgerUnavailable` as a missing
database, so it fails closed. Auth mirrors the bridge: a token in
`~/.claudephone/ledger_token` (0600), constant-time compared, required on
everything but `/health`; the socket binds `127.0.0.1` only.

Verified from the Samsung over `adb reverse`: `/health` 200 without a token,
`/budget` 401 without it and 200 with it, and @saravbhaita's ceilings came back
scaled to 25% with the review note attached.

### App cards and verified deep links (B6)

An agent on the phone never read PROJECT-CONTEXT, so without help it would
rediscover each trap - Instagram 446's search going silent after one query, the
reel overlay lagging the swipe, a sheet left open stranding the next step - at
the cost of steps and, on Instagram, of reads counted against the account.

**Cards** (`src/claudephone/cards/<package>.md`) hold that knowledge per app:
Instagram, Telegram and X today. The first time an app with a card comes to the
front, the card goes into the conversation - once per run, so a run that never
opens Instagram never pays for the Instagram card.

**`open_link(url)`** replaces a navigation sequence with one intent - on
Instagram, search + typing + tapping a result becomes a single profile open. It
fails in ways that look like success, so it is strict:

- only prefixes in `cards/deeplinks.json` are followed, and only once
  `claudephone deeplinks --verify` has watched them land on a real phone;
- the foreground must become the package the registry names - landing anywhere
  else is an error, and nothing is recorded in the ledger;
- a locked or sleeping phone is refused up front, because on a lock screen every
  deep link "succeeds" and every read comes back empty (PROJECT-CONTEXT §6);
- the URL comes from the model and goes into a shell command, so anything
  outside a URL-safe character set is refused rather than escaped;
- the read it stands for is still counted (a profile opened by link is a
  `profile_open`).

Verified 2026-09-18: `instagram://user?username=` (IG 447) and
`twitter://user?screen_name=` (X 12.25) on the Samsung; `https://t.me/` and
`tg://resolve?domain=` (Telegram 12.10) on the realme, which is where Telegram is
installed - on the Samsung both correctly reported "did not land". The first
verification attempt found the Samsung's screen had locked; all four were refused
with nothing spent.

**Notes now go in after a turn's tool results.** Stagnation warnings and cards
are held until every tool call in the model's turn has its result. A user message
between two tool results breaks the tool-calling format, and the B4 code also
broke out of the batch on a warning, leaving the model's remaining calls with no
result at all - reproduced on the old code: three calls, two results.

### Step capsules, `remember` and `recall` (B7)

Until B7, a result older than six steps was cut to its first 220 characters. On
a profile pass the follower count read at step 3 was gone by step 10: the model
either re-read the screen - a counted read on Instagram - or guessed. And the
cut was blind: 220 characters of a screen dump are mostly JSON keys.

Now an aged-out result becomes one line, built by `harness/history.py` from
what the loop already knows - the call, the screen fingerprint and labels
before and after - with no model call:

    T+00:03 #4 ui_dump(limit=40) -> content changed (com.android.settings);
    appeared: 'Digital Wellbeing & parental controls', ... +3 more
    | full result: recall(steps=[4])

- **Neutral wording.** A capsule says what was observed - `content changed`,
  `same items, positions moved`, `screen unchanged`, `no screen read`,
  `returned an error: ...` - never "successfully", "failed" or "navigated to". A
  verdict in history gets believed later, even when it was wrong.
- **`remember(key, value)`** pins a fact for the whole run. Notes ride in the
  system message on every call and are never compacted. A note that reads like
  a verdict is kept, with a hint to record what was seen instead.
- **`recall(steps=[n])`** returns step n's full result from the run's own JSONL;
  **`recall(query=...)`** searches every earlier result and thought. Recall
  never finds its own earlier results. With recording off, it reads an
  in-memory copy.
- **JSON-mode runs are compacted now.** Results were found by `role == "tool"`,
  but in json mode a result is a user message, so a json-mode run was never
  compacted at all. Results are now marked by step, whatever the convention.
- `remember` and `recall` neither advance nor reset the stagnation idle count:
  pinning three notes is not "the screen has not changed in three steps".

Verified 2026-09-19 on both phones (Settings, scripted model: no model quota,
no ledger reads). Six old results became capsules naming what each scroll brought
into view. The pinned note stayed in the system message. `recall(steps=[1])`
returned step 1's 3.7-4 KB result. `recall(query=...)` found the label at steps
1 and 10, where the list had been scrolled back.

### Handing back to a person, and the screens that are not ours to clear (B3)

Two directions, and they are not the same:

- **`request_human(reason)`** - the agent gives up; the run ends with
  `stopped_by="human_required"` and the reason. Nothing waits.
- **`ask_operator(question)`** - the agent needs one fact and can continue with
  it. The run BLOCKS. On the CLI the question goes to the terminal; over HTTP the
  task emits an `ask` event carrying a `session_id` and waits for
  `POST /reply {"session_id", "answer"}`. No channel, or no answer in time, ends
  the run rather than letting the model guess.

The third path is not the model's to decide. Doctrine is that a phone meeting a
checkpoint or 2FA **stops that account entirely**, and a cheap model looking at
"We detected unusual activity" will keep tapping, because tapping is what it
does. So `harness/handoff.py` checks every screen the loop sees, and a match ends
the run whatever the model intended - before the next tool call, not after it.
The prompt says the same in words; the guard is what actually holds.

What it matches: suspicious/unusual activity, "confirm it's you", security
checks and CAPTCHAs, "action blocked", "try again later", two-factor and code
prompts, disabled or restricted accounts - plus **a password field**, which has
no business appearing in a session on an account that is already signed in.
Ordinary words like "Log in" are deliberately not matched; they appear on
screens that are perfectly safe.

Verified live: pointed at a page containing the phrase, the run stopped at step 1
with `human_required` and the evidence attached. That run also shows the honest
limitation - **it matches text, so a screen that merely mentions a challenge
trips it too** (there, a search box containing the phrase). Stopping a run that
did not need stopping is the cheap direction of that error, and
`checkpoint_guard=False` exists for the rare case where it is wrong.

### A stuck run says so, instead of burning the budget (B4)

Cheap models loop, and these apps give them reasons to: Instagram 446's search
goes quiet after the first query in a session, swipes do not always advance, and
a tap on a control that has scrolled away does nothing at all. Left alone the
model repeats the call until `max_steps` ends the run — the most expensive way to
fail, reported under the wrong reason.

`harness/stagnation.py` watches for one narrow thing: **the same tool, the same
arguments, and no evidence the screen moved.**

| attempts | what happens |
|---|---|
| 2 | a `POSSIBLY STUCK` note goes into the conversation, telling the model to read the screen and change approach rather than retry |
| 3 | the run ends with `stopped_by="stagnation"`, naming the tool and arguments |

Repeating a call that *does* move the screen is progress through a feed and is
never flagged. A screen matching one seen `revisit_gap` steps earlier produces a
softer hint ("you were on this screen at step N") because circling back is
sometimes the right route. The fingerprint comes from `state.remember`, which
every read already computes, so this costs nothing.

**No fingerprint at all counts as no progress, not as progress.** That was found
live: 25 identical swipes with no `ui_dump` between them ran to `max_steps`
without a word, because "unknown" was being treated as "the screen moved". With
that fixed, the same run stops in 3 steps, or 5 with reads interleaved.

`--no-stagnation-stop` keeps the warning and drops the stop, which is what an
operator watching a run by hand usually wants. The prompt carries the matching
operating rules — wait at most three times, check the last action took effect,
lengthen a swipe that did nothing then reverse it, one query per tab, three
routes then report — so the model has the same policy the harness enforces.

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
