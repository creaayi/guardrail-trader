"""Every rule in the risk gate, tested without a broker."""
from pathlib import Path

import pytest

from guardrail_trader.config import PROJECT_ROOT
from guardrail_trader.risk import (
    Holding, Instrument, MarketInfo, PortfolioState, Proposal, RiskConfig,
    evaluate, kill_switch_triggered, liquidation_proposals, load_risk_config,
)

UNIVERSE = {i.key: i for i in [Instrument("AAA", "SMART", "USD"), Instrument("BBB", "ASX", "AUD")]}


def cfg(**kw) -> RiskConfig:
    base = dict(capital=100.0, base_currency="AUD", max_position_pct=25.0, max_trades_per_month=10,
                max_drawdown_pct=25.0, max_price_deviation_pct=2.0, max_commission_pct=10.0,
                allow_fractional=False, universe=UNIVERSE)
    base.update(kw)
    return RiskConfig(**base)


# AAA: US$10, 1 USD = 1.5 AUD, commission A$1.50.  BBB: A$5, commission A$0.50
MKT = {"AAA:USD": MarketInfo(10.0, 1.5, 1.5), "BBB:AUD": MarketInfo(5.0, 1.0, 0.5)}


def buy(sym="BBB", qty=4, px=5.0, ex="ASX", ccy="AUD"):
    return Proposal(sym, ex, ccy, "BUY", qty, px, "test")


def sell(sym="BBB", qty=1, px=5.0, ex="ASX", ccy="AUD"):
    return Proposal(sym, ex, ccy, "SELL", qty, px, "test")


def one(p, state=None, c=None, mkt=MKT):
    state = state or PortfolioState(cash_base=100.0, peak_value_base=100.0)
    (d,), _ = evaluate([p], state, mkt, c or cfg())
    return d


def test_valid_buy_is_approved():
    d = one(buy(qty=4))  # 4 x A$5 = A$20 = 20% of A$100
    assert d.approved, d.reasons


def test_not_on_allowlist_blocked():
    d = one(buy(sym="ZZZ"))
    assert not d.approved and "allowlist" in d.reasons[0]


def test_wrong_exchange_blocked():
    assert not one(buy(ex="SMART")).approved


def test_position_limit_blocked():
    d = one(buy(qty=6))  # A$30 = 30% > 25%
    assert not d.approved and any("position would be" in r for r in d.reasons)


def test_no_margin():
    state = PortfolioState(cash_base=10.0, holdings={"AAA:USD": Holding(6, 15.0)}, peak_value_base=100.0)
    d = one(buy(qty=4), state)  # needs A$20.50, has A$10
    assert not d.approved and any("no margin" in r for r in d.reasons)


def test_no_shorting():
    d = one(sell(qty=1))
    assert not d.approved and any("no shorting" in r for r in d.reasons)


def test_sell_of_held_shares_approved():
    state = PortfolioState(cash_base=80.0, holdings={"BBB:AUD": Holding(4, 5.0)}, peak_value_base=100.0)
    assert one(sell(qty=4), state).approved


def test_fat_finger_price_blocked():
    d = one(buy(qty=4, px=5.5))  # 10% above reference
    assert not d.approved and any("from price" in r for r in d.reasons)


def test_market_orders_rejected():
    assert not one(buy(px=0)).approved


def test_fractional_blocked_by_default():
    assert not one(buy(qty=1.5)).approved


def test_monthly_trade_limit():
    state = PortfolioState(cash_base=100.0, peak_value_base=100.0, trades_this_month=10)
    d = one(buy(qty=1), state)
    assert not d.approved and any("monthly" in r for r in d.reasons)


def test_commission_cap():
    d = one(buy(qty=1))  # A$0.50 commission on A$5 = 10% -> exactly at limit, allowed
    assert d.approved
    d = one(buy(qty=1), c=cfg(max_commission_pct=5.0))
    assert not d.approved and any("commission" in r for r in d.reasons)


