from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from guardrail_trader import dashboard_data
from guardrail_trader.journal import Journal
from guardrail_trader.risk import Decision, Holding, PortfolioState, Proposal
from tests.test_agent import CFG


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(5000.0, "AUD")
    yield jr
    jr.close()


def test_empty_journal_builds(j):
    d = dashboard_data.build(j, CFG)
    assert d["kpis"]["value"] == 5000.0 and d["kpis"]["pnl"] == 0 and d["holdings"] == []


def test_pnl_is_against_total_deposits_so_a_topup_is_not_profit(j):
    j.topup_to(10000.0, "AUD")                               # second deposit of 5,000
    d = dashboard_data.build(j, CFG)
    k = d["kpis"]
    assert k["capital"] == 10000.0 and k["value"] == 10000.0
    assert k["pnl"] == 0 and k["pnl_pct"] == 0               # a deposit is not a gain
    assert [dp["amount"] for dp in k["deposits"]] == [5000.0, 5000.0]


def test_topup_after_the_last_run_counts_at_once_not_as_a_loss(j):
    run = j.start_run("paper")
    j.record_fill("CSCO", "USD", "BUY", 2, 110.0, commission=1.0, fx_to_base=1.4)         # cost 309.4
    state = PortfolioState(j.cash_base(), {"CSCO:USD": Holding(2, 120 * 1.4)}, 5000.0)
    j.finish_run(run, "ok", state, "bought CSCO")
    before = dashboard_data.build(j, CFG)["kpis"]
    j.topup_to(10000.0, "AUD")                               # no run has happened since
    with j.db:                                               # timestamps are whole seconds: pin it after the run
        j.db.execute("UPDATE ledger SET ts='2999-01-01T00:00:00+10:00' WHERE kind='DEPOSIT' AND amount_base=5000 "
                     "AND id=(SELECT MAX(id) FROM ledger)")
    after = dashboard_data.build(j, CFG)["kpis"]
    assert after["value"] == pytest.approx(before["value"] + 5000.0)
    assert after["pnl"] == pytest.approx(before["pnl"])      # a deposit moves value and baseline together


def test_holdings_pnl_and_decisions(j):
    run = j.start_run("paper")
    p = Proposal("CSCO", "SMART", "USD", "BUY", 2, 110.0, reason="cheap vs SMA50")
    pid = j.record_decision(run, Decision(p, True, [], 308.0, 1.4))
    j.record_decision(run, Decision(Proposal("META", "SMART", "USD", "BUY", 3, 680.0, "x"), False, ["position too big"]))
    j.record_order(pid, NS(order_id=9, symbol="CSCO", action="BUY", quantity=2, limit_price=110.0,
                           status="Filled", filled=2, avg_fill_price=110.0, message=""))
    j.record_fill("CSCO", "USD", "BUY", 2, 110.0, commission=1.0, fx_to_base=1.4, broker_order_id=9)  # cost 309.4
    state = PortfolioState(j.cash_base(), {"CSCO:USD": Holding(2, 120 * 1.4)}, 5000.0)
    j.finish_run(run, "ok", state, "bought CSCO")

    d = dashboard_data.build(j, CFG)
    (h,) = d["holdings"]
    assert h["avg_cost_base"] == pytest.approx(154.7) and h["price_base"] == pytest.approx(168.0)
    assert h["pnl_base"] == pytest.approx((168.0 - 154.7) * 2)
    assert d["kpis"]["value"] == pytest.approx(5000 - 309.4 + 336.0)
    assert d["kpis"]["brokerage_base"] == pytest.approx(1.4) and d["kpis"]["brokerage_fills"] == 1
    assert d["kpis"]["traded_base"] == pytest.approx(308.0)
    assert {x["symbol"]: x["approved"] for x in d["decisions"]} == {"CSCO": 1, "META": 0}
    assert d["decisions"][-1]["order_status"] == "Filled"
    assert d["runs"][0]["approved"] == 1 and d["runs"][0]["blocked"] == 1


def _run_at(j, ts, symbol):
    """One run with a proposal and a fill, all timestamps pinned (they default to 'now', whole seconds)."""
    run = j.start_run("paper")
    p = Proposal(symbol, "SMART", "USD", "BUY", 1, 10.0, reason=f"buy {symbol}")
    j.record_decision(run, Decision(p, True, [], 10.0, 1.0))
    j.record_fill(symbol, "USD", "BUY", 1, 10.0, commission=1.0, fx_to_base=1.0)
    j.finish_run(run, "ok", PortfolioState(j.cash_base(), {}, 5000.0), f"bought {symbol}")
    with j.db:
        j.db.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?", (ts, ts, run))
        j.db.execute("UPDATE proposals SET ts=? WHERE run_id=?", (ts, run))
        j.db.execute("UPDATE ledger SET ts=? WHERE id=(SELECT MAX(id) FROM ledger)", (ts,))
    return run


@pytest.fixture
def week(j):
    """Sydney timestamps. US/Eastern dates: Thu 24 Sep, Fri 25 Sep, then Mon 28 Sep (open AND close slot)."""
    return {
        "thu": _run_at(j, "2026-09-25T00:30:01+10:00", "AAA"),
        "fri": _run_at(j, "2026-09-26T00:30:01+10:00", "BBB"),
        "mon_open": _run_at(j, "2026-09-29T00:30:01+10:00", "CCC"),
        "mon_close": _run_at(j, "2026-09-29T05:00:01+10:00", "DDD"),
    }


