"""One bot run: reconcile -> price -> kill switch -> Claude -> risk gate -> execute -> journal.

  .venv/bin/python scripts/run_bot.py --dry-run --fake-claude   # whole harness, no API key, no orders
  .venv/bin/python scripts/run_bot.py --dry-run                 # real Claude, gate verdicts, no orders
  .venv/bin/python scripts/run_bot.py                           # paper trading (market must be open)
  .venv/bin/python scripts/run_bot.py --slot eu_open            # trade only that slot's markets (scheduler)

The run itself is guardrail_trader.pipeline.run_once(); this file wires in IBKR and the LLM.
Exit codes: 0 ok/skipped, 1 error, 2 gateway unusable (transient), 3 needs a human (reconciliation / capital guard).
Every run ends with an ntfy alert (guardrail_trader/alerts.py): a summary, plus a high-priority one on anomalies.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from guardrail_trader import llm
from guardrail_trader.alerts import Notifier, not_executed, run_alerts
from guardrail_trader.broker import IBKRBroker
from guardrail_trader.bars import BarCache
from guardrail_trader.config import PROJECT_ROOT, load_ibkr_config
from guardrail_trader.journal import Journal
from guardrail_trader.pipeline import IBKRMarketData, LLMSetup, run_once
from guardrail_trader.risk import load_momentum_config, load_risk_config
from guardrail_trader.slots import SLOTS


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
    ap.add_argument("--trail-stops", action="store_true", help="refresh held lines' bars and raise trailing stops")
    ap.add_argument("--stops-only", action="store_true", help="book stop fills, reconcile, place stops; no Claude")
    ap.add_argument("--slot", choices=sorted(SLOTS), help="decision slot: trade only its markets (default: all open)")
    args = ap.parse_args()

    cfg = load_risk_config()
    ib_cfg = load_ibkr_config()
    notifier = Notifier.from_env(log)
    j = Journal()
    if j.base_currency() is None:
        log("Journal not initialised: run scripts/journal_cli.py init")
        notifier.send(not_executed("journal not initialised (journal_cli.py init)"))
        return 1
    last_id = j.db.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]
    mode = ib_cfg.mode + ("-dry" if args.dry_run else "") + ("-fake" if args.fake_claude else "")
    picks_file = PROJECT_ROOT / "config/universe/my_picks.csv"   # optional owner picks (not used in phase 1)
    picks = {r["symbol"] for r in csv.DictReader(open(picks_file))} if picks_file.exists() else set()
    bars = BarCache()
    try:
        with IBKRBroker(ib_cfg, readonly=args.dry_run) as broker:
            return run_once(broker, IBKRMarketData(broker, cfg), j, cfg, llm_setup(args.fake_claude),
                            mode=mode, dry_run=args.dry_run, fill_timeout=args.fill_timeout, my_picks=picks,
                            web_search=os.getenv("ENABLE_WEB_SEARCH", "0") == "1", log=log,
                            bars=bars, momentum_cfg=load_momentum_config(),
                            trail_stops=args.trail_stops, stops_only=args.stops_only,
                            slot=SLOTS[args.slot] if args.slot else None)
    except Exception as e:
        if not j.db.execute("SELECT 1 FROM runs WHERE id > ?", (last_id,)).fetchone():
            notifier.send(not_executed(f"{args.slot or 'manual'}: {type(e).__name__}: {e}"))
        raise
    finally:
        try:
            send_run_alerts(j, last_id, notifier)
        finally:
            bars.close()
            j.close()


def send_run_alerts(j: Journal, last_id: int, notifier: Notifier) -> None:
    """Summary + anomaly alerts for the runs this process made. Never raises."""
    try:
        from guardrail_trader.metrics import from_journal
        ids = [r[0] for r in j.db.execute("SELECT id FROM runs WHERE id > ? ORDER BY id", (last_id,))]
        for a in run_alerts(j, ids, from_journal(j).excess_pct):
            notifier.send(a)
    except Exception as e:     # an alert problem is logged, never a run failure
        log(f"alerts: could not build ({type(e).__name__}: {e})")


if __name__ == "__main__":
    sys.exit(main())
