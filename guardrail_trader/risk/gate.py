"""The risk gate: plain code between Claude's proposals and the broker.

Pure functions only - no broker, no network, no clock - so every rule is unit-testable.

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

from guardrail_trader.risk.config import RiskConfig, instrument_key

EPS = 1e-9


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
    if action == "SELL":
        if p.quantity > held + EPS:
            r.append(f"cannot sell {p.quantity}, only {held} held (no shorting)")
    else:
        cost = notional + m.est_commission_base
        if cost > sim.cash_base + EPS:
            r.append(f"cost {cost:.2f} exceeds cash {sim.cash_base:.2f} (no margin)")
        value_after = sim.value_base - m.est_commission_base
        pos_after = (held + p.quantity) * m.price * m.fx_to_base
        pos_pct = pos_after / value_after * 100 if value_after > 0 else float("inf")
        if pos_pct > cfg.max_position_pct + EPS:
            r.append(f"position would be {pos_pct:.1f}% of portfolio (max {cfg.max_position_pct}%)")
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
