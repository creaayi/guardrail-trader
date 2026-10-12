"""Scenario evals: the real pipeline (run_once) against a simulated broker, market and Claude.

Each scenario states the invariant it checks. The harness passes when the invariant holds no matter
what the market, the broker or the model does. Deterministic; runs in CI with the unit tests.
"""
from types import SimpleNamespace as NS

import pytest

from evals.sim import PRICES, Bot, ScriptedClaude, SimMarket, order, submits

HELD = {"AAA:USD": (8, 100.0), "BBB:USD": (20, 50.0)}       # ~A$2,700 of A$5,000 invested
CRASH = {"AAA:USD": 40.0, "BBB:USD": 20.0, "CCC:USD": 20.0}  # -60%: drawdown ~32%, over the 25% kill switch


# ------------------------------------------------------------------ the model misbehaves
def test_limit_breaking_orders_never_reach_the_broker(tmp_path):
    """Invariant: only gate-approved orders reach the broker, whatever the model submits."""
    bad = [order("ZZZ", 1),                          # not on the allowlist
           order("AAA", 20),                         # 20 x A$150 = 60% of portfolio (> 25%)
           order("BBB", 5, px=0),                    # market order / no limit price
           order("CCC", 5, action="SELL"),           # short sale (nothing held)
           order("CCC", 1.5),                        # fractional
           order("BBB", 5, px=60.0),                 # limit 20% from price (fat finger)
           order("BBB", float("inf")),               # non-finite quantity
           order("CCC", 10)]                         # the one valid order
    bot = Bot(tmp_path)
    out = bot.run(submits(*bad))
    assert out.rc == 0 and out.status == "ok"
    assert [(o.symbol, o.qty) for o in bot.broker.orders.values()] == [("CCC", 10)]
    assert bot.reconciled()


def test_blocked_submit_is_bounced_once_then_valid_orders_go_ahead(tmp_path):
    """Invariant: the model gets the gate's reasons once, and the loop still terminates."""
    client = ScriptedClaude([[("submit_orders", {"orders": [order("AAA", 20)], "summary": "too big"})],
                             [("submit_orders", {"orders": [order("AAA", 5)], "summary": "resized"})]])
    bot = Bot(tmp_path)
    out = bot.run(client)
    assert out.llm_calls == 2
    assert [(o.symbol, o.qty) for o in bot.broker.orders.values()] == [("AAA", 5)]


def test_monthly_order_limit_holds(tmp_path):
    """Invariant: no more than max_trades_per_month orders reach the broker in a month."""
    bot = Bot(tmp_path)
    for _ in range(10):
        bot.run(submits(order("CCC", 1)))
    out = bot.run(submits(order("CCC", 1)))
    assert len(bot.broker.orders) == 10


def test_order_limit_used_up_means_no_model_call(tmp_path):
    """Invariant: once the monthly order limit is used, Claude isn't called (no cost for nothing)."""
    bot = Bot(tmp_path)
    for _ in range(10):
        bot.run(submits(order("CCC", 1)))
    out = bot.run(submits(order("CCC", 1)))
    assert out.status == "order_limit_reached" and out.llm_calls == 0 and len(bot.broker.orders) == 10


def test_batch_that_crosses_the_limit_is_cut_at_the_limit(tmp_path):
    """Invariant: with 9 used, a 3-order batch sends only 1."""
    bot = Bot(tmp_path)
    for _ in range(9):
        bot.run(submits(order("CCC", 1)))
    out = bot.run(submits(order("CCC", 1), order("BBB", 1), order("AAA", 1)))
    assert out.llm_calls > 0 and len(bot.broker.orders) == 10