def test_fx_conversion_used_for_us_stocks():
    d = one(Proposal("AAA", "SMART", "USD", "BUY", 1, 10.0))  # US$10 -> A$15 = 15%
    assert d.approved and d.notional_base == pytest.approx(15.0)
    d = one(Proposal("AAA", "SMART", "USD", "BUY", 2, 10.0))  # A$30 = 30%
    assert not d.approved


def test_batch_cannot_jointly_exceed_limits():
    # each buy alone is 20%, together 40% of one instrument
    ds, _ = evaluate([buy(qty=4), buy(qty=4)], PortfolioState(100.0, peak_value_base=100.0), MKT, cfg())
    assert [d.approved for d in ds] == [True, False]


def test_sells_evaluated_before_buys():
    state = PortfolioState(cash_base=0.0, holdings={"AAA:USD": Holding(1, 15.0)}, peak_value_base=15.0)
    c = cfg(max_position_pct=100.0)
    ds, _ = evaluate([buy(qty=2), Proposal("AAA", "SMART", "USD", "SELL", 1, 10.0)], state, MKT, c)
    assert all(d.approved for d in ds), [d.reasons for d in ds]


def test_kill_switch_blocks_everything():
    state = PortfolioState(cash_base=75.0, peak_value_base=100.0)  # 25% drawdown
    assert kill_switch_triggered(state, cfg())
    assert not one(buy(qty=1), state).approved


def test_halted_blocks_everything():
    state = PortfolioState(cash_base=100.0, peak_value_base=100.0, halted=True)
    d = one(buy(qty=1), state)
    assert not d.approved and "halted" in d.reasons[0]


def test_liquidation_sells_all_holdings():
    state = PortfolioState(cash_base=10.0, holdings={"BBB:AUD": Holding(4, 5.0), "AAA:USD": Holding(2, 15.0)},
                           peak_value_base=100.0)
    props = liquidation_proposals(state, MKT, cfg())
    assert {(p.symbol, p.action, p.quantity) for p in props} == {("BBB", "SELL", 4), ("AAA", "SELL", 2)}
    assert all(p.limit_price < MKT[p.key].price for p in props)


def test_input_state_not_mutated():
    state = PortfolioState(cash_base=100.0, peak_value_base=100.0)
    evaluate([buy(qty=4)], state, MKT, cfg())
    assert state.cash_base == 100.0 and not state.holdings


def test_shipped_config_loads_and_is_fail_closed():
    c = load_risk_config()
    assert (c.capital, c.base_currency) == (950000.0, "EUR")      # CHANGELOG 13/10/2026
    assert (c.max_position_pct, c.max_trades_per_month, c.max_drawdown_pct) == (2.0, 60, 15.0)
    assert (c.drawdown_resume_pct, c.drawdown_pause_pct, c.drawdown_pause_days, c.drawdown_reduce_pct,
            c.reduce_max_equity_pct, c.daily_loss_halt_pct) == (4.0, 8.0, 5, 12.0, 50.0, 3.0)
    assert (c.max_price_deviation_pct, c.max_commission_pct) == (0.5, 1.0)
    assert (c.min_position_pct, c.max_sector_pct, c.max_us_pct, c.max_equity_pct, c.reentry_cooldown_days) == \
        (0.5, 20.0, 70.0, 90.0, 10)
    assert c.enable_hedge is False and "IWDA:EUR" in c.hedge and not set(c.hedge) & set(c.universe)


def test_invalid_config_refuses_to_load(tmp_path: Path):
    bad = tmp_path / "risk.toml"
    bad.write_text('[budget]\ncapital=100\ncurrency="AUD"\n[limits]\nmax_position_pct=150\n'
                   'max_trades_per_month=10\nmax_drawdown_pct=25\nmax_price_deviation_pct=2\nmax_commission_pct=10\n')
    with pytest.raises(ValueError):
        load_risk_config(bad)


@pytest.mark.parametrize("qty,px", [(float("inf"), 5.0), (float("nan"), 5.0), (4, float("inf")), (4, float("nan"))])
def test_non_finite_numbers_blocked_not_crashed(qty, px):
    d = one(buy(qty=qty, px=px))  # e.g. a model sending "inf" as a quantity
    assert not d.approved and d.reasons


