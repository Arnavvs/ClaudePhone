APP CARD - Telegram (org.telegram.messenger). What this project learned the hard way:

- Chat-list rows are custom-drawn: the accessibility tree has their rectangles but no names, previews or unread badges. Do not try to read the chat list; open chats by handle.
- Open a chat by link, not by searching: tg_open(chat="@handle") or open_link("https://t.me/HANDLE"). A handle that does not resolve leaves the chat list showing - that means "not found", not success.
- Every chat opened is counted against the ledger (tg_read). tg_catchup reads several channels in one call and is the cheap way to follow a set of them.
- Joining is a budgeted write (a handful a day). Never tap Join yourself, and never send a message.
- Backing out of an open search takes two presses of back.
- Channel view counters tick live, so the same message can look "changed" between two reads; compare author, time and text instead.
- A login or code prompt: call request_human and stop.
