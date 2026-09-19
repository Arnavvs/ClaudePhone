"""Text entry that proves it worked (B10).

    python -m pytest tests/test_text_entry.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import unquote

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from claudephone import device as dev  # noqa: E402
from claudephone.runtime import bridge as br  # noqa: E402
from claudephone.runtime import text_entry as te  # noqa: E402
from claudephone.ui import Element  # noqa: E402

HINGLISH = "दिल्ली food ₹99 😋"


class Phone:
    """A focused field, a bridge whose SET_TEXT may be ignored, and adb."""

    def __init__(self, honours_set_text=True, bridge=True, password=False,
                 residual=False, focus=True):
        self.field = ""
        self.honours = honours_set_text
        self.bridge_up = bridge
        self.password = password
        self.residual = residual      # the node the bridge sets is not the visible one
        self.focus = focus
        self.shell: list[str] = []

    # bridge /text
    def text(self, value, mode="replace"):
        if not self.focus:
            return {"ok": False, "channel": "set_text",
                    "error": "no input-focused field; tap the field first"}
        before = self.field
        want = (before if mode == "append" else "") + value
        node = want if (self.honours or self.residual) else before
        if self.honours and not self.residual:
            self.field = want
        return {"ok": node == want, "channel": "set_text", "acted": True,
                "before": before, "readback": node, "matched": node == want,
                "field": {"id": "search", "editable": True, "password": self.password}}

    def tree(self, limit=300, all_windows=False, max_text=0):
        return {"elements": [Element(i=0, rid="search", text=self.field, cls="EditText",
                                     editable=True, focused=self.focus,
                                     password=self.password)]}

    # adb
    def run(self, cmd, serial=None, **k):
        self.shell.append(cmd)
        if cmd.startswith("input text "):
            body = cmd[len("input text '"):-1].replace("%s", " ").replace("'\\''", "'")
            self.field += body
        elif cmd.startswith("input keyevent KEYCODE_MOVE_END"):
            n = cmd.count("KEYCODE_DEL")
            self.field = self.field[:max(0, len(self.field) - n)]
        return ""


@pytest.fixture
def phone(monkeypatch):
    def make(**kw):
        p = Phone(**kw)
        monkeypatch.setattr(br, "available", lambda serial="", recheck=False: p.bridge_up)
        monkeypatch.setattr(br, "bridge", lambda serial="": p)
        monkeypatch.setattr(dev, "shell", p.run)
        monkeypatch.setattr(te.time, "sleep", lambda s: None)
        monkeypatch.setattr(te, "_visible_texts", lambda serial="": [p.field])
        return p
    return make


def test_unicode_goes_through_the_bridge_and_is_read_back_twice(phone):
    p = phone()
    r = te.enter(HINGLISH)
    assert r["verified"] is True and r["channel"] == "bridge_set_text"
    assert r["checked_by"] == "bridge node + tree read"
    assert p.field == HINGLISH and p.shell == []


def test_replace_replaces_and_append_appends(phone):
    p = phone()
    p.field = "old"
    te.enter("new")
    assert p.field == "new"
    te.enter(" more", mode="append")
    assert p.field == "new more"


def test_an_app_that_ignores_set_text_falls_back_to_adb_for_ascii(phone):
    p = phone(honours_set_text=False)
    p.field = "draft"
    r = te.enter("delhi food")
    assert r["verified"] and r["channel"] == "adb_input_text"
    assert r["tried"][0]["channel"] == "bridge_set_text" and not r["tried"][0]["matched"]
    assert p.field == "delhi food"                   # the draft was cleared first


def test_non_ascii_is_refused_rather_than_typed_with_pieces_missing(phone):
    p = phone(honours_set_text=False)
    r = te.enter(HINGLISH)
    assert "cannot type non-ASCII" in r["error"] and p.shell == []


def test_without_the_bridge_ascii_uses_adb_and_unicode_is_refused(phone):
    p = phone(bridge=False)
    assert te.enter("hello there")["channel"] == "adb_input_text"
    assert p.field == "hello there"
    shell_before = list(p.shell)
    assert "error" in te.enter("नमस्ते")
    assert p.shell == shell_before


def test_true_from_the_action_is_not_trusted_alone(phone):
    """A residual focus node: the bridge's node matches, the visible field does not."""
    p = phone(residual=True)
    r = te.enter("नमस्ते")
    assert "error" in r and r["tried"][0]["tree_readback"] == ""


def test_no_focused_field_is_an_error_with_no_fallback(phone):
    p = phone(focus=False)
    r = te.enter("hello")
    assert "tap the field first" in r["error"] and p.shell == []


def test_a_password_field_is_not_claimed_verified(phone):
    phone(password=True)
    r = te.enter("x")
    assert r["verified"] is None and "masked" in r["note"]


def test_a_typing_mismatch_after_adb_is_an_error(phone, monkeypatch):
    p = phone(bridge=False)
    monkeypatch.setattr(p, "run", lambda cmd, serial=None, **k: p.shell.append(cmd))
    monkeypatch.setattr(dev, "shell", p.run)
    r = te.enter("hello")
    assert r["error"].startswith("the field does not hold") and r["expected"] == "hello"


def test_bad_mode_is_refused(phone):
    phone()
    assert "mode" in te.enter("x", mode="overwrite")["error"]


# -- the client and the tree flags --------------------------------------------

def test_the_client_encodes_everything_and_never_retries_an_append(monkeypatch):
    seen = []
    b = br.Bridge()
    monkeypatch.setattr(b, "_get", lambda path, **k: seen.append((path, k)) or {"ok": True})
    b.text("a+b & ₹ दिल्ली")
    b.text("x", mode="append")
    path, kw = seen[0]
    assert unquote(path.split("value=")[1]) == "a+b & ₹ दिल्ली"
    assert "+" not in path.split("value=")[1] and kw["_retry"] is True
    assert seen[1][0].endswith("&mode=append") and seen[1][1]["_retry"] is False


def test_editable_focused_and_password_flags_round_trip():
    els = br._elements_from([{"i": 0, "id": "q", "cls": "EditText", "f": "CEFPH",
                              "c": [1, 1]}])
    e = els[0]
    assert e.editable and e.focused and e.password and e.clickable
    assert e.hint and e.to_dict()["f"] == "CEFPH"
