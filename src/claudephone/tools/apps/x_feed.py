"""MCP surface for X feed control.

Thin registration only - the logic lives in `mobileagent.feed.x` so the CLI in
`tools/xfeed.py` and the server run exactly the same code paths.

Mutating tools default to `apply=False` and return the tap they *would* make.
That is deliberate: these act on a real account, and an agent that has to ask
twice cannot fire one by accident on the first.
"""

from __future__ import annotations

from ...feed import x as xf


def register(mcp) -> None:

    @mcp.tool(
        description=(
            "List X's Home timeline tabs and report which one is ACTIVE. "
            "The active flag lives on an anonymous node that ui_dump filters "
            "out, so this is the only reliable source for it. Returns "
            "active=null rather than guessing when the dump is mid-animation. "
            "Call before any per-post feed action - the post-options menu "
            "differs by tab."
        )
    )
    def x_feed_timelines() -> dict:
        return xf.timelines()

    @mcp.tool(
        description=(
            "Get X to the top of Home, where the tab strip exists. Relaunches "
            "the app if it is not in the foreground and scrolls up if the "
            "header is collapsed. Never presses back, so it cannot leave X."
        )
    )
    def x_feed_home() -> dict:
        return xf.ensure_home()

    @mcp.tool(description="Switch to a named X timeline tab (e.g. 'For you').")
    def x_feed_switch(name: str) -> dict:
        return xf.switch_timeline(name)

    @mcp.tool(
        description=(
            "Open the nth visible post's overflow menu and return its items as "
            "LABELS. Read this before acting: X's menu is surface-dependent, "
            "so positions are not stable across tabs."
        )
    )
    def x_feed_post_options(nth: int = 0) -> dict:
        r = xf.post_options(nth)
        xf.close_sheet()
        return r

    @mcp.tool(
        description=(
            "Send the nth visible post a negative ranking signal - one of five "
            "negative labels the X ranker predicts, weighted well above any "
            "positive engagement. X words it per surface: 'Not interested in "
            "Post' on For you, \"This post's not helpful\" on search results, "
            "and it is absent on List/Topic tabs. Matches the label set rather "
            "than the tab, and refuses when none is present - tapping by "
            "position there would hit 'Follow @handle', the opposite signal. "
            "Plans by default; apply=true fires. Journalled to artifacts/feed/."
        )
    )
    def x_feed_not_interested(nth: int = 0, apply: bool = False) -> dict:
        return xf.not_interested(nth, apply=apply)

    @mcp.tool(
        description=(
            "Open 'Add/remove from Lists' for the nth post. With no list_name "
            "this only reports which Lists exist. Pass apply=true with a "
            "list_name to toggle membership."
        )
    )
    def x_feed_lists(nth: int = 0, list_name: str = "",
                     apply: bool = False) -> dict:
        return xf.add_to_list(nth, list_name, apply=apply)

    @mcp.tool(description="Open X's 'Add tab' -> Timelines customization screen.")
    def x_feed_timelines_screen() -> dict:
        return xf.open_timelines_screen()

    @mcp.tool(
        description=(
            "Search the Topics/Lists catalogue on the Timelines screen. Note "
            "the catalogue is US-named: 'football' returns nothing, 'soccer' "
            "matches - an empty result is not proof of absence."
        )
    )
    def x_feed_search_topics(query: str) -> dict:
        return xf.search_timelines(query)

    @mcp.tool(
        description=(
            "Pin or unpin a Topic/List as a Home tab, from the Timelines "
            "screen. Plans by default; pass apply=true to fire."
        )
    )
    def x_feed_pin(name: str, unpin: bool = False, apply: bool = False) -> dict:
        return xf.pin(name, unpin=unpin, apply=apply)

    @mcp.tool(
        description=(
            "Sample the live X timeline to artifacts/feed/ as JSON, with "
            "composition stats (ad share, distinct authors, repeat rate, top "
            "authors) and the timeline it was captured from. This is the "
            "measuring instrument: without a baseline, no feed change is "
            "falsifiable."
        )
    )
    def x_feed_snapshot(max_tweets: int = 40, max_swipes: int = 20) -> dict:
        r = xf.snapshot(max_tweets, max_swipes)
        r.pop("tweets", None)
        return r

    @mcp.tool(
        description=(
            "Report which ranked surface is on screen: a Home timeline tab, "
            "search results, or neither. Feed controls are surface-dependent, "
            "so the same tap means different things in each."
        )
    )
    def x_feed_surface() -> dict:
        return xf.surface()

    @mcp.tool(
        description=(
            "Search X: Explore -> search box -> submit, optionally switching "
            "result tab (top|latest|people|media|lists). This is the first half "
            "of the only route measured to move this feed - see feed-former.md "
            "8.2. Read-only."
        )
    )
    def x_feed_search(query: str, tab: str = "") -> dict:
        return xf.search(query, tab=tab)

    @mcp.tool(
        description=(
            "Switch between Top / Latest / People / Media / Lists on a search-"
            "results screen. Note Lists: search exposes curated feeds to follow, "
            "an alternative to building one by hand."
        )
    )
    def x_feed_result_tab(name: str) -> dict:
        return xf.switch_result_tab(name)

    @mcp.tool(
        description=(
            "Like the nth visible post. ENGAGEMENT WRITE: 'favorite' is a scored "
            "action in X's ranker, so this steers the feed rather than reading "
            "it. Use singly and deliberately, never in a loop - a loop breaks "
            "the project's no-synthetic-engagement rule and corrupts any "
            "measurement running alongside. Plans by default; apply=true fires."
        )
    )
    def x_feed_like(nth: int = 0, apply: bool = False) -> dict:
        return xf.like(nth, apply=apply)

    @mcp.tool(
        description=(
            "Read a feed by scrolling and dwelling, the way a person does. This "
            "is the lever measured to work: a human moved For you from 0% to "
            "77% Indian politics in five minutes by search plus sustained "
            "reading, and 'dwell' is a scored action in X's ranker. It is a "
            "TREATMENT, not an instrument - do not run it against a timeline "
            "you are also measuring. Performs no likes, follows or replies. "
            "Returns what it read, and journals every run. apply=true to fire."
        )
    )
    def x_feed_consume(duration_s: float = 120.0, dwell_min: float = 1.5,
                       dwell_max: float = 6.0, apply: bool = False) -> dict:
        r = xf.consume(duration_s, dwell_min, dwell_max, apply=apply)
        r.pop("tweets", None)
        return r
