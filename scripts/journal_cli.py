"""Operator CLI for the journal (operator only - the bot never calls these).

  .venv/bin/python scripts/journal_cli.py init                 # deposit budget from config/risk.toml (once;
                                                               #   needs IB Gateway: the account must cover it,
                                                               #   and the benchmark IWDA buys its virtual units)
  .venv/bin/python scripts/journal_cli.py topup [--confirm]    # deposit up to `capital` in config/risk.toml (dry run without --confirm)
  .venv/bin/python scripts/journal_cli.py status               # cash, holdings, halt state, vs IWDA, recent activity
  .venv/bin/python scripts/journal_cli.py reset-halt --confirm # re-arm after a kill switch
  .venv/bin/python scripts/journal_cli.py stops                # active protective stops
  .venv/bin/python scripts/journal_cli.py book-stop-fill --perm-id P --shares N --price X --commission C \
        --fx F [--commission-fx G] --confirm
      Book a stop execution IBKR no longer reports (it only reports the current day), from the IBKR
      statement: price and commission as charged, F = base-currency units per 1 instrument-currency unit,
      G = base units per 1 commission-currency unit (default: commission in the instrument currency).
"""
from __future__ import annotations

import argparse
import json
import sys

from guardrail_trader.broker import BrokerSafetyError, IBKRBroker, check_capital
from guardrail_trader.config import load_ibkr_config
from guardrail_trader.journal import Journal
from guardrail_trader.market import price_instruments
from guardrail_trader.metrics import BENCHMARK_KEY, from_journal
from guardrail_trader.risk import load_risk_config


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    t = sub.add_parser("topup")
    t.add_argument("--confirm", action="store_true")
    r = sub.add_parser("reset-halt")
    r.add_argument("--confirm", action="store_true")
    sub.add_parser("stops")
    b = sub.add_parser("book-stop-fill")
    b.add_argument("--perm-id", type=int, required=True)
    b.add_argument("--shares", type=float, required=True)
    b.add_argument("--price", type=float, required=True)
    b.add_argument("--commission", type=float, required=True)
    b.add_argument("--commission-currency", default=None)
    b.add_argument("--fx", type=float, required=True, help="base units per 1 unit of the instrument currency")
    b.add_argument("--commission-fx", type=float, default=None, help="base units per 1 commission-currency unit")
    b.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    cfg = load_risk_config()
    j = Journal()

    if args.cmd == "init":
        # Refuse a virtual budget the paper account can't pay for (read-only connection, no orders).
        # The benchmark is 100% invested on day 1 (CLAUDE.md §1): no IWDA price, no start.
        bench = cfg.hedge.get(BENCHMARK_KEY) or cfg.universe.get(BENCHMARK_KEY)
        if bench is None:
            print(f"Refusing to initialise: benchmark {BENCHMARK_KEY} is not in the hedge or universe files.")
            return 2
        try:
            with IBKRBroker(load_ibkr_config(), readonly=True) as broker:
                snap = broker.ensure_ready()
                value = check_capital(snap.net_liquidation, broker.fx_to_base(snap.currency, cfg.base_currency),
                                      cfg.capital, cfg.base_currency)
                bench_px = price_instruments(broker, [bench]).get(BENCHMARK_KEY)
                bench_fx = broker.fx_to_base(bench.currency, cfg.base_currency)
        except BrokerSafetyError as e:
            print(f"Refusing to initialise: {e}")
            return 2
        if not bench_px:
            print(f"Refusing to initialise: no price for the benchmark {BENCHMARK_KEY}; retry when IBKR quotes it.")
            return 2
        acct = snap.account_id
        print(f"Account {acct[:2]}•••{acct[-2:]}: {value:,.2f} {cfg.base_currency} covers capital {cfg.capital:,.2f}.")
        j.init_budget(cfg.capital, cfg.base_currency)
        print(f"Deposited {cfg.capital:,.2f} {cfg.base_currency} into the virtual ledger.")
        j.invest_benchmark(bench_px * bench_fx)
        nav = j.record_benchmark(bench_px * bench_fx)
        print(f"Benchmark {BENCHMARK_KEY}: {j.benchmark_units():,.4f} virtual units at {bench_px:,.2f} "
              f"{bench.currency} (NAV {nav:,.2f} {cfg.base_currency}).")

    elif args.cmd == "topup":
        total = j.total_deposits()
        if not j.base_currency():
            print("Budget not initialised; run `init` first.")
            return 2
        if cfg.base_currency.upper() != j.base_currency():
            print(f"config currency {cfg.base_currency} does not match the journal's {j.base_currency()}.")
            return 2
        if cfg.capital < total - 0.005:
            print(f"capital {cfg.capital:,.2f} is below the {total:,.2f} already deposited; "
                  "deposits are append-only (no withdrawals).")
            return 2
        delta = round(cfg.capital - total, 2)
        if delta < 0.01:
            print(f"Nothing to do: {total:,.2f} {cfg.base_currency} already deposited (capital = {cfg.capital:,.2f}).")
            return 0
        print(f"deposited so far: {total:,.2f}   capital in config: {cfg.capital:,.2f}   top-up: {delta:,.2f} {cfg.base_currency}")
        if not args.confirm:
            print("Dry run. Re-run with --confirm to deposit it.")
            return 0
        done = j.topup_to(cfg.capital, cfg.base_currency)
        print(f"Deposited {done:,.2f} {cfg.base_currency}. Cash is now {j.cash_base():,.2f}; "
              f"total deposited {j.total_deposits():,.2f}.")

    elif args.cmd == "status":
        base = j.base_currency() or "(not initialised)"
        print(f"base currency : {base}")
        print(f"cash          : {j.cash_base():,.2f}")
        print(f"peak value    : {j.peak():,.2f}")
        print(f"halted        : {j.is_halted()}  {j._get('halted_reason', '') or ''}")
        print(f"orders/month  : {j.orders_this_month()} of {cfg.max_trades_per_month}")
        print(f"allowlist     : {len(cfg.universe)} instruments")
        print(f"LLM spend     : US${j.llm_spend_usd():.2f} this month, US${j.llm_spend_usd('all'):.2f} lifetime")
        h = j.holdings_qty()
        print(f"holdings      : {h or 'none'}")
        perf = from_journal(j)
        if perf.days:
            fmt = lambda v, f="{:+.2f}%": "n/a" if v is None else f.format(v)  # noqa: E731
            print(f"vs {BENCHMARK_KEY}   : {perf.first_date} -> {perf.last_date} ({perf.days} days) "
                  f"bot {fmt(perf.return_pct)} benchmark {fmt(perf.benchmark_return_pct)} "
                  f"excess {fmt(perf.excess_pct)} alpha {fmt(perf.alpha_pct)} beta {fmt(perf.beta, '{:.2f}')}")
        else:
            print(f"vs {BENCHMARK_KEY}   : not enough daily values yet")
        print("\nlast 10 proposals:")
        for row in j.db.execute("SELECT ts, action, quantity, symbol, currency, limit_price, approved, block_reasons, reason "
                                "FROM proposals ORDER BY id DESC LIMIT 10"):
            verdict = "APPROVED" if row["approved"] else f"BLOCKED {json.loads(row['block_reasons'])}"
            print(f"  {row['ts']} {row['action']} {row['quantity']:g} {row['symbol']}:{row['currency']} "
                  f"@ {row['limit_price']} -> {verdict}\n      why: {row['reason']}")

    elif args.cmd == "reset-halt":
        if not args.confirm:
            print("Refusing without --confirm. This re-enables trading after a kill switch.")
            return 2
        # Without live prices we can't value holdings, so only re-arm when flat (all cash).
        if j.holdings_qty():
            print("Holdings still present; liquidate first so the new peak is unambiguous.")
            return 2
        j.reset_halt(j.cash_base())
        print(f"Kill switch reset. New peak = {j.cash_base():,.2f}. Trading re-enabled.")

    elif args.cmd == "stops":
        for s in j.db.execute("SELECT * FROM stops WHERE status IN ('active','missing') ORDER BY id"):
            print(f"  {s['status']:<7} permId {s['perm_id']} {s['quantity']:g} {s['symbol']}:{s['currency']} "
                  f"stop {s['stop_price']} entry {s['entry_price']} since {s['entry_date']}")

    elif args.cmd == "book-stop-fill":
        s = j.db.execute("SELECT * FROM stops WHERE perm_id=?", (args.perm_id,)).fetchone()
        if s is None:
            print(f"No stop with permId {args.perm_id} in the journal.")
            return 2
        ccy = s["currency"]
        cc = (args.commission_currency or ccy).upper()
        if cc != ccy and args.commission_fx is None:
            print("--commission-fx is required when the commission currency differs from the instrument's.")
            return 2
        comm_instr = args.commission if cc == ccy else args.commission * args.commission_fx / args.fx
        print(f"book SELL {args.shares:g} {s['symbol']}:{ccy} @ {args.price} commission {args.commission} {cc} "
              f"(fx {args.fx}) for stop permId {args.perm_id} -> closed trade 'stop'")
        if not args.confirm:
            print("Dry run. Re-run with --confirm to book it.")
            return 0
        j.book_stop_fill(f"manual-{args.perm_id}-{j.stop_filled_qty(args.perm_id):g}", args.perm_id, s["symbol"],
                         ccy, args.shares, args.price, args.commission, cc, comm_instr, args.fx)
        if j.stop_filled_qty(args.perm_id) >= s["quantity"] - 1e-9:
            j.update_stop(args.perm_id, "booked by the operator", status="filled")
        print("Booked. The next run reconciles again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
