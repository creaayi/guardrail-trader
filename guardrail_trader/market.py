"""Market data helpers built on the broker adapter (read-only)."""
from __future__ import annotations

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from guardrail_trader.broker.ibkr import IBKRBroker, _num, stock_contract, to_major
from guardrail_trader.risk.config import Instrument


def parse_liquid_hours(liquid_hours: str, tz_id: str) -> list[tuple[datetime, datetime]]:
    """IBKR format: '20260921:0930-20260921:1600;20260922:CLOSED;...' -> [(open, close), ...]."""
    tz = ZoneInfo({"US/Eastern": "America/New_York"}.get(tz_id, tz_id))
    sessions = []
    for part in filter(None, liquid_hours.split(";")):
        if "CLOSED" in part:
            continue
        start, end = part.split("-")
        sessions.append((datetime.strptime(start, "%Y%m%d:%H%M").replace(tzinfo=tz),
                         datetime.strptime(end, "%Y%m%d:%H%M").replace(tzinfo=tz)))
    return sessions


def is_open(sessions: list[tuple[datetime, datetime]], now: datetime, min_left_minutes: int = 15) -> bool:
    """Open now, with at least `min_left_minutes` before the close (time to fill or cancel)."""
    return any(o <= now and (c - now).total_seconds() >= min_left_minutes * 60 for o, c in sessions)


def market_sessions(broker: IBKRBroker, inst: Instrument) -> list[tuple[datetime, datetime]]:
    c = broker.stock(inst.symbol, inst.exchange, inst.currency)
    d = broker.ib.reqContractDetails(c)[0]
    return parse_liquid_hours(d.liquidHours, d.timeZoneId)


def price_instruments(broker: IBKRBroker, insts: list[Instrument], batch: int = 50,
                      wait_seconds: float = 8.0) -> dict[str, float]:
    """Latest price (instrument currency) for many instruments, in batches. Missing -> absent."""
    out: dict[str, float] = {}
    for n in range(0, len(insts), batch):
        chunk = insts[n:n + batch]
        contracts = [stock_contract(i.symbol, i.exchange, i.currency) for i in chunk]
        broker.ib.qualifyContracts(*contracts)
        live = [(i, c, broker.ib.reqMktData(c)) for i, c in zip(chunk, contracts) if c.conId]
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            broker.ib.sleep(0.5)
            if all(not math.isnan(_num(t.marketPrice())) or not math.isnan(_num(t.close)) for *_, t in live):
                break
        for i, c, t in live:
            broker.ib.cancelMktData(c)
            px = _num(t.marketPrice())
            px = to_major(px if not math.isnan(px) else _num(t.close), broker.magnifier(c))  # GBX -> GBP
            if not math.isnan(px) and px > 0:
                out[i.key] = px
    return out


def daily_history(broker: IBKRBroker, inst: Instrument, months: int = 6) -> list[tuple[str, float]]:
    """Daily closes (currency units), oldest first. IBKR pacing: keep this to a handful of calls per run."""
    c = broker.stock(inst.symbol, inst.exchange, inst.currency)
    bars = broker.ib.reqHistoricalData(c, endDateTime="", durationStr=f"{months} M", barSizeSetting="1 day",
                                       whatToShow="TRADES", useRTH=True, formatDate=1, timeout=30)
    mag = broker.magnifier(c)
    return [(str(b.date), to_major(float(b.close), mag)) for b in bars]


def summarize_history(closes: list[tuple[str, float]]) -> dict:
    """Compact features for Claude instead of 125 raw rows."""
    px = [c for _, c in closes]
    if len(px) < 2:
        return {"error": "not enough history"}

    def ret(days: int):
        return round((px[-1] / px[-days - 1] - 1) * 100, 2) if len(px) > days else None

    def sma(days: int):
        return round(sum(px[-days:]) / days, 2) if len(px) >= days else None

    daily = [px[i] / px[i - 1] - 1 for i in range(1, len(px))]
    mean = sum(daily) / len(daily)
    vol = (sum((d - mean) ** 2 for d in daily) / len(daily)) ** 0.5 * (252 ** 0.5) * 100
    peak = max(px)
    return {
        "last_close": px[-1], "as_of": closes[-1][0],
        "return_1w_pct": ret(5), "return_1m_pct": ret(21), "return_3m_pct": ret(63), "return_6m_pct": ret(len(px) - 1),
        "sma_20": sma(20), "sma_50": sma(50), "high_6m": peak, "low_6m": min(px),
        "drawdown_from_6m_high_pct": round((px[-1] / peak - 1) * 100, 2),
        "annualized_volatility_pct": round(vol, 1),
        "last_10_closes": [round(p, 2) for p in px[-10:]],
    }
