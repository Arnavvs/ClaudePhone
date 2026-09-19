"""remember / recall: what the model keeps of its own run (B7).

The machinery is in `harness/history.py`; these are the model-facing handles.
"""

from __future__ import annotations

from typing import Optional

from ..harness import history


def register(reg) -> None:

    @reg.tool(
        description=(
            "Pin a fact you will need later in this run - a follower count, a "
            "handle, the answer to part of the goal. Notes stay in view for the "
            "whole run; old tool results do not (they shrink to one line after "
            "a few steps). Record what you saw, not a verdict. "
            "remember(key, '') clears a note."
        )
    )
    def remember(key: str, value: str) -> dict:
        return history.current.remember(key, value)

    @reg.tool(
        description=(
            "Read back something from earlier in THIS run instead of going back "
            "to the screen. recall(steps=[3]) returns step 3's full result; "
            "recall(query='followers') finds that text in every earlier result "
            "and thought. Cheaper than re-reading, and on Instagram a re-read "
            "is a counted read."
        )
    )
    def recall(query: str = "", steps: Optional[list] = None) -> dict:
        return history.current.recall(query=query, steps=steps)
