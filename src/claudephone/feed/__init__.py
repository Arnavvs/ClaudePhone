"""Feed shaping: read and change what the ranked timelines serve.

Split out from `tools/apps/` on purpose. Everything under `tools/` is an MCP
registration layer; this package is plain functions with no MCP dependency, so
the same logic can be driven from the CLI in `tools/xfeed.py`, from a test, or
from the server. One implementation, three front-ends.
"""
