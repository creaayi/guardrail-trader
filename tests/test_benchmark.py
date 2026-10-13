"""T7: IWDA benchmark (virtual units bought on day 1), daily NAV, alpha/beta and the other metrics."""
import math
from datetime import date
from types import SimpleNamespace as NS

import pytest

from guardrail_trader import metrics
from guardrail_trader.journal import Journal
from guardrail_trader.metrics import BENCHMARK_KEY, compute, from_journal
from guardrail_trader.pipeline import record_benchmark
from guardrail_trader.risk import Instrument


def days(n, start=1):
    return [f"2026-11-{d:02d}" for d in range(start, start + n)]


def series(rets, start=100.0):
    out, v = [start], start
    for r in rets:
        v *= 1 + r
        out.append(v)
    return out


RB = [0.01 * math.sin(i * 1.7) + 0.002 * math.cos(i * 0.3) for i in range(25)]   # a deterministic "market"


# ------------------------------------------------------------------ metrics on synthetic series
def test_alpha_and_beta_are_recovered_from_a_synthetic_series():
    a = 0.0004                                         # 4 bp a day over the benchmark, beta 1.2
    rp = [a + 1.2 * r for r in RB]
    d = days(26)
    perf = compute(dict(zip(d, series(rp))), dict(zip(d, series(RB))))
    assert perf.days == 25 and perf.beta == pytest.approx(1.2)
    assert perf.alpha_pct == pytest.approx(a * 252 * 100)
    assert perf.excess_pct == pytest.approx(metrics.twr_pct(rp) - metrics.twr_pct(RB))


def test_holding_the_benchmark_has_no_alpha():
    d = days(26)
    nav = dict(zip(d, series(RB, 950000.0)))
    perf = compute(nav, dict(nav))
    assert perf.alpha_pct == pytest.approx(0, abs=1e-9) and perf.beta == pytest.approx(1.0)
    assert perf.excess_pct == pytest.approx(0, abs=1e-9) and perf.sharpe == pytest.approx(perf.benchmark_sharpe)


def test_a_deposit_is_a_flow_not_a_return():
    d = days(3)
    nav = {d[0]: 1000.0, d[1]: 1010.0, d[2]: 1510.0}           # +1%, then a 500 top-up and flat
    bench = {d[0]: 1000.0, d[1]: 1000.0, d[2]: 1500.0}         # benchmark invests the same 500 on day 3
    perf = compute(nav, bench, {d[2]: 500.0}, {d[2]: 500.0})
    assert perf.return_pct == pytest.approx(1.0) and perf.benchmark_return_pct == pytest.approx(0.0)


def test_benchmark_holidays_carry_the_last_value():
    d = days(3)
    perf = compute({d[0]: 100.0, d[1]: 101.0, d[2]: 102.0}, {d[0]: 100.0, d[2]: 103.0})
    assert perf.days == 2 and perf.benchmark_return_pct == pytest.approx(3.0)


def test_drawdown_sharpe_and_sortino():
    assert metrics.max_drawdown_pct([0.10, -0.20, 0.05]) == pytest.approx(20.0)
    assert metrics.sharpe([0.01, 0.01]) is None                # no volatility: undefined, not infinite
    assert metrics.sortino([0.01, 0.02]) is None               # no losing day
    assert metrics.sortino([0.01, -0.01, 0.02]) > 0


def test_not_enough_data_is_none_never_invented():
    perf = compute({"2026-11-02": 100.0}, {"2026-11-02": 100.0})
    assert perf.days == 0 and perf.return_pct is None and perf.alpha_pct is None and perf.sharpe is None


# ------------------------------------------------------------------ journal
@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(950000.0, "EUR")
    yield jr
    jr.close()


def test_init_buys_capital_over_price_units(j):
    j.invest_benchmark(100.0)
    assert j.benchmark_units() == pytest.approx(9500.0)
    assert j.invest_benchmark(120.0) == 0.0                    # nothing new to invest: idempotent
    assert j.record_benchmark(110.0, date(2026, 11, 2)) == pytest.approx(1045000.0)


def test_snapshot_before_the_benchmark_is_started_refuses(tmp_path):
    jr = Journal(tmp_path / "x.sqlite")
    jr.init_budget(1000.0, "EUR")
    with pytest.raises(RuntimeError, match="init"):
        jr.record_benchmark(100.0)


