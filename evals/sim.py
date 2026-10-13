"""Simulated broker, market and Claude for driving the real pipeline (guardrail_trader.pipeline.run_once).

Everything is deterministic: no network, no API key, no IBKR. Scenarios change one thing
(a price, a fill, an error, what "Claude" says) and check the harness's invariants.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Callable

from guardrail_trader.decision import example
from guardrail_trader.broker import BrokerNotReadyError
from guardrail_trader.broker.ibkr import ExecFill, OpenOrder, OrderResult
from guardrail_trader.journal import Journal
from guardrail_trader.pipeline import LLMSetup, run_once
from guardrail_trader.risk import Instrument, Proposal, RiskConfig

USD_AUD = 1.5
UNIVERSE = {i.key: i for i in [Instrument("AAA", "SMART", "USD"), Instrument("BBB", "SMART", "USD"),
                               Instrument("CCC", "SMART", "USD")]}
# Scenario limits (the original project's; T3+ rules off). The shipped limits are tested in tests/test_risk_gate.py.
CFG = RiskConfig(capital=5000.0, base_currency="AUD", max_position_pct=25.0, max_trades_per_month=10,
                 max_drawdown_pct=25.0, max_price_deviation_pct=2.0, max_commission_pct=10.0,
                 allow_fractional=False, universe=UNIVERSE)
PRICES = {"AAA:USD": 100.0, "BBB:USD": 50.0, "CCC:USD": 20.0}
COMMISSION_USD = 1.0


# ------------------------------------------------------------------------------ broker
@dataclass
class SimOrder:
    oid: int
    symbol: str
    action: str
    qty: float
    limit: float
    filled: float = 0.0
    status: str = "Submitted"
    late_fill: float = 0.0          # shares that fill at IBKR *after* our cancel (never reported)


@dataclass
class SimStop:
    perm: int
    symbol: str
    qty: float
    price: float
    status: str = "Submitted"     # Submitted | Cancelled | Filled


class SimBroker:
    """IBKR as the bot sees it. `holdings` is the broker's truth (what reconciliation compares).

    fill(order) -> (shares filled now, shares that fill late after the cancel). Default: all now.
    """

    def __init__(self, holdings: dict[str, float] | None = None,
                 fill: Callable[[SimOrder], tuple[float, float]] | None = None):
        self.holdings = dict(holdings or {})
        self.fill = fill or (lambda o: (o.qty, 0.0))
        self.orders: dict[int, SimOrder] = {}
        self._ids = itertools.count(100)
        self.fail_positions: Exception | None = None
        self.not_ready = False                      # gateway answers, but serves no account data
        self.fail_place_after: int | None = None    # raise on the Nth+1 place_order
        self.open: list = []                        # stray open orders reported by open_orders()
        self.net_liquidation, self.currency = 5000.0, "AUD"   # the whole paper account, as IBKR reports it
        self.stops: dict[int, SimStop] = {}         # protective STP GTC orders, by permId
        self.execs: list[ExecFill] = []             # what reqExecutions still reports (current day)
        self._perms = itertools.count(9000)
        self.fill_stop_on_cancel: set[str] = set()  # symbols whose stop triggers just before our cancel lands

    # reads
    def ensure_ready(self):
        if self.not_ready:
            raise BrokerNotReadyError("simulated: account summary empty (nightly re-login)")
        return NS(account_id="DU0000000", net_liquidation=self.net_liquidation, currency=self.currency)

    def positions(self):
        if self.fail_positions:
            raise self.fail_positions
        return [NS(symbol=k.split(":")[0], currency=k.split(":")[1], quantity=q) for k, q in self.holdings.items() if q]

    def open_orders(self):
        return list(self.open)

    def all_open_orders(self):
        live = [OpenOrder(s.perm, 0, 1, s.symbol, "USD", "SELL", "STP", s.qty, s.price)
                for s in self.stops.values() if s.status == "Submitted"]
        return [*self.open, *live]

    # stops
    def place_stop(self, contract, quantity, stop_price):
        s = SimStop(next(self._perms), contract.symbol, float(quantity), round(float(stop_price), 2))
        self.stops[s.perm] = s
        return OpenOrder(s.perm, s.perm, 1, s.symbol, "USD", "SELL", "STP", s.qty, s.price)

    def modify_stop(self, perm_id, stop_price=None, quantity=None, never_below=None):
        s = self.stops.get(perm_id)
        if s is None or s.status != "Submitted":
            return ("NotOpen" if s is None or s.status == "Cancelled" else s.status), math.nan
        if stop_price is not None and (never_below is None or round(stop_price, 2) >= never_below - 1e-9):
            s.price = round(float(stop_price), 2)
        if quantity is not None:
            s.qty = float(quantity)
        return s.status, s.price

    def cancel_stop(self, perm_id):
        s = self.stops.get(perm_id)
        if s is None:
            return "NotOpen"
        if s.status == "Submitted" and s.symbol in self.fill_stop_on_cancel:
            self.trigger_stop(perm_id, s.price)      # race: the stop fired before the cancel arrived
        if s.status == "Submitted":
            s.status = "Cancelled"
        return s.status

    def executions(self, perm_ids):
        return [e for e in self.execs if e.perm_id in perm_ids]

    def trigger_stop(self, perm_id, price):
        """Test hook: the stop fires at IBKR (between runs, or during one)."""
        s = self.stops[perm_id]
        s.status = "Filled"
        key = f"{s.symbol}:USD"
        self.holdings[key] = self.holdings.get(key, 0.0) - s.qty
        self.execs.append(ExecFill(f"x{perm_id}", perm_id, s.symbol, "USD", "SLD", s.qty, price,
                                   COMMISSION_USD, "USD", "t"))

    def new_day(self):
        """IBKR reqExecutions only reports the current day."""
        self.execs = []

    def stock(self, symbol, exchange="SMART", currency="USD"):
        return NS(symbol=symbol, exchange=exchange, currency=currency)

    def preview_order(self, contract, action, quantity, limit_price):
        return {"commission": COMMISSION_USD, "commission_currency": "USD"}

    # orders
    def place_order(self, contract, action, quantity, limit_price=None, wait_seconds=0):
        if self.fail_place_after is not None and len(self.orders) >= self.fail_place_after:
            raise ConnectionError("simulated: IB Gateway disconnected while placing orders")
        o = SimOrder(next(self._ids), contract.symbol, action, float(quantity), float(limit_price))
        now, late = self.fill(o)
        o.late_fill = late
        self._apply(o, now)
        if o.filled >= o.qty:
            o.status = "Filled"
        self.orders[o.oid] = o
        return self._result(o)

    def wait_for_orders(self, ids, timeout):
        pass

    def cancel_order(self, oid, wait_seconds=0):
        o = self.orders[oid]
        if o.status != "Filled":
            o.status = "Cancelled"
            self._apply(o, o.late_fill, reported=False)   # race: fills after the cancel was sent
        return self._result(o)

    def fill_details(self, oid):
        o = self.orders[oid]
        if o.filled <= 0:
            return 0.0, math.nan, 0.0, ""
        return o.filled, o.limit, COMMISSION_USD, "USD"

    def order_result(self, oid):
        return self._result(self.orders[oid])

    # helpers
    def _apply(self, o: SimOrder, shares: float, reported: bool = True):
        if shares <= 0:
            return
        key = f"{o.symbol}:USD"
        self.holdings[key] = self.holdings.get(key, 0.0) + (shares if o.action == "BUY" else -shares)
        if reported:
            o.filled += shares

    @staticmethod
    def _result(o: SimOrder) -> OrderResult:
        return OrderResult(o.oid, o.symbol, o.action, o.qty, "LMT", o.limit, o.status, o.filled,
                           o.limit if o.filled else math.nan, "")


# ------------------------------------------------------------------------------ market
class SimMarket:
    """Market data + the agent's MarketView. `news` is injected into price-history results."""

    def __init__(self, prices: dict[str, float] | None = None, open_: bool = True,
                 news: dict[str, str] | None = None):
        self.quotes = dict(PRICES if prices is None else prices)
        self.open_, self.news = open_, news or {}
        self.prices: dict[str, float] = {}
        self.fx: dict[str, float] = {}

    def fx_rates(self):
        return {"USD": USD_AUD}

    def fx_to_base(self, currency):
        return {"USD": USD_AUD, "AUD": 1.0}[currency]

    def price_all(self):
        return dict(self.quotes)

    def market_open(self, now):
        return self.open_

    def history_summary(self, key):
        px = self.quotes[key]
        out = {"last_close": px, "return_1m_pct": 2.0, "return_6m_pct": 8.0, "sma_20": px * 0.99, "sma_50": px * 0.97}
        if key in self.news:
            out["news"] = self.news[key]
        return out

    def commission_base(self, p: Proposal):
        return COMMISSION_USD * USD_AUD


