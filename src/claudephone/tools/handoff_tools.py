"""Tools for handing the phone back to a person (B3)."""

from __future__ import annotations

from ..harness import handoff


def register(reg) -> None:

    @reg.tool(
        description=(
            "Stop the run and hand the phone to a person, with the reason. Use "
            "this the moment you meet a login, checkpoint, 2FA, CAPTCHA or "
            "'suspicious activity' screen - those are never yours to clear, and "
            "retrying one can cost the account. Also use it when the goal needs "
            "a decision you were not given (which account, whether to spend "
            "money, anything irreversible). The run ends; nothing waits for you."
        )
    )
    def request_human(reason: str, detail: str = "") -> dict:
        if not (reason or "").strip():
            return {"error": "say why a person is needed"}
        return handoff.stop(reason.strip(), detail.strip())

    @reg.tool(
        description=(
            "Ask the operator ONE question and wait for the answer, then carry "
            "on. Use it when a single fact unblocks you - which of two accounts "
            "to open, whether a handle is the right one. The run pauses while "
            "you wait, so ask only when you cannot find the answer on the "
            "phone. If nobody is reachable, or nobody answers in time, the run "
            "stops and reports the question."
        )
    )
    def ask_operator(question: str, timeout_s: float = 300.0) -> dict:
        return handoff.ask_operator(question, timeout_s)
