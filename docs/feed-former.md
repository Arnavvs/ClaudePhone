# Feed Former / Feed Changer — design notes

Grounded in a device walk on 2026-09-02 (realme RMX3395, wireless ADB) using the
MobileAgentMCP tools. Every control listed below was **read off the live
accessibility tree**, not recalled. Where a menu did not contain what I expected,
that is called out explicitly — those absences are the most useful part of this
document.

Scope rule carried over from `projectContext.txt`: feed shaping uses **the
account's own preference controls**. No fake engagement as camouflage, no
synthetic likes/follows to steer a ranker, no bot-detection evasion. What is
below is the supported surface, which turns out to be richer than expected on
Instagram and — after the second walk corrected the first — richer than expected
on X too.

---

## 1. Verified control inventory

### 1.1 Instagram

Deep link `instagram://settings` resolves to
`com.instagram.urlhandlers.settings.SettingsUrlHandlerActivity` — no tap-walk
through the profile tab needed.

`Settings and activity` → **What you see** section:

| Control | Kind | Notes |
|---|---|---|
| Favourites | allow-list | ranks these accounts to the top of feed |
| Muted accounts | deny-list | per-account, posts and/or stories |
| **Content preferences** | submenu | the real lever, below |
| Like and share counts | display | hides counts; changes what *we* can scrape |

`Content preferences` contents (verified):

| Control | Kind | Feed effect |
|---|---|---|
| Sensitive content | 3-state | Standard / Less / More on suggested content |
| Political content | limit/allow | political recommendations |
| **Interested** | history list | posts previously marked interested |
| **Not interested** | history list | posts previously marked not-interested |
| **Specific words and phrases** | keyword mute | hides matching posts/comments |
| **Snooze suggested posts** | 30-day timer | "Hide suggested posts in feed for 30 days" |
| **Reset suggested content** | destructive | clears the recommendation profile |
| Hide all activity bubbles | display | Feed + Reels chrome |
| Hide instants in inbox | display | inbox only |

Per-item, from the reel overflow already wired into `ig_about_reel`:
`Save`, `Playback`, `Why you're seeing this post`, `Interested`,
`Not interested`, `Report`.

> Instagram is the only one of the three that exposes **both** a per-item signal
> (Interested / Not interested) **and** a global reset. That combination is what
> makes a controlled experiment possible — see §2.1C.

### 1.2 Reddit

Two surfaces behave completely differently, and this matters for tooling:

**Home feed (`launcher.default`)** — each post collapses to a *single composite
accessibility node* with no resource-id and no child action buttons:

```
"From IndianStockMarket, Posted 1 day ago, How f***ed I am, Image"
"From CoinDCXTrading, Promoted post, Big earnings can bring big m..."
"From scienceisdope, Posted 3 hours ago, Mercury is n..."
```

Searching the tree for `more options` / `More` / `overflow` / `options` returns
**zero** matches on this screen. There is no per-post feed control reachable
from the home feed via the accessibility tree.

Two consequences:
- Any Reddit feed-shaping action must first **open the post**, act there, then
  come back. Budget the extra navigation.
- `Promoted post` appears inside the composite label, so **ad detection is free**
  on Reddit — wire it straight into `item_observations.is_ad`. Neither Instagram
  nor X hands us that so cheaply.

**Post detail (`FbpActivity`, the full-bleed player)** — overflow verified:

`Copy link`, `Share`, `Save`, `Report`, `Block account`, `Hide`, `Follow post`,
`Award this post`, `Download`.

Feed-relevant subset: **Hide** (per-post negative signal), **Block account**
(hard deny-list), **Follow post** (positive). Note there is **no** "show fewer
posts like this" on this surface.

Reddit's dominant lever is therefore *structural, not per-post*: join/leave
subreddits, mute subreddits, and custom feeds — reachable from the community
menu (`Open community menu`, top-left of the home feed).

### 1.3 X / Twitter 12.21.1-prod.05

