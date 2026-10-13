"""T6: every order carries a complete decision record (CLAUDE.md §8.1); journal migration; closed decisions."""
import json
import sqlite3

import pytest

from guardrail_trader import decision
from guardrail_trader.decision import example, field_errors
from guardrail_trader.journal import SLIPPAGE_PCT, Journal
from guardrail_trader.risk import Decision, Instrument, MarketInfo, PortfolioState, Proposal, evaluate, load_risk_config
from guardrail_trader.risk.config import RiskConfig


def buy(**kw):
    return {"symbol": "SAP", "currency": "EUR", "action": "BUY", "quantity": 1, "limit_price": 100.0,
            **example("BUY"), **kw}


def sell(**kw):
    return {"symbol": "SAP", "currency": "EUR", "action": "SELL", "quantity": 1, "limit_price": 100.0,
            **example("SELL"), **kw}


# ------------------------------------------------------------------ validation
def test_complete_records_pass():
    assert field_errors(buy(), ("A",)) == [] and field_errors(sell(), ("A",)) == []


@pytest.mark.parametrize("key", decision.COMMON + decision.BUY_ONLY)
def test_a_buy_missing_any_field_is_rejected(key):
    o = buy()
    del o[key]
    assert f"missing field {key}" in field_errors(o)


@pytest.mark.parametrize("key", decision.COMMON + decision.SELL_ONLY)
def test_a_sell_missing_any_field_is_rejected(key):
    o = sell()
    o[key] = ""
    assert f"missing field {key}" in field_errors(o)


@pytest.mark.parametrize("kw,match", [
    ({"poche": "D"}, "poche must be one of"),
    ({"fiabilite": "certain"}, "fiabilite"),
    ({"confiance": 1.2}, "confiance"),
    ({"confiance": True}, "confiance"),
    ({"confiance": float("nan")}, "confiance"),
    ({"horizon_jours": 2.5}, "horizon_jours"),
    ({"horizon_jours": 0}, "horizon_jours"),
    ({"stop_pct": -8}, "stop_pct"),
    ({"objectif_pct": 0}, "objectif_pct"),
    ({"sources": "momentum_ranking"}, "sources"),
    ({"sources": [""]}, "sources"),
    ({"these": "Momentum."}, "2-4 sentences"),
])
def test_malformed_fields_are_rejected(kw, match):
    assert any(match in e for e in field_errors(buy(**kw)))


@pytest.mark.parametrize("motif", ["stop", "kill", "non_precise", "autre"])
def test_claude_cannot_claim_a_code_motif_or_an_unknown_one(motif):
    assert any("motif_sortie" in e for e in field_errors(sell(motif_sortie=motif)))


def test_pocket_b_is_rejected_in_phase_1():
    assert any("not enabled" in e for e in field_errors(buy(poche="B"), ("A",)))
    assert field_errors(buy(poche="B"), ("A", "B")) == []


# ------------------------------------------------------------------ the gate (defence in depth) and config
UNI = {"SAP:EUR": Instrument("SAP", "IBIS", "EUR", "SAP SE", "Technology", "DE")}


def test_gate_blocks_a_pocket_that_is_not_enabled():
    cfg = RiskConfig(100000.0, "EUR", 25.0, 10, 25.0, 2.0, 10.0, False, UNI, pockets=("A",))
    s = PortfolioState(cash_base=100000.0, peak_value_base=100000.0)
    mkt = {"SAP:EUR": MarketInfo(100.0, 1.0, 1.0)}
    (d,), _ = evaluate([Proposal("SAP", "IBIS", "EUR", "BUY", 10, 100.0, poche="B")], s, mkt, cfg)
    assert not d.approved and "pocket B is not enabled" in d.reasons[0]
    (d,), _ = evaluate([Proposal("SAP", "IBIS", "EUR", "BUY", 10, 100.0, poche="A")], s, mkt, cfg)
    assert d.approved


def test_shipped_config_enables_pocket_a_only():
    assert load_risk_config().pockets == ("A",)