def test_kill_switch_still_liquidates_with_the_order_limit_used_up(tmp_path):
    """Invariant: the monthly order limit never blocks an emergency exit."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()                                            # establishes the peak (and the last value)
    bot.run()                                            # confirms it
    for _ in range(10):
        bot.run(submits(order("CCC", 1)))
    assert bot.j.orders_this_month() == 10
    out = bot.run(market=SimMarket({**CRASH, "CCC:USD": 20.0}))
    assert out.status == "kill_switch" and bot.j.holdings_qty() == {} and bot.reconciled()


def test_model_can_sell_a_broken_holding_to_fund_a_better_one(tmp_path):
    """Invariant: a model-initiated SELL (no kill switch involved) passes the gate, fills, reconciles,
    frees the cash for the buy in the same submit, and Claude sees cost basis and P/L when deciding."""
    bot = Bot(tmp_path, holdings=HELD)
    market = SimMarket({**PRICES, "BBB:USD": 45.0})                      # BBB -10% vs its A$ cost of 50
    out = bot.run(submits(order("BBB", 20, "SELL", px=45.0), order("CCC", 30, "BUY")), market)
    assert out.status == "ok" and bot.reconciled()
    assert bot.j.holdings_qty() == {"AAA:USD": 8, "CCC:USD": 30}
    assert bot.j.orders_this_month() == 2
    transcript = (tmp_path / "runs" / f"run_{bot.j.db.execute('SELECT MAX(id) FROM runs').fetchone()[0]}.json").read_text()
    assert "unrealized_pnl" in transcript and "avg_cost" in transcript


def test_run_orphaned_by_a_dead_process_is_closed_by_the_next_run(tmp_path):
    """Invariant: a run killed mid-flight (container restart) never stays 'running' forever, and the
    next run still reconciles and proceeds normally."""
    from datetime import datetime, timedelta

    from guardrail_trader.journal import TZ
    bot = Bot(tmp_path, holdings=HELD)
    orphan = bot.j.start_run("paper")                       # the process died before finish_run
    with bot.j.db:
        bot.j.db.execute("UPDATE runs SET started_at=? WHERE id=?",
                         ((datetime.now(TZ) - timedelta(hours=3)).isoformat(timespec="seconds"), orphan))
    out = bot.run()
    assert out.status == "ok" and bot.reconciled()
    status = bot.j.db.execute("SELECT status FROM runs WHERE id=?", (orphan,)).fetchone()[0]
    assert status == "aborted"
    assert not bot.j.db.execute("SELECT 1 FROM runs WHERE status='running'").fetchall()


def test_budget_topup_mid_life_raises_the_cap_without_a_false_kill_switch(tmp_path):
    """Invariant: depositing more virtual cash never reads as a drawdown, cannot halt the bot, and the
    per-holding cap scales with the larger portfolio so the new cash can actually be used."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()
    before = bot.j.cash_base()
    assert bot.j.topup_to(10000.0, "AUD") == 5000.0 and bot.j.cash_base() == pytest.approx(before + 5000.0)
    bot.broker.net_liquidation = 10000.0             # the account must back the larger budget (account guard)
    # 60 x CCC = A$1,800: over the old 25% cap (~A$1,250 of 5,000) but under the new one (~A$2,500 of 10,000).
    out = bot.run(submits(order("CCC", 60)))
    assert out.status == "ok" and not bot.j.is_halted()
    assert bot.j.holdings_qty().get("CCC:USD") == 60 and bot.reconciled()
    assert bot.run().status == "ok" and bot.j.peak() >= 9000.0      # the peak catches up after two runs


def test_model_that_never_submits_trades_nothing(tmp_path):
    """Invariant: a model looping on tools forever stops at MAX_TURNS with no trades."""
    bot = Bot(tmp_path)
    out = bot.run(ScriptedClaude([[("get_portfolio", {})]]))
    assert out.llm_calls == 20 and bot.broker.orders == {} and out.status == "ok"


def test_run_cost_budget_stops_the_loop(tmp_path):
    """Invariant: one expensive turn over the per-run budget ends the run with no trades."""
    bot = Bot(tmp_path)
    out = bot.run(submits(order("CCC", 1)), cost_fn=lambda _m, _u: 0.20, run_budget_usd=0.10)
    assert out.llm_calls == 1 and bot.broker.orders == {}


def test_loop_time_budget_stops_the_loop(tmp_path):
    bot = Bot(tmp_path)
    out = bot.run(submits(order("CCC", 1)), max_seconds=0)
    assert out.llm_calls == 0 and bot.broker.orders == {}