def _ids(d):
    return {r["id"] for r in d["runs"]}, {x["symbol"] for x in d["decisions"]}, {f["symbol"] for f in d["fills"]}


def test_tables_default_to_the_last_two_trading_days(j, week):
    d = dashboard_data.build(j, CFG)
    assert _ids(d) == ({week["fri"], week["mon_open"], week["mon_close"]}, {"BBB", "CCC", "DDD"}, {"BBB", "CCC", "DDD"})
    w = d["window"]
    assert (w["days"], w["since"], w["until"], w["available_days"]) == (2, "2026-09-25", "2026-09-28", 3)
    assert w["shown"] == {"runs": 3, "decisions": 3, "fills": 3}
    assert w["totals"] == {"runs": 4, "decisions": 4, "fills": 4}


def test_open_and_close_slots_share_one_trading_day_and_weekends_cost_nothing(j, week):
    d = dashboard_data.build(j, CFG, days=1)               # Monday only: both slots, and the gap over the weekend
    assert _ids(d)[0] == {week["mon_open"], week["mon_close"]}
    assert d["window"]["since"] == d["window"]["until"] == "2026-09-28"


def test_a_trading_day_is_the_us_eastern_date_not_the_sydney_one(j):
    a = _run_at(j, "2026-09-28T23:00:01+10:00", "EEE")     # Sydney Mon 23:00 = NY Mon 09:00
    b = _run_at(j, "2026-09-29T05:00:01+10:00", "FFF")     # Sydney Tue 05:00 = NY Mon 15:00: same session
    c = _run_at(j, "2026-09-27T05:00:01+10:00", "GGG")     # Sydney Sun 05:00 = NY Sat 15:00: the day before
    d = dashboard_data.build(j, CFG, days=1)
    assert _ids(d)[0] == {a, b} and c not in _ids(d)[0]


def test_days_none_shows_everything_and_a_big_window_is_the_same(j, week):
    everything = _ids(dashboard_data.build(j, CFG, days=None))
    assert everything == ({*week.values()}, {"AAA", "BBB", "CCC", "DDD"}, {"AAA", "BBB", "CCC", "DDD"})
    assert _ids(dashboard_data.build(j, CFG, days=30)) == everything


def test_window_only_affects_the_tables_not_the_chart_or_kpis(j, week):
    small, full = dashboard_data.build(j, CFG, days=1), dashboard_data.build(j, CFG, days=None)
    assert small["value_series"] == full["value_series"] and len(small["value_series"]) == 4
    assert small["kpis"] == full["kpis"] and small["holdings"] == full["holdings"]


def test_window_with_no_runs_is_empty_not_an_error(j):
    w = dashboard_data.build(j, CFG)["window"]
    assert w["available_days"] == 0 and w["since"] is None and w["until"] is None
    assert w["shown"] == {"runs": 0, "decisions": 0, "fills": 0}


@pytest.mark.parametrize("raw,expected", [(None, 2), ("", 2), ("1", 1), ("30", 30), ("all", None)])
def test_parse_days(raw, expected):
    assert dashboard_data.parse_days(raw) == expected


@pytest.mark.parametrize("bad", ["0", "-1", "2.5", "abc", "99999", "ALL", "2; DROP TABLE runs"])
def test_parse_days_rejects_junk(bad):
    with pytest.raises(ValueError):
        dashboard_data.parse_days(bad)


def test_dashboard_rejects_foreign_host_headers(monkeypatch):
    """DNS rebinding: only localhost Host headers are served."""
    import importlib.util
    from guardrail_trader.config import PROJECT_ROOT
    spec = importlib.util.spec_from_file_location("dash", PROJECT_ROOT / "scripts" / "dashboard.py")
    dash = importlib.util.module_from_spec(spec); spec.loader.exec_module(dash)
    monkeypatch.delenv("DASHBOARD_ALLOWED_HOSTS", raising=False)
    hosts = dash.allowed_hosts(8765)
    assert hosts == {"127.0.0.1:8765", "localhost:8765"}
    assert "attacker.example:8765" not in hosts
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "Dash.lan:8765")
    assert "dash.lan:8765" in dash.allowed_hosts(8765)


def test_executor_refuses_blocked_decision():
    from guardrail_trader.executor import execute
    blocked = Decision(Proposal("CSCO", "SMART", "USD", "BUY", 1, 100.0, "x"), False, ["no"])
    with pytest.raises(RuntimeError):
        execute(broker=None, journal=None, decisions=[(1, blocked)], fx={})


def test_decisions_show_pocket_confidence_and_thesis(j):
    from guardrail_trader.decision import details, example
    run = j.start_run("paper")
    rec = {"action": "BUY", **example("BUY"), "confiance": 0.6}
    j.record_decision(run, Decision(Proposal("CSCO", "SMART", "USD", "BUY", 2, 110.0, "x", poche="A",
                                             details=details(rec)), True, []))
    (row,) = dashboard_data.build(j, CFG)["decisions"]
    assert (row["poche"], row["confiance"], row["fiabilite"]) == ("A", 0.6, "mecanique")
    assert row["these"] == rec["these"]
    html = (Path(dashboard_data.__file__).parent / "web" / "dashboard.html").read_text()
    assert "decisionTags" in html and "r.these" in html