# ------------------------------------------------------------------ T3 rules (CLAUDE.md §4)
T3_UNI = {i.key: i for i in [
    Instrument("AAA", "SMART", "USD", "Aaa", "Technology", "US"),
    Instrument("BBB", "SMART", "USD", "Bbb", "Technology", "US"),
    Instrument("CCC", "IBIS", "EUR", "Ccc", "Technology", "DE"),
    Instrument("DDD", "SBF", "EUR", "Ddd", "Energy", "FR"),
    Instrument("EEE", "SMART", "USD", "Eee", "Energy", "US"),
]}
T3_MKT = {k: MarketInfo(10.0, 1.0, 0.0) for k in T3_UNI}   # every price 10 in base currency, no commission


def t3cfg(**kw):
    base = dict(universe=T3_UNI, max_position_pct=100.0, min_position_pct=0.0, max_sector_pct=100.0,
                max_us_pct=100.0, max_equity_pct=100.0, reentry_cooldown_days=10)
    base.update(kw)
    return cfg(**base)


def t3buy(sym, qty, ex=None):
    i = next(i for i in T3_UNI.values() if i.symbol == sym)
    return Proposal(sym, ex or i.exchange, i.currency, "BUY", qty, 10.0, "t3")


def t3(props, c, state=None):
    state = state or PortfolioState(cash_base=1000.0, peak_value_base=1000.0)
    ds, _ = evaluate(props, state, T3_MKT, c)
    return ds


def test_min_position_size_at_entry():
    (d,) = t3([t3buy("DDD", 4)], t3cfg(min_position_pct=0.5))            # 40 / 1000 = 4% ok
    assert d.approved
    (d,) = t3([t3buy("DDD", 4)], t3cfg(min_position_pct=5.0))            # 4% < 5%
    assert not d.approved and any("min 5.0%" in r for r in d.reasons)


def test_partial_sell_cannot_leave_a_line_below_the_minimum_but_a_full_sell_can():
    state = PortfolioState(cash_base=900.0, holdings={"DDD:EUR": Holding(10, 10.0)}, peak_value_base=1000.0)
    c = t3cfg(min_position_pct=5.0)
    sell_ddd = lambda q: Proposal("DDD", "SBF", "EUR", "SELL", q, 10.0)  # noqa: E731
    (d,) = t3([sell_ddd(6)], c, state)                                   # leaves 4% < 5%
    assert not d.approved and any("leave" in r for r in d.reasons)
    assert t3([sell_ddd(10)], c, state)[0].approved                       # full sale
    assert t3([sell_ddd(5)], c, state)[0].approved                        # leaves 5%


def test_sector_cap():
    state = PortfolioState(cash_base=850.0, holdings={"AAA:USD": Holding(15, 10.0)}, peak_value_base=1000.0)
    (d,) = t3([t3buy("CCC", 6)], t3cfg(max_sector_pct=20.0), state)       # Technology 150 + 60 = 21%
    assert not d.approved and any("sector Technology" in r for r in d.reasons)
    assert t3([t3buy("CCC", 5)], t3cfg(max_sector_pct=20.0), state)[0].approved   # exactly 20%
    assert t3([t3buy("DDD", 6)], t3cfg(max_sector_pct=20.0), state)[0].approved   # other sector


def test_burst_that_breaks_a_sector_together_but_not_separately():
    c = t3cfg(max_sector_pct=20.0)
    burst = [t3buy("AAA", 10), t3buy("BBB", 10), t3buy("CCC", 10)]        # 10% each, all Technology
    for p in burst:
        assert t3([p], c)[0].approved                                     # alone: fine
    assert [d.approved for d in t3(burst, c)] == [True, True, False]      # together: the third breaks 20%


