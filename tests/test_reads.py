"""Counted reads (B2b) without a phone.

Reads go through the same datacollect ledger as writes: budget_for ceilings,
per-minute pacing, fail closed when there is no ledger. The ledger code is the
real one, against a throwaway database (see conftest.py).

    python -m pytest tests/test_reads.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import device as dev  # noqa: E402
from claudephone import state  # noqa: E402
from claudephone.policy import reads  # noqa: E402
from claudephone.policy import writes as wr  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"
TG = "org.telegram.messenger"
DC = os.path.normpath(os.path.join(ROOT, "..", "datacollect", "scripts"))
REALME, SAMSUNG = "HIDMFQ8X894DIVLZ", "RZ8N70HYQSB"


def el(rid="", text="", desc="", cls="Button", bounds=(0, 0, 100, 100), selected=False):
    return Element(i=0, rid=rid, anchor=rid, text=text, desc=desc, cls=cls,
                   bounds=bounds, clickable=True, selected=selected)


@pytest.fixture(autouse=True)
def fresh_config():
    wr.configure(mode="auto", writes=(), allow_rules=(), run_id="test")
    yield
    wr.configure(mode="auto", writes=(), allow_rules=(), run_id="")


@pytest.fixture
def temp_ledger(monkeypatch, tmp_path):
    if not os.path.isfile(os.path.join(DC, "ledger.py")):
        pytest.skip("datacollect not present")
    monkeypatch.delenv("CLAUDEPHONE_ACCOUNT_MAP", raising=False)
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
    lg, store = wr._ledger_module()
    monkeypatch.setattr(store, "DB", str(tmp_path / "reads_test.db"))
    return lg, store


def prefill(temp_ledger, account, device, action, n, minutes_ago=5):
    lg, store = temp_ledger
    con = store.connect()
    led = lg.Ledger(con, account=account, device=device)
    for _ in range(n):
        led.record(action, target="earlier")
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    con.execute("UPDATE action_log SET at = ? WHERE target = 'earlier'", (at,))
    con.commit()


def rows(temp_ledger, action):
    _, store = temp_ledger
    return store.connect().execute(
        "SELECT account, action, target, run_id, note FROM action_log "
        "WHERE action = ? AND target != 'earlier'", (action,)).fetchall()


# -- classification ----------------------------------------------------------------

@pytest.mark.parametrize("element,action", [
    (el("clips_author_username", text="their_bubbles"), "profile_open"),
    (el("clips_author_profile_pic"), "profile_open"),
    (el("row_search_user_username", text="creator"), "profile_open"),
    (el("", desc="Go to their_bubbles's profile"), "profile_open"),
    (el("clips_ufi_more_button_component", desc="More"), "sheet_open"),
    (el("comment_button", desc="Comment"), "comment_read"),
    (el("preview_clip_thumbnail", desc="Reel by x. View count 1.2M"), "reel_open"),
    (el("image_button", desc="3 photos by x at row 1, column 2"), "reel_open"),
    (el("clips_tab", desc="Reels"), "feed_reel"),
    (el("direct_share_button", desc="Share"), ""),
    (el("like_button", desc="Like"), ""),
])
def test_counted_classification(element, action):
    assert reads.classify_count(element, IG)[1] == action
    assert reads.classify_count(element, "com.android.settings") == ("", "")


def test_a_forward_swipe_on_a_reel_is_a_feed_reel_or_a_reel_open():
    reel = [el("clips_author_username", text="a"), el("clips_ufi_more_button_component")]
    assert reads.reel_advance_action(reel + [el("clips_tab", selected=True)], IG) == "feed_reel"
    assert reads.reel_advance_action(reel + [el("clips_tab", selected=False)], IG) == "reel_open"
    assert reads.reel_advance_action(reel, IG) == "reel_open"
    assert reads.reel_advance_action([el("image_button")], IG) == ""
    card = [el("clips_viewer_view_pager"), el("inline_follow_button", text="Follow"),
            el("clips_tab", selected=True)]                # "Suggested for you" in the Reels tab
    assert reads.reel_advance_action(card, IG) == "feed_reel"
    assert reads.reel_advance_action(card[:2] + [el("clips_tab")], IG) == ""
    assert reads.reel_advance_action(reel, TG) == ""


@pytest.mark.parametrize("element,is_composer", [
    (el("layout_comment_thread_edittext_multiline", cls="EditText"), True),
    (el("", text="What do you think of this?", cls="EditText"), True),
    (el("", text="Add a comment…", cls="EditText"), True),
    (el("", text="Message...", cls="EditText"), True),
    (el("", text="Add a comment for vaishnavi.arts_", cls="com.instagram.ui.widget.IgEditText"), True),
    (el("", text="What do you think of this?", cls="AutoCompleteTextView"), True),
    (el("action_bar_search_edit_text", text="Search", cls="EditText"), False),
    (el("", text="What do you think of this?", cls="TextView"), False),
])
def test_composer_detection(element, is_composer):
    assert (reads.composer_on_screen([element], IG) is not None) == is_composer


# -- acquire / commit / unit ------------------------------------------------------------

def test_unknown_phone_fails_closed_unless_uncounted_is_allowed(monkeypatch):
    monkeypatch.setenv("CLAUDEPHONE_ACCOUNT_MAP", json.dumps({}))
    d = reads.acquire("profile_open", "ig", serial="127.0.0.1:5555")
    assert not d.allowed and "no ledger account" in d.why
    wr.configure(allow_uncounted_reads=True)
    d = reads.acquire("profile_open", "ig", serial="127.0.0.1:5555")
    assert d.allowed and not d.counted and d.to_dict()["counted"] is False


def test_missing_ledger_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDEPHONE_ACCOUNT_MAP", json.dumps(
        {SAMSUNG: {"alias": "samsung", "account": "aisha_xmehra", "model": "SM-M215F"}}))
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", str(tmp_path))
    d = reads.acquire("profile_open", "ig", serial=SAMSUNG)
    assert not d.allowed and "ledger unavailable" in d.why
    monkeypatch.setenv("CLAUDEPHONE_UNCOUNTED_READS", "1")
    assert reads.acquire("profile_open", "ig", serial=SAMSUNG).counted is False


def test_acquire_and_commit_record_one_row_with_the_run_id(temp_ledger):
    d = reads.acquire("profile_open", "ig", target="creator_a", serial=SAMSUNG)
    assert d.allowed and d.account == "aisha_xmehra" and d.counted
    reads.commit(d, target="creator_a", serial=SAMSUNG)
    assert [tuple(r) for r in rows(temp_ledger, "profile_open")] == [
        ("aisha_xmehra", "profile_open", "creator_a", "claudephone:test", "claudephone read")]


def test_hourly_ceiling_refuses_on_the_25pct_account(temp_ledger):
    lg, _ = temp_ledger
    per_hour = lg.budget_for("saravbhaita", "profile_open")["per_hour"]
    assert per_hour == lg.budget_for("aisha_xmehra", "profile_open")["per_hour"] // 4
    prefill(temp_ledger, "saravbhaita", "RMX3395", "profile_open", per_hour)
    d = reads.acquire("profile_open", "ig", serial=REALME)
    assert not d.allowed and d.why.startswith("ledger:")


def test_per_minute_ceiling_paces_then_gives_up(temp_ledger, monkeypatch):
    lg, _ = temp_ledger
    per_min = lg.budget_for("saravbhaita", "profile_open")["per_min"]
    prefill(temp_ledger, "saravbhaita", "RMX3395", "profile_open", per_min, minutes_ago=0)
    clock = [1000.0]
    monkeypatch.setattr(reads.time, "time", lambda: clock[0])
    monkeypatch.setattr(reads.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    d = reads.acquire("profile_open", "ig", serial=REALME, max_wait_s=20)
    assert not d.allowed and "per-minute" in d.why and d.paced_s >= 20


def test_unit_stops_a_loop_at_the_hourly_ceiling(temp_ledger):
    lg, _ = temp_ledger
    per_hour = lg.budget_for("saravbhaita", "reel_open")["per_hour"]
    prefill(temp_ledger, "saravbhaita", "RMX3395", "reel_open", per_hour - 1)
    d = reads.acquire("reel_open", "ig", serial=REALME, pace=False)
    assert d.allowed
    assert reads.unit(d, "post 1", REALME, max_wait_s=0)
    assert not reads.unit(d, "post 2", REALME, max_wait_s=0)
    assert d.why.startswith("stopped") and d.recorded == 1


# -- the tool surface ------------------------------------------------------------

def _env(monkeypatch, screen, pkg=IG, serial=SAMSUNG):
    from claudephone.runtime import targeting as tg
    from claudephone.tools import input_tools as it
    monkeypatch.setattr(dev, "default_serial", lambda: serial)
    monkeypatch.setattr(tg, "read_screen", lambda serial="": {
        "elements": screen, "obstructions": [], "package": pkg, "backend": "fake", "ms": 0})
    taps, shell = [], []
    monkeypatch.setattr(it, "_dispatch_tap",
                        lambda x, y, hold_ms=0: taps.append((x, y)) or {"ok": True, "via": "fake"})
    monkeypatch.setattr(dev, "shell", lambda cmd, *a, **k: shell.append(cmd) or "")
    monkeypatch.setattr(it, "_screen_wh", lambda: (1080, 2400))
    state.remember(screen, pkg)
    return taps, shell


def _registry():
    from claudephone.agent import build_registry
    return build_registry()


def test_tap_on_an_author_counts_a_profile_open(monkeypatch, temp_ledger):
    name = el("clips_author_username", text="their_bubbles", bounds=(100, 1900, 400, 1960))
    taps, _ = _env(monkeypatch, [name])
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert r.get("ok") and taps == [(250, 1930)]
    assert r["read"]["action"] == "profile_open" and r["read"]["account"] == "aisha_xmehra"
    assert len(rows(temp_ledger, "profile_open")) == 1


def test_tap_on_an_author_is_refused_at_the_ceiling_and_taps_nothing(monkeypatch, temp_ledger):
    lg, _ = temp_ledger
    prefill(temp_ledger, "aisha_xmehra", "SM-M215F", "profile_open",
            lg.budget_for("aisha_xmehra", "profile_open")["per_hour"])
    name = el("clips_author_username", text="their_bubbles", bounds=(100, 1900, 400, 1960))
    taps, _ = _env(monkeypatch, [name])
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert r["error"] == "ledger refused this read" and taps == []


def test_counted_tap_without_a_ledger_needs_uncounted_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", str(tmp_path))
    monkeypatch.setenv("CLAUDEPHONE_ACCOUNT_MAP", json.dumps(
        {SAMSUNG: {"alias": "samsung", "account": "aisha_xmehra", "model": "SM-M215F"}}))
    more = el("clips_ufi_more_button_component", desc="More", bounds=(950, 1700, 1050, 1800))
    taps, _ = _env(monkeypatch, [more])
    assert "ledger refused" in _registry().call("tap", {"ref": state.ref(0)})["error"]
    assert taps == []
    wr.configure(allow_uncounted_reads=True)
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert r.get("ok") and r["read"]["counted"] is False and taps == [(1000, 1750)]


def test_plain_taps_are_not_counted(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", str(tmp_path))     # no ledger at all
    share = el("direct_share_button", desc="Share", bounds=(950, 1500, 1050, 1600))
    taps, _ = _env(monkeypatch, [share])
    r = _registry().call("tap", {"ref": state.ref(0)})
    assert r.get("ok") and "read" not in r and taps == [(1000, 1550)]


def test_typing_into_a_comment_box_is_refused(monkeypatch):
    box = el("layout_comment_thread_edittext_multiline", text="What do you think of this?",
             cls="EditText", bounds=(0, 2100, 1080, 2200))
    _, shell = _env(monkeypatch, [box])
    reg = _registry()
    r = reg.call("text_input", {"text": "nice"})
    assert r["write"]["rule"] == "any.composer" and shell == []
    r = reg.call("press_key", {"key": "enter"})
    assert r["write"]["rule"] == "any.composer" and shell == []
    assert "error" not in reg.call("press_key", {"key": "back"})
    wr.configure(allow_rules={"any.composer"})
    assert reg.call("text_input", {"text": "nice"}).get("typed") == "nice"


def test_typing_an_instagram_search_counts_a_search(monkeypatch, temp_ledger):
    box = el("action_bar_search_edit_text", text="Search", cls="EditText", bounds=(0, 100, 1080, 200))
    _, shell = _env(monkeypatch, [box])
    r = _registry().call("text_input", {"text": "delhi food"})
    assert r["read"]["action"] == "search" and len(shell) == 1
    assert [tuple(x)[2] for x in rows(temp_ledger, "search")] == ["delhi food"]


def test_swiping_the_reels_feed_counts_feed_reels(monkeypatch, temp_ledger):
    screen = [el("clips_author_username", text="a"), el("clips_tab", selected=True)]
    _, shell = _env(monkeypatch, screen)
    reg = _registry()
    assert reg.call("swipe", {"direction": "up"})["read"]["action"] == "feed_reel"
    assert "read" not in reg.call("swipe", {"direction": "down"})
    assert len(shell) == 2 and len(rows(temp_ledger, "feed_reel")) == 1


def test_ig_open_profile_is_refused_before_the_deep_link(monkeypatch, temp_ledger):
    lg, _ = temp_ledger
    prefill(temp_ledger, "aisha_xmehra", "SM-M215F", "profile_open",
            lg.budget_for("aisha_xmehra", "profile_open")["per_hour"])
    _, shell = _env(monkeypatch, [])
    r = _registry().call("ig_open_profile", {"handle": "@creator_a", "settle_s": 0})
    assert r["error"] == "ledger refused this read" and r["opened"] is False and shell == []


def test_ig_open_profile_counts_one_open(monkeypatch, temp_ledger):
    from claudephone.tools.apps import instagram_profile as ig_profile
    _, shell = _env(monkeypatch, [])
    monkeypatch.setattr(ig_profile, "_dump", lambda: [el("action_bar_title", text="creator_a")])
    r = _registry().call("ig_open_profile", {"handle": "@creator_a", "settle_s": 0})
    assert r["opened"] and r["read"]["recorded"] == 1 and len(shell) == 1
    assert [tuple(x)[2] for x in rows(temp_ledger, "profile_open")] == ["creator_a"]


def test_telegram_chat_open_is_a_counted_tg_read(monkeypatch, temp_ledger):
    from claudephone.tools.apps import telegram as tgm
    monkeypatch.setattr(dev, "default_serial", lambda: SAMSUNG)
    opened = []
    monkeypatch.setattr(tgm, "_open", lambda chat, wait_s=4.5, reset=True:
                        opened.append(chat) or {"in_telegram": True, "resolved": True})
    _, err = tgm._open_guard("@somechannel")
    assert err is None and opened == ["@somechannel"]
    assert [tuple(x)[0] for x in rows(temp_ledger, "tg_read")] == ["tg:samsung"]

    lg, _ = temp_ledger
    prefill(temp_ledger, "tg:samsung", "SM-M215F", "tg_read",
            lg.budget_for("tg:samsung", "tg_read")["per_hour"])
    _, err = tgm._open_guard("@another")
    assert err["error"] == "ledger refused this read" and opened == ["@somechannel"]


def test_run_config_carries_uncounted_reads():
    from claudephone.agent import build_policy
    from claudephone import cli
    assert build_policy(allow_uncounted_reads=True).allow_uncounted_reads is True
    assert build_policy().allow_uncounted_reads is False
    src = open(cli.__file__, encoding="utf-8").read()
    assert "--allow-uncounted-reads" in src
