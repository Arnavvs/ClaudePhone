"""Target-level write gate (B2) without a phone.

Classification uses policy/writes.json as shipped. The ledger tests run the REAL
datacollect ledger code (budget_for, Ledger.can/record) against a throwaway
database, so the ceilings exercised are the project's actual ones - including
@saravbhaita at 25% (follow: 1/min, 2/hour, 10/day).

    python -m pytest tests/test_writes.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import state  # noqa: E402
from claudephone.policy import writes as wr  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"
X = "com.twitter.android"
TG = "org.telegram.messenger"
DC = os.path.normpath(os.path.join(ROOT, "..", "datacollect", "scripts"))
REALME, SAMSUNG = "HIDMFQ8X894DIVLZ", "RZ8N70HYQSB"


def el(rid="", text="", desc="", anchor="", bounds=(0, 0, 100, 100), clickable=True):
    return Element(i=0, rid=rid, anchor=anchor or rid, text=text, desc=desc,
                   cls="Button", bounds=bounds, clickable=clickable)


# -- classification ------------------------------------------------------------

@pytest.mark.parametrize("element,pkg,kind,action_or_rule", [
    (el("inline_follow_button", text="Follow", desc="Follow Chaitanya Sharma"), IG, "write", "follow"),
    (el("profile_header_follow_button", text="Follow back"), IG, "write", "follow"),
    (el("inline_follow_button", text="Following"), IG, "forbidden", "ig.unfollow"),
    (el("profile_header_follow_button", text="Requested"), IG, "forbidden", "ig.unfollow"),
    (el("profile_header_follow_button", text="Message"), IG, "forbidden", "ig.follow.unknown_state"),
    (el("", text="Follow", anchor="inline_follow_button", clickable=False), IG, "write", "follow"),
    (el("like_button", desc="Like"), IG, "forbidden", "ig.like"),
    (el("like_button", desc="Unlike"), IG, "forbidden", "ig.like"),
    (el("save_button", desc="Save"), IG, "forbidden", "ig.save"),
    (el("", desc="Repost"), IG, "forbidden", "ig.repost"),
    (el("control_option_text", text="Not interested"), IG, "write", "not_interested"),
    (el("control_option_text", text="Interested"), IG, "write", "interested"),
    (el("control_option_text", text="Report"), IG, "forbidden", "ig.sheet.other_write"),
    (el("control_option_text", text="About this account"), IG, "read", ""),
    (el("direct_share_sheet_grid_view_pog", desc="OD_01 creator Chat not selected"), IG, "forbidden", "ig.share.recipient"),
    (el("button", desc="Add to story"), IG, "forbidden", "ig.share.story"),
    (el("", desc="Tap to Like Comment"), IG, "forbidden", "ig.comment.like"),
    (el("comment_composer_appreciation_gift_button"), IG, "forbidden", "ig.comment.gift"),
    (el("comment_button", desc="Comment"), IG, "read", ""),
    (el("direct_share_button", desc="Share"), IG, "read", ""),
    (el("clips_author_username", text="their_bubbles"), IG, "read", ""),
    (el("", text="Like"), X, "forbidden", "x.like"),
    (el("", text="Follow"), X, "write", "follow"),
    (el("", text="Not interested in this post"), X, "write", "not_interested"),
    (el("", text="JOIN"), TG, "write", "tg_join"),
    (el("", text="Send"), "com.android.settings", "forbidden", "any.send"),
    (el("", text="Wi-Fi"), "com.android.settings", "read", ""),
])
def test_classify(element, pkg, kind, action_or_rule):
    v = wr.classify(element, pkg)
    assert v.kind == kind, v
    if kind == "write":
        assert v.action == action_or_rule
    elif kind == "forbidden":
        assert v.rule == action_or_rule


def test_tap_is_judged_by_what_is_under_the_point_too():
    row = el("row_search_user_container", text="some creator", bounds=(0, 500, 1080, 700))
    follow = el("inline_follow_button", text="Follow", bounds=(850, 560, 1050, 640))
    v = wr.classify_tap(row, [row, follow], 950, 600, IG)
    assert v.kind == "write" and v.action == "follow"
    assert wr.classify_tap(row, [row, follow], 300, 600, IG).kind == "read"


# -- decisions without a ledger ---------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(monkeypatch):
    monkeypatch.delenv("CLAUDEPHONE_WRITES", raising=False)
    monkeypatch.delenv("CLAUDEPHONE_ALLOW_RULES", raising=False)
    wr.configure(mode="auto", writes=(), allow_rules=(), run_id="test")
    yield
    wr.configure(mode="auto", writes=(), allow_rules=(), run_id="")


def test_read_is_allowed():
    assert wr.decide(wr.Verdict()).allowed


def test_forbidden_is_refused_unless_that_rule_is_allowed():
    v = wr.classify(el("like_button", desc="Like"), IG)
    assert not wr.decide(v, SAMSUNG).allowed
    wr.configure(allow_rules={"ig.like"})
    assert wr.decide(v, SAMSUNG).allowed
    assert not wr.decide(wr.classify(el("save_button"), IG), SAMSUNG).allowed


def test_budgeted_write_needs_to_be_enabled():
    v = wr.classify(el("inline_follow_button", text="Follow"), IG)
    d = wr.decide(v, SAMSUNG)
    assert not d.allowed and "not enabled" in d.why


def test_readonly_refuses_even_an_enabled_write():
    wr.configure(mode="readonly", writes={"follow"})
    d = wr.decide(wr.classify(el("inline_follow_button", text="Follow"), IG), SAMSUNG)
    assert not d.allowed and "readonly" in d.why


def test_unknown_phone_is_refused(monkeypatch):
    monkeypatch.setenv("CLAUDEPHONE_ACCOUNT_MAP", json.dumps({}))
    wr.configure(writes={"follow"})
    d = wr.decide(wr.classify(el("inline_follow_button", text="Follow"), IG), "127.0.0.1:5555")
    assert not d.allowed and "no ledger account" in d.why


def test_missing_ledger_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDEPHONE_ACCOUNT_MAP", json.dumps(
        {SAMSUNG: {"alias": "samsung", "account": "aisha_xmehra", "model": "SM-M215F"}}))
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", str(tmp_path))     # no ledger.py here
    wr.configure(writes={"follow"})
    d = wr.decide(wr.classify(el("inline_follow_button", text="Follow"), IG), SAMSUNG)
    assert not d.allowed and "ledger unavailable" in d.why


def test_account_keys_follow_the_ledger_conventions(monkeypatch):
    monkeypatch.delenv("CLAUDEPHONE_ACCOUNT_MAP", raising=False)
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
    assert wr.account_for(REALME, "ig") == "saravbhaita"
    assert wr.account_for(SAMSUNG, "ig") == "aisha_xmehra"
    assert wr.account_for(SAMSUNG, "li") == "li:samsung"
    assert wr.account_for(SAMSUNG, "tg") == "tg:samsung"
    assert wr.account_for("127.0.0.1:5555", "ig") is None


# -- decisions against the real ledger code, throwaway database ---------------------

@pytest.fixture
def temp_ledger(monkeypatch, tmp_path):
    if not os.path.isfile(os.path.join(DC, "ledger.py")):
        pytest.skip("datacollect not present")
    monkeypatch.delenv("CLAUDEPHONE_ACCOUNT_MAP", raising=False)
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
    lg, store = wr._ledger_module()
    monkeypatch.setattr(store, "DB", str(tmp_path / "ledger_test.db"))
    return lg, store


def test_follow_on_the_25pct_account_is_capped_per_minute(temp_ledger):
    lg, _ = temp_ledger
    assert lg.budget_for("saravbhaita", "follow")["per_min"] == 1
    wr.configure(writes={"follow"}, run_id="t1")
    v = wr.classify(el("profile_header_follow_button", text="Follow"), IG)
    d1 = wr.decide(v, REALME)
    assert d1.allowed and d1.account == "saravbhaita"
    assert wr.commit(d1, target="creator_a", serial=REALME) is None
    d2 = wr.decide(v, REALME)
    assert not d2.allowed and "last minute" in d2.why


def test_hourly_ceiling_refuses(temp_ledger):
    lg, store = temp_ledger
    con = store.connect()
    led = lg.Ledger(con, account="saravbhaita", device="RMX3395")
    for _ in range(2):                                   # 25% account: 2 follows/hour
        led.record("follow", target="earlier")
    from datetime import datetime, timedelta, timezone
    five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    con.execute("UPDATE action_log SET at = ?", (five_min_ago,))   # the ledger's own ISO format
    con.commit()
    wr.configure(writes={"follow"})
    d = wr.decide(wr.classify(el("inline_follow_button", text="Follow"), IG), REALME)
    assert not d.allowed and "ceiling is 2" in d.why


def test_commit_records_to_the_ledger_with_the_run_id(temp_ledger):
    lg, store = temp_ledger
    wr.configure(writes={"not_interested"}, run_id="run42")
    d = wr.decide(wr.classify(el("control_option_text", text="Not interested"), IG), SAMSUNG)
    assert d.allowed and d.account == "aisha_xmehra"
    assert wr.commit(d, target="reel xyz", serial=SAMSUNG) is None
    row = store.connect().execute(
        "SELECT account, device, action, target, run_id, note FROM action_log").fetchone()
    assert tuple(row) == ("aisha_xmehra", "SM-M215F", "not_interested", "reel xyz",
                          "claudephone:run42", "rule ig.sheet.not_interested")


# -- the tool surface: tap goes through the gate ------------------------------------

def _patched_tool_env(monkeypatch, screen_elements, pkg=IG):
    from claudephone import device as dev
    from claudephone.runtime import targeting as tg
    from claudephone.tools import input_tools as it
    monkeypatch.setattr(dev, "default_serial", lambda: SAMSUNG)
    monkeypatch.setattr(tg, "read_screen", lambda serial="": {
        "elements": screen_elements, "obstructions": [], "package": pkg,
        "backend": "fake", "ms": 0})
    taps = []
    monkeypatch.setattr(it, "_dispatch_tap",
                        lambda x, y, hold_ms=0: taps.append((x, y)) or {"ok": True, "via": "fake"})
    state.remember(screen_elements, pkg)
    return taps


def _registry():
    from claudephone.agent import build_registry
    return build_registry()


def test_tool_tap_refuses_a_like_and_taps_nothing(monkeypatch):
    like = el("like_button", desc="Like", bounds=(950, 1000, 1050, 1100))
    taps = _patched_tool_env(monkeypatch, [like])
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert "write refused" in r.get("error", "") and taps == []


def test_tool_tap_verify_false_cannot_bypass_the_gate(monkeypatch):
    like = el("like_button", desc="Like", bounds=(950, 1000, 1050, 1100))
    taps = _patched_tool_env(monkeypatch, [like])
    r = _registry().call("tap", {"ref": state.ref(0), "verify": False})
    assert "write refused" in r.get("error", "") and taps == []


def test_tool_coordinate_tap_on_a_follow_is_gated(monkeypatch):
    follow = el("inline_follow_button", text="Follow", bounds=(850, 560, 1050, 640))
    taps = _patched_tool_env(monkeypatch, [follow])
    r = _registry().call("tap", {"x": 950, "y": 600})
    assert "write refused" in r.get("error", "") and taps == []


def test_tool_tap_on_an_enabled_follow_taps_and_records(monkeypatch, temp_ledger):
    _, store = temp_ledger
    follow = el("inline_follow_button", text="Follow", bounds=(850, 560, 1050, 640))
    taps = _patched_tool_env(monkeypatch, [follow])
    wr.configure(writes={"follow"}, run_id="tooltest")
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert r.get("ok") and taps == [(950, 600)]
    assert r["write"]["action"] == "follow" and r["write"]["account"] == "aisha_xmehra"
    n = store.connect().execute("SELECT COUNT(*) FROM action_log WHERE action='follow'").fetchone()[0]
    assert n == 1


def test_tool_tap_on_a_plain_read_is_untouched(monkeypatch):
    name = el("clips_author_username", text="their_bubbles", bounds=(100, 1900, 400, 1960))
    taps = _patched_tool_env(monkeypatch, [name])
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert r.get("ok") and taps == [(250, 1930)] and "write" not in r
