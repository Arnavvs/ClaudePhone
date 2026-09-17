"""Handing control back to a human, and knowing when it is compulsory (B3).

Two directions, and they are not the same thing:

* **`request_human(reason)`** - the agent gives up and the run ends with
  `stopped_by="human_required"`. Nothing waits; a person reads the reason later.
* **`ask_operator(question)`** - the agent needs one answer and can continue once
  it has it. The run BLOCKS until the operator replies, or until the wait times
  out, and then carries on with the answer in hand.

The third path is the one that cannot be left to the model. Project doctrine is
that a phone hitting a checkpoint or 2FA stops *that account entirely* - and a
cheap model looking at "We detected unusual activity" will happily keep tapping,
because tapping is what it does. So the harness checks every screen it sees, and
a match ends the run whatever the model thinks. Prompt rules say the same thing
in words, but the rule here is the one that holds.

The detector is deliberately made of phrases that are specific to being
challenged - not "log in", which appears on ordinary screens - plus the presence
of a password field, which never belongs in an automated session on an account
that is already signed in.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# Screens that mean: stop, and do not touch this account again until a human has
# looked. Matched against lowercased text and content-desc of any element.
CHECKPOINT_PATTERNS = [
    (r"suspicious (login|attempt|activity)", "suspicious activity notice"),
    (r"unusual (login |sign-?in )?activity", "unusual activity notice"),
    (r"we (have )?detected (unusual|suspicious)", "platform challenge"),
    (r"(confirm|verify) (it'?s )?(you|your identity)", "identity challenge"),
    (r"help us confirm", "identity challenge"),
    (r"security check|captcha|i'?m not a robot", "security check"),
    (r"action blocked|we restricted|temporarily blocked|you'?re temporarily",
     "account action blocked"),
    (r"try again later", "rate-limited or blocked"),
    (r"two-?factor|2-?step verification|authentication code|verification code",
     "two-factor prompt"),
    (r"enter the (\d+[- ])?digit code|enter the code we sent", "code prompt"),
    (r"your account (has been )?(disabled|suspended|restricted|locked)",
     "account restricted"),
    (r"challenge_required", "platform challenge"),
]
_COMPILED = [(re.compile(p), why) for p, why in CHECKPOINT_PATTERNS]

# A password field on an account we are supposed to be signed into already.
_PASSWORD_RID = re.compile(r"password|passwd")


@dataclass
class Channel:
    """How this run can reach a person, if at all."""

    ask: Optional[Callable[[str, float], Optional[str]]] = None
    checkpoint_guard: bool = True
    asked: list = field(default_factory=list)


CONFIG = Channel()


def configure(ask: Optional[Callable[[str, float], Optional[str]]] = None,
              checkpoint_guard: bool = True) -> None:
    CONFIG.ask = ask
    CONFIG.checkpoint_guard = checkpoint_guard
    CONFIG.asked = []


def stop(reason: str, detail: str = "", kind: str = "requested") -> dict:
    """The tool result that ends a run and hands over to a person."""
    return {"_handoff": {"kind": "stop", "why": kind, "reason": reason,
                         "detail": detail}}


def ask_operator(question: str, timeout_s: float = 300.0) -> dict:
    """Block until a person answers, or the wait runs out."""
    q = (question or "").strip()
    if not q:
        return {"error": "ask_operator needs a question"}
    if CONFIG.ask is None:
        # Nobody is listening. Ending the run beats guessing on: whatever the
        # model wanted to ask about, it does not know the answer.
        return stop("no operator is reachable to answer: " + q[:200],
                    kind="no_channel")
    t0 = time.time()
    answer = CONFIG.ask(q, timeout_s)
    waited = round(time.time() - t0, 1)
    CONFIG.asked.append({"question": q, "answer": answer, "waited_s": waited})
    if answer is None or str(answer).strip() == "":
        return stop("no answer from the operator after " + str(waited)
                    + " s, question was: " + q[:200], kind="no_answer")
    return {"answered": True, "question": q, "answer": str(answer),
            "waited_s": waited,
            "_handoff": {"kind": "answered", "question": q,
                         "answer": str(answer), "waited_s": waited}}


def checkpoint_on_screen(elements, package: str = "") -> Optional[dict]:
    """A login / 2FA / "unusual activity" screen, if one is showing."""
    if not CONFIG.checkpoint_guard:
        return None
    for e in elements or []:
        if e.rid and _PASSWORD_RID.search(e.rid) and "edittext" in (e.cls or "").lower():
            return {"why": "password field on screen", "package": package,
                    "evidence": e.rid}
        label = " ".join(((e.text or "") + " " + (e.desc or "")).split()).lower()
        if not label:
            continue
        for rx, why in _COMPILED:
            if rx.search(label):
                return {"why": why, "package": package, "evidence": label[:120]}
    return None
