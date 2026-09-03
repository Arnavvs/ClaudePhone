"""Tools the agent uses to manage its own tool surface.

This is the other half of the pack design in registry.py. The model starts with
`core` only and widens deliberately. Three calls are enough for that to work:
see what exists, search it, load it.
"""

from __future__ import annotations


def register(reg) -> None:

    @reg.tool(
        description="List the available tool packs and how many tools each "
                    "holds, marking which are currently loaded. Only loaded "
                    "packs can be called; widen with use_tools."
    )
    def list_tool_packs() -> dict:
        return {
            "loaded": sorted(reg.active_packs),
            "packs": [
                {"pack": p, "tools": n, "loaded": p in reg.active_packs}
                for p, n in reg.packs().items()
            ],
            "hint": "use_tools('instagram') loads a pack for the rest of the run.",
        }

    @reg.tool(
        description="Search every tool by name and description, including "
                    "packs that are not loaded. Use this before guessing a "
                    "tool name."
    )
    def find_tool(query: str, limit: int = 12) -> dict:
        hits = reg.search(query, limit=limit)
        return {
            "query": query,
            "matches": hits,
            "note": ("Some matches are in unloaded packs - call "
                     "use_tools(pack) before calling them."),
        }

    @reg.tool(
        description="Load a tool pack so its tools become callable. Loading "
                    "costs context, so load only what the task needs."
    )
    def use_tools(pack: str) -> dict:
        known = reg.packs()
        if pack not in known:
            return {"error": "no such pack: " + pack,
                    "available": sorted(known)}
        reg.active_packs.add(pack)
        names = sorted(t.name for t in reg.tools.values() if t.pack == pack)
        return {"loaded": pack, "added": len(names), "tools": names,
                "packs_active": sorted(reg.active_packs)}

    @reg.tool(
        description="Unload a pack when finished with it, to free context for "
                    "the rest of the run. The core pack cannot be unloaded."
    )
    def drop_tools(pack: str) -> dict:
        if pack == "core":
            return {"error": "core cannot be unloaded"}
        reg.active_packs.discard(pack)
        return {"dropped": pack, "packs_active": sorted(reg.active_packs)}
