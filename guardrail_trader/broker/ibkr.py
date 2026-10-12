"""Interactive Brokers adapter (via ib_async -> TWS / IB Gateway).

This is the ONLY module that talks to the broker. It executes; it never decides.
The risk gate (next step) sits in front of place_order().

Safety layers in this module (defence in depth):
  1. Endpoint check - paper mode refuses IBKR's live ports (and vice versa).
  2. Account check  - paper account IDs start with "DU"; mode and account must agree,
                      otherwise we disconnect immediately.
  3. Read-only mode - IBKRBroker(readonly=True) cannot place orders, locally or at the API.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ib_async import IB, Contract, ExecutionFilter, LimitOrder, MarketOrder, Stock, StopOrder

from guardrail_trader.config import LIVE_PORTS, MARKET_DATA_TYPES, PAPER_PORTS, IBKRConfig

UNSET_DOUBLE = 1.7976931348623157e308  # IBKR's "no value" sentinel


class MarketDataUnavailableError(ValueError):
    """The gateway serves account data but no prices - e.g. IBKR error 10197 (the session lost
    its market-data line to a competing session). Transient: restarting the gateway fixes it."""


class BrokerNotReadyError(RuntimeError):
    """Connected to IB Gateway, but it isn't serving account data (yet). Transient: retry."""


class BrokerSafetyError(RuntimeError):
    """Raised when a safety check fails. Never catch-and-continue on this."""


# ---------------------------------------------------------------------------
# Pure safety checks (unit-testable without a broker)
# ---------------------------------------------------------------------------
def check_endpoint(mode: str, port: int) -> None:
    if mode == "paper" and port in LIVE_PORTS:
        raise BrokerSafetyError(f"TRADING_MODE=paper but IBKR_PORT={port} is a LIVE port {sorted(LIVE_PORTS)}.")
    if mode == "live" and port in PAPER_PORTS:
        raise BrokerSafetyError(f"TRADING_MODE=live but IBKR_PORT={port} is a PAPER port {sorted(PAPER_PORTS)}.")


def check_account(mode: str, account_id: str) -> None:
    is_paper = account_id.upper().startswith("DU")
    if mode == "paper" and not is_paper:
        raise BrokerSafetyError(
            f"TRADING_MODE=paper but account {account_id} is not a paper account (paper IDs start with 'DU')."
        )
    if mode == "live" and is_paper:
        raise BrokerSafetyError(f"TRADING_MODE=live but account {account_id} is a paper account.")


def check_capital(net_liquidation: float, fx_to_base: float, required_base: float, base: str) -> float:
    """The broker account, converted to the base currency, must hold at least `required_base`:
    the capital at `journal_cli.py init`, the bot's current virtual value at every run.

    The paper account is capped (1 M USD), so a virtual budget larger than what the account can pay
    for would let the bot "spend" money that isn't there. Returns the account value in base currency.
    """
    value = net_liquidation * fx_to_base
    if not (math.isfinite(value) and value >= required_base):   # NaN-safe: unknown value fails closed
        raise BrokerSafetyError(
            f"account net liquidation {value:,.2f} {base} is below the required {required_base:,.2f} {base}.")
    return value


