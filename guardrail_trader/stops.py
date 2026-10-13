"""Protective stops at IBKR (CLAUDE.md §5, T5). Code decides; Claude never sees an order path here.

Pure rules:
  initial_stop()  entry - stop_atr_mult x ATR14, bounded to [entry x (1 - max%), entry x (1 - min%)]
  trailed_stop()  raised to entry once the highest close since entry >= entry + breakeven_atr_mult x ATR14,
                  then trail_atr_mult x ATR14 below that highest close; never lowered

StopManager keeps exactly one active SELL STP GTC per held line, the same quantity as held:
  sync()         book stop executions IBKR reports (before reconciliation); a stop gone from IBKR with
                 shares missing and no execution found stops the run (a human books it)
  ensure_all()   place / resize / cancel stops so each holding has one; new lines get initial_stop()
  before_sell()  cancel or shrink the stop BEFORE a SELL leaves, so stop + sell can never oversell;
                 if the stop filled meanwhile, the sell shrinks to what is still held
  trail()        raise stops from the bar cache (never lower)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from guardrail_trader.bars import Bar
from guardrail_trader.momentum import atr as atr14
from guardrail_trader.risk import RiskConfig, instrument_key

EPS = 1e-9


class StopSyncError(RuntimeError):
    """IBKR and the journal disagree about a stop in a way code must not guess about. Needs a human."""


def initial_stop(entry: float, atr: float | None, cfg: RiskConfig) -> tuple[float, str]:
    """(stop price, note). Without an ATR the widest allowed stop is used and the note says so."""
    lo, hi = entry * (1 - cfg.stop_max_pct / 100), entry * (1 - cfg.stop_min_pct / 100)
    if atr is None or not atr > 0:
        return lo, f"ATR14 unavailable: stop at the -{cfg.stop_max_pct:g}% bound"
    raw = entry - cfg.stop_atr_mult * atr
    return min(max(raw, lo), hi), f"entry - {cfg.stop_atr_mult:g} x ATR14 ({atr:.4g}), bounded"


def trailed_stop(entry: float, current: float, highest_close: float, atr: float | None, cfg: RiskConfig) -> float:
    """Never below `current`. Without an ATR nothing moves."""
    if atr is None or not atr > 0 or highest_close < entry + cfg.breakeven_atr_mult * atr - EPS:
        return current
    return max(current, entry, highest_close - cfg.trail_atr_mult * atr)


def highest_close_since(bars: list[Bar], entry_date: str | None) -> float | None:
    since = [b.close for b in bars if entry_date is None or b.date >= entry_date[:10]]
    return max(since) if since else None


@dataclass
class StopManager:
    broker: object
    journal: object
    cfg: RiskConfig
    fx_to_base: Callable[[str], float]          # currency -> base units per 1
    bars_for: Callable[[str], list[Bar]] = lambda _k: []
    log: Callable[[str], None] = print
    dry_run: bool = False

    # ---------------------------------------------------------------- helpers
    def _contract(self, key: str):
        inst = self.cfg.universe.get(key) or self.cfg.hedge.get(key)
        sym, ccy = key.split(":")
        return self.broker.stock(sym, inst.exchange if inst else "SMART", ccy)

    def _atr(self, key: str) -> float | None:
        bars = self.bars_for(key)
        return atr14(bars[-30:]) if len(bars) >= 15 else None

    def _book(self, f) -> bool:
        if f.commission != f.commission:                                       # NaN: never book a guess
            raise StopSyncError(f"stop execution {f.exec_id} on {f.symbol} has no commission report yet")
        fx_instr = self.fx_to_base(f.currency)
        comm = f.commission
        ccy = f.commission_currency or f.currency
        comm_instr = comm * self.fx_to_base(ccy) / fx_instr if ccy != f.currency else comm
        new = self.journal.book_stop_fill(f.exec_id, f.perm_id, f.symbol, f.currency, f.shares, f.price,
                                          comm, ccy, comm_instr, fx_instr)
        if new:
            self.log(f"[stops] STOP FILLED {f.shares:g} {f.symbol} @ {f.price} (permId {f.perm_id}); booked")
        return new

    def _book_executions(self, perm_ids: set[int]) -> None:
        for f in self.broker.executions(perm_ids):
            if f.side == "SLD":
                self._book(f)

    # ---------------------------------------------------------------- before reconciliation
    def sync(self, ibkr_positions: dict[str, float]) -> None:
        """Book stop fills since the last run; settle the status of every active stop."""
        active = self.journal.active_stops()
        if not active:
            return
        self._book_executions({s["perm_id"] for s in active})
        open_ids = {getattr(o, "perm_id", None) for o in self.broker.all_open_orders()}   # strays: reconciliation
        mine = self.journal.holdings_qty()
        for s in active:
            if s["perm_id"] in open_ids:
                continue
            key = instrument_key(s["symbol"], s["currency"])
            if self.journal.stop_filled_qty(s["perm_id"]) >= s["quantity"] - EPS:
                self.journal.update_stop(s["perm_id"], "executed at IBKR", status="filled")
            elif ibkr_positions.get(key, 0.0) < mine.get(key, 0.0) - EPS:
                raise StopSyncError(
                    f"stop permId {s['perm_id']} on {key} is gone from IBKR and IBKR holds "
                    f"{ibkr_positions.get(key, 0.0):g} vs journal {mine.get(key, 0.0):g}, but IBKR no longer reports "
                    "the execution (it only reports the current day). Book it with "
                    "`journal_cli.py book-stop-fill` from the IBKR statement.")
            else:
                self.journal.update_stop(s["perm_id"], "not open at IBKR, shares still held", status="missing")
                self.log(f"[stops] ANOMALY: stop permId {s['perm_id']} on {key} vanished without a fill; "
                         "a new one will be placed")

    # ---------------------------------------------------------------- after reconciliation / trades
    def ensure_all(self, buy_prices: dict[str, float] | None = None) -> None:
        """One active stop per held line, sized to the holding. buy_prices: key -> this run's fill price
        (a top-up's stop is the higher of the current stop and the new tranche's initial stop)."""
        buy_prices = buy_prices or {}
        held = self.journal.holdings_qty()
        for key, qty in held.items():
            s = self.journal.active_stop(key)
            entry_ts, entry_px = self.journal.position_entry(key)
            if s is None:
                price, note = initial_stop(buy_prices.get(key, entry_px), self._atr(key), self.cfg)
                if self.dry_run:
                    self.log(f"[stops] dry run: would place STP {qty:g} {key} @ {price:.4f} ({note})")
                    continue
                o = self.broker.place_stop(self._contract(key), qty, price)
                self.journal.add_stop(*key.split(":"), o.perm_id, o.order_id, qty, o.stop_price, entry_px,
                                      entry_ts, self._atr(key), note)
                self.log(f"[stops] placed STP {qty:g} {key} @ {o.stop_price} permId {o.perm_id} ({note})")
                continue
            new_price = None
            if key in buy_prices:
                p, _ = initial_stop(buy_prices[key], self._atr(key), self.cfg)
                new_price = p if p > s["stop_price"] + EPS else None
            if abs(s["quantity"] - qty) > EPS or new_price:
                if self.dry_run:
                    continue
                status, sent = self.broker.modify_stop(s["perm_id"], stop_price=new_price, quantity=qty,
                                                       never_below=s["stop_price"])
                if status in ("Filled", "NotOpen"):
                    raise StopSyncError(f"stop permId {s['perm_id']} on {key} {status} while being resized")
                raised = sent if new_price and sent > s["stop_price"] + EPS else None
                self.journal.update_stop(s["perm_id"], "resized to holding", stop_price=raised, quantity=qty)
                self.log(f"[stops] resized {key} stop to {qty:g}" + (f" @ {raised}" if raised else ""))
        for s in self.journal.active_stops():
            key = instrument_key(s["symbol"], s["currency"])
            if key not in held and not self.dry_run:
                status = self.broker.cancel_stop(s["perm_id"])
                if status == "Filled":
                    raise StopSyncError(f"stop permId {s['perm_id']} on {key} filled with no holding in the journal")
                self.journal.update_stop(s["perm_id"], "line closed", status="cancelled")

    def before_sell(self, key: str, quantity: float) -> float:
        """Cancel (full sale) or shrink (partial) the stop BEFORE the SELL is sent. Returns the quantity that
        may still be sold: less if the stop triggered first (its fill is booked here)."""
        s = self.journal.active_stop(key)
        if s is None or self.dry_run:
            return quantity
        perm, held = s["perm_id"], self.journal.holdings_qty().get(key, 0.0)
        full = quantity >= held - EPS
        if full:
            status = self.broker.cancel_stop(perm)
        else:
            status, _ = self.broker.modify_stop(perm, quantity=held - quantity)
        self._book_executions({perm})            # whatever the stop executed so far, booked exactly once
        filled = self.journal.stop_filled_qty(perm)
        if filled >= s["quantity"] - EPS:
            self.journal.update_stop(perm, "executed at IBKR before the sale", status="filled")
        elif filled > EPS:
            raise StopSyncError(f"stop permId {perm} on {key} partly executed ({filled:g}/{s['quantity']:g}) "
                                "while the sale was being prepared")
        elif status == "NotOpen":
            self.journal.update_stop(perm, "not open at IBKR before the sale", status="missing")
        elif full:
            self.journal.update_stop(perm, "cancelled before a full sale", status="cancelled")
        else:
            self.journal.update_stop(perm, "shrunk before a partial sale", quantity=held - quantity)
        left = self.journal.holdings_qty().get(key, 0.0)
        if left < quantity - EPS:
            self.log(f"[stops] {key}: stop triggered before the sale; selling {max(left, 0):g} instead of {quantity:g}")
        return max(min(quantity, left), 0.0)

    # ---------------------------------------------------------------- trailing
    def trail(self, today: date, max_age_days: int = 5) -> None:
        for s in self.journal.active_stops():
            key = instrument_key(s["symbol"], s["currency"])
            bars = self.bars_for(key)
            if not bars or (today - date.fromisoformat(bars[-1].date)).days > max_age_days:
                self.log(f"[stops] {key}: no recent bars; stop left at {s['stop_price']}")
                continue
            high = highest_close_since(bars, s["entry_date"])
            if high is None or s["entry_price"] is None:
                continue
            a = self._atr(key)
            new = trailed_stop(s["entry_price"], s["stop_price"], high, a, self.cfg)
            if new <= s["stop_price"] + EPS:
                continue
            reason = "breakeven" if s["entry_price"] >= high - self.cfg.trail_atr_mult * a else "trail"
            if self.dry_run:
                self.log(f"[stops] dry run: would raise {key} stop {s['stop_price']} -> {new:.4f} ({reason})")
                continue
            status, sent = self.broker.modify_stop(s["perm_id"], stop_price=new, never_below=s["stop_price"])
            if status in ("Filled", "NotOpen"):
                continue                       # settled by the next sync (booked, or flagged as missing)
            if sent > s["stop_price"] + EPS:
                self.journal.update_stop(s["perm_id"], reason, stop_price=sent)
                self.log(f"[stops] raised {key} stop {s['stop_price']} -> {sent} ({reason}; highest close {high})")