@pytest.mark.parametrize("value", ['[]', '["A", "A"]', '["A", "D"]', '"A"'])
def test_pockets_key_is_validated(tmp_path, value):
    from tests.test_drawdown_tiers import _toml
    p = _toml(tmp_path)
    p.write_text(p.read_text().replace('pockets = ["A"]', f"pockets = {value}"))
    with pytest.raises(ValueError, match="pockets"):
        load_risk_config(p)


def test_pockets_key_is_required(tmp_path):
    from tests.test_drawdown_tiers import _toml
    with pytest.raises(KeyError):
        load_risk_config(_toml(tmp_path, pockets=None))


# ------------------------------------------------------------------ journal migration
OLD_SCHEMA = """
CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running', value_base REAL, cash_base REAL, peak_base REAL, drawdown_pct REAL, notes TEXT);
CREATE TABLE proposals (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), ts TEXT NOT NULL,
  symbol TEXT, exchange TEXT, currency TEXT, action TEXT, quantity REAL, limit_price REAL, reason TEXT,
  approved INTEGER NOT NULL, block_reasons TEXT, notional_base REAL, est_commission_base REAL);
CREATE TABLE ledger (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT, currency TEXT,
  quantity REAL, price REAL, commission REAL, fx_to_base REAL, amount_base REAL NOT NULL, broker_order_id INTEGER);
CREATE TABLE closed_trades (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, currency TEXT NOT NULL, entry_ts TEXT,
  exit_ts TEXT NOT NULL, quantity REAL NOT NULL, entry_price REAL, exit_price REAL NOT NULL, pnl_net_base REAL,
  motif TEXT NOT NULL, perm_id INTEGER, broker_order_id INTEGER);
INSERT INTO runs(id, started_at, mode, status) VALUES (1, '2026-10-12T10:00:00+02:00', 'paper', 'ok');
INSERT INTO proposals(run_id, ts, symbol, action, quantity, limit_price, reason, approved)
  VALUES (1, '2026-10-12T10:00:00+02:00', 'SAP', 'BUY', 10, 100.0, 'old run', 1);
INSERT INTO ledger(ts, kind, currency, amount_base, fx_to_base) VALUES ('2026-10-12T09:00:00+02:00', 'DEPOSIT', 'EUR', 1000, 1);
INSERT INTO closed_trades(symbol, currency, exit_ts, quantity, exit_price, motif)
  VALUES ('SAP', 'EUR', '2026-10-12T16:00:00+02:00', 1, 90.0, 'non_precise');
"""


def test_migration_adds_the_columns_to_an_existing_journal_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.close()
    for _ in range(2):                                         # idempotent: the second open changes nothing
        j = Journal(path)
        cols = {r[1] for r in j.db.execute("PRAGMA table_info(proposals)")}
        assert {"poche", "these", "fiabilite", "confiance", "sources", "motif_sortie"} <= cols
        assert {"poche", "confiance", "duree_jours", "reussite", "slippage_base"} <= \
               {r[1] for r in j.db.execute("PRAGMA table_info(closed_trades)")}
        assert tuple(j.db.execute("SELECT reason, poche FROM proposals").fetchone()) == ("old run", None)
        assert j.db.execute("SELECT motif FROM closed_trades").fetchone()[0] == "non_precise"   # history kept
        assert j.cash_base() == 1000
        j.close()


# ------------------------------------------------------------------ journaling and closed decisions
@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(10000.0, "EUR")
    yield jr
    jr.close()


def _proposal(o):
    return Proposal("SAP", "IBIS", "EUR", o["action"], o["quantity"], o["limit_price"], o["these"],
                    poche=o["poche"], details=decision.details(o))


def test_the_decision_record_is_journaled(j):
    run = j.start_run("paper")
    pid = j.record_decision(run, Decision(_proposal(buy(confiance=0.62)), True, []))
    r = j.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    assert (r["poche"], r["fiabilite"], r["confiance"], r["horizon_jours"]) == ("A", "mecanique", 0.62, 60)
    assert json.loads(r["sources"]) == ["momentum_ranking"] and r["motif_sortie"] is None
    pid = j.record_decision(run, Decision(_proposal(sell(motif_sortie="horizon")), True, []))
    assert tuple(j.db.execute("SELECT motif_sortie, catalyseur FROM proposals WHERE id=?", (pid,)).fetchone()) == \
        ("horizon", None)


