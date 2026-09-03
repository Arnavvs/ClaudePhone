"""Expose the same tools over MCP stdio.

This is the compatibility path: it lets a laptop Claude Code session drive the
phone directly, exactly as MobileAgentMCP did, with no agent loop on the phone.
Every tool is exposed - MCP clients have their own way of managing a large tool
list, so the pack gating that protects cheap models is not applied here.

Prefer `bridge/mcp_bridge.py` for day-to-day use: it hands whole tasks to the
phone rather than making the laptop wait on each step.
"""

from __future__ import annotations

from .agent import build_registry

INSTRUCTIONS = (
    "Drives a physical Android phone.\n"
    "Loop: ui_dump to see the screen -> tap/swipe/text_input to act -> "
    "re-dump to confirm. Tap by element index `i` from the most recent dump.\n"
    "Prefer ui_dump over screenshot: it is far cheaper and machine-readable. "
    "Use wait_for instead of blind sleeps after actions that trigger loading.\n"
    "extract_fields returns typed values for known screens; check_drift tells "
    "you whether the app UI changed underneath you."
)


def main() -> None:
    from mcp.server import MCPServer

    mcp = MCPServer(name="claudephone", instructions=INSTRUCTIONS)
    reg = build_registry()
    for tool in reg.tools.values():
        mcp.tool(description=tool.description, name=tool.name)(tool.fn)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
