"""MCP tool annotations: which tools only look, and which can do harm (B12).

MCP clients use `readOnlyHint` to decide what may run without asking, and
`destructiveHint` to decide what deserves a warning. So a wrong hint in the
permissive direction is a safety bug, not a cosmetic one, and the rules here
are conservative on purpose:

* **Read-only is an explicit list**, never inferred from a name. A tool is on
  it only if it observes without changing the phone, spending ledger budget, or
  handing back private content. That rules out, for example, `phone_sms_list`,
  `clipboard_get`, `notifications` and `read_file`, which change nothing but
  return other people's messages and files; and `ig_open_profile`, which only
  reads but navigates and is a counted read.
* **Destructive** is every tool in a dangerous pack, every OUTBOUND tool (they
  reach other people), and the account writes that are not in a dangerous pack
  (`x_feed_like`, `tg_join`, ...).
* **Everything else gets no destructive hint at all.** Per the MCP spec a
  missing hint means "may be destructive", which is the truth for `tap`: it
  does whatever the button under it does.
"""

from __future__ import annotations

READ_ONLY = frozenset({
    # the screen and the device, observed
    "ui_dump", "look", "find_element", "extract_fields", "screenshot", "wait_for",
    "wait_stable", "foreground_app", "device_info", "devices", "screen_state",
    "battery_status", "network_info", "storage_info", "list_apps", "list_files",
    "phone_battery", "phone_info", "phone_camera_info", "phone_sensors",
    # the harness's own state
    "bridge_status", "ledger_status", "list_tool_packs", "find_tool",
    "observe_stats", "recall", "registry_info", "check_drift",
    "ig_capture_status", "ig_capture_list",
    # file listings (names, not contents)
    "list_dir", "find_files",
})

# Account writes and other effects that are not in a dangerous pack.
DESTRUCTIVE_EXTRA = frozenset({
    "x_feed_like", "x_feed_not_interested", "x_feed_pin",
    "tg_join", "tg_leave", "tg_mute", "tg_send", "tg_reply",
    "phone_sms_send", "phone_call", "delete_file", "write_file", "edit_file",
    "ig_web_login", "phone_clipboard_set", "clipboard_set",
})


def hints(tool) -> dict:
    """Keyword arguments for mcp.types.ToolAnnotations."""
    from .agent import OUTBOUND
    name = tool.name
    if name in READ_ONLY:
        return {"readOnlyHint": True, "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": False}
    if tool.dangerous or name in OUTBOUND or name in DESTRUCTIVE_EXTRA:
        return {"readOnlyHint": False, "destructiveHint": True,
                "openWorldHint": name in OUTBOUND or name.startswith(("tg_", "x_", "ig_"))}
    return {"readOnlyHint": False}


def batch_refusal(tool) -> str:
    """Why a tool may not run inside `batch`, or "".

    A batch is approved once. A destructive or outbound call inside one would
    skip the per-call approval the client would otherwise ask for.
    """
    h = hints(tool)
    if h.get("destructiveHint"):
        return tool.name + " can change accounts or data; call it on its own"
    return ""
