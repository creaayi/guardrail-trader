"""One bot run: reconcile -> price -> kill switch -> Claude -> risk gate -> execute -> journal.

  .venv/bin/python scripts/run_bot.py --dry-run --fake-claude   # whole harness, no API key, no orders
  .venv/bin/python scripts/run_bot.py --dry-run                 # real Claude, gate verdicts, no orders
  .venv/bin/python scripts/run_bot.py                           # paper trading (market must be open)

The run itself is guardrail_trader.pipeline.run_once(); this file wires in IBKR and the LLM.
Exit codes: 0 ok/skipped, 1 error, 2 gateway unusable (transient), 3 needs a human (reconciliation / capital guard).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from guardrail_trader import llm
from guardrail_trader.broker import IBKRBroker
from guardrail_trader.config import PROJECT_ROOT, load_ibkr_config
from guardrail_trader.journal import Journal
from guardrail_trader.pipeline import IBKRMarketData, LLMSetup, run_once
from guardrail_trader.risk import load_risk_config


def log(msg: str) -> None:
    print(f"{datetime.now(ZoneInfo('Europe/Paris')):%H:%M:%S} {msg}", flush=True)


def llm_setup(fake: bool) -> LLMSetup:
    if fake:
        from guardrail_trader.fake_llm import FakeClaude
        return LLMSetup(lambda m: FakeClaude(price_fn=lambda s: m.prices[f"{s}:USD"]), "fake", "fake")
    return LLMSetup(lambda _m: llm.make_client(), llm.provider(), llm.model(), cost_fn=llm.cost_usd,
                    run_budget_usd=llm.run_budget_usd(), monthly_budget_usd=llm.monthly_budget_usd(),
                    request_timeout_s=llm.request_timeout_s())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no orders; read-only broker connection")
    ap.add_argument("--fake-claude", action="store_true", help="scripted agent, no API calls")
    ap.add_argument("--fill-timeout", type=float, default=120.0)
    args = ap.parse_args()

    cfg = load_risk_config()
    ib_cfg = load_ibkr_config()
    j = Journal()
    if j.base_currency() is None:
        log("Journal not initialised: run scripts/journal_cli.py init")
        return 1
    mode = ib_cfg.mode + ("-dry" if args.dry_run else "") + ("-fake" if args.fake_claude else "")
    picks = {r["symbol"] for r in csv.DictReader(open(PROJECT_ROOT / "config/universe/my_picks.csv"))}
    try:
        with IBKRBroker(ib_cfg, readonly=args.dry_run) as broker:
            return run_once(broker, IBKRMarketData(broker, cfg), j, cfg, llm_setup(args.fake_claude),
                            mode=mode, dry_run=args.dry_run, fill_timeout=args.fill_timeout, my_picks=picks,
                            web_search=os.getenv("ENABLE_WEB_SEARCH", "0") == "1", log=log)
    finally:
        j.close()


if __name__ == "__main__":
    sys.exit(main())
