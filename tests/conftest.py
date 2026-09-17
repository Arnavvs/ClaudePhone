"""Shared test guard: no test may touch the real datacollect ledger.

Tools now count reads and writes in the per-account ledger. A test that forgets
to point it somewhere else would write rows to datacollect/collect.db and spend
a real account's budget - which happened once, while B2b was being built. So
every test gets a throwaway database; tests that want to inspect it use the
`temp_ledger` fixture in test_writes.py / test_reads.py, which re-points it.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
DC = os.path.normpath(os.path.join(ROOT, "..", "datacollect", "scripts"))


@pytest.fixture(autouse=True)
def _never_the_real_ledger(monkeypatch, tmp_path):
    from claudephone.policy import writes as wr
    wr._LEDGERS.clear()
    monkeypatch.delenv("CLAUDEPHONE_UNCOUNTED_READS", raising=False)
    if os.path.isfile(os.path.join(DC, "ledger.py")):
        monkeypatch.setenv("CLAUDEPHONE_DATACOLLECT", DC)
        try:
            _, store = wr._ledger_module()
        except wr.LedgerUnavailable:
            store = None
        if store is not None:
            real = os.path.abspath(store.DB)
            monkeypatch.setattr(store, "DB", str(tmp_path / "guard_ledger.db"))
            yield
            assert os.path.abspath(store.DB) != real
            wr._LEDGERS.clear()
            return
    yield
    wr._LEDGERS.clear()