def test_a_topup_is_invested_at_the_next_snapshot_and_counted_as_a_flow(j):
    j.invest_benchmark(100.0)
    j.record_benchmark(100.0, date(2026, 11, 2))
    j.record_nav(950000.0, date(2026, 11, 2))
    j.topup_to(1000000.0, "EUR")
    j.record_benchmark(125.0, date(2026, 11, 3))               # +25%, then 50,000 buys 400 units
    j.record_benchmark(125.0, date(2026, 11, 3))               # same day again: invested once
    assert j.benchmark_units() == pytest.approx(9900.0)
    row = j.db.execute("SELECT invested_base, nav_base FROM benchmark WHERE date='2026-11-03'").fetchone()
    assert tuple(row) == (pytest.approx(50000.0), pytest.approx(1237500.0))


def test_performance_from_the_journal(j):
    j.invest_benchmark(100.0)
    for day, bench_px, nav in [(2, 100.0, 950000.0), (3, 110.0, 950000.0), (4, 121.0, 969000.0)]:
        j.record_benchmark(bench_px, date(2026, 11, day))
        j.record_nav(nav, date(2026, 11, day))
    perf = from_journal(j)
    assert (perf.first_date, perf.last_date, perf.days) == ("2026-11-02", "2026-11-04", 2)
    assert perf.return_pct == pytest.approx(2.0) and perf.benchmark_return_pct == pytest.approx(21.0)
    assert perf.excess_pct == pytest.approx(-19.0)


def test_closed_trade_return_is_compared_with_the_benchmark_over_the_same_days(j):
    from datetime import timedelta
    j.invest_benchmark(100.0)
    today = date.today()
    j.record_benchmark(100.0, today - timedelta(days=7))
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 0.0, 1.0)
    with j.db:                                                 # bought a week ago
        j.db.execute("UPDATE ledger SET ts=? WHERE kind='BUY'", ((today - timedelta(days=7)).isoformat() + "T10:00:00+01:00",))
    j.record_benchmark(103.0, today)                           # benchmark +3% over the holding period
    j.book_sell("SAP", "EUR", 10, 110.0, 0.0, 1.0, "objectif")   # trade +10%
    t = j.db.execute("SELECT pnl_vs_benchmark_pct, duree_jours FROM closed_trades").fetchone()
    assert tuple(t) == (pytest.approx(7.0), 7)


def test_closed_trade_without_benchmark_data_leaves_the_comparison_empty(tmp_path):
    jr = Journal(tmp_path / "y.sqlite")
    jr.init_budget(1000.0, "EUR")
    jr.record_fill("SAP", "EUR", "BUY", 1, 100.0, 0.0, 1.0)
    jr.book_sell("SAP", "EUR", 1, 110.0, 0.0, 1.0, "objectif")
    assert jr.db.execute("SELECT pnl_vs_benchmark_pct FROM closed_trades").fetchone()[0] is None


# ------------------------------------------------------------------ pipeline snapshot
CFG = NS(hedge={BENCHMARK_KEY: Instrument("IWDA", "AEB", "EUR")}, universe={}, base_currency="EUR")


def test_pipeline_snapshot_records_the_day(j):
    j.invest_benchmark(100.0)
    logs = []
    record_benchmark(j, NS(benchmark_price=lambda k: 105.0), CFG, {"EUR": 1.0}, date(2026, 11, 2), logs.append)
    assert j.db.execute("SELECT nav_base FROM benchmark").fetchone()[0] == pytest.approx(997500.0)


@pytest.mark.parametrize("market", [NS(benchmark_price=lambda k: None),
                                    NS(benchmark_price=lambda k: (_ for _ in ()).throw(ConnectionError("x")))])
def test_pipeline_snapshot_never_blocks_a_run(j, market):
    j.invest_benchmark(100.0)
    logs = []
    record_benchmark(j, market, CFG, {"EUR": 1.0}, date(2026, 11, 2), logs.append)
    assert j.db.execute("SELECT COUNT(*) FROM benchmark").fetchone()[0] == 0 and "carried" in logs[0]


def test_pipeline_snapshot_is_skipped_until_init_started_the_benchmark(tmp_path):
    jr = Journal(tmp_path / "z.sqlite")
    jr.init_budget(1000.0, "EUR")
    record_benchmark(jr, NS(benchmark_price=lambda k: pytest.fail("not asked")), CFG, {"EUR": 1.0},
                     date(2026, 11, 2), print)


def test_shipped_hedge_file_lists_the_benchmark():
    from guardrail_trader.risk import load_risk_config
    assert BENCHMARK_KEY in load_risk_config().hedge
