"""Operator CLI for the journal (operator only - the bot never calls these).

  .venv/bin/python scripts/journal_cli.py init                 # deposit budget from config/risk.toml (once;
                                                               #   needs IB Gateway: the account must cover it)
  .venv/bin/python scripts/journal_cli.py topup [--confirm]    # deposit up to `capital` in config/risk.toml (dry run without --confirm)
  .venv/bin/python scripts/journal_cli.py status               # cash, holdings, halt state, recent activity
  .venv/bin/python scripts/journal_cli.py reset-halt --confirm # re-arm after a kill switch
"""
from __future__ import annotations

import argparse
import json
import sys

from guardrail_trader.broker import BrokerSafetyError, IBKRBroker, check_capital
from guardrail_trader.config import load_ibkr_config
from guardrail_trader.journal import Journal
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
    args = ap.parse_args()

    cfg = load_risk_config()
    j = Journal()

    if args.cmd == "init":
        # Refuse a virtual budget the paper account can't pay for (read-only connection, no orders).
        try:
            with IBKRBroker(load_ibkr_config(), readonly=True) as broker:
                snap = broker.ensure_ready()
                value = check_capital(snap.net_liquidation, broker.fx_to_base(snap.currency, cfg.base_currency),
                                      cfg.capital, cfg.base_currency)
        except BrokerSafetyError as e:
            print(f"Refusing to initialise: {e}")
            return 2
        print(f"Account {snap.account_id}: {value:,.2f} {cfg.base_currency} covers capital {cfg.capital:,.2f}.")
        j.init_budget(cfg.capital, cfg.base_currency)
        print(f"Deposited {cfg.capital:,.2f} {cfg.base_currency} into the virtual ledger.")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