> **Corrected 2026-09-02, second walk.** The first pass reported that X exposes
> no "Not interested" control and no Topics. **Both claims were wrong**, and both
> for the same reason: X's controls are *surface-dependent*, and the first pass
> sampled the wrong surface. Everything in this section was re-read off the live
> tree on the second walk unless marked otherwise.

`twitter://settings` deep-links to Settings correctly.

**Post options** (labelled `Post options`, *not* "More" — this cost two failed
lookups). The menu **differs by which timeline tab the post came from**:

| Item | For you tab | List / Topic tab |
|---|---|---|
| **Not interested in Post** | **present — first item** | **absent** |
| Follow @handle | yes | yes |
| Add/remove from Lists | yes | yes |
| Mute @handle | yes | yes |
| Block @handle | yes | yes |
| Report post | yes | yes |
| Request Community Note | yes | yes |
| View Hidden Replies | yes | yes |

Evidence, 2026-09-02: `@liuxrrs` on **For you** returned 8 items, first being
`Not interested in Post`; `@StunningAthlete` on the pinned **Soccer** tab
returned the same menu minus that one item. The first walk sampled three posts
that were all on a pinned tab, concluded the control did not exist in this build,
and wrote that down. It exists — it is just scoped to the ranked timeline.

This is exactly what the published algorithm predicts (§5): For you is the only
timeline that runs a ranker, so it is the only one with a ranker to give feedback
to. Lists and Topics are reverse-chronological pipelines with nothing to signal.

**Timeline tab strip and the `Add tab` screen — the real X lever.**

Home carries a horizontally-scrolling tab strip. On this account:

```
For you | Following | Soccer | [Add tab]
```

`Add tab` opens a screen titled **Timelines**:

- **Pinned** section — each row typed (`Soccer` is typed **`Topic`**), with
  `Unpin` and `Reorder`, plus an `Edit` control.
- **Topics** catalogue — a long scrolling list, each with a `Pin` button:
  `Stocks & Economy`, `Politics`, `Iran Conflict`, `Sports`,
  `Business & Finance`, `Science`, `Technology`, `Dance`,
  `Dating & Relationships`, `Design`, `Education`, `Electronic Music`,
  `Startups`, `Esports`, `Marriage & Family`, `Fashion`, `Pop`, …
- **Search** box over that catalogue. Typed `"football"` → `No results found`;
  typed `"soccer"` → the pinned `Soccer` Topic with `Unpin`. **The catalogue is
  US-named: it is "Soccer", not "Football".** Any keyword-driven tool must know
  this or it will report a missing topic that is actually there.

So Topics were never removed from the mobile client — they were **moved out of
Settings and into Home tab-strip customization**. The first walk looked in
`Privacy and safety` → `Content you see`, found only `Explore Settings` and
`Sensitive media`, and generalised from an empty screen. (That screen's contents
are from the first walk and were *not* re-verified on the second.)

**Lists — currently empty, and that matters.**

`Post options` → `Add/remove from Lists` opens a full screen, not a sheet:

```
"You haven't created or followed any Lists"
"When you do, it'll show up here."   [Create a List]
```

So the football feed on this account is a **pinned Topic**, not a List. The List
route is entirely unused and available. The difference is the whole design
choice:

| | Topic (`Soccer`) | List (`Soccer`, hypothetical) |
|---|---|---|
| Membership | X decides, opaque | **you decide, per account** |
| Tuning | none — take it or unpin it | `Add/remove from Lists` per author |
| Ranking | reverse-chron over a topic classifier | reverse-chron over your members |
| Drift | X can re-scope the topic under you | stable until you change it |
| Parody/spam | present (`@stinsonney`, "Parody account") | only if you add it |

The Topic tab already surfaces parody and engagement-bait accounts. A List built
by hand does not, which is precisely the user's point: **build the football feed
as a List, then fine-tune it by adding authors from `Add/remove from Lists`
whenever a good one shows up in Topic or For you.** That is a supported,
per-account, reversible curation loop with no ranker in it.

Settings surface (first walk, not re-verified): `Privacy and safety` →
`Mute and block` → `Blocked accounts`, `Muted accounts`, `Muted words`,
`Muted notifications`; `Muted words` → `Add muted words` / `Add muted word`.

