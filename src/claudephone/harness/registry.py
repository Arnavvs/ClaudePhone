"""Tool registry.

Deliberately exposes the same surface as an MCP server object - a `.tool()`
decorator taking `description=` - so every tool module written for
MobileAgentMCP registers here **unchanged**:

    def register(mcp):
        @mcp.tool(description="...")
        def ui_dump(query: str = "", limit: int = 120) -> dict: ...

`register(registry)` and it just works. That compatibility is why ~90 tools
ported into this project without being rewritten.

## Packs, and why they matter more than they look

The target model for this project is a *cheap* one (see docs/MODELS.md). Cheap
models degrade sharply once the tool list gets long: they pick plausible-looking
wrong tools, and the schema block alone can eat several thousand tokens before
the task is even described.

So tools live in **packs**, and only `core` is exposed by default. The model
widens its own surface with `use_tools(pack)` when it needs to. A task that
never touches Instagram never pays for Instagram's 20 tool schemas.
"""

from __future__ import annotations

import inspect
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, get_args, get_origin, get_type_hints

# Packs always visible to the model. Everything else is opt-in via use_tools().
DEFAULT_PACKS = ("core",)

_JSON_TYPES = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _schema_for(annotation: Any) -> dict:
    """Best-effort JSON-schema fragment for a parameter annotation."""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = get_origin(annotation)
    if origin is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if origin in (list, set, tuple):
            inner = _schema_for(args[0]) if args else {"type": "string"}
            return {"type": "array", "items": inner}
        if origin is dict:
            return {"type": "object"}
        # Optional[X] / Union[X, None] -> schema of X
        if args:
            return _schema_for(args[0])
        return {"type": "string"}
    return {"type": _JSON_TYPES.get(annotation, "string")}


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    pack: str = "core"
    schema: dict = field(default_factory=dict)
    dangerous: bool = False

    def spec(self) -> dict:
        """OpenAI-style tool spec (what OpenRouter and llama.cpp both expect)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


def next_step(e: BaseException) -> str:
    """What to do about a tool that raised, in one line (B12).

    A bare exception string leaves a model to guess, and the usual guess is to
    call the same thing again. Most failures here have one of a few causes, and
    each has a known next move.
    """
    kind, text = type(e).__name__, str(e).lower()
    if kind == "DeviceError" or "adb" in text:
        if "unauthorized" in text:
            return ("the phone has not authorised this computer: a person must "
                    "accept the USB-debugging prompt on the phone")
        if "offline" in text or "not found" in text or "no devices" in text:
            return "the phone is not connected; call diagnose() to see why"
        return "an adb command failed; call diagnose() before retrying"
    if kind == "BridgeError" or "bridge" in text:
        return ("the accessibility bridge did not answer; call bridge_status(), "
                "and diagnose(attempt_fix=True) if it is unbound")
    if kind in ("TimeoutError", "timeout") or "timed out" in text:
        return "it timed out; read the screen (ui_dump) and retry once at most"
    return ("read the screen (ui_dump) before trying again, and do not repeat "
            "the same call more than once")


class ToolRegistry:
    """Holds tools, renders their specs, and runs them."""

    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self._pack: str = "core"          # pack assigned to the next registration
        self._dangerous: bool = False
        self.active_packs: set[str] = set(DEFAULT_PACKS)

    # -- registration --------------------------------------------------------

    def pack(self, name: str, dangerous: bool = False):
        """Context manager: everything registered inside belongs to `name`."""
        registry = self

        class _Ctx:
            def __enter__(self):
                registry._pack, registry._dangerous = name, dangerous
                return registry

            def __exit__(self, *exc):
                registry._pack, registry._dangerous = "core", False
                return False

        return _Ctx()

    def tool(self, description: str = "", name: str = "",
             dangerous: Optional[bool] = None):
        """Decorator. Signature-compatible with `MCPServer.tool`."""

        def deco(fn: Callable) -> Callable:
            tool_name = name or fn.__name__
            doc = description or (inspect.getdoc(fn) or "").strip()
            try:
                hints = get_type_hints(fn)
            except Exception:
                hints = {}
            sig = inspect.signature(fn)
            props: dict[str, Any] = {}
            required: list[str] = []
            for pname, p in sig.parameters.items():
                if pname in ("self", "cls") or p.kind in (
                        p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                frag = _schema_for(hints.get(pname, p.annotation))
                if p.default is not inspect.Parameter.empty:
                    frag["default"] = p.default
                else:
                    required.append(pname)
                props[pname] = frag
            schema = {"type": "object", "properties": props}
            if required:
                schema["required"] = required
            self.tools[tool_name] = Tool(
                name=tool_name, description=doc, fn=fn, pack=self._pack,
                schema=schema,
                dangerous=self._dangerous if dangerous is None else dangerous,
            )
            return fn

        return deco

    # -- introspection -------------------------------------------------------

    def packs(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tools.values():
            out[t.pack] = out.get(t.pack, 0) + 1
        return dict(sorted(out.items()))

    def specs(self, packs: Optional[set[str]] = None) -> list[dict]:
        """Tool specs for the packs currently in play."""
        live = self.active_packs if packs is None else packs
        return [t.spec() for t in self.tools.values() if t.pack in live]

    def search(self, query: str, limit: int = 12) -> list[dict]:
        q = query.lower().strip()
        hits = []
        for t in self.tools.values():
            hay = (t.name + " " + t.description + " " + t.pack).lower()
            if all(w in hay for w in q.split()):
                hits.append({"name": t.name, "pack": t.pack,
                             "description": t.description.split(". ")[0][:160]})
        return hits[:limit]

    # -- execution -----------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        """Run a tool. Never raises - the model needs to see the error text."""
        t = self.tools.get(name)
        if t is None:
            import difflib
            key = str(name or "")
            near = [n for n in self.tools if key.lower() in n.lower()][:5]
            near += [n for n in difflib.get_close_matches(key, list(self.tools), n=3)
                     if n not in near]
            return {"error": "no such tool: " + key,
                    "did_you_mean": near or None,
                    "next": "call find_tool(query='<what you want to do>') to "
                            "search every pack, including unloaded ones"}
        t0 = time.time()
        try:
            sig = inspect.signature(t.fn)
            clean = {k: v for k, v in (args or {}).items()
                     if k in sig.parameters}
            dropped = sorted(set((args or {}).keys()) - set(clean.keys()))
            result = t.fn(**clean)
            out = result if isinstance(result, dict) else {"result": result}
            if dropped:
                out["_ignored_args"] = dropped
            out["_ms"] = int((time.time() - t0) * 1000)
            return out
        except TypeError as e:
            return {"error": "bad arguments for " + name + ": " + str(e),
                    "expected": t.schema,
                    "next": "call " + name + " again with the parameters in "
                            "`expected` (names and types exactly as listed)",
                    "_ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            return {"error": type(e).__name__ + ": " + str(e)[:600],
                    "next": next_step(e),
                    "trace": traceback.format_exc(limit=3)[-600:],
                    "_ms": int((time.time() - t0) * 1000)}