# ------------------------------------------------------------------------------ Claude
def order(sym, qty, action="BUY", px=None, reason="eval", **record):
    """An order with a complete decision record (decision.example), overridable field by field."""
    return {"symbol": sym, "currency": "USD", "action": action, "quantity": qty,
            "limit_price": PRICES.get(f"{sym}:USD", 1.0) if px is None else px, "reason": reason,
            **example(action), **record}


class ScriptedClaude:
    """Plays a list of turns; each turn is a list of (tool, args). After the script: repeat the last turn."""

    def __init__(self, turns: list[list[tuple[str, dict]]], usage=None):
        self.turns, self.usage = turns, usage or NS(input_tokens=0, output_tokens=0)
        self.calls, self.messages = 0, self
        self._ids = itertools.count(1)

    def create(self, **kw):
        self.calls += 1
        self.last_request = kw
        turn = self.turns[min(self.calls - 1, len(self.turns) - 1)]
        blocks = [NS(type="tool_use", id=f"toolu_{next(self._ids)}", name=n, input=a) for n, a in turn]
        return NS(content=blocks, stop_reason="tool_use", usage=self.usage)


def submits(*orders_) -> ScriptedClaude:
    """Looks at the portfolio, then submits these orders (and resubmits them if bounced)."""
    return ScriptedClaude([[("get_portfolio", {})], [("submit_orders", {"orders": list(orders_), "summary": "eval"})]])


