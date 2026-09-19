"""The ledger served to the phone (B2c), without a phone.

The service runs in-process against a throwaway database (conftest.py points
store.DB there), and the gate is pointed at it through CLAUDEPHONE_LEDGER_URL -
the same path an agent in Termux takes over `adb reverse`.

    python -m pytest tests/test_ledger_service.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone.policy import ledger_service as ls  # noqa: E402
from claudephone.policy import reads  # noqa: E402
from claudephone.policy import writes as wr  # noqa: E402
from claudephone.ui import Element  # noqa: E402

IG = "com.instagram.android"
DC = os.path.normpath(os.path.join(ROOT, "..", "datacollect", "scripts"))
SAMSUNG, REALME = "RZ8N70HYQSB", "HIDMFQ8X894DIVLZ"
TOKEN = "test-token-not-the-real-one"


@pytest.fixture
def service(monkeypatch, tmp_path):
    """A ledger service on a free port, over a throwaway database."""
    if not os.path.isfile(os.path.join(DC, "ledger.py")):
        pytest.skip("datacollect not present")
    monkeypatch.delenv("CLAUDEPHONE_ACCOUNT_MAP", raising=False)
    monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
    lg, store = wr._ledger_module()
    monkeypatch.setattr(store, "DB", str(tmp_path / "served.db"))
    httpd = ls.serve(port=0, tok=TOKEN, block=False)
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    monkeypatch.setenv("CLAUDEPHONE_LEDGER_URL", url)
    monkeypatch.setenv("CLAUDEPHONE_LEDGER_TOKEN", TOKEN)
    wr.configure(mode="auto", writes=(), allow_rules=(), run_id="svc")
    wr._LEDGERS.clear()
    yield url, lg, store
    httpd.shutdown()
    wr._LEDGERS.clear()
    wr.configure(mode="auto", writes=(), allow_rules=(), run_id="")


def rows(store, action):
    return [tuple(r) for r in store.connect().execute(
        "SELECT account, device, action, target, run_id, note FROM action_log "
        "WHERE action = ?", (action,)).fetchall()]


def test_health_needs_no_token_but_everything_else_does(service):
    url, _, _ = service
    assert ls.available(url)["ok"] is True
    good = ls.RemoteLedger(url, "aisha_xmehra", tok=TOKEN)
    assert good.can("profile_open")[0] is True
    bad = ls.RemoteLedger(url, "aisha_xmehra", tok="wrong")
    with pytest.raises(wr.LedgerUnavailable) as e:
        bad.can("profile_open")
    assert "401" in str(e.value)


def test_the_service_answers_with_the_accounts_own_ceilings(service):
    url, lg, _ = service
    scaled = ls.RemoteLedger(url, "saravbhaita", tok=TOKEN).budget("profile_open")
    full = ls.RemoteLedger(url, "aisha_xmehra", tok=TOKEN).budget("profile_open")
    assert scaled == lg.budget_for("saravbhaita", "profile_open")
    assert scaled["per_hour"] == full["per_hour"] // 4        # the 25% account


def test_a_counted_read_from_the_phone_lands_in_the_real_table(service):
    _, _, store = service
    d = reads.acquire("profile_open", "ig", target="creator_a", serial=SAMSUNG)
    assert d.allowed and d.account == "aisha_xmehra"
    reads.commit(d, target="creator_a", serial=SAMSUNG)
    assert rows(store, "profile_open") == [
        ("aisha_xmehra", "SM-M215F", "profile_open", "creator_a",
         "claudephone:svc", "claudephone read")]


def test_a_budgeted_write_from_the_phone_is_checked_and_recorded(service):
    _, _, store = service
    wr.configure(writes={"follow"}, run_id="svc")
    follow = Element(i=0, rid="inline_follow_button", anchor="inline_follow_button",
                     text="Follow", desc="", cls="Button", bounds=(0, 0, 10, 10),
                     clickable=True)
    d = wr.decide(wr.classify(follow, IG), SAMSUNG)
    assert d.allowed and d.account == "aisha_xmehra"
    assert wr.commit(d, target="creator_a", serial=SAMSUNG) is None
    assert len(rows(store, "follow")) == 1


def test_the_ceiling_is_enforced_by_the_service_not_the_caller(service):
    url, lg, store = service
    led = lg.Ledger(store.connect(), account="saravbhaita", device="RMX3395")
    for _ in range(lg.budget_for("saravbhaita", "profile_open")["per_hour"]):
        led.record("profile_open", target="earlier")
    store.connect().commit()
    d = reads.acquire("profile_open", "ig", serial=REALME)
    assert not d.allowed and d.why.startswith("ledger:")
    # and the remote client cannot talk its way past it
    assert ls.RemoteLedger(url, "saravbhaita", tok=TOKEN).can("profile_open")[0] is False


def test_the_service_going_away_fails_closed(service, monkeypatch):
    url, _, _ = service
    monkeypatch.setenv("CLAUDEPHONE_LEDGER_URL", url.replace(str(url.rsplit(":", 1)[1]), "1"))
    wr._LEDGERS.clear()
    d = reads.acquire("profile_open", "ig", serial=SAMSUNG)
    assert not d.allowed and "ledger" in d.why
    wr.configure(writes={"follow"})
    follow = Element(i=0, rid="inline_follow_button", anchor="inline_follow_button",
                     text="Follow", desc="", cls="Button", bounds=(0, 0, 10, 10),
                     clickable=True)
    assert not wr.decide(wr.classify(follow, IG), SAMSUNG).allowed


def test_unset_url_still_uses_the_local_database(service, monkeypatch):
    _, _, store = service
    monkeypatch.delenv("CLAUDEPHONE_LEDGER_URL")
    wr._LEDGERS.clear()
    d = reads.acquire("grid_scan", "ig", target="local", serial=SAMSUNG)
    assert d.allowed
    reads.commit(d, target="local", serial=SAMSUNG)
    assert rows(store, "grid_scan")[0][3] == "local"


def test_token_file_is_created_private(tmp_path):
    p = str(tmp_path / "ledger_token")
    t = ls.token(p)
    assert len(t) > 20 and ls.token(p) == t                  # stable once written
    assert json.dumps(t)                                     # plain string
    if os.name != "nt":
        assert oct(os.stat(p).st_mode)[-3:] == "600"