def test_us_cap_counts_us_country_only():
    state = PortfolioState(cash_base=300.0, holdings={"AAA:USD": Holding(70, 10.0)}, peak_value_base=1000.0)
    c = t3cfg(max_us_pct=70.0)
    (d,) = t3([t3buy("EEE", 1)], c, state)
    assert not d.approved and any("US holdings" in r for r in d.reasons)
    assert t3([t3buy("DDD", 10)], c, state)[0].approved                   # FR stock: not US


def test_equity_cap_keeps_cash():
    state = PortfolioState(cash_base=150.0, holdings={"DDD:EUR": Holding(85, 10.0)}, peak_value_base=1000.0)
    c = t3cfg(max_equity_pct=90.0)
    assert t3([t3buy("CCC", 5)], c, state)[0].approved                    # 90%
    (d,) = t3([t3buy("CCC", 6)], c, state)                                # 91%
    assert not d.approved and any("equity exposure" in r for r in d.reasons)


def test_reentry_cooldown_after_a_stop():
    state = PortfolioState(cash_base=1000.0, peak_value_base=1000.0, recently_stopped={"DDD:EUR": "2026-10-26"})
    (d,) = t3([t3buy("DDD", 2)], t3cfg(), state)
    assert not d.approved and any("2026-10-26" in r for r in d.reasons)
    assert t3([t3buy("CCC", 2)], t3cfg(), state)[0].approved


def test_sells_are_never_blocked_by_exposure_caps():
    state = PortfolioState(cash_base=0.0, holdings={"AAA:USD": Holding(100, 10.0)}, peak_value_base=1000.0,
                           recently_stopped={"AAA:USD": "2026-10-26"})
    c = t3cfg(max_sector_pct=20.0, max_us_pct=70.0, max_equity_pct=90.0)
    assert t3([Proposal("AAA", "SMART", "USD", "SELL", 50, 10.0)], c, state)[0].approved


@pytest.mark.parametrize("key,value", [
    ("min_position_pct", "2.0"),          # not below max_position_pct (2.0 in this file)
    ("min_position_pct", "0"),
    ("max_sector_pct", "120"),
    ("max_us_pct", "-1"),
    ("max_equity_pct", "0"),
    ("reentry_cooldown_days", "-1"),
    ("reentry_cooldown_days", "1.5"),
])
def test_t3_keys_are_validated(tmp_path: Path, key, value):
    text = (PROJECT_ROOT / "config" / "risk.toml").read_text()
    import re
    text = re.sub(rf"(?m)^{key} = [^#\n]+", f"{key} = {value} ", text)
    p = tmp_path / "risk.toml"
    p.write_text(text.replace('"config/universe/', f'"{PROJECT_ROOT}/config/universe/'))
    with pytest.raises(ValueError):
        load_risk_config(p)


@pytest.mark.parametrize("key", ["min_position_pct", "max_sector_pct", "max_us_pct", "max_equity_pct",
                                 "reentry_cooldown_days"])
def test_t3_keys_are_required(tmp_path: Path, key):
    import re
    text = re.sub(rf"(?m)^{key} = .*\n", "", (PROJECT_ROOT / "config" / "risk.toml").read_text())
    p = tmp_path / "risk.toml"
    p.write_text(text.replace('"config/universe/', f'"{PROJECT_ROOT}/config/universe/'))
    with pytest.raises(KeyError):
        load_risk_config(p)


def test_partial_sell_leaving_0_3_pct_is_rejected_full_sell_is_accepted():
    # 1% line (10 x 10) in a 10,000 portfolio; min 0.5%
    state = PortfolioState(cash_base=9900.0, holdings={"DDD:EUR": Holding(10, 10.0)}, peak_value_base=10000.0)
    c = t3cfg(min_position_pct=0.5)
    (d,) = t3([Proposal("DDD", "SBF", "EUR", "SELL", 7, 10.0)], c, state)     # leaves 3 x 10 = 0.3%
    assert not d.approved and any("0.30% line" in r for r in d.reasons)
    assert t3([Proposal("DDD", "SBF", "EUR", "SELL", 10, 10.0)], c, state)[0].approved
