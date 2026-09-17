"""Target-level write detection, wired to the per-account ledger (B2).

Why this exists. ClaudePhone's safety used to be per TOOL: `tap` was one risk
class whether it hit "Close" or "Follow". Meanwhile the project's real safety
rail - the per-account ledger in datacollect, with `budget_for` ceilings and the
@saravbhaita account at 25% - only protected the phase scripts. An agent run
could have spent an account's follows without anything counting them.

So every tap is judged by the element it will actually hit (after the B1 pre-tap
check has re-found it), using `writes.json`:

    read        not a write                                  -> tap
    forbidden   a write with no ledger action (like, save,   -> refuse, always,
                repost, DM, comment, report, gift...)           unless the operator
                                                                allowed that rule id
    write       a budgeted ledger action (follow, interested,-> tap only if ALL of:
                not_interested, tg_join)                        1. mode is not readonly
                                                                2. the operator enabled
                                                                   that action for the run
                                                                3. the phone's account is known
                                                                4. the ledger is reachable
                                                                5. budget_for(account, action)
                                                                   has room (hour, day, minute)
                                                             -> then record() it

Every "no" fails CLOSED. On the phone itself there is no collect.db, so
budgeted writes are refused there until a ledger service exists. Budgeted
READS (profile opens, grid scans, reels, sheets, comments, searches, Telegram
chat opens) go through the same ledger in policy/reads.py (B2b) and are
refused there too, unless the operator runs with --allow-uncounted-reads.

Ceilings are only ever read through datacollect's `Ledger.can` / `budget_for`,
never from BUDGET directly (CLAUDE.md: "Read ceilings only through
ledger.budget_for").
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
_RULES: Optional[dict] = None

# Where datacollect lives: env first, else the sibling of the ClaudePhone repo
# (Dev/ClaudePhone and Dev/datacollect).
_DEFAULT_DC = os.path.normpath(os.path.join(HERE, "..", "..", "..", "..",
                                            "datacollect", "scripts"))


# ------------------------------------------------------------------ config

@dataclass
class Config:
    """Set per run by the agent (Policy), or from the environment for direct use."""
    mode: str = "auto"                              # auto | ask | readonly
    writes: set = field(default_factory=set)       # ledger actions enabled this run
    allow_rules: set = field(default_factory=set)  # forbidden rule ids allowed this run
    allow_uncounted_reads: bool = False            # reads without a ledger (policy/reads.py)
    run_id: str = ""


def _env_set(name: str) -> set:
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


CONFIG = Config(writes=_env_set("CLAUDEPHONE_WRITES"),
                allow_rules=_env_set("CLAUDEPHONE_ALLOW_RULES"))


def configure(mode: str = "auto", writes=(), allow_rules=(), run_id: str = "",
              allow_uncounted_reads: bool = False) -> None:
    CONFIG.mode = mode
    CONFIG.writes = set(writes) | _env_set("CLAUDEPHONE_WRITES")
    CONFIG.allow_rules = set(allow_rules) | _env_set("CLAUDEPHONE_ALLOW_RULES")
    CONFIG.allow_uncounted_reads = bool(allow_uncounted_reads)
    CONFIG.run_id = run_id


# ------------------------------------------------------------------ rules

def rules() -> dict:
    global _RULES
    if _RULES is None:
        with open(os.path.join(HERE, "writes.json"), encoding="utf-8") as f:
            _RULES = json.load(f)
    return _RULES


@dataclass
class Verdict:
    kind: str = "read"            # read | write | forbidden
    rule: str = ""
    action: str = ""
    reason: str = ""
    platform: str = ""
    label: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


def _label(e) -> str:
    return " ".join(((e.text or "") or (e.desc or "")).split()).lower()


def _labels(e) -> list:
    return [" ".join(v.split()).lower() for v in (e.text or "", e.desc or "") if v]


def _matches(rule: dict, e) -> bool:
    rid = rule.get("rid")
    if rid:
        pat = re.compile("(?:" + rid + ")")
        if not (pat.fullmatch(e.rid or "") or pat.fullmatch(e.anchor or "")):
            return False
    lab = rule.get("label")
    if lab:
        pat = re.compile(lab)
        if not any(pat.search(v) for v in _labels(e)):
            return False
    return bool(rid or lab)


def classify(e, package: str = "") -> Verdict:
    """What tapping element `e` inside `package` would do to the account."""
    if e is None:
        return Verdict()
    spec = rules()
    app = (spec.get("apps") or {}).get(package or "") or {}
    platform = app.get("platform", "")
    for rule in (app.get("rules") or []) + (spec.get("any_app") or []):
        if not _matches(rule, e):
            continue
        if rule.get("action"):
            return Verdict("write", rule["id"], rule["action"], "",
                           platform, _label(e)[:60])
        return Verdict("forbidden", rule["id"], "", rule.get("forbidden", ""),
                       platform, _label(e)[:60])
    return Verdict(platform=platform)


_RANK = {"read": 0, "write": 1, "forbidden": 2}


def _contains(b, x: int, y: int) -> bool:
    return b[0] <= x < b[2] and b[1] <= y < b[3]


def at_point(elements, x: int, y: int):
    """The smallest element covering (x, y) - what a tap there actually hits.

    Clickable or not: a label inside a button (anchor = the button's id) is
    enough to name the control."""
    hits = [e for e in elements or [] if _contains(e.bounds, x, y)]
    if not hits:
        return None
    return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) *
                                   (e.bounds[3] - e.bounds[1]))


def classify_tap(target, elements, x: int, y: int, package: str = "") -> Verdict:
    """Judge the tap by the stricter of the intended target and what is under
    the point. A clickable row whose centre sits on its own Follow button is a
    follow, whatever the row's label says."""
    verdicts = [classify(target, package)]
    under = at_point(elements, x, y)
    if under is not None and under is not target:
        verdicts.append(classify(under, package))
    return max(verdicts, key=lambda v: _RANK[v.kind])


# ------------------------------------------------------------------ accounts

def _datacollect_dir() -> str:
    return os.environ.get("CLAUDEPHONE_DATACOLLECT") or _DEFAULT_DC


def _accounts() -> dict:
    """datacollect's serial -> account map, read WITHOUT importing guard.py
    (which pulls in device code and path side effects)."""
    env = os.environ.get("CLAUDEPHONE_ACCOUNT_MAP")
    if env:
        try:
            return json.loads(env)
        except ValueError:
            return {}
    path = os.path.join(_datacollect_dir(), "guard.py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "ACCOUNTS" for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return {}
    return {}


def account_for(serial: str, platform: str) -> Optional[str]:
    """The ledger account key for this phone on this platform, or None.

    Instagram uses the handle bound to the phone (guard.ACCOUNTS); other
    platforms use "<platform>:<phone alias>", the convention LinkedIn already
    uses in the ledger ("li:samsung").
    """
    info = _accounts().get(serial or "")
    if not info or not platform:
        return None
    if platform == "ig":
        return info.get("account") or None
    alias = info.get("alias")
    return (platform + ":" + alias) if alias else None


# ------------------------------------------------------------------ ledger

class LedgerUnavailable(RuntimeError):
    pass


def _ledger_module():
    d = _datacollect_dir()
    if not os.path.isfile(os.path.join(d, "ledger.py")):
        raise LedgerUnavailable("no datacollect ledger at " + d)
    import sys
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import ledger as lg      # type: ignore
        import store             # type: ignore
    except Exception as e:       # zstandard missing, db locked, ...
        raise LedgerUnavailable("could not load the ledger: "
                                + type(e).__name__ + ": " + str(e)[:120])
    return lg, store


_LEDGERS: dict = {}


def _open(account: str, device: str):
    """A Ledger for this account, connection reused within the process.

    store.connect() runs the schema and migrations on every call; a feed loop
    asking once per reel should not pay that each time. Keyed on the database
    path too, so a test that points store.DB elsewhere gets its own connection.
    """
    lg, store = _ledger_module()
    run = ("claudephone:" + CONFIG.run_id) if CONFIG.run_id else "claudephone"
    key = (getattr(store, "DB", ""), account, device)
    led = _LEDGERS.get(key)
    if led is None:
        led = lg.Ledger(store.connect(), account=account, device=device, run_id=run)
        _LEDGERS[key] = led
    led.run_id = run
    return led


def ledger_for(account: str, serial: str = ""):
    """The Ledger for an account on a phone (device model from guard.ACCOUNTS)."""
    device = (_accounts().get(serial or "") or {}).get("model", "")
    return _open(account, device)


def ledger_check(account: str, action: str, device: str = "") -> tuple:
    """-> (ok, why). Hour/day through Ledger.can (budget_for inside), plus the
    per-minute ceiling, which Ledger only paces rather than refuses."""
    led = _open(account, device)
    ok, why = led.can(action, n=1)
    if not ok:
        return False, why
    per_min = led.budget(action)["per_min"]
    if led.count(action, 1) >= per_min:
        return False, ("@" + account + " already did " + action + " "
                       + str(per_min) + "x in the last minute; wait ~60 s")
    return True, why


def ledger_record(account: str, action: str, target: str, rule: str,
                  device: str = "") -> None:
    _open(account, device).record(action, target=target, note="rule " + rule)


# ------------------------------------------------------------------ the gate

@dataclass
class Decision:
    allowed: bool
    verdict: Verdict
    why: str = ""
    account: str = ""
    ledger: str = ""

    def to_dict(self) -> dict:
        d = {"allowed": self.allowed, **self.verdict.to_dict()}
        for k in ("why", "account", "ledger"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


def decide(verdict: Verdict, serial: str = "") -> Decision:
    """Whether a tap with this verdict may proceed. Fails closed."""
    if verdict.kind == "read":
        return Decision(True, verdict)
    if verdict.kind == "forbidden":
        if verdict.rule in CONFIG.allow_rules:
            return Decision(True, verdict, why="rule allowed by the operator for this run")
        return Decision(False, verdict,
                        why="forbidden: " + verdict.reason + ". No ledger action "
                            "exists for this, so it is never automated (allow rule '"
                            + verdict.rule + "' explicitly to override for one run)")
    # budgeted write
    if CONFIG.mode == "readonly":
        return Decision(False, verdict, why="readonly mode: " + verdict.action
                                            + " writes to the account")
    if verdict.action not in CONFIG.writes:
        return Decision(False, verdict,
                        why="write '" + verdict.action + "' is not enabled for this "
                            "run (enable with --allow-write " + verdict.action
                            + " or CLAUDEPHONE_WRITES)")
    account = account_for(serial, verdict.platform)
    if not account:
        return Decision(False, verdict,
                        why="no ledger account known for phone '" + (serial or "?")
                            + "' on platform '" + verdict.platform + "' - writes "
                            "refused (see datacollect guard.ACCOUNTS)")
    device = (_accounts().get(serial) or {}).get("model", "")
    try:
        ok, why = ledger_check(account, verdict.action, device)
    except LedgerUnavailable as e:
        return Decision(False, verdict, why="ledger unavailable, write refused: "
                                            + str(e), account=account)
    if not ok:
        return Decision(False, verdict, why="ledger: " + why, account=account,
                        ledger=why)
    return Decision(True, verdict, account=account, ledger=why)


def commit(decision: Decision, target: str = "", serial: str = "") -> Optional[str]:
    """Record an executed budgeted write. Returns an error string, or None."""
    if not decision.allowed or decision.verdict.kind != "write":
        return None
    device = (_accounts().get(serial) or {}).get("model", "")
    try:
        ledger_record(decision.account, decision.verdict.action,
                      target or decision.verdict.label, decision.verdict.rule,
                      device)
        return None
    except Exception as e:      # the write happened; say loudly it is uncounted
        return ("WRITE NOT RECORDED in the ledger: " + type(e).__name__ + ": "
                + str(e)[:160])


def gate_action(action: str, platform: str, serial: str = "",
                rule: str = "") -> Decision:
    """For tools that write without a generic tap (X feed tools, tg_join)."""
    if not action:
        return decide(Verdict("forbidden", rule or "tool", "", "a write with no "
                              "ledger action", platform), serial)
    return decide(Verdict("write", rule or ("tool." + action), action, "",
                          platform), serial)
