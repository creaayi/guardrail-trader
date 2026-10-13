"""T8: decision slots in their own zones, per-exchange open checks, and what each slot may trade."""
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from guardrail_trader.pipeline import IBKRMarketData, slot_task
from guardrail_trader.risk import Instrument, MarketInfo, PortfolioState, Proposal, RiskConfig, evaluate
from guardrail_trader.slots import EU_EXCHANGES, SLOTS, US_EXCHANGES, current_slot, next_slot

PARIS, NY, UTC = ZoneInfo("Europe/Paris"), ZoneInfo("America/New_York"), ZoneInfo("UTC")


def at(y, m, d, hh, mm, tz):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


@pytest.mark.parametrize("now,slot", [
    (at(2026, 10, 12, 10, 0, PARIS), "eu_open"),
    (at(2026, 10, 12, 10, 59, PARIS), "eu_open"),
    (at(2026, 10, 12, 11, 0, PARIS), None),
    (at(2026, 10, 12, 10, 30, NY), "us_open"),
    (at(2026, 10, 12, 15, 10, NY), "us_close"),
    (at(2026, 10, 12, 16, 20, NY), "stops"),
    (at(2026, 10, 17, 10, 15, PARIS), None),                   # Saturday
    (at(2026, 10, 18, 10, 45, NY), None),                      # Sunday
    # 16/03/2026: the US is on summer time, Europe not yet. Each slot follows its own zone.
    (at(2026, 3, 16, 9, 0, UTC), "eu_open"),                   # 10:00 Paris (UTC+1)
    (at(2026, 3, 16, 14, 30, UTC), "us_open"),                 # 10:30 New York (UTC-4)
    # 30/03/2026: both on summer time
    (at(2026, 3, 30, 8, 0, UTC), "eu_open"),                   # 10:00 Paris (UTC+2)
    (at(2026, 3, 30, 19, 0, UTC), "us_close"),                 # 15:00 New York (UTC-4)
    # winter
    (at(2026, 1, 5, 9, 30, UTC), "eu_open"),
    (at(2026, 1, 5, 20, 0, UTC), "us_close"),                  # 15:00 New York (UTC-5)
])
def test_slot_detection_in_both_zones(now, slot):
    s = current_slot(now)
    assert (s.name if s else None) == slot


def test_slots_never_overlap_and_trade_the_right_markets():
    assert SLOTS["eu_open"].exchanges == EU_EXCHANGES == {"SBF", "AEB", "IBIS", "LSE", "EBS"}
    assert SLOTS["us_open"].exchanges == SLOTS["us_close"].exchanges == US_EXCHANGES
    assert SLOTS["stops"].exchanges == frozenset() and SLOTS["stops"].args == ("--stops-only",)
    assert SLOTS["us_close"].args == ("--trail-stops",)
    minute = at(2026, 3, 16, 0, 0, UTC)                       # every minute of a DST-gap week day
    from datetime import timedelta
    for k in range(24 * 60):
        t = minute + timedelta(minutes=k)
        assert sum(s.contains(t) for s in SLOTS.values()) <= 1


def test_monday_rebalance_only_in_the_opening_slots():
    mon_eu, mon_us, mon_close = at(2026, 10, 12, 10, 5, PARIS), at(2026, 10, 12, 10, 45, NY), at(2026, 10, 12, 15, 5, NY)
    assert SLOTS["eu_open"].rebalance(mon_eu) and SLOTS["us_open"].rebalance(mon_us)
    assert not SLOTS["us_close"].rebalance(mon_close)
    assert not SLOTS["eu_open"].rebalance(at(2026, 10, 13, 10, 5, PARIS))
    assert "weekly pocket A rebalance" in slot_task(SLOTS["eu_open"], EU_EXCHANGES, mon_eu)
    assert "Not a rebalance slot" in slot_task(SLOTS["us_close"], US_EXCHANGES, mon_close)


def test_next_slot_for_the_dashboard():
    s, start = next_slot(at(2026, 10, 16, 16, 50, NY))        # Friday evening
    assert s.name == "eu_open" and start == at(2026, 10, 19, 10, 0, PARIS)


def test_scheduler_key_uses_the_slot_s_own_day():
    late_ny = at(2026, 10, 12, 16, 30, NY)                     # 22:30 Paris, same day
    assert SLOTS["stops"].day(late_ny).isoformat() == "2026-10-12"


# ------------------------------------------------------------------ holidays per exchange (liquidHours)
UNI = {i.key: i for i in [Instrument("AAPL", "SMART", "USD", "Apple", "Technology", "US"),
                          Instrument("SAP", "IBIS", "EUR", "SAP", "Technology", "DE"),
                          Instrument("MC", "SBF", "EUR", "LVMH", "Consumer", "FR")]}


class _Broker:
    """contract details per symbol: AAPL open, SAP closed (German holiday), MC open."""
    hours = {"AAPL": ("20261003:0930-20261003:1600", "US/Eastern"),
             "SAP": ("20261003:CLOSED", "Europe/Berlin"),
             "MC": ("20261003:0900-20261003:1730", "Europe/Paris")}

    def stock(self, symbol, exchange, currency):
        return NS(symbol=symbol)

    def __init__(self):
        self.ib = NS(reqContractDetails=lambda c: [NS(liquidHours=self.hours[c.symbol][0],
                                                      timeZoneId=self.hours[c.symbol][1])])


def test_each_exchange_is_judged_on_its_own_hours():
    m = IBKRMarketData(_Broker(), RiskConfig(1.0, "EUR", 25.0, 10, 25.0, 2.0, 10.0, False, UNI))
    now = at(2026, 10, 3, 10, 30, PARIS)
    assert m.open_exchanges(now, EU_EXCHANGES) == {"SBF"}     # Xetra closed; no LSE/AEB/EBS instrument here
    assert m.open_exchanges(now, US_EXCHANGES) == set()        # 04:30 in New York
    assert m.open_exchanges(at(2026, 10, 3, 11, 0, NY), US_EXCHANGES) == {"SMART"}


# ------------------------------------------------------------------ the gate
def test_gate_blocks_orders_outside_the_slot_s_open_exchanges():
    cfg = RiskConfig(100000.0, "EUR", 25.0, 10, 25.0, 2.0, 10.0, False, UNI)
    mkt = {k: MarketInfo(100.0, 1.0, 1.0) for k in UNI}
    s = PortfolioState(cash_base=100000.0, peak_value_base=100000.0, tradable_exchanges=frozenset({"SMART"}))
    (eu, us), _ = evaluate([Proposal("SAP", "IBIS", "EUR", "BUY", 10, 100.0),
                            Proposal("AAPL", "SMART", "USD", "BUY", 10, 100.0)], s, mkt, cfg)
    assert not eu.approved and "IBIS is not tradable in this slot" in eu.reasons[0]
    assert us.approved