def test_monthly_llm_budget_exhausted_means_no_model_call(tmp_path):
    bot = Bot(tmp_path)
    run = bot.j.start_run("paper-eval")
    bot.j.record_llm_usage(run, "eval", "claude-sonnet-5", NS(input_tokens=1, output_tokens=1), 2.50)
    out = bot.run(submits(order("CCC", 1)), cost_fn=lambda _m, _u: 0.0)
    assert out.status == "llm_budget_exhausted" and out.llm_calls == 0 and bot.broker.orders == {}


def test_market_closed_means_no_model_call(tmp_path):
    bot = Bot(tmp_path)
    out = bot.run(submits(order("CCC", 1)), SimMarket(open_=False))
    assert out.status == "market_closed" and out.llm_calls == 0


# ------------------------------------------------------------------ the market misbehaves
def test_flash_crash_liquidates_and_halts_without_asking_the_model(tmp_path):
    """Invariant: at >= 25% drawdown, code sells everything and halts; Claude is not consulted."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()                                            # establishes the peak (~A$5,000)
    out = bot.run(submits(order("AAA", 1)), SimMarket(CRASH))
    assert out.status == "kill_switch" and out.llm_calls == 0
    assert bot.j.is_halted() and bot.j.holdings_qty() == {} and bot.reconciled()
    assert all(o.action == "SELL" for o in bot.broker.orders.values())
    later = bot.run(submits(order("CCC", 1)))            # stays halted until a human resets it
    assert later.status == "halted" and later.llm_calls == 0


def test_kill_switch_outside_market_hours_liquidates_at_next_open(tmp_path):
    """Invariant: holdings are sold at the first open-market run after the kill switch fires."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()
    closed = bot.run(market=SimMarket(CRASH, open_=False))
    assert closed.status == "kill_switch" and bot.broker.orders == {}      # can't sell while closed
    bot.run(market=SimMarket(CRASH, open_=True))
    assert bot.j.holdings_qty() == {} and bot.reconciled()


def test_missing_price_for_a_holding_refuses_to_value(tmp_path):
    """Invariant: no valuation (so no kill-switch or trade decision) on incomplete prices."""
    bot = Bot(tmp_path, holdings=HELD)
    prices = {k: v for k, v in PRICES.items() if k != "AAA:USD"}
    out = bot.run(submits(order("CCC", 1)), SimMarket(prices))
    assert out.rc == 1 and out.llm_calls == 0 and bot.broker.orders == {}


@pytest.mark.parametrize("bad", [0.0, float("nan"), -5.0])
def test_corrupt_candidate_price_blocks_orders_for_it(tmp_path, bad):
    """Invariant: no order is sized against a zero, negative or NaN reference price."""
    bot = Bot(tmp_path)
    out = bot.run(submits(order("CCC", 5, px=20.0)), SimMarket({**PRICES, "CCC:USD": bad}))
    assert bot.broker.orders == {} and out.status == "ok"


def test_one_off_price_spike_does_not_trigger_the_kill_switch(tmp_path):
    """Invariant: a single corrupt quote must not cause a liquidation later (peak needs 2 runs)."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()
    bot.run(market=SimMarket({**PRICES, "AAA:USD": 1000.0}, open_=False))   # bad tick: 10x
    out = bot.run()                                                          # prices back to normal
    assert out.status != "kill_switch" and bot.j.holdings_qty() != {}


def test_real_rise_raises_the_peak_one_run_later(tmp_path):
    """Invariant: genuine gains are protected by the kill switch, one run after they appear."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()
    up = {**PRICES, "AAA:USD": 200.0, "BBB:USD": 100.0}                     # value ~A$7,700
    bot.run(market=SimMarket(up, open_=False))
    bot.run(market=SimMarket(up, open_=False))                               # seen twice -> peak
    assert bot.j.peak() > 7000
    out = bot.run(market=SimMarket({**up, "AAA:USD": 60.0, "BBB:USD": 25.0}))   # then a real crash
    assert out.status == "kill_switch"


@pytest.mark.xfail(strict=True, reason="KNOWN GAP: a one-off DOWNWARD bad tick (positive but far too low) "
                                       "fires the kill switch immediately; confirming a crash over 2 runs would "
                                       "delay real crash protection (owner decision).")
