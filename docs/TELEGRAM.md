# Telegram

Verified against `org.telegram.messenger` **12.10.1** on the realme narzo 50 Pro
5G (RMX3395, Android 14), driven from a laptop over USB with uiautomator2.

Telegram is the hardest app in this repo to automate, and the reasons are worth
writing down because none of them are guessable from the outside.

## It publishes no resource-ids. At all.

Instagram anchors a caption to `clips_caption_component`; X anchors a tweet
header to `timeline_post`. Telegram anchors nothing. Every node in a dump comes
back as `anchor=content`, class `ViewGroup` or `View`, with no id — the entire
UI is drawn onto a canvas.

What it gives instead is unusually rich accessibility **text**. A whole message
— body, signing author, clock, reactions, view count — arrives as one string on
one node:

```
Short Term Swing

Indo Rama
70.8 to 78
Received at Kapil Verma (SEBI REGISTERED RA), 15:03
13 people reacted with ❤
Viewed 4360 times
```

So `tools/apps/telegram.py` is a **parser**, not a selector map.
`parse_message` splits that blob on the `Received at` / `Sent at` landmark:
everything before it is the body, everything after is metadata. The landmark
doubles as the direction flag — `Received` is inbound, `Sent` is yours.

Two renderings of the same field exist and both appear in one screenful, so
both are handled: reactions come as an inline list (`Reactions: ❤ 21, 👍 8`)
when there are several, and as prose (`13 people reacted with ❤`) when there is
one — with the count omitted entirely for a single reactor.

## Four traps, each of which fails silently

**1. The Send button's node is mostly dead space.** Its bounds span the whole
right end of the composer row (300 px wide), but the tappable circle sits at the
far right. Tapping the node's centre does nothing — no error, no send, the text
just sits in the box. `_tap_send` taps `x2 - height/2` instead. This cost an
hour before anyone thought to try a different pixel.

**2. `ui.parse` truncated every long post out of existence.** The shared parser
capped labels at 300 characters, which is right for apps that label one control
at a time. Here the cap decapitated long posts *and took their `Received at
HH:MM` with them* — so a long message stopped looking like a message and
vanished from the transcript entirely. Measured on one channel: **3 messages
read where there were 12.** `parse(max_text=...)` now exists and this module
passes `MAX_LABEL = 4000`.

**3. A deep link that does not resolve leaves the previous chat on screen.**
Telegram does not clear the view for a bad handle; it just stays put. An early
join run therefore reported two channels under handles that had never resolved —
it read the header of the channel it had opened a moment earlier and believed
it. Every tool that takes a `chat` now goes through `_open_guard`, which lands
on the chat list first so an unresolved handle is *detectable*, and reports it
rather than acting on the wrong chat.

**4. The chat list and the search overlay both show a "Search Chats" box.** With
an empty query the box's text still reads "Search Chats", so matching on it made
`tg_chats` scrape global-search hits as if they were the user's own chats. Only
the **New Message FAB** tells the two screens apart.

## What the app will not give you

**Chat-list rows carry no text.** The names, previews, timestamps and unread
badges you can plainly see in a screenshot are painted onto a canvas; the
accessibility tree exposes the rows as bare full-width rectangles with empty
`text` and `content-desc`. There is no unread count to read, from anywhere.

`tg_chats` is honest about this: the fast mode returns the row count and says
why it cannot name them, and `deep=true` opens each row to read its real title
(~1.4 s per chat). For following channels you already know, `tg_catchup` is much
cheaper than either.

**Global post search is Premium.** `tg_search(tab="posts")` searches the text of
public messages across all of Telegram — on a Premium account. Without one the
tab renders a sales pitch, which would otherwise be reported as "0 posts found"
and read as "nobody is talking about this". The tool detects the wall and says
so, and points at `tg_search_in_chat`, which is free.

**Search result labels are elided by the renderer.** A row reads
`Kkkkk, @stockmarketbulls2026, 323 subscri…` and a long handle comes back cut in
half. A handle from a search hit is a *lead*, not an identifier — every hit
carries `truncated` and its `raw` label, and `tg_info` resolves the real one off
the profile screen's invite link.

