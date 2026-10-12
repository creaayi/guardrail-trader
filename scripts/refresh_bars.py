"""Fill the daily-bar cache (data/bars.sqlite) for the universe and hedge ETFs. Weekend batch job.

IBKR allows ~60 historical-data requests per 10 minutes: one request every --pace seconds (10.5 s),
least recently refreshed instruments first, at most --max-requests per run, so the job can be split
across several runs ("par lots"). A first fill asks for 1 year, later ones for 2 months (overlap is
upserted). Prices are stored in currency units (LSE pence converted). Errors are recorded per
instrument and the batch continues; nothing is invented for a failed instrument.

Uses IBKR_CLIENT_ID + 1, so it never collides with a bot run's connection.

  .venv/bin/python scripts/refresh_bars.py [--max-requests 1000] [--pace 10.5]
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from datetime import date

from guardrail_trader.bars import BarCache, from_ibkr


def duration(latest: str | None, today: date) -> str:
    """1 year for a first fill or a long gap, else 2 months of overlap."""
    if latest is None or (today - date.fromisoformat(latest)).days > 45:
        return "1 Y"
    return "2 M"


def main() -> int:
    from guardrail_trader.broker import IBKRBroker
    from guardrail_trader.config import load_ibkr_config
    from guardrail_trader.risk import load_risk_config

    ap = argparse.ArgumentParser()
    ap.add_argument("--max-requests", type=int, default=1000)
    ap.add_argument("--pace", type=float, default=10.5)
    args = ap.parse_args()

    cfg = load_risk_config()
    insts = {**cfg.universe, **cfg.hedge}
    cache = BarCache()
    ib_cfg = load_ibkr_config()
    ok = failed = 0
    try:
        with IBKRBroker(replace(ib_cfg, client_id=ib_cfg.client_id + 1), readonly=True) as broker:
            broker.ensure_ready()
            for key in cache.refresh_order(list(insts))[:args.max_requests]:
                inst = insts[key]
                try:
                    c = broker.stock(inst.symbol, inst.exchange, inst.currency)
                    raw = broker.ib.reqHistoricalData(
                        c, endDateTime="", durationStr=duration(cache.latest_date(key), date.today()),
                        barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1, timeout=60)
                    if not raw:
                        raise ValueError("no bars returned")
                    cache.upsert(key, from_ibkr(raw, broker.magnifier(c)))
                    cache.mark(key, "ok")
                    ok += 1
                except Exception as e:   # one bad instrument must not stop the batch
                    cache.mark(key, f"error: {e!r}"[:300])
                    failed += 1
                    print(f"{key}: {e!r}", flush=True)
                time.sleep(args.pace)
    finally:
        cache.close()
    print(f"bars refreshed: {ok} ok, {failed} failed, {len(insts)} instruments in total", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