def test_one_off_downward_bad_tick_does_not_liquidate(tmp_path):
    bot = Bot(tmp_path, holdings=HELD)
    bot.run()
    out = bot.run(market=SimMarket({**PRICES, "AAA:USD": 1.0, "BBB:USD": 0.5}))   # 1/100th: bad data
    assert out.status != "kill_switch"


# ------------------------------------------------------------------ the broker misbehaves
def test_broker_disconnect_before_trading_is_an_error_with_no_orders(tmp_path):
    bot = Bot(tmp_path)
    bot.broker.fail_positions = ConnectionError("simulated: gateway down")
    out = bot.run(submits(order("CCC", 1)))
    assert out.error is not None and out.status == "error" and bot.broker.orders == {} and out.llm_calls == 0


def test_disconnect_mid_execution_is_caught_before_the_next_trade(tmp_path):
    """Invariant: if orders reach IBKR but aren't booked, no further trading until a human looks."""
    bot = Bot(tmp_path)
    bot.broker.fail_place_after = 1                     # first order placed (and fills), second fails
    out = bot.run(submits(order("CCC", 5), order("BBB", 5)))
    assert out.status == "error"
    bot.broker.fail_place_after = None
    nxt = bot.run(submits(order("CCC", 1)))
    assert nxt.rc == 3 and nxt.status == "reconcile_failed" and nxt.llm_calls == 0


def test_partial_fill_books_only_what_filled(tmp_path):
    bot = Bot(tmp_path)
    bot.broker.fill = lambda o: (2.0, 0.0)              # 2 of 10 fill, rest cancelled
    bot.run(submits(order("CCC", 10)))
    assert bot.j.holdings_qty() == {"CCC:USD": 2.0} and bot.reconciled()
    assert bot.run().status == "ok"                     # next run reconciles cleanly


def test_late_fill_after_cancel_stops_the_next_run(tmp_path):
    """Invariant: a fill the bot never saw is caught by reconciliation, not traded on top of."""
    bot = Bot(tmp_path)
    bot.broker.fill = lambda o: (2.0, 3.0)              # 2 fill now, 3 more after our cancel
    bot.run(submits(order("CCC", 10)))
    assert not bot.reconciled()
    nxt = bot.run(submits(order("CCC", 1)))
    assert nxt.rc == 3 and nxt.llm_calls == 0


def test_gateway_that_serves_no_data_is_retryable_not_a_reconcile_failure(tmp_path):
    """Invariant: timed-out requests (empty positions) never read as an emptied account.

    Seen live 23 Sep 2026: every IBKR request timed out during the nightly re-login, positions
    came back empty, and a healthy six-holding portfolio was reported as a mismatch.
    """
    bot = Bot(tmp_path, holdings=HELD)
    bot.broker.not_ready = True
    bot.broker.holdings = {}                       # what a timed-out positions request looks like
    out = bot.run(submits(order("CCC", 1)))
    assert out.rc == 2 and out.status == "broker_not_ready"     # rc 2 = the scheduler retries
    assert out.llm_calls == 0 and bot.broker.orders == {}
    assert bot.j.holdings_qty() != {}                           # journal untouched


def test_all_cash_portfolio_does_not_trade_on_a_dead_gateway(tmp_path):
    """The dangerous case: with an empty journal, empty positions would *match* and pass."""
    bot = Bot(tmp_path)                            # no holdings: journal {} == positions {}
    bot.broker.not_ready = True
    out = bot.run(submits(order("CCC", 1)))
    assert out.status == "broker_not_ready" and out.llm_calls == 0 and bot.broker.orders == {}


def test_no_market_data_is_its_own_retryable_status(tmp_path):
    """Invariant: a gateway serving no prices stops the run before Claude, and says why.

    Seen live 24 Sep 2026: after a network outage the gateway came back as a secondary session,
    kept account data but lost its market-data line (IBKR 10197), and six runs died with a
    ValueError stack trace instead of an actionable status.
    """
    from guardrail_trader.broker import MarketDataUnavailableError

    class DeadFeed(SimMarket):
        def fx_rates(self):
            raise MarketDataUnavailableError("simulated: no FX rate (IBKR 10197)")

    bot = Bot(tmp_path, holdings=HELD)
    out = bot.run(submits(order("CCC", 1)), DeadFeed())
    assert out.rc == 2 and out.status == "market_data_unavailable"
    assert out.llm_calls == 0 and bot.broker.orders == {}