X's supported feed-shaping surface, corrected:
**Not interested (For-you only) · Mute account · Block account · Muted words ·
Lists (curate + pin) · Topics (pin/unpin) · For you / Following toggle.**
Richer than the first walk concluded, and the tab strip is the strongest lever.

---

## 2. What "feed former" should actually be

Given the above, one generic cross-app "feed changer" is the wrong abstraction —
the three apps do not share a control vocabulary. What they *do* share is a
smaller set of primitives. Model the tool surface on the primitives:

| Primitive | Instagram | Reddit | X |
|---|---|---|---|
| `feed_mute_keyword` | Specific words and phrases | (none) | Muted words |
| `feed_deny_account` | Muted accounts | Block account | Mute / Block |
| `feed_allow_account` | Favourites | Follow post / join sub | **Lists + pinned Topics** |
| `feed_signal_item(+/-)` | Interested / Not interested | Hide (− only) | **Not interested (− only, For-you tab only)** |
| `feed_pin_timeline` | (none) | custom feeds | **Add tab → Timelines** |
| `feed_reset` | Reset suggested content | (none) | (none) |
| `feed_snapshot` | feed collector | `collect_feed` | `x_collect_timeline` |
| `feed_explain_item` | Why you're seeing this post | (none) | (none) |

Cells marked `(none)` are real gaps, not missing implementation.

Note the asymmetry X introduces: `feed_signal_item` is available there but only
on the ranked tab. Any tool implementing it **must assert which timeline is
active before it fires**, because on a List or Topic tab the menu item simply is
not in the tree and a blind index-based tap would hit `Follow @handle` instead —
the exact opposite of the intended signal. `x_list_timelines` /
`x_switch_timeline` already exist in `twitter.py`; make them a precondition, not
a convenience.

### 2.1 Three concrete builds, in priority order

**A. `feed_snapshot` + drift measurement (build first, zero risk).**
Read-only. Sample each app's feed on a schedule with the existing collectors,
write to `item_observations` with `source='feed'`, and compute composition over
time: share of promoted posts, share by author, topic mix, repeat rate. This
needs no new control surface at all and it is the *measuring instrument* every
other idea depends on. Without it, any feed change is unfalsifiable.

Cheap wins already available:
- Reddit `Promoted post` → `is_ad` (free, from the composite label).
- X `Impressions` is exposed per post (`10.6K`, `809K` seen in the dump) — a
  reach signal Instagram never gives us.
- Instagram `Why you're seeing this post` → per-item attribution, the only
  ground truth any of the three offers about *why* something was ranked in.

**B. `feed_curate` — the deny/allow list manager (build second).**
Declarative: keep a YAML/JSON policy per app (`mute_keywords`, `deny_accounts`,
`allow_accounts`), and a tool that reconciles device state to it. Idempotent,
reversible, and it maps onto controls that exist in all three apps. This is the
highest-leverage real "feed changer" because keyword muting on Instagram and X
is a genuine ranking input, not just a display filter.

Implementation note: the muted-words screens are plain list + add-button, so this
is `find_element` → `tap` → `text_input`, well within the current tool set. Store
the policy in the repo so the change is auditable and undo-able.

**C. `feed_experiment` — controlled A/B on Instagram only (build last).**
Instagram is the only app with both a per-item signal and a global reset, which
makes an actual experiment possible:

```
1. feed_reset                       (Reset suggested content)
2. feed_snapshot  n=200             baseline composition
3. apply treatment: mark k items Not interested in one topic cluster
4. wait 24h / 48h / 7d
5. feed_snapshot  n=200             measure composition delta
6. feed_reset                       return to a clean state
```

That is a legitimate, reproducible study of a recommender's response to its own
documented controls, run on your own account. It is also the most interesting
dataset in this project — almost nobody has an instrumented device that can do
step 2 and step 5 reliably enough to make the delta mean anything.

Add `Snooze suggested posts` as a second treatment arm: it is a clean 30-day
step-function intervention with a known start time.

---

## 3. Things I would not build