def _round_trip(j, exit_price, exit_limit, *, confiance=0.6):
    run = j.start_run("paper")
    entry = j.record_decision(run, Decision(_proposal(buy(confiance=confiance)), True, []))
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 1.0, 1.0, 7, proposal_id=entry, limit_price=100.0)
    out = j.record_decision(run, Decision(_proposal(sell(motif_sortie="objectif")), True, []))
    j.book_sell("SAP", "EUR", 10, exit_price, 1.0, 1.0, "objectif", 8, proposal_id=out, limit_price=exit_limit)
    return entry, out, j.db.execute("SELECT * FROM closed_trades").fetchone()


def test_closed_trade_links_both_decisions(j):
    entry, out, t = _round_trip(j, 110.0, 110.0, confiance=0.7)
    assert (t["entry_proposal_id"], t["exit_proposal_id"], t["poche"], t["confiance"]) == (entry, out, "A", 0.7)
    assert t["pnl_net_base"] == pytest.approx(98.0) and t["duree_jours"] == 0
    assert t["slippage_base"] == pytest.approx((1000 + 1100) * SLIPPAGE_PCT / 100) and t["reussite"] == 1


def test_a_gain_eaten_by_the_slippage_allowance_is_not_a_success(j):
    # +2.20 gross on 1,000 - 2.00 commissions = +0.20 net; allowance 0.05% x (1,000 + 1,002.2) = 1.00 -> failure
    *_, t = _round_trip(j, 100.22, 100.22)
    assert t["pnl_net_base"] == pytest.approx(0.2) and t["reussite"] == 0


def test_no_allowance_on_a_leg_filled_better_than_its_limit(j):
    *_, t = _round_trip(j, 100.30, 100.25)                    # exit filled above its limit: no exit allowance
    assert t["slippage_base"] == pytest.approx(1000 * SLIPPAGE_PCT / 100)


def test_a_stop_exit_inherits_the_entry_decision(j):
    run = j.start_run("paper")
    entry = j.record_decision(run, Decision(_proposal(buy(confiance=0.55)), True, []))
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 1.0, 1.0, 7, proposal_id=entry, limit_price=100.0)
    j.book_stop_fill("x1", 5, "SAP", "EUR", 10, 92.0, 1.0, "EUR", 1.0, 1.0)
    t = j.db.execute("SELECT * FROM closed_trades").fetchone()
    assert (t["motif"], t["entry_proposal_id"], t["confiance"], t["exit_proposal_id"]) == ("stop", entry, 0.55, None)
    assert t["slippage_base"] == pytest.approx(0.5) and t["reussite"] == 0     # entry leg only


def test_partial_exits_split_the_entry_allowance(j):
    run = j.start_run("paper")
    entry = j.record_decision(run, Decision(_proposal(buy()), True, []))
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 1.0, 1.0, 7, proposal_id=entry, limit_price=100.0)
    j.book_sell("SAP", "EUR", 4, 120.0, 1.0, 1.0, "objectif")
    j.book_sell("SAP", "EUR", 6, 120.0, 1.0, 1.0, "horizon")
    slips = [r[0] for r in j.db.execute("SELECT slippage_base FROM closed_trades ORDER BY id")]
    assert slips == [pytest.approx(0.2), pytest.approx(0.3)]
    assert [r[0] for r in j.db.execute("SELECT entry_proposal_id FROM closed_trades")] == [entry, entry]


# ------------------------------------------------------------------ the agent's submit
def test_kill_switch_sells_carry_the_kill_motif():
    from guardrail_trader.risk import Holding, liquidation_proposals
    cfg = RiskConfig(1000.0, "EUR", 25.0, 10, 25.0, 2.0, 10.0, False, UNI)
    s = PortfolioState(cash_base=0.0, holdings={"SAP:EUR": Holding(5, 100.0)}, peak_value_base=1000.0)
    (p,) = liquidation_proposals(s, {"SAP:EUR": MarketInfo(100.0, 1.0, 0.0)}, cfg)
    assert p.details == {"motif_sortie": "kill"} and p.poche == ""
