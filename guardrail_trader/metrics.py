"""Performance against the benchmark (CLAUDE.md §8.3, §9): daily returns, alpha, beta, Sharpe, Sortino, drawdown.

Conventions (CHANGELOG 13/10/2026, T7, to be confirmed by the human):
- daily returns from the day's last valuation (nav_daily, benchmark), deposits removed:
  r_t = (V_t - D_t) / V_{t-1} - 1, D_t = flows of day t: the bot's deposits after day 1 (ledger), and for the
  benchmark the same amounts on the day they were invested in it (benchmark.invested_base);
- the benchmark is carried forward over its holidays (last known NAV);
- risk-free rate 0; annualisation over 252 trading days;
- alpha = Jensen's alpha, annualised: (mean(r_p) - beta x mean(r_b)) x 252; beta = cov(r_p, r_b) / var(r_b);
- excess return = time-weighted return of the portfolio minus the benchmark's, same period.
Missing data is reported as None, never invented.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

TRADING_DAYS = 252
BENCHMARK_KEY = "IWDA:EUR"     # CLAUDE.md §1: iShares Core MSCI World (AEB, EUR), listed in hedge.csv


@dataclass(frozen=True)
class Performance:
    days: int                      # daily returns used
    first_date: str | None
    last_date: str | None
    return_pct: float | None       # time-weighted, portfolio
    benchmark_return_pct: float | None
    excess_pct: float | None       # return_pct - benchmark_return_pct
    alpha_pct: float | None        # annualised Jensen's alpha, in %
    beta: float | None
    sharpe: float | None
    benchmark_sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    benchmark_max_drawdown_pct: float | None


def returns(navs: list[float], flows: list[float]) -> list[float]:
    """Daily returns with each day's flows removed (flows[i] arrived on day i). Needs positive NAVs."""
    if any(v <= 0 for v in navs[:-1]):
        raise ValueError("a NAV <= 0 has no return")
    return [(navs[i] - flows[i]) / navs[i - 1] - 1 for i in range(1, len(navs))]


def twr_pct(rets: list[float]) -> float:
    return (math.prod(1 + r for r in rets) - 1) * 100


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def beta(rp: list[float], rb: list[float]) -> float | None:
    if len(rp) < 2:
        return None
    mp, mb = _mean(rp), _mean(rb)
    var = sum((b - mb) ** 2 for b in rb)
    return None if var == 0 else sum((p - mp) * (b - mb) for p, b in zip(rp, rb)) / var


def alpha_pct(rp: list[float], rb: list[float]) -> float | None:
    b = beta(rp, rb)
    return None if b is None else (_mean(rp) - b * _mean(rb)) * TRADING_DAYS * 100


def sharpe(rets: list[float]) -> float | None:
    if len(rets) < 2:
        return None
    m = _mean(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return None if sd == 0 else m / sd * math.sqrt(TRADING_DAYS)


def sortino(rets: list[float]) -> float | None:
    if len(rets) < 2:
        return None
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    return None if downside == 0 else _mean(rets) / downside * math.sqrt(TRADING_DAYS)


def max_drawdown_pct(rets: list[float]) -> float | None:
    if not rets:
        return None
    level, peak, worst = 1.0, 1.0, 0.0
    for r in rets:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    return worst * 100


def aligned(nav: dict[str, float], bench: dict[str, float], flows: dict[str, float], bench_flows: dict[str, float]
            ) -> tuple[list[str], list[float], list[float], list[float], list[float]]:
    """(dates, portfolio NAVs, benchmark NAVs, portfolio flows, benchmark flows) on the portfolio's days from the
    benchmark's first day, the benchmark carried forward over its holidays (no flow on a carried day)."""
    dates, p, b, f, fb = [], [], [], [], []
    last_b = None
    for day in sorted(nav):
        last_b = bench.get(day, last_b)
        if last_b is None:
            continue
        dates.append(day)
        p.append(nav[day])
        b.append(last_b)
        f.append(flows.get(day, 0.0))
        fb.append(bench_flows.get(day, 0.0) if day in bench else 0.0)
    return dates, p, b, f, fb


def compute(nav: dict[str, float], bench: dict[str, float], flows: dict[str, float] | None = None,
            bench_flows: dict[str, float] | None = None) -> Performance:
    dates, p, b, f, fb = aligned(nav, bench, flows or {}, bench_flows or {})
    rp, rb = returns(p, f), returns(b, fb)
    n = len(rp)
    ret, bret = (twr_pct(rp), twr_pct(rb)) if n else (None, None)
    return Performance(
        days=n, first_date=dates[0] if dates else None, last_date=dates[-1] if dates else None,
        return_pct=ret, benchmark_return_pct=bret, excess_pct=None if ret is None else ret - bret,
        alpha_pct=alpha_pct(rp, rb), beta=beta(rp, rb), sharpe=sharpe(rp), benchmark_sharpe=sharpe(rb),
        sortino=sortino(rp), max_drawdown_pct=max_drawdown_pct(rp), benchmark_max_drawdown_pct=max_drawdown_pct(rb))


def from_journal(j) -> Performance:
    """Performance of the bot vs the benchmark, from the journal's daily tables."""
    nav = {r[0]: r[1] for r in j.db.execute("SELECT date, value_base FROM nav_daily")}
    rows = j.db.execute("SELECT date, nav_base, invested_base FROM benchmark").fetchall()
    bench = {r[0]: r[1] for r in rows}
    bench_flows = {r[0]: r[2] for r in rows if r[2]}
    first = min(bench) if bench else None
    flows: dict[str, float] = {}
    for ts, amount in j.deposits():
        day = datetime.fromisoformat(ts).date().isoformat()
        if first is not None and day > first:     # the initial deposit is the starting value, not a flow
            flows[day] = flows.get(day, 0.0) + amount
    return compute(nav, bench, flows, bench_flows)