# ------------------------------------------------------------------------------ running a scenario
@dataclass
class Outcome:
    rc: int | None
    journal: Journal
    broker: SimBroker
    llm_calls: int
    logs: list[str] = field(default_factory=list)
    error: Exception | None = None

    @property
    def status(self) -> str:
        return self.journal.db.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]


class Bot:
    """A journal + simulated broker that persist across runs, like the real deployment."""

    def __init__(self, tmp: Path, cfg: RiskConfig = CFG, holdings: dict[str, tuple[float, float]] | None = None):
        """holdings: key -> (qty, cost price in USD), booked in both the journal and the broker."""
        self.cfg, self.tmp = cfg, tmp
        self.j = Journal(tmp / "journal.sqlite")
        self.j.init_budget(cfg.capital, cfg.base_currency)
        self.broker = SimBroker()
        for key, (qty, px) in (holdings or {}).items():
            self.j.record_fill(key.split(":")[0], "USD", "BUY", qty, px, COMMISSION_USD, USD_AUD)
            self.broker.holdings[key] = qty

    def run(self, client=None, market: SimMarket | None = None, *, dry_run=False, cost_fn=None,
            monthly_budget_usd=2.50, run_budget_usd=0.10, max_seconds=None, model="claude-sonnet-5",
            **run_kw) -> Outcome:
        client = client or ScriptedClaude([[("submit_orders", {"orders": [], "summary": "hold"})]])
        made = []

        def make_client(_m):
            made.append(client)
            return client
        llm = LLMSetup(make_client, "eval", model, cost_fn=cost_fn, run_budget_usd=run_budget_usd,
                       monthly_budget_usd=monthly_budget_usd, max_seconds=max_seconds)
        logs: list[str] = []
        rc, err = None, None
        try:
            rc = run_once(self.broker, market or SimMarket(), self.j, self.cfg, llm, mode="paper-eval",
                          dry_run=dry_run, fill_timeout=0, log=logs.append, runs_dir=self.tmp / "runs", **run_kw)
        except Exception as e:  # the pipeline re-raises after journaling the error
            err = e
        calls_before = getattr(client, "_calls_seen", 0)
        client._calls_seen = client.calls
        return Outcome(rc, self.j, self.broker, (client.calls - calls_before) if made else 0, logs, err)

    def reconciled(self) -> bool:
        """Journal holdings == broker holdings (the invariant every run starts by checking)."""
        mine = {k: q for k, q in self.j.holdings_qty().items() if abs(q) > 1e-9}
        theirs = {k: q for k, q in self.broker.holdings.items() if abs(q) > 1e-9}
        return mine == theirs