def stock_contract(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
    """US: SMART. Europe: SMART routing with the primary exchange named (SBF, AEB, IBIS, LSE, EBS),
    which also disambiguates a ticker listed on several exchanges."""
    exchange = (exchange or "SMART").upper()
    if exchange == "SMART":
        return Stock(symbol.upper(), "SMART", currency.upper())
    return Stock(symbol.upper(), "SMART", currency.upper(), primaryExchange=exchange)


def to_major(price: float, magnifier: int) -> float:
    """IBKR quote units -> currency units. LSE stocks quote in pence (GBX, priceMagnifier 100)."""
    return price / magnifier if magnifier and magnifier > 1 else price


def to_quote_units(price: float, magnifier: int) -> float:
    """Currency units -> IBKR quote units, for limit and stop prices sent to IBKR."""
    return round(price * magnifier, 6) if magnifier and magnifier > 1 else price


def round_to_tick(price: float, tick: float, down: bool) -> float:
    """Round a price (IBKR quote units) to the exchange's tick, down or up. tick <= 0: unchanged."""
    if not tick or tick <= 0 or not math.isfinite(price):
        return price
    steps = price / tick
    steps = math.floor(steps + 1e-9) if down else math.ceil(steps - 1e-9)
    return round(steps * tick, 10)


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return math.nan if v >= UNSET_DOUBLE or v == -1 else v


# ---------------------------------------------------------------------------
# Plain data objects returned to the rest of the system (no ib_async types leak out)
# ---------------------------------------------------------------------------
@dataclass
class AccountSnapshot:
    account_id: str
    currency: str
    net_liquidation: float
    cash: float
    available_funds: float


@dataclass
class Position:
    symbol: str
    exchange: str
    currency: str
    quantity: float
    avg_cost: float


@dataclass
class Quote:
    symbol: str
    currency: str
    price: float  # best available: last/mid, else previous close
    bid: float
    ask: float
    close: float
    data_type: str  # live | frozen | delayed | delayed-frozen


@dataclass
class OrderResult:
    order_id: int
    symbol: str
    action: str
    quantity: float
    order_type: str
    limit_price: float | None
    status: str
    filled: float
    avg_fill_price: float
    message: str = ""


@dataclass
class OpenOrder:
    """Any order open at IBKR for this account, from any client (reconciliation)."""
    perm_id: int
    order_id: int
    client_id: int
    symbol: str
    currency: str
    action: str
    order_type: str
    quantity: float
    stop_price: float | None   # currency units, STP only


@dataclass
class ExecFill:
    """One execution of an order, for booking stop fills that happened between runs."""
    exec_id: str
    perm_id: int
    symbol: str
    currency: str
    side: str          # "SLD" | "BOT"
    shares: float
    price: float       # currency units
    commission: float
    commission_currency: str
    time: str


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------
class IBKRBroker:
    def __init__(self, cfg: IBKRConfig, readonly: bool = False):
        self.cfg = cfg
        self.readonly = readonly
        self.ib = IB()
        self.account_id: str | None = None
        self._magnifiers: dict[int, int] = {}   # conId -> priceMagnifier (GBX = 100)
        self._details: dict[int, object] = {}   # conId -> ContractDetails
        self._rules: dict[int, list] = {}       # marketRuleId -> price increments

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> "IBKRBroker":
        check_endpoint(self.cfg.mode, self.cfg.port)  # before any network I/O
        self.ib.connect(
            self.cfg.host, self.cfg.port, clientId=self.cfg.client_id,
            readonly=self.readonly, timeout=15,
        )
        accounts = self.ib.managedAccounts()
        try:
            if len(accounts) != 1:
                raise BrokerSafetyError(f"Expected exactly one account, got {accounts}.")
            check_account(self.cfg.mode, accounts[0])
        except BrokerSafetyError:
            self.disconnect()
            raise
        self.account_id = accounts[0]
        self.ib.reqMarketDataType(self.cfg.market_data_type)
        return self

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self) -> "IBKRBroker":
        # Idempotent: `with IBKRBroker(cfg).connect() as b:` must not open a 2nd socket
        # (IBKR drops a duplicate clientId connection).
        return self if self.ib.isConnected() else self.connect()

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # -- reads --------------------------------------------------------------
    def account_snapshot(self) -> AccountSnapshot:
        vals = {v.tag: v for v in self.ib.accountSummary(self.account_id)}
        currency = vals["NetLiquidation"].currency if "NetLiquidation" in vals else "?"
        get = lambda tag: _num(vals[tag].value) if tag in vals else math.nan  # noqa: E731
        return AccountSnapshot(
            account_id=self.account_id,
            currency=currency,
            net_liquidation=get("NetLiquidation"),
            cash=get("TotalCashValue"),
            available_funds=get("AvailableFunds"),
        )

    def ensure_ready(self) -> AccountSnapshot:
        """Confirm the gateway is actually serving data, not just accepting the socket.

        After IBKR's nightly reset the gateway can be connected while every request times out:
        the account summary comes back empty and positions come back as an empty list. An empty
        list then looks exactly like "this account holds nothing", which is how a healthy
        portfolio once got reported as a reconciliation mismatch. Checked before any decision.
        """
        snap = self.account_snapshot()
        if not snap.net_liquidation > 0 or snap.currency == "?":     # NaN-safe
            raise BrokerNotReadyError(
                f"IB Gateway is connected but not serving account data for "
                f"{self.account_id or '(no account)'} (account summary empty). This is usually "
                f"IBKR's nightly re-login; the run should be retried shortly.")
        return snap

    def positions(self) -> list[Position]:
        return [
            Position(
                symbol=p.contract.symbol,
                exchange=p.contract.primaryExchange or p.contract.exchange,
                currency=p.contract.currency,
                quantity=float(p.position),
                avg_cost=float(p.avgCost),
            )
            for p in self.ib.positions(self.account_id)
        ]

    def stock(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Contract:
        """Build and qualify a stock/ETF contract. e.g. ('AAPL','SMART','USD'), ('SAP','IBIS','EUR')."""
        contract = stock_contract(symbol, exchange, currency)
        qualified = [c for c in self.ib.qualifyContracts(contract) if c and c.conId]
        if not qualified:
            raise ValueError(f"Could not find contract {symbol} on {exchange} in {currency}.")
        return qualified[0]

    def magnifier(self, contract: Contract) -> int:
        """IBKR's priceMagnifier for a qualified contract (100 for LSE stocks quoted in pence).

        Only GBP contracts are looked up (one contract-details request each, cached by conId); the
        universe has no other currency quoted in minor units. Every price crossing this adapter is
        converted, so the rest of the system only sees currency units (GBP, not pence).
        """
        if (getattr(contract, "secType", "STK") != "STK" or getattr(contract, "currency", "") != "GBP"
                or not getattr(contract, "conId", 0)):
            return 1   # FX pairs (e.g. EURGBP) and non-GBP stocks quote in currency units
        if contract.conId not in self._magnifiers:
            details = self.ib.reqContractDetails(contract)
            if not details:
                raise ValueError(f"No contract details for {contract.symbol}; price unit unknown.")
            self._magnifiers[contract.conId] = int(details[0].priceMagnifier or 1)
        return self._magnifiers[contract.conId]

    def tick_size(self, contract: Contract, quote_price: float) -> float:
        """Price increment at this price (IBKR quote units). European exchanges use price-band tick
        tables (MiFID II), so the market rule for the SMART route is used, else minTick, else 0."""
        if not getattr(contract, "conId", 0):
            return 0.0
        if contract.conId not in self._details:
            details = self.ib.reqContractDetails(contract)
            self._details[contract.conId] = details[0] if details else None
        d = self._details[contract.conId]
        if d is None:
            return 0.0
        rule_ids = [r for r in str(getattr(d, "marketRuleIds", "") or "").split(",") if r]
        exchanges = str(getattr(d, "validExchanges", "") or "").split(",")
        if rule_ids:
            rid = int(rule_ids[exchanges.index("SMART")] if "SMART" in exchanges and
                      exchanges.index("SMART") < len(rule_ids) else rule_ids[0])
            if rid not in self._rules:
                self._rules[rid] = sorted(self.ib.reqMarketRule(rid) or [], key=lambda x: x.lowEdge)
            steps = [x.increment for x in self._rules[rid] if x.lowEdge <= quote_price + 1e-12]
            if steps:
                return float(steps[-1])
        return float(getattr(d, "minTick", 0.0) or 0.0)

    def quote(self, contract: Contract, wait_seconds: float = 5.0) -> Quote:
        ticker = self.ib.reqMktData(contract)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            self.ib.sleep(0.25)
            if not math.isnan(_num(ticker.marketPrice())) or not math.isnan(_num(ticker.close)):
                break
        self.ib.cancelMktData(contract)

        mag = self.magnifier(contract)
        price = to_major(_num(ticker.marketPrice()), mag)
        close = to_major(_num(ticker.close), mag)
        return Quote(
            symbol=contract.symbol,
            currency=contract.currency,
            price=price if not math.isnan(price) else close,
            bid=to_major(_num(ticker.bid), mag),
            ask=to_major(_num(ticker.ask), mag),
            close=close,
            data_type=MARKET_DATA_TYPES.get(ticker.marketDataType, "unknown"),
        )

    def fx_to_base(self, currency: str, base: str) -> float:
        """Base-currency units per 1 unit of `currency` (e.g. EUR per USD). IDEALPRO quote."""
        currency, base = currency.upper(), base.upper()
        if currency == base:
            return 1.0
        for pair, invert in ((base + currency, True), (currency + base, False)):
            try:
                contract = self.fx_contract(pair)
            except ValueError:
                continue
            q = self.quote(contract)
            if not math.isnan(q.price) and q.price > 0:
                return 1 / q.price if invert else q.price
        raise MarketDataUnavailableError(
            f"No FX rate available for {currency}->{base}: the gateway is connected but serving no "
            f"market data (IBKR error 10197 = the session lost its data line; restart the gateway).")

    def fx_contract(self, pair: str) -> Contract:
        from ib_async import Forex
        contract = Forex(pair)
        qualified = [c for c in self.ib.qualifyContracts(contract) if c and c.conId]
        if not qualified:
            raise ValueError(f"Unknown FX pair {pair}")
        return qualified[0]

    def open_orders(self) -> list[OrderResult]:
        return [self._to_result(t) for t in self.ib.openTrades()]

    def all_open_orders(self) -> list[OpenOrder]:
        """Every open order on the account, whichever client placed it (reqAllOpenOrders):
        reconciliation must see orders from other clients too, not only this session's."""
        out = []
        for t in self.ib.reqAllOpenOrders():
            if t.orderStatus.status in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
                continue
            o, mag = t.order, self.magnifier(t.contract)
            out.append(OpenOrder(int(o.permId), int(o.orderId), int(o.clientId), t.contract.symbol,
                                 t.contract.currency, o.action, o.orderType, float(o.totalQuantity),
                                 to_major(_num(o.auxPrice), mag) if o.orderType == "STP" else None))
        return out

    def executions(self, perm_ids: set[int]) -> list[ExecFill]:
        """Executions IBKR still reports (current day only) for these orders, with commissions."""
        out = []
        fills = [f for f in self.ib.reqExecutions(ExecutionFilter()) if int(f.execution.permId) in perm_ids]
        deadline = time.monotonic() + 10       # commission reports arrive after the executions
        while time.monotonic() < deadline and not all(f.commissionReport and f.commissionReport.execId for f in fills):
            self.ib.sleep(0.5)
        for f in fills:
            e = f.execution
            cr = f.commissionReport
            out.append(ExecFill(e.execId, int(e.permId), f.contract.symbol, f.contract.currency, e.side,
                                float(e.shares), to_major(float(e.price), self.magnifier(f.contract)),
                                _num(cr.commission) if cr and cr.execId else math.nan,
                                cr.currency if cr else "", str(e.time)))
        return out

    # -- protective stops (T5): SELL STP GTC, identified by permId ------------
    def _writable(self) -> None:
        if self.readonly:
            raise BrokerSafetyError("Broker is in read-only mode; orders are disabled.")
        if self.account_id is None:
            raise BrokerSafetyError("Not connected.")
        check_account(self.cfg.mode, self.account_id)

    def _stop_trade(self, perm_id: int):
        return next((t for t in self.ib.reqAllOpenOrders() if int(t.order.permId) == perm_id), None)

    def place_stop(self, contract: Contract, quantity: float, stop_price: float,
                   wait_seconds: float = 10.0) -> OpenOrder:
        """SELL STP GTC (regular hours). stop_price in currency units, rounded DOWN to the tick (a stop is
        never set tighter than computed). Waits for IBKR's permId, which identifies it across sessions."""
        self._writable()
        if not quantity > 0 or not stop_price > 0:
            raise ValueError(f"invalid stop {quantity} @ {stop_price}")
        mag = self.magnifier(contract)
        aux = to_quote_units(stop_price, mag)
        aux = round_to_tick(aux, self.tick_size(contract, aux), down=True)
        order = StopOrder("SELL", quantity, aux)
        order.account, order.tif, order.outsideRth = self.account_id, "GTC", False
        trade = self.ib.placeOrder(contract, order)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and not trade.order.permId and not trade.isDone():
            self.ib.sleep(0.5)
        if not trade.order.permId or trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
            raise RuntimeError(f"stop for {contract.symbol} not accepted by IBKR: {trade.orderStatus.status} "
                               f"{[e.message for e in trade.log if e.message][-1:]}")
        return OpenOrder(int(trade.order.permId), int(trade.order.orderId), int(trade.order.clientId),
                         contract.symbol, contract.currency, "SELL", "STP", float(quantity), to_major(aux, mag))

    def modify_stop(self, perm_id: int, stop_price: float | None = None, quantity: float | None = None,
                    never_below: float | None = None, wait_seconds: float = 10.0) -> tuple[str, float]:
        """Change a stop's price (rounded down to the tick) and/or quantity. Returns (status, stop price in
        currency units at IBKR). A price that would round below `never_below` is not sent (a stop is never
        lowered). 'Filled' / 'NotOpen' mean it triggered or vanished first: the caller books or flags it."""
        self._writable()
        trade = self._stop_trade(perm_id)
        if trade is None:
            return "NotOpen", math.nan
        o, mag = trade.order, self.magnifier(trade.contract)
        if stop_price is not None:
            aux = to_quote_units(stop_price, mag)
            aux = round_to_tick(aux, self.tick_size(trade.contract, aux), down=True)
            if never_below is not None and to_major(aux, mag) < never_below - 1e-9:
                stop_price = None          # rounding would lower it: keep the current price
            else:
                o.auxPrice = aux
        if stop_price is None and quantity is None:
            return trade.orderStatus.status, to_major(_num(o.auxPrice), mag)
        if quantity is not None:
            o.totalQuantity = quantity
        trade = self.ib.placeOrder(trade.contract, o)
        self.ib.sleep(min(wait_seconds, 2.0))
        return trade.orderStatus.status, to_major(_num(o.auxPrice), self.magnifier(trade.contract))

    def cancel_stop(self, perm_id: int, wait_seconds: float = 30.0) -> str:
        """Cancel and wait for a final state. 'Filled' (the stop triggered first) is a valid answer."""
        self._writable()
        trade = self._stop_trade(perm_id)
        if trade is None:
            return "NotOpen"
        if not trade.isDone():
            self.ib.cancelOrder(trade.order)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline and not trade.isDone():
                self.ib.sleep(0.5)
        return trade.orderStatus.status

    # -- writes (only reachable through the risk gate) -----------------------
    def _build_order(self, action: str, quantity: float, limit_price: float | None):
        action = action.upper()
        if action not in ("BUY", "SELL"):
            raise ValueError(f"action must be BUY or SELL, got {action!r}")
        if not quantity > 0:
            raise ValueError(f"quantity must be > 0, got {quantity}")
        if limit_price is not None and not limit_price > 0:
            raise ValueError(f"limit_price must be > 0, got {limit_price}")
        order = LimitOrder(action, quantity, limit_price) if limit_price else MarketOrder(action, quantity)
        order.account = self.account_id
        order.tif = "DAY"
        return order

    def preview_order(self, contract: Contract, action: str, quantity: float,
                      limit_price: float | None = None) -> dict:
        """IBKR 'what-if': estimated commission and margin impact, without placing anything.
        limit_price in currency units (GBP for LSE), converted to IBKR quote units here."""
        lmt = None if limit_price is None else to_quote_units(limit_price, self.magnifier(contract))
        state = self.ib.whatIfOrder(contract, self._build_order(action, quantity, lmt))
        if not hasattr(state, "commission"):  # IBKR rejected the what-if (ib_async returns [])
            raise ValueError(f"IBKR rejected what-if for {quantity} {contract.symbol} (see IBKR error above)")
        return {
            "commission": _num(state.commission),
            "min_commission": _num(state.minCommission),
            "max_commission": _num(state.maxCommission),
            "commission_currency": state.commissionCurrency,
            "equity_with_loan_after": _num(state.equityWithLoanAfter),
            "warning": state.warningText,
        }

    def place_order(self, contract: Contract, action: str, quantity: float,
                    limit_price: float | None = None, wait_seconds: float = 10.0) -> OrderResult:
        if self.readonly:
            raise BrokerSafetyError("Broker is in read-only mode; orders are disabled.")
        if self.account_id is None:
            raise BrokerSafetyError("Not connected.")
        check_account(self.cfg.mode, self.account_id)  # re-check at the last moment

        lmt = None
        if limit_price is not None:
            # Tick-conform, and never a worse price than the gate approved: BUY down, SELL up.
            lmt = to_quote_units(limit_price, self.magnifier(contract))
            lmt = round_to_tick(lmt, self.tick_size(contract, lmt), down=action.upper() == "BUY")
        trade = self.ib.placeOrder(contract, self._build_order(action, quantity, lmt))
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and not trade.isDone():
            self.ib.sleep(0.5)
            if trade.orderStatus.status in ("Submitted", "PreSubmitted") and limit_price:
                break  # a resting limit order is a valid outcome; don't block on it
        return self._to_result(trade)

    def cancel_order(self, order_id: int, wait_seconds: float = 30.0) -> OrderResult:
        """Cancel an order if it is still working, and wait for a final state.

        Race-safe: an order can fill (or be cancelled) between our last status check and this
        call. Then there is nothing to cancel, and we return its final state instead of raising.
        """
        trade = self._trade(order_id)
        if trade is None:
            raise ValueError(f"Unknown order id {order_id}.")
        if not trade.isDone():
            self.ib.cancelOrder(trade.order)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline and not trade.isDone():
                self.ib.sleep(0.5)
        return self._to_result(trade)

    def wait_for_orders(self, order_ids: list[int], timeout: float) -> None:
        """Block until all orders are done (filled/cancelled/rejected) or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            trades = [t for t in self.ib.trades() if t.order.orderId in order_ids]
            if trades and all(t.isDone() for t in trades):
                return
            self.ib.sleep(1.0)

    def fill_details(self, order_id: int, settle_seconds: float = 15.0) -> tuple[float, float, float, str]:
        """(filled_qty, avg_price, total_commission, commission_currency) for an order.

        Executions and their commission reports arrive as separate messages, so wait (up to
        settle_seconds) until the executions add up to the order's filled quantity and every
        execution has its commission report. Otherwise the ledger could book a fill short or
        with zero commission.
        """
        trade = self._trade(order_id)
        if trade is None:
            return 0.0, math.nan, 0.0, ""
        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline and not _settled(trade):
            self.ib.sleep(0.5)
        if not trade.fills:
            return 0.0, math.nan, 0.0, ""
        qty = sum(f.execution.shares for f in trade.fills)
        avg = to_major(sum(f.execution.shares * f.execution.price for f in trade.fills) / qty,
                       self.magnifier(trade.contract))
        comm = sum(_num(f.commissionReport.commission) for f in trade.fills
                   if f.commissionReport and not math.isnan(_num(f.commissionReport.commission)))
        ccy = next((f.commissionReport.currency for f in trade.fills if f.commissionReport), "")
        return float(qty), float(avg), float(comm), ccy

    def order_result(self, order_id: int) -> OrderResult:
        trade = self._trade(order_id)
        if trade is None:
            raise ValueError(f"Unknown order id {order_id}.")
        return self._to_result(trade)

    def _trade(self, order_id: int):
        return next((t for t in self.ib.trades() if t.order.orderId == order_id), None)

    # -- helpers ------------------------------------------------------------
    def _to_result(self, trade) -> OrderResult:
        o, s = trade.order, trade.orderStatus
        mag = self.magnifier(trade.contract)
        msgs = [e.message for e in trade.log if e.message]
        return OrderResult(
            order_id=o.orderId,
            symbol=trade.contract.symbol,
            action=o.action,
            quantity=float(o.totalQuantity),
            order_type=o.orderType,
            limit_price=to_major(_num(o.lmtPrice), mag) if o.orderType == "LMT" else None,
            status=s.status,
            filled=float(s.filled),
            avg_fill_price=to_major(_num(s.avgFillPrice), mag),
            message=msgs[-1] if msgs else "",
        )


def _settled(trade) -> bool:
    """Executions add up to the reported filled quantity and each has its commission report."""
    shares = sum(f.execution.shares for f in trade.fills)
    reported = _num(trade.orderStatus.filled)
    has_comm = all(getattr(f.commissionReport, "execId", "") for f in trade.fills)
    return has_comm and (math.isnan(reported) or abs(shares - reported) < 1e-9)
