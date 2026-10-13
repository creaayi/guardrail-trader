"""T10: dry-run week report - pairs of runs of the same slot, disagreement, costs, blocked reasons."""
import importlib.util
import sys
from types import SimpleNamespace as NS

import pytest

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal
from guardrail_trader.risk import Decision, Proposal

spec = importlib.util.spec_from_file_location("consistency", PROJECT_ROOT / "scripts" / "consistency.py")
cs = importlib.util.module_from_spec(spec)
sys.modules["consistency"] = cs          # dataclasses look their module up while the class is built
spec.loader.exec_module(cs)


def pair(a, b):
    return cs.Pair("2026-10-19", "eu_open", (1, 2), frozenset(a), frozenset(b))


@pytest.mark.parametrize("a,b,orders,symbols,opposite", [
    ([], [], 0.0, 0.0, set()),                                          # both hold: agreement
    ([("SAP:EUR", "BUY")], [("SAP:EUR", "BUY")], 0.0, 0.0, set()),
    ([("SAP:EUR", "BUY"), ("MC:EUR", "BUY")], [("SAP:EUR", "BUY")], 0.5, 0.5, set()),
    ([("SAP:EUR", "BUY")], [("SAP:EUR", "SELL")], 1.0, 0.0, {"SAP:EUR"}),
    ([("SAP:EUR", "BUY")], [], 1.0, 1.0, set()),
])
def test_disagreement(a, b, orders, symbols, opposite):
    p = pair(a, b)
    assert (p.orders, p.symbols, p.opposite) == (orders, symbols, opposite)


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(1000.0, "EUR")
    yield jr
    jr.close()


def _run(j, slot, orders, status="dry_run", cost=0.4, mode="paper-dry"):
    rid = j.start_run(mode, slot)
    for sym, action, ok in orders:
        j.record_decision(rid, Decision(Proposal(sym, "IBIS", "EUR", action, 1, 100.0, "x"), ok,
                                        [] if ok else [f"{sym}:EUR position would be 2.4% (> 2.0%)"]))
    j.finish_run(rid, status)
    j.record_llm_usage(rid, "anthropic", "m", NS(input_tokens=1, output_tokens=1), cost)
    return rid


def test_week_report(j):
    _run(j, "eu_open", [("SAP", "BUY", True), ("MC", "BUY", False)])
    _run(j, "eu_open", [("SAP", "BUY", True)])
    _run(j, "us_open", [("AAPL", "BUY", True)])                       # alone: no pair
    _run(j, "us_close", [], status="market_closed", cost=0.0)
    _run(j, "eu_open", [("BMW", "BUY", True)], mode="paper")          # a real run is never counted
    today = j.db.execute("SELECT substr(MAX(started_at),1,10) FROM runs").fetchone()[0]
    text = cs.build(j, today, today)
    assert "4 runs à blanc" in text and "coût LLM : 1.20 USD au total" in text
    assert "moyenne 0.400, max 0.400 USD" in text
    assert "| eu_open | #1/#2 |" in text and "| 50% | 50% | - |" in text
    assert "**Taux de désaccord moyen** sur 1 paires : ordres 50%, titres 50%" in text
    assert "1 × <titre> position would be #% (> #%)" in text
    assert "BMW" not in text


def test_no_pairs_is_nd(j):
    _run(j, "eu_open", [])
    today = j.db.execute("SELECT substr(MAX(started_at),1,10) FROM runs").fetchone()[0]
    assert "n/d : aucune paire" in cs.build(j, today, today)


def test_scheduler_dry_run_week_switch(monkeypatch):
    sspec = importlib.util.spec_from_file_location("scheduled_run", PROJECT_ROOT / "scripts" / "scheduled_run.py")
    sr = importlib.util.module_from_spec(sspec)
    sspec.loader.exec_module(sr)
    monkeypatch.delenv("SCHEDULER_DRY_RUN", raising=False)
    assert not sr.dry_run_week()
    monkeypatch.setenv("SCHEDULER_DRY_RUN", "1")
    assert sr.dry_run_week() and set(sr.DECISION_SLOTS) == {"eu_open", "us_open", "us_close"}