def test_empty_price_list_is_treated_as_no_market_data(tmp_path):
    class NoPrices(SimMarket):
        def price_all(self):
            return {}

    bot = Bot(tmp_path)
    out = bot.run(submits(order("CCC", 1)), NoPrices())
    assert out.rc == 2 and out.status == "market_data_unavailable" and out.llm_calls == 0


def test_manual_trade_in_the_account_stops_the_bot(tmp_path):
    bot = Bot(tmp_path)
    bot.broker.holdings["AAA:USD"] = 1                  # someone traded by hand
    out = bot.run(submits(order("CCC", 1)))
    assert out.rc == 3 and out.llm_calls == 0


def test_stray_open_order_stops_the_bot(tmp_path):
    bot = Bot(tmp_path)
    bot.broker.open = [object()]
    out = bot.run(submits(order("CCC", 1)))
    assert out.rc == 3 and out.llm_calls == 0


# ------------------------------------------------------------------ capital guard (CHANGELOG 12/10/2026)
def test_account_smaller_than_the_bots_value_stops_before_claude(tmp_path):
    """Invariant: the bot never trades a virtual budget the broker account can't pay for."""
    bot = Bot(tmp_path)
    bot.broker.net_liquidation = 4999.0              # all cash: the bot is worth 5,000
    out = bot.run(submits(order("CCC", 1)))
    assert out.rc == 3 and out.status == "capital_guard"        # rc 3 = needs a human, no retry
    assert out.llm_calls == 0 and bot.broker.orders == {}


def test_capital_guard_converts_the_account_currency(tmp_path):
    """A USD account is converted with the run's FX before the comparison."""
    bot = Bot(tmp_path)
    bot.broker.net_liquidation, bot.broker.currency = 3400.0, "USD"   # x1.5 = 5,100 >= 5,000
    assert bot.run(submits(order("CCC", 1))).status == "ok"
    bot.broker.net_liquidation = 3300.0                               # x1.5 = 4,950 < 5,000
    assert bot.run(submits(order("CCC", 1))).status == "capital_guard"


def test_capital_guard_never_blocks_the_kill_switch(tmp_path):
    """Invariant: a forced liquidation goes ahead even when the account is below capital."""
    bot = Bot(tmp_path, holdings=HELD)
    bot.broker.net_liquidation = 1.0
    out = bot.run(market=SimMarket(prices=CRASH))
    assert out.status == "kill_switch" and {o.action for o in bot.broker.orders.values()} == {"SELL"}


def test_empty_universe_is_a_quiet_skip_not_a_gateway_failure(tmp_path):
    """Invariant: fail-closed universe = no Claude call, no order, and no retry/restart signal (rc 0)."""
    from dataclasses import replace
    from evals.sim import CFG
    bot = Bot(tmp_path, cfg=replace(CFG, universe={}))
    out = bot.run(submits(order("CCC", 1)))
    assert out.rc == 0 and out.status == "empty_universe" and out.llm_calls == 0 and bot.broker.orders == {}


# ------------------------------------------------------------------ momentum ranking (T2b)
def test_claude_gets_the_code_computed_ranking_and_missing_data_is_flagged(tmp_path):
    """Invariant: the ranking comes from the bar cache; instruments without history are counted, not invented."""
    from datetime import date, datetime, timedelta

    from guardrail_trader.bars import Bar, BarCache
    from guardrail_trader.risk import MomentumConfig

    cache = BarCache(tmp_path / "bars.sqlite")
    last = date(2026, 10, 9)
    closes = [50 + i * 0.2 for i in range(140)]
    cache.upsert("BBB:USD", [Bar((last - timedelta(days=139 - i)).isoformat(), c, c * 1.01, c * 0.99, c, 1e6)
                             for i, c in enumerate(closes)])
    client = submits()
    out = Bot(tmp_path).run(client, bars=cache, momentum_cfg=MomentumConfig(),
                            now=datetime(2026, 10, 12, 11, 0))
    assert out.status == "ok"
    task = client.last_request["messages"][0]["content"][0]["text"]
    assert "Pocket A momentum ranking" in task and '"symbol":"BBB"' in task and '"AAA"' not in task
    assert any("momentum ranking as of 2026-10-09: 1 ranked" in m and "'no cached history': 2" in m for m in out.logs)
    cache.close()


