"""Pocket A ranking, computed by code (Claude receives it, never recomputes it).

Score = return from `lookback_days` to `skip_days` trading days ago ("6 months excluding the last
month"). Volatility filter: ATR14 / last close above `max_atr_pct` is excluded. Pure functions over the
bar cache; instruments with missing, short or stale history are excluded and counted, never filled in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from guardrail_trader.bars import Bar, avg_traded_value
from guardrail_trader.risk.config import Instrument, MomentumConfig


def atr(bars: list[Bar], n: int = 14) -> float | None:
    """Wilder's ATR over the bars given (oldest first); None if fewer than n+1 bars or a missing high/low."""
    if len(bars) < n + 1 or any(b.high is None or b.low is None for b in bars):
        return None
    tr = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars, bars[1:])]
    value = sum(tr[:n]) / n
    for x in tr[n:]:
        value = (value * (n - 1) + x) / n
    return value


def momentum_return(closes: list[float], lookback: int, skip: int) -> float | None:
    """close[t-skip] / close[t-lookback] - 1, with t the last bar; None without enough history."""
    if len(closes) < lookback + 1 or closes[-1 - lookback] <= 0:
        return None
    return closes[-1 - skip] / closes[-1 - lookback] - 1


@dataclass
class Ranking:
    as_of: str                                   # most recent bar date used
    method: str
    top: list[dict]                              # best first, at most cfg.top_n
    excluded: dict[str, int] = field(default_factory=dict)   # reason -> count

    def for_claude(self) -> dict:
        return {"as_of": self.as_of or None, "method": self.method, "top": self.top,
                "excluded_counts": self.excluded}


def rank(universe: dict[str, Instrument], bars_for, cfg: MomentumConfig, today: date,
         fx: dict[str, float]) -> Ranking:
    """bars_for(key) -> list[Bar] oldest first (e.g. BarCache.bars); fx: currency -> base units per 1.

    Liquidity (CLAUDE.md §3): 20-day average traded value >= min_traded_value, in USD for US stocks and
    in the base currency for the others."""
    scored, excluded, as_of = [], {}, ""

    def skip(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for key, inst in universe.items():
        b = bars_for(key)
        if not b:
            skip("no cached history")
            continue
        if (today - date.fromisoformat(b[-1].date)).days > cfg.max_age_days:
            skip(f"history older than {cfg.max_age_days} days")
            continue
        r = momentum_return([x.close for x in b], cfg.lookback_days, cfg.skip_days)
        a = atr(b[-(cfg.lookback_days + 1):])
        if r is None or a is None:
            skip("history too short")
            continue
        adv = avg_traded_value(b)
        if not (inst.country == "US" and inst.currency == "USD"):
            if inst.currency not in fx:
                skip("no FX rate")
                continue
            adv *= fx[inst.currency]
        if adv < cfg.min_traded_value:
            skip(f"20-day traded value below {cfg.min_traded_value:,.0f}")
            continue
        atr_pct = a / b[-1].close * 100
        if atr_pct > cfg.max_atr_pct:
            skip(f"ATR14/price above {cfg.max_atr_pct}%")
            continue
        as_of = max(as_of, b[-1].date)
        scored.append({"symbol": inst.symbol, "currency": inst.currency, "name": inst.name,
                       "sector": inst.sector, "country": inst.country,
                       "momentum_pct": round(r * 100, 2), "atr14_pct": round(atr_pct, 2), "last_close": b[-1].close})
    scored.sort(key=lambda x: (-x["momentum_pct"], x["symbol"]))
    top = [{"rank": i, **row} for i, row in enumerate(scored[:cfg.top_n], start=1)]
    method = (f"return from {cfg.lookback_days} to {cfg.skip_days} trading days ago; excluded if ATR14/price "
              f"> {cfg.max_atr_pct}%, 20-day traded value < {cfg.min_traded_value:,.0f}, "
              f"or history missing/short/older than {cfg.max_age_days} days")
    return Ranking(as_of, method, top, excluded)