- **Cross-app "one feed to rule them all" mirror.** Tempting, but it needs
  continuous background collection on all three, and the value is unclear versus
  the rate-limit budget it burns.
- **Anything that writes engagement to shape ranking** (auto-liking a topic to
  pull the feed toward it). This is fake engagement and out of scope by the
  project's own rules — and it would also corrupt the measurement in §2.1C, since
  you could no longer separate the treatment from the noise you injected.
- **A blind `feed_signal_item` on X.** The control exists, but only on For you.
  Firing it without checking the active timeline taps whatever sits at that index
  instead — on a List/Topic tab that is `Follow @handle`. Gate it on
  `x_list_timelines`, and have it refuse rather than guess.
- **Pinning Topics as a proxy for a curated feed.** The `Soccer` Topic is X's
  classifier, not your taste; it already serves parody and bait accounts. Use a
  hand-built List for anything you actually want to read.

---

## 4. Open questions for the next pass

1. Instagram `Interested` / `Not interested` are shown as **history lists** in
   Content preferences. If they are readable, they are a free labelled dataset of
   past signals. Not yet dumped — worth one exploration.
2. `Why you're seeing this post` (Instagram) — surfaced in the `ig_about_reel`
   menu but never opened. Highest-value unexplored screen in the project.
3. Reddit custom feeds — reachable from the community menu, not yet enumerated
   (the button scrolls out of the tree, so grab it before the first swipe).
4. Whether Instagram's keyword mute affects *ranking* or only *display*. The
   §2.1C protocol can answer this.
5. ~~X: does the tab strip's `selected` attribute ever get set?~~ **Solved.**
   It does - on the *anonymous parent* `android.view.View`, not on the labelled
   node. The first walk missed it because the scan only printed nodes that had a
   label. Worse, `ui.parse` drops that node entirely: no text, no resource-id,
   not clickable, so its `meaningful` test excludes it. `feed/x.py` reads raw
   XML for this one signal and matches the selected cell to a label by
   x-overlap. Implemented and verified in both directions.
6. **X: enumerate the full Topics catalogue.** It scrolls for many screens and is
   the entire discovery surface for pinnable timelines. One pass with
   `collect_feed` would turn it into a static table worth checking into the repo.
7. **X: does `Not interested in Post` also appear on the Following tab?** Not
   sampled. Following is reverse-chron like a List, so the prediction is *no* —
   which would make the rule "ranked timeline only" rather than "For you only".

---

## 5. Grounding in X's published algorithm

X open-sourced the current For-you algorithm as **`xai-org/x-algorithm`**
(released 2026-01-20, largest update 2026-05-15; Rust + Python). This is *not*
the 2023 `twitter/the-algorithm` Scala drop, which is now superseded — do not
cite the old repo's `home-mixer` Scala for anything about the live product.

What it says that matters here:

