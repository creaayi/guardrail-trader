"""Every rule in the risk gate, tested without a broker."""
from pathlib import Path

import pytest

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
    assert (c.max_position_pct, c.max_trades_per_month, c.max_drawdown_pct) == (25.0, 10, 25.0)
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
