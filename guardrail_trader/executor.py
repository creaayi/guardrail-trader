"""Executes APPROVED decisions and books fills. No decisions are made here."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

from guardrail_trader.broker import IBKRBroker
from guardrail_trader.decision import CODE_MOTIFS, EXIT_MOTIFS
from guardrail_trader.journal import Journal
from guardrail_trader.risk import Decision


DONE = ("Filled", "Cancelled", "ApiCancelled", "Inactive")


@dataclass
class ExecutionReport:
    symbol: str
    action: str
    requested: float
    filled: float
    avg_price: float
    commission: float
    status: str


def _motif(p, log: Callable[[str], None]) -> str:
    """Exit motif for closed_trades: Claude's validated motif_sortie, or 'kill' set by the kill switch.
    A fill is booked whatever happens (the money moved): an unexpected missing motif is flagged, not fatal."""
    motif = p.details.get("motif_sortie")
    if motif in EXIT_MOTIFS + CODE_MOTIFS:
        return motif
    log(f"[exec] WARNING SELL {p.symbol} has no valid exit motif ({motif!r}); booked as 'inconnu'")
    return "inconnu"


def execute(decisions: list[tuple[int, Decision]], broker: IBKRBroker, journal: Journal,
            fx: dict[str, float], fill_timeout: float = 120.0,
            log: Callable[[str], None] = print, stops=None) -> list[ExecutionReport]:
    """decisions: (proposal_row_id, approved Decision). Sells go first (already ordered by the gate).

    Orders are DAY limit orders. We wait up to `fill_timeout` seconds, cancel whatever is
    still open, then book only what actually filled into the virtual ledger.

    stops (StopManager): before each SELL its line's protective stop is cancelled or shrunk, so a stop and
    a sale can never sell the same shares; afterwards every holding gets a stop matching its size.
    """
    placed: list[tuple[int, Decision, int]] = []
    for pid, d in decisions:
        if not d.approved:  # explicit, not assert: asserts are stripped under python -O
            raise RuntimeError(f"executor received a blocked decision: {d.proposal}")
        p = d.proposal
        if stops is not None and p.action.upper() == "SELL":
            allowed = stops.before_sell(p.key, p.quantity)
            if allowed <= 1e-9:
                log(f"[exec] SELL {p.symbol} skipped: its stop already sold the shares")
                continue
            if allowed < p.quantity:
                p = replace(p, quantity=allowed)
                d = replace(d, proposal=p)
        contract = broker.stock(p.symbol, p.exchange, p.currency)
        r = broker.place_order(contract, p.action, p.quantity, limit_price=p.limit_price, wait_seconds=2)
        log(f"[exec] placed {p.action} {p.quantity:g} {p.symbol} @ {p.limit_price} -> id={r.order_id} {r.status}")
        placed.append((pid, d, r.order_id))

    broker.wait_for_orders([oid for *_, oid in placed], fill_timeout)

    reports, buy_prices = [], {}
    for pid, d, oid in placed:
        # No-op for orders already done; safe if an order filled a moment ago (race).
        status = broker.cancel_order(oid).status
        if status not in DONE:
            log(f"[exec] WARNING order {oid} still {status} after cancel; booking the fills known now. "
                f"If more fills arrive later, the next run's reconciliation stops and flags it.")
        qty, avg, comm, comm_ccy = broker.fill_details(oid)
        p = d.proposal
        if qty > 0:
            # commission is booked in the instrument currency; convert if IBKR reported another one
            comm_in_instr = comm * (fx.get(comm_ccy, 1.0) / fx[p.currency]) if comm_ccy and comm_ccy != p.currency else comm
            if p.action.upper() == "SELL":
                journal.book_sell(p.symbol, p.currency, qty, avg, comm_in_instr, fx[p.currency], _motif(p, log), oid,
                                  proposal_id=pid, limit_price=p.limit_price)
            else:
                journal.record_fill(p.symbol, p.currency, p.action, qty, avg, comm_in_instr, fx[p.currency], oid,
                                    proposal_id=pid, limit_price=p.limit_price)
                buy_prices[p.key] = avg
        journal.record_order(pid, broker.order_result(oid))
        log(f"[exec] {p.action} {p.symbol}: filled {qty:g}/{p.quantity:g} @ {avg if not math.isnan(avg) else '-'}"
            f" commission {comm:.2f} {comm_ccy} -> {status}")
        reports.append(ExecutionReport(p.symbol, p.action, p.quantity, qty, avg, comm, status))
    if stops is not None:
        stops.ensure_all(buy_prices)     # new lines get their stop; partial sells leave a right-sized one
    return reports
