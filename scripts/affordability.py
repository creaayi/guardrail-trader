"""Which allowlisted tickers can the bot actually buy? (read-only)

With whole shares, 1 share must cost <= max_position_pct x capital (in base currency).
Writes data/affordability.csv and prints a summary.

  .venv/bin/python scripts/affordability.py
"""
from __future__ import annotations

import csv
import math
import sys
import time

from guardrail_trader.broker import IBKRBroker
from guardrail_trader.broker.ibkr import _num, stock_contract, to_major
from guardrail_trader.config import PROJECT_ROOT, load_ibkr_config
from guardrail_trader.risk import load_risk_config

BATCH = 50
OUT = PROJECT_ROOT / "data" / "affordability.csv"


def main() -> int:
    cfg = load_risk_config()
    cap_base = cfg.capital * cfg.max_position_pct / 100
    picks_file = PROJECT_ROOT / "config/universe/my_picks.csv"
    picks = {r["symbol"] for r in csv.DictReader(open(picks_file))} if picks_file.exists() else set()
    insts = sorted(cfg.universe.values(), key=lambda i: i.symbol)
    rows = []

    with IBKRBroker(load_ibkr_config(), readonly=True) as b:
        fx = {c: b.fx_to_base(c, cfg.base_currency) for c in {i.currency for i in insts}}
        for n in range(0, len(insts), BATCH):
            batch = insts[n:n + BATCH]
            contracts = [stock_contract(i.symbol, i.exchange, i.currency) for i in batch]
            b.ib.qualifyContracts(*contracts)
            live = [(i, c) for i, c in zip(batch, contracts) if c.conId]
            tickers = [(i, c, b.ib.reqMktData(c)) for i, c in live]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                b.ib.sleep(0.5)
                if all(not math.isnan(_num(t.marketPrice())) or not math.isnan(_num(t.close)) for _, _, t in tickers):
                    break
            for i, c, t in tickers:
                b.ib.cancelMktData(c)
                px = _num(t.marketPrice())
                px = to_major(px if not math.isnan(px) else _num(t.close), b.magnifier(c))
                rows.append((i, px))
            for i in batch:
                if i not in [x for x, _ in live]:
                    rows.append((i, math.nan))
            print(f"  priced {min(n + BATCH, len(insts))}/{len(insts)}", flush=True)

    OUT.parent.mkdir(exist_ok=True)
    affordable, too_dear, no_price = [], [], []
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "currency", "price", "price_base", "affordable"])
        for i, px in rows:
            pb = px * fx[i.currency] if not math.isnan(px) else math.nan
            ok = not math.isnan(pb) and pb <= cap_base
            w.writerow([i.symbol, i.currency, f"{px:.2f}", f"{pb:.2f}", ok])
            (affordable if ok else no_price if math.isnan(pb) else too_dear).append((i.symbol, px, pb))

    print(f"\nMax per holding: {cap_base:.2f} {cfg.base_currency} "
          f"({cfg.max_position_pct:g}% of {cfg.capital:g}); FX {fx}")
    print(f"Affordable (1 share fits): {len(affordable)} | too expensive: {len(too_dear)} | no price: {len(no_price)}")
    print("Affordable:", ", ".join(f"{s} ({p:.2f})" for s, p, _ in sorted(affordable, key=lambda x: x[2])))
    print("\nYour picks:")
    for s, px in sorted((i.symbol, px) for i, px in rows if i.symbol in picks):
        pb = px * fx["USD"]
        print(f"  {s:<6} US${px:>9.2f} = A${pb:>9.2f}  {'OK' if pb <= cap_base else 'too expensive for 1 share'}")
    if no_price:
        print("\nNo price:", ", ".join(s for s, _, _ in no_price))
    print(f"\nDetails: {OUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