**`input text` is ASCII-only.** Non-ASCII is dropped by the shell input command
without complaint, so `tg_send`/`tg_reply` report what was dropped rather than
pretending the ✅ went through. Telegram also auto-capitalises the first
character of a sent message, which is why send verification compares
case-insensitively.

## Speed

Reading a channel used to take 21 s. It takes 0.6–3 s.

| | before | after |
|---|---|---|
| `tg_last`, channel already open | — | **0.6 s** |
| `tg_last`, channel visited before | — | **2.5 s** |
| `tg_last`, cold channel | — | 3.6 s |
| `tg_read`, 10 messages | 20.8 s | 6.9 s |
| `tg_read`, 25 messages | 31.9 s | 17.3 s |
| `tg_catchup`, 4 channels × 3 messages | — | 6.9 s |

Where it came from, in order of size:

* **Blind sleeps became polls.** Every intent was followed by `sleep(4.5)` — the
  worst case, paid every time. A warm Telegram opens a channel in under a
  second. `_wait_for` polls a predicate instead; `_settle` waits for a fling to
  stop rather than for a fixed duration.
* **`_settle` keys on message identity, not raw text.** A channel's view
  counters tick live — "Viewed 5302 times" becomes 5303 a second later — so a
  signature over raw labels never repeats and the settle budget was burned in
  full on every swipe. Hashing author + clock + body instead makes the screen
  hold still.
* **The reset is skipped when this module itself put the chat there.**
  `_LAST_OPEN` records the handle and title of the last deep link; a repeat
  visit to the same channel — the polling loop — skips straight to the link.
  Worth about two seconds a call.
* **Wider swipes.** A 45 % swipe yielded ~2 new messages per screen. 68 % yields
  3–4 and still overlaps enough to skip nothing.
* **Reads avoid `observer.look()`.** It calls `dev.foreground()`, a second
  ~200 ms adb round trip, which is most of the cost of a read on the u2 path and
  buys nothing here. `_read` uses the on-device bridge (~13 ms) when it is up
  and a bare u2 dump (~260 ms) otherwise — so every number above shrinks again
  when the agent runs on the phone.

## The tools

| Tool | What it is for |
|---|---|
| `tg_open` | Open Telegram, or one chat by handle / t.me link / tg:// URI |
| `tg_last` | The last few messages of a channel, as fast as possible |
| `tg_read` | Read a chat, scrolling back through history |
| `tg_catchup` | Latest messages from several channels, merged and tagged |
| `tg_chats` | The chat list (see the caveat above) |
| `tg_search` | Global search: chats, channels, apps, media, posts |
| `tg_search_in_chat` | Search inside one chat — free, unlike global post search |
| `tg_info` | Profile: title, subscribers, real @username, description |
| `tg_join` / `tg_leave` | Membership. `tg_leave` is gated as dangerous |
| `tg_mute` | Mute a channel — how you follow a dozen without drowning |
| `tg_send` / `tg_reply` | **OUTBOUND.** Denied unless named with `--allow` |

`tg_send` and `tg_reply` reach other people, so they sit in `agent.OUTBOUND`
alongside `phone_call` and `phone_sms_send`: being `dangerous` is not enough for
these, the operator has to name them.

## Reference: what a chat reference can be

`deeplink()` accepts all of these, because listicles and forwards publish all of
them:

```
@STOCKGAINERSS            -> tg://resolve?domain=STOCKGAINERSS
STOCKGAINERSS             -> tg://resolve?domain=STOCKGAINERSS
https://t.me/s/abhayvarn  -> tg://resolve?domain=abhayvarn
t.me/+iJXDrzNRT3w1ZWE1    -> https://t.me/+iJXDrzNRT3w1ZWE1   (private invite)
t.me/joinchat/AAAA        -> https://t.me/joinchat/AAAA
```

Private invites stay `https` — `tg://resolve` only takes public usernames.

## Tests

`python tests/test_telegram.py` — 66 checks, no phone required. Every string in
it was captured verbatim off the device, which is the only thing that makes a
parser test worth anything.
