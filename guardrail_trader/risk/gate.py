"""The risk gate: plain code between Claude's proposals and the broker.

Pure functions only - no broker, no network, no clock - so every rule is unit-testable.

Drawdown tiers (T4): next_tier() moves NORMAL -> PAUSED -> RECOVERY -> NORMAL, and any tier ->
REDUCED; the journal stores the tier, the gate applies it to BUYs. The kill switch is separate.

Hard rules (not configurable):
  * BUY / SELL only, limit orders only (no market orders on delayed data)
  * no shorting  - can only sell what the virtual ledger holds
  * no margin    - buys must be paid from virtual cash, incl. commission
  * halted       - after a kill switch, nothing trades until the operator resets it
Configurable rules come from config/risk.toml.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from guardrail_trader.risk.config import RiskConfig, instrument_key

EPS = 1e-9

NORMAL, PAUSED, RECOVERY, REDUCED = "NORMAL", "PAUSED", "RECOVERY", "REDUCED"


def add_business_days(day: date, n: int) -> date:
    """`n` Monday-Friday days after `day`. Exchange holidays are not skipped (a pause or cooldown can
    end a day early around a holiday)."""
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def next_tier(tier: str, pause_until: str | None, drawdown_pct: float, today: date,
              cfg: RiskConfig) -> tuple[str, str | None]:
    """Drawdown tier for this run (CLAUDE.md §4, decisions of 12/10/2026). Returns (tier, pause_until).

    * any tier, drawdown >= reduce          -> REDUCED (no BUY while equity > reduce_max_equity_pct)
    * REDUCED, drawdown back under reduce   -> RECOVERY
    * NORMAL, drawdown >= pause             -> PAUSED until pause_days business days after today
    * PAUSED, pause over                    -> RECOVERY (half-size BUYs), whatever the drawdown
    * RECOVERY, drawdown < resume           -> NORMAL
    RECOVERY never re-pauses: a deeper fall is caught by REDUCED, then by the kill switch.
    """
    if drawdown_pct >= cfg.drawdown_reduce_pct - EPS:
        return REDUCED, None
    if tier == REDUCED:
        tier = RECOVERY
    if tier == PAUSED:
        if pause_until and today <= date.fromisoformat(pause_until):
            return PAUSED, pause_until
        tier = RECOVERY
    if tier == NORMAL and drawdown_pct >= cfg.drawdown_pause_pct - EPS:
        return PAUSED, add_business_days(today, cfg.drawdown_pause_days).isoformat()
    if tier == RECOVERY and drawdown_pct < cfg.drawdown_resume_pct:
        return NORMAL, None
    return tier, None


@dataclass(frozen=True)
class Proposal:
    symbol: str
    exchange: str
    currency: str
    action: str          # "BUY" | "SELL"
    quantity: float
    limit_price: float   # in the instrument's currency
    reason: str = ""     # Claude's rationale, journaled verbatim

    @property
    def key(self) -> str:
        return instrument_key(self.symbol, self.currency)


@dataclass(frozen=True)
class MarketInfo:
    price: float                # reference price, instrument currency
    fx_to_base: float           # base-currency units per 1 unit of instrument currency
    est_commission_base: float  # broker what-if estimate, in base currency


@dataclass
class Holding:
    quantity: float
    price_base: float  # current price converted to base currency


@dataclass
class PortfolioState:
    cash_base: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    peak_value_base: float = 0.0
    trades_this_month: int = 0
    halted: bool = False
    # instrument key -> last day (ISO date) of its re-entry cooldown after a stop exit; the journal
    # computes the business days, so the gate stays clock-free
    recently_stopped: dict[str, str] = field(default_factory=dict)
    tier: str = NORMAL                 # drawdown tier for this run (see next_tier)
    pause_until: str | None = None     # PAUSED: last day without BUYs
    daily_halt: str = ""               # non-empty = yesterday's loss blocks every order today (the reason)

    @property
    def value_base(self) -> float:
        return self.cash_base + sum(h.quantity * h.price_base for h in self.holdings.values())

    @property
    def drawdown_pct(self) -> float:
        peak = max(self.peak_value_base, self.value_base)
        return 0.0 if peak <= 0 else (1 - self.value_base / peak) * 100


@dataclass
class Decision:
    proposal: Proposal
    approved: bool
    reasons: list[str]           # why it was blocked (empty if approved)
    notional_base: float = 0.0
    commission_base: float = 0.0


def _meta(key: str, cfg: RiskConfig):
    """Instrument metadata for a key (universe or hedge); None for anything else."""
    return cfg.universe.get(key) or cfg.hedge.get(key)


def exposures(state: PortfolioState, cfg: RiskConfig) -> dict:
    """Holdings value by sector, US and total equity, in base currency. A holding with unknown
    metadata counts under sector '?' and as non-US; it still counts as equity."""
    sectors: dict[str, float] = {}
    us = equity = 0.0
    for key, h in state.holdings.items():
        v = h.quantity * h.price_base
        inst = _meta(key, cfg)
        sector = inst.sector if inst and inst.sector else "?"
        sectors[sector] = sectors.get(sector, 0.0) + v
        us += v if inst and inst.country == "US" else 0.0
        equity += v
    return {"sectors": sectors, "us": us, "equity": equity}


def kill_switch_triggered(state: PortfolioState, cfg: RiskConfig) -> bool:
    return state.drawdown_pct >= cfg.max_drawdown_pct - EPS


def evaluate(
    proposals: list[Proposal],
    state: PortfolioState,
    market: dict[str, MarketInfo],
    cfg: RiskConfig,
) -> tuple[list[Decision], PortfolioState]:
    """Check every proposal. Sells are evaluated first so they can fund buys.

    Proposals are applied to a *simulated* state one by one, so a batch can't
    jointly break a limit that each order passes alone. Input state is not modified.
    """
    sim = copy.deepcopy(state)
    ordered = sorted(proposals, key=lambda p: 0 if p.action.upper() == "SELL" else 1)
    decisions: list[Decision] = []

    global_block = None
    if sim.halted:
        global_block = "trading halted by kill switch - manual reset required"
    elif kill_switch_triggered(sim, cfg):
        global_block = f"drawdown {sim.drawdown_pct:.1f}% >= limit {cfg.max_drawdown_pct}% - kill switch"
    elif sim.daily_halt:
        global_block = sim.daily_halt

    for p in ordered:
        if global_block:
            decisions.append(Decision(p, False, [global_block]))
            continue
        reasons = _check(p, sim, market, cfg)
        if reasons:
            decisions.append(Decision(p, False, reasons))
            continue
        m = market[p.key]
        notional = p.quantity * p.limit_price * m.fx_to_base
        _apply(p, sim, m, notional)
        decisions.append(Decision(p, True, [], notional, m.est_commission_base))
    return decisions, sim


def _check(p: Proposal, sim: PortfolioState, market: dict[str, MarketInfo], cfg: RiskConfig) -> list[str]:
    r: list[str] = []
    action = p.action.upper()

    if action not in ("BUY", "SELL"):
        return [f"action must be BUY or SELL, got {p.action!r}"]
    if p.key in cfg.hedge and not cfg.enable_hedge:
        return [f"{p.key} is a hedge instrument (pocket C), disabled in phase 1 (enable_hedge = false)"]
    if p.key not in cfg.universe:
        r.append(f"{p.key} is not on the allowlist")
    elif cfg.universe[p.key].exchange != p.exchange.upper():
        r.append(f"{p.key} must trade on {cfg.universe[p.key].exchange}, not {p.exchange}")
    if not (math.isfinite(p.quantity) and math.isfinite(p.limit_price)):
        return [f"quantity and limit price must be finite numbers, got {p.quantity} @ {p.limit_price}"]
    if not p.quantity > 0:
        r.append(f"quantity must be > 0, got {p.quantity}")
    elif not cfg.allow_fractional and p.quantity != int(p.quantity):
        r.append(f"fractional quantity {p.quantity} not allowed")
    if not p.limit_price > 0:
        r.append("a positive limit price is required (limit orders only)")

    m = market.get(p.key)
    if m is None or not m.price > 0:
        r.append(f"no reference price for {p.key}")
        return r
    if r:
        return r

    dev = abs(p.limit_price - m.price) / m.price * 100
    if dev > cfg.max_price_deviation_pct + EPS:
        r.append(f"limit {p.limit_price} is {dev:.2f}% from price {m.price} (max {cfg.max_price_deviation_pct}%)")

    if sim.trades_this_month + 1 > cfg.max_trades_per_month:
        r.append(f"monthly trade limit reached ({cfg.max_trades_per_month})")

    notional = p.quantity * p.limit_price * m.fx_to_base
    comm_pct = m.est_commission_base / notional * 100 if notional > 0 else float("inf")
    if comm_pct > cfg.max_commission_pct + EPS:
        r.append(f"commission is {comm_pct:.1f}% of order value (max {cfg.max_commission_pct}%)")

    held = sim.holdings.get(p.key, Holding(0.0, m.price * m.fx_to_base)).quantity
    px_base = m.price * m.fx_to_base
    value_after = sim.value_base - m.est_commission_base

    def pct(v: float) -> float:
        return v / value_after * 100 if value_after > 0 else float("inf")

    if action == "SELL":
        if p.quantity > held + EPS:
            r.append(f"cannot sell {p.quantity}, only {held} held (no shorting)")
        else:
            left = pct((held - p.quantity) * px_base)
            if held - p.quantity > EPS and left < cfg.min_position_pct - EPS:
                r.append(f"would leave a {left:.2f}% line (min {cfg.min_position_pct}%): sell all of it or less")
        return r

    if sim.tier == PAUSED:
        r.append(f"drawdown pause (>= {cfg.drawdown_pause_pct}%): no new BUY until after {sim.pause_until}")
    max_pos = cfg.max_position_pct / 2 if sim.tier == RECOVERY else cfg.max_position_pct
    max_eq = min(cfg.max_equity_pct, cfg.reduce_max_equity_pct) if sim.tier == REDUCED else cfg.max_equity_pct

    cost = notional + m.est_commission_base
    if cost > sim.cash_base + EPS:
        r.append(f"cost {cost:.2f} exceeds cash {sim.cash_base:.2f} (no margin)")
    pos_pct = pct((held + p.quantity) * px_base)
    if pos_pct > max_pos + EPS:
        r.append(f"position would be {pos_pct:.1f}% of portfolio (max {max_pos:g}%"
                 + (", half size in the RECOVERY tier)" if sim.tier == RECOVERY else ")"))
    if pos_pct < cfg.min_position_pct - EPS:
        r.append(f"position would be {pos_pct:.2f}% of portfolio (min {cfg.min_position_pct}%)")

    until = sim.recently_stopped.get(p.key)
    if until:
        r.append(f"{p.key} was stopped out: no re-entry until after {until} ({cfg.reentry_cooldown_days} business days)")

    added = p.quantity * px_base
    ex, inst = exposures(sim, cfg), cfg.universe[p.key]
    sector = inst.sector or "?"
    sec_pct = pct(ex["sectors"].get(sector, 0.0) + added)
    if sec_pct > cfg.max_sector_pct + EPS:
        r.append(f"sector {sector} would be {sec_pct:.1f}% of portfolio (max {cfg.max_sector_pct}%)")
    if inst.country == "US":
        us_pct = pct(ex["us"] + added)
        if us_pct > cfg.max_us_pct + EPS:
            r.append(f"US holdings would be {us_pct:.1f}% of portfolio (max {cfg.max_us_pct}%)")
    eq_pct = pct(ex["equity"] + added)
    if eq_pct > max_eq + EPS:
        r.append(f"equity exposure would be {eq_pct:.1f}% of portfolio (max {max_eq:g}%"
                 + (f", REDUCED tier: drawdown >= {cfg.drawdown_reduce_pct}%)" if sim.tier == REDUCED
                    else ", rest stays cash)"))
    return r


def _apply(p: Proposal, sim: PortfolioState, m: MarketInfo, notional: float) -> None:
    h = sim.holdings.setdefault(p.key, Holding(0.0, m.price * m.fx_to_base))
    h.price_base = m.price * m.fx_to_base
    if p.action.upper() == "BUY":
        sim.cash_base -= notional + m.est_commission_base
        h.quantity += p.quantity
    else:
        sim.cash_base += notional - m.est_commission_base
        h.quantity -= p.quantity
        if h.quantity <= EPS:
            del sim.holdings[p.key]
    sim.trades_this_month += 1


def liquidation_proposals(
    state: PortfolioState, market: dict[str, MarketInfo], cfg: RiskConfig, discount_pct: float = 1.0
) -> list[Proposal]:
    """Kill switch: SELL every holding at a limit slightly below reference so it fills.

    These bypass the trade-count limit (safety exits must never be blocked),
    but still only sell what is held.
    """
    out = []
    for key, h in state.holdings.items():
        m = market.get(key)
        inst = cfg.universe.get(key)
        if h.quantity <= EPS or m is None or inst is None:
            continue
        limit = round(m.price * (1 - discount_pct / 100), 2)
        out.append(Proposal(inst.symbol, inst.exchange, inst.currency, "SELL", h.quantity, limit,
                            reason="kill switch: max drawdown reached"))
    return out