def test_account_guard_follows_the_bots_value_not_the_initial_capital(tmp_path):
    """CHANGELOG 12/10/2026: the bot's own losses lower the account too; that must not stop the bot."""
    bot = Bot(tmp_path, holdings=HELD)                          # ~2,700 invested of 5,000
    down = {"AAA:USD": 90.0, "BBB:USD": 45.0, "CCC:USD": 20.0}  # -10% on holdings: bot ~4,730, no kill switch
    bot.broker.net_liquidation = 4800.0                          # below capital 5,000, above the bot's value
    out = bot.run(market=SimMarket(prices=down))
    assert out.status == "ok", out.logs
    bot.broker.net_liquidation = 4600.0                          # below the bot's value
    assert bot.run(market=SimMarket(prices=down)).status == "capital_guard"


def test_topup_the_account_cannot_back_is_stopped_by_the_account_guard(tmp_path):
    bot = Bot(tmp_path)
    bot.j.topup_to(10000.0, "AUD")                   # ledger says 10,000; the account still holds 5,000
    out = bot.run(submits(order("CCC", 60)))
    assert out.status == "capital_guard" and bot.broker.orders == {}


# ------------------------------------------------------------------ drawdown tiers (T4)
def _tiered():
    from dataclasses import replace
    from evals.sim import CFG
    return replace(CFG, drawdown_pause_pct=8.0, drawdown_pause_days=5, drawdown_reduce_pct=12.0,
                   reduce_max_equity_pct=50.0, drawdown_resume_pct=4.0, daily_loss_halt_pct=3.0)


def test_a_drawdown_past_the_pause_tier_blocks_new_buys(tmp_path):
    """Invariant: at -8% the code, not Claude, stops new entries; the tier is journaled."""
    from datetime import datetime
    bot = Bot(tmp_path, cfg=_tiered(), holdings=HELD)
    down = {"AAA:USD": 80.0, "BBB:USD": 43.0, "CCC:USD": 20.0}   # ~-9% from the 5,000 peak
    out = bot.run(submits(order("CCC", 1)), market=SimMarket(prices=down), now=datetime(2026, 10, 12, 11, 0))
    assert out.status == "ok" and bot.broker.orders == {}
    assert bot.j.tier() == ("PAUSED", "2026-10-19")
    assert any("DRAWDOWN TIER NORMAL -> PAUSED" in m for m in out.logs)
    reasons = bot.j.db.execute("SELECT block_reasons FROM proposals").fetchone()[0]
    assert "drawdown pause" in reasons


def test_a_bad_day_halts_the_next_day_without_calling_claude(tmp_path):
    from datetime import date, datetime
    bot = Bot(tmp_path, cfg=_tiered())
    bot.j.record_nav(5000.0, date(2026, 10, 9))
    bot.j.record_nav(4800.0, date(2026, 10, 12))                  # Monday lost 4%
    out = bot.run(submits(order("CCC", 1)), now=datetime(2026, 10, 13, 11, 0))
    assert out.status == "daily_loss_halt" and out.llm_calls == 0 and bot.broker.orders == {}


def test_the_kill_switch_still_liquidates_on_a_daily_halt_day(tmp_path):
    from datetime import date, datetime
    bot = Bot(tmp_path, cfg=_tiered(), holdings=HELD)
    bot.j.record_nav(5000.0, date(2026, 10, 9))
    bot.j.record_nav(4800.0, date(2026, 10, 12))
    out = bot.run(market=SimMarket(prices=CRASH), now=datetime(2026, 10, 13, 11, 0))
    assert out.status == "kill_switch" and {o.action for o in bot.broker.orders.values()} == {"SELL"}
