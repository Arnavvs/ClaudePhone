APP CARD - X (com.twitter.android). What this project learned the hard way:

- The post-options menu differs by surface: For you, search results and List tabs each show different items. Read the labels first (x_feed_post_options) and act by label, never by position - on some surfaces the position of "Not interested" holds "Follow @handle", the opposite signal.
- Likes, reposts, bookmarks and follows are writes. The harness refuses them unless this run was given that write.
- Reads are counted: timeline reads (x_scroll), searches (x_search) and post menus (x_sheet_open).
- To search, use x_feed_search(query) - it does Explore > search box > submit in one call.
- Timeline tabs: x_feed_timelines reports which one is active; the header collapses when scrolled, so x_feed_home brings the tab strip back.
- A login or "verify it's you" screen: call request_human and stop.
