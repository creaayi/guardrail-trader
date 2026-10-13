"""T5: protective stops (CLAUDE.md §5) - pure rules, broker adapter, journal."""
import random
from types import SimpleNamespace as NS

import pytest

from guardrail_trader.broker.ibkr import IBKRBroker, round_to_tick
from guardrail_trader.journal import Journal
from guardrail_trader.risk import RiskConfig, load_risk_config
from guardrail_trader.stops import highest_close_since, initial_stop, trailed_stop
from guardrail_trader.bars import Bar

CFG = RiskConfig(1000.0, "EUR", 2.0, 60, 15.0, 0.5, 1.0, False, {}, stop_atr_mult=2.0, stop_min_pct=6.0,
                 stop_max_pct=12.0, breakeven_atr_mult=1.0, trail_atr_mult=2.0)


# ------------------------------------------------------------------ initial stop, bounded [-12%, -6%]
@pytest.mark.parametrize("atr,expected", [
    (4.0, 92.0),      # 100 - 2 x 4 = 92 (-8%): inside the bounds
    (1.0, 94.0),      # 100 - 2 = 98 (-2%) is too tight: -6% bound
    (10.0, 88.0),     # 100 - 20 = 80 (-20%) is too wide: -12% bound
    (3.0, 94.0),      # exactly -6%
    (6.0, 88.0),      # exactly -12%
])
def test_initial_stop_is_bounded(atr, expected):
    price, _ = initial_stop(100.0, atr, CFG)
    assert price == pytest.approx(expected)


def test_initial_stop_without_atr_uses_the_widest_bound_and_says_so():
    price, note = initial_stop(100.0, None, CFG)
    assert price == pytest.approx(88.0) and "unavailable" in note


def test_shipped_stop_config():
    c = load_risk_config()
    assert (c.stop_atr_mult, c.stop_min_pct, c.stop_max_pct, c.breakeven_atr_mult, c.trail_atr_mult) == \
        (2.0, 6.0, 12.0, 1.0, 2.0)


# ------------------------------------------------------------------ trailing: breakeven, trail, never lower
def test_no_move_until_entry_plus_one_atr():
    assert trailed_stop(100.0, 92.0, 103.9, 4.0, CFG) == 92.0


def test_breakeven_at_entry_plus_one_atr():
    assert trailed_stop(100.0, 92.0, 104.0, 4.0, CFG) == 100.0          # 104 - 8 = 96 < entry: entry wins


def test_then_trails_two_atr_below_the_highest_close():
    assert trailed_stop(100.0, 100.0, 115.0, 4.0, CFG) == pytest.approx(107.0)


def test_never_lowered():
    assert trailed_stop(100.0, 107.0, 115.0, 6.0, CFG) == 107.0         # 115 - 12 = 103 < 107
    assert trailed_stop(100.0, 107.0, 115.0, None, CFG) == 107.0


def test_trailing_is_monotonic_on_random_paths():
    rng = random.Random(7)
    for _ in range(200):
        stop, high, px = 92.0, 100.0, 100.0
        for _ in range(120):
            px *= 1 + rng.gauss(0.0005, 0.02)
            high = max(high, px)
            new = trailed_stop(100.0, stop, high, rng.uniform(1.0, 6.0), CFG)
            assert new >= stop
            stop = new


def test_highest_close_since_entry_date():
    bars = [Bar(d, 0, 0, 0, c, 0) for d, c in [("2026-10-01", 150.0), ("2026-10-05", 101.0), ("2026-10-06", 99.0)]]
    assert highest_close_since(bars, "2026-10-05T10:00:00+02:00") == 101.0     # the 150 before entry is ignored


# ------------------------------------------------------------------ ticks
def test_round_to_tick():
    assert round_to_tick(92.037, 0.01, down=True) == 92.03
    assert round_to_tick(92.031, 0.01, down=False) == 92.04
    assert round_to_tick(92.03, 0.01, down=True) == 92.03
    assert round_to_tick(7249.7, 0.5, down=True) == 7249.5
    assert round_to_tick(12.3456, 0.0, down=True) == 12.3456


# ------------------------------------------------------------------ broker adapter
def _broker(magnifier=1, increments=((0.0, 0.01),), perm=777):
    sent = {}

    class Trade:
        def __init__(self, contract, order):
            self.contract, self.order = contract, order
            self.order.permId, self.order.orderId, self.order.clientId = perm, 12, 1
            self.orderStatus, self.log = NS(status="PreSubmitted"), []

        def isDone(self):
            return self.orderStatus.status in ("Filled", "Cancelled")

    trades = []

    def place(contract, order):
        sent.update(type=order.orderType, action=order.action, qty=order.totalQuantity, aux=order.auxPrice,
                    tif=order.tif, rth=order.outsideRth)
        if order.orderType == "LMT":
            sent["lmt"] = order.lmtPrice
        t = next((t for t in trades if t.order is order), None) or Trade(contract, order)
        if t not in trades:
            trades.append(t)
        return t

    def cancel(order):
        next(t for t in trades if t.order is order).orderStatus.status = "Cancelled"
    details = NS(priceMagnifier=magnifier, marketRuleIds="26,26", validExchanges="SMART,LSE", minTick=0.0001)
    ib = NS(placeOrder=place, cancelOrder=cancel, sleep=lambda s: None, reqAllOpenOrders=lambda: list(trades),
            reqContractDetails=lambda c: [details],
            reqMarketRule=lambda rid: [NS(lowEdge=a, increment=b) for a, b in increments])
    b = IBKRBroker.__new__(IBKRBroker)
    b.ib, b.account_id, b.readonly, b.cfg = ib, "DU1234567", False, NS(mode="paper")
    b._magnifiers, b._details, b._rules = {}, {}, {}
    contract = NS(symbol="VOD" if magnifier > 1 else "SAP", currency="GBP" if magnifier > 1 else "EUR",
                  conId=5, secType="STK")
    return b, contract, sent


