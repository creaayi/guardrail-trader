"""T9: the weekly report from a synthetic journal (CLAUDE.md §8.3)."""
import importlib.util
from datetime import date

import pytest

from guardrail_trader import decision
from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.journal import Journal
from guardrail_trader.report import build, calibration, summary, trade_stats, week_bounds
from guardrail_trader.risk import Decision, Holding, Instrument, PortfolioState, Proposal, RiskConfig

UNI = {i.key: i for i in [Instrument("AAPL", "SMART", "USD", "Apple", "Technology", "US"),
                          Instrument("SAP", "IBIS", "EUR", "SAP", "Technology", "DE"),
                          Instrument("MC", "SBF", "EUR", "LVMH", "Consumer, Cyclical", "FR")]}
CFG = RiskConfig(100000.0, "EUR", 25.0, 60, 15.0, 2.0, 10.0, False, UNI)
FRI = date(2026, 11, 13)


def row(pnl, slip=0.0, conf=None, ok=None):
    return {"pnl_net_base": pnl, "slippage_base": slip, "confiance": conf,
            "reussite": int(pnl - slip > 0) if ok is None else ok}


def test_trade_stats_use_pnl_net_of_the_slippage_allowance():
    s = trade_stats([row(100.0), row(-50.0), row(0.5, 1.0), row(30.0)])
    assert (s.n, s.wins) == (4, 2) and s.win_rate == 50.0
    assert s.avg_gain == pytest.approx(65.0) and s.avg_loss == pytest.approx(-25.25)
    assert s.profit_factor == pytest.approx(130.0 / 50.5)
    empty = trade_stats([])
    assert empty.win_rate is None and empty.profit_factor is None


def test_calibration_buckets_and_gap():
    rows = [row(1.0, conf=0.62), row(1.0, conf=0.65), row(-1.0, conf=0.61), row(-1.0, conf=0.68),   # 50% at ~64%
            row(1.0, conf=0.81)]                                                                 # 100% at 81%
    buckets, gap = calibration(rows)
    b6 = next(b for b in buckets if b["bucket"] == "0.6–0.7")
    assert b6["n"] == 4 and b6["success"] == 50.0 and b6["confidence"] == pytest.approx(64.0)
    assert gap == pytest.approx((14.0 * 4 + 19.0) / 5)


def test_week_bounds():
    assert week_bounds(FRI) == (date(2026, 11, 9), date(2026, 11, 15))


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(100000.0, "EUR")
    jr.invest_benchmark(100.0)
    for d, nav, bench in [(6, 100000.0, 100.0), (9, 100500.0, 100.5), (13, 101000.0, 101.0)]:
        jr.record_nav(nav, date(2026, 11, d))
        jr.record_benchmark(bench, date(2026, 11, d))
    run = jr.start_run("paper")
    rec = {"action": "BUY", **decision.example("BUY"), "confiance": 0.65}
    pid = jr.record_decision(run, Decision(Proposal("SAP", "IBIS", "EUR", "BUY", 10, 100.0, "x", poche="A",
                                                    details=decision.details(rec)), True, []))
    jr.record_fill("SAP", "EUR", "BUY", 10, 100.0, 1.0, 1.0, 1, proposal_id=pid, limit_price=100.0)
    jr.book_sell("SAP", "EUR", 10, 112.0, 1.0, 1.0, "objectif", 2, limit_price=112.0)
    with jr.db:     # the round trip happened this week
        jr.db.execute("UPDATE ledger SET ts='2026-11-10T10:00:00+01:00' WHERE kind IN ('BUY','SELL')")
        jr.db.execute("UPDATE closed_trades SET exit_ts='2026-11-12T15:00:00+01:00'")
    jr.record_fill("AAPL", "USD", "BUY", 20, 200.0, 1.0, 0.9)          # an open line, 3,600 EUR
    jr.finish_run(run, "ok", PortfolioState(cash_base=jr.cash_base(), holdings={"AAPL:USD": Holding(20, 180.0)},
                                            peak_value_base=101000.0))
    yield jr
    jr.close()


def test_report_sections_and_figures(j):
    text = build(j, CFG, FRI)
    assert text.startswith("# Rapport hebdomadaire 2026-W46 (09/11 – 15/11/2026)")
    for section in ("## Valeur et performance", "## Décisions fermées", "## Calibration", "## Rotation et coûts",
                    "## Expositions", "## Meilleures et pires"):
        assert section in text
    assert "| Rendement net | +1.00 % |" in text and "| IWDA:EUR | +1.00 % |" in text
    assert "| Nombre | 1 | 1 |" in text and "| Taux de réussite | 100 % | 100 % |" in text
    assert "aucun verdict" in text                                           # far below 200 decisions
    assert "Sharpe : n/d" in text and "ratios n/d sous 20 jours" in text      # 2 days: no ratio
    assert "drawdown courant : 0.00 %" in text                               # at a new high
    assert "| 0.6–0.7 | 1 | 65 % | 100 % | +35 pts |" in text
    assert "US " in text and "Technology" in text and "USD" in text
    assert "thèse : Scripted decision" in text and "objectif" in text


def test_missing_data_is_nd_never_invented(tmp_path):
    jr = Journal(tmp_path / "empty.sqlite")
    jr.init_budget(1000.0, "EUR")
    text = build(jr, CFG, FRI)
    assert "| Rendement net | n/d | n/d |" in text and "alpha (Jensen, annualisé) : n/d" in text
    assert "Aucune décision fermée cette semaine." in text and "n/d (aucun run valorisé)." in text


def test_summary_for_the_phone(j):
    s = summary(build(j, CFG, FRI))
    assert "NAV" in s and "Rendement net" in s and "Nombre" in s and "|" not in s.splitlines()[0][:2]


def test_weekly_report_script_writes_the_file(j, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("weekly_report", PROJECT_ROOT / "scripts" / "weekly_report.py")
    wr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wr)
    monkeypatch.setattr(wr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(wr, "Journal", lambda: Journal(tmp_path / "j.sqlite"))
    monkeypatch.setattr(wr, "load_risk_config", lambda: CFG)
    assert wr.main(["--date", "2026-11-13"]) == 0
    assert (tmp_path / "reports" / "2026-46.md").read_text().startswith("# Rapport hebdomadaire 2026-W46")
