from types import SimpleNamespace

import pytest

from guardrail_trader.journal import Journal
from guardrail_trader.risk import Decision, Proposal


@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(100.0, "AUD")
    yield jr
    jr.close()


def test_budget_initialised_once(j):
    assert j.cash_base() == 100.0 and j.base_currency() == "AUD"
    with pytest.raises(RuntimeError):
        j.init_budget(100.0, "AUD")


def test_fills_update_virtual_cash_and_holdings(j):
    j.record_fill("AAA", "USD", "BUY", 2, 10.0, commission=1.0, fx_to_base=1.5)   # -(20+1)*1.5 = -31.5
    assert j.cash_base() == pytest.approx(68.5)
    assert j.holdings_qty() == {"AAA:USD": 2}
    j.record_fill("AAA", "USD", "SELL", 2, 11.0, commission=1.0, fx_to_base=1.5)  # +(22-1)*1.5 = 31.5
    assert j.cash_base() == pytest.approx(100.0)
    assert j.holdings_qty() == {}


def test_portfolio_state_values_holdings_and_tracks_peak(j):
    j.record_fill("BBB", "AUD", "BUY", 4, 5.0, commission=0.5, fx_to_base=1.0)
    s = j.portfolio_state({"BBB:AUD": 6.0})
    assert s.value_base == pytest.approx(79.5 + 24.0)
    assert j.peak() == pytest.approx(100.0)            # starts at capital; 103.5 not yet confirmed
    s = j.portfolio_state({"BBB:AUD": 4.0})
    assert s.peak_value_base == pytest.approx(100.0)  # peak never goes down
    with pytest.raises(KeyError):
        j.portfolio_state({})  # refuses to value a holding without a price


def test_halt_and_reset(j):
    j.halt("drawdown")
    assert j.is_halted() and j.portfolio_state({}).halted
    j.reset_halt(90.0)
    assert not j.is_halted() and j.peak() == 90.0


def test_decisions_and_orders_are_journaled_and_counted(j):
    run = j.start_run("paper")
    p = Proposal("BBB", "ASX", "AUD", "BUY", 1, 5.0, reason="because")
    pid = j.record_decision(run, Decision(p, True, [], 5.0, 0.5))
    j.record_decision(run, Decision(p, False, ["not on allowlist"]))
    j.record_order(pid, SimpleNamespace(order_id=7, symbol="BBB", action="BUY", quantity=1, limit_price=5.0,
                                        status="Submitted", filled=0, avg_fill_price=float("nan"), message=""))
    assert j.orders_this_month() == 1
    rows = j.db.execute("SELECT approved, reason FROM proposals").fetchall()
    assert [(r["approved"], r["reason"]) for r in rows] == [(1, "because"), (0, "because")]
    j.finish_run(run, "ok", j.portfolio_state({}))


def test_peak_only_rises_to_a_value_seen_on_two_consecutive_runs(j):
    j.record_fill("BBB", "AUD", "BUY", 4, 5.0, commission=0.5, fx_to_base=1.0)   # cash 79.5; peak = capital 100
    j.portfolio_state({"BBB:AUD": 5.0})                    # 99.5
    assert j.portfolio_state({"BBB:AUD": 50.0}).peak_value_base == pytest.approx(100.0)  # spike 279.5: unconfirmed
    assert j.portfolio_state({"BBB:AUD": 5.0}).peak_value_base == pytest.approx(100.0)   # back to normal
    j.portfolio_state({"BBB:AUD": 6.0})                    # real rise to 103.5, seen once
    assert j.peak() == pytest.approx(100.0)
    j.portfolio_state({"BBB:AUD": 6.5})                    # 105.5 -> 103.5 confirmed
    assert j.peak() == pytest.approx(103.5)


def test_revaluation_in_the_same_run_does_not_confirm_a_spike(j):
    j.record_fill("BBB", "AUD", "BUY", 4, 5.0, commission=0.5, fx_to_base=1.0)
    j.portfolio_state({"BBB:AUD": 5.0})
    j.portfolio_state({"BBB:AUD": 50.0})                   # spike run...
    j.portfolio_state({"BBB:AUD": 50.0}, observe=False)    # ...its post-trade valuation
    assert j.peak() == pytest.approx(100.0)


def test_existing_journal_without_last_value_does_not_raise_peak_on_first_run(j):
    j._set("peak_base", "100.0")                            # journal from before this change
    j.record_fill("BBB", "AUD", "BUY", 4, 5.0, commission=0.5, fx_to_base=1.0)
    assert j.portfolio_state({"BBB:AUD": 50.0}).peak_value_base == pytest.approx(100.0)


def test_avg_costs_include_commission_and_shrink_with_partial_sells(j):
    j.record_fill("AAA", "USD", "BUY", 2, 10.0, commission=1.0, fx_to_base=1.5)    # cost (20+1)*1.5 = 31.5 for 2
    j.record_fill("AAA", "USD", "BUY", 2, 20.0, commission=1.0, fx_to_base=1.5)    # cost (40+1)*1.5 = 61.5 for 2
    assert j.avg_costs() == {"AAA:USD": pytest.approx((31.5 + 61.5) / 4)}
    j.record_fill("AAA", "USD", "SELL", 1, 30.0, commission=1.0, fx_to_base=1.5)
    assert j.avg_costs() == {"AAA:USD": pytest.approx((31.5 + 61.5) / 4)}          # average unchanged by a sell
    j.record_fill("AAA", "USD", "SELL", 3, 30.0, commission=1.0, fx_to_base=1.5)
    assert j.avg_costs() == {}                                                     # closed position drops out