def test_place_stop_is_a_sell_stp_gtc_rounded_down_and_identified_by_perm_id():
    b, c, sent = _broker()
    o = b.place_stop(c, 10, 92.037)
    assert sent == {"type": "STP", "action": "SELL", "qty": 10, "aux": 92.03, "tif": "GTC", "rth": False}
    assert (o.perm_id, o.stop_price, o.quantity) == (777, 92.03, 10.0)


def test_gbx_stop_is_sent_in_pence_on_the_pence_tick():
    b, c, sent = _broker(magnifier=100, increments=((0.0, 0.1), (5000.0, 0.5)))
    o = b.place_stop(c, 100, 72.4977)            # 7,249.77 pence -> 7,249.5 on the 0.5 tick
    assert sent["aux"] == 7249.5 and o.stop_price == pytest.approx(72.495)


def test_modify_stop_never_lowers_through_rounding():
    b, c, sent = _broker(increments=((0.0, 0.05),))
    o = b.place_stop(c, 10, 100.0)
    status, price = b.modify_stop(o.perm_id, stop_price=100.04, never_below=100.0)   # rounds to 100.00
    assert price == 100.0
    status, price = b.modify_stop(o.perm_id, stop_price=100.07, never_below=100.0)   # rounds to 100.05: raised
    assert price == pytest.approx(100.05) and sent["aux"] == pytest.approx(100.05)
    status, price = b.modify_stop(o.perm_id, stop_price=99.0, never_below=100.05)    # would lower: not sent
    assert price == pytest.approx(100.05)


def test_modify_or_cancel_a_stop_that_is_gone():
    b, _, _ = _broker()
    assert b.modify_stop(123, quantity=5)[0] == "NotOpen" and b.cancel_stop(123) == "NotOpen"


def test_readonly_broker_cannot_place_stops():
    b, c, _ = _broker()
    b.readonly = True
    from guardrail_trader.broker import BrokerSafetyError
    with pytest.raises(BrokerSafetyError):
        b.place_stop(c, 1, 90.0)


def test_buy_limit_rounds_down_and_sell_limit_rounds_up_to_the_tick():
    """Never a worse price than the gate approved."""
    b, c, sent = _broker(increments=((0.0, 0.05),))
    b._to_result = lambda t: t
    b.place_order(c, "BUY", 1, limit_price=100.03, wait_seconds=0)
    assert sent["lmt"] == pytest.approx(100.0)
    b.place_order(c, "SELL", 1, limit_price=100.03, wait_seconds=0)
    assert sent["lmt"] == pytest.approx(100.05)


# ------------------------------------------------------------------ journal
@pytest.fixture
def j(tmp_path):
    jr = Journal(tmp_path / "j.sqlite")
    jr.init_budget(10000.0, "EUR")
    yield jr
    jr.close()


def test_position_entry_resets_after_a_round_trip(j):
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 1.0, 1.0)
    j.record_fill("SAP", "EUR", "BUY", 10, 110.0, 1.0, 1.0)
    assert j.position_entry("SAP:EUR")[1] == pytest.approx(105.0)
    j.record_fill("SAP", "EUR", "SELL", 20, 120.0, 1.0, 1.0)
    assert j.position_entry("SAP:EUR") == (None, None)
    j.record_fill("SAP", "EUR", "BUY", 5, 90.0, 1.0, 1.0)
    assert j.position_entry("SAP:EUR")[1] == pytest.approx(90.0)


def test_book_sell_writes_the_closed_trade_with_net_pnl(j):
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 2.0, 1.0)              # cost 1,002
    j.book_sell("SAP", "EUR", 10, 110.0, 2.0, 1.0, "stop", perm_id=5)    # proceeds 1,098
    row = j.db.execute("SELECT * FROM closed_trades").fetchone()
    assert (row["motif"], row["quantity"], row["entry_price"], row["exit_price"]) == ("stop", 10, 100.0, 110.0)
    assert row["pnl_net_base"] == pytest.approx(96.0)
    assert j.holdings_qty() == {}


def test_stop_fill_is_booked_once(j):
    j.record_fill("SAP", "EUR", "BUY", 10, 100.0, 2.0, 1.0)
    args = ("x1", 5, "SAP", "EUR", 10, 92.0, 2.0, "EUR", 2.0, 1.0)
    assert j.book_stop_fill(*args) is True and j.book_stop_fill(*args) is False
    assert j.holdings_qty() == {} and j.stop_filled_qty(5) == 10
    assert j.recently_stopped(10) != {}                                  # feeds the T3 re-entry cooldown


def test_stop_rows_and_history(j):
    j.add_stop("SAP", "EUR", 5, 12, 10, 92.0, 100.0, "2026-10-12", 4.0, "test")
    assert j.active_stop("SAP:EUR")["stop_price"] == 92.0
    j.update_stop(5, "trail", stop_price=100.0)
    j.update_stop(5, "line closed", status="cancelled")
    assert j.active_stop("SAP:EUR") is None
    assert [r["reason"] for r in j.db.execute("SELECT reason FROM stop_history ORDER BY id")] == \
        ["placed test", "trail", "line closed -> cancelled"]