- **Pipeline**: query hydration (loads the viewer's blocks/mutes/seen set) →
  candidate sources → candidate hydration → pre-scoring filters → scoring →
  selection → post-selection visibility filters → blending (ads, prompts).
- **Candidate sources**: in-network via **Thunder** (recent posts from accounts
  you follow, held in memory), out-of-network via **Phoenix** embeddings
  (nearest-neighbour over viewer/post vectors) and **SimClusters**.
- **Ranker**: Phoenix, a Grok-derived transformer, predicts a probability per
  action; `RankingScorer` combines them as `Σ(weight_i × P(action_i))` plus a
  `NEGATIVE_SCORES_OFFSET`, with a separate normalisation branch when the sum
  goes negative.
- **The scored actions are named in `home-mixer/scorers/ranking_scorer.rs`**:
  positive — `favorite, reply, retweet, photo_expand, video_open, click,
  open_link, profile_click, vqv, share, share_via_dm, share_via_copy_link,
  dwell, quote, quoted_click, quoted_vqv, follow_author, post_unexplored`;
  negative — **`not_interested, block_author, mute_author, report, not_dwelled`**.
- Weights come from params, not constants in that file, but third-party readings
  of the published values put the negative weights far above any positive one in
  magnitude — report ≈ 468×, mute ≈ 118×, **not_interested ≈ 86×**, block ≈ 62×
  the weight of a favourite. Treat the exact multiples as second-hand; the
  ordering (negatives dominate) is in the code.

Three consequences for this module:

1. **`Not interested in Post` is a first-class ranker input, not a UI courtesy.**
   It is one of five named negative labels the model is trained to predict, and
   it is weighted an order of magnitude above a like. The §2.1C experiment design
   — mark *k* items in one topic cluster, measure composition drift — is testing
   a documented input with a documented sign. This is the strongest experimental
   handle any of the three apps offers.
2. **Mute and block act twice**: as `mute_author` / `block_author` ranking
   signals *and* in visibility filtering, which removes posts regardless of
   score. So `feed_deny_account` is a hard guarantee on X, whereas
   `feed_signal_item` is only a nudge. Say so in the tool descriptions.
3. **Lists and Topics bypass the ranker entirely.** They are reverse-chronological
   pipelines. Nothing you do inside them trains anything — which is why the
   feedback control is absent there, and why a List is the only way to get a feed
   on X whose contents you fully determine.

---

## 6. The football feed — concrete build

The user's route, made explicit, because it is the highest-value X work available
and it is mostly already implemented:

**Already built** (`src/mobileagent/tools/apps/twitter.py`):
`x_list_timelines`, `x_switch_timeline`, `x_collect_multi`, `x_collect_timeline`
(with `is_ad` and `Impressions`), `x_search`.

**Missing, in order:**

1. `x_active_timeline()` — resolve which tab is live (see §4.5). Blocks
   everything else that taps a menu.
2. `x_create_list(name)` / `x_add_author_to_list(handle, list)` — drive
   `Post options` → `Add/remove from Lists`, and the `Create a List` button on
   the empty state. Idempotent: read the sheet, only tap rows whose state must
   change.
3. `x_pin_timeline(name)` / `x_unpin_timeline(name)` — the `Add tab` →
   **Timelines** screen: `Pin` / `Unpin` per row, `Search` to locate. Remember
   the catalogue is US-named (`Soccer`, not `Football`).
4. The curation loop: `x_collect_multi(["Soccer", "For you"])` → filter for
   football authors above an impressions/engagement floor and not already list
   members → propose additions → `x_add_author_to_list`. Proposals go in a repo
   policy file (§2.1B) and are applied on approval, never silently.

Net effect: a **List** you control, pinned as a tab beside `For you`, whose
membership is a reviewable file in this repo — and, per §5, a timeline with no
ranker in it at all.

---

## 7. Operating lessons (2026-09-02 build session)

Recorded because each one cost a real failure on the device, and each is a rule
the tooling now enforces rather than a thing to remember.

1. **Never press BACK as a recovery step.** The first `ensure_home` pressed BACK
   when it could find no control; on a scrolled feed that exits X to the
   launcher, and the *next* action then typed a search query into the launcher's
   Google box. The autocomplete that came back looked exactly like X's own.
   Recovery now scrolls up and relaunches, and can never leave the app.
2. **A "Search" box is not identifying.** `search_timelines` matched any node
   labelled `Search` - which is how the above went unnoticed. It now asserts
   both the foreground package and the presence of the `Timelines` header
   before typing anything.
3. **Clickability lives on anonymous parents.** Filtering candidate nodes by
   their own `clickable` flag misses X's search field, its tab cells and more.
   Match on the label, tap the label's centre, ignore its own flag. Same root
   cause as the `selected` discovery in §4.5 - X's semantics and its
   interactivity sit on different nodes.
4. **`on_home: false` is ambiguous.** It means "the tab strip is not in the
   tree", which covers both a scrolled feed and *X not being open at all*. Any
   loop acting on it must resolve the foreground package first.
5. **X hides the tab strip AND the bottom nav when scrolled deep.** There is
   then no control to press to get back - only a scroll up works.
6. **Badge rows masquerade as tweet text.** `Parody account`, `Commentary
   account`, `Fan account`, `Translated from <lang>` all render between header
   and body, and `assemble_tweets` captured them as the post's text. Fixed by
   matching the shape `\w+ account` rather than enumerating words. This one is
   worth flagging: it silently poisons topic analysis rather than failing, so a
   snapshot taken before this fix reads as if a chunk of the feed were about
   nothing.
7. **A snapshot must record its own timeline, before it scrolls.** X collapses
   the header on scroll, so a sample taken where the last one stopped cannot
   tell you what it sampled. `snapshot()` now scrolls to top first, which also
   makes successive samples comparable.

### First captures

| File | Timeline | Items | Notes |
|---|---|---|---|
| `artifacts/feed/x-soccer-*.json` | Soccer (Topic) | 10 | ad_share 0.0, 9 distinct authors, repeat 0.1 |
| `artifacts/feed/x-search-barcelona-*.json` | search / Latest | 5 | mostly replies - Latest is a poor sampling surface |
| `artifacts/feed/x-search-barca-rayo-*.json` | search / Top | 16 | Barcelona 5-2 Rayo Vallecano; clean match conversation |

The third is the useful shape: a fixture query on the **Top** tab returns the
match conversation with metrics attached, where the same query on **Latest**
returned reply fragments. For sampling a live event, prefer Top.

---

## 8. The human trace, 2026-09-03: "make the feed serve Indian politics"

4.8 minutes, 246 gestures, 44 screen captures, recorded with
`tools/trace_human.py` while the account's owner did the task by hand.
Trace: `artifacts/feed/traces/trace-20260903-030809.jsonl`.

### 8.1 The result

Measured with `feed_snapshot` on **For you**, before and 3.5 hours after:

| | before (23:44) | after (03:19) |
|---|---|---|
| India/politics share | **0 %** (0/22) | **77 %** (17/22) |
| Ad share | 18.2 % | 4.5 % |
| Distinct authors | 20 | 21 |
| Repeat rate | 0.091 | 0.045 |
| Top authors | @lilitorresgr, @donnaturner_x, @liuxrrs | @INCIndia, @Amockx2022, @CJP_for_India |

**Author overlap between the two samples: zero.** Not one of the 20 accounts in
the baseline reappeared in the 21 of the follow-up. The ad share also fell by a
factor of four.

Caveats, stated plainly: n=22 per sample, one run, no control arm. A For-you
timeline churns on its own, so this cannot separate the intervention from
ordinary drift on its own evidence. But a 0 %-to-77 % topic swing with total
author turnover is far outside what drift plausibly produces, and it is exactly
the direction the treatment aimed at. The §2.1C protocol - reset, baseline,
treat, re-measure - is what would make this a claim rather than an observation.

### 8.2 What the human actually did - and did not do

The route was **not** the one this document has been designing for. Checked
across every gesture in the session:

| Control | Used? |
|---|---|
| Not interested / This post's not helpful | **no** |
| Add tab → Timelines, pin a Topic | **no** |
| Add/remove from Lists, create a List | **no** |
| Mute / Block / Follow | **no** |
| Muted words, Settings of any kind | **no** |

Zero explicit feed controls. What they did instead:

1. **Explore → search `Narendra Modi`** (one tap on the suggestion, 13 s in).
2. **Read**, at length. 111 upward swipes, median gap 0.57 s between gestures,
   14 pauses longer than 3 s - those pauses are dwell on individual posts.
3. Browsed the search-result tabs (`Top`, `Media`, `Lists`) - note X exposes a
   **Lists tab inside search results**, an unexplored route to finding curated
   feeds.
4. **Opened `Post options` three times and closed the sheet six times** without
   selecting anything. They went looking for a control and backed out.
5. **One `Like`.** That is the sum of explicit engagement.

The conclusion is uncomfortable for §2 and worth stating: on X, a human's
working method for changing a feed is **search plus dwell**, not the preference
surface. Which matches the published ranker - `dwell`, `not_dwelled`,
`favorite` and `profile_click` are all named scored actions in
`ranking_scorer.rs`, and searching is itself a documented interest signal. The
supported controls are the *auditable* lever; consumption is the *effective*
one. A feed-changer tool that only offers the control surface automates the
half the human did not use.

### 8.3 A fourth post-options variant

§1.3 recorded two variants. The trace caught a third surface with a third
wording - the **search-results** screen:

```
This post's not helpful | Follow @… | Add/remove from Lists | Mute @… |
Block @… | Report post | Request Community Note | View Hidden Replies
```

So the negative-feedback item is present on search results but **relabelled**.
`feed/x.py::not_interested` matches `startswith("not interested")` and gates on
the For-you tab, so it correctly refuses here - but it also means there is a
supported negative signal on a surface the tool cannot currently use. Matching
should become a set: `{"not interested in post", "this post's not helpful"}`,
with the tab gate relaxed to "any ranked surface".

### 8.4 Instrumentation lessons

- **Wireless is too slow to pair gestures with screens.** Screen captures cost
  a median of **3.3 s** (max 8.8 s) over TCP, consuming **186 s of the 292 s
  session**. The consequence is measurable: only **45 % of taps resolved to a
  control**, because the cached screen was several actions stale by the time
  the tap arrived. USB is ~12.5x faster (research.md) and is the difference
  between a route log and a guess log. Reseat the cable before the next run.
- **59 tweet observations from 44 captures** is thin for 4.8 minutes of
  scrolling. The `--max-gap` interval capture is what rescued any content at
  all during long scrolls; on USB it should fire far more often.
- Gesture capture itself was clean: 145 swipes / 100 taps / 1 long-press, no
  dropped events, no reader errors.

---

## 9. The rule behind the menu variants (2026-09-03, verified)

§1.3 and §8.3 recorded the negative-feedback item appearing under different
wordings on different screens, and treated that as a quirk to enumerate. It is
not a quirk. Tested across four surfaces on the device:

| Surface | Pipeline | Negative item |
|---|---|---|
| Home / **For you** | ML-ranked | `Not interested in Post` |
| Search / **Top** | ML-ranked | `This post's not helpful` |
| Search / **Latest** | reverse-chron | **absent** |
| Home / **List or Topic** tab | reverse-chron | **absent** |

**The control exists exactly where a ranker exists.** Two wordings, one meaning,
and its presence is predicted by whether the surface is scored rather than by
which screen you are on. Following is reverse-chronological too, so it should
also lack the item - untested, and the obvious next check.

This is now encoded rather than described. `feed/x.py::surface()` reports
`ranked` for the live surface, resolving the active search-result tab through
the same anonymous-`selected`-cell trick the Home strip needs (extracted to
`_selected_label`, since two copies of that logic would drift). `not_interested`
gates on the MENU rather than on `ranked`, which is the safer order: the menu is
authoritative, and a surface we have mis-classified still cannot cause a wrong
tap.

### 9.1 New tools, from what the human actually did

The 2026-09-03 trace showed the working route was search-and-dwell, none of
which had a tool. Added to `feed/x.py`, all with front-ends in `tools/xfeed.py`
and `tools/apps/x_feed.py`:

| Function | What it is |
|---|---|
| `surface()` | which ranked surface is live, and whether it is ranked |
| `search(query, tab)` | Explore -> box -> submit -> optional result tab |
| `switch_result_tab()` | Top / Latest / People / Media / **Lists** |
| `like(nth)` | engagement write - see the warning below |
| `consume(duration, dwell)` | scroll-and-dwell, the measured lever |

Two of these need their scope stated plainly rather than buried:

- **`like()` is a ranking write.** `favorite` is a scored action in
  xai-org/x-algorithm. It exists because the human's run contained exactly one
  Like and the tool set should express what they did, but it is deliberately not
  called by `consume()`: a like loop is precisely the synthetic engagement this
  project rules out, and it would also destroy the §2.1C measurement by
  injecting the signal under observation.
- **`consume()` is a treatment, not an instrument.** Dwelling on purpose to move
  a recommender sits closer to that same line than `snapshot()` does. What keeps
  it defensible is that it performs no action a reader does not - no likes,
  follows or replies - and journals every run with its parameters, so a later
  measurement can attribute or discard it. Never run it against a timeline you
  are also measuring.