def _run_started(j, minutes_ago):
    from datetime import datetime, timedelta

    from guardrail_trader.journal import TZ
    rid = j.start_run("paper")
    ts = (datetime.now(TZ) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    with j.db:
        j.db.execute("UPDATE runs SET started_at=? WHERE id=?", (ts, rid))
    return rid


def test_abort_stale_runs_closes_only_old_running_rows(j):
    old = _run_started(j, 120)          # died mid-run two hours ago
    fresh = _run_started(j, 2)          # legitimately running right now
    done = _run_started(j, 300)
    j.finish_run(done, "ok")            # old but finished: must be left alone
    assert j.abort_stale_runs() == [old]
    st = {r[0]: r[1] for r in j.db.execute("SELECT id, status FROM runs")}
    assert st == {old: "aborted", fresh: "running", done: "ok"}
    assert j.db.execute("SELECT finished_at FROM runs WHERE id=?", (old,)).fetchone()[0] is not None


def test_abort_stale_runs_is_idempotent(j):
    _run_started(j, 120)
    assert len(j.abort_stale_runs()) == 1
    assert j.abort_stale_runs() == []


def test_topup_deposits_the_difference_exactly_once(j):
    assert j.topup_to(250.0, "AUD") == 150.0                 # fixture deposited 100
    assert j.cash_base() == pytest.approx(250.0) and j.total_deposits() == pytest.approx(250.0)
    assert [a for _, a in j.deposits()] == [100.0, 150.0]
    assert j.topup_to(250.0, "aud") == 0.0                   # same target again: nothing, and currency is case-blind
    assert len(j.deposits()) == 2


def test_topup_refuses_withdrawals_wrong_currency_and_an_uninitialised_journal(j, tmp_path):
    with pytest.raises(ValueError, match="append-only"):
        j.topup_to(50.0, "AUD")
    with pytest.raises(ValueError, match="base currency"):
        j.topup_to(500.0, "USD")
    assert j.total_deposits() == 100.0                       # nothing slipped through
    empty = Journal(tmp_path / "empty.sqlite")
    with pytest.raises(RuntimeError, match="not initialised"):
        empty.topup_to(500.0, "AUD")
    empty.close()


def test_topup_is_not_a_drawdown_and_cannot_inflate_the_peak_in_one_tick(j):
    j.portfolio_state({})                                    # run 1: value 100 (all cash)
    j.topup_to(200.0, "AUD")
    first = j.portfolio_state({})                            # run 2: value 200, previous 100
    assert first.peak_value_base == 100.0 and first.drawdown_pct == 0.0
    assert j.portfolio_state({}).peak_value_base == 200.0    # run 3 confirms 200


# -- EUR budget, Paris calendar month (CHANGELOG 12/10/2026) ---------------------------------
def test_eur_budget(tmp_path):
    j = Journal(tmp_path / "eur.sqlite")
    j.init_budget(800_000.0, "eur")
    assert j.base_currency() == "EUR" and j.cash_base() == 800_000.0 and j.peak() == 800_000.0
    j.record_fill("SAP", "EUR", "BUY", 10, 200.0, commission=3.0, fx_to_base=1.0)
    j.record_fill("AAPL", "USD", "BUY", 10, 250.0, commission=1.0, fx_to_base=0.86)
    assert j.cash_base() == pytest.approx(800_000.0 - 2003.0 - 2501.0 * 0.86)


def test_orders_are_counted_by_paris_calendar_month(j):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    for ts in ("2026-10-31T23:59:00+01:00",          # Paris, last minute of October
               "2026-11-01T00:01:00+01:00",          # Paris, first minute of November
               "2026-11-02T10:00:00+01:00"):
        j.db.execute("INSERT INTO orders(ts, symbol) VALUES(?, 'X')", (ts,))
    paris = ZoneInfo("Europe/Paris")
    assert j.orders_this_month(datetime(2026, 10, 31, 12, 0, tzinfo=paris)) == 1
    assert j.orders_this_month(datetime(2026, 11, 1, 0, 30, tzinfo=paris)) == 2
    # 23:30 UTC on 31 Oct is already November in Paris; 22:30 UTC is still October
    assert j.orders_this_month(datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)) == 2
    assert j.orders_this_month(datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc)) == 1


# -- re-entry cooldown after a stop (T3; closed_trades arrives with T5) ----------------------
def test_add_business_days_skips_weekends():
    from datetime import date

    from guardrail_trader.journal import add_business_days
    assert add_business_days(date(2026, 10, 9), 1) == date(2026, 10, 12)     # Fri + 1 = Mon
    assert add_business_days(date(2026, 10, 12), 10) == date(2026, 10, 26)   # Mon + 10 = Mon two weeks on


def test_recently_stopped_without_stop_exits_is_empty(j):
    assert j.recently_stopped(10) == {}


def test_recently_stopped_lists_stops_inside_the_cooldown(j):
    from datetime import date
    j.db.executemany("INSERT INTO closed_trades(symbol,currency,exit_ts,motif,quantity,exit_price) "
                     "VALUES (?,?,?,?,1,1)", [
        ("AAA", "USD", "2026-10-12T16:00:00+02:00", "stop"),
        ("BBB", "EUR", "2026-10-12T16:00:00+02:00", "objectif"),     # not a stop
        ("CCC", "EUR", "2026-09-01T10:00:00+02:00", "stop"),         # cooldown long over
    ])
    assert j.recently_stopped(10, today=date(2026, 10, 20)) == {"AAA:USD": "2026-10-26"}
    assert j.recently_stopped(10, today=date(2026, 10, 27)) == {}
    assert j.recently_stopped(0, today=date(2026, 10, 20)) == {}
    s = j.portfolio_state({}, cooldown_days=10)
    assert isinstance(s.recently_stopped, dict)
