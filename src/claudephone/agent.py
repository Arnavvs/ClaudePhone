"""Assembly: build the tool registry and the agent that drives it.

Pack membership is the only editorial decision here, and it is a real one. The
`core` pack is what the model sees before it asks for anything else, so it has
to be sufficient to orient on an unknown screen and no larger. Everything that
is domain-specific - Instagram, X, capture, telephony - waits behind
`use_tools()`.
"""

from __future__ import annotations

import os
from typing import Optional

from .harness import meta_tools
from .harness.loop import Agent, Budget, Policy
from .harness.stagnation import Stagnation
from .harness.models import Chat, ModelConfig
from .harness.registry import ToolRegistry
from .tools import (compound_tools, device_tools, explore_tools, file_tools,
                    input_tools, registry_tools, shell_tools, system_tools,
                    thread_tools, ui_tools)
from .tools import handoff_tools
from .tools import phone_tools
from .tools.apps import instagram as ig_tools
from .tools.apps import instagram_comments as ig_comments
from .tools.apps import instagram_profile as ig_profile
from .tools.apps import instagram_web as ig_web
from .tools.apps import reel_capture as ig_capture
from .tools.apps import telegram as tg_tools
from .tools.apps import twitter as x_tools
from .tools.apps import x_feed as x_feed_tools

# Reaches other people, or costs money. Allowed only when the operator says so
# by name (`--allow phone_sms_send`). Being dangerous is not enough for these.
OUTBOUND = {"phone_sms_send", "phone_call", "tg_send", "tg_reply"}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()

    # core - enough to see a screen, act on it, and find more tools
    with reg.pack("core"):
        meta_tools.register(reg)
        device_tools.register(reg)
        ui_tools.register(reg)
        input_tools.register(reg)
        handoff_tools.register(reg)

    # Compound actions belong in core: they are how the agent should move by
    # default, and gating them behind use_tools() would mean the expensive
    # step-at-a-time path is the one it reaches for first.
    with reg.pack("core"):
        compound_tools.register(reg)

    with reg.pack("system"):
        system_tools.register(reg)

    with reg.pack("shell", dangerous=True):
        shell_tools.register(reg)

    with reg.pack("files"):
        file_tools.register(reg)

    with reg.pack("phone"):
        phone_tools.register(reg)

    # Discovery: how the agent teaches itself an app it has never seen.
    with reg.pack("learn"):
        explore_tools.register(reg)
        thread_tools.register(reg)
        registry_tools.register(reg)

    with reg.pack("instagram"):
        ig_tools.register(reg)
        ig_profile.register(reg)
        ig_profile.register_orchestrator(reg)
        ig_profile.register_about(reg)
        ig_comments.register(reg)

    with reg.pack("instagram_capture"):
        ig_capture.register(reg)
        ig_capture.register_full(reg)

    with reg.pack("instagram_web"):
        ig_web.register(reg)
        ig_web.register_session_import(reg)

    with reg.pack("telegram"):
        tg_tools.register(reg)
        tg_tools.register_membership(reg)
        tg_tools.register_search_in_chat(reg)
        tg_tools.register_outbound(reg)

    with reg.pack("x"):
        x_tools.register(reg)
        x_tools.register_nav(reg)
        x_tools.register_timelines(reg)
        x_feed_tools.register(reg)

    return reg


def build_policy(mode: str = "auto", allow: Optional[list[str]] = None,
                 deny: Optional[list[str]] = None,
                 on_ask=None, allow_writes: Optional[list[str]] = None,
                 allow_rules: Optional[list[str]] = None,
                 allow_uncounted_reads: bool = False) -> Policy:
    allow_set = set(allow or [])
    deny_set = set(deny or []) | (OUTBOUND - allow_set)
    return Policy(mode=mode, allow=allow_set, deny=deny_set, on_ask=on_ask,
                  writes=set(allow_writes or []),
                  allow_rules=set(allow_rules or []),
                  allow_uncounted_reads=bool(allow_uncounted_reads))


def build_agent(provider: str = "", model: str = "", mode: str = "auto",
                allow: Optional[list[str]] = None,
                deny: Optional[list[str]] = None,
                packs: Optional[list[str]] = None,
                budget: Optional[Budget] = None,
                operator_notes: str = "", on_ask=None,
                allow_writes: Optional[list[str]] = None,
                allow_rules: Optional[list[str]] = None,
                allow_uncounted_reads: bool = False,
                stagnation: bool = True,
                on_ask_operator=None,
                helper_model: str = "",
                summarize: bool = False) -> Agent:
    cfg = ModelConfig.from_env(provider)
    if model:
        cfg.model = model
    # B5: a separate helper only when one is named; otherwise the decider does
    # the side work too, and is counted once.
    hcfg = ModelConfig.from_env(provider, role="helper")
    if helper_model:
        hcfg.model = helper_model
    helper = Chat(hcfg) if hcfg.model and hcfg.model != cfg.model else None
    reg = build_registry()
    for p in packs or []:
        if p in reg.packs():
            reg.active_packs.add(p)
    notes = operator_notes or os.environ.get("CLAUDEPHONE_NOTES", "")
    return Agent(
        chat=Chat(cfg),
        registry=reg,
        policy=build_policy(mode, allow, deny, on_ask, allow_writes, allow_rules,
                            allow_uncounted_reads),
        budget=budget or Budget(),
        operator_notes=notes,
        # B4: 0 disables the stop but keeps the warning, which is what an
        # operator watching a run by hand usually wants.
        stagnation=Stagnation() if stagnation else Stagnation(stop_after=0),
        on_ask_operator=on_ask_operator,
        helper=helper,
        summarize=summarize,
    )
