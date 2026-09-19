"""Expose the same tools over MCP stdio.

This is the compatibility path: it lets a laptop Claude Code session drive the
phone directly, exactly as MobileAgentMCP did, with no agent loop on the phone.
Every tool is exposed - MCP clients have their own way of managing a large tool
list, so the pack gating that protects cheap models is not applied here.

Every tool carries MCP annotations (`mcp_hints.py`, B12): read-only tools can
be auto-approved by the client, destructive ones are flagged. `batch` runs
several calls in one round trip, but never a destructive one - that would let
one approval stand in for several.

Prefer `bridge/mcp_bridge.py` for day-to-day use: it hands whole tasks to the
phone rather than making the laptop wait on each step.
"""

from __future__ import annotations

from .agent import build_registry
from .mcp_hints import batch_refusal, hints

INSTRUCTIONS = (
    "Drives a physical Android phone.\n"
    "Loop: ui_dump to see the screen -> tap/swipe/text_input to act -> "
    "re-read to confirm. Tap with ref=\"<ver>_<i>\" exactly as the latest read "
    "returned it; a ref from an older read is refused on purpose.\n"
    "Prefer ui_dump over screenshot: it is far cheaper and machine-readable. "
    "Use wait_for instead of blind sleeps after actions that trigger loading.\n"
    "batch runs several non-destructive calls in one round trip.\n"
    "If anything fails in a way you do not understand, call diagnose(); every "
    "error also carries a `next` field saying what to do."
)

MAX_BATCH = 20


def run_batch(reg, calls: list, stop_on_error: bool = True,
              list_at_end: bool = False) -> dict:
    """Several tool calls in order, one response (B12)."""
    if not isinstance(calls, list) or not calls:
        return {"error": "calls must be a non-empty list of "
                         "{\"tool\": name, \"args\": {...}}",
                "next": 'e.g. calls=[{"tool": "foreground_app"}, '
                        '{"tool": "ui_dump", "args": {"limit": 40}}]'}
    if len(calls) > MAX_BATCH:
        return {"error": "at most %d calls per batch" % MAX_BATCH}
    # Refuse the whole batch up front rather than half-run it.
    for c in calls:
        name = (c or {}).get("tool") or (c or {}).get("name") or ""
        if name == "batch":
            return {"error": "batch cannot contain batch"}
        t = reg.tools.get(name)
        why = batch_refusal(t) if t is not None else ""
        if why:
            return {"error": "refused: " + why,
                    "next": "run the other calls in a batch and this one alone"}
    results = []
    for n, c in enumerate(calls):
        name = c.get("tool") or c.get("name")
        r = reg.call(name, c.get("args") or {})
        results.append({"tool": name, "result": r})
        if stop_on_error and isinstance(r, dict) and "error" in r:
            return {"results": results, "stopped_at": n,
                    "skipped": len(calls) - n - 1}
    out: dict = {"results": results}
    if list_at_end:
        out["screen"] = reg.call("ui_dump", {"limit": 60})
    return out


def main() -> None:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer(name="claudephone", instructions=INSTRUCTIONS)
    reg = build_registry()
    for tool in reg.tools.values():
        mcp.tool(description=tool.description, name=tool.name,
                 annotations=ToolAnnotations(**hints(tool)))(tool.fn)

    @mcp.tool(
        name="batch",
        description=(
            "Run several tool calls in order and return every result in one "
            "response. calls=[{\"tool\": name, \"args\": {...}}, ...], at most "
            "20. stop_on_error stops at the first error; list_at_end appends a "
            "ui_dump of the final screen. Destructive tools (sends, likes, "
            "joins, file writes, shell) are refused - call those on their own."
        ),
        annotations=ToolAnnotations(readOnlyHint=False),
    )
    def batch(calls: list, stop_on_error: bool = True,
              list_at_end: bool = False) -> dict:
        return run_batch(reg, calls, stop_on_error, list_at_end)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
